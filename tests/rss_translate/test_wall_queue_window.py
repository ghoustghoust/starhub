# -*- coding: utf-8 -*-
"""运行时翻译队列必须按"渲染窗口"排队，而不是遍历整条 ART。

现场证据（2026-10-01 实测，读数见 HANDOFF §10.25）：
  产物 25766 条里标题仅 5.5% 仍需翻（构建期预翻覆盖 100%），摘要 33% 需翻；
  而 `_translateWallItems` 的候选来自 **整条 ART**，每批 10 条、批间 2.5s
  ⇒ 一次访问要把 ≈9902 条待翻文本走 ≈41 分钟，绝大部分花在看不见的条目上；
  GTX 在大陆不可达时这些还整批转 /api/translate ⇒ 访客在替全库消耗服务端配额。

用户定的口径（2026-10-01 批准）：
  ① 队列 = visibleArts()[0..wallLimit]，按屏幕顺序；窗口外不入队（滚到才翻）
  ② 摘要只翻窗口前 30 条
  ③ 切排序/筛选打断在飞批次：旧批次的结果**不得写进新窗口**
  ④ 构建期预翻一行不动

判据全部跑真 node（抽 build_rss_aggregator.py 的 JS + shim），且刻意只依赖
"发出去了什么文本 / 条目被写成什么 / 在飞的请求被 abort 没有"这些外部可观察量 ——
不锁内部变量名，免得实现一改判据就假红。
"""
import json
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")

START = "var _CJK_R = "
END = "/* 运行时翻译诊断"


def _runtime_js():
    import ast
    import warnings
    with open(BUILD, encoding="utf-8") as fh:
        text = fh.read()
    i = text.find(START)
    assert i >= 0, "找不到 _CJK_R 锚点（_needsTranslation 上方的常量声明），判据失去意义"
    j = text.find(END, i)
    assert j > i, "找不到运行时翻译诊断的结束锚点"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        js = ast.literal_eval('"""' + text[i:j] + '"""')
    assert "_translateWallItems" in js, "抽取区间没覆盖 _translateWallItems，判据会假绿"
    return js


PRELUDE = r"""
var ART = [], afItems = [];
var wallLimit = 120, WALL_WINDOW_SUMMARIES = 30;
var filter = { type: 'all' }, sortMode = 'newest';   // 队列要读的页面级全局，给确定值而不是让它 ReferenceError
function artKey(a){ return a.u; }
var renderCalls = { wall: 0 };
function renderWall(){ renderCalls.wall++; }
function renderPanel(){}
function renderChips(){}
function buildArt(){}
function updateTitle(){}
function updateUnreadBtn(){}
function updateBmBtn(){}
function toast(){}
var _store = {};
var localStorage = {
  getItem: function(k){ return Object.prototype.hasOwnProperty.call(_store,k) ? _store[k] : null; },
  setItem: function(k,v){ _store[k]=String(v); }, removeItem: function(k){ delete _store[k]; }
};
/* 窗口的唯一事实源：判据靠它定义"看得见的范围"，不碰实现内部变量 */
var WINDOW = [];
function visibleArts(){ return WINDOW; }
var SIGNALS = [];                     // 每次 fetch 带出去的 signal，用来验"在飞的有没有被打断"
function AbortController(){
  var c = { aborted: false, signal: {} };
  c.signal.aborted = false;
  c.abort = function(){ c.aborted = true; c.signal.aborted = true; };
  SIGNALS.push(c);
  return c;
}
var REQ = [];
/* 兜底往返带 400ms 延迟：不打延迟的话整批在换窗口前就已 resolve，
   "在飞被打断"这条判据根本测不到（我第一版就是这样把假红误当成实现没生效）。 */
function fetch(url, opts){
  REQ.push({ url: String(url), body: opts && opts.body ? String(opts.body) : '' });
  if (String(url).indexOf('translate_a/single') >= 0)
    return Promise.reject(new TypeError('Failed to fetch'));
  var texts = JSON.parse(String(opts.body)).texts;
  return new Promise(function(resolve){
    setTimeout(function(){
      resolve({ ok: true, status: 200, json: function(){
        return Promise.resolve({ ok: true, engine: 'agnes',
          translations: texts.map(function(t){ return '译' + t.slice(0,4); }) });
      }});
    }, 400);
  });
}
function dump(o){ process.stdout.write(JSON.stringify(o) + "\n"); }
"""


