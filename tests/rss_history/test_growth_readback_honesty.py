# -*- coding: utf-8 -*-
"""回读端 `tools/growth_history.py` 的假绿护栏：把「我没看到数据」和「没在长」分开说。

它回答的是「还长吗」，而最坏的答法是把两种「没取到」读成「没长」：
① 本地优先 —— 这条工作树的 .git 是浅取副本，远端新写的 growth 行永远不会出现在本地那份
   `build_logs/<day>.jsonl` 里；本地读到 0 条就直接收工，等于把「本地没读到」当结论。
② 取数失败 —— 第一版 `fetch_remote` 把所有异常吞成空串，断网/超时/403 与「这天确实没有
   growth 行」在输出上一模一样。
两条都打在行为层（注入 fetch、看函数与 CLI 的实际产出），不 grep 源码文本。
"""
import importlib.util
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GROWTH_LINE = ('{"ts":"2026-10-02T03:00:00+08:00","type":"growth",'
               '"new_blobs":1,"new_bytes":24576,"commit":"a"}\n')
BUILD_LINE = '{"ts":"2026-10-02T03:00:00+08:00","type":"build"}\n'


def _load():
    sys.modules.pop("_gh_reader", None)
    spec = importlib.util.spec_from_file_location(
        "_gh_reader", os.path.join(ROOT, "tools", "growth_history.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_gh_reader"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_cli(m, tmp_path, fetch):
    argv = sys.argv
    try:
        sys.argv = ["growth_history.py", "--dir", str(tmp_path), "--days", "1"]
        return m.main(fetch=fetch)
    finally:
        sys.argv = argv


def test_stale_local_copy_falls_through_to_the_remote(tmp_path):
    """本地一条 growth 都没有 ⇒ 必须去问远端，不许把「本地没读到」当结论。"""
    m = _load()
    d = tmp_path / "build_logs"
    d.mkdir()
    (d / "2026-10-02.jsonl").write_text(BUILD_LINE, encoding="utf-8")
    calls = []

    def fetch(day):
        calls.append(day)
        return GROWTH_LINE

    evs = m.growth_events_for_day("2026-10-02", local_dir=str(d), fetch=fetch)
    assert calls == ["2026-10-02"], "本地 0 条 growth 却没问远端 ⇒ 浅副本会把没看到变成没在长"
    assert len(evs) == 1, evs


def test_fresh_local_copy_does_not_hit_the_network(tmp_path):
    """反向护栏：本地已有 growth 就别白跑网络（这条优化本身是真的）。"""
    m = _load()
    d = tmp_path / "build_logs"
    d.mkdir()
    (d / "2026-10-02.jsonl").write_text(GROWTH_LINE, encoding="utf-8")

    def boom(day):
        raise AssertionError("本地已有读数时不该联网")

    evs = m.growth_events_for_day("2026-10-02", local_dir=str(d), fetch=boom)
    assert len(evs) == 1, evs


def test_unreachable_remote_is_reported_as_unknown_not_as_flat(tmp_path, capsys):
    """远端不可达 ⇒ CLI 必须说出「取数失败」，不能只报「没有 growth 事件」。"""
    def dead(day):
        raise urllib.error.URLError("network down")

    rc = _run_cli(_load(), tmp_path, dead)
    out = capsys.readouterr().out
    assert "取数失败" in out, "断网被读成了没有 growth 事件，读数与故障不可区分：" + repr(out[-240:])
    assert rc == 0, "只是读数缺失，不该让工具变红"


def test_present_but_growthless_remote_is_not_called_a_failure(tmp_path, capsys):
    """远端有内容但没有 growth 行 ⇒ 是「这天没测到」，不许报成故障。"""
    def alive(day):
        return BUILD_LINE

    _run_cli(_load(), tmp_path, alive)
    out = capsys.readouterr().out
    assert "取数失败" not in out, "把正常的空读数说成了故障：" + repr(out[-200:])


def test_http_404_is_an_absent_day_not_a_failure(tmp_path, capsys):
    """404 = 那天没有日志文件，归「无读数」；只有非 404 才算取数失败。"""
    def nf(day):
        raise urllib.error.HTTPError("https://x/404", 404, "Not Found", {}, None)

    _run_cli(_load(), tmp_path, nf)
    out = capsys.readouterr().out
    assert "取数失败" not in out, "404 被当成不可达：" + repr(out[-200:])
