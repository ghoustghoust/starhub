# -*- coding: utf-8 -*-
"""批 6 的回读端：从 `build_logs/*.jsonl` 里的 growth 事件算出"每场新增字节"的历史。

为什么要有它：Actions 的输出只活 90 天，而这轮所有的判断都建立在"每场新增多少字节"这一个数上
（19.83 → 3.32 → 0.481 → 0.032 MiB/场）。写进构建日志之后必须能**从仓库本身**读回来，
否则下次有人问"还长吗"，又得手工去拼 tree-diff。

判据不打网络：`summarize()` 是纯函数，喂合成事件即可；取数那一层单独验"本地优先、缺就远端补"。
"""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.environ.get("STARHUB_GROWTH_HISTORY") or os.path.join(ROOT, "tools", "growth_history.py")


def _load():
    spec = importlib.util.spec_from_file_location("growth_history", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ev(day, i, nbytes, blobs=1, commit=None):
    return {"ts": "%sT%02d:00:00+08:00" % (day, i), "type": "growth",
            "new_bytes": nbytes, "new_blobs": blobs, "commit": commit or ("c%d" % i)}


def test_tool_exposes_a_pure_summarizer():
    m = _load()
    assert hasattr(m, "summarize"), "缺 summarize —— 读数没法在不调网络的情况下被验证"
    assert hasattr(m, "iter_growth_events"), "缺 iter_growth_events —— 解析与取数混在一起就没法测"


def test_iter_growth_events_skips_foreign_lines(tmp_path):
    """jsonl 里混着 build/trigger/deploy/growth 四种行，还有可能半截；只能挑出 growth。"""
    m = _load()
    p = tmp_path / "2026-10-02.jsonl"
    lines = [json.dumps(_ev("2026-10-02", 1, 33000)),
             json.dumps({"ts": "x", "type": "build", "items_snapshot": 9}),
             "{not json",
             "",
             json.dumps(_ev("2026-10-02", 2, 31000))]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = list(m.iter_growth_events(str(p)))
    assert [g["new_bytes"] for g in got] == [33000, 31000], got
    assert len(got) == 2, "半截 JSON 行必须被跳过而不是让整场读数失败：%r" % (got,)


def test_summarize_reports_rate_and_worst_build():
    m = _load()
    evs = [_ev("2026-10-01", 9, 3_300_000), _ev("2026-10-01", 10, 33_000),
           _ev("2026-10-02", 1, 31_000), _ev("2026-10-02", 2, 29_000)]
    out = m.summarize(evs)
    assert out["total_builds"] == 4, out
    assert out["max_bytes"] == 3_300_000, "必须保留最坏一场：日界那次日志提交就是它，不能只给均值"
    assert out["median_bytes"] == (31_000 + 33_000) // 2 or out["median_bytes"] in (31_000, 33_000, 32_000), out
    per_day = {d["date"]: d for d in out["days"]}
    assert per_day["2026-10-01"]["builds"] == 2 and per_day["2026-10-02"]["builds"] == 2, out
    assert per_day["2026-10-01"]["sum_bytes"] == 3_333_000, per_day


def test_empty_input_is_reported_as_empty_not_zero_average(tmp_path):
    """没有 growth 事件时必须明说"没有"，不能报 0 —— 报 0 会被读成"曲线是平的"。"""
    m = _load()
    out = m.summarize([])
    assert out["total_builds"] == 0
    assert out.get("note") or out.get("max_bytes") is None, (
        "空输入也要有可区分的形状：%s" % out)
    empty_day = tmp_path / "2026-01-01.jsonl"
    empty_day.write_text(json.dumps({"ts": "x", "type": "build"}) + "\n", encoding="utf-8")
    assert list(m.iter_growth_events(str(empty_day))) == []


def test_cli_reads_local_first_and_falls_back_to_remote(tmp_path, monkeypatch):
    """本地有当天日志就不联网；缺才走 raw。否则每次问一句都要 8 个网络请求。

    第一版这条是"假钉"：它只调了 `iter_growth_events(路径)`，压根没经过
    `growth_events_for_day` 的本地/远端分支 —— 于是把该分支改成"永远走远端"也照样全绿
    （变异 GH4 实测就是这么漏的）。现在必须走那个函数本身。
    """
    m = _load()
    d = tmp_path / "build_logs"
    d.mkdir()
    (d / "2026-10-02.jsonl").write_text(json.dumps(_ev("2026-10-02", 1, 12345)) + "\n",
                                         encoding="utf-8")
    calls = []
    monkeypatch.setattr(m, "fetch_remote", lambda day: calls.append(day) or "")
    got = m.growth_events_for_day("2026-10-02", local_dir=str(d))
    assert got and got[0]["new_bytes"] == 12345, "本地文件没被读到：%r" % (got,)
    assert calls == [], (
        "本地已有 %s 却仍然联网 ⇒ 本地优先的分支是摆设" % d)
    missing = m.growth_events_for_day("2026-01-05", local_dir=str(d))
    assert calls == ["2026-01-05"], "本地缺该日时必须回落到远端：%s" % calls
    assert missing == [], "远端返回空串时应得空列表而不是抛"


def test_type_filtering_happens_at_the_reader_not_the_summarizer(tmp_path):
    """反空转：type 过滤发生在 `iter_growth_events`，`summarize` 只算交给它的事件。

    这条钉的是责任划分：读者筛一次、算者再筛一次的话，任何一侧改 type 名都会变成
    「两边都以为对方在筛」的漏筛。所以要求：非 growth 的行一条都不许进到读数里。
    """
    m = _load()
    p = tmp_path / "2026-10-02.jsonl"
    evs = [_ev("2026-10-02", i, 1000 * i) for i in range(1, 6)]
    p.write_text("\n".join(json.dumps(r) for r in evs) + "\n", encoding="utf-8")
    assert m.summarize(list(m.iter_growth_events(str(p))))["total_builds"] == 5
    p.write_text("\n".join(json.dumps(dict(r, type="build")) for r in evs) + "\n", encoding="utf-8")
    assert list(m.iter_growth_events(str(p))) == [], (
        "type 不再是 growth 时读者必须一条都不给，否则任何日志行都会被当成读数")
