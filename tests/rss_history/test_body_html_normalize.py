# tests/rss_history/test_body_html_normalize.py
# -*- coding: utf-8 -*-
"""正文出厂前的两条规范化：懒加载图片提升为 src、相对 URL 绝对化；截断不许截半个标签。

现场证据（2026-10-04 用户诊断 + 本地实测 rss-data-0.js）：
  380 个公众号源、3044 条微信链接的正文里图片写成 <img data-src="https://mmbiz.qpic.cn/...">，
  而 `_KEPT_ATTRS["img"]` 只认 src/alt，`_sanitize_html` 进一步因为"img 没有 src"
  把**整枚**标签丢弃 —— 用户看到的是整段没有图，不是图挂了。
  相对路径 `/xxx.png` 被 `_URL_SCHEME` 放行（它显式允许 `/`），出厂后浏览器按
  github.io 解析 → 404。截断 `fc[:50000]` 是裸切片，能落在 `<img src="htt` 中间。
"""
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# 必须用本目录的 _loader（不是 tests/rss_composite 那份）：它带 sys.modules 缓存。
# 无缓存版每次调用都 exec_module，会把 sys.modules["build_rss_aggregator"] 换成一个
# 没有 _loaded_from 的实例，于是本目录后续每个测试文件的缓存判定全部失效 —— 实测
# A2 门禁里模块 exec 次数从 3 涨到 69（build_rss_aggregator 顶层重挂 sys.stdout，
# 同进程多次 exec 是本目录 _loader.py 文档里点名的 exit 127 硬崩来源）。
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

BASE = "https://mp.weixin.qq.com/s/AbC123"


def _n(html, base=BASE):
    return mod._normalize_body_html(html, base)


def test_lazy_src_promoted_when_src_absent():
    got = _n('<p>x</p><img data-src="https://mmbiz.qpic.cn/a.jpg">')
    # I1：原来断言的是子串 'src="…"'，而输入里的 `data-src="…"` 本来就含它 ——
    # 把整趟提升删掉这条照样绿（假绿）。必须锚在 `<img` 后面的那枚**真** src 上。
    assert '<img src="https://mmbiz.qpic.cn/a.jpg"' in got, got
    assert re.search(r'(?<![\w-])src="([^"]*)"', got) is not None, got


def test_placeholder_src_replaced_by_lazy_value():
    """微信的占位 src 是 data:image/gif 或 1x1.gif，真图在 data-src。"""
    got = _n('<img src="data:image/gif;base64,R0lGOD" data-src="https://mmbiz.qpic.cn/b.jpg">')
    assert 'src="https://mmbiz.qpic.cn/b.jpg"' in got, got
    assert "R0lGOD" not in got, got


def test_real_src_not_touched_by_lazy_attr():
    got = _n('<img src="https://cdn.example/real.png" data-src="https://cdn.example/other.png">')
    assert got.lower().count(" src=") == 1 and 'real.png' in got, got


def test_relative_paths_become_absolute_against_item_link():
    got = _n('<img src="/i/a.png"><a href="../b.html">t</a>')
    assert 'src="https://mp.weixin.qq.com/i/a.png' in got, got
    assert 'href="https://mp.weixin.qq.com/b.html' in got, got


def test_scheme_relative_and_anchor_untouched():
    got = _n('<img src="//cdn.example/c.png"><a href="#sec">s</a>')
    assert 'src="//cdn.example/c.png"' in got, got
    assert 'href="#sec"' in got, got


def test_data_src_attribute_itself_is_not_rewritten():
    r"""`(?<![\w-])` 少了就会被 data-src 命中：那会把 data-src 改写成第二个 src。"""
    got = _n('<img src="/i/a.png" data-src="/i/lazy.png">')
    assert 'data-src="/i/lazy.png"' in got, got
    assert got.lower().count(" src=") == 1, got


def test_no_base_url_keeps_body_as_is():
    got = mod._normalize_body_html('<img src="/i/a.png">', "")
    assert 'src="/i/a.png"' in got, got


# --- C1：懒加载属性值是不可信数据，不许当 re.sub 的 replacement 模板 -----------------

def test_lazy_src_value_is_not_evaluated_as_a_regex_template():
    r"""无 src 分支（`r'\1 src="%s"' % lazy`）：值里的反斜杠会被 re 解释。

    旧实现实测（开工探针）：
      `https://a\u0026b.jpg` → re.error: bad escape \u at position 17（整条正文构建抛断）
      `https://a\1b.jpg`     → 静默把捕获组 `<img` 注进属性值：`<img src="https://a<imgb.jpg"`
      `https://a\b.jpg`      → 退化成退格符 `\x08`
    改回调式替换后必须**逐字**落地。
    """
    for lazy in (r"https://a\u0026b.jpg", r"https://a\1b.jpg", r"https://a\b.jpg",
                 r"https://a\\b.jpg", r"https://100%\2a.jpg"):
        got = _n('<img data-src="%s">' % lazy)
        assert '<img src="%s"' % lazy in got, repr(got)
        assert got.count("<img") == 1, repr(got)


def test_placeholder_branch_value_is_not_a_template_either():
    r"""占位分支（`' src="%s"' % lazy`）同病：那个 pattern 没有捕获组，
    值里的 `\1` 直接抛 `invalid group reference 1`，`\u0026` 抛 `bad escape \u`。"""
    for lazy in (r"https://a\u0026b.jpg", r"https://a\1b.jpg", r"https://a\b.jpg"):
        got = _n('<img src="data:image/gif;base64,R0lGOD" data-src="%s">' % lazy)
        assert ' src="%s"' % lazy in got, repr(got)
        assert "R0lGOD" not in got, repr(got)


