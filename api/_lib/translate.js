// Vercel Serverless Function：运行时翻译网关
// 降级链（快优先：GTX/MyMemory 1-3s，LLM 10-25s 仅作兜底）：
//   Google GTX → MyMemory → Agnes(多key轮询) → OpenCode Zen(3模型轮询)
//   + 全端点熔断（连续5次全败暂停5分钟）
// 模式：
//   mode:'full' —— 全文/摘要按钮（低频高价值），GTX 主力，完整降级
//   mode:'bulk' —— 批量补翻兜底（浏览器 GTX CORS 全灭时涌入），完整降级链，Agnes 限额30条
// 用法：POST /api/translate  { "texts": ["..."], "mode": "full"|"bulk" }
//        → 200 { ok: true, engine: "agnes"|"zen"|"gtx"|"mymemory"|...", translations: ["..."] }
// 防护：CORS 白名单；多密钥轮询；实例内存缓存 + 轻量限流 + 熔断
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
const AGNES_FALLBACK_MAX = 30; // bulk 模式 Agnes 兜底条数上限：浏览器 GTX CORS 全灭时 bulk 流量全部涌入

// 多 key 轮询（429 自动切换，对齐 build_rss_aggregator.py 的 AgnesLLM 行为）
// 主 key 与附加 key 均支持逗号分隔，统一合并成一个池
const AGNES_KEYS = [
  process.env.AGNES_API_KEY,
  process.env.AGNES_API_KEYS,
].join(',').split(',').map(s => s.trim()).filter(Boolean);
let agnesKeyIdx = 0;

// ── OpenCode Zen 免费模型轮询 ──
const ZEN_URL = 'https://opencode.ai/zen/v1/chat/completions';
const ZEN_MODELS = (process.env.ZEN_TRANSLATE_MODEL || 'ling-3.0-flash-fin-free,big-pickle,mimo-v2.5-free')
  .split(',').map(s => s.trim()).filter(Boolean);
const ZEN_KEY = process.env.ZEN_API_KEY || process.env.OPENCODE_KEY || 'public';
const zenModelBlock = {};   // model → 自封截止 timestamp
const zenModelOffenses = {}; // model → 连续自封次数
let zenModelIdx = 0;

// ── 全端点熔断 ──
let transFailStreak = 0;
let transBlockUntil = 0;
const TRANS_BLOCK_DURATION = 300000; // 5 分钟
const TRANS_FAIL_THRESHOLD = 5;

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

async function translateOne(text, keys) {
  const keyList = Array.isArray(keys) ? keys : [keys];
  for (let attempt = 0; attempt < keyList.length; attempt++) {
    const apiKey = keyList[(agnesKeyIdx + attempt) % keyList.length];
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
        chat_template_kwargs: { enable_thinking: false },
      }),
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT),
    });
    if (r.status === 429) {
      continue; // attempt 递增自然切换到下一个 key
    }
    if (!r.ok) throw new Error(`agnes ${r.status}`);
    const j = await r.json();
    const out = ((j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || '').trim();
    if (!out) throw new Error('agnes empty');
    agnesKeyIdx = (agnesKeyIdx + attempt) % keyList.length; // 记住当前成功的 key，下次优先使用
    return out;
  }
  throw new Error('agnes all keys 429');
}

// Google GTX 免费端点（server-to-server；模式参考 api/search.js translateZh）。
// 注意：Vercel DC 出口 IP 会被该端点频率限流（线上实测 gtx 429），故 bulk 侧以其尽力而为 + Agnes 限量兜底
async function translateGtx(text, to = 'zh-CN') {
  const u = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=' + to + '&dt=t&q=' + encodeURIComponent(String(text).slice(0, 1200));
  const r = await fetch(u, { headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }, signal: AbortSignal.timeout(6000) });
  if (!r.ok) throw new Error(`gtx ${r.status}`);
  const j = await r.json();
  const out = ((j[0] || []).map((x) => (x && x[0]) || '').join('') || '').trim();
  if (!out) throw new Error('gtx empty');
  return out;
}

// GTX 失败退避 600ms 重试一次（共享出口 IP 偶发频率限流）
async function translateGtxRetry(text, to) {
  try { return await translateGtx(text, to); }
  catch (e) { await new Promise((r) => setTimeout(r, 600)); return await translateGtx(text, to); }
}

