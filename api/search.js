// Vercel Serverless Function：全网 GitHub 仓库搜索（跨语言）
// 用法：POST /api/search  Body: { q: "视频创作", sort: "best-match"|"stars"|"updated", lang?: "Python", page?: 1 }
//
// 翻译（T6 重构 2026-10-04）：改用 api/translate.js 的统一降级链
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
import { translateWithFallback } from './translate.js';

const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

const SORTS = new Set(['best-match', 'stars', 'updated']);
const PER_PAGE = 30;
const MAX_PAGE = 34; // 1000 / 30
const TTL_SEARCH = 10 * 60 * 1000;
const TTL_TRANS = 60 * 60 * 1000;
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
  if (Date.now() - hit.t > ttl) { map.delete(key); return undefined; }
  return hit.v;
}
function cacheSet(map, key, val) { map.set(key, { t: Date.now(), v: val }); }

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
// ⚠ 只缓存成功结果——失败（null）入缓存会把一次瞬时故障放大成 1 小时的
// "translated:false"（部署后首轮撞 GTX 间歇 429 实测复现）。失败下次重试。
async function translateTerm(term) {
  const cached = cacheGet(transCache, term, TTL_TRANS);
  if (cached !== undefined) return cached;
  const r = await translateWithFallback(term, { to: 'en' });
  const engine = r ? r.engine : 'no-result';
  const en = (r && r.zh) ? String(r.zh).trim() : null;
  if (en) {
    cacheSet(transCache, term, en);
  } else {
    console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'search_trans_fail', term: term.slice(0, 30), engine }));
  }
  return en;
}

// 查询档位序列：total=0 才降下一档（见文件头注释）。
// 每词的最佳形态 = 翻译命中用翻译、否则原词（英文词本身就是"翻译"）——
// 避免纯英文多词输入掉进引号短语档（实测召回塌陷）。
function buildQueryPlan(zh, enMap) {
  const quote = s => /\s/.test(s) ? '"' + s + '"' : s;
  const zhTerms = zh.split(/\s+/).filter(Boolean);
  const best = zhTerms.map(t => enMap.get(t) || t);
  const enTerms = zhTerms.map(t => enMap.get(t)).filter(Boolean);
  const plans = [];
  if (zhTerms.length <= 1) {
    const z = zhTerms[0] || zh;
    const e = enMap.get(z);
    plans.push(e && e.toLowerCase() !== z.toLowerCase() ? quote(z) + ' OR ' + quote(e) : quote(z));
    return plans;
  }
  const hasTrans = enTerms.length > 0;
  plans.push(best.join(' '));                                            // ① 逐词 AND（不加引号）
  plans.push(quote(zh) + (enJoined(zhTerms, enMap) ? ' OR ' + quote(enJoined(zhTerms, enMap)) : '')); // ② 整串短语兜底
  if (enTerms.length) plans.push(enTerms[0]);                            // ③ 仅英文主词
  plans.push(zhTerms[0]);                                                // ④ 仅首个中文词
  return plans;
}
function enJoined(zhTerms, enMap) {
  const parts = zhTerms.map(t => enMap.get(t)).filter(Boolean);
  return parts.length === zhTerms.length ? parts.join(' ') : '';
}

export { buildQueryPlan };

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
      'User-Agent': 'starhub-refresh',
    },
    signal: AbortSignal.timeout(15000),
  });
}

// 422（查询语法）：去掉引号与多余空白后重试一次
function sanitizeQ(q) { return q.replace(/"/g, '').replace(/\s+/g, ' ').trim(); }

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  const allowed = ALLOWED_ORIGINS.has(origin);
  if (allowed) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Search-Key');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'POST') { res.status(405).json({ error: 'Method Not Allowed' }); return; }
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
    const hasZh = /[\u4e00-\u9fff]/.test(zh);
    const zhTerms = zh.split(/\s+/).filter(Boolean);
    const enMap = new Map();
    if (hasZh) {
      for (const t of zhTerms) enMap.set(t, await translateTerm(t));
    }
    const translated = hasZh && [...enMap.values()].some(Boolean);
    const plans = buildQueryPlan(zh, enMap);

    let data = null, usedQuery = null;
    for (const plan of plans) {
      const query = plan + (lang ? ' language:' + lang : '');
      const ck = query + '|' + sort + '|' + page;
      const hit = cacheGet(searchCache, ck, TTL_SEARCH);
      if (hit) { data = hit; usedQuery = query; break; }
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
        let spamOut = 0;
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
        }));
        // 翻译不可用时的结果侧兜底：GitHub 对 CJK 分词过宽（原词直搜会匹配数万无关项），
        // 在返回页内做一次关键词包含过滤，把"完全无关"的条目剔掉
        let relevanceOut = 0;
        if (!translated && hasZh) {
          const words = zhTerms.map(t => t.toLowerCase());
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
          translated,
          strategy: query,
          page,
          total: j.total_count,
          // 分页判据必须用原始条数：前端旧写法是 items.length === 30，
          // 服务端一过滤就会把「还有下一页」判成到底（那段兜底早就踩了这个坑）。
          raw_count: raw.length,
          filtered_out: spamOut + relevanceOut,
          items,
        };
        cacheSet(searchCache, ck, data);
        usedQuery = query;
        break;
      }
      // total=0 → 降下一档
    }
    if (!data) {
      res.status(200).json({ query: zh, translated, strategy: zh, page, total: 0, items: [] });
      return;
    }
    res.status(200).json(data);
  } catch (e) {
    res.status(502).json({ error: '上游服务不可用' });
  }
}
