# tests/rss_history/test_body_noise_strip.py
# -*- coding: utf-8 -*-
"""Readability/信源正文里的站内导航与"相关阅读"块不是正文。

用户症状（2026-10-04）："全文里出现无关链接"。两类最集中：
  ① 正文尾部的推荐块（related-posts / read-next / article-tags / author-box / byline）；
  ② 指向 /tag/ /category/ /author/ 的内联链接 —— 链接文字往往正是正文里的词，
     不能整枚删掉，只能**去掉链接留文字**（删掉会吃掉正文）。
这条边界写进判据，是为了防止下一个人图省事改成"删掉所有 <a>"。

加载器用**本目录**的 `tests/rss_history/_loader.py`（带 sys.modules 缓存），
不照 brief 去 `tests/rss_composite` —— 无缓存版会把 A2 的模块 exec 次数放大并撞 exit 127
（task-2-report §5-D1 已记一次，本文件沿用同口径）。
"""
import io
import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
sys.path.insert(0, HERE)
from _loader import load_build  # noqa: E402

mod = load_build()
_c = mod._deep_clean_html

CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


def _entry_fulltext(cdata_html, link="https://blog.example/p/1"):
    """走**真解析入口**拿 `full_content`（而不是手工调 `_deep_clean_html`）。

    R42 之后这两条路给出的答案不一样：直接调能看到 class 属性，链路上看不到。
    凡是要判"清洗到底做了什么"的判据都得走这里，否则测的是那条不存在的路径。
    """
    xml = (u'<item xmlns:content="%s"><title>t</title>'
           u'<link>%s</link><description>d</description>'
           u'<content:encoded><![CDATA[%s]]></content:encoded>'
           u'<pubDate>Mon, 06 Oct 2025 08:00:00 GMT</pubDate></item>') % (
               CONTENT_NS, link, cdata_html)
    items = []
    mod._parse_rss_item(ET.fromstring(xml), u"某个博客", "some_blog_9", "tech", items)
    assert len(items) == 1, items
    return items[0]["full_content"]


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

def test_related_block_is_dropped():
    """【R42 改判】按 class/id 整枚删容器的那把刀已删除，这里钉"它不再存在"这件事。

    原判定（`<div class="related-posts">…</div>` 整枚消失）只在**直接调**
    `_deep_clean_html` 时才成立；生产链路是
    `_deep_clean_html(_sanitize_html(_normalize_body_html(...)))`，
    `_sanitize_html` 的属性白名单 `_KEPT_ATTRS` 不保留 class/id ⇒ 那把刀拿到手时
    `<div class="related-posts">` 早就是 `<div>`，命中恒为 0。
    2026-10-05 裁定：删掉开不了火的刀，不为它去改清洗顺序或加第四层；
    保留真正在起作用的那半（按 href 脱导航链接），由下面第二条判据钉住。
    闸：tests/rss_history/test_no_inert_deep_clean_rules.py。
    """
    fc = _entry_fulltext('<p>正文</p><div class="related-posts">相关阅读：'
                         '<a href="https://blog.example/x">别处</a></div>')
    assert "正文" in fc, fc
    assert "class" not in fc, "sanitize 现在保留 class 了：R42 的删除口径需要重新复核（%r）" % fc
    assert '<div class="related-posts">' not in fc and "相关阅读" in fc, \
        "容器名那一刀又被人塞回来了（或 sanitize 改了口径）：%r" % fc


def test_new_container_knobs_are_covered():
    """【R42 改判】保留的那半（按 href 脱链接）在容器名块里**照样**开火。

    原判定逐个试 read-next / article-tags / author-box / byline / table-of-contents 这些
    **容器名**能不能剥掉整块 —— 那正是开不了火的那一半，改判后不再钉它。
    这里换成钉"容器里指向 /tag/、/author/ 的链接仍然被脱掉 href、文字一个不丢"，
    也就是真数据上实测摘掉 36 条导航 href 的那把刀（同一份 class 命名的块是它的常见落点：
    推荐块里的链接基本都是 tag/author 链接）。
    """
    fc = _entry_fulltext('<p>正文</p><div class="related-posts">'
                         '<a href="/tag/kubernetes">Kubernetes</a>'
                         '<a href="https://blog.example/author/anna">anna</a></div>')
    assert "Kubernetes" in fc and "anna" in fc, "去链接把正文词吃掉了：%r" % fc
    assert "/tag/kubernetes" not in fc and "/author/anna" not in fc, \
        "容器里的导航链接没被脱 href：%r" % fc
    assert "<a " not in fc, fc


