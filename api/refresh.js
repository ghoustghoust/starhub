// Vercel Serverless Function：中转触发 GitHub Actions workflow_dispatch
// 用法：POST /api/refresh → 触发 starhub 仓库 update.yml（ref=main）
// 认证令牌通过 Vercel 环境变量 GH_TOKEN 注入（fine-grained PAT，仅 starhub 仓库 + Actions:write）
export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') { res.status(204).end(); return; }
  if (req.method !== 'POST') { res.status(405).json({ error: 'Method Not Allowed' }); return; }

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
      res.status(200).json({ ok: true });
    } else {
      const text = await r.text();
      res.status(r.status).json({ error: 'GitHub API ' + r.status + ': ' + text.slice(0, 200) });
    }
  } catch (e) {
    res.status(502).json({ error: '网络错误: ' + String(e) });
  }
}