# --- I5：元组顺序就是优先级 -----------------------------------------------------------

def test_lazy_src_priority_follows_tuple_order_not_attribute_order():
    """注释写的是"顺序即优先级"，旧实现却按**属性出现顺序** break，元组顺序零影响。

    每一对都反过来写一遍：把元组里靠后的那个放在标签里更前面，赢的必须还是元组里靠前的。
    """
    names = mod._LAZY_SRC_ATTRS
    # 字面顺序也是契约的一部分（brief：JS 端口必须"同名同序"）：把元组整个倒过来，
    # 下面那圈"逐对反着写"的循环是测不出来的，所以这里单独钉一次。加字段必须两侧同改。
    assert names == ("data-src", "data-original", "data-lazy-src", "data-croporisrc"), names
    assert len(names) >= 2, names
    for i, first in enumerate(names):
        for j in range(i + 1, len(names)):
            second = names[j]
            html = '<img %s="https://e.com/lose_%d.png" %s="https://e.com/win_%d.png">' % (
                second, j, first, i)
            got = _n(html)
            real = re.findall(r'(?<![\w-])src="([^"]*)"', got)
            assert real == ["https://e.com/win_%d.png" % i], (first, second, got)


# --- I6：占位 src 判定只认确定形态，误判方向一律"保留原 src" --------------------------

def test_real_urls_that_merely_contain_placeholder_words_keep_their_src():
    """反向用例：全部是本仓真实数据里的形态（旧版整条 URL 关键词 search 会误杀）。

    来源：`_scratch/parity/rss-data-2.js` 的 `<img src="https://image.pseudoyu.com/images/
    lazy_cat_pic.png">`、img-proxy 链接尾上的 `%26wx_lazy%3D1` 查询串（本仓 5600+ 条）、
    mmbiz base64 段里撞出来的 `1x1`/`0x0`、`hkhl` 的 `/0x0/` 缩放目录段。
    误判成占位符 = 把真图换成镜像地址，比不修更糟，所以这些必须一字不动。
    """
    urls = (
        "https://image.pseudoyu.com/images/lazy_cat_pic.png",
        "https://mmbiz.qpic.cn/mmbiz_jpg/JPu5lvAD1XGLKR7Llu10zR5IjiahxPH6EpB69DhcsLlmT"
        "cx1x1h4qCMJyTWsibofLsSEajnibodIcqicWHzqmwjZYrwtg14GrIFbTOjjWRkp1GA/640?wx_fmt=jpeg",
        "https://wechat2rss.bestblogs.dev/img-proxy/?k=0021fdb2&amp;u=https%3A%2F%2F"
        "mmbiz.qpic.cn%2Fmmbiz_gif%2FjZCice40rkLiaP%2F640%3Fwx_fmt%3Dgif%26wx_lazy%3D1",
        "https://image.hkhl.hk/f/1024p0/0x0/100/none/807af893656362112995b1c809b92a5f/"
        "2026-09/New_Project_76__0.jpg",
        "https://cdn.example.com/img/placeholder_bear_v2.png",
        "https://cdn.example.com/i/blanket_0x01.jpg",
        "https://cdn.example.com/img/cat.jpg?from=lazyload&blank=1",
        "https://spacer.dev/1x1/hero.png",
    )
    for u in urls:
        got = _n('<img src="%s" data-src="https://e.com/mirror.png">' % u)
        real = re.findall(r'(?<![\w-])src="([^"]*)"', got)
        assert real == [u], (u, got)


def test_obvious_placeholder_srcs_are_still_replaced():
    """正向用例：收窄之后确定形态的占位图仍然必须被换掉（否则 Task 1 主缺陷没修上）。"""
    for ph in ("data:image/gif;base64,R0lGOD",
               "https://e.com/1x1.gif",
               "https://e.com/i/blank.gif",
               "https://cdn.example.com/spacer_1x1.png",
               "https://cdn.example.com/placeholder.png",
               "https://cdn.example.com/0x0.jpeg?v=2",
               "https://cdn.example.com/lazy.gif",
               "https://cdn.example.com/i/blank.GIF"):
        got = _n('<img src="%s" data-src="https://e.com/real.jpg">' % ph)
        real = re.findall(r'(?<![\w-])src="([^"]*)"', got)
        assert real == ["https://e.com/real.jpg"], (ph, got)


# --- M4：base 里带裸引号 --------------------------------------------------------------

def test_base_url_containing_a_double_quote_is_refused():
    """`https://e.com/d"q/` 这类 base：urljoin 的产物把引号带进属性值
    （`src="https://e.com/d"q/b.html"`），下游 _sanitize_html 的属性提取在第一个引号处
    截断 → 出厂一条静默变形的坏 URL。按"没有 base"处理，相对路径原样保留。"""
    base = 'https://e.com/d"q/'
    got = _n('<a href="b.html">t</a><img src="/i/a.png">', base)
    assert 'href="b.html"' in got and 'src="/i/a.png"' in got, got
    assert "e.com/d" not in got, got


def test_cap_never_leaves_a_half_tag():
    body = "<p>" + "x" * 49990 + '</p><img src="https://e.com/a.jpg">'
    got = mod._cap_body(body, 50000)
    assert len(got) <= 50000
    assert not re.search(r"<[^>]*$", got), repr(got[-40:])
    # I4：光主张"不许留残缺"，把标签正则写成 `<[^>]*>?$`（连完整标签一起吞）也照样绿。
    # 这里补上内容守恒的一头：`[:50000]` 的尾巴是 `<im`，剥到 `</p>` 就停，
    # 正确长度是 3 + 49990 + 4 = 49997（少剥 = 残缺，多剥 = 吃内容，两头都要有牙）。
    assert got.endswith("</p>"), repr(got[-40:])
    assert len(got) == 49997, len(got)
    assert len(got) > 50000 - 12, len(got)