def test_internal_nav_link_loses_href_but_keeps_text():
    html = '<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署</p>'
    got = _c(html)
    assert "Kubernetes" in got and "<a " not in got, got


def test_author_and_category_links_too():
    got = _c('<p>x<a href="https://blog.com/author/anna">anna</a>'
             '<a href="https://blog.com/category/dev">dev</a></p>')
    assert "<a " not in got and "anna" in got and "dev" in got, got


def test_external_content_link_survives():
    html = '<p>参考 <a href="https://openai.com/blog/x">原始公告</a></p>'
    got = _c(html)
    assert 'href="https://openai.com/blog/x"' in got, got


def test_anchor_only_and_root_links_are_dropped_whole():
    assert "<a " not in _c('<p>x</p><a href="#">阅读更多</a>')
    assert "阅读更多" not in _c('<p>x</p><a href="#">阅读更多</a>'), "空链接连同文字一起消失才算干净"


# ──────────────────── 补充钉位（brief 的 6 条挡不住派工点名的变异）────────────────────

def test_root_slash_link_is_dropped_whole():
    """站点根链接（"返回首页"那类壳）整枚去掉 —— 与空锚点同一条规则的两半。"""
    got = _c('<p>正文</p><a href="/">返回首页</a>')
    assert "返回首页" not in got and "<a " not in got, got
    assert "正文" in got, "删根链接时把正文一起吃掉了：%r" % got


def test_nav_delink_keeps_surrounding_prose_verbatim():
    """去链接只动 `<a>` 这一枚：前后文字与空格一个字符不许变。

    这条是 `r'\\1'` 的**形状**判据（模板写错、多吃一个空格、或者把 `<p>` 一起吞掉都会红），
    上一条只判"Kubernetes 还在"，宽度不够。
    """
    html = '<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署</p>'
    assert _c(html) == '<p>我们用 Kubernetes 部署</p>', repr(_c(html))


def test_markup_inside_nav_anchor_survives_delinking():
    """链接里套了标签（`<a href="/tag/x"><strong>Kubernetes</strong></a>`）时，
    去链接只脱最外层，内层结构留着 —— 判据钉住 `([\\s\\S]*?)` 的捕获组不是 `[^<]*`。"""
    got = _c('<p>看 <a href="/tags/kubernetes"><strong>Kubernetes</strong></a> 吧</p>')
    assert "<a " not in got and "<strong>Kubernetes</strong>" in got, repr(got)


def test_in_page_fragment_link_is_not_dropped():
    """`href="#sec-2"` 是**有内容**的锚点（正文里的"见下节"），不属于"空锚点"。

    反例判据：规则 6 只能吃 `href="#"` / `href="/"` 两种精确形状。
    把它放宽成 `href="#[^"]*"` 就会把目录锚点的文字一起删掉（判据当场红）。
    """
    got = _c('<p>见 <a href="#sec-2">第二节</a> 的讨论</p>')
    assert 'href="#sec-2"' in got and "第二节" in got, repr(got)


def test_absolute_external_link_with_authorish_path_is_only_delinked_not_eaten():
    """外部链接命中导航词表时**也只能去链接**，文字必须留下（红线一的另一种形状）。"""
    got = _c('<p>作者 <a href="https://blog.com/authors/anna">Anna K.</a> 说</p>')
    assert "<a " not in got and "Anna K." in got, repr(got)


def test_host_named_after_a_nav_word_is_not_delinked():
    """`about`/`subscribe` 落在 **host** 上时不是站内导航段。

    brief 原样的整串子串匹配（`href="[^"]*(?:/about|/subscribe)[^"]*"`）把
    `https://about.fb.com/news/…` 当成 `/about` 命中 —— 真数据实测（3140 条出厂正文）
    这类误伤和下面那条中段误伤一起带走了 142 条外链 href（文字还在但点不动了）。
    这条判据在收紧之前是**红的**，收紧（只看 host 之后的第一个路径段）之后才绿。
    """
    html = ('<p>看 <a href="https://about.fb.com/news/2026/09/introducing-x">Meta 公告</a> 与 '
            '<a href="https://subscribe.example.com/mail/dev">订阅页</a> 说明</p>')
    got = _c(html)
    assert 'href="https://about.fb.com/news/2026/09/introducing-x"' in got, repr(got)
    assert 'href="https://subscribe.example.com/mail/dev"' in got, repr(got)


