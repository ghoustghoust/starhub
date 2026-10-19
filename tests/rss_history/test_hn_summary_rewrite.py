# tests/rss_history/test_hn_summary_rewrite.py
# -*- coding: utf-8 -*-
"""hnrss 的 description 是模板元数据，不是摘要：整段塞进阅读器就是"全是链接"。

实测样本（rss-data-0.js，2026-10-04）：
    hn_ai_7  s='Article URL: https://…\\nComments URL: https://news.ycombinator.com/item?id=49916739\\nPoints: 1\\n# Comments: 0'
    hackernews_6 s 同形状（Points: 10）
翻译后还有中文版（用户记录："评论网址/积分"）。这三行 URL 与计数在卡片上只是噪声，
而 `link` 字段本来就有 Article URL —— 信息一点没丢。

判据故意**不按 source_key 判断**：按形状判断才扛得住源改名、`hn_newest_56` 那类
新增镜像，以及翻译版；按 key 判断会漏，而且等于把"HN 这个源"写死进清洗层。
所以本文件既测 `_rewrite_hn_summary` 的形状判据，也用**改名后的 source_key**
端到端喂真解析器，钉住"接线不按 key 短路"。
"""
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
from _loader import load_build  # noqa: E402

mod = load_build()
_r = mod._rewrite_hn_summary

EN = ("Article URL: https://ex.com/a\n"
      "Comments URL: https://news.ycombinator.com/item?id=1\nPoints: 254\n# Comments: 162")
ZH = "文章网址：https://ex.com/a\n评论网址：https://news.ycombinator.com/item?id=2\n积分: 3\n# 评论数: 0"


def _src():
    with io.open(BUILD, encoding="utf-8") as fh:
        return fh.read().replace("\\'", "'")


def _fn(name):
    """按"函数名 → 下一个顶层函数名"切片读源码（本仓既有写法，不写死行号）。"""
    t = _src()
    i = t.find("def %s(" % name)
    assert i >= 0, "源码里找不到 def %s(：实现被删了或改名了" % name
    m = re.search(r"\ndef ", t[i + 1:])
    assert m, "%s 之后找不到下一个顶层 def：切片没有结束边界" % name
    return t[i:i + 1 + m.start()]


# ────────────────────────── brief 的 6 条 ──────────────────────────

def test_en_meta_becomes_one_compact_line():
    got = _r(EN, "https://ex.com/a")
    assert "Article URL" not in got and "Comments URL" not in got, got
    assert "254" in got and "162" in got, got
    assert "http" not in got, "重写后仍不该出现裸链接：%s" % got


def test_zh_translated_variant_also_matched():
    got = _r(ZH, "https://ex.com/a")
    assert "评论网址" not in got and "3" in got, got


def test_ordinary_summary_is_untouched():
    s = "本文介绍如何用 Rust 重写网关，性能提升明显。"
    assert _r(s, "https://ex.com/b") == s
    assert _r("", "") == ""


def test_one_meta_line_alone_is_not_enough():
    """只命中一行不算 HN 模板 —— 否则会把"Points: 3"这种正常句子吃掉。"""
    s = "评分说明\nPoints: 254"
    assert _r(s, "https://ex.com/c") == s


def test_ask_hn_text_post_keeps_body_text():
    """Ask/Show HN 的 description 是正文本身（只带 Comments URL + Points 两行元数据）。"""
    s = ("大家怎么看新的 Vercel 计费？\n"
         "Comments URL: https://news.ycombinator.com/item?id=9\nPoints: 5\n# Comments: 3")
    got = _r(s, "https://news.ycombinator.com/item?id=9")
    assert "大家怎么看" in got, got
    assert "Comments URL" not in got and "5" in got, got