def test_cap_keeps_a_complete_tag_sitting_on_the_boundary():
    """I4/V6：切片正好停在一枚**完整**标签的 `>` 上时，一个字都不许多剥。

    `<p>`(3) + x*49993 + `</p>`(4) = 50000，再拖 4 个字符 `tail` 保证这次真的截断了
    （I7：没截断的正文压根不许动刀，所以构造时必须越过上限）。
    变异体 `<[^>]*>?$` 会把结尾的 `</p>` 一起吞掉 → 这里必红。
    """
    body = "<p>" + "x" * 49993 + "</p>" + "tail"
    got = mod._cap_body(body, 50000)
    assert got == body[:50000], repr(got[-20:])
    assert len(got) == 50000 and got.endswith("</p>"), repr(got[-20:])


def test_cap_never_leaves_a_half_entity():
    body = "<p>" + "y" * 49992 + "</p>&am"
    got = mod._cap_body(body, 50000)
    assert not re.search(r"&[#A-Za-z0-9]*$", got), repr(got[-20:])


def test_cap_strips_entity_broken_one_char_into_its_name():
    """I2：把 `_DANGLING_ENTITY_RE` 收窄成 `&$`（完全不剥 `&am`）时，上面那条和下面那条
    裸 `&` 用例全都算不到 —— 它们的截断点本来就停在 `&` 上，尾部只剩一个字符。

    这条的算式（自己解的，不是抄的）：`<p>`(3) + y*49991 + `</p>`(4) + `&am`(3) = 50001，
    `[:50000]` 的尾巴是 `</p>&a`（`&` 后面还挂着 1 个实体名字符，索引 49998 才是 `&`）。
    正确实现剥回 `</p>`，长度 3 + 49991 + 4 = 49998；`&$` 变异体留着 `&a` 不放 → 长度 50000。
    """
    body = "<p>" + "y" * 49991 + "</p>&am"
    assert body.index("&") == 49998 and len(body) == 50001
    got = mod._cap_body(body, 50000)
    assert got.endswith("</p>"), repr(got[-20:])
    assert len(got) == 49998, len(got)
    assert not re.search(r"&[#A-Za-z0-9]*$", got), repr(got[-20:])


def test_cap_strips_entity_broken_right_after_the_ampersand():
    """截断正好停在 `&` 上：这是第一版正则（`&[A-Za-z#]…` 要求至少一个后继字符）漏掉的一种。

    数字 49992 是解出来的，不是试出来的：`<p>`(3) + y*49992 + `</p>`(4) + `&`(1) = 50000，
    写到这里就**等于没截断** —— I7 之后这种情况必须原样返回，所以再拖一个 `x` 把总长推到
    50001，切片边界才真正落在裸 `&` 上（索引 49999）。
    （历史：brief 原本写 49996 → 总长 50004、尾巴停在 `<` 上，裸 `&` 压根没进截断结果。）
    """
    got = mod._cap_body("<p>" + "y" * 49992 + "</p>&" + "x", 50000)
    assert not got.endswith("&"), repr(got[-20:])
    assert got.endswith("</p>"), repr(got[-20:])
    assert len(got) == 49999, len(got)


# --- NEW-1：刀二的窗口必须先把切点上的尾部空白退掉（上一轮修复自己引入的回归）--------

