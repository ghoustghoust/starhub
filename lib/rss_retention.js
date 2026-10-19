// lib/rss_retention.js
// RSS 留存出口闸门（运行时侧；放在 lib/ 而不是 api/，因为 api/ 下每个文件都会被 Vercel 部署成端点）。规则与 build_rss_aggregator.py 的 _apply_retention 一致：
//   窗口 = min(168, max(72, 2 × 该源中位发布间隔))，每源保底 3 条（且保底也受 168h 约束），
//   判不了龄一律丢 —— 不再回退成"当前时间"。
//
// 为什么运行时也要过一遍：?batch=N 与 ?source=KEY 是 api/rss.js 自己发 HTTP 抓上游，
// 完全不吃构建期产物。只裁构建产物会出现"页面刷新一下，4 年前的旧文整批回来"
// （2026-09-20 对抗审查 P0-2 实测）。
//
// 为什么单独成文件、而不是在 rss.js 里就地写一遍：同一套阈值散在两处必然漂移。
// 跨语言一致性由 tests/rss_history/test_retention_js_parity.py 钉住
// （Python 生成 fixture → node 复算 → 逐条比对窗口与去留），改任何一侧忘了另一侧就会红。
'use strict';

const POLICY = {
  baseH: 72,          // 基线窗口 = 用户可见契约
  hardH: 168,         // 硬上限 7 天，放宽与保底共用
  coef: 2.0,          // 窗口 = coef × 中位发布间隔
  floor: 3,           // 每源最少留几条（防整源空白）
  minSamples: 3,      // 样本不足不放宽
  epochYear: 1971,    // 0001-01-01 这类占位值不是发布时间
};

const HOUR_MS = 3600000;
const FUTURE_TOLERANCE_MS = 60 * 60000;   // 与 Python 侧 FUTURE_DATE_TOLERANCE_MIN 同值

const EPOCH_MIN = Date.UTC(POLICY.epochYear, 0, 1);

/** ISO / RFC7216 时间串 → epoch ms；拿不到返回 null（不猜、不用 now 兜底）。 */
function parseMs(s) {
  if (!s || typeof s !== 'string') return null;
  const t = Date.parse(s.trim());
  if (Number.isNaN(t)) return null;
  const y = new Date(t).getUTCFullYear();
  if (y <= POLICY.epochYear) return null;
  return t;
}

/**
 * 判龄基准：条目自身的 d（按源声明的时区偏移校正一次）；
 * 拿不到或被判伪（晚于我方记录）时改用 knownDates[url]（构建期快照里那条的日期，
 * 等价于 Python 侧的 first_seen 回退）。两者都不可得 → null（闸门丢弃）。
 */
function refDateMs(item, opts) {
  const rawKnown = (opts.knownDates && item && item.u) ? opts.knownDates.get(item.u) : null;
  const known = typeof rawKnown === 'number' ? rawKnown : parseMs(rawKnown);
  const offsetMin = (opts.offsets && opts.offsets[item && item._srcKey]) || 0;
  let d = parseMs(item && item.d);
  if (d !== null && offsetMin) d += offsetMin * 60000;
  if (d !== null && known !== null && d - known > FUTURE_TOLERANCE_MS) {
    // 我们"在文章发布前抓到了它" —— 物理不可能，该日期已被证伪，改用我方记录
    return known;
  }
  if (d !== null) return d;
  return known === null ? null : known;
}

function ageH(item, opts) {
  const d = refDateMs(item, opts);
  return d === null ? null : (opts.nowMs - d) / HOUR_MS;
}

/** 该源保留窗口（小时）。中位口径与 Python 一致：排序后取 len//2（偏上那个）。 */
function windowH(items, opts) {
  const ds = [];
  for (const it of items || []) {
    const d = refDateMs(it, opts);
    if (d !== null) ds.push(d);
  }
  if (ds.length < POLICY.minSamples) return POLICY.baseH;
  ds.sort((a, b) => a - b);
  const gaps = [];
  for (let i = 1; i < ds.length; i++) gaps.push((ds[i] - ds[i - 1]) / HOUR_MS);
  gaps.sort((a, b) => a - b);
  const med = gaps[Math.floor(gaps.length / 2)];
  return Math.min(POLICY.hardH, Math.max(POLICY.baseH, POLICY.coef * med));
}

/**
 * 出口闸门：窗口 → 每源保底（受 hardH 约束）。
 * 只删条目、不改顺序（首屏按顺序取前 CHUNK0_SIZE 条）；每源条数上限由构建期负责，
 * 运行时单源最多 30 条（parseFeed 的 maxItems），叠上限是空转，故此处不做。
 */
function applyRetention(sources, opts) {
  const o = Object.assign({ nowMs: Date.now(), offsets: null, knownDates: null }, opts || {});
  let before = 0, after = 0, undatable = 0;
  const out = [];
  for (const src of sources || []) {
    const items = src.items || [];
    before += items.length;
    const forSrc = Object.assign({}, o, {});
    const scored = [];
    items.forEach((it, idx) => {
      const probe = Object.assign({}, it, { _srcKey: src.key });
      const a = ageH(probe, forSrc);
      if (a === null) undatable += 1;
      else scored.push({ idx, age: a, it });
    });
    const win = windowH(items.map((it) => Object.assign({}, it, { _srcKey: src.key })), forSrc);
    let sel = scored.filter((s) => s.age <= win);
    if (sel.length < POLICY.floor) {
      const chosen = new Set(sel.map((s) => s.idx));
      for (const s of scored.slice().sort((a, b) => a.age - b.age)) {
        if (sel.length >= POLICY.floor) break;
        if (s.age > POLICY.hardH) continue;      // 保底不得越过硬上限
        if (!chosen.has(s.idx)) { sel.push(s); chosen.add(s.idx); }
      }
    }
    const kept = sel.sort((a, b) => a.idx - b.idx).map((s) => s.it);
    after += kept.length;
    out.push(Object.assign({}, src, { items: kept }));
  }
  return { sources: out, stats: { before, after, undatable } };
}

module.exports = { POLICY, parseMs, refDateMs, ageH, windowH, applyRetention };
