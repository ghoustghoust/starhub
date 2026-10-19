# -*- coding: utf-8 -*-
"""回读端诚实性判据的变异电池：把三种「空」重新混回一种，判据必须当场红。

账目形状沿用 §10.63：轮内还原 + sha256 校验 + 进场前守卫 + 靶判据归属。
只就地改本地工作副本（判据用 importlib 按真实路径加载 tools/ 下的文件，没有 env 开关）。
"""
import hashlib
import os
import subprocess
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH = os.path.join(ROOT, "tools", "growth_history.py")
TESTS = [os.path.join("tests", "rss_history", "test_growth_readback_honesty.py")]

MUTS = [
    ("H1 本地读到 0 条就不复核远端（浅副本的假绿原状）",
     "        if local:\n            return local, \"本地\"",
     "        if True:\n            return local, \"本地\"",
     "test_stale_local_copy_falls_through_to_the_remote"),
    ("H2 不看本地、每天都联网（把优化反过来）",
     "    if local_dir and os.path.exists(p):",
     "    if False:",
     "test_fresh_local_copy_does_not_hit_the_network"),
    ("H3 取数失败被吞成空串（旧 fetch_remote 的形状）",
     "    except Exception as e:\n        return [], \"取数失败（%s）⇒ 读数缺失，不代表没长\" % type(e).__name__",
     "    except Exception:\n        return [], \"无该日日志\"",
     "test_unreachable_remote_is_reported_as_unknown_not_as_flat"),
    ("H4 404 也算取数失败（把没日志说成故障）",
     "        if e.code == 404:\n            return \"\"",
     "        if False:\n            return \"\"",
     "test_http_404_is_an_absent_day_not_a_failure"),
    ("H5 CLI 不再播报失败天（只在函数层绿、出口糊）",
     "    if failed:\n        print(",
     "    if False:\n        print(",
     "test_unreachable_remote_is_reported_as_unknown_not_as_flat"),
]


def run():
    p = subprocess.run([sys.executable, "-m", "pytest"] + TESTS + ["-q", "--no-header",
                        "-p", "no:cacheprovider"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    lines = (p.stdout or "").strip().splitlines()
    failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


orig = open(GH, "rb").read()
h0 = sha(GH)
src = orig.decode("utf-8")
NL = "\r\n" if "\r\n" in src else "\n"

red, tail, failed = run()
print("基线（未变异）：%s  rc_expected=green" % tail)
if red:
    print("基线不绿 ⇒ 不声称任何覆盖率；失败的有：%s" % failed)
    sys.exit(1)

escapes = 0
leaks = []
for label, a, b, target in MUTS:
    if sha(GH) != h0:
        leaks.append("%s 进场前工具文件已被上一轮留下改动" % label)
    aa, bb = a.replace("\n", NL), b.replace("\n", NL)
    n = src.count(aa)
    if n != 1:
        print("%-46s INVALID（锚点命中 %d 次）" % (label, n))
        escapes += 1
        continue
    open(GH, "w", encoding="utf-8", newline="").write(src.replace(aa, bb, 1))
    try:
        red, tail, failed = run()
    finally:
        open(GH, "wb").write(orig)
        if sha(GH) != h0:
            leaks.append("%s 之后还原失败" % label)
    print("%-46s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:34],
                             ("  <- " + ", ".join(failed[:2])) if failed else ""))
    if not red:
        escapes += 1
    elif target not in failed:
        print("        ^ 靶判据 %s 不在 failed 里（%s）⇒ 记为未挡住" % (target, failed))
        escapes += 1

print("还原校验：%s" % ("工具文件逐字节回到初始值" if sha(GH) == h0 else "失败"))
print("轮间泄漏/还原失败：%s" % (leaks if leaks else "无"))
print("未被挡住的变异/无效变异：%d" % escapes)
sys.exit(1 if escapes or leaks or sha(GH) != h0 else 0)