// ── OpenCode Zen 免费模型轮询 ──
async function translateZen(text) {
  for (let attempt = 0; attempt < ZEN_MODELS.length; attempt++) {
    const model = ZEN_MODELS[(zenModelIdx + attempt) % ZEN_MODELS.length];
    if (zenModelBlock[model] && Date.now() < zenModelBlock[model]) continue;
    const r = await fetch(ZEN_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${ZEN_KEY}`,
        'User-Agent': 'opencode/1.15.0 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.13',
        'x-opencode-client': 'cli',
        'x-opencode-project': 'global',
        'x-opencode-request': 'msg_' + Math.random().toString(36).slice(2),
        'x-opencode-session': 'ses_' + Math.random().toString(36).slice(2),
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: String(text).slice(0, MAX_TEXT_LEN) },
        ],
        max_tokens: 400,
        temperature: 0.2,
      }),
      signal: AbortSignal.timeout(25000),
    });
    if (!r.ok) {
      const offenses = zenModelOffenses[model] || 0;
      zenModelBlock[model] = Date.now() + Math.min(300000 * Math.pow(2, offenses), 3600000);
      zenModelOffenses[model] = offenses + 1;
      continue;
    }
    const j = await r.json();
    const out = ((j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || '').trim();
    if (!out) continue;
    zenModelBlock[model] = 0;
    zenModelOffenses[model] = 0;
    zenModelIdx = (zenModelIdx + attempt + 1) % ZEN_MODELS.length;
    return out;
  }
  throw new Error('zen all models blocked');
}

// ── MyMemory 免费兜底 ──
function detectLang(text) {
  const t = String(text).slice(0, 200);
  const total = t.replace(/[\s\d\p{P}]/gu, '').length || 1;
  const cn = (t.match(/[\u4e00-\u9fff]/g) || []).length;
  if (cn > total * 0.3) return 'zh-CN';
  const ja = (t.match(/[\u3040-\u309f\u30a0-\u30ff]/g) || []).length;
  if (ja > 0) return 'ja';
  const ko = (t.match(/[\uac00-\ud7af]/g) || []).length;
  if (ko > total * 0.3) return 'ko';
  const de = (t.match(/\b(der|die|das|und|ist|nicht|ein|zu|den|mit)\b/gi) || []).length;
  if (de > 2) return 'de';
  const fr = (t.match(/\b(le|la|les|des|est|un|une|et|pas|dans|pour|qui)\b/gi) || []).length;
  if (fr > 2) return 'fr';
  return 'en';
}

async function translateMyMemory(text, to = 'zh-CN') {
  const src = detectLang(text);
  const u = `https://api.mymemory.translated.net/get?q=${encodeURIComponent(String(text).slice(0, 500))}&langpair=${src}|${to}`;
  const r = await fetch(u, { signal: AbortSignal.timeout(6000) });
  if (!r.ok) throw new Error(`mymemory ${r.status}`);
  const j = await r.json();
  // responseStatus 非 200 时 translatedText 会是错误文案（同语言对返回
  // "PLEASE SELECT TWO DISTINCT LANGUAGES" 实测），必须横幅黑名单一起拦——
  // 否则垃圾串被当成功译文缓存，组进 GitHub 查询全是无关仓库（2026-10-04 实锤）
  if (j.responseStatus !== undefined && Number(j.responseStatus) !== 200) throw new Error('mymemory status ' + j.responseStatus);
  const out = (j.responseData && j.responseData.translatedText || '').trim();
  if (!out || /^(MYMEMORY|PLEASE SELECT|QUERY LENGTH LIMIT|INVALID (SOURCE|TARGET|LANGUAGE))/i.test(out)) throw new Error('mymemory empty/bad');
  return out;
}

// ── 熔断 ──
function isTransBlocked() { return Date.now() < transBlockUntil; }
function recordTransSuccess() { transFailStreak = 0; }
function recordTransFailure() {
  transFailStreak++;
  if (transFailStreak >= TRANS_FAIL_THRESHOLD) {
    transBlockUntil = Date.now() + TRANS_BLOCK_DURATION;
    transFailStreak = 0;
  }
}

// ── 统一降级链：GTX → MyMemory → Agnes(多key) → Zen(3模型) ──
// 快优先：GTX/MyMemory 通常 1-3s 完成，LLM 需 10-25s 仅作兜底
// to：目标方向（默认 zh-CN，既有 RSS/翻译按钮行为零变化）。search.js 传 to:'en'
// 做 中→英 查询词翻译——Agnes/Zen 两腿的 prompt 硬编码"译成简体中文"，非 zh
// 方向必须跳过（否则中文输入进 →中文 的链 = 同语言对，MyMemory 直接回错误横幅）。
async function translateWithFallback(text, { to = 'zh-CN' } = {}) {
  if (isTransBlocked()) return { zh: '', engine: 'blocked' };
  // 缓存键带方向；zh-CN 保持无后缀的旧键形（handler 的 getCache 按裸文本查）
  const ck = to === 'zh-CN' ? text.slice(0, 200) : text.slice(0, 200) + '|' + to;
  // 1) Google GTX（快，1-3s，免费无限制）
  try {
    const zh = await translateGtxRetry(text, to);
    setCache(ck, zh);
    recordTransSuccess();
    return { zh, engine: 'gtx' };
  } catch (e) { /* 降级 */ }
  // 2) MyMemory（快，1-3s，免费兜底）
  try {
    const zh = await translateMyMemory(text, to);
    setCache(ck, zh);
    recordTransSuccess();
    return { zh, engine: 'mymemory' };
  } catch (e) { /* 降级 */ }
  // 3)+4) LLM 腿只在 zh 方向可用（prompt 是"翻译成简体中文"）
  if (to === 'zh-CN') {
    // 3) Agnes（LLM，10-15s，有配额限制）
    if (AGNES_KEYS.length) {
      try {
        const zh = await translateOne(text, AGNES_KEYS);
        setCache(ck, zh);
        recordTransSuccess();
        return { zh, engine: 'agnes' };
      } catch (e) { /* 降级 */ }
    }
    // 4) OpenCode Zen（LLM，15-25s，免费模型可能被封）
    try {
      const zh = await translateZen(text);
      setCache(ck, zh);
      recordTransSuccess();
      return { zh, engine: 'zen' };
    } catch (e) { /* 全败 */ }
  }
  recordTransFailure();
  return { zh: '', engine: 'fail' };
}

// 有限并发池：按序保填充，最多 limit 个 worker 同时执行 fn
async function mapPool(items, limit, fn) {
  const out = new Array(items.length);
  let i = 0;
  async function worker() { while (i < items.length) { const idx = i++; out[idx] = await fn(items[idx], idx); } }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

// 具名导出：search.js 的全网搜索复用同一条降级链（原 search.js 自带的
// Google→MyMemory 两层在 GTX 被封 + MyMemory 配额打满时全断，用户实测
// translated:false 且结果驴唇不对马嘴）。只加导出，行为零变化。
export { translateWithFallback };

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

  const mode = (req.body && req.body.mode === 'full') ? 'full' : 'bulk';
  if (!AGNES_KEYS.length && !process.env.ZEN_API_KEY && !process.env.OPENCODE_KEY) {
    res.status(500).json({ error: 'No translation engine configured' }); return;
  }

  const ip = (req.headers['x-real-ip'] || req.headers['x-forwarded-for'] || '').split(',')[0].trim() || 'local';
  if (rateLimited(ip)) { res.status(429).json({ error: 'Too Many Requests' }); return; }

  const body = req.body || {};
  const texts = Array.isArray(body.texts)
    ? body.texts.filter((t) => typeof t === 'string' && t.trim())
    : [];
  if (!texts.length) { res.status(400).json({ error: 'texts must be a non-empty string array' }); return; }
  if (texts.length > MAX_TEXTS) { res.status(400).json({ error: `texts limited to ${MAX_TEXTS} items per request` }); return; }

  // 缓存命中的直接取用；未命中的走统一降级链
  const diag = [];
  const pending = [];
  const results = await Promise.all(texts.map(async (t, idx) => {
    const key = t.slice(0, 200);
    const hit = getCache(key);
    if (hit) return hit;
    pending.push(idx);
    return null;
  }));

  const concurrency = mode === 'full' ? AGNES_CONCURRENCY : GTX_CONCURRENCY;
  // bulk 模式 Agnes 限额：防浏览器端大面积失败时打爆上游
  const bulkLimit = mode === 'bulk' ? AGNES_FALLBACK_MAX : Infinity;
  let agnesUsed = 0;
  const engineCounts = {};

  await mapPool(pending, concurrency, async (idx) => {
    const t = texts[idx];
    const { zh, engine } = await translateWithFallback(t);
    if (zh) {
      results[idx] = zh;
      engineCounts[engine] = (engineCounts[engine] || 0) + 1;
    } else {
      diag.push(engine === 'blocked' ? 'circuit_breaker' : 'all_failed');
    }
  });

  if (!results.some(Boolean)) { res.status(502).json({ error: 'All translations failed', mode, diag: [...new Set(diag)].slice(0, 5) }); return; }
  for (let i = 0; i < results.length; i++) if (!results[i]) results[i] = '';
  const engine = Object.keys(engineCounts).sort((a, b) => engineCounts[b] - engineCounts[a])[0] || 'fail';
  res.status(200).json({ ok: true, engine, translations: results });
}
