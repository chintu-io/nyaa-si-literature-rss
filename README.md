# Nyaa Literature Format Colors

A Tampermonkey userscript and generated RSS feeds for separating Manga, Novels, and Audiobooks from Nyaa's **Literature / English-translated** category.

## Categorized RSS feeds

Subscribe to these URLs in your RSS reader:

| Feed | Detected file extensions | RSS URL |
|---|---|---|
| Manga | `.cbz`, `.cbr` | [manga.xml](https://raw.githubusercontent.com/chintune/Nyaa-Literature-Format-Colors/main/feeds/manga.xml) |
| Novels | `.epub`, `.pdf` | [novels.xml](https://raw.githubusercontent.com/chintune/Nyaa-Literature-Format-Colors/main/feeds/novels.xml) |
| Audiobooks | `.m4b` | [audiobooks.xml](https://raw.githubusercontent.com/chintune/Nyaa-Literature-Format-Colors/main/feeds/audiobooks.xml) |

These feeds are generated from Nyaa's source feed:

`https://nyaa.si/?page=rss&c=3_1&f=0`

A scheduled GitHub Actions workflow refreshes the categorized feeds twice per hour. For each new torrent, it reads the torrent detail page's visible file list, stores the classification cache, and regenerates the feed files. Up to 200 recent matching entries are included per feed. Cached entries are retained between runs so items remain in the feeds after they leave Nyaa's limited recent RSS window.

**Mixed-format torrents can appear in multiple feeds.** For example, an upload that contains both CBZ and EPUB files appears in both Manga and Novels. A torrent containing an M4B file appears in Audiobooks.

### Important limitations

- Nyaa's source RSS only exposes its recent window. The workflow discovers torrents while they remain in that source window, so it is not a historical backfill of every torrent ever uploaded.
- `.pdf` is treated as a novel/document format as requested, but some PDFs are manga scans. File-extension classification is a useful heuristic rather than perfect content understanding.
- If Nyaa doesn't render a torrent's file list (for example, because it has too many files) or a request fails, the torrent is left out until it can be classified.
- Generated feeds live in this repository's `feeds/` directory. GitHub Actions must be enabled for automatic updates. You can start a refresh from the **Actions** tab using **Build categorized Nyaa RSS feeds → Run workflow**.

## Tampermonkey userscript

Install the [userscript](https://raw.githubusercontent.com/chintune/Nyaa-Literature-Format-Colors/main/nyaa-literature-format-colors.user.js) in Tampermonkey, then visit [Nyaa Literature / English-translated](https://nyaa.si/?f=0&c=3_1&q=).

The userscript inspects torrent detail pages in background requests (no tabs are opened) and colors each search-result row. Successful classifications are cached in your browser; use **Retry UNKNOWN** for entries that could not be identified.

## Format colors

- **Manga** (teal): `.cbz` and/or `.cbr`
- **Novels** (purple): `.epub` and/or `.pdf`
- **Audiobooks** (blue): `.m4b`
- **Mixed** (amber): multiple format families detected
- **Unknown** (grey): no supported extension found or the file list was unavailable

Nyaa's native trust/remake indicators are separate from the custom format classifications.
