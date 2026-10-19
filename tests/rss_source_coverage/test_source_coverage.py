# tests/rss_source_coverage/test_source_coverage.py
# -*- coding: utf-8 -*-
"""源覆盖率健康：去重崩溃、域熔断误伤、RSS 1.0/RDF 解析缺口。

Why: 2026-09-16 05:17~05:25 UTC 的三个"卡片去重"提交上线后，"有数据源"从 968 一夜
掉到 815 并一直平在那里（2026-09-20 05:14 实测 776）。逐一实测 182 个问题源后归因：
  - 84 个源死于 _dedup_source_items 的 TypeError —— pub_date 是 datetime 对象，
    .replace("Z","+00:00") 抛 TypeError，而 except 只列了 (ValueError, AttributeError)，
    异常逃到 _worker 的裸 except 被静默吞成"0 条"，日志里一个字都没有；
  - 51 个 api.xgo.ing 源根本没被尝试过（今日 18 场被尝试 0.00 次/场）—— 域熔断把
    "HTTP 200 但 feed 0 条"也计为域名失败，同一域名 160 个源里前 3 个空就把其余全跳过，
    而提交顺序固定 ⇒ 尾部源永远轮不到；
  - 8 个源上游有 37~137 条、我方解析出 0 条 —— _parse_rss_item 用不带命名空间的
    findtext("title")，RSS 1.0/RDF 里 title 属于 {http://purl.org/rss/1.0/} 命名空间，
    取不到就被 `if not title: return` 全部丢弃（DW/Nature/Science 均属此类）。
"""
import datetime
import io
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()


# ────────────────── 1. 去重：datetime 型 pub_date 不得抛 TypeError ──────────────────

def _it(title, link, pub, summary=None):
    return {"title": title, "link": link,
            "summary": "s-" + link if summary is None else summary,
            "pub_date": pub, "source": "S", "source_key": "s_1", "cat": "ai"}


def test_dedup_keeps_same_title_far_apart_when_pub_date_is_datetime():
    """同名条目相隔 30 天 ⇒ 超出 48h 窗口，两条都该保留，且不得抛异常。

    这是 84 个源归零的直接复现：大存档 feed（OpenAI 博客 1209 条、Vercel 1595 条）
    与推特转推必然出现同名标题，一进这个分支整个源就没了。
    """
    d1 = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.timezone.utc)
    d2 = d1 + datetime.timedelta(days=30)
    items = [_it("GPT-5 发布", "http://x/1", d1), _it("GPT-5 发布", "http://x/2", d2)]
    got = mod._dedup_source_items(items, "s_1")
    assert len(got) == 2, "48h 窗口外的同名条目应都保留，实际 %d 条" % len(got)


def test_dedup_still_collapses_same_title_within_48h():
    """防"修过头"：相隔 1 小时的同名条目仍须折叠成 1 条。"""
    d1 = datetime.datetime(2026, 9, 19, 8, 0, tzinfo=datetime.timezone.utc)
    d2 = d1 + datetime.timedelta(hours=1)
    items = [_it("GPT-5 发布", "http://x/1", d1), _it("GPT-5 发布", "http://x/2", d2)]
    got = mod._dedup_source_items(items, "s_1")
    assert len(got) == 1


def test_dedup_tolerates_string_and_empty_pub_date():
    """pub_date 为字符串或缺失时，48h 判定不该因此炸或误删。"""
    items = [_it("标题甲", "http://x/1", "2026-09-01T08:00:00+00:00"),
             _it("标题甲", "http://x/2", ""),
             _it("标题甲", "http://x/3", None)]
    got = mod._dedup_source_items(items, "s_1")
    assert len(got) == 3, "拿不到可靠时间时宁可不折叠，也不能整源消失"


def test_dedup_compares_naive_and_aware_pub_date_without_raising():
    """同一源里 naive 与 aware 混排是真实存在的（pubDate 写成 '2026-09-16 14:00:00'
    就没有时区），相减会抛 TypeError —— 必须归一时区后再比。"""
    aware = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.timezone.utc)
    naive = datetime.datetime(2026, 9, 20, 8, 0)          # 无 tzinfo
    items = [_it("同名条目", "http://x/1", aware), _it("同名条目", "http://x/2", naive)]
    got = mod._dedup_source_items(items, "s_1")           # 相差 19 天 > 48h ⇒ 都保留
    assert len(got) == 2


