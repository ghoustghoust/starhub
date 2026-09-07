// Vercel Serverless Function：构建日志查询 API
// GET /api/build_log?date=YYYY-MM-DD&type=build&limit=50&offset=0
// GET /api/build_log?summary=1&date=YYYY-MM-DD  → 返回当日摘要

import { readFileSync, readdirSync, existsSync } from 'fs';
import { join } from 'path';

const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

function readLogFiles(dateStr) {
  const logDir = join(process.cwd(), 'build_logs');
  if (!existsSync(logDir)) return [];

  let files;
  if (dateStr) {
    const p = join(logDir, `${dateStr}.jsonl`);
    files = existsSync(p) ? [p] : [];
  } else {
    // 默认读最近 7 天
    files = [];
    const allFiles = readdirSync(logDir).filter(f => f.endsWith('.jsonl')).sort().reverse();
    files = allFiles.slice(0, 7).map(f => join(logDir, f));
  }

  const entries = [];
  for (const fp of files) {
    const lines = readFileSync(fp, 'utf-8').split('\n');
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        entries.push(JSON.parse(line));
      } catch { /* skip malformed */ }
    }
  }
  return entries;
}

export default async function handler(req, res) {
  // CORS
  const origin = (req.headers['origin'] || '').toLowerCase();
  const allowed = ALLOWED_ORIGINS.has(origin);
  res.setHeader('Access-Control-Allow-Origin', allowed ? origin : '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Vary', 'Origin');

  if (req.method === 'OPTIONS') return res.status(204).end();
  if (req.method !== 'GET') return res.status(405).json({ error: 'Method Not Allowed' });

  const q = req.query || {};
  const dateStr = q.date || null;
  const type = q.type || null;
  const limit = Math.min(parseInt(q.limit) || 50, 200);
  const offset = Math.max(parseInt(q.offset) || 0, 0);

  // 摘要模式
  if (q.summary === '1') {
    const entries = readLogFiles(dateStr);
    const builds = entries.filter(e => e.type === 'build');
    const triggers = entries.filter(e => e.type === 'trigger');
    const deploys = entries.filter(e => e.type === 'deploy');
    const refreshes = entries.filter(e => e.type === 'refresh');

    return res.status(200).json({
      date: dateStr || new Date().toISOString().slice(0, 10),
      builds: builds.length,
      triggers: triggers.length,
      deploys: deploys.length,
      refreshes: refreshes.length,
      last_build: builds[0] || null,
      last_deploy: deploys[0] || null,
      total_items_latest: builds[0]?.items_snapshot ?? 0,
      total_items_oldest: builds.length > 1 ? builds[builds.length - 1]?.items_snapshot ?? 0 : 0,
      items_delta: builds.length > 1
        ? (builds[0]?.items_snapshot ?? 0) - (builds[builds.length - 1]?.items_snapshot ?? 0)
        : 0,
    });
  }

  // 列表模式
  let entries = readLogFiles(dateStr);

  // 类型过滤
  if (type) {
    entries = entries.filter(e => e.type === type);
  }

  // 时间倒序
  entries.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

  const total = entries.length;
  const page = entries.slice(offset, offset + limit);

  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  return res.status(200).json({ total, entries: page, limit, offset });
}
