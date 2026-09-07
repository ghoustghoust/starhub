// Vercel Serverless Function：中转触发 GitHub Actions workflow_dispatch
// 用法：POST /api/refresh → 触发 starhub 仓库 update.yml（ref=main）
// 认证令牌通过 Vercel 环境变量 GH_TOKEN 注入（fine-grained PAT，仅 starhub 仓库 + Actions:write）
// 防护：① CORS 仅允许白名单 Origin（页面所在域名，比较时统一小写，兼容 GitHub Pages 域名大小写）；
//       ② 请求须携带 X-Refresh-Key 头，
//       值与 Vercel 环境变量 REFRESH_KEY 一致（弱防护，拦截无头扫描器；静态源码公开故非机密）
const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  const allowed = ALLOWED_ORIGINS.has(origin);
  // 携带正确 X-Refresh-Key 的服务端调用（如 cron-job.org）也放行
  const key = process.env.REFRESH_KEY;
  const keyValid = key && req.headers['x-refresh-key'] === key;
  const pass = allowed || keyValid;
  if (pass) {
    res.setHeader('Access-Control-Allow-Origin', allowed ? origin : '*');
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Refresh-Key');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(pass ? 204 : 403).end(); return; }
  if (req.method !== 'POST') { res.status(405).json({ error: 'Method Not Allowed' }); return; }
  if (!pass) { res.status(403).json({ error: 'Forbidden' }); return; }

  if (!key || !keyValid) {
    res.status(403).json({ error: 'Forbidden' });
    return;
  }

  const token = process.env.GH_TOKEN;
  if (!token) {
    res.status(500).json({ error: 'GH_TOKEN 未配置（Vercel 环境变量）' });
    return;
  }

  try {
    const r = await fetch(
      'https://api.github.com/repos/ghoustghoust/starhub/actions/workflows/update.yml/dispatches',
      {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Accept': 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'Content-Type': 'application/json',
          'User-Agent': 'starhub-refresh',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );
    if (r.status === 204) {
      console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'refresh', origin, key_valid: keyValid, status: 204, ok: true }));
      res.status(200).json({ ok: true });
    } else {
      console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'refresh', origin, key_valid: keyValid, status: r.status, ok: false, error: 'GitHub API 调用失败' }));
      res.status(r.status).json({ error: 'GitHub API 调用失败' });
    }
  } catch (e) {
    console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'refresh', origin, key_valid: keyValid, status: 502, ok: false, error: e.message }));
    res.status(502).json({ error: '上游服务不可用' });
  }
}
