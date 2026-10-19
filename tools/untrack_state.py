# -*- coding: utf-8 -*-
"""批 3 的本地索引对齐：把已退出远端 git 树的 7 个跨场状态从**本地索引**里摘掉。

为什么需要：本仓的远端写入走 `tools/data_api_push.py`（Git Data API），本地 `.git` 从不随之前进，
所以远端已删除的路径在本地仍是"被跟踪"。这让 `test_state_files_are_not_tracked` 在本地恒红，
而一条恒红的判据会**污染整个变异电池**（任何变异都"看起来被挡住了"）——
`mut_state_cache.py` 的基线守卫正是为此而存在：基线不绿就不许声称覆盖率。

只动索引、不动磁盘（`--cached`），且第一步就落一个可逆命令文件，撤销=执行它：

    python tools/untrack_state.py --restore

用法：
    python tools/untrack_state.py            # 列出将要摘的路径并执行，写回滚命令
    python tools/untrack_state.py --dry-run  # 只看不动
    python tools/untrack_state.py --restore  # 用 git restore --staged 把索引还原
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILES = (
    "hot_history.json",
    "analysis_snapshot.json",
    "translations.json",
    "rss_trend_history.json",
    "insight_tracking_history.jsonl",
    "daily_insight_tracking_history.jsonl",
    "daily_insight_history.json",
    # 批 6（2026-10-02 树差点名）：每场唯一重写的那个 blob，28.5~37.9 KB/场
    "daily-insight.json",
)
ROLLBACK = os.path.join(ROOT, ".deploy-tmp", "restore_state_index.sh")
ROLLBACK_ALIGN = os.path.join(ROOT, ".deploy-tmp", "restore_index_align.sh")

# 批 6 之后摘除的一次性 Scratch 件（实测 411,404 B + 1,039 B，无任何引用、不进 Pages）。
# 列在这里是为了让 --align-remote 认得它们：远端删完而本地索引还留着的话，
# tests/site_nav_drift/test_ignored_not_tracked.py 会在本地恒红，而恒红的判据会污染变异电池。
RETIRED_SCRATCH = ("_check_js_temp.js", "_fix_quotes.py")


def git(*args):
    # encoding 必须显式 utf-8：本机默认 locale 是 GBK，git 输出里的 UTF-8 中文会抛
    # UnicodeDecodeError（或被替换成乱码），而乱码路径永远匹配不到远端条目。
    return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def tracked():
    r = git("ls-files", "--", *STATE_FILES)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def remote_paths():
    """远端 main 的递归树路径集合（走 gh api，本仓的远端真相只有这里能查）。"""
    import json
    import subprocess as sp
    head = sp.run(["gh", "api", "repos/Kwei168/starhub/commits?per_page=1",
                   "-H", "Accept: application/vnd.github+json"],
                  cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if head.returncode:
        raise SystemExit("gh 取 HEAD 失败：" + (head.stderr or "")[:200])
    sha = json.loads(head.stdout)[0]["sha"]
    tree = sp.run(["gh", "api", "repos/Kwei168/starhub/git/trees/%s?recursive=1" % sha,
                   "-H", "Accept: application/vnd.github+json"],
                  cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if tree.returncode:
        raise SystemExit("gh 取 tree 失败：" + (tree.stderr or "")[:200])
    doc = json.loads(tree.stdout)
    if doc.get("truncated"):
        raise SystemExit("远端 tree 被截断：路径集合不完整，绝不能据此对齐索引")
    return sha, {e["path"] for e in doc["tree"]}


def do_align(dry=False):
    """把"远端已删除、本地索引还在跟踪"的路径摘掉 —— 这是本地假红的唯一根源。

    为什么需要：本仓远端写入走 Data API，本地 `.git` 从不随之前进。一条恒红的判据会污染
    整个变异电池（任何变异都"看起来被挡住"），所以电池都带"基线不绿就拒绝自评"的守卫；
    守卫生效的代价就是每次都得先把索引对齐。
    只动索引、不动磁盘，且先在 HEAD 里确认该路径可取回（撤销 = `git restore --staged`）。
    """
    sha, rem = remote_paths()
    local = git("ls-files", "-z")
    assert local.returncode == 0, "git ls-files 失败：探针本身不可信，不对齐"
    # -z 不能省：不带它，非 ASCII 路径会被 git 输出成 "\344\270\255…" 的转义+引号形式，
    # 于是 .qoder/repowiki 那 91 个中文名永远匹配不到远端条目 —— 对齐会静默少掉一整族。
    loc = [p for p in (local.stdout or "").split("\0") if p]
    gone = [p for p in loc if p not in rem]
    # 白名单：只对齐这批存储工作**主动退役**的路径。本地 HEAD 还带着另一条工作线（AI 日报）的提交，
    # 他们的文件天然"本地跟踪 / 远端还没有"，全量对齐会把别人的在制品从索引里摘掉。
    allowed = set(STATE_FILES) | set(RETIRED_SCRATCH) | {"build_logs"} | {
        ".github/workflows/build-log-summary.yml",
        "rss-data-0.js", "hot_snapshot.json", "trending_snapshot.json", "descriptions_zh.json",
        "tests/rss_history/test_state_seed.py", "tools/mut_state_seed.py",
        "build_logs/.state_seed.done",
    }
    # docs/ 与 .qoder/：2026-10-02 用户裁定"跟代码无关的不进 git"，整目录退出跟踪。
    # 远端删完后本地索引还留着 147 条，不对齐的话防回潮判据在本地恒红（恒红的判据会污染变异电池）。
    offenders = [p for p in gone if p in allowed or p.startswith("build_logs/")
                 or p.startswith("rss-data-") or p.startswith(".qoder/")
                 or p.startswith("docs/")]
    skipped = [p for p in gone if p not in offenders]
    print("远端 %s：本地跟踪 %d 个；远端已不存在 %d 个，其中本工具允许对齐 %d 个"
          % (sha[:10], len(loc), len(gone), len(offenders)))
    if skipped:
        print("  不动（可能是另一条工作线的在制品）：%s" % ", ".join(skipped[:12]))
    for p in offenders:
        print("   -", p)
    if not offenders:
        return 0
    if dry:
        print("dry-run：不写索引")
        return 0
    # 可逆性前置检查：HEAD 里必须还留着这些条目，否则 `git restore --staged`（它的来源就是 HEAD）救不回来。
    # 同样要 -z：ls-tree 不带 -z 时，中文路径会被转义引号包起来，那样 91 个 CJK 条目会全体
    # 被误判成「无法还原」而跳过 —— 表现为「对齐了一半」，比不对齐全更糟。
    head_paths = set(p for p in git("ls-tree", "-r", "-z", "--name-only", "HEAD").stdout.split("\0") if p)
    unrestoreable = [p for p in offenders if p not in head_paths]
    if unrestoreable:
        print("[NG] 这些路径无法从索引快照还原，跳过不动：%s" % unrestoreable)
    offenders = [p for p in offenders if p in head_paths]
    # 回滚文件必须给每个路径加引号：这批 147 条里有含空格的目录名（`StarHub 收藏台与 …`），
    # 不加引号的话它是一份"看着能跑、实际会拆成几十个参数"的假可逆。
    quoted = " ".join('"%s"' % p for p in offenders)
    with open(ROLLBACK_ALIGN, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\n# 撤销 tools/untrack_state.py --align-remote（把 %d 个路径放回索引）\n"
                  "git restore --staged -- %s\n" % (len(offenders), quoted))
    print("回滚命令已写入：%s" % ROLLBACK_ALIGN)
    r = git("rm", "--cached", "--quiet", "--", *offenders)
    if r.returncode != 0:
        print("[NG] git rm --cached 失败：%s" % (r.stderr or r.stdout).strip()[:200])
        return 1
    still = [p for p in offenders if p in set(x for x in git("ls-files", "-z").stdout.split("\0") if x)]
    print("对齐后仍被跟踪：%s" % (still or "无"))
    return 0 if not still else 1


def do_restore():
    if not os.path.isfile(ROLLBACK):
        print("没有回滚文件 %s（本地索引此刻是干净的或从未对齐过）" % ROLLBACK)
        return 1
    names = [l for l in open(ROLLBACK, encoding="utf-8").read().splitlines()
             if l.startswith("git restore")][0].split()[3:]
    r = git("restore", "--staged", "--", *names)
    print((r.stdout.strip() + " " + r.stderr.strip()).strip())
    print("还原后本地仍跟踪：%d 个" % len(tracked()))
    return 0 if r.returncode == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--align-remote", action="store_true",
                    help="按远端 main 的递归树对齐：远端已删除但本地索引仍跟踪的路径一律摘掉（只动索引）")
    a = ap.parse_args()
    if a.restore:
        return do_restore()
    if a.align_remote:
        return do_align(dry=a.dry_run)

    before = tracked()
    print("本地索引里被跟踪的状态文件：%d / %d" % (len(before), len(STATE_FILES)))
    if not before:
        print("已经是干净的（远端已删除，本地索引也已对齐）")
        return 0
    if a.dry_run:
        print("dry-run：将执行 git rm --cached -- %s" % " ".join(before))
        return 0

    os.makedirs(os.path.dirname(ROLLBACK), exist_ok=True)
    with open(ROLLBACK, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("#!/bin/sh\n# 撤销 tools/untrack_state.py 的索引对齐（把 %d 个路径放回索引）\n"
                  "git restore --staged %s\n" % (len(before), " ".join(before)))
    print("回滚命令已写入：%s" % ROLLBACK)

    for p in before:
        r = git("rm", "--cached", "--quiet", "--", p)
        if r.returncode != 0:
            print("  [NG] %s：%s" % (p, (r.stderr or r.stdout).strip()[:160]))
            return 1
        print("  [OK] 从索引摘除 %s（文件仍在磁盘上）" % p)
    print("对齐后本地仍跟踪：%d 个" % len(tracked()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