def test_cap_entity_knife_rewinds_trailing_whitespace_before_matching():
    r"""刀二在**没退空白**的窗口上匹配 ⇒ 漏剥 ⇒ 尾部残缺实体重新暴露。

    上一轮把 while 换成"切点循环"之后，循环体是
    `window = out[cut - _ENTITY_TAIL_WINDOW:cut]`，而每退一步 `cut` 都可能停在空白上；
    `_DANGLING_ENTITY_RE` 是 `$` 锚定的，窗口末字符是空白 ⇒ 必然匹配不到 ⇒ break；
    于是最后一行 `out[:cut].rstrip()` 才把那个空白剥掉 —— 剥完，残缺实体又挂在尾巴上了。
    期望值一律取**定点语义 + NEW-3 的"实体名段至多剥 1 段"**，开工实测（上一轮实现 → 期望）：
      `cap("& &m", 3)`      → ''    期望 ''      —— 复核实测的最小复现（上一轮已修好）
      `cap("A&B &am ", 7)`  → 'A'   期望 'A&B'   —— NEW-3：第二刀不许再剥第二个 `&名字`
      `cap("A&NBSPnbsp", 7)` → 'A'   期望 'A'    —— 同一条：这里第二段是**裸 &**，仍然要剥
      `cap("&\xa0&nbsp", 6)` → ''    期望 ''      —— NBSP 版，顺带打死 `out[i] <= ' '` 那种修法
    （NBSP = U+00A0；上面那一行的 `NBSP` 是它的占位写法，断言里用的是真字符。）
    `\xa0`（NBSP）必须是退得掉的：本仓公众号正文里的"空格"大量是它，而出口的空白口径是
    `str.rstrip()`，`<= ' '` 与 rstrip 不等价（rstrip 认 \xa0/\u2028/…，`<= ' '` 不认）。
    """
    assert mod._cap_body("& &m", 3) == "", repr(mod._cap_body("& &m", 3))
    assert mod._cap_body("A&B &am ", 7) == "A&B", repr(mod._cap_body("A&B &am ", 7))
    assert mod._cap_body("&\xa0&nbsp", 6) == "", repr(mod._cap_body("&\xa0&nbsp", 6))
    assert mod._cap_body("A&\xa0&nbsp", 7) == "A", repr(mod._cap_body("A&\xa0&nbsp", 7))
    # 复核另外两条真实形态的"空白前没有 &"变体：这一族在回归实现下本来就对（留着防回退，
    # 并且它们同时是 V13 的靶 —— 出口那记 rstrip 必须连 NBSP 一起退掉）。
    assert mod._cap_body("Tom &am ", 7) == "Tom", repr(mod._cap_body("Tom &am ", 7))
    assert mod._cap_body("Tom &amp ", 8) == "Tom", repr(mod._cap_body("Tom &amp ", 8))
    assert mod._cap_body(u"价格\xa0&nbspz", 7) == u"价格", repr(mod._cap_body(u"价格\xa0&nbspz", 7))

    # 生产上限上的同一族（limit = _BODY_CAP，尾巴正好落在切片边界上）
    for tail, want_len in (("A&B &am ", 49995), ("& &m", 49996), ("&\xa0&nbsp", 49993)):
        body = "<p>" + "y" * (50000 - 3 - len(tail)) + tail + "z"
        assert len(body) == 50001, (tail, len(body))
        got = mod._cap_body(body, 50000)
        assert len(got) == want_len, (tail, len(got))
        assert got == body[:want_len], (tail, repr(got[-12:]))
        # ①a（NEW-3 改写）：这里只能断**无歧义形态**。旧断言 `&[#A-Za-z0-9]*$` 是星号+名段版，
        # 它会把契约明确保留的合法文字 `A&B`（尾段 `&B`）也判成残缺 —— 与 NEW-3 直接冲突。
        # 口径复用 `_CAP_UNAMBIG_TAIL_RE`（ASCII 显式类，与实现的扫描器同源），不在这里再写一份。
        assert not _CAP_UNAMBIG_TAIL_RE.search(got), (tail, repr(got[-12:]))

    # 刀二停稳之后，唯一还在退空白的就是出口那记 rstrip（V13：删掉它这条就红）。
    assert mod._cap_body("ab  &mz", 6) == "ab", repr(mod._cap_body("ab  &mz", 6))


# --- NEW-3：第二刀及以后只许剥"无歧义形态"（NEW-1 的修法自己把合法文字吃了）----------

def test_cap_second_knife_strips_only_unambiguous_dangling_entity():
    r"""实体名段至多剥 **1 段**：刀二起只许 `&$` / `&#\d*$` / `&#x[0-9a-fA-F]*$`。

    NEW-1 那次修复把"退尾部空白"放进了刀二的每一轮循环，却没有同时收窄第二刀的**形态**，
    于是回溯之后又拿 `&[#A-Za-z0-9]{0,8}$` 剥了一次 —— 把合法文字里的 `&X` 当残缺实体吃掉。
    limit 全是我自己算的：必须 `len(text) > limit`（否则按 R14 根本不动刀，测不到级联），
    且切片尾巴要正好把"第二个 `&名字`"留在退完空白的切点上。开工实测（`c84a59b` → 期望）：
      cap("A&B &am ", 7)   len 8 → 'A'    期望 'A&B'   切片 `A&B &am`，刀一剥掉 `&am`
      cap("AT&T &am", 7)   len 8 → 'AT'   期望 'AT&T'  切片 `AT&T &a`，刀一剥掉 `&a`
      cap("Q&A &am ", 7)   len 8 → 'Q'    期望 'Q&A'   同第一族（尾部带空白那版）
      cap("R&D &nbsp", 8)  len 9 → 'R'    期望 'R&D'   切片 `R&D &nbs`，刀一剥掉 `&nbs`
      cap("a&&b&am", 6)    len 7 → 'a'    期望 'a&&b'  级联深 3：`&a` → `&b` → 尾巴裸 `&`
      cap("x&xab&am", 7)   len 8 → 'x'    期望 'x&xab' `&xab` 不是 `&#x…`，刀二不许剥
    第一刀的既有契约**不动**（`Tom&Jerryzz`/7 修复前后同为 'Tom'）：`&X` 被吃这件事本来就在，
    NEW-1 的空白回溯只是把它从第一刀扩到了第二刀 —— 所以本轮只收第二刀及以上的权限。

    反向不许顺手放宽：无歧义形态在第二刀仍**必须**剥（否则 NEW-1 复发）：
      cap("& &m", 3)      == ''        第二段是裸 `&`
      cap("x&  &ab", 6)   == 'x'       退两枚空白后的裸 `&` 仍在权限内
      cap("x&#12&am", 7)  == 'x'       第二段是 `&#`+十进制（数字实体）
      cap("x&#xab&am", 8) == 'x'       第二段是 `&#x`+十六进制（数字实体）
    而且这一查是**无条件**的（名段那一刀什么都没剥到时也查），否则 ①a 只是"碰不到"而非"守得住"：
      cap("a&#000000000z", 11)  == 'a'  尾巴 11 个字符的十进制残缺，名段正则（≤9）看不见它
      cap("a&#x000000000z", 13) == 'a'  十六进制版同理
    """
    for body, limit, want in (("A&B &am ", 7, "A&B"), ("AT&T &am", 7, "AT&T"),
                              ("Q&A &am ", 7, "Q&A"), ("R&D &nbsp", 8, "R&D"),
                              ("a&&b&am", 6, "a&&b"), ("x&xab&am", 7, "x&xab")):
        assert len(body) > limit, (body, limit)          # 真截断了才谈动刀（R14）
        got = mod._cap_body(body, limit)
        assert got == want, (body, limit, repr(got), repr(want))
    for body, limit, want in (("& &m", 3, ""), ("x&  &ab", 6, "x"),
                              ("x&#12&am", 7, "x"), ("x&#xab&am", 8, "x"),
                              ("a&#000000000z", 11, "a"), ("a&#x000000000z", 13, "a")):
        assert len(body) > limit, (body, limit)
        got = mod._cap_body(body, limit)
        assert got == want, (body, limit, repr(got), repr(want))


