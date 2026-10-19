# tests/rss_source_coverage/test_fetch_size.py
# -*- coding: utf-8 -*-
"""抓取体积上限：5MB 的 read 上限会把大 feed 切在 CDATA 中间，导致整源解析失败。

Why: 语言即世界(10.5MB)、zartbot(7.8MB)、Invest Like the Best(7.4MB)、点拾投资(5.5MB)
四个源上游健康、全量能正常解析出条目，但 `_fetch_url` 用 `r.read(5000000)` 截断后
CDATA 的开/闭数量不相等，ET 报 "unclosed CDATA section" → 该源 0 条。日志里它表现为
"解析失败"，看起来像上游 XML 写坏了，实际是我们自己切的。
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from _loader import load_build  # noqa: E402

mod = load_build()

MB = 1024 * 1024


class _FakeResp:
    """替身：按标准库语义实现 read(n)——n 为 None 时读到结束。"""

    def __init__(self, payload):
        self._payload = payload
        self._pos = 0

    def read(self, n=None):
        if n is None:
            chunk = self._payload[self._pos:]
            self._pos = len(self._payload)
        else:
            chunk = self._payload[self._pos:self._pos + n]
            self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fetch_with(payload_bytes):
    orig = urllib.request.urlopen

    def fake(req, timeout=None):
        return _FakeResp(payload_bytes)
    mod.urllib.request.urlopen = fake
    try:
        return mod._fetch_url("http://example.test/feed")
    finally:
        mod.urllib.request.urlopen = orig


def test_fetch_url_returns_whole_body_for_large_feed():
    """8MB 的 feed 必须完整返回，不能被静默截成 5MB。"""
    payload = b"<rss><channel>" + b"x" * (8 * MB) + b"</channel></rss>"
    got = _fetch_with(payload)
    assert len(got.encode("utf-8", "replace")) == len(payload), \
        "响应被截断：拿到 %d 字节，上游有 %d 字节" % (
            len(got.encode("utf-8", "replace")), len(payload))


def test_cap_clears_the_largest_feed_we_know_is_live():
    """上限必须高于已实测到的最大存活 feed（语言即世界 10,484,071 字节）。

    只测"不截断 8MB"守不住常数本身：把 MAX_FEED_BYTES 调成 6MB 上面那条照样过，
    而这个源会立刻回到整源 0 条 —— 代价是悬崖式的，不是丢尾巴。
    抬上限的约束是内存（见 build_rss_aggregator.py 里 MAX_FEED_BYTES 的注释），
    真要改数值，连同那条注释一起改。
    """
    assert mod.MAX_FEED_BYTES > 10484071, \
        "上限 %d 已低于实测最大存活 feed 10.5MB，会重新造成截断归零" % mod.MAX_FEED_BYTES
