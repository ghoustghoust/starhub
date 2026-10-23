// Vercel Serverless Function：全网 GitHub 仓库搜索（跨语言）
// 用法：POST /api/search  Body: { q: "视频创作", sort: "best-match"|"stars"|"updated", lang?: "Python", page?: 1 }
//
// 翻译（T6 重构 2026-10-04）：改用 api/_lib/translate.js 的统一降级链
//   GTX → MyMemory → Agnes(多key) → Zen —— 旧版自带的 Google→MyMemory 两层在
//   GTX 被数据中心 IP 封锁 + MyMemory 出口配额打满时全断（用户实测 translated:false，
//   中文原词直接搜 GitHub 结果完全不可用）。translate.js 内置词级缓存与熔断。
//
// 多词查询（T6）：旧版对含空格的中文整串加引号 = GitHub 短语搜索，几乎必然 0 结果。
//   现按空格拆词、逐词翻译，查询档位依次尝试（total=0 才降下一档）：
//   单词 → [zh OR en]
//   多词 → [en1 en2（不加引号，GitHub 隐式 AND，召回最大）
//          → "zh1 zh2" OR "en1 en2"（短语兜底）
//          → en1（仅英文主词）→ zh1（仅首个中文词）]
//   实际生效的查询串在响应 strategy 字段返回，前端可标注。
//
// 防护：CORS 白名单 + X-Search-Key（与 refresh.js 同 key）+ 内存限流
//   （每 IP 10 req/min、全局 25 req/min——key 公开内联在页面里，无限流时
//   GH_TOKEN 的 30 req/min 搜索配额可被恶意烧穿）。
// 缓存：搜索结果内存缓存 10 分钟（key=查询|sort|page）；词级翻译缓存 1 小时。
import { translateWithFallback } from './_lib/translate.js';
import { applyCors, rejectMethod } from './_lib/http.js';

const SORTS = new Set(['best-match', 'stars', 'updated']);
const PER_PAGE = 30;
const MAX_PAGE = 34; // 1000 / 30
const TTL_SEARCH = 10 * 60 * 1000;
const TTL_SEARCH_UNTRANSLATED = 2 * 60 * 1000;
const TTL_TRANS = 60 * 60 * 1000;
const MAX_TRANSLATE_TERMS = 6;
const MAX_QUERY_LEN = 256;
const CJK_RE = /[\u3400-\u9fff]/;
const RATE_IP = 10;     // 每 IP 每分钟
const RATE_GLOBAL = 25; // 全局每分钟

// 模块级内存缓存（Vercel 单实例有效，冷启动丢失可接受）
const searchCache = new Map();
const transCache = new Map();
const rateIp = new Map();
let rateGlobalTs = [];

function cacheGet(map, key, ttl) {
  const hit = map.get(key);
  if (!hit) return undefined;
  if (Date.now() - hit.t > (hit.ttl || ttl)) { map.delete(key); return undefined; }
  return hit.v;
}
function cacheSet(map, key, val, ttl) { map.set(key, { t: Date.now(), v: val, ttl }); }

const RATE_WINDOW = 60 * 1000;
function rateLimited(ip) {
  const now = Date.now();
  rateGlobalTs = rateGlobalTs.filter(t => now - t < RATE_WINDOW);
  if (rateGlobalTs.length >= RATE_GLOBAL) return true;
  const arr = (rateIp.get(ip) || []).filter(t => now - t < RATE_WINDOW);
  const limited = arr.length >= RATE_IP;
  rateIp.set(ip, arr.concat([now]));
  if (!limited) rateGlobalTs.push(now);
  return limited;
}

// 单词翻译：统一链（→en 方向：GTX→MyMemory，LLM 腿 prompt 硬编码译成中文故跳过），词级缓存 1h。
// ⚠ 只缓存成功结果——失败入缓存会把一次瞬时故障放大成 1 小时的
// "translated:false"（部署后首轮撞 GTX 间歇 429 实测复现）。失败下次重试。
async function translateTerm(term) {
  const cached = cacheGet(transCache, term, TTL_TRANS);
  if (cached !== undefined) return cached;
  try {
    const r = await translateWithFallback(term, { to: 'en' });
    const en = (r && r.zh) ? String(r.zh).trim().replace(/"/g, '').slice(0, 120) : '';
    if (en) {
      const hit = { en, engine: r.engine || 'unknown' };
      cacheSet(transCache, term, hit, TTL_TRANS);
      return hit;
    }
    console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'search_trans_fail', term: term.slice(0, 30), engine: r ? r.engine : 'no-result' }));
  } catch (e) {
    console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'search_trans_error', term: term.slice(0, 30) }));
  }
  return null;
}