def test_dedup_without_reliable_time_collapses_only_full_duplicates():
    """时间不可靠时不能一律保留：同源多条同标题但摘要不同（每日专栏）要留住，
    标题+摘要完全一致的（重复群发）必须折叠 —— 否则未去重的重复会挤掉
    ITEMS_PER_SOURCE 名额，把真条目挡在 30 条之外。"""
    a = _it("每日专栏", "http://x/1", "")
    a["summary"] = "第一期内容"
    b = _it("每日专栏", "http://x/2", "")
    b["summary"] = "第二期内容"
    c = _it("每日专栏", "http://x/3", "")
    c["summary"] = "第一期内容"
    got = mod._dedup_source_items([a, b, c], "s_1")
    assert len(got) == 2, "摘要不同的两条应留住、摘要全同的第三条应折叠，实际 %d 条" % len(got)


# ────────────────── 2. 抓取失败与"确实空"必须可区分 ──────────────────

def test_fetch_rss_returns_none_on_network_failure(monkeypatch):
    """网络/HTTP 失败 ⇒ None（可计入域名熔断）；成功但 0 条 ⇒ []（中性）。

    今天两种情况都返回 []，调用方无从区分，于是把上游正常的空 feed 当成域名故障。

    必须同时断言"日志里是我们注入的那个错误"：只断言 got is None 的话，
    替身与 _fetch_rss 的签名对不上（TypeError 同样被吞成 None）会让这条假绿 ——
    _fetch_url 加 ua 参数后就真实发生过一次。
    """
    def boom(url, timeout=None, accept=None, ua=None):
        raise OSError("connection refused")
    buf = io.StringIO()
    monkeypatch.setattr(mod.sys, "stderr", buf)
    orig = mod._fetch_url
    mod._fetch_url = boom
    try:
        got = mod._fetch_rss({"key": "net_fail_1", "name": "NetFail", "url": "http://e/feed",
                              "cat": "ai"})
    finally:
        mod._fetch_url = orig
    assert got is None, "网络失败必须与『合法空 feed』可区分，实际 %r" % (got,)
    err = buf.getvalue()
    assert "connection refused" in err, \
        "没走到真实失败分支，日志是：%r（签名漂移会在这里暴露，而不是假绿）" % err[:200]


def test_fetch_rss_parses_rss10_rdf_items():
    """RSS 1.0/RDF（DW / Nature / Science）条目须解析出来：上游 54~137 条现在解析为 0。"""
    rdf = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        ' xmlns="http://purl.org/rss/1.0/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<channel rdf:about="https://example.org/"><title>Ex</title>'
        '<link>https://example.org/</link><description>d</description></channel>'
        '<item rdf:about="https://example.org/a"><title>条目甲</title>'
        '<link rdf:resource="https://example.org/a"/>'
        '<dc:date>2026-09-19T10:00:00Z</dc:date><description>d1</description></item>'
        '<item rdf:about="https://example.org/b"><title>条目乙</title>'
        '<link rdf:resource="https://example.org/b"/>'
        '<dc:date>2026-09-18T10:00:00Z</dc:date><description>d2</description></item>'
        '</rdf:RDF>'
    )
    orig = mod._fetch_url
    mod._fetch_url = lambda url, timeout=None, accept=None, ua=None: rdf
    mod._rss_cache = {}
    try:
        got = mod._fetch_rss({"key": "rdf_probe_1", "name": "RdfProbe",
                              "url": "http://e/rdf", "cat": "news"})
    finally:
        mod._fetch_url = orig
    assert got and len(got) == 2, "RDF 源应解析出 2 条，实际 %r" % (got,)
    assert {g["title"] for g in got} == {"条目甲", "条目乙"}
    assert all(g["link"].endswith("/a") or g["link"].endswith("/b") for g in got)