def test_nav_word_outside_first_path_segment_is_not_delinked():
    """导航词出现在路径**中段/尾段**时属于内容地址，不是站内导航：
    `github.com/<user>/<repo>/blob/main/docs/author/x.md`、`blog.example.com/2024/09/subscribe`。
    同上，这条在收紧前红、收紧后绿。
    """
    for u in ("https://github.com/u/repo/blob/main/docs/author/x.md",
              "https://blog.example.com/2024/09/subscribe",
              "https://docs.example.com/v1/tags/foo"):
        got = _c('<p>参考 <a href="%s">这条资料</a></p>' % u)
        assert 'href="%s"' % u in got, "外链非第一段的路径词被当导航剥掉：%r" % got


def test_first_segment_archive_is_a_known_and_bounded_cost():
    """**本批取向记录**：host 之后第一段正好是 `archive` 的外链仍按站内导航处理。

    `https://lists.gnu.org/archive/html/dev/…/msg00001.html` 会被去链接 —— 这是保留
    brief 词表里 `archive` 的既定代价（这份真数据快照实测 6 条 / 2159 条外部 href），
    不是新 bug：站内 `/archive/` 归档导航比 GNU 邮件存档外链常见得多。
    红线一在这一格仍然守得住：**只脱链接、文字原样留下**。
    若下一批决定把 `archive` 从词表里摘掉，第二条断言会红 —— 那是刻意的闸，不是腐化。
    """
    u = "https://lists.example.org/archive/html/dev/2024-01/msg00001.html"
    got = _c('<p>参考 <a href="%s">这封邮件</a></p>' % u)
    assert "这封邮件" in got, "去链接把正文文字也吃掉了：%r" % got
    assert "<a " not in got, "本批取向：/archive 第一段仍按站内导航处理（代价与理由见 docstring）：%r" % got


def test_nav_first_segment_still_delinked_with_deeper_path_and_query():
    """收紧的**反面**守卫：host 之后第一段是导航词时，后面再接多深的路径/查询都要照剥。

    防下一个人把这条刀收成"整段必须等于 /tag" 那种过度收窄（等于把 Task 10 撤回零效果）。
    """
    for u in ("https://blog.example.com/tag/kubernetes/2024",
              "https://blog.example.com/authors?sort=hits",
              "/category/dev/page/2"):
        got = _c('<p>我们用 <a href="%s">词</a> 部署</p>' % u)
        assert "<a " not in got and "词" in got and 'href="%s"' % u not in got, repr(got)


def test_rules_run_inside_the_real_parse_entry_chain():
    """端到端：`_deep_clean_html` 在链路里跑的是 **_sanitize_html 之后**的正文。

    这条同时钉三件事：
      ① 相对导航链接被 `_normalize_body_html` 绝对化之后**仍然**被去链接（规则 5 的
         `[^"]*` 前缀必须能吃掉 scheme+host）；
      ② 外部正文链接的 `href` 一路活到出厂；
      ③ 链接文字（正文词）一个字没丢。
    只测 `_c()` 的判据挡不住"sanitize 把 href 改写成单引号/别的属性形状"这类连带回归。
    """
    xml = (u'<item xmlns:content="%s"><title>t</title>'
           u'<link>https://blog.example/p/1</link><description>d</description>'
           u'<content:encoded><![CDATA['
           u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署，'
           u'参考 <a href="https://openai.com/blog/x">原始公告</a> 与 <a href="/category/dev">dev</a> 分类</p>'
           u']]></content:encoded>'
           u'<pubDate>Mon, 06 Oct 2025 08:00:00 GMT</pubDate></item>') % CONTENT_NS
    items = []
    mod._parse_rss_item(ET.fromstring(xml), u"某个博客", "some_blog_9", "tech", items)
    assert len(items) == 1, items
    fc = items[0]["full_content"]
    assert "Kubernetes" in fc and "原始公告" in fc and "dev" in fc, repr(fc)
    assert 'href="https://openai.com/blog/x"' in fc, "外部正文链接被吃了：%r" % fc
    assert "/tag/kubernetes" not in fc and "/category/dev" not in fc, "导航链接没被去 href：%r" % fc
    assert '<a href="https://blog.example/tag/kubernetes"' not in fc, repr(fc)


