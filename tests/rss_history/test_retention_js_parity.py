# tests/rss_history/test_retention_js_parity.py
# -*- coding: utf-8 -*-
"""运行时闸门（lib/rss_retention.js）必须与构建期闸门（Python）判得一模一样。

为什么要有这条：72h 契约有两个执行点 —— 构建期写产物，运行时（api/rss.js 的 ?batch / ?source /
refresh）自己抓上游。规则一旦在两边各写一遍，早晚会出现"页面裁了 API 没裁"那种双标；
唯一能长期钉住它的办法是让两边吃同一份输入、逐条比判决，而不是靠人记得同步。

跑法：node 在 CI 的 ubuntu runner 上一定有（门禁 A 已经用 `node --check api/*.js`）。
"""
import datetime
import io
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

NODE_TEST = os.path.join(ROOT, "tests", "rss_js", "test_rss_retention.js")
LIB = os.path.join(ROOT, "lib", "rss_retention.js")
API_RSS = os.path.join(ROOT, "api", "rss.js")

NOW_TEXT = "2026-09-20T12:00:00+08:00"


def _hours(ms):
    return ms / 3600000.0


def iso(hours_ago, now=None):
    base = now or mod._parse_hist_dt(NOW_TEXT)
    return (base - datetime.timedelta(hours=hours_ago)).isoformat()


def _py_decisions(items, key, offsets, known):
    """用**生产代码**算期望值：窗口 + 逐条判龄 + 去留。"""
    now_bj = mod._parse_hist_dt(NOW_TEXT)
    mod._rss_history = {}
    for it in items:
        fs = known.get(it["u"])
        if fs is not None:
            mod._rss_history[it["u"]] = {"link": it["u"], "source_key": key,
                                         "pub_date": it.get("d") or "",
                                         "first_seen": fs}
    srcs = [{"key": key, "name": key, "cat": "ai", "color": "#fff", "tier": 1,
             "items": [{"link": it["u"], "pub_date": it.get("d") or ""} for it in items]}]
    out, stats = mod._apply_retention(srcs, offsets or {}, now_bj)
    ages = [mod._retention_age_h({"link": it["u"], "pub_date": it.get("d") or ""},
                                 offsets or {}, now_bj, key) for it in items]
    return {"windowH": mod._source_window_h(
                [{"link": it["u"], "pub_date": it.get("d") or ""} for it in items],
                offsets or {}, key),
            "kept": [x["link"] for x in out[0]["items"]],
            "ages": ages,
            "sources_after": stats["sources_after"]}


