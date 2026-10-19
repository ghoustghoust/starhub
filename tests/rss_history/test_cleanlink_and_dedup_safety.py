# tests/rss_history/test_cleanlink_and_dedup_safety.py
# -*- coding: utf-8 -*-
"""api/rss.js 的两处"数据消失"防线（对抗审查 2026-09-21 深夜，两条我自己复核过）。

判据都打在同一件事上：**修复动作本身不许把条目变没**。前一轮批
（58da6b9 清链接、8b83487 剥 fragment）就是为了修抽屉合并而动的，结果实测：

1. cleanLink 里 String.fromCodePoint 对超范围码点（`&#1114112;`）抛 RangeError。
   抛点在 parseFeed 内部，外面是 fetchOne 的 try ⇒ HTTP 200 + items=[]，
   批量出口连 _error 都不印。一条脏链接干掉整源，比原来的"链接畸形"严重一档。
   实测（_scratch/_verify_review.py）：base=764b319 parsed=11，cur parsed=抛错。
2. Pass 2 的空链接处理与 Python 不同：Python 是 `if link and link in seen_urls`
   （空链接**不去重也不删**），JS 是 `if (!link || seenUrls.has(link)) return false`
   （空链接整条删）。剥 fragment 把占位链接 `'#'` 变成 `''` 后，这条差异从
   "3 条并成 1 条"变成"3 条全没"。实测：base afterDedup=1，cur=0，Python=3。

所以第 2 条的正解不是回退剥 fragment（那会退回 spiegel 30/30 的旧缺陷），
而是把 JS 的 Pass 2 对齐到 Python 的语义 —— 判据也就按"两侧计数相等"来写。
"""
import html
import json
import os
import subprocess
import xml.etree.ElementTree as ET

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
import sys
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()
API_RSS = os.path.join(ROOT, "api", "rss.js")


def _node():
    for c in ("node", "node.exe"):
        try:
            r = subprocess.run([c, "-v"], capture_output=True)
        except Exception:
            continue
        if r.returncode == 0:
            return c
    return None


def _js_pipeline_src():
    """切出 extractTag..fetchOne 之间的纯函数（含 cleanLink / parseFeed / dedupSourceItems）。"""
    text = open(API_RSS, encoding="utf-8").read()
    a = text.find("function extractTag(")
    b = text.find("async function fetchOne(")
    assert 0 < a < b, "api/rss.js 里找不到 extractTag..fetchOne 区段，helper 结构变了"
    return text[a:b]


def _run_js(node, body):
    # COVER 必须先注进同一作用域：parseFeed 现在会调 COVER.pickItemImage，
    # 切片跑法没有它 ⇒ ReferenceError，判据会红在不存在的问题上。
    script = ("const COVER=require(%r);\n" % os.path.join(ROOT, "lib", "rss_cover.js")
              + "const s=%r;\neval(s);\n%s" % (_js_pipeline_src(), body))
    out = subprocess.run([node, "-e", script], capture_output=True)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")[:400]
    return json.loads(out.stdout.decode("utf-8"))


GOOD = "".join("<item><title>ok%d</title><link>https://a.test/%d</link>"
               "<pubDate>Tue, 01 Sep 2026 10:00:00 GMT</pubDate></item>" % (i, i)
               for i in range(5))
TAIL = "".join("<item><title>tail%d</title><link>https://a.test/t%d</link>"
               "<pubDate>Tue, 01 Sep 2026 11:00:00 GMT</pubDate></item>" % (i, i)
               for i in range(5))


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 的 ubuntu runner 一定有）")
def test_out_of_range_charref_does_not_erase_the_source():
    """一条带超范围字符引用的链接，不许让整源解析归零。

    取 `&#1114112;`（= 0x110000，正好越过 Unicode 上限 0x10FFFF）：
    String.fromCodePoint 对它必抛，而抛点在被 try 包住的 parseFeed 里 ——
    后果是静默 200 + 空数组，抽屉里那一整源"刷新没反应"。
    """
    node = _node()
    xml = ('<?xml version="1.0"?><rss><channel>%s'
           '<item><title>poison</title>'
           '<link>https://x.test/&#1114112;/y</link>'
           '<pubDate>Tue, 01 Sep 2026 12:00:00 GMT</pubDate></item>%s'
           '</channel></rss>') % (GOOD, TAIL)
    body = ("const items = parseFeed(%s, 'demo_src', 50);\n"
            "process.stdout.write(JSON.stringify([items.length, items.map(x=>x.link)]));\n"
            % json.dumps(xml))
    n, links = _run_js(node, body)
    assert n == 11, "整源只剩 %d 条（应为 11）：脏链接把别的条目一起带走了" % n
    # 解不了的码点**原样保留**。这里刻意不向 Python 的 html.unescape 对齐（它吐 '?'）：
    # 现测 Python 通道根本走不到那一步 —— `ET.fromstring` 对 `&#1114112;` 直接
    # ParseError("reference to invalid character number")，整篇 feed 一起失败。
    # 也就是说这条上 JS 比 Python 稳；把它改成吐 '?' 是照抄一条不在生产路径上的函数。
    assert links[5] == "https://x.test/&#1114112;/y", (
        "超范围码点被判成 %r：既不许抛错整源归零，也不许悄悄改写链接" % (links[5],))


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 的 ubuntu runner 一定有）")
def test_no_link_item_never_becomes_a_card_on_both_channels():
    """没有落点的条目两通道都不许出厂成卡片 —— 判据必须打在**决定产物的那一层**。

    这条判据写错过一次，错法值得留着：我先拿 `_dedup_source_items` 的
    `if link and link in seen_urls`（空链接不去重也不删）当"Python 的口径"，
    据此把 JS 的 Pass 2 改成保留空链接，测试也照这个中层函数断言 ⇒ 两边"一致"，
    但产物里根本不存在无链接卡片 —— 因为上游 `_parse_rss_item` 就有
    `if not title or not link: return`，批量出口再筛一道 `!it.u || it.u === '#'`。
    线上实测出厂分块 360/360 条 link 非空非 '#'。照错层改的后果是把死链卡片放进来
    （点不开的文章），正是本任务禁止的那一类。

    所以这里两端都跑**完整链路**：Python 用真 `_parse_rss_item`，
    JS 用真 `parseFeed → dedupSourceItems`，断言存活数同为 0。
    """
    node = _node()
    frags = ['<item><title>%s</title><pubDate>Tue, 01 Sep 2026 1%d:00:00 GMT</pubDate></item>' % (t, i)
             for i, t in enumerate(("A one", "B two", "C three"))]
    py_alive = []
    for frag in frags:
        out = []
        mod._parse_rss_item(ET.fromstring(frag), "S", "s_demo", "security", out)
        py_alive.append(out)
    assert sum(len(o) for o in py_alive) == 0, (
        "Python 出厂侧居然留住了无链接条目：%s —— 判据的参照层变了，先重读生产代码" % py_alive)

    xml = ('<?xml version="1.0"?><rss><channel>%s</channel></rss>') % "".join(frags)
    body = ("const items = parseFeed(%s, 's_demo', 50);\n"
            "process.stdout.write(JSON.stringify([items.length, dedupSourceItems(items.slice(), 's_demo').length]));\n"
            % json.dumps(xml))
    parsed, alive = _run_js(node, body)
    assert parsed == 3, "parseFeed 应当先解析出 3 条（含无链接的），实际 %d" % parsed
    assert alive == 0, (
        "JS 出厂侧留了 %d 条无链接卡片 —— 抽屉里点不开的死链，且与出厂口径不一致" % alive)