# --- NEW-3b：无歧义那一刀的扫描实现必须与正位**同一套概念**（一个概念两套写法 = M3）----

def test_cap_unambiguous_scan_is_the_same_predicate_as_the_regex():
    r"""`_unambiguous_entity_start` 是 `_DANGLING_ENTITY_UNAMBIG_RE` 的等价实现，不是第二套口径。

    为什么本轮要新增这条：契约（不变量①a）要求"截断过的出口绝不挂裸 `&` / `&#`数字 /
    `&#x`十六进制 的尾巴"，而这一查是**每一轮**都要做的。用整串正则做 = `&`×50000 二次方
    （M1 明令禁止），所以实现走右往左扫描。扫描器和正则是同一概念的两份写法，
    必须逐字等价 —— 等价性在这里**穷举**对账，不留"两处真相"给 Task 3 的 JS 端口。
    穷举规模：字母表 7 个字符（`&` `#` `x` `1` `a` `g` 空格，覆盖裸 &、`#`、`&#x`、
    十进制、十六进制含 `a`/`f` 类字母、非十六进制的 `g`）× 长度 0~5 的全部串 × 每个前缀；
    外加 4000 条随机长串（14 字符，含大写十六进制与 9）同样逐前缀对账。
    """
    scan = mod._unambiguous_entity_start
    rx = mod._DANGLING_ENTITY_UNAMBIG_RE
    alpha = ("&", "#", "x", "1", "a", "g", " ")
    checked = hit = 0
    t0 = time.perf_counter()
    pool = [""]
    for _ in range(5):
        pool = [p + c for p in pool for c in alpha]
    for rnd_str in pool:
        for end in range(len(rnd_str) + 1):
            m = rx.search(rnd_str[:end])
            want = None if m is None else m.start()
            got = scan(rnd_str, end)
            checked += 1
            if want is not None:
                hit += 1
            assert got == want, (repr(rnd_str), end, repr(got), repr(want))
    rnd = random.Random(4711)
    wide = ("&", "#", "x", "X", "0", "9", "f", "F", "a", "z", " ", ";", "\xa0", "1")
    for _ in range(4000):
        s = "".join(rnd.choice(wide) for _ in range(14))
        for end in range(len(s) + 1):
            m = rx.search(s[:end])
            want = None if m is None else m.start()
            got = scan(s, end)
            checked += 1
            if want is not None:
                hit += 1
            assert got == want, (repr(s), end, repr(got), repr(want))
    dt = time.perf_counter() - t0
    # 空转防护：这套对账必须真的见过"命中"的形态，否则等价性是零样本里蒙出来的。
    assert checked > 100000, checked
    assert hit >= 500, hit
    assert dt < 5.0, "%.2fs" % dt


# --- NEW-2a：R14 的"恰好等于上限"边界（此前只被内容干净的输入钉着）------------------

def test_cap_leaves_exactly_at_limit_body_with_a_dangling_tail():
    r"""总长**恰好等于** limit 且尾部就是一条残缺实体/残缺标签 ⇒ 必须逐字节原样返回。

    上一轮的 I7 判据用的是 `"x" * 50000`（内容干净），所以把 `len(text) <= limit` 改成
    `<`（V11）之后 23 条全绿：边界上那条"未截断也进了动刀分支"的路径没有任何判据守着。
    这里把 R14 的口径写死成一对：恰好等于 = 不动刀（含残缺尾巴），多一个字符 = 必须动刀。
    """
    assert mod._cap_body("y" * 49999 + "&", 50000) == "y" * 49999 + "&"
    # 残缺实体 + 残缺标签同时挂在尾巴上，也不许动（`&am` 与 `<sp` 都是契约要剥的形态）
    assert mod._cap_body("y" * 49993 + "&am <sp", 50000) == "y" * 49993 + "&am <sp"
    # 默认 limit（_BODY_CAP）同一条口径，尾巴还多带一个空白
    assert mod._cap_body("y" * 49990 + "&abcdefgh ") == "y" * 49990 + "&abcdefgh "
    # 差一个字符就完全是另一回事：这两条保证上面三句不是因为"整条判据根本没跑"而绿
    assert mod._cap_body("y" * 49999 + "&x", 50000) == "y" * 49999
    assert mod._cap_body("y" * 49993 + "&am <spz", 50000) == "y" * 49993


# --- NEW-2b：`_ENTITY_TAIL_WINDOW` 的取值本身（此前零判据）--------------------------

