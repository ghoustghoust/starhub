/* tests/rss_composite/cases_diverse.js
   weightedShuffle() 断言用例 — TDD RED
   运行方式：由 test_diverse_js.py 拼接进 harness 后交给 node 执行。 */

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

var _Q = {}; // quality map: source_key → score (0-100)

/* D1: 空数组不抛异常 */
it('D1 空数组返回空', function () {
  eq(weightedShuffle([], 120, _Q), [], 'D1 空输入 → 空输出');
});

/* D2: 单条原样返回 */
it('D2 单条原样返回', function () {
  var a = [{ t: 'only', date: '2026-09-14T10:00:00+08:00', sk: 'S1' }];
  var r = weightedShuffle(a, 120, _Q);
  eq(r.length, 1, 'D2 单条返回 1 篇');
  eq(r[0].t, 'only', 'D2 内容不变');
});

/* D3: 窗口=0 时退化为纯时间降序（不打散） */
it('D3 窗口=0 退化为时间降序', function () {
  var arts = [
    { t: 'old', date: '2026-09-14T08:00:00+08:00', sk: 'S1' },
    { t: 'new', date: '2026-09-14T12:00:00+08:00', sk: 'S2' },
    { t: 'mid', date: '2026-09-14T10:00:00+08:00', sk: 'S3' }
  ];
  var r = weightedShuffle(arts, 0, _Q);
  eq(r.map(function (a) { return a.t; }), ['new', 'mid', 'old'], 'D3 窗口=0 严格时间降序');
});

/* D4: 无日期条目沉底，不丢失 */
it('D4 无日期沉底不丢失', function () {
  var arts = [
    { t: 'nodate', date: null, sk: 'S1' },
    { t: 'dated', date: '2026-09-14T10:00:00+08:00', sk: 'S2' }
  ];
  var r = weightedShuffle(arts, 120, _Q);
  eq(r.length, 2, 'D4a 条目数守恒');
  eq(r[r.length - 1].t, 'nodate', 'D4b 无日期沉底');
});

/* D5: 同窗口内高信誉源有更高概率排在前面（统计测试：100 次中 ≥60 次 S-Hi 在 S-Lo 前） */
it('D5 高信誉源在同窗口内倾向靠前', function () {
  var qmap = { 'S-HI': 90, 'S-LO': 10 };
  var baseDate = '2026-09-14T10:00:00+08:00';
  var hiFirst = 0;
  for (var trial = 0; trial < 100; trial++) {
    var arts = [
      { t: 'lo', date: baseDate, sk: 'S-LO' },
      { t: 'hi', date: baseDate, sk: 'S-HI' }
    ];
    var r = weightedShuffle(arts, 120, qmap);
    if (r[0].t === 'hi') hiFirst++;
  }
  ok(hiFirst >= 60, 'D5 高信誉(90) vs 低信誉(10)：100 次中高信誉排首位 ≥60 次（实际 ' + hiFirst + '）');
});

/* D6: 不同窗口的文章不被打散（时序保持） */
it('D6 跨窗口不打散', function () {
  var arts = [
    { t: 'far-old', date: '2026-09-14T06:00:00+08:00', sk: 'S1' },
    { t: 'recent', date: '2026-09-14T12:00:00+08:00', sk: 'S2' }
  ];
  // 窗口 60 分钟，两条间隔 6h >> 60min → 各自独立窗口 → 按时间降序
  var r = weightedShuffle(arts, 60, _Q);
  eq(r.map(function (a) { return a.t; }), ['recent', 'far-old'], 'D6 跨窗口保持时间降序');
});

/* D7: 不修改入参数组（纯函数） */
it('D7 不修改入参', function () {
  var arts = [
    { t: 'a', date: '2026-09-14T10:00:00+08:00', sk: 'S1' },
    { t: 'b', date: '2026-09-14T11:00:00+08:00', sk: 'S2' }
  ];
  var orig = JSON.parse(JSON.stringify(arts));
  weightedShuffle(arts, 120, _Q);
  eq(arts.map(function (a) { return a.t; }), orig.map(function (a) { return a.t; }), 'D7 入参顺序不变');
});

/* D8: 确定性 — quality 全相等时，同输入同输出（不引入随机性） */
it('D8 quality 全相等时同输入同输出', function () {
  var qmap = { 'S1': 50, 'S2': 50, 'S3': 50 };
  var baseDate = '2026-09-14T10:00:00+08:00';
  var arts = [
    { t: 'a', date: baseDate, sk: 'S1' },
    { t: 'b', date: baseDate, sk: 'S2' },
    { t: 'c', date: baseDate, sk: 'S3' }
  ];
  var r1 = weightedShuffle(arts, 120, qmap).map(function (a) { return a.t; }).join(',');
  var r2 = weightedShuffle(arts, 120, qmap).map(function (a) { return a.t; }).join(',');
  eq(r1, r2, 'D8 两次调用结果一致（确定性）');
});

