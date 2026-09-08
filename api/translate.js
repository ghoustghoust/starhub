// Vercel Serverless Function：Agnes AI 翻译代理
// 背景：前端运行时翻译。引擎分流（用户定版策略）：
//       mode:'full' —— 全文/摘要按钮的用户主动点击翻译（低频高价值）：Agnes 主力
//         （agnes-2.5-flash，质量高但上游限额极低），失败时服务端自动降级 GTX 兜底；
//       mode:'bulk'（缺省）—— 批量补翻的服务端兜底：主力是前端 _browserGtx 浏览器直连 GTX
//         （用户本地 IP，端点响应带 ACAO:* 实证开放），仅其失败条目流入此处；服务端 GTX 尽力
//         而为（Vercel DC 出口 IP 常被该端点限流，线上实测 gtx 429），残余条目再由 Agnes 限量兜底
//         （AGNES_FALLBACK_MAX）。API 只做兜底与全文翻译（用户定版策略）。
//       旧版批量补翻也打 Agnes，首屏几十条打爆上游限额（实测分钟级仅 1~2 次，agnes 429），
//       反把全文翻译拖死；2026-09-08 改为按场景分流。
// 用法：POST /api/translate  { "texts": ["..."], "mode": "full"|"bulk" }
//        → 200 { ok: true, engine: "agnes"|"gtx"|"agnes+gtx"|"gtx+agnes", translations: ["..."] }（与 texts 等长、按序对应；单条失败为空串）
// 防护：CORS 白名单；密钥从环境变量 AGNES_API_KEY 读取（full 模式必需；bulk 仅 Agnes 兜底时使用）；
//       实例内存缓存 + 轻量限流
const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

const AGNES_URL = 'https://apihub.agnes-ai.com/v1/chat/completions';
const SYSTEM_PROMPT = '你是翻译引擎。把用户输入翻译成简体中文，只输出译文，不要解释。';
const MAX_TEXTS = 20;        // 与前端分批大小（15）匹配，留余量
const MAX_TEXT_LEN = 1500;   // 与构建侧 _agnes_translate 截断一致
const CACHE_TTL = 24 * 60 * 60 * 1000;
const RATE_LIMIT = 120;      // 每实例每分钟最多请求数。批量补翻主力已移至浏览器端直连后，此处流量以
                            // 兜底为主；120 保留以应对浏览器端全灭时的兜底风暴并拦截滥用
const UPSTREAM_TIMEOUT = 12000;
const AGNES_CONCURRENCY = 4; // Agnes 上游并发上限（仅 full 模式）。实测无限制并发会遭上游批量拒绝（502），收敛到 4
const GTX_CONCURRENCY = 2;   // GTX 并发上限（bulk 模式 + full 兜底）。Vercel 出口 IP 共享，Google 端点对频率敏感，保守 2
const AGNES_FALLBACK_MAX = 8; // bulk 模式 Agnes 兜底条数上限：仅保零星兜底，防浏览器端大面积失败时打爆上游限额

const cacheMap = new Map();  // text 前缀 → { t, zh }
const rateMap = new Map();   // ip → [windowStart, count]

function getCache(key) {
  const e = cacheMap.get(key);
  if (e && Date.now() - e.t < CACHE_TTL) return e.zh;
  cacheMap.delete(key);
  return null;
}

function setCache(key, zh) {
  if (cacheMap.size >= 500) {
    const oldest = cacheMap.keys().next().value;
    cacheMap.delete(oldest);
  }
  cacheMap.set(key, { t: Date.now(), zh });
}

function rateLimited(ip) {
  const now = Date.now();
  const e = rateMap.get(ip);
  if (!e || now - e[0] > 60000) { rateMap.set(ip, [now, 1]); return false; }
  e[1] += 1;
  return e[1] > RATE_LIMIT;
}

