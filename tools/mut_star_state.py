# -*- coding: utf-8 -*-
"""star-state 提交步的变异电池：每条判据都要能被一个对应的坏改动挡住，否则那判据是空转。

它替代 2026-10-08 P1 退役的 `mut_push_retry.py`——那个电池钉的是"撞 main ⇒ merge 重试一次"
那条链，而本车道不再写 main 之后单写者不需要 merge，链整体消失 ⇒ 留着它就是钉一个不存在的
机制（另一种空转）。这里钉的是新机制的十个真实失败模式。

做法与 tools/mut_reader.py 同：真实文件一个字节不动，把 star-fast.yml 复制进临时目录打变异，
用 STAR_FAST_YML 注入点让判据读副本。每轮从干净副本重打（变异绝不互相掩盖），
并要求"整体红 + 靶判据自己红"两个条件同时成立——只红别的判据算钉错了地方。

用法：py -3.11 tools/mut_star_state.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YML = os.path.join(".github", "workflows", "star-fast.yml")
TEST = os.path.join("tests", "site_nav_drift", "test_star_fast_wiring.py")

MUTATIONS = [
    ("S1 推回 main（P1 白做）",
     'git push $lease origin "$commit:refs/heads/star-state"',
     'git push origin "$commit:refs/heads/main"',
     "test_star_state_lands_on_its_branch_and_main_stays_untouched"),
    ("S2 租约退化成裸 --force",
     'lease="--force-with-lease=star-state:$parent"',
     'lease="--force"',
     "test_star_state_appends_and_never_uses_bare_force"),
    ("S3 内容面混进第三个文件",
     "for f in known_categories.json known_notes.json; do",
     "for f in known_categories.json known_notes.json descriptions_zh.json; do",
     "test_star_state_content_surface_is_exactly_two_files"),
    ("S4 无新星也照样推（每 15 分钟白涨提交）",
     '            echo "changed=false" >> "$GITHUB_OUTPUT"; exit 0\n          fi\n          # 2026-10-08 P1',
     '            echo "changed=false" >> "$GITHUB_OUTPUT"\n          fi\n          # 2026-10-08 P1',
     "test_no_new_star_pushes_nothing_at_all"),
    ("S5 不追加、每场重开一条历史（覆盖上一场）",
     'commit=$(git commit-tree "$tree" -p "$parent" \\',
     'commit=$(git commit-tree "$tree" \\',
     "test_star_state_appends_and_never_uses_bare_force"),
    # S6 = 10-08 09:45/10:00 真实那场事故的形状：只 ls-remote 拿 sha、不 fetch 就喂 commit-tree -p。
    # 判据必须在这一条上回红，否则它钉住的只是"本地已经有父提交"那种理想环境（全量 clone），
    # 而 CI 的 actions/checkout 是单分支浅检出——这正是它当时全绿、生产连红两场的差额。
    ("S6 不 fetch 就把远端 sha 当父提交（浅检出必崩）",
     'if ! git fetch -q --depth=1 origin refs/heads/star-state; then',
     'if ! true; then',
     "test_star_state_appends_and_never_uses_bare_force"),
    # S7 = 10-08 那条真丢了的点评的形状：fetch 到了父提交却只用它的 sha、不读它的内容，
    # 于是本场基于 main（notes 早已无人提交）造的 tree 会把上一场写进分支的键整片抹掉。
    # S7：整段并集（连同守门）都不做。写成 `if ! true` 是因为直接删 if 行会留下孤儿子句、
    # bash 语法崩 ⇒ 整步非零退出，那是"钉错地方"而不是"判据红"。`! true` 恒假 ⇒ 并集不调用、
    # 守门体也不进，等价于"回到只拿 sha 不拿内容的世界"。
    ("S7 造 tree 前不做写端并集（每场抹掉上一场的键）",
     'if ! PYTHONIOENCODING=utf-8 python tools/restore_star_state.py --merge-onto "$parent"; then',
     'if ! true; then',
     "test_star_state_recovers_a_key_only_the_branch_has"),
    # S8：并集跑了、但拒绝信号被吃掉 ⇒ 一路走到 push，把没并过的缩小 tree 盖到分支 tip 上。
    # 写成 `&& false` 是为了保持 bash 语法合法（直接删 if 行会让整步崩在语法上，
    # 那是"非零退出"而不是"判据红"，电池会把它判成钉错地方）。
    ("S8 闸门拒绝了也照样推（把防丢失变成丢更多）",
     'if ! PYTHONIOENCODING=utf-8 python tools/restore_star_state.py --merge-onto "$parent"; then',
     'if PYTHONIOENCODING=utf-8 python tools/restore_star_state.py --merge-onto "$parent" >/dev/null 2>&1 && false; then',
     "test_star_state_gives_up_the_push_when_the_merge_is_refused"),
    # S9/S10 = 2026-10-08 对抗审查命中的两条静默形状：tee 掩掉 python 的崩溃、
    # 管道尾的 cut 掩掉 ls-remote 的失败。
    ("S9 Fast refresh 去掉 pipefail（崩溃被 tee 掩成绿）",
     'run: set -o pipefail; python fast_refresh.py | tee fast.log',
     'run: python fast_refresh.py | tee fast.log',
     "test_fast_step_fails_when_fast_refresh_crashes"),
    ("S10 ls-remote 写成管道（失败被 cut 掩成空串⇒假因'第二个写者'）",
     'ls_line=$(git ls-remote --exit-code origin refs/heads/star-state) || rc=$?',
     'ls_line=$(git ls-remote origin refs/heads/star-state | cut -f1); rc=0',
     "test_star_state_names_the_real_cause_when_the_baseline_read_fails"),
]


def _run(tmp, name):
    env = {**os.environ, "STAR_FAST_YML": os.path.join(tmp, YML),
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "-k", name],
                       cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _stage(tmp):
    # 内容面判据要读 .gitignore（钉"分支里不许混进被 ignore 的文件"），所以三件都得 copy；
    # 少一件就是临时目录里 FileNotFoundError，被下面的"非零退出"误报成"判据不合格"。
    for rel in (YML, TEST, ".gitignore"):
        dst = os.path.join(tmp, rel)
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dst)
    # 行为判据会把这脚本复制进临时裸仓的工作树（写端并集是它干的），而它按 __file__ 反推仓库根
    # ⇒ 电池那份副本里也必须有它，否则对照组直接 FileNotFoundError，六条变异全被判成"作废"。
    os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
    shutil.copyfile(os.path.join(ROOT, "tools", "restore_star_state.py"),
                    os.path.join(tmp, "tools", "restore_star_state.py"))

def main():
    if hasattr(sys.stdout, "reconfigure"):
        # 电池自己的输出也得钉编码：Windows 默认 GBK，消息里一个 '⇒' 就能让脚本崩在 print 上，
        # 而 rc=1 看起来像"有变异没挡住"——那是我的打印失败，不是判据失败。
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if shutil.which("bash") is None or shutil.which("git") is None:
        print("缺 bash/git，无法实跑（这电池必须跑 shell，不降级为文本检查）")
        return 1
    bad = []
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, ".deploy-tmp")) as tmp:
        for mid, old, new, must in MUTATIONS:
            _stage(tmp)  # 每轮从干净副本重打
            code0, out0 = _run(tmp, must)
            if code0 != 0:
                bad.append("%s：对照组（未变异）就不绿，本条判定作废：%s" % (mid, out0.strip()[-220:]))
                continue
            p = os.path.join(tmp, YML)
            s = io.open(p, encoding="utf-8").read()
            n = s.count(old)
            if n != 1:
                bad.append("%s：靶子在 yml 里出现 %d 次 ⇒ 锚点失效，变异没打上" % (mid, n))
                continue
            io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new, 1))
            code, out = _run(tmp, must)
            if code == 0:
                bad.append("%s：打了坏改动，%s 仍然绿 ⇒ 空判据" % (mid, must))
                continue
            if "failed" not in out and "error" not in out.lower():
                bad.append("%s：非零退出但不是判据红（可能是收集崩）：%s" % (mid, out.strip()[-220:]))
                continue
            print("  %-38s 挡住 -> %s" % (mid, must))

    if bad:
        print("\n%d 项不合格：" % len(bad))
        for b in bad:
            print("  - " + b)
        return 1
    print("\n全部 %d 个变异都被各自靶判据挡住，且每条都有未变异对照组先证明过绿。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