def test_cap_entity_window_is_wide_enough_for_eight_name_chars():
    r"""窗口宽度 9 是从正则量词**推**出来的，不是拍的：把它改成 8（V12）此前全绿。

    `_DANGLING_ENTITY_RE = &[#A-Za-z0-9]{0,8}$` 的任何匹配最长 = 1 枚 `&` + 8 个实体名字符
    = 9 个字符，且必然以切片边界结束 ⇒ 起点一定落在最后 9 个字符里 ⇒ 只看这 9 个字符的窗口
    就等价于看整串。所以 `_ENTITY_TAIL_WINDOW == 1 + {0,8} 的上界`，两边只能有一个真相
    （Task 3 的 JS 端口照这个推导抄，别只抄字面 9）。
    """
    q = re.search(r"\{0,(\d+)\}", mod._DANGLING_ENTITY_RE.pattern)
    assert q is not None, mod._DANGLING_ENTITY_RE.pattern
    assert mod._ENTITY_TAIL_WINDOW == int(q.group(1)) + 1, (
        mod._ENTITY_TAIL_WINDOW, mod._DANGLING_ENTITY_RE.pattern)
    assert mod._ENTITY_TAIL_WINDOW == 9, mod._ENTITY_TAIL_WINDOW

    # 8 个实体名字符把窗口占满：窗口只有 8 就看不见那枚 `&` → 残缺尾巴出厂
    body = "<p>" + "y" * (50000 - 3 - 13) + "</p>&abcdefgh" + "z"
    assert len(body) == 50001 and body[49991] == "&", len(body)
    got = mod._cap_body(body, 50000)
    assert got == "<p>" + "y" * 49984 + "</p>", repr(got[-16:])
    assert len(got) == 49991 and not re.search(r"&[#A-Za-z0-9]*$", got), repr(got[-16:])
    # 同一形态再拖一个空格（NEW-1 × 满窗口）：先退空白、再吃满 9 个字符
    body2 = "<p>" + "y" * (50000 - 3 - 14) + "</p>&abcdefgh " + "z"
    assert len(body2) == 50001, len(body2)
    got2 = mod._cap_body(body2, 50000)
    assert got2 == "<p>" + "y" * 49983 + "</p>", repr(got2[-16:])
    assert len(got2) == 49990, len(got2)


# --- 属性化自证：截断的三条不变量（①a 无歧义尾巴 / ①b 级联有界 / ② R14）在几千条输入上成立

_CAP_ALPHA = ("<", ">", "&", " ", "\t", "\n", "\xa0", "a", "m", "p", "0", "#", ";", "/",
              "\u4ef7", "\U0001f42a")
# ①a 的残缺标签口径，同时用来把刀一剥掉的片段从 ①b 的差分里排除（见用例里的说明）
_CAP_DANGLING_TAG_RE = re.compile(r"<[^>]*$")
# ①b 的"实体名段"：`&` + 字母起头 + 至多 7 个名符（`&#12` 那类数字实体不在此列，理由见用例）
_CAP_NAME_SEG_RE = re.compile(r"&[A-Za-z][A-Za-z0-9]{0,7}")
# ①a 的三种"无歧义残缺实体"形态，合成一条 alternation：尾部裸 `&` / `&#`+十进制 / `&#x`+十六进制。
# 量词写成 ASCII 显式类（`[0-9]`）而不是 `\d`：契约那一侧的实现是右往左扫描（只能按 ASCII 判），
# `\d` 在 str 模式下还认 Unicode 十进制数字，两边会分叉；收窄的方向是"少剥"，与 NEW-3 同向。
_CAP_UNAMBIG_TAIL_RE = re.compile(r"&(?:#[0-9]*|#x[0-9a-fA-F]*)$")
# 生成器自由度的证据用：`&` 之后连续实体名字符的运行（字符类与契约的 `[#A-Za-z0-9]` 同源，
# `&` 本身不算名符 —— 这一点是 ①b 成立的根基：一次剥离的差分里只可能有一枚 `&`）。
_CAP_NAME_RUN_RE = re.compile(r"&([#A-Za-z0-9]*)")
# 注入长名段用的字符：`[#A-Za-z0-9]` 与下面那套字母表的交集（`&` 不在其中，一次注入只有一枚 &）
_CAP_NAME_RUN_CHARS = ("#", "a", "m", "p", "0")


def _cap_longest_name_run(s):
    r"""`s` 里 `&` 之后连续实体名字符的最长长度（0 = 串里没有 `&`）。"""
    return max([len(r) for r in _CAP_NAME_RUN_RE.findall(s)] + [0])


def _cap_random_text(rnd, ln):
    r"""随机字符流，**不做任何"实体名封顶"**（NEW-3 的口径修正）。

    上一版把 `&` 之后的名段长度封顶在 8，是为了迁就星号不变量 `&[#A-Za-z0-9]*$` —— 那条
    不变量比契约的 `{0,8}` 强，"封顶"等于让判据去适配实现（复核点名为口径缺陷）。
    现在不变量拆成 ①a（只断无歧义形态：`&名字` 多长都是合法文字，必须原样出厂）与
    ①b（级联有界：与量词宽窄无关，数"剥了几段 `&名字`"），生成器于是可以自由产生命名
    实体运行。自由度本身有判据：每 6 个字符有一处**加权注入**的 `&` + 1~12 个名符（长度不
    封顶），用例断言这批切片里出现过 `&` + ≥9 个名符的运行 —— 封顶在 8 的旧生成器恒不可能。
    注入不是为了刁难实现：尾巴挂 >8 个名符时 `&[#A-Za-z0-9]{0,8}$` 按契约**不匹配**，那一整段
    必须原样出厂（刀一与刀二+同理），所以这批输入同时是"不许多剥"的正面样本。
    """
    chars = []
    while len(chars) < ln:
        if rnd.randrange(6) == 0:                    # ~1/6 的位置 = 一整段实体名运行
            chars.append("&")
            chars.extend(rnd.choice(_CAP_NAME_RUN_CHARS) for _ in range(rnd.randint(1, 12)))
        else:
            chars.append(rnd.choice(_CAP_ALPHA))
    return "".join(chars[:ln])