/* D9: quality 缺失的源按默认值 50 处理 */
it('D9 quality 缺失按默认值', function () {
  var arts = [
    { t: 'no-q', date: '2026-09-14T10:00:00+08:00', sk: 'UNKNOWN' },
    { t: 'has-q', date: '2026-09-14T10:00:00+08:00', sk: 'KNOWN' }
  ];
  var r = weightedShuffle(arts, 120, { 'KNOWN': 90 });
  eq(r.length, 2, 'D9 不丢条目');
});

/* D10: 条目总数守恒 */
it('D10 条目总数守恒', function () {
  var arts = [];
  for (var i = 0; i < 50; i++) {
    arts.push({ t: 'a' + i, date: '2026-09-14T10:00:00+08:00', sk: 'S' + (i % 5) });
  }
  var r = weightedShuffle(arts, 120, _Q);
  eq(r.length, 50, 'D10 50 条进 50 条出');
});

/* D11: applySort diverse 模式调用 weightedShuffle */
it('D11 applySort diverse 模式', function () {
  ART.length = 0;
  ART.push(
    { t: 'lo', sk: 'S-LO', date: '2026-09-14T10:00:00+08:00', ti: 2 },
    { t: 'hi', sk: 'S-HI', date: '2026-09-14T10:00:00+08:00', ti: 2 }
  );
  sortMode = 'diverse';
  ANALYSIS_DATA = { quality: { 'S-HI': 90, 'S-LO': 10 } };
  applySort();
  eq(ART.map(function (a) { return a.t; }).join(','), 'hi,lo', 'D11 diverse 模式高信誉靠前');
  sortMode = 'newest';
  ANALYSIS_DATA = null;
});

/* D12: diverse 模式条目守恒——打散不得丢篇 */
it('D12 diverse 模式条目守恒', function () {
  ART.length = 0;
  for (var i = 0; i < 20; i++) {
    ART.push({ t: 'a' + i, sk: 'S' + (i % 4), date: '2026-09-14T10:0' + (i % 10) + ':00+08:00', ti: 2 });
  }
  sortMode = 'diverse';
  ANALYSIS_DATA = { quality: { S0: 90, S1: 70, S2: 50, S3: 30 } };
  applySort();
  eq(ART.length, 20, 'D12 diverse 模式不丢条目');
  sortMode = 'newest';
  ANALYSIS_DATA = null;
});

/* D13: 窗口=0 必须与纯时间降序逐条一致（V2 的可执行形态） */
it('D13 窗口=0 与纯时间降序逐条一致', function () {
  var arts = [];
  for (var i = 0; i < 40; i++) {
    arts.push({
      t: 'a' + i,
      sk: 'S' + (i % 6),
      date: (i % 7 === 0) ? null : '2026-09-14T' + (10 + (i % 12)) + ':' + (i % 60) + ':00+08:00'
    });
  }
  var got = weightedShuffle(arts, 0, _Q).map(function (a) { return a.t; });
  var want = arts.slice().sort(function (a, b) { return _dateCmpDesc(a.date, b.date); })
                        .map(function (a) { return a.t; });
  eq(got, want, 'D13 窗口=0 输出 == 纯时间降序（含无日期沉底）');
});

/* D14: 任意窗口下条目集合守恒（打散不得吞掉或复制条目） */
it('D14 窗口内条目集合守恒', function () {
  var arts = [];
  for (var i = 0; i < 30; i++) {
    arts.push({ t: 'x' + i, sk: 'S' + (i % 3), date: '2026-09-14T10:' + (i % 50) + ':00+08:00' });
  }
  var got = weightedShuffle(arts, 120, { S0: 95, S1: 40, S2: 5 }).map(function (a) { return a.t; }).sort();
  var want = arts.map(function (a) { return a.t; }).sort();
  eq(got, want, 'D14 打散前后条目集合完全一致');
});

/* D15: 集成契约 — topic 模式不得触发 weightedShuffle（与 diverse 互斥） */
it('D15 topic 模式不走打散', function () {
  ART.length = 0;
  ART.push(
    { t: 'lo', sk: 'S-LO', date: '2026-09-14T10:00:00+08:00', ti: 2 },
    { t: 'hi', sk: 'S-HI', date: '2026-09-14T10:00:00+08:00', ti: 2 }
  );
  sortMode = 'topic';
  ANALYSIS_DATA = { quality: { 'S-HI': 90, 'S-LO': 10 } };
  applySort();
  /* topic 下若误走打散，lo/hi 会被换成 hi,lo */
  eq(ART.map(function (a) { return a.t; }).join(','), 'lo,hi', 'D15 topic 模式保持时间序，不打散');
  sortMode = 'newest';
  ANALYSIS_DATA = null;
});

