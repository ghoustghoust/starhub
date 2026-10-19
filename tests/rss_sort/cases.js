/* RSS 排序修复 · 断言用例
   运行方式：由 test_rss_sort.py 拼接进 _harness.js 后交给 node 执行。
   约定：一个用例只描述一个行为。 */

var PASS = 0, FAIL = 0, FAILED_NAMES = [];

function reset(sort, art, limit) {
  ART.length = 0;
  (art || []).forEach(function (a) { ART.push(a); });
  sortMode = sort || 'newest';
  wallLimit = (limit === undefined) ? 120 : limit;
  ANALYSIS_DATA = null;
  renderCalls = { chips: 0, wall: 0, panel: 0 };
}

function eq(got, want, msg) {
  var ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) { PASS++; console.log('  ok   ' + msg); }
  else { FAIL++; FAILED_NAMES.push(msg); console.log('  FAIL ' + msg + '\n         got  = ' + JSON.stringify(got) + '\n         want = ' + JSON.stringify(want)); }
}

function ok(cond, msg) { eq(!!cond, true, msg); }

function it(name, fn) {
  try { fn(); }
  catch (e) { FAIL++; FAILED_NAMES.push(name); console.log('  FAIL ' + name + '\n         threw: ' + e.message); }
}

var T = function (s) { return new Date(s).getTime(); };

/* ---------- T1 无日期条目必须沉底（本次缺陷的核心） ---------- */
it('T1 newest: 无日期条目沉底', function () {
  reset('newest', [
    { t: 'nodate', sk: 'S1', date: null },
    { t: 'dated', sk: 'S2', date: '2026-09-14T10:00:00+08:00' }
  ]);
  applySort();
  eq(ART.map(function (a) { return a.t; }), ['dated', 'nodate'], 'T1 无日期沉底');
});

/* ---------- T2 全有日期时严格降序（回归保护） ---------- */
it('T2 newest: 严格时间降序', function () {
  reset('newest', [
    { t: 'mid', sk: 'S1', date: '2026-09-14T10:00:00+08:00' },
    { t: 'new', sk: 'S2', date: '2026-09-14T12:00:00+08:00' },
    { t: 'old', sk: 'S3', date: '2026-09-14T08:00:00+08:00' }
  ]);
  applySort();
  eq(ART.map(function (a) { return a.t; }), ['new', 'mid', 'old'], 'T2 新的在前');
});

/* ---------- T3 两个无日期条目保持稳定相对顺序 ---------- */
it('T3 newest: 无日期之间稳定', function () {
  reset('newest', [
    { t: 'A', sk: 'S1', date: null },
    { t: 'B', sk: 'S2', date: null },
    { t: 'C', sk: 'S3', date: '2026-09-14T10:00:00+08:00' }
  ]);
  applySort();
  eq(ART.map(function (a) { return a.t; }), ['C', 'A', 'B'], 'T3 无日期保持输入顺序');
});

/* ---------- T4 oldest 模式回归：原文注释声称的"无日期沉底"必须真的成立 ---------- */
it('T4 oldest: 无日期仍在末尾', function () {
  reset('oldest', [
    { t: 'nodate', sk: 'S1', date: null },
    { t: 'older', sk: 'S2', date: '2026-09-14T08:00:00+08:00' },
    { t: 'newer', sk: 'S3', date: '2026-09-14T10:00:00+08:00' }
  ]);
  applySort();
  eq(ART.map(function (a) { return a.t; }), ['older', 'newer', 'nodate'], 'T4 升序下无日期沉底');
});

/* ---------- T5 active 模式：同一反转也要被修掉 ---------- */
it('T5 active: 无日期不置顶', function () {
  reset('active', [
    { t: 'nodate', sk: 'S1', date: null },
    { t: 'datedA', sk: 'S2', date: '2026-09-14T10:00:00+08:00' },
    { t: 'datedB', sk: 'S3', date: '2026-09-14T09:00:00+08:00' }
  ]);
  applySort();
  ok(ART[0].date, 'T5 active 首位必须有日期');
  eq(ART.map(function (a) { return a.t; }), ['datedA', 'datedB', 'nodate'], 'T5 active 全序');
});