def test_snapshot_exit_does_not_re_expose_meta_lines(tmp_path, monkeypatch):
    """summary_zh 存在 translations.json 里、跨场复用且不会被重新解析，
    所以解析入口改过之后，快照出口还要再过一次同一把刀。

    这条必须 **in-process 生成快照**：读仓库里已存在的 rss_api_snapshot.json 的写法，
    改源码它不会红（等于给变异电池留盲区）。_save_api_snapshot 写的是**相对**文件名，
    所以 chdir 到 tmp_path 就能拦住落点，仓库里那份真快照不会被覆盖。

    两个源一起喂，第二个换了 source_key：快照出口若按 key 短路（只有 HN 那个名字才包刀），
    这条照样红 —— 与 test_rss_entry_rewrites_for_a_renamed_source_key 同一个道理。
    """
    src = {"key": "hackernews_6", "name": "Hacker News", "cat": "tech", "tier": 1,
           "items": [{"title": "t", "link": "https://ex.com/a", "summary": EN,
                      "summary_zh": ZH, "pub_date": "2025-10-06 08:00"}]}
    renamed = {"key": "renamed_mirror_99", "name": "改名后的镜像", "cat": "tech", "tier": 1,
               "items": [{"title": "t2", "link": "https://ex.com/b", "summary": EN,
                          "summary_zh": ZH, "pub_date": "2025-10-06 08:00"}]}
    monkeypatch.chdir(tmp_path)
    mod._save_api_snapshot([src, renamed])
    payload = json.loads(io.open("rss_api_snapshot.json", encoding="utf-8").read())
    assert len(payload["sources"]) == 2, payload["sources"]
    for bs in payload["sources"]:
        s = bs["items"][0]["s"]
        for needle in ("Article URL", "Comments URL", "文章网址", "评论网址"):
            assert needle not in s, "快照出口又漏出 hnrss 模板行（key=%r）：%r" % (bs["key"], s)
        # 出口选的是 summary_zh（ZH 那条，积分 3），所以这里对的是中文变体的重写结果
        assert "3 分" in s, "重写过头，把分数也吃了（key=%r）：%r" % (bs["key"], s)


# ──────────────────────── 补充钉位（brief 的 6 条不够）────────────────────────

def test_rss_parse_entry_wraps_the_summary_exit():
    """RSS 出口的 `summary` 字段必须真包了刀：函数写了、出口没接 = 用户那边一模一样没变。

    Task 11 在 `_truncate` 与 `_rewrite_hn_summary` 之间加了 `_strip_glued_url` 一层，
    这里的链形跟着收紧成三层全钉 —— 顺序同样是被判据钉住的契约，不是宽松化。
    """
    seg = _fn("_parse_rss_item")
    assert re.search(r'"summary":\s*_truncate\(_strip_glued_url\(_rewrite_hn_summary\(', seg), \
        "`_parse_rss_item` 的 summary 出口没按 `_truncate(_strip_glued_url(_rewrite_hn_summary(` 接刀"


def test_atom_parse_entry_wraps_the_summary_exit():
    """Atom 出口（`_fetch_rss` 里 feed 分支）与 RSS 出口是两段代码，漏一个另一半照旧出噪声。"""
    seg = _fn("_fetch_rss")
    assert re.search(r'"summary":\s*_truncate\(_strip_glued_url\(_rewrite_hn_summary\(', seg), \
        "Atom 出口的 summary 没按 `_truncate(_strip_glued_url(_rewrite_hn_summary(` 接刀"


def test_rss_entry_rewrites_for_a_renamed_source_key():
    """端到端：把 hnrss 模板喂给**改了名的源**（source_key 里没有 hn/hacker）。

    这条专治"接线按 source_key 短路"—— 那种写法今天对 `hackernews_6` 有效，
    明天 `hn_newest_56` 改名或新镜像进来就漏，而判据只查 HN 的 key 时永远绿。
    """
    for key in ("renamed_mirror_99", "some_feed_7", "hn_newest_56"):
        it = ET.fromstring(u'<item><title>t</title><link>https://ex.com/a</link>'
                           u'<description><![CDATA[%s]]></description></item>' % EN)
        items = []
        mod._parse_rss_item(it, u"某个改名后的源", key, "tech", items)
        assert len(items) == 1, items
        s = items[0]["summary"]
        assert "Article URL" not in s and "Comments URL" not in s, \
            "按 source_key=%r 判定漏了（形状命中就该重写）：%r" % (key, s)
        assert "254" in s and "162" in s, "重写过头把计数吃了：%r" % s
        assert "http" not in s, "出口仍有裸链接：%r" % s


