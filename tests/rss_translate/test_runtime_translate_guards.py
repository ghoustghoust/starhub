# -*- coding: utf-8 -*-
"""运行时翻译链路的行为判据（node 真跑，不 grep 源码）。

现场证据（2026-10-01 08:0x，Pages 版 rss-aggregator.html 控制台）：
  - 浏览器直连 GTX 8 条文本 / 16 次请求 / **0 次成功**：translate.googleapis.com 从大陆网络
    不可达，Chrome 报成 CORS + net::ERR_FAILED。而 GTX 是"主力"、服务端只是兜底，断路器要
    连续 2 批全败才熔断 ⇒ 每次访问先白等两轮超时。
  - 送翻内容里出现了**中文**（"💻基本信息+[1m硬件质量体检报告…"）：_needsTranslation 用
    `t.replace(/[\\s\\d\\p{P}]/gu,'')` 估"非中文长度"，把 `+`、`[1m` 这类符号也算进去，
    稀释了 cjk 占比 -> 中文被判成需要翻译。
  - 送翻内容里出现了**整段摘要**且从句子中间截断（"Hundreds of trekkers on the "）：
    _browserGtx 用 .slice(0,500) 硬切。摘要**必须**继续翻（卡片渲染 a.s），
    但截断要落在词边界，否则译文带半截词。
    服务端 /api/translate 实测可用（200 / engine:agnes / ACAO 放行 kwei168.github.io），
    所以"断路器熔断后走兜底"这条路是通的 —— 修的方向是少白等，不是砍摘要。

判据一律跑真 node（抽取 5192→运行时翻译诊断 之间的真实 JS + shim），因为这三条缺陷
"读 YAML/py 文本都看不出问题"：符号稀释占比、熔断阈值、截断位置都是行为层面的。
"""
import json
import os
import subprocess
import warnings

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")

# 起点必须落在 `var _CJK_R`：_needsTranslation 依赖它上面那三行 var 声明，
# 从 `function _needsTranslation(` 起抽会得到一个 ReferenceError（我踩过，别把锚点改回去）。
START = "var _CJK_R = "
END = "/* 运行时翻译诊断"

# 线上真实取样（控制台 msgid=8/4/12），中文垃圾 + HN 样板 + 正常英文长句
ZH_SPAM = ("💻基本信息" + "+" * 78 + "[1m硬件质量体检报告：[36m45.192.*.*[0m"
           "[4mhttps://github.com/xykt/HardwareQuality[")
HN_STUB = ("Article URL: https://huggingface.co/datasets/sinabis-group/x\n"
           "Comments URL: https://news.ycombinator.com/item?id=49915070\n"
           "Points: 1\n# Comments: 0")
EN_TITLE = "Researchers develop fluorescent probes to detect glucose in living animals"
EN_SUMMARY = ("Researchers from the University of Bath have developed fluorescent molecular probes "
              "that can detect changes in glucose levels inside living animals, giving scientists "
              "new ways of visualizing sugar uptake and studying diabetes, cancer and other "
              "metabolic diseases in whole organisms in real time. The findings are published in "
              "the journal Advanced Science.")
FR_TITLE = "Le maire de Béziers était poursuivi devant le tribunal correctionnel de Montpellier"
# 必须长过 500 字符，否则测不到截断路径（线上那条 BBC 摘要正是在 500 处被切成半句）
EN_SUMMARY = EN_SUMMARY_RAW = (
    "Researchers from the University of Bath have developed fluorescent molecular probes that "
    "can detect changes in glucose levels inside living animals, giving scientists new ways of "
    "visualizing sugar uptake and studying diabetes, cancer and other metabolic diseases in whole "
    "organisms in real time. The findings are published in the journal Advanced Science and open "
    "the door to continuous, non-destructive monitoring of metabolic function in intact tissue, "
    "which until now required terminal sampling procedures or invasive instrumentation of the "
    "subject, both of which destroy the very signal they are meant to measure.")



