# -*- coding: utf-8 -*-
""""被忽略却已跟踪"判据的变异电池：探针坏掉、名单松掉，判据必须当场红。

账目形状沿用 §10.63：轮内还原 + sha256 校验 + 靶判据归属（红了但不是自己的靶判据 ⇒ 记为未挡住）。
被改的是判据文件本身（它就是被测物），改副本没有意义 —— 所以就地改、每轮立刻还原。
"""
import hashlib
import os
import subprocess
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "tests", "site_nav_drift", "test_ignored_not_tracked.py")
CTL = "test_probe_sees_an_ignored_tracked_file_in_both_directions"
MAIN = "test_no_new_ignored_file_is_tracked"

MUTS = [
    # 锚点跟着 LEGACY 走：2026-10-02 摘掉 _check_js_temp.js / _fix_quotes.py 之后，
    # 这两条原来锚在已删除的行上 ⇒ 电池当场报 INVALID（锚点命中 0 次），而不是悄悄少测两项。
    # 现在改锚到名单里仍然存在的 .vercel/project.json。
    ("L1 LEGACY 少一项（任何被忽略却回库的路径都被放开）",
     '    ".vercel/project.json",                             # 120 B：Vercel 项目链接，删前先确认 CLI 不需要\n',
     "", MAIN),
    ("L2 LEGACY 多一项不存在的路径（名单开始骗人）",
     '    ".vercel/project.json",                             # 120 B：Vercel 项目链接，删前先确认 CLI 不需要\n',
     '    ".vercel/project.json",                             # 120 B：Vercel 项目链接，删前先确认 CLI 不需要\n'
     '    "_never_was_here.js",\n', MAIN),
    ("L3 探针丢掉 -i：从此只看已跟踪，永远看不到违规",
     '["git", "ls-files", "-i", "-c", "--exclude-standard", "-z"]',
     '["git", "ls-files", "-c", "--exclude-standard", "-z"]', CTL),
    ("L4 探针恒返回空集（最典型的空转绿）",
     "    return {p for p in (r.stdout or \"\").split(\"\\0\") if p}",
     "    return set()", CTL),
]


def run():
    p = subprocess.run([sys.executable, "-m", "pytest", T, "-q", "--no-header",
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
    aa, bb = a.replace("\n", NL), b.replace("\n", NL)
    n = src.count(aa)
    if n != 1:
        print("%-50s INVALID（锚点命中 %d 次）" % (label, n))
        escapes += 1
        continue
    open(T, "w", encoding="utf-8", newline="").write(src.replace(aa, bb, 1))
    try:
        red, tail, failed = run()
    finally:
        open(T, "wb").write(orig)
        if sha(T) != h0:
            leaks.append("%s 之后还原失败" % label)
    print("%-50s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:30],
                             ("  <- " + ", ".join(failed[:2])) if failed else ""))
    if not red:
        escapes += 1
    elif target not in failed:
        print("        ^ 靶判据 %s 不在 failed 里（%s）⇒ 记为未挡住" % (target, failed))
        escapes += 1

print("还原校验：%s" % ("判据文件逐字节回到初始值" if sha(T) == h0 else "失败"))
print("轮间泄漏/还原失败：%s" % (leaks if leaks else "无"))
print("未被挡住的变异/无效变异：%d" % escapes)
sys.exit(1 if escapes or leaks or sha(T) != h0 else 0)