def test_deep_clean_html_is_wired_into_both_parse_entries():
    """接线判据：`_deep_clean_html` 必须仍在**两个**解析入口上（Atom 与 RSS 是两段代码）。

    本 Task 只改函数内部，但"函数自己有测试 ≠ 构建时会调用它"是本仓口径；
    入口漏一个，另一半正文照旧带噪声。
    """
    for fname in ("_fetch_rss", "_parse_rss_item"):
        seg = _fn(fname)
        assert "_deep_clean_html(" in seg, "%s 里已经没有 _deep_clean_html 调用" % fname


def test_sanitize_drops_class_so_rule1_is_inert_on_the_entry_chain():
    """口径事实（写进判据防漂移）：`_sanitize_html` 的白名单**不保留 class/id**
    （`_KEPT_ATTRS` 只给 a 留 href、给 img 留 src/alt）。

    ⇒ `_deep_clean_html` 规则 1（按 class/id 整枚删）在**真实入口链路**上是空转的，
    推荐块关键词表只在直接调 `_deep_clean_html` 时生效；入口链路上真正捞到无关链接的是
    规则 5/6（按 href 判）。这条现在为真；若哪天给 `_KEPT_ATTRS` 加上 class，它会红，
    那时必须回来复核规则 1 的删除面（否则整段正文可能被连坐删除）。
    """
    got = mod._sanitize_html('<div class="related-posts"><p>正文</p></div>')
    assert "class" not in got, "sanitize 现在保留 class 了：规则 1 的删除面需要重新复核（%r）" % got
    assert "正文" in got, got

# ────────────────────────── Task 11：粘连 URL 剥离 ──────────────────────────

def test_glued_url_is_stripped_only_when_no_separator():
    """实测样本（nodeseek_54）：description 是"链接+正文"直接粘连，无空格/换行。
    正常以空格分隔、或整条就是 URL 的摘要一律不许动 —— 放宽这条会连剥带错。"""
    s = "https://www.nodeseek.com/post-957723-1看了大佬的IX文章必须有行动力，马上干"
    assert mod._strip_glued_url(s) == "看了大佬的IX文章必须有行动力，马上干"
    # 有空格分隔：不动
    keep = "https://example.com/a 这是正文"
    assert mod._strip_glued_url(keep) == keep
    # 整条就是 URL：不动（剥了就没内容了）
    only = "https://example.com/a"
    assert mod._strip_glued_url(only) == only
    assert mod._strip_glued_url("") == ""
    assert mod._strip_glued_url("普通摘要，开头没有链接") == "普通摘要，开头没有链接"
    # hnrss 形状不能被这条误伤（它的 URL 后面是换行 + 英文）
    assert mod._strip_glued_url("Article URL: https://ex.com/a\nComments URL: https://y.com/b") == \
        "Article URL: https://ex.com/a\nComments URL: https://y.com/b"


def test_url_followed_by_newline_is_not_glued():
    """换行也是分隔符："URL 独占首行 + 正文在后"是合法摘要形状，不许剥。

    反例判据：把粘连正则放宽成 `^\\s*https?://\\S+`（去掉汉字先行断言）时，
    这条会连带把首行 URL 后面的整段正文口径改掉（判据红）。
    """
    s = "  https://ex.com/a\n评论在楼下了，欢迎补充"
    assert mod._strip_glued_url(s) == s, repr(mod._strip_glued_url(s))


def test_url_followed_by_fullwidth_punctuation_is_not_glued():
    """"URL 后紧跟**汉字**"是唯一形状：全角逗号/句号不是汉字 ⇒ 少剥一侧原样返回。

    这条记录的是**刻意的窄**（与 `_cap_body` 的"少剥"取向一致）：
    `https://ex.com/a，详见正文` 今天不剥，放宽到吃标点之前要先想清楚
    "URL 结尾的逗号到底属不属于 URL"（`a,` 是合法路径字符，正则 `\\S+` 会连标点一起吃）。
    """
    for s in ("https://example.com/a，这是正文", "https://example.com/a。第二段"):
        assert mod._strip_glued_url(s) == s, repr(mod._strip_glued_url(s))