def test_atom_entry_rewrites_for_a_renamed_source_key(monkeypatch):
    """Atom 出口（`_fetch_rss` 的 feed 分支）端到端：_fetch_url 打桩，绝不发请求。

    RSS 与 Atom 是两段各自写 summary 的代码，只测 RSS 那一路的话，
    Atom 那半被改成"按 key 才包刀"也照样全绿。
    """
    atom = (u'<?xml version="1.0" encoding="utf-8"?>'
            u'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            u'<title>t</title><link href="https://ex.com/a"/>'
            u'<summary>' + EN + u'</summary>'
            u'<updated>2026-10-05T00:00:00Z</updated></entry></feed>')
    monkeypatch.setattr(mod, "_fetch_url", lambda *a, **k: atom.encode("utf-8"))
    items = mod._fetch_rss({"key": "renamed_atom_mirror_77", "name": u"改名后的 Atom 镜像",
                            "url": "https://example.invalid/feed", "cat": "tech"})
    assert items, "Atom 解析没出条目（判据前提失效）"
    s = items[0]["summary"]
    assert "Article URL" not in s and "Comments URL" not in s, \
        "Atom 出口按 source_key 短路了（改名镜像就漏）：%r" % s
    assert "254" in s and "162" in s and "http" not in s, s


def test_rewrite_is_idempotent():
    """快照出口对**已经重写过**的 summary 再过一次同一把刀，必须原样返回。

    不是洁癖：`s` 出口读的是 `summary` 或 `summary_zh`，两条链路重复过刀是常态，
    不幂等就会把"254 分 · 162 评论"再吃一遍。
    """
    once = _r(EN, "https://ex.com/a")
    assert _r(once, "https://ex.com/a") == once
    once_zh = _r(ZH, "https://ex.com/a")
    assert _r(once_zh, "https://ex.com/a") == once_zh


def test_prose_mentioning_points_is_not_eaten():
    """整行锚定：正文里出现 "Points: 254 …" 这种**句子**不许被当成模板行。

    门槛（≥2 行）之外还需要锚定，否则 `re.search` 那种宽松写法会把评分句吃掉。
    """
    s = ("The benchmark reports Points: 254 as the best score in the leaderboard\n"
         "and the follow-up study confirms it.")
    assert _r(s, "https://ex.com/d") == s, "非整行的评分句被吃掉了"


def test_body_lines_survive_verbatim():
    """正文一个字不许吃：Ask HN 多段正文逐行原样留在重写结果里。"""
    body = u"第一段：大家怎么看新的 Vercel 计费？\n第二段：有没有实测过成本的同事？"
    s = (body + "\nComments URL: https://news.ycombinator.com/item?id=9\n"
               "Points: 5\n# Comments: 3")
    got = _r(s, "https://news.ycombinator.com/item?id=9")
    for ln in body.split("\n"):
        assert ln in got, "正文行 %r 被吃掉：%r" % (ln, got)
    assert "5" in got and "3" in got, got
    assert "Comments URL" not in got, got


def test_two_meta_lines_without_points_or_comments_url_are_left_alone():
    """门槛的第二半：命中两行但没有 Points 也没有 Comments URL ⇒ 不算 hnrss 模板。"""
    s = u"文章网址：https://ex.com/a\n评论数: 3"
    assert _r(s, "https://ex.com/a") == s, "只有网址+评论数的形状被误判成 hnrss 模板"


# ─────────────── R47：标签被丢掉后剩下的"两行裸 URL" ───────────────
# 上游翻译层会把 `Article URL:` / `Comments URL:` 的**标签整个丢掉**，于是进来的正文是
#     https://… / https://… / 积分：2 / # 评论：0
# 四槽刀照旧开火（积分 + 评论两行就够门槛），但那两行 URL 以"正文行"的身份留在摘要里 ——
# 用户报的原症状"HN 里全是链接"到这一步只清了一半。剥它们的刀**只许在已判定为 HN 形状的
# 分支里动手**（`_HN_BARE_URL_LINE`）；做成通用剥离就是在吃正文，
# 所以第二条是**反例判据**，派工点名不许放宽。

