#!/usr/bin/env python3
"""Build separate RSS feeds by inspecting Nyaa Literature torrent file lists.

The source RSS exposes recent torrent metadata, but not the files inside each
torrent. This script fetches detail pages for uncached entries, classifies by
file extension, and publishes persistent, format-filtered RSS files.
"""
from __future__ import annotations

import concurrent.futures
import email.utils
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "classification-cache.json"
FEED_DIR = ROOT / "feeds"
DISCOVERY_STATE_PATH = ROOT / "data" / "discovery-state.json"

SOURCE_FEED_URL = "https://nyaa.si/?page=rss&c=3_1&f=0"
LISTING_URL = "https://nyaa.si/"
REPO_URL = "https://github.com/chintune/nyaa-si-literature-rss"
USER_AGENT = f"NyaaLiteratureFormatColorsRSS/1.0 (+{REPO_URL})"
MAX_WORKERS = 2
MIN_REQUEST_INTERVAL_SECONDS = 0.55
REQUEST_TIMEOUT_SECONDS = 20
MAX_STATE_ITEMS = 5000
MAX_FEED_ITEMS = 75
MAX_DISCOVERY_PAGES_PER_RUN = 20  # up to 1,500 older listing rows per catch-up step
MAX_CURSOR_LOOKUP_PAGES = MAX_DISCOVERY_PAGES_PER_RUN + 5  # allow modest page shifts between runs
MAX_DETAIL_CLASSIFICATIONS_PER_RUN = 250  # keep each Actions job within its time budget
CLASSIFIED_REFRESH_SECONDS = 30 * 24 * 60 * 60
UNKNOWN_REFRESH_SECONDS = 7 * 24 * 60 * 60
ERROR_RETRY_SECONDS = 6 * 60 * 60

FORMAT_EXTENSIONS = {
    "manga": {".cbz", ".cbr"},
    "novel": {".epub", ".pdf"},
    "audiobook": {".m4b"},
}
EXTENSION_RE = re.compile(r"\.(cbz|cbr|epub|pdf|m4b)(?=$|[^a-z0-9])", re.IGNORECASE)

_pacing_lock = threading.Lock()
_next_request_at = 0.0


class FileListParser(HTMLParser):
    """Collect visible text only from the torrent-file-list container."""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.found = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        classes = (attrs_map.get("class") or "").split()
        if not self.stack and not self.found and "torrent-file-list" in classes:
            self.found = True
            self.stack.append(tag)
            return
        if self.stack and tag not in self.VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        # Close the matching element and any unclosed descendants above it.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack and data:
            self.parts.append(data)

    @property
    def file_text(self) -> str:
        return " ".join(self.parts)


def request_bytes(url: str) -> bytes:
    global _next_request_at
    # Keep requests politely spaced even when two workers are enabled.
    with _pacing_lock:
        wait = _next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next_request_at = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(item: ET.Element, name: str) -> str:
    for child in item:
        if local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def torrent_id_from_link(link: str, guid: str = "") -> str | None:
    for candidate in (link, guid):
        match = re.search(r"(?:/view/|[?&](?:tid|id)=)(\d+)", candidate or "")
        if match:
            return match.group(1)
    return None


