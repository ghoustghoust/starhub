# -*- coding: utf-8 -*-
"""本轮"清 docs/.qoder"两处新判据的变异电池：每道闸各自破一次，判据必须当场红。

覆盖三处 URL 转义（`tools/data_api_push.py`）与三条探针（`tests/site_nav_drift/test_noncode_not_tracked.py`）。
被改的是真文件，所以沿用 §10.63 的账目形状：轮内还原 + sha256 对账 + 轮前守卫（另一份文件也必须还是初始字节）
+ 靶判据归属（红了但不是自己的靶 ⇒ 记为未挡住）。

另外因为这里就地改的正是**推送工具本体**，全程持有 `_scratch/.mutation-lock`：
`data_api_push.gate()` 见到锁就拒绝推送，防止某轮还原失败时把变异体推上线。
"""
import hashlib
import os
import subprocess
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "tools", "data_api_push.py")
S = os.path.join(ROOT, "tests", "site_nav_drift", "test_noncode_not_tracked.py")
LOCK = os.path.join(ROOT, "_scratch", ".mutation-lock")

GONE = '    for p in gone:\n        try:\n            got = req("GET", "%s/contents/%s?ref=%s" % (REPO, quote(p), ref))'
EXP = '    for p, want in expect.items():\n        got = req("GET", "%s/contents/%s?ref=%s" % (REPO, quote(p), ref))'

# (标签, 目标文件, 锚点, 变异体, 靶判据)
MUTS = [
    ("Q1 删除复查不转义（本轮唯一的删除证据链）", T, GONE, GONE.replace("quote(p)", "p"),
     "test_gone_recheck_sends_escaped_urls"),
    ("Q2 blob 比对不转义", T, EXP, EXP.replace("quote(p)", "p"),
     "test_expect_recheck_sends_escaped_urls"),
    ("Q3 存在性检查不转义（异常被 except 吞成假阴性）", T,
     '"%s/contents/%s" % (REPO, quote(f)), tries=2',
     '"%s/contents/%s" % (REPO, f), tries=2',
     "test_existence_check_quotes_too"),
    ("R1 404 立即上抛扩成「所有 HTTPError 都不重试」（关掉真重试）", T,
     "if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:",
     "if isinstance(exc, urllib.error.HTTPError):",
     "test_transient_5xx_still_retries"),
    ("R2 404 又回到重试循环（147 条复查白烧 110 分钟）", T,
     "if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:",
     "if False:",
     "test_404_is_raised_without_a_single_retry"),
    ("N1 探针丢掉 --no-index", S,
     '"git", "check-ignore", "--no-index", "-v", probe',
     '"git", "check-ignore", "-v", probe',
     "test_probe_bites_on_a_synthetic_repo"),
    ("N2 扫描器丢掉 -z（CJK 路径整族看不见）", S,
     '["git", "ls-files", "-z"]', '["git", "ls-files"]',
     "test_probe_bites_on_a_synthetic_repo"),
    ("N3 扫描器恒空（判据变成空集自证）", S,
     "    return [p for p in paths if p.startswith(prefix)]", "    return []",
     "test_no_tracked_file_under_noncode_prefixes"),
]

FILES = [T, S]

# 分段跑：Q 组只依赖 tests/tools/test_push_url_quoting.py（现在就能全绿），
# N 组依赖索引侧判据 —— 而那条在「远端删除 + 本地索引对齐」之前**本来就该红**，
# 混在一起会被基线守卫正当拦下。所以 argv[1] ∈ {quote, gates, all}。
GROUPS = {
    "push": ("QR", [os.path.join("tests", "tools", "test_push_url_quoting.py"),
                    os.path.join("tests", "tools", "test_push_retry_policy.py")]),
    "gates": ("N", [os.path.join("tests", "site_nav_drift", "test_noncode_not_tracked.py")]),
}


def run(tests):
    p = subprocess.run([sys.executable, "-m", "pytest"] + tests +
                       ["-q", "--no-header", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    lines = (p.stdout or "").strip().splitlines()
    failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def nl_of(text):
    return "\r\n" if "\r\n" in text else "\n"


orig = {f: open(f, "rb").read() for f in FILES}
h0 = {f: sha(f) for f in FILES}
src = {f: orig[f].decode("utf-8") for f in FILES}
NL = {f: nl_of(src[f]) for f in FILES}

def main():
    group = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if group not in GROUPS:
        print("用法：mut_untrack_gates.py [push|gates]")
        return 2
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write("mut_untrack_gates.py(%s) 正在就地改 tools/data_api_push.py，禁止推送" % group)
    try:
        return loop(group)
    finally:
        if os.path.isfile(LOCK):
            os.remove(LOCK)
        back = [os.path.basename(f) for f in FILES if sha(f) != h0[f]]
        print("收尾：锁已释放；未还原的文件：%s" % (back if back else "无"))


def loop(group):
    letters, tests = GROUPS[group]
    muts = [m for m in MUTS if m[0][0] in letters]
    red, tail, failed = run(tests)
    print("基线（未变异，%s 组 %d 个变异体）：%s  rc_expected=green" % (group, len(muts), tail))
    if red:
        print("基线不绿 ⇒ 不声称任何覆盖率；失败的有：%s" % failed)
        return 1
    escapes = 0
    leaks = []
    for label, path, a, b, target in muts:
        for other, want in h0.items():
            if sha(other) != want:
                leaks.append("%s 进场前 %s 已不是初始字节" % (label, os.path.basename(other)))
        aa, bb = a.replace("\n", NL[path]), b.replace("\n", NL[path])
        n = src[path].count(aa)
        if n != 1:
            print("%-46s INVALID（锚点命中 %d 次，不算挡住也不算漏）" % (label, n))
            escapes += 1
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(src[path].replace(aa, bb, 1))
        import py_compile
        try:
            py_compile.compile(path, doraise=True,
                               cfile=os.path.join(ROOT, ".deploy-tmp", "_pyc_check.pyc"))
        except Exception as exc:
            print("%-46s INVALID（变异体不是合法代码：%s）" % (label, str(exc)[:60]))
            with open(path, "wb") as f:
                f.write(orig[path])
            escapes += 1
            continue
        try:
            red, tail, failed = run(tests)
        finally:
            with open(path, "wb") as f:
                f.write(orig[path])
        print("%-46s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:24],
                                 ("  <- " + ", ".join(x for x in failed)[:70]) if failed else ""))
        if not red:
            escapes += 1
        elif target not in failed:
            print("        ^ 靶判据 %s 不在 failed 里（%s）⇒ 记为未挡住" % (target, failed))
            escapes += 1
        for other, want in h0.items():
            if sha(other) != want:
                leaks.append("%s 之后 %s 还原失败" % (label, os.path.basename(other)))
    print("未被挡住的变异/无效变异：%d" % escapes)
    print("轮间泄漏/还原失败：%s" % (leaks if leaks else "无"))
    return 1 if (escapes or leaks or any(sha(f) != h0[f] for f in FILES)) else 0


if __name__ == "__main__":
    sys.exit(main())