def test_cap_invariants_over_randomized_truncations():
    r"""属性用例：固定 seed 的随机截断上只断**不变量**，不比样例输出（NEW-3 把 ① 拆两条）。

    为什么必须补这一层：上一轮"输出与定点语义等价"这句话只有 2 条输入撑着，复核自己在
    3000 条随机截断里数出 24 条不等价（NEW-1 就是其中一族）—— 定点语义本身是参照物，
    而参照物不能写进判据（那等于把实现抄一遍），能写的是它的可判定后果：
      ①a 凡真被截断（len(text) > limit）者，输出不得以 `<[^>]*$` 结尾，也不得以任何
         **无歧义残缺实体**形态结尾：裸 `&$`、`&#` 加十进制、`&#x` 加十六进制
         （合成一条 `_CAP_UNAMBIG_TAIL_RE`）。这三类后面不可能再接着合法文字 ⇒ 剥它们
         零风险 ⇒ 契约保证一条都不剩。
         **不许**再写成 `&[#A-Za-z0-9]*$`：那种"名段也算残缺"的星号断言比契约的 `{0,8}` 强，
         上一版是靠生成器把名段封顶在 8 才把差异遮掉的（复核点名为口径缺陷）；NEW-3 起
         `A&B` 是必须原样出厂的合法文字，星号断言会转头指控契约本身。
      ①b 级联有界：数"实体名段"的差分 = **刀一剥完之后**那段尾部（`_CAP_DANGLING_TAG_RE`
         先在切片上把残缺标签片段减掉，再取 `sl2[len(got):]`），名段口径 `_CAP_NAME_SEG_RE`
         = `&` + 字母 + 至多 7 个名符，**至多 1 段**；>1 段 = 第二刀又剥了一次 `&名字`
         = 吃到了合法文字（NEW-3 的形状，本轮新增的级联变异死在这句上，实测 71 条）。
      ②  凡未被截断者，输出与输入逐字节相等（R14，未变）。

    两处差分口径必须先说清，否则 ①b 会去指控契约（都是实测，不是推理）：
      · **差分必须排除刀一**。`<[^>]*$` 能吃掉一枚 `<` 之后的全部字符，那里面天然有
        `&名字` 段（`&a&a<🐪\t//pp` 就是刀一的合法产物），照"切片与输出的差分"原式直接数段
        会在**正确实现**上报 140 条假红；先减掉残缺标签片段再数 → 0 条，级联变异照抓 71 条。
      · **名段口径不含 `#` 起头**。`&#12` 是 ①a 认定的无歧义形态、契约明许刀二+剥，把它算进
        名段就让 ①a 与 ①b 互相矛盾：`cap("x&#12&am", 7) == 'x'` 是合法输出，差分 `&#12&am`
        用 `&[A-Za-z#][A-Za-z0-9]{0,7}`（复核原式）数到 2 段。同一批 2800 条上实测：
        字母口径在正确实现上 0 条 / 级联实现 71 条；`[A-Za-z#]` 口径在正确实现上**也有 6 条假红**
        / 级联实现 102 条 —— 所以本轮把名段口径收成"字母起头"，两个口径的计数都进报告。
    规模：2400 条小 limit（1~48，长度落在 limit 前后）+ 400 条生产 limit（_BODY_CAP，
    随机尾巴正好压在切片边界上）。生成器**没有**再封顶实体名（见 `_cap_random_text`）。
    耗时阈值给 2 s（实测毫秒级，机器相关的脆阈值没意义）。
    """
    rnd = random.Random(20261004)
    n_small, n_scale = 2400, 400
    bad = []
    truncated = untouched = boundary_amp = 0
    cascaded = name_once = long_run = 0
    t0 = time.perf_counter()
    for i in range(n_small + n_scale):
        if i < n_small:
            limit = rnd.randint(1, 48)
            text = _cap_random_text(rnd, max(1, limit + rnd.randint(-6, 12)))
        else:
            limit = mod._BODY_CAP
            region = _cap_random_text(rnd, rnd.randint(2, 60))
            k = rnd.randint(1, len(region) - 1)      # 压在切片边界里的尾巴长度
            text = "y" * (limit - k) + region
        if "&" in text[max(0, limit - 9):limit]:
            boundary_amp += 1
        got = mod._cap_body(text, limit)
        if len(text) > limit:
            truncated += 1
            sl = text[:limit]
            # ①b 的差分：先按刀一的口径把尾部残缺标签片段减掉，剩下的才是实体刀吃过的区域。
            # 输出恒为该结果的前缀（两刀都只往左移切点），所以 `sl2[len(got):]` 就是实体差分。
            sl2 = _CAP_DANGLING_TAG_RE.sub("", sl)
            removed = sl2[len(got):]
            if _cap_longest_name_run(sl) >= 9:
                long_run += 1                        # 生成器真的自由（封顶版不可能出现）
            if removed.count("&") >= 2:
                cascaded += 1                        # 刀二及以后确实动过，①a 不是空转
            segs = len(_CAP_NAME_SEG_RE.findall(removed))
            if segs > 1:
                bad.append((i, limit, "CASCADE-%d" % segs, repr(text[-24:]), repr(got[-24:])))
            if segs == 1:
                name_once += 1
            if _CAP_DANGLING_TAG_RE.search(got) or _CAP_UNAMBIG_TAIL_RE.search(got):
                bad.append((i, limit, repr(text[-24:]), repr(got[-24:])))
        else:
            untouched += 1
            if got != text:
                bad.append((i, "UNTOUCHED-MOVED", repr(text[-24:]), repr(got[-24:])))
    dt = time.perf_counter() - t0
    # 覆盖率自检：每条路径都必须真的被跑到，否则这组不变量是空转出来的绿。
    # 阈值留了一半余量：这批输入由固定 seed 决定，计数与机器无关（实测 1927/873/1790/69/449/796）。
    assert truncated >= 1200, truncated
    assert untouched >= 300, untouched
    assert boundary_amp >= 100, boundary_amp
    assert cascaded >= 40, cascaded                # 刀二+ 的无歧义剥离确实发生过
    assert name_once >= 200, name_once             # ①b 的正样本：恰好剥 1 段名
    assert long_run >= 300, long_run               # 生成器自由：出现过 &+≥9 名符的运行
    assert not bad, "%d 条违反不变量（前 3 条）：%r" % (len(bad), bad[:3])
    assert dt < 2.0, "%.2fs" % dt


