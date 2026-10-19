# tests/rss_composite/test_date_uniform.py
# -*- coding: utf-8 -*-
"""同日期伪造检测（bad_date 模式 C）单元测试。

验证：当同一源的所有条目 pub_date 完全相同时，该源被标记为 bad_date。
"""
import collections
import sys
import os
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.environ.get("RSS_BUILD_SRC", os.path.join(ROOT, "build_rss_aggregator.py"))

# 从 build_rss_aggregator.py 提取 _detect_uniform_dates 函数
src_text = open(SRC, encoding='utf-8').read()

# 提取函数体
import re
_func_match = re.search(
    r'(def _detect_uniform_dates\(.*?)(?=\ndef |\nclass |\Z)',
    src_text, re.DOTALL
)
if not _func_match:
    print("[FAIL] _detect_uniform_dates 函数未找到")
    sys.exit(1)

exec(_func_match.group(1), globals())

PASS = 0
FAIL = 0

def eq(got, want, msg):
    global PASS, FAIL
    if got == want:
        PASS += 1; print(f"  ok   {msg}")
    else:
        FAIL += 1; print(f"  FAIL {msg}\n    got  = {got!r}\n    want = {want!r}")

def ok(cond, msg):
    eq(bool(cond), True, msg)

# DU-1: 同日期源被标记
print("DU-1 同日期源（≥5 条）被标记为 bad_date")
items = [
    {"source_key": "sk_a", "pub_date": "2026-09-15T10:00:00+08:00", "link": f"l{i}"}
    for i in range(10)
]
result = _detect_uniform_dates(items, min_items=5)
ok("sk_a" in result, "DU-1a sk_a 被检出")

# DU-2: 不同日期的源不被标记
print("DU-2 不同日期的源不标记")
items2 = [
    {"source_key": "sk_b", "pub_date": f"2026-09-15T10:{i:02d}:00+08:00", "link": f"l{i}"}
    for i in range(10)
]
result2 = _detect_uniform_dates(items2, min_items=5)
ok("sk_b" not in result2, "DU-2a sk_b 不被标记")

# DU-3: 条目不足 min_items 不标记
print("DU-3 条目不足 min_items 不标记")
items3 = [
    {"source_key": "sk_c", "pub_date": "2026-09-15T10:00:00+08:00", "link": f"l{i}"}
    for i in range(3)
]
result3 = _detect_uniform_dates(items3, min_items=5)
ok("sk_c" not in result3, "DU-3a sk_c 条目不足不标记")

# DU-4: 混合源——只有同日期源被标记
print("DU-4 混合源只标记同日期源")
items4 = items + items2  # sk_a 同日期 + sk_b 不同日期
result4 = _detect_uniform_dates(items4, min_items=5)
ok("sk_a" in result4, "DU-4a sk_a 被标记")
ok("sk_b" not in result4, "DU-4b sk_b 不被标记")

# DU-5: 阈值边界——恰好 min_items 条同日期 → 标记
print("DU-5 阈值边界")
items5 = [
    {"source_key": "sk_d", "pub_date": "2026-09-15T10:00:00+08:00", "link": f"l{i}"}
    for i in range(5)
]
result5 = _detect_uniform_dates(items5, min_items=5)
ok("sk_d" in result5, "DU-5a 恰好 5 条同日期 → 标记")

# DU-6: 4 条同日期不标记（低于阈值）
items6 = items5[:4]
result6 = _detect_uniform_dates(items6, min_items=5)
ok("sk_d" not in result6, "DU-6 4 条同日期不标记")

# DU-7: 80% 同日期（非 100%）也应标记
print("DU-7 80% 同日期也标记")
items7 = [
    {"source_key": "sk_e", "pub_date": "2026-09-15T10:00:00+08:00", "link": f"l{i}"}
    for i in range(8)
] + [
    {"source_key": "sk_e", "pub_date": "2026-09-15T09:00:00+08:00", "link": "lx"}
]
result7 = _detect_uniform_dates(items7, min_items=5)
ok("sk_e" in result7, "DU-7 80% 同日期 → 标记")

print(f"\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
