// Vercel Serverless Function：关注账号动态实时聚合（近 24 小时滚动窗口）
// 用法：GET /api/events → { updated_at, items: [...] }（结构兼容前端 renderFeed）
// 逻辑：关注列表 → 每用户 events/public（最多 2 页）→ 24h 窗口过滤 → 7 类事件映射，
//       与 fetch_and_build.py 的 fetch_following_events() 保持一致
// 防护：CORS 白名单（同 refresh.js，比较时统一小写）；GET 无敏感参数，无需密钥
// 缓存：结果 10 分钟 TTL（Vercel 实例内存）——前端每 30 分钟轮询，命中缓存时零 GitHub 开销
// 限流：关注用户每批并发 3 个；单个用户失败静默跳过（与 Python 版 except break 一致）
const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

const USER = 'Kwei168';
const WINDOW_MS = 24 * 60 * 60 * 1000; // 滚动窗口：最近 24 小时
const TTL = 10 * 60 * 1000;
const CONCURRENCY = 3;

let cache = { t: 0, v: null };

function cacheGet() {
  if (cache.v && Date.now() - cache.t < TTL) return cache.v;
  return null;
}

// 北京时区工具（服务器 UTC → +8）
const CN_OFFSET = 8 * 3600 * 1000;
function cnNow() { return new Date(Date.now() + CN_OFFSET); }
function fmt2(n) { return String(n).padStart(2, '0'); }
function cnStr(dt) {
  return {
    date: dt.getUTCFullYear() + '-' + fmt2(dt.getUTCMonth() + 1) + '-' + fmt2(dt.getUTCDate()),
    time: fmt2(dt.getUTCHours()) + ':' + fmt2(dt.getUTCMinutes()),
  };
}
function parseCn(iso) {
  // "2026-08-18T06:08:00Z" → 北京时间 Date
  const d = new Date(iso);
  return isNaN(d) ? null : new Date(d.getTime() + CN_OFFSET);
}

async function gh(url, token) {
  const r = await fetch(url, {
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'starhub-refresh',
    },
    signal: AbortSignal.timeout(12000),
  });
  if (!r.ok) throw new Error('gh ' + r.status);
  return r.json();
}

async function fetchFollowing(token) {
  const out = [];
  for (let page = 1; page <= 5; page++) {
    const data = await gh('https://api.github.com/users/' + USER + '/following?per_page=100&page=' + page, token);
    out.push(...data.map(x => x.login).filter(Boolean));
    if (data.length < 100) break;
  }
  return out;
}

// 单个用户事件 → 窗口内条目（最多 2 页，与 Python 版一致）
async function fetchUserEvents(user, token, cutoff, today) {
  const items = [];
  for (let page = 1; page <= 2; page++) {
    let evs;
    try {
      evs = await gh('https://api.github.com/users/' + user + '/events/public?per_page=100&page=' + page, token);
    } catch (e) {
      break; // 失败静默跳过该用户（与 Python except break 一致）
    }
    if (!evs || !evs.length) break;
    for (const e of evs) {
      const dt = parseCn(e.created_at);
      if (!dt || dt < cutoff) continue;
      const { date, time } = cnStr(dt);
      const t = e.type;
      const payload = e.payload || {};
      const actor = (e.actor || {}).login || '';
      const repo = (e.repo || {}).name || '';
      const day = date === today ? '今天' : '昨天';
      let item = null;
      if (t === 'CreateEvent' && payload.ref_type === 'repository') {
        item = { kind: 'repo', actor, repo, time, day, date, url: 'https://github.com/' + repo };
      } else if (t === 'WatchEvent' && payload.action === 'started') {
        item = { kind: 'star', actor, repo, time, day, date, url: 'https://github.com/' + repo };
      } else if (t === 'FollowEvent') {
        const target = (payload.target || {}).login || '';
        item = { kind: 'follow', actor, target, time, day, date, url: 'https://github.com/' + target };
      } else if (t === 'PullRequestEvent' && payload.action === 'opened') {
        const pr = payload.pull_request || {};
        item = { kind: 'pr', actor, repo, title: (pr.title || '').slice(0, 60), url: pr.html_url || 'https://github.com/' + repo, time, day, date };
      } else if (t === 'ReleaseEvent' && payload.action === 'published') {
        const release = payload.release || {};
        item = { kind: 'release', actor, repo, tag: release.tag_name || '', url: release.html_url || 'https://github.com/' + repo + '/releases', time, day, date };
      } else if (t === 'PublicEvent') {
        item = { kind: 'public', actor, repo, url: 'https://github.com/' + repo, time, day, date };
      } else if (t === 'PushEvent') {
        const size = payload.size || 0;
        if (size > 0) item = { kind: 'push', actor, repo, size, url: 'https://github.com/' + repo + '/commits', time, day, date };
      }
      if (item) items.push(item);
    }
    // 本页最早一条早于窗口起点则无需继续翻页
    const last = parseCn(evs[evs.length - 1].created_at);
    if (!last || last < cutoff) break;
  }
  return items;
}

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  const allowed = ALLOWED_ORIGINS.has(origin);
  if (allowed) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(allowed ? 204 : 403).end(); return; }
  if (req.method !== 'GET') { res.status(405).json({ error: 'Method Not Allowed' }); return; }
  if (!allowed) { res.status(403).json({ error: 'Forbidden' }); return; }

  const token = process.env.GH_TOKEN;
  if (!token) {
    res.status(500).json({ error: 'GH_TOKEN 未配置（Vercel 环境变量）' });
    return;
  }

  const hit = cacheGet();
  if (hit) { res.status(200).json(hit); return; }

  try {
    const now = cnNow();
    const cutoff = new Date(now.getTime() - WINDOW_MS);
    const today = cnStr(now).date;
    const following = await fetchFollowing(token);

    // 每批 CONCURRENCY 个用户并发，失败用户静默跳过
    const all = [];
    for (let i = 0; i < following.length; i += CONCURRENCY) {
      const batch = following.slice(i, i + CONCURRENCY);
      const results = await Promise.allSettled(batch.map(u => fetchUserEvents(u, token, cutoff, today)));
      for (const r of results) if (r.status === 'fulfilled') all.push(...r.value);
    }
    all.sort((a, b) => (b.date + b.time).localeCompare(a.date + a.time));

    const body = {
      updated_at: cnStr(now).date + ' ' + cnStr(now).time,
      window: '24h',
      items: all,
    };
    cache = { t: Date.now(), v: body };
    res.status(200).json(body);
  } catch (e) {
    res.status(502).json({ error: '上游服务不可用' });
  }
}
