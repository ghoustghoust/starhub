# -*- coding: utf-8 -*-
"""运行时翻译诊断必须随页面出厂（2026-09-27）。

摘要翻译自 2026-09-16 起移出构建期，"到底翻没翻"只有浏览器知道：
构建日志里 `trans_*` 那 8 个字段全是构建期的账，运行时那半边是黑的。
本次就是靠 `?transdiag=1` 的面板读数定死结论的 —— 线上 8650 条里
"补翻已尝试 920 / 未尝试 7730"，两条通道 gtx 200/169ms、vercel bulk 200/1108ms，
于是排除了"被墙"和"代码坏"，指向每轮 10 篇 / 2.5 秒的吞吐。

判据打在**生成物**上（模板里有不等于出厂有），并且反向要求暴露名：
只写函数不挂 `window`，IIFE 里外面调不到（本仓踩过），那这个诊断等于没有。
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _page_js():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import build_rss_aggregator as M
    srcs = [{"key": "k1", "name": "N", "cat": "科技", "color": "#f00", "items": []}]
    js = M._build_js(srcs)
    joined = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", js, re.S))
    assert joined, "生成物里找不到 <script> 段，探针失效"
    return joined


def test_translation_diagnostic_ships_and_is_callable():
    js = _page_js()
    assert "function _transDiag" in js, "运行时翻译诊断函数没进出厂页面"
    assert "window.transDiag" in js, (
        "诊断函数没挂到 window：它在 IIFE 里，console 调不到，等于没交付观测面")
    assert "transdiag=1" in js, "少了 ?transdiag=1 自动触发入口"


def test_diagnostic_reports_both_channels_and_backlog():
    """面板必须同时报"积压多少没试过"和"两条通道通不通"，缺一样就定不了位。"""
    js = _page_js()
    for token in ("_zhTried", "gtx_direct", "vercel_bulk"):
        assert token in js, "诊断里丢了 %s：只剩计数或只剩通道，都不够判因" % token
