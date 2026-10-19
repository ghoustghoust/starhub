# -*- coding: utf-8 -*-
"""症状③最后一格：全文与摘要逐字同段时，阅读器不许再把摘要单列一遍。

为什么量**产物**而不是源文件：`build_rss_aggregator.py` 里的 JS 活在非 raw 的 Python 三引号串中，
源码写的转义要先被 Python 解一道才成为 JS（本仓 2026-10-01/10-05 各踩过一次），
所以只有生成出来的内联脚本才算被测对象。

为什么是**真跑**而不是 grep：本仓点名过"定义了不调用 = 等于没修"，也点名过
"接线判据只 grep 函数名 ⇒ 子匹配变异不红"。这里把渲染那一段连同辅助函数一起喂给 node，
问的是"这段跑完 `h` 里到底有没有 `r2-summary`"。
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))

from _loader import load_build  # noqa: E402

SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
BLOCK_START = "    if(a.s){"
BLOCK_END = "h+='<div class=\"r2-foot-hint\">"


def _node():
    n = shutil.which("node")
    if not n:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    return n


def _artifact_script():
    mod = load_build()
    html = mod.build_html([], "2026-10-05 06:00", 0, 0, analysis_data=None,
                          diverse_window_minutes=120, diverse_enabled=True)
    for m in SCRIPT_RE.finditer(html):
        body = m.group(1)
        if "_summaryDuplicatedByFulltext" in body:
            return body
    raise AssertionError("产物里没有 _summaryDuplicatedByFulltext ⇒ 去重的刀没进页面")


def _slice_block(script):
    """取 `if(a.s){...} else {...}` 这一整段（含新加的 _dupFt 守卫），到页脚提示之前为止。"""
    i = script.find(BLOCK_START)
    assert i >= 0, "渲染段起点没找到：`if(a.s){` 形状变了 ⇒ 这条判据会空跑"
    j = script.find(BLOCK_END, i)
    assert j > i, "渲染段终点没找到"
    helper_i = script.find("function _summaryDuplicatedByFulltext")
    assert helper_i >= 0
    helper_j = script.find("\n  }", helper_i)
    assert helper_j > helper_i
    helper = script[helper_i:helper_j + len("\n  }")]
    return helper, script[i:j]


def _run(tmp_path, script, cases, helper=None, block=None):
    """把真产物里的辅助函数 + 渲染段装进一个可调用外壳，逐条喂样本，取回 h。"""
    helper = helper if helper is not None else _slice_block(script)[0]
    block = block if block is not None else _slice_block(script)[1]
    driver = "\n".join([
        helper,
        "module.exports = function (a) {",
        "  var h = '';",
        "  function esc(x){ return String(x == null ? '' : x); }",
        "  function formatSummary(x){ return String(x); }",
        # 存意给 false：让"原文/翻译"那一块真的被产出，守卫的边界才有东西可钉
        # （给 true 时整块不外发，"摘要被隐掉但按钮漏出来"这类缺陷看不见）。
        "  function isMostlyZh(){ return false; }",
        "  " + block,
        "  return h;",
        "};",
    ])
    f = os.path.join(str(tmp_path), "driver.js")
    with open(f, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(driver)
    runner = os.path.join(str(tmp_path), "run.js")
    with open(runner, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("const f = require(%r);\nprocess.stdout.write(JSON.stringify(%s.map(f)));\n"
                 % (f, repr(cases)))
    p = subprocess.run([_node(), runner], capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, "node 跑崩：\n%s\n%s" % (out[:400], p.stderr.decode("utf-8", "replace")[:600])
    return __import__("json").loads(out)


LONG = "这是一段足够长的摘要文字，用来判断它是否与内嵌全文逐字相同。"
LONGER = LONG + "而全文在后面还有别的内容，所以两者不该被当成重复。"
# `fetchFullArticle` 的入屏门槛是 `a.fc.length > 100`（**原始**长度，带标签）。
# 所以"隐摘要"必须和它同门槛：短于门槛的 fc 根本不上屏，此时再隐 ⇒ 面板一个字都不剩。
# LONGDUP 是"够长的那一半"（加 `<p></p>` 后 127 字符 > 100 ⇒ 会入屏 ⇒ 允许隐）。
LONGDUP = LONG * 4
# 门槛两侧的精确一对：fc 原始长度**正好 100**（不许隐）与 **101**（可以隐）。
P100 = "文" * 93          # + "<p></p>" = 100
P101 = "文" * 94          # + "<p></p>" = 101
# 前缀重复（2026-10-05 用户裁定"重复的就要隐藏"）：摘要就是正文开头那一段，后面还有别的内容。
# 现取线上读数：7,184 条里逐字相等 97 条、**前缀且不等 71 条**（两者都 >24 字、fc 都 >100）。
# 出口那四条会把摘要截到 200 并补省略号（U+2026），所以比前缀之前要先把尾巴摘掉，
# 否则现抓/批量通道上的那一格永远判不出重复。
PFX_TAIL = "而后正文里还有整段别的内容，这部分是摘要没有给出的。"
ELL = chr(0x2026)        # 省略号：一律写码位，不贴字形
# 渲染里那行"出厂摘要入屏"的原文（产物层，不是源文件层）：反证要拿它做锚点。
SUM_LINE = "h+='<div class=\"r2-summary\">'+formattedSummary+'</div>';"


def test_reader_hides_summary_when_fulltext_repeats_it(tmp_path):
    """同段 ⇒ 不出现 r2-summary；其余形状必须照旧出现（反向那半与正向同权重）。"""
    script = _artifact_script()
    helper, block = _slice_block(script)
    assert "if(!_dupFt){" in block, "渲染段里没有 _dupFt 守卫 ⇒ 刀没接上"
    cases = [
        {"s": LONGDUP, "fc": "<p>" + LONGDUP + "</p>"},                  # 逐字重复且够长会入屏 ⇒ 隐藏
        {"s": LONGDUP, "fc": "<p>" + LONGDUP + "</p><img src=x>"},        # 重复但正文带图 ⇒ 仍隐藏（图在正文里）
        {"s": LONGDUP, "fc": "<p>" + LONGER + "</p>"},                    # 正文更长 ⇒ 摘要照旧出现
        {"s": LONGDUP, "fc": ""},                                         # 无内嵌全文 ⇒ 摘要出现
        {"s": "短句。", "fc": "<p>短句。</p>"},                              # ≤24 字 ⇒ 不隐藏（宁少剥）
        {"s": P100, "fc": "<p>" + P100 + "</p>"},                         # 逐字相等但 fc 原始正好 100 字符 ⇒
        # ↑ `fetchFullArticle` 只在 `fc.length>100` 时才把正文放上屏，等于 100 不上屏 ⇒ 摘要不许隐，
        #   否则这块面板会一个字都不剩（线上今天 93/93 都是长正文，属数据巧合，不是代码保证）
        {"s": P101, "fc": "<p>" + P101 + "</p>"},                         # 刚过门槛 ⇒ 允许隐
        {"s": LONGDUP, "fc": "<p>" + LONGDUP + PFX_TAIL + "</p>"},         # 摘要=正文开头那一段 ⇒ 也要隐
        {"s": LONGDUP + ELL, "fc": "<p>" + LONGDUP + PFX_TAIL + "</p>"},   # 同上，但摘要被出口截断补了省略号
    ]
    got = _run(tmp_path, script, cases, helper, block)
    has = [("r2-summary" in g) for g in got]
    assert has == [False, False, True, True, True, True, False, False, False], (
        "摘要去重的开火形状不对：%s\n%s" % (has, "\n".join(g[:120] for g in got)))
    # 守卫的边界：`原文/翻译`那一块和摘要是一对（隐一个就得隐两个），
    # 漏出半对会在卡片顶部挂一对指向空摘要的按钮；漏隐另一半则是同段又出现两遍。
    tog = [("r2-lang-toggle" in g) for g in got]
    assert tog == has, "摘要与其翻译按钮不同步（隐了摘要漏了按钮，或整对没隐）：%s vs %s" % (has, tog)
    for g, want in zip(got, has):
        assert ("btnOrig" in g) is want, "btnOrig 没跟着这一对走：%r" % g[:120]
    # fallback-card（无摘要那一支）不许被守卫挪走：这条改成**跑出来的**，不是段里有没有字面量
    no_s = _run(tmp_path, script, [{"s": "", "fc": "<p>" + LONGDUP + "</p>"}], helper, block)
    assert "fallback-card" in no_s[0], "摘要为空时兜底卡没渲染 ⇒ else 那一支断了"
    assert "r2-summary" not in no_s[0]
    print("[去重] 7 形状读数 has(r2-summary)=%s has(lang-toggle)=%s；空摘要⇒兜底卡在" % (has, tog))


def test_reader_dedupe_helper_truth_table(tmp_path):
    """辅助函数单独真跑：等值/前缀/短摘要/缺参四种形状各给一个答案。"""
    script = _artifact_script()
    helper = _slice_block(script)[0]
    f = os.path.join(str(tmp_path), "helper.js")
    with open(f, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(helper + "\nmodule.exports = _summaryDuplicatedByFulltext;\n")
    runner = os.path.join(str(tmp_path), "hrun.js")
    cases = [[LONGDUP, LONGDUP], [LONGDUP, LONGER], ["短句。", "短句。"], [LONGDUP, ""], ["", LONGDUP],
             [LONGDUP, "<div  >\n" + LONGDUP + "\n</div>"],
             [P100, "<p>" + P100 + "</p>"], [P101, "<p>" + P101 + "</p>"], [LONG, "<p>" + LONG + "</p>"],
             [LONGDUP, LONGDUP + PFX_TAIL],                 # 前缀（摘要短、正文还有后续）
             [LONGDUP + ELL, LONGDUP + PFX_TAIL],           # 前缀 + 出口补的省略号
             [LONGDUP + PFX_TAIL, LONGDUP],                 # 反过来：摘要比正文长 ⇒ 不是重复
             ["短句。", "短句。后面还有内容"],                 # ≤24 字 ⇒ 不隐
             [P100, P100 + PFX_TAIL]]                       # 门槛比的是**正文**长度，不是摘要长度
    with open(runner, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("const f = require(%r);\nprocess.stdout.write(JSON.stringify(%s.map(c => f(c[0], c[1]))));\n"
                 % (f, repr(cases)))
    p = subprocess.run([_node(), runner], capture_output=True)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[:500]
    got = __import__("json").loads(p.stdout.decode("utf-8", "replace"))
    assert got == [True, False, False, False, False, True, False, True, False,
                   True, True, False, False, True], (
        "真值表不对（等值/前缀且 fc>100=True、正文更长≠前缀=False、≤24 字=False、缺一半=False、"
        "只差标签/空白=True、fc 原始 100/37=False、fc 原始 101=True、摘要比正文长=False）：%s" % got)


def test_dedupe_guard_is_not_vacuous(tmp_path):
    """反空转：把守卫硬写成"永不重复"，第一条用例就必须重新出现 r2-summary。

    没有这一格，上面那条"隐藏"断言可以在 `return false` 的实现上照样绿。
    """
    script = _artifact_script()
    helper, block = _slice_block(script)
    neutered = helper.replace("return a.length > 24 && f.slice(0, a.length) === a;", "return false;")
    assert neutered != helper, "辅助函数体形状变了 ⇒ 这条反证是空的"
    got = _run(tmp_path, script, [{"s": LONGDUP, "fc": "<p>" + LONGDUP + "</p>"}], neutered, block)
    assert "r2-summary" in got[0], (
        "把去重判据写死成 false 之后摘要仍然不出现 ⇒ 隐藏动作不是这个判据在管的")
    print("[反空转] 写死 false ⇒ r2-summary 回到页面（判据有牙）")

    # 第二格反证：把 `原文/翻译`那一块从守卫里放出去（补一个 `}` 关摘要、再开一个裸块接住原括号，
    # 括号守恒 ⇒ node 不会因语法错冒充"挡住"）。这条必须让同段那一格出现"没摘要但有按钮"，
    # 否则上面那句 tog == has 是恒真的。
    assert SUM_LINE in block, "锚点行不在渲染段里 ⇒ 这条反证是空的（别自证干净）"
    escaped = block.replace(SUM_LINE, SUM_LINE + "} {", 1)
    got2 = _run(tmp_path, script, [{"s": LONGDUP, "fc": "<p>" + LONGDUP + "</p>"}], helper, escaped)
    assert "r2-summary" not in got2[0] and "r2-lang-toggle" in got2[0], (
        "escape 变异没造出【隐了摘要、漏了按钮】的形状 ⇒ 同步断言盯不住它：%r" % got2[0][:160])
    print("[反空转] 按钮逃出守卫 ⇒ 判据抓到（摘要消失、r2-lang-toggle 漏出）")

    # 第三格反证：把无摘要那一支的 `else` 禁掉（`} else {` → `} else if(!1) {`，括号与语法都不动）。
    # 若这样兜底卡仍然"在"，上面那条 fallback-card 断言就是空的（复评第 4 轮点名：旧形状只钉字面量，
    # 副本里改成 `} else if(0){` 三条判据全绿）。
    assert block.count("    } else {") == 1, (
        "`} else {` 在渲染段里不是恰好一处 ⇒ 这条反证会打错地方：%d" % block.count("    } else {"))
    neutered_else = block.replace("    } else {", "    } else if(!1) {", 1)
    got3 = _run(tmp_path, script, [{"s": "", "fc": ""}], helper, neutered_else)
    assert "fallback-card" not in got3[0], (
        "把 else 支禁掉后兜底卡仍在 ⇒ 那条断言盯不住它：%r" % got3[0][:160])
    print("[反空转] else 支写死不进 ⇒ 兜底卡消失（断言有牙）")
