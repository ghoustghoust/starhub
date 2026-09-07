// Vercel Serverless Function：API-First 实时 RSS 聚合
// GET /api/rss → 返回 JSON（服务端缓存 5 分钟）
// 页面加载时立即调用，获取全部源的最新内容
// 失败时回退到滚动缓存（上次成功抓取的数据）

import { readFileSync } from 'fs';
import { join } from 'path';
import { createHash } from 'crypto';

const FETCH_TIMEOUT = 5000;     // 单源超时 5s（从8s降低以加快失败速度）
const CONCURRENCY = 20;         // 20 路并发（从10提高到20以加快速度）
const CACHE_TTL = 5 * 60 * 1000;  // 服务端缓存 5 分钟
const UA = 'starhub-rss-aggregator/1.0';

// 滚动缓存：每个源保留上次成功抓取的数据
let rollingCache = new Map();  // key → { items, lastModified }
let fullCache = { t: 0, v: null };  // 完整响应缓存

// ── 加载 API 快照（构建时生成的 72h 累积数据） ──

function loadSnapshot() {
  try {
    const p = join(process.cwd(), 'rss_api_snapshot.json');
    const snap = JSON.parse(readFileSync(p, 'utf-8'));
    const total = (snap.sources || []).reduce((n, s) => n + (s.items || []).length, 0);
    console.log(`[rss] Loaded snapshot: ${(snap.sources || []).length} sources, ${total} items`);
    return snap;
  } catch (err) {
    console.log('[rss] Snapshot not found, falling back to live fetch');
    return null;
  }
}

// ── 加载翻译缓存 ──

function loadTransCache() {
  try {
    const p = join(process.cwd(), 'translations.json');
    const cache = JSON.parse(readFileSync(p, 'utf-8'));
    console.log(`[rss] Loaded ${Object.keys(cache).length} translation cache entries`);
    return cache;
  } catch (err) {
    console.log('[rss] Translation cache not found, using empty cache');
    return {};
  }
}

function md5(text) {
  return createHash('md5').update(text, 'utf-8').digest('hex');
}

// ── 加载源列表 ──

function loadSources() {
  const p = join(process.cwd(), 'rss_sources.json');
  return JSON.parse(readFileSync(p, 'utf-8'));
}

// ── 简易 XML 文本提取 ──

function extractTag(xml, tag) {
  const m = xml.match(new RegExp('<' + tag + '[^>]*>([\\s\\S]*?)</' + tag + '>', 'i'));
  return m ? m[1].trim() : '';
}

function extractAttr(xml, tag, attr) {
  const m = xml.match(new RegExp('<' + tag + '[^>]*\\s' + attr + '="([^"]*)"', 'i'));
  return m ? m[1] : '';
}

function stripHtml(text) {
  if (!text) return '';
  return text
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<!\[CDATA\[/g, '')
    .replace(/\]\]>/g, '')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
    .replace(/&#\d+;/g, '')
    .replace(/&[a-z]+;/gi, '')
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]+>/g, '')
    .replace(/<[^>]*$/, '')   // 移除末尾未闭合的标签片段（如 <video src="..." controls="controls" webkit-playsin…）
    .replace(/\s+/g, ' ')
    .trim();
}

const SAFE_TAGS = new Set(['p','br','img','a','b','i','em','strong','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','figure','figcaption','table','tr','td','th','thead','tbody','span','div','hr','sup','sub','dl','dt','dd','audio','video','source','iframe']);

// 允许的 iframe 域名（YouTube / Vimeo embed）
const SAFE_IFRAME_HOSTS = /youtube\.com|youtu\.be|vimeo\.com/i;

