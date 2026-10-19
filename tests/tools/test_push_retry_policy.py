# -*- coding: utf-8 -*-
"""`req()` 不许把 404 当瞬时故障重试（判据先红）。

实测取证（`.deploy-tmp/probe_404_retry.py`，2026-10-02）：对一条**已删除**的路径发一次
`tries=2` 的 contents GET，耗时 10.6s，stderr 里两行「重试 N/2 … HTTP Error 404」。
默认 `tries=6` 时每条要 sleep 3+6+9+12+15 = 45s。本轮 `--delete` 147 条，`verify(gone=…)`
正是靠 404 来证明"删干净了"⇒ 光复查就要烧约 110 分钟，而这 147 个 404 全是**期望答案**。

为什么要单独钉住：4xx 里 404 是唯一"语义正确"的失败，它不该进重试循环；
5xx/网络断裂仍然必须重试（该端点频繁 TLS EOF，半程失败只留孤儿 blob 而不动 ref）。
所以判据是双向的：404 一次都不重试，5xx 仍按 tries 重试 —— 只钉一边会变成"关掉整个重试"。
"""
import importlib.util
import io
import os
import sys
from unittest import mock
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")


class _FakeTime:
    def __init__(self):
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)


class _FakeSubprocess:
    @staticmethod
    def check_output(*a, **k):
        return b"fake-token\n"


def _load():
    spec = importlib.util.spec_from_file_location("dap_retry", os.path.join(TOOLS, "data_api_push.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["dap_retry"] = m
    spec.loader.exec_module(m)
    m.time = _FakeTime()          # 只替换这份模块副本里的引用，不污染全局 time
    m.subprocess = _FakeSubprocess()
    return m


def _httperror(code):
    return HTTPError("https://api.github.com/x", code, "boom", {}, io.BytesIO(b"{}"))


def test_404_is_raised_without_a_single_retry():
    m = _load()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise _httperror(404)

    with mock.patch("urllib.request.urlopen", boom):
        try:
            m.req("GET", "repos/x/contents/y")
            raise AssertionError("404 应当上抛，让 verify 认成「已删除」")
        except HTTPError as e:
            assert e.code == 404
    assert len(calls) == 1, "404 被重试了 %d 次 ⇒ --delete 的删除复查每条要等 45s" % len(calls)
    assert m.time.slept == [], "404 之后还在 sleep：%s" % (m.time.slept,)


def test_transient_5xx_still_retries():
    """反向控制：不能为了快把真重试一起关掉。"""
    m = _load()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise _httperror(502)

    with mock.patch("urllib.request.urlopen", boom):
        try:
            m.req("GET", "repos/x/contents/y", tries=3)
        except HTTPError:
            pass
    assert len(calls) == 3, "5xx 只试了 %d 次（该重试 3 次）" % len(calls)
    assert len(m.time.slept) == 3, "退避 sleep 次数对不上：%s" % (m.time.slept,)


def test_it_recovers_when_a_retry_succeeds():
    m = _load()
    seq = iter([_httperror(503), {"sha": "deadbeef"}])

    def maybe(*a, **k):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        class R:
            def read(self_):
                import json
                return json.dumps(item).encode()
            def __enter__(self_):
                return self_
            def __exit__(self_, *a2):
                return False
        return R()

    with mock.patch("urllib.request.urlopen", maybe):
        assert m.req("GET", "repos/x/contents/y") == {"sha": "deadbeef"}
    assert len(m.time.slept) == 1, "退避只应发生在真失败那一次：%s" % (m.time.slept,)
