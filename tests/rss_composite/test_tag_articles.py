# tests/rss_composite/test_tag_articles.py
# -*- coding: utf-8 -*-
"""_tag_articles() 单元测试 — TDD RED/GREEN

验收标准：
  - 每篇文章获得 1-3 个话题标签
  - 标签来自标题+摘要中的分词命中
  - 空标题/空摘要 → tags 为空列表
  - 确定性：同输入同输出
  - 兼容两条数据通道：构建期 title/summary 与刷新通道 t/s
"""
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
    """构造单源结构。items = [(title, summary), ...]"""
    return [{
        "key": "S1", "name": "测试源", "cat": "ai", "color": "#000", "tier": 2,
        "items": [{"title": t, "title_zh": t, "summary": s, "summary_zh": s,
                   "link": "https://x/" + (t[:8] or "blank")} for t, s in items],
    }]


print("=" * 74)
print("A. _tag_articles 基本行为")
print("=" * 74)

# A1 含术语标题应提取出标签
src = mk_src([("OpenAI 发布 GPT-5 大模型", ""), ("Claude 新版本支持 Agent", "")])
B._tag_articles(src)
tags0 = src[0]["items"][0].get("tags", [])
ok(len(tags0) >= 1, "A1a 含术语标题至少有 1 个标签")
ok(all(isinstance(t, str) and len(t) >= 2 for t in tags0), "A1b 标签为长度≥2 的字符串")

# A2 空标题 → 空标签
src2 = mk_src([("", "")])
B._tag_articles(src2)
eq(src2[0]["items"][0].get("tags", []), [], "A2 空标题空摘要 → tags=[]")

# A3 确定性：两次调用结果一致
src3a = mk_src([("深度学习 Transformer 注意力机制", "")])
src3b = mk_src([("深度学习 Transformer 注意力机制", "")])
B._tag_articles(src3a)
B._tag_articles(src3b)
eq(src3a[0]["items"][0].get("tags"), src3b[0]["items"][0].get("tags"), "A3 确定性：同输入同输出")

# A4 标签数上限 ≤ 3
src4 = mk_src([("OpenAI GPT Anthropic Claude 大模型 人工智能 机器学习 深度学习 自然语言处理 神经网络 多模态", "")])
B._tag_articles(src4)
ok(len(src4[0]["items"][0].get("tags", [])) <= 3, "A4 标签数 ≤ 3")

# A5 不修改原始 item 的其他字段
src5 = mk_src([("OpenAI 发布新模型", "这是一条摘要")])
original_keys = set(src5[0]["items"][0].keys())
B._tag_articles(src5)
new_keys = set(src5[0]["items"][0].keys())
ok(new_keys - original_keys == {"tags"}, "A5 仅新增 tags 字段，不修改其他字段")

# A6 数值单位短语不得成为标签
src8 = mk_src([("该公司完成 3 亿美元 融资 估值 上涨", "")])
B._tag_articles(src8)
tags8 = src8[0]["items"][0].get("tags", [])
ok(not any(t in ("亿美元", "万元", "亿元", "亿美元融") for t in tags8),
   "A6 数值单位短语不成为标签（实得 %r）" % (tags8,))
# A6a 防空集恒真：上一条是否定式断言，tags 为空时必然通过，
# 故必须同时断言产出非空（issue m5：把 _tag_articles 变异成恒返回 [] 时 A6 单独仍绿）
ok(len(tags8) > 0, "A6a 夹具产出非空标签（防空集恒真，实得 %r）" % (tags8,))

# A7 标签不得含首尾空白（issue m3）
# 根因：_tokenize_cached 的 n-gram 切割会在中文与 ASCII 边界留下空格，
# 实测原夹具 "该公司完成 3 亿美元 融资 估值 上涨" 会产出 '完成 '。
src9 = mk_src([("该公司完成 3 亿美元 融资 估值 上涨", ""),
               ("小米 发布 新车 SU7 交付", ""),
               ("美团 外卖 单量 创新高", "")])
B._tag_articles(src9)
all_tags9 = [t for s in src9 for it in s["items"] for t in (it.get("tags") or [])]
eq([t for t in all_tags9 if t != t.strip()], [],
   "A7 标签无首尾空白（全部实得 %r）" % (all_tags9,))
ok(all(len(t) >= 2 for t in all_tags9), "A7b strip 后仍满足长度≥2")

print()
print("=" * 74)
print("B. 刷新通道字段兼容（t/s 契约）")
print("=" * 74)

# B1 刷新通道 item 只有 t/s 字段时也要能打标
src6 = [{"key": "S2", "name": "刷新源", "items": [
    {"t": "量子计算 芯片 突破", "s": "", "u": "https://y/1", "d": "2026-09-14T10:00:00+08:00"},
]}]
B._tag_articles(src6)
ok(len(src6[0]["items"][0].get("tags", [])) >= 1, "B1 刷新通道 t/s 字段也能提取标签")

# B2 标题优先于摘要
src7 = [{"key": "S3", "name": "s", "items": [
    {"t": "苹果发布会", "s": "三星 Galaxy 折叠屏 三星 Galaxy 折叠屏 三星 Galaxy 折叠屏"},
]}]
B._tag_articles(src7)
tags7 = src7[0]["items"][0].get("tags", [])
ok("苹果" in "".join(tags7) or any("苹果" in t for t in tags7), "B2 标题词进入标签（实得 %r）" % (tags7,))

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    for f in failures:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