function sanitizeHtml(text) {
  if (!text) return '';
  text = text
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<!\[CDATA\[/g, '')
    .replace(/\]\]>/g, '')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  text = text.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '');
  text = text.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
  // 提取安全 iframe（YouTube / Vimeo），替换为占位符，清洗后还原
  const safeIframes = [];
  const safeIframeRe = /<iframe\s[^>]*src\s*=\s*"([^"]*(?:youtube\.com|youtu\.be|vimeo\.com)[^"]*)"[^>]*>\s*<\/iframe>/gi;
  text = text.replace(safeIframeRe, (match) => {
    safeIframes.push(match);
    return '\x00IFRAME' + (safeIframes.length - 1) + '\x00';
  });
  // 移除剩余非安全 iframe
  text = text.replace(/<iframe[^>]*>[\s\S]*?<\/iframe>/gi, '');
  text = text.replace(/<form[^>]*>[\s\S]*?<\/form>/gi, '');
  text = text.replace(/<[^>]+>/g, (match) => {
    const m = match.match(/^<\/?(\w[\w-]*)/);
    if (!m) return '';
    const tag = m[1].toLowerCase();
    if (!SAFE_TAGS.has(tag)) return '';
    // iframe 二次校验：仅放行 YouTube / Vimeo
    if (tag === 'iframe') {
      const srcMatch = match.match(/src\s*=\s*"([^"]*)"/i);
      if (!srcMatch || !SAFE_IFRAME_HOSTS.test(srcMatch[1])) return '';
    }
    const ALLOWED_ATTRS = {
      img: new Set(['src', 'alt']),
      a: new Set(['href']),
      iframe: new Set(['src', 'width', 'height', 'frameborder', 'allowfullscreen']),
      audio: new Set(['src', 'controls', 'preload']),
      video: new Set(['src', 'controls', 'preload', 'poster', 'width', 'height']),
      source: new Set(['src', 'type']),
    };
    const allowed = ALLOWED_ATTRS[tag] || null;
    const attrs = [];
    const attrRe = /([\w-]+)\s*=\s*"([^"]*)"/g;
    let am;
    while ((am = attrRe.exec(match)) !== null) {
      if (/^on/i.test(am[1])) continue;
      if (am[1].toLowerCase() === 'href' && am[2].trim().toLowerCase().startsWith('javascript:')) continue;
      if (allowed && !allowed.has(am[1].toLowerCase())) continue;
      attrs.push(am[1] + '="' + am[2] + '"');
    }
    // 布尔属性（如 controls）无 ="value"，单独检测
    if (tag === 'audio' || tag === 'video') {
      if (/\bcontrols(?:\s|>|\/)/i.test(match) && (!allowed || allowed.has('controls'))) {
        attrs.push('controls');
      }
    }
    const isClose = match.startsWith('</');
    if (attrs.length) return '<' + (isClose ? '/' : '') + tag + ' ' + attrs.join(' ') + '>';
    return isClose ? '</' + tag + '>' : '<' + tag + '>';
  });
  // 还原安全 iframe
  for (let i = 0; i < safeIframes.length; i++) {
    text = text.replace('\x00IFRAME' + i + '\x00', safeIframes[i]);
  }
  return text.trim();
}

// ── 深度清洗：移除 RSS 正文中的广告、推广、引导关注等噪音 ──