def test_glued_url_mid_sentence_is_not_stripped():
    """粘连刀只认**行首**的 URL：同一副"URL 紧跟汉字"的形状出现在句中 ⇒ 原样返回。

    这条钉的是 `^\\s*` 那半截锚定。只放宽锚定（`^\\s*[\\s\\S]*?https?://…(?=汉字)`）时，
    行首的 `看了 ` 会被连坐剥掉 —— 变异 ④c 实测就红在这里（另三条反例在只放宽锚定时
    仍然绿，所以这条不是重复布防）。
    """
    s = "看了 https://ex.com/a评论区里都在说这个"
    assert mod._strip_glued_url(s) == s, repr(mod._strip_glued_url(s))
    # 句中 + 空白分隔：同样不动
    s2 = "评论 https://news.ycombinator.com/item?id=1 里吵翻了"
    assert mod._strip_glued_url(s2) == s2, repr(mod._strip_glued_url(s2))


def test_glued_strip_never_eats_the_body():
    """只剥 URL，正文一个字不许跟着走（"剥完整摘要"那类写法当场红）。"""
    body = "看了大佬的IX文章必须有行动力，马上干"
    got = mod._strip_glued_url("https://www.nodeseek.com/post-957723-1" + body)
    assert got == body, repr(got)
    # 粘连多段：剥掉前导 URL 后，正文里的 URL 与后续句子都还在
    mixed = "https://a.cn/x看了这条 https://b.cn/y 以及那句"
    assert mod._strip_glued_url(mixed) == "看了这条 https://b.cn/y 以及那句", repr(mixed)


def test_glued_strip_is_idempotent_and_japanese_counts():
    """幂等：解析入口剥过一遍，快照出口再过一次必须原样返回（两处都包刀的前提）。
    假名与汉字同侧（NodeSeek 有日文正文，只认 `\\u4e00-\\u9fff` 会漏）。"""
    once = mod._strip_glued_url("https://ex.com/a本文是结论")
    assert once == "本文是结论", repr(once)
    assert mod._strip_glued_url(once) == once
    assert mod._strip_glued_url("https://ex.com/aこれは日本語") == "これは日本語", \
        repr(mod._strip_glued_url("https://ex.com/aこれは日本語"))


def _hn_en():
    return ("Article URL: https://ex.com/a\n"
            "Comments URL: https://news.ycombinator.com/item?id=1\nPoints: 254\n# Comments: 162")


def test_rss_parse_entry_wraps_glued_strip_in_the_right_order():
    """接线 + 顺序：`_truncate(_strip_glued_url(_rewrite_hn_summary(desc, link)))`。

    顺序是本 Task 的承重点：粘连刀必须在 HN 重写**之后**。
    剥在前会改掉 HN 重写要看的行首形状（另见下一条行为判据）。
    """
    for fname in ("_parse_rss_item", "_fetch_rss"):
        seg = _fn(fname)
        assert re.search(r'"summary":\s*_truncate\(_strip_glued_url\(_rewrite_hn_summary\(', seg), \
            "%s 的 summary 出口没按 `_truncate(_strip_glued_url(_rewrite_hn_summary(` 接线" % fname


def test_snapshot_exit_also_wraps_glued_strip():
    """快照 `s` 出口必须包刀（与 Task 8 同因：`summary_zh` 存在 translations.json 里、
    跨场复用且不会被重新解析，老缓存里的粘连 URL 只能靠出口这把刀捞）。"""
    seg = _fn("_save_api_snapshot")
    assert re.search(r"_strip_glued_url\(\s*_rewrite_hn_summary\(", seg), \
        "`_save_api_snapshot` 的 s 出口没接 `_strip_glued_url`（快照读的是跨场缓存的 summary_zh）"


def test_rss_entry_end_to_end_strips_glued_summary():
    """端到端（RSS 入口）：NodeSeek 形状粘连摘要出厂即干净。"""
    it = ET.fromstring(u'<item><title>t</title><link>https://www.nodeseek.com/post-957723-1</link>'
                       u'<description>https://www.nodeseek.com/post-957723-1看了大佬的IX文章必须有行动力，马上干'
                       u'</description><pubDate>Mon, 06 Oct 2025 08:00:00 GMT</pubDate></item>')
    items = []
    mod._parse_rss_item(it, u"NodeSeek 某镜像", "nodeseek_54", "tech", items)
    assert len(items) == 1, items
    s = items[0]["summary"]
    assert s == "看了大佬的IX文章必须有行动力，马上干", repr(s)
    assert "http" not in s, "出口仍带裸链接：%r" % s


