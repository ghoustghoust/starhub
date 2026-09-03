// Vercel Serverless Function：API-First 实时 RSS 聚合
// GET /api/rss → 返回 JSON（服务端缓存 5 分钟）
// 页面加载时立即调用，获取全部源的最新内容
// 失败时回退到滚动缓存（上次成功抓取的数据）

import { readFileSync } from 'fs';
import { join } from 'path';
import { createHash } from 'crypto';

const FETCH_TIMEOUT = 8000;     // 单源超时 8s
const CONCURRENCY = 10;         // 10 路并发
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
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')  // 先提取 CDATA
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')  // 移除 script
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')    // 移除 style
    .replace(/<[^>]+>/g, '')                            // 移除所有 HTML 标签
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
    .replace(/&#\d+;/g, '')                             // 移除数字实体
    .replace(/&[a-z]+;/gi, '')                          // 移除命名实体
    .replace(/\s+/g, ' ')                               // 合并空白
    .trim();
}

function truncate(text, maxLen) {
  if (!text) return '';
  text = text.trim();
  if (text.length <= maxLen) return text;
  const cut = text.slice(0, maxLen).lastIndexOf('。');
  return (cut > maxLen * 0.5 ? text.slice(0, cut + 1) : text.slice(0, maxLen)) + '…';
}

// ── Feed 解析 ──

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
        items.push({
          title: stripHtml(title),
          link: link || '#',
          summary: truncate(stripHtml(summary), 200),
          pub_date: pubDate || new Date().toISOString(),
        });
      }
    }
    return items;
  }
  // RSS
  const rssItems = xml.match(/<item[^>]*>[\s\S]*?<\/item>/gi) || [];
  for (const item of rssItems.slice(0, maxItems)) {
    const title = extractTag(item, 'title');
    const link = extractTag(item, 'link');
    const desc = extractTag(item, 'description') || extractTag(item, 'content:encoded');
    const pubDate = extractTag(item, 'pubDate') || extractTag(item, 'dc:date');
    if (title) {
      items.push({
        title: stripHtml(title),
        link: link || '#',
        summary: truncate(stripHtml(desc), 200),
        pub_date: pubDate || new Date().toISOString(),
      });
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
      items: items.map(it => ({
        t: it.title,
        u: it.link,
        s: it.summary,
        d: it.pub_date,
      })),
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
    console.log(`[rss] Live fetching ${sources.length} sources...`);
    
    const results = await fetchAllBatched(sources);
    
    // 日期过滤：只保留最近 72 小时的文章
    const cutoff = new Date(now - 72 * 60 * 60 * 1000);
    const filtered = results.map(source => {
      if (!source.items) return source;
      const filteredItems = source.items.filter(item => {
        if (!item.d) return false;  // 无日期则过滤
        try {
          const pubDate = new Date(item.d);
          return pubDate >= cutoff;
        } catch {
          return false;  // 日期解析失败则过滤
        }
      }).map(item => {
        // 应用翻译缓存
        const titleHash = md5(item.t || '');
        const summaryHash = md5(item.s || '');
        const translatedTitle = transCache[titleHash] || item.t;
        const translatedSummary = transCache[summaryHash] || item.s;
        return {
          ...item,
          t: stripHtml(translatedTitle),  // 清理 HTML
          s: stripHtml(translatedSummary),  // 清理 HTML
        };
      });
      return { ...source, items: filteredItems };
    });
    
    const response = {
      t: new Date().toISOString(),
      sources: filtered,
    };
    
    // 更新完整响应缓存
    fullCache = { t: now, v: response };
    
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'public, max-age=300');
    res.setHeader('X-RSS-Cache', 'miss');
    return res.status(200).json(response);
  } catch (err) {
    console.error('[rss] Handler error:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
