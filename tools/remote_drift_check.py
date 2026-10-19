"""远端漂移核查（只读）：本地 HEAD vs origin/main 的逐文件 blob 差异。

用途：Data API 推送不回写本地、CI auto-commit 的重试分支会用 `git reset --soft`
把工作树差异整体提交，两条都会让"我以为推上去的东西"和"远端真实内容"悄悄分叉。
推前推后各跑一次；差异非空就是事故现场，不要直接覆盖，先看清方向（谁新谁旧）。

用法：
  py -3.11 tools/remote_drift_check.py            # 全仓比对
  py -3.11 tools/remote_drift_check.py --who       # 额外查每个差异路径远端最近一次改动者与作者
  py -3.11 tools/remote_drift_check.py path1 path2 # 只比指定路径
退出码：0=无差异，1=有差异，2=远端不可达
"""
import argparse
import json
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    # 仓库里大量路径含中文，GBK 控制台会直接 UnicodeEncodeError 让核查半途而废
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def sh(args):
    return subprocess.run(args, capture_output=True, text=True, errors="replace")


def remote_head():
    r = sh(["gh", "api", "repos/Kwei168/starhub/git/ref/heads/main", "--jq", ".object.sha"])
    return r.stdout.strip() if r.returncode == 0 else None


def remote_tree(sha):
    t = sh(["gh", "api", "repos/Kwei168/starhub/git/trees/%s?recursive=1" % sha])
    if t.returncode != 0:
        return None
    data = json.loads(t.stdout)
    return {e["path"]: e["sha"] for e in data.get("tree", []) if e.get("type") == "blob"}


def local_tree():
    # -z：NUL 分隔 + 原始 UTF-8 路径（否则 git 会把含非 ASCII 的路径转义成 "..." 引号形式，
    # 导致按前缀过滤/比对全部失效）
    raw = subprocess.check_output(["git", "ls-tree", "-r", "-z", "HEAD"])
    res = {}
    for entry in raw.decode("utf-8").split("\0"):
        if not entry.strip():
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            res[path] = parts[2]
    return res


def who_last(path):
    r = sh(["gh", "api",
            "repos/Kwei168/starhub/commits?path=%s&per_page=1" % path,
            "--jq", '.[0] | "\\(.sha[0:8]) \\(.commit.author.date) \\(.commit.author.name)"'])
    return r.stdout.strip() if r.returncode == 0 else "?"


IGNORE_PREFIXES = (".qoder/", ".deploy-tmp/", "node_modules/", ".git/")


def relevant(p):
    return not p.startswith(IGNORE_PREFIXES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--who", action="store_true")
    ap.add_argument("--all", action="store_true", help="不过滤 .qoder/.deploy-tmp 等本地专用路径")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    head = remote_head()
    if not head:
        print("远端不可达（gh api 失败），本次核查未完成")
        return 2
    rtree = remote_tree(head)
    if rtree is None:
        print("远端 tree 读取失败，本次核查未完成")
        return 2
    ltree = local_tree()

    scope = set(args.paths) if args.paths else set(ltree) | set(rtree)
    if not (args.paths or args.all):
        scope = {p for p in scope if relevant(p)}
    only_local = sorted(p for p in scope if p in ltree and p not in rtree)
    only_remote = sorted(p for p in scope if p in rtree and p not in ltree)
    differ = sorted(p for p in scope if p in ltree and p in rtree and ltree[p] != rtree[p])

    print("远端 main=%s  （已过滤 %s；--all 可关）" % (head[:10], ", ".join(IGNORE_PREFIXES)))
    print("仅本地有=%d  仅远端有=%d  内容不同=%d" % (len(only_local), len(only_remote), len(differ)))
    for label, items in (("仅本地(远端缺，可能被 auto-commit 删除)", only_local),
                         ("仅远端(本地缺，多为 CI 生成物或他端提交)", only_remote),
                         ("内容不同", differ)):
        for p in items[:15]:
            line = "  %s: %s" % (label, p)
            if label == "内容不同":
                line += "  local=%s remote=%s" % (ltree[p][:8], rtree[p][:8])
                if args.who:
                    line += "  远端最近: %s" % who_last(p)
            print(line)
        if len(items) > 15:
            print("    ...另有 %d 项" % (len(items) - 15))
    return 1 if (only_local or only_remote or differ) else 0


if __name__ == "__main__":
    sys.exit(main())
