# -*- coding: utf-8 -*-
"""派生字段必须真被剥掉（Task 1 的出厂侧判据）。

为什么单独立一条：常量 `ITEM_DERIVED_FIELDS` 与 `_strip_derived_items()` 都是"定义了
不等于生效"的东西——历史上「装配处不再写」和「出厂处按常量剥离」这两侧只要漏一侧，
产物里就还有 time_str，每场照样全量重写，而任何单元测试都不会响。
所以这条直接跑真实出厂函数，读**产物字节**：不许出现 time_str。

判据同时守住另一头：剥字段不许把条目剥丢、不许改动内存里的历史对象。

跑法：py -3.11 -m pytest tests/rss_history/test_item_field_stability.py -q -s
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

B = load_build()


def _items(n, t0_iso):
    return [{"title": "t%d" % i, "link": "https://e/%d" % i, "pub_date": t0_iso,
             "summary": "s", "full_content": "fc", "time_str": "3小时前",
             "cat": "news", "color": "#123"} for i in range(n)]


def _corpus(now_iso="2026-09-30T10:00:00+08:00"):
    """三源：s1/s2 各 200 条新（会一起进首屏 360），s3 全 200 条**陈到出不了首屏**。

    s3 是刻意造的：`_split_data_chunks` 有三条出厂通道 —— 首屏 c0、同源剩余 rest、
    以及"整源都不在首屏"的 elif 分支。只有两源时第三条永不执行，那条通道的剥离
    就没被测过（第一版语料正是这个形状）。
    """
    old_iso = "2026-09-29T05:00:00+08:00"
    return [{"key": "s1", "name": "S1", "cat": "news", "color": "#111",
             "tier": 2, "url": "https://e/s1", "items": _items(200, now_iso)},
            {"key": "s2", "name": "S2", "cat": "news", "color": "#222",
             "tier": 3, "url": "https://example.com/s2", "items": _items(200, now_iso)},
            {"key": "s3", "name": "S3", "cat": "news", "color": "#333",
             "tier": 3, "url": "https://e/s3", "items": _items(200, old_iso)}]


def _write(tmp_path, corpus):
    saved = B.OUT
    try:
        B.OUT = os.path.join(str(tmp_path), "rss-aggregator.html")
        B.write_data_chunks(corpus)
    finally:
        B.OUT = saved
    return {f: (tmp_path / f).read_text(encoding="utf-8")
            for f in sorted(os.listdir(str(tmp_path))) if f.startswith("rss-data-")}


def test_shipped_chunks_carry_no_time_str(tmp_path):
    """产物字节里不许出现 time_str —— 这才是"移出去了"的事实。"""
    files = _write(tmp_path, _corpus())
    assert files, "根本没写出分块"
    for name, text in files.items():
        assert "time_str" not in text, "%s 里还有 time_str，派生字段没真移出" % name


def test_strip_does_not_lose_items_or_fields(tmp_path):
    """剥派生字段不许顺手丢条目、丢事实字段。"""
    corpus = _corpus()
    want = sum(len(s["items"]) for s in corpus)
    files = _write(tmp_path, corpus)
    got = 0
    for text in files.values():
        i = text.find("{")
        j = text.rstrip().rstrip(";").rfind("}")
        pack = json.loads(text[i:j + 1])
        for s in pack["sources"]:
            for it in s.get("items", []):
                got += 1
                assert "pub_date" in it and "link" in it and "full_content" in it, it
                assert "cat" in it and "color" in it, "源级字段是条目自带的，本轮没授权删：%s" % sorted(it)
    assert got == want, "条目数对不上：出厂 %d / 输入 %d" % (got, want)


def test_history_objects_are_not_mutated_by_stripping():
    """剥离必须发生在副本上；改了内存历史等于把别处的字段也抹了。"""
    src = _corpus()
    c0, c1 = B._split_data_chunks(src, chunk0_size=10)
    assert c0 and c1, "分块为空，等于没测到两条通道"
    for s in src:
        for it in s["items"]:
            assert "time_str" in it, "入参被就地改写了（历史条目本该保留自己的键）"


def test_all_three_factory_channels_are_stripped(tmp_path):
    """三条出厂通道逐一验到：首屏 c0、同源剩余 rest、整源出不了首屏的 elif。

    为什么按源验而不是数调用点：`count("_strip_derived_items(")` 会把 `def` 行本身
    算进去（实测 1 def + 3 调用 = 4），所以 `>= 3` 掉一个调用点仍然绿。行为判据才
    真的咬得住：哪条通道漏剥，那个源的条目就会带着 time_str 出现在产物里。
    """
    files = _write(tmp_path, _corpus())
    seen_keys = set()
    for text in files.values():
        i = text.find("{")
        j = text.rstrip().rstrip(";").rfind("}")
        for s in json.loads(text[i:j + 1])["sources"]:
            seen_keys.add(s["key"])
            for it in s.get("items", []):
                assert "time_str" not in it, "%s 通道的条目没剥净" % s["key"]
    assert {"s1", "s2", "s3"} <= seen_keys, "有源没进产物，等于该通道没被测到：%s" % seen_keys


def test_strip_helper_is_actually_wired():
    """静态兜底：调用点数必须等于"减一条就红"的阈值，且含 def 行本身。"""
    stripped = B._strip_derived_items([{"link": "x", "time_str": "3小时前", "title": "t"}])
    assert stripped == [{"link": "x", "title": "t"}], stripped
    assert isinstance(B.ITEM_DERIVED_FIELDS, frozenset) and "time_str" in B.ITEM_DERIVED_FIELDS
    text = open(os.path.join(ROOT, "build_rss_aggregator.py"), encoding="utf-8").read()
    # 1 个 def + 3 条出厂通道；少任一通道即红（审查实测：`>= 3` 会漏掉一种断线形态）
    assert text.count("_strip_derived_items(") == 4, \
        "剥离点数量变了（现 %d，应为 1 def + 3 通道）" % text.count("_strip_derived_items(")
