# Nyaa Literature Format Colors

A Tampermonkey userscript that classifies torrents on Nyaa's **Literature / English-translated** results page by reading the torrent detail page's file list.

## Install

1. Open [the userscript](https://raw.githubusercontent.com/chintune/Nyaa-Literature-Format-Colors/main/nyaa-literature-format-colors.user.js) after the script has been added to this repository.
2. Install it using Tampermonkey, or copy the script into a new Tampermonkey script and save.
3. Visit https://nyaa.si/?f=0&c=3_1&q=

## Classifications

- **MANGA** (teal): file list contains `.cbz` and/or `.cbr`
- **NOVEL** (purple): file list contains `.epub` and/or `.pdf`
- **MIXED** (amber): both types appear
- **UNKNOWN** (grey): no matching files were found or the file list could not be inspected

The script adds a format badge and a row indicator while leaving Nyaa's existing trust/remake indicators intact.

## How it works

The search results don't contain every torrent's full file list, so uncached entries are checked with background same-origin requests to their detail pages. It does not open new tabs. Requests are limited to two at a time, and successful classifications are cached in the browser for 180 days (unknown results for one day). The page includes a **Retry UNKNOWN** button for unknown items.

## Limitations

Extensions are heuristics, not perfect semantic classification. A PDF can be a manga scan, and some torrents may not expose a complete file list. Mixed-format torrents are marked **MIXED** rather than forced into one category.