def test_cap_leaves_untruncated_body_byte_for_byte():
    r"""I7：没被截断（`len(text) <= limit`）就不许动刀 —— 旧版无条件收口：
    `Tom&Jerry`→`Tom`、`ends with <span`→`ends with`、尾部空白被顺手 rstrip。
    注意 `A&B&C` 这种"看着像残缺实体"的短正文也在被吃之列（旧版 while 循环一路剥成 `A`）。
    """
    for body in ("Tom&Jerry", "A&B&C", "ends with <span", "trailing spaces   ", "\n",
                 "&am", "x" * 10):
        assert mod._cap_body(body, 50000) == body, repr(body)
    assert mod._cap_body("Tom&Jerry") == "Tom&Jerry"      # 默认 limit 同样不许动
    assert mod._cap_body("x" * 50000) == "x" * 50000      # 恰好等于上限 = 没截断
    assert mod._cap_body("") == ""


def test_cap_default_limit_is_body_cap():
    """I3/V7：`_BODY_CAP` 和"走默认 limit"这条契约零判据（把常量改成 100 全绿）。
    Task 2 的三个出口调用的正是默认参数，两头都钉死：常量值 + 默认行为。
    """
    assert mod._BODY_CAP == 50000, mod._BODY_CAP
    body = "<p>" + "z" * 60000 + "</p>"
    got = mod._cap_body(body)                              # 不传 limit
    assert len(got) == 50000, len(got)
    assert got == body[:50000], repr(got[-12:])
    assert mod._cap_body(body, 100) == body[:100]          # 显式 limit 依旧生效


def test_cap_is_linear_on_degenerate_ampersand_runs():
    """M1：旧版 `while True` 收敛对 `&` 连排是二次方 —— 开工实测（本机）：
    `&`*50000 → 18.77 s（复核机器 14.24 s）、`A&`*25000 → 7.75 s。一条畸形 feed 就能拖住构建。
    阈值给宽松的 1 s（旧实现是它的 18 倍，新实现应当是毫秒级），不卡机器相关的脆阈值。

    NEW-3 改了 `A&` 那一族的**期望值**（旧实现一路剥成 'A'，那是级联吃合法文字）：切片 =
    `A&`*25000，刀一只剥尾部那枚裸 `&`（无歧义形态，权限内），刀二起切点尾巴是 `…A&A` 的
    `A` —— 既不是名段（额度也只有刀一能用）也不是无歧义形态 ⇒ 输出 49999 字符、以 `A` 收尾。
    配套把尾部断言从星号版 `&[#A-Za-z0-9]*$` 换成属性用例那两条（①a 无歧义形态 + ①b 级联
    有界）：`&A` 是必须留下的合法文字，星号断言会把它判成残缺；而旧实现输出 'A' 时星号断言
    反而"绿" —— 少剥/多剥两头都数不清，正是这一族要改成逐字节期望值的原因。
    """
    degenerate = (("&" * 50001, ""),                      # 全是裸 &：无歧义，剥到空
                  ("A&" * 25000 + "Z", "A&" * 24999 + "A"),   # NEW-3：只剥尾部裸 &，`&A` 留下
                  ("&amp;" * 10000 + "&am", "&amp;" * 10000))  # 完整实体不许被吃
    for body, want in degenerate:
        t0 = time.perf_counter()
        got = mod._cap_body(body, 50000)
        dt = time.perf_counter() - t0
        assert dt < 1.0, (len(body), "%.3fs" % dt)
        assert got == want, (len(body), repr(got[-20:]), repr(want[-20:]))
        assert len(got) <= 50000, len(got)
        assert not _CAP_UNAMBIG_TAIL_RE.search(got), repr(got[-20:])       # ①a
        removed = body[:50000][len(got):]
        assert len(_CAP_NAME_SEG_RE.findall(removed)) <= 1, repr(removed[-24:])  # ①b


def test_cap_counts_code_points_not_utf16_units():
    """emoji 是代理对：JS 的 slice 与 Python 的 len 口径不同，两边必须按码点截。"""
    body = "🐪" * 400 + "<p>tail</p>"
    assert mod._cap_body(body, 400) == "🐪" * 400


def test_normalized_survives_the_sanitizer():
    """规范化的全部意义：过完 _sanitize_html 后图还在。"""
    html = _n('<img data-src="https://mmbiz.qpic.cn/a.jpg">')
    assert 'src="https://mmbiz.qpic.cn/a.jpg"' in mod._sanitize_html(html)
