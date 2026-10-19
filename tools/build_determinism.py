# -*- coding: utf-8 -*-
"""构建非确定性探针（每日洞察产物稳定性计划的 Task 0）。

要回答的只有一个问题：同一份输入、同一个"现在"、**两个独立进程**，产物是否逐字节相同。
为什么必须跨进程：CI 每场构建都是新进程，而 `set`/`dict` 对 str 键的迭代顺序随
PYTHONHASHSEED 变化。同进程跑两遍检不出这一类随机源，会让探针给出假绿结论。

分类口径（三类，不许混谈）：
  deterministic  纯函数路径，两次同输入必须逐字节相同，否则判红
  time-derived   输出里含构建时刻派生值；钉住 now 后必须相同
  external       依赖网络或模型调用，本探针不判，只列出来（不许拿它当"稳定"的证据）

用法：
  py -3.11 tools/build_determinism.py            # 父进程：起两个不同 hash seed 的 worker 并比对
  py -3.11 tools/build_determinism.py --mutate   # 变异体：让出厂顺序依赖 set 迭代序，必须判红
  py -3.11 tools/build_determinism.py --worker --out DIR [--seed-no N] [--mutate]

输出只用 ASCII，Windows 控制台是 cp936，生僻符号会把日志整行吞掉。
"""
import os
import sys
import hashlib
import shutil
import tempfile
import subprocess
import argparse
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BJT = datetime.timezone(datetime.timedelta(hours=8))
NOW_FIX = datetime.datetime(2026, 9, 30, 12, 0, 0, tzinfo=BJT)
KEEP = os.path.join(tempfile.gettempdir(), "starhub-determinism-keep")
# 两个刻意不同的 hash seed：只要产物顺序受 set/dict 迭代序影响，这两趟就会分叉
SEEDS = ("1", "982451653")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def first_diff(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        i = 0
        while True:
            ca = fa.read(1 << 20)
            cb = fb.read(1 << 20)
            if ca != cb:
                for k in range(min(len(ca), len(cb))):
                    if ca[k] != cb[k]:
                        return i + k
                return i + min(len(ca), len(cb))
            i += len(ca)
            if not ca:
                return -1


def compare_dirs(dir_a, dir_b):
    names = sorted(set(os.listdir(dir_a)) | set(os.listdir(dir_b)))
    rows = []
    for n in names:
        pa, pb = os.path.join(dir_a, n), os.path.join(dir_b, n)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            size = os.path.getsize(pb) if os.path.exists(pb) else 0
            rows.append((n, False, 0, size))
            continue
        same = sha256_file(pa) == sha256_file(pb)
        rows.append((n, same, -1 if same else first_diff(pa, pb), os.path.getsize(pb)))
    return rows


# ───────────────────── worker：真实出厂路径 ─────────────────────
def make_corpus(n=400):
    """合成语料：多源、多时刻，含无日期与同刻条目（撞排序平局）。

    不用随机数：探针自身必须确定，否则它没有资格审判别人。
    """
    items = []
    for i in range(n):
        if i % 37 == 0:
            pub = ""
        elif i % 11 == 0:
            pub = (NOW_FIX - datetime.timedelta(hours=3)).isoformat()   # 同刻平局
        else:
            pub = (NOW_FIX - datetime.timedelta(minutes=13 * i)).isoformat()
        items.append({
            "title": "t%04d" % i,
            "link": "https://example.com/a/%04d" % i,
            "pub_date": pub,
            "summary": "s" * (20 + i % 30),
            "summary_zh": "zh" * (i % 15),
            "title_zh": "z" * (i % 9),
            "full_content": "fc" * (30 + i % 50),
            "image": "https://img/%d.png" % (i % 7),
            "media_url": "", "media_type": "",
            "first_seen": (NOW_FIX - datetime.timedelta(hours=1)).isoformat(),
            "cat": "news", "color": "#123456",
            "source_key": "src_%d" % (i % 6), "source": "src%d" % (i % 6),
            "time_str": "3h",
        })
    return [{"key": "src_%d" % k, "name": "src%d" % k, "cat": "news",
             "color": "#123456", "tier": 2, "url": "https://example.com/feed%d" % k,
             "items": [it for it in items if it["source_key"] == "src_%d" % k]}
            for k in range(6)]


def apply_mutate(B):
    """变异体：让块内条目顺序经由 set 迭代得到（str 迭代序随 PYTHONHASHSEED 变）。

    这一版模拟的是"忘了补 tie-breaker 的 set 去重"，探针必须因此判红；
    判不红就是探针恒真，不许拿它的绿去支撑任何结论。
    """
    def split_via_set(sources, chunk0_size=360):
        flat = [it for s in sources for it in s.get("items", [])]
        # 必须是 set：dict 保序、不受 hash seed 影响，用它做变异体等于自证无效
        order = list({it["link"] for it in flat})                # 迭代序随 PYTHONHASHSEED 变
        by = {it["link"]: it for it in flat}
        picked = order[:chunk0_size]
        rest = order[chunk0_size:]
        chunk0, chunk1 = [], []
        for s in sources:
            c0 = [by[l] for l in picked if l in {x["link"] for x in s.get("items", [])}]
            c1 = [by[l] for l in rest if l in {x["link"] for x in s.get("items", [])}]
            if c0:
                chunk0.append(dict(s, items=c0))
            if c1:
                chunk1.append(dict(s, items=c1))
        return chunk0, chunk1
    B._split_data_chunks = split_via_set


def run_worker(out_dir, mutate):
    sys.path.insert(0, os.path.join(ROOT, "tests", "rss_history"))
    from _loader import load_build
    B = load_build()
    if mutate:
        apply_mutate(B)
    os.makedirs(out_dir, exist_ok=True)
    saved = B.OUT
    try:
        B.OUT = os.path.join(out_dir, "rss-aggregator.html")
        B.write_data_chunks(make_corpus())
    finally:
        B.OUT = saved
    return out_dir


# ───────────────────── parent：起两进程并比对 ─────────────────────
def run_parent(mutate):
    if os.path.isdir(KEEP):
        shutil.rmtree(KEEP)
    os.makedirs(KEEP, exist_ok=True)
    me = os.path.abspath(__file__)
    sides = []
    for tag, seed in zip(("a", "b"), SEEDS):
        d = os.path.join(KEEP, tag)
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        cmd = [sys.executable, me, "--worker", "--out", d]
        if mutate:
            cmd.append("--mutate")
        p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            sys.stdout.write(p.stdout or "")
            sys.stderr.write((p.stderr or "")[-800:])
            print("worker %s (PYTHONHASHSEED=%s) exit=%d" % (tag, seed, p.returncode))
            return 3
        sides.append(d)
    rows = compare_dirs(sides[0], sides[1])
    bad = [r for r in rows if not r[1]]
    print("PYTHONHASHSEED A=%s  B=%s ; %d file(s) compared" % (SEEDS[0], SEEDS[1], len(rows)))
    for name, same, off, size in rows:
        print("  %-22s same=%-5s bytes=%-9d first_diff=%s"
              % (name, "yes" if same else "NO", size, "n/a" if same else off))
    print()
    if bad:
        print("VERDICT: NON-DETERMINISTIC -> %d file(s) differ: identical input, same pinned clock,"
              " only the hash seed differs" % len(bad))
        print("  这类抖动机理与'每场内容稳定即不新增对象'互斥，必须先修")
    else:
        print("VERDICT: DETERMINISTIC for the chunk path (cross-process, hash seed varied)")
    print("  两侧产物: %s" % KEEP)
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        run_worker(args.out, args.mutate)
        return 0
    return run_parent(args.mutate)


if __name__ == "__main__":
    sys.exit(main())