/* D16: 集成契约 — sortMode 白名单规范化非法值（不产生未定义分支）
   旧版 D16 的空洞：赋值非法值后立刻覆盖成 newest，中间没有 applySort()，
   非法值从未被消费；末条 ok(diverseOrder.length>0) 取自前半段，恒真。
   现改为直接测白名单函数 + 让非法值真正流经 applySort()。 */
it('D16 非法 sortMode 被白名单规范化', function () {
  ART.length = 0;
  ART.push(
    { t: 'old', sk: 'S1', date: '2026-09-14T08:00:00+08:00', ti: 2 },
    { t: 'new', sk: 'S2', date: '2026-09-14T12:00:00+08:00', ti: 2 }
  );
  ok(typeof _normSortMode === 'function', 'D16a 存在 _normSortMode 白名单函数');
  eq(_normSortMode('不存在的模式'), 'newest', 'D16b 非法值 → newest');
  eq(_normSortMode(null), 'newest', 'D16c null → newest');
  eq(_normSortMode(''), 'newest', 'D16d 空串 → newest');
  eq(_normSortMode('diverse'), 'diverse', 'D16e 合法值 diverse 保留');
  eq(_normSortMode('quality'), 'quality', 'D16f 合法值 quality 保留');
  /* 非法值真正被消费，产出合法时间降序 */
  sortMode = _normSortMode('不存在的模式');
  eq(sortMode, 'newest', 'D16g 非法值经白名单后成为 newest');
  applySort();
  eq(ART.map(function (a) { return a.t; }).join(','), 'new,old', 'D16h 非法值消费后产出时间降序');
  sortMode = 'newest';
});

/* D17: 刷新通道边界 — 条目缺 tags 字段时 diverse 不崩溃
   （issue M2：T1 实时源注入的条目在构建期未知，可能不带 tags） */
it('D17 条目缺 tags 字段时 diverse 不崩溃', function () {
  ART.length = 0;
  ART.push(
    { t: 'a', sk: 'S1', date: '2026-09-14T12:00:00+08:00', ti: 2 },
    { t: 'b', sk: 'S2', date: '2026-09-14T11:00:00+08:00', ti: 2, tags: ['x'] },
    { t: 'c', sk: 'S1', date: '2026-09-14T10:00:00+08:00', ti: 2, tags: [] }
  );
  sortMode = 'diverse';
  applySort();
  eq(ART.length, 3, 'D17a 缺 tags 时条目数不变');
  ok(ART.every(function (a) { return typeof a.t === 'string'; }), 'D17b 条目结构完好');
  eq(ART.map(function (a) { return a.t; }).sort().join(','), 'a,b,c', 'D17c 无条目丢失或重复');
  sortMode = 'newest';
});

/* ── E 段：同源抑制（issue C1）─────────────────────────────────────────────
   缺陷：weightedShuffle 只在窗口内按权重重排，没有任何「抑制同源相邻」的项，
   首屏会被单一源连续霸占。

   ⚠ 夹具判别性（本轮修复的测试缺陷，务必保留说明）：
   上一版 E 段用「5 源 × 6 篇」**均衡**夹具，实测在**去掉同源抑制的变异体上仍然全绿**
   —— 属变异存活（误绿），等于没有守住修复。均衡夹具下旧实现偶然也能打散，
   maxRun 天然 ≤2。现改用**倾斜夹具**（单一主导源占 23%~29%，其余 5~10 源等量）：
   实测 当前实现 maxRun=1，去抑制变异体=3~4，具备判别性。
   判别性验证方法：去掉 SRC_GAP 过滤后重跑本套件，E1/E1b/E1c/E5/E8 必须变红。

   约束的精确边界（不要读成「最长连续恒 ≤ 2」）：
   · 池非空 ⇒ 同一源在最近 SRC_GAP 位内不会被再次选中 ⇒ 相邻必异源（run=1）。
   · 池为空（组内剩余条目的源都恰好出现在最近 SRC_GAP 位里）⇒ 退化为
     「最久未出现优先」；若此时只剩单一源，连出长度 = 该源剩余条目数，不可避。
   · 实测残留（9092 条真实语料）：最长连出 11（人民网，第 7480 位，82% 深处），
     发生在 g28 组内（该组 140 条 / 57 源 / 人民网占 46 条）。该组构成**可行**
     （46 ≤ floor(142/3)=47），根因是贪心消耗不均衡使组尾塌成单源，不是数据所迫。
     已实测的替代选择规则（池内改为「剩余量优先」）能把全程 maxRun 压到 1，
     但会把前 200 位单源占比从 12% 抬到 25%、覆盖源数从 36 压到 13、
     前 20 位源质量均值从 82.45 降到 78.08 —— 牺牲首屏多样性换深层不可见指标，
     净亏，故不改，如实记录为已知限制。 */

