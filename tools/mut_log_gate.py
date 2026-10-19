"""批 4 判据的变异测试：闸门/摘要顺序/删除 staging 被改动时必须变红。

用法：py -3.11 tools/mut_log_gate.py
只改副本：STARHUB_UPDATE_YML 注入。
诚实的限制：`test_hourly_summary_workflow_is_not_tracked` 读的是**本地 git 索引**，
在本地索引与远端对齐之前无法用变异体证明（把文件 add 回索引就能证，但那要动索引，
留到索引对齐之后做一次）。其余三条都在 yml 文本/结构上，可完全覆盖。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
TMP = os.path.join(ROOT, ".deploy-tmp")
TEST = os.path.join("tests", "rss_history", "test_log_daily_commit_gate.py")

GATE_IF = '          if [ "$(bash tools/daily_commit_gate.sh build_logs/.committed_date "$TODAY_BJ")" = open ]; then'
ADD_LOGS = "            git add build_logs/"
SUMMARY_NAME = "      - name: Generate daily summary"
COMMIT_NAME = "      - name: Commit & push if changed"


def step_block(text, name):
    i = text.index(name)
    j = text.index("\n      - name: ", i + len(name))
    return i, j


def swap_summary_after_commit(text):
    """把 `Generate daily summary` 搬回 Commit 之后（即改动前的形状）。"""
    si, sj = step_block(text, SUMMARY_NAME)
    body = text[si:sj]
    rest = text[:si] + text[sj:]
    ci = rest.index(COMMIT_NAME)
    cj = rest.index("\n      - name: ", ci + len(COMMIT_NAME))
    return rest[:cj] + body + rest[cj:]


def main():
    src = open(WF, encoding="utf-8").read()
    si, _ = step_block(src, SUMMARY_NAME)
    ci, _ = step_block(src, COMMIT_NAME)
    assert si < ci, "前置：摘要步应已在 Commit 之前（否则本变异脚本的语义不成立）"

    MUTS = [
        ("G1 拿掉第一处闸门的 if 行（add 变无条件）",
         src.replace(GATE_IF + "\n", "", 1)),
        ("G2 闸门写死成恒 open",
         src.replace('$(bash tools/daily_commit_gate.sh build_logs/.committed_date "$TODAY_BJ")',
                     'echo open', 1)),
        ("G3 去掉第一处 ls-files -d / git rm --cached",
         src.replace("            git ls-files -d -z build_logs | xargs -0 -r git rm --cached --quiet\n", "", 1)),
        ("G4 摘要步搬回 Commit 之后（回到今天的样子）", swap_summary_after_commit(src)),
        ("G5 标记文件不入库（第一处不写 marker）",
         src.replace('            echo "$TODAY_BJ" > build_logs/.committed_date\n', "", 1)),
    ]

    env_base = dict(os.environ, PYTHONIOENCODING="utf-8")
    env_base.pop("STARHUB_UPDATE_YML", None)
    base = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT, env=env_base,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    bl = (base.stdout.strip().splitlines() or ["?"])
    print("基线（未变异）：%s rc=%d" % (bl[-1], base.returncode))
    print("   注：唯一允许的红是 not_tracked 那条（本地索引未对齐），它不参与本变异评分")
    if "1 failed" not in bl[-1] and base.returncode != 0:
        print("\n".join(bl[-25:]))
        return 1

    escapes = 0
    for label, mutated in MUTS:
        if mutated == src or not mutated:
            print("%-52s SKIP（锚点没找到）" % label)
            escapes += 1
            continue
        out = os.path.join(TMP, "_mut_log_gate.yml")
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(mutated)
        env = dict(os.environ, STARHUB_UPDATE_YML=out, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                            "-p", "no:cacheprovider", "--deselect",
                            "tests/rss_history/test_log_daily_commit_gate.py::test_hourly_summary_workflow_is_not_tracked"],
                           cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        os.remove(out)
        lines = (p.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "?"
        failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
        red = p.returncode != 0
        print("%-52s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail,
                                 ("  <- " + ", ".join(failed[:2])) if failed else ""))
        if not red:
            escapes += 1
    print("\n未被挡住的变异/无效变异：%d" % escapes)
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(main())