// GitHub 语法：限定符原样保留，只翻译自由文本里的中文词。
// 否则「视频 language:Python」会把 language:Python 也送去翻译，既慢又可能被
// 译成中文而让限定符失效。
const QUALIFIER_RE = /^(-?)([a-z][a-z0-9-]{1,24}:)(?:"([^"]*)"|([^\s]+))/i;

export function tokenizeSearchQuery(input) {
  const s = String(input || '').trim();
  const tokens = [];
  let i = 0;
  while (i < s.length) {
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;
    const rest = s.slice(i);
    const q = rest.match(QUALIFIER_RE);
    if (q) {
      tokens.push({ kind: 'qualifier', raw: q[0], key: q[2].slice(0, -1).toLowerCase(), value: q[3] !== undefined ? q[3] : q[4], negated: q[1] === '-' });
      i += q[0].length;
      continue;
    }
    let negated = false;
    if (s[i] === '-' && i + 1 < s.length) { negated = true; i++; }
    if (s[i] === '"') {
      const end = s.indexOf('"', i + 1);
      if (end > i) {
        const text = s.slice(i + 1, end).trim();
        if (text) tokens.push({ kind: 'word', text, quoted: true, negated, raw: (negated ? '-' : '') + '"' + text + '"' });
        i = end + 1;
        continue;
      }
    }
    let j = i;
    while (j < s.length && !/\s/.test(s[j])) j++;
    const text = s.slice(i, j);
    if (text) tokens.push({ kind: 'word', text, quoted: false, negated, raw: (negated ? '-' : '') + text });
    i = j;
  }
  return tokens;
}

