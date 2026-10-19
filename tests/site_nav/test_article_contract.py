# -*- coding: utf-8 -*-
"""/api/rss 与 /api/article 的正文出口必须真的过 `lib/body_rules` 这道闸。

为什么读源码 + 本地跑切出来的函数，而不是发 HTTP：CI 上没有可用的 Vercel 凭证，
`/api/article` 的真实响应要等线上场；而"忘了接"这件事在源码层就能钉死
（同形状先例：`tests/site_nav/test_orphan_requests.py`、
`tests/rss_history/test_parse_parity_js.py` 都是读源文本 + 用 node 跑切片）。
线上读数由 §验收清单 用浏览器通道补，两层都要有。

**硬约束：本判据不许 require npm 依赖。** CI 没跑 `npm ci`、runner 上没有 `node_modules`，
而 `api/article.js` 顶部就是 `require('jsdom')` ⇒ 任何 import 它的测试在 CI 上必崩
（红了还查不到原因）。所以：
  · `/api/article` 的返回契约走**源码级断言**（形状照 test_orphan_requests.py）；
  · 行为判定走 `lib/body_rules.js` 的 node 用例（tests/rss_js/test_body_rules.js）；
  · `api/rss.js` 的**产物**走 node 切片实跑 —— 切出来的 `extractTag..fetchOne` 区段
    只依赖 fs/path + `lib/*.js`，零 npm 依赖，因此能在 CI 上跑。
"接线不是断链"的直接证据在下面 `test_realtime_parse_feed_*` 两条：它们拿的是**真产物**。
"""
import io
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 变异体注入点：与本仓 STARHUB_BODY_RULES / RSS_BUILD_SRC / STARHUB_UPDATE_YML 同一约定。
API_RSS = os.environ.get("STARHUB_API_RSS") or os.path.join(ROOT, "api", "rss.js")
API_ARTICLE = os.environ.get("STARHUB_API_ARTICLE") or os.path.join(ROOT, "api", "article.js")
LIB_BODY = os.environ.get("STARHUB_BODY_RULES") or os.path.join(ROOT, "lib", "body_rules.js")
LIB_COVER = os.path.join(ROOT, "lib", "rss_cover.js")


