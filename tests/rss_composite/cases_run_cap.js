/* tests/rss_composite/cases_run_cap.js
   _applyRunCap() 断言用例 — TDD RED
   运行方式：由 test_run_cap.py 拼接进 harness 后交给 node 执行。 */

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

function skRun(arr) {
  var maxRun = 0, cur = 0, prev = null, worst = null;
  for (var i = 0; i < arr.length; i++) {
    var s = arr[i].sk;
    if (s === prev) { cur++; } else { cur = 1; prev = s; }
    if (cur > maxRun) { maxRun = cur; worst = s; }
  }
  return { maxRun: maxRun, worst: worst };
}

function skSet(arr) {
  return arr.map(function(a){ return a.sk; }).sort().join(',');
}

/* RC-0: _applyRunCap 存在 */
it('RC-0 _applyRunCap 函数存在', function () {
  ok(typeof _applyRunCap === 'function', 'RC-0 _applyRunCap 已定义');
});

/* RC-1: 空数组不抛异常 */
it('RC-1 空数组不抛异常', function () {
  _applyRunCap([], 3);
  ok(true, 'RC-1 空数组安全');
});

/* RC-2: null/undefined 不抛异常 */
it('RC-2 null 不抛异常', function () {
  _applyRunCap(null, 3);
  _applyRunCap(undefined, 3);
  ok(true, 'RC-2 null/undefined 安全');
});

/* RC-3: 单源 10 条 cap=3 → 条目守恒 */
it('RC-3 单源条目守恒', function () {
  var arr = [];
  for (var i = 0; i < 10; i++) arr.push({ sk: 'SA', t: 'a' + i });
  _applyRunCap(arr, 3);
  eq(arr.length, 10, 'RC-3a 10 条不丢');
  eq(arr.every(function(x){ return x.sk === 'SA'; }), true, 'RC-3b 全为同源（无处可换）');
});

/* RC-4: 已满足 cap 的数组不变 */
it('RC-4 已满足 cap 不变', function () {
  var arr = [
    { sk: 'A', t: '1' }, { sk: 'B', t: '2' },
    { sk: 'A', t: '3' }, { sk: 'B', t: '4' }
  ];
  var before = arr.map(function(a){ return a.t; }).join(',');
  _applyRunCap(arr, 3);
  var after = arr.map(function(a){ return a.t; }).join(',');
  eq(after, before, 'RC-4 已满足 cap=3 的数组不变');
});

/* RC-5: 10 条同源 + 10 条异源 cap=3 → maxRun ≤ 3 */
it('RC-5 同源连续被抑制', function () {
  var arr = [];
  for (var i = 0; i < 10; i++) arr.push({ sk: 'DOM', t: 'D' + i });
  for (var i = 0; i < 10; i++) arr.push({ sk: 'O' + (i % 5), t: 'O' + i });
  _applyRunCap(arr, 3);
  var st = skRun(arr);
  ok(st.maxRun <= 3, 'RC-5a maxRun ≤ 3（实得 ' + st.maxRun + '，源 ' + st.worst + '）');
  eq(arr.length, 20, 'RC-5b 条目守恒 20 条');
});

/* RC-6: 交换后元素集合守恒 */
it('RC-6 集合守恒', function () {
  var arr = [];
  for (var i = 0; i < 10; i++) arr.push({ sk: 'DOM', t: 'D' + i });
  for (var i = 0; i < 10; i++) arr.push({ sk: 'O' + (i % 5), t: 'O' + i });
  var before = skSet(arr);
  _applyRunCap(arr, 3);
  var after = skSet(arr);
  eq(after, before, 'RC-6 处理前后 sk 多重集一致');
});

/* RC-7: cap=5 允许更长连续 */
it('RC-7 cap=5 允许更长连续', function () {
  var arr = [];
  for (var i = 0; i < 8; i++) arr.push({ sk: 'DOM', t: 'D' + i });
  for (var i = 0; i < 4; i++) arr.push({ sk: 'O' + i, t: 'O' + i });
  _applyRunCap(arr, 5);
  var st = skRun(arr);
  ok(st.maxRun <= 5, 'RC-7a cap=5 maxRun ≤ 5（实得 ' + st.maxRun + '）');
  eq(arr.length, 12, 'RC-7b 条目守恒');
});

/* RC-8: 数组长度 ≤ cap 时直接返回 */
it('RC-8 短数组直接返回', function () {
  var arr = [{ sk: 'A', t: '1' }, { sk: 'A', t: '2' }];
  _applyRunCap(arr, 5);
  eq(arr[0].t, '1', 'RC-8 短数组不变');
  eq(arr[1].t, '2', 'RC-8b 短数组不变');
});

console.log('\nRESULT: ' + PASS + ' passed, ' + FAIL + ' failed');
if (FAIL) { console.log('FAILED: ' + FAILED_NAMES.join(' | ')); process.exit(1); }
process.exit(0);
