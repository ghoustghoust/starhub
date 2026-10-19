# -*- coding: utf-8 -*-
"""正文里的图片要按封面同一套规则兜底：referrer 策略、破图隐藏、老快照的 data-src。

为什么不新建规则：`_REF_HOSTS`/`_BAD_COVER_HOSTS` 已经是全站唯一的一份表，
而且判空/放行两边分叉过（tests/rss_cover/test_realtime_cover_js.py 的 docstring 记着
`HTTPS://ICHEF…` 那次）。正文图片再抄一份表 = 第 4 个事实来源。

为什么还要在渲染层做一遍：构建期规范（Task 2）管得到出厂的 fc，但
  ① `/api/article` 现抓的正文压根不过构建期；
  ② 快照与 chunk 里 72h 前入库的条目由历史重写覆盖，实时 `?source=` 通道不覆盖；
  ③ Readability 的输出格式完全由站点决定。
这三条都在浏览器落地，所以渲染层必须还有一道；它只改属性，不改内容结构。

实测缺口（task-4-5-report.md 的 L1）：双重转义形态的图在运行时通道服务端就拿不回来，
`normalizeBodyHtml` 看不到 `&lt;img data-src=…&gt;` —— 只能在渲染层救。所以本文件的
行为判据一律拿**产物里的真函数**（`_hardenBodyImages` + `_coverRefPolicy` + `_hostInTable`
+ `_coverHost` + `_REF_HOSTS`）在 node 里跑，输入是运行时通道形状（只有 data-src 的 img）。
零 npm 依赖：只用 node 自带的全局（URL）+ 手写的最小 DOM 桩。
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
    """产物（Python 解过一道转义之后的 JS）—— 判据跑的是真要上线的那段代码。"""
    if "html" not in _ARTIFACT:
        import sys
        sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
        from _loader import load_build
        mod = load_build()
        _ARTIFACT["html"] = mod.build_html([], "2026-10-01 08:20", 0, 0, analysis_data=None,
                                           diverse_window_minutes=120, diverse_enabled=True)
    return _ARTIFACT["html"]


def _js_func(text, head):
    """按大括号配平切函数源码（这些函数体里没有字符串大括号，配平可靠）。"""
    a = text.find(head)
    assert a >= 0, "产物里找不到 %r：渲染层兜底被删了或改名了" % head
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


# 真产物兜底：把渲染层函数和它复用的那份封面表一起搬到 node 里跑。
def _harden(tmp_path, samples):
    art = _artifact()
    m = re.search(r"var _REF_HOSTS\s*=\s*\[[^\]]*\];", art)
    assert m, "产物里读不到 _REF_HOSTS（封面那份表）"
    code = "\n".join([m.group(0),
                      _js_func(art, "function _coverHost(u){"),
                      _js_func(art, "function _hostInTable(host, table){"),
                      _js_func(art, "function _coverRefPolicy(u){"),
                      _js_func(art, "function _hardenBodyImages(root,baseUrl){")])
    runner = tmp_path / "harden_body_images.js"
    runner.write_text(
        "var CODE = %s;\n"
        "eval(CODE);   /* 非严格 direct eval：函数与 var 落到本作用域，parseFeed 判据同手法 */\n"
        "function mkImg(attrs){\n"
        "  var im={attrs:{},setLog:[],rmLog:[],handlers:{},style:{},removed:false};\n"
        "  for(var k in attrs){ im.attrs[k]=attrs[k]; }\n"
        "  im.getAttribute=function(n){ return (n in im.attrs)?String(im.attrs[n]):null; };\n"
        "  im.setAttribute=function(n,v){ im.setLog.push(n); im.attrs[n]=String(v); };\n"
        "  im.removeAttribute=function(n){ im.rmLog.push(n); delete im.attrs[n]; };\n"
        "  im.parentNode={removeChild:function(c){ c.removed=true; }};\n"
        "  im.addEventListener=function(t,fn){ im.handlers[t]=fn; };\n"
        "  return im;\n"
        "}\n"
        "var samples=%s;\n"
        "var out=[];\n"
        "for(var s=0;s<samples.length;s++){\n"
        "  var imgs=[];\n"
        "  for(var i=0;i<samples[s].imgs.length;i++){ imgs.push(mkImg(samples[s].imgs[i])); }\n"
        "  var root={querySelectorAll:function(sel){ return imgs; }};\n"
        "  global.location={href:samples[s].page||'https://page.test/'};\n"
        "  _hardenBodyImages(root, samples[s].base);\n"
        "  for(var q=0;q<imgs.length;q++){\n"
        "    var im=imgs[q]; var fired='';\n"
        "    if(im.handlers&&im.handlers.error){ im.handlers.error.call(im); fired=im.style.display||''; }\n"
        "    out.push({attrs:im.attrs,removed:im.removed,setLog:im.setLog,rmLog:im.rmLog,"
        "hasError:!!(im.handlers&&im.handlers.error),displayAfterError:fired});\n"
        "  }\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));\n"
        % (json.dumps(code), json.dumps(samples)),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "node 跑渲染层兜底崩了：\n%s\n%s" % (
        (r.stdout or "")[-600:], (r.stderr or "")[-1500:])
    return json.loads(r.stdout)


# 运行时通道（/api/article 现抓、不过构建期规范）的常见形状：只有 data-src。
RUNTIME_LAZY_PAGE = "https://example.com/post/one/"


def _one(tmp_path, img, base=RUNTIME_LAZY_PAGE, page=None):
    return _harden(tmp_path, [{"base": base, "page": page, "imgs": [img]}])[0]


# ---------------------------------------------------------------- 静态判据（brief 四条）
def test_body_images_are_hardened():
    src = _src()
    fn = re.search(r"function _hardenBodyImages\(root,baseUrl\)\{", src)
    assert fn, "没有 _hardenBodyImages：正文图片仍在用出厂原样（无 referrer 策略、破图不隐藏）"
    body = src[fn.start():src.find("function ", fn.start() + 10)]
    assert "_coverRefPolicy(" in body, "正文图片没复用封面那张唯一的表"
    assert "loading" in body and "lazy" in body, "正文图片没设懒加载"
    assert re.search(r"addEventListener\(['\"]error", body), "破图没有 error 兜底"
    assert "data-src" in body, "老条目的 data-src 不在渲染层兜底范围内"


def test_harden_is_called_from_fulltext_insert():
    src = _src()
    i = src.find("function _insertFulltext(")
    body = src[i:src.find("function ", i + 10)]
    assert "_hardenBodyImages(" in body, "渲染兜底函数定义了但没被调用（等于没修）"


def test_no_second_referrer_host_table():
    """表只有一份：新增第二张 `_REF_HOSTS` 变体就是新的事实来源。"""
    assert _src().count("var _REF_HOSTS") == 1, "出现了第二张 referrer 主机名表"


def test_empty_body_img_is_removed_instead_of_shown_broken():
    src = _src()
    i = src.find("function _hardenBodyImages")
    body = src[i:i + 1600]
    assert re.search(r"removeChild|remove\(\)", body), "无可用 src 的 img 仍会留成破图占位"


# ---------------------------------------------------------------- 静态判据（补充钉位）
def test_harden_layer_declares_no_new_host_literals():
    """渲染层不许写死任何域名：那是第二张表的开头形状。"""
    src = _src()
    fn = re.search(r"function _hardenBodyImages\(root,baseUrl\)\{", src)
    assert fn, "没有 _hardenBodyImages"
    body = src[fn.start():src.find("function ", fn.start() + 10)]
    assert not re.search(r"\.(com|cn|net|org)\b", body), \
        "渲染层里出现了域名后缀：referrer 判定只许走 _coverRefPolicy 那一份表"


def test_blank_policy_uses_remove_attribute_not_empty_attr():
    """referrerpolicy 为空串（=放行 Referer）时必须 removeAttribute，写 attr="" 语义不确定。"""
    src = _src()
    fn = re.search(r"function _hardenBodyImages\(root,baseUrl\)\{", src)
    assert fn, "没有 _hardenBodyImages"
    body = src[fn.start():src.find("function ", fn.start() + 10)]
    a = body.find("referrerpolicy")
    assert a >= 0, "正文图片根本没设 referrerpolicy"
    seg = body[a - 60:a + 240]
    assert "removeAttribute" in seg, \
        "空策略没走 removeAttribute（写成 attr=\"\" 是不确定语义）：%s" % seg


# ---------------------------------------------------------------- 行为判据（真产物兜底）
def test_runtime_channel_lazy_src_is_promoted_and_policy_follows_the_table(tmp_path):
    """运行时通道形状：`<img data-src=…>` 没有 src。

    表内域名（caifuzhongwen.com 那家"不带 Referer 就不给图"）⇒ 策略必须是空串，
    而空串只能用 removeAttribute 表达，所以 referrerpolicy 属性应当**不存在**。
    """
    r = _one(tmp_path, {"data-src": "https://images1.caifuzhongwen.com/a.png"})
    assert r["attrs"].get("src") == "https://images1.caifuzhongwen.com/a.png", \
        "渲染层没把 data-src 提升为 src（运行时通道的图仍然是破图）：%s" % r["attrs"]
    assert "referrerpolicy" not in r["attrs"], \
        "表内域名不该带 referrerpolicy（空串=放行 Referer）：%s" % r["attrs"]
    assert "referrerpolicy" not in r["setLog"], \
        "空策略被写成 attr= 而非 removeAttribute：%s" % r["setLog"]
    assert r["attrs"].get("loading") == "lazy", "正文图片没设懒加载：%s" % r["attrs"]
    assert r["attrs"].get("decoding") == "async", "正文图片没设 decoding：%s" % r["attrs"]
    assert r["hasError"], "取图失败没有 error 兜底（破图会一直占位）"
    assert r["displayAfterError"] == "none", "error 兜底没把破图隐藏：%r" % r["displayAfterError"]


def test_runtime_channel_relative_lazy_src_is_absolutized(tmp_path):
    """相对 URL 按**条目原文链接**落地：Readability 出来的正文经常只有 /imgs/a.png。"""
    r = _one(tmp_path, {"data-src": "/imgs/a.png"})
    assert r["attrs"].get("src") == "https://example.com/imgs/a.png", \
        "相对 data-src 没被绝对化：%s" % r["attrs"]
    assert r["attrs"].get("referrerpolicy") == "no-referrer", \
        "表外域名该带 no-referrer：%s" % r["attrs"]


def test_placeholder_data_src_is_replaced_by_lazy_value(tmp_path):
    """占位图（data:image/gif）+ data-src 是懒加载的标准形状：留占位就是满屏灰块。"""
    r = _one(tmp_path, {"src": "data:image/gif;base64,R0lGOD",
                        "data-src": "https://e.test/real.png"})
    assert r["attrs"].get("src") == "https://e.test/real.png", \
        "占位 src 没被 data-src 顶掉：%s" % r["attrs"]


def test_no_usable_src_is_removed_not_left_broken(tmp_path):
    r = _one(tmp_path, {"alt": "本来就没图"})
    assert r["removed"], "没有可用 src 的 img 被留在了 DOM 里（破图占位）：%s" % r["attrs"]
    assert not r["hasError"], "已被移除的 img 不该再挂 error 监听"


def test_garbage_and_protocol_relative_src_are_decided(tmp_path):
    """协议相对地址按**页面**补全（base 缺失时用 location.href）；`javascript:` 垃圾值整枚摘掉。"""
    rel = _one(tmp_path, {"src": "//cdn.example.org/b.png"}, base="", page="https://page.test/p/")
    assert rel["attrs"].get("src") == "https://cdn.example.org/b.png", \
        "协议相对地址没按页面协议补全（base 空时要退回 location.href）：%s" % rel["attrs"]
    bad = _one(tmp_path, {"src": "javascript:void(0)"})
    assert bad["removed"], "无法成为图片地址的 src 留成了破图：%s" % bad["attrs"]


def test_substring_spoofed_host_still_gets_no_referrer(tmp_path):
    """冒充形状之一：表内域名住在 **query/path** 里（`indexOf(u)` 那种写法会被骗）。

    与 test_orphan_requests.py 的封面样本同形（变异⑥ 的捕手 A）。
    """
    r = _one(tmp_path, {"src": "http://evil.com/?x=caifuzhongwen.com/a.png"})
    assert r["attrs"].get("referrerpolicy") == "no-referrer", \
        "referrer 判定被整条 URL 的子串匹配冒充：%s" % r["attrs"]


def test_lookalike_host_is_not_matched_as_substring(tmp_path):
    """冒充形状之二：host 本身**含着**表内子串 —— `notcaifuzhongwen.com` 不是那家图床。

    `_hostInTable` 收的是 `_coverHost()` 抽出来的 host，所以把判定改成
    `host.indexOf(d)>=0` 时上一条（query 冒充）照样绿：必须两条形状各自钉住（变异⑥ 的捕手 B）。
    """
    r = _one(tmp_path, {"data-src": "https://notcaifuzhongwen.com/a.png"})
    assert r["attrs"].get("referrerpolicy") == "no-referrer", \
        "host 子串匹配把冒充域名当表内域名放了行（Referer 泄露给非图床站点）：%s" % r["attrs"]


def test_uppercase_scheme_host_is_judged_the_same_way(tmp_path):
    """`HTTPS://IMAGES1.CAIFUZHONGWEN.COM/a.png` 与全小写同格：判空/放行两边不许分叉。"""
    r = _one(tmp_path, {"data-src": "HTTPS://IMAGES1.CAIFUZHONGWEN.COM/a.png"})
    assert "referrerpolicy" not in r["attrs"], \
        "大写 scheme/域名没被认成表内域名（正文与封面两套判定）：%s" % r["attrs"]


def test_real_data_image_src_is_kept(tmp_path):
    """真的 data:image 内联图（有些 feed 直接内联小图）不该被整枚摘掉。"""
    r = _one(tmp_path, {"src": "data:image/png;base64,iVBORw0KGgo="})
    assert not r["removed"], "内联 data:image 被误删：%s" % r["attrs"]
    assert r["attrs"].get("src") == "data:image/png;base64,iVBORw0KGgo="
