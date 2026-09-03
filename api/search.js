// Vercel Serverless Function：全网 GitHub 仓库搜索（跨语言）
// 用法：POST /api/search  Body: { q: "视频创作", sort: "best-match"|"stars"|"updated", lang?: "Python", page?: 1 }
// 流程：中文输入 → 翻译英文（Google 端点 → MyMemory 降级 → 失败原词）→ "中文 OR 英文" 组合查询
// 防护：与 refresh.js 一致 —— ① CORS 仅允许白名单 Origin（比较时统一小写）；② X-Search-Key 头 == REFRESH_KEY（弱防护）
// 缓存：搜索结果内存缓存 10 分钟（key=q|sort|lang|page）；翻译结果缓存 1 小时（key=原词）
// 分页：per_page=30，page 上限 34（GitHub 搜索最多返回前 1000 条）
const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

const SORTS = new Set(['best-match', 'stars', 'updated']);
const PER_PAGE = 30;
const MAX_PAGE = 34; // 1000 / 30
const TTL_SEARCH = 10 * 60 * 1000;
const TTL_TRANS = 60 * 60 * 1000;

// 模块级内存缓存（Vercel 单实例有效，冷启动丢失可接受）
const searchCache = new Map();
const transCache = new Map();

function cacheGet(map, key, ttl) {
  const hit = map.get(key);
  if (!hit) return undefined;
  if (Date.now() - hit.t > ttl) { map.delete(key); return undefined; }
  return hit.v;
}
function cacheSet(map, key, val) { map.set(key, { t: Date.now(), v: val }); }

// 中文 → 英文：Google 非官方端点（免 key）→ MyMemory 降级 → 失败返回 null
async function translateZh(zh) {
  const cached = cacheGet(transCache, zh, TTL_TRANS);
  if (cached !== undefined) return cached;
  let en = null;
  try {
    const u = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=en&dt=t&q=' + encodeURIComponent(zh);
    const r = await fetch(u, { signal: AbortSignal.timeout(8000) });
    if (r.ok) {
      const j = await r.json();
      const t = (j[0] || []).map(x => x && x[0]).join('').trim();
      if (t) en = t;
      else console.error('[translate] Google 响应为空: zh=' + zh);
    } else {
      console.error('[translate] Google HTTP ' + r.status + ': zh=' + zh + ' body=' + (await r.text()).slice(0, 120));
    }
  } catch (e) { console.error('[translate] Google 异常: zh=' + zh + ' err=' + (e && e.message || e)); }
  if (!en) {
    try {
      const u = 'https://api.mymemory.translated.net/get?q=' + encodeURIComponent(zh) + '&langpair=zh-CN|en';
      const r = await fetch(u, { signal: AbortSignal.timeout(8000) });
      if (r.ok) {
        const j = await r.json();
        const t = ((j.responseData || {}).translatedText || '').trim();
        if (t) en = t;
        else console.error('[translate] MyMemory 响应为空: zh=' + zh);
        const warn = ((j.responseData || {}).translatedText || '').match(/MYMEMORY WARNING[^\n]*/i);
        if (warn) console.error('[translate] MyMemory 警告: ' + warn[0]);
      } else {
        console.error('[translate] MyMemory HTTP ' + r.status + ': zh=' + zh + ' body=' + (await r.text()).slice(0, 120));
      }
    } catch (e) { console.error('[translate] MyMemory 异常: zh=' + zh + ' err=' + (e && e.message || e)); }
  }
  if (!en) console.error('[translate] 两个端点均失败: zh=' + zh);
  cacheSet(transCache, zh, en);
  return en;
}

// "中文 OR 英文"；含空格时加引号（GitHub 空格默认 AND）；翻译结果与原文相同 → 仅原词
function buildQuery(zh, en) {
  const quote = s => /\s/.test(s) ? '"' + s + '"' : s;
  if (!en || en.toLowerCase() === zh.toLowerCase()) return quote(zh);
  return quote(zh) + ' OR ' + quote(en);
}

async function githubSearch(q, sort, page) {
  const u = new URL('https://api.github.com/search/repositories');
  u.searchParams.set('q', q);
  u.searchParams.set('per_page', String(PER_PAGE));
  u.searchParams.set('page', String(page));
  if (sort !== 'best-match') { u.searchParams.set('sort', sort); u.searchParams.set('order', 'desc'); }
  const r = await fetch(u, {
    headers: {
      'Authorization': 'Bearer ' + process.env.GH_TOKEN,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'starhub-refresh',
    },
    signal: AbortSignal.timeout(15000),
  });
  return r;
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

  let body;
  try { body = await new Promise((resolve, reject) => { let d = ''; req.on('data', c => { d += c; if (d.length > 4096) { reject(new Error('too large')); req.destroy(); } }); req.on('end', () => resolve(JSON.parse(d || '{}'))); req.on('error', reject); }); }
  catch (e) { res.status(400).json({ error: '请求体格式错误' }); return; }

  const zh = String(body.q || '').trim();
  if (zh.length < 2) { res.status(400).json({ error: '关键词至少 2 个字符' }); return; }
  const sort = SORTS.has(body.sort) ? body.sort : 'best-match';
  const lang = String(body.lang || '').trim();
  const page = Math.max(1, Math.min(parseInt(body.page, 10) || 1, MAX_PAGE));

  try {
    // 含中文 → 翻译；纯英文/其他 → 原词直搜
    const hasZh = /[\u4e00-\u9fff]/.test(zh);
    const en = hasZh ? await translateZh(zh) : null;
    const translated = hasZh && !!en;
    let query = buildQuery(zh, en) + (lang ? ' language:' + lang : '');

    const ck = query + '|' + sort + '|' + page;
    let data = cacheGet(searchCache, ck, TTL_SEARCH);
    if (!data) {
      let r = await githubSearch(query, sort, page);
      // 422：清洗后重试一次
      if (r.status === 422) {
        query = sanitizeQ(query);
        r = await githubSearch(query, sort, page);
      }
      if (r.status === 403 || r.status === 429) {
        res.status(503).json({ error: '搜索太频繁，请稍后再试' });
        return;
      }
      if (!r.ok) {
        // 不向上游调用者回显 GitHub 错误细节（防信息泄露），细节由 Vercel 日志记录
        res.status(502).json({ error: 'GitHub 搜索服务暂不可用' });
        return;
      }
      const j = await r.json();
      data = {
        query,
        translated,
        page,
        total: j.total_count,
        items: (j.items || []).map(x => ({
          full_name: x.full_name,
          desc: x.description,
          language: x.language,
          stars: x.stargazers_count,
          updated_at: x.updated_at,
          html_url: x.html_url,
          topics: (x.topics || []).slice(0, 3),
        })),
      };
      cacheSet(searchCache, ck, data);
    }
    res.status(200).json(data);
  } catch (e) {
    res.status(502).json({ error: '上游服务不可用' });
  }
}
