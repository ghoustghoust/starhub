# -*- coding: utf-8 -*-
"""Pages 缺件落痕能不能活过本场——钉的是步序，不是正文。

2026-10-02 实测的落差：Stage 正文（第 27 步）确实写了 `pages_missing`，但同场第 28 步
`Prune large build artifacts` 里 `rm -rf ... build_logs ...`，而第 26 步 Save cross-build state cache
已经在 Stage 之前跑完 ⇒ 记录既没进 git、也没进缓存，当场被删。
只跑 Stage 正文的合成判据看不见这件事：它测的是"这段 shell 会写"，不是"写了还活着"。
"""
import os

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")


# ---- 跨场可查的真判据：落痕步必须排在 Save state cache 之前，且中间不许有 prune ----
# 2026-10-02 实测的落差：Stage 正文（第 27 步）确实写了 `pages_missing`，但同场第 28 步
# `Prune large build artifacts` 里有 `rm -rf ... build_logs ...`，而第 26 步 Save cross-build
# state cache 已经在 Stage 之前跑完 ⇒ 这条记录既没进 git、也没进缓存，当场就被删了。
# 只看 Stage 正文的合成判据（上面那些）永远看不见这件事 —— 它测的是"这段 shell 会写"，
# 不是"写了还活着"。本文件因此钉**步序**，而不是钉正文。
SAVE = "Save cross-build state cache"
MARK = "pages_missing"


def _doc(path=None):
    with open(path or WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _ordered_steps(doc):
    steps = []
    for job in doc["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def _record_survives(doc):
    """返回 (是否活着, 落痕步下标, 缓存保存步下标, 中间删 build_logs 的步名)。"""
    steps = _ordered_steps(doc)
    writers = [i for i, s in enumerate(steps)
               if MARK in (s.get("run") or "") and "build_logs" in (s.get("run") or "")]
    savers = [i for i, s in enumerate(steps) if (s.get("name") or "").startswith(SAVE)]
    assert len(savers) == 1, "缓存保存步命中 %d 次（应为 1）⇒ 本判据不作数" % len(savers)
    assert writers, "没有任何一步把 %s 写进 build_logs ⇒ 落痕这一半根本没实现" % MARK
    save = savers[0]
    worst = (False, (None, save, None))
    for w in writers:
        wiped = [steps[i].get("name", "?") for i in range(w + 1, save)
                 if "build_logs" in (steps[i].get("run") or "")
                 and "rm -rf" in (steps[i].get("run") or "")]
        if w < save and not wiped:
            return True, (w, save, None)
        worst = (False, (w, save, wiped[0] if wiped else None))
    return worst


def test_missing_record_survives_the_build_lifecycle():
    ok, detail = _record_survives(_doc())
    assert ok, ("缺件落痕活不过本场：%s" % (
        "写记录的步（第 %d 步）排在 Save cache（第 %d 步）之后" % (detail[0] + 1, detail[1] + 1)
        if detail[2] is None else
        "第 %d 步写了，但还没到 Save cache（第 %d 步）就被「%s」删掉了" % (detail[0] + 1, detail[1] + 1, detail[2])))


def test_the_survival_probe_is_not_vacuous():
    """双向控制（不依赖真 workflow 的形状）：正确步序必须判活，中间挨一刀必须判死。"""
    writer = {"name": "W", "run": "echo pages_missing >> build_logs/x.jsonl"}
    saver = {"name": SAVE + " (whatever)", "run": "echo save"}
    killer = {"name": "K", "run": "rm -rf build_logs docs"}
    good = {"jobs": {"j": {"steps": [writer, saver]}}}
    bad = {"jobs": {"j": {"steps": [writer, killer, saver]}}}
    too_late = {"jobs": {"j": {"steps": [saver, writer]}}}
    assert _record_survives(good)[0], "正确步序被判死 ⇒ 本判据是假红源"
    assert not _record_survives(bad)[0], "Save 之前挨一刀删 build_logs 还判活 ⇒ 主判据是空转"
    assert not _record_survives(too_late)[0], "落痕步排在 Save 之后还判活 ⇒ 同上"


DEPLOY = "Deploy to GitHub Pages"


def _red_flag(steps):
    """找出"Pages 没发布就把整场标红"的那一步：if 依赖 stage 失败、正文 exit 1、且排在 Deploy 之后。"""
    hits = []
    for i, st in enumerate(steps):
        cond = st.get("if") or ""
        run = st.get("run") or ""
        if "steps.stage.outcome" in cond and "failure" in cond and "exit 1" in run:
            hits.append(i)
    return hits


def test_unpublished_run_is_marked_red_not_green():
    """缺件那场的 job 结论不许还是 success：Stage 是 continue-on-error 的，靠它自己标不红。"""
    steps = _ordered_steps(_doc())
    deploys = [i for i, s in enumerate(steps) if (s.get("name") or "").startswith(DEPLOY)]
    assert len(deploys) == 1, "Deploy 步命中 %d 次 ⇒ 本判据不作数" % len(deploys)
    flags = _red_flag(steps)
    assert flags, (
        "没有任何一步在 stage 失败时把整场标红 ⇒ 缺件那场的结论仍是 success，"
        "只有 run 页面上一条 ::error:: 标注")
    assert flags[0] > deploys[0], (
        "标红步（第 %d 步）排在 Deploy（第 %d 步）之前 —— 那样它会连坐 Vercel/Pages 的部署动作，"
        "本仓明确不要这种形状" % (flags[0] + 1, deploys[0] + 1))


def test_the_red_flag_probe_is_not_vacuous():
    """双向控制：没有标红步必须判死；有但排在 Deploy 之前也要判死。"""
    writer = {"name": "W", "run": "echo pages_missing >> build_logs/x.jsonl"}
    saver = {"name": SAVE, "run": "echo save"}
    dep = {"name": DEPLOY, "run": "echo deploy"}
    good = {"jobs": {"j": {"steps": [writer, saver, dep,
                                     {"name": "F", "if": "always() && steps.stage.outcome == 'failure'",
                                      "run": "exit 1"}]}}}
    steps = good["jobs"]["j"]["steps"]
    assert _red_flag(steps) == [3], _red_flag(steps)
    assert not _red_flag([s for s in steps if s.get("name") != "F"]), "删掉标红步还认得出 ⇒ 空转"
    moved = [steps[0], steps[1], steps[3], steps[2]]      # 标红挪到 Deploy 之前
    assert _red_flag(moved) == [2], _red_flag(moved)