function tokenQuery(token, text, { multiQuote = false } = {}) {
  let body = String(text || '').trim();
  if (!body) return '';
  if (token.quoted || (multiQuote && /\s/.test(body))) body = '"' + body.replace(/"/g, '') + '"';
  return (token.negated ? '-' : '') + body;
}
function quotePhrase(text) {
  const s = String(text || '').trim();
  return /\s/.test(s) ? '"' + s.replace(/"/g, '') + '"' : s;
}
function trimQuery(q) {
  const s = String(q || '').replace(/\s+/g, ' ').trim();
  if (s.length <= MAX_QUERY_LEN) return s;
  const cut = s.slice(0, MAX_QUERY_LEN);
  return cut.slice(0, Math.max(0, cut.lastIndexOf(' '))) || cut;
}

// 查询档位序列：total=0 才降下一档。
// 多词先 AND 保精度，再用短语与 OR 兜底；限定符在每一档都原样带上。
export function buildQueryPlan(input, enMap = new Map()) {
  const tokens = tokenizeSearchQuery(input);
  const words = tokens.filter(t => t.kind === 'word');
  const suffix = tokens.filter(t => t.kind === 'qualifier').map(t => t.raw).join(' ');
  if (!words.length) return suffix ? [trimQuery(suffix)] : [];

  const plans = [];
  const add = (core) => {
    const q = trimQuery((core + (suffix ? ' ' + suffix : '')).trim());
    if (q && !plans.includes(q)) plans.push(q);
  };
  const prepared = words.map(t => {
    const en = enMap.get(t.text) || '';
    const changed = Boolean(en) && en.toLowerCase() !== t.text.toLowerCase() && !t.negated;
    return { ...t, en: changed ? en : '', effective: changed ? en : t.text };
  });

  if (prepared.length === 1) {
    const t = prepared[0];
    const orig = tokenQuery(t, t.text, { multiQuote: true });
    if (t.en) {
      const en = tokenQuery(t, t.en, { multiQuote: true });
      add(orig + ' OR ' + en);
      add(en);
    } else {
      add(tokenQuery(t, t.effective, { multiQuote: true }));
    }
    add(orig);
    return plans;
  }

  const best = prepared.map(t => tokenQuery(t, t.effective));
  add(best.join(' ')); // ① 逐词 AND（不加引号，召回最大）
  const anyNegated = prepared.some(t => t.negated);
  if (!anyNegated) {
    const origPhrase = quotePhrase(prepared.map(t => t.text).join(' '));
    const enReady = prepared.every(t => t.en || !CJK_RE.test(t.text));
    const enPhrase = enReady ? quotePhrase(prepared.map(t => t.en || t.text).join(' ')) : '';
    add(origPhrase + (enPhrase && enPhrase !== origPhrase ? ' OR ' + enPhrase : '')); // ② 整串短语兜底
  }
  const orTerms = [...new Set(prepared.map(t => tokenQuery(t, t.effective, { multiQuote: true })))].slice(0, 4);
  if (!anyNegated && orTerms.length > 1) add(orTerms.join(' OR ')); // ③ OR 回退，避免只搜首词导致召回塌陷
  add(best[0]); // ④ 仅首个最佳词
  add(tokenQuery(prepared[0], prepared[0].text, { multiQuote: true })); // ⑤ 仅首个原词
  return plans;
}

export function collectHighlightTerms(input, enMap = new Map()) {
  const terms = [];
  for (const t of tokenizeSearchQuery(input)) {
    if (t.kind !== 'word' || t.negated) continue;
    terms.push(t.text);
    const en = enMap.get(t.text);
    if (en && en.toLowerCase() !== t.text.toLowerCase()) terms.push(en);
  }
  return [...new Set(terms)].slice(0, 16);
}

// ── 低质/骗排名仓库判据（2026-10-08）────────────────────────────────
// 只信 /search/repositories 自带字段 ⇒ 零额外请求、不建关键词表。
// 阈值来自实测（不是估的）：三页真样本里正常仓库的 description 最长 350 字符（GitHub UI
// 的写入上限就是 350 ⇒ 超过它只能是走 API 塞进来的异常长文）；靶样本
// （cirosantilli/china-dictatorship）把 **64,765 字符**正文塞进 description 骗排名，
// 而搜索接口**原样返回**该字段 ⇒ 一条垃圾就把整页响应撑到 63KB，前端渲染成文字墙。
//
// 曾经有判据②「topics 顶格 20 + 描述 >200 + 无代码语言 ⇒ 关键词堆砌」，已删除：
// 它在第二页就把 **144,081 星的 x1xhlol/system-prompts-and-models-of-ai-tools** 误杀了
// （它恰好 topics=20、desc=328、language=null）。这三条特征合法热门清单仓库全都有，
// 不具区分度。判据③「无代码/只有 README」同理不做独立屏蔽（见 tests 里的真样本清单）。
// 两条判据的回归锚点都钉在 tests/site_nav_drift/test_search_p0.py，防止以后被重新加回来。
const SPAM_DESC_MAX = 400;   // ① 描述长度硬门：>400 只能是绕开 UI 上限写入的异常长文
const DESC_KEEP = 300;       // 保留条目也截断：350 字符的合法长描述仍会成倍放大响应

// 跑在**原始 item** 上，不能跑在映射后的条目上：映射时 topics 只留 3 个，
// 任何依赖 topics 的判据会永远不成立。
export function spamReason(x) {
  const desc = x.description || '';
  return desc.length > SPAM_DESC_MAX ? 'desc_len' : null;
}

async function githubSearch(q, sort, page) {
  const u = new URL('https://api.github.com/search/repositories');
  u.searchParams.set('q', q);
  u.searchParams.set('per_page', String(PER_PAGE));
  u.searchParams.set('page', String(page));
  if (sort !== 'best-match') { u.searchParams.set('sort', sort); u.searchParams.set('order', 'desc'); }
  return fetch(u, {
    headers: {
      'Authorization': 'Bearer ' + process.env.GH_TOKEN,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'starhub',
    },
    signal: AbortSignal.timeout(15000),
  });
}

// 422（查询语法）：去掉引号与多余空白后重试一次
function sanitizeQ(q) { return trimQuery(q.replace(/"/g, '').replace(/\s+/g, ' ')); }

function withLanguage(plan, lang) {
  if (!lang) return plan;
  if (/(^|\s)language:[^\s]+/i.test(plan)) return plan;
  return trimQuery(plan + ' language:' + lang);
}

export default async function handler(req, res) {
  const allowed = applyCors(req, res, {
    methods: 'POST, OPTIONS',
    headers: 'Content-Type, X-Search-Key',
    allowNoOrigin: true,
  });
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'POST') { rejectMethod(res, 'POST, OPTIONS'); return; }
  if (!allowed) { res.status(403).json({ error: 'Forbidden' }); return; }

  const key = process.env.REFRESH_KEY;
  if (!key || req.headers['x-search-key'] !== key) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }
  if (!process.env.GH_TOKEN) {
    res.status(500).json({ error: 'GH_TOKEN 未配置（Vercel 环境变量）' });
    return;
  }

  // 限流：key 是公开的（内联页面），防烧 GH_TOKEN 搜索配额（30 req/min）
  const ip = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim() || 'unknown';
  if (rateLimited(ip)) {
    res.status(429).json({ error: '搜索太频繁，请稍后再试' });
    return;
  }

  let body;
  try { body = await new Promise((resolve, reject) => { let d = ''; req.on('data', c => { d += c; if (d.length > 4096) { reject(new Error('too large')); req.destroy(); } }); req.on('end', () => resolve(JSON.parse(d || '{}'))); req.on('error', reject); }); }
  catch (e) { res.status(400).json({ error: '请求体格式错误' }); return; }

  const zh = String(body.q || '').trim();
  if (zh.length < 2) { res.status(400).json({ error: '关键词至少 2 个字符' }); return; }
  const sort = SORTS.has(body.sort) ? body.sort : 'best-match';
  const lang = String(body.lang || '').trim();
  const page = Math.max(1, Math.min(parseInt(body.page, 10) || 1, MAX_PAGE));

  try {
    const wordTokens = tokenizeSearchQuery(zh).filter(t => t.kind === 'word');
    const needsTranslation = wordTokens.some(t => CJK_RE.test(t.text));
    const enMap = new Map();
    const engines = new Set();
    if (needsTranslation) {
      // 只翻中文词，英文词/限定符原样保留；同一词只翻一次，多词并行避免串行等待
      const terms = [...new Set(wordTokens.filter(t => CJK_RE.test(t.text)).map(t => t.text))].slice(0, MAX_TRANSLATE_TERMS);
      const results = await Promise.all(terms.map(async text => [text, await translateTerm(text)]));
      for (const [text, hit] of results) {
        if (hit && hit.en && hit.en.toLowerCase() !== text.toLowerCase()) {
          enMap.set(text, hit.en);
          if (hit.engine) engines.add(hit.engine);
        }
      }
    }
    const translated = enMap.size > 0;
    const meta = {
      translated,
      needs_translation: needsTranslation,
      translation_engine: [...engines].join(',') || 'none',
      translated_terms: [...enMap.values()],
      highlight_terms: collectHighlightTerms(zh, enMap),
      original_query: zh,
    };
    const plans = buildQueryPlan(zh, enMap);

    let data = null;
    for (const plan of plans) {
      const query = withLanguage(plan, lang);
      const ck = query.trim().replace(/\s+/g, ' ').toLowerCase() + '|' + sort + '|' + page;
      const hit = cacheGet(searchCache, ck, TTL_SEARCH);
      if (hit) { data = hit; break; }
      let r = await githubSearch(query, sort, page);
      if (r.status === 422) {
        r = await githubSearch(sanitizeQ(query), sort, page);
      }
      if (r.status === 403 || r.status === 429) {
        // GitHub 侧限流是全局的，换查询档位没有意义——直接告知用户
        res.status(503).json({ error: '搜索太频繁，请稍后再试' });
        return;
      }
      if (!r.ok) {
        res.status(502).json({ error: 'GitHub 搜索服务暂不可用' });
        return;
      }
      const j = await r.json();
      if (j.total_count > 0 || plans.length === 1) {
        const raw = j.items || [];
        let spamOut = 0, dedupOut = 0;
        const seen = new Set();
        let items = raw.filter(x => {
          if (spamReason(x)) { spamOut++; return false; }
          return true;
        }).map(x => ({
          full_name: x.full_name,
          desc: x.description
            ? (x.description.length > DESC_KEEP ? x.description.slice(0, DESC_KEEP) + '…' : x.description)
            : null,
          language: x.language,
          stars: x.stargazers_count,
          updated_at: x.updated_at,
          html_url: x.html_url,
          topics: (x.topics || []).slice(0, 3),
        })).filter(x => {
          if (!x.full_name || seen.has(x.full_name)) { dedupOut++; return false; }
          seen.add(x.full_name);
          return true;
        });
        // 翻译不可用时的结果侧兜底：GitHub 对 CJK 分词过宽（原词直搜会匹配数万无关项），
        // 在返回页内做一次关键词包含过滤，把"完全无关"的条目剔掉
        let relevanceOut = 0;
        if (!translated && needsTranslation) {
          const words = wordTokens.map(t => t.text.toLowerCase());
          const before = items.length;
          items = items.filter(x => words.some(w =>
            ((x.desc || '') + ' ' + (x.full_name || '')).toLowerCase().includes(w)));
          relevanceOut = before - items.length;
          console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'search_local_filter', before, after: items.length }));
        }
        if (spamOut) {
          console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'search_spam_filtered', out: spamOut, of: raw.length }));
        }
        data = {
          query,
          ...meta,
          strategy: query,
          page,
          total: j.total_count,
          // 分页判据必须用原始条数：前端旧写法是 items.length === 30，
          // 服务端一过滤就会把「还有下一页」判成到底（那段兜底早就踩了这个坑）。
          raw_count: raw.length,
          filtered_out: spamOut + dedupOut + relevanceOut,
          items,
        };
        // 翻译失败的结果只短暂缓存，翻译恢复后能尽快用上英文查询
        cacheSet(searchCache, ck, data, (!translated && needsTranslation) ? TTL_SEARCH_UNTRANSLATED : TTL_SEARCH);
        break;
      }
      // total=0 → 降下一档
    }
    if (!data) {
      res.status(200).json({ query: zh, ...meta, strategy: zh, page, total: 0, items: [] });
      return;
    }
    res.status(200).json(data);
  } catch (e) {
    res.status(502).json({ error: '上游服务不可用' });
  }
}
