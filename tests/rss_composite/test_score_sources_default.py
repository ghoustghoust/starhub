# tests/rss_composite/test_score_sources_default.py
# -*- coding: utf-8 -*-
"""_score_sources() tier 默认分测试 — TDD

验证 72h 无数据源获得 tier 推断默认分而非 0。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from build_rss_aggregator import _score_sources

FAIL = 0
PASS = 0
failures = []


def eq(actual, expected, label):
    global FAIL, PASS
    if actual == expected:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        failures.append(label)
        print("  [FAIL] %s\n         期望 %r\n         实得 %r" % (label, expected, actual))


def ok(cond, label):
    eq(bool(cond), True, label)


print("=" * 60)
print("_score_sources tier 默认分测试")
print("=" * 60)

# 构造测试数据：3 个源，均无历史数据
sources = [
    {"key": "T1_SRC", "tier": 1},
    {"key": "T2_SRC", "tier": 2},
    {"key": "T3_SRC", "tier": 3},
]
rss_history = {}  # 空历史 → 所有源 n=0

result = _score_sources(sources, rss_history)

# SS-1: T1 源无历史数据 → score=60
eq(result["T1_SRC"]["score"], 60, "SS-1 T1 源无历史 → score=60")

# SS-2: T2 源无历史数据 → score=40
eq(result["T2_SRC"]["score"], 40, "SS-2 T2 源无历史 → score=40")

# SS-3: T3 源无历史数据 → score=25
eq(result["T3_SRC"]["score"], 25, "SS-3 T3 源无历史 → score=25")

# SS-4: 返回结果含 inferred=True 标记
ok(result["T1_SRC"].get("inferred") is True, "SS-4a T1 inferred=True")
ok(result["T2_SRC"].get("inferred") is True, "SS-4b T2 inferred=True")
ok(result["T3_SRC"].get("inferred") is True, "SS-4c T3 inferred=True")

# SS-5: 无 quality=0 的源
for sk, v in result.items():
    ok(v["score"] > 0, "SS-5 %s score>0（实得 %s）" % (sk, v["score"]))

# SS-6: 有历史数据的源走正常评分（无 inferred 标记）
sources_with_data = [{"key": "HAS_DATA", "tier": 1}]
history_with_data = {
    "item1": {
        "source_key": "HAS_DATA",
        "summary": "x" * 70,
        "full_content": "y" * 200,
        "pub_date": "2026-09-14",
        "bad_date": False,
    }
}
result2 = _score_sources(sources_with_data, history_with_data)
ok(result2["HAS_DATA"].get("inferred") is not True, "SS-6a 有数据源无 inferred 标记")
ok(result2["HAS_DATA"]["score"] > 0, "SS-6b 有数据源 score>0")

print()
print("=" * 60)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    for f in failures:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
