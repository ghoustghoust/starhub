# tests/rss_composite/test_composite_sort.py
# -*- coding: utf-8 -*-
"""_split_data_chunks() 复合排序适配测试

验收标准：
  - tags 字段穿透到 chunk0/chunk1
  - 排序键仍为 _chrono_key（时间降序），tags 不影响分块选片
  - 条目总数守恒
  - main() 中 _tag_articles() 必须早于 write_data_chunks()（否则产物无 tags）
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load_build

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


def ok(cond, label):
    eq(bool(cond), True, label)


def mk_src(items):
    return [{
        "key": "S1", "name": "测试源", "cat": "ai", "color": "#000", "tier": 2,
        "items": [{"title": t, "link": "https://x/" + t[:8], "pub_date": d, "tags": tags}
                  for t, d, tags in items],
    }]


print("=" * 74)
print("A. tags 字段穿透")
print("=" * 74)

src = mk_src([
    ("AI Article One", "2026-09-14T10:00:00+08:00", ["AI", "大模型"]),
    ("Tech News Two", "2026-09-14T09:00:00+08:00", ["芯片"]),
])
c0, c1 = B._split_data_chunks(src, chunk0_size=10)
item0 = c0[0]["items"][0]
eq(item0.get("tags"), ["AI", "大模型"], "A1 tags 穿透到 chunk0")

src2 = mk_src([
    ("A", "2026-09-14T10:00:00+08:00", ["X"]),
    ("B", "2026-09-14T09:00:00+08:00", ["Y"]),
    ("C", "2026-09-14T08:00:00+08:00", ["Z"]),
])
c0, c1 = B._split_data_chunks(src2, chunk0_size=1)
if c1 and c1[0].get("items"):
    eq(c1[0]["items"][0].get("tags"), ["Y"], "A2 tags 穿透到 chunk1")
else:
    eq(True, False, "A2 chunk1 应有条目")

print()
print("=" * 74)
print("B. 排序键不变（仍为时间降序）")
print("=" * 74)

src3 = mk_src([
    ("Old", "2026-09-14T08:00:00+08:00", ["A"]),
    ("New", "2026-09-14T12:00:00+08:00", ["B"]),
    ("Mid", "2026-09-14T10:00:00+08:00", ["C"]),
])
c0, _ = B._split_data_chunks(src3, chunk0_size=3)
titles = [it["title"] for s in c0 for it in s.get("items", [])]
eq(titles, ["New", "Mid", "Old"], "B1 chunk0 内部仍按时间降序")

print()
print("=" * 74)
print("C. 条目总数守恒 + 打标先于分块")
print("=" * 74)

src4 = mk_src([
    ("A", "2026-09-14T10:00:00+08:00", ["X"]),
    ("B", "", ["Y"]),
])
c0, c1 = B._split_data_chunks(src4, chunk0_size=1)
n0 = sum(len(s.get("items", [])) for s in c0)
n1 = sum(len(s.get("items", [])) for s in c1)
eq(n0 + n1, 2, "C1 条目总数守恒")

# C2 是真正的守卫：产物里有没有 tags，取决于 main() 的调用顺序
# 必须读 RSS_BUILD_SRC（变异测试指向副本），读真实源码会让本断言对变异免疫
_src_path = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
src_text = io.open(_src_path, encoding="utf-8").read()
mi = src_text.find("def main(")
ok(mi >= 0, "C2a 源码中存在 main()")
main_body = src_text[mi:] if mi >= 0 else ""
i_tag = main_body.find("_tag_articles(")
i_wdc = main_body.find("write_data_chunks(")
ok(i_tag >= 0, "C2b main() 调用了 _tag_articles()")
ok(i_wdc >= 0, "C2c main() 调用了 write_data_chunks()")
ok(i_tag >= 0 and i_wdc >= 0 and i_tag < i_wdc,
   "C2d _tag_articles() 必须先于 write_data_chunks()（tag=%d, chunk=%d）" % (i_tag, i_wdc))

print()
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    for f in failures:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
