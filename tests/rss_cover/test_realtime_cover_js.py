# -*- coding: utf-8 -*-
"""实时链路补抽图（③#4）的判据：node 侧抽取规则 + Python/JS 两层过滤的口径一致。

两件事各自会怎么坏：
  ① `lib/rss_cover.js` 的优先级跑偏（把音频 enclosure 当封面、或 `&amp;` 截断 URL）
     ⇒ 由 `tests/rss_js/test_realtime_cover.js` 的 12 个用例盯着，这里只做调用与退出码判定。
  ② **构建期判空（`_drop_unloadable_cover`）与页面渲染层判空（产物里的 `_dropBadCover`）分叉**
     ⇒ 这是今天第二次撞同一个形状：上午是 `_needs_translation`（Python 剥 ANSI/URL、JS 没剥，
     分叉 5/14）。这次探针真的逮到了一条：`HTTPS://ICHEF.BBCI.CO.UK/A.JPG` 在 Python 侧判空、
     JS 侧因为用**原样大小写**比 scheme 而放行。⇒ 判空只写在 Python 会漏、只写在 JS 会漏，
     所以这条判据把两边放在**同一批样本**上逐条对，任一边改规则就红。

`_BAD_COVERS` 是从 Python 那张表注入到产物的（只有一份事实来源），所以这里同时钉注入本身：
名单变了而产物没变 = 生成端与消费端脱钩。
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS_TEST = os.path.join(ROOT, "tests", "rss_js", "test_realtime_cover.js")
# 变异体注入点（本仓既有约定：STARHUB_UPDATE_YML / RSS_BUILD_SRC 的第三个同族）
API_RSS = os.environ.get("STARHUB_API_RSS") or os.path.join(ROOT, "api", "rss.js")
LIB_COVER = os.environ.get("STARHUB_COVER_LIB") or os.path.join(ROOT, "lib", "rss_cover.js")
# 切出来的 `parseFeed` 里，Atom 分支自 R43 起就走 `BODY.capBody`（`buildFullContent`）⇒ 跑它的
# node 驱动必须一并 require 运行时规则表，否则任何让 RSS 分支也过清洗链的改动都会以
# `ReferenceError: BODY is not defined` 红在这里（不是判据坏，是桩缺依赖）。
# 与 COVER 一样走 env：变异电池的 tmp 副本靠它把 require 指到副本目录。
LIB_BODY = os.environ.get("STARHUB_BODY_LIB") or os.path.join(ROOT, "lib", "body_rules.js")

# 两边必须给同一条答案的样本。覆盖：表内精确/子域、大写 scheme、带端口与 userinfo、
# Referer 放行域名、健康域名、相对路径与 data URI、空值。
COVER_SAMPLES = [
    "https://ichef.bbci.co.uk/1/640/c000.jpg",
    "HTTPS://ICHEF.BBCI.CO.UK/1/640/c000.jpg",
    "  https://i.guim.co.uk/a.jpg  ",
    "https://secure.i.guim.co.uk/a.jpg",              # 真子域（点分边界）
    "https://npr.brightspotcdn.com:443/a.jpg",
    "https://user:pass@external-preview.redd.it/a.jpg",
    "http://bucket-cb.ops.xhyun.news.cn/a.jpg",
    "https://images1.caifuzhongwen.com/a.jpg",         # Referer 放行域名，不许判空
    "https://preview.redd.it/a.jpg",                   # 同族但未进表，不许误伤
    "https://imgpai.thepaper.cn/a.jpg",
    "https://i.guim.co.uk.attacker.example/a.jpg",     # 前缀相似冒充
    "https://notichef.bbci.co.uk/a.jpg",               # 后缀相似冒充
    "/local/a.jpg",
    "data:image/png;base64,AAAA",
    "",
]


def _load():
    sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from _loader import load_build
    return load_build()


def _node():
    return shutil.which("node")


def _generated_html():
    return _load().build_html([], "2026-10-01 12:20", 0, 0, analysis_data=None,
                              diverse_window_minutes=120, diverse_enabled=True)


def _cover_filter_js(html):
    """从现生成产物里抠出域名判空那一整段（名单 + 三个函数），供 node 直接执行。"""
    start = html.index("var _REF_HOSTS")
    end = html.index("function renderWall")
    assert end > start, "产物里 JS 段顺序变了，抠取锚点需要重找（不许改成静默返回空段）"
    seg = html[start:end]
    for name in ("var _BAD_COVERS", "function _coverHost", "function _hostInTable",
                 "function _dropBadCover", "function _coverRefPolicy"):
        assert name in seg, "产物缺少 %s ⇒ 渲染层判空不在了" % name
    return seg


def test_realtime_cover_extraction_node_suite():
    """① lib/rss_cover.js 的抽取规则：跑 node 用例集，退出码即结论。"""
    node = _node()
    if not node:
        pytest.skip("本机没有 node，实时封面抽取判据测不了（同 test_artifact_js_parses 的口径）")
    r = subprocess.run([node, JS_TEST], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, "实时封面抽取用例集失败（rc=%d）：\n%s" % (r.returncode, out[-2500:])
    # 反空跑：用例集必须真的报出 11+ 条 ok，否则"0 个用例也 exit 0"就成了假绿
    assert out.count("\nok ") >= 11, "node 用例集疑似空跑：\n%s" % out[-800:]


def test_js_bad_cover_list_is_the_injected_one(tmp_path):
    """产物里的 `_BAD_COVERS` 必须等于 Python 那张表（注入没掉、也没被手抄成第二份）。"""
    B = _load()
    html = _generated_html()
    seg = _cover_filter_js(html)
    line = [l for l in seg.splitlines() if l.strip().startswith("var _BAD_COVERS")][0]
    listed = json.loads(line[line.index("=") + 1:].strip().rstrip(";"))
    assert set(listed) == set(B._BAD_COVER_HOSTS), (
        "产物注入的域名表 %s ≠ 构建侧 %s：渲染层与出厂层各按一套域名跑"
        % (sorted(listed), sorted(B._BAD_COVER_HOSTS)))
    assert listed == sorted(listed), "`_BAD_COVERS` 应保持稳定排序，便于 diff 与复现"


def _js_pipeline_src():
    """切 api/rss.js 里 extractTag..fetchOne 之间的纯函数（含 parseFeed），与既有判据同一手法。"""
    text = open(API_RSS, encoding="utf-8").read()
    a = text.find("function extractTag(")
    b = text.find("async function fetchOne(")
    assert 0 < a < b, "api/rss.js 里找不到 extractTag..fetchOne 区段，helper 结构变了"
    seg = text[a:b]
    assert "function parseFeed(" in seg, "切出的区段里没有 parseFeed"
    return seg


# 同一条 feed 必须给两条通道同一个封面答案。四种形状都要覆盖：
# media:thumbnail / 描述里转义过的 <img> / 描述里 CDATA 的 <img> / 只有音频 enclosure（不该有封面）
REALTIME_ITEMS = [
    # media: 前缀必须显式声明命名空间，否则 Python 侧 ET 直接 ParseError（JS 侧是正则，不在乎）
    ('<item xmlns:media="http://search.yahoo.com/mrss/"><title>i1</title>'
     '<link>https://t.test/1</link><description>d</description>'
     '<media:thumbnail url="https://m.test/1.jpg"/></item>', "https://m.test/1.jpg"),
    ('<item><title>i2</title><link>https://t.test/2</link>'
     '<description>&lt;p&gt;&lt;img src="https://c.test/2.png"&gt;&lt;/p&gt;</description></item>',
     "https://c.test/2.png"),
    ('<item><title>i3</title><link>https://t.test/3</link>'
     '<description><![CDATA[<p><img src="https://c.test/3.gif?a=1&amp;b=2"></p>]]></description></item>',
     "https://c.test/3.gif?a=1&b=2"),
    ('<item><title>i4</title><link>https://t.test/4</link><description>d</description>'
     '<enclosure url="https://a.test/4.mp3" type="audio/mpeg"/></item>', ""),
]


def test_parse_feed_cover_matches_buildtime_parser(tmp_path):
    """③#4 的正身判据：实时 parseFeed 抽出的封面必须与构建期 `_parse_rss_item` 逐条同答案。

    没有这条的话，"补抽图"可以整个消失而全场仍全绿 —— `lib/rss_cover.js` 的用例集只证明
    规则本身对，不证明 `parseFeed` 真的调了它、也不证明短键映射把 img 带进了响应。
    """
    import xml.etree.ElementTree as ET

    node = _node()
    if not node:
        pytest.skip("本机没有 node，实时 parseFeed 跑不了")
    B = _load()

    xml = '<?xml version="1.0"?><rss><channel>%s</channel></rss>' % "".join(
        frag for frag, _ in REALTIME_ITEMS)
    runner = tmp_path / "parse_feed_cover.js"
    runner.write_text(
        "const COVER=require(%r);\n"
        "const BODY=require(%r);\n"
        "const s=%r;\neval(s);\n"
        "const its=parseFeed(%s,'k',50);\n"
        "process.stdout.write(JSON.stringify(its.map(function(x){return x.img||'';})));\n"
        % (LIB_COVER, LIB_BODY, _js_pipeline_src(),
           json.dumps(xml)),
        encoding="utf-8", newline="\n")
    r = subprocess.run([node, str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "实时 parseFeed 跑崩：\n%s%s" % (r.stdout, r.stderr[-800:])
    js_imgs = json.loads(r.stdout.strip())
    assert len(js_imgs) == len(REALTIME_ITEMS), (
        "实时侧解析出 %d 条、fixture %d 条 ⇒ 判据在错位样本上比" % (len(js_imgs), len(REALTIME_ITEMS)))

    for idx, (frag, want) in enumerate(REALTIME_ITEMS):
        py_out = []
        B._parse_rss_item(ET.fromstring(frag), "测试源", "t_test", "ai", py_out)
        assert py_out, "构建期解析器没解析出 fixture %d" % (idx + 1)
        py_img = py_out[0].get("image", "")
        assert js_imgs[idx] == py_img, (
            "fixture %d 封面分叉：实时 %r / 构建期 %r" % (idx + 1, js_imgs[idx], py_img))
        assert py_img == want, "fixture %d 期望 %r，构建期给了 %r（fixture 先失效了）" % (
            idx + 1, want, py_img)
    assert any(w for _, w in REALTIME_ITEMS) and any(not w for _, w in REALTIME_ITEMS), \
        "fixture 退化：必须同时含「有封面」和「无封面」两类，否则上面那串等式恒真"


def test_lib_rss_cover_exports_all_have_callers():
    """反向守卫：`lib/rss_cover.js` 的每个导出都必须真有调用方（同 `test_no_api_function_without_a_caller` 的形状）。

    第一版我把 `decodeBasic` 与两个正则也导出了，理由是"以后也许用得上"—— 那是死出口，
    会让下一个人误以为它们是公共 API。现在只导真被调的两个，并用这条判据钉住。
    """
    src = open(LIB_COVER, encoding="utf-8").read()
    m = re.search(r"module\.exports\s*=\s*\{([^}]*)\}", src)
    assert m, "lib/rss_cover.js 没有 module.exports —— 抽取逻辑不再可复用，判据也就没东西可跑"
    names = [x.strip() for x in m.group(1).split(",") if x.strip()]
    assert names, "导出表为空：这条判据会退化成恒绿"

    callers = (open(API_RSS, encoding="utf-8").read()
               + open(JS_TEST, encoding="utf-8").read())
    orphans = [n for n in names if not re.search(r"\b(?:C|COVER)\." + re.escape(n) + r"\b", callers)]
    assert not orphans, (
        "这些导出没有任何调用方：%s（要么删掉导出，要么在调用点用上它；不许留公共 API 的空壳）"
        % ", ".join(sorted(orphans)))


def test_realtime_cover_key_chain_is_complete():
    """#4 的链路必须从头通到尾：API 短键 img → 合并成长键 image → buildArt 再回短键 img。

    这条链有两处"名字换来换去"的接缝，任一处被简化掉都会让实时封面静默消失：
      `_apiMergeTo` 里必须把 API 的 `it.img` 写成 `image`；
      `buildArt` 里必须同时认 `it.image`（合并后的长名）与 `it.img`（batch 路径的短名）。
    只验"API 有没有发 img"是不够的 —— 发了但没人接，等于没发。
    """
    html = _generated_html()

    def _fn(name):
        """按同缩进的下一个 `function ` 收边界，避免把整页当一段来 grep。"""
        i = html.index("function " + name + "(")
        j = html.find("\n  function ", i + 1)
        assert j > i, "找不到 %s 的结束边界，判据的范围失效" % name
        return html[i:j]

    merge = _fn("_apiMergeTo")
    art = _fn("buildArt")
    assert "image:it.img||''" in merge, (
        "`_apiMergeTo` 不再把 API 的 img 落成 image ⇒ 实时条目到页面就没有封面（#4 白做）")
    assert "img:it.image||it.img||''" in art, (
        "`buildArt` 不再同时认长名 image 与短名 img ⇒ 实时封面在映射处被丢掉")


def test_realtime_cover_is_carried_into_the_response_shape():
    """抽到图还要带得出去：两条分支写 img，短键映射把 img 放进 cached item。

    这是"补了抽图但响应里仍然恒空"那类半成品唯一的探测器（`img: it.img || ''` 那行
    在映射缺失时会静默给空串，判据不会自己红）。
    """
    text = open(API_RSS, encoding="utf-8").read()
    seg = _js_pipeline_src()
    assert seg.count("COVER.pickItemImage(") == 2, (
        "实时封面抽取必须接在 Atom 与 RSS 两条分支上，实际 %d 处" % seg.count("COVER.pickItemImage("))
    assert "if (it.img) obj.img = it.img;" in text, (
        "短键映射里没有 img ⇒ parseFeed 抽到的封面到不了响应（出口那行 `img: it.img || ''` 恒空）")


def test_buildtime_and_render_cover_filters_agree(tmp_path):
    """② 同一批 URL，出厂判空与渲染层判空必须逐条同答案。

    触发这条的现实场景：只改一边（比如给 JS 的 `_coverHost` 去掉小写、或给 Python
    加一条 scheme 例外），当场红。今天写这条之前，探针实测到的就是这种分叉。
    """
    node = _node()
    if not node:
        pytest.skip("本机没有 node，渲染层判空测不了")
    B = _load()
    B._bad_cover_hits.clear()
    seg = _cover_filter_js(_generated_html())
    runner = tmp_path / "cover_parity.js"
    payload = tmp_path / "samples.json"
    payload.write_text(json.dumps(COVER_SAMPLES), encoding="utf-8")
    runner.write_text(
        "'use strict';\n"
        "const fs = require('fs');\n"
        "%s\n"
        "const samples = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));\n"
        "console.log(JSON.stringify(samples.map(function (s) { return _dropBadCover(s) || ''; })));\n"
        % seg,
        encoding="utf-8", newline="\n")
    r = subprocess.run([node, str(runner), str(payload)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "node 侧判空跑失败：\n%s%s" % (r.stdout, r.stderr[-1500:])
    js_out = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(js_out) == len(COVER_SAMPLES), "node 返回的条数与样本数不一致（判据在空跑）"

    py_out = [(B._drop_unloadable_cover(s) or "") for s in COVER_SAMPLES]
    diverged = [(s, p, j) for s, p, j in zip(COVER_SAMPLES, py_out, js_out) if p != j]
    assert not diverged, "出厂判空与渲染层判空分叉 %d/%d 条：%s" % (
        len(diverged), len(COVER_SAMPLES),
        "; ".join("%r -> python=%r js=%r" % d for d in diverged))
    # 反空跑：这批样本必须同时含"被判空"和"被放行"两类，否则上面那条恒等式毫无内容
    nulled = [x for x in py_out if x == ""]
    kept = [x for x, s in zip(py_out, COVER_SAMPLES) if x and x == s]
    assert nulled and kept, "样本退化：判空 %d 条、放行 %d 条" % (len(nulled), len(kept))
