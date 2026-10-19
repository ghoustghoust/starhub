# -*- coding: utf-8 -*-
"""update.yml 里"读端必须在还原动作之后、构建之前"的接线判据（A2 blocking）。

为什么这条值得用 blocking 挡：2026-10-08 P1 的读端（把 star 状态从 orphan 分支 `star-state`
并集回工作树）当时排在第 2 步，位置在 `Sync to latest remote` 的 reset 之后——注释里还专门写了
"顺序硬要求"。但工作流里**还有第二个会还原工作树的步骤**：`Restore worktree after insight tests`
的 `git checkout -- .` + `git clean -fdq`（它本来是为了擦掉洞察测试写脏的文件）。读端排在它之前，
并集就被静默回滚：读端日志明明白白写着 `known_notes.json：main 326 + 分支 326 → 并集 327（新增 1）`，
而线上首页那条 note 仍然是空串——"步骤成功 + 日志正确 + 产物里没有"，任何一道闸都不会红。

这不是把某一行写错，是"我改的到底放没放进最终产物"缺一层核对。所以这里钉三条：
① 读端步骤必须排在还原步之后、`Fetch stars & build` 之前；
② 读端与构建之间不许再出现任何还原工作树的命令；
③ 读端的 run 必须真的调用 `tools/restore_star_state.py`（防止它被改名后悄悄不干活）。

⚠ 扫描第②条时必须先剔注释行：本文件在解释这个坑时合法地写着 `git checkout -- .` 与
`git clean -fdq` 这两个字面量，不剔注释就会把注释当成命令而假红（本仓为这个坑写过 _code_only）。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPDATE_YML = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(
    ROOT, ".github", "workflows", "update.yml")

REVERT = re.compile(r"git\s+(?:checkout\s+--\s+\.|clean\s+-[a-z]*|reset\s+--hard)")


def _src():
    with open(UPDATE_YML, encoding="utf-8") as f:
        return f.read()


def _step_names(src):
    """按出现顺序取步骤名（只看 `- name:` 那一层，注释与 run 正文里的同名字符串不算）。"""
    return [m.group(1).strip().strip("\"'")
            for m in re.finditer(r"^\s{6}- name: (.+)$", src, re.M)]


def _code_only(text):
    """去掉 YAML 块里的整行注释，只留命令行——注释里合法叙述历史时会把注释当行为。"""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_readback_runs_after_worktree_revert_and_before_build():
    src = _src()
    names = _step_names(src)
    assert "Restore star state from its own branch" in names, \
        "读端步骤不见了 ⇒ star-state 的并集不会再进构建，分支上的分类与点评会静默消失"
    i_rd = names.index("Restore star state from its own branch")
    i_rt = names.index("Restore worktree after insight tests")
    i_bd = names.index("Fetch stars & build")
    assert i_rt < i_rd, \
        "读端排在 `Restore worktree after insight tests`（checkout -- . / clean -fdq）之前 ⇒ 并集被回滚"
    assert i_rd < i_bd, "读端必须排在构建之前，否则并集赶不上 fetch_and_build 读表"


def test_no_worktree_revert_between_readback_and_build():
    """读端与构建之间不许再有还原工作树的命令（只看命令行，注释先剔）。"""
    src = _code_only(_src())
    i_rd = src.index("Restore star state from its own branch")
    i_bd = src.index("name: Fetch stars & build")
    gap = src[i_rd:i_bd]
    hits = REVERT.findall(gap)
    assert not hits, "读端之后、构建之前出现了还原工作树的命令 %s ⇒ 并集会在这里被抹掉" % hits


def test_readback_actually_calls_the_tool():
    """run 正文必须真调用脚本——改名、删调用而留着步骤名，是另一种"看着有防线"。"""
    src = _src()
    i = src.index("Restore star state from its own branch")
    j = src.find("\n      - name:", i + 10)
    body = _code_only(src[i:j if j > i else len(src)])
    assert "tools/restore_star_state.py" in body, "读端步骤的 run 没调用 restore_star_state.py"


def test_readback_stays_nonblocking():
    """读端不许变成 blocking 闸：本仓有"A2 blocking + Deploy 无 if"的连坐教训，
    分支读不到时该降级继续发布，而不是把整站冻在一颗孤儿分支上。"""
    src = _src()
    i = src.index("Restore star state from its own branch")
    j = src.find("\n      - name:", i + 10)
    body = src[i:j if j > i else len(src)]
    assert "continue-on-error" not in body, \
        "continue-on-error 会把失败遮成 success（要出声就用 || echo warning，不要遮）"
    assert "::warning::" in body and "exit 1" not in _code_only(body), \
        "读端失败必须是 warning 降级，不许 exit 1 挡住发布"
