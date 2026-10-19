const { JSDOM } = require('jsdom');
const { Readability } = require('@mozilla/readability');
const { readFileSync } = require('fs');
const { join } = require('path');
// 运行时正文规范层：与 api/rss.js、构建期 build_rss_aggregator 同一套口径
// （lib/body_rules.js，Python↔JS 逐条对账）。这里同样是硬闸门，不做 try+可选降级：
// 装不上就出声失败，不许静默出厂未规范化/被裸截断的正文。
const BODY = require('../lib/body_rules.js');

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

function extractGeneric(dom, rawHtml) {
  const doc = dom.window.document;
  const titleEl = doc.querySelector('meta[property="og:title"]') || doc.querySelector('title');
  const title = titleEl ? (titleEl.content || titleEl.textContent) : '';

  const clone = doc.cloneNode(true);
  const article = new Readability(clone).parse();
  if (!article || !article.content) return null;
  const len = (article.textContent || '').trim().length;
  // 100 字这道**接受门槛**保持不动：把短正文阈值提到千把字，会把短小真实文章整批
  // 变成"抓不到" —— 那是用一个新故障换旧故障（已裁定）。短正文的正确出口是下面的标注。
  if (len < 100) return null;

  // 不丢内容，只丢掉"这就是全文"的暗示：付费墙试读与短正文一律标注，由阅读器显示成
  // "正文可能不完整"。取值域只有 paywall / short / null，且只来自 lib/body_rules.js
  // （这里不写第二套词表，也不写第二个阈值）。
  const degraded = BODY.classifyBody(len, rawHtml);
  return { title: article.title || title, content: article.content, source: 'readability', degraded: degraded };
}

/* 现抓通道的"可执行面"剥离 —— 对抗审查两轮点名的唯一未修 P0（用户裁定 ④：接）。
 * 这条通道把远端站点的 HTML 段直接交给阅读器 `innerHTML`，所以原文站点本来可以在我们的
 * 页面里跑脚本（读 localStorage 里的收藏/已读、伪造界面）。这里在**出口这一格**清掉能执行的
 * 东西：成对的危险标签 + 残留的孤标签、`on*=` 内联事件、`javascript:`/`data:text/html` 之类的
 * 伪协议 URL。文字、图片、链接一律保留（不吃正文）。
 * 为什么是纯文本刀而不是 DOM/白名单：
 *   ① CI 没有 `npm ci` ⇒ 这个文件（require jsdom）在闸里跑不起来，只有纯文本刀能被 node 判据实跑；
 *   ② 复用 `api/rss.js` 那份白名单要把它搬出 parseFeed 的切片，而
 *      `tests/site_nav/test_article_contract.py:233` 明确要求 `function sanitizeHtml(` 留在切片里
 *      （它同时钉着"deepClean 的唯一调用点在 sanitize 之后"那格死刀）⇒ 不在这里顺手动它。
 * 已知代价（写明白，不静默）：`iframe`（含 YouTube/Vimeo）在这条通道会被丢，而构建期产物里
 * 那一份按白名单放行。两通道这一格不同判，等白名单合并时一起收。
 * 判据：tests/site_nav/test_reader_fulltext_errors.py 的行为格（node 实跑切出来的本函数）
 *      + 接线格 + tools/mut_reader.py 的靶 R78。
 */
