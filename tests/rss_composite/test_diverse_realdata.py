# tests/rss_composite/test_diverse_realdata.py
# -*- coding: utf-8 -*-
"""diverse 打散的真实语料回归守卫（issue C1）

为什么需要这个文件：
  cases_diverse.js 的 E 段用合成夹具，能锁住「算法形态」；
  但它证明不了「真实语料上首屏确实不再被单一源霸占」。
  本文件直接拿仓库里已入库的真实分块 rss-data-0.js（首屏 chunk，41 源 / 360 条）
  跑真实的 buildArt()，断言首屏不变量。

  对照标尺：同一份语料的**纯时间降序**（newest）基线。
  这样断言是「相对」的——数据漂移不会让用例莫名其妙变红，
  但「diverse 必须比时间降序更散」这条不变式永远成立。

判别性（TDD 要求）：
  去掉 weightedShuffle 的同源抑制过滤后，本文件 R2/R3/R6 必须变红。
  实测判别数据（当前快照）：
    指标                     当前实现   去抑制   纯时间降序
    前200位同源最长连续          1         3        9
    前50位同源最长连续           1         3        -
"""
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
CHUNK0 = os.path.join(ROOT, "rss-data-0.js")
SNAP = os.path.join(ROOT, "analysis_snapshot.json")
HARNESS = os.path.join(tempfile.gettempdir(), "_rss_diverse_realdata_harness.js")

FAIL = 0
PASS = 0
failures = []


def eq(actual, expected, label):
    global FAIL, PASS
    if actual == expected:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        failures.append(label)
        print("  [FAIL] %s\n         期望 %r\n         实得 %r" % (label, expected, actual))


def ok(cond, label):
    eq(bool(cond), True, label)


PRELUDE = r"""
global.window = global;
global.window.__CHUNKS = global.window.__CHUNKS || [];
var ART = [], SOURCES = [];
var wallLimit = 100000000, WALL_STEP = 80, curArt = null;
var ANALYSIS_DATA = null;
var _store = {};
var localStorage = {
  getItem: function(k){ return Object.prototype.hasOwnProperty.call(_store, k) ? _store[k] : null; },
  setItem: function(k, v){ _store[k] = String(v); },
  removeItem: function(k){ delete _store[k]; }
};
var document = { getElementById: function(){ return null; }, addEventListener: function(){} };
function renderChips(){} function renderWall(){} function renderPanel(){}
function _fmtRel(d){ return d ? String(d) : ''; }
function artKey(a){ return (a.sk || '') + '|' + (a.u && a.u !== '#' ? a.u : (a.t || '')); }
_store['rss_sort_mode'] = 'diverse';
"""

PROBE = r"""
require(%(c0)s);
(function(){
  var c0 = ((window.__CHUNKS[0] || {}).sources) || [];
  SOURCES = c0.map(function(s){
    return { key: s.key, name: s.name, cat: s.cat, color: s.color, tier: s.tier, bb: s.bb, items: (s.items || []).slice() };
  });
  ANALYSIS_DATA = { quality: QUALITY_MAP };
})();

function skRun(arr, n){
  var t = n ? arr.slice(0, n) : arr, m = 0, c = 0, p = null, w = null;
  for (var i = 0; i < t.length; i++){ var s = t[i].sk; if (s === p) c++; else { c = 1; p = s; } if (c > m) { m = c; w = s; } }
  return { maxRun: m, worst: w };
}
function srcTop(arr, n){
  var c = {}, tot = 0;
  for (var i = 0; i < Math.min(n, arr.length); i++){ var s = arr[i].sk; c[s] = (c[s] || 0) + 1; tot++; }
  var mx = 0, kx = null;
  Object.keys(c).forEach(function(k){ if (c[k] > mx) { mx = c[k]; kx = k; } });
  return { max: mx, key: kx, n: tot };
}

buildArt();
var DIVERSE = ART.slice();
var NEWEST = ART.slice().sort(function(a, b){ return _dateCmpDesc(a.date, b.date); });

function keyOf(a){ return (a.sk || '') + '|' + (a.u || a.t || ''); }
function keySet(arr){ var m = {}; arr.forEach(function(a){ m[keyOf(a)] = (m[keyOf(a)] || 0) + 1; }); return m; }
var kd = keySet(DIVERSE), kn = keySet(NEWEST);
var diff = 0, dup = 0;
Object.keys(kn).forEach(function(k){ if (kd[k] !== kn[k]) diff++; });
Object.keys(kd).forEach(function(k){ if (kd[k] > 1) dup++; });

var R = {
  n: DIVERSE.length,
  nNewest: NEWEST.length,
  runFull: skRun(DIVERSE).maxRun,
  run200: skRun(DIVERSE, 200),
  run50: skRun(DIVERSE, 50),
  top20: srcTop(DIVERSE, 20),
  top200: srcTop(DIVERSE, 200),
  baseRun200: skRun(NEWEST, 200),
  baseRunFull: skRun(NEWEST).maxRun,
  baseTop20: srcTop(NEWEST, 20),
  setDiff: diff,
  dupKeys: dup,
  distinct200: (function(){ var s = {}; DIVERSE.slice(0, 200).forEach(function(a){ s[a.sk] = 1; }); return Object.keys(s).length; })()
};
console.log('@@JSON@@' + JSON.stringify(R));
"""


def extract(src, s, e):
    i = src.find(s)
    if i < 0:
        raise SystemExit("[抽取失败] 起始锚点: %r" % s)
    j = src.find(e, i + len(s))
    if j < 0:
        raise SystemExit("[抽取失败] 结束锚点: %r" % e)
    return src[i:j]


