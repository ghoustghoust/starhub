# tests/rss_history/test_retention_window.py
# -*- coding: utf-8 -*-
"""72 小时留存的**出口闸门**：进入产物的每一条都必须过窗，判不了龄的一律丢弃。

为什么不是「把历史裁剪参数调一下」：线上 rss-data-*.js 实测 938 源 / 20,904 条里
**59% 超过 72 小时**（中位 137h、p99 4 年、12 条 pub_date 是 0001-01-01），而代码里的
72h 裁剪只遍历 `_rss_history`。当次抓取的条目在 `src_map[key]["items"] = list(_fetched)`
这一步**原样进输出，从未与 cutoff 比较** —— 上游 feed 自己挂着的老文章（低频博客/播客
一期 30 条是常态）走的就是这条旁路。另一条旁路是合并阶段的「无日期回退成当前时间」，
它造出永不过期的条目。所以修法是在返回值上加一道统一闸门，而不是加第四个裁剪循环。

闸门放在 `_accumulate_history` 返回之前，是因为 `_save_api_snapshot` / `build_html` /
`write_data_chunks` 三个消费者共用同一个返回值 —— 这样天然不会出现「页面裁了 API 没裁」。

本文件里的红线：
  · 该丢的必须丢（当次抓取的超龄条目 / 无日期 / 纪元占位值）；
  · 该留的一条不能少（窗口内、保底 3 条、判龄口径只校正一次）；
  · 闸门必须**真的被调用且作用在返回值上**（AST 锁调用点，防止实现成没人调的死代码）；
  · 只减少条目：不改顺序、不裁掉字段、不破坏同链接去重。
"""
import ast
import datetime
import io
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

BUILD = os.path.join(ROOT, "build_rss_aggregator.py")


def _now():
    return mod._now_bj()


def _iso(hours_ago, now=None):
    """按「N 小时前」生成 aware ISO 串（北京时间口径）。"""
    return ((now or _now()) - datetime.timedelta(hours=hours_ago)).isoformat()


def _item(link, hours_ago=None, now=None, pub=True, **extra):
    it = {"link": link, "title": "t " + link, "summary": "s",
          "pub_date": "" if not pub else _iso(hours_ago, now)}
    it.update(extra)
    return it


def _source(items, key="src_a", name="A"):
    return {"key": key, "name": name, "cat": "ai", "color": "#fff",
            "tier": 1, "url": "http://%s/feed" % key, "items": items}


def _hist_item(link, source_key, hours_ago, first_seen_ago=None, pub_date=None, now=None):
    """往 `_rss_history` 里塞一条（历史存的是**原始** pub_date，不含降级改写）。"""
    now = now or _now()
    fs = first_seen_ago if first_seen_ago is not None else hours_ago
    return {
        "link": link, "source": source_key, "source_key": source_key, "cat": "ai",
        "color": "#fff", "title": "t " + link, "summary": "s", "title_zh": "",
        "summary_zh": "", "full_content": "", "image": "", "media_url": "",
        "media_type": "",
        "pub_date": _iso(hours_ago, now) if pub_date is None else pub_date,
        "first_seen": _iso(fs, now),
    }


@pytest.fixture
def clean(monkeypatch, tmp_path):
    """隔离模块级全局：`_rss_history` 不重置会跨测试文件串味（既有约定）。"""
    monkeypatch.chdir(tmp_path)
    mod._rss_history = {}
    monkeypatch.setattr(mod, "RSS_SOURCES", [])
    return monkeypatch


def _ship(sources):
    """跑一遍真实出口，返回 (每源 link 清单, 结果, 条数)。"""
    result, total = mod._accumulate_history(sources)
    return {s["key"]: [it.get("link") for it in s.get("items", [])] for s in result}, result, total


# ── 现状必须红：三条旁路 ────────────────────────────────────────────────

def test_stale_fetched_item_must_not_ship(clean):
    """当次抓取里 100h 前的条目不得进产物 —— 这是 59% 超龄条目的主通道。

    留 4 条窗口内条目是为了让保底 3 条无从兜底：保底只补到 3，不能拿来给超窗条目开后门。
    """
    now = _now()
    src = _source([_item("f%d" % i, 1.0 + i, now) for i in range(4)] +
                  [_item("stale", 100.0, now)])
    got, result, total = _ship([src])
    assert "f0" in got["src_a"], "窗口内的条目被误删，页面只会更空"
    assert "stale" not in got["src_a"], "超窗条目仍然出厂：72h 契约只作用于历史，抓取侧无人过闸"


