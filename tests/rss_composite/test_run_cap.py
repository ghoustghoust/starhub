# -*- coding: utf-8 -*-
"""tests/rss_composite/test_run_cap.py
_applyRunCap() 测试运行器 — TDD RED/GREEN

从 build_rss_aggregator.py 抽取 sort 代码块（内含 _applyRunCap），
拼接 shim + 用例，交给 node 执行。
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
HARNESS = os.path.join(tempfile.gettempdir(), "_rss_run_cap_harness.js")

# 抽取边界：sort 块
BLOCKS = [
    ("sort", "/* ── Sort ── */", "/* ── State ── */"),
]

# _applyRunCap 应在 sort 块内（SRC_GAP 定义之前）
FUNC_ANCHOR = "function _applyRunCap("

PRELUDE = r"""
/* ===== shim ===== */
var ART = [];
var SOURCES = [];
var window = {};
var wallLimit = 120, WALL_STEP = 80, curArt = null;
var ANALYSIS_DATA = null;
function renderChips(){} function renderWall(){} function renderPanel(){}
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

    # 锚点检查：_applyRunCap 是否已实现
    if FUNC_ANCHOR not in src:
        print("=" * 60)
        print("_applyRunCap RED 测试：函数未实现（预期 RED）")
        print("  锚点 %r 未在源码中找到" % FUNC_ANCHOR)
        print("  → 请先实现 _applyRunCap() 使测试变绿")
        print("=" * 60)
        sys.exit(1)

    chunks = [PRELUDE]
    for name, s, e in BLOCKS:
        body = extract(src, s, e)
        chunks.append("\n/* ===== 抽取块: %s ===== */\n" % name + body)

    cases_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_run_cap.js")
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
