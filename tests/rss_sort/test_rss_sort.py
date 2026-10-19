# -*- coding: utf-8 -*-
"""RSS 排序修复 · 测试运行器

做法：从 build_rss_aggregator.py 中**原地抽取**内嵌 JS 的两个代码块，
拼接 shim 前置 + 用例，交给 node 执行。不依赖构建产物，不依赖网络。

用法：
    python tests/rss_sort/test_rss_sort.py
退出码：0 = 全绿；1 = 有失败；2 = 抽取失败（说明构建脚本结构变了）
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# RSS_BUILD_SRC 用于变异测试：允许把被测源码指向一个副本，从而在不改动真实源码的
# 前提下验证「把这行改坏，测试是否会红」。默认指向真实构建脚本。
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
# harness 写到系统临时目录，不进仓库（变异测试并发时用 RSS_HARNESS_NAME 区分）
HARNESS = os.path.join(tempfile.gettempdir(), os.environ.get("RSS_HARNESS_NAME") or "_rss_sort_harness.js")

# 抽取边界：(名称, 起止锚点)。结束锚点不计入。
# tagsfn 块是 buildArt / _mergeRemoteSources 共用的 tags 归一化助手（issue M5），
# 它位于 buildArt 定义**之前**，不单独抽取会让 build 块里出现未定义引用。
BLOCKS = [
    ("tagsfn", "  function _tagsOf(x){", "  function buildArt(){"),
    ("build", "function buildArt(){", "/* ── Sort ── */"),
    ("sort", "/* ── Sort ── */", "/* ── State ── */"),
    ("merge", "function _mergeRemoteSources(j){", "function _applyRemote(j, manual){"),
    ("dyn", "/* ── Dynamic relative time: computed from a.date at render time, never frozen ─ */",
     "function _fmtRel(dstr){"),
]

PRELUDE = r"""
/* ===== shim（只提供被测代码依赖的宿主环境，不实现业务逻辑） ===== */
var ART = [];
var SOURCES = [];
var window = {};
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
"""


def extract(src, start, end):
    i = src.find(start)
    if i < 0:
        raise SystemExit("[抽取失败] 未找到起始锚点: %r" % start)
    j = src.find(end, i + len(start))
    if j < 0:
        raise SystemExit("[抽取失败] 未找到结束锚点: %r" % end)
    return src[i:j]


def main():
    with open(BUILD, "r", encoding="utf-8") as f:
        src = f.read()

    chunks = [PRELUDE]
    for name, s, e in BLOCKS:
        body = extract(src, s, e)
        chunks.append("\n/* ===== 抽取块: %s ===== */\n" % name + body)

    cases_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.js")
    with open(cases_path, "r", encoding="utf-8") as f:
        chunks.append("\n/* ===== 用例 ===== */\n" + f.read())

    with open(HARNESS, "w", encoding="utf-8") as f:
        f.write("".join(chunks))

    node = r"C:\Users\40832\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
    if not os.path.exists(node):
        node = "node"
    p = subprocess.run([node, HARNESS], capture_output=True, text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(p.stdout or "")
    if p.stderr:
        sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
