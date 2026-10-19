# -*- coding: utf-8 -*-
"""Per-build git growth monitor (read-only).

Why this exists: the growth curve was flattened on 2026-10-01 by stopping per-build
commits of cross-build state (measured ladder: 19.83 -> 2.85 -> ~0.05 MiB per build).
The remaining risk is a silent regression back to "rewrite everything every build",
which is unrecoverable in git history. So this prints the number once per day in CI
and emits a warning above a threshold.

Measurement basis (do not change casually): recursive tree diff between two commits,
summing the blob size of every (path, sha) that is new or changed. Tip size cannot
reveal this -- a replaced blob stays in history forever.

It never mutates the repository: no writes, no history operations.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

_BJT = timezone(timedelta(hours=8))

# Once per day the log commit legitimately adds ~2.6 MiB (jsonl + summary), so a lower
# bound would fire daily and a warning that fires daily is a warning nobody reads.
# The regression shape we are actually watching for is 17-20 MiB per build.
DEFAULT_THRESHOLD = 6 * 1024 * 1024


def threshold():
    return DEFAULT_THRESHOLD


def verdict(new_bytes):
    return "warn" if new_bytes >= threshold() else "ok"


def parse_ls_tree(text):
    """`git ls-tree -rl` lines -> {path: (sha, size)}. Non-tree lines yield {}."""
    out = {}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        cols = meta.split()
        if len(cols) != 4 or cols[1] != "blob":
            continue
        size = 0 if cols[3] == "-" else int(cols[3])
        out[path] = (cols[2], size)
    return out


def new_blob_bytes(prev, cur):
    """Paths added or whose blob sha changed, with the NEW copy's byte size."""
    hits = []
    for path, (sha, size) in cur.items():
        if prev.get(path, (None,))[0] != sha:
            hits.append((path, size))
    return hits


