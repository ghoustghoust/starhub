# -*- coding: utf-8 -*-
"""读端接线判据的变异电池（tests/site_nav/test_pages_build_wiring.py 的四条）。

它要回答的只有一个问题：那四条判据是不是真的会红。2026-10-08 的教训是"步骤成功 + 日志正确 +
产物里没有"——读端排在 `git checkout -- .` 之前，并集被抹掉却没有任何一道闸响应。所以这里的
变异不是改坏脚本，而是**把当时的错误顺序复原**，看判据能不能抓回来。

与被测文件的关系：真实 update.yml 一个字节不动，副本写到 .deploy-tmp，靠 STARHUB_UPDATE_YML
注入点让判据读副本（与 tools/mut_artifact_scope.py 同形）。

用法：py -3.11 tools/mut_build_wiring.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
COPY = os.path.join(ROOT, ".deploy-tmp", "_update_build_wiring.yml")
TEST = os.path.join(ROOT, "tests", "site_nav", "test_pages_build_wiring.py")

RD_MARK = "      - name: Restore star state from its own branch"
RT_MARK = "      - name: Restore worktree after insight tests"


def _cut_step(src, mark):
    """取出从某个 `- name:` 起、到下一个同缩进 `- name:` 之前的整步文本。"""
    i = src.index(mark)
    j = src.find("\n      - name:", i + len(mark))
    assert j > i, "找不到 %r 之后的步骤边界" % mark
    return src[i:j], i, j


def x1_wrong_order(src):
    """把读端整步搬回还原步**之前**（就是 10-08 上午真实那个位置）。"""
    rd, i, j = _cut_step(src, RD_MARK)
    body = src[:i] + src[j:]
    k = body.index(RT_MARK)
    return body[:k] + rd + body[k:]


def x2_revert_after_readback(src):
    """读端之后、构建之前塞一条还原命令（等价于"后面还有别人在擦工作树"）。"""
    rd, i, j = _cut_step(src, RD_MARK)
    injected = rd.replace("            || echo \"::warning::[star-state]",
                          "            || echo \"::warning::[star-state]\"\n"
                          "          git clean -fdq")
    assert injected != rd, "X2 锚点没打上"
    return src[:i] + injected + src[j:]


def x3_tool_not_called(src):
    """步骤名留着、run 改成别的命令：典型的"防线还在但已经不干活"。"""
    return src.replace("python tools/restore_star_state.py \\",
                       "python tools/restore_star_state_v0.py \\", 1)


def x4_blocking(src):
    """把降级改成 exit 1：一颗孤儿分支不该有能力冻住整场发布。"""
    rd, i, j = _cut_step(src, RD_MARK)
    return src[:i] + rd.replace("本场使用 main 上的副本（不阻断发布）\"",
                                "本场使用 main 上的副本（不阻断发布）\"; exit 1") + src[j:]


MUTS = [
    ("X1 把读端搬回还原步之前（10-08 真实那个位置）", x1_wrong_order,
     "test_readback_runs_after_worktree_revert_and_before_build"),
    ("X2 读端之后又来一条 git clean（并集被抹掉）", x2_revert_after_readback,
     "test_no_worktree_revert_between_readback_and_build"),
    ("X3 步骤名在但 run 不调脚本（防线空转）", x3_tool_not_called,
     "test_readback_actually_calls_the_tool"),
    ("X4 读端失败改成 exit 1（孤儿分支获得冻发布的能力）", x4_blocking,
     "test_readback_stays_nonblocking"),
]


def run(target=None):
    cmd = [sys.executable, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"]
    if target:
        cmd += ["-k", target]
    env = dict(os.environ, STARHUB_UPDATE_YML=COPY, PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=900)
    lines = (p.stdout or "").strip().splitlines()
    failed = sorted({l.split("::")[-1].split(" ")[0]
                     for l in lines if l.startswith(("FAILED", "ERROR"))})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    src = open(WF, encoding="utf-8").read()
    open(COPY, "w", encoding="utf-8", newline="").write(src)
    red, tail, failed = run()
    print("基线（未变异副本）：%s" % tail)
    if red:
        print("基线不绿 ⇒ 不声称覆盖率；失败：%s" % failed)
        return 1

    bad = []
    for label, fn, target in MUTS:
        try:
            mutated = fn(src)
        except AssertionError as e:
            bad.append("%s 打不上：%s" % (label, e))
            continue
        if mutated == src:
            bad.append("%s 没产生任何改动 ⇒ 锚点失效" % label)
            continue
        open(COPY, "w", encoding="utf-8", newline="").write(mutated)
        red, tail, failed = run(target)
        # 两个条件都要满足：整场红 + 靶判据自己红（只红别的判据 = 钉错了地方）
        if not red:
            bad.append("%s：打了坏改动仍然绿 ⇒ 空判据" % label)
            continue
        if not any(target.startswith(f.replace("()", "")) or f in target for f in failed):
            bad.append("%s：红了但不是靶判据（失败=%s）" % (label, failed))
            continue
        print("  %-46s 挡住 -> %s" % (label, target))

    if bad:
        print("\n%d 项不合格：" % len(bad))
        for b in bad:
            print("  - " + b)
        return 1
    print("\n全部 %d 个变异都被各自靶判据挡住，且每条都有未变异基线先证明过绿。" % len(MUTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
