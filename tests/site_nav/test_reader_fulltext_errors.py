# -*- coding: utf-8 -*-
"""服务端说"我没抓到"时，用户必须看见；不许只删转圈。

现场证据（build_rss_aggregator.py:4525，brief 里的 :4186 已被 Task 1-5 顶掉）：
    if(d.ok&&d.content) _insertFulltext(d.content);
  —— 没有 else。`ok:false` 时上一行刚把 `.r2-ft-loading` remove 掉，于是阅读器
  既不显示正文也不显示原因，用户只能反复点（"加载失败/部分内容加载不出"）。
  只有网络层 reject（.catch）才有 UI。更要命的是 `:4523` 的 `_articleCache[a.u]=d`
  在判断**之前**，失败结果被缓存 4 小时：`:4509` 再读到它就永久静默。

契约（与 api/article.js 的 Task 5 出口一一对应，码名/字段名不许自创）：
  失败 {ok:false, error:'missing_url'|'invalid_url'|'timeout'|'fetch_failed'
        |'challenge_page'|'no_body'|'extraction_failed'}
  成功 {ok:true, …, source:'rss_fulltext'|'readability'|'youtube'|'github',
        degraded?:'paywall'|'short'}
  注意 `timeout` 在 api/article.js:196 由 `e.name === 'AbortError' ? 'timeout' : 'fetch_failed'`
  动态产生，不是字面 `error:'timeout'` —— 所以本判据只查前端文案表**有没有这一格**，
  不查服务端有没有 `error:'timeout'` 这个字面量（那会永远查不到）。
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")

# 后端契约（Task 5 定死的 7 个码 + 2 个 degraded 值），前端文案表必须逐一对应。
FAILURE_CODES = ["missing_url", "invalid_url", "timeout", "fetch_failed",
                 "challenge_page", "no_body", "extraction_failed"]
DEGRADED_VALUES = ["paywall", "short"]


def _src():
    with io.open(BUILD, encoding="utf-8") as fh:
        return fh.read().replace("\\'", "'")


def _block(a, b):
    t = _src()
    i = t.find(a)
    assert i >= 0, "找不到 %r：实现被删了或改名了" % a
    j = t.find(b, i)
    assert j > i, "%r 没有结束边界" % a
    return t[i:j]


FT = _block("function fetchFullArticle(a){", "/* ── 媒体嵌入")


def test_server_side_failure_shows_a_note():
    assert re.search(r"_showFulltextNote\(", FT), \
        "ok:false 仍走静默路径：用户看不到失败原因"


# ---- 补充钉位（brief 的 4 条不够）：缓存命中分支也调 _showFulltextNote，
# 只查"FT 里出现过"的话，把首次抓取那一处调用删掉判据照样绿（变异① 的幸存者形状）。
def test_fresh_fetch_failure_shows_a_note_not_silence():
    """首次抓取（`.then(function(d){…})`）的失败分支必须自己出声，不能靠缓存分支顶。"""
    m = re.search(r"\.then\(function\(d\)\{", FT)
    assert m, "fetch(...).then(function(d){ 的形状变了，重读判据前提"
    seg = FT[m.end():m.end() + 900].split(".catch(")[0]
    assert "_showFulltextNote(" in seg, \
        "首次抓取失败没有任何用户可见提示（问题 2 的静默还在）：%s" % seg[-300:]


def test_failure_result_is_not_cached():
    """缓存写入必须只在 d.ok 为真时发生，否则 4h 内重试永远拿回同一份失败。"""
    m = re.search(r"_articleCache\[a\.u\]\s*=\s*d", FT)
    assert m, "_articleCache 的写入点变了，重读判据前提"
    head = FT[max(0, m.start() - 160):m.start()]
    assert re.search(r"if\s*\(\s*d\.ok", head), "失败结果又被无条件缓存了（写缓存前没有 d.ok 判断）"


def test_cache_hit_branch_keeps_the_ok_gate():
    """缓存命中分支必须仍然判 ok：分享通道（:5563）往同一张表里写 d 时不判 ok，
    命中失败项若直接 `_insertFulltext(d.content)` 就是把 undefined 塞进 innerHTML。"""
    m = re.search(r"if\s*\(\s*_articleCache\[a\.u\]\s*\)", FT)
    assert m, "缓存命中分支的形状变了，重读判据前提"
    seg = FT[m.end():m.end() + 420].split("var old=")[0]
    assert re.search(r"&&\s*\w*\.?ok\b|\bd\.ok\b|cc\.ok", seg), \
        "缓存命中不再判 ok：会把失败项的 undefined 当正文插入：%s" % seg[:220]


def test_every_documented_error_code_has_copy():
    """前端文案表要覆盖契约里的每个码：漏一个就是新的静默（走 fallback 也算，但必须显式）。"""
    notes = _src()
    for code in ("challenge_page", "no_body", "timeout", "fetch_failed", "extraction_failed",
                 "invalid_url", "missing_url"):
        assert re.search(r"['\"]%s['\"]\s*:" % code, notes), "错误码 %s 没有对应文案" % code


def test_error_code_table_has_no_off_contract_entries():
    """文案表也不许多出自造码：前端写了契约里不会返回的码 = 看上去覆盖了、其实没人调用。"""
    m = re.search(r"var _FT_NOTES\s*=\s*\{(.*?)\n\s*\};", _src(), re.S)
    assert m, "读不到 _FT_NOTES 表体"
    keys = re.findall(r"['\"]([a-z_]+)['\"]\s*:", m.group(1))
    assert keys, "_FT_NOTES 是空的（等于没文案表）"
    extra = sorted(set(keys) - set(FAILURE_CODES))
    assert not extra, "文案表里有契约外的码 %s：后端不会返回它，这条格子是装饰" % extra
    missing = [c for c in FAILURE_CODES if c not in keys]
    assert not missing, "契约里的码 %s 没有文案格" % missing
    assert len(keys) == len(set(keys)), "同一个码写了两次文案：后一格覆盖前一格，读代码的人看不出来"


def test_notes_table_is_actually_consumed():
    """表定义了必须有人按 `d.error` 取：只写表不查表就是"文案存在但用户永远看不到"。"""
    assert re.search(r"_FT_NOTES\s*\[", FT), "_FT_NOTES 定义了却没按错误码取用（新造空表）"
    # 按位置钉：两处调用点各自都必须从表里取值，写死一句通用文案就是"表在、没人读"。
    m = re.search(r"\.then\(function\(d\)\{", FT)
    assert m, ".then(function(d){ 的形状变了，重读判据前提"
    fresh = m.group(0) + FT[m.end():m.end() + 900].split(".catch(")[0]
    assert re.search(r"_FT_NOTES\s*\[\s*d\.error\s*\]", fresh), \
        "首次抓取失败没按 d.error 取文案：服务端的回答被压成同一句通用提示\n%s" % fresh[-260:]
    m2 = re.search(r"if\s*\(\s*_articleCache\[a\.u\]\s*\)", FT)
    assert m2, "缓存命中分支的形状变了，重读判据前提"
    hit = FT[m2.end():m2.end() + 420]
    assert re.search(r"_FT_NOTES\s*\[", hit), "缓存命中分支没读文案表：%s" % hit[:220]


def test_degraded_body_is_labeled():
    src = _src()
    assert "degraded" in src, "degraded 标注没落到前端"
    assert re.search(r"正文可能不完整", src) or re.search(r"\\u6b63\\u6587\\u53ef\\u80fd\\u4e0d\\u5b8c\\u6574", src), \
        "degraded 没有用户可见文案"


def test_both_degraded_values_have_their_own_copy():
    """paywall 和 short 是两种不同的"为什么这不完整"，合成一句就是瞎报。"""
    src = _src()
    for v in DEGRADED_VALUES:
        assert re.search(r"degraded\s*===?\s*['\"]%s['\"]" % v, src), "degraded=%r 没有分支" % v
    assert re.search(r"付费", src), "paywall 没有用户可见文案"


def test_degraded_is_passed_to_the_inserter():
    """`_insertFulltext` 收了 degraded 形参就必须真的被传值：签名改了没人传 = 标注永远不亮。"""
    src = _src()
    assert re.search(r"function _insertFulltext\(\s*html\s*,\s*degraded\s*\)", src), \
        "_insertFulltext 没有 degraded 形参"
    assert re.search(r"_insertFulltext\(\s*d\.content\s*,\s*d\.degraded\s*\)", FT), \
        "抓取成功那一路没把 degraded 传进去"
    assert re.search(r"_insertFulltext\([^)]*\.degraded\s*\)", FT), \
        "缓存命中那一路没把 degraded 传进去"