function skRun(arr, gap) {
  /* arr 元素需带 sk；返回 {maxRun, worst} —— 最长同源连续段 */
  var maxRun = 0, cur = 0, prev = null, worst = null;
  for (var i = 0; i < arr.length; i++) {
    var s = arr[i].sk;
    if (s === prev) { cur++; } else { cur = 1; prev = s; }
    if (cur > maxRun) { maxRun = cur; worst = s; }
  }
  return { maxRun: maxRun, worst: worst };
}

function srcCover(arr, n) {
  /* 前 n 位内出现的不同源数量 —— 覆盖度守卫 */
  var set = {};
  for (var i = 0; i < Math.min(n, arr.length); i++) set[arr[i].sk] = 1;
  return Object.keys(set).length;
}

function mkSkew(domCount, nSrc, perSrc) {
  /* 同一时间窗口内的**倾斜**多源条目：一个主导源 + nSrc 个等量异源。
     全部落在 120 分钟窗口内（异源跨度 110 分钟）⇒ 单一分组。
     这种形态是判别性夹具：均衡夹具无法暴露「同源连出」。 */
  var base = Date.UTC(2026, 8, 14, 0, 0, 0);
  var arts = [], i, k;
  for (i = 0; i < domCount; i++) {
    arts.push({ t: 'D' + i, sk: 'DOM', date: new Date(base + Math.floor(i * 60 / Math.max(1, domCount)) * 60000).toISOString() });
  }
  for (k = 0; k < nSrc; k++) {
    for (i = 0; i < perSrc; i++) {
      arts.push({ t: 'O' + k + '_' + i, sk: 'O' + k, date: new Date(base + Math.floor(i * 110 / Math.max(1, perSrc)) * 60000).toISOString() });
    }
  }
  return arts;
}

/* E1: 倾斜夹具 → 同源最长连续 ≤ 2（去抑制变异体实测 4）*/
it('E1 倾斜夹具下同源不得连出', function () {
  var arts = mkSkew(12, 5, 6);               // 12 主导 + 5 源 × 6 = 42 条，主导占 28.6%
  var r = weightedShuffle(arts, 120, {});    // 质量表为空：全部等权，只考位置约束
  var st = skRun(r, 2);
  ok(st.maxRun <= 2, 'E1 同源最长连续 ≤2（实得 ' + st.maxRun + '，源 ' + st.worst + '）');
});

/* E1b/E1c: 换两种倾斜形态，避免「只对一种夹具成立」 */
it('E1b 倾斜夹具（8 异源）同源不得连出', function () {
  var arts = mkSkew(12, 8, 5);               // 12 + 40 = 52 条，主导占 23.1%
  var st = skRun(weightedShuffle(arts, 120, {}), 2);
  ok(st.maxRun <= 2, 'E1b 同源最长连续 ≤2（实得 ' + st.maxRun + '，源 ' + st.worst + '）');
});

it('E1c 倾斜夹具（10 异源）同源不得连出', function () {
  var arts = mkSkew(14, 10, 4);              // 14 + 40 = 54 条，主导占 25.9%
  var st = skRun(weightedShuffle(arts, 120, {}), 2);
  ok(st.maxRun <= 2, 'E1c 同源最长连续 ≤2（实得 ' + st.maxRun + '，源 ' + st.worst + '）');
});

/* E2: 抑制不得丢篇或复制 */
it('E2 同源抑制下条目守恒', function () {
  var arts = mkSkew(12, 5, 6);
  var r = weightedShuffle(arts, 120, {});
  eq(r.length, arts.length, 'E2a 42 条进 42 条出');
  var a = r.map(function (x) { return x.t; }).sort().join(',');
  var b = arts.map(function (x) { return x.t; }).sort().join(',');
  eq(a, b, 'E2b 条目集合完全一致（无丢失/重复）');
});

/* E3: 单源场景（无处可抑制）不得崩、不得丢 —— 退化为原行为 */
it('E3 单源场景退化为原行为', function () {
  var arts = mkSkew(10, 0, 0);
  var r = weightedShuffle(arts, 120, {});
  eq(r.length, 10, 'E3a 单源 10 条不丢');
  eq(r.every(function (x) { return x.sk === 'DOM'; }), true, 'E3b 全为同源');
});