FULL = chr(0xFF1A)   # 全角冒号：写码位不贴字形（与半角 ':' 在编辑器里看不出区别）


def test_hn_shape_bare_url_lines_are_stripped():
    """正例：标签没了、门槛照样过时，两行裸 URL 一条不许留在出口。"""
    s = u"https://ex.com/a\nhttps://news.ycombinator.com/item?id=1\n积分" + FULL + u"2\n# 评论: 0"
    got = _r(s, u"https://ex.com/a")
    assert "http" not in got, "HN 形状出口仍留着裸 URL 行：%r" % got
    assert got == u"2 分 · 0 评论 · Hacker News", got
    # 中英两套标签都在门槛之内 ⇒ 剥 URL 行这件事对两者同判
    both = _r(EN, u"https://ex.com/a")
    assert "http" not in both and "254" in both and "162" in both, both
    # 剥完留下的空洞（正文与元数据行之间那个空行）也要收掉，否则每场重写多/少一个空行
    tail = _r(u"正文一句。\n\nhttps://ex.com/a\nPoints: 4\n# Comments: 2", u"https://ex.com/a")
    assert tail == u"正文一句。\n4 分 · 2 评论 · Hacker News", tail
    # 大写 scheme 同判（Python 的 IGNORECASE 对齐 JS 的 i）
    up = _r(u"HTTPS://EX.COM/A\n积分" + FULL + u"1\n评论" + FULL + u"2", u"https://ex.com/a")
    assert "http" not in up.lower(), up
    # 句中 URL 不是"整行 URL"，一个字不许动
    mid = _r(u"正文 https://ex.com/inline 在句中\n积分" + FULL + u"9\n# 评论: 1", u"https://ex.com/a")
    assert u"https://ex.com/inline" in mid, "句中 URL 被当成裸 URL 行吃了：%r" % mid
    # 整行是 **URL+正文粘连**那一形（NodeSeek 类）：剥它是 `_strip_glued_url` 的活，本刀不许
    # 把整行吃掉。值段写成 `\S` 而不是 ASCII 可打印类时，这一条就是抓回归的那颗钉子。
    glued = _r(u"积分" + FULL + u"5\n# 评论: 3\nhttps://ex.com/a看了大佬的文章", u"https://ex.com/a")
    assert glued == u"https://ex.com/a看了大佬的文章\n5 分 · 3 评论 · Hacker News", glued


def test_bare_url_line_from_an_ordinary_summary_is_kept():
    """反例判据（派工点名）：**不许**在通用摘要上剥"整行就是 URL"的内容。

    很多源的正常摘要就是单独一行链接。门槛没过时那条分支必须先 return，
    把 `_HN_BARE_URL_LINE` 挪到门槛之前（=做成通用剥离）这条立刻变红。
    """
    for s in (u"https://ex.com/only",
              u"本周更新\nhttps://ex.com/changelog",
              u"See also\nhttps://a.test/x\nhttps://b.test/y",
              u"全文在此：\n\nhttps://a.test/z"):
        assert _r(s, u"https://ex.com/only") == s, \
            "门槛没过却被剥掉裸 URL 行（通用剥摘要）：%r" % s


def test_r47_strip_is_idempotent():
    """幂等：历史清扫与快照出口每场对同一字段重跑，第二遍必须一个字都不动。"""
    for s in (u"https://ex.com/a\nhttps://news.y.com/item?id=1\n积分" + FULL + u"2\n# 评论: 0",
              EN, ZH,
              u"正文一段。\n\nhttps://ex.com/last\nPoints: 4\n# Comments: 2"):
        once = _r(s, u"https://ex.com/a")
        assert _r(once, u"https://ex.com/a") == once, "第二遍仍在改动：%r -> %r" % (s, once)


