# -*- coding: utf-8 -*-
"""相对时间标签：页面 JS 必须与读者已在看的文案逐字相同（Task 1 的对等判据）。

背景：`time_str` 以前由构建期 Python 算好烘进产物。**它不是卡片显示的主路径**——
卡片那行走 `_dynTime(a)`，`a.time` 只是它的兜底分支（`pub_date` 缺失/非法/坏日期）
以及书签快照与分享文案读的值。现在这串改成页面按 pub_date + BUILD_TS 现算
（`lib/rel_time.js`）。**可接受结果是 `a.time` 对同一 pub_date 与旧实现逐字相等**，
兜底位与书签才不会漂；判据不能拿"当前实现"当参照——那等于自己跟自己比，改错了也发现不了。

参照因此是一份**黄金表** `goldens/rel_time_labels.json`：在删除 Python 实现之前，
用它对一批用例取一次值冻结下来，代表"2026-09-30 读者实际看到的那串字"。
此后 JS 与黄金表比；要改文案必须显式重生成黄金表并写明理由，不能靠改实现顺手漂移。

还钉一个真会错手的细节：pub_date 混着 "+08:00" / "Z" / 裸值三种写法，Python 把裸值
按北京时间解释，而 `new Date(裸值)` 按访客本地时区解释。访客不在东八区时后者会算出
另一个时刻——本仓反复栽过的"时区盲"，所以三种写法各来一遍，并单独有一条同标签判据。

跑法：py -3.11 -m pytest tests/rss_history/test_time_label_parity.py -q -s
"""
import datetime
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

NODE_HARNESS = os.path.join(ROOT, "tests", "rss_js", "test_rel_time.js")
LIB = os.path.join(ROOT, "lib", "rel_time.js")
GOLDEN = os.path.join(HERE, "goldens", "rel_time_labels.json")

NOW_BJ = datetime.datetime(2026, 9, 30, 12, 0, 0,
                           tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


def _iso_bj(sec):
    return (NOW_BJ - datetime.timedelta(seconds=sec)).isoformat()


def _iso_utc(sec):
    dt = (NOW_BJ - datetime.timedelta(seconds=sec)).astimezone(datetime.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _iso_bare(sec):
    return (NOW_BJ - datetime.timedelta(seconds=sec)).replace(tzinfo=None).isoformat()


def _golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)["rows"]


def _js_labels(cases):
    # 逐条透传 now_epoch：早先这里硬编码成 NOW_BJ，会把调用方传的基准时刻吞掉，
    # 于是"挪基准标签必须变"那条资格证明测试变成假通过。
    payload = [{"id": c["id"], "iso": c["iso"],
                "now_epoch": int(c["now_epoch"]) if "now_epoch" in c else int(NOW_BJ.timestamp())}
               for c in cases]
    env = dict(os.environ)
    env["STARHUB_RELTIME_LIB"] = LIB.replace("\\", "/")
    p = subprocess.run(["node", NODE_HARNESS], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, cwd=ROOT)
    assert p.returncode == 0, "node harness 失败: %s" % (p.stderr or "")[-800:]
    return {r["id"]: r["js"] for r in json.loads(p.stdout)}


def test_golden_table_is_not_empty_and_covers_every_branch():
    """黄金表自己得先有内容，否则后面全是跟空气比。"""
    rows = _golden()
    assert len(rows) >= 40, len(rows)
    labels = [r["label"] for r in rows]
    assert any(x.endswith("秒前") for x in labels), labels[:6]
    assert any(x.endswith("分钟前") for x in labels), labels[:6]
    assert any(x.endswith("小时前") for x in labels), labels[:6]
    assert any(x.endswith("天前") for x in labels), labels[:6]
    assert any(len(x) == 11 and x[2] == "-" and x[5] == " " and x[8] == ":" for x in labels), (
        "缺未来分支 'MM-DD HH:MM'")
    assert any(len(x) == 10 and x[4] == "-" for x in labels), "缺 >=30 天分支 'YYYY-MM-DD'"
    assert any(x == "" for x in labels), "缺空/非法输入分支"


def test_js_label_matches_golden_case_by_case():
    got = _js_labels(_golden())
    diff = []
    for r in _golden():
        if got.get(r["id"]) != r["label"]:
            diff.append("%-22s golden=%-14r js=%-14r" % (r["id"], r["label"], got.get(r["id"])))
    assert not diff, "JS 与读者已在看的文案分叉 %d 条：\n  %s" % (len(diff), "\n  ".join(diff))


def test_bare_and_offsetted_forms_agree():
    """同一时刻的三种写法必须同标签——这条专防"时区盲"回归。"""
    cases = [{"id": "a", "iso": _iso_bj(7200)}, {"id": "b", "iso": _iso_utc(7200)},
             {"id": "c", "iso": _iso_bare(7200)}]
    got = _js_labels(cases)
    assert got["a"] == got["b"] == got["c"] == "2小时前", got


def test_parity_catches_a_shifted_reference():
    """判据的资格证明：基准挪 10 小时，标签必须变。不变就说明没在真比。"""
    got = _js_labels([{"id": "x", "iso": _iso_bj(7200),
                       "now_epoch": int(NOW_BJ.timestamp()) + 10 * 3600}])
    assert got["x"] != "2小时前", got


def test_builder_no_longer_ships_a_second_formatter():
    """Python 侧那个实现必须真被删掉，不能留个没人调的函数当"参照"。

    两条口径并存是这轮改动最怕复发的形态：改的人只会看见其中一个。
    """
    src = open(os.path.join(ROOT, "build_rss_aggregator.py"), encoding="utf-8").read()
    assert "def _fmt_rel_time(" not in src, "Python 侧相对时间实现回来了：会有两套口径"
    assert "_fmt_rel_time(" not in src.replace("def _fmt_rel_time(", ""), "仍有调用点"


def test_time_str_is_no_longer_written_or_shipped():
    """出厂侧必须真的在剥（防"定义了常量但没人调用"的空转）。"""
    src = open(os.path.join(ROOT, "build_rss_aggregator.py"), encoding="utf-8").read()
    assert 'item["time_str"]' not in src and 'it["time_str"]' not in src and 'entry["time_str"]' not in src, \
        "还有装配处在写 time_str，派生字段并没真移出去"
    assert "if(o.time_str)" not in src, "抽屉合并处还在搬 time_str（等于从后门把它请回来）"
    assert "_strip_derived_items(" in src, "剥离函数没人调用 = 空转"