/* E4: 抑制是「延后」而不是「删除」—— 一个源在窗口内仍会被全部输出 */
it('E4 抑制不得删除任何源', function () {
  var arts = mkSkew(12, 5, 6);
  var r = weightedShuffle(arts, 120, {});
  var bySrc = {};
  r.forEach(function (x) { bySrc[x.sk] = (bySrc[x.sk] || 0) + 1; });
  eq(Object.keys(bySrc).sort().join(','), 'DOM,O0,O1,O2,O3,O4', 'E4a 六个源均出现');
  eq(bySrc.DOM, 12, 'E4b 主导源 12 篇一篇不少');
  var others = 0;
  Object.keys(bySrc).forEach(function (k) { if (k !== 'DOM') others += bySrc[k]; });
  eq(others, 30, 'E4c 异源合计仍为 30 篇');
});

/* E5: 集成契约 —— applySort 的 diverse 分支同样受同源抑制约束 */
it('E5 diverse 模式集成受同源抑制约束', function () {
  ART.length = 0;
  var arts = mkSkew(12, 8, 5);
  for (var i = 0; i < arts.length; i++) { arts[i].ti = 2; ART.push(arts[i]); }
  sortMode = 'diverse';
  ANALYSIS_DATA = { quality: { DOM: 90, O0: 70, O1: 60, O2: 50, O3: 40, O4: 30, O5: 20, O6: 10, O7: 5 } };
  applySort();
  var st = skRun(ART, 2);
  ok(st.maxRun <= 2, 'E5a diverse 模式同源最长连续 ≤2（实得 ' + st.maxRun + '）');
  eq(ART.length, arts.length, 'E5b diverse 模式条目守恒');
  sortMode = 'newest';
  ANALYSIS_DATA = null;
});

/* E6: 首屏不被单一源霸占 —— 主导源质量最高（99）时仍不得连出 */
it('E6 最高质量源也不得连出', function () {
  var arts = mkSkew(12, 5, 6);
  var r = weightedShuffle(arts, 120, { DOM: 99, O0: 20, O1: 20, O2: 20, O3: 20, O4: 20 });
  var st = skRun(r.slice(0, 20), 2);
  ok(st.maxRun <= 2, 'E6a 前 20 位同源最长连续 ≤2（实得 ' + st.maxRun + '）');
  eq(r[0].sk, 'DOM', 'E6b 最高质量源仍居首（位置约束不吞噬质量偏好）');
});

/* E7: 覆盖度守卫 —— 防止「压住连出」的代价是牺牲首屏源多样性。
   实测依据：把池内选择改成「剩余量优先」虽能把 maxRun 压到 1，
   但前 200 位单源占比会从 12% 抬到 25%、覆盖源数从 36 压到 13。
   本用例锁住「前 20 位至少 6 个不同源、单源不超过 6 条」。 */
it('E7 首屏源覆盖度不得被压缩', function () {
  var arts = mkSkew(12, 5, 6);
  var r = weightedShuffle(arts, 120, { DOM: 99, O0: 20, O1: 20, O2: 20, O3: 20, O4: 20 });
  var head = r.slice(0, 20);
  var cnt = {};
  head.forEach(function (x) { cnt[x.sk] = (cnt[x.sk] || 0) + 1; });
  var top = 0;
  Object.keys(cnt).forEach(function (k) { if (cnt[k] > top) top = cnt[k]; });
  eq(srcCover(r, 20), 6, 'E7a 前 20 位覆盖全部 6 个源（实得 ' + srcCover(r, 20) + '）');
  ok(top <= 6, 'E7b 前 20 位单源最多 ' + top + ' 条（≤6）');
});

/* E8: 不可行域回归锁 —— 主导源占比过大（40%）时允许连出，但不得回到旧实现在该形态
   下的 12 连。本用例是「不回退」守卫，不是「≤2」保证，见段首边界说明。 */
it('E8 不可行域不得回退到旧实现的连出长度', function () {
  var arts = mkSkew(20, 5, 6);               // 20 主导 + 30 异源 = 50 条，主导占 40%
  var r = weightedShuffle(arts, 120, {});
  var st = skRun(r, 2);
  eq(r.length, 50, 'E8a 条目守恒');
  ok(st.maxRun <= 8, 'E8b 连出长度 ≤8（当前实现 7，旧等价实现 12，实得 ' + st.maxRun + '）');
});

/* E9: 抑制间隔本身必须被守住 —— 同一源两次出现的最小距离 ≥3。
   E1 只断言「连续段 ≤2」，把 SRC_GAP 从 2 改成 1（只挡相邻）仍能通过，
   等于常量本身无人守卫。本用例锁住 SRC_GAP=2 的语义（间隔 2 位 ⇒ 距离 ≥3）。

   为何必须用**均衡**夹具：在倾斜夹具上实测最小距离 = 2 —— 这不是过滤失效，
   而是池为空时的退化分支（3b）**有意**放宽到「最久未出现优先」的后果。
   均衡夹具（每源占 1/6 ≈ 16.7% < 1/3）下池永不枯竭，间隔语义才能被干净地测出来。 */