def parse_source_feed(xml_bytes: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RuntimeError(f"Nyaa RSS returned invalid XML: {exc}") from exc

    raw_items = [node for node in root.iter() if local_name(node.tag) == "item"]
    if not raw_items:
        raise RuntimeError("Nyaa RSS contained no <item> entries; refusing to replace existing feeds.")

    items: list[dict] = []
    for raw in raw_items:
        title = child_text(raw, "title")
        link = child_text(raw, "link")
        guid = child_text(raw, "guid")
        torrent_id = torrent_id_from_link(link, guid)
        if not torrent_id or not link:
            continue
        if link.startswith("/"):
            link = "https://nyaa.si" + link
        elif link.startswith("http://nyaa.si/"):
            link = "https://" + link[len("http://"):]
        items.append({
            "id": torrent_id,
            "title": title or f"Nyaa torrent {torrent_id}",
            "link": link,
            "pub_date": child_text(raw, "pubDate"),
            "description": child_text(raw, "description"),
            "guid": guid or f"https://nyaa.si/view/{torrent_id}",
        })
    if not items:
        raise RuntimeError("Could not identify any torrent IDs in Nyaa RSS items.")
    return items


def classify_detail_page(torrent_id: str) -> dict:
    url = f"https://nyaa.si/view/{torrent_id}"
    try:
        page = request_bytes(url).decode("utf-8", errors="replace")
    except Exception as exc:  # kept as a retryable status in the persistent cache
        return {
            "status": "error", "formats": [], "extensions": [],
            "reason": f"Could not fetch torrent detail page: {type(exc).__name__}: {exc}",
            "checked_at": int(time.time()),
        }

    parser = FileListParser()
    try:
        parser.feed(page)
    except Exception:
        pass

    if not parser.found:
        lowered = html.unescape(re.sub(r"<[^>]+>", " ", page)).lower()
        if "too many files to display" in lowered:
            reason = "Nyaa does not render the file list because the torrent has too many files."
        elif "file list is not available" in lowered:
            reason = "Nyaa reports that this torrent's file list is unavailable."
        else:
            reason = "No torrent-file-list container was found on the detail page."
        return {
            "status": "unknown", "formats": [], "extensions": [],
            "reason": reason, "checked_at": int(time.time()),
        }

    extensions_found = sorted({
        "." + match.group(1).lower()
        for match in EXTENSION_RE.finditer(parser.file_text)
    })
    formats = sorted(
        name for name, extensions in FORMAT_EXTENSIONS.items()
        if any(ext in extensions for ext in extensions_found)
    )
    reason = (
        "Matched file extensions: " + ", ".join(extensions_found)
        if extensions_found else
        "The visible file list had none of the supported extensions (.cbz, .cbr, .epub, .pdf, .m4b)."
    )
    return {
        "status": "classified" if formats else "unknown",
        "formats": formats,
        "extensions": extensions_found,
        "reason": reason,
        "checked_at": int(time.time()),
    }



class NyaaListingParser(HTMLParser):
    """Extract torrent rows and upload timestamps from Nyaa's HTML search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_torrent_table = False
        self.in_tbody = False
        self.current_row: dict | None = None
        self.current_anchor: dict | None = None
        self.rows: list[dict] = []
        self.saw_table = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = dict(attrs)
        classes = (attrs_map.get("class") or "").split()

        if tag == "table" and "torrent-list" in classes:
            self.in_torrent_table = True
            self.saw_table = True
            return
        if not self.in_torrent_table:
            return

        if tag == "tbody":
            self.in_tbody = True
            return
        if self.in_tbody and tag == "tr":
            self.current_row = {"id": "", "title": "", "timestamp": None}
            self.current_anchor = None
            return

        if self.current_row is not None:
            if tag == "td":
                timestamp = attrs_map.get("data-timestamp")
                if timestamp:
                    try:
                        self.current_row["timestamp"] = int(timestamp)
                    except (TypeError, ValueError):
                        pass
            elif tag == "a":
                href = attrs_map.get("href") or ""
                match = re.search(r"(?:^|/)view/(\d+)(?:#.*)?$", href)
                if match and "comments" not in classes:
                    self.current_anchor = {
                        "id": match.group(1),
                        "title": (attrs_map.get("title") or "").strip(),
                        "text": [],
                    }

    def handle_data(self, data: str) -> None:
        if self.current_anchor is not None and data:
            self.current_anchor["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_anchor is not None and self.current_row is not None:
            anchor = self.current_anchor
            self.current_row["id"] = anchor["id"]
            self.current_row["title"] = anchor["title"] or " ".join(anchor["text"]).strip()
            self.current_anchor = None
            return

        if tag == "tr" and self.current_row is not None:
            if self.current_row.get("id"):
                self.rows.append(self.current_row)
            self.current_row = None
            self.current_anchor = None
        elif tag == "tbody" and self.in_torrent_table:
            self.in_tbody = False
        elif tag == "table" and self.in_torrent_table:
            self.in_torrent_table = False
            self.in_tbody = False


def listing_page_url(page_number: int) -> str:
    query = urllib.parse.urlencode({
        "f": "0",
        "c": "3_1",
        "q": "",
        "p": str(page_number),
        "s": "id",
        "o": "desc",
    })
    return f"{LISTING_URL}?{query}"


def fetch_listing_page(page_number: int) -> list[dict]:
    """Fetch one paginated HTML results page; fail loudly if HTML changed."""
    url = listing_page_url(page_number)
    page_html = request_bytes(url).decode("utf-8", errors="replace")
    parser = NyaaListingParser()
    parser.feed(page_html)
    if not parser.saw_table:
        raise RuntimeError(
            f"Nyaa listing page {page_number} did not contain a torrent-list table. "
            "The site may be unavailable or its HTML structure may have changed."
        )

    results: list[dict] = []
    seen_ids: set[str] = set()
    for row in parser.rows:
        torrent_id = str(row["id"])
        if torrent_id in seen_ids:
            continue
        seen_ids.add(torrent_id)
        timestamp = row.get("timestamp")
        pub_date = email.utils.formatdate(timestamp, usegmt=True) if timestamp else ""
        title = (row.get("title") or "").strip() or f"Nyaa torrent {torrent_id}"
        detail_url = f"https://nyaa.si/view/{torrent_id}"
        results.append({
            "id": torrent_id,
            "title": title,
            "link": f"https://nyaa.si/download/{torrent_id}.torrent",
            "pub_date": pub_date,
            "description": f'<a href="{detail_url}">View torrent details on Nyaa.si</a>',
            "guid": detail_url,
        })
    return results


def load_discovery_state() -> dict:
    try:
        value = json.loads(DISCOVERY_STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_discovery_state(value: dict) -> None:
    DISCOVERY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_STATE_PATH.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def discover_listing_items(
    known_before_run: set[str],
    discovery_state: dict,
    target_anchor_id: str,
) -> tuple[list[dict], dict, bool, bool]:
    """
    Scan newest-first Nyaa HTML pages up to the previous run's frontier.

    Normally this only needs one or two pages. If a run falls behind far enough
    to hit the page safety limit, save a resume_after_id checkpoint. On the next
    run, locate that checkpoint from page 1, then advance up to
    MAX_DISCOVERY_PAGES_PER_RUN pages deeper toward the prior frontier.

    Returns (listing_items, next_state, caught_up, scan_failed).
    """
    checkpoint_id = str(discovery_state.get("resume_after_id") or "")
    checkpoint_found = not bool(checkpoint_id)
    checkpoint_page: int | None = None
    scanned_rows: dict[str, dict] = {}
    last_page_items: list[dict] = []
    pages_scanned = 0
    pages_after_checkpoint = 0
    caught_up = False
    scan_failed = False
    reached_end = False
    stopped_on_known_boundary = False

    # Without a cursor, at most 20 pages are needed. With a cursor, first find
    # the previously scanned anchor (which may shift a little as new uploads
    # arrive), then scan another bounded batch of older pages.
    max_pages_without_cursor = MAX_DISCOVERY_PAGES_PER_RUN
    previous_cursor_page = int(discovery_state.get("resume_after_page") or MAX_DISCOVERY_PAGES_PER_RUN)
    max_pages_before_cursor = max(MAX_CURSOR_LOOKUP_PAGES, previous_cursor_page + 5)

    for page_number in range(1, max_pages_without_cursor + 1 if not checkpoint_id else max_pages_before_cursor + MAX_DISCOVERY_PAGES_PER_RUN + 1):
        try:
            page_items = fetch_listing_page(page_number)
        except Exception as exc:
            scan_failed = True
            print(
                f"WARNING: catch-up scan stopped at page {page_number}: "
                f"{type(exc).__name__}: {exc}"
            )
            break

        if page_number == 1 and not page_items:
            scan_failed = True
            print("WARNING: Nyaa listing page 1 had no torrent rows; preserving discovery cursor.")
            break
        if not page_items:
            reached_end = True
            print(f"Reached the end of Nyaa listings at page {page_number}.")
            caught_up = True
            break

        pages_scanned = page_number
        last_page_items = page_items
        page_ids = [item["id"] for item in page_items]
        all_previously_known = all(torrent_id in known_before_run for torrent_id in page_ids)

        # Capture rows through the target frontier only. The remainder of the
        # page is older than the previous run's boundary and isn't part of this
        # catch-up window.
        anchor_index = next(
            (index for index, item in enumerate(page_items) if item["id"] == target_anchor_id),
            None,
        )
        if anchor_index is not None:
            items_to_add = page_items[:anchor_index + 1]
            caught_up = True
        else:
            items_to_add = page_items

        for item in items_to_add:
            scanned_rows[item["id"]] = item

        if checkpoint_id and not checkpoint_found and checkpoint_id in page_ids:
            checkpoint_found = True
            checkpoint_page = page_number
            print(f"Found catch-up checkpoint torrent #{checkpoint_id} on page {page_number}.")

        if caught_up:
            print(f"Reached previous discovery frontier at torrent #{target_anchor_id}.")
            break

        if checkpoint_id:
            if not checkpoint_found:
                if page_number >= max_pages_before_cursor:
                    print(
                        f"WARNING: checkpoint #{checkpoint_id} wasn't found within "
                        f"{max_pages_before_cursor} pages; keeping it for the next run."
                    )
                    break
            elif checkpoint_page is not None and page_number > checkpoint_page:
                pages_after_checkpoint += 1
                # If the old anchor was deleted from Nyaa, a fully-known page
                # beyond the checkpoint is a safe boundary. Do not use this
                # shortcut before the checkpoint, or it could skip pending history.
                if all_previously_known:
                    stopped_on_known_boundary = True
                    caught_up = True
                    print(
                        f"Reached a fully-known page ({page_number}) after the checkpoint; "
                        "no unseen listings remain in the current catch-up window."
                    )
                    break
                if pages_after_checkpoint >= MAX_DISCOVERY_PAGES_PER_RUN:
                    print(
                        f"Catch-up advanced {pages_after_checkpoint} pages beyond its checkpoint; "
                        "saving a new checkpoint for the next run."
                    )
                    break
        else:
            if all_previously_known:
                stopped_on_known_boundary = True
                caught_up = True
                print(
                    f"Reached a fully-known page ({page_number}); "
                    "the source window has been caught up."
                )
                break
            if page_number >= max_pages_without_cursor:
                print(
                    f"Page safety limit reached at page {page_number}; "
                    "saving a checkpoint for the next run."
                )
                break

    next_state = dict(discovery_state)
    next_state["last_scan_at"] = int(time.time())
    next_state["last_scan_pages"] = pages_scanned

    if scan_failed:
        # Keep both the target frontier and any resume checkpoint untouched.
        pass
    elif caught_up or reached_end or stopped_on_known_boundary:
        next_state["resume_after_id"] = None
        next_state["resume_after_page"] = None
        next_state["catch_up_pending"] = False
    else:
        if last_page_items:
            # Advance the checkpoint only if its previous anchor was found. If
            # it wasn't found, advancing it could skip an unscanned gap.
            if not checkpoint_id or checkpoint_found:
                next_state["resume_after_id"] = last_page_items[-1]["id"]
                next_state["resume_after_page"] = pages_scanned
            else:
                next_state["resume_after_id"] = checkpoint_id
                # If new uploads pushed the anchor beyond the lookup window,
                # expand the next lookup range gradually so the cursor remains reachable.
                next_state["resume_after_page"] = pages_scanned + 5
        next_state["catch_up_pending"] = True

    return list(scanned_rows.values()), next_state, caught_up, scan_failed


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def should_check(entry: dict, now: int) -> bool:
    checked = int(entry.get("checked_at") or 0)
    if not checked:
        return True
    status = entry.get("status")
    if status == "error":
        return now - checked >= ERROR_RETRY_SECONDS
    if status == "unknown":
        return now - checked >= UNKNOWN_REFRESH_SECONDS
    return now - checked >= CLASSIFIED_REFRESH_SECONDS


def parse_pub_timestamp(pub_date: str, fallback: int) -> int:
    try:
        parsed = email.utils.parsedate_to_datetime(pub_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return fallback


def update_item_metadata(entry: dict, item: dict, now: int) -> None:
    for key in ("title", "link", "pub_date", "description", "guid"):
        if item.get(key):
            entry[key] = item[key]
    entry["last_seen"] = now
    entry.setdefault("first_seen", now)


def make_rss_item(parent: ET.Element, entry: dict, feed_format: str) -> None:
    node = ET.SubElement(parent, "item")
    ET.SubElement(node, "title").text = entry.get("title", "")
    ET.SubElement(node, "link").text = entry.get("link", "")
    ET.SubElement(node, "guid", {"isPermaLink": "false"}).text = str(entry["id"])
    pub_date = entry.get("pub_date") or email.utils.formatdate(
        int(entry.get("first_seen") or time.time()), usegmt=True
    )
    ET.SubElement(node, "pubDate").text = pub_date

    description = entry.get("description", "").strip()
    detected = ", ".join(entry.get("extensions", [])) or "unknown file types"
    suffix = f"Format detected from torrent file list: {feed_format}; extensions: {detected}."
    if description:
        description += "\n\n" + suffix
    else:
        description = suffix
    ET.SubElement(node, "description").text = description
    ET.SubElement(node, "category").text = feed_format
    ET.SubElement(node, "source", {"url": SOURCE_FEED_URL}).text = "Nyaa Literature / English-translated"


def write_feed(filename: str, feed_format: str, entries: list[dict], now: int) -> None:
    channel = ET.Element("channel")
    feed_title = {
        "manga": "Nyaa Literature — Manga (CBZ/CBR)",
        "novel": "Nyaa Literature — Novels (EPUB/PDF)",
        "audiobook": "Nyaa Literature — Audiobooks (M4B)",
    }[feed_format]
    ET.SubElement(channel, "title").text = feed_title
    ET.SubElement(channel, "link").text = "https://nyaa.si/?f=0&c=3_1&q="
    ET.SubElement(channel, "description").text = (
        f"Filtered from Nyaa Literature / English-translated by inspecting torrent file lists. "
        f"This feed includes items containing {', '.join(sorted(FORMAT_EXTENSIONS[feed_format]))}."
    )
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = email.utils.formatdate(now, usegmt=True)
    ET.SubElement(channel, "ttl").text = "30"
    for entry in entries[:MAX_FEED_ITEMS]:
        make_rss_item(channel, entry, feed_format)

    rss = ET.Element("rss", {"version": "2.0"})
    rss.append(channel)
    xml_bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    (FEED_DIR / filename).write_bytes(xml_bytes + b"\n")


def main() -> None:
    now = int(time.time())
    print(f"Fetching source RSS: {SOURCE_FEED_URL}")
    source_items = parse_source_feed(request_bytes(SOURCE_FEED_URL))

    state = load_state()
    known_before_run = set(state.keys())
    discovery_state = load_discovery_state()

    # On the first run after this upgrade, infer the previous frontier from
    # the most recently seen cache cohort. That avoids crawling old history
    # just because this new discovery-state file doesn't exist yet.
    target_anchor_id = str(discovery_state.get("baseline_anchor_id") or "")
    if not target_anchor_id and state:
        seen_times = [
            int(entry.get("last_seen") or entry.get("first_seen") or 0)
            for entry in state.values()
        ]
        newest_seen = max(seen_times, default=0)
        recent_cohort = [
            entry for entry in state.values()
            if int(entry.get("last_seen") or entry.get("first_seen") or 0) >= newest_seen - 60
            and entry.get("id")
        ]
        if recent_cohort:
            oldest_recent = min(
                recent_cohort,
                key=lambda entry: parse_pub_timestamp(
                    entry.get("pub_date", ""), int(entry.get("id") or 0)
                ),
            )
            target_anchor_id = str(oldest_recent["id"])

    # If this is a brand-new install, use the oldest entry in the current RSS
    # window as the initial frontier rather than backfilling arbitrary history.
    if not target_anchor_id:
        oldest_source_item = min(
            source_items,
            key=lambda item: parse_pub_timestamp(item.get("pub_date", ""), int(item["id"])),
        )
        target_anchor_id = oldest_source_item["id"]

    listing_items, next_discovery_state, caught_up, scan_failed = discover_listing_items(
        known_before_run, discovery_state, target_anchor_id
    )

    # Only advance the regular frontier after a successful scan actually reaches
    # the previous frontier (or a known boundary). If scanning failed, leave the
    # old frontier intact so the next successful run retries the same window.
    if caught_up and not scan_failed:
        oldest_source_item = min(
            source_items,
            key=lambda item: parse_pub_timestamp(item.get("pub_date", ""), int(item["id"])),
        )
        next_discovery_state["baseline_anchor_id"] = oldest_source_item["id"]
    elif target_anchor_id:
        next_discovery_state["baseline_anchor_id"] = target_anchor_id

    # Prefer richer RSS metadata for RSS entries; add paginated HTML rows for
    # listings older than the source RSS window that the catch-up scan finds.
    merged_items: dict[str, dict] = {item["id"]: item for item in source_items}
    for item in listing_items:
        existing = merged_items.get(item["id"])
        if existing is None:
            merged_items[item["id"]] = item
        else:
            for key, value in item.items():
                if not existing.get(key) and value:
                    existing[key] = value

    source_ids = {item["id"] for item in source_items}
    check_ids: list[str] = []
    checked_ids: set[str] = set()

    for item in merged_items.values():
        torrent_id = item["id"]
        existed_before = torrent_id in state
        entry = state.setdefault(torrent_id, {
            "id": torrent_id, "formats": [], "extensions": [], "status": "",
            "reason": "", "checked_at": 0,
        })

        if torrent_id in source_ids:
            # Source RSS metadata is refreshed each run, as before.
            update_item_metadata(entry, item, now)
        elif not existed_before:
            # Set last_seen for new history discovered through HTML pages.
            update_item_metadata(entry, item, now)
        else:
            # Refresh missing metadata without making old history appear newly seen.
            for key in ("title", "link", "pub_date", "description", "guid"):
                if item.get(key) and not entry.get(key):
                    entry[key] = item[key]

        if torrent_id not in checked_ids and should_check(entry, now):
            checked_ids.add(torrent_id)
            check_ids.append(torrent_id)

    # If a previous run discovered more listings than it could classify, resume
    # these pending records even if they have since fallen out of the source RSS.
    for torrent_id, entry in state.items():
        if not entry.get("status") and torrent_id not in checked_ids:
            if should_check(entry, now):
                checked_ids.add(torrent_id)
                check_ids.append(torrent_id)

    # Process newest items first and bound detail-page requests per job. Remaining
    # blank-status records persist in the cache and are picked up on later runs.
    check_ids.sort(
        key=lambda torrent_id: parse_pub_timestamp(
            state[torrent_id].get("pub_date", ""),
            int(state[torrent_id].get("first_seen") or state[torrent_id].get("id") or 0),
        ),
        reverse=True,
    )
    if len(check_ids) > MAX_DETAIL_CLASSIFICATIONS_PER_RUN:
        print(
            f"Classification batch capped at {MAX_DETAIL_CLASSIFICATIONS_PER_RUN}; "
            f"{len(check_ids) - MAX_DETAIL_CLASSIFICATIONS_PER_RUN} pending item(s) "
            "will be handled on later runs."
        )
        check_ids = check_ids[:MAX_DETAIL_CLASSIFICATIONS_PER_RUN]

    print(
        f"Source RSS items: {len(source_items)}; listing rows scanned: {len(listing_items)}; "
        f"detail pages to inspect this run: {len(check_ids)}"
    )
    if check_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(classify_detail_page, torrent_id): torrent_id
                for torrent_id in check_ids
            }
            for future in concurrent.futures.as_completed(futures):
                torrent_id = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "status": "error", "formats": [], "extensions": [],
                        "reason": f"Unhandled classifier error: {type(exc).__name__}: {exc}",
                        "checked_at": int(time.time()),
                    }
                state[torrent_id].update(result)
                state[torrent_id]["id"] = torrent_id
                print(f"  {torrent_id}: {result['status']} {result.get('formats', [])}")

    # Keep the cache finite while retaining far more than one RSS window.
    if len(state) > MAX_STATE_ITEMS:
        keep = sorted(
            state.values(),
            key=lambda entry: int(entry.get("last_seen") or entry.get("first_seen") or 0),
            reverse=True,
        )[:MAX_STATE_ITEMS]
        state = {str(entry["id"]): entry for entry in keep}

    FEED_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    save_discovery_state(next_discovery_state)

    # Newest-first feeds. Every file-list category is assigned independently:
    # a mixed torrent can appear in more than one feed if it contains multiple formats.
    ordered = sorted(
        state.values(),
        key=lambda entry: parse_pub_timestamp(
            entry.get("pub_date", ""), int(entry.get("first_seen") or 0)
        ),
        reverse=True,
    )
    for feed_format, filename in (
        ("manga", "manga.xml"),
        ("novel", "novels.xml"),
        ("audiobook", "audiobooks.xml"),
    ):
        included = [
            entry for entry in ordered
            if feed_format in entry.get("formats", [])
            and entry.get("status") == "classified"
        ]
        write_feed(filename, feed_format, included, now)
        print(f"Wrote feeds/{filename}: {len(included[:MAX_FEED_ITEMS])} item(s)")

    counts = {
        name: sum(
            1 for entry in state.values()
            if entry.get("status") == "classified" and name in entry.get("formats", [])
        )
        for name in FORMAT_EXTENSIONS
    }
    print("Cache totals:", json.dumps(counts))
    if next_discovery_state.get("catch_up_pending"):
        print(
            "WARNING: catch-up is still pending. The next scheduled run will continue "
            "from the saved checkpoint."
        )


if __name__ == "__main__":
    main()
