# -*- coding: utf-8 -*-
"""全量回归：用真实载荷（8553 条）驱动**构建脚本内嵌的真实 JS**，对比修复前/后。

关键设计：基线不是"我的复刻"，而是从 `git show HEAD:build_rss_aggregator.py`
抽出的**原始发布代码**。两边走同一套 harness、同一份数据。

用法：python tests/rss_sort/regression_full.py
前置：.deploy-tmp/live-api-rss.json（/api/rss 实取载荷）
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.path.join(ROOT, "build_rss_aggregator.py")
PAYLOAD = os.path.join(ROOT, ".deploy-tmp", "live-api-rss.json")
HARNESS = os.path.join(ROOT, ".deploy-tmp", "_full_sort_harness.js")

PRELUDE = r"""
var localStorage = { getItem: function(){ return null; }, setItem: function(){} };
var document = { getElementById: function(){ return null; } };
var ANALYSIS_DATA = null;
var wallLimit = 120, WALL_STEP = 80, curArt = null;
function renderChips(){} function renderWall(){} function renderPanel(){} function toast(){}
var window = {};
"""

MEASURE = r"""
function _t(x){ return x ? new Date(x).getTime() : NaN; }
buildArt();
var head = ART.slice(0, 120);
var nod = head.filter(function(a){ return isNaN(_t(a.date)); }).length;
var srcs = {}; head.forEach(function(a){ srcs[a.sk] = (srcs[a.sk]||0)+1; });
var keys = Object.keys(srcs);
var firstNoDate = -1;
for (var i=0;i<ART.length;i++){ if (isNaN(_t(ART[i].date))) { firstNoDate = i+1; break; } }
var top3 = keys.map(function(k){ return srcs[k]; }).sort(function(a,b){ return b-a; }).slice(0,3)
              .reduce(function(s,v){ return s+v; }, 0);
var maxSrc = keys.length ? Math.max.apply(null, keys.map(function(k){ return srcs[k]; })) : 0;
var newest = head.length ? Math.max.apply(null, head.map(function(a){ return _t(a.date); })) : NaN;
var now = Date.now();
console.log(JSON.stringify({
  total: ART.length,
  headNoDate: nod,
  headSources: keys.length,
  top3Share: +(100*top3/head.length).toFixed(1),
  maxSrcShare: +(100*maxSrc/head.length).toFixed(1),
  firstNoDateRank: firstNoDate,
  newestAgeMin: isNaN(newest) ? null : +((now-newest)/60000).toFixed(1),
  top: head.slice(0,5).map(function(a){ return a.sk + ' / ' + (isNaN(_t(a.date)) ? '(无时间)' : a.date); })
}));
"""

BLOCKS = [
    # _tagsOf 必须先于 buildArt 抽取：M5 修复后 buildArt 构造 ART 时会调用 _tagsOf(it)，
    # 漏抽这一块会让 harness 直接抛 ReferenceError: _tagsOf is not defined，
    # 而旧版 run() 对 node 崩溃只打印一行「无输出」，于是**静默失败**（本轮已修）。
    # 基线（git HEAD 的旧版）里还没有这个函数，故抽取失败只告警、不中断。
    ("tagsOf", "  function _tagsOf(x){", "function buildArt(){"),
    ("build", "function buildArt(){", "/* ── Sort ── */"),
    ("sort", "/* ── Sort ── */", "/* ── State ── */"),
]


def extract(src, start, end):
    i = src.find(start)
    j = src.find(end, i + len(start)) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    return src[i:j]


def blocks_from(src, label):
    parts, missing = [], []
    for name, s, e in BLOCKS:
        body = extract(src, s, e)
        if body is None:
            missing.append(name)
            continue
        parts.append(body)
    if missing:
        print("[%s] 警告：未找到块 %s（基线版本可能尚无该代码）" % (label, missing))
    return "\n".join(parts)


def sources_js():
    with open(PAYLOAD, "r", encoding="utf-8-sig") as f:
        pl = json.load(f)
    out = []
    for s in pl["sources"]:
        items = [{"title_zh": it.get("t") or "", "link": it.get("u") or "",
                  "pub_date": it.get("d") or "", "summary_zh": it.get("s") or "",
                  "time_str": ""} for it in s.get("items", [])]
        if not items:
            continue
        out.append({"key": s.get("key"), "name": s.get("name"), "cat": s.get("cat"),
                    "color": s.get("color"), "tier": s.get("tier") or 3, "items": items})
    # ensure_ascii=True：载荷含孤立代理字符（emoji 截断），直写会触发 UnicodeEncodeError；
    # 转义为 \udXXX 后 JS 字符串仍是合法字面量，语义不变
    return "var SOURCES = " + json.dumps(out, ensure_ascii=True) + ";\n"


def run(label, code):
    with open(HARNESS, "w", encoding="utf-8") as f:
        f.write(PRELUDE + sources_js() + code + MEASURE)
    node = r"C:\Users\40832\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
    if not os.path.exists(node):
        node = "node"
    p = subprocess.run([node, HARNESS], capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = [l for l in (p.stdout or "").splitlines() if l.startswith("{")]
    if not line:
        print("[%s] 无输出\nstdout=%s\nstderr=%s" % (label, p.stdout, p.stderr))
        return None
    r = json.loads(line[-1])
    print("\n### %s" % label)
    print("  总量=%s  首屏无日期=%s/%s  独立信源=%s  前3源占比=%s%%  最大单源=%s%%" % (
        r["total"], r["headNoDate"], 120, r["headSources"], r["top3Share"], r["maxSrcShare"]))
    print("  首个无日期条目所在名次=%s   首屏最新条目=%s 分钟前" % (r["firstNoDateRank"], r["newestAgeMin"]))
    print("  首屏前5条: %s" % " | ".join(r["top"]))
    return r


def main():
    if not os.path.exists(PAYLOAD):
        print("缺少载荷 %s" % PAYLOAD)
        return 2
    with open(BUILD, "r", encoding="utf-8") as f:
        cur = f.read()
    p = subprocess.run(["git", "-C", ROOT, "show", "HEAD:build_rss_aggregator.py"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    old = p.stdout if p.returncode == 0 else ""
    if not old:
        print("警告：无法从 git HEAD 取原始版本，跳过基线")
    else:
        run("BASELINE 修复前（git HEAD 原始代码）", blocks_from(old, "BASELINE"))
    fixed = run("FIXED 修复后（工作区当前代码）", blocks_from(cur, "FIXED"))
    if fixed is None:
        # 当前代码跑不出结果就是硬失败，不能return 0 掩盖
        print("FIXED 运行失败：当前源码无法产出结果，判定为失败")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
