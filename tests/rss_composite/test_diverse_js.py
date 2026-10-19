# -*- coding: utf-8 -*-
"""weightedShuffle() JS 侧测试运行器

做法：从 build_rss_aggregator.py 中抽取 sort 代码块（内含 applySort 与
weightedShuffle），拼接 shim + 用例，交给 node 执行。
抽取 sort 块而非只抽 weightedShuffle，是为了让 D11/D12 能拿到真实的
ART / sortMode / applySort 上下文，而不是在 harness 里另造一份。
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
HARNESS = os.path.join(tempfile.gettempdir(), "_rss_composite_diverse_harness.js")

# 被测函数锚点：源码中缺失即为「功能未实现」→ RED
FUNC_ANCHOR = "function weightedShuffle("
END_ANCHOR = "/* ── weightedShuffle end ── */"

# 抽取边界：sort 块内含 sortMode / _dateCmpDesc / applySort / weightedShuffle
BLOCKS = [
    ("sort", "/* ── Sort ── */", "/* ── State ── */"),
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

    if FUNC_ANCHOR not in src:
        raise SystemExit("[抽取失败] 未找到起始锚点: %r" % FUNC_ANCHOR)
    if END_ANCHOR not in src:
        raise SystemExit("[抽取失败] 未找到结束锚点: %r" % END_ANCHOR)

    chunks = [PRELUDE]
    for name, s, e in BLOCKS:
        body = extract(src, s, e)
        chunks.append("\n/* ===== 抽取块: %s ===== */\n" % name + body)

    cases_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_diverse.js")
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
