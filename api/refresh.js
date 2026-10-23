// POST /api/refresh — dispatch this repository's update workflow.
import { GITHUB_REPOSITORY } from './_lib/config.js';
import { applyCors, rejectMethod } from './_lib/http.js';
import { gh } from './_lib/github.js';

export default async function handler(req, res) {
  const allowed = applyCors(req, res, {
    methods: 'POST, OPTIONS',
    headers: 'Content-Type, X-Refresh-Key',
    allowNoOrigin: true,
  });
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'POST') { rejectMethod(res, 'POST, OPTIONS'); return; }
  if (!allowed) { res.status(403).json({ error: 'Forbidden' }); return; }

  const expected = process.env.REFRESH_KEY;
  const keyValid = expected && req.headers['x-refresh-key'] === expected;
  if (!keyValid) { res.status(403).json({ error: 'Forbidden' }); return; }
  if (!process.env.GH_TOKEN) { res.status(500).json({ error: 'GH_TOKEN is not configured' }); return; }

  try {
    await gh(`/repos/${GITHUB_REPOSITORY}/actions/workflows/update.yml/dispatches`, {
      method: 'POST',
      body: { ref: 'main' },
      timeout: 10000,
    });
    res.status(200).json({ ok: true, workflow: 'update.yml' });
  } catch (error) {
    console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'refresh_failed', repo: GITHUB_REPOSITORY, error: String(error) }));
    res.status(502).json({ error: 'Could not dispatch the update workflow' });
  }
}
