# tests/rss_source_coverage/test_hot_title_type.py
# -*- coding: utf-8 -*-
"""热榜标题类型：上游给 JSON number 时必须在入口收窄成 str。

Why: 2026-10-14 11:00/12:00/13:00 三场连续红。pcbeta 有条帖子标题就是裸的 Windows
版本号 `26300.9711`，上游序列化时不带引号 ⇒ Python json 解成 float ⇒ 热榜又刻意不过
`_filter_noise`，于是同一个 float 咬穿两处按 str 拼接的代码：
`build_daily_insight.py:125` `_classify_topic`（洞察整条流水线崩 ⇒
`daily-insight-history.html` 缺件 ⇒ Pages 不发、run 红）与
`build_rss_aggregator.py:8912` `_cross_platform_topics`（被 except 吞成一行无
`::error::` 的 stderr，整块智能分析静默变 None）。10:00 那场同一位置是
`"26300.9711来了"`（带中文后缀⇒str），所以这不是代码回归，是外部数据形状。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from _loader import load_build  # noqa: E402

mod = load_build()


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {"Content-Type": "application/json; charset=utf-8"}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _payload(items):
    return json.dumps({"status": "success", "items": items}).encode("utf-8")


def _run_fetch(payload_by_platform):
    """只喂两个平台跑真实函数，返回 fetch_newsnow_snapshot 的产物（不触网）。"""
    orig_urlopen = mod.urllib.request.urlopen
    orig_plats = mod.NEWSNOW_PLATFORMS

    def fake(req, timeout=None):
        plat = str(req.full_url).rsplit("id=", 1)[-1]
        return _Resp(payload_by_platform[plat])

    mod.urllib.request.urlopen = fake
    mod.NEWSNOW_PLATFORMS = list(payload_by_platform)
    try:
        return mod.fetch_newsnow_snapshot()
    finally:
        mod.urllib.request.urlopen = orig_urlopen
        mod.NEWSNOW_PLATFORMS = orig_plats


def test_numeric_hot_title_is_coerced_to_str():
    """数字型标题必须变成 str，且不丢原文（float 的 str() 恰好还是 "26300.9711"）。"""
    snapshot = _run_fetch({
        "pcbeta": _payload([
            {"title": "win11 26h2 讨论", "url": "https://bbs.pcbeta.com/a"},
            {"title": 26300.9711, "url": "https://bbs.pcbeta.com/b"},
            {"title": 2026, "url": "https://bbs.pcbeta.com/c"},
        ]),
        "weibo": _payload([{"title": "26300.9711来了", "url": "https://weibo/x"}]),
    })

    # 反向判据：替身确实产出了条目，否则下面的类型断言是空集假通过
    assert len(snapshot) == 2, "假响应没产出平台，本测试无从判定：%r" % snapshot
    items = [it for pf in snapshot for it in pf["items"]]
    assert len(items) == 4, "条目数不对，判据会空转：%d" % len(items)

    bad = [(type(it["title"]).__name__, it["title"]) for it in items
           if not isinstance(it["title"], str)]
    assert not bad, "热榜标题没收成 str，build_daily_insight.py:125 与 " \
                    "build_rss_aggregator.py:8912 还会 TypeError：%r" % bad
    assert "26300.9711" in [it["title"] for it in items], \
        "收窄时把原文弄丢了：%r" % [it["title"] for it in items]


def test_falsy_hot_title_keeps_pre_fix_shape():
    """None/0/0.0/"" 不许被 str() 成 "None"/"0.0" 这类真值——那会改变下游条目取舍。"""
    snapshot = _run_fetch({
        "pcbeta": _payload([
            {"title": None, "url": "u1"},
            {"title": 0, "url": "u2"},
            {"title": 0.0, "url": "u3"},
            {"title": "", "url": "u4"},
            {"url": "u5"},
            {"title": "真标题", "url": "u6"},
        ]),
        "weibo": _payload([{"title": "另一平台", "url": "u7"}]),
    })
    items = [it for pf in snapshot for it in pf["items"]]
    assert len(items) == 7, "条目数不对，判据会空转：%d" % len(items)

    falsy = sorted([it["title"] for it in items if not it["title"]], key=repr)
    # 与改动前逐字节同形：None 仍是 None、0 仍是 0，缺失仍是 ""
    assert [repr(t) for t in falsy] == ["''", "''", "0", "0.0", "None"], \
        "falsy 标题被改写，条目取舍会和改动前不一致：%r" % [repr(t) for t in falsy]