CASES = [
    {
        "name": "fast_hourly_source",
        "key": "fast_1",
        "items": [{"u": "http://e/%d" % i, "d": iso(1.0 + i)} for i in range(8)],
        "known": {}, "offsets": {},
    },
    {
        # 周更源：窗口放宽到 168h，100h 前那条必须留下（一刀切 72h 会误删）
        "name": "weekly_source_widened",
        "key": "weekly_1",
        "items": ([{"u": "http://w/%d" % i, "d": iso(24.0 * 7 * i + 1)} for i in range(6)]
                  + [{"u": "http://w/edge", "d": iso(120.0)}]),
        "known": {}, "offsets": {},
    },
    {
        # 停更源：全源超硬上限 ⇒ 出厂 0 条，保底不许把 12 天前的旧文捞回来
        "name": "dead_source_empties",
        "key": "dead_1",
        "items": [{"u": "http://d/%d" % i, "d": iso(300.0 + i)} for i in range(6)],
        "known": {}, "offsets": {},
    },
    {
        # 无日期但快照里见过（= 我方 first_seen）：必须按收录时刻判龄，不是丢弃也不是 now
        "name": "undated_known_date_rescues",
        "key": "nodate_1",
        "items": ([{"u": "http://n/k%d" % i, "d": iso(1.0 + i)} for i in range(3)]
                  + [{"u": "http://n/ghost", "d": ""}]),
        "known": {"http://n/ghost": iso(20.0)}, "offsets": {},
    },
    {
        # 判不了龄：上游没给日期、快照也没见过 ⇒ 丢，不能回退成 now
        "name": "undatable_dropped",
        "key": "un_1",
        "items": ([{"u": "http://u/k%d" % i, "d": iso(1.0 + i)} for i in range(3)]
                  + [{"u": "http://u/void", "d": ""}]),
        "known": {}, "offsets": {},
    },
    {
        # 纪元占位值（线上实测 12 条 0001-01-01）：不能当发布时间
        "name": "epoch_placeholder",
        "key": "ep_1",
        "items": ([{"u": "http://p/k%d" % i, "d": iso(1.0 + i)} for i in range(3)]
                  + [{"u": "http://p/zero", "d": "0001-01-01T00:00:00+00:00"}]),
        "known": {}, "offsets": {},
    },
    {
        # 源声明了时区偏移：判龄要校正一次（既不能不校，也不能校两次）
        "name": "declared_tz_offset",
        "key": "tz_1",
        "items": ([{"u": "http://t/k%d" % i, "d": iso(1.0 + i)} for i in range(3)]
                  + [{"u": "http://t/raw70", "d": iso(70.0)}]),
        "known": {}, "offsets": {"tz_1": -480},
    },
    {
        # 中间带：中位间隔 40h ⇒ 窗口 80h（既不是 72 也不是 168）。
        # 没有这条的话，两边比对的全是钳位点，`2 × 中位间隔` 这个公式本身从未跨语言验过
        # （2026-09-20 对抗审查 P2：8 个 case 的窗口值只出现 72/168 两个端点）。
        "name": "midband_window_is_formula_not_clamp",
        "key": "mid_1",
        "items": [
            {"u": "http://m/1", "d": iso(1.0)},
            {"u": "http://m/2", "d": iso(41.0)},
            {"u": "http://m/3", "d": iso(81.0)},     # 81 > 80 ⇒ 必须丢
            {"u": "http://m/4", "d": iso(121.0)},
            {"u": "http://m/edge", "d": iso(75.0)},  # 75 <= 80 ⇒ 必须留
        ],
        "known": {}, "offsets": {},
    },
    {
        # 上游把日期标到未来：以我方记录时刻为准（Python 侧 first_seen 回退）
        "name": "falsified_future_date",
        "key": "ff_1",
        "items": ([{"u": "http://f/k%d" % i, "d": iso(1.0 + i)} for i in range(3)]
                  + [{"u": "http://f/lie", "d": iso(-48.0)}]),
        "known": {"http://f/lie": iso(30.0)}, "offsets": {},
    },
]


