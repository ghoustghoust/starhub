# -*- coding: utf-8 -*-
"""批 6 判据（补漏）：写端与读端的键名契约。

`tools/history_growth.py` 写、`tools/growth_history.py` 读，两个文件各长各的键名 ——
今天读端在生产上遇到的还是**空集**（BJ 10-02 的 growth 事件要等下一个北京日首次提交才进 git），
所以"能不能读出来"至今没被真数据证明过。本仓已经吃过一次"Py 与 JS 两套翻译判据分叉 5/14"，
同一个仓库里两份各自演化的字面量就是分叉源 ⇒ 用真写、真读对跑一遍来钉，而不是两边各写单测。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _writer():
    return _load("_hg_writer", "tools/history_growth.py")


def _reader():
    return _load("_gg_reader", "tools/growth_history.py")


def _clock(ymd):
    """造一个只认 `ymd` 这一天的假时钟，装到写端模块的 `datetime` 上。

    `log_line` 的 ts 来自 `datetime.now(_BJT)` ⇒ 测试若把日期写死进断言，跨过北京 00:00 就必然红。
    本文件在 **A2（blocking）** 里，那颗雷会冻住构建与两次部署，所以日期一律由钉住的时钟产生。
    """
    import datetime as _dt
    y, m, d = (int(x) for x in ymd.split("-"))

    class _Clock:
        @staticmethod
        def now(tz=None):
            return _dt.datetime(y, m, d, 13, 0, tzinfo=tz or _dt.timezone.utc)

    return _Clock


def test_written_record_is_readable_by_the_reader(tmp_path):
    """真 log_line 写一行，真 iter_growth_events 必须读回同一条，且字段一个都不许丢。"""
    w, r = _writer(), _reader()
    p = tmp_path / "2026-10-02.jsonl"
    w.log_line(33554, ["a.js", "b.js"], str(p), commit="0123456789abcdef0123456789abcdef01234567")
    evs = list(r.iter_growth_events(str(p)))
    assert len(evs) == 1, "写了 1 条却读回 %d 条 ⇒ 两端键名/形状已分叉" % len(evs)
    e = evs[0]
    assert e["new_bytes"] == 33554, e
    assert e["new_blobs"] == 2, e
    assert e["commit"].startswith("0123456789abcdef"), e


def test_summarize_consumes_what_the_writer_emits(tmp_path, monkeypatch):
    """读数最终要落到 summarize 的中位数/日均上：写端换键名后这里必须变空或变 0。"""
    w, r = _writer(), _reader()
    monkeypatch.setattr(w, "datetime", _clock("2026-10-02"))
    p = tmp_path / "2026-10-02.jsonl"
    for nbytes, blobs in ((100, ["x"]), (300, ["y", "z"]), (200, ["w"])):
        w.log_line(nbytes, blobs, str(p), commit="c" * 40)
    s = r.summarize(list(r.iter_growth_events(str(p))))
    assert s["total_builds"] == 3, s
    assert s["median_bytes"] == 200, s
    assert s["max_bytes"] == 300, s
    assert s["days"][0]["date"] == "2026-10-02", s


def test_summarize_groups_by_the_record_date(tmp_path, monkeypatch):
    """上面那条钉住时钟之后的控制：日期来自记录自己的 ts，不是来自跑测试的那天。

    两天各写一条 ⇒ 必须落成两个日桶。少了这条，`_clock()` 只要"让日期恒定"就能骗过上一条，
    而 summarize 真按别的口径（比如文件mtime）分组时没人会红。
    """
    w, r = _writer(), _reader()
    p = tmp_path / "two-days.jsonl"
    for day in ("2026-10-01", "2026-10-02"):
        monkeypatch.setattr(w, "datetime", _clock(day))
        w.log_line(1000, ["a"], str(p), commit="c" * 40)
    s = r.summarize(list(r.iter_growth_events(str(p))))
    assert [d["date"] for d in s["days"]] == ["2026-10-01", "2026-10-02"], s
    assert s["total_builds"] == 2, s
    assert all(d["builds"] == 1 for d in s["days"]), s


def test_zero_growth_record_is_a_real_reading(tmp_path):
    """0 是合法读数（相邻提交没新增 blob），不许被"更严格的过滤"顺手丢掉。"""
    w, r = _writer(), _reader()
    p = tmp_path / "2026-10-02.jsonl"
    w.log_line(0, [], str(p), commit="e" * 40)
    evs = list(r.iter_growth_events(str(p)))
    assert len(evs) == 1, "写端报了 0 字节，读端却当没发生：%s" % (evs,)
    assert evs[0]["new_bytes"] == 0, evs
    assert r.summarize(evs)["total_builds"] == 1, r.summarize(evs)


def test_other_record_types_are_not_growth(tmp_path):
    """同文件里的其它 type 不许被读成 growth 事件。

    起因是 ②（Stage 缺件点名）往 `build_logs/<日期>.jsonl` 里新增了一种 `pages_missing` 记录，
    而 growth 读数器解析的**就是这份文件** —— 两种新记录从此共存，读数的纯度得钉住。
    故意放一条"带 new_bytes 的 build 记录"：只看 `new_bytes` 是不是整数、不看 type 的读数器
    会把它算进去并抬高统计，所以这条判据守的是 type 这一层（上面那条守 null/坏行）。
    """
    w, r = _writer(), _reader()
    p = tmp_path / "2026-10-02.jsonl"
    w.log_line(1000, ["a"], str(p), commit="a" * 40)
    with open(str(p), "a", encoding="utf-8") as fh:
        fh.write('{"ts":"2026-10-02T13:00:00+08:00","type":"pages_missing","files":"index.html ai-daily.html"}\n')
        fh.write('{"ts":"2026-10-02T13:00:01+08:00","type":"build","new_bytes":999999}\n')
        fh.write('{"ts":"2026-10-02T13:00:02+08:00","type":"trigger"}\n')
    evs = list(r.iter_growth_events(str(p)))
    assert [e["new_bytes"] for e in evs] == [1000], \
        "读数器把非 growth 记录也算进来了（type 过滤失效）：%s" % (evs,)
    s = r.summarize(evs)
    assert s["total_builds"] == 1 and s["max_bytes"] == 1000, s


def test_reader_does_not_silently_zero_out_a_bad_record(tmp_path):
    """坏记录不许被当 0 混进统计：否则"曲线是平的"可以是假话。"""
    w, r = _writer(), _reader()
    p = tmp_path / "2026-10-02.jsonl"
    w.log_line(500, ["a"], str(p), commit="d" * 40)
    with open(str(p), "a", encoding="utf-8") as fh:
        fh.write('{"ts":"2026-10-02T13:00:00+08:00","type":"growth","new_bytes":null}\n')
        fh.write("不是 json 的一行\n")
    evs = list(r.iter_growth_events(str(p)))
    assert len(evs) == 1, "非法/缺字段的行必须被丢掉，不能变成读数为 0 的事件：%s" % (evs,)