def test_floor_does_not_become_a_loophole(clean):
    """保底只补到 3 条：4 条窗内 + 5 条超窗时，产物里必须只有那 4 条。

    写成「len(dated) 补到 floor」而不是「无条件留最新 3 条」的实现，会让每个源至少留 3 条
    超龄内容 —— 实测那就是 59% 老条目里最扎眼的那部分。
    """
    now = _now()
    src = _source([_item("f%d" % i, 1.0 + i, now) for i in range(4)] +
                  [_item("o%d" % i, 100.0 + i, now) for i in range(5)])
    got, result, total = _ship([src])
    assert got["src_a"] == ["f0", "f1", "f2", "f3"]


def test_undatable_item_is_dropped_not_kept_alive(clean):
    """无 pub_date 且无 first_seen ⇒ 判不了龄就丢，不能回退成「当前时间」造永生条目。"""
    got, result, total = _ship([_source([_item("", None, pub=False)])])
    assert got["src_a"] == [], "无日期条目按 now 处理会永不过期 —— 这正是无限增长的入口"


def test_epoch_pub_date_not_trusted(clean):
    """0001-01-01 是占位值不是发布时间（线上实测 12 条），既不能当极老也不能当最新。"""
    src = _source([_item("epoch", None, pub=False,
                         pub_date="0001-01-01T00:00:00+00:00")])
    got, result, total = _ship([src])
    assert "epoch" not in got["src_a"], "纪元占位值被当成真实日期参与留存判定"


def test_fresh_capture_without_pub_date_survives_on_first_seen(clean):
    """反向锚点（必须绿）：无 pub_date 但**有** first_seen 的条目是刚收到的，必须留。

    判不了龄的定义是两条时间线索都不可得，不是「pub_date 缺失」。上游确实不给日期的源
    若按前者处理会被整源掏空。
    """
    link = "http://x/nodate"
    mod._rss_history[link] = _hist_item(link, "src_a", 10.0)
    mod._rss_history[link]["pub_date"] = ""
    src = _source([{"link": link, "title": "t", "summary": "s", "pub_date": ""}])
    got, result, total = _ship([src])
    assert link in got["src_a"]


# ── 判龄口径 ───────────────────────────────────────────────────────────

def test_window_uses_tz_corrected_pub_date(clean):
    """声明了 pub_date_offset_min 的源必须按**校正后**的时间判龄。

    不校正则该源比应有的多活 8 小时，且与合并/裁剪两处已有口径不一致。
    抓取的 item 字典里没有 source_key（历史条目才有），所以这条同时锁住「闸门要把
    src["key"] 传进去」—— 少传时偏移永远取不到，且不会有任何报错。
    """
    clean.setattr(mod, "RSS_SOURCES", [{"key": "src_tz", "name": "TZ",
                                        "url": "http://tz/feed",
                                        "pub_date_offset_min": -480, "tier": 1}])
    now = _now()
    src = _source([_item("f%d" % i, 1.0 + i, now) for i in range(4)] +
                  [_item("raw70", 70.0, now)], key="src_tz")
    got, result, total = _ship([src])
    assert got["src_tz"] == ["f0", "f1", "f2", "f3"], \
        "原始 70h、校正后 78h：按未校正值续命了"


def test_age_corrected_exactly_once(clean):
    """同一偏移只能套一次：出口处 item["pub_date"] 可能已被平移过，再套就凭空挪 ±偏移。"""
    link = "http://x/tz2"
    now = _now()
    mod._rss_history[link] = _hist_item(link, "src_tz", 80.0, first_seen_ago=60.0, now=now)
    age = mod._retention_age_h({"link": link, "source_key": "src_tz",
                                "pub_date": _iso(70.0, now)}, {"src_tz": 600}, now)
    # 原始 80h + 校正 10h ⇒ 70h。二次校正给 60h，完全不校正给 80h，两者都该红。
    assert age == pytest.approx(70.0, abs=1.0), "判龄 %.1fh" % age


