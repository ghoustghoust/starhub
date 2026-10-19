# tests/rss_composite/test_refresh_tags_js.py
# -*- coding: utf-8 -*-
"""tags 穿透两条通道的 JS 侧测试运行器（issue M5）

做法：与 test_diverse_js.py 同构 —— 从 build_rss_aggregator.py 抽取真实代码块
（data 块提供 buildArt，sort 块提供 applySort/tierInterleave，
 另外单抽 _mergeRemoteSources 函数体），拼接 shim + 用例交给 node 执行。

为什么必须抽真实源码而不是在 harness 里另造一份：
  本用例断言的是「字段白名单里有没有 tags」这一具体实现细节，
  自造实现会让断言与被测对象脱钩（正是 M5 长期未被发现的原因：
  四个复合排序测试文件里 _mergeRemoteSources 引用数为 0）。
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
HARNESS = os.path.join(tempfile.gettempdir(), "_rss_refresh_tags_harness.js")

# 缺失即「功能未实现」→ RED
REQUIRED_ANCHORS = [
    "function buildArt(){",
    "function _mergeRemoteSources(j){",
    "function weightedShuffle(",
]

BLOCKS = [
    ("data", "/* ── Data ── */", "/* ── Sort ── */"),
    ("sort", "/* ── Sort ── */", "/* ── State ── */"),
]

# _mergeRemoteSources 位于 Refresh 大块内，整块依赖过重，单抽该函数体
FUNC_BLOCKS = [
    ("_mergeRemoteSources", "  function _mergeRemoteSources(j){", "  function _applyRemote("),
]

PRELUDE = r"""
/* ===== shim（只提供被测代码依赖的宿主环境，不实现业务逻辑） ===== */
var ART = [];
var SOURCES = [];
var window = {};
var wallLimit = 120, WALL_STEP = 80, curArt = null;
var ANALYSIS_DATA = null;
var CATEGORY_ORDER = [];
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
"""


def extract(src, start, end):
    i = src.find(start)
    if i < 0:
        raise SystemExit("[抽取失败] 未找到起始锚点: %r" % start)
    j = src.find(end, i + len(start))
    if j < 0:
        raise SystemExit("[抽取失败] 未找到结束锚点: %r" % end)
    return src[i:j]


# 数据块内含 Python f-string 插值（如 `var CAT_ORDER = """ + json.dumps(...) + """;`），
# 直接抽取会产出非法 JS。这些变量与 buildArt 的断言无关，统一置为 null。
_PY_INTERP = re.compile(r'"""\s*\+[^"]+?\+\s*"""')


def sanitize(js):
    return _PY_INTERP.sub("null", js)


def main():
    with open(BUILD, "r", encoding="utf-8") as f:
        src = f.read()

    missing = [a for a in REQUIRED_ANCHORS if a not in src]
    if missing:
        raise SystemExit("[抽取失败] 源码缺少锚点: %s" % missing)

    chunks = [PRELUDE]
    for name, s, e in BLOCKS:
        chunks.append("\n/* ===== 抽取块: %s ===== */\n" % name + sanitize(extract(src, s, e)))
    for name, s, e in FUNC_BLOCKS:
        chunks.append("\n/* ===== 抽取函数: %s ===== */\n" % name + extract(src, s, e))

    cases_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_refresh_tags.js")
    with open(cases_path, "r", encoding="utf-8") as f:
        chunks.append("\n/* ===== 用例 ===== */\n" + f.read())

    with open(HARNESS, "w", encoding="utf-8") as f:
        f.write("".join(chunks))

    node = r"C:\Users\40832\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
    if not os.path.exists(node):
        node = "node"
    p = subprocess.run([node, HARNESS], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    sys.stdout.write(p.stdout or "")
    if p.stderr:
        sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