const _EXEC_TAGS_RE_PAIR = /<\s*(script|style|object|embed|form|iframe|link|meta|base)\b[\s\S]*?<\s*\/\s*\1\s*>/gi;
const _EXEC_TAGS_RE_LONE = /<\s*\/?\s*(script|style|object|embed|form|iframe|link|meta|base)\b[^>]*>/gi;
const _EVENT_ATTR_RE = /\son[a-z0-9._-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi;
const _PSEUDO_URL_RE = /\s(src|href|action|xlink:href|data-src|poster)\s*=\s*("[\s]*(?:javascript|vbscript|data:text\/html|data:application)[^"]*"|'[\s]*(?:javascript|vbscript|data:text\/html|data:application)[^']*')/gi;

function stripRemoteExecutables(html) {
  if (!html) return '';
  return String(html)
    .replace(_EXEC_TAGS_RE_PAIR, '')
    .replace(_EXEC_TAGS_RE_LONE, '')
    .replace(_EVENT_ATTR_RE, '')
    .replace(_PSEUDO_URL_RE, ' ');
}

// ── /api/article 返回契约（Task 6 的前端 _FT_NOTES 按这份表写文案，字段名与码一个都不许改）──
//   成功 {ok:true, url, title, content, source:'rss_fulltext'|'readability'|'youtube'|'github',
//         degraded?:'paywall'|'short'}
//   失败 {ok:false, error:'missing_url'|'invalid_url'|'timeout'|'fetch_failed'|'challenge_page'
//         |'no_body'|'extraction_failed'}
// 取消的那条：抽取失败时把页面摘要包成 <p> 当全文返回 —— 它与阅读器上方的摘要重复，
// 就是用户报的"全文=摘要"。现在只有真正文（readability / rss_fulltext）和两类站点的
// 正当呈现（youtube / github）算成功，其余一律诚实报 no_body。
// 判据：tests/site_nav/test_article_contract.py（双向钉 —— 删码、加不在册的码都会红）。
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
    // 快照里的 fc 是构建期已过 normalize+sanitize+deepClean 的成品 ⇒ 这里**只补 cap**，
    // 不重复 normalize（重复跑等于把"哪一层改写了正文"变成查不清的事）。
    const out = { ok: true, url, title: entry.title, content: BODY.capBody(entry.content), source: entry.source };
    cacheSet(url, out);
    res.setHeader('Cache-Control', 'public, max-age=14400, s-maxage=86400');
    return res.json(out);
  }

  let dom;
  let rawHtml = '';
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

    rawHtml = await resp.text();
    // 挑战页闸必须排在 new JSDOM 之前：WAF/CDN 的人机验证壳返回的是 **HTTP 200**，
    // resp.ok 挡不住（这就是"全文可靠性"问题 1 的根因）。不拦的话 Readability 会从
    // 验证壳里抽出导航/脚注当正文，阅读器显示一屏"正在验证您的浏览器"。
    if (BODY.isChallengePage(rawHtml)) {
      return res.json({ ok: false, error: 'challenge_page' });
    }
    dom = new JSDOM(rawHtml, { url });
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
      result = extractGeneric(dom, rawHtml);
    }

    if (!result || !result.content || result.content.length < 50) {
      // 抽不出正文 = no_body（"这篇没有可显示的正文"），与"抓挂了"分成两个码：
      // 前端要能区分"这站本来没正文"（换源/直接看原文）和"我这边失败了"。
      return res.json({ ok: false, error: 'no_body' });
    }

    // 出口闸：Readability 给的是站内相对地址与懒加载属性，不规范化就没有图；
    // cap 必须是码点安全的（裸 slice 会截出半个标签，阅读器 innerHTML 一插就整段崩）。
    // 中间那一格 `delinkNavLinks`（审查 ⑤）：同一个阅读器里，走快照 fc 的文章在构建期早已
    // 脱过站内导航链接（/tag/、/category/…），走这条现抓通道的却还带着 —— 点进去是站点
    // 归档页，而阅读器显示的是"这篇文章的正文"。顺序照运行时 RSS 通道
    // （api/rss.js 的 buildFullContent：normalize → … → deepClean 的 2.5 脱链 → cap）：
    // **必须在 normalize 之后**，否则 `/tag/x` 还没绝对化，只有以 `/` 开头的那一小撮命中；
    // **必须在 cap 之前**，否则截断尾巴上可能留半枚脱链产物。
    // 只搬 2.5 这一把刀：2.6（空锚点整枚删）与逐块推广那两把在构建期锚的是 feed 正文形状，
    // Readability 的产物里没有那些形状（登记为遗留顾虑，不静默扩大范围）。
    const content = stripRemoteExecutables(BODY.capBody(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url))));
    const out = { ok: true, url, title: result.title, content: content, source: result.source };
    // degraded 只在这三格里取值（paywall / short / 不带这个字段）—— 判定住在 body_rules
    if (result.degraded) out.degraded = result.degraded;
    cacheSet(url, out);
    res.setHeader('Cache-Control', 'public, max-age=14400, s-maxage=86400');
    return res.json(out);
  } catch (e) {
    return res.json({ ok: false, error: 'extraction_failed' });
  }
};