# ── 源自适应窗口 ───────────────────────────────────────────────────────

def test_sparse_source_does_not_widen(clean):
    """样本 <3 条不放宽（走 72h 基线）：两条间隔不构成「该源本来就慢」的证据。"""
    now = _now()
    items = [_item("a", 200.0, now), _item("b", 210.0, now)]
    assert mod._source_window_h(items, {}) == pytest.approx(72.0)


def test_slow_source_window_hits_hard_cap(clean):
    """周更博客（中位间隔 ~168h）放宽后被 168h 硬上限钳住，不允许更长。"""
    now = _now()
    items = [_item("w%d" % i, 24.0 * 7 * i + 1, now) for i in range(6)]
    assert mod._source_window_h(items, {}) == pytest.approx(168.0)


def test_fast_source_stays_at_baseline(clean):
    """小时级更新的源中位间隔很小 ⇒ 不放宽，仍是 72h 基线。"""
    now = _now()
    items = [_item("f%d" % i, 1.0 * i + 1, now) for i in range(8)]
    assert mod._source_window_h(items, {}) == pytest.approx(72.0)


def test_adaptive_window_widens_midrange_source(clean):
    """中位间隔 50h 的源：2×50=100h ⇒ 窗口 100h，90h 前的条目该留（一刀切 72h 会误删）。"""
    now = _now()
    items = [_item("d%d" % i, 50.0 * i + 1, now) for i in range(6)]
    window = mod._source_window_h(items, {})
    assert window == pytest.approx(100.0, abs=2.0), "自适应窗口 %.1fh" % window
    src = _source(items + [_item("edge", 95.0, now)], key="src_mid")
    got, result, total = _ship([src])
    assert "edge" in got["src_mid"]


# ── 保底与上限（防回归的两条硬线）──────────────────────────────────────

def test_floor_three_keeps_slow_source(clean):
    """保底 3 条：全源都超窗的慢源也要留最新 3 条，否则卡片整块消失。

    实测去掉保底会有 286/938 个源变空 —— 那正是 tests/rss_source_coverage/ 用 blocking
    门禁死守的断崖（历史上「有数据源」从 968 掉到 776 就是同一形态）。
    """
    now = _now()
    src = _source([_item("s%d" % i, 90.0 + i, now) for i in range(6)])
    got, result, total = _ship([src])
    assert len(got["src_a"]) == 3, "保底没生效或过头：应为 3，实为 %d" % len(got["src_a"])
    assert got["src_a"] == ["s0", "s1", "s2"], "保底应取最新的 3 条并保持原相对顺序"


def test_no_source_emptied_when_a_within_bound_item_exists(clean):
    """反向锚点（必须绿）：源里只要有 ≥1 条「可判龄且没超硬上限」的条目，就不该被裁空。"""
    now = _now()
    src = _source([_item("only", 100.0, now)], key="src_one", name="One")
    got, result, total = _ship([src])
    assert got["src_one"] == ["only"], "100h 的单条源被清空，保底没起作用"


def test_floor_never_ships_beyond_the_hard_ceiling(clean):
    """保底不得越过 168h 硬上限：停更源就该出厂 0 条，而不是塞一条 4 年前的旧文。

    线上实测不设这道限的后果：产物里多出 1,012 条 >168h 内容（最老 41,791h ≈ 4.7 年的 CNN
    旧文，散在 426 个源上）—— 与被判死的「无日期回退成 now」是同一个 bug 换了件衣服。
    """
    now = _now()
    src = _source([_item("dead%d" % i, 300.0 + i, now) for i in range(6)],
                  key="src_dead", name="Dead")
    got, result, total = _ship([src])
    assert got["src_dead"] == [], "停更源靠超龄内容续命：72 小时契约被保底掏空"


def test_floor_pads_only_within_the_bound(clean):
    """保底只在龄限内补：2 条窗内 + 3 条超硬上限时，产物只能是那 2 条。"""
    now = _now()
    src = _source([_item("n0", 10.0, now), _item("n1", 12.0, now)] +
                  [_item("z%d" % i, 400.0 + i, now) for i in range(3)],
                  key="src_pad", name="Pad")
    got, result, total = _ship([src])
    assert got["src_pad"] == ["n0", "n1"]


