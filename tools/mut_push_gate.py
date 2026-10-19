# -*- coding: utf-8 -*-
"""推送闸的变异电池：`tests/tools/test_push_gate_ref_write.py` 能不能被捅穿。

被变异的东西是 **推送器本身**（`tools/data_api_push.py`），不是判据：
判据支持 `STARHUB_TOOLS_DIR` 这个口子 ⇒ 只往 `.deploy-tmp` 里写副本，真实文件全程不动，
所以本电池不需要 `_scratch/.mutation-lock`（那条锁是给就地改真实文件的 harness 用的）。

为什么这批变异值得跑（2026-10-02 的现实）：闸判错的两种后果都不响 ——
放行了 ⇒ 推上去被 auto-commit 回滚（09-19 实证过），拒到底 ⇒ 4 小时推不出去（当天实测）。
所以**每个"保守兜底"分支都要有靶**，否则改天有人"顺手简化"成 `return False` 也没人红。

约定与本仓其它电池一致：基线不绿 ⇒ 不声称覆盖率；红了但不是自己的靶算问题；
GREEN 控制项与正向同权重（防止判据其实"怎么改都红"）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "tools", "data_api_push.py")
DIR = os.path.join(ROOT, ".deploy-tmp", "_mut_push_gate")
COPY = os.path.join(DIR, "data_api_push.py")
TEST = os.path.join("tests", "tools", "test_push_gate_ref_write.py")

NAMED = "test_ref_write_step_is_named_in_update_yml"
HANG = "test_hanging_after_the_commit_step_is_not_a_writer"
INF = "test_commit_step_in_flight_is_a_writer"
NOSTEPS = "test_job_without_steps_is_a_writer"
UNREAD = "test_unreadable_jobs_are_writers"
UNKN = "test_run_without_the_step_is_a_writer"
FSKIP = "test_failed_and_skipped_commit_steps_are_not_writers"
GPASS = "test_gate_passes_when_the_only_busy_run_is_past_its_commit_step"
GBLOCK = "test_gate_still_blocks_when_a_busy_run_has_not_committed"
GQUEUED = "test_gate_blocks_a_queued_run"
RISKY = "test_risky_runs_only_counts_those_that_can_still_commit"
WRITERS = "test_the_only_remote_writers_are_inside_the_commit_step"

src = open(SRC, encoding="utf-8").read()


def sub(old, new):
    """落一次文本替换；锚不在 ⇒ 返回 None，调用方判 INVALID（不许把"没改动"当成"改坏了"）。"""
    if old not in src:
        return None
    return src.replace(old, new, 1)


OPENBLK = "    if jobs is None:\n        return True"
STEPBLK = "    if not mine:\n        return True"
EMPTYBLK = "    if not steps:\n        return True"
GATEBLK = "        writers = [r for r in busy if will_write_main(jobs_of(r[\"id\"]))]"
REJECT = "步还没走完，现在推会被它的旧检出回滚"

CASES = [
    # 一个变异会同时打断几条判据是常态（M6 让所有"不该再等"的断言一起红）⇒ 靶必须登记全，
    # 否则"红了但不是自己的靶"这条归属检查就成了空话。靶集合由实跑读出，不是推算。
    ("M1 闸退回旧行为：未完成的 run 一律算危险",
     sub(GATEBLK, "        writers = list(busy)"),
     "red", [GPASS]),
    ("M2 把「jobs 取不到」当安全",
     sub(OPENBLK, "    if jobs is None:\n        return False"),
     "red", [UNREAD]),
    ("M3 认不出步骤序列就放行",
     sub(STEPBLK, "    if not mine:\n        return False"),
     "red", [UNKN]),
    ("M4 「未完成」集合漏掉 in_progress",
     sub('_OPEN_STEP = ("queued", "pending", "in_progress")',
         '_OPEN_STEP = ("queued", "pending")'),
     "red", [INF, GBLOCK, RISKY]),
    ("M5 risky_runs 回到只看 status（推后空等 20 分钟）",
     sub('if r["created_at"] < push_ts and will_write_main(jobs_of(r["id"]))]',
         'if r["created_at"] < push_ts and r["status"] != "completed"]'),
     "red", [RISKY]),
    ("M6 常量与 update.yml 的真实步名脱钩",
     sub('REF_WRITE_STEP = "Commit & push if changed"', 'REF_WRITE_STEP = "Commit & push"'),
     "red", [NAMED, WRITERS, HANG, FSKIP, GPASS, RISKY]),
    ("M7 「看不到 steps」当安全（刚建 job / queued 那几分钟）",
     sub(EMPTYBLK, "    if not steps:\n        return False"),
     "red", [NOSTEPS, GQUEUED]),
    # ── GREEN 控制 ──
    ("G1 「未完成」集合再加一个无关状态（必须绿：放宽不该误红）",
     sub('_OPEN_STEP = ("queued", "pending", "in_progress")',
         '_OPEN_STEP = ("queued", "pending", "in_progress", "waiting")'),
     "green", ""),
    ("G2 拒绝文案改词（必须绿：判据只钉放行那句的步名，不该钉死措辞）",
     sub(REJECT, "Commit 步未结束，推会被回滚"),
     "green", ""),
    ("G3 提交步前后加别的步骤（必须绿：判据不依赖它在序列里的位置）",
     sub('    mine = [s for s in steps if (s.get("name") or "").strip() == REF_WRITE_STEP]',
         '    steps = steps[:1] + [{"name": "无关步骤", "status": "completed", "conclusion": "success"}] \\\n        + steps[1:]\n'
         '    mine = [s for s in steps if (s.get("name") or "").strip() == REF_WRITE_STEP]'),
     "green", ""),
]


def run():
    env = dict(os.environ, STARHUB_TOOLS_DIR=DIR, PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider", "--tb=line"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    lines = (p.stdout or "").strip().splitlines()
    bad = sorted({l.split("::")[-1].split(" ")[0]
                  for l in lines if l.startswith(("FAILED", "ERROR"))})
    return p.returncode != 0, (lines[-1] if lines else "?"), bad


def write(body):
    """写副本 + 清字节码缓存。少了后半句，本电池会**自己骗自己**（2026-10-02 实踩）：
    M2 与 M3 都是把 `return True` 改成 `return False`，改完文件字节数完全相同，
    Python 按 (mtime, size) 判定"源码没变"就复用了上一份 .pyc ⇒ M3 跑的其实是 M2 的变异，
    于是报"该红的没红"。任何 `import` 型电池（判据读的是模块而不是文本）都必须清缓存。

    prod_verify.py 也要一起放进来（2026-10-03 实踩）：data_api_push 现在 `import prod_verify`，
    副本目录里缺它 ⇒ 基线就 ModuleNotFoundError，整个电池直接不绿。
    """
    os.makedirs(DIR, exist_ok=True)
    cache = os.path.join(DIR, "__pycache__")
    if os.path.isdir(cache):
        for f in os.listdir(cache):
            os.remove(os.path.join(cache, f))
        os.rmdir(cache)
    dep = os.path.join(DIR, "prod_verify.py")
    if not os.path.exists(dep):
        with open(dep, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(open(os.path.join(ROOT, "tools", "prod_verify.py"), encoding="utf-8").read())
    with open(COPY, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    back = open(COPY, encoding="utf-8").read()
    if back != body:
        raise AssertionError("副本写进去和读出来不一致 ⇒ 变异没落地，这次结果不作数")
    cp = subprocess.run([sys.executable, "-B", "-m", "py_compile", COPY],
                        capture_output=True, text=True)
    if cp.returncode:
        raise AssertionError("变异副本编译不过 ⇒ 这条变异是无效红，不算挡住：%s"
                             % (cp.stderr or cp.stdout or "").strip()[-200:])


def main():
    write(src)
    red, tail, bad = run()
    print("基线（未变异副本）：%s  rc=%d" % (tail, 1 if red else 0))
    if red:
        print("基线不绿 ⇒ 本次不声称任何覆盖率；失败：%s" % bad)
        return 1
    problems = []
    for label, body, want, targets in CASES:
        if body is None:
            problems.append("%s INVALID（锚不在当前文件里，先对齐锚点再谈覆盖）" % label)
            continue
        if body == src and want == "red":
            problems.append("%s INVALID（变异没落到字节上）" % label)
            continue
        write(body)
        red, tail, bad = run()
        ok = (red == (want == "red"))
        if ok and want == "red":
            missed = [t for t in targets if t not in bad]
            if missed:
                ok = False
                tail += " 该红的没红：" + ",".join(missed)
            extra = sorted(set(bad) - set(targets))
            if extra:
                ok = False
                tail += " 红了但不是自己的靶：" + ",".join(extra)
        print("%-52s %s %s" % (label, "RED  " if red else "GREEN", tail[:64]
                              + ("" if ok else "   <- 期望%s 红的=%s" % (
                                  "红" if want == "red" else "绿", bad if bad else "（没解析到任何名字）"))))
        if not ok:
            problems.append(label + ("（没挡住/红错对象）" if want == "red" else "（误伤：合法改动也被判红）"))
    if os.path.exists(COPY):
        os.remove(COPY)
    cache = os.path.join(DIR, "__pycache__")
    if os.path.isdir(cache):
        for f in os.listdir(cache):
            os.remove(os.path.join(cache, f))
        os.rmdir(cache)
    print("\n问题条目：%s" % (problems if problems else "无"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
