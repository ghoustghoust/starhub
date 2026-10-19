/* tests/rss_composite/cases_refresh_tags.js
   tags 字段穿透断言用例 — TDD RED
   覆盖 issue M5：tags 在两条白名单（buildArt / _mergeRemoteSources）处被丢弃，
   打标结果成为死数据，前端消费不到。
   运行方式：由 test_refresh_tags_js.py 抽取真实源码拼接后交给 node 执行。 */

var PASS = 0, FAIL = 0, FAILED_NAMES = [];

function eq(got, want, msg) {
  var ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) { PASS++; console.log('  ok   ' + msg); }
  else { FAIL++; FAILED_NAMES.push(msg); console.log('  FAIL ' + msg + '\n         got  = ' + JSON.stringify(got) + '\n         want = ' + JSON.stringify(want)); }
}
function ok(cond, msg) { eq(!!cond, true, msg); }
function it(name, fn) {
  try { fn(); } catch (e) { FAIL++; FAILED_NAMES.push(name); console.log('  FAIL ' + name + '\n         threw: ' + e.message); }
}

/* 每例前重置：buildArt 会重建 ART，SOURCES 由用例填充 */
function reset(sources) {
  ART.length = 0;
  SOURCES.length = 0;
  for (var i = 0; i < sources.length; i++) SOURCES.push(sources[i]);
}

/* ── 构建期通道：buildArt() ── */

/* M5-1: item 带 tags → ART 条目必须带同样的 tags */
it('M5-1 buildArt 把 tags 带进 ART', function () {
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [
    { title: 'OpenAI 发布新模型', title_zh: 'OpenAI 发布新模型', summary_zh: '',
      link: 'https://x/1', pub_date: '2026-09-14T10:00:00+08:00', tags: ['openai', '大模型'] }
  ] }]);
  buildArt();
  eq(ART.length, 1, 'M5-1a 条目数 = 1');
  eq(ART[0].tags, ['openai', '大模型'], 'M5-1b tags 穿透 buildArt');
});

/* M5-2: item 不带 tags → 不得伪造（必须为空值，而非凭空造一个） */
it('M5-2 无 tags 时不伪造', function () {
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [
    { title: '无标签文章', title_zh: '无标签文章', summary_zh: '',
      link: 'https://x/2', pub_date: '2026-09-14T10:00:00+08:00' }
  ] }]);
  buildArt();
  eq(ART.length, 1, 'M5-2a 条目数 = 1');
  ok(ART[0].tags === undefined || ART[0].tags === null || (Array.isArray(ART[0].tags) && ART[0].tags.length === 0),
     'M5-2b 无 tags 时为空值（got ' + JSON.stringify(ART[0].tags) + '）');
});

/* M5-3: tags 类型守卫 —— 下游可能直接读 tags.length，空字符串/数字会崩 */
it('M5-3 tags 类型守卫', function () {
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [
    { title: 'A', link: 'https://x/a', pub_date: '2026-09-14T10:00:00+08:00', tags: ['ok'] },
    { title: 'B', link: 'https://x/b', pub_date: '2026-09-14T09:00:00+08:00', tags: [] },
    { title: 'C', link: 'https://x/c', pub_date: '2026-09-14T08:00:00+08:00' }
  ] }]);
  buildArt();
  eq(ART.length, 3, 'M5-3a 条目数 = 3');
  var okTypes = ART.every(function (a) {
    return a.tags === undefined || a.tags === null || Array.isArray(a.tags);
  });
  ok(okTypes, 'M5-3b 每条 tags 均为数组或空值（防空字符串/数字混入）');
});

/* ── 刷新通道：_mergeRemoteSources() ── */

/* M5-4: 远端条目带 tags → 合并进 ART 后必须保留 */
it('M5-4 _mergeRemoteSources 保留 tags', function () {
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [
    { title: '已有文章', title_zh: '已有文章', link: 'https://x/old',
      pub_date: '2026-09-14T08:00:00+08:00', tags: ['old'] }
  ] }]);
  buildArt();
  var n = _mergeRemoteSources({ sources: [
    { key: 'S9', name: '远端源', cat: 'ai', color: '#222', tier: 1, items: [
      { t: '远端新文章', s: '', u: 'https://x/new', d: '2026-09-14T12:00:00+08:00', tags: ['远端标签', 'agent'] }
    ] }
  ] });
  eq(n, 1, 'M5-4a 新增 1 条');
  var fresh = null;
  for (var i = 0; i < ART.length; i++) { if (ART[i].u === 'https://x/new') fresh = ART[i]; }
  ok(fresh !== null, 'M5-4b 新条目已进入 ART');
  eq(fresh && fresh.tags, ['远端标签', 'agent'], 'M5-4c tags 穿透刷新通道');
});

/* M5-5: 远端条目不带 tags → 不伪造，且不崩 */
it('M5-5 远端无 tags 时不伪造', function () {
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [
    { title: '已有文章', link: 'https://x/old', pub_date: '2026-09-14T08:00:00+08:00' }
  ] }]);
  buildArt();
  _mergeRemoteSources({ sources: [
    { key: 'S9', name: '远端源', cat: 'ai', color: '#222', tier: 1, items: [
      { t: '远端无标签', s: '', u: 'https://x/new2', d: '2026-09-14T12:00:00+08:00' }
    ] }
  ] });
  var fresh = null;
  for (var i = 0; i < ART.length; i++) { if (ART[i].u === 'https://x/new2') fresh = ART[i]; }
  ok(fresh !== null, 'M5-5a 新条目已进入 ART');
  ok(fresh && (fresh.tags === undefined || fresh.tags === null || (Array.isArray(fresh.tags) && fresh.tags.length === 0)),
     'M5-5b 无 tags 时为空值（got ' + JSON.stringify(fresh && fresh.tags) + '）');
});

/* M5-6: 两条通道对同一份 tags 的产出形状一致（防只修一条） */
it('M5-6 两条通道形状一致', function () {
  var tags = ['a1', 'b2', 'c3'];
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [
    { title: '通道A', title_zh: '通道A', link: 'https://x/t1',
      pub_date: '2026-09-14T10:00:00+08:00', tags: tags }
  ] }]);
  buildArt();
  var viaBuild = ART[0].tags;
  reset([{ key: 'S1', name: '源一', cat: 'ai', color: '#111', tier: 2, items: [] }]);
  buildArt();
  _mergeRemoteSources({ sources: [
    { key: 'S9', name: '远端源', cat: 'ai', color: '#222', tier: 1, items: [
      { t: '通道B', s: '', u: 'https://x/t2', d: '2026-09-14T10:00:00+08:00', tags: tags }
    ] }
  ] });
  var viaRemote = ART[0].tags;
  eq(viaBuild, tags, 'M5-6a 构建期通道形状正确');
  eq(viaRemote, tags, 'M5-6b 刷新通道形状与构建期一致');
  eq(Array.isArray(viaBuild) && Array.isArray(viaRemote), true, 'M5-6c 两通道均为数组');
});

console.log('\nRESULT: ' + PASS + ' passed, ' + FAIL + ' failed');
if (FAIL) { console.log('FAILED: ' + FAILED_NAMES.join(' | ')); process.exit(1); }
process.exit(0);
