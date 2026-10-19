# -*- coding: utf-8 -*-
"""冷启动演练（计划 §3）：状态文件不在盘上时，读取端必须"退化成空"而不是抛或静默当正常。

为什么不用"本地跑一次 fetch_and_build.py"来做这件事：本地跑会挂住烧翻译配额、还会写出脏的跟踪产物
（见 gate-b-no-local-run）。所以这里直接调真实读取函数，把 cwd 换到一个临时空目录 ——
验的是生产代码里那几条缺失分支本身，不是 grep 源码。

批 5a 之后这条判据有了具体痛感：那次缓存族被 path 变更整体失效，6 个状态文件当场 MISSING。
如果读取端在这种情况下会抛，构建就红在 20 分钟之后（而 A2 早已放行）—— 那正是最坏的发现时机。
"""
import ast
import json
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _bra():
    """按本目录既有约定在函数内导入。

    `build_rss_aggregator` 顶层要拉 `insight_engine` ⇒ `llama_index`；A2 是 blocking 闸，
    模块级导入若因依赖缺失崩在 collection，会把整步判据一起带走（本目录今早就冻过一次）。
    """
    import build_rss_aggregator as m
    return m


@pytest.fixture
def empty_workdir(tmp_path, monkeypatch):
    """把 cwd 挪到一个什么都没有的目录：模块里的 *_FILE 都是相对路径 ⇒ 这就是"缓存没还原"的世界。

    顺带把 `bra._trans_cache` 这个模块级全局在测试后还原：本文件的多条判据就是拿它当被观测对象的，
    不还原就会让"哪条先跑"影响结果 —— 变异电池里已经出现过一次这种串味（K4 把不相干的翻译判据带红）。
    """
    bra = _bra()
    saved = bra._trans_cache
    monkeypatch.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        bra._trans_cache = saved


def test_cache_paths_are_relative_to_workdir_root():
    """缓存 path 名单写的是根目录文件名；读取端必须用同一个形状，否则"缓存命中≠文件在盘上"。"""
    bra = _bra()
    constants = [("bra.TRANS_CACHE_FILE", bra.TRANS_CACHE_FILE),
                 ("bra.HOT_HISTORY_FILE", bra.HOT_HISTORY_FILE),
                 ("bra.ANALYSIS_SNAPSHOT_FILE", bra.ANALYSIS_SNAPSHOT_FILE),
                 ("bra.RSS_TREND_HISTORY_FILE", bra.RSS_TREND_HISTORY_FILE),
                 ("bra.TRENDING_SNAPSHOT_FILE", bra.TRENDING_SNAPSHOT_FILE)]
    bad = [n for n, v in constants if os.path.isabs(v) or "/" in v or "\\" in v]
    assert not bad, (
        "这些状态常量不是工作目录根的相对路径，缓存与读取端会各读各的：%s" % bad)
    names = {v for _, v in constants}
    assert len(names) == len(constants), (
        "读取端常量撞名（%d 个不同值 / %d 条），这条判据在退化集合上跑" % (len(names), len(constants)))


def test_translation_cache_missing_is_a_noop_not_a_crash(empty_workdir):
    """实测契约：`_load_caches()` 在文件缺失时什么都不做（不抛，也不主动清空）。"""
    bra = _bra()
    bra._trans_cache = {"stale": "sentinel"}
    bra._load_caches()
    assert bra._trans_cache == {"stale": "sentinel"}, (
        "缺失分支的行为变了：它现在会覆写全局，那 test_module_initial_cache_is_empty 的前提要一起改")
    (empty_workdir / bra.TRANS_CACHE_FILE).write_text("{not json", encoding="utf-8")
    bra._trans_cache = {}
    bra._load_caches()                      # 半截文件必须被 except 吞掉
    assert bra._trans_cache == {}


def test_module_initial_cache_is_empty():
    """冷启动那场的"空缓存"来自模块初始值而不是读取函数清空 ⇒ 初始值必须是空 dict。

    若有人把它初始化成 None，缺失分支的"什么都不做"就变成隐性炸点：首场拿那个值当已有译文用，
    而命中判断靠 `in`，None 会当场抛在 20 分钟之后。
    """
    tree = ast.parse(open(os.path.join(ROOT, "build_rss_aggregator.py"), encoding="utf-8").read())
    init = [node.value for node in tree.body
            if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name) and t.id == "_trans_cache"]
    assert init, "顶层找不到 `_trans_cache = ...` 的初始化，它现在从哪来的？"
    val = init[0]
    assert isinstance(val, ast.Dict) and len(val.keys) == 0, (
        "翻译缓存的模块初始值必须是空 dict（冷启动靠它），实测=%s" % ast.dump(val)[:80])


def test_analysis_snapshot_missing_and_corrupt_return_none(empty_workdir):
    bra = _bra()
    assert bra._load_prev_analysis() is None, (
        "analysis_snapshot.json 缺失必须返回 None，调用端靠它判冷启动")
    (empty_workdir / bra.ANALYSIS_SNAPSHOT_FILE).write_text("[]", encoding="utf-8")
    assert bra._load_prev_analysis() is None, "类型不对（list）也要退化成 None，不给调用端意外形状"
    (empty_workdir / bra.ANALYSIS_SNAPSHOT_FILE).write_text('{"k": 1}', encoding="utf-8")
    assert bra._load_prev_analysis() == {"k": 1}, (
        "有正常文件时必须真读回来，否则上面两条是在空转上判绿")


def test_hot_history_accumulator_survives_missing_file(empty_workdir):
    """`_accumulate_hot_history` 是 append-only 历史的唯一入口：冷启动时它必须能自己起步。"""
    bra = _bra()
    snap = [{"platform": "weibo", "name": "微博",
             "items": [{"rank": 1, "title": "t", "url": "u", "hot": 1}]}]
    bra._accumulate_hot_history(snap, datetime.now(timezone.utc))
    assert os.path.exists(bra.HOT_HISTORY_FILE), "冷启动起步后必须落盘，否则历史永远积不起来"
    data = json.load(open(bra.HOT_HISTORY_FILE, encoding="utf-8"))
    assert isinstance(data, dict) and data, "起步后的历史文件必须是非空 dict"
