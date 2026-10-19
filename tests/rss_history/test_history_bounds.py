# tests/rss_history/test_history_bounds.py
# -*- coding: utf-8 -*-
"""缓存侧的体积口径：**只有 72h 时间窗是约束，窗内内容不许按条数淘汰**。

为什么把"每源上限 400 条"撤掉（这是生产实测推翻设计，不是我觉得不好）：
1. 时间窗自己就把体积框住了。淘汰上线前的 93 场构建里，历史是
   16,069 → 11,756 条，平均斜率 **-46.9 条/场**（在往下走），根本没有单调爬升。
   我之前看到的 +230/场 只是窗口填装期的暂态，拿它当趋势是错的。
2. 条数上限不省体积，只是持续丢弃窗内内容。淘汰上线后 10 场斜率反而变成
   **+65.7 条/场**（被腾空的名额又填回来了），同期累计淘汰 2,231 条 ——
   也就是说这道刀切掉的量最后又被补回来，换来的是窗内条目被永久换掉。
3. 上限切掉的几乎全是窗内条目。淘汰首落地那场切 1,547 条，而同一场真正的过期裁剪只有 21 条。

所以本文件的判据转向两件事：**窗内的必须留下、过期的必须出去**，
体积侧的报警面（深度警戒线 + 最老龄期）已于 2026-09-21 退役（理由见台账 §18），
本文件因此只守三件事：窗内不删、过期必删、账目只有一列且不许兜底。
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
SRC = os.path.join(ROOT, "build_rss_aggregator.py")
# 已被撤销的"按条数淘汰"机制的符号清单：删就要删干净（py_compile 查不出 NameError）
REMOVED_SYMBOLS = ("HISTORY_MAX_PER_SOURCE", "_bound_history_per_source",
                   "_hist_recency_key", "_RECENCY_FLOOR", "_LAST_HISTORY_BOUND",
                   "history_evicted_per_source",
                   # 2026-09-21 退役的深度哨兵：条数上限撤销后它只剩"报一个不会动的数"，
                   # 而 oldest_age_h 在裁剪与闸门共用一只表之后恒等于把 cutoff 念一遍。
                   "HISTORY_WATCH_PER_SOURCE", "_history_depth_watch",
                   "history_max_per_source", "history_over_watch", "history_oldest_age_h")


@pytest.fixture
def clean(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mod._rss_history = {}
    mod._LAST_HISTORY_ACCOUNT.clear()
    mod._LAST_UNRELIABLE_SRCS = set()
    monkeypatch.setattr(mod, "RSS_SOURCES", [])
    return monkeypatch


def _hist_entry(link, key, hours_ago, first_seen_ago=None, pub_date=None):
    now = mod._now_bj()
    fs = first_seen_ago if first_seen_ago is not None else hours_ago
    return {
        "link": link, "source": key, "source_key": key, "cat": "ai", "color": "#fff",
        "title": "t " + link, "summary": "s", "title_zh": "", "summary_zh": "",
        "full_content": "", "image": "", "media_url": "", "media_type": "",
        "pub_date": pub_date if pub_date is not None
        else (now - datetime.timedelta(hours=hours_ago)).isoformat(),
        "first_seen": (now - datetime.timedelta(hours=fs)).isoformat(),
    }


def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _accumulate(hist, key="bulk", items=None):
    mod._rss_history = dict(hist)
    res, _total = mod._accumulate_history(
        [{"key": key, "name": key, "cat": "ai", "color": "#fff", "tier": 1,
          "items": list(items or [])}])
    return res


def _source_hist(key, n, base_age=1.0, step=0.04):
    return {"http://%s/%04d" % (key, i): _hist_entry("http://%s/%04d" % (key, i), key,
                                                     base_age + i * step)
            for i in range(n)}


def test_in_window_entries_are_never_cut_by_count(clean):
    """根因判据：一个源在 72h 窗内堆到警戒线以上（2400 条），也必须 2400 条全留。

    这条正是被撤销的那道"每源上限 400"打红的形态。造数必须**越过警戒线**：
    不越线的话，"哨兵顺手把超出部分删掉"这种实现照样能骗过全部断言
    （变异体 W1 第一版就这么活了下来 —— 计数在删除之前算，报出来的深度还是真的）。
    """
    hist = _source_hist("heavy", 2400, step=0.02)      # 龄期 1h .. 49h，全在窗内
    _accumulate(hist, key="heavy")
    left = len(mod._rss_history)
    assert left == 2400, "窗内条目被按条数淘汰了（%d < 2400）：时间窗才是唯一体积约束" % left


def test_stale_entries_still_leave_even_when_a_source_is_huge(clean):
    """撤掉条数上限不等于撤掉清理：900 条里 300 条已过 72h，那 300 条必须出去。"""
    hist = {}
    for i in range(300):
        l = "http://mix/old%03d" % i
        hist[l] = _hist_entry(l, "mix", 80.0 + i * 0.1)
    for i in range(600):
        l = "http://mix/new%04d" % i
        hist[l] = _hist_entry(l, "mix", 1.0 + i * 0.05)
    _accumulate(hist, key="mix")
    left = set(mod._rss_history)
    assert not [k for k in left if "/old" in k], "过期条目留下了（%d 条）—— 清理被撤掉了" % (
        len([k for k in left if '/old' in k]))
    assert len(left) == 600


def test_expired_account_counts_only_stale_entries(clean, capsys):
    """"过期账"只统计真过期的条目，且必须非负。

    生产实测旧口径 `history_before - history_after` 全天 12 场恒为负数（-264/-201/…），
    因为每场新抓进来的比删掉的多 —— 那本账从来没存在过。

    造数必须**同时带当次抓取条目**（对抗审查 P1-2 指出原判据的漏洞）：
    原来 5 个调用点全走 items 默认空，new_count 恒为 0，于是
    `pruned_expired = before - len(history) + new_count` 这种"把新增混进过期账"的写法
    照样能骗过断言 —— 而那正是 §12 那笔负数账的同一族错误。
    """
    hist = {}
    for i in range(120):
        l = "http://exp/old%03d" % i
        hist[l] = _hist_entry(l, "exp", 80.0 + i * 0.1)
    hist.update(_source_hist("exp", 500, base_age=1.0, step=0.05))
    # 当次新抓 5 条：它们既不在"裁剪前"里，也不该算进过期账
    fresh = [{"title": "new %d" % i, "link": "http://exp/new%d" % i, "summary": "s",
              # 真实出厂形状：翻译阶段已把 pub_date 转成 ISO 串（datetime 进不了 json）
              "pub_date": (mod._now_bj() - datetime.timedelta(hours=1 + i)).isoformat()}
             for i in range(5)]
    mod._LAST_HISTORY_ACCOUNT.clear()
    _accumulate(hist, key="exp", items=fresh)
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "篇过期" in l]
    assert line, "没打印过期裁剪数：%r" % out[-300:]
    n = int(line[0].split("裁剪 ")[1].split(" 篇过期")[0])
    assert n == 120, "「过期裁剪」数不对（%d ≠ 120）：%s" % (n, line[0])
    assert mod._LAST_HISTORY_ACCOUNT.get("expired") == 120, "日志账不对或被新增条目污染"


def test_accumulate_history_never_mutates_entry_fields(clean):
    """合并/裁剪这条遍历必须只读：给条目加一个键都会跟着 _rss_history 存盘，
    再经 `entry = dict(item)` 进出厂 payload。

    这条判据原来挂在已退役的深度哨兵上（test_watch_does_not_mutate_entry_fields）。
    哨兵删掉之后，风险并没有跟着消失 —— 审计遍历 `_accumulate_history` 里那句
    `item["watch_seen"] = 1` 照样能污染数据，而当时全仓 128 条判据没有一条会红
    （对抗审查 P1-1）。所以把判据搬到还在的那条路径上，而不是删了了事。
    """
    import copy
    hist = {}
    for i in range(30):
        l = "http://ro/keep%02d" % i
        hist[l] = _hist_entry(l, "ro", 1.0 + i * 0.1)
    hist["http://ro/stale"] = _hist_entry("http://ro/stale", "ro", 90.0)
    snapshot = copy.deepcopy(hist)
    mod._rss_history = hist
    items = [{"title": "t new", "link": "http://ro/fresh", "summary": "s",
              "pub_date": mod._now_bj().isoformat()}]
    mod._accumulate_history([{"key": "ro", "name": "ro", "cat": "ai", "color": "#fff",
                              "tier": 1, "items": list(items)}])
    for link, before in snapshot.items():
        after = mod._rss_history.get(link)
        if after is None:
            continue        # 被裁掉的那条不在此列
        assert set(after) == set(before), (
            "条目字段被改了：多 %s 缺 %s" % (
                sorted(set(after) - set(before)), sorted(set(before) - set(after))))
        for k in before:
            assert after[k] == before[k], "字段 %s 的值被就地改写：%r -> %r" % (
                k, before[k], after[k])


def test_removed_count_cap_leaves_no_dangling_symbols(clean):
    """撤销机制要按符号清单在**入库文件**里断言清除：留半个引用就是运行时 NameError。

    只查 git ls-files：一次性变异脚本（_mut_*.py）不入库，不该算残留；
    本文件自己带着符号清单（REMOVED_SYMBOLS），也要跳过。台账（docs/）保留历史叙述。
    """
    import subprocess
    raw = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT,
                         capture_output=True).stdout
    hits = []
    names = [n for n in raw.split(bytes([0])) if n]
    assert names, "git ls-files 返回空 ⇒ 本用例是空跑（隔离树里没有 index 就会这样）"
    assert len(names) > 50, "扫到的入库文件只有 %d 个，不像真仓库，判据视为无效" % len(names)
    for name in raw.split(bytes([0])):
        if not name:
            continue
        rel = name.decode('utf-8', 'replace').replace(chr(92), '/')
        if rel.startswith('docs/') or rel == 'tests/rss_history/test_history_bounds.py':
            continue
        if not rel.endswith(('.py', '.js', '.html', '.yml', '.yaml')):
            continue
        text = io.open(os.path.join(ROOT, rel), encoding='utf-8', errors='ignore').read()
        for sym in REMOVED_SYMBOLS:
            if sym in text:
                hits.append('%s::%s' % (rel, sym))
    assert not hits, "还有入库文件引用被撤销的条数上限机制：%s" % hits[:8]


def test_build_log_keeps_only_the_expired_account(clean):
    """日志里的历史账只剩"真过期多少"，三条深度账必须已经消失。

    为什么反过来断言：撤销一个观测面时，只删机制不删字段会留下**永远为 None 的假账**，
    下一个人读到 None 分不清是"没跑到"还是"这格本来就没东西"。所以字段本身也要清掉。
    同时保留两条老约束：不许 .get(k, 默认) 与 `or 0` 兜底（审查 F5/F6 钻的就是这两个空子）。
    """
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    main = _fn(tree, "main")
    assert main is not None
    pairs = {}
    for node in ast.walk(main):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and \
                    k.value.startswith("history_"):
                pairs[k.value] = v
    for gone in ("history_evicted_per_source", "history_max_per_source",
                 "history_over_watch", "history_oldest_age_h"):
        assert gone not in pairs, "%s 还在日志里，机制没撤干净（现有 %s）" % (gone, sorted(pairs))
    assert "history_expired" in pairs, "日志缺 history_expired（现有 %s）" % sorted(pairs)
    fn = _fn(tree, "_accumulate_history")
    written = set()
    for st in ast.walk(fn):
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Subscript) and \
                        getattr(t.value, "id", "") == "_LAST_HISTORY_ACCOUNT" and \
                        isinstance(t.slice, ast.Constant):
                    written.add(t.slice.value)
    assert written == {"expired"}, "缓存侧还在写多条历史账：%s" % sorted(written)
    val = pairs["history_expired"]
    keys = {c.value for c in ast.walk(val) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert keys & written, "history_expired 读的 key %s 与写入侧 %s 对不上" % (
        sorted(keys), sorted(written))
    for call in ast.walk(val):
        if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "get":
            assert len(call.args) == 1, "history_expired 用 .get(key, 默认值) 兜底"
        if isinstance(call, ast.BoolOp) and isinstance(call.op, ast.Or):
            assert not any(isinstance(v, ast.Constant) and v.value == 0
                           for v in call.values), "history_expired 用 `or 0` 兜底"


def test_falsified_future_dates_leave_the_cache_at_72h(clean):
    """判过期必须和出口闸门用同一只表。

    造一条 pub_date 在未来、first_seen 已在 100h 前的条目（上游时钟错乱 + 我方按收录
    降级判伪的那种）：旧口径只看 pub_date ⇒ 它"还年轻"永远留在缓存里，而闸门每次都丢它
    —— 真实重放实测缓存里留着的最老条目真龄 140.1h。
    """
    now = mod._now_bj()
    l = "http://clk/falsified"
    e = _hist_entry(l, "clk", 100.0)
    e["pub_date"] = (now + datetime.timedelta(hours=50)).isoformat()   # 不可能的未来日期
    e["first_seen"] = (now - datetime.timedelta(hours=100)).isoformat()
    hist = {l: e}
    hist.update(_source_hist("clk", 5, step=0.5))
    _accumulate(hist, key="clk")
    assert l not in mod._rss_history, (
        "被证伪的未来日期条目靠假日期赖在缓存里（真龄 100h > 72h）")
    assert len(mod._rss_history) == 5


def test_prune_and_gate_share_one_clock(clean):
    """缓存里不该留着"闸门一定会丢"的条目：两个口径的差集必须为空。

    混合三类：真龄 10h 正常、真龄 80h 正常（超 72h 应出缓存）、真龄 100h 且 pub_date 是
    未来假日期（旧口径会留在缓存）。断言只剩第一条，且出厂集合与缓存集合一致。
    """
    now = mod._now_bj()
    hist = {}
    for name, age, fake in (("fresh", 10.0, False), ("stale", 80.0, False),
                            ("falsified", 100.0, True)):
        l = "http://two/%s" % name
        e = _hist_entry(l, "two", age)
        if fake:
            e["pub_date"] = (now + datetime.timedelta(hours=40)).isoformat()
            e["first_seen"] = (now - datetime.timedelta(hours=age)).isoformat()
        hist[l] = e
    res = _accumulate(hist, key="two")
    assert set(mod._rss_history) == {"http://two/fresh"}, (
        "缓存留下了闸门会丢的条目：%s" % sorted(mod._rss_history))
    shipped = {i["link"] for i in res[0]["items"]}
    assert shipped == {"http://two/fresh"}, "出厂与缓存不同一份数据：%s" % sorted(shipped)


def test_undated_fresh_item_ships_capture_time(clean):
    """当次抓到的无日期条目，出厂必须带收录时刻并打降级标记，且不许改写历史里的原始值。

    生产实测（台账 §17 + 本场复测）：出口有 9 条卡片 pub_date 与 time_str 双空，
    连续两场是**同一批链接**（100% 重叠），所以不是"本轮新增、下场自愈"。
    根因在 _accumulate_history 里那句 continue：降级值写在历史条目上，
    而同链接当次又抓到时走的是当次那份字典 —— 它从没被兜过底。

    第二个断言守的是既有不变量：_retention_ref_dt 的推导前提是
    "历史里没有 pub_date 就等于出口那份是降级键"，
    所以修复只能改出厂副本，绝不能把收录时刻写回历史的 pub_date
    （写了就等于把收录时间当发布时间去套源偏移，二次校正）。
    """
    now = mod._now_bj()
    link = "http://nofb/undated-1"
    fresh = [{"title": "无日期的一条", "link": link, "summary": "s",
              "pub_date": "", "image": ""}]
    mod._rss_history = {}
    res, _total = mod._accumulate_history(
        [{"key": "nofb", "name": "nofb", "cat": "ai", "color": "#fff",
          "tier": 1, "items": list(fresh)}])
    shipped = [i for i in res[0]["items"] if i.get("link") == link]
    assert shipped, "当次条目没出厂（res=%r）" % (res[0]["items"],)
    it = shipped[0]
    assert (it.get("pub_date") or "").strip(), (
        "出厂 pub_date 仍是空的：这张卡片在页面上没有任何时间可显示")
    assert it.get("date_fallback"), "降级值必须打标记，前端据此显示「收录」而不是冒充发布时间"
    fs = (mod._rss_history.get(link) or {}).get("first_seen") or ""
    assert fs, "历史里没写 first_seen，降级值无从取值"
    assert str(it["pub_date"])[:16] == str(fs)[:16], (
        "出厂时间应当是收录时刻：出厂 %r vs first_seen %r" % (it["pub_date"], fs))
    assert not (mod._rss_history[link].get("pub_date") or "").strip(), (
        "历史的 pub_date 被改写成收录时刻了 —— _retention_ref_dt 的推导前提会被破坏")


def test_dated_fresh_item_is_left_alone(clean):
    """防修过头：有真实发布日期的条目不许被盖上收录时刻。

    兜底一旦写成无条件赋值，所有条目都会变成"刚收录"，首屏时间排序整体失真 ——
    形态与 2026-09-14 用户报的"同刻扎堆"同源。
    """
    now = mod._now_bj()
    real = (now - datetime.timedelta(hours=20)).replace(microsecond=0)
    link = "http://nofb/dated-1"
    mod._rss_history = {}
    res, _total = mod._accumulate_history(
        [{"key": "nofb", "name": "nofb", "cat": "ai", "color": "#fff",
          "tier": 1, "items": [{"title": "有日期", "link": link, "summary": "s",
                                  "pub_date": real.isoformat(), "image": ""}]}])
    it = [i for i in res[0]["items"] if i.get("link") == link][0]
    assert str(it.get("pub_date"))[:19] == real.isoformat()[:19], (
        "有真实日期的条目被兜底改写了：%r != %r" % (it.get("pub_date"), real.isoformat()))
    assert not it.get("date_fallback"), "有日期的条目不该带降级标记"
