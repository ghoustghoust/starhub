import { ALLOWED_ORIGINS } from './config.js';

export function applyCors(req, res, { methods = 'GET, OPTIONS', headers = 'Content-Type', allowNoOrigin = false } = {}) {
  const origin = String(req.headers.origin || '').toLowerCase();
  const originAllowed = ALLOWED_ORIGINS.has(origin);
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
