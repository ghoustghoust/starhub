// Vercel Serverless Function：AGI Hunt 资讯代理
// 背景：AGI Hunt Agent API（agihunt.info/agent/v1）需要 API Key 认证，
//       不能暴露在前端代码中。本函数在服务端持有密钥，前端通过本代理访问。
// 用法：GET /api/agihunt?channel=models&day=2026-09-06&sort=hot
//        → { items: [{title, text, url, author, hot, published_at, channel, ...}] }
// 防护：CORS 白名单；密钥从环境变量 AGIHUNT_API_KEY 读取
// 缓存：结果 10 分钟 TTL（Vercel 实例内存），遵守 AGI Hunt 使用守则
const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

const AGIHUNT_BASE = 'https://agihunt.info/agent/v1';
const SKILL_VERSION = '1.2.2';
const TTL = 10 * 60 * 1000;

// 按 channel+day+sort 缓存（最多 20 条缓存项）
const cacheMap = new Map();

function cacheKey(params) {
  return `${params.channel}|${params.day}|${params.sort}`;
}

function getCache(key) {
  const entry = cacheMap.get(key);
  if (entry && Date.now() - entry.t < TTL) return entry.v;
  cacheMap.delete(key);
  return null;
}

function setCache(key, value) {
  if (cacheMap.size >= 20) {
    // 淘汰最旧条目
    const oldest = cacheMap.keys().next().value;
    cacheMap.delete(oldest);
  }
  cacheMap.set(key, { t: Date.now(), v: value });
}

// 合法频道白名单（避免任意参数转发到上游）
const VALID_CHANNELS = new Set([
  'models', 'research', 'coding-agents', 'products', 'multimodal',
  'infra', 'hardware', 'funding', 'policy', 'agi', 'companies', 'fun',
]);

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  if (ALLOWED_ORIGINS.has(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(ALLOWED_ORIGINS.has(origin) ? 204 : 403).end(); return; }
  if (req.method !== 'GET') { res.status(405).json({ error: 'Method Not Allowed' }); return; }
  if (!ALLOWED_ORIGINS.has(origin) && origin !== '') { res.status(403).json({ error: 'Forbidden' }); return; }

  const apiKey = process.env.AGIHUNT_API_KEY;
  if (!apiKey) {
    res.status(500).json({ error: 'AGIHUNT_API_KEY not configured' });
    return;
  }

  // 解析参数
  const { channel, day, sort } = req.query || {};
  if (!channel || !VALID_CHANNELS.has(channel)) {
    res.status(400).json({ error: 'Invalid channel. Valid: ' + [...VALID_CHANNELS].join(', ') });
    return;
  }
  // day 可选，默认今天（北京时间）
  const today = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  const reqDay = day || today;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(reqDay) && !/^\d{8}$/.test(reqDay)) {
    res.status(400).json({ error: 'Invalid day format. Use YYYY-MM-DD or YYYYMMDD' });
    return;
  }
  const reqSort = sort === 'new' ? 'new' : 'hot';

  // 检查缓存
  const ck = cacheKey({ channel, day: reqDay, sort: reqSort });
  const cached = getCache(ck);
  if (cached) { res.status(200).json(cached); return; }

  // 请求 AGI Hunt API
  const url = `${AGIHUNT_BASE}/channel/${encodeURIComponent(channel)}/items?day=${reqDay}&sort=${reqSort}`;
  try {
    const r = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'X-AgiHunt-Skill-Version': SKILL_VERSION,
        'User-Agent': `agihunt-skill/${SKILL_VERSION}`,
      },
      signal: AbortSignal.timeout(10000),
    });
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      res.status(r.status).json({ error: `AGI Hunt API returned ${r.status}`, detail: body.slice(0, 200) });
      return;
    }
    const data = await r.json();
    const body = {
      channel,
      day: reqDay,
      sort: reqSort,
      updated_at: new Date().toISOString(),
      items: data.items || [],
    };
    setCache(ck, body);
    res.status(200).json(body);
  } catch (e) {
    res.status(502).json({ error: 'Failed to fetch from AGI Hunt', detail: e.message });
  }
}
