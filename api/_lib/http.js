import { ALLOWED_ORIGINS } from './config.js';

export function applyCors(req, res, { methods = 'GET, OPTIONS', headers = 'Content-Type', allowNoOrigin = false } = {}) {
  const origin = String(req.headers.origin || '').toLowerCase();
  // 站点由 Vercel rewrite 代理 Pages，生产域/预览域与 API 同源；这些 Origin 不在
  // 静态白名单里，若直接拒绝会导致同源 POST /api/search 403。
  const host = String(req.headers.host || '').toLowerCase();
  const sameOrigin = Boolean(origin) && Boolean(host) && origin.replace(/^https?:\/\//, '') === host;
  const originAllowed = ALLOWED_ORIGINS.has(origin) || sameOrigin;
  // API responses must never be reused across origins; otherwise a no-Origin
  // response can be cached and later served to a cross-origin fetch without
  // Access-Control-Allow-Origin.
  res.setHeader('Vary', 'Origin');
  res.setHeader('Cache-Control', 'no-store');
  if (originAllowed) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', methods);
    res.setHeader('Access-Control-Allow-Headers', headers);
  }
  return originAllowed || (allowNoOrigin && !origin);
}

export function rejectMethod(res, allowed = 'GET, OPTIONS') {
  res.setHeader('Allow', allowed);
  res.status(405).json({ error: 'Method Not Allowed' });
}
