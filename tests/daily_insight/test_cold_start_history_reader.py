# -*- coding: utf-8 -*-
"""`build_daily_insight` 的冷启动读取端（放在 gate B 而不是 A2：它是 blocking 闸）。

A2 的既有依赖只到 `build_rss_aggregator`（顶层拉 insight_engine ⇒ llama_index，
`test_trans_diag_ships.py:23`、`test_dfb_drawer_map_wire.py:183` 早就这么用了）。
但 `build_daily_insight` 只有 advisory 的 gate B 碰过它 —— 把读它的判据塞进 A2，
等于让"洞察依赖装不上"这种环境问题有能力冻住整场构建与两次部署，
而这条判据管的是跨场缓存冷启动，不是构建正确性。 ⇒ 放这里：每场都跑，只在 gate B 出声。
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _bdi():
    import build_daily_insight as m
    return m


@pytest.fixture
def empty_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_insight_history_missing_and_corrupt_degrade_to_empty_days(empty_workdir):
    bdi = _bdi()
    assert bdi._load_history() == {"days": []}, "daily_insight_history.json 缺失应退化成空历史"
    (empty_workdir / bdi.HISTORY_FILE).write_text("{oops", encoding="utf-8")
    assert bdi._load_history() == {"days": []}, "损坏的洞察历史必须被吞成空历史而不是抛出"
    (empty_workdir / bdi.HISTORY_FILE).write_text(json.dumps({"days": [{"d": 1}]}), encoding="utf-8")
    assert bdi._load_history() == {"days": [{"d": 1}]}, "正常路径必须读回来，否则前两条是假绿"


def test_tracking_file_constant_is_relative_and_shared_with_cache(empty_workdir):
    """`TRACKING_FILE` 必须是根目录相对名 —— 缓存 path 写的就是这个名字。

    两边形状一旦不一致（比如有人把它挪进子目录），缓存命中也不再会把文件放回报读点，
    而且不会有任何一处报错：追踪日志会每场从空开始。
    """
    bdi = _bdi()
    assert bdi.TRACKING_FILE and not os.path.isabs(bdi.TRACKING_FILE)
    assert "/" not in bdi.TRACKING_FILE and "\\" not in bdi.TRACKING_FILE, bdi.TRACKING_FILE
    wf = open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8").read()
    assert ("\n            %s\n" % bdi.TRACKING_FILE) in wf, (
        "%s 不在缓存 path 名单里 ⇒ 它既不进 git（批 3 已摘）也没人跨场带它" % bdi.TRACKING_FILE)
