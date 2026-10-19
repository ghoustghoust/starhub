# -*- coding: utf-8 -*-
"""历史陈旧条目清理 · 单元测试

背景（2026-09-14 实测）：
    解析器修好后，小胡子哥 / tinyprojects 这类源会带着**真实发布时间**（2022~2023 年）回来，
    远超 72 小时窗口 —— 抓取循环里 `if pd_bj < cutoff: continue` 会直接跳过写入，
    于是 `rss_history` 里那条「pub_date 为空、first_seen 很新」的**陈旧条目原样留存**，
    再被降级键渲染成「收录 1 天前」留在页面上，直到 first_seen 自己老过 72 小时。

正确行为：既然现在知道真实日期是 2023 年，就该当场按真实日期清掉陈旧条目，
而不是让它靠 first_seen「续命」。

用法：python tests/rss_date/test_stale_history.py    退出码 0=全绿 1=有失败
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


def mk_src(items):
    return [{"key": "S1", "name": "源", "cat": "dev", "color": "#000", "tier": 2,
             "items": [dict(it, source="源", source_key="S1") for it in items]}]


LINK = "https://www.barretlee.com/blog/2023/old"

print("=" * 74)
print("A. 陈旧条目必须被真实日期清掉（而不是靠 first_seen 续命）")
print("=" * 74)

# 历史里已存在该链接：无日期、first_seen 是刚刚（因此旧逻辑下会被保留）
B._rss_history = {
    LINK: {"link": LINK, "source": "源", "source_key": "S1", "cat": "dev", "color": "#000",
           "title": "旧文", "title_zh": "", "summary": "", "summary_zh": "",
           "full_content": "", "image": "", "media_url": "", "media_type": "",
           "pub_date": "", "first_seen": "2026-09-13T14:19:04"},
}

# 本轮抓取带回真实发布时间：2023-10-27（远超 72h）
res, total = B._accumulate_history(mk_src([
    {"title": "旧文", "link": LINK, "summary": "", "full_content": "", "image": "",
     "media_url": "", "media_type": "", "pub_date": "2023-10-27T15:13:00+08:00"},
]))

out_links = [it["link"] for s in res for it in s.get("items", [])]
eq(LINK in B._rss_history, False, "A1 陈旧条目已从历史中清除")
eq(LINK in out_links, False, "A2 陈旧条目不再出现在输出里")

print()
print("=" * 74)
print("B. 对照组：真实日期在窗口内的条目必须照常保留（不能误删）")
print("=" * 74)

LINK2 = "https://example.com/fresh"
B._rss_history = {}
res2, _t2 = B._accumulate_history(mk_src([
    {"title": "新文", "link": LINK2, "summary": "", "full_content": "", "image": "",
     "media_url": "", "media_type": "", "pub_date": B._now_bj().isoformat()},
]))
out2 = [it["link"] for s in res2 for it in s.get("items", [])]
eq(LINK2 in out2, True, "B1 窗口内的新条目正常保留")
item2 = [it for s in res2 for it in s.get("items", []) if it["link"] == LINK2][0]
eq(bool(item2.get("date_fallback")), False, "B2 有真实日期的条目不得被标记为降级")

print()
print("=" * 74)
print("C. 无日期但 first_seen 在窗口内 → 必须保留并带降级标记（不允许沉底不可见）")
print("=" * 74)

LINK3 = "https://zhihu.example.com/nodate"
B._rss_history = {}
res3, _t3 = B._accumulate_history(mk_src([
    {"title": "无日期文", "link": LINK3, "summary": "", "full_content": "", "image": "",
     "media_url": "", "media_type": "", "pub_date": None},
]))
items3 = [it for s in res3 for it in s.get("items", []) if it["link"] == LINK3]
eq(len(items3), 1, "C1 无日期条目保留在输出中")
if items3:
    eq(bool(items3[0].get("date_fallback")), True, "C2 标记为降级（前端显示「收录」）")
    eq(bool(items3[0].get("pub_date")), True, "C3 降级键非空 → 可参与时间排序")

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    print("失败用例：")
    for f in failures:
        print("  - %s" % f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