function deepCleanHtml(text) {
  if (!text) return '';
  // 1. 移除广告/推广/订阅/评论相关 class 或 id 的整个元素
  text = text.replace(/<(\w+)[^>]*\b(?:class|id)\s*=\s*"[^"]*\b(?:ad[s_-]?|advert|banner|sponsor|promo|newsletter|subscribe|social-share|share-buttons?|related-posts|recommend|widget|comments?|disqus|pagination|footer-links|follow-us|qrcode|qr-code)[^"]*"[^>]*>[\s\S]*?<\/\1>/gi, '');
  // 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）
  text = text.replace(/<a[^>]*href="[^"]*(?:link-proxy|wechat2rss|mp\.weixin\.qq\.com)[^"]*"[^>]*>[^<]*<\/a>/gi, '');
  text = text.replace(/<a[^>]*>[^<]*\u8df3\u8f6c\u5fae\u4fe1[^<]*<\/a>/gi, '');
  // 2. 逐块检测：剥离内联标签后匹配推广模式
  const promoRe = /\u4ee3\u5f00\u5173\u6ce8|\u957f\u6309\u4e8c\u7ef4\u7801|\u626b\u7801\u5173\u6ce8|\u626b\u4e00\u626b\u5173\u6ce8|\u5fae\u4fe1\u641c\u7d22.*\u5173\u6ce8|\u5173\u6ce8\u516c\u4f17\u53f7|\u5173\u6ce8\u6211\u4eec|\u7acb\u5373\u8d2d\u4e70|\u70b9\u51fb\u9886\u53d6|\u70b9\u51fb\u6ce8\u518c|\u9650\u65f6\u4f18\u60e0|\u79d2\u6740\u6d3b\u52a8|\u52a0\u5165\u793e\u7fa4|\u52a0\u5165\u6211\u4eec|\u52fe\u9009\u5173\u6ce8|\u957f\u6309\u5173\u6ce8|\u8bc6\u522b\u4e8c\u7ef4\u7801|\u4e8c\u7ef4\u7801|\u957f\u6309\u8bc6\u522b|\u5173\u6ce8.*\u516c\u4f17\u53f7|\u5173\u6ce8.*\u5fae\u4fe1|\u70b9\u51fb.*\u8ba2\u9605|\u8ba2\u9605.*\u9891\u9053|\u8ba2\u9605.*\u90ae\u4ef6|\u52a0\u5165.*\u90ae\u4ef6\u5217\u8868|\u5fae\u535a.*\u5173\u6ce8|\u5173\u6ce8.*\u5fae\u535a|\u5206\u4eab.*\u597d\u53cb|\u8f6c\u53d1.*\u670b\u53cb|\u8f6c\u53d1.*\u5173\u6ce8|\u5173\u6ce8.*\u8f6c\u53d1|\u70b9\u8d5e.*\u5173\u6ce8|\u5173\u6ce8.*\u70b9\u8d5e|\u70b9\u8d5e.*\u5728\u770b|\u559c\u6b22.*\u5173\u6ce8|\u559c\u6b22.*\u70b9\u8d5e|\u89c9\u5f97.*\u5173\u6ce8|\u89c9\u5f97.*\u6709\u7528|\u7cbe\u5f69.*\u4e0d\u9519\u8fc7|\u8bf7\u957f\u6309|\u8bf7\u626b\u7801|\u70b9\u51fb\u539f\u6587|\u70b9\u51fb.*\u539f\u6587|\u70b9\u51fb.*\u67e5\u770b\u539f\u6587|buy now|subscribe\s+(?:now|today)|limited.?time|click here to|sign up (?:now|today)|special offer|discount code|use code|free trial|donate (?:now|today)|support us|follow us (?:on|for)|join our|share this (?:article|post)/i;
  text = text.replace(/<(p|div)\b[^>]*>[\s\S]*?<\/\1>/gi, (block) => {
    const plain = block.replace(/<[^>]+>/g, '');
    return promoRe.test(plain) ? '' : block;
  });
  // 3. 移除清洗后残留的空块元素
  text = text.replace(/<(?:p|div|span)\b[^>]*>\s*(?:<br\s*\/?>\s*)*<\/(?:p|div|span)>/gi, '');
  // 4. 压缩连续空行（保留段落间距）
  text = text.replace(/(?:\s*\n){3,}/g, '\n\n');
  return text.trim();
}

function truncate(text, maxLen) {
  if (!text) return '';
  text = text.trim();
  if (text.length <= maxLen) return text;
  const cut = text.slice(0, maxLen).lastIndexOf('。');
  return (cut > maxLen * 0.5 ? text.slice(0, cut + 1) : text.slice(0, maxLen)) + '…';
}

// ── Feed 解析 ──