def test_cap_per_source(clean):
    """每源上限：高频源一次抓 200 条全在窗内时封顶 120 条（留最新）。"""
    now = _now()
    items = [_item("c%03d" % i, 1.0 + i * 0.1, now) for i in range(200)]
    got, result, total = _ship([_source(items, key="src_many", name="Many")])
    kept = got["src_many"]
    assert len(kept) == 120, "无上限时单源可占满产物，全站的可见性压在一个源上"
    assert kept == ["c%03d" % i for i in range(120)], "封顶应留最新 120 条且保持原顺序"


def test_gate_keeps_fields_and_original_order(clean):
    """闸门只删条目：不改字段、不改顺序（首屏分块按顺序取前 CHUNK0_SIZE 条）。"""
    now = _now()
    src = _source([_item("k1", 2.0, now, title_zh="甲", image="http://i/1.png"),
                   _item("k2", 5.0, now),
                   _item("gone", 100.0, now),
                   _item("k3", 8.0, now)])
    result, total = mod._accumulate_history([src])
    items = result[0]["items"]
    assert [it["link"] for it in items] == ["k1", "k2", "k3"], "顺序被打乱或误删窗口内条目"
    assert items[0]["title_zh"] == "甲" and items[0]["image"] == "http://i/1.png"


def test_history_and_fetch_same_link_not_duplicated(clean):
    """同链接去重不得被闸门绕过（历史上出现过重复卡，`Fix 2` 那段守卫就是防它的）。"""
    link = "http://x/dup"
    mod._rss_history[link] = _hist_item(link, "src_a", 5.0)
    src = _source([{"link": link, "title": "t", "summary": "s", "pub_date": _iso(5.0)}])
    got, result, total = _ship([src])
    assert got["src_a"].count(link) == 1


def test_total_matches_shipped_items(clean):
    """返回值 total 必须等于闸门**之后**的条数：html 头部条数与实际卡片数不能双标。"""
    now = _now()
    src = _source([_item("m%d" % i, 1.0 + i, now) for i in range(5)] +
                  [_item("gone", 300.0, now)])
    result, total = mod._accumulate_history([src])
    assert total == sum(len(s.get("items", [])) for s in result)
    assert total == 5


# ── 接线：闸门必须作用在返回值上，且三个消费者同口径 ─────────────────────

def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _assigned_by_gate(fn, func_name="_apply_retention"):
    """`x, y = _apply_retention(...)` 左侧出现的名字 —— 闸门结果的落点。"""
    out = set()
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call) and \
                getattr(stmt.value.func, "id", "") == func_name:
            for t in stmt.targets:
                out |= _names(t)
    return out


def _transitive_gate_names(fn, func_name="_apply_retention"):
    """闸门结果经过的所有别名。`r, _ = _apply_retention(); result = r` 也算闸门输出。

    只认单跳会把「多改一次名」这种语义等价重构判成假红 —— 假红的下场是检查被人顺手删掉。
    """
    known = set(_assigned_by_gate(fn, func_name))
    while True:
        grown = set(known)
        for stmt in ast.walk(fn):
            if not isinstance(stmt, ast.Assign):
                continue
            tgt = set()
            for t in stmt.targets:
                tgt |= _names(t)
            if tgt and (tgt & known) and (_names(stmt.value) & known):
                grown |= tgt
        if grown == known:
            return known
        known = grown


