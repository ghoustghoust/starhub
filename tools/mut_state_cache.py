"""批 1+2 判据的变异测试：每个变异都必须让至少一条 starhub-state 接线判据变红。

用法：py -3.11 tools/mut_state_cache.py
约定：只改副本，靠 STARHUB_UPDATE_YML / STARHUB_GITIGNORE 注进判据（与 RSS_BUILD_SRC 同族），
     绝不就地改 .github/workflows/update.yml 或 .gitignore。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
GI = os.path.join(ROOT, ".gitignore")
TMP = os.path.join(ROOT, ".deploy-tmp")
TEST = os.path.join("tests", "rss_history", "test_state_cache_wiring.py")

RESTORE_MARK = "      - name: Restore cross-build state cache"
DIAGNOSE_MARK = "      - name: Diagnose cross-build state cache restore"
SAVE_MARK = "      - name: Save cross-build state cache"
CLEANUP_MARK = "      - name: Cleanup old build logs"
STAGE_MARK = "      - name: Stage Pages site for Actions deployment"
# 2026-10-02：Stage 被搬到 Save 之前（缺件落痕要赶在缓存保存前），所以 Save 块的**结束锚**
# 不能再是 Stage —— 那会让 index() 找不到终点直接崩。换成 Save 之后的那一步（Prune）。
PRUNE_MARK = "      - name: Prune large build artifacts"
WORKTREE_MARK = "      - name: Restore worktree after insight tests"
BUILD_MARK = "      - name: Fetch stars & build"
# 行锚不能绑首列文件名：批 5b 把 rss-data-0.js 从清单里摘掉后，旧锚 `git add rss-data-0.js `
# 会一处也匹配不到 ⇒ 所有 T1/T2 变异变成"没落地的 INVALID"。改成"缩进 10 空格 + ≥4 个操作数"
# 这种与内容无关的形状，条件式 `then git add x; fi`（只有 1 个操作数）天然不匹配。
ADD_SHAPE = re.compile(r"^          git add \S+(?: \S+){3,}$", re.M)


def block(text, start_mark, end_mark):
    i = text.index(start_mark)
    j = text.index(end_mark, i)
    return text[i:j]


def add_lines(src):
    """返回所有主/重试清单行的 (起点, 终点) 区间。"""
    return [(m.start(), m.end()) for m in ADD_SHAPE.finditer(src)]


def main():
    src = open(WF, encoding="utf-8").read()
    gi = open(GI, encoding="utf-8").read()
    restore_blk = block(src, RESTORE_MARK, DIAGNOSE_MARK)
    diagnose_blk = block(src, DIAGNOSE_MARK, BUILD_MARK)
    save_blk = block(src, SAVE_MARK, PRUNE_MARK)
    al = add_lines(src)
    assert len(al) == 2, "git add 清单行应有 2 处（主路径 + 重试分支），实测 %d" % len(al)

    MUTS = [
        ("S1 删掉 Restore 步", src.replace(restore_blk, "", 1), None),
        ("S2 删掉 Save 步", src.replace(save_blk, "", 1), None),
        ("S3 Save 的 key 去掉 run_attempt", save_blk_keyless(src), None),
        ("S4 restore-keys 写成别的族名",
         src.replace("starhub-state-${{ runner.os }}-\n", "starhub-stateX-${{ runner.os }}-\n", 1), None),
        ("S5 缓存 path 少一个文件",
         src.replace("            rss_trend_history.json\n", "", 2), None),
        ("S6 Restore 挪到 git clean 之前",
         src.replace(restore_blk, "", 1).replace(WORKTREE_MARK, restore_blk + WORKTREE_MARK, 1), None),
        ("S7 Save 挪到 Cleanup 之前",
         src.replace(save_blk, "", 1).replace(CLEANUP_MARK, save_blk + CLEANUP_MARK, 1), None),
        ("S8 trim 不给这个族配 --keep", src.replace(" --keep starhub-state=3", "", 1), None),
        ("S9 --keep starhub-state=1", src.replace("--keep starhub-state=3", "--keep starhub-state=1", 1), None),
        ("S10 族名改成下划线（trim 认不出）",
         src.replace("--keep starhub-state=3", "--keep starhub_state=3", 1), None),
        ("S11 诊断不打字节数", src.replace('echo "  OK $f ($(wc -c < "$f") B)"', 'echo "  OK $f"', 1), None),
        ("S12 冷启动不出声",
         src.replace('echo "::warning title=跨场状态冷启动::$f 不在盘上，本场按冷启动跑（代价：重译/重跑重分析）"',
                     'echo "  note: $f absent"', 1), None),
        ("T1 把 hot_history 加回主路径清单",
         src[:al[0][1]] + " hot_history.json" + src[al[0][1]:], None),
        ("T2 只摘主路径、重试分支仍提交状态（越序复活）",
         src[:al[1][1]] + " translations.json analysis_snapshot.json" + src[al[1][1]:], None),
        ("T3 .gitignore 少一行（状态以未跟踪形态回来）", src,
         gi.replace("\ntranslations.json\n", "\n", 1)),
        ("T4 缓存只保存不还原（Restore 与 Save 名单不对称）",
         src.replace(block(src, SAVE_MARK, PRUNE_MARK),
                     block(src, SAVE_MARK, PRUNE_MARK).replace(
                         "            daily_insight_history.json\n", "", 1), 1), None),
        ("T5 缓存 path 里去掉 build_logs（每日闸门失去累积承载）",
         src.replace(block(src, SAVE_MARK, PRUNE_MARK),
                     block(src, SAVE_MARK, PRUNE_MARK).replace(
                         "\n            build_logs", "", 1), 1), None),
        # ── 批 5a：三个"读回型"文件的通路（少了就是静默冻基线，构建照样绿）──
        ("C1 只在 Restore 名单去掉 descriptions_zh.json（保存但不还原）",
         src.replace("            descriptions_zh.json\n", "", 1), None),
        ("C2 .gitignore 少 trending_snapshot.json（可被 add 请回库里）", src,
         gi.replace("\ntrending_snapshot.json\n", "\n", 1)),
        ("C6 往缓存 path 里「顺手」加一个名字（整族旧缓存瞬间不可达=冷启动）",
         src.replace("            hot_snapshot.json\n",
                     "            hot_snapshot.json\n            source_quality.json\n", 2), None),
        ("C3 诊断循环漏掉 hot_snapshot.json（不在盘上无人知道）",
         src.replace("descriptions_zh.json trending_snapshot.json hot_snapshot.json; do",
                     "descriptions_zh.json trending_snapshot.json; do", 1), None),
    ]
    assert " hot_history.json" not in src.split("\n")[al[0][1] - 1:], "锚点自检失败"

    def run(mut_wf, mut_gi, label):
        if (mut_wf is None or mut_wf == src) and (mut_gi is None or mut_gi == gi):
            # 无效变异绝不能算"已挡住"：那样只要锚点写错，整个脚本就会自称 0 逃逸。
            return "%-52s INVALID（变异没落到任何字节上 = 这条判据没被证明过）" % label, False
        wfp = os.path.join(TMP, "_mut_update.yml")
        with open(wfp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(mut_wf)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", STARHUB_UPDATE_YML=wfp, PYTHONIOENCODING="utf-8")
        if mut_gi is not None:
            gip = os.path.join(TMP, "_mut_gitignore")
            with open(gip, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(mut_gi)
            env["STARHUB_GITIGNORE"] = gip
        p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                            "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        for pth in (os.path.join(TMP, "_mut_update.yml"), os.path.join(TMP, "_mut_gitignore")):
            if os.path.isfile(pth):
                os.remove(pth)
        lines = (p.stdout or "").strip().splitlines()
        tail = lines[-1] if lines else "?"
        failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
        red = p.returncode != 0
        return "%-52s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail,
                                  ("  <- " + ", ".join(failed[:2])) if failed else ""), red

    base = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    print("基线（未变异）：%s  rc=%d" % ((base.stdout.strip().splitlines() or ["?"])[-1],
                                        base.returncode))
    if base.returncode != 0:
        print(base.stdout[-2500:])
        return 1
    escapes = 0
    for label, mut_wf, mut_gi in MUTS:
        line, red = run(mut_wf, mut_gi, label)
        print(line)
        if not red:
            escapes += 1
    print("\n未被任何判据挡住的变异/无效变异：%d" % escapes)
    return 1 if escapes else 0


def save_blk_keyless(src):
    """把 Save 的 key 去掉 run_attempt —— 这个键形状会覆盖同一 run 内更早那次的缓存。

    锚点用"Save 之后的那一步"来定位：2026-10-02 把 Stage 挪到 Save **之前**（缺件落痕要赶在
    保存前进缓存）之后，Save 的下一步已经是 Prune —— 继续锚 Stage 会让本变异"一处也落不上"，
    于是 S3 从"被挡住"退化成 INVALID（而这正是最容易骗过人的那种假干净）。
    """
    return src.replace(
        "          key: starhub-state-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}\n\n"
        "      - name: Prune large build artifacts",
        "          key: starhub-state-${{ runner.os }}-${{ github.run_id }}\n\n"
        "      - name: Prune large build artifacts", 1)


if __name__ == "__main__":
    sys.exit(main())