function extractMediaFromEntry(entry) {
  // enclosure
  const encMatch = entry.match(/<enclosure[^>]*>/i);
  if (encMatch) {
    const typeM = encMatch[0].match(/type\s*=\s*"([^"]*)"/i);
    const urlM = encMatch[0].match(/url\s*=\s*"([^"]*)"/i);
    if (urlM && typeM) {
      const type = typeM[1].toLowerCase();
      if (type.startsWith('audio') || type.startsWith('video')) {
        return { media_url: urlM[1], media_type: type };
      }
    }
  }
  // media:content
  const mcMatch = entry.match(/<media:content[^>]*>/i);
  if (mcMatch) {
    const urlM = mcMatch[0].match(/url\s*=\s*"([^"]*)"/i);
    const medM = mcMatch[0].match(/medium\s*=\s*"([^"]*)"/i);
    if (urlM && medM && (medM[1] === 'audio' || medM[1] === 'video')) {
      return { media_url: urlM[1], media_type: medM[1] };
    }
  }
  return {};
}

function parseFeed(xml, sourceKey, maxItems) {
  const items = [];
  // Atom
  const atomEntries = xml.match(/<entry[^>]*>[\s\S]*?<\/entry>/gi) || [];
  if (atomEntries.length > 0) {
    for (const entry of atomEntries.slice(0, maxItems)) {
      const title = extractTag(entry, 'title');
      const link = extractAttr(entry, 'link', 'href') || extractTag(entry, 'link');
      const summary = extractTag(entry, 'summary') || extractTag(entry, 'content');
      const pubDate = extractTag(entry, 'published') || extractTag(entry, 'updated');
      if (title) {
        const item = {
          title: stripHtml(title),
          link: link || '#',
          summary: truncate(stripHtml(summary), 200),
          pub_date: pubDate || new Date().toISOString(),
        };
        const media = extractMediaFromEntry(entry);
        if (media.media_url) { item.media_url = media.media_url; item.media_type = media.media_type; }
        items.push(item);
      }
    }
    return items;
  }
  // RSS
  const rssItems = xml.match(/<item[^>]*>[\s\S]*?<\/item>/gi) || [];
  for (const item of rssItems.slice(0, maxItems)) {
    const title = extractTag(item, 'title');
    const link = extractTag(item, 'link');
    const desc = extractTag(item, 'description') || '';
    const contentEncoded = extractTag(item, 'content:encoded') || '';
    const fullContent = contentEncoded.length > desc.length ? contentEncoded : '';
    const pubDate = extractTag(item, 'pubDate') || extractTag(item, 'dc:date');
    if (title) {
      const result = {
        title: stripHtml(title),
        link: link || '#',
        summary: truncate(stripHtml(desc || contentEncoded), 200),
        pub_date: pubDate || new Date().toISOString(),
      };
      if (fullContent) {
        result.fullContent = deepCleanHtml(sanitizeHtml(fullContent)).slice(0, 50000);
      }
      // 提取 enclosure / media:content 中的音频视频
      const encMatch = item.match(/<enclosure[^>]*>/i);
      if (encMatch) {
        const typeM = encMatch[0].match(/type\s*=\s*"([^"]*)"/i);
        const urlM = encMatch[0].match(/url\s*=\s*"([^"]*)"/i);
        if (urlM && typeM) {
          const t = typeM[1].toLowerCase();
          if (t.startsWith('audio') || t.startsWith('video')) {
            result.media_url = urlM[1]; result.media_type = t;
          }
        }
      }
      items.push(result);
    }
  }
  return items;
}

// ── 单源抓取 ──

async function fetchOne(source) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT);
  
  try {
    const res = await fetch(source.url, {
      signal: ctrl.signal,
      headers: { 'User-Agent': UA, 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml' },
    });
    clearTimeout(timer);
    
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    
    const xml = await res.text();
    const items = parseFeed(xml, source.key, 30);
    
    // 成功：更新滚动缓存
    const cached = {
      items: items.map(it => {
        const obj = {
          t: it.title,
          u: it.link,
          s: it.summary,
          d: it.pub_date,
        };
        if (it.fullContent) obj.fc = it.fullContent;
        if (it.media_url) { obj.mu = it.media_url; obj.mt = it.media_type; }
        return obj;
      }),
      lastModified: new Date().toUTCString(),
    };
    rollingCache.set(source.key, cached);
    
    return {
      key: source.key,
      name: source.name,
      cat: source.cat,
      color: source.color,
      url: source.url,
      ...cached,
    };
  } catch (err) {
    clearTimeout(timer);
    console.error(`[rss] ${source.key} failed:`, err.message);
    
    // 失败：返回滚动缓存中的旧数据
    const cached = rollingCache.get(source.key);
    if (cached) {
      return {
        key: source.key,
        name: source.name,
        cat: source.cat,
        color: source.color,
        url: source.url,
        ...cached,
        _stale: true,  // 标记为旧数据
      };
    }
    
    // 无缓存：返回空
    return {
      key: source.key,
      name: source.name,
      cat: source.cat,
      color: source.color,
      url: source.url,
      items: [],
      lastModified: new Date().toUTCString(),
      _error: err.message,
    };
  }
}