def test_gate_is_wired_into_accumulate_return(clean):
    """AST 锁调用点：`_accumulate_history` 返回前必须调用 `_apply_retention` 且返回它的结果。

    只看「函数存在」是假绿：函数可以写得完全正确但没人调，或调完把结果丢掉 ——
    两种都会退化成空转（用户看到的老条目一条不少）。
    """
    tree = ast.parse(io.open(BUILD, encoding="utf-8").read())
    fn = _func(tree, "_accumulate_history")
    assert fn is not None
    calls = [c for c in ast.walk(fn)
             if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_apply_retention"]
    assert calls, "闸门没被调用：_apply_retention 是死代码"
    rets = [r for r in ast.walk(fn) if isinstance(r, ast.Return) and r.value is not None]
    assert rets, "_accumulate_history 没有返回值"
    returned = set()
    for r in rets:
        returned |= _names(r.value)
    assert _transitive_gate_names(fn) & returned, "闸门结果没有流向 return（返回的仍是未过滤的原列表）"
    call_line = min(c.lineno for c in calls)
    assert call_line < min(r.lineno for r in rets), "调用写在 return 之后，永远不会执行"
    # total 也必须在闸门之后算：否则 html 头部写 6 条、页面只有 3 条（对抗审查 P2-C 指出
    # 原来只查 return 顺序，把 total 挪到闸门之前即可绕过）。
    stores = [n.lineno for n in ast.walk(fn)
              if isinstance(n, ast.Name) and n.id == "total" and isinstance(n.ctx, ast.Store)]
    assert stores, "_accumulate_history 里没有给 total 赋值？检查对象已失效"
    assert min(stores) > call_line, "total 在闸门之前算，条数会与出厂内容双标"


def test_all_consumers_share_one_gate(clean):
    """快照 / 页面 / 分块三个消费者必须吃同一个过闸后的列表，否则「页面裁了 API 没裁」。

    只查这一件事，且不锁变量名形状（`r, _s = ...; result = r` 这种语义等价的写法不该判红）。
    消费者内部会不会绕开形参另取数据，由下面的 test_snapshot_items_equal_gated_items 用数据证。
    """
    tree = ast.parse(io.open(BUILD, encoding="utf-8").read())
    main = _func(tree, "main")
    assert main is not None, "找不到 main()：接线断言会退化成永远绿的空检查"
    gated = _transitive_gate_names(main, "_accumulate_history")
    consumers = {}
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            fid = getattr(node.func, "id", "")
            if fid in ("_save_api_snapshot", "write_data_chunks", "build_html"):
                consumers[fid] = _names(node.args[0]) if node.args else set()
    assert len(consumers) == 3, "消费者少了一个：新增出口或改名都要在这里显式对齐"
    for fid, args in sorted(consumers.items()):
        assert args & gated, "%s 用的不是 _accumulate_history 的返回值 → 双标口径" % fid


def test_refactor_renaming_the_return_value_is_not_false_red(clean):
    """反向锚点（必须绿）：语义等价的两步改写不该被判红。

    AST 接线检查很容易写成「锁名字形状」：那种实现会在别人做正常重构时假红，
    然后被人顺手删掉 —— 检查也就跟着没了。
    """
    src = ("def _accumulate_history(x):\n"
           "    r, _s = _apply_retention(x, {}, None)\n"
           "    result = r\n"
           "    return result, 1\n")
    tree = ast.parse(src)
    fn = _func(tree, "_accumulate_history")
    assert _transitive_gate_names(fn) & {"_s", "r", "result"}
    assert _assigned_by_gate(fn) & {"r"}


def test_snapshot_items_equal_gated_items(clean):
    """数据级同口径断言：AST 只看形状，拦不住「形参收了过闸列表、函数体另从 _rss_history 取数」。"""
    import json
    links = ["http://x/s1", "http://x/s2", "http://x/s3"]
    # 历史里必须放一条**不在过闸结果里**的链接：否则"快照自己去读 _rss_history"这种实现
    # 与正确实现产出完全相同，测试就成了装饰（变异体 R20 第一次就是这么活下来的）。
    mod._rss_history = {"http://x/ghost": _hist_item("http://x/ghost", "src_snap", 5.0)}
    gated = [{"key": "src_snap", "name": "Snap", "cat": "ai", "color": "#fff", "tier": 1,
              "items": [{"link": l, "u": l, "t": "t" + l, "s": "s", "d": _iso(2.0),
                         "title": "t" + l, "summary": "s", "pub_date": _iso(2.0)}
                        for l in links]}]
    mod._save_api_snapshot(gated)
    found = set()
    for fn in os.listdir("."):
        if not fn.startswith("rss_api_snapshot") or not fn.endswith(".json"):
            continue
        obj = json.loads(io.open(fn, encoding="utf-8").read())
        for s in obj.get("sources", []):
            for it in s.get("items", []):
                found.add(it.get("u"))
    assert found == set(links), "快照与闸门输出的链接集合不一致：%s" % sorted(found ^ set(links))


def test_build_log_carries_post_gate_counts(clean):
    """构建日志必须带闸门**之后**的计数（对抗审查 P0-1）。

    `sources_with_data` 与 `per_source` 取的都是抓取阶段的数：实测 961 源里 238 个出厂 0 条，
    而构建日志纹丝不动。T3 层塌了四分之一却没有任何观测面 —— 与 2026-09-16
    「有数据源 968→776」是同一种不可见塌陷。「门禁不会变红」不是好消息，是问题本身。
    """
    tree = ast.parse(io.open(BUILD, encoding="utf-8").read())
    main = _func(tree, "main")
    keys = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "append":
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    for k in arg.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
    for need in ("sources_after_gate", "sources_empty_after_gate", "items_after_gate",
                 "items_le_base_h", "items_base_to_hard_h", "items_over_hard_h"):
        assert need in keys, "构建日志缺 %s：闸门效果只落在产物里，CI 日志上看不出来" % need
    src = io.open(BUILD, encoding="utf-8").read()
    # 这六个字段不能用 0 兜底：闸门没跑到的构建（早退/异常路径）会记成 0，
    # 而 0 又正好是"闸门把内容全裁光"的样子 —— 两种故障在日志里长一样，等于没有观测面。
    for getter in ('get("sources_after", 0)', 'get("after", 0)', 'get("le72", 0)',
                   'get("mid", 0)', 'get("over", 0)'):
        assert getter not in src, "闸后字段退回 0 兜底了：%s" % getter
    assert '_LAST_RETENTION_STATS.get("after")' in src, "items_after_gate 不再读闸后统计"


def test_breakdown_line_pins_three_shipped_age_buckets(clean, capsys):
    """龄期分项必须是真数：验收靠构建日志，日志里的数字不能是写死的 0。

    线上产物试跑暴露的正是这条 —— 不分项的话「>72h 从 59% 降到 18.6%」这一堆里
    混着 1,012 条 >168h 的旧文，光看总数看不出来。
    """
    now = _now()
    src = _source([_item("a", 1.0, now), _item("b", 2.0, now),
                   _item("mid", 100.0, now), _item("far", 400.0, now)],
                  key="src_bk", name="Bk")
    mod._accumulate_history([src])
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "出厂龄期分项" in l]
    assert line, "没有输出龄期分项行：验收只能靠构建日志，不能靠本地一次性脚本"
    assert "≤72h 2 条" in line[0], line[0]
    assert "72-168h 1 条" in line[0], line[0]
    assert ">168h 0 条" in line[0], line[0]


