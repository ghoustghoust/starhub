# tests/site_nav/test_reader_hn_discussion.py
# -*- coding: utf-8 -*-
"""news.ycombinator.com 的条目没有"正文"可抽：那是评论列表页。

现场证据（用户 2026-10-04 记录）：Ask/Show HN 文本帖的 `link` 就指向讨论页，
`api/article.js` 对它跑 Readability ⇒ 抽出的是"用户名 + 评论锚点链接"列表，
用户在阅读器里看到的"全是链接"。抓失败时还会叠上 Task 6 之前那套静默。
裁定：讨论页直接显示信源摘要 + 一个"在 Hacker News 查看讨论"的出口，不发 /api/article。

为什么判据一半跑**产物**而不是只 grep 源码（Task 7 同一教训）：
  ① 讨论页判定必须是**按 host 边界**的。源码 grep 只能看见"没写 indexOf(u)>=0"，
     看不见 `s.indexOf('news.ycombinator.com')>=0` 这种照样会被
     `http://evil.com/?x=news.ycombinator.com` 冒充的形状（与
     tests/site_nav/test_orphan_requests.py 里 caifuzhongwen 那条同形状）⇒ 拿真函数在
     node 里跑冒充表。
  ② 这段 JS 活在非 raw 的 Python 三引号串里，`\\n` 单写会被 Python 先解成真换行，
     只有产物才暴露 ⇒ 换行转 <br> 的判据也在产物层读。
"""
import io
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
_ARTIFACT = {}


def _src():
    with io.open(BUILD, encoding="utf-8") as fh:
        return fh.read().replace("\\'", "'")


def _artifact():
    """产物（Python 解过一道转义之后的 JS）：判据跑的是真要上线的那段代码。"""
    if "html" not in _ARTIFACT:
        import sys
        sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
        from _loader import load_build
        mod = load_build()
        _ARTIFACT["html"] = mod.build_html([], "2026-10-01 08:20", 0, 0, analysis_data=None,
                                           diverse_window_minutes=120, diverse_enabled=True)
    return _ARTIFACT["html"]


def _js_func(text, head):
    """按大括号配平切函数源码（本区既有写法：函数名 → 函数名，绝不写死行号）。"""
    a = text.find(head)
    assert a >= 0, "产物里找不到 %r：讨论页短路被删了或改名了" % head
    i = text.index("{", a)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[a:j + 1]
    raise AssertionError("%s 的大括号没配平" % head)


def _node():
    n = shutil.which("node")
    if not n:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    return n


