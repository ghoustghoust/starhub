# -*- coding: utf-8 -*-
"""A2：出口超龄条目必须**点名**（link + 出厂那串 + 判龄基准），不能只报一个数。

起因（现取读数 2026-10-03）：今晨 04:11 那场 `[留存] 出厂龄期分项：…｜>168h 1 条`，
而 07:38 / 08:09 两场是 `>168h 0 条` ⇒ 破口是**偶发**的，一天里只冒 0~1 条。
更要命的是日志只有计数、没有身份：想查"这条到底怎么混进来的"就得等它再次出厂时人工翻产物，
而 10-02 那条 JetBrains 样本今天已经滚出 rss-data-6.js（现取整块 5,118,022 字符里 0 命中）。

为什么先点名、后改判龄：10-02 我对机制的解释（"历史缺 pub_date ⇒ 判龄回退 first_seen ⇒ 放行"）
在本文件所属的同一套出口代码上**复现不出来** ——
① 按 first_seen 回退构造的条目被 `_accumulate_history` 真实出口丢掉（08:4x 实跑：进 5 条 → 留 4 条，
   那条不在出厂名单里）；
② 按源级 `pub_date_offset_min` 差值构造的条目，判龄恒等于出厂龄（offset=0/1500/2600 三档实跑判龄都是 190.0h）
   ⇒ 直接被窗口丢弃。
⇒ 也就是说：**没有真实身份就没有正确的修法**。这一节先把"是谁、两把尺各读多少"钉成日志与 stats，
下一场再冒出来时可直接定位；② 的标记改造要等这个读数，不靠猜。

打桩口径（说清楚，免得被当成"伪造业务结论"）：下面只把 `_retention_age_h` 打桩成"这条很新"，
用来**必然触发**超龄出厂分支，从而测点名机制本身（字段齐全、上限生效、会 print）；
`_retention_ref_dt` 不打桩 ⇒ `ref_age_h` 是真实第二把尺的读数，正是我们要在日志里看到的对照。
反向那半与正向同权重：正常的一场绝不许多打一行、也绝不许多点名一条。
"""
import datetime
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()


def _iso(hours_ago, now):
    return (now - datetime.timedelta(hours=hours_ago)).isoformat()


def _hist(link, now, hours_ago):
    return {"link": link, "source": "src_a", "source_key": "src_a", "cat": "ai", "color": "#fff",
            "title": "t", "summary": "s", "pub_date": _iso(hours_ago, now),
            "first_seen": _iso(hours_ago, now)}


def _source(items):
    return {"key": "src_a", "name": "A", "cat": "ai", "color": "#fff", "tier": 1,
            "url": "http://src_a/feed", "items": items}


@pytest.fixture
def clean(monkeypatch, tmp_path):
    mod._rss_history = {}
    monkeypatch.setattr(mod, "RSS_SOURCES", [])
    monkeypatch.chdir(tmp_path)
    return monkeypatch


def _item(link, now, hours_ago):
    return {"link": link, "title": "t", "summary": "s",
            "pub_date": _iso(hours_ago, now), "source_key": "src_a"}