def main():
    print("=" * 74)
    print("diverse 真实语料首屏回归（chunk0: 首屏分块）")
    print("=" * 74)

    # 数据与源码存在性：fail-loud（数据文件已入 git，缺失说明环境不对，不能静默跳过）
    for p in (BUILD, CHUNK0, SNAP):
        ok(os.path.exists(p), "数据/源码存在: %s" % os.path.basename(p))
    if FAIL:
        print("\nRESULT: %d passed, %d failed" % (PASS, FAIL))
        sys.exit(1)

    src = io.open(BUILD, encoding="utf-8").read()
    for anchor in ("function buildArt(){", "function weightedShuffle(", "function _dateCmpDesc("):
        ok(anchor in src, "源码含锚点 %r" % anchor)
    if FAIL:
        print("\nRESULT: %d passed, %d failed" % (PASS, FAIL))
        sys.exit(1)

    snap = json.load(io.open(SNAP, encoding="utf-8"))
    qjson = json.dumps(snap.get("quality") or {}, ensure_ascii=False)

    parts = [PRELUDE, "\nvar QUALITY_MAP = " + qjson + ";\n"]
    for name, a, b in [("tagsOf", "  function _tagsOf(x){", "  function buildArt(){"),
                       ("buildArt", "  function buildArt(){", "  /* ── Sort ── */"),
                       ("sort", "/* ── Sort ── */", "/* ── State ── */")]:
        parts.append("\n/* ===== 抽取块: %s ===== */\n" % name + extract(src, a, b))
    parts.append(PROBE % {"c0": repr(CHUNK0).replace("\\", "/")})

    io.open(HARNESS, "w", encoding="utf-8").write("".join(parts))
    node = r"C:\Users\40832\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
    if not os.path.exists(node):
        node = "node"
    p = subprocess.run([node, "--max-old-space-size=4096", HARNESS],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "")
    if p.stderr:
        print(p.stderr.strip())

    line = ""
    for l in out.splitlines():
        if l.startswith("@@JSON@@"):
            line = l[len("@@JSON@@"):]
    if not line:
        ok(False, "node 产出结果 JSON（rc=%d, stdout=%r）" % (p.returncode, out[-400:]))
        print("\nRESULT: %d passed, %d failed" % (PASS, FAIL))
        sys.exit(1)
    R = json.loads(line)

    # ── A. 夹具自证：先证明真实语料本身存在「单一源连出」现象 ──────────────
    print()
    print("A. 夹具自证（防止空断言：若基线本来就不连出，后面的改善就无意义）")
    ok(R["n"] > 200, "A1 首屏分块条目数 %d > 200（足够做前 200 位断言）" % R["n"])
    ok(R["baseRun200"]["maxRun"] >= 5,
       "A2 纯时间降序基线前 200 位确实连出（实得 %d，源 %s）" % (R["baseRun200"]["maxRun"], R["baseRun200"]["worst"]))
    ok(R["baseTop20"]["max"] >= 8,
       "A3 纯时间降序基线前 20 位单源确实霸占（实得 %d/%d，源 %s）"
       % (R["baseTop20"]["max"], R["baseTop20"]["n"], R["baseTop20"]["key"]))

    # ── B. 守恒 ──────────────────────────────────────────────────────────
    print()
    print("B. 打散守恒")
    eq(R["setDiff"], 0, "B1 diverse 与时间降序的条目集合完全一致（差异键 %d）" % R["setDiff"])
    eq(R["dupKeys"], 0, "B2 diverse 未复制任何条目（重复键 %d）" % R["dupKeys"])
    eq(R["n"], R["nNewest"], "B3 条目数守恒（%d vs %d）" % (R["n"], R["nNewest"]))

    # ── C. 首屏不变量 ────────────────────────────────────────────────────
    print()
    print("C. 首屏不变量（当前快照：diverse 1 / 1，去抑制变异体 3 / 3，时间降序 9）")
    ok(R["run200"]["maxRun"] <= 2,
       "C1 前 200 位同源最长连续 ≤2（实得 %d，源 %s）" % (R["run200"]["maxRun"], R["run200"]["worst"]))
    ok(R["run50"]["maxRun"] <= 2,
       "C2 前 50 位同源最长连续 ≤2（实得 %d，源 %s）" % (R["run50"]["maxRun"], R["run50"]["worst"]))
    ok(R["top200"]["max"] <= 30,
       "C3 前 200 位单源最多 ≤30（15%%）（实得 %d，源 %s）" % (R["top200"]["max"], R["top200"]["key"]))
    ok(R["top20"]["max"] <= 6,
       "C4 前 20 位单源最多 ≤6（实得 %d，源 %s）" % (R["top20"]["max"], R["top20"]["key"]))

    # ── D. 相对改善（对数据漂移稳健的核心不变式）─────────────────────────
    print()
    print("D. 相对不变式：diverse 必须在首屏聚类上严格优于时间降序")
    ok(R["run200"]["maxRun"] < R["baseRun200"]["maxRun"],
       "D1 前 200 位最长连续：diverse %d < 时间降序 %d"
       % (R["run200"]["maxRun"], R["baseRun200"]["maxRun"]))
    ok(R["runFull"] < R["baseRunFull"],
       "D2 全长最长连续：diverse %d < 时间降序 %d" % (R["runFull"], R["baseRunFull"]))
    ok(R["top20"]["max"] < R["baseTop20"]["max"],
       "D3 前 20 位单源最多：diverse %d < 时间降序 %d" % (R["top20"]["max"], R["baseTop20"]["max"]))
    ok(R["distinct200"] >= 15,
       "D4 前 200 位覆盖源数 ≥15（实得 %d）—— 防止「压住连出」的代价是牺牲多样性" % R["distinct200"])

    print()
    print("=" * 74)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    if failures:
        for f in failures:
            print("  - %s" % f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