# 2026-10-06 修同名遮蔽：这一条曾与上面那条**同名**（两个 def test_breakdown_line_reports_real_numbers），
# Python 只绑定后定义的 ⇒ 前一条从未被 pytest 收集，且远端 main 上就是这个形状（31 个 def、实跑 30 个）。
# 前面那条的三条断言今天照旧成立（当场重放过），所以保留并改名，两条一起跑。
def test_breakdown_line_reports_real_numbers(clean, capsys):
    """龄期分项必须是真数：验收靠构建日志，日志里的数字不能是写死的 0。"""
    now = _now()
    src = _source([_item("r%d" % i, 1.0 + i, now) for i in range(3)] +
                  [_item("old%d" % i, 200.0 + i, now) for i in range(4)],
                  key="src_rep", name="Rep")
    mod._accumulate_history([src])
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "出厂龄期分项" in l]
    assert line, "没有输出龄期分项行：验收只能靠构建日志，不能靠本地一次性脚本"
    # 只认「数字 + 条」这一种可机器解析的形态，不锁阈值/写法；顺序就是分项顺序。
    nums = [int(x) for x in re.findall(r"(\d+)\s*(?:条|items?|件|個|个)", line[0])]
    assert len(nums) >= 3, "分项行解析不出三段数字：%s" % line[0]
    assert nums[0] == 3, "分项第一段应是 3 条，实为 %d：%s" % (nums[0], line[0])
    assert nums[2] == 0, "超硬上限桶被保底填了旧文：%s" % line[0]


