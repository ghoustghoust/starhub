# -*- coding: utf-8 -*-
"""推送工具 ignore 闸的变异电池：把"闸"从六个可能的方向各自破一次，判据必须当场红。

账目形状沿用 §10.63：轮内还原 + sha256 校验 + 轮前守卫 + 靶判据归属（红了但不是自己的靶 ⇒ 记为未挡住）。
被改的是 `tools/data_api_push.py`（判据按真实路径加载它，且 CLI 级判据要跑真身），所以就地改、每轮还原。
"""
import hashlib
import os
import subprocess
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "tools", "data_api_push.py")
TEST = os.path.join("tests", "tools", "test_push_ignores_scratch.py")

MUTS = [
    ("P1 目录展开不再过滤忽略项（漏口原状）", "    drop = set(_ignored(out))", "    drop = set()",
     "test_directory_expansion_drops_ignored_paths"),
    ("P2 点名也不拦（静默上传）", "    return [p for p in paths if norm_path(p) in hit]", "    return []",
     "test_explicit_ignored_path_is_refused_not_uploaded"),
    ("P3 开关在展开层失效", "    if allow_ignored:\n        return out", "    if False:\n        return out",
     "test_opt_in_flag_is_the_only_way_through"),
    ("P4 反向过头：展开一律清空（推送通道报废）", "    return [p for p in out if p not in drop]", "    return []",
     "test_directory_expansion_drops_ignored_paths"),
    ("P5 出口层不认开关", "    blocked = [] if a.allow_ignored else refused_ignored(", "    blocked = refused_ignored(",
     "test_cli_refuses_an_ignored_path_and_honours_the_flag"),
    ("P6 丢掉 --no-index（正好放过已跟踪却被忽略那类）",
     '"git", "check-ignore", "--stdin", "--no-index", "-z"', '"git", "check-ignore", "--stdin", "-z"',
     "test_already_tracked_but_ignored_is_still_named"),
]


def run():
    p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    lines = (p.stdout or "").strip().splitlines()
    failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


orig = open(T, "rb").read()
h0 = sha(T)
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
    if sha(T) != h0:
        leaks.append("%s 进场前工具文件带着上一轮的改动" % label)
    aa, bb = a.replace("\n", NL), b.replace("\n", NL)
    n = src.count(aa)
    if n != 1:
        print("%-42s INVALID（锚点 %d 次）" % (label, n))
        escapes += 1
        continue
    open(T, "w", encoding="utf-8", newline="").write(src.replace(aa, bb, 1))
    try:
        red, tail, failed = run()
    finally:
        open(T, "wb").write(orig)
        if sha(T) != h0:
            leaks.append("%s 之后还原失败" % label)
    print("%-42s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:26],
                             ("  <- " + ", ".join(failed[:2])) if failed else ""))
    if not red:
        escapes += 1
    elif target not in failed:
        print("        ^ 靶判据 %s 不在 failed 里（%s）⇒ 记为未挡住" % (target, failed))
        escapes += 1

print("还原校验：%s" % ("工具文件逐字节回到初始值" if sha(T) == h0 else "失败"))
print("轮间泄漏/还原失败：%s" % (leaks if leaks else "无"))
print("未被挡住的变异/无效变异：%d" % escapes)
sys.exit(1 if escapes or leaks or sha(T) != h0 else 0)