def _run(tmp_path, name, code, driver):
    runner = tmp_path / name
    runner.write_text(
        "var CODE = %s;\n"
        "eval(CODE);   /* 非严格 direct eval：函数落到本作用域，与 parseFeed 判据同手法 */\n"
        "%s\n"
        "process.stdout.write(JSON.stringify(OUT));\n"
        % (json.dumps(code), driver),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "node 跑讨论页短路崩了：\n%s\n%s" % (
        (r.stdout or "")[-800:], (r.stderr or "")[-1500:])
    return json.loads(r.stdout)


# ───────────────────────── 判据 1-3：brief 的三条（锚点按形状）─────────────────────────

def test_discussion_pages_short_circuit_before_fetching():
    src = _src()
    i = src.find("function fetchFullArticle(a){")
    assert i >= 0, "找不到 fetchFullArticle：阅读器正文抓取被改名了（判据前提失效）"
    body = src[i:src.find("/* ── 媒体嵌入", i)]
    assert body, "fetchFullArticle 的结束锚点 `/* ── 媒体嵌入` 找不到了（判据前提失效）"
    j = body.find("_isHnDiscussion")
    assert j >= 0, "阅读器仍对 HN 讨论页跑 Readability（抽出来是评论链接列表）"
    k = body.find("apiBase+'?url=")
    assert k < 0 or j < k, "_isHnDiscussion 的判断在 fetch 之后，等于白判"


def test_hn_host_match_is_exact_or_subdomain():
    """子串匹配会让 `http://evil.com/?x=news.ycombinator.com` 冒充命中
    —— 与 tests/site_nav/test_orphan_requests.py 里 caifuzhongwen 那条同形状。"""
    src = _src()
    a = src.find("function _isHnDiscussion")
    assert a >= 0, "找不到 function _isHnDiscussion：讨论页判定被删了或改名了"
    fn = _js_func(src, "function _isHnDiscussion")
    assert "indexOf(u)>=0" not in fn.replace(" ", ""), "讨论页判定仍是整串子匹配"
    assert re.search(r"\.indexOf\(['\"]\.", fn) or re.search(r"hostname", fn), \
        "没有按 host 边界匹配的实现痕迹"


def test_summary_is_shown_as_body_with_discussion_link():
    src = _src()
    assert "_showSummaryAsBody(" in src, "讨论页没有可显示的正文路径"
    assert re.search(r"news\.ycombinator\.com.*target=|查看讨论", src), \
        "缺少指向讨论页的出口"


# ───────────────────── 判据 4：冒充表（真产物函数在 node 里跑）─────────────────────

HN_TABLE = [
    # (URL, 期望)
    ("https://news.ycombinator.com/item?id=49916751", True),
    ("http://news.ycombinator.com/item?id=1", True),
    ("https://NEWS.YCOMBINATOR.COM/item?id=2", True),
    ("https://n.news.ycombinator.com/item?id=3", True),          # 子域
    ("http://evil.com/?x=news.ycombinator.com", False),           # query 里塞域名
    ("https://news.ycombinator.com.evil.com/item?id=4", False),   # 后缀冒充
    ("https://evil.com/#news.ycombinator.com", False),            # fragment
    ("https://news.ycombinator.com@evil.com/item", False),        # userinfo 冒充
    ("https://notnews.ycombinator.com/", False),                  # 前缀粘连
    ("https://blog.example.com/post", False),
    ("", False),
    ("#", False),
    ("javascript:alert(1)", False),
]


def test_isHnDiscussion_host_boundary_table(tmp_path):
    art = _artifact()
    code = _js_func(art, "function _isHnDiscussion(u){")
    driver = ("var samples=%s;\n"
              "var OUT=[];\n"
              "for(var i=0;i<samples.length;i++){ OUT.push({u:samples[i][0], got:!!_isHnDiscussion(samples[i][0])}); }\n"
              % json.dumps(HN_TABLE, ensure_ascii=False))
    got = _run(tmp_path, "is_hn.js", code, driver)
    assert len(got) == len(HN_TABLE), got
    bad = [(u, exp, g["got"]) for (u, exp), g in zip(HN_TABLE, got) if bool(g["got"]) is not exp]
    assert not bad, "讨论页判定按 host 边界判错了（%s）—— 整串子匹配就是这几条会漏" % "; ".join(
        "%r 应为 %s、实得 %s" % (u, exp, g) for u, exp, g in bad)


# ─────────────── 判据 5：摘要当正文显示时真转义（产物函数 + 最小 DOM 桩）───────────────

SUMMARY_SAMPLES = [
    {"s": "<img src=x onerror=alert(1)>积分噪声", "u": "https://news.ycombinator.com/item?id=9"},
    {"s": "第一段\n第二段", "u": "https://news.ycombinator.com/item?id=10"},
    {"s": "", "u": "https://news.ycombinator.com/item?id=11"},
    {"s": "正常正文", "u": 'https://x.test/?a=1" onclick="alert(1)&b=2'},
]


def _show_summary_htmls(tmp_path):
    art = _artifact()
    code = "\n".join([_js_func(art, "function esc(s){"),
                      _js_func(art, "function _isHnDiscussion(u){"),
                      _js_func(art, "function _showSummaryAsBody(inner,a){")])
    driver = (
        "function mkEl(){ var e={_nodes:[],_html:'',className:'',style:{},\n"
        "  appendChild:function(n){ this._nodes.push(n); },\n"
        "  querySelector:function(){ return null; },\n"
        "  insertBefore:function(n){ this._nodes.push(n); return n; }};\n"
        "  Object.defineProperty(e,'innerHTML',{get:function(){\n"
        "    return this._nodes.map(function(t){return String(t._text);}).join('')\n"
        "      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');},\n"
        "    set:function(v){ this._html=String(v); }});\n"
        "  return e; }\n"
        "global.document={createElement:function(){ return mkEl(); },\n"
        "  createTextNode:function(t){ return {_text:t}; }};\n"
        "var samples=%s;\n"
        "var OUT=[];\n"
        "for(var i=0;i<samples.length;i++){ var inner=mkEl(); inner.appendChild=mkEl().appendChild;\n"
        "  inner.querySelectorAll=[]; _showSummaryAsBody(inner,samples[i]);\n"
        "  var pushed=null;\n"
        "  for(var q=0;q<inner._nodes.length;q++){ if(inner._nodes[q]&&inner._nodes[q]._html!==undefined){pushed=inner._nodes[q];break;} }\n"
        "  OUT.push({html:pushed?pushed._html:'', appended:inner._nodes.length}); }\n"
        % json.dumps(SUMMARY_SAMPLES, ensure_ascii=False))
    return _run(tmp_path, "show_summary.js", code, driver)


def test_summary_shown_as_body_is_escaped(tmp_path):
    out = _show_summary_htmls(tmp_path)
    assert len(out) == len(SUMMARY_SAMPLES), out
    h0 = out[0]["html"]
    assert "<img src=x" not in h0, "摘要里的标签没转义就进了 innerHTML：%r" % h0
    assert "&lt;img" in h0, "摘要没有按文本转义（应出现 &lt;）：%r" % h0
    assert "查看讨论" in h0, "讨论页正文里没有可点的讨论入口：%r" % h0
    h3 = out[3]["html"]
    assert 'onclick=&quot;alert' in h3 or '" onclick="' not in h3, \
        "href 属性里的引号没转义，可以 breakout：%r" % h3
    assert "&amp;b=2" in h3, "URL 里的 & 没转义（属性上下文同样要转）：%r" % h3


def test_missing_summary_still_renders_a_placeholder(tmp_path):
    out = _show_summary_htmls(tmp_path)
    h2 = out[2]["html"]
    assert "无文字内容" in h2, "空摘要既没有正文也没有占位：%r" % h2
    assert "item?id=11" in h2, "占位分支没给讨论入口：%r" % h2


def test_summary_newlines_become_br_in_the_artifact(tmp_path):
    """换行必须是 `<br>`，且 `\\n` 得原样落到产物里（Python 先解一道就是真换行）。"""
    fn = _js_func(_artifact(), "function _showSummaryAsBody(inner,a){")
    assert "<br>" in fn, "摘要换行没转成 <br>：多段正文挤成一坨"
    m = re.search(r"replace\(/\s*(.{0,4}?)\s*/g", fn)
    assert m, "找不到换行替换的正则（判据前提失效）：%s" % fn[:400]
    assert m.group(1) == "\\n", "换行正则不是 \\n 两字符（Python 把它吃成真换行了）：%r" % m.group(1)
    out = _show_summary_htmls(tmp_path)
    assert "第一段<br>第二段" in out[1]["html"], "node 实跑换行没变 <br>：%r" % out[1]["html"]


# ─────────────── 判据 6-7：函数定义了必须被调用、且不许吞掉真全文 ───────────────

def test_show_summary_as_body_is_actually_called():
    src = _src()
    i = src.find("function fetchFullArticle(a){")
    assert i >= 0, "找不到 fetchFullArticle（判据前提失效）"
    ft = src[i:src.find("/* ── 媒体嵌入", i)]
    assert re.search(r"_isHnDiscussion\(\s*a\.u\s*\)", ft), \
        "_isHnDiscussion 定义了却没在 fetchFullArticle 里按 a.u 调用"
    assert re.search(r"_showSummaryAsBody\(\s*inner\s*,\s*a\s*\)\s*;\s*return", ft), \
        "_showSummaryAsBody 定义了却没被调用（或调用了没 return，仍会去 fetch）"
    d = _js_func(src, "function _showSummaryAsBody")
    assert d.strip(), "_showSummaryAsBody 是空函数"
    assert "innerHTML" in d, "_showSummaryAsBody 没写任何用户可见内容（空壳）"


def test_real_fulltext_still_wins_over_the_discussion_branch():
    """有真全文（a.fc）时先显示全文，讨论页短路排在它之后、fetch 之前。

    这条钉住一个会把修复做成回退的写法：把 guard 放到函数最前面，将来 HN 条目带上
    fc 时，正文会被摘要顶掉。
    """
    src = _src()
    i = src.find("function fetchFullArticle(a){")
    ft = src[i:src.find("/* ── 媒体嵌入", i)]
    g = ft.find("_isHnDiscussion")
    fc = ft.find("if(a.fc&&a.fc.length>100)")
    fetch = ft.find("apiBase+'?url=")
    assert fc >= 0 and g >= 0, "fetchFullArticle 的 fc 分支或讨论页 guard 形状变了（判据前提失效）"
    assert fc < g, "讨论页 guard 抢在了真全文之前：%d > %d" % (g, fc)
    assert g < fetch, "讨论页 guard 在 fetch 之后，等于白判"