def test_atom_entry_end_to_end_strips_glued_summary(monkeypatch):
    """端到端（Atom 入口）：与 RSS 是两段代码，只接一半时这半照旧露粘连。"""
    atom = (u'<?xml version="1.0" encoding="utf-8"?>'
            u'<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            u'<title>t</title><link href="https://www.nodeseek.com/post-957723-1"/>'
            u'<summary>https://www.nodeseek.com/post-957723-1看了大佬的文章马上干</summary>'
            u'<updated>2026-10-05T00:00:00Z</updated></entry></feed>')
    monkeypatch.setattr(mod, "_fetch_url", lambda *a, **k: atom.encode("utf-8"))
    items = mod._fetch_rss({"key": "nodeseek_mirror_77", "name": u"NodeSeek 改名镜像",
                            "url": "https://example.invalid/feed", "cat": "tech"})
    assert items, "Atom 解析没出条目（判据前提失效）"
    assert items[0]["summary"] == "看了大佬的文章马上干", repr(items[0]["summary"])


def test_hn_template_summary_survives_the_glued_strip():
    """两条刀共存：hnrss 模板经过 `_strip_glued_url` 包一层后，Task 8 的口径一个字不许变。

    顺序倒置（先剥粘连再走 HN 刀）在这条上会红 —— 粘连刀把行首 URL 剥走后，
    剩下的 `论数: 3` 之类残行不再是模板行，HN 门槛随之失效。
    """
    en = _hn_en()
    got = mod._truncate(mod._strip_glued_url(mod._rewrite_hn_summary(en, "https://ex.com/a")))
    assert got == mod._truncate(mod._rewrite_hn_summary(en, "https://ex.com/a")), repr(got)
    assert "254" in got and "162" in got and "http" not in got, repr(got)


def test_order_matters_when_meta_lines_come_before_the_glued_line():
    """顺序的**行为**证据（不是只 grep 形状）：元数据在前、粘连行在后时，
    HN 重写会把粘连行提到最前，粘连刀必须在那之后才看得见它。

    先剥再重写 ⇒ 出口留着 `https://ex.com/a看了正文` 这种粘连串（判据红）。
    """
    s = "Points: 5\n# Comments: 3\nhttps://ex.com/a看了大佬的文章马上干"
    got = mod._truncate(mod._strip_glued_url(mod._rewrite_hn_summary(s, "https://ex.com/a")))
    assert got.startswith("看了大佬的文章马上干"), repr(got)
    # 倒序（变异 ⑦ 的形状）在这里给出不同答案
    wrong = mod._rewrite_hn_summary(mod._strip_glued_url(s), "https://ex.com/a")
    assert wrong != got, "两条刀在这个输入上不可区分，本判据失效（需要换输入）"


def test_snapshot_exit_strips_glued_url_from_cached_translation(tmp_path, monkeypatch):
    """快照出口的**行为**判据：`summary_zh` 是上一场翻译的缓存，
    那批老缓存里存的是**剥之前**的粘连形状 ⇒ 出口不包刀就照旧露给用户。

    与 task-8 的同名判据一样必须 in-process 生成快照（读仓库里已存在的
    `rss_api_snapshot.json` 改源码不会红 = 给变异电池留盲区）。
    """
    glued = "https://www.nodeseek.com/post-957723-1看了大佬的文章马上干"
    src = {"key": "nodeseek_54", "name": u"NodeSeek", "cat": "tech", "tier": 1,
           "items": [{"title": u"t", "link": "https://www.nodeseek.com/post-957723-1",
                      "summary": glued, "summary_zh": glued, "pub_date": "2025-10-06 08:00"}]}
    monkeypatch.chdir(tmp_path)
    mod._save_api_snapshot([src])
    payload = __import__("json").loads(io.open("rss_api_snapshot.json", encoding="utf-8").read())
    s = payload["sources"][0]["items"][0]["s"]
    assert s == "看了大佬的文章马上干", repr(s)
