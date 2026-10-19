# -*- coding: utf-8 -*-
"""批 6 冷启动判据的变异电池：破坏生产读取端的缺失分支，判据必须转红。

就地改的只有本地工作副本，但**每轮改完立刻逐字节还原并按 sha256 校验**：
曾经只在 finally 里还原，于是 K3 改的 build_rss_aggregator.py 在跑 K4（改
build_daily_insight.py）时还挂在树上，K4 的"2 failed"里有一条其实是 K3 的靶判据
⇒ 挡住 K4 的功劳有一部分是借来的，账目不可信。守卫放在轮内：任何一轮开始时若
"本不该动的那份文件"字节变了，直接判 INVALID 并中止，不声称覆盖率。
"""
import hashlib
import os
import subprocess
import sys

# 变异体改的是生产模块本体；pyc 按 (mtime, 大小) 命中，同长度改动可能沿用旧字节码 = 假绿
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A2 瘦身之后 bdi 的那两条判据搬到了 tests/daily_insight/（gate B）。
# 电池必须跟着改跑两个文件：否则 K4（改 build_daily_insight 的缺失分支）就没有目标判据，
# 只会把别条判据的串味失败误报成"已挡住"。
TESTS = [os.path.join("tests", "rss_history", "test_cold_start_readers.py"),
         os.path.join("tests", "daily_insight", "test_cold_start_history_reader.py")]
AGG = os.path.join(ROOT, "build_rss_aggregator.py")
INS = os.path.join(ROOT, "build_daily_insight.py")

MUTS = [
    ("K1 _load_prev_analysis 缺失时返回 {} 而不是 None", AGG,
     "    if not os.path.exists(ANALYSIS_SNAPSHOT_FILE):\n        return None",
     "    if not os.path.exists(ANALYSIS_SNAPSHOT_FILE):\n        return {}",
     "test_analysis_snapshot_missing_and_corrupt_return_none"),
    ("K2 _trans_cache 初始值改成 None（首场 `in` 直接抛）", AGG,
     '_trans_cache = {}  # {text_hash: translated_text}',
     "_trans_cache = None",
     "test_module_initial_cache_is_empty"),
    ("K3 _load_caches 缺失分支覆写全局（契约变成会清空）", AGG,
     "    if os.path.exists(TRANS_CACHE_FILE):",
     "    if True:\n        _trans_cache = {}\n    if os.path.exists(TRANS_CACHE_FILE):",
     "test_translation_cache_missing_is_a_noop_not_a_crash"),
    ("K4 _load_history 缺失时返回 None（30 天历史页会炸在下游）", INS,
     "    if not os.path.exists(HISTORY_FILE):\n        return {\"days\": []}",
     "    if not os.path.exists(HISTORY_FILE):\n        return None",
     "test_insight_history_missing_and_corrupt_degrade_to_empty_days"),
]


def run(label):
    p = subprocess.run([sys.executable, "-m", "pytest"] + TESTS + ["-q", "--no-header",
                        "-p", "no:cacheprovider"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    lines = (p.stdout or "").strip().splitlines()
    failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


orig = {f: open(f, "rb").read() for f in (AGG, INS)}
h0 = {f: hashlib.sha256(v).hexdigest() for f, v in orig.items()}


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


base_red, base_tail, _ = run("baseline")
print("基线（未变异）：%s  rc_expected=green" % base_tail)
if base_red:
    print("基线不绿 ⇒ 不声称任何覆盖率")
    sys.exit(1)
escapes = 0
leaks = []
try:
    for label, path, a, b, target in MUTS:
        # 轮内守卫：这一轮只许 path 带着变异进场，另一份文件必须还是初始字节。
        # 上一版的洞就在这儿——K3 改的 AGG 挂到 K4 那轮，K4 的"挡住了"有一半是借的。
        for other, want in h0.items():
            if other != path and sha(other) != want:
                leaks.append("%s 进场前 %s 已不是初始字节" % (label, os.path.basename(other)))
        src = orig[path].decode("utf-8")
        # 生产文件的行尾不统一（build_rss_aggregator.py 是 CRLF、build_daily_insight.py 是 LF），
        # 锚点必须按目标文件自己的行尾来拼，否则多行锚点一处也命中不了 = 静默 INVALID。
        nl = "\r\n" if "\r\n" in src else "\n"
        aa = a.replace("\n", nl)
        bb = b.replace("\n", nl)
        if src.count(aa) != 1:
            print("%-58s INVALID（锚点命中 %d 次）" % (label, src.count(aa)))
            escapes += 1
            continue
        open(path, "w", encoding="utf-8", newline="").write(src.replace(aa, bb, 1))
        try:
            red, tail, failed = run(label)
        finally:
            open(path, "wb").write(orig[path])
            if sha(path) != h0[path]:
                leaks.append("%s 之后 %s 还原失败" % (label, os.path.basename(path)))
        print("%-58s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:44],
                                 ("  <- " + ", ".join(failed[:2])) if failed else ""))
        if not red:
            escapes += 1
        elif target not in failed:
            # 红是红了，可挡它的不是这条变异的靶判据 ⇒ 功劳是别人借来的，按未挡住计
            print("        ^ 靶判据 %s 不在 failed 里（%s）⇒ 记为未挡住" % (target, failed))
            escapes += 1
finally:
    for path, blob in orig.items():
        open(path, "wb").write(blob)
bad = [p for p in orig if sha(p) != h0[p]]
print("还原校验：%s" % ("两份生产文件逐字节回到初始值" if not bad else "失败 %s" % bad))
print("轮间泄漏/还原失败：%s" % (leaks if leaks else "无"))
print("未被挡住的变异/无效变异：%d" % escapes)
sys.exit(1 if escapes or bad or leaks else 0)
