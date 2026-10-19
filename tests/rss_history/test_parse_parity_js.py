# tests/rss_history/test_parse_parity_js.py
# -*- coding: utf-8 -*-
"""`api/rss.js` 的解析 helper 必须与 Python 侧 `_parse_rss_item` 判得一模一样。

为什么单独一条判据（而不是"看起来一样"）：72h 契约与"日期不许伪装"这两件事各有两个执行点 ——
构建期写产物、运行时 `api/rss.js` 自己抓上游。2026-09-21 同一天里，
"guid 即链接"和"无日期条目降级收录时刻"两处修复都只落在 Python 侧，
JS 侧靠人记得同步是不可靠的（台账 §20 记的就是这次差点漏掉）。

做法：从 api/rss.js 里切出 helper 源码，用 node 跑同一组 fixture，
再与生产代码 mod._parse_rss_item 的判决逐条比对。
不用 require('api/rss.js')：那是 Vercel 入口模块，加载即带网络与 env 依赖。
"""
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()
API_RSS = os.path.join(ROOT, "api", "rss.js")
LIB_COVER = os.path.join(ROOT, "lib", "rss_cover.js")


def _cover_prelude():
    """parseFeed 现在会调 COVER.pickItemImage ⇒ 切片跑法必须先把 COVER 注进同一作用域。

    少这一句就是 ReferenceError 让判据"红得没有道理"，与 `_js_media_src` 注释里
    "只切 helper 会让 cleanLink 未定义"是同一条教训的第二个实例。
    """
    return "const COVER=require(%r);\n" % LIB_COVER

FIXTURES = [
    # (名字, RSS2 item 片段, 期望：link 是否取到 guid)
    ("guid_only", u'<item><guid>https://www.anquanke.com/post/id/316124</guid>'
                  u'<title>t</title><pubDate>2026-09-19 10:57:40</pubDate></item>', True),
    ("numeric_guid", u'<item><guid>12345</guid><title>t</title></item>', False),
    ("permalink_false", u'<item><guid isPermaLink="false">https://x.example/a</guid>'
                        u'<title>t</title></item>', False),
    ("plain_link", u'<item><link>https://ok.example/a</link><guid>https://other.example/b</guid>'
                   u'<title>t</title></item>', None),
    ("no_date", u'<item><link>https://ok.example/a</link><title>t</title></item>', None),
    # ── 线上实测的三种 link 形状（2026-09-21，JS 出口 3 个源 100% 畸形、Python 出口 9,067 条零畸形）
    # 这三条不是"顺手加固"：链接决定卡片点哪儿、决定抽屉合并能不能认出"同一篇"，
    # 两侧不一致就是同一篇文章两个身份。
    ("cdata_link", u'<item><link><![CDATA[https://toi.example/b/a?photostory=7507755]]></link>'
                   u'<title>t</title></item>', None),
    ("amp_entities", u'<item><link>https://bbc.example/n/cw4gm7l742dmo&amp;at_campaign=rss'
                     u'&amp;at_medium=rss</link><title>t</title></item>', None),
    # 实体解码只许走一趟：`&amp;amp;` 的正确结果是 `&amp;`，先把 &amp; 解掉就会得到 `&`。
    # 两侧实测一致（Python 与 JS 都吐 `...u=1&amp;keep=1`），所以这条能当判据；
    # 它专门用来挡"把 &amp; 放到最前面解"那种等价改写。
    ("double_encoded_amp", u'<item><link>https://x.example/a?u=1&amp;amp;keep=1</link>'
                           u'<title>t</title></item>', None),
    ("hash_fragment_kept", u'<item><link>https://spiegel.example/x/y.html#ref=rss</link>'
                           u'<title>t</title></item>', None),
    ("padded_link", u'<item><link>  https://ok.example/a  </link><title>t</title></item>', None),
]


def _node():
    for c in ("node", "node.exe"):
        try:
            r = subprocess.run([c, "-v"], capture_output=True)
        except Exception:
            continue
        if r.returncode == 0:
            return c
    return None


