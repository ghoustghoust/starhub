# -*- coding: utf-8 -*-
"""日期解析与降级排序键 · 单元测试

背景（2026-09-14 实测）：
    12 个源的 201 条条目在输出里 pub_date 为空，其中 75 条（小胡子哥 30 / 卢昌海 29 /
    tinyprojects 16）的**原始 feed 里明确带 pubDate**，只是 _parse_rss_date 的格式覆盖
    不足而解析失败 → 这些条目被判为「无日期」→ 在默认排序里沉底（旧实现是置顶）。

本测试锁死两件事：
    A. 解析器必须覆盖真实世界里出现过的日期写法（含回归：原本能解析的不能坏）。
    B. 确实取不到日期时，必须能用 first_seen 生成**可排序的降级键**（带标记），
       而不是留下空值让条目沉底不可见。

用法：python tests/rss_date/test_parse_rss_date.py    退出码 0=全绿 1=有失败
"""
import datetime
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


def iso(dt):
    return dt.isoformat() if dt else None


print("=" * 74)
print("A. _parse_rss_date 格式覆盖（源侧真实值 → 必须解析成功）")
print("=" * 74)
P = B._parse_rss_date

# A1 JS Date.toString() 形态（小胡子哥 rss2.xml 的真实 pubDate）
eq(iso(P("Fri Oct 27 2023 15:13:00 GMT+0800 (China Standard Time)")),
   "2023-10-27T15:13:00+08:00", "A1 JS Date.toString() + GMT+0800 + 括号时区名")

# A2 无星期前缀 + 字母时区（卢昌海 feed.xml 的真实 pubDate）
eq(iso(P("13 Sep 2026 07:28:00 EST")),
   "2026-09-13T07:28:00-05:00", "A2 无星期前缀 + 字母时区 EST")

# A3 只有日期、无时间（tinyprojects feed.xml 的真实 pubDate）
eq(iso(P("Mon, 18 May 2020")),
   "2020-05-18T00:00:00+00:00", "A3 仅日期无时间")

# A4 数字时区两种写法
eq(iso(P("Tue, 01 Sep 2026 08:30:00 +0800")),
   "2026-09-01T08:30:00+08:00", "A4a 数字时区 +0800")
eq(iso(P("Tue, 01 Sep 2026 08:30:00 +08:00")),
   "2026-09-01T08:30:00+08:00", "A4b 数字时区 +08:00")

# A5 秒可省略
eq(iso(P("Tue, 01 Sep 2026 08:30 +0800")),
   "2026-09-01T08:30:00+08:00", "A5 时间省略秒")

# A6 其他字母时区
eq(iso(P("Tue, 01 Sep 2026 08:30:00 PST")),
   "2026-09-01T08:30:00-08:00", "A6a PST")
eq(iso(P("Tue, 01 Sep 2026 08:30:00 UTC")),
   "2026-09-01T08:30:00+00:00", "A6b UTC")
eq(iso(P("Fri Oct 27 2023 15:13:00 GMT+0800 (CST)")),
   "2023-10-27T15:13:00+08:00", "A6c GMT+0800 且括号里是 CST（不得按 -0600 误判）")

print()
print("=" * 74)
print("B. 回归：原本能解析的形态不能被改坏")
print("=" * 74)
eq(iso(P("Mon, 14 Sep 2026 01:18:00 GMT")),
   "2026-09-14T01:18:00+00:00", "B1 人民网 GMT")
eq(iso(P("Sat, 12 Sep 2026 16:00:00 GMT")),
   "2026-09-12T16:00:00+00:00", "B2 财富中文网 GMT")
eq(iso(P("2026-09-13T14:19:04+00:00")),
   "2026-09-13T14:19:04+00:00", "B3 ISO 8601 走 _parse_iso")
eq(iso(P("2026-09-13T14:19:04Z")),
   "2026-09-13T14:19:04+00:00", "B4 ISO 带 Z 后缀")

print()
print("=" * 74)
print("C. 非法输入不得抛异常，且必须返回 None")
print("=" * 74)
eq(P(""), None, "C1 空串")
eq(P(None), None, "C2 None")
eq(P("   "), None, "C3 全空白")
eq(P("not a date at all"), None, "C4 垃圾串")
eq(P("Mon, 31 Feb 2026 00:00:00 GMT"), None, "C5 不存在的日期 2 月 31 日")
eq(P("Fri, 99 Sep 2026 00:00:00 GMT"), None, "C6 不存在的日 99")

print()
print("=" * 74)
print("D. 降级排序键 _fallback_date_iso（取不到日期时不得留空 → 不允许沉底）")
print("=" * 74)
F = B._fallback_date_iso

# D1 无 pub_date + first_seen 裸北京时间 → 必须转成带 +08:00 的可排序串
eq(F({"pub_date": "", "first_seen": "2026-09-13T14:19:04"}),
   "2026-09-13T14:19:04+08:00", "D1 first_seen 裸值 → +08:00")

# D2 有 pub_date 时不得覆盖
eq(F({"pub_date": "2026-09-13T14:19:04+00:00", "first_seen": "2026-09-13T14:19:04"}),
   "", "D2 有 pub_date → 不生成降级键")

# D3 连 first_seen 都没有 → 空（此时才允许沉底，属极端兜底）
eq(F({"pub_date": "", "first_seen": ""}), "", "D3 无 first_seen → 返回空")
eq(F({"pub_date": "", "first_seen": "不是时间"}), "", "D4 first_seen 非法 → 返回空")

# D5 first_seen 已是带时区串 → 归一到北京时间
eq(F({"pub_date": None, "first_seen": "2026-09-13T06:19:04+00:00"}),
   "2026-09-13T14:19:04+08:00", "D5 带时区 first_seen → 归一到 +08:00")

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    print("失败用例：")
    for f in failures:
        print("  - %s" % f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