def test_emptied_sources_are_named_with_their_own_two_rulers(clean):
    """被闸门清空的源要**带名字进台账**，并各自附两个读数：最新一条的龄期、它拿到的窗口。

    只有计数时，「这场清空 256 个源」之后必然接着问「哪 256 个」，而答案只有 stderr 前 10 名
    （每场被覆盖）+ 一次手工探针 —— 10-05 定案 12 个 megaphone 源就是这么来的：名单只活在
    一次性脚本里，下一场构建就又没人知道了。
    `newest_age_h` 与 `window_h` 并排，是为了当场分开两种完全不同的病：
      上游真没货（最新 400h、窗 72h ⇒ 交信源清单健康度跟进）
      还是只差一点（最新 80h、窗 72h ⇒ 该看窗口算法本身）。
    """
    now = _now()
    dead = _source([_item("d1", 500.0, now), _item("d2", 400.0, now)],
                   key="src_dead", name="停更源")
    blind = _source([_item("u1", None, now, pub=False)], key="src_blind", name="无日期源")
    live = _source([_item("l1", 1.0, now)], key="src_live", name="正常源")
    _, stats = mod._apply_retention([dead, blind, live], None, now)

    assert stats["emptied"] == ["停更源", "无日期源"], (
        "名单与既有 emptied 字段不同批 ⇒ 计数和名单会各说一套")
    rows = {r["key"]: r for r in stats["emptied_detail"]}
    assert set(rows) == {"src_dead", "src_blind"}, (
        "名单里的源与 emptied 对不上：两个面各算各的，日志会自相矛盾")

    r = rows["src_dead"]
    assert r["name"] == "停更源" and r["n_items"] == 2
    assert r["newest_age_h"] == 400.0, (
        "newest_age_h 必须是**最新**那条的龄期（400h）而不是最旧（500h）："
        "写成 max 就永远看不出这个源其实两天前刚更新过")
    assert r["window_h"] == 72.0, "样本不足 3 条时窗口退回基线 72h，读数要跟着这个口径"
    assert r["undatable"] == 0

    b = rows["src_blind"]
    assert b["newest_age_h"] is None and b["undatable"] == 1, (
        "判不了龄的源必须能看出是「判不了」而不是「没货」：newest_age_h=None + undatable=1；"
        "否则它和停更源在日志里长得一模一样，而这两件事的处置完全相反")


def test_emptied_roster_is_written_into_build_log_not_only_counted():
    """台账字段必须写**名单本身**，不能退化成 `len(名单)`。

    与上一条是一套：上一条验「名单算得出来」，这一条验「名单送得出仓库」。
    只验前者的话，把字段改成计数照样全绿 —— 而「计数已有、名单没有」正是这次要修的形状。
    """
    txt = io.open(BUILD, encoding="utf-8").read()
    assert '"sources_empty_detail"' in txt, "闸后清空名单没落进构建日志字段"
    assert '_LAST_RETENTION_STATS.get("emptied_detail")' in txt, (
        "sources_empty_detail 不再读闸后统计的名单面：退化成计数等于没有观测面")
    # None 与 [] 必须可区分（沿用那六个闸后字段同一条规矩）：
    # [] = 这场没有一个源被清空，None = 闸门没跑到（早退/异常路径）。
    assert '"sources_empty_detail": _LAST_RETENTION_STATS["emptied_detail"]' not in txt, (
        "sources_empty_detail 用下标直取：闸门没跑到的那场会 KeyError 崩掉整场构建，"
        "而不是把「这一步没跑到」如实记成 None")


def test_breakdown_uses_shipped_date_not_judged_date(clean):
    """分项必须按**出厂那串日期**分桶（用户看到 24h 就不能被算进 72-168h）。

    判龄走原始 pub_date + 偏移，出厂值可能是降级键，两者可以差一个偏移量。
    """
    item = {"link": "http://x/disp", "pub_date": _iso(24.0), "date_fallback": True}
    mod._rss_history[item["link"]] = {
        "link": item["link"], "source_key": "src_rep", "cat": "ai", "color": "#fff",
        "source": "Rep", "title": "t", "summary": "s", "title_zh": "", "summary_zh": "",
        "full_content": "", "image": "", "media_url": "", "media_type": "",
        "pub_date": _iso(80.0), "first_seen": _iso(80.0)}
    src = [{"key": "src_rep", "name": "Rep", "cat": "ai", "color": "#fff", "tier": 1,
            "items": [item]}]
    out, stats = mod._apply_retention(src, {}, _now())
    assert stats["over"] == 0
    assert stats["mid"] == 0, "分项按判定龄取数，与外显日期分家"
    assert stats["le72"] == 1, "外显 24h 的条目没进 <=72h 桶"


