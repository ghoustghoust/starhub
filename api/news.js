// GET /api/news — AI news proxy for 36Kr and the Redis blog.
import { applyCors, rejectMethod, setPublicCache } from './_lib/http.js';

const MIRRORS = [
  'https://rsshub.ktachibana.party/36kr/information/AI',
  'https://rsshub.woodland.cafe/36kr/information/AI',
  'https://rsshub.rssforever.com/36kr/information/AI',
  'https://hub.slarker.me/36kr/information/AI',
];
const REDIS_FEED = 'https://redis.io/feed/';
const REDIS_LIMIT = 3;
const TTL = 10 * 60 * 1000;
const CDN_TTL = 600;
let cache = { t: 0, v: null };

const stripHtml = (value) => (value || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

async function fetchFeed(url) {
  const response = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (starhub)',
      Accept: 'application/rss+xml, application/xml, text/xml',
    },
    signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) throw new Error('http ' + response.status);
  return response.text();
}

function parseRss(xml) {
  const items = [];
  const blocks = xml.match(/<item>[\s\S]*?<\/item>/g) || [];
  for (const block of blocks) {
    const get = (tag) => {
      const match = block.match(new RegExp('<' + tag + '>([\\s\\S]*?)</' + tag + '>'));
      if (!match) return '';
      return match[1].replace(/^<!\[CDATA\[/, '').replace(/\]\]>$/, '').trim();
    };
    const title = stripHtml(get('title'));
    const link = get('link');
    const summary = stripHtml(get('description')).slice(0, 200);
    const published = get('pubDate');
    const date = published ? new Date(published) : null;
    if (title && link) {
      items.push({ title, link, summary, publishedAt: date && !Number.isNaN(date.getTime()) ? date.toISOString() : '' });
    }
  }
  return items;
}

export default async function handler(req, res) {
  const allowed = applyCors(req, res, { methods: 'GET, OPTIONS', allowNoOrigin: true, publicCors: true });
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'GET') { rejectMethod(res, 'GET, OPTIONS'); return; }
  if (!allowed) { res.status(403).json({ error: 'Forbidden' }); return; }

  if (cache.v && Date.now() - cache.t < TTL) {
    setPublicCache(res, CDN_TTL, 300);
    res.status(200).json(cache.v);
    return;
  }

  const items = [];
  const sources = [];
  for (const url of MIRRORS) {
    try {
      const got = parseRss(await fetchFeed(url)).slice(0, 30).map((item) => ({ ...item, source: '36氪' }));
      if (got.length) { items.push(...got); sources.push('36氪'); break; }
    } catch { /* try the next mirror */ }
  }
  try {
    const got = parseRss(await fetchFeed(REDIS_FEED)).slice(0, REDIS_LIMIT).map((item) => ({ ...item, source: 'Redis' }));
    if (got.length) { items.push(...got); sources.push('Redis'); }
  } catch { /* optional source */ }

  if (!items.length) { res.status(502).json({ error: 'Upstream unavailable' }); return; }
  const body = { updated_at: new Date().toISOString(), sources, items };
  cache = { t: Date.now(), v: body };
  setPublicCache(res, CDN_TTL, 300);
  res.status(200).json(body);
}