# ────────────────── 3. 域熔断不得饿死"合法空 feed"的同域兄弟源 ──────────────────

@pytest.fixture
def offline_main(tmp_path, monkeypatch):
    """让 main() 整体离线跑一遍，抓取结果由测试逐个指定。

    delay 用来还原生产的时序形态：真实构建里近千个源排在 12 线程的队列后面，
    熔断是在"前面的源已失败、后面的还排着队"时命中的。延迟为 0 时整场瞬间跑完，
    测不出这条，只会给反向错误信心。
    """
    calls = []
    logged = {}

    def make_sources(n, domain="http://bridge.test"):
        return [{"key": "b%d" % i, "name": "Bridge%d" % i, "cat": "twitter",
                 "url": "%s/feed%d" % (domain, i), "tier": 3} for i in range(n)]

    srcs = make_sources(6)

    def run(handler, n=6, delay=0.0):
        calls.clear()
        logged.clear()
        mod._rss_cache = {}
        srcs_local = make_sources(n)

        def fake_fetch(source, timeout=None):
            if delay:
                import time as _t
                _t.sleep(delay)
            calls.append(source["key"])
            return handler(source)

        def _capture(entry):
            if entry.get("type") == "build":
                logged.update({s["key"]: s for s in entry.get("per_source") or []})

        monkeypatch.setattr(mod, "RSS_SOURCES", srcs_local)
        monkeypatch.setattr(mod, "_fetch_rss", fake_fetch)
        for name in ("_translate_source_items", "_tag_articles", "write_data_chunks",
                     "_save_api_snapshot", "_save_caches", "_load_caches", "_save_history",
                     "_audit_image_quality", "_accumulate_hot_history"):
            monkeypatch.setattr(mod, name, lambda *a, **k: None)
        monkeypatch.setattr(mod, "fetch_newsnow_snapshot", lambda *a, **k: [])
        monkeypatch.setattr(mod, "_run_analysis", lambda *a, **k: {})
        monkeypatch.setattr(mod, "build_html", lambda *a, **k: "<html></html>")
        monkeypatch.setattr(mod, "build_logger",
                            types.SimpleNamespace(append=_capture, cleanup=lambda n: 0))

        def _no_network(*a, **k):
            raise OSError("门禁不应发起网络请求：" + str(a[:1]))
        monkeypatch.setattr(mod.urllib.request, "urlopen", _no_network)
        monkeypatch.chdir(tmp_path)
        mod.main(mode="incremental")
        return Run(list(calls), dict(logged))

    return run


class Run(object):
    """一次 main() 跑完后的两个观测面：真正发起了请求的源、以及落进日志的逐源状态。"""

    def __init__(self, calls, per_source):
        self.calls = calls
        self.per_source = per_source
        self.skipped = [k for k, v in per_source.items() if v.get("status") == "domain_broken"]


def _item(k):
    return [{"title": "t" + k, "link": "http://x/" + k, "summary": "s",
             "pub_date": mod._now_bj().isoformat()}]


def test_valid_empty_feeds_do_not_starve_same_domain_sources(offline_main):
    """同域源的 feed 合法为空时，不得连坐跳过其余同域源。

    这就是 api.xgo.ing 160 个推特源的死法：一批账号本来就不常发推，feed 返回 200 + 0 条
    是正常应答；旧实现把它记成域名失败，连续 3 个就把其余全部跳过，而提交顺序固定 ⇒
    尾部源永远轮不到（实测 51 个"当场手动抓完全正常"的源被尝试 0.00 次/场 × 18 场）。
    """
    res = offline_main(lambda src: [], n=40, delay=0.02)
    assert sorted(res.calls) == sorted("b%d" % i for i in range(40)), \
        "同域空 feed 引发连坐，只抓到 %d 个" % len(res.calls)
    assert not res.skipped, "被域熔断跳过了 %d 个源" % len(res.skipped)