def _node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _node_available(), reason="本机没有 node（CI 上有）")
def test_js_self_test_passes():
    """lib/rss_retention.js 自带的 13 条行为断言必须全绿。"""
    r = subprocess.run(["node", NODE_TEST], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, "node 自测失败：\n%s" % r.stdout[-1500:]
    assert "PASSED" in r.stdout


@pytest.mark.skipif(not _node_available(), reason="本机没有 node（CI 上有）")
def test_js_and_python_reach_identical_decisions(tmp_path):
    """同一份输入，JS 与 Python 的窗口 / 判龄 / 去留必须逐条相等。"""
    import copy
    fx = {"cases": [{"name": c["name"], "key": c["key"], "now": NOW_TEXT,
                     "items": copy.deepcopy(c["items"]),
                     "known": c["known"], "offsets": c["offsets"] or None}
                    for c in CASES]}
    path = tmp_path / "fx.json"
    path.write_text(json.dumps(fx), encoding="utf-8")
    r = subprocess.run(["node", NODE_TEST, "--fixture", str(path)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, "fixture 模式跑挂：\n%s\n%s" % (r.stdout[-800:], r.stderr[-800:])
    js = {x["name"]: x for x in json.loads(r.stdout)}
    assert set(js) == set(c["name"] for c in CASES)
    for c in CASES:
        exp = _py_decisions(c["items"], c["key"], c["offsets"], c["known"])
        # sources_after 是闸后口径（246/247 那个数就是从它来的），算了不断言等于没测
        assert exp["sources_after"] == (1 if exp["kept"] else 0), \
            "%s 闸后源计数与出厂结果不一致" % c["name"]
        if c["name"] == "midband_window_is_formula_not_clamp":
            assert exp["windowH"] not in (mod.RETENTION_BASE_H, mod.RETENTION_MAX_WINDOW_H), \
                "中间带用例退化成了钳位点比对：窗口 %.1f" % exp["windowH"]
        got = js[c["name"]]
        assert abs(got["windowH"] - exp["windowH"]) < 0.01, \
            "%s 窗口不一致：JS %s vs Python %s" % (c["name"], got["windowH"], exp["windowH"])
        assert got["kept"] == exp["kept"], \
            "%s 去留不一致：JS %s vs Python %s" % (c["name"], got["kept"], exp["kept"])
        for u, ja, pa in zip([i["u"] for i in c["items"]], got["ages"], exp["ages"]):
            if ja is None or pa is None:
                assert (ja is None) == (pa is None), "%s/%s 判不了龄判定不一致" % (c["name"], u)
            else:
                assert abs(ja - pa) < 0.01, "%s/%s 判龄不一致：JS %.3f vs Python %.3f" % (
                    c["name"], u, ja, pa)


def test_policy_constants_match_python():
    """阈值两边各写一份，改一边忘一边就是双标 —— 直接把等式钉住。"""
    txt = io.open(LIB, encoding="utf-8").read()
    def num(k):
        v = txt.split(k + ":", 1)[1]
        return float(v.split(",", 1)[0].strip())
    assert num("baseH") == mod.RETENTION_BASE_H
    assert num("hardH") == mod.RETENTION_MAX_WINDOW_H
    assert num("hardH") == mod.RETENTION_FLOOR_MAX_H
    assert num("coef") == mod.RETENTION_COEF
    assert int(num("floor")) == mod.RETENTION_FLOOR
    assert int(num("minSamples")) == mod.RETENTION_MIN_SAMPLES
    assert int(num("epochYear")) == mod.RETENTION_EPOCH_YEAR
    assert "FUTURE_TOLERANCE_MS = 60 * 60000" in txt, \
        "证伪容忍阈值与 Python 的 FUTURE_DATE_TOLERANCE_MIN=60 脱钩"


def test_runtime_exits_are_gated():
    """api/rss.js 的三条实时出口都必须过闸，且旧的"无日期保留"后门必须消失。"""
    js = io.open(API_RSS, encoding="utf-8").read()
    assert "gateSources(" in js, "api/rss.js 根本没调用闸门：运行时又在出厂老内容"
    assert "if (!item.d) return true" not in js, \
        "旧 filterItems 还在：无日期条目在运行时被无条件保留"
    assert "pubDate || new Date().toISOString()" not in js, \
        "上游没给日期时用 now 兜底 —— Python 侧刚废掉的永生通道在 JS 侧复活"
    assert js.count("gateSources(results") >= 1, "?batch 出口没过闸"
    assert js.count("gateSources([result]") >= 1, "?source 出口没过闸"
    assert js.count("gateSources(mergedSources") >= 1, "refresh 合并出口没过闸"


def test_gate_does_not_reload_the_snapshot():
    """闸门自己绝不加载快照：那是几十 MB 的 JSON，一次请求里加载两遍会把端点打挂。

    这条是我自己踩出来的：第一版 gateSources 里调了 loadSnapshot()，batch 路径本来
    就已经为翻译加载过一次 —— 不写死断言，下次顺手"复用一下"就会把 OOM 带进生产。
    """
    js = io.open(API_RSS, encoding="utf-8").read()
    body = js.split("function gateSources(", 1)[1]
    body = body.split("\n}", 1)[0]
    assert "loadSnapshot(" not in body, "gateSources 里又去加载快照了：knownDates 必须由调用方传入"
    assert "buildDateIndex(" not in body, "同上：索引在调用方建一次就够"
    assert js.count("buildDateIndex(") >= 2, "batch 与 refresh 两条路径都应复用已加载的快照建索引"