def ls_tree(rev):
    r = subprocess.run(["git", "ls-tree", "-rl", rev], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    return parse_ls_tree(r.stdout)


_SELF = {
    "a.js": ("1", 100),
    "b.js": ("2", 200),
    "gone.js": ("3", 50),
}
_SELF2 = {"a.js": ("1", 100), "b.js": ("9", 300), "new.js": ("4", 70)}


def self_test():
    parsed = parse_ls_tree("100644 blob aaa1111 1234\thot_snapshot.json\n"
                           "100644 blob ddd4444 -\tempty.json\n")
    assert len(parsed) == 2 and parsed["empty.json"][1] == 0
    assert parse_ls_tree("not a tree line\n") == {}
    got = dict(new_blob_bytes(_SELF, _SELF2))
    assert sorted(got) == ["b.js", "new.js"], got
    assert got["b.js"] == 300
    assert new_blob_bytes(_SELF, _SELF) == []
    assert verdict(0) == "ok" and verdict(DEFAULT_THRESHOLD) != "ok"
    print("[self-test] ok (threshold=%d bytes)" % DEFAULT_THRESHOLD)
    return 0


def attribution(run_sha, head_sha):
    """这条读数算不算「本场的增长」。

    False = HEAD 就是本场检出的那个 sha ⇒ 构建没提交，测到的相邻提交差属于**别人**；
    True  = HEAD 变了 ⇒ 是本场（或本场期间）产生的提交；
    None  = 基线或 HEAD 拿不到（本地跑、浅取、git 读失败）⇒ 宁可不猜。

    为什么要有这个函数（2026-10-02 实测）：13:00 那场日志里同时出现
    `No changes, skip commit.` 与 `[growth] new blobs=2 0.037 MiB` —— 后者是我 12:52 推的批 9
    被记成了"这场构建长了 0.037 MiB"。这条曲线唯一要回答的是"有没有人把每场重写的文件加回提交清单"，
    把自己的推送混进分母，等于在监测里造噪声；反向情形（真提交没算进来）更危险。
    """
    if not run_sha or not head_sha:
        return None
    return run_sha.strip()[:40] != head_sha.strip()[:40]


def head_info():
    """HEAD 的 sha 与提交标题（读不到就返回空串，绝不让监测自己炸掉）。"""
    def _one(args):
        # 不写 cwd：本工具的 git 调用一律跟着 CWD 走（ls_tree 也是这样），凭空造一个 ROOT
        # 会让"在临时仓里跑工具"的既有用例行为改变。
        r = subprocess.run(["git"] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return r.stdout.strip() if r.returncode == 0 else ""
    return _one(["rev-parse", "HEAD"]), _one(["log", "-1", "--format=%s"])


def report(new_bytes, hits, top=6, attributed=None):
    """播报正文。判据直接喂字节数进这一层，验的是真会被读到的那段文本，不是 grep 源码。"""
    out = ["[growth] new blobs=%d  %.3f MiB  threshold=%.2f MiB  verdict=%s"
           % (len(hits), new_bytes / 1048576.0, threshold() / 1048576.0, verdict(new_bytes))]
    if attributed is False:
        # 数字照报，但当场说明它不算本场 —— 少了这行，读表的人会把别人的提交折进"每场增长"
        out.append("[growth] 归属：本场未提交（HEAD 仍是检出的那个 sha），这条相邻提交差不计入每场增长")
    for path, size in sorted(hits, key=lambda x: -x[1])[:top]:
        out.append("   %10d B  %s" % (size, path))
    if verdict(new_bytes) != "ok":
        out.append("::warning title=每场新增字节超阈值::本场给 git 历史永久新增 %.2f MiB（阈值 %.2f MiB）"
                   "—— 查一下是不是有人把每场重写的文件加回了提交清单"
                   % (new_bytes / 1048576.0, threshold() / 1048576.0))
    return "\n".join(out)


def log_line(new_bytes, hits, path, commit="", head="", head_subject="", attributed=None):
    """把读数作为 `growth` 事件追加进当日构建日志。

    Actions 的输出只活 90 天，而 `build_logs/<今天>.jsonl` 每天一次进 git —— 写在这里才回答得了
    "哪一场开始又长回去了"。附带行为：路径不可写时静默跳过，绝不能把监测本身弄红。
    `committed_by_run` 只在知道归属时写字段（不知道就缺席，不写默认值 —— 默认值会把"没测到"
    伪装成"测到了 False"）。
    """
    rec = {"ts": datetime.now(_BJT).isoformat(), "type": "growth",
           "new_blobs": len(hits), "new_bytes": int(new_bytes), "commit": (commit or "")[:40]}
    if head:
        rec["head"] = head[:40]
    if head_subject:
        rec["head_subject"] = head_subject[:80]
    if attributed is not None:
        rec["committed_by_run"] = bool(attributed)
    try:
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        print("[growth] 写日志失败（不影响测量本身）: %s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--cur", default="HEAD")
    ap.add_argument("--prev", default="HEAD^1")
    ap.add_argument("--top", type=int, default=6, help="print the N biggest new paths")
    ap.add_argument("--log", default="", help="把读数作为 growth 事件追加进这个 jsonl")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    cur, prev = ls_tree(a.cur), ls_tree(a.prev)
    if cur is None or prev is None:
        print("[growth] skipped: cannot read both trees (%s vs %s)" % (a.prev, a.cur))
        return 0
    hits = new_blob_bytes(prev, cur)
    total = sum(sz for _, sz in hits)
    base = os.environ.get("GITHUB_SHA", "")
    head_sha, head_subject = head_info()
    att = attribution(base, head_sha)
    print(report(total, hits, a.top, attributed=att), flush=True)
    if a.log:
        log_line(total, hits, a.log, base, head=head_sha, head_subject=head_subject, attributed=att)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        # 树里有 CJK 路径（.qoder/repowiki/...），Windows 默认 GBK 控制台会在 print 处崩，
        # 而这条是 advisory 步——崩了也等于给"没人看"的告警又添一条噪声。
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(os.environ.get("BUILD_WORKDIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    sys.exit(main())
