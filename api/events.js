// GET /api/events — recent public activity from the token owner's following list.
import { applyCors, rejectMethod } from './_lib/http.js';
import { ghJson } from './_lib/github.js';

const WINDOW_MS = 24 * 60 * 60 * 1000;
const TTL = 10 * 60 * 1000;
const CONCURRENCY = 3;

let cache = { t: 0, v: null };

const CN_OFFSET = 8 * 3600 * 1000;
const cnNow = () => new Date(Date.now() + CN_OFFSET);
const fmt2 = (n) => String(n).padStart(2, '0');
function cnStr(date) {
  return {
    date: date.getUTCFullYear() + '-' + fmt2(date.getUTCMonth() + 1) + '-' + fmt2(date.getUTCDate()),
    time: fmt2(date.getUTCHours()) + ':' + fmt2(date.getUTCMinutes()),
  };
}
function parseCn(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : new Date(date.getTime() + CN_OFFSET);
}

async function fetchFollowing() {
  const out = [];
  for (let page = 1; page <= 5; page++) {
    const data = await ghJson(`/user/following?per_page=100&page=${page}`, { timeout: 12000 });
    out.push(...data.map((entry) => entry.login).filter(Boolean));
    if (data.length < 100) break;
  }
  return out;
}

async function fetchUserEvents(user, cutoff, today) {
  const items = [];
  for (let page = 1; page <= 2; page++) {
    let events;
    try {
      events = await ghJson(`/users/${encodeURIComponent(user)}/events/public?per_page=100&page=${page}`, { timeout: 12000 });
    } catch {
      break;
    }
    if (!events || !events.length) break;
    for (const event of events) {
      const dt = parseCn(event.created_at);
      if (!dt || dt < cutoff) continue;
      const { date, time } = cnStr(dt);
      const type = event.type;
      const payload = event.payload || {};
      const actor = (event.actor || {}).login || '';
      const repo = (event.repo || {}).name || '';
      const day = date === today ? '今天' : '昨天';
      let item = null;
      if (type === 'CreateEvent' && payload.ref_type === 'repository') {
        item = { kind: 'repo', actor, repo, time, day, date, url: 'https://github.com/' + repo };
      } else if (type === 'WatchEvent' && payload.action === 'started') {
        item = { kind: 'star', actor, repo, time, day, date, url: 'https://github.com/' + repo };
      } else if (type === 'FollowEvent') {
        const target = (payload.target || {}).login || '';
        item = { kind: 'follow', actor, target, time, day, date, url: 'https://github.com/' + target };
      } else if (type === 'PullRequestEvent' && payload.action === 'opened') {
        const pr = payload.pull_request || {};
        item = { kind: 'pr', actor, repo, title: (pr.title || '').slice(0, 60), url: pr.html_url || 'https://github.com/' + repo, time, day, date };
      } else if (type === 'ReleaseEvent' && payload.action === 'published') {
        const release = payload.release || {};
        item = { kind: 'release', actor, repo, tag: release.tag_name || '', url: release.html_url || 'https://github.com/' + repo + '/releases', time, day, date };
      } else if (type === 'PublicEvent') {
        item = { kind: 'public', actor, repo, url: 'https://github.com/' + repo, time, day, date };
      } else if (type === 'PushEvent') {
        const size = payload.size || 0;
        if (size > 0) item = { kind: 'push', actor, repo, size, url: 'https://github.com/' + repo + '/commits', time, day, date };
      }
      if (item) items.push(item);
    }
    const last = parseCn(events[events.length - 1].created_at);
    if (!last || last < cutoff) break;
  }
  return items;
}

export default async function handler(req, res) {
  const allowed = applyCors(req, res, { methods: 'GET, OPTIONS', allowNoOrigin: true });
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'GET') { rejectMethod(res, 'GET, OPTIONS'); return; }
  if (!allowed) { res.status(403).json({ error: 'Forbidden' }); return; }
  if (!process.env.GH_TOKEN) { res.status(500).json({ error: 'GH_TOKEN is not configured' }); return; }

  if (cache.v && Date.now() - cache.t < TTL) { res.status(200).json(cache.v); return; }

  try {
    const now = cnNow();
    const cutoff = new Date(now.getTime() - WINDOW_MS);
    const today = cnStr(now).date;
    const following = await fetchFollowing();
    const all = [];
    for (let index = 0; index < following.length; index += CONCURRENCY) {
      const batch = following.slice(index, index + CONCURRENCY);
      const results = await Promise.allSettled(batch.map((user) => fetchUserEvents(user, cutoff, today)));
      for (const result of results) if (result.status === 'fulfilled') all.push(...result.value);
    }
    all.sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));
    const body = { updated_at: cnStr(now).date + ' ' + cnStr(now).time, window: '24h', items: all };
    cache = { t: Date.now(), v: body };
    res.status(200).json(body);
  } catch {
    res.status(502).json({ error: 'Upstream unavailable' });
  }
}