def test_hard_domain_failures_still_trip_the_breaker(offline_main):
    """保护不能一起丢掉：整域都在硬失败（桥接服务挂了 / 在限流我们）时，
    后面的同域源必须被跳过，别把对方打穿。

    只断言"域内部分失败"是断不出来的：完成顺序不是提交顺序，任何一次成功都会把
    连续失败计数归零 —— 那种混合场景本来就不该熔断（域名在正常应答）。
    """
    def handler(src):
        return None

    res = offline_main(handler, n=40, delay=0.05)
    assert res.skipped, "整域全失败也没有触发熔断：40 个请求全发出去了"
    assert len(res.calls) < 40, "被跳过的源仍发起了请求（%d 个）" % len(res.calls)


def test_upstream_failure_and_legit_empty_are_told_apart_in_log(offline_main):
    """逐源日志必须区分"上游失败"与"上游正常但 0 条"。

    两者都记成 empty 的话，看日志的人无从判断该换源还是该修代码 —— 2026-09-16 起
    84 个源被静默吞成 0 条、三天没人发现，缺的就是这个区分。
    """
    def handler(src):
        if src["key"] == "b0":
            return None                      # 上游失败
        if src["key"] == "b1":
            return []                        # 合法空 feed
        return _item(src["key"])

    res = offline_main(handler)
    st = {k: v.get("status") for k, v in res.per_source.items()}
    assert st["b0"] == "error", "上游失败仍被记成 empty：%s" % st
    assert st["b1"] == "empty", "合法空 feed 与失败混为一谈：%s" % st


def test_worker_crash_is_reported_not_swallowed(monkeypatch, offline_main):
    """抓取链路内部异常必须留在日志里 —— 静默吞异常是这次查了一天才定位的原因。"""
    buf = io.StringIO()
    monkeypatch.setattr(mod.sys, "stderr", buf)

    def handler(src):
        raise TypeError("boom in dedup")

    offline_main(handler)
    err = buf.getvalue()
    assert "TypeError" in err and "Bridge0" in err, \
        "worker 异常被无声吞掉，日志里只剩『0 条』：%r" % err[:300]


def test_guid_only_item_keeps_its_permalink():
    """条目没有 <link>、永久链接只在 <guid> 里时，必须认 guid，不能整条丢掉。

    这不是假想：安全客 _664 的 feed（api.anquanke.com/data/v1/rss）实测就是这种形状 ——
    raw 里 20 个 <item>，我方解析 0 条，日志把它记成 empty，看上去像"上游没内容"，
    于是 2026-09-21 那份失效分析把它单独归到 PARSER 类并明确"不许删源"。
    原形状照抄（含它那个裸文本节点与无时区的 pubDate 写法）：
      <item><guid>https://…/post/id/316124</guid><title>…</title>
            <author> 安全客</author><description></description>subject
            <source></source><pubDate>2026-09-19 10:57:40</pubDate></item>
    旧实现在 link 为空时直接 return ⇒ 任何"guid 即链接"的源都整源归零。
    """
    import xml.etree.ElementTree as ET
    xml = (u'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
           u'<item><guid>https://www.anquanke.com/post/id/316124</guid>'
           u'<title>3000 美元、3 个人、72 小时</title><author> 安全客</author>'
           u'<description></description>subject<source></source>'
           u'<pubDate>2026-09-19 10:57:40</pubDate></item></channel></rss>')
    out = []
    mod._parse_rss_item(ET.fromstring(xml)[0][0], u"安全客", u"安全客_664", "security", out)
    assert len(out) == 1, "guid-only 条目被丢掉了（解析 %d 条）" % len(out)
    assert out[0]["link"] == "https://www.anquanke.com/post/id/316124", \
        "link 没取到 guid：%r" % out[0]["link"]
    assert out[0]["pub_date"] is not None, "无时区的 'YYYY-MM-DD HH:MM:SS' 写法没解析出日期"


