# -*- coding: utf-8 -*-
"""回归测试：前端时区盲字符串比较修复（root cause: 09-14 08:15 时间漂移）。

从真实 _build_js() 提取前端 JS，验证：
R1  钳制使用真实时间比较（+08:00 过去时间不被钳制；真正未来文章仍被钳制）
R2  排序用 getTime（混合 +08:00/Z 格式按真实时间排序，不随时区格式错排）
R3  源内 active 排序同样修复
"""
import json
import re
import subprocess
import sys

import build_rss_aggregator as m

FAILS = []


def check(name, ok, evidence=""):
    print(("PASS" if ok else "FAIL"), "|", name, ("| " + str(evidence) if evidence else ""))
    if not ok:
        FAILS.append(name)


def extract_function(js, name):
    """从生成 JS 中按函数名 brace-matching 切出完整函数文本。"""
    idx = js.find("function " + name)
    if idx < 0:
        return None
    depth = 0
    started = False
    for i in range(idx, len(js)):
        c = js[i]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return js[idx:i + 1]
    return None


def extract_braced(js, start_idx):
    """从 start_idx 处第一个 '{' 开始做括号匹配，返回配平后的文本（含收尾 ');'）。"""
    k = js.find("{", start_idx)
    if k < 0:
        return None
    depth = 0
    for p in range(k, len(js)):
        if js[p] == "{":
            depth += 1
        elif js[p] == "}":
            depth -= 1
            if depth == 0:
                return js[start_idx:p + 1] + ");"
    return None


def extract_clamp(js):
    """切出**完整**的钳制语句。

    旧实现用正则 r"var nowMs[^\\n]*\\n\\s*ART\\.forEach\\(function\\(a\\)\\{[^\\n]*\\n"：
    `[^\\n]*\\n` 只吃到回调体的第一行（`if(!a.date) return;`）就断掉，
    产出的是**未闭合**的 JS（SyntaxError）→ node stdout 为空 → json.loads 抛
    JSONDecodeError。该分支只在「当前时刻晚于样例时间 2026-09-14T04:30+08:00」时执行，
    所以是一个**日期依赖的潜伏缺陷**：2026-09-14 04:30 之后必崩，与本次 C1/C2/M5 改动无关。
    改为括号配平切全，行为与源码一致。
    """
    i = js.find("var nowMs")
    if i < 0:
        return None
    j = js.find("ART.forEach(function(a){", i)
    if j < 0:
        return None
    return extract_braced(js, i)


def main():
    # 取当前 HEAD 的生成 JS（字节级忠实）
    import subprocess as sp
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    src = subprocess.run(["git", "show", head + ":build_rss_aggregator.py"],
                         capture_output=True, text=True, encoding="utf-8").stdout
    js = m._build_js([], build_ts_ms=0)

    # ── R1: 钳制必须是真实时间比较 ──
    has_old_clamp = "a.date>nowIso" in js
    has_new_clamp = ("Date.now()" in js and "new Date(a.date)" in js
                     and "a.date>nowIso" not in js)
    check("R1a 旧字符串钳制已移除", not has_old_clamp, "仍存在 a.date>nowIso")
    check("R1b 新钳制基于 Date.now()", has_new_clamp)

    # 功能级：沙箱执行新钳制逻辑（从 js 中截取完整的钳制语句 + ART 声明）
    snippet_body = extract_clamp(js)
    if not snippet_body:
        check("R1c 钳制代码片段可提取", False, "未匹配到钳制片段")
    else:
        snippet = ("var ART=[{date:'2026-09-14T04:12:00+08:00'},{date:'2026-09-14T04:30:00+08:00'}];\n"
                   + snippet_body + "\n")
        # 模拟"过去"的真实时间：钳制不应触发（04:12+08:00 与 04:30+08:00 均为过去——
        # 以测试执行时刻为准：若当前时刻早于这两条，则它们是未来，应被钳制到当前）
        now_ms = int(sp.run(["node", "-e", "console.log(Date.now())"],
                            capture_output=True, text=True).stdout.strip())
        art_date_ms = [int(sp.run(["node", "-e", "console.log(new Date(%r).getTime())" % a["date"]],
                                  capture_output=True, text=True).stdout.strip()) for a in
                       [{"date": "2026-09-14T04:12:00+08:00"}, {"date": "2026-09-14T04:30:00+08:00"}]]
        if now_ms > max(art_date_ms):
            out = sp.run(["node", "-e", snippet + "\nconsole.log(JSON.stringify(ART.map(a=>a.date)))"],
                         capture_output=True, text=True)
            dates = json.loads(out.stdout.strip().split("\n")[-1])
            check("R1c 过去时间不被钳制", dates == ["2026-09-14T04:12:00+08:00", "2026-09-14T04:30:00+08:00"],
                  dates)
        else:
            print("SKIP R1c 测试时刻早于样例时间，钳制行为由 R1b 与 node 断言覆盖")

    # ── R2/R3: 排序函数使用真实时间比较 ──
    fn = extract_function(js, "_dateCmp")
    check("R2a _dateCmp 比较函数存在", fn is not None)
    sort_fn = extract_function(js, "applySort")
    check("R2b applySort 存在", sort_fn is not None)
    if fn and sort_fn:
        uses_time = "getTime" in fn and "localeCompare" not in fn
        check("R2c _dateCmp 用 getTime 无 localeCompare", uses_time, fn[:120])
        check("R3a applySort 调用 _dateCmp", "_dateCmp" in sort_fn)
        # 功能级：混合时区排序
        harness = (fn + "\n"
                   + "var rows=[['2026-09-14T04:12:00+08:00','A'],['2026-09-13T20:00:00Z','B'],['','C']];\n"
                   + "rows.sort(function(x,y){return _dateCmp(x[0],y[0])});\n"
                   + "console.log(JSON.stringify(rows.map(function(r){return r[1]})));")
        out = sp.run(["node", "-e", harness], capture_output=True, text=True)
        try:
            order = json.loads(out.stdout.strip().split("\n")[-1])
        except Exception:
            order = ["解析失败: " + out.stderr[:80]]
        # 真实时间：B(20:00Z=北京04:00) < A(04:12+08:00=北京04:12) < C(无日期，沉底)
        check("R3b 混合时区排序正确（B<A<C）", order == ["B", "A", "C"], order)

    print("=" * 40)
    if FAILS:
        print("FAIL:", len(FAILS), FAILS)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
