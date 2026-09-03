// Vercel Serverless Function：AI 资讯流代理（36氪 + Redis 官方博客）
// 背景：36氪官方 RSS（36kr.com/feed*）有人机验证反爬墙，浏览器/服务器直连均被拦截；
//       且 RSSHub 公共镜像的 CORS 头不合法（回显固定字符串），前端无法直连。
//       故由本函数在服务端中转，输出干净 JSON；每条携带 source 标记。
//       Redis 官方博客（redis.io/feed/）可直连但同样需服务端中转解决 CORS。
// 用法：GET /api/news → { updated_at, sources: [...], items: [{title, link, summary, publishedAt, source}] }
// 防护：CORS 白名单（同 events.js）；GET 无敏感参数，无需密钥
// 缓存：结果 10 分钟 TTL（Vercel 实例内存）
const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

// RSSHub 公共镜像链（按可用性排序，依次尝试；部分镜像封数据中心 IP，故多备几个；
// 路由 /36kr/information/AI 为 36氪 AI 资讯流）
const MIRRORS = [
  'https://rsshub.ktachibana.party/36kr/information/AI',
  'https://rsshub.woodland.cafe/36kr/information/AI',
  'https://rsshub.rssforever.com/36kr/information/AI',
  'https://hub.slarker.me/36kr/information/AI',
];

// Redis 官方博客（内容以向量检索/LLM/Agent 为主，周更 2-3 篇，可直连）
const REDIS_FEED = 'https://redis.io/feed/';
const REDIS_LIMIT = 3;

const TTL = 10 * 60 * 1000;
let cache = { t: 0, v: null };

function stripHtml(s) {
  return (s || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

async function fetchFeed(url) {
  const r = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (starhub-refresh)',
      'Accept': 'application/rss+xml, application/xml, text/xml',
    },
    signal: AbortSignal.timeout(8000),
  });
  if (!r.ok) throw new Error('http ' + r.status);
  return r.text();
}

// RSS 2.0 极简解析（36氪源结构固定，无需完整 XML 解析器）
function parseRss(xml) {
  const items = [];
  const blocks = xml.match(/<item>[\s\S]*?<\/item>/g) || [];
  for (const b of blocks) {
    const get = tag => {
      const m = b.match(new RegExp('<' + tag + '>([\\s\\S]*?)</' + tag + '>'));
      if (!m) return '';
      return m[1].replace(/^<!\[CDATA\[/, '').replace(/\]\]>$/, '').trim();
    };
    const title = stripHtml(get('title'));
    const link = get('link');
    const summary = stripHtml(get('description')).slice(0, 200);
    const pub = get('pubDate');
    const d = pub ? new Date(pub) : null;
    if (title && link) {
      items.push({ title, link, summary, publishedAt: d && !isNaN(d) ? d.toISOString() : '' });
    }
  }
  return items;
}

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  // 只读公开数据无敏感信息：白名单外额外放行无 Origin 请求（部分环境同源请求不携带 Origin），
  // 仅回显 ACAO 给白名单内源（避免非白名单源拿到跨域可读响应）
  const allowed = ALLOWED_ORIGINS.has(origin) || origin === '';
  if (ALLOWED_ORIGINS.has(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'GET') { res.status(405).json({ error: 'Method Not Allowed' }); return; }
  if (!allowed) { res.status(403).json({ error: 'Forbidden' }); return; }

  if (cache.v && Date.now() - cache.t < TTL) { res.status(200).json(cache.v); return; }

  // 36氪：镜像链依次尝试，命中即止；单源失败不影响其他源（降级而非整体 502）
  const items = [];
  const sources = [];
  for (const url of MIRRORS) {
    try {
      const xml = await fetchFeed(url);
      const got = parseRss(xml).slice(0, 30).map(it => ({ ...it, source: '36氪' }));
      if (got.length) { items.push(...got); sources.push('36氪'); break; }
    } catch (e) { /* 静默尝试下一个镜像 */ }
  }
  // Redis：官方博客直连，取最新 3 条（更新频率低，仅补位）
  try {
    const xml = await fetchFeed(REDIS_FEED);
    const got = parseRss(xml).slice(0, REDIS_LIMIT).map(it => ({ ...it, source: 'Redis' }));
    if (got.length) { items.push(...got); sources.push('Redis'); }
  } catch (e) { /* 静默跳过 */ }
  if (!items.length) {
    res.status(502).json({ error: '上游服务不可用' });
    return;
  }

  const body = {
    updated_at: new Date().toISOString(),
    sources,
    items,
  };
  cache = { t: Date.now(), v: body };
  res.status(200).json(body);
}
