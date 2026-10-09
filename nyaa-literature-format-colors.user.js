// ==UserScript==
// @name         Nyaa Literature Format Colors
// @namespace    https://nyaa.si/
// @version      1.0.0
// @description  Identify manga vs novels in Nyaa's English-translated Literature results by inspecting each torrent's file list.
// @match        https://nyaa.si/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

(() => {
  'use strict';

  const CATEGORY_ID = '3_1'; // Literature / English-translated
  const CACHE_KEY = 'nyaa-literature-format-colors:v1';
  const CACHE_TTL_MS = 180 * 24 * 60 * 60 * 1000;
  const UNKNOWN_TTL_MS = 24 * 60 * 60 * 1000;
  const CONCURRENCY = 2;
  const WORKER_DELAY_MS = 400;
  const REQUEST_TIMEOUT_MS = 15000;
  const SCRIPT_PREFIX = 'nyaa-ft';

  const TYPE_META = {
    manga: { label: 'MANGA', hint: 'File list contains .CBZ and/or .CBR', color: '#087f73', tint: 'rgba(8, 127, 115, 0.09)' },
    novel: { label: 'NOVEL', hint: 'File list contains .EPUB and/or .PDF', color: '#6651c9', tint: 'rgba(102, 81, 201, 0.10)' },
    mixed: { label: 'MIXED', hint: 'File list contains both manga and novel file types', color: '#b56a00', tint: 'rgba(181, 106, 0, 0.11)' },
    unknown: { label: 'UNKNOWN', hint: 'No matching file extension found, or Nyaa did not show the file list', color: '#777777', tint: 'rgba(119, 119, 119, 0.07)' },
    checking: { label: 'CHECKING…', hint: 'Reading the torrent file list in the background', color: '#737373', tint: 'rgba(119, 119, 119, 0.05)' },
  };

  // Only operate on the English-translated Literature results page.
  if (location.pathname !== '/' || new URLSearchParams(location.search).get('c') !== CATEGORY_ID) return;
  const table = document.querySelector('table.torrent-list');
  if (!table) return;
  addStyles();

  let cache = readCache();
  const rows = collectRows(table);
  if (!rows.length) return;

  const panel = makePanel();
  const wrapper = table.closest('.table-responsive') || table;
  wrapper.parentNode.insertBefore(panel, wrapper);

  const pending = [];
  let completed = 0;
  let failedCount = 0;

  // Apply saved classifications before making any requests.
  for (const item of rows) {
    const entry = getUsableCache(item.id);
    if (entry) {
      applyClassification(item, entry.type, entry.reason);
      completed++;
    } else {
      applyClassification(item, 'checking');
      pending.push(item);
    }
  }
  updatePanel();
  if (pending.length) runQueue(pending);

  panel.querySelector('[data-action="retry-unknown"]').addEventListener('click', () => {
    const retry = [];
    for (const item of rows) {
      if (item.row.dataset.nyaaFormatType === 'unknown') {
        delete cache[item.id];
        applyClassification(item, 'checking');
        retry.push(item);
      }
    }
    if (!retry.length) {
      setStatus('There are no UNKNOWN rows on this page to retry.');
      return;
    }
    saveCache();
    failedCount = 0;
    completed = rows.length - retry.length;
    setStatus(`Retrying ${retry.length} UNKNOWN item(s)…`);
    runQueue(retry);
  });

  function collectRows(torrentTable) {
    const result = [];
    for (const row of torrentTable.querySelectorAll('tbody tr')) {
      const titleLinks = Array.from(row.querySelectorAll('a[href*="/view/"]'))
        .filter(a => !a.classList.contains('comments'));
      const titleLink = titleLinks[titleLinks.length - 1];
      if (!titleLink) continue;
      let url;
      try { url = new URL(titleLink.getAttribute('href'), location.origin); } catch (_) { continue; }
      const match = url.pathname.match(/^\/view\/(\d+)/);
      if (!match) continue;
      result.push({ id: match[1], url: `${location.origin}/view/${match[1]}`, row, titleLink });
    }
    return result;
  }

  async function runQueue(items) {
    let cursor = 0;
    const workers = Array.from({ length: Math.min(CONCURRENCY, items.length) }, async () => {
      while (cursor < items.length) {
        const index = cursor++;
        const item = items[index];
        if (index > 0) await sleep(WORKER_DELAY_MS);
        try {
          const result = await inspectTorrent(item.url);
          if (result.type !== 'error') {
            cache[item.id] = { type: result.type, reason: result.reason || '', checkedAt: Date.now() };
            saveCache();
          } else {
            failedCount++;
          }
          applyClassification(item, result.type, result.reason);
        } catch (error) {
          failedCount++;
          applyClassification(item, 'unknown', 'Could not fetch this torrent page; this result was not cached. It can be retried later.');
          console.warn('[Nyaa Format Colors] Detail-page request failed:', item.id, error);
        } finally {
          completed++;
          updatePanel();
        }
      }
    });
    await Promise.all(workers);
    updatePanel('Finished');
  }

  async function inspectTorrent(url) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(url, {
        method: 'GET', credentials: 'same-origin', redirect: 'follow',
        signal: controller.signal, headers: { Accept: 'text/html' },
      });
      if (!response.ok) return { type: 'error', reason: `Nyaa returned HTTP ${response.status}; not cached.` };
      const html = await response.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const fileList = doc.querySelector('.torrent-file-list');
      if (!fileList) {
        const pageText = (doc.body?.innerText || '').toLowerCase();
        if (pageText.includes('too many files to display')) return { type: 'unknown', reason: 'Nyaa says there are too many files to display.' };
        if (pageText.includes('file list is not available')) return { type: 'unknown', reason: 'Nyaa does not provide a file list for this torrent.' };
        return { type: 'unknown', reason: 'No visible file list found on the torrent detail page.' };
      }
      const files = Array.from(fileList.querySelectorAll('li')).filter(li =>
        Array.from(li.children).some(child => child.classList?.contains('file-size'))
      );
      const names = files.map(fileNameFromListItem).filter(Boolean);
      const hasManga = names.some(name => /\.(?:cbz|cbr)$/i.test(name));
      const hasNovel = names.some(name => /\.(?:epub|pdf)$/i.test(name));
      if (hasManga && hasNovel) return { type: 'mixed', reason: 'File list contains both manga and novel file types.' };
      if (hasManga) return { type: 'manga', reason: 'Detected .CBZ and/or .CBR file(s).' };
      if (hasNovel) return { type: 'novel', reason: 'Detected .EPUB and/or .PDF file(s).' };
      return { type: 'unknown', reason: names.length ? 'No .CBZ, .CBR, .EPUB, or .PDF files were found.' : 'No individual file names could be read.' };
    } catch (_) {
      return { type: 'error', reason: 'The detail page could not be inspected.' };
    } finally {
      clearTimeout(timeout);
    }
  }

  function fileNameFromListItem(li) {
    return Array.from(li.childNodes)
      .filter(node => node.nodeType === Node.TEXT_NODE)
      .map(node => node.textContent || '')
      .join(' ')
      .trim();
  }

  function applyClassification(item, type, reason = '') {
    const safeType = type === 'error' ? 'unknown' : type;
    const meta = TYPE_META[safeType] || TYPE_META.unknown;
    item.row.classList.remove(...['manga', 'novel', 'mixed', 'unknown', 'checking'].map(t => `${SCRIPT_PREFIX}-${t}`));
    item.row.classList.add(`${SCRIPT_PREFIX}-${safeType}`);
    item.row.dataset.nyaaFormatType = safeType;

    let badge = item.titleLink.parentElement.querySelector(':scope > .nyaa-ft-badge');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'nyaa-ft-badge';
      item.titleLink.insertAdjacentElement('afterend', badge);
    }
    badge.textContent = meta.label;
    badge.className = `nyaa-ft-badge nyaa-ft-badge-${safeType}`;
    badge.title = reason ? `${meta.hint}. ${reason}` : meta.hint;
    badge.setAttribute('aria-label', `Detected format: ${meta.label}`);
    item.row.style.setProperty('--nyaa-ft-color', meta.color);
    item.row.style.setProperty('--nyaa-ft-tint', meta.tint);
  }

  function makePanel() {
    const el = document.createElement('section');
    el.id = 'nyaa-literature-format-colors-panel';
    el.innerHTML = `
      <div class="nyaa-ft-panel-top"><strong>Nyaa format colors</strong><span class="nyaa-ft-status" aria-live="polite">Preparing classifications…</span></div>
      <div class="nyaa-ft-legend">
        <span><i class="nyaa-ft-swatch swatch-manga"></i>Manga <small>.CBZ / .CBR</small></span>
        <span><i class="nyaa-ft-swatch swatch-novel"></i>Novel <small>.EPUB / .PDF</small></span>
        <span><i class="nyaa-ft-swatch swatch-mixed"></i>Mixed <small>both types</small></span>
        <span><i class="nyaa-ft-swatch swatch-unknown"></i>Unknown <small>not identified</small></span>
      </div>
      <div class="nyaa-ft-panel-bottom"><span class="nyaa-ft-note">Checks detail pages in the background; no tabs are opened. Results are cached in this browser.</span><button type="button" class="nyaa-ft-retry" data-action="retry-unknown">Retry UNKNOWN</button></div>`;
    return el;
  }

  function updatePanel(prefix = 'Scan') {
    const status = panel.querySelector('.nyaa-ft-status');
    const count = type => rows.filter(x => x.row.dataset.nyaaFormatType === type).length;
    const manga = count('manga'), novel = count('novel'), mixed = count('mixed'), unknown = count('unknown');
    const checking = rows.length - manga - novel - mixed - unknown;
    if (checking === 0) status.textContent = `${rows.length} rows · ${manga} manga · ${novel} novels · ${mixed} mixed · ${unknown} unknown${failedCount ? ` · ${failedCount} request failure(s)` : ''}`;
    else status.textContent = `${prefix}: ${completed}/${rows.length} reviewed · ${checking} pending · ${manga} manga · ${novel} novels · ${mixed} mixed`;
  }
  function setStatus(message) { panel.querySelector('.nyaa-ft-status').textContent = message; }
  function readCache() {
    try {
      const data = JSON.parse(localStorage.getItem(CACHE_KEY) || '{}');
      return data && typeof data === 'object' && !Array.isArray(data) ? data : {};
    } catch (_) { return {}; }
  }
  function getUsableCache(id) {
    const entry = cache[id];
    if (!entry || !entry.type || !entry.checkedAt) return null;
    const age = Date.now() - entry.checkedAt;
    const ttl = entry.type === 'unknown' ? UNKNOWN_TTL_MS : CACHE_TTL_MS;
    return age >= 0 && age < ttl ? entry : null;
  }
  function saveCache() {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(cache)); }
    catch (error) { console.warn('[Nyaa Format Colors] Could not save local cache:', error); }
  }
  function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

  function addStyles() {
    const style = document.createElement('style');
    style.textContent = `
      #nyaa-literature-format-colors-panel { margin:0 0 12px;padding:10px 12px;border:1px solid var(--bs-border-color,#d8d8d8);border-radius:5px;background:var(--bs-tertiary-bg,rgba(127,127,127,.05));color:inherit;font-size:13px; }
      #nyaa-literature-format-colors-panel .nyaa-ft-panel-top,#nyaa-literature-format-colors-panel .nyaa-ft-panel-bottom { display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 14px; }
      #nyaa-literature-format-colors-panel .nyaa-ft-status { opacity:.82; }
      #nyaa-literature-format-colors-panel .nyaa-ft-legend { display:flex;align-items:center;flex-wrap:wrap;gap:8px 18px;margin:9px 0; }
      #nyaa-literature-format-colors-panel .nyaa-ft-legend>span { display:inline-flex;align-items:center;gap:5px; }
      #nyaa-literature-format-colors-panel .nyaa-ft-legend small { opacity:.72; }
      #nyaa-literature-format-colors-panel .nyaa-ft-swatch { display:inline-block;width:10px;height:10px;border-radius:2px; }
      #nyaa-literature-format-colors-panel .swatch-manga { background:#087f73; } #nyaa-literature-format-colors-panel .swatch-novel { background:#6651c9; }
      #nyaa-literature-format-colors-panel .swatch-mixed { background:#b56a00; } #nyaa-literature-format-colors-panel .swatch-unknown { background:#777; }
      #nyaa-literature-format-colors-panel .nyaa-ft-note { opacity:.72;font-size:12px; }
      #nyaa-literature-format-colors-panel .nyaa-ft-retry { border:1px solid #888;border-radius:4px;padding:3px 8px;color:inherit;background:transparent;cursor:pointer; }
      .torrent-list .nyaa-ft-badge { display:inline-block;vertical-align:1px;margin-left:7px;padding:2px 5px;border:1px solid currentColor;border-radius:3px;font-size:10px;line-height:1.2;font-weight:700;letter-spacing:.035em;white-space:nowrap; }
      .torrent-list .nyaa-ft-badge-manga { color:#087f73;background:#d8f2ee; } .torrent-list .nyaa-ft-badge-novel { color:#5944b4;background:#e9e4ff; }
      .torrent-list .nyaa-ft-badge-mixed { color:#925400;background:#ffedc8; } .torrent-list .nyaa-ft-badge-unknown,.torrent-list .nyaa-ft-badge-checking { color:#666;background:#eee; }
      .torrent-list tbody tr.nyaa-ft-manga>td:first-child { border-left:5px solid #087f73!important; }
      .torrent-list tbody tr.nyaa-ft-novel>td:first-child { border-left:5px solid #6651c9!important; }
      .torrent-list tbody tr.nyaa-ft-mixed>td:first-child { border-left:5px solid #b56a00!important; }
      .torrent-list tbody tr.nyaa-ft-unknown>td:first-child,.torrent-list tbody tr.nyaa-ft-checking>td:first-child { border-left:5px solid #888!important; }
      /* Tint normal rows only, preserving Nyaa's native colored status rows. */
      .torrent-list tbody tr.nyaa-ft-manga.default>td { background-color:rgba(8,127,115,.075)!important; }
      .torrent-list tbody tr.nyaa-ft-novel.default>td { background-color:rgba(102,81,201,.085)!important; }
      .torrent-list tbody tr.nyaa-ft-mixed.default>td { background-color:rgba(181,106,0,.09)!important; }
      .torrent-list tbody tr.nyaa-ft-unknown.default>td,.torrent-list tbody tr.nyaa-ft-checking.default>td { background-color:rgba(119,119,119,.035)!important; }
      @media(max-width:700px) { #nyaa-literature-format-colors-panel .nyaa-ft-panel-top { align-items:flex-start; } #nyaa-literature-format-colors-panel .nyaa-ft-status { width:100%; } }
    `;
    document.head.appendChild(style);
  }
})();