def test_non_url_guid_does_not_become_a_link():
    """防修过头：guid 不是 URL 时不许当链接用，否则点出去就是死链。

    RSS 允许 guid 是任意稳定 ID（`isPermaLink="false"` 或干脆是数字/哈希）。
    无条件把 guid 当 link 会把「没有链接」变成「有一个错链接」—— 比空链接更坏，
    因为卡片看起来能点，点开是 404 或站内乱跳。
    """
    import xml.etree.ElementTree as ET
    for guid, why in ((u"12345", "纯数字 ID"),
                      (u"anquanke-post-abc", "非 URL 字符串"),
                      (u'isPermaLink="false" 的 URL', "显式声明不是永久链接")):
        if guid == u'isPermaLink="false" 的 URL':
            xml = (u'<rss version="2.0"><channel><item>'
                   u'<guid isPermaLink="false">https://www.anquanke.com/post/id/1</guid>'
                   u'<title>t</title></item></channel></rss>')
        else:
            xml = (u'<rss version="2.0"><channel><item><guid>' + guid +
                   u'</guid><title>t</title></item></channel></rss>')
        out = []
        mod._parse_rss_item(ET.fromstring(xml)[0][0], u"S", "s_1", "security", out)
        assert not out or (out[0].get("link") or "").startswith("http"), \
            "%s 被当成链接用了：%r" % (why, out[:1])

import urllib.error as _uerr


class RateLimitedTest(Exception):
    pass


def _rl(key="429 source"):
    """构造一个与生产同形的限流异常：_fetch_rss 将把 429 折成这个类型抛出。"""
    return _uerr.HTTPError("http://bridge.test/feed0", 429, "Too Many Requests", {}, None)


def test_429_is_its_own_status_not_error(offline_main):
    """限流必须是第四种状态。

    混进 error 的代价现成就有一份：域熔断按"连续 3 次硬失败"触发，而 429 被算硬失败，
    于是同域其余源被连坐（我们明知是限速却按源坏处理），并且在被连坐之前那几个请求
    还在继续锤限流器 —— 既扩大伤害又加深伤害（台账 §21）。
    """
    def handler(src):
        if src["key"] == "b0":
            raise _rl()
        return _item(src["key"])

    res = offline_main(handler, n=6, delay=0.02)
    st = {k: v.get("status") for k, v in res.per_source.items()}
    assert st.get("b0") == "rate_limited", "429 没被单独定性：%s" % st


def test_429_cooldown_stops_hammering_same_domain(offline_main):
    """命中 429 之后，同域剩余源本场不得再发请求。"""
    def handler(src):
        if src["key"] in ("b0", "b1"):
            raise _rl()
        return _item(src["key"])

    res = offline_main(handler, n=6, delay=0.02)
    after = [k for k in res.calls if k not in ("b0", "b1")]
    assert not after, "同域已限流，还在继续锤：%s" % after
    st = {k: v.get("status") for k, v in res.per_source.items()}
    assert st.get("b2") == "rate_limited" and st.get("b3") == "rate_limited", \
        "被冷却的源状态没标成 rate_limited：%s" % st


def test_403_and_timeout_must_not_enter_the_cooldown(offline_main):
    """反向判据（必须绿）：只有 429 触发冷却。

    403/404/500 那类是真死源（wechat2rss 五源、CISA、AP News 都是），
    若也进这个分支，日志会把"永久坏"说成"限流，等会儿再来"，
    于是既不会删源也不会换源 —— 状态语义一旦被污染，可判决性就没了。
    """
    def handler(src):
        if src["key"] == "b0":
            raise _uerr.HTTPError("http://bridge.test/feed0", 403, "Forbidden", {}, None)
        return _item(src["key"])

    res = offline_main(handler, n=6, delay=0.02)
    st = {k: v.get("status") for k, v in res.per_source.items()}
    assert st.get("b0") == "error", "403 被当成限流：%s" % st
    assert "b4" in res.calls, "403 触发了域冷却，同域其他源不再被抓：%s" % res.calls


def test_single_source_domain_keeps_today_behaviour(offline_main):
    """反向判据：单源域没有"兄弟源"可保护，行为与今天一致（记 rate_limited，不引入跳过逻辑）。"""
    def handler(src):
        raise _rl()

    res = offline_main(handler, n=1, delay=0)
    st = {k: v.get("status") for k, v in res.per_source.items()}
    assert st.get("b0") == "rate_limited", "单源域的 429 定性不稳定：%s" % st
    assert res.calls == ["b0"], "单源域多抓了一次：%s" % res.calls