def _read(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _code(text):
    """只留代码行。注释里提一句被禁字面量不该把判据打红
    （口径照 tests/site_nav/test_body_rules_parity.py:678 `test_js_lib_has_zero_requires...`）。
    真把 `.slice(0, 50000)` 写回代码行仍然红 —— 见报告里的变异①。
    """
    keep, block = [], False
    for ln in text.split("\n"):
        t = ln.strip()
        if block:
            if t.endswith("*/") or "*/" in t:
                block = False
            continue
        if t.startswith("/*"):
            if "*/" not in t:
                block = True
            continue
        if t.startswith("//") or t.startswith("*"):
            continue
        keep.append(ln)
    return "\n".join(keep)


def _node():
    n = shutil.which("node")
    if not n:
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    return n


# ---------------------------------------------------------------- Task 4：接线在不在
def test_both_runtimes_share_the_one_body_rules_module():
    for p in (API_RSS, API_ARTICLE):
        assert re.search(r"""require\(['"]\.\./lib/body_rules\.js['"]\)""", _read(p)), \
            "%s 没接 lib/body_rules.js ⇒ 正文规范只剩构建期一份" % os.path.basename(p)


def test_no_bare_slice_of_50000_left_in_rss_exit():
    """`.slice(0, 50000)` 是裸切片，会截出半个标签 —— 必须换成 capBody。"""
    assert "slice(0, 50000)" not in _code(_read(API_RSS)), "api/rss.js 还在裸截断"


def test_article_output_is_normalized_and_capped(tmp_path):
    """派工 F1：规范化与截断必须**落到出口**，判据量的不能是"函数名出现过"。

    原判据只有两条 grep ⇒ 在 %TEMP% 副本里把出口改成"算了但丢弃结果"
    （`_normalized = BODY.normalizeBodyHtml(result.content, url)` 之后
    `content = BODY.capBody(result.content)`）它照样 26 passed，`:113` 数出现次数也绿。
    根因是结构性的：`api/article.js` 在 `tools/mut_reader.py` 里属 ONCE（只读）、不在
    COPIES ⇒ 电池压根没法给它登记靶。这里两半一起补：判据改走**实跑切片**（问
    `out.content` 的去向），`api/article.js` 进 COPIES 并登记 F01/F02/F03 三条靶。
    原有那两条 grep 一个字没删（只加不减），新增的两段行为才是有牙的那半。
    """
    src = _read(API_ARTICLE)
    assert re.search(r"normalizeBodyHtml\(", src), "article.js 没做正文规范化"
    assert re.search(r"capBody\(", src), "article.js 没做安全截断"

    # ── 去向一（规范化）：算过不等于出厂。相对地址必须在产物里已经绝对化。──
    c = _article_exit_product(tmp_path, _NORM_BODY)["content"]
    assert 'src="https://blog.example.com/imgs/a.png"' in c, (
        "规范化算了但没落到出口（产物仍是未绝对化的懒加载形状）⇒ 阅读器里就是丢图：%s" % c)
    assert "/tag/go" not in c, "规范化那半被摘掉时，脱链也不在出口上：%s" % c

    # ── 去向二（截断）：capBody 必须是**出口值的最后一道**，不是中途的一次计算。──
    pad = 49990
    long_body = u"<p>" + u"y" * pad + u"</p><img src=\"https://e.test/x.png\">"
    assert 50000 < len(long_body) < 50100, "fixture 本身要刚好跨过上限，实际 %d" % len(long_body)
    t = _article_exit_product(tmp_path, long_body)["content"]
    assert len(t) <= 50000, "没截断：%d 字符出厂" % len(t)
    assert not re.search(r"<[^>]*$", t), "尾巴挂着半个标签（还是裸切片）：%r" % t[-40:]
    assert not re.search(r"&[#A-Za-z0-9]*$", t), "尾巴挂着残缺实体：%r" % t[-40:]
    assert t.endswith("</p>"), "截断点没收敛到干净边界：%r" % t[-24:]
    assert len(t) > 49000, "截得太狠（正文被整段丢了）：%d" % len(t)


# ── /api/article 现抓通道"出口整形"的**实跑**底座（审查 ⑤ + 派工 F1）────────────
# 为什么必须实跑：`api/article.js` 顶部就是 `require('jsdom')`，CI 上没有 node_modules ⇒
# 整份文件 require 不进来（一 import 就 MODULE_NOT_FOUND，红了还查不到原因，本文件 §硬约束
# 就是这么写的）。但成功返回前那一段（`const content = …` 到 `cacheSet(url, out);` 之前）
# 只认三个名字：BODY（真 lib，零 npm 依赖）、result、url ⇒ 注入这三个就能把**出厂那几行
# 原样跑一遍**。量的是 `out.content` 的**去向**而不是"源码里出现过 BODY.xxx"：
# 规范化算了但没落到出口 ⇒ 产物里仍是相对地址/未脱导航 href，当场红（F1 的根因形状）。
# 锚点找不到 ⇒ assert 红并说清是出口形状变了，绝不静默 skip（静默 skip 就是第二条假绿）。
_ARTICLE_URL = u"https://blog.example.com/post/one/"
_EXIT_START = re.compile(r"^[ \t]*const content = ", re.M)
_EXIT_END = re.compile(r"^[ \t]*cacheSet\(url, out\);", re.M)

# 现抓通道的样本正文：Readability 出来的形状就是"站内相对链接 + 懒加载属性 + 外部正文链接"。
_NAV_BODY = (u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署，'
             u'<a href="https://blog.example.com/category/engineering">分类</a> 页与'
             u'<a href="#sec-2">见下节</a>，'
             u'参考 <a href="https://openai.com/blog/x">原始公告</a></p>'
             u'<img data-src="/imgs/a.png" alt="a">')

# F1 的样本：只留"规范化在产物上看得见"的那一格（懒加载提升 + 绝对化）。
# 现抓通道没有 sanitize（Readability 的产物形状与 feed 不同），所以 data-src 会留着，
# 判据盯的是**提升出来的绝对 src** —— "算了但丢弃"那一版产物里它压根不存在。
_NORM_BODY = (u'<p>正文</p><img data-src="/imgs/a.png" alt="a">'
              u'<a href="/tag/go">Go</a>')


def _article_exit_src():
    """切出 article.js 里"出口整形 → 成功载荷"那几行的**原文**。"""
    text = _read(API_ARTICLE)
    a = _EXIT_START.search(text)
    assert a, ("api/article.js 里找不到出口段起点（行首 `const content = `）⇒ "
               "出口整形的形状变了，本判据失去着力点")
    b = _EXIT_END.search(text, a.end())
    assert b, ("api/article.js 里找不到出口段终点（行首 `cacheSet(url, out);`）⇒ "
               "成功返回前多/少了步骤，本判据失去着力点")
    seg = text[a.start():b.start()]
    assert "const out = {" in seg, "切到的区间里没有成功载荷构造行 ⇒ 切错了地方"
    return seg


def _article_exit_product(tmp_path, content, url=_ARTICLE_URL):
    """拿**真** lib 跑**真**出口那几行，返回出厂的成功载荷（dict）。

    出口那一行自裁定 ④ 起会调用 `stripRemoteExecutables`，而刀定义在切片区间的**前面** ⇒
    必须把刀从同一个源文件里一起切进来喂给它（桩只补"同文件里的另一半"，不自己重写一遍，
    否则就是本仓点过名的"桩越权替被测文件声明依赖"⇒ 删掉定义的变异反而全绿）。
    """
    runner = tmp_path / "article_exit_product.js"
    runner.write_text(
        "const BODY = require(%s);\n"
        "const url = %s;\n"
        "const result = {title: 'T', content: %s, source: 'readability', degraded: 'short'};\n"
        "%s\n%s\n"
        "process.stdout.write(JSON.stringify(out));\n"
        % (json.dumps(LIB_BODY.replace("\\", "/")), json.dumps(url), json.dumps(content),
           _exec_knife_src(), _article_exit_src()),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "article.js 出口切片实跑崩了：\n%s\n%s" % (
        (r.stdout or "")[-400:], (r.stderr or "")[-1200:])
    return json.loads(r.stdout)


def test_article_live_channel_delinks_insite_nav_links(tmp_path):
    """审查 ⑤：现抓通道与快照通道必须**同一把脱链刀**，产物里不许留站内导航 href。

    判据是**行为**：把 Readability 形状的正文喂给真出口切片，断言产出的 HTML 里
    站内导航（`/tag/`、`/category/`）已脱成纯文字，而三类"不许动"的一条没被吃掉 ——
    外部正文链接、页内锚点、正文词。走快照 fc 的条目在构建期已过这把刀，
    这里补齐的是**多数文章**走的那条现抓通道。
    """
    out = _article_exit_product(tmp_path, _NAV_BODY)
    c = out["content"]
    assert out.get("ok") is True and out.get("source") == "readability", out
    assert out.get("degraded") == "short", "degraded 那一格被出口整形改掉了（契约外）：%s" % out
    for word in (u"Kubernetes", u"分类", u"见下节", u"原始公告"):
        assert word in c, "脱链把链接文字一起吃掉了：%r" % c
    assert "/tag/kubernetes" not in c, \
        "现抓通道没接 delinkNavLinks ⇒ 正文里仍是站内导航链接：%r" % c
    assert "/category/engineering" not in c, \
        "同站绝对形状的导航链接也没脱（normalize 之后才看得见的那一半）：%r" % c
    assert 'href="https://openai.com/blog/x"' in c, "外部正文链接被脱了：%r" % c
    assert 'href="#sec-2"' in c, "页内锚点被脱了（构建期同名反例）：%r" % c


def test_rss_exit_keeps_the_buildtime_order_normalize_sanitize_deepclean_cap():
    """顺序必须与构建期 `_normalize → _sanitize → _deep_clean → _cap` 一致。

    不是洁癖：`sanitizeHtml` 的 `ALLOWED_ATTRS["img"] = ['src','alt']` 会把 `data-src`
    整条剥掉。normalize 挪到 sanitize 之后就永远看不见懒加载属性 ⇒ 图片在出口前就没了
    （用户报的"正文丢图"就是这个形状）。所以既钉字面嵌套顺序，也钉真产物（下面那条）。
    """
    src = _read(API_RSS)
    m = re.search(r"BODY\.capBody\(\s*deepCleanHtml\(\s*sanitizeHtml\(\s*BODY\.normalizeBodyHtml\(", src)
    assert m, "api/rss.js 的 fullContent 出口顺序不是 normalize→sanitize→deepClean→cap"
    # base 用的是条目自己的 link（局部变量），不是被 `|| '#'` 兜过的那份
    assert re.search(r"normalizeBodyHtml\(\s*fullContent\s*,\s*link\s*\)", src), \
        "base 不该取 result.link（它已被写成 link || '#'，'#' 不是 base）"


# ── 裁定 ④（2026-10-05）：现抓通道的可执行面剥离 —— 两轮审查唯一未修的那格 P0 ──────────
# 这条通道把远端站点的 HTML 交给阅读器 `innerHTML` ⇒ 原文站点可以在我们页面里跑脚本。
# 刀必须**切函数来跑**：api/article.js require jsdom，CI 没有 npm ci ⇒ 整文件跑不起来，
# 但那把刀是纯文本正则，切出来就能在 node 里实跑（所以它设计成纯文本，不是 DOM）。
_EXEC_ANCHOR = "const _EXEC_TAGS_RE_PAIR"
_CONTRACT_ANCHOR = "// ── /api/article 返回契约"


def _exec_knife_src():
    src = _read(API_ARTICLE)
    i = src.find(_EXEC_ANCHOR)
    j = src.find(_CONTRACT_ANCHOR)
    assert 0 < i < j, "切不出可执行面剥离那一段：锚点变了 ⇒ 这条判据会是空的"
    seg = src[i:j]
    for name in ("function stripRemoteExecutables", "_EXEC_TAGS_RE_LONE",
                 "_EVENT_ATTR_RE", "_PSEUDO_URL_RE"):
        assert name in seg, "切出的段里缺 %s ⇒ 刀不完整" % name
    return seg


def _exec_products(tmp_path, samples):
    node = _node()
    fn = tmp_path / "exec_knife.js"
    fn.write_text(_exec_knife_src() + "\nmodule.exports = stripRemoteExecutables;\n",
                  encoding="utf-8", newline="\n")
    runner = tmp_path / "exec_run.js"
    runner.write_text("const f = require(%r);\nprocess.stdout.write(JSON.stringify(%s.map(f)));\n"
                      % (str(fn).replace("\\", "/"), json.dumps(samples, ensure_ascii=False)),
                      encoding="utf-8", newline="\n")
    r = subprocess.run([node, str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=120)
    assert r.returncode == 0, "node 跑可执行面刀崩了：\n%s\n%s" % (
        (r.stdout or "")[-300:], (r.stderr or "")[-800:])
    return json.loads(r.stdout)


_EXEC_HOSTILE = [
    '<img src="x" onerror="alert(1)">',
    '<script>alert(1)</script>',
    '<scr<script>ipt>alert(1)</scr</script>ipt>',       # 嵌套骗过成对匹配的那一类
    '<a href="javascript:alert(1)">点我</a>',
    '<iframe src="https://player.vimeo.com/x/1"></iframe>',
    '<svg onload="alert(1)"><circle r="2"/></svg>',
    '<form action="https://evil/"><input name="a"></form>',
    '<object data="data:text/html;base64,PHNjcmlwdD4="></object>',
    '<link rel="stylesheet" href="https://evil/c.css">',
    '<style>@import url("https://evil/x.css");</style>',
]
# 良性那一半与敌意那一半同权重：刀要是把正文/图/链接一起吞了，就是"用一个新故障换旧故障"。
_EXEC_BENIGN = [
    '<p>正文<strong>加粗</strong>与<a href="https://x.test/1">链接</a></p>',
    '<img src="https://a.test/b.png" alt="图">',
    '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">',
    '<video src="https://m.test/a.mp4" controls></video>',
]


def test_article_exit_strips_executable_surface(tmp_path):
    """行为：敌意样本里"能执行"的一样不许活；良性内容必须**逐字**不动。"""
    out = _exec_products(tmp_path, _EXEC_HOSTILE + _EXEC_BENIGN)
    for i, got in enumerate(out[:len(_EXEC_HOSTILE)]):
        low = got.lower()
        assert "onerror" not in low and "onload" not in low and " onclick" not in low, (
            "内联事件活下来了：进 %r ⇒ 出 %r" % (_EXEC_HOSTILE[i], got))
        assert "javascript:" not in low, "伪协议 URL 活下来了：%r" % got
        for tag in ("script", "style", "iframe", "object", "embed", "form", "link", "meta", "base"):
            assert "<" + tag not in low, "危险标签 %s 没剥掉：%r" % (tag, got)
    for i, got in enumerate(out[len(_EXEC_HOSTILE):]):
        assert got == _EXEC_BENIGN[i], "良性内容被吃掉了：进 %r ⇒ 出 %r" % (_EXEC_BENIGN[i], got)
    print("[可执行面] 敌意 %d 条全清、良性 %d 条逐字不动" % (len(_EXEC_HOSTILE), len(_EXEC_BENIGN)))


def test_article_exit_wires_the_scrub_at_the_single_choke_point():
    """接线：三条现抓支路汇成的那**一行**必须过刀；快照那条（构建期已过白名单）不许再过。"""
    src = _read(API_ARTICLE)
    assert src.count("stripRemoteExecutables(") == 2, (
        "刀应当是【定义 1 + 唯一调用点 1】，实得 %d" % src.count("stripRemoteExecutables("))
    assert "const content = stripRemoteExecutables(BODY.capBody(" in src, (
        "出口那一行没接上刀 ⇒ 现抓通道仍可出厂可执行 HTML")
    assert re.search(r"content:\s*BODY\.capBody\(entry\.content\)", src), (
        "快照那条出口的形状变了（这条接线判据的前提，别让它被顺手接上第二遍清洗）")


def test_article_snapshot_channel_is_capped_but_not_normalized_again():
    """快照通道的 `fc` 在构建期已过规范 ⇒ 运行时只补 cap，重复 normalize 会二次改写。

    `normalizeBodyHtml` 幂等的前提是"输入已规范"，但二次绝对化会把已经绝对化的值再喂给
    urlJoin 一次（`https://a/x` 这种在 base 同域时结果相同，但 `data:`/`#` 之外仍有形态会动），
    而最坏的是排查时分不清是哪一层改写的。所以这里数次数，不数"有没有"。
    """
    src = _read(API_ARTICLE)
    assert re.search(r"BODY\.capBody\(\s*entry\.content\s*\)", src), \
        "快照通道没走 capBody（构建期的 fc 也要在运行时出口裁一次）"
    n = len(re.findall(r"BODY\.normalizeBodyHtml\(", src))
    assert n == 1, "normalizeBodyHtml 在 article.js 里出现 %d 次，应当只有实时抽取那一次" % n


# ---------------------------------------------------------------- Task 4：真产物（接线不是断链的直接证据）
def _rss_pipeline_src():
    """切 api/rss.js 里 extractTag..fetchOne 之间的纯函数（含 parseFeed），与既有判据同一手法。"""
    text = _read(API_RSS)
    a = text.find("function extractTag(")
    b = text.find("async function fetchOne(")
    assert 0 < a < b, "api/rss.js 里找不到 extractTag..fetchOne 区段，helper 结构变了"
    seg = text[a:b]
    assert "function parseFeed(" in seg and "function sanitizeHtml(" in seg, "区段缺 parseFeed 或 sanitizeHtml"
    return seg


def _parse_feed_product(tmp_path, xml):
    """用 node 跑**真** parseFeed，返回 items（不发任何外网请求，输入是本地字符串）。"""
    runner = tmp_path / "parse_feed_product.js"
    runner.write_text(
        # COVER/BODY 必须先注进同一作用域：parseFeed 会调它们，而切片切不到文件顶部的 require
        # （漏了就是 ReferenceError 式假红，与 test_parse_parity_js.py:60 那条教训同形）。
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\n"
        "eval(src);\n"
        "const items = parseFeed(%s, 'probe', 50);\n"
        "process.stdout.write(JSON.stringify(items));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(_rss_pipeline_src()), json.dumps(xml)),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "本地 parseFeed 跑崩：\n%s\n%s" % ((r.stdout or "")[-400:], (r.stderr or "")[-1200:])
    return json.loads(r.stdout)


LAZY_XML = (u'<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            u'<channel><item><title>t</title>'
            u'<link>https://example.com/post/one/</link>'
            u'<description>摘要</description>'
            u'<content:encoded><![CDATA[<p>正文</p>'
            u'<img data-src="/imgs/a.png" alt="a">]]></content:encoded>'
            u'<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')


def test_realtime_parse_feed_promotes_lazy_src_and_absolutizes_it(tmp_path):
    """真产物三件事：data-src 被提升、相对路径被绝对化、尾巴没有半个标签。

    这一条是"接线不是断链"的直接证据 —— 源码里写了 `BODY.` 但产物里图还是没了，
    同样是白接（Task 2 的实测就是栽在顺序上：整枚 <img> 被 sanitize 删掉只剩 <p>）。
    """
    items = _parse_feed_product(tmp_path, LAZY_XML)
    assert len(items) == 1, "fixture 应该解析出 1 条，实际 %d" % len(items)
    fc = items[0].get("fullContent") or ""
    assert fc, "content:encoded 没进 fullContent（运行时入口形状变了）"
    assert 'src="https://example.com/imgs/a.png"' in fc, \
        "懒加载没被提升+绝对化（规范层没接上，或接在 sanitize 之后）：%s" % fc
    assert "<img" in fc, "整枚 <img> 又不见了：%s" % fc
    assert "data-src" not in fc, "data-src 该被 sanitize 剥掉，留着说明 sanitize 没跑：%s" % fc


def test_realtime_parse_feed_cap_leaves_no_dangling_tag(tmp_path):
    """超长正文必须过 capBody：截断点落在标签里时，半个标签要抹掉。

    fixture 是算出来的：让第 50000 个码点正好落在 `<img ...>` 内部 ⇒ 裸切片一定留下
    `<img src="https://e` 这种残骸（阅读器 innerHTML 一插就是整段渲染崩）。
    """
    pad = 49990
    body = u"<p>" + u"y" * pad + u"</p><img src=\"https://e.test/x.png\">"
    assert 50000 < len(body) < 50100, "fixture 本身要刚好跨过上限，实际 %d" % len(body)
    xml = (u'<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
           u'<channel><item><title>t</title><link>https://example.com/post/two/</link>'
           u'<description>d</description><content:encoded><![CDATA[' + body + u']]></content:encoded>'
           u'<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')
    fc = _parse_feed_product(tmp_path, xml)[0]["fullContent"]
    assert len(fc) <= 50000, "没截断：%d 字符出厂" % len(fc)
    assert not re.search(r"<[^>]*$", fc), "尾巴挂着半个标签（还是裸切片）：%r" % fc[-40:]
    assert not re.search(r"&[#A-Za-z0-9]*$", fc), "尾巴挂着残缺实体：%r" % fc[-40:]
    assert fc.endswith("</p>"), "截断点没收敛到干净边界：%r" % fc[-24:]
    assert len(fc) > 49000, "截得太狠（正文被整段丢了）：%d" % len(fc)


# ================================================================ 对抗审查 ①：Atom 实时出口也产 fc
# `parseFeed` 的 Atom 分支与 RSS 分支是**两段代码**（台账 §20 的原始事故形态就是"只修一条"）。
# 审查实测：同一份 Atom 文档，构建期 Python 出 `full_content=<p>转义正文
# <img src="https://y.example/img/a.png"></p>`（提 src、绝对化、capBody、deepClean 四把刀全开火），
# 而 `api/rss.js` 的 Atom 分支只造 `{t,u,s,d}` ⇒ 四把刀在这条出口集体缺席，卡片根本没有 `fc`。
# 下面三条按"四把刀逐个可见 + 两条出口同口径 + 出厂卡片有 fc"写，全部本地现造 XML，零网络。
ATOM_LINK = u"https://example.com/post/one/"


def _atom_feed(body, link=ATOM_LINK, summary=u"摘要"):
    return (u'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            u'<entry><title>t</title><link href="' + link + u'"/>'
            u'<summary>' + summary + u'</summary>'
            u'<content type="html"><![CDATA[' + body + u']]></content>'
            u'<published>Tue, 01 Sep 2026 10:00:00 GMT</published></entry></feed>')


def _rss_item(body, link=ATOM_LINK, summary=u"摘要"):
    return (u'<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            u'<channel><item><title>t</title><link>' + link + u'</link>'
            u'<description>' + summary + u'</description>'
            u'<content:encoded><![CDATA[' + body + u']]></content:encoded>'
            u'<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')


def test_realtime_atom_entry_produces_full_content(tmp_path):
    """Atom 出口的四把刀逐个可见：提升 data-src、绝对化、sanitize 掉懒加载属性、脱站内导航。"""
    body = (u'<p>转义正文 <img data-src="/imgs/a.png" alt="a"></p>'
            u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署</p>')
    items = _parse_feed_product(tmp_path, _atom_feed(body))
    assert len(items) == 1, "fixture 应解析出 1 条 Atom entry，实际 %d" % len(items)
    fc = items[0].get("fullContent") or ""
    assert fc, ("Atom 分支没产 fullContent —— 提 src / 绝对化 / capBody / deepClean 四把刀"
                "在这条出口集体缺席（审查实测：Python 有 full_content，JS 无 fc）")
    assert 'src="https://example.com/imgs/a.png"' in fc, "懒加载没提升+绝对化：%r" % fc
    assert "data-src" not in fc, "sanitize 没跑（data-src 留在出厂正文里）：%r" % fc
    assert "/tag/kubernetes" not in fc and "Kubernetes" in fc, "deepClean 没跑：%r" % fc
    # 出厂卡片：没有 fc 就没有"有全文"这件事，阅读器只会退回摘要
    card = _card_product(tmp_path, _atom_feed(body))[0]
    assert card.get("fc"), "toCardItem 没把 Atom 的 fullContent 带进出厂字段 fc"


def test_realtime_atom_and_rss_exits_agree_on_the_same_body(tmp_path):
    """同口径判据（复评 B2 升级）：两条 JS 出口互等**还不够**，必须与 Python 同判。

    两条出口各写一份清洗链就是下一个分叉源（构建期 `_fetch_rss` 的 Atom 分支与
    `_parse_rss_item` 吃的就是同一条链），所以这里不比"都有 fc"，比相等。
    原判据只拿 Atom 与 RSS 两条 JS 出口互相比 ⇒ 两份一起错也照样绿（复评实测的
    `fc` 门槛就是同时错在两条出口上的），故保留原来那条逐字节互等，另加"与参照物相等"这一半。
    """
    body = (u'<p>正文 <img data-src="/i/x.png"></p>'
            u'<p><a href="https://blog.example/author/anna">anna</a> 与 '
            u'<a href="https://openai.com/blog/x">原始公告</a></p>')
    atom = _parse_feed_product(tmp_path, _atom_feed(body))
    rss = _parse_feed_product(tmp_path, _rss_item(body))
    assert atom and rss, "两条出口都该解析出 1 条"
    fc_a = atom[0].get("fullContent") or ""
    fc_r = rss[0].get("fullContent") or ""
    assert fc_a, "Atom 出口没有 fc（①的原始形状）"
    assert fc_a == fc_r, "Atom 与 RSS 两条实时出口给出不同正文：\n atom: %r\n  rss: %r" % (fc_a, fc_r)
    assert "https://example.com/i/x.png" in fc_a, "绝对化没开火：%r" % fc_a
    assert 'href="https://openai.com/blog/x"' in fc_a, "外部正文链接被脱了：%r" % fc_a
    # ── 升级的那半：同一份 Atom 文档喂参照物 Python `_fetch_rss`，fc 必须逐字节相同 ──
    py = _py_feed_products([_atom_feed(body)])[0]
    assert py, "参照物没解析出条目（判据前提失效）"
    fc_p = py[0].get("full_content") or ""
    assert fc_p, "参照物 Python 侧没出 full_content ⇒ 对账没有左边"
    assert fc_a == fc_p, "两条 JS 出口一致但一起与 Python 分叉：\n js: %r\n py: %r" % (fc_a, fc_p)


def test_realtime_atom_cap_leaves_no_dangling_tag(tmp_path):
    """cap 这把刀也必须在 Atom 出口开火：截断点落在标签里时不许留残骸。"""
    pad = 49990
    body = u"<p>" + u"y" * pad + u"</p><img src=\"https://e.test/x.png\">"
    assert 50000 < len(body) < 50100, "fixture 本身要刚好跨过上限，实际 %d" % len(body)
    fc = _parse_feed_product(tmp_path, _atom_feed(body))[0].get("fullContent") or ""
    assert fc, "Atom 出口没有 fc，cap 无从谈起"
    assert len(fc) <= 50000, "没截断：%d 字符出厂" % len(fc)
    assert not re.search(r"<[^>]*$", fc), "尾巴挂着半个标签：%r" % fc[-40:]
    assert fc.endswith("</p>"), "截断点没收敛到干净边界：%r" % fc[-24:]


# ================================================== 复评 B1+B2：Atom 出口的 s / fc 与 Python 同判
# 根因是一件事：**JS Atom 分支的摘要/全文判定与 Python 不同源**，两处各错一格 ——
#   B1 `const summary = summaryTag || extractTag(entry,'content')` —— `<summary>` 缺失时
#      回退用正文。参照物 Python 就一句 `desc = _strip_html(summary_raw)`
#      （`summary_raw = e.findtext(ns+"summary") or ""`），**没有回退**；
#      上一波又给同一条产了 `fc` ⇒ 阅读器先渲 `.r2-summary`（正文前 200 字）
#      再 `_insertFulltext(fc)`（同一正文全文）= 正文显示两遍。
#   B2 `contentEncoded.length > summaryTag.length` —— 比的是两个**原始标签**的长度
#      （CDATA 包装白送 12 字符）。参照物是 `len(atom_content) > len(desc)`
#      —— 清洗后的正文 vs 剥标签后的摘要。复评构造的形状全部不同判，
#      含"`<summary>` 与 `<content>` 逐字相同"（py 出 fc、js 不出）与
#      "`<summary>短</summary>` + CDATA 正文"（js 出 fc、py 不出）。
# 判据按**行为对账**写：同一份 Atom XML 分别喂参照物 `_fetch_rss`（`_fetch_url` 打桩，零网络）
# 与 `api/rss.js` 的**真** `parseFeed`（node 切片），逐条比 `s` 与 `fc`。
# **语料边界（写清楚，别当成全覆盖）**：`<summary>` 里直接嵌**未转义的真标签**
# （`<summary><p>x</p></summary>`）不在语料内 —— 那是 XML 解析层的事
# （ElementTree `findtext` 只给第一个子元素之前的文本 ⇒ py 得 `''`，正则切片给全文），
# 与本批改的"回退"与"门槛基准"两格无关，改它要动的是解析层而不是这两行。
# 同理，`<content>` 里带双重转义实体的形状不在逐字节对账内（`sanitizeHtml` 仍是顺序级联，
# 该分叉在 `api/rss.js:390` 明写为已知挂起，由 §实体那三条判据单独管）。
ATOM_BODY = u"<p>正文一 正文二</p>"
ATOM_LINK_ = u"https://example.com/post/one/"


def _cdata(t):
    return u"<![CDATA[" + t + u"]]>"


def _atom_shape(summary_inner, content_inner):
    s = (u"" if summary_inner is None else u"<summary>" + summary_inner + u"</summary>")
    c = (u"" if content_inner is None else u"<content type=\"html\">" + content_inner + u"</content>")
    return (u'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
            u'<title>t</title><link href="' + ATOM_LINK_ + u'"/>' + s + c +
            u'<published>2026-10-01T10:00:00Z</published></entry></feed>')


ATOM_PARITY_SHAPES = [
    # —— 复评点名的四个形状 ——
    (u"summary 与 content 逐字相同（同 CDATA）",
     _atom_shape(_cdata(ATOM_BODY), _cdata(ATOM_BODY))),
    (u"summary 缺失（复评 B1 的双显示形状）", _atom_shape(None, _cdata(ATOM_BODY))),
    (u"CDATA 包装白送 12 字符（JS 曾多产）", _atom_shape(u"短", _cdata(u"短"))),
    (u"清洗后正文长于剥标签后的摘要", _atom_shape(u"短", _cdata(ATOM_BODY))),
    # —— 对照组：两侧本来就该同判的形状，缺了它们门槛改反方向也测不出来 ——
    (u"空 summary 标签", _atom_shape(u"", _cdata(ATOM_BODY))),
    (u"纯文本逐字相同（无 CDATA）", _atom_shape(u"只有正文", u"只有正文")),
    (u"正文比摘要短", _atom_shape(_cdata(u"长" * 20), _cdata(u"短"))),
    (u"没有 content", _atom_shape(u"只有摘要", None)),
    (u"content 是空标签", _atom_shape(u"摘要", u"")),
    (u"正文带懒加载图与站内导航", _atom_shape(
        u"摘要", _cdata(ATOM_BODY + u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署</p>'
                       u'<img data-src="/imgs/a.png" alt="a">'))),
    # 基准必须用 `stripHtmlKeepLines`（解实体走 BODY 那格端口、**不压内部空白**）而不是
    # 文件里另一个 `stripHtml`（旧顺序级联，`&#20998;` 整枚吃成空串）。这一格靠这条形状区分：
    # 真长度 4 / 旧级联 3 / 清洗后正文 4 ⇒ 用错基准就会多出一次 fc（电池 B03 打的就是这格）。
    (u"摘要带数字实体（旧级联会吃掉）", _atom_shape(u"分数&#20998;了", _cdata(u"正文一二"))),
    (u"摘要是 HN 模板四行", _atom_shape(
        u"Article URL: https://ex.com/a\nComments URL: https://news.ycombinator.com/item?id=1"
        u"\nPoints: 254\n# Comments: 162", _cdata(ATOM_BODY))),
]

# 语料自检：每条摘要都短于两侧共同的 500 字上限（JS SUMMARY_CAP = Python `_truncate` 默认），
# 这样 `s` 比的是**同一个字符串**，不是"两侧各自截了一刀之后刚好相等"。
# （长度口径那一格另有判据：test_summary_exit_cap_matches_buildtime_default。）
for _n, _x in ATOM_PARITY_SHAPES:
    assert 0 < len(re.sub(r"<[^>]+>", "", _x)) < 200, "语料 %s 超出 200 字，s 对账会失真" % _n


def _parse_feed_products(tmp_path, xmls):
    """node 跑**真** `parseFeed` + 真出口 `toCardItem`，一次给全部形状的 {items, cards}。

    与 `_parse_feed_product` 同一手法（COVER/BODY 先注进同一作用域，样本走 JSON 文件中转
    不进 JS 源码），只是批量化 —— 11 个形状开 1 次 node 而不是 11 次。
    """
    spec_p = tmp_path / "atom_parity_spec.json"
    out_p = tmp_path / "atom_parity_out.json"
    spec_p.write_text(json.dumps(xmls, ensure_ascii=False), encoding="utf-8", newline="\n")
    runner = tmp_path / "atom_parity_product.js"
    runner.write_text(
        "const fs = require('fs');\n"
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\n"
        "eval(src);\n"
        "const docs = JSON.parse(fs.readFileSync(%s, 'utf8'));\n"
        "const out = docs.map(function (x) {\n"
        "  const its = parseFeed(x, 'probe', 50);\n"
        "  return {items: its, cards: its.map(toCardItem)};\n"
        "});\n"
        "fs.writeFileSync(%s, JSON.stringify(out));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(_rss_pipeline_src()),
           json.dumps(str(spec_p).replace("\\", "/")),
           json.dumps(str(out_p).replace("\\", "/"))),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "批量 parseFeed 跑崩：\n%s\n%s" % (
        (r.stdout or "")[-400:], (r.stderr or "")[-1200:])
    got = json.loads(out_p.read_text(encoding="utf-8"))
    assert len(got) == len(xmls), "返回条数与语料不等（%d vs %d）" % (len(got), len(xmls))
    return got


# ── RSS 出口那一半的同一对缺陷：**摘要回退那一格本批收了，门槛那一格照旧挂账**────────
# 实测（同一份 `<item>`、`<description>` 缺失、零网络，2026-10-05，改前）：
#     参照物 py：summary=''  full_content='<p>正文一 正文二</p>'
#     改  前 js：s='正文一 正文二'  fc='<p>正文一 正文二</p>'   ⇒ 同一份正文两遍
# 收掉的那一格（B1）：`summaryRaw` / `summary` 都删掉了 `|| contentEncoded` 回退，
# 判据 = 下面 `RSS_NOFALLBACK_SHAPES` 那两条（行为对账 + 反重复），靶 = tools/mut_reader.py 的 B04。
# **没修的那一半（B2）仍然没有判据盖着**：RSS 分支的 `fc` 门槛比的还是两个**原始标签**的长度
# （`contentEncoded.length > desc.length`，CDATA 包装白送 12），而参照物比的是
# 清洗后的正文 vs 剥标签后的摘要。为什么不顺手修：把"先清洗再定门槛"搬到 RSS 分支，
# `parseFeed` 就会对**每一条带 `<content:encoded>` 的条目**都走一遍 `BODY`，而三条**只注入
# `COVER`、不注入 `BODY`** 的他人线 node 切片判据（`tests/rss_cover/test_realtime_cover_js.py`、
# `tests/rss_source_coverage/test_dateless_source_guard.py`，本批不许改）当场红在
# `ReferenceError: BODY is not defined`（实测 7 条红）⇒ 只能退回登记，不拿"半边对齐"充数。
# 前置动作（下一批）：那两个 harness 的 runner 各加一行 `const BODY = require(...)`，
# 或与 `legacyEntityCascade` 上方"已知挂起"那条一起把 require 挪到切片可覆盖的位置；
# 前置到位后 `_parity_diffs()` 直接喂一份 RSS 语料就能收 fc 那一面 —— 它是方言无关的。
# （正因如此，下面两条 RSS 判据**只比 `s`、不比 `fc` 的存在性/逐字节**：那半是 B2 的账，
#   把它混进来会让"挂起"变成"看着已收"。xgo 那一格也另说：Python `_parse_rss_item` 有
#   `_is_xgo_content` 专支、JS 没有，那是既有整块分叉，不是这两行门槛能收的账。）


def _py_feed_products(xmls, key_prefix=u"atom_parity_probe"):
    """同一批 XML 喂**参照物** `build_rss_aggregator._fetch_rss`（`_fetch_url` 打桩，零网络）。

    `key_prefix` 只是缓存键的名字空间：Atom 与 RSS 两批语料各自用各自的键，
    免得一条判据留下的 `_rss_cache` 残留被另一条读到（`_fetch_rss` 的缓存在桩之前）。
    """
    mod = _build_module()
    out = []
    old = mod._fetch_url
    try:
        for i, xml in enumerate(xmls):
            key = u"%s_%d" % (key_prefix, i)
            mod._rss_cache.pop(key, None)      # 缓存在 `_fetch_rss` 之前，不桩到桩后
            mod._fetch_url = (lambda doc: (lambda *a, **k: doc.encode("utf-8")))(xml)
            out.append(mod._fetch_rss({"key": key, "name": u"对账探针源",
                                       "url": "https://example.invalid/feed",
                                       "cat": "tech"}) or [])
    finally:
        mod._fetch_url = old
        for i in range(len(xmls)):
            mod._rss_cache.pop(u"%s_%d" % (key_prefix, i), None)
    return out


def _shows_body_twice(s, fc):
    """「同一份正文显示两遍」这个形状本身：`s` 非空、`fc` 非空，且 `s` 是 `fc` 的**前缀截断**。"""
    if not s or not fc:
        return False
    plain = _build_module()._strip_html(fc)
    head = s[:-1] if s.endswith(u"…") else s
    return bool(head) and plain.startswith(head)


def test_atom_parity_detects_the_double_display_shape(tmp_path):
    """反重复判据**自己**得会开火：坏形状喂进去必须判 True（否则下一条是空判据）。"""
    assert _shows_body_twice(u"正文一 正文二", u"<p>正文一 正文二</p>"), \
        "检测器认不出「摘要就是正文前缀」这个形状 ⇒ 反重复判据是空的"
    assert _shows_body_twice(u"正文一…", u"<p>正文一 正文二</p>"), "带省略号的截断没认出来"
    assert not _shows_body_twice(u"摘要", u"<p>正文一 正文二</p>"), "正常摘要被误判成重复显示"
    assert not _shows_body_twice(u"", u"<p>正文一 正文二</p>"), "s 为空不该算重复"
    assert not _shows_body_twice(u"正文一", u""), "fc 为空不该算重复"


def test_realtime_atom_never_ships_the_same_body_as_both_summary_and_fulltext(tmp_path):
    """反重复判据（B1 的用户可见后果）：JS 不得比参照物多出一遍正文。

    三面各钉一格，合成一条才咬得住 B1：
      1. **绝对**：raw 里 `<summary>` 缺失或为空的条目，出厂 `s` 必须是空串 ——
         回退到 `<content>` 就是"先渲正文前 200 字、点开再插一遍全文"。
      2. **相对**：全语料里"s 是 fc 的前缀截断且 fc 非空"的**形状集合**，JS 必须等于 Python
         （JS 不得比参照物多出任何一条）。不能只写"JS 侧一条都不许出现"，因为参照物
         自己在"`<summary>` 与 `<content>` 逐字相同"这一格就是两遍
         （`desc='正文一 正文二'` 与 `atom_content='<p>正文一 正文二</p>'` 同源），
         而派工的口径是**以 Python 为准** —— 下面第 3 面把这件事从"绕过"变成"点名"。
      3. **非空跑**：那条参照物自身的两遍必须真的出现在两侧的同判集合里，
         免得这条判据退化成"两边都没有所以相等"。
    **覆盖面**：只有 Atom 出口。RSS 出口那一半有同一对缺陷（实测读数与为什么不修，
    记在上方 `_py_feed_products` 前那段登记里），本条不假装盖住了它。
    """
    shapes = ATOM_PARITY_SHAPES
    js = _parse_feed_products(tmp_path, [x for _n, x in shapes])
    py = _py_feed_products([x for _n, x in shapes])
    js_twice, py_twice = set(), set()
    for (name, _x), j, p in zip(shapes, js, py):
        card = (j["cards"][0] if j["cards"] else {})
        if _shows_body_twice(card.get("s") or "", card.get("fc") or ""):
            js_twice.add(name)
        if _shows_body_twice(p[0].get("summary") or "", p[0].get("full_content") or ""):
            py_twice.add(name)
    assert not (js_twice - py_twice), (
        "JS 比参照物多出一遍正文的形状：%s —— 摘要回退或门槛基准又分叉了" % sorted(js_twice - py_twice))
    assert js_twice == py_twice, "两侧「正文两遍」的集合不等：js=%s py=%s" % (sorted(js_twice), sorted(py_twice))
    # 1. summary 缺失/为空 ⇒ 出厂 s 必须是空串
    for name in (u"summary 缺失（复评 B1 的双显示形状）", u"空 summary 标签"):
        i = [n for n, _x in shapes].index(name)
        s = (js[i]["cards"][0].get("s") or "") if js[i]["cards"] else "（没有条目）"
        assert s == "", "「%s」出厂 s=%r —— `<summary>` 没内容时不许拿正文顶上" % (name, s[:60])
    # 3. 参照物自身那一格必须在场（点名它，别让它藏在"两侧相等"里）
    verbatim = u"summary 与 content 逐字相同（同 CDATA）"
    assert verbatim in py_twice and verbatim in js_twice, (
        "「%s」这一格在两侧都没出现 ⇒ 语料失效了：参照物在该形状下本来就会把同一份正文"
        "既放进 summary 又放进 full_content，这是「以 Python 为准」的既有代价，"
        "登记在报告里；判据必须看得见它" % verbatim)


def _parity_diffs(tmp_path, shapes):
    """同一批 XML 分别喂参照物 `_fetch_rss` 与真 `parseFeed`，逐条比 s / fc，返回分叉清单。

    三面一起比，缺任一面都留得出盲区：
      · `s` **存在性/逐字** —— B1 的回退只在这一面红（py `s=''`、js `s='只有正文'`）；
      · `fc` **存在性** —— B2 的门槛基准在这一面红（逐字相同 py 出 / js 不出，
        CDATA 白送那格反过来 js 出 / py 不出）；
      · `fc` **逐字节** —— 存在性相同但清洗链少一把刀（审查 ① 的原始形状）也红。
    """
    xmls = [x for _n, x in shapes]
    js = _parse_feed_products(tmp_path, xmls)
    py = _py_feed_products(xmls)
    diffs, s_ok, fc_ok = [], 0, 0
    for (name, _x), j, p in zip(shapes, js, py):
        assert p, "%s：参照物一条都没解析出来 ⇒ 对账没有左边" % name
        assert j["items"], "%s：JS 一条都没解析出来 ⇒ 对账没有右边" % name
        pj, jc = p[0], (j["cards"][0] if j["cards"] else {})
        ji = j["items"][0]
        s_p, s_j = pj.get("summary") or "", jc.get("s") or ""
        f_p, f_j = pj.get("full_content") or "", ji.get("fullContent") or ""
        if s_p != s_j:
            diffs.append("%s\n  s  py=%r\n     js=%r" % (name, s_p[:60], s_j[:60]))
        else:
            s_ok += 1
        if bool(f_p) != bool(f_j):
            diffs.append("%s\n  fc 存在性  py=%s(%d 字)  js=%s(%d 字)"
                         % (name, bool(f_p), len(f_p), bool(f_j), len(f_j)))
        elif f_p != f_j:
            diffs.append("%s\n  fc 逐字节  py=%r\n             js=%r" % (name, f_p[:80], f_j[:80]))
        else:
            fc_ok += 1
    assert not diffs, "实时出口与参照物不同判 %d 处：\n%s" % (len(diffs), "\n".join(diffs))
    assert s_ok == len(shapes), "s 对账没有跑满全部形状（%d/%d）" % (s_ok, len(shapes))
    assert fc_ok == len(shapes), "fc 对账没有跑满全部形状（%d/%d）" % (fc_ok, len(shapes))
    # fc 两侧都为真、两侧都为零的形状都要在语料里出现过，否则"存在性同判"是单边凑出来的。
    both = [bool(p[0].get("full_content")) for p in py]
    assert any(both) and not all(both), \
        "语料的 fc 存在性全是同一边（%s）⇒ 门槛改坏也测不出来" % both
    return diffs


def test_realtime_atom_exit_matches_python_s_and_fc_shape_by_shape(tmp_path):
    """行为对账（Atom）：JS 的 `s`/`fc` 与 Python 的 `summary`/`full_content` 逐条同判。

    `_parity_diffs` 是方言无关的 —— RSS 那一半已在
    `test_realtime_rss_exit_matches_python_s_and_fc_shape_by_shape` 接上同一把尺（2026-10-05 B2 收口）。
    """
    diffs = _parity_diffs(tmp_path, ATOM_PARITY_SHAPES)
    assert not diffs, "Atom 实时出口与参照物不同判 %d 处：\n%s" % (len(diffs), "\n".join(diffs))


# ============================================== 复评 B1 的 RSS 那一半：摘要不许回退正文
# 与上面 Atom 那两条同形的一对，喂的是 `<item>` + `<content:encoded>`：`parseFeed` 是**两段
# 代码**（审查 ① 的原始事故形态就是"只修一条"），Atom 语料一条都盖不住 RSS 分支。
# 实测读数（零网络，2026-10-05，改前）：py `summary=''` / js `s='正文一 正文二'` + 同份 `fc`。
# **口径边界**：下面这两条只比 `s` 那一格，**不比 `fc` 的存在性集合与逐字节** ——
# `fc` 那一格由 `test_realtime_rss_exit_matches_python_s_and_fc_shape_by_shape` 负责
# （复评 B2 于 2026-10-05 收口：RSS 门槛改成"清洗后的正文 vs 剥标签后的摘要"，
# 靶 B05 打在它上面）。唯一例外是
# `test_realtime_rss_exit_matches_python_summary_shape_by_shape` 末句那条
# `full_content`/`fullContent` **都非空**的断言：它只声明"这一格两侧确实都出了正文"，
# 好让反重复判据不是白得的，不比长度、不比字节。
RSS_BODY = u"<p>正文一 正文二</p>"
RSS_LINK = u"https://example.com/post/one/"
RSS_NO_DESC_SHAPE = u"description 缺失（B1 的双显示形状）"


def _rss_shape(desc_inner, content_inner):
    """RSS `<item>`：`desc_inner is None` ⇒ **整枚 `<description>` 不出现**（缺陷的触发形状）。"""
    d = u"" if desc_inner is None else u"<description>" + desc_inner + u"</description>"
    c = (u"" if content_inner is None
         else u"<content:encoded>" + _cdata(content_inner) + u"</content:encoded>")
    return (u'<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            u'<channel><item><title>t</title><link>' + RSS_LINK + u'</link>' + d + c +
            u'<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')


# description 一律用**纯文本形状**（不被 HN 刀/粘连刀改写）⇒ 参照物的 `summary`、parseFeed 的
# `summary`、出厂卡片的 `s` 三格能逐字互比；那两把刀自己的对账另有判据
# （`test_realtime_rss_summary_rewrites_the_hn_template` / `..._strips_the_glued_url`），不混进来。
RSS_NOFALLBACK_SHAPES = [
    (RSS_NO_DESC_SHAPE, _rss_shape(None, RSS_BODY)),
    (u"description 是空标签", _rss_shape(u"", RSS_BODY)),
    (u"description 只有空白", _rss_shape(u"    ", RSS_BODY)),
    (u"description 缺失且没有正文", _rss_shape(None, None)),
    (u"正常摘要 + 正文", _rss_shape(u"摘要", RSS_BODY)),
    (u"CDATA 包着的摘要 + 正文", _rss_shape(_cdata(u"<p>摘要</p>"), RSS_BODY)),
    (u"摘要有、正文没有", _rss_shape(u"只有摘要", None)),
    (u"摘要带 &amp; 实体（两侧同圈解码）", _rss_shape(u"Tom &amp; Jerry", RSS_BODY)),
]

# 与 Atom 语料同一条自检：摘要必须短于两侧共同的 500 字上限（SUMMARY_CAP = `_truncate` 默认），
# 这样三格比的是**同一个字符串**，不是"两侧各自截了一刀之后刚好相等"。
for _n, _x in RSS_NOFALLBACK_SHAPES:
    assert 0 < len(re.sub(r"<[^>]+>", "", _x)) < 200, "RSS 语料 %s 超出 200 字，s 对账会失真" % _n

# ── 复评 B2 收口：RSS 支的 fc 门槛也拿 `_parity_diffs` 那把尺（s 逐字 + fc 存在性 + fc 逐字节）。
# 前提有两件：① `api/rss.js` 的 RSS 门槛从"两个**原始标签**的长度"改成"清洗后的正文 vs
# 剥标签后的摘要"（与 Python `build_rss_aggregator.py:3297` 同判）；
# ② 他人线 harness `tests/rss_cover/test_realtime_cover_js.py` 的 node 驱动补了 `BODY` require
# （它以前只注入 `COVER`，任何让 RSS 支过清洗链的改动都会红在 ReferenceError，实测 7 条）。
RSS_PARITY_SHAPES = [
    (u"摘要短、正文长 ⇒ 两侧都出 fc", _rss_shape(u"这是摘要", u"<p>" + u"正文" * 20 + u"</p>")),
    (u"剥标签后两侧逐字相同 ⇒ 两侧都不出", _rss_shape(u"同一句话不要出现两遍", u"同一句话不要出现两遍")),
    (u"正文只是摘要再加一层标签 ⇒ 都不出", _rss_shape(u"摘要文字一二三", u"<p>摘要文字一二三</p>")),
    (u"description 缺失 ⇒ 两侧 s 空、都出 fc", _rss_shape(None, u"<p>只有正文的一段话</p>")),
    (u"清洗后正文比摘要短 ⇒ 都不出 fc", _rss_shape(u"摘要" * 12, u"<div><p>短</p></div>")),
    (u"正文是空标签 ⇒ 都不出", _rss_shape(u"只有摘要", u"")),
    (u"摘要有实体（两侧同圈解码）", _rss_shape(u"Tom &amp; Jerry 的摘要", u"<p>" + u"正文" * 12 + u"</p>")),
]


def test_realtime_rss_exit_matches_python_s_and_fc_shape_by_shape(tmp_path):
    """行为对账（RSS 支）：与 Atom 那条共用一把尺子，把 B2 那一半也接上。

    旧写法（比原始标签长度）会在第 2/3/5 格上 js 出 fc、py 不出 —— CDATA 包装白送 12 字符、
    正文标签让左边虚高，正是"摘要与全文在阅读器里同段两遍"的上游来源之一。
    """
    for _n, _x in RSS_PARITY_SHAPES:
        assert 0 < len(re.sub(r"<[^>]+>", "", _x)) < 200, "RSS fc 语料 %s 超出 200 字" % _n
    diffs = _parity_diffs(tmp_path, RSS_PARITY_SHAPES)
    assert not diffs, "RSS 实时出口与参照物不同判 %d 处：\n%s" % (len(diffs), "\n".join(diffs))


def _rss_three_way(tmp_path, shapes, key_prefix):
    """同一批 `<item>` 喂两侧，给每格读数：参照物 `summary` / parseFeed `summary` / 出厂 `s`。

    回退写在 **两行**上（`summaryRaw` 与 `summary`），所以三格必须一起量：
      · parseFeed 的 `summary` —— 它是微信那格的去重键的一半
        （`dedupSourceItems` 的 `isWechat` 支用 `__biz + normText(title) + normText(summary)`，
        参照物 `_dedup_source_items` 用的是**没有回退**的 `summary`）；回退让实时通道的
        "同一篇文章"与构建期认出的不是同一篇；
      · 出厂卡片的 `s` —— `toCardItem` 走 `cleanSummary(it.summaryRaw)`，阅读器 `.r2-summary`
        渲的就是它，也正是"同一份正文两遍"里的那第一遍。
    少比任一格，另一格被改回退时这条判据就是空的。
    """
    xmls = [x for _n, x in shapes]
    js = _parse_feed_products(tmp_path, xmls)
    py = _py_feed_products(xmls, key_prefix=key_prefix)
    rows = []
    for (name, _x), j, p in zip(shapes, js, py):
        assert p, "%s：参照物一条都没解析出来 ⇒ 对账没有左边" % name
        assert j["items"], "%s：JS 一条都没解析出来 ⇒ 对账没有右边" % name
        card = (j["cards"][0] if j["cards"] else {})
        rows.append((name, p[0], j["items"][0], card))
    return rows


def test_realtime_rss_exit_matches_python_summary_shape_by_shape(tmp_path):
    """行为对账（RSS 分支）：同一份 `<item>` 三格 `s` 同判 ⇒ 缺 `<description>` 就是**空串**。

    参照物是 `build_rss_aggregator._parse_rss_item`：`desc_raw = _rss_text(it, "description")`、
    `desc = _strip_html(desc_raw)` —— **没有 `|| content_encoded` 这一格回退**，缺 description
    就出厂 `summary=''`。JS 的 RSS 分支以前写 `desc || contentEncoded`，于是同一条既出 `s`
    （正文前 200 字）又出 `fc`（同一份正文）⇒ 阅读器里两遍。
    """
    rows = _rss_three_way(tmp_path, RSS_NOFALLBACK_SHAPES, u"rss_parity_probe")
    diffs, empty, nonempty = [], 0, 0
    for name, pi, ji, card in rows:
        s_p = pi.get("summary") or ""
        s_it = ji.get("summary") or ""
        s_c = card.get("s") or ""
        if s_p != s_c:
            diffs.append("%s\n  s 出厂 py=%r\n       js=%r" % (name, s_p[:60], s_c[:60]))
        if s_p != s_it:
            diffs.append("%s\n  s parseFeed py=%r\n            js=%r" % (name, s_p[:60], s_it[:60]))
        empty += 1 if not s_p else 0
        nonempty += 1 if s_p else 0
    assert not diffs, "RSS 实时出口的摘要与参照物不同判 %d 处：\n%s" % (
        len(diffs), "\n".join(diffs))
    # 派工点名的那一格：三处都必须读作空串，且这一格两侧**确实都出了正文**（否则"都空"是白得的）
    i = [n for n, _x in RSS_NOFALLBACK_SHAPES].index(RSS_NO_DESC_SHAPE)
    _name, pi, ji, card = rows[i]
    assert (pi.get("summary") or "") == "", "参照物这一格就不是空串了（语料失效）：%r" % pi.get("summary")
    assert (card.get("s") or "") == "", (
        "`<description>` 缺失时出厂 s=%r —— 不许拿 `<content:encoded>` 顶上，"
        "阅读器先渲这 200 字、点开再插同一份全文" % (card.get("s") or "")[:60])
    assert (ji.get("summary") or "") == "", (
        "`<description>` 缺失时 parseFeed summary=%r —— 这一格还兼作实时去重键" % (ji.get("summary") or "")[:60])
    assert (pi.get("full_content") or "") and (ji.get("fullContent") or ""), (
        "这一格两侧都没出正文 ⇒ 反重复那一半失去前提，语料要修")
    assert empty >= 2 and nonempty >= 2, (
        "语料的 s 空/非空不两边都有（空 %d / 非空 %d）⇒ 回退改回去也测不出来" % (empty, nonempty))


def test_realtime_rss_never_ships_the_same_body_as_both_summary_and_fulltext(tmp_path):
    """反重复（RSS 分支）：任一条目不得同时「摘要非空」且「摘要是正文剥标签后的前缀」。

    三面各量一遍（出厂卡片、parseFeed 条目、参照物），坏改动**打在哪一行都会在这里露形**：
    回退写在 `summaryRaw`（→ `s`）与 `summary`（→ 条目/去重键）两行，任一行被改回
    `desc || contentEncoded` 就会出现一条 `s`=正文前缀且 `fc`=同一份正文的条目。
    检测器 `_shows_body_twice` 自己开不开火由
    `test_atom_parity_detects_the_double_display_shape` 钉着（两侧共用同一个检测器）。
    """
    rows = _rss_three_way(tmp_path, RSS_NOFALLBACK_SHAPES, u"rss_nodup_probe")
    hits, with_fc = [], 0
    for name, pi, ji, card in rows:
        if _shows_body_twice(card.get("s") or "", card.get("fc") or ""):
            hits.append("%s：出厂卡片 s=%r 是 fc 的前缀" % (name, (card.get("s") or "")[:40]))
        if _shows_body_twice(ji.get("summary") or "", ji.get("fullContent") or ""):
            hits.append("%s：parseFeed 条目 summary=%r 是 fullContent 的前缀"
                        % (name, (ji.get("summary") or "")[:40]))
        if _shows_body_twice(pi.get("summary") or "", pi.get("full_content") or ""):
            hits.append("%s：参照物 summary=%r 是 full_content 的前缀"
                        % (name, (pi.get("summary") or "")[:40]))
        with_fc += 1 if (card.get("fc") or ji.get("fullContent")) else 0
    assert not hits, "同一份正文显示两遍 %d 条：\n%s" % (len(hits), "\n".join(hits))
    assert with_fc >= 2, "语料里没有条目出厂正文（%d 条）⇒ 这条判据是空的" % with_fc


# ================================================================ 对抗审查 ④：构建期规则 2.6 接上实时出口
# 构建期 `_deep_clean_html` 的**规则 2.6**（空锚点 `href="#"` / 站点根链接 `href="/"` **整枚**删，
# 连文字一起）原本只长在 `build_rss_aggregator.py` 一份，实时出口的 `fc` 没有它 ⇒
# 同一份 `content:encoded` 两侧产物不同：Python 出厂 `<p>正文一</p><p>阅读更多</p>…`
# （壳被删空后由规则 3 收尾），JS 的 fc 里还留着 `<p><a href="#"></a>阅读更多</p>`。
#
# 判据按**行为对账**写，不 grep 函数名：同一批样本分别喂
#   · 构建期真函数 `build_rss_aggregator._deep_clean_html`
#   · 运行时真函数 `api/rss.js` 的 `deepCleanHtml`（切片进 node 跑，零网络、零 npm 依赖）
# 逐条比输出的**字节**。三种坏形状各自咬得住：整条刀没接（两侧不同 ⇒ 对账红）、
# 接错一格（挪到规则 3 之后 ⇒ 空 `<p></p>` 壳留在出厂正文，对账与出口判据都红）、
# 放宽成任意 href（正文里的"见下节" `#sec-2` 被吃 ⇒ 反例判据红）。
EMPTY_ANCHOR_SAMPLES = [
    (u"空锚点壳在正文之后", u'<p>正文一</p><p><a href="#">阅读更多</a></p>'),
    (u"根链接壳在正文之前", u'<p><a href="/">返回首页</a></p><p>正文二</p>'),
    (u"页内锚点不是空锚点", u'<p>见 <a href="#sec-2">第二节</a> 的讨论</p>'),
    (u"大写标签与大写属性名", u'<P><A HREF="#">壳</A></P>正文'),
    (u"壳带额外属性", u'<p><a href="#" title="更多">壳</a>正文</p>'),
    (u"空串 href 不在刀形里", u'<p><a href="">空值</a>正文</p>'),
    (u"根路径带内容", u'<p><a href="/x">真链接</a>正文</p>'),
    (u"未闭合的壳删不掉", u'<p>尾巴 <a href="#">未闭合'),
    (u"壳内套标签与换行", u'<p><a href="#"><span>套</span><br>壳</a></p>正文'),
    (u"两枚壳连着排", u'<p><a href="/">甲</a></p><p><a href="#">乙</a></p><p>正文</p>'),
    (u"壳跨行", u'<p>前 <a href="#">跨\n行\n壳</a></p>'),
    (u"一枚 a 上两个 href", u'<p><a href="/x" href="#">双 href</a>正文</p>'),
    (u"空文本的壳", u'<p><a href="#"></a>阅读更多</p>'),
    (u"href 无引号（两侧同盲区）", u'<p><a href=#>壳</a>正文</p>'),
    (u"div 里的壳", u'<div><a href="/">回首页</a></div><p>正文</p>'),
]

EMPTY_ANCHOR_EXIT_BODY = (u'<p>正文一</p><p><a href="#"></a>阅读更多</p>'
                          u'<p><a href="#">阅读全文</a></p>'
                          u'<p>见 <a href="#sec-2">第二节</a> 的讨论</p>')

_BUILD_MOD = {}


def _build_module():
    """构建期那个模块（对账的另一侧就在里面）。

    先认 `sys.modules` 再决定要不要 exec：`build_rss_aggregator` 顶层会把 `sys.stdout`
    重挂一次，同一进程里第二次 exec 会在关闭旧流时把解释器硬崩
    （口径抄 tests/rss_history/_loader.py 的 docstring；手法抄 test_reader_body_images.py）。
    """
    if "mod" not in _BUILD_MOD:
        import sys
        mod = sys.modules.get("build_rss_aggregator")
        if mod is None:
            sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
            from _loader import load_build
            mod = load_build()
        _BUILD_MOD["mod"] = mod
    return _BUILD_MOD["mod"]


def _deep_clean_products(tmp_path, samples):
    """用 node 跑**真** `api/rss.js` 的 deepCleanHtml，逐条给返回值。

    样本走 JSON 文件中转、不进 JS 源码：带引号的样本如果能改写被测脚本，
    那"判据跑的是真产物"这件事本身就不可信（Task 1 的 C1 同一类）。
    """
    spec_p = tmp_path / "deep_clean_spec.json"
    out_p = tmp_path / "deep_clean_out.json"
    spec_p.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8", newline="\n")
    runner = tmp_path / "deep_clean_product.js"
    runner.write_text(
        "const fs = require('fs');\n"
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\n"
        "eval(src);\n"
        "const samples = JSON.parse(fs.readFileSync(%s, 'utf8'));\n"
        "fs.writeFileSync(%s, JSON.stringify(samples.map(function (s) { return deepCleanHtml(s); })));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(_rss_pipeline_src()),
           json.dumps(str(spec_p).replace("\\", "/")),
           json.dumps(str(out_p).replace("\\", "/"))),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "本地 deepCleanHtml 跑崩：\n%s\n%s" % (
        (r.stdout or "")[-400:], (r.stderr or "")[-1200:])
    return json.loads(out_p.read_text(encoding="utf-8"))


def _sample_outputs(tmp_path):
    raw = [s for _name, s in EMPTY_ANCHOR_SAMPLES]
    out = _deep_clean_products(tmp_path, raw)
    assert len(out) == len(raw), "样本条数与产物条数不符（node 侧吃了注入？）"
    return raw, out


def test_realtime_deep_clean_matches_buildtime_on_empty_anchor_shapes(tmp_path):
    """行为对账（审查 ④ 的主判据）：同一份输入两侧逐条给同一个答案。"""
    deep_clean_py = _build_module()._deep_clean_html
    raw, js = _sample_outputs(tmp_path)
    diff = []
    for (name, src), got in zip(EMPTY_ANCHOR_SAMPLES, js):
        want = deep_clean_py(src)
        if want != got:
            diff.append(u"【%s】\n  输入   %r\n  构建期 %r\n  运行时 %r" % (name, src, want, got))
    assert not diff, ("空锚点/根链接整枚删这一格两侧行为不同 —— 构建期有、实时出口没接"
                      "（或接错格/形状不一致）：\n%s" % "\n".join(diff))
    # 对账本身不许空转：这条刀必须在运行时真的动过手（两侧都"什么都不删"也能对上，那不算）。
    fired = [n for (n, s), g in zip(EMPTY_ANCHOR_SAMPLES, js) if g != s]
    assert len(fired) >= 6, "刀没开火，只改了 0 条样本：%s" % fired
    assert u"页内锚点不是空锚点" not in fired, "放宽了：页内锚点被一起吃掉"


def test_realtime_deep_clean_empty_anchor_shapes(tmp_path):
    """运行时侧的绝对行为：删什么、留什么，逐条给死（对账红了要能指到是哪一半坏）。"""
    from_name = dict(zip([n for n, _s in EMPTY_ANCHOR_SAMPLES], _sample_outputs(tmp_path)[1]))
    assert from_name[u"空锚点壳在正文之后"] == u"<p>正文一</p>", from_name[u"空锚点壳在正文之后"]
    assert from_name[u"根链接壳在正文之前"] == u"<p>正文二</p>", from_name[u"根链接壳在正文之前"]
    assert from_name[u"两枚壳连着排"] == u"<p>正文</p>", from_name[u"两枚壳连着排"]
    assert from_name[u"空文本的壳"] == u"<p>阅读更多</p>", from_name[u"空文本的壳"]
    assert from_name[u"大写标签与大写属性名"] == u"正文", from_name[u"大写标签与大写属性名"]
    assert from_name[u"一枚 a 上两个 href"] == u"<p>正文</p>", \
        "贪婪 `[^>]*` 改 lazy 就锚在第一个 href 上（两侧分叉的形状）"
    assert from_name[u"div 里的壳"] == u"<p>正文</p>", from_name[u"div 里的壳"]
    # 反例：一刀都不许动
    for n in (u"页内锚点不是空锚点", u"空串 href 不在刀形里", u"根路径带内容",
              u"未闭合的壳删不掉", u"href 无引号（两侧同盲区）"):
        src = dict(EMPTY_ANCHOR_SAMPLES)[n]
        assert from_name[n] == src, "反例被吃了【%s】：%r → %r" % (n, src, from_name[n])


def test_realtime_fulltext_exit_has_no_empty_anchor_shell(tmp_path):
    """端到端：两条实时出口（RSS 与 Atom）出厂的 fc 里空锚点壳整枚消失、页内锚点活着。

    这一格只钉 `href="#"`：`href="/"` 那半在**出口链**上到不了本刀 —— normalize 在
    deepClean 之前，`/` 先被绝对化成 `https://host/` 了（构建期同一条链、同一个顺序，
    所以两侧同形）。`/` 那一半的行为对账在上一批判据（deepClean 层逐条比输出）里钉。
    """
    for kind, xml in ((u"RSS", _rss_item(EMPTY_ANCHOR_EXIT_BODY)),
                      (u"Atom", _atom_feed(EMPTY_ANCHOR_EXIT_BODY))):
        fc = _parse_feed_product(tmp_path, xml)[0].get("fullContent") or ""
        assert fc, "%s 出口没产 fc，正文一刀都没接上" % kind
        assert 'href="#"' not in fc, "%s 出口的 fc 里空锚点还在（审查 ④ 的原始形状）：%r" % (kind, fc)
        assert "阅读全文" not in fc, "%s 出口没整枚删（文字留在正文里）：%r" % (kind, fc)
        assert "阅读更多" in fc and "正文一" in fc, "%s 出口把正文一起吃了：%r" % (kind, fc)
        assert "<p></p>" not in fc, "%s 出口壳删空后没被规则 3 收尾（接错一格）：%r" % (kind, fc)
        assert 'href="#sec-2"' in fc and "第二节" in fc, "%s 出口吃了页内锚点：%r" % (kind, fc)


# ================================================================ Task 5：/api/article 返回契约
# 契约表（Task 6 的前端 _FT_NOTES 必须与这里同集合，一个字段名都不许改）：
#   成功 {ok:true, url, title, content, source:'rss_fulltext'|'readability'|'youtube'|'github',
#         degraded?:'paywall'|'short'}
#   失败 {ok:false, error:'missing_url'|'invalid_url'|'timeout'|'fetch_failed'|'challenge_page'
#         |'no_body'|'extraction_failed'}
SUCCESS_SOURCES = ["rss_fulltext", "readability", "youtube", "github"]
FAILURE_CODES = ["missing_url", "invalid_url", "timeout", "fetch_failed",
                 "challenge_page", "no_body", "extraction_failed"]
DEGRADED_VALUES = ["paywall", "short"]


def _func_src(name, text):
    """按大括号配平切出某个函数的源码（判据要按函数体看，不按"文件里出现过"看）。"""
    a = text.find("function " + name + "(")
    assert a > 0, "api/article.js 里找不到 %s，函数名或形状变了" % name
    i = text.index("{", a)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[a:j + 1]
    raise AssertionError("%s 的大括号没配平" % name)


def test_no_meta_pseudo_fulltext_any_more():
    """把 og:description 包成 <p> 当全文返回，与阅读器上方摘要重复（用户报的"全文=摘要"）。
    取消它，改成明确的 no_body 失败 —— 不许再"好歹返回点东西"。

    只钉 extractGeneric：`extractYouTube`/`extractGitHub` 里的 desc 兜底**必须保留**，
    那是这两类站点的正当呈现（视频页/仓库页本来就没有正文），一刀切会把它俩打回抓不到。
    """
    src = _read(API_ARTICLE)
    assert "'meta'" not in src and '"meta"' not in src, "article.js 还在返回 source:'meta'"
    body = _func_src("extractGeneric", src)
    assert "og:description" not in body and 'name="description"' not in body, \
        "extractGeneric 里还在用 og:description 兜正文"
    assert "'youtube'" in src and "'github'" in src, "youtube/github 两个抽取器被一起删掉了"
    assert "og:description" in _func_src("extractYouTube", src), "youtube 的 desc 兜底被误删"


def test_failure_codes_are_the_documented_set():
    """码表一致性：契约里声明的每个 error 码都必须**真的被返回**（只看代码行）。

    双向都钉 —— 只钉"在册的都在源码里"，将来加个不在册的码就等于给前端留了空文案；
    只钉"源码里的都在册"，删码就发现不了（挑战页那条被删时，前端的"这个页面要人工验证"
    会静默变成"未知错误"，正是本项目点名的自欺形状）。

    只看代码行不只看"文件里出现过"：本文件顶部的契约注释块里逐字写着全部 7 个码，
    拿 `code in src` 做正向判据时，变异⑥（把 challenge_page 改成 fetch_failed）**照样绿**
    —— 码只活在注释里 = 前端文案被架空。这是我在红→绿之间自己撞到的一条假绿。
    """
    src = _code(_read(API_ARTICLE))
    returned = set(re.findall(r"\berror:\s*'([a-z_]+)'", src))
    for stmt in re.findall(r"const errType = [^;]+;", src):
        returned |= set(re.findall(r"'([a-z_]+)'", stmt))
    assert returned, "判据读不到任何错误码，返回形状变了（本判据正在空跑）"
    missing = [c for c in FAILURE_CODES if c not in returned]
    assert not missing, "这些契约里的错误码不再真的被返回：%s ⇒ 前端 _FT_NOTES 拿不到文案" % missing
    extra = sorted(returned - set(FAILURE_CODES))
    assert not extra, "源码在返回契约外的错误码 %s：前端没有对应文案，用户看到未知错误" % extra


def test_challenge_page_and_degraded_are_wired():
    src = _code(_read(API_ARTICLE))   # 同下：注释里写一句函数名不算接上了
    assert re.search(r"isChallengePage\(", src), "没接挑战页判定 ⇒ WAF 壳页仍会被当正文"
    assert re.search(r"classifyBody\(", src), "没接付费墙/短正文判定"
    assert re.search(r"degraded", src), "degraded 字段没落到返回值"


def test_challenge_gate_runs_before_dom_construction():
    """WAF 壳页返回 HTTP 200，`resp.ok` 挡不住 ⇒ 必须在 new JSDOM 之前拦。

    放在 JSDOM 之后也能判，但 Readability 已经先抽出过一版导航垃圾；
    而且挑战页正文抽出来就是问题 1 的原始症状（阅读器里一屏"正在验证您的浏览器"）。
    """
    src = _code(_read(API_ARTICLE))
    a = src.find("isChallengePage(")
    b = src.find("new JSDOM(")
    assert 0 < a < b, "挑战页判定没排在 new JSDOM 之前（a=%d b=%d）" % (a, b)
    assert re.search(r"return res\.json\(\{\s*ok:\s*false,\s*error:\s*'challenge_page'\s*\}\)", src), \
        "命中挑战页没有明确报 challenge_page"


def test_extraction_failure_now_says_no_body_not_extraction_failed():
    """抽不出正文 = no_body（"这篇没有可显示的正文"）；extraction_failed 只留给真的抛异常。

    两者混用一个码，前端就没法区分"这站本来没正文"和"我抓挂了"（该重试/该换源）。
    """
    src = _code(_read(API_ARTICLE))   # 只扫代码行：闸门里那段解释注释不该把判据读断
    m = re.search(r"if \(\s*!result \|\| !result\.content[^)]*\)\s*\{\s*return res\.json\(\{\s*ok:\s*false,\s*error:\s*'([a-z_]+)'\s*\}\)", src)
    assert m, "内容长度闸门形状变了，判据读不到它"
    assert m.group(1) == "no_body", "抽不出正文仍报 %r —— 与'抓挂了'混成一个码" % m.group(1)
    assert re.search(r"}\s*catch \(e\) \{\s*return res\.json\(\{\s*ok:\s*false,\s*error:\s*'extraction_failed'\s*\}\)", src), \
        "异常分支没报 extraction_failed"


def test_readability_acceptance_gate_is_still_100_chars():
    """100 字这道**接受门槛**保持不动。

    提到 1200 会把短小真实文章整批变成"抓不到"—— 那是用一个新故障换旧故障（已裁定）。
    短正文的正确出口是 degraded:'short'（照给内容，只是不再暗示"这就是全文"），
    1200 这个数只住在 lib/body_rules.js 的 SHORT_BODY_CHARS 里，不许在 article.js 再写一份。
    """
    body = _func_src("extractGeneric", _read(API_ARTICLE))
    assert re.search(r"len\s*<\s*100\b", body) or re.search(r"textContent\.length\s*>\s*100\b", body), \
        "接受门槛不再是 100 字：%s" % body[-400:]
    assert "1200" not in _read(API_ARTICLE), "1200 被抄进 article.js：短正文阈值只许住在 body_rules 里"


def test_degraded_vocabulary_is_not_invented_here():
    """degraded 的值只许来自 classifyBody，且只可能是 paywall/short（另一个是 null=不标注）。

    在 article.js 里自己写 'paywall'/'short' 就是第二套词表：Task 6 的前端按一套写文案，
    运行时可能给出第三种值，落到"未知错误"。返回域本身由 node 用例钉
    （tests/rss_js/test_body_rules.js 的 classifyBody 返回域那条）。
    """
    src = _read(API_ARTICLE)
    assert not re.search(r"degraded\s*[:=]\s*['\"]", src), "degraded 被硬编码成字面量，绕开了 classifyBody"
    assert re.search(r"BODY\.classifyBody\(\s*len\s*,\s*rawHtml\s*\)", _code(src)), \
        "classifyBody 没按 (抽取正文长度, 原始 HTML) 调用"
    assert re.search(r"if \(result\.degraded\)\s*out\.degraded\s*=\s*result\.degraded", src), \
        "degraded 没落到成功返回值（或缺了就硬塞 null，前端要判两种'没有'）"


def test_no_retry_and_timeout_untouched():
    """不许给 /api/article 加重试（已裁定）：重试对 WAF/付费墙零收益，只把 8s 变 16s，
    而 vercel.json 的 `maxDuration: 15` 正是线上 FUNCTION_INVOCATION_TIMEOUT 的来源。
    """
    src = _read(API_ARTICLE)
    assert re.search(r"const FETCH_TIMEOUT = 8000;", src), "FETCH_TIMEOUT 被动过：超时预算是 15s 闸门的一部分"
    assert src.count("await fetch(") == 1, "fetch 出现 %d 次 ⇒ 给这个端点加了重试" % src.count("await fetch(")
    assert "retry" not in src.lower() and "attempt" not in src.lower(), "引入了重试逻辑（明确裁定不做）"


def test_success_payload_shape_and_source_values():
    src = _read(API_ARTICLE)
    m = re.search(r"const out = \{ ok: true[^}]*\}", _code(src))
    assert m, "读不到成功返回的构造行，返回形状变了"
    for key in ("url", "title:", "content", "source"):
        assert key in m.group(0), "成功返回缺字段 %s：%s" % (key, m.group(0))
    for s in SUCCESS_SOURCES:
        assert re.search(r"'%s'" % s, src), "契约里的 source 值 %s 在源码里不存在" % s
    extra = set(re.findall(r"source:\s*'([a-z_]+)'", src)) - set(SUCCESS_SOURCES)
    assert not extra, "出现了契约外的 source 值 %s：前端按 source 决定提示，多出来的值没人认得" % sorted(extra)


# ---------------------------------------------------------------- 导出审计（不许留孤儿接口）
def test_body_rules_exports_all_have_callers():
    """`lib/body_rules.js` 的每个导出都必须有调用方，否则就是"已导出没人调"的接口。

    本仓点名的自欺形状之一：为了让判据看起来全绿而留一个没人用的导出，
    或编一个假调用方。调用方只算 `api/`、`lib/`、`tests/` 三处（node 用例集与对账判据都算），
    排除 lib/body_rules.js 自身。宁可删导出，也不许留孤儿。
    """
    body = _read(LIB_BODY)
    m = re.search(r"module\.exports = \{(.*?)\n\};", body, re.S)
    assert m, "读不到 module.exports 块，导出面形状变了"
    names = [x for x in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1))]
    assert names, "导出面为空"
    callers = {}
    for base in ("api", "lib", "tests", "tools"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith((".js", ".py")):
                    continue
                p = os.path.join(dirpath, fn)
                if os.path.abspath(p) == os.path.abspath(LIB_BODY):
                    continue
                t = _read(p)
                for n in names:
                    if re.search(r"(BODY|R)\." + n + r"\b|['\"]" + n + r"['\"]", t):
                        callers.setdefault(n, []).append(os.path.relpath(p, ROOT))
    orphans = sorted(n for n in names if n not in callers)
    assert not orphans, (
        "这些导出谁都不调用：%s ⇒ 删掉导出（函数本体可以留着被内部调用）。"
        "报告里要逐条给 导出→调用方 清单" % orphans)


# ================================================================ R43：三把刀的运行时出口
# 这三把刀（HN 模板重写 / 粘连裸链接 / 站内导航去链接）原本只接在构建期，实时通道
# `?source=`/`?batch=` 自己抓 RSS 却没有它们 ⇒ 线上走实时出口的用户照旧看到四行模板。
# 下面几条是"接上了、而且接在对的那一格"的直接证据：拿**真 parseFeed 的产物**说话，
# 而不是"源码里出现过 BODY.xxx"（那种静态判据在子匹配变异下不红，Task 8/9 §4.2 栽过）。

_HN_DESC = (u"Article URL: https://ex.test/a\n"
            u"Comments URL: https://news.ycombinator.com/item?id=1\n"
            u"Points: 254\n# Comments: 162")
# 全角冒号一律写转义：它与半角 ':' 在编辑器里看不出区别（Task 8 的实测教训）
_HN_DESC_ZH = (u"文章网址：https://ex.test/a\n"
               u"评论网址：https://news.ycombinator.com/item?id=2\n"
               u"积分：7\n# 评论数：9")
_GLUED = u"https://www.nodeseek.com/post-957723-1看了大佬的IX文章必须有行动力"
_SEPARATED = u"https://ex.test/a 看了大佬的文章"


def _rss_xml(desc, body=u"", link=u"https://ex.test/a"):
    return (u'<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            u'<channel><item><title>t</title><link>' + link + u'</link>'
            u'<description>' + desc + u'</description>'
            + (u'<content:encoded><![CDATA[' + body + u']]></content:encoded>' if body else u"")
            + u'<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')


def _atom_xml(summary):
    return (u'<?xml version="1.0"?><feed><entry><title>t</title>'
            u'<link href="https://ex.test/a"/>'
            u'<summary>' + summary + u'</summary>'
            u'<published>Tue, 01 Sep 2026 10:00:00 GMT</published></entry></feed>')


def _card_product(tmp_path, xml):
    """真 parseFeed 的产物再过**真出口函数** `toCardItem` ⇒ 出厂 `s` 字段的真值。

    R43 的摘要刀接在 `toCardItem`（fetchOne 用的那张映射），不接在 parseFeed 里：
    parseFeed 的 `summary` 形状一个字节都不许变（去重键与两份既有 harness 都指着它），
    而用户看的、以及被写进滚动缓存的是 `s`。
    """
    runner = tmp_path / "card_product.js"
    runner.write_text(
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\n"
        "eval(src);\n"
        "const its = parseFeed(%s, 'probe', 50);\n"
        "process.stdout.write(JSON.stringify(its.map(toCardItem)));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(_rss_pipeline_src()), json.dumps(xml)),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "真出口 toCardItem 跑崩：\n%s\n%s" % (
        (r.stdout or "")[-400:], (r.stderr or "")[-1200:])
    return json.loads(r.stdout)


def _summary(tmp_path, xml):
    cards = _card_product(tmp_path, xml)
    assert cards, "解析+映射后一条都没有，fixture 与生产不同形"
    return cards[0]["s"]


def _card(tmp_path, xml):
    cards = _card_product(tmp_path, xml)
    assert cards, "解析+映射后一条都没有，fixture 与生产不同形"
    return cards[0]


def test_realtime_rss_summary_rewrites_the_hn_template(tmp_path):
    """实时 RSS 出口：四行模板出厂时压成一行，裸链接一条不留。"""
    s = _summary(tmp_path, _rss_xml(_HN_DESC))
    assert "Article URL" not in s and "Comments URL" not in s, "模板行还在（刀没接上）：%r" % s
    assert "news.ycombinator.com" not in s, "Comments URL 裸链没剥：%r" % s
    assert "254" in s and "162" in s and "Hacker News" in s, \
        "分数/评论数/来源被一起吃掉了：%r" % s


def test_realtime_rss_summary_rewrites_the_zh_template(tmp_path):
    """中文版（文章网址/评论网址/积分/评论数，全角冒号）也要命中：判形状不判语言。"""
    s = _summary(tmp_path, _rss_xml(_HN_DESC_ZH))
    assert "文章网址" not in s and "评论网址" not in s, "中文模板没命中：%r" % s
    assert "7" in s and "9" in s and "Hacker News" in s, s


def test_realtime_atom_summary_rewrites_the_hn_template(tmp_path):
    """Atom 分支是**另一段代码**：只接 RSS 那一半就是半个断链（Task 8 的 ②b 同形）。"""
    s = _summary(tmp_path, _atom_xml(_HN_DESC))
    assert "Article URL" not in s and "Points:" not in s, s
    assert "254" in s and "Hacker News" in s, s


def test_realtime_rss_summary_strips_the_glued_url(tmp_path):
    """NodeSeek 实测形状：粘连裸链接剥掉，正文一个字不少。"""
    s = _summary(tmp_path, _rss_xml(_GLUED, link=u"https://www.nodeseek.com/post-957723-1"))
    assert "nodeseek.com" not in s, "粘连的裸链接还在：%r" % s
    assert s.startswith(u"看了大佬"), "正文被吃掉了一部分：%r" % s


def test_realtime_summary_does_not_strip_a_separated_url(tmp_path):
    """反例：空格分隔的"链接 + 正文"不许动（剥多了就是把摘要改坏）。"""
    s = _summary(tmp_path, _rss_xml(_SEPARATED))
    assert "ex.test/a" in s, "空格分隔的链接被误剥：%r" % s


def test_realtime_body_delinks_nav_links_and_keeps_external(tmp_path):
    """正文出口：站内 /tag/ 脱链接留文字，外部正文链接的 href 一路活到出厂。"""
    body = (u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署，'
            u'参考 <a href="https://openai.com/blog/x">原始公告</a></p>')
    fc = _parse_feed_product(tmp_path, _rss_xml(u"d", body=body))[0]["fullContent"]
    assert "Kubernetes" in fc and "原始公告" in fc, "正文词被吃了：%r" % fc
    assert "/tag/kubernetes" not in fc, \
        "导航链接没被脱（去链接那半没接进 deepCleanHtml）：%r" % fc
    assert 'href="https://openai.com/blog/x"' in fc, "外部正文链接被脱了：%r" % fc


def test_realtime_summary_knives_run_in_the_buildtime_order():
    """链形判据：先 HN 刀、再粘连刀（倒序会先改掉 HN 要看的行首形状），两个入口都接。"""
    seg = _rss_pipeline_src()
    code = _code(seg)      # 注释里提"cleanSummary/truncate"不算接线，也不算断链
    assert re.search(r"return\s+collapseRuns\(\s*BODY\.stripGluedUrl\(\s*BODY\.rewriteHnSummary\(",
                     code), ("cleanSummary 不再是「先 HN 后粘连」的嵌套顺序，"
                             "与构建期 _strip_glued_url(_rewrite_hn_summary(...)) 分叉了")
    assert code.count("cleanSummary(") == 2, (
        "cleanSummary 应该定义 1 次 + 出厂出口 toCardItem 1 次，实际 %d 次"
        % code.count("cleanSummary("))
    assert code.count("summaryRaw:") == 2, (
        "Atom 与 RSS 两个分支都要把**未剥标签的原始描述**留给出口（%d 处）"
        % code.count("summaryRaw:"))
    assert "s: truncate(cleanSummary(it.summaryRaw" in code, "出厂 s 没走带刀出口"
    assert "truncate(stripHtml(" in code, "parseFeed 的 summary 形状变了（去重键与既有 harness 指着它）"
    assert "s: it.summary," not in code, "旧出口（直接抄 parseFeed.summary）又回来了"
    # HN 刀必须看得见换行：先 stripHtmlKeepLines，压空白放回最后一步。
    # 这里用正则而不是子串：`stripHtml(` 是 `stripHtmlKeepLines(` 的前缀，
    # 子串判据会把"接对了"的写法也当成"接错了"（本仓踩过的子匹配口径）。
    assert not re.search(r"rewriteHnSummary\(\s*stripHtml\(", code.replace("\n", "")), (
        "摘要刀接在了压完空白的 stripHtml 之后 ⇒ HN 那四行按行锚定，永不命中（又一把空转刀）")


def test_runtime_deep_clean_has_no_class_or_id_knife():
    """R42 的运行时对称：deepCleanHtml 里不许再有按 class/id 找容器的刀。

    `api/rss.js` 的 sanitizeHtml 与构建期同款（ALLOWED_ATTRS 不保留 class/id），
    而 deepCleanHtml 的唯一调用点就在 sanitizeHtml 之后 ⇒ 那条刀命中恒为 0。
    """
    src = _read(API_RSS)
    a = src.find("function deepCleanHtml(")
    assert a >= 0, "找不到 deepCleanHtml（运行时清洗入口改名了？）"
    end = src.find("\nfunction ", a + 10)
    assert end > a, "deepCleanHtml 之后找不到下一个顶层函数，切片没有结束边界"
    code = _code(src[a:end])
    assert not re.search(r"\(\?:class\|id\)|class\s*=|id\s*=", code), (
        "运行时 deepCleanHtml 里又出现了按 class/id 匹配的规则，它接不到出口")
    assert "BODY.delinkNavLinks(text)" in code, "去链接那半没接进 deepCleanHtml"


def test_strip_html_split_keeps_the_old_output_shape(tmp_path):
    """stripHtml 拆成两层后，**出厂形状不许变**：卡片与去重键还指着单行文本。

    拆层只是为了让 HN 那把刀看得见换行；`stripHtml` 自己必须仍然压空白 + trim，
    否则标题/摘要会带着换行进卡片 —— 那是一条没被派工的行为变更。
    """
    runner = tmp_path / "strip_html_shape.js"
    probe = [u"  a\n\nb\tc" + chr(0x00a0), u"a\n\nb"]
    runner.write_text(
        # COVER/BODY 注入与本文件 `_parse_feed_product` 同一条理由：切片切不到文件顶部的
        # require，而审查 ③ 之后 `stripHtmlKeepLines` 的实体那一格要走 BODY。
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\neval(src);\n"
        "const items = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n"
        "process.stdout.write(JSON.stringify([stripHtml(items[0]), stripHtmlKeepLines(items[1])]));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(_rss_pipeline_src())), encoding="utf-8", newline="\n")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(probe, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([_node(), str(runner), str(spec)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "node 跑 stripHtml 崩了：\n%s" % (r.stderr or "")[-800:]
    got, kept = json.loads(r.stdout)
    # NBSP 在 JS 的 `\s` 里 ⇒ 被压成空格再 trim 掉（**改动前就是这个形状**，
    # 这里要钉的是"没变"，不是"等于 Python _strip_html"——后者保留换行、不做空白折叠）
    assert got == u"a b c", "stripHtml 的出厂形状变了：%r" % got
    assert kept == u"a\n\nb", "stripHtmlKeepLines 不该压换行（HN 刀靠它看行）：%r" % kept


# ── 判据 2（复核 P0-2）：`s` 到出口只许解**一趟** ──────────────────────────────
# 四条出厂出口（`?batch=` 格式化 / 快照服务 / refresh 合并 T1 / refresh 合并快照 T2T3）原先各抄
# 一份 `s: truncate(stripHtml(x), 200)`。`s` 走到出口时**已经**过单趟清洗（实时条目
# `toCardItem → cleanSummary → BODY.decodeEntities`；快照条目的 `s` 是构建期 `_strip_html` 的产物），
# 而 `stripHtml` 走的是旧顺序级联 `legacyEntityCascade`，里面那支 `&[a-z]+;` → 空串会把**已经剩下来
# 的字面量吃掉名字**（实测两条通道的产物因此不同判）：
#     `&amp;copy; 2026 版权所有` → 单趟 `&copy; 2026 版权所有`（= 构建期答案）
#                          → 第二趟 `2026 版权所有`
# 修法是收进唯一一份 `shipSummary(x)`：当时**长度口径一字未改**（照旧 200 封顶
# + 既有省略号），只是不再二次解码。**标题那一格 `t: stripHtml(...)` 本批故意没动**（它是
# `legacyEntityCascade` 上方登记的"已知挂起"，牵动他人线 harness）⇒ 下面所有断言只碰 `s`。
# 三条判据的分工：`test_summary_exits_decode_exactly_once` 是**行为**（真 `shipSummary` + 真出口
# 那 4 行表达式实跑）、`test_summary_exit_wiring_is_shipsummary_not_a_second_pass` 是**接线**
# （4 处调用 + 定义唯一 + 第二趟形状绝迹，并自带双向控制）、最后一条是**接线的反证**（删掉定义
# 之后 node 侧必须真崩，不是 grep 崩）。
_SNAPSHOT_ONCE = u"&copy; 2026 版权所有"        # 单趟之后、进出口之前该长这样
_SNAPSHOT_NUMERIC = u"&#20998; 数"              # 另一格：旧级联的 `&#\d+;` → 空串
_SNAPSHOT_DECODED_AMP = u"版权 &amp; 备注"       # 再一格：`&amp;` 是**货**，不是还没解的实体
_SNAPSHOT_SAMPLES = [_SNAPSHOT_ONCE, _SNAPSHOT_NUMERIC, _SNAPSHOT_DECODED_AMP]


def _ship_exit_line_specs():
    """从 `api/rss.js` 的**请求处理区段**（`async function fetchOne(` 之后）抠出每一条 `s:` 出口。

    返回 `[(整行文本, shipSummary 的那个实参), ...]`。为什么在这儿要求"每一条都是 shipSummary"：
    只数 `s: shipSummary(` 的次数的话，把某条出口改回别的写法（`s: item.s,`）就只掉一个计数、
    剩下的三条照样绿；这里是"先收集全部 `s:` 行、再要求它们**逐个**通过"，少一条、多一条、
    换一种写法都会红。
    """
    text = _read(API_RSS)
    cut = text.find("async function fetchOne(")
    assert cut > 0, "找不到 async function fetchOne ⇒ 出口区段的边界变了，判据要跟着改"
    tail = _code(text[cut:])          # `_code` 去掉注释行：注释里提一句不许把判据打红
    lines = [ln.strip() for ln in tail.split("\n") if re.match(r"^\s*s:\s", ln)]
    assert lines, "fetchOne 之后一条 `s:` 出口都没有 ⇒ 出厂形状整个换了"
    out = []
    for ln in lines:
        m = re.match(r"^s:\s*shipSummary\((?P<arg>.+)\),$", ln)
        assert m, ("有条出口没走 shipSummary（应当是 `s: shipSummary(<实参>),`）：%r" % ln)
        assert m.group("arg").strip(), "shipSummary 的实参是空的：%r" % ln
        out.append((ln, m.group("arg").strip()))
    return out


def _exit_line_products(tmp_path, lines, samples):
    """把抠出来的**真出口行**在 node 里逐行求值：`{lines: [[每条样本的产物], ...]}`。

    行文本来自仓库源码（与 `_rss_pipeline_src()` 同一信任级），样本一律走 JSON 文件中转，
    不进 JS 源码 —— 能引号改写被测脚本的"实跑"不算实跑（Task 1 的 C1 同一类）。
    """
    spec_p = tmp_path / "ship_exit_spec.json"
    out_p = tmp_path / "ship_exit_out.json"
    spec_p.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8", newline="\n")
    runner = tmp_path / "ship_exit_product.js"
    runner.write_text(
        "const fs = require('fs');\n"
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\n"
        "eval(src);\n"
        "const LINES = %s;\n"
        "const S = JSON.parse(fs.readFileSync(%s, 'utf8'));\n"
        "const out = LINES.map(function (ln) {\n"
        "  const f = new Function('it', 'item', 'shipSummary', 'truncate', 'stripHtml',\n"
        "                          'return {' + ln + '}');\n"
        "  return S.map(function (v) {\n"
        "    return f({s: v}, {s: v}, shipSummary, truncate, stripHtml).s;\n"
        "  });\n"
        "});\n"
        "fs.writeFileSync(%s, JSON.stringify(out));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(_rss_pipeline_src()), json.dumps([ln.rstrip(",") for ln, _a in lines]),
           json.dumps(str(spec_p).replace("\\", "/")), json.dumps(str(out_p).replace("\\", "/"))),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "真出口行实跑崩了：\n%s\n%s" % (
        (r.stdout or "")[-300:], (r.stderr or "")[-1200:])
    got = json.loads(out_p.read_text(encoding="utf-8"))
    assert len(got) == len(lines) and all(len(x) == len(samples) for x in got), (
        "出口行数/样本数与产物对不上：%s" % [len(x) for x in got])
    return got


def _seg_product(tmp_path, tag, expr, samples, seg=None):
    """在 node 里跑真切片（默认真 `extractTag..fetchOne` 区段），对每个样本求值 `expr`。"""
    spec_p = tmp_path / ("ship_%s_spec.json" % tag)
    out_p = tmp_path / ("ship_%s_out.json" % tag)
    spec_p.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8", newline="\n")
    runner = tmp_path / ("ship_%s.js" % tag)
    runner.write_text(
        "const fs = require('fs');\n"
        "const COVER = require(%s);\n"
        "const BODY = require(%s);\n"
        "const src = %s;\n"
        "eval(src);\n"
        "const S = JSON.parse(fs.readFileSync(%s, 'utf8'));\n"
        "const f = %s;\n"
        "fs.writeFileSync(%s, JSON.stringify(S.map(f)));\n"
        % (json.dumps(LIB_COVER.replace("\\", "/")), json.dumps(LIB_BODY.replace("\\", "/")),
           json.dumps(seg if seg is not None else _rss_pipeline_src()),
           json.dumps(str(spec_p).replace("\\", "/")), expr,
           json.dumps(str(out_p).replace("\\", "/"))),
        encoding="utf-8", newline="\n")
    r = subprocess.run([_node(), str(runner)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    assert r.returncode == 0, "node 跑 %s 崩了：\n%s\n%s" % (
        tag, (r.stdout or "")[-300:], (r.stderr or "")[-1200:])
    return json.loads(out_p.read_text(encoding="utf-8"))


def _wiring_violations(code):
    """判据 2 那三道**接线**闸的口径，抠成一个函数：好让"双向控制"拿同一把尺子量坏副本。"""
    v = []
    n = code.count("s: shipSummary(")
    if n != 4:
        v.append("`s: shipSummary(` 应恰好 4 处（四条出口各一处），实得 %d" % n)
    if "s: truncate(stripHtml(" in code:
        v.append("出口里又出现 `s: truncate(stripHtml(` —— 那是第二趟解码")
    d = code.count("function shipSummary(")
    if d != 1:
        v.append("`function shipSummary(` 的定义应恰好 1 处，实得 %d ⇒ 四条出口指的是谁？" % d)
    return v


def test_summary_exit_cap_matches_buildtime_default(tmp_path):
    """②「放宽出口」的口径闸：JS 的上限必须等于 Python `_truncate` 的默认上限（参照物是读出来的）。

    裁定前那四处出口各自写死 200、构建期是 `_truncate(..., 500)` ⇒ 同一条摘要两条通道两个长度
    （现取 659 条里 209 条长度不同）。这一格把"两个数必须同一个"钉住：
    谁只改一边（或把 500 又抄成第二处写死的数），当场红。
    """
    dflt = _build_module()._truncate.__defaults__
    assert dflt and len(dflt) == 1, "Python `_truncate` 的默认值形状变了：%r" % (dflt,)
    cap_py = int(dflt[0])
    src = _read(API_RSS)
    m = re.search(r"const SUMMARY_CAP = (\d+);", src)
    assert m, "api/rss.js 里没有 SUMMARY_CAP ⇒ 出口长度又回到各写各的"
    cap_js = int(m.group(1))
    assert cap_js == cap_py, (
        "出口上限 %d 与构建期 `_truncate` 默认 %d 不同数（裁定：两边用同一个数）" % (cap_js, cap_py))
    assert src.count("SUMMARY_CAP") >= 5, "SUMMARY_CAP 的引用数掉了：%d" % src.count("SUMMARY_CAP")
    assert u", 200)" not in src, "api/rss.js 又出现写死的 200 字上限：%r" % [
        ln for ln in src.split(chr(10)) if u", 200)" in ln][:2]
    # 行为那一格：正好到上限不许被削，超过才截（截出 cap 字 + 既有省略号）
    at_cap, over = u"y" * cap_py, u"y" * (cap_py + 20)
    out_at, out_over = _seg_product(tmp_path, "capgate",
                                    "function (s) { return shipSummary(s); }",
                                    [at_cap, over])
    assert out_at == at_cap, "正好 %d 字被削了一格（不该截）：%d 字" % (cap_py, len(out_at))
    assert (len(out_over) == cap_py + 1 and out_over.endswith(u"\u2026")
            and out_over[:-1] == u"y" * cap_py), (
        "超过上限的形状不对：%d 字，尾 %r" % (len(out_over), out_over[-6:]))
    print("[出口上限] py `_truncate` 默认=%d js SUMMARY_CAP=%d；%d 字原样、%d 字截到 %d+省略号"
          % (cap_py, cap_js, cap_py, cap_py + 20, cap_py))


def test_summary_exits_decode_exactly_once(tmp_path):
    """判据 2（行为）：出口只截断、**不再解第二遍**；长度口径由下一格 `..._cap_matches_buildtime_default` 管。"""
    # ── (1) 单趟 vs 第二趟：同一份输入，两个形状必须给出**不同**答案，且单趟那个等于构建期 ──
    once = _seg_product(tmp_path, "onepass",
                        "function (s) { return shipSummary(BODY.decodeEntities(s)); }",
                        [u"&amp;copy; 2026 版权所有"])[0]
    twice = _seg_product(tmp_path, "twopass",
                         "function (s) { return truncate(stripHtml(BODY.decodeEntities(s)), 500); }",
                         [u"&amp;copy; 2026 版权所有"])[0]
    build = _build_module()._strip_html(u"&amp;copy; 2026 版权所有")
    assert u"&amp;copy;" not in once and u"&copy;" in once, (
        "单趟那一格不对：%r（期望实体只解一层、`&copy;` 原样留着）" % once)
    assert once == build, "出口产物与构建期分叉\n  py: %r\n js: %r" % (build, once)
    assert u"&copy;" not in twice and twice != once, (
        "对照那一格失效：旧的第二趟写法竟然与单趟同形（%r / %r）⇒ 输入样本不再能分辨两遍解码"
        % (twice, once))
    # ── (2) 长度口径 = 构建期那一侧：520 字留 500 字 + 既有省略号 ──
    long_in = u"y" * 520
    long_out = _seg_product(tmp_path, "cap",
                            "function (s) { return shipSummary(s); }", [long_in])[0]
    assert len(long_out) == 501 and long_out.endswith(u"…") and long_out[:-1] == u"y" * 500, (
        "`shipSummary` 的截断口径与 SUMMARY_CAP 不符（长度口径见下一条判据）：%d 字，尾 %r"
        % (len(long_out), long_out[-6:]))
    short_out = _seg_product(tmp_path, "short", "function (s) { return shipSummary(s); }",
                             [_SNAPSHOT_ONCE])[0]
    assert short_out == _SNAPSHOT_ONCE, "上限以内被动了：%r" % short_out
    # ── (3) 四条出口的**真行**逐条实跑：每条都只许截断，不许再解一遍 ──
    lines = _ship_exit_line_specs()
    assert len(lines) == 4, "四条出口应当抠出 4 行，实得 %d：%s" % (
        len(lines), [ln for ln, _a in lines])
    products = _exit_line_products(tmp_path, lines, _SNAPSHOT_SAMPLES)
    for (ln, _arg), col in zip(lines, products):
        for want, got in zip(_SNAPSHOT_SAMPLES, col):
            assert got == want, ("某条出口又解了第二遍：%r\n  进 %r\n  出 %r" % (ln, want, got))
    # ── (4) 反空转：这批样本在**旧写法**下必须确实会坏（否则 (3) 是在数空气）──
    old = _seg_product(tmp_path, "oldshape", "function (s) { return truncate(stripHtml(s), %d); }" % _buildtime_cap(),
                       _SNAPSHOT_SAMPLES)
    broke = [w for w, g in zip(_SNAPSHOT_SAMPLES, old) if g != w]
    assert len(broke) == len(_SNAPSHOT_SAMPLES), (
        "旧出口形状竟然不破坏任何一条样本（%s）⇒ 上面 (3) 的判别力是假的，样本要换"
        % [w for w, g in zip(_SNAPSHOT_SAMPLES, old) if g == w])
    print("[判据2 行为] 单趟=构建期（%r）；第二趟会吃掉 %r；4 条出口 × %d 份已解码样本逐个原样出厂；"
          "240 字仍截到 200+省略号" % (once, twice, len(_SNAPSHOT_SAMPLES)))


def test_summary_exit_wiring_is_shipsummary_not_a_second_pass():
    """判据 2（接线）：四条出口真的都指到同一份 `shipSummary`，且第二趟形状绝迹。

    行为那一格（上一条）量的是产物，这一条量的是"四条出口有没有各自抄一份"：本批的缺陷形状
    就是**同一个第二遍解码被抄了四遍**，所以计数必须恰好 4，且不许残留旧写法。
    双向控制写在这儿：两种坏改动（某条出口改回 stripHtml / 定义整个删掉）拿**同一个**
    `_wiring_violations` 量必须红 —— 否则这条判据只是"当前源码刚好没违规"。
    """
    code = _code(_read(API_RSS))
    assert _wiring_violations(code) == [], "真源码现在就违规：%s" % _wiring_violations(code)
    # 控制 1：把某一条出口改回第二遍（= 电池靶 R63 的形状）
    v1 = code.replace(u"s: shipSummary(it.s),", u"s: truncate(stripHtml(it.s), 200),", 1)
    assert v1 != code, "控制 1 没打上（`?batch=` 那条出口的锚点变了）"
    assert _wiring_violations(v1), "把一条出口改回 stripHtml 竟然不红 ⇒ 这条接线判据没牙"
    # 控制 2：删掉 `shipSummary` 的定义（四条出口从此指向不存在的东西）
    v2 = code.replace(u"function shipSummary(x) {", u"function _retiredShipSummary(x) {", 1)
    assert v2 != code, "控制 2 没打上（定义的锚点变了）"
    assert _wiring_violations(v2), "删掉 shipSummary 的定义竟然不红 ⇒ 这条接线判据没牙"
    # 控制 3：把某一条出口整个换成不再截断的写法（计数掉到 3，第二趟也没有）
    v3 = code.replace(u"s: shipSummary(item.s),", u"s: item.s,", 1)
    assert v3 != code and _wiring_violations(v3), "出口少了一处 shipSummary 却不红"
    print("[判据2 接线] `s: shipSummary(` 恰好 4 处 / 定义 1 处 / `s: truncate(stripHtml(` 0 处；"
          "三种坏改动（退回 stripHtml、删定义、少一处接线）各自都红")


def test_summary_exit_probe_dies_without_the_shipsummary_definition(tmp_path):
    """判据 2（接线的反证）：删掉定义之后**实跑**必须真崩，不是只有 grep 崩。

    上一条控制走的是文本口径；这条走 node —— 它证明 `shipSummary` 是出口行**运行时**指向的那个
    东西（`ReferenceError`），也就是"接线判据不许只 grep 函数名"那半。
    """
    ok = _seg_product(tmp_path, "def_alive", "function (s) { return shipSummary(s); }",
                      [_SNAPSHOT_ONCE])[0]
    assert ok == _SNAPSHOT_ONCE, "控制那一格先红了（真切片里 shipSummary 就不通？）：%r" % ok
    seg = _rss_pipeline_src()
    broken = seg.replace(u"function shipSummary(x) {", u"function _retiredShipSummary(x) {", 1)
    assert broken != seg, "删定义的锚点没打上 ⇒ 这条反证是空的"
    raised = None
    try:
        _seg_product(tmp_path, "def_gone", "function (s) { return shipSummary(s); }",
                     [_SNAPSHOT_ONCE], seg=broken)
    except AssertionError as e:
        raised = str(e)
    assert raised is not None, "删掉 shipSummary 的定义，出口实跑竟然照样通过"
    assert "shipSummary is not defined" in raised, (
        "崩是崩了，但不是崩在那个字上（说明出口行根本没用 shipSummary）：%s" % raised[-400:])
    print("[判据2 反证] 删定义 ⇒ node 真崩在 `shipSummary is not defined`")

def _strip_tags_only_reference(x):
    """Python 侧参照物：`_strip_html` 的**后两步**（剥标签 + 剥尾部半截标签）再 trim。

    故意**不含** `html.unescape`：出口拿到的是已经单趟解码过的值，再解一遍就是本批判掉的缺陷
    （`&copy;` 会被旧级联吃成空串）。所以这条参照物表达的是「出口应当只做的那半边」，
    而不是把构建期整条链抄第二份。
    """
    t = re.sub(r"<[^>]+>", "", x)
    t = re.sub(r"<[^>]*$", "", t)
    return t.strip()


_TAG_SHAPES = [
    u'前<a href="https://x.test/a">链</a>后',
    u'尾部半个标签<div',
    u'纯文本，没有标签',
]
_ENTITY_MUST_SURVIVE = u'&amp;copy; 不该被再解'


def test_summary_exit_still_strips_tags_but_never_decodes_twice(tmp_path):
    """R67：我把 `truncate(stripHtml(x),200)` 整行换掉时，把「防御性剥标签」一起带走了。

    旧行里的 `stripHtml` 干两件事：剥标签（`api/rss.js:1050` 原注释写明「对快照数据做 HTML
    清理（防御性）」）与解实体（这一件才是缺陷，实测吃掉 98/1060 条摘要里的文字）。
    `shipSummary` 必须只补回前一件，后一件保持「绝不第二遍」。
    """
    got = _seg_product(tmp_path, "tagshapes", "function (s) { return shipSummary(s); }",
                       _TAG_SHAPES + [_ENTITY_MUST_SURVIVE])
    want = [_strip_tags_only_reference(x) for x in _TAG_SHAPES]
    assert got[:3] == want, (
        "出口剥标签这半边没与构建期同形｜进 %r｜出 %r｜参照 %r" % (_TAG_SHAPES, got[:3], want))
    assert u"<a" not in got[0] and u"链" in got[0], "标签没剥掉或把文字也剥掉了：%r" % got[0]
    assert u"<div" not in got[1], "尾部半截标签没剥掉：%r" % got[1]
    assert got[2] == u"纯文本，没有标签", "无标签样本被动了：%r" % got[2]
    # 另外半边：实体必须**原样**留着（这正是本批删第二遍的理由，不许被「补防御」顺手带回来）
    assert got[3] == _ENTITY_MUST_SURVIVE, (
        "出口又解了第二遍实体：进 %r 出 %r" % (_ENTITY_MUST_SURVIVE, got[3]))
    # ── 反空转：把 shipSummary 退回「只截断」（= 我这批刚犯过的形状），上面的断言必须真红 ──
    seg = _rss_pipeline_src()
    old_body = (u"  const t = unwrapCdata(String(x || '')).replace(/<[^>]+>/g, '')"
                u".replace(/<[^>]*$/, '');" + chr(10) +
                u"  return truncate(collapseRuns(t), SUMMARY_CAP);")
    weak_body = u"  return truncate(x || '', SUMMARY_CAP);"
    assert old_body in seg, "shipSummary 的函数体形状与判据锚点不符 ⇒ 这条反证会是空的"
    weak = seg.replace(old_body, weak_body, 1)
    assert weak != seg
    bad = _seg_product(tmp_path, "weakbody", "function (s) { return shipSummary(s); }",
                       _TAG_SHAPES, seg=weak)
    assert bad[0] == _TAG_SHAPES[0] and u"<a" in bad[0], (
        "退回只截断后竟然也剥了标签 ⇒ 上面那条剥标签断言没牙：%r" % bad[0])
    print("[R67] 出口剥标签与 Python 后两步同形（3 形状）；`&amp;copy;` 原样留着（不第二遍）；"
          "把函数体退回只截断 ⇒ 同一批样本立刻保留 <a>，判据有牙")

# 样本用 chr() 拼出来，不在源码里写反斜杠转义：本仓记过 js-in-python-string-escapes 这个坑
# （生成器里的反斜杠 n 会先被 Python 吃掉，判据就变成在测自己的转义而不是测形状）。
_SHAPE_SAMPLES = [
    u'a' + chr(10) + chr(10) + u'b   c',
    u'x' + chr(160) + u'y' + chr(160) + u'z',
    u'尾巴' + u' ' * 8 + u'少一句' + u' ' * 8 + u'被吃到这里',
    u'前半<a href="x">链</a>后半',
    # CDATA 包装：改动前那条出口的**第一步**是 unwrapCdata。漏了它，`<[^>]+>` 会把
    # `<![CDATA[正文]]>` 整段当一个标签吃掉 ⇒ 出厂空串（复评第 4 轮 2026-10-05 合成样本量到）。
    u'<![CDATA[' + u'正文一段' + u']]>',
    u'<![CDATA[' + u'未闭合的那半',
]
_ENTITY_SAMPLE = u'&copy; 2026 版权所有'   # 已经过单趟解码的出厂输入（不是 feed 原文）


def _buildtime_cap():
    """构建期那一侧的摘要上限：从 Python `_truncate` 的默认值**读**出来，不在测试里抄数字。"""
    d = _build_module()._truncate.__defaults__
    assert d and len(d) == 1, "`_truncate` 默认值形状变了：%r" % (d,)
    return int(d[0])

def test_summary_exit_shape_equals_the_pre_batch_exit(tmp_path):
    """R67b：除"不再吃实体"这一件，出口必须与**改动前**逐字节同形。

    我一度把 collapseRuns 连同第二遍解码一起删了。现取本地真快照 9,308 条 s 实测：
    与改动前的出厂值不同 **2,064 条（22%）**，其中 **21 条变短** —— 未折叠的连行与连续空格
    吃掉 200 字预算，尾巴少一句。补回 collapseRuns 后同一批实测 **diff=0**。
    这条判据钉的就是"形状不许顺手改"，另附反空转：把折叠摘掉必须真红。
    """
    old = _seg_product(tmp_path, "prebatch",
                       "function (s) { return truncate(stripHtml(s), %d); }" % _buildtime_cap(), _SHAPE_SAMPLES)
    now = _seg_product(tmp_path, "nowshape",
                       "function (s) { return shipSummary(s); }", _SHAPE_SAMPLES)
    assert old == now, (
        "出口形状与改动前不一致（本批只该删第二遍解码）：" + chr(10) +
        chr(10).join("  进 %r / 改动前 %r / 现在 %r" % (x, o, n)
                     for x, o, n in zip(_SHAPE_SAMPLES, old, now)))
    # 折叠/剥标签必须真做了，否则"两边都没干活"也会相等
    assert now[0] == u"a b c", "换行与连续空格没被折叠：%r" % now[0]
    assert now[1] == u"x y z", "NBSP 没被折叠：%r" % now[1]
    assert now[3] == u"前半链后半", "标签没被剥：%r" % now[3]
    # 唯一该出现的差异：双编码实体那一格，改动前被吃成空串，现在原样留着
    e_old = _seg_product(tmp_path, "prebatch_e",
                         "function (s) { return truncate(stripHtml(s), %d); }" % _buildtime_cap(),
                         [_ENTITY_SAMPLE])[0]
    e_now = _seg_product(tmp_path, "now_e",
                         "function (s) { return shipSummary(s); }", [_ENTITY_SAMPLE])[0]
    assert u"&copy;" in e_now and u"&copy;" not in e_old, (
        "实体那格应当**只在这里**出现差异：改动前 %r / 现在 %r" % (e_old, e_now))
    # 反空转：把 collapseRuns 从 shipSummary 摘掉 ⇒ 与改动前同形这条必须真红
    seg = _rss_pipeline_src()
    stripped = seg.replace(u"  return truncate(collapseRuns(t), SUMMARY_CAP);",
                           u"  return truncate(t, SUMMARY_CAP);", 1)
    assert stripped != seg, "控制没打上：shipSummary 折叠那步的形状变了"
    bad = _seg_product(tmp_path, "nocollapse",
                       "function (s) { return shipSummary(s); }", _SHAPE_SAMPLES, seg=stripped)
    assert bad != old, "摘掉 collapseRuns 后竟然仍与改动前同形 ⇒ 这条判据没牙"
    print("[R67b] %d 形状与改动前逐字节同形；实体格保留 &copy;；摘掉折叠 ⇒ 立刻不同形"
          % len(_SHAPE_SAMPLES))
