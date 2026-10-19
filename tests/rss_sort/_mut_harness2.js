
/* ===== shim（只提供被测代码依赖的宿主环境，不实现业务逻辑） ===== */
var ART = [];
var wallLimit = 120, WALL_STEP = 80, curArt = null;
var ANALYSIS_DATA = null;
var renderCalls = { chips: 0, wall: 0, panel: 0 };
function renderChips(){ renderCalls.chips++; }
function renderWall(){ renderCalls.wall++; }
function renderPanel(){ renderCalls.panel++; }
var _store = {};
var localStorage = {
  getItem: function(k){ return Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null; },
  setItem: function(k, v){ _store[k] = String(v); },
  removeItem: function(k){ delete _store[k]; }
};
var document = { getElementById: function(){ return null; }, addEventListener: function(){} };
var alert = function(){};
var _fmtRel = function(d){ return d ? String(d) : ''; };
function artKey(a){ return (a.sk || '') + '|' + (a.u && a.u !== '#' ? a.u : (a.t || '')); }

/* ===== 抽取块: sort ===== */
/* ── Sort ── */
  var sortMode = localStorage.getItem('rss_sort_mode') || 'newest';
  /* 时区安全日期比较【仅用于升序】（D3 修复：+08:00/Z 混合格式的字符串比较是时区盲的）；
     无日期沉底，两个无日期视为相等。
     注意：本函数的 NaN 分支是为升序写的，降序调用会反转语义（无日期置顶）。
     任何降序排序一律使用 _dateCmpDesc，不要反向调用本函数。 */
  function _dateCmp(x,y){
    var tx=x?new Date(x).getTime():NaN, ty=y?new Date(y).getTime():NaN;
    if(isNaN(tx)&&isNaN(ty)) return 0;
    if(isNaN(tx)) return 1;
    if(isNaN(ty)) return -1;
    return tx-ty;
  }
  /* 时区安全日期比较【仅用于降序】：无日期一律沉底，语义与调用方向无关。
     修复前 newest/active 反向调用 _dateCmp，导致 201 条无日期条目被顶到首屏。 */
  function _dateCmpDesc(x,y){
    var tx=x?new Date(x).getTime():NaN, ty=y?new Date(y).getTime():NaN;
    if(isNaN(tx)&&isNaN(ty)) return 0;
    if(isNaN(tx)) return 1;      /* x 无日期 → x 靠后 */
    if(isNaN(ty)) return -1;
    if(tx===ty) return 0;
    return tx<ty?1:-1;           /* 新的在前 */
  }
  /* F1 修复：'active' 按信源最近更新时间排序 */
  function applySort(){
    if(sortMode==='oldest') ART.sort(function(a,b){ return _dateCmp(a.date,b.date); });
    else if(sortMode==='active'){
      var srcLatest={};
      ART.forEach(function(a){ if(a.date){ var cur=srcLatest[a.sk]; if(!cur||_dateCmp(a.date,cur)>0) srcLatest[a.sk]=a.date; }});
      ART.sort(function(a,b){
        var sa=srcLatest[a.sk]||'', sb=srcLatest[b.sk]||'';
        /* _dateCmpDesc 自身即为降序语义，参数不可再反转（反转会使无日期源置顶） */
        if(sa!==sb) return _dateCmpDesc(sa,sb);
        return _dateCmpDesc(a.date,b.date);
      });
    }
    else ART.sort(function(a,b){ return _dateCmp(b.date,a.date); });
    if(sortMode==='quality' && ANALYSIS_DATA && ANALYSIS_DATA.quality){
      var qm=ANALYSIS_DATA.quality;
      ART.sort(function(a,b){ return (qm[b.sk]||0)-(qm[a.sk]||0); });
    }
  }
  var _sortEl = document.getElementById('sortSelect');
  if(_sortEl){ _sortEl.value=sortMode; _sortEl.addEventListener('change',function(){ sortMode=this.value; localStorage.setItem('rss_sort_mode',sortMode); applySort(); wallLimit=WALL_STEP; curArt=null; renderWall(); }); }
  /* A6 修复：中文阅读速度约 400 字/分钟 */
  function estRead(a){ var mins=Math.max(1,Math.round((a.s||'').length/400)); return mins+' min'; }

  /* ── 分层交织：每 4 篇高频文章穿插 1 篇低频文章 ─ */
  function tierInterleave(){
    var hi=[], lo=[];
    ART.forEach(function(a){ (a.ti<=2 ? hi : lo).push(a); });
    var result=[], i=0, j=0;
    while(i<hi.length || j<lo.length){
      var he=Math.min(4, hi.length-i);
      for(var k=0;k<he;k++) result.push(hi[i++]);
      if(j<lo.length) result.push(lo[j++]);
    }
    ART=result;
  }

  
/* ===== 抽取块: merge ===== */
function _mergeRemoteSources(j){
    try{
      if(!j||!j.sources||!j.sources.length) return 0;
      var known={}; for(var i=0;i<ART.length;i++) known[artKey(ART[i])]=1;
      var added=[];
      j.sources.forEach(function(s){
        if(!s||!s.items||!s.items.length) return;
        s.items.forEach(function(it){
          if(!it||!it.u||it.u==='#') return;
          var a={t:it.t||'', s:it.s||'', src:s.name, sk:s.key, c:s.cat, sc:s.color, ti:s.tier||3,
                 time:_fmtRel(it.d), date:it.d||'', u:it.u, fc:it.fc||'', img:it.img||'', mu:it.mu||'', mt:it.mt||'', bad_date:!!it.bad_date};
          if(!a.t) return;
          var k=artKey(a);
          if(known[k]) return;
          known[k]=1; added.push(a);
        });
      });
      if(!added.length){
        /* 无新增内容时也要纠错：若可见首屏混入无日期条目（NaN 置顶缺陷的指纹），
           重新规范化排序。健康状态下该分支不触发，因此不会造成可见跳变。
           例外：quality 模式按源质量重排，天然会把高质量源的无日期条目顶回首屏，
           谓词会恒真 → 每次刷新都重渲染，故排除该模式。 */
        var _head=Math.min(ART.length, wallLimit||0), _bad=0;
        for(var h=0;h<_head;h++){ var _d=ART[h].date; if(!_d||isNaN(new Date(_d).getTime())) _bad++; }
        if(sortMode!=='quality' && _head>0 && _bad*2>_head){ applySort(); renderChips(); renderWall(); renderPanel(); }
        return 0;
      }
      for(var i=added.length-1;i>=0;i--) ART.unshift(added[i]);
      /* 不再对 added 单独预排序：紧接着的 applySort() 会对整个 ART 重排，
         预排序对最终顺序无影响（原为 localeCompare，已随口径统一移除） */
      applySort();
      tierInterleave();
      wallLimit=Math.min(ART.length, Math.max(wallLimit, WALL_STEP));
      return added.length;
    }catch(e){ return 0; }
  }
  
/* ===== 用例 ===== */
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

console.log('\nRESULT: ' + PASS + ' passed, ' + FAIL + ' failed');
if (FAIL) { console.log('FAILED: ' + FAILED_NAMES.join(' | ')); process.exit(1); }
process.exit(0);
