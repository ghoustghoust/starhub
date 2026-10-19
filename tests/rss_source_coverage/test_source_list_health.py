# tests/rss_source_coverage/test_source_list_health.py
# -*- coding: utf-8 -*-
"""源清单基线守卫： rss_sources.json 是手工维护的单点，一次误写就能让站点静默掉几百个源。

Why: 本轮为了修覆盖率重写了整个清单（1005 → 985），核对改动面只能靠脚本比对；
且 wechat2rss / xgo.ing / rsshub.bestblogs 三个桥接服务承载了 606/985 个源，
任何一次批量误删都不会报错、只会让"有数据源"慢慢往下掉（正是这次要查的那个现象）。
"""
import collections
import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 与 _loader.py 的 RSS_BUILD_SRC 同一手法：变异体检查要拿一份被截断的清单喂进来，
# 证明这些断言真会红，而不是恒绿的摆设。
PATH = os.environ.get("RSS_SOURCES_SRC") or os.path.join(ROOT, "rss_sources.json")


def _sources():
    return json.load(io.open(PATH, encoding="utf-8"))


def test_total_source_count_does_not_collapse():
    n = len(_sources())
    assert n >= 950, "源清单只剩 %d 个，疑似被批量误删" % n


def test_bridge_host_groups_do_not_collapse():
    """三个第三方桥接是清单的主体，哪个整组消失都说明出事，而不是"源坏了"。"""
    urls = [s["url"] for s in _sources()]
    groups = {
        "wechat2rss.bestblogs.dev": 300,
        "api.xgo.ing": 140,
        "rsshub.bestblogs.dev": 50,
    }
    for host, floor in groups.items():
        n = sum(1 for u in urls if host in u)
        assert n >= floor, "桥接组 %s 只剩 %d 个源（基线 ≥%d）" % (host, n, floor)


def test_no_duplicate_keys_or_urls():
    S = _sources()
    keys = collections.Counter(s["key"] for s in S)
    dupk = [k for k, n in keys.items() if n > 1]
    assert not dupk, "重复 key: %s" % dupk[:5]
    bare = collections.Counter(
        re.sub(r"^https?://", "", (s["url"] or "").lower().replace("www.", "", 1))
        and re.sub(r"^https?://", "", (s["url"] or "").lower().replace("www.", "", 1)).split("#")[0]
        for s in S)
    dupu = [u for u, n in bare.items() if n > 1]
    assert not dupu, "指向同一 feed 的重复源: %s" % dupu[:5]


def test_every_source_is_loadable_by_the_aggregator():
    """加载器会静默丢弃缺 key/name/url 的条目，这里提前挡住，别让线上少源无感。"""
    for s in _sources():
        assert isinstance(s, dict) and s.get("key") and s.get("name"), s
        assert (s.get("url") or "").startswith(("http://", "https://")), s
