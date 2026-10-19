# tests/rss_source_coverage/test_per_source_ua.py
# -*- coding: utf-8 -*-
"""按源覆盖 User-Agent：有些站点的 403 是按 UA 判的，不是按 IP。

Why: Engadget 用全局 Chrome UA 稳定 403（CloudFront "Request blocked"），
换 curl/8.5.0 立刻 200 + 20 条。没有 per-source 覆盖能力，这类源只能整条删掉，
而它们内容活着。
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from _loader import load_build  # noqa: E402

mod = load_build()

_seen = []


class _Resp:
    def read(self, n=None):
        return b"<rss><channel/></rss>"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_headers():
    orig = urllib.request.urlopen

    def fake(req, timeout=None):
        _seen.append(dict(req.headers))
        return _Resp()
    urllib.request.urlopen = fake
    return orig


def test_source_ua_overrides_global_default():
    """源里带 ua 字段时，请求必须用该 UA，而不是全局 Chrome 串。"""
    orig = _capture_headers()
    try:
        mod._fetch_url("https://www.engadget.com/rss.xml", ua="curl/8.5.0")
    finally:
        urllib.request.urlopen = orig
    sent = {k.lower(): v for k, v in _seen[-1].items()}
    assert sent.get("user-agent") == "curl/8.5.0", \
        "per-source UA 没生效，实际发出 %r" % sent.get("user-agent")


def test_default_ua_unchanged_when_source_has_none():
    """不传 ua 时必须沿用全局 UA，别把 1000 多个源的请求头一起改掉。"""
    orig = _capture_headers()
    try:
        mod._fetch_url("https://example.test/feed")
    finally:
        urllib.request.urlopen = orig
    sent = {k.lower(): v for k, v in _seen[-1].items()}
    assert sent.get("user-agent") == mod.UA["User-Agent"], \
        "默认 UA 被改动了：%r" % sent.get("user-agent")
