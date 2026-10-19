# tests/rss_composite/test_tag_semantics.py
# -*- coding: utf-8 -*-
"""_tag_articles 标签语义质量测试（issue C2）— TDD

issue C2 的原始判定：`_tag_articles` 产出 n-gram 碎片、不可用，两个夹具逐字复现：
  A6 "该公司完成 3 亿美元 融资 估值 上涨" → ['该公司', '完成 ', '融资']
  B2 "苹果发布会"                        → ['苹果发', '布会', 'galaxy']

根因：`_tokenize` 的贪心扫描从位置 0 起逐位吞 3 字 n-gram，
      词典术语会被前一位置的误吞切断（'人工智能' → '工智能'、'苹果发布会' → '苹果发'+'布会'）。
      实测真实语料 27076 个标签里，16.7% 的标签首/尾字符是虚词/功能字
      （'的星尘'、'或让'、'将瞄准'、'额上'），4.2% 含内部/首尾空白。

本文件守住的目标（均为可证伪的量化断言）：
  - 词典术语整词命中，绝不出现它的真子串（'工智能' ⊄ '人工智能'）
  - 边界对齐：标签首/尾不得是虚词/功能字
  - 无空白（首尾与内部）
  - 台标/栏目样板词（源名自指、源内高频重复）不得成为标签
  - 覆盖率与格式不回退
  - 真实语料上的边界违例率 ≤ 2%（基线 16.7%）
"""
import collections
import io
import json
import os
import re
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


def mk_src(items, key="S1", name="测试源"):
    """items = [(title, summary), ...]"""
    return [{
        "key": key, "name": name, "cat": "ai", "color": "#000", "tier": 2,
        "items": [{"title": t, "title_zh": t, "summary": s, "summary_zh": s,
                   "link": "https://x/" + (t[:10] or "blank") + str(i)}
                  for i, (t, s) in enumerate(items)],
    }]


def tags_of(src, i=0):
    return src[0]["items"][i].get("tags") or []


NOISE = B._TAG_EDGE_NOISE

print("=" * 74)
print("A. 报告夹具（issue C2 逐字复现）")
print("=" * 74)

# A1 夹具 A6：'该公司' / '完成' / 数值单位短语都不得成为标签
src = mk_src([("该公司完成 3 亿美元 融资 估值 上涨", "")])
B._tag_articles(src)
t6 = tags_of(src)
ok(len(t6) > 0, "A1a 夹具 A6 产出非空标签（防空集恒真，实得 %r）" % (t6,))
eq([t for t in t6 if t in ("该公司", "完成", "亿美元", "万元", "亿元", "亿美元融", "估值 ", "上涨 ")],
   [], "A1b 夹具 A6 不含碎片/样板/数值单位（实得 %r）" % (t6,))
ok("融资" in t6, "A1c 词典术语 '融资' 被保留（实得 %r）" % (t6,))

# A2 夹具 B2：'苹果发布会' 不得被切成 '苹果发' + '布会'
src2 = mk_src([("苹果发布会", "三星 Galaxy 折叠屏")])
B._tag_articles(src2)
t7 = tags_of(src2)
ok(any("苹果" in t for t in t7), "A2a 标签保留 '苹果'（实得 %r）" % (t7,))
eq([t for t in t7 if t in ("苹果发", "布会")], [],
   "A2b '苹果发布会' 不被切成 '苹果发'/'布会'（实得 %r）" % (t7,))

print()
print("=" * 74)
print("B. 词典术语整词命中（根因守卫）")
print("=" * 74)

# B1 词典术语不得被切断：'人工智能' 的 3 字子串 '工智能' 不得出现
src3 = mk_src([("关于人工智能的监管讨论", "")])
B._tag_articles(src3)
t8 = tags_of(src3)
ok("人工智能" in t8, "B1a 词典术语 '人工智能' 整词命中（实得 %r）" % (t8,))
eq([t for t in t8 if t != "人工智能" and t in "人工智能"], [],
   "B1b 没有 '人工智能' 的真子串（'工智能'/'智能' 等，实得 %r）" % (t8,))

# B2 多术语标题：词典优先，按出现位置取前 3
src4 = mk_src([("英伟达发布新芯片，人工智能算力需求激增", "")])
B._tag_articles(src4)
t9 = tags_of(src4)
ok("芯片" in t9 and "人工智能" in t9, "B2 词典术语 '芯片'/'人工智能' 均在标签中（实得 %r）" % (t9,))

# B3 出现次数多的词典术语优先于只出现一次的
src5 = mk_src([("人工智能与人工智能芯片：人工智能算力", "")])
B._tag_articles(src5)
eq(tags_of(src5)[0], "人工智能", "B3 高频词典术语排首位（实得 %r）" % (tags_of(src5),))

print()
print("=" * 74)
print("C. 边界与格式")
print("=" * 74)

src6 = mk_src([("该家公司被曝光的星尘计划或让市场重新定价", ""),
               ("额上用尽后用户批评定价策略", ""),
               ("梗图：博士生入学前后对比，读完笑容消失", "")])
