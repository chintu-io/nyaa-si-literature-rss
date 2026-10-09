import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


BUILDER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_feeds.py"
SPEC = importlib.util.spec_from_file_location("nyaa_feed_builder", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def item(torrent_id: str, title: str = "") -> dict:
    title = title or f"Release {torrent_id}"
    return {
        "id": torrent_id,
        "title": title,
        "link": f"https://nyaa.si/download/{torrent_id}.torrent",
        "pub_date": "Fri, 09 Oct 2026 00:00:00 GMT",
        "description": f'<a href="https://nyaa.si/view/{torrent_id}">View details</a>',
        "guid": f"https://nyaa.si/view/{torrent_id}",
    }


class NyaaListingParserTests(unittest.TestCase):
    def test_extracts_torrent_titles_ids_and_upload_timestamp(self) -> None:
        page = """
        <table class="table table-bordered torrent-list">
          <thead><tr><th>Name</th></tr></thead>
          <tbody>
            <tr>
              <td><a href="/?c=3_1" title="Literature">Literature</a></td>
              <td colspan="2">
                <a href="/view/123#comments" class="comments" title="2 comments">2</a>
                <a href="/view/123" title="A Light Novel [EPUB]">A Light Novel [EPUB]</a>
              </td>
              <td class="text-center" data-timestamp="1791500000">2026-10-08 22:53</td>
            </tr>
            <tr>
              <td colspan="2">
                <a href="/view/122" title="A Manga [CBZ]">A Manga [CBZ]</a>
              </td>
              <td class="text-center" data-timestamp="1791499000">2026-10-08 22:36</td>
            </tr>
          </tbody>
        </table>
        """
        parser = builder.NyaaListingParser()
        parser.feed(page)

        self.assertTrue(parser.saw_table)
        self.assertEqual(
            [(row["id"], row["title"], row["timestamp"]) for row in parser.rows],
            [
                ("123", "A Light Novel [EPUB]", 1791500000),
                ("122", "A Manga [CBZ]", 1791499000),
            ],
        )

    def test_fetch_listing_page_builds_rss_compatible_records(self) -> None:
        page = """
        <table class="torrent-list"><tbody><tr>
          <td colspan="2"><a href="/view/123" title="Novel [EPUB]">Novel [EPUB]</a></td>
          <td data-timestamp="1791500000">2026-10-08 22:53</td>
        </tr></tbody></table>
        """
        with patch.object(builder, "request_bytes", return_value=page.encode("utf-8")):
            rows = builder.fetch_listing_page(1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "123")
        self.assertEqual(rows[0]["title"], "Novel [EPUB]")
        self.assertEqual(rows[0]["link"], "https://nyaa.si/download/123.torrent")
        self.assertEqual(rows[0]["guid"], "https://nyaa.si/view/123")
        self.assertTrue(rows[0]["pub_date"])


class CatchUpScannerTests(unittest.TestCase):
    def test_stops_at_previous_frontier_without_importing_older_rows(self) -> None:
        pages = {
            1: [item("130"), item("120"), item("110"), item("100")],
        }
        with patch.object(builder, "fetch_listing_page", side_effect=lambda page: pages.get(page, [])):
            listing, next_state, caught_up, failed = builder.discover_listing_items(
                known_before_run={"100"},
                discovery_state={"baseline_anchor_id": "100"},
                target_anchor_id="100",
            )

        self.assertTrue(caught_up)
        self.assertFalse(failed)
        self.assertEqual([entry["id"] for entry in listing], ["130", "120", "110", "100"])
        self.assertIsNone(next_state.get("resume_after_id"))
        self.assertFalse(next_state.get("catch_up_pending", False))

    def test_cursor_resumes_and_reaches_old_frontier(self) -> None:
        pages = {
            1: [item("130"), item("120"), item("110")],
            2: [item("100"), item("90"), item("80")],
        }
        with patch.object(builder, "fetch_listing_page", side_effect=lambda page: pages.get(page, [])):
            listing, next_state, caught_up, failed = builder.discover_listing_items(
                known_before_run={"110", "100", "90"},
                discovery_state={
                    "resume_after_id": "110",
                    "resume_after_page": 1,
                    "baseline_anchor_id": "90",
                    "catch_up_pending": True,
                },
                target_anchor_id="90",
            )

        self.assertTrue(caught_up)
        self.assertFalse(failed)
        self.assertEqual([entry["id"] for entry in listing], ["130", "120", "110", "100", "90"])
        self.assertIsNone(next_state.get("resume_after_id"))
        self.assertFalse(next_state.get("catch_up_pending", False))

    def test_page_limit_saves_resumable_checkpoint(self) -> None:
        pages = {
            1: [item("30"), item("29"), item("28")],
            2: [item("27"), item("26"), item("25")],
        }
        with patch.object(builder, "MAX_DISCOVERY_PAGES_PER_RUN", 2):
            with patch.object(builder, "fetch_listing_page", side_effect=lambda page: pages.get(page, [])):
                listing, next_state, caught_up, failed = builder.discover_listing_items(
                    known_before_run=set(),
                    discovery_state={"baseline_anchor_id": "10"},
                    target_anchor_id="10",
                )

        self.assertFalse(caught_up)
        self.assertFalse(failed)
        self.assertEqual(len(listing), 6)
        self.assertEqual(next_state.get("resume_after_id"), "25")
        self.assertEqual(next_state.get("resume_after_page"), 2)
        self.assertTrue(next_state.get("catch_up_pending"))


if __name__ == "__main__":
    unittest.main()