def test_over_age_item_is_named_with_both_time_bases(clean, capsys):
    """超龄出厂的那条必须进 stats["over_names"]，且带两把尺的读数。"""
    now = mod._now_bj()
    link = "http://x/resurfaced"
    mod._rss_history[link] = _hist(link, now, 250.0)
    src = _source([_item("f%d" % i, now, 1.0 + i) for i in range(3)] + [_item(link, now, 250.0)])
    # 只打桩"闸门判龄"这一步：让这条被判成很新从而走到出厂分项，逼出超龄分支
    clean.setattr(mod, "_retention_age_h", lambda it, *a, **k: 1.0)
    result, stats = mod._apply_retention([src], {}, now)
    assert stats["over"] == 1, "出厂分项没把这算成超龄 ⇒ 本用例的形状不成立，点名判据不作数"
    names = stats.get("over_names")
    assert names, (
        "stats 里没有 over_names ⇒ 日志只有一个数字，没人能查这条是谁、两把尺各读多少（实得键 %s）"
        % sorted(stats))
    n = names[0]
    for field in ("link", "src", "ship", "ship_age_h", "ref_age_h", "dfb"):
        assert field in n, "点名记录缺字段 %s（实得 %s）⇒ 拿回去还是定位不了" % (field, sorted(n))
    assert n["link"] == link and n["src"] == "src_a"
    assert abs(n["ship_age_h"] - 250.0) < 1.0, "出厂龄读数不对：%s" % n
    assert abs(n["ref_age_h"] - 250.0) < 1.0, (
        "判龄基准那把尺没独立算（应为真实 _retention_ref_dt 的读数，实得 %s）" % n["ref_age_h"])
    assert n["dfb"] is False
    out = capsys.readouterr().out
    assert "超龄点名" in out, "点名只进 stats 不进日志 ⇒ 下一场构建日志里仍然看不到身份"
    assert link in out


def test_the_two_rulers_are_read_independently(clean):
    """`ref_age_h` 必须是**独立**算出来的判龄基准，不许抄出厂那把尺。

    上一用例里两个读数恰好相同（250/250），实现就算写成 `ref_age_h = ship_age_h` 也能过 ⇒
    点名会退化成"同一个数打两遍"，而这条判据的存在意义正是让两者的差可见。
    这里把 `_retention_ref_dt` 打桩成 10 小时前，两把尺必然分开。
    """
    now = mod._now_bj()
    link = "http://x/two-rulers"
    mod._rss_history[link] = _hist(link, now, 250.0)
    src = _source([_item("f%d" % i, now, 1.0 + i) for i in range(3)] + [_item(link, now, 250.0)])
    clean.setattr(mod, "_retention_age_h", lambda it, *a, **k: 1.0)
    clean.setattr(mod, "_retention_ref_dt",
                  lambda it, *a, **k: now - datetime.timedelta(hours=10.0))
    result, stats = mod._apply_retention([src], {}, now)
    n = stats["over_names"][0]
    assert abs(n["ship_age_h"] - 250.0) < 1.0 and abs(n["ref_age_h"] - 10.0) < 1.0, (
        "两把尺读成同一个数了（出厂 %.1fh / 判龄基准 %s）⇒ 点名看不出这条为什么能过闸"
        % (n["ship_age_h"], n["ref_age_h"]))


def test_names_capped_so_log_cannot_explode(clean):
    """点名有上限：超龄成百上千时不许把构建日志刷爆（只点名前 N 条，计数仍是全量）。"""
    now = mod._now_bj()
    olds = [_item("http://x/old%d" % i, now, 300.0 + i) for i in range(8)]
    src = _source([_item("f%d" % i, now, 1.0 + i) for i in range(3)] + olds)
    clean.setattr(mod, "_retention_age_h", lambda it, *a, **k: 1.0)
    result, stats = mod._apply_retention([src], {}, now)
    assert stats["over"] == 8, "计数本身被上限截了 ⇒ 分项数字失去意义"
    assert len(stats["over_names"]) <= 5, (
        "点名没有上限（实得 %d 条）⇒ 破口变大那天构建日志会被几千行点名淹没" % len(stats["over_names"]))


def test_clean_build_adds_no_names_and_no_line(clean, capsys):
    """反向（必须绿）：一切正常的一场既不许多点名一条，也不许多打一行。"""
    now = mod._now_bj()
    src = _source([_item("f%d" % i, now, 1.0 + i) for i in range(4)])
    result, stats = mod._apply_retention([src], {}, now)
    assert stats["over"] == 0
    assert not stats.get("over_names"), "窗内的一场也被点名 ⇒ 告警噪音明天就会被绕过"
    out = capsys.readouterr().out
    assert "超龄点名" not in out, "正常场多打一行 ⇒ 逐场比的口径被改动（假警比无警更糟）"