/* ---------- T6 刷新路径纠错：added=0 且首屏被无日期污染时必须规范化 ---------- */
it('T6 刷新: added=0 仍纠正被污染的首屏', function () {
  reset('newest', [
    { t: 'n1', sk: 'S1', u: 'https://x/1', date: null },
    { t: 'n2', sk: 'S1', u: 'https://x/2', date: null },
    { t: 'd1', sk: 'S2', u: 'https://y/1', date: '2026-09-14T10:00:00+08:00' }
  ], 3);
  var payload = { sources: [{ key: 'S1', name: 's', tier: 1, items: [{ t: 'n1', u: 'https://x/1' }] }] };
  _mergeRemoteSources(payload);
  ok(ART[0].date, 'T6 规范化后首位有日期');
  eq(ART[0].t, 'd1', 'T6 首位是被污染前的正常条目');
});

/* ---------- T7 刷新路径的时间口径：必须按绝对时间，不得回退字符串比较 ---------- */
it('T7 刷新: 混合时区按绝对时间排序', function () {
  reset('newest', [
    { t: 'plus8', sk: 'S1', u: '#', ti: 1, date: '2026-09-14T09:00:00+08:00' }
  ], 120);
  var payload = { sources: [{ key: 'S9', name: 's', tier: 1, items: [{ t: 'utc', u: 'https://new/1', d: '2026-09-14T02:00:00Z' }] }] };
  _mergeRemoteSources(payload);
  eq(ART[0].t, 'utc', 'T7 02:00Z(=10:00+08) 应排在 01:00Z(=09:00+08) 之前');
});

/* ---------- T8 幂等：连续两次 applySort 结果不变（防刷新抖动） ---------- */
it('T8 newest: applySort 幂等', function () {
  reset('newest', [
    { t: 'a', sk: 'S1', date: '2026-09-14T10:00:00+08:00' },
    { t: 'b', sk: 'S2', date: null },
    { t: 'c', sk: 'S3', date: '2026-09-14T12:00:00+08:00' }
  ]);
  applySort();
  var first = ART.map(function (a) { return a.t; }).join(',');
  applySort();
  var second = ART.map(function (a) { return a.t; }).join(',');
  eq(second, first, 'T8 两次排序结果一致');
});

/* ---------- T9 quality 模式：无日期置顶是合法的，不得触发纠错重渲染 ----------
   否则 applySort() 在 quality 下会再次把无日期条目按源质量顶回首屏，
   谓词恒真 → 每次定时刷新都重渲染（静默闪烁）。 */
it('T9 quality: 不得触发纠错重渲染', function () {
  reset('quality', [
    { t: 'n1', sk: 'S1', u: 'https://x/1', ti: 1, date: null },
    { t: 'n2', sk: 'S1', u: 'https://x/2', ti: 1, date: null },
    { t: 'd1', sk: 'S2', u: 'https://y/1', ti: 1, date: '2026-09-14T10:00:00+08:00' }
  ], 3);
  ANALYSIS_DATA = { quality: { S1: 100, S2: 1 } };
  var payload = { sources: [{ key: 'S1', name: 's', tier: 1, items: [{ t: 'n1', u: 'https://x/1' }] }] };
  _mergeRemoteSources(payload);
  eq(renderCalls.wall, 0, 'T9 quality 模式零重渲染');
});

/* ---------- T10 相等有效日期：比较器必须满足反对称性 ----------
   缺 if(tx===ty) return 0 时 cmp(x,y) 与 cmp(y,x) 会同返 -1，破坏排序的一致性。 */
it('T10 相等日期：比较返回 0 且顺序稳定', function () {
  eq(_dateCmpDesc('2026-09-14T10:00:00+08:00', '2026-09-14T10:00:00+08:00'), 0, 'T10 相等日期比较返回 0');
  eq(_dateCmpDesc('2026-09-14T02:00:00Z', '2026-09-14T10:00:00+08:00'), 0, 'T10 等价时区表示返回 0');
  reset('newest', [
    { t: 'a', sk: 'S1', date: '2026-09-14T10:00:00+08:00' },
    { t: 'b', sk: 'S2', date: '2026-09-14T10:00:00+08:00' },
    { t: 'c', sk: 'S3', date: '2026-09-14T11:00:00+08:00' }
  ]);
  applySort();
  eq(ART.map(function (a) { return a.t; }).join(','), 'c,a,b', 'T10 相等日期保持输入顺序');
});

/* ---------- T11 日期降级标记必须穿透 buildArt ----------
   Python 侧对「无 pub_date 但有 first_seen」的条目写入 date_fallback（降级排序键），
   前端必须把该标记带到 ART——否则会把「收录时间」冒充成「发布时间」展示给用户。 */