B._tag_articles(src6)
allt = [t for i in range(3) for t in tags_of(src6, i)]
ok(len(allt) > 0, "C1 样本产出非空标签（实得 %r）" % (allt,))
eq([t for t in allt if t[0] in NOISE or t[-1] in NOISE], [],
   "C2 标签首/尾均非虚词功能字（实得 %r）" % (allt,))
eq([t for t in allt if t != t.strip() or re.search(r"\s", t)], [],
   "C3 标签无首尾/内部空白（实得 %r）" % (allt,))
eq([t for t in allt if len(t) < 2], [], "C4 标签长度 ≥2（实得 %r）" % (allt,))
eq([t for t in allt if len(t) > B._TAG_MAX_LEN and not t.isascii()], [],
   "C5 中文标签长度 ≤ %d（实得 %r）" % (B._TAG_MAX_LEN, allt))
eq([t for t in allt if B._is_numeric_unit_phrase(t)], [],
   "C6 数值单位短语不成为标签（实得 %r）" % (allt,))

print()
print("=" * 74)
print("D. 台标/样板词抑制")
print("=" * 74)

# D1 源名自指：源叫 CNN，标题里的 CNN 不是话题
src7 = mk_src([("CNN 报道：某地发生地震后救援进展", "")], key="CNN_1", name="CNN")
B._tag_articles(src7)
eq([t for t in tags_of(src7) if t.lower() == "cnn"], [],
   "D1 源名自指台标 'cnn' 被过滤（实得 %r）" % (tags_of(src7),))

# D2 源内高频重复（栏目名/台标）：15 篇里 12 篇含 RFI
items = [("RFI 独家：法国罢工进入第二天，交通受阻", "") for _ in range(12)]
items += [("量子计算芯片取得突破", ""), ("新能源汽车销量创新高", ""), ("开源社区发布新许可证", "")]
src8 = mk_src(items, key="RFI_1", name="法广中文")
B._tag_articles(src8)
per = [tags_of(src8, i) for i in range(15)]
eq([t for tags in per for t in tags if t.lower() == "rfi"], [],
   "D2 源内高频台标 'rfi' 被整源抑制（实得 %r）" % (per[:3],))
ok(any(tags_of(src8, i) for i in range(12, 15)),
   "D2b 抑制样板词后其余条目仍产出标签（实得 %r）" % (per[12:],))

print()
print("=" * 74)
print("E. 兼容性与确定性")
print("=" * 74)

src9 = [{"key": "S2", "name": "刷新源", "items": [
    {"t": "量子计算芯片突破", "s": "", "u": "https://y/1", "d": "2026-09-14T10:00:00+08:00"},
]}]
B._tag_articles(src9)
ok(len(src9[0]["items"][0].get("tags") or []) >= 1, "E1 刷新通道 t/s 字段契约仍可打标")
ok(isinstance(src9[0]["items"][0].get("tags"), list), "E2 tags 为列表")
orig = mk_src([("OpenAI 发布新模型", "摘要")])[0]["items"][0]
before = set(orig.keys())
src10 = mk_src([("OpenAI 发布新模型", "摘要")])
B._tag_articles(src10)
eq(set(src10[0]["items"][0].keys()) - before, {"tags"}, "E3 仅新增 tags 字段")
a = mk_src([("深度学习 Transformer 注意力机制", "")])
b = mk_src([("深度学习 Transformer 注意力机制", "")])
B._tag_articles(a)
B._tag_articles(b)
eq(tags_of(a), tags_of(b), "E4 确定性：同输入同输出")
src11 = mk_src([("", "")])
B._tag_articles(src11)
eq(tags_of(src11), [], "E5 空标题空摘要 → tags=[]")
src12 = mk_src([("OpenAI GPT Anthropic Claude 大模型 人工智能 机器学习", "")])
B._tag_articles(src12)
ok(len(tags_of(src12)) <= 3, "E6 标签数 ≤3")

print()
print("=" * 74)
print("E7. 摘要通道不切中文段（结构性契约，而非聚合指标）")
print("=" * 74)

# 为什么要有这一段：`_tag_articles` 对摘要传 allow_runs=False（只取词典术语 + 英文词），
# 对标题传 True（可做边界对齐的整段取词）。这个差别在 F 段聚合指标上只体现为
# 「疑似切片 21.21% → 21.72%」「词典率 18.21% → 17.49%」——幅度小于阈值，
# 因此把 allow_runs 改回 True 时 F 段**仍然全绿**（变异存活）。
# 聚合指标不灵敏 ≠ 契约不重要，故在此直接断言通道差别本身。
# 实测：把 allow_runs 对摘要改成 True 后，E7a/E7b 变红。
_summ_only = "嘉宾出席演讲"     # 6 字中文段：无词典术语、无英文词
src13 = mk_src([("", _summ_only)])
B._tag_articles(src13)
eq(tags_of(src13), [],
   "E7a 摘要不切中文段（纯中文段摘要不得硬切出标签，实得 %r）" % (tags_of(src13),))