def test_real_history_r47_leaves_non_hn_entries_untouched():
    """真语料复核（只读 rss_history.json，不发请求）：**R47 没有扩大改动面**。

    "非 HN 条目改动数必须为 0"不能靠"新实现自己说没改"来证 —— 那是同义反复。
    所以这里现造一个**门槛之前不剥 URL 行**的参照实现 `_pre`（四张正则与门槛逐字照抄本模块，
    唯独没有 R47 那一刀），再数两条：
      ① `_r` 改了而 `_pre` 没改的字段数 ⇒ 必须为 0。把 R47 那刀挪到门槛**之前**（做成通用
         剥离）时这一格立刻 >0 —— 它与上一条手写反例判据是同一件事在真语料上的读数。
      ② `_r` 出口里的裸 URL 行数 ⇒ 必须为 0（改前实测 5 行 / 4 个字段）。
    缺语料时按本仓既有口径 skip（上面两条手写判据已覆盖形状，不是"跳过即通过"）。
    """
    hist_p = os.path.join(ROOT, "rss_history.json")
    if not os.path.exists(hist_p):
        pytest.skip("rss_history.json 是 gitignore 的本地语料")
    with io.open(hist_p, encoding="utf-8") as fh:
        hist = json.load(fh)
    assert len(hist) >= 15000, "语料小到这不像那份历史：%d" % len(hist)

    def _pre(desc, link):
        """R47 **之前**的那把刀：门槛与四槽完全一样，只是不剥裸 URL 行。"""
        if not desc:
            return desc
        art = cmt = pts = cmts = u""
        keep = []
        for ln in desc.replace(u"\r\n", u"\n").split(u"\n"):
            ln = ln.strip()
            if not ln:
                if keep:
                    keep.append(ln)
                continue
            m = mod._HN_ART_URL.match(ln)
            if m:
                art = m.group(1)
                continue
            m = mod._HN_CMT_URL.match(ln)
            if m:
                cmt = m.group(1)
                continue
            m = mod._HN_POINTS.match(ln)
            if m:
                pts = m.group(1)
                continue
            m = mod._HN_COMMENTS.match(ln)
            if m:
                cmts = m.group(1)
                continue
            keep.append(ln)
        while keep and not keep[-1]:
            keep.pop()
        hits = (1 if art else 0) + (1 if cmt else 0) + (1 if pts else 0) + (1 if cmts else 0)
        if hits < 2 or not (pts or cmt):
            return desc
        meta = []
        if pts:
            meta.append(u"%s 分" % pts)
        if cmts:
            meta.append(u"%s 评论" % cmts)
        meta.append(u"Hacker News")
        return u"\n".join(keep + [u" · ".join(meta)]) if keep else u" · ".join(meta)

    bare = re.compile(r"^https?://\S+$", re.IGNORECASE)
    widened = 0          # "刀有没有开火"的判决在两把刀之间不一致 ⇒ R47 动了门槛/扩了改动面
    residual = 0         # 改后：HN 形状（刀开火）出口里仍留着的裸 URL 行
    pre_residual = 0     # 改前：同一批字段的裸 URL 行（只数开火的，门槛没过者本来就不许动）
    pre_kept_url = 0     # 改前那批字段里，"刀没开火却留着裸 URL 行"的行数 = 通用剥离会误伤的面
    fired_fields = 0
    untouched_fields = 0
    for v in hist.values():
        if not isinstance(v, dict):
            continue
        lk = v.get("link") or u""
        for f in (u"summary", u"summary_zh"):
            s = v.get(f) or u""
            if not s:
                continue
            out = _r(s, lk)
            pre = _pre(s, lk)
            if (out != s) != (pre != s):
                widened += 1
            if out == s:
                untouched_fields += 1
                pre_kept_url += sum(1 for ln in pre.split(u"\n") if bare.match(ln.strip()))
                continue
            fired_fields += 1
            residual += sum(1 for ln in out.split(u"\n") if bare.match(ln.strip()))
            pre_residual += sum(1 for ln in pre.split(u"\n") if bare.match(ln.strip()))
    assert widened == 0, "R47 动了门槛/扩了改动面：%d 个字段的开火判决与门槛口径不一致" % widened
    assert residual == 0, "HN 形状出口仍剩 %d 行裸 URL" % residual
    print(u"[R47 真语料] 条目=%d 开火字段=%d 门槛没过字段=%d | HN 出口裸URL行 改前=%d 改后=%d"
          u" | 非 HN 改动=0（那 %d 行整行 URL 原样保留）"
          % (len(hist), fired_fields, untouched_fields, pre_residual, residual, pre_kept_url))