function mkBalanced(nSrc, perSrc) {
  var base = Date.UTC(2026, 8, 14, 0, 0, 0);
  var arts = [], s, i;
  for (s = 0; s < nSrc; s++) {
    for (i = 0; i < perSrc; i++) {
      arts.push({ t: 'S' + s + '_' + i, sk: 'S' + s, date: new Date(base + Math.floor(i * 110 / perSrc) * 60000).toISOString() });
    }
  }
  return arts;
}

it('E9 均衡夹具下同源最小距离 ≥3', function () {
  var arts = mkBalanced(6, 6);
  var r = weightedShuffle(arts, 120, {});
  var last = {}, minD = Infinity;
  for (var i = 0; i < r.length; i++) {
    var s = r[i].sk;
    if (last[s] !== undefined) { var d = i - last[s]; if (d < minD) minD = d; }
    last[s] = i;
  }
  ok(minD >= 3, 'E9 同源最小间隔 ≥3（实得 ' + minD + '）');
});

/* E9b: 倾斜夹具上退化分支的放宽幅度必须有界 —— 记录「距离掉到 2」的占比。
   这是对上面那条注释的可执行背书：不是断言「绝不出现」，而是断言「不成规模」。 */
it('E9b 退化分支的间隔放宽不成规模', function () {
  var arts = mkSkew(12, 5, 6);
  var r = weightedShuffle(arts, 120, {});
  var last = {}, tight = 0;
  for (var i = 0; i < r.length; i++) {
    var s = r[i].sk;
    if (last[s] !== undefined && (i - last[s]) < 3) tight++;
    last[s] = i;
  }
  ok(tight <= Math.ceil(r.length * 0.2),
     'E9b 间隔被放宽的条目数 ≤20%（实际 ' + tight + '/' + r.length + '）');
});

/* E10: 双源夹具必须严格交替（连出 ≤1）。
   为什么单列：过滤失效时池恒非空，但**退化分支内部的选源规则**（3b 的「取最大 gap」
   还是「取最小 gap」）在其余 24 个夹具上实测**行为完全一致** ——
   因为池枯竭时通常只剩单一源，max 与 min 相等，那条分支近似死代码（变异 C1-c 存活）。
   只有「双源 + 篇数相等」能把池枯竭时的选源规则暴露出来：
     当前实现 S1S0S1S0…（连出 1）；反向选择 S1S0S0S1S1…（连出 2）。
   注意必须**等量**：篇数不等时少的一方耗尽后必然连出，那是数据所迫，不算缺陷。 */
it('E10 双源等量夹具严格交替', function () {
  var arts = mkBalanced(2, 10);
  var r = weightedShuffle(arts, 120, {});
  var st = skRun(r, 2);
  eq(r.length, 20, 'E10a 条目守恒（20 条）');
  ok(st.maxRun <= 1, 'E10b 双源严格交替，同源最长连续 ≤1（实得 ' + st.maxRun + '，源 ' + st.worst + '）');
});

/* ── D18/D24: _applyRunCap 集成测试（active / newest 模式）────────────── */