it('T11 buildArt: 传播 date_fallback 标记', function () {
  SOURCES = [{ key: 'S1', name: 's', cat: 'news', color: '#000', tier: 2, items: [
    { title: '\u6709\u65e5\u671f', link: 'https://a/1', pub_date: '2026-09-14T10:00:00+08:00' },
    { title: '\u964d\u7ea7', link: 'https://a/2', pub_date: '2026-09-13T14:19:04+08:00', date_fallback: true }
  ] }];
  wallLimit = 120;
  buildArt();
  var byT = {};
  ART.forEach(function (a) { byT[a.t] = a; });
  eq(byT['\u6709\u65e5\u671f'].dfb, false, 'T11a 真实发布时间条目 dfb=false');
  eq(byT['\u964d\u7ea7'].dfb, true, 'T11b 降级条目 dfb=true');
  eq(ART[0].t, '\u6709\u65e5\u671f', 'T11c 降级条目按时间正常参与排序（不被沉底）');
});

/* ---------- T12 降级时间的展示必须区分「发布时间」与「收录时间」 ---------- */
it('T12 _dynTime: 降级条目显示「收录」前缀', function () {
  var twoHoursAgo = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
  eq(_dynTime({ date: twoHoursAgo, dfb: true }), '\u6536\u5f55 2 \u5c0f\u65f6\u524d', 'T12a 降级 → 收录 X 小时前');
  eq(_dynTime({ date: twoHoursAgo }), '2 \u5c0f\u65f6\u524d', 'T12b 正常 → 不带前缀');
  eq(_dynTime({ date: null, dfb: true }), '', 'T12c 无日期且无 time_str → 空');
});

/* ---------- T13 钳制：多条未来条目不得被折叠成同一时刻 ----------
   旧实现把所有未来日期一律改写成 `now` → N 条获得**完全相同**的时刻，
   默认降序排序下同刻并列、整块钉在首屏顶部（2026-09-14：超能网 6 条占据前 7 位中的 6 位）。
   正确行为：保序回拉——最新的一条拉到 now，其余按原始间距排在它之前。 */
it('T13 钳制: 未来条目保序回拉，不制造同刻并列', function () {
  var nowMs = Date.now();
  var t1 = new Date(nowMs + 2 * 3600 * 1000).toISOString();
  var t2 = new Date(nowMs + 5 * 3600 * 1000).toISOString();
  SOURCES = [{ key: 'S1', name: 's', cat: 'news', color: '#000', tier: 2, items: [
    { title: '\u8f83\u65e9\u7684\u672a\u6765', link: 'https://f/1', pub_date: t1 },
    { title: '\u8f83\u665a\u7684\u672a\u6765', link: 'https://f/2', pub_date: t2 }
  ] }];
  wallLimit = 120;
  buildArt();
  var byT = {};
  ART.forEach(function (a) { byT[a.t] = a; });
  var d1 = new Date(byT['\u8f83\u65e9\u7684\u672a\u6765'].date).getTime();
  var d2 = new Date(byT['\u8f83\u665a\u7684\u672a\u6765'].date).getTime();
  ok(d1 !== d2, 'T13a 两条未来条目不得同刻（旧实现此处相等）');
  ok(d1 < d2, 'T13b 保持原始先后顺序');
  ok(d2 <= Date.now(), 'T13c 不越界：最晚的一条也不超过 now');
  ok((Date.now() - d2) < 5000, 'T13d 最新的一条被拉到 now 附近');
  eq(ART[0].t, '\u8f83\u665a\u7684\u672a\u6765', 'T13e 较晚者仍在前');
});

/* ---------- T14 钳制：过去日期一律不得被改动（回归保护） ---------- */
it('T14 钳制: 过去日期原样保留', function () {
  var past = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
  SOURCES = [{ key: 'S1', name: 's', cat: 'news', color: '#000', tier: 2, items: [
    { title: '\u8fc7\u53bb', link: 'https://p/1', pub_date: past }
  ] }];
  wallLimit = 120;
  buildArt();
  eq(ART[0].date, past, 'T14 过去日期未被改写');
});

console.log('\nRESULT: ' + PASS + ' passed, ' + FAIL + ' failed');
if (FAIL) { console.log('FAILED: ' + FAILED_NAMES.join(' | ')); process.exit(1); }
process.exit(0);