def test_history_reap_swallower_is_fixed(clean):
    """出口闸门之外，老 prune 的「判不了龄→拿 now 兜底→永不删除」也必须补上。

    只补出口那一刀是不够的：这种条目会被每轮 prune 重新续期，然后下一轮又原样进 src_map，
    线上会长期存在"外显很久、判定永远 0h"的内容。
    """
    item = {"link": "http://x/swallow", "pub_date": "", "first_seen": ""}
    mod._rss_history = {item["link"]: dict(item, source_key="src_a", source="A", cat="ai",
                                           color="#fff", title="t", summary="s",
                                           title_zh="", summary_zh="", full_content="",
                                           image="", media_url="", media_type="")}
    assert mod._retention_age_h(dict(item, source_key="src_a"), {}, _now()) is None
    fresh = {"link": "http://x/fresh", "title": "f", "summary": "s", "pub_date": _iso(1.0)}
    src = _source([fresh], key="src_a", name="A")
    before = len(mod._rss_history)
    mod._accumulate_history([src])
    assert item["link"] not in mod._rss_history, \
        "prune 对两条时间线索都不可得的条目永久跳过（pd_bj 用 now 兜底）"
    assert len(mod._rss_history) >= before


def test_epoch_items_count_as_undatable(clean, capsys):
    """纪元占位值必须计入"判不了龄"，而不是被当成"极老"或"最新"。

    这条盯的是统计口径：如果纪元值被算成普通超龄条目，`判不了龄` 数会永远显示 0，
    那 5 个全源判不了龄的缺陷源就再也浮不上来。
    """
    now = _now()
    src = _source([_item("e1", None, pub=False, pub_date="0001-01-01T00:00:00+00:00"),
                   _item("k1", 2.0, now), _item("k2", 3.0, now),
                   _item("k3", 4.0, now), _item("k4", 5.0, now)],
                  key="src_ep", name="Ep")
    mod._accumulate_history([src])
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "出口闸门" in l]
    assert line, "没有播报出口闸门统计"
    assert "判不了龄 1" in line[0], "纪元值没被计入判不了龄：%s" % line[0]


def test_retention_constants_are_sane(clean):
    """常量偏序是设计的一部分：基线 ≤ 硬上限窗口、0 < 保底 < 上限、系数 > 1。

    写成断言而不是注释，是因为这几个数被人顺手改一下就会静默造出空源或让产物重新膨胀。
    """
    assert mod.RETENTION_BASE_H == mod.RSS_HISTORY_HOURS
    assert mod.RETENTION_MAX_WINDOW_H > mod.RETENTION_BASE_H
    assert 0 < mod.RETENTION_FLOOR < mod.RETENTION_MAX_ITEMS
    assert mod.RETENTION_COEF > 1.0
    # 保底与放宽共用同一条硬上限：两者不一致就会出现「放宽到 7 天但保底能留 4 年」的裂缝
    assert mod.RETENTION_FLOOR_MAX_H == mod.RETENTION_MAX_WINDOW_H


def test_gate_reports_its_effect(clean, capsys):
    """闸门必须自我播报：静默裁掉六成条目却不留痕，事后无法归因（本仓库栽过两次）。"""
    now = _now()
    src = _source([_item("r%d" % i, 1.0 + i, now) for i in range(3)] +
                  [_item("old%d" % i, 200.0 + i, now) for i in range(4)],
                  key="src_rep", name="Rep")
    mod._accumulate_history([src])
    out = capsys.readouterr().out
    assert "[留存]" in out, "闸门没有输出任何可核对的口径行"
    assert re.search(r"留 \d+ 条", out), "播报里没有留下条数"