def _js_helper_src():
    text = open(API_RSS, encoding="utf-8").read()
    a = text.find("function extractTag(")
    b = text.find("function stripHtml(")
    assert a > 0 and b > a, "api/rss.js 里找不到 extractTag..stripHtml 区间，helper 结构变了"
    return text[a:b]


@pytest.mark.parametrize("name,frag,expect_guid", [tuple(f) for f in FIXTURES])
def test_js_matches_python_link_verdict(name, frag, expect_guid):
    node = _node()
    if not node:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    script = (
        "const s=%r;\n" % _js_helper_src() +
        "eval(s);\n"
        "const f=%s;\n" % json.dumps([f[1] for f in FIXTURES], ensure_ascii=False) +
        "const r=f.map(x=>linkOrPermaId(x,'link','guid'));\n"
        "process.stdout.write(JSON.stringify(r));\n")
    out = subprocess.run([node, "-e", script], capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:300]
    js_links = json.loads(out.stdout.decode("utf-8"))
    i = [f[0] for f in FIXTURES].index(name)
    js = js_links[i]

    py_out = []
    mod._parse_rss_item(ET.fromstring(frag), "S", "s_1", "security", py_out)
    py = (py_out[0]["link"] if py_out else "")
    assert js == py, "%s：JS 判成 %r，Python 判成 %r —— 两侧口径又分叉了" % (name, js, py)
    if expect_guid is True:
        assert py.startswith("https://"), "%s 应当从 guid 取到链接" % name
    if expect_guid is False:
        assert py == "", "%s 不该把非永久链接的 guid 当链接用" % name


def test_js_undated_item_is_marked_not_blank():
    """JS 侧缺日期时必须给收录时刻并打标；空串会让卡片没有时间，不打标就是伪装发布时间。"""
    node = _node()
    if not node:
        pytest.skip("本机没有 node")
    script = ("const s=%r;\n" % _js_helper_src() + "eval(s);\n"
              "const a=datedOrCapture(''),b=datedOrCapture('2026-09-19 10:57:40');\n"
              "process.stdout.write(JSON.stringify([a.pub_date,a.date_fallback,b.pub_date,b.date_fallback]));\n")
    out = subprocess.run([node, "-e", script], capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:300]
    pd_a, fb_a, pd_b, fb_b = json.loads(out.stdout.decode("utf-8"))
    assert pd_a and fb_a is True, "无日期条目没拿到降级值或没打标：%r" % (pd_a, fb_a)
    assert pd_b == "2026-09-19 10:57:40" and not fb_b, "有日期条目被改写了：%r" % (pd_b, fb_b)


def _js_dedup_src():
    """切出 JS 的 dedupSourceItems（Pass 1 URL 归一化）源码。"""
    text = open(API_RSS, encoding="utf-8").read()
    a = text.find("function dedupSourceItems(")
    assert a > 0, "api/rss.js 里找不到 dedupSourceItems，函数名变了"
    depth, i = 0, text.index("{", a)
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[a:j + 1]
    raise AssertionError("dedupSourceItems 大括号没配平")


# 两侧都必须把"同一篇文章的 URL 变体"归一到同一个键：
# Python 的 Pass 1 有 `#(?!reply\d+$)[^#]*$` 这一刀（剥尾部 fragment），
# JS 侧历史上漏了它 —— 线上实测 der_spiegel_783 的 30/30 条带 `#ref=rss`，
# 与构建期产物的同一篇文章链接不相等 ⇒ 抽屉按 link 认不出旧条目，合并退化成整条替换。
LINK_CASES = [
    ("spiegel_ref", "https://www.spiegel.de/a/b-a-1234#ref=rss",
     "https://www.spiegel.de/a/b-a-1234"),
    ("v2ex_reply_kept", "https://www.v2ex.com/t/123#reply5",
     "https://www.v2ex.com/t/123#reply5"),
    ("utm_stripped", "https://x.example/p?utm_source=rss&at_campaign=rss",
     "https://x.example/p"),
    ("plain", "https://x.example/q", "https://x.example/q"),
]


