const { JSDOM } = require('jsdom');
const { Readability } = require('@mozilla/readability');
const { readFileSync } = require('fs');
const { join } = require('path');

const FETCH_TIMEOUT = 8000;
const CACHE_MAX = 500;
const CACHE_TTL = 4 * 60 * 60 * 1000;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';

const cache = new Map();
let snapshotContentCache = null;
let snapshotLoadTime = 0;

function loadSnapshotContent() {
  if (snapshotContentCache && Date.now() - snapshotLoadTime < 10 * 60 * 1000) {
    return snapshotContentCache;
  }
  try {
    const p = join(process.cwd(), 'rss_api_snapshot.json');
    const snap = JSON.parse(readFileSync(p, 'utf-8'));
    const urlMap = {};
    for (const src of (snap.sources || [])) {
      for (const it of (src.items || [])) {
        if (it.u && it.fc) {
          urlMap[it.u] = { title: it.t, content: it.fc, source: 'rss_fulltext' };
        }
      }
    }
    snapshotContentCache = urlMap;
    snapshotLoadTime = Date.now();
    console.log(`[article] Loaded snapshot content map: ${Object.keys(urlMap).length} entries with full text`);
    return urlMap;
  } catch (err) {
    console.error('[article] Failed to load snapshot:', err.message);
    return {};
  }
}

function cacheGet(key) {
  const entry = cache.get(key);
  if (!entry) return null;
  if (Date.now() - entry.ts > CACHE_TTL) { cache.delete(key); return null; }
  return entry.val;
}

function cacheSet(key, val) {
  if (cache.size >= CACHE_MAX) {
    const oldest = cache.keys().next().value;
    cache.delete(oldest);
  }
  cache.set(key, { val, ts: Date.now() });
}

function isSafeUrl(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    const host = u.hostname.toLowerCase();
    if (host === 'localhost' || host.endsWith('.local')) return false;
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) {
      const parts = host.split('.').map(Number);
      if (parts[0] === 10 || parts[0] === 127 || (parts[0] === 192 && parts[1] === 168) || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)) return false;
    }
    return true;
  } catch { return false; }
}

function extractYouTube(dom) {
  const doc = dom.window.document;
  const desc = doc.querySelector('meta[property="og:description"]');
  const title = doc.querySelector('meta[property="og:title"]');
  const videoId = doc.querySelector('meta[property="og:url"]');
  let content = '';
  if (videoId) {
    const match = videoId.content.match(/v=([a-zA-Z0-9_-]+)/);
    if (match) {
      content = `<iframe width="100%" height="400" src="https://www.youtube.com/embed/${match[1]}" frameborder="0" allowfullscreen></iframe>`;
    }
  }
  if (desc) content += `<p>${desc.content}</p>`;
  return {
    title: title ? title.content : '',
    content,
    source: 'youtube'
  };
}

function extractGitHub(dom) {
  const doc = dom.window.document;
  const title = doc.querySelector('meta[property="og:title"]');
  const desc = doc.querySelector('meta[property="og:description"]');
  const body = doc.querySelector('#readme, .repository-content, article');
  let content = '';
  if (body) content = body.innerHTML;
  else if (desc) content = `<p>${desc.content}</p>`;
  return {
    title: title ? title.content : '',
    content,
    source: 'github'
  };
}

function extractGeneric(dom) {
  const doc = dom.window.document;
  const titleEl = doc.querySelector('meta[property="og:title"]') || doc.querySelector('title');
  const title = titleEl ? (titleEl.content || titleEl.textContent) : '';

  const clone = doc.cloneNode(true);
  const reader = new Readability(clone);
  const article = reader.parse();

  if (article && article.content && article.textContent.length > 100) {
    return { title: article.title || title, content: article.content, source: 'readability' };
  }

  const metaDesc = doc.querySelector('meta[property="og:description"]') || doc.querySelector('meta[name="description"]');
  if (metaDesc && metaDesc.content) {
    return { title: title || '', content: `<p>${metaDesc.content}</p>`, source: 'meta' };
  }

  return null;
}

module.exports = async (req, res) => {
  // CORS 头：允许 GitHub Pages 跨域访问（与 api/rss.js 对齐），否则无内嵌全文文章的兜底通道被浏览器拦截
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  const url = req.query.url;
  if (!url || typeof url !== 'string') {
    return res.status(400).json({ ok: false, error: 'missing_url' });
  }

  if (!isSafeUrl(url)) {
    return res.status(400).json({ ok: false, error: 'invalid_url' });
  }

  const cached = cacheGet(url);
  if (cached) {
    res.setHeader('Cache-Control', 'public, max-age=14400, s-maxage=86400');
    return res.json(cached);
  }

  const snapshotMap = loadSnapshotContent();
  if (snapshotMap[url]) {
    const entry = snapshotMap[url];
    const out = { ok: true, url, title: entry.title, content: entry.content, source: entry.source };
    cacheSet(url, out);
    res.setHeader('Cache-Control', 'public, max-age=14400, s-maxage=86400');
    return res.json(out);
  }

  let dom;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT);
    const resp = await fetch(url, {
      signal: controller.signal,
      headers: { 'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml' }
    });
    clearTimeout(timer);

    if (!resp.ok) {
      return res.json({ ok: false, error: 'fetch_failed' });
    }

    const html = await resp.text();
    dom = new JSDOM(html, { url });
  } catch (e) {
    const errType = e.name === 'AbortError' ? 'timeout' : 'fetch_failed';
    return res.json({ ok: false, error: errType });
  }

  try {
    const host = new URL(url).hostname.toLowerCase();
    let result;

    if (host.includes('youtube.com') || host.includes('youtu.be')) {
      result = extractYouTube(dom);
    } else if (host.includes('github.com')) {
      result = extractGitHub(dom);
    } else {
      result = extractGeneric(dom);
    }

    if (!result || !result.content || result.content.length < 50) {
      return res.json({ ok: false, error: 'extraction_failed' });
    }

    const out = { ok: true, url, title: result.title, content: result.content, source: result.source };
    cacheSet(url, out);
    res.setHeader('Cache-Control', 'public, max-age=14400, s-maxage=86400');
    return res.json(out);
  } catch (e) {
    return res.json({ ok: false, error: 'extraction_failed' });
  }
};