def _runtime_js():
    """抽出 JS 片段，并**按 Python 的转义语义解码**后再交给 node。

    必须解码：这段 JS 活在 build_rss_aggregator.py 的一个普通（非 raw）三引号字符串里，
    源码里的 `\\u4e00` 会被 Python 变成真字符、`\\n` 会被变成 JS 认识的 `\n`。
    直接把文件字节喂 node 会同时得到两种语义的混合物，node 当场语法错。
    """
    import ast
    with open(BUILD, encoding="utf-8") as fh:
        text = fh.read()
    i = text.find(START)
    assert i >= 0, "找不到 _needsTranslation 锚点，本判据失去意义（%s）" % BUILD
    j = text.find(END, i)
    assert j > i, "找不到运行时翻译诊断的结束锚点，抽取区间无法确定"
    raw = text[i:j]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")          # \p{L} 这类非法转义是既有写法，警告无害
        js = ast.literal_eval('"""' + raw + '"""')
    assert "GTX_ABORT_MS" in js, "抽取区间没覆盖到断路器（锚点漂了），判据会假绿"
    return js


def _run(node_src):
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "_runtime_translate_guard.js")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(node_src)
    r = subprocess.run(["node", p], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    assert r.returncode == 0, "node 判据本身崩了：%s\n%s" % (r.stdout[-1500:], r.stderr[-1500:])
    return json.loads(r.stdout.strip().splitlines()[-1])


PRELUDE = r"""
var ART = [];
var afItems = [];
/* 运行时队列现在读窗口指纹（wallLimit/filter/sortMode/artKey）：真实页面里这些都是全局，
   harness 必须一并给出，否则判据是"环境缺东西"而红，不是实现有 bug。 */
var wallLimit = 120, WALL_STEP = 80;
var filter = { type: 'all' }, sortMode = 'newest';
function artKey(a){ return a.u; }
var localStorage = (function(){ var s={}; return {
  getItem:function(k){ return Object.prototype.hasOwnProperty.call(s,k)?s[k]:null; },
  setItem:function(k,v){ s[k]=String(v); }, removeItem:function(k){ delete s[k]; } }; })();
var AbortController = (typeof globalThis.AbortController === 'function')
  ? globalThis.AbortController : function(){ this.signal={}; };
var renderWall = function(){ renderCalls.wall++; };
var renderPanel = function(){}; var updateUnreadBtn = function(){}; var updateBmBtn = function(){};
var updateTitle = function(){}; var buildArt = function(){}; var renderChips = function(){};
var toast = function(){};
var renderCalls = { wall: 0 };
var REQ = [];                      // 所有出站请求（含 googleapis 与 TR_API）
function fetch(url, opts){
  REQ.push({ url: String(url), body: opts && opts.body ? String(opts.body) : '' });
  if (globalThis.__MODE === 'reject') return Promise.reject(new Error('Failed to fetch'));
  if (String(url).indexOf('translate_a/single') >= 0) return Promise.reject(new Error('CORS'));
  return Promise.resolve({ ok: true, status: 200, json: function(){
    var texts = JSON.parse(String(opts.body)).texts;
    return Promise.resolve({ ok: true, engine: 'agnes',
      translations: texts.map(function(t){ return '译文：' + t.slice(0, 6); }) });
  }});
}
"""

EPILOGUE = r"""
function dump(o){ process.stdout.write(JSON.stringify(o) + "\n"); }
"""


def _drive(body, mode="cors", wait_ms=1200):
    src = (PRELUDE + "\nglobalThis.__MODE = %s;\n" % json.dumps(mode)
           + _runtime_js() + "\n" + body + "\n" + EPILOGUE)
    return _run(src + "setTimeout(function(){%s}, %d);\n" % (body_dump(body), wait_ms))


def body_dump(body):
    # 由 body 自己定义 RESULT 变量；这里只负责在等待后打印
    return "dump(RESULT)"


def test_chinese_text_is_not_sent_even_with_symbol_runs():
    """变异体一：中文里夹 78 个 `+` 与 ANSI 残片，判据不许再判成"需要翻译"。"""
    body = ("var RESULT = { zh: _needsTranslation(%s), hn: _needsTranslation(%s) };"
            % (json.dumps(ZH_SPAM, ensure_ascii=False), json.dumps(HN_STUB)))
    out = _drive(body, wait_ms=0)
    assert out["zh"] is False, "中文垃圾文本被判成需要翻译（符号把 cjk 占比稀释了）"


def test_hn_metadata_stub_is_not_translated():
    """HN 链接投稿的正文就是 Article URL/Points 元数据，翻它没有任何产出。"""
    body = "var RESULT = { hn: _needsTranslation(%s) };" % json.dumps(HN_STUB)
    out = _drive(body, wait_ms=0)
    assert out["hn"] is False, "HN 元数据样板仍被送去翻译（纯浪费配额，译文里只有 URL 和数字）"


def test_plain_english_still_needs_translation():
    """反向对照：把中文挡掉的同时不许把英/法文一起挡了，否则这条判据是摆设。"""
    body = ("var RESULT = { en: _needsTranslation(%s), fr: _needsTranslation(%s) };"
            % (json.dumps(EN_TITLE), json.dumps(FR_TITLE)))
    out = _drive(body, wait_ms=0)
    assert out["en"] is True, "正常英文标题不再翻译了 —— 那是把功能改坏，不是修守卫"
    assert out["fr"] is True, "法文标题不再翻译了 —— 同上"


def test_summary_still_translated_and_never_cut_mid_word():
    """摘要必须继续翻（卡片渲染 a.s，见 build_rss_aggregator.py 的 card-summary），
    但发出去的截断不许落在单词中间 —— 线上实测 "Hundreds of trekkers on the " 这种半句。

    这条是**反向护栏**：我一开始打算"批量只翻标题"，核对渲染面之后作废 ——
    摘掉摘要 = 卡片直接退回英文，属于功能回归，不是省配额。
    """
    body = ("""
var ART = [{ t: %s, s: %s }];
_translateWallItems();
var RESULT = {};
/* 必须在兜底往返**之后**再取值：同步读会拿到翻译前的旧串（我第一版就踩了，
   判据因此把"成功回填"误判成"没翻"）。*/
setTimeout(function(){ RESULT.t = ART[0].t; RESULT.s = ART[0].s; RESULT.reqs = REQ; }, 900);
""" % (json.dumps(EN_TITLE), json.dumps(EN_SUMMARY)))
    out = _drive(body, mode="cors", wait_ms=1500)
    assert u"译文" in out["s"], ("摘要没被翻译（卡片显示 a.s，这里退化成英文就是回归）：%s" % out["s"][:60])
    from urllib.parse import unquote
    # 夹具自证：硬切 500 必须正好落在词中间，否则这条判据是空转的 ——
    # 上一版夹具的 500 位是空格，变异体（退回 .slice(0,500)）因此当场存活，判据什么也没测到。
    assert EN_SUMMARY[499].isalpha() and EN_SUMMARY[500].isalpha(), (
        "夹具第 500 位不在词中间（实为 %r），本判据测不到截断" % EN_SUMMARY[498:504])
    sent = [unquote(r["url"].split("q=")[-1]) for r in out["reqs"]
            if "translate_a/single" in r["url"] and "q=" in r["url"]]
    long_ones = [q for q in sent if len(q) >= 400]
    assert long_ones, "摘要根本没走直连（无法验证截断位置）：%r" % sent
    for q in long_ones:
        assert q == EN_SUMMARY[:len(q)], "发出的不是摘要前缀，无法按原文核对截断"
        tail = EN_SUMMARY[len(q):len(q) + 1]
        assert tail in (" ", ".", ",", ""), (
            "直连请求在 %d 字符处从单词中间截断，译文会带半截词：…%r|截点后紧跟 %r"
            % (len(q), q[-18:], tail))


def test_breaker_trips_after_one_all_failed_batch():
    """断路器：GTX 在大陆网络 0 成功，第一批次全败就该熔断，不许再白等第二轮。"""
    body = ("""
var RESULT = {};
_browserGtx([%s, %s]).then(function(){
  return _browserGtx([%s, 'another english sentence to translate']);
}).then(function(){
  RESULT.google = REQ.filter(function(r){
    return r.url.indexOf('translate_a/single') >= 0; }).length;
  dump2(RESULT);
});
function dump2(o){ dump(o); }
""" % (json.dumps(EN_TITLE), json.dumps(FR_TITLE), json.dumps("Third english line to translate")))
    out = _drive(body, mode="reject", wait_ms=2000)
    assert out["google"] <= 2, (
        "两轮全败仍在发直连请求（熔断阈值太高）：本环境直连成功率为 0，第一轮就该停 —— 实发 %d 次"
        % out["google"])


def test_dead_verdict_survives_the_next_visit():
    """失败译文进不了 _trCache（那里只存成功），所以"今天已熔断"必须单独持久化，
    否则每次重访都重发同一批必败请求（线上控制台正是这样刷屏的）。"""
    body = ("""
localStorage.setItem('_gtxDead', new Date().toISOString().slice(0,10));
_browserGtx([%s]);
var RESULT = { google: REQ.filter(function(r){
  return r.url.indexOf('translate_a/single') >= 0; }).length };
""" % json.dumps(EN_TITLE))
    out = _drive(body, mode="reject", wait_ms=600)
    assert out["google"] == 0, (
        "昨天已判不可达，今天仍重发直连 %d 次：本环境成功率为 0，这就是纯控制台噪音" % out["google"])


def test_duplicate_texts_in_one_batch_cost_one_request():
    """同一批里重复文本（跨源同稿很常见）只许发一次请求，结果回填给每个位置。"""
    body = ("""
_browserGtx([%s, %s, %s]);
var RESULT = { google: REQ.filter(function(r){
  return r.url.indexOf('translate_a/single') >= 0; }).length };
""" % (json.dumps(EN_TITLE), json.dumps(EN_TITLE), json.dumps(FR_TITLE)))
    out = _drive(body, mode="reject", wait_ms=600)
    assert out["google"] == 2, (
        "同批重复文本各发了一次（应只发 2 次，实发 %d）" % out["google"])


def test_gtx_abort_window_is_shortened_for_unreachable_network():
    """8s 超时是给"能连通但慢"设计的；本环境是连不通，等待要压在 4s 内。"""
    body = "var RESULT = { ms: (typeof GTX_ABORT_MS === 'number') ? GTX_ABORT_MS : -1 };"
    out = _drive(body, wait_ms=0)
    assert out["ms"] != -1, "没有 GTX_ABORT_MS 常量，超时是硬编码的字面量（无法验证真实值）"
    assert out["ms"] <= 4000, "直连超时仍是 %sms：连不通的网络上等于每批白等" % out["ms"]


def test_yesterdays_breaker_expires_and_gtx_is_tried_again():
    """熔断是**按天**存的：昨天那条不许今天继续掐直连。

    同日那一侧已有判据（`test_dead_verdict_survives_the_next_visit` 写的是今天的日期），
    但没人钉"隔日复位"这一支。代码里 `_gtxDeadToday()` 比的是日期相等，所以现在是好的；
    坏在一个看起来很无害的改法上：把 `_gtxDeadToday` 写成 `return !!_gtxDayKey()`（有值就算熔断），
    全部判据照样绿，而用户侧后果是"一次网络抖动 ⇒ 永久不再试浏览器直连"，
    所有量压到限量兜底 Agnes（注释里写明它不是主力）。
    """
    body = """
var _y = new Date(Date.now() - 86400000).toISOString().slice(0,10);
localStorage.setItem('_gtxDead', _y);
_browserGtx([%s]);
var RESULT = { google: REQ.filter(function(r){
    return r.url.indexOf('translate_a/single') >= 0; }).length,
  day: _y, today: new Date().toISOString().slice(0,10) };
""" % json.dumps(EN_TITLE)
    out = _drive(body, mode="reject", wait_ms=600)
    assert out["day"] != out["today"], "夹具没造出昨天的日期，这条判据是假测"
    assert out["google"] >= 1, (
        "昨天的熔断今天仍在拦直连（实发 %d 次）：按天存的全部意义就是隔日自动复位"
        % out["google"])
