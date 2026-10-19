# -*- coding: utf-8 -*-
"""构建期切块排序口径 · 单元测试

背景（2026-09-14 实测）：
    `_split_data_chunks()` 决定哪 360 篇进首屏块（chunk0）。它原本按 pub_date **字符串**
    降序，而数据里同时存在 "+08:00" 与 "+00:00/Z" 两种写法 → 字符串比较是时区盲的。
    实测后果：同一时刻，"…T09:00:00+08:00"（=01:00Z）会被判为比 "…T02:00:00+00:00"（=02:00Z）
    更新，**首屏选片选错**。

口径要求（与前端 _dateCmpDesc 保持一致）：
    1. 按解析后的绝对时间降序；
    2. 无日期/无法解析的条目沉底（进 chunk1），且不得因排序抛异常。

用法：python tests/rss_date/test_split_chunks.py    退出码 0=全绿 1=有失败
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from _loader import load_build  # noqa: E402

B = load_build()

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


def mk(items):
    """构造单源结构；items 为 (标题, pub_date) 列表"""
    return [{
        "key": "S1", "name": "源", "cat": "news", "color": "#000", "tier": 2,
        "items": [{"title": t, "link": "https://x/" + t, "pub_date": d} for t, d in items],
    }]


def chunk0_titles(items, size):
    c0, _c1 = B._split_data_chunks(mk(items), chunk0_size=size)
    out = []
    for s in c0:
        for it in s.get("items", []):
            out.append(it["title"])
    return out


print("=" * 74)
print("A. 时区口径：字符串序与绝对时间序必须一致（不一致即选片错误）")
print("=" * 74)

# A1 混合时区：+08:00 的 09:00 = 01:00Z，比 +00:00 的 02:00Z 更旧 → 首屏必须选 UTC 那条
eq(chunk0_titles([("plus8", "2026-09-14T09:00:00+08:00"),
                  ("utc", "2026-09-14T02:00:00+00:00")], 1),
   ["utc"], "A1 混合时区按绝对时间选片（字符串序会错选 plus8）")

# A2 同款镜像：带 A 后缀 vs 数字偏移，结果必须一致
eq(chunk0_titles([("z", "2026-09-14T02:00:00Z"),
                  ("p8", "2026-09-14T09:00:00+08:00")], 1),
   ["z"], "A2 Z 与 +08:00 混排")

# A3 纯 UTC 内部仍按时间降序
eq(chunk0_titles([("a", "2026-09-14T01:00:00+00:00"),
                  ("c", "2026-09-14T03:00:00+00:00"),
                  ("b", "2026-09-14T02:00:00+00:00")], 3),
   ["c", "b", "a"], "A3 同区内部降序")

print()
print("=" * 74)
print("B. 无日期条目：切块时必须沉底（进 chunk1），不得占据首屏块")
print("=" * 74)
eq(chunk0_titles([("nodate", ""), ("dated", "2026-09-14T02:00:00+00:00")], 1),
   ["dated"], "B1 空串日期沉底")
eq(chunk0_titles([("nodate", None), ("dated", "2026-09-14T02:00:00+00:00")], 1),
   ["dated"], "B2 None 日期沉底")

print()
print("=" * 74)
print("C. 边界：不得抛异常、不得丢条目")
print("=" * 74)
eq(chunk0_titles([("bad", "不是日期"), ("dated", "2026-09-14T02:00:00+00:00")], 1),
   ["dated"], "C1 非法日期串沉底且不抛异常")

c0, c1 = B._split_data_chunks(mk([("x", "2026-09-14T02:00:00+00:00"), ("y", "")]), chunk0_size=1)
n0 = sum(len(s.get("items", [])) for s in c0)
n1 = sum(len(s.get("items", [])) for s in c1)
eq((n0, n1), (1, 1), "C2 切块后条目总数守恒（1+1）")

c0e, c1e = B._split_data_chunks([], chunk0_size=10)
eq((c0e, c1e), ([], []), "C3 空输入返回两个空列表")

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    print("失败用例：")
    for f in failures:
        print("  - %s" % f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
