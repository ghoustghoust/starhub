// api/test_rss_retention.js —— 运行时留存闸门的自测（node 直跑，退出码非 0 即失败）
// 跑法：node tests/rss_js/test_rss_retention.js
// CI 里由 tests/rss_history/test_retention_js_parity.py 调用（门禁 A2 是 blocking 的 Python 目录），
// 这样不必改 .github/workflows/update.yml 也能把 JS 侧纳入 CI。
'use strict';

const assert = require('assert');
const fs = require('fs');
const R = require('../../lib/rss_retention.js');

const HOUR = 3600000;
const NOW = Date.parse('2026-09-20T12:00:00+08:00');

function iso(hoursAgo) {
  return new Date(NOW - hoursAgo * HOUR).toISOString();
}
function it(u, hoursAgo, extra) {
  return Object.assign({ u: u, t: 't-' + u, d: hoursAgo === null ? '' : iso(hoursAgo) }, extra || {});
}
function src(key, items) {
  return { key: key, name: key, cat: 'ai', color: '#fff', tier: 1, items: items };
}
function run(sources, extra) {
  return R.applyRetention(sources, Object.assign({ nowMs: NOW }, extra || {}));
}
function links(s) {
  return s.map((x) => x.u);
}

const cases = [];
function test(name, fn) { cases.push([name, fn]); }

test('stale fetched item is dropped', () => {
  const out = run([src('a', [it('a', 1), it('old', 100), it('b', 2), it('c', 3)])]);
  assert.deepStrictEqual(links(out.sources[0].items), ['a', 'b', 'c']);
});

test('undatable item is dropped, not anchored to now', () => {
  const out = run([src('a', [it('x', null), it('k1', 1), it('k2', 2), it('k3', 3)])]);
  assert.ok(!links(out.sources[0].items).includes('x'), '无日期条目靠 now 续命');
});

test('epoch placeholder is not a publish date', () => {
  const out = run([src('a', [it('e', null, { d: '0001-01-01T00:00:00+00:00' }),
                             it('k1', 1), it('k2', 2), it('k3', 3)])]);
  assert.ok(!links(out.sources[0].items).includes('e'));
});

test('snapshot-known date rescues a dateless fresh item', () => {
  const known = new Map([['x', iso(20)]]);
  const out = run([src('a', [it('x', null)])], { knownDates: known });
  assert.deepStrictEqual(links(out.sources[0].items), ['x'], '构建期已见过且只有 20h 的条目被误删');
});

test('falsified future date falls back to our own record', () => {
  const known = new Map([['x', iso(10)]]);
  const out = run([src('a', [it('x', null, { d: iso(-200) })])], { knownDates: known });
  assert.deepStrictEqual(links(out.sources[0].items), ['x']);
  // 基准取我方记录的 10h，而不是被证伪的 -200h（那等于永生）
  const a = R.ageH(Object.assign({ _srcKey: 'a' }, it('x', null, { d: iso(-200) })),
                   { nowMs: NOW, knownDates: known });
  assert.ok(Math.abs(a - 10) < 0.5, '判龄应为 10h，实为 ' + a);
});

test('declared tz offset is applied exactly once', () => {
  const item = Object.assign({ _srcKey: 'tz' }, it('z', 70));
  const a = R.ageH(item, { nowMs: NOW, offsets: { tz: -480 } });
  assert.ok(Math.abs(a - 78) < 0.5, '应为 78h，实为 ' + a);
});

test('slow source widens but never past the hard ceiling', () => {
  const weekly = [];
  for (let i = 0; i < 6; i++) weekly.push(it('w' + i, 24 * 7 * i + 1));
  assert.strictEqual(R.windowH(weekly.map((x) => Object.assign({ _srcKey: 's' }, x)),
                               { nowMs: NOW }), 168);
});

test('fast source stays at the 72h baseline', () => {
  const fast = [];
  for (let i = 0; i < 8; i++) fast.push(it('f' + i, i + 1));
  assert.strictEqual(R.windowH(fast.map((x) => Object.assign({ _srcKey: 's' }, x)),
                               { nowMs: NOW }), 72);
});

test('two samples do not widen the window', () => {
  const two = [it('p', 200), it('q', 210)];
  assert.strictEqual(R.windowH(two, { nowMs: NOW }), 72);
});

test('floor keeps the three newest within the ceiling', () => {
  const items = [];
  for (let i = 0; i < 6; i++) items.push(it('s' + i, 90 + i));
  const out = run([src('slow', items)]);
  assert.deepStrictEqual(links(out.sources[0].items), ['s0', 's1', 's2']);
});

test('floor never ships beyond the hard ceiling', () => {
  const items = [];
  for (let i = 0; i < 6; i++) items.push(it('d' + i, 300 + i));
  const out = run([src('dead', items)]);
  assert.deepStrictEqual(links(out.sources[0].items), [], '停更源靠超龄旧文续命');
});

test('floor does not become a loophole', () => {
  const items = [it('f0', 1), it('f1', 2), it('f2', 3), it('f3', 4),
                 it('z0', 100), it('z1', 101), it('z2', 102), it('z3', 103), it('z4', 104)];
  const out = run([src('mix', items)]);
  assert.deepStrictEqual(links(out.sources[0].items), ['f0', 'f1', 'f2', 'f3']);
});

test('order is preserved and stats are real', () => {
  const out = run([src('o', [it('k1', 2), it('gone', 100), it('k2', 5), it('k3', 8)])]);
  assert.deepStrictEqual(links(out.sources[0].items), ['k1', 'k2', 'k3']);
  assert.strictEqual(out.stats.before, 4);
  assert.strictEqual(out.stats.after, 3);
  assert.strictEqual(out.stats.dropped === undefined ? out.stats.before - out.stats.after : 1, 1);
});

// ── fixture 模式：Python 生成输入与期望值，这里只负责用 JS 复算并打印 ──
const fxArg = process.argv.indexOf('--fixture');
if (fxArg >= 0) {
  const path = process.argv[fxArg + 1];
  const fx = JSON.parse(fs.readFileSync(path, 'utf-8'));
  const res = [];
  for (const c of fx.cases) {
    const known = c.known ? new Map(Object.entries(c.known)) : null;
    const opts = { nowMs: Date.parse(c.now), knownDates: known, offsets: c.offsets || null };
    const items = c.items.map((x) => Object.assign({}, x));
    res.push({
      name: c.name,
      windowH: R.windowH(items.map((x) => Object.assign({ _srcKey: c.key }, x)), opts),
      kept: R.applyRetention([src(c.key, items)], opts).sources[0].items.map((x) => x.u),
      ages: items.map((x) => R.ageH(Object.assign({ _srcKey: c.key }, x), opts)),
    });
  }
  process.stdout.write(JSON.stringify(res));
  return;
}

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log('ok  - ' + name); }
  catch (e) { failed++; console.log('FAIL- ' + name + ' :: ' + e.message); }
}
console.log((failed ? 'FAILED ' : 'PASSED ') + (cases.length - failed) + '/' + cases.length);
process.exit(failed ? 1 : 0);