@pytest.mark.parametrize("name,src,want", LINK_CASES)
def test_url_normalization_matches_python(name, src, want):
    node = _node()
    if not node:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    # Python 侧：走真正的 Pass 1（两条同类条目才能触发归一化后的写回）
    import copy
    items = [{"link": src, "title": "T1", "summary": ""},
             {"link": src + "-other", "title": "T2", "summary": ""}]
    got_py = mod._dedup_source_items(copy.deepcopy(items), "s_x")[0]["link"]
    script = (
        "const s=%r;\n" % _js_dedup_src() + "eval(s);\n"
        "const a=[{link:%r,title:'T1',summary:''},{link:%r,title:'T2',summary:''}];\n"
        "process.stdout.write(JSON.stringify(dedupSourceItems(a,'s_x')[0].link));\n"
        % (src, src + "-other"))
    out = subprocess.run([node, "-e", script], capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:300]
    got_js = json.loads(out.stdout.decode("utf-8"))
    assert got_py == want, "Python 把 %r 归一成 %r（期望 %r）" % (src, got_py, want)
    assert got_js == want, (
        "%s：JS 归一成 %r，Python 归一成 %r ⇒ 两条通道对同一篇文章给出不同链接，"
        "抽屉合并认不出旧条目" % (name, got_js, got_py))


def test_both_rss_paths_use_the_helpers():
    """两条解析路径**各自**必须走 helper —— 用整句源码锁，不用"附近出现过"。

    这条判据改过两次，前两版都是假的：
      v1 `text.count("linkOrPermaId(") >= 2` —— helper 定义自身占一次，
         变异体把 Atom 路径改回 extractTag 照样绿；
      v2 "从循环起点往后截 1200 字符，段内含 linkOrPermaId" —— 窗口会溢出到另一条
         路径的代码，同一个变异体**仍然绿**（我实测过两次才承认它无效）。
    现在锁具体语句：每条路径那一行必须原样存在，被改坏就立刻找不到；
    日期兜底则要求两条路径各出现一次（count==2，多了少了都说明接线动过）。
    """
    text = open(API_RSS, encoding="utf-8").read()
    assert "linkOrPermaId(item, 'link', 'guid')" in text, \
        "RSS2 路径没走 guid 回退：guid 即链接的源（如安全客）在运行时侧仍是空链接"
    assert "linkOrPermaId(entry, 'link', 'id')" in text, \
        "Atom 路径没走 guid/id 回退：只修一条等于没修（台账 §20 的原始事故形态）"
    assert text.count("const _dt = datedOrCapture(pubDate);") == 2, \
        "收录时刻兜底必须两条路径各一次，实际 %d 次" % text.count("const _dt = datedOrCapture(pubDate);")
    assert "pub_date: pubDate || ''" not in text, "还有路径把缺日期直接出厂成空串"


# ── 媒体地址（mu/mt）的两通道一致性 ──
# Python 侧 `_pick_item_media` 读的是 **ET 解析后的属性**，`&amp;` 早在 XML 层就解成 `&`；
# JS 侧用正则从原文里抠 `url="..."`，抠出来的是**原始实体**。同一集音频因此两条通道
# 长得不一样（`...mp3&amp;sig=1` vs `...mp3&sig=1`），后者直接喂给 <audio src> 是打不开的地址。
# 这条判据是第四轮对抗审查 grep 出来的（任务 #38），与 link 走的是同一个漏网点：
# `linkOrPermaId` 修过之后，媒体 url 仍然没接 cleanLink。
MEDIA_CASES = [
    ("enclosure_amp",
     '<item><title>t</title><enclosure url="https://m.test/a.mp3&amp;sig=1" type="audio/mpeg"/></item>'),
    ("media_content_amp",
     '<item xmlns:media="http://search.yahoo.com/mrss/"><title>t</title>'
     '<media:content url="https://m.test/b.mp4?a=1&amp;b=2" medium="video"/></item>'),
]