def _run(body, wait_ms=1500):
    src = PRELUDE + "\n" + _runtime_js() + "\n" + body + \
          "\nsetTimeout(function(){ dump(RESULT); process.exit(0); }, %d);\n" % wait_ms
    # process.exit 是必须的：_translateWallItems 尾部有 setTimeout 自我续跑，
    # 不显式退出会让 node 一直挂着 —— 这套判据曾因此跑到 402 秒，CI 里绝不可接受。
    p = os.path.join(tempfile.gettempdir(), "_wall_window_guard.js")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(src)
    r = subprocess.run(["node", p], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    assert r.returncode == 0, "node 判据本身崩了：\n%s\n%s" % (r.stdout[-1500:], r.stderr[-1500:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def _sent_texts(out):
    from urllib.parse import unquote
    texts = []
    for r in out["reqs"]:
        if "translate_a/single" in r["url"]:
            texts.append(unquote(r["url"].split("q=")[-1]))
        elif r["body"]:
            try:
                texts.extend(json.loads(r["body"]).get("texts", []))
            except ValueError:
                pass
    return texts


SETUP = r"""
function mk(i){ return { t: 'English headline number ' + i + ' about models',
                         s: 'English summary body ' + i + ' ' + 'word '.repeat(20),
                         c: 'ai', u: 'http://x/' + i, sk: 'src_' + (i % 9),
                         d: '2026-10-01T08:00:00+08:00' }; }
ART = []; for (var i=0;i<400;i++) ART.push(mk(i));
/* 窗口刻意摆在 ART 的**尾部**：队列只要还在"从 ART 头上遍历"，第一批就会越界，
   一条判据当场咬住。把窗口放在头部时第一批恰好落在窗口内，判据是空转的（我踩过）。 */
WIN = ART.slice(350, 400);
WINDOW = WIN;
wallLimit = 50;
RESULT = {};
"""


def test_queue_only_touches_the_rendered_window():
    """窗口外条目（ART[0..349]）一条都不许发出去。"""
    body = SETUP + "\n_translateWallItems();\nsetTimeout(function(){ RESULT.reqs = REQ; RESULT.win = WINDOW.map(function(a){return a.t;}).slice(0,2); }, 1200);\n"
    out = _run(body)
    sent = _sent_texts(out)
    assert sent, "窗口内条目一条都没发 —— 那是把功能改坏，不是减负"
    for t in sent:
        for bad in range(0, 350):
            if ('headline number %d ' % bad) in t or ('summary body %d ' % bad) in t:
                raise AssertionError(
                    "窗口外的第 %d 条被发出去了（队列仍在遍历整条 ART）：%s" % (bad, t[:60]))


def test_summaries_are_capped_at_the_first_thirty_in_window():
    """摘要只翻窗口前 30 条：窗口第 31..50 条（全局 380..399）的摘要不许发。"""
    body = SETUP + "\n_translateWallItems();\nsetTimeout(function(){ RESULT.reqs = REQ; }, 1200);\n"
    out = _run(body)
    sent = _sent_texts(out)
    over = [t for t in sent if "summary body" in t and
            any(("summary body %d " % k) in t for k in range(380, 400))]
    assert not over, (
        "窗口前 30 条之外的摘要仍被发出（摘要才是量的大头）：%s" % [o[:40] for o in over[:3]])
    assert any("summary body 353 " in t for t in sent), "窗口前 30 条内的摘要该翻却没翻"


def test_inflight_results_are_not_written_after_the_window_changes():
    """切排序/筛选打断在飞批次：旧批次的译文不许写进新窗口的条目。"""
    body = SETUP + r"""
_translateWallItems();
var before = { t: WIN[0].t, s: WIN[0].s };
setTimeout(function(){
  WINDOW = ART.slice(0, 50); wallLimit = 50;      // 用户此刻切了排序：窗口整个换掉
  _translateWallItems();
}, 200);
setTimeout(function(){
  RESULT.old_untouched = (WIN[0].t === before.t && WIN[0].s === before.s);
  RESULT.reqs = REQ;
}, 1200);
"""
    out = _run(body)
    assert out["old_untouched"], (
        "窗口已经换掉，旧批次的结果仍写进了原首条 —— 打断语义没生效（会串台到新窗口）")


def test_inflight_request_is_aborted_when_window_changes():
    """不只是丢弃结果：真正在飞的那批请求要被打断，否则带宽与配额照扣。"""
    body = SETUP + r"""
_translateWallItems();
setTimeout(function(){ WINDOW = ART.slice(0, 50); wallLimit = 50; _translateWallItems(); }, 200);
setTimeout(function(){
  RESULT.aborted = SIGNALS.filter(function(c){ return c.aborted; }).length;
  RESULT.n_signals = SIGNALS.length;
}, 1200);
"""
    out = _run(body)
    assert out["aborted"] > 0, (
        "换窗口时没有一个在飞请求被 abort（打断只停留在丢结果，没打断请求）：signal=%d"
        % out["n_signals"])


def test_growing_the_window_does_not_abort_the_inflight_batch():
    """滚动扩窗是"追加"，不是"替换"：同一窗口头部变长时不许打断在飞批次。

    方向别搞反 —— 若把 wallLimit/长度算进窗口指纹，快速滚动会每滚一次就 abort 一轮，
    结果是一条都翻不完（我写完①②③后自己发现的设计缺陷，这条是它的护栏）。
    """
    body = SETUP + r"""
_translateWallItems();
setTimeout(function(){ WINDOW = ART.slice(350, 450); wallLimit = 100; _translateWallItems(); }, 200);
setTimeout(function(){
  RESULT.aborted = SIGNALS.filter(function(c){ return c.aborted; }).length;
  RESULT.newHead = ART[350].t;
}, 1200);
"""
    out = _run(body, wait_ms=1500)
    assert out["aborted"] == 0, (
        "只是把窗口加长就打断了在飞批次（扩窗被当成换窗口）：abort=%d" % out["aborted"])


def test_request_volume_is_bounded_by_the_window():
    """一次访问的总量必须被窗口卡住：≤ wallLimit 条标题 + 30 条摘要。

    这条钉的是本次改动的**目的本身**（旧写法无界，会把 9902 条走完 ≈41 分钟）。
    以后谁把窗口撑大或把摘要上限抬到窗口级别，这里就会红。
    """
    body = SETUP + r"""
_translateWallItems();
setTimeout(function(){
  var seen = {};
  REQ.forEach(function(r){
    if (r.url.indexOf('translate_a/single') >= 0) {
      seen[decodeURIComponent(r.url.split('q=').pop())] = 1;
    } else if (r.body) {
      JSON.parse(r.body).texts.forEach(function(x){ seen[x] = 1; });
    }
  });
  RESULT.distinct_texts = Object.keys(seen).length;
}, 1200);
"""
    out = _run(body, wait_ms=1500)
    n = out["distinct_texts"]
    assert n > 0, "窗口内一条都没发 —— 那是把功能改坏"
    assert n <= 50 + 30, (
        "发出的文本数 %d 超出窗口上限（窗口 50 条标题 + 摘要 30 条）：总量必须随浏览而非随全库" % n)


def test_browser_gtx_concurrency_stays_capped():
    """直连并发必须 ≤3。窗口化之后一次要发上百条，若有人把 worker 数调大，
    这就是把"减负"变成"打爆 Google 端点"的入口。"""
    body = SETUP + r"""
var INFLIGHT = 0, PEAK = 0;
var _orig_fetch = fetch;
fetch = function(url, opts){
  if (String(url).indexOf('translate_a/single') >= 0) {
    INFLIGHT++; if (INFLIGHT > PEAK) PEAK = INFLIGHT;
    Promise.reject(new TypeError('Failed to fetch')).catch(function(){}).then(function(){ INFLIGHT--; });
  }
  return _orig_fetch(url, opts);
};
WINDOW = ART.slice(350, 400);   // 50 条，足够让并发达到上限
_translateWallItems();
setTimeout(function(){ RESULT.peak = PEAK; }, 1200);
"""
    out = _run(body, wait_ms=1500)
    assert out["peak"] <= 3, (
        "浏览器直连并发达到 %d（应 ≤3）：窗口化后一次发上百条，并发调大就是打爆端点" % out["peak"])


def test_window_titles_still_get_translated():
    """反向护栏：减负不许把首屏标题翻不动（构建期只覆盖 94.5%，剩下的必须补上）。"""
    body = SETUP + r"""
_translateWallItems();
setTimeout(function(){
  RESULT.translated = WINDOW.filter(function(a){ return /^译/.test(String(a.t)); }).length;
}, 1200);
"""
    out = _run(body)
    assert out["translated"] >= 10, (
        "窗口内 50 条标题只翻出 %d 条 —— 首屏优先是本设计的目的地，不是被省掉的对象"
        % out["translated"])