/* D18: active 模式 _applyRunCap 生效 */
it('D18 active 模式同源连续 ≤3', function () {
  ART.length = 0;
  // 8 条 SA + 6 条 SB + 6 条 SC = 20 条，异源足够打破连出
  for (var i = 0; i < 8; i++)
    ART.push({ t: 'A' + i, sk: 'SA', date: '2026-09-14T10:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 6; i++)
    ART.push({ t: 'B' + i, sk: 'SB', date: '2026-09-14T09:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 6; i++)
    ART.push({ t: 'C' + i, sk: 'SC', date: '2026-09-14T08:' + (i % 60) + ':00+08:00', ti: 2 });
  sortMode = 'active';
  applySort();
  var st = skRun(ART);
  ok(st.maxRun <= 3, 'D18 active 模式 maxRun ≤3（实得 ' + st.maxRun + '）');
  eq(ART.length, 20, 'D18b 条目守恒');
  sortMode = 'newest';
});

/* D24: newest 模式 _applyRunCap 生效 */
it('D24 newest 模式同源连续 ≤3', function () {
  ART.length = 0;
  // 10 条 SX + 5 条 SY + 5 条 SZ = 20 条，3 源场景
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'X' + i, sk: 'SX', date: '2026-09-14T12:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 5; i++)
    ART.push({ t: 'Y' + i, sk: 'SY', date: '2026-09-14T11:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 5; i++)
    ART.push({ t: 'Z' + i, sk: 'SZ', date: '2026-09-14T10:' + (i % 60) + ':00+08:00', ti: 2 });
  sortMode = 'newest';
  applySort();
  var st = skRun(ART);
  ok(st.maxRun <= 3, 'D24 newest 模式 maxRun ≤3（实得 ' + st.maxRun + '）');
  eq(ART.length, 20, 'D24b 条目守恒');
});

/* D24-2src: 2 源极端场景 _applyRunCap 尽力而为（不要求 ≤3，但验证有改善） */
it('D24-2src 2 源 50/50 _applyRunCap 尽力改善', function () {
  ART.length = 0;
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'X' + i, sk: 'SX', date: '2026-09-14T12:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'Y' + i, sk: 'SY', date: '2026-09-14T11:' + (i % 60) + ':00+08:00', ti: 2 });
  sortMode = 'newest';
  applySort();
  var st = skRun(ART);
  ok(st.maxRun < 10, 'D24-2src 2 源 maxRun 从 10 降低（实得 ' + st.maxRun + '）');
  eq(ART.length, 20, 'D24-2srcb 条目守恒');
  sortMode = 'newest';
});

/* ── D20/D21/D22: _srcWeight tier 乘子测试 ────────────────────────────── */

/* D20: _srcWeight tier 乘子 */
it('D20 _srcWeight tier 乘子', function () {
  ok(typeof _srcWeight === 'function', 'D20a _srcWeight 存在');
  var w_t1 = _srcWeight({sk:'S1', ti:1}, {S1: 80});
  var w_t3 = _srcWeight({sk:'S2', ti:3}, {S2: 80});
  ok(w_t1 > w_t3, 'D20b T1 权重(' + w_t1 + ') > T3 权重(' + w_t3 + ')（同 quality=80）');
  eq(w_t1, 120, 'D20c T1×80 = 80×1.5 = 120');
  eq(w_t3, 80, 'D20d T3×80 = 80×1.0 = 80');
  var w_t2 = _srcWeight({sk:'S3', ti:2}, {S3: 60});
  eq(w_t2, 72, 'D20e T2×60 = 60×1.2 = 72');
});

/* D21: _srcWeight quality=0 回落默认 50 */
it('D21 _srcWeight quality=0 用默认 50', function () {
  var w = _srcWeight({sk:'ZERO', ti:3}, {ZERO: 0});
  eq(w, 50, 'D21a quality=0 T3 → 默认 50×1.0 = 50');
  var w_t1 = _srcWeight({sk:'ZERO', ti:1}, {ZERO: 0});
  eq(w_t1, 75, 'D21b quality=0 T1 → 默认 50×1.5 = 75');
});

/* D22: _srcWeight quality 缺失用默认 50 */
it('D22 _srcWeight quality 缺失用默认 50', function () {
  var w = _srcWeight({sk:'MISSING', ti:3}, {});
  eq(w, 50, 'D22 quality 缺失 → 默认 50');
});

/* D25: quality 模式 _applyRunCap 生效 */
it('D25 quality 模式同源连续 ≤5', function () {
  ART.length = 0;
  for (var i = 0; i < 12; i++)
    ART.push({ t: 'Q' + i, sk: 'SQ', date: '2026-09-14T10:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 4; i++)
    ART.push({ t: 'R' + i, sk: 'SR', date: '2026-09-14T09:' + (i % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 4; i++)
    ART.push({ t: 'T' + i, sk: 'ST', date: '2026-09-14T08:' + (i % 60) + ':00+08:00', ti: 2 });
  sortMode = 'quality';
  ANALYSIS_DATA = { quality: { SQ: 90, SR: 90, ST: 90 } };
  applySort();
  var st = skRun(ART);
  ok(st.maxRun <= 5, 'D25 quality 模式 maxRun ≤5（实得 ' + st.maxRun + '）');
  eq(ART.length, 20, 'D25b 条目守恒');
  sortMode = 'newest';
  ANALYSIS_DATA = null;
});

/* ── F 段：族群配额（source family cap）────────────────────────────────── */

/* F1: SOURCE_FAMILIES 存在且为对象 */
it('F1 SOURCE_FAMILIES 定义', function () {
  ok(typeof SOURCE_FAMILIES === 'object', 'F1 SOURCE_FAMILIES 是对象');
  eq(SOURCE_FAMILIES['v2ex_all_50'], 'v2ex', 'F1b v2ex_all_50 → v2ex');
  eq(SOURCE_FAMILIES['nodeseek_54'], 'nodeseek', 'F1c nodeseek_54 → nodeseek');
});

/* F2: _applyRunCap 族级 cap — V2EX 4 子源合计 ≤ familyCap */
it('F2 V2EX 族群合计 cap', function () {
  ART.length = 0;
  // 构造 V2EX 4 子源各 5 条 + 异源 5 条 = 25 条
  var v2exSrcs = ['v2ex_all_50', 'v2ex技术_44'];
  for (var s = 0; s < v2exSrcs.length; s++)
    for (var i = 0; i < 5; i++)
      ART.push({ t: 'V' + s + '_' + i, sk: v2exSrcs[s], date: '2026-09-14T12:0' + (s * 5 + i % 5) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 5; i++)
    ART.push({ t: 'O' + i, sk: 'OTHER_' + i, date: '2026-09-14T11:0' + i + ':00+08:00', ti: 2 });
  _applyRunCap(ART, 3, SOURCE_FAMILIES);
  // 检查：V2EX 族群连续条目不超过 familyCap（默认 6）
  var famRun = 0, famMax = 0;
  for (var i = 0; i < ART.length; i++) {
    var gk = SOURCE_FAMILIES[ART[i].sk] || ART[i].sk;
    if (gk === 'v2ex') { famRun++; if (famRun > famMax) famMax = famRun; }
    else famRun = 0;
  }
  ok(famMax <= 6, 'F2 V2EX 族群最长连续 ≤6（实得 ' + famMax + '）');
  eq(ART.length, 15, 'F2b 条目守恒');
});

/* F3: 无 family 定义的源退化为单源 cap（向后兼容） */
it('F3 无 family 退化为单源 cap', function () {
  ART.length = 0;
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'A' + i, sk: 'SA', date: '2026-09-14T12:0' + (i % 10) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 5; i++)
    ART.push({ t: 'B' + i, sk: 'SB', date: '2026-09-14T11:0' + (i % 10) + ':00+08:00', ti: 2 });
  _applyRunCap(ART, 3, SOURCE_FAMILIES);
  var st = skRun(ART);
  ok(st.maxRun <= 3, 'F3 非 family 源 maxRun ≤3（实得 ' + st.maxRun + '）');
  eq(ART.length, 15, 'F3b 条目守恒');
});

/* F4: _applyRunCap 第三参数缺省时向后兼容 */
it('F4 _applyRunCap 缺省 families 参数', function () {
  ART.length = 0;
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'X' + i, sk: 'SX', date: '2026-09-14T12:0' + (i % 10) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 3; i++)
    ART.push({ t: 'Y' + i, sk: 'SY', date: '2026-09-14T11:0' + i + ':00+08:00', ti: 2 });
  _applyRunCap(ART, 3);  // 不传 families
  var st = skRun(ART);
  ok(st.maxRun <= 3, 'F4 缺省 families 仍正常 cap（实得 ' + st.maxRun + '）');
  eq(ART.length, 13, 'F4b 条目守恒');
});

/* F5: NodeSeek 单源族 cap ≤ 4 */
it('F5 NodeSeek 族群 cap', function () {
  ART.length = 0;
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'N' + i, sk: 'nodeseek_54', date: '2026-09-14T12:0' + (i % 10) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 5; i++)
    ART.push({ t: 'M' + i, sk: 'OTHER_' + i, date: '2026-09-14T11:0' + i + ':00+08:00', ti: 2 });
  _applyRunCap(ART, 3, SOURCE_FAMILIES);
  // NodeSeek 是单源族，familyCap 应 ≤ 4
  var nsRun = 0, nsMax = 0;
  for (var i = 0; i < ART.length; i++) {
    if (ART[i].sk === 'nodeseek_54') { nsRun++; if (nsRun > nsMax) nsMax = nsRun; }
    else nsRun = 0;
  }
  ok(nsMax <= 4, 'F5 NodeSeek 连续 ≤4（实得 ' + nsMax + '）');
  eq(ART.length, 15, 'F5b 条目守恒');
});

/* F6: 条目守恒 + 集合守恒（族群 cap 不得丢篇或复制） */
it('F6 族群 cap 条目守恒', function () {
  ART.length = 0;
  var v2exSrcs = ['v2ex_all_50', 'v2ex技术_44'];
  for (var s = 0; s < v2exSrcs.length; s++)
    for (var i = 0; i < 8; i++)
      ART.push({ t: 'V' + s + '_' + i, sk: v2exSrcs[s], date: '2026-09-14T12:0' + ((s * 8 + i) % 60) + ':00+08:00', ti: 2 });
  for (var i = 0; i < 10; i++)
    ART.push({ t: 'O' + i, sk: 'OTHER_' + (i % 5), date: '2026-09-14T11:0' + i + ':00+08:00', ti: 2 });
  var before = ART.map(function(a){ return a.t; }).sort().join(',');
  _applyRunCap(ART, 3, SOURCE_FAMILIES);
  var after = ART.map(function(a){ return a.t; }).sort().join(',');
  eq(ART.length, 26, 'F6a 26 条进 26 条出');
  eq(before, after, 'F6b 条目集合完全一致');
});

console.log('\nRESULT: ' + PASS + ' passed, ' + FAIL + ' failed');
if (FAIL) { console.log('FAILED: ' + FAILED_NAMES.join(' | ')); process.exit(1); }
process.exit(0);
