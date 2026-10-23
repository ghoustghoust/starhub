import { ALLOWED_ORIGINS } from './config.js';

export function applyCors(req, res, { methods = 'GET, OPTIONS', headers = 'Content-Type', allowNoOrigin = false, publicCors = false } = {}) {
  const origin = String(req.headers.origin || '').toLowerCase();
  // 站点由 Vercel rewrite 代理 Pages，生产域/预览域与 API 同源；这些 Origin 不在
  // 静态白名单里，若直接拒绝会导致同源 POST /api/search 403。
  const host = String(req.headers.host || '').toLowerCase();
  const sameOrigin = Boolean(origin) && Boolean(host) && origin.replace(/^https?:\/\//, '') === host;
  const originAllowed = ALLOWED_ORIGINS.has(origin) || sameOrigin;
  res.setHeader('Vary', 'Origin');
  res.setHeader('Cache-Control', 'no-store');
  // 公开只读数据允许任何 Origin，才能让 CDN 缓存的同一份响应安全地跨域复用；
  // POST 搜索仍走白名单与 X-Search-Key，publicCors 只在 GET 时开启。
  if (publicCors) {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', methods);
    res.setHeader('Access-Control-Allow-Headers', headers);
    return true;
  }
  if (originAllowed) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', methods);
    res.setHeader('Access-Control-Allow-Headers', headers);
  }
  return originAllowed || (allowNoOrigin && !origin);
}

export function setPublicCache(res, seconds, stale = 60) {
  res.setHeader('Cache-Control', `public, s-maxage=${seconds}, stale-while-revalidate=${stale}`);
}

export function rejectMethod(res, allowed = 'GET, OPTIONS') {
  res.setHeader('Allow', allowed);
  res.status(405).json({ error: 'Method Not Allowed' });
}