async function translateOne(text, apiKey) {
  const r = await fetch(AGNES_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${apiKey}`,
      'User-Agent': 'starhub-auto-update',
    },
    body: JSON.stringify({
      model: 'agnes-2.5-flash',
      messages: [
        { role: 'system', content: SYSTEM_PROMPT },
        { role: 'user', content: String(text).slice(0, MAX_TEXT_LEN) },
      ],
      max_tokens: 400,
      temperature: 0.2,
      // 思考型模型：关闭思考避免 max_tokens 被推理耗尽，同时加速响应
      chat_template_kwargs: { enable_thinking: false },
    }),
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT),
  });
  if (!r.ok) throw new Error(`agnes ${r.status}`);
  const j = await r.json();
  const out = ((j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || '').trim();
  if (!out) throw new Error('agnes empty');
  return out;
}

// 非 429 失败退避后重试一次（网络抖动/上游瞬时故障）。agnes 429 是上游限额极低，
// 短重试只会放大请求让限流窗口无法恢复 → 直接抛出，由上层 GTX 兜底
async function translateOneRetry(text, apiKey) {
  try { return await translateOne(text, apiKey); }
  catch (e) {
    if (((e && e.message) || '').indexOf('agnes 429') !== -1) throw e;
    await new Promise((r) => setTimeout(r, 400));
    return await translateOne(text, apiKey);
  }
}

// Google GTX 免费端点（server-to-server；模式参考 api/search.js translateZh）。
// 注意：Vercel DC 出口 IP 会被该端点频率限流（线上实测 gtx 429），故 bulk 侧以其尽力而为 + Agnes 限量兜底
async function translateGtx(text) {
  const u = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=' + encodeURIComponent(String(text).slice(0, 1200));
  const r = await fetch(u, { headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }, signal: AbortSignal.timeout(6000) });
  if (!r.ok) throw new Error(`gtx ${r.status}`);
  const j = await r.json();
  const out = ((j[0] || []).map((x) => (x && x[0]) || '').join('') || '').trim();
  if (!out) throw new Error('gtx empty');
  return out;
}

// GTX 失败退避 600ms 重试一次（共享出口 IP 偶发频率限流）
async function translateGtxRetry(text) {
  try { return await translateGtx(text); }
  catch (e) { await new Promise((r) => setTimeout(r, 600)); return await translateGtx(text); }
}

// 有限并发池：按序保填充，最多 limit 个 worker 同时执行 fn
async function mapPool(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  async function worker() { while (i < items.length) { const idx = i++; out[idx] = await fn(items[idx], idx); } }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  if (ALLOWED_ORIGINS.has(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(ALLOWED_ORIGINS.has(origin) ? 204 : 403).end(); return; }
  if (req.method !== 'POST') { res.status(405).json({ error: 'Method Not Allowed' }); return; }
  if (!ALLOWED_ORIGINS.has(origin) && origin !== '') { res.status(403).json({ error: 'Forbidden' }); return; }

  const mode = (req.body && req.body.mode === 'full') ? 'full' : 'bulk'; // 缺省 bulk：批量补翻不消耗 Agnes 额度
  const apiKey = process.env.AGNES_API_KEY;
  if (mode === 'full' && !apiKey) { res.status(500).json({ error: 'AGNES_API_KEY not configured' }); return; }

  const ip = (req.headers['x-real-ip'] || req.headers['x-forwarded-for'] || '').split(',')[0].trim() || 'local';
  if (rateLimited(ip)) { res.status(429).json({ error: 'Too Many Requests' }); return; }

  const body = req.body || {};
  const texts = Array.isArray(body.texts)
    ? body.texts.filter((t) => typeof t === 'string' && t.trim())
    : [];
  if (!texts.length) { res.status(400).json({ error: 'texts must be a non-empty string array' }); return; }
  if (texts.length > MAX_TEXTS) { res.status(400).json({ error: `texts limited to ${MAX_TEXTS} items per request` }); return; }

  // 缓存命中的直接取用；未命中的按 mode 引擎分流（单条失败返回空串，不阻塞整批）
  const diag = []; // 诊断：记录上游失败原因（仅状态码/错误类，不含密钥），502 时回传便于线上定位
  const pending = [];
  const results = await Promise.all(texts.map(async (t, idx) => {
    const key = t.slice(0, 200);
    const hit = getCache(key);
    if (hit) return hit;
    pending.push(idx);
    return null; // 占位，池完成后再回填
  }));
  let gtxSaved = 0;   // full 模式下由 Agnes 失败转 GTX 兜底成功的条数（用于 engine 标记）
  let agnesSaved = 0; // bulk 模式下由 GTX 失败转 Agnes 限量兜底成功的条数（用于 engine 标记）

  if (mode === 'full') {
    // Agnes 主力：全文/摘要按钮（低频高价值）
    const filled = await mapPool(pending, AGNES_CONCURRENCY, async (idx) => {
      const t = texts[idx];
      try {
        const zh = await translateOneRetry(t, apiKey);
        setCache(t.slice(0, 200), zh);
        return zh;
      } catch (e) {
        const reason = (e && e.message) || 'unknown';
        if (!diag.includes(reason)) diag.push(reason);
        return '';
      }
    });
    filled.forEach((zh, k) => { results[pending[k]] = zh; });
    // Agnes 失败条目（限流/故障）→ 服务端 GTX 兜底，保证用户点击总有结果
    const gtxIdx = pending.filter((idx) => !results[idx]);
    if (gtxIdx.length) {
      await mapPool(gtxIdx, GTX_CONCURRENCY, async (idx) => {
        const t = texts[idx];
        try {
          const zh = await translateGtxRetry(t);
          setCache(t.slice(0, 200), zh);
          results[idx] = zh;
          gtxSaved += 1;
        } catch (e) {
          const reason = (e && e.message) || 'unknown';
          if (!diag.includes(reason)) diag.push(reason);
        }
      });
    }
  } else {
    // 批量补翻服务端兜底：主力在前端浏览器直连 GTX（用户本地 IP），此处仅承接其失败条目
    await mapPool(pending, GTX_CONCURRENCY, async (idx) => {
      const t = texts[idx];
      try {
        const zh = await translateGtxRetry(t);
        setCache(t.slice(0, 200), zh);
        results[idx] = zh;
      } catch (e) {
        const reason = (e && e.message) || 'unknown';
        if (!diag.includes(reason)) diag.push(reason);
      }
    });
    // 残余失败条目 → Agnes 限量兜底（key 存在时）：免费端点对 DC IP 不可靠（实测 429），
    // Agnes 作最终兜底符合「API 用于兜底」策略；限量防浏览器端大面积失败时打爆上游
    const agnesIdx = pending.filter((idx) => !results[idx]);
    if (agnesIdx.length && apiKey) {
      const limited = agnesIdx.slice(0, AGNES_FALLBACK_MAX);
      await mapPool(limited, AGNES_CONCURRENCY, async (idx) => {
        const t = texts[idx];
        try {
          const zh = await translateOneRetry(t, apiKey);
          setCache(t.slice(0, 200), zh);
          results[idx] = zh;
          agnesSaved += 1;
        } catch (e) {
          const reason = (e && e.message) || 'unknown';
          if (!diag.includes(reason)) diag.push(reason);
        }
      });
    }
  }

  if (!results.some(Boolean)) { res.status(502).json({ error: 'All translations failed', mode, diag: diag.slice(0, 5) }); return; }
  for (let i = 0; i < results.length; i++) if (!results[i]) results[i] = ''; // 失败条目归一为空串（与用法注释一致；超出 AGNES_FALLBACK_MAX 的条目亦然）
  const engine = mode === 'full' ? (gtxSaved ? 'agnes+gtx' : 'agnes') : (agnesSaved ? 'gtx+agnes' : 'gtx');
  res.status(200).json({ ok: true, engine, translations: results });
}