// ── 并发控制 ──

async function fetchAllBatched(sources) {
  const results = [];
  for (let i = 0; i < sources.length; i += CONCURRENCY) {
    const batch = sources.slice(i, i + CONCURRENCY);
    const batchResults = await Promise.all(batch.map(fetchOne));
    results.push(...batchResults);
  }
  return results;
}

// ── Handler ──

export default async function handler(req, res) {
  // CORS 头：允许 GitHub Pages 跨域访问
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  
  // 处理 OPTIONS 预检请求
  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  // 轻量探测端点：仅返回快照总条数，供前端轮询检测新构建（避免整包拉取 15MB）
  if (req.query && req.query.meta === '1') {
    let metaTotal = 0;
    try {
      const snapMeta = loadSnapshot();
      if (snapMeta && snapMeta.sources) {
        metaTotal = snapMeta.sources.reduce((n, s) => n + (s.items || []).length, 0);
      }
    } catch (e) { /* 快照不可用时返回 0，前端不会触发合并 */ }
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    return res.status(200).json({ total: metaTotal });
  }

  const now = Date.now();
  const isRefresh = req.query && req.query.refresh === '1';
  
  // 检查完整响应缓存（refresh 时跳过缓存）
  if (fullCache.v && now - fullCache.t < CACHE_TTL && !isRefresh) {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'public, max-age=300');
    res.setHeader('X-RSS-Cache', 'hit');
    return res.status(200).json(fullCache.v);
  }
  
  try {
    // 优先返回构建时生成的 API 快照（包含 72h 累积历史数据）
    if (!isRefresh) {
      const snapshot = loadSnapshot();
      if (snapshot && snapshot.sources && snapshot.sources.length > 0) {
        // 对快照数据做 HTML 清理（防御性）
        snapshot.sources = snapshot.sources.map(src => ({
          ...src,
          items: (src.items || []).map(item => ({
            ...item,
            t: stripHtml(item.t || ''),
            s: truncate(stripHtml(item.s || ''), 200),
          })),
        }));
        const total = snapshot.sources.reduce((n, s) => n + s.items.length, 0);
        console.log(`[rss] Serving snapshot: ${snapshot.sources.length} sources, ${total} items`);
        res.setHeader('Content-Type', 'application/json; charset=utf-8');
        res.setHeader('Cache-Control', 'public, max-age=300');
        res.setHeader('X-RSS-Source', 'snapshot');
        return res.status(200).json(snapshot);
      }
    }
    
    // Fallback: 实时抓取 RSS（仅当快照不存在或 refresh=1 时）
    const sources = loadSources();
    const transCache = loadTransCache();  // 加载翻译缓存
    const snapshot = loadSnapshot();
    const snapshotMap = {};
    if (snapshot && snapshot.sources) {
      for (const s of snapshot.sources) {
        snapshotMap[s.key] = s;
      }
    }

    // 分层：T1 实时抓取，T2/T3 从快照读取
    const t1Sources = sources.filter(s => s.tier === 1);
    const otherSources = sources.filter(s => s.tier !== 1);
    console.log(`[rss] T1 live fetch: ${t1Sources.length} sources, T2/T3 from snapshot: ${otherSources.length} sources`);

    const t1Results = await fetchAllBatched(t1Sources);

    // T1 英文源实时翻译
    const t1EnKeys = new Set([
      'agihunt_0', 'openclaw_commits_14', 'hn_newest_56',
      'hn_ai_7', 'hackernews_6', 'hn_show_58',
      'arxiv_ai_4', 'arxiv_ml_5', 'arxiv_nlp_6',
    ]);
    const translateEndpoints = [
      (text) => `https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=${encodeURIComponent(text.substring(0, 500))}`,
      (text) => `https://api.mymemory.translated.net/get?q=${encodeURIComponent(text.substring(0, 500))}&langpair=en|zh-CN`,
      (text) => `https://translate.googleapis.com/translate_a/single?client=dict-chrome&sl=auto&tl=zh-CN&q=${encodeURIComponent(text.substring(0, 500))}`,
    ];

    async function translateText(text) {
      if (!text || !/[a-zA-Z]/.test(text)) return null;
      const hash = md5(text);
      if (transCache[hash]) return transCache[hash];
      for (const makeUrl of translateEndpoints) {
        try {
          const res = await fetch(makeUrl(text), { signal: AbortSignal.timeout(3000) });
          if (!res.ok) continue;
          const data = await res.json();
          let result = null;
          if (Array.isArray(data) && data[0]) {
            result = data[0].map(seg => seg[0]).join('');
          } else if (data && data.responseData) {
            result = data.responseData.translatedText;
          }
          if (result && result !== text) {
            transCache[hash] = result;
            return result;
          }
        } catch { /* try next endpoint */ }
      }
      return null;
    }

    // 翻译 T1 英文源的文章标题和摘要
    const translatePromises = [];
    for (const src of t1Results) {
      if (!t1EnKeys.has(src.key)) continue;
      for (const item of (src.items || [])) {
        if (item.t) translatePromises.push(translateText(item.t).then(r => { if (r) item.t = stripHtml(r); }));
        if (item.s) translatePromises.push(translateText(item.s).then(r => { if (r) item.s = stripHtml(r); }));
      }
    }
    await Promise.all(translatePromises);

    // 日期过滤：只保留最近 72 小时的文章
    const cutoff = new Date(now - 72 * 60 * 60 * 1000);
    const filterItems = (items) => (items || []).filter(item => {
      if (!item.d) return true;  // 无日期保留（快照数据可能缺日期）
      try { return new Date(item.d) >= cutoff; } catch { return false; }
    });

    // 合并 T1 实时 + T2/T3 快照
    const mergedSources = t1Results.map(src => ({
      key: src.key, name: src.name, cat: src.cat, color: src.color,
      tier: 1,
      items: filterItems(src.items).map(item => ({
        ...item,
        t: stripHtml(item.t || ''),
        s: truncate(stripHtml(item.s || ''), 200),
      })),
    })).concat(otherSources.map(src => {
      const snap = snapshotMap[src.key];
      return {
        key: src.key, name: src.name, cat: src.cat, color: src.color,
        tier: src.tier || 3,
        items: filterItems(snap ? snap.items : []).map(item => ({
          ...item,
          t: stripHtml(item.t || ''),
          s: truncate(stripHtml(item.s || ''), 200),
        })),
      };
    }));

    const response = {
      t: new Date().toISOString(),
      sources: mergedSources,
    };

    // 更新完整响应缓存
    fullCache = { t: now, v: response };

    const liveItemCount = t1Results.reduce((n, s) => n + (s.items || []).length, 0);
    const snapItemCount = mergedSources.reduce((n, s) => n + (s.items || []).length, 0);
    console.log(`[rss] Refresh merge: T1 live=${liveItemCount} items from ${t1Sources.length} sources, total merged=${snapItemCount} items`);

    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    if(isRefresh) {
      res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate');
      res.setHeader('X-RSS-Refresh', '1');
    } else {
      res.setHeader('Cache-Control', 'public, max-age=300');
    }
    res.setHeader('X-RSS-Cache', 'miss');
    return res.status(200).json(response);
  } catch (err) {
    console.error('[rss] Handler error:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