def _js_media_src():
    """必须从 extractTag 起切：cleanLink 定义在那之前，只切 extractMediaFromEntry
    会让 eval 里的 cleanLink 未定义 ⇒ 变异体因 ReferenceError 而"红"，是假阳性。"""
    text = open(API_RSS, encoding="utf-8").read()
    a = text.find("function extractTag(")
    b = text.find("function parseFeed(")
    assert 0 < a < b, "api/rss.js 里找不到 extractTag..parseFeed 区段"
    seg = text[a:b]
    assert "function cleanLink(" in seg, "切出的区段里没有 cleanLink，判据会退化成假红"
    return seg


def _js_pipeline_src_for_media():
    """完整链路要含 parseFeed 本体：切到 `async function fetchOne(` 为止。"""
    text = open(API_RSS, encoding="utf-8").read()
    a = text.find("function extractTag(")
    b = text.find("async function fetchOne(")
    assert 0 < a < b, "api/rss.js 里找不到 extractTag..fetchOne 区段"
    seg = text[a:b]
    assert "function parseFeed(" in seg and "function cleanLink(" in seg, "区段缺 parseFeed 或 cleanLink"
    return seg


@pytest.mark.parametrize("name,frag", MEDIA_CASES)
def test_media_url_matches_python(name, frag):
    node = _node()
    if not node:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    py_url, py_type = mod._pick_item_media(ET.fromstring(frag))
    assert py_url, "%s：Python 侧没取到媒体地址，fixture 与生产不同形，先修 fixture" % name
    script = ("const s=%r;\neval(s);\nconst m=extractMediaFromEntry(%s);\n"
              "process.stdout.write(JSON.stringify([m&&m.media_url||'',m&&m.media_type||'']));\n"
              % (_js_media_src(), json.dumps(frag)))
    out = subprocess.run([node, "-e", script], capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:300]
    js_url, js_type = json.loads(out.stdout.decode("utf-8"))
    assert js_url == py_url, (
        "%s：媒体地址 JS 侧 %r / Python 侧 %r —— 实体没解，抽屉里的音频/视频是打不开的链接，"
        "且与构建产物不是同一篇的同一个媒体" % (name, js_url, py_url))
    assert js_type == py_type, "%s：媒体类型 JS %r / Python %r" % (name, js_type, py_type)


# RSS2 分支不走 extractMediaFromEntry，它在 parseFeed 里自己抠 enclosure（api/rss.js:489），
# 所以只判 helper 会漏掉这一处 —— 上面那条对这条变异体是绿的（"红不起来"），
# 这条按完整链路补上：真 parseFeed vs 真 _parse_rss_item。
def test_rss_enclosure_media_url_matches_python_end_to_end():
    node = _node()
    if not node:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    item = ('<item><title>ep 1</title><link>https://pod.test/e1</link>'
            '<enclosure url="https://cdn.test/e1.mp3?a=1&amp;sig=2" type="audio/mpeg"/>'
            '<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item>')
    py_out = []
    mod._parse_rss_item(ET.fromstring(item), "S", "s_1", "security", py_out)
    assert py_out, "Python 侧没解析出条目，fixture 与生产不同形"
    py_url = py_out[0].get("media_url", "")
    assert py_url, "Python 侧没取到 enclosure：%s" % py_out[0].keys()

    xml = '<?xml version="1.0"?><rss><channel>%s</channel></rss>' % item
    script = (_cover_prelude() + "const s=%r;\neval(s);\nconst its=parseFeed(%s,'s_1',50);\n"
              "process.stdout.write(JSON.stringify(its[0]&&its[0].media_url||''));\n"
              % (_js_pipeline_src_for_media(), json.dumps(xml)))
    out = subprocess.run([node, "-e", script], capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:300]
    js_url = json.loads(out.stdout.decode("utf-8"))
    assert js_url == py_url, (
        "RSS enclosure：JS %r / Python %r —— parseFeed 内联那一处没解实体，"
        "音频地址带 &amp; 时两条通道给出不同的可播地址" % (js_url, py_url))


