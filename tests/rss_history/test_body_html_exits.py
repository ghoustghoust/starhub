# tests/rss_history/test_body_html_exits.py
# -*- coding: utf-8 -*-
"""正文规范必须覆盖全部三个出口：解析入口、快照、历史重写。

为什么单独钉一条：`_apply_retention` 的注释（build_rss_aggregator.py:963）已经写清
"三个消费者共用同一个返回值"是**刻意设计**，而运行时 `?source=` 通道不吃产物要单独过闸。
本次同样有两条出口：`_save_api_snapshot`（写 rss_api_snapshot.json，api/rss.js 直接读）
和 `write_data_chunks`（chunk 通道字段名叫 full_content）。少接一条 = 一半条目仍是坏图，
而线上看起来"修过了"。

历史重写（`_renormalize_history_fulltext`）挡的是另一半：72h 窗口内的老条目不会被重新抓取，
不重写的话用户最长要再等 3 天才看到图回来。

I8（上一轮复核移交）另有一条独立的形状要挡：正文以**双重转义**形态到达时
（`&lt;img data-src=&quot;…&quot;&gt;`），"先规范化后 sanitize"这个顺序里规范化看不到任何标签，
原样返回，sanitize 再 unescape 出 `<img data-src=…>` —— 仍被整枚丢弃。⇒ 规范化必须建立在
"已解一层"的文本之上，且**只在检测到转义形态时**才解（无条件 unescape 会吃掉正文里
合法的 `&amp;lt;` 字面量，那是要展示代码的正文）。
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# R9：必须用**本目录**的 _loader（tests/rss_history/_loader.py，带 sys.modules 崩溃缓存）。
# 计划里写的 tests/rss_composite 那份每次调用都 exec_module，会把本目录其余 15 个判据的
# 缓存判定全部打掉 —— 实测 A2 门禁里模块 exec 次数从 3 涨到 69，且 build_rss_aggregator
# 顶层重挂 sys.stdout，同进程多次 exec 是 _loader.py 文档点名的 exit 127 硬崩来源。
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

WECHAT_BODY = '<p>正文</p><img data-src="https://mmbiz.qpic.cn/real.jpg">'
WX_LINK = "https://mp.weixin.qq.com/s/AbC123"
PROMOTED = '<img src="https://mmbiz.qpic.cn/real.jpg"'
# I8 的正形：整段被转义（wechat2rss 一类代理会把 content:encoded 再转义一层）。
WECHAT_BODY_ESCAPED = (
    "&lt;p&gt;正文&lt;/p&gt;"
    '&lt;img data-src=&quot;https://mmbiz.qpic.cn/real.jpg&quot;&gt;'
)
# I8 的反例：正文里**本来**就写着 `&lt;img&gt;` 这段字面量（展示代码用），不是被转义的正文。
LITERAL_CODE_SAMPLE = "示例写法：&amp;lt;img&amp;gt; 表示一张图。"


def _rss_items(body, link=WX_LINK, desc="摘要"):
    """走真解析器：造一个最小 RSS，断言 full_content 出厂即带 src。"""
    xml = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel>"
        "<title>t</title><link>https://mp.weixin.qq.com</link>"
        '<item><title>标题</title>'
        "<link>%s</link>"
        "<description>%s</description>"
        "<content:encoded><![CDATA[%s]]></content:encoded>"
        "<pubDate>Mon, 06 Oct 2025 08:00:00 +0800</pubDate>"
        "</item></channel></rss>" % (link, desc, body)
    )
    # xmlns:content 必须声明：计划原文没写，`ET.fromstring` 在"unbound prefix"上直接
    # ParseError，判据会以"解析失败"的名义假红（不是被测缺陷）。
    root = mod.ET.fromstring(xml)
    items = []
    mod._parse_rss_item(root.find("./channel/item"), "微信测试源", "wx_test", "info", items)
    return items


def _atom_items(body, link=WX_LINK):
    """走真解析器的 Atom 分支（_fetch_rss 里那条），_fetch_url 被打桩，不出网。"""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
        "<entry><title>标题</title>"
        '<link href="%s"/>'
        "<summary>摘要</summary>"
        "<content type=\"html\"><![CDATA[%s]]></content>"
        "<updated>2025-10-06T08:00:00+08:00</updated>"
        "</entry></feed>" % (link, body)
    ).encode("utf-8")
    source = {"name": "微信测试源", "key": "wx_atom_exit_test",
              "url": "https://example.invalid/feed", "cat": "info"}
    real = mod._fetch_url
    mod._fetch_url = lambda url, **kw: xml
    try:
        return mod._fetch_rss(source)
    finally:
        mod._fetch_url = real
        mod._rss_cache.pop(source["key"], None)


# ── 出口一：两个解析入口 ─────────────────────────────────────────

def test_rss_parse_entry_promotes_lazy_src():
    it = _rss_items(WECHAT_BODY)[0]
    assert PROMOTED in it["full_content"], it["full_content"]


def test_atom_parse_entry_promotes_lazy_src():
    items = _atom_items(WECHAT_BODY)
    assert len(items) == 1, items
    assert PROMOTED in items[0]["full_content"], items[0]["full_content"]


def test_rss_parse_entry_absolutizes_relative_img_src():
    body = '<p>正文</p><img src="/images/pic.png">'
    it = _rss_items(body, link="https://mp.weixin.qq.com/s/AbC123")[0]
    assert 'src="https://mp.weixin.qq.com/images/pic.png"' in it["full_content"], it["full_content"]


# ── 出口一（I8）：转义态正文 ────────────────────────────────────

def test_rss_parse_entry_fixes_escaped_wechat_body():
    it = _rss_items(WECHAT_BODY_ESCAPED)[0]
    assert PROMOTED in it["full_content"], repr(it["full_content"])
    assert "<p>" in it["full_content"], repr(it["full_content"])


def test_atom_parse_entry_fixes_escaped_wechat_body():
    items = _atom_items(WECHAT_BODY_ESCAPED)
    assert PROMOTED in items[0]["full_content"], repr(items[0]["full_content"])


def test_literal_escaped_code_sample_is_not_treated_as_escaped_body():
    """I8 反例：`&amp;lt;img&amp;gt;` 是**要展示的代码字面量**，不许被解成标签。

    判据方向：规范化这一步绝不能从它身上"提升"出任何 src —— 一旦解成真的 `<img>`，
    正文里就多出一枚不存在的图。
    """
    assert mod._unescape_escaped_body(LITERAL_CODE_SAMPLE) == LITERAL_CODE_SAMPLE, (
        "只有 &amp;lt;（双重转义字面量）也被解了一层 = 无条件 unescape，代码样例会被激活成标签")
    it = _rss_items(LITERAL_CODE_SAMPLE)[0]
    assert '<img src="https://mmbiz' not in it["full_content"], repr(it["full_content"])
    assert PROMOTED not in it["full_content"], repr(it["full_content"])


def test_mixed_body_fixes_escaped_img_and_keeps_literal_code_escaped():
    """混合体：真图是被转义的、字面量是双重转义的 —— 两者必须分别处置。"""
    body = WECHAT_BODY_ESCAPED + " " + LITERAL_CODE_SAMPLE
    it = _rss_items(body)[0]
    assert PROMOTED in it["full_content"], repr(it["full_content"])
    # 字面量那枚绝不允许拿到提升后的 src（那等于凭空造出一张文章里没有的图）
    assert it["full_content"].count('src="https://mmbiz.qpic.cn/real.jpg"') == 1, repr(it["full_content"])
    assert mod._unescape_escaped_body(body).count("&amp;lt;img&amp;gt;") == 0
    assert mod._unescape_escaped_body(body).count("&lt;img&gt;") == 1, (
        "解一层后字面量应恰好退到 `&lt;img&gt;`（仍是转义态），再多解就是激活")


# ── 出口二：快照 fc ─────────────────────────────────────────────

def _snapshot_fc(body, monkeypatch, tmp_path):
    src = {"key": "wx_test", "name": "微信测试源", "cat": "info", "tier": 2,
           "items": [{"title": "标题", "link": WX_LINK,
                      "summary": "摘要", "pub_date": "2025-10-06 08:00",
                      "full_content": body}]}
    monkeypatch.chdir(tmp_path)
    # _save_api_snapshot 写的是**相对**文件名（build_rss_aggregator.py:1038），
    # 所以 chdir 既拦住落点也不会覆盖仓库里那份真快照。别改成绝对路径写法。
    mod._save_api_snapshot([src])
    got = tmp_path / "rss_api_snapshot.json"
    assert got.exists(), (
        "快照没落盘 —— _save_api_snapshot 用 try/except 吞掉写入异常只 print，"
        "不在这里点名的话下一句 read_text 的 FileNotFoundError 会被当成'功能缺陷'看")
    payload = json.loads(got.read_text(encoding="utf-8"))
    return payload["sources"][0]["items"][0]["fc"]


def test_snapshot_exit_caps_without_dangling_tag(monkeypatch, tmp_path):
    """快照出口的 fc 必须既被 cap、又不在标签中间收尾。"""
    body = "<p>" + "z" * (mod._BODY_CAP - 30) + '</p><img src="https://e.com/long/a.jpg">'
    assert len(body) > mod._BODY_CAP, len(body)
    fc = _snapshot_fc(body, monkeypatch, tmp_path)
    assert len(fc) <= mod._BODY_CAP
    # 计划原文只断言 endswith((".jpg", '="htt'))：裸切片 fc[:50000] 的尾巴是
    # `<img src="https://e.co`，两条都不命中 ⇒ 接线前照样绿（假绿）。真不变量在下面两条。
    assert "<img" not in fc, repr(fc[-40:])
    assert fc.endswith("</p>"), repr(fc[-40:])


def test_snapshot_exit_caps_without_dangling_entity(monkeypatch, tmp_path):
    pad = "y" * (mod._BODY_CAP - 6)
    body = "<p>" + pad + "&amp;</p>"
    assert len(body) > mod._BODY_CAP, len(body)
    fc = _snapshot_fc(body, monkeypatch, tmp_path)
    assert len(fc) <= mod._BODY_CAP
    assert not fc.endswith("&am"), repr(fc[-40:])
    assert "&" not in fc[-8:], repr(fc[-8:])


def test_snapshot_exit_leaves_short_body_untouched(monkeypatch, tmp_path):
    body = '<p>正文</p><img src="https://e.com/a.jpg">'
    fc = _snapshot_fc(body, monkeypatch, tmp_path)
    assert fc == body, repr(fc)


# ── 出口三：历史重写 ────────────────────────────────────────────

def test_history_renorm_fixes_already_stored_items():
    hist = {
        "k1": {"link": WX_LINK, "full_content": WECHAT_BODY},
        "k2": {"link": "https://x.com/1", "full_content": "<p>无需处理</p>"},
        "k3": {"link": "https://x.com/2"},
    }
    changed = mod._renormalize_history_fulltext(hist)
    assert changed == 1, changed
    assert 'src="https://mmbiz.qpic.cn/real.jpg"' in hist["k1"]["full_content"]
    assert hist["k2"]["full_content"] == "<p>无需处理</p>"


def test_history_renorm_is_idempotent():
    hist = {"k": {"link": WX_LINK, "full_content": WECHAT_BODY}}
    mod._renormalize_history_fulltext(hist)
    once = dict(hist["k"])
    assert mod._renormalize_history_fulltext(hist) == 0, "第二遍又改了 = 规则不收敛"
    assert hist["k"]["full_content"] == once["full_content"]


# ── 接线本身（函数自己有测试 ≠ 构建时会调用它）─────────────────

# 解析入口的调用链：unescape 在最里层、normalize 紧贴 sanitize 之前、deep_clean 在最外。
_ENTRY_CHAIN_RE = re.compile(
    r"_deep_clean_html\(\s*_sanitize_html\(\s*_normalize_body_html\(\s*"
    r"_unescape_escaped_body\(\s*content_raw\s*\)\s*,\s*link\s*\)\s*\)\s*\)")


def _build_source():
    p = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    return io.open(p, encoding="utf-8").read()


def _func_source(name):
    import inspect
    return inspect.getsource(getattr(mod, name))


def test_both_parse_entries_are_wired():
    for fn in ("_fetch_rss", "_parse_rss_item"):
        assert _ENTRY_CHAIN_RE.search(_func_source(fn)), (
            "%s 的正文链路没接规范化（或顺序倒了/漏了 I8 的 unescape 前置）" % fn)


def test_normalize_runs_before_sanitize():
    """顺序倒了等于没修（_sanitize_html 只认 src，无 src 的 img 整枚丢弃）。"""
    src = _build_source()
    assert "_sanitize_html(_normalize_body_html" in src, "规范化必须在 sanitize **之内**（先跑）"
    assert "_normalize_body_html(_sanitize_html" not in src, "规范化跑在 sanitize 之后 = 什么都没修"


def test_snapshot_exit_is_wired():
    assert re.search(
        r"_strip_oss_signature\(\s*_cap_body\(\s*fc\s*\)\s*\)", _func_source("_save_api_snapshot")), (
        "快照出口的 fc 还在用裸切片 fc[:50000]（会截出半个标签）")


def test_history_renorm_is_wired_into_the_build():
    """与 tests/rss_history/test_gate_wiring.py 同一条理由：'已接线'也会静默失效。"""
    src = _build_source()
    call = "_hist_normed = _renormalize_history_fulltext(_rss_history)"
    assert src.count(call) == 1, "历史重写没在主流程里调用（或被复制成两处）"
    anchor = "_hist_purged = _purge_bad_covers_in_history(_rss_history)"
    assert anchor in src and 0 <= src.find(anchor) < src.find(call), \
        "重写没紧跟封面 purger —— 两个历史清扫分头落点，下次只会改到一个"