src14 = mk_src([(_summ_only, "")])
B._tag_articles(src14)
ok(len(tags_of(src14)) >= 1,
   "E7b 标题仍做整段取词（同一文本作标题时应产出标签，实得 %r）" % (tags_of(src14),))

# 直接对候选函数断言，锁住「allow_runs 是真实开关」而不只是「恰好没触发」
_c_on = B._tag_candidates(_summ_only, "", allow_runs=True)
_c_off = B._tag_candidates(_summ_only, "", allow_runs=False)
ok(len(_c_on) >= 1, "E7c allow_runs=True 时中文段可取词（实得 %r）" % (_c_on,))
eq(_c_off, [], "E7d allow_runs=False 时不切中文段（实得 %r）" % (_c_off,))

print()
print("=" * 74)
print("F. 真实语料量化（基线 16.7%% 边界违例 / 4.2%% 含空白 / 词典率 4.2%%）")
print("=" * 74)

snap_path = os.path.join(ROOT, "rss_api_snapshot.json")
if os.path.exists(snap_path):
    with io.open(snap_path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    S = snap.get("sources", [])
    corpus = [{"key": s.get("key"), "name": s.get("name"), "cat": s.get("cat"),
               "color": s.get("color"), "tier": s.get("tier", 3),
               "items": [dict(it) for it in s.get("items", [])]} for s in S]
    B._tag_articles(corpus)
    flat = [(t for t in (it.get("tags") or []))
            for s in corpus for it in s.get("items", [])]
    n_items = len(flat)
    n_tagged = sum(1 for t in flat if t)
    alltags = [t for tags in flat for t in tags]
    n = len(alltags) or 1

    DICT = set(x.lower() for x in B._TECH_DICT)
    edge = sum(1 for t in alltags if t[0] in NOISE or t[-1] in NOISE)
    space = sum(1 for t in alltags if re.search(r"\s", t))
    frag = 0
    for s in corpus:
        for it in s.get("items", []):
            text = ((it.get("t") or "") + " " + (it.get("s") or "")).lower()
            for t in (it.get("tags") or []):
                if any(t != d and t in d and d in text for d in DICT):
                    frag += 1
    print("  语料：%d 篇 / 标签 %d（每篇 %.2f）" % (n_items, n, n / float(n_items or 1)))
    print("  边界违例 %d/%d = %.2f%%" % (edge, n, 100.0 * edge / n))
    print("  含空白   %d/%d = %.2f%%" % (space, n, 100.0 * space / n))
    print("  词典子串 %d/%d = %.2f%%" % (frag, n, 100.0 * frag / n))
    print("  词典率   %d/%d = %.2f%%" % (sum(1 for t in alltags if t.lower() in DICT), n,
                                         100.0 * sum(1 for t in alltags if t.lower() in DICT) / n))

    ok(n_tagged / float(n_items) >= 0.5,
       "F1 覆盖率 ≥50%%（实际 %.1f%%）" % (100.0 * n_tagged / n_items))
    eq([t for t in alltags if not isinstance(t, str) or len(t) < 2], [],
       "F2 全部标签为长度 ≥2 的字符串")
    ok(100.0 * edge / n <= 2.0,
       "F3 边界违例率 ≤2%%（基线 16.7%%，实际 %.2f%%）" % (100.0 * edge / n))
    ok(space == 0, "F4 含空白标签为 0（基线 4.2%%，实际 %d 个）" % space)
    ok(100.0 * frag / n <= 1.0,
       "F5 词典术语子串率 ≤1%%（实际 %.2f%%）" % (100.0 * frag / n))

    # F6 残留指标（参考）：非词典中文标签中，起点紧邻汉字的比例。
    # 口径是「疑似」——它含假阳性（'数学家' 出现在 '名顶尖数学家' 里是正常词，
    # 也会被计入），所以只看相对值：基线 27.28%，当前 21.21%。
    # 残留来自「超过 6 字且不含可用词典术语的中文段」——离线无分词器时只能按 n-gram 硬切。
    def _slice_flag(tag, text):
        if tag.lower() in DICT or tag.isascii():
            return 0
        k = 0
        while True:
            k = text.find(tag, k)
            if k < 0:
                return 0
            if k == 0 or not ("\u4e00" <= text[k - 1] <= "\u9fff"):
                k += 1
                continue
            return 1

    n_slice = 0
    for s in corpus:
        for it in s.get("items", []):
            text = ((it.get("t") or "") + " " + (it.get("s") or "")).lower()
            for t in (it.get("tags") or []):
                n_slice += _slice_flag(t, text)
    rate = 100.0 * n_slice / n
    print("  疑似中途切片（含假阳性口径）%d/%d = %.2f%%（基线 27.28%%）" % (n_slice, n, rate))
    ok(rate <= 24.0,
       "F6 疑似中途切片率 ≤24%%（基线 27.28%%，实际 %.2f%%）" % rate)
else:
    print("  [SKIP] 无 rss_api_snapshot.json，跳过 F 段")

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    for f in failures:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
