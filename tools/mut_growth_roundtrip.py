# -*- coding: utf-8 -*-
"""写↔读契约判据的变异电池（批 6 补漏）。

被改的两份工具都在 `tools/` 下，判据用 importlib 按真实路径加载 ⇒ 只能就地改本地副本。
账目形状沿用 mut_cold_start.py 修好的那套（§10.63）：
① 每轮 finally 立刻按字节还原并 sha256 校验；
② 每轮进场前断言"本轮不该碰的那份文件"仍是初始字节（脏了就拒绝自评、非零退出）；
③ 每条变异登记自己的靶判据，红了但靶判据不在 failed 里 ⇒ 记为未挡住（不许借别人的信用）。
"""
import hashlib
import os
import subprocess
import sys

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH = os.path.join(ROOT, "tools", "growth_history.py")
HGR = os.path.join(ROOT, "tools", "history_growth.py")
TESTS = [os.path.join("tests", "rss_history", "test_growth_roundtrip.py")]

MUTS = [
    ("G1 读端回到「凡 growth 都收」（缺陷原状）", GH,
     "            nb = rec.get(\"new_bytes\")\n"
     "            if isinstance(nb, int) and not isinstance(nb, bool):\n"
     "                out.append(rec)",
     "            out.append(rec)",
     "test_reader_does_not_silently_zero_out_a_bad_record"),
    ("G2 过滤过严：把合法的 0 读数也丢掉", GH,
     "            if isinstance(nb, int) and not isinstance(nb, bool):",
     "            if isinstance(nb, int) and not isinstance(nb, bool) and nb > 0:",
     "test_zero_growth_record_is_a_real_reading"),
    ("G3 写端键名改名（new_bytes -> bytes_added）", HGR,
     '"new_bytes": int(new_bytes)',
     '"bytes_added": int(new_bytes)',
     "test_written_record_is_readable_by_the_reader"),
    ("G4 写端不再写 new_blobs（读数只剩一半）", HGR,
     '"new_blobs": len(hits), ',
     "",
     "test_written_record_is_readable_by_the_reader"),
    ("G5 按天聚合截错长度（ts[:10] -> ts[:8]）", GH,
     'day = str(e.get("ts", ""))[:10]',
     'day = str(e.get("ts", ""))[:8]',
     "test_summarize_consumes_what_the_writer_emits"),
    # ②（Stage 缺件点名）往同一份 build_logs 里新增了 pages_missing 记录 ⇒ type 这一层过滤
    # 必须有判据守着：只看 new_bytes 是整数、不看 type 的读数器会把一条 build 记录当成增长。
    ("G6 只看字段不看 type（别的记录类型混进增长统计）", GH,
     'if isinstance(rec, dict) and rec.get("type") == "growth":',
     "if isinstance(rec, dict):",
     "test_other_record_types_are_not_growth"),
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


orig = {f: open(f, "rb").read() for f in (GH, HGR)}
h0 = {f: hashlib.sha256(v).hexdigest() for f, v in orig.items()}

base_red, base_tail, _ = run()
print("基线（未变异）：%s  rc_expected=green" % base_tail)
if base_red:
    print("基线不绿 ⇒ 不声称任何覆盖率")
    sys.exit(1)

escapes = 0
leaks = []
try:
    for label, path, a, b, target in MUTS:
        for other, want in h0.items():
            if other != path and sha(other) != want:
                leaks.append("%s 进场前 %s 已不是初始字节" % (label, os.path.basename(other)))
        src = orig[path].decode("utf-8")
        nl = "\r\n" if "\r\n" in src else "\n"
        aa = a.replace("\n", nl)
        bb = b.replace("\n", nl)
        if src.count(aa) != 1:
            print("%-52s INVALID（锚点命中 %d 次）" % (label, src.count(aa)))
            escapes += 1
            continue
        open(path, "w", encoding="utf-8", newline="").write(src.replace(aa, bb, 1))
        try:
            red, tail, failed = run()
        finally:
            open(path, "wb").write(orig[path])
            if sha(path) != h0[path]:
                leaks.append("%s 之后 %s 还原失败" % (label, os.path.basename(path)))
        print("%-52s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:40],
                                 ("  <- " + ", ".join(failed[:2])) if failed else ""))
        if not red:
            escapes += 1
        elif target not in failed:
            print("        ^ 靶判据 %s 不在 failed 里（%s）⇒ 记为未挡住" % (target, failed))
            escapes += 1
finally:
    for path, blob in orig.items():
        open(path, "wb").write(blob)

bad = [p for p in orig if sha(p) != h0[p]]
print("还原校验：%s" % ("两份工具逐字节回到初始值" if not bad else "失败 %s" % bad))
print("轮间泄漏/还原失败：%s" % (leaks if leaks else "无"))
print("未被挡住的变异/无效变异：%d" % escapes)
sys.exit(1 if escapes or bad or leaks else 0)
