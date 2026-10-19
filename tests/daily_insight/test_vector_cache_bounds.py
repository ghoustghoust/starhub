# -*- coding: utf-8 -*-
"""向量缓存合并处的淘汰与封顶（任务 #29，2026-09-21）。

现场（run 35566639074，14:13 场）：
    数据汇总: RSS 12244, 热榜 400, AIHOT 100, AGI Hunt 2245      ← RSS 占输入 82%
    增量合并: 旧 33485 + 新增 19125 = 52610 (去重后)
    FAISS 索引构建: 33797 向量
    meta.bm25_window = {non_rss:33697, rss_total:100, rss_budget_slots:0}
原实现 `build_daily_insight.py:5206-5214` 有三个问题，且**上限那一支自己没生效**：
    budget = MAX_EMBED_CHUNKS - len(other_mc)   # 30000 - 33697 = -3697
    if budget < 100: budget = 100               # RSS 从此钉死在 100 条
    merged = rss_mc + other_mc                  # 100 + 33697 = 33797 > 30000，封顶形同虚设
非RSS 一支从头到尾没有任何淘汰（同日 7 场 non_rss 单调 31840→33697，约 +370/场），
这与用户最初提的"不能无限增长、总有一天会撑爆"是同一缺陷族，只是长在洞察侧。

口径：RSS 优先按新鲜度保留，剩余名额给非RSS 按新鲜度填；两边都按 pub_date 淘汰超龄，
判不了龄的一律丢弃（与 :5130 的 `_hours_ago(None)==999 > 168` 现有边界一致，
所以无日期条目本来也进不了洞察 —— 沿用同一规则，不新造一类永生条目）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B


def _mk(htype, idx, age_h, text=None):
    """造一个 chunk。age_h = 距今小时数；传 None 表示"判不了龄"（pub_date 空串）。"""
    from datetime import datetime, timedelta, timezone
    BJT = timezone(timedelta(hours=8))
    # 先算 pub 再判 age_h 会在 timedelta(hours=None) 上直接炸 —— 这就是"造数必须照出厂形状"
    pub = (datetime.now(BJT) - timedelta(hours=age_h)).isoformat() if age_h is not None else ""
    h = "h%s_%06d" % (htype[0], idx)
    return {"chunk_id": "c_" + h, "content_hash": h, "source_type": htype,
            "title": "t" + h, "pub_date": pub,
            "text": text or ("body " + h)}


def _split(merged):
    rss = [c for c in merged if c.get("source_type") == "rss"]
    other = [c for c in merged if c.get("source_type") != "rss"]
    return rss, other


def _age_h(c):
    """测试自己算龄期：生产里的 `_hours_ago` 依赖 main() 才初始化的 NOW_BJ，
    在测试上下文里是 None，会炸成 TypeError 而不是暴露判据问题。"""
    from datetime import datetime, timedelta, timezone
    dt = B._parse_iso(c.get("pub_date", ""))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return (datetime.now(timezone(timedelta(hours=8))) - dt).total_seconds() / 3600.0


class TestMergeContract:
    def test_helper_exists_and_is_called(self):
        """新逻辑必须是可测函数，且真的被构建调用（防写成死函数/空转）。"""
        assert hasattr(B, "_merge_vector_cache"), \
            "合并逻辑仍内联在构建主流程里，无法被断言覆盖"
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert src.count("_merge_vector_cache(") >= 2, \
            "_merge_vector_cache 只有定义没有调用点 ⇒ 判据锁的是死代码"

    def test_old_amputation_branch_is_gone(self):
        """`budget < 100 → 100` 这一支必须整体消失，不能只是被绕过。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "if budget < 100:" not in src, \
            "旧兜底仍在：非RSS 撑满时 RSS 会被钉死在 100 条"
        assert "rss_mc[:budget]" not in src, "旧的 RSS 单支截断仍在"


class TestRssNotStarved:
    def test_rss_survives_a_crowded_non_rss_pool(self):
        """本判据就是今天那个故障的最小复现：12,244 RSS + 33,697 非RSS。"""
        old = [_mk("hot", i, 1) for i in range(33697)]
        new = [_mk("rss", i, 1) for i in range(12244)]
        merged, st = B._merge_vector_cache(old, new)
        rss, other = _split(merged)
        assert len(merged) <= B.MAX_EMBED_CHUNKS, \
            "封顶没生效：合并后 %d 条 > 上限 %d" % (len(merged), B.MAX_EMBED_CHUNKS)
        assert len(rss) > 1000, (
            "RSS 只剩 %d 条 —— 与今天的 100 条同一种饥饿，非RSS 撑满时 RSS 必须按新鲜度保住份额"
            % len(rss))

    def test_cap_is_enforced_even_when_non_rss_exceeds_it(self):
        old = [_mk("agihunt", i, 1) for i in range(40000)]
        new = [_mk("rss", i, 1) for i in range(500)]
        merged, st = B._merge_vector_cache(old, new)
        assert len(merged) == B.MAX_EMBED_CHUNKS, \
            "非RSS 超上限时结果应为恰好封顶，实际 %d" % len(merged)
        assert len(st.get("dropped_hashes") or []) > 0, "必须报告丢了哪些，否则淘汰不可核对"


class TestEvictionActuallyHappens:
    def test_stale_non_rss_is_evicted_and_counted(self):
        """造数必须越过阈值并自证机制动手了：只比键集合不算证明淘汰发生。"""
        stale = [_mk("hot", i, 30 * 24) for i in range(500)]      # 30 天前
        fresh = [_mk("hot", 900000 + i, 2) for i in range(500)]   # 2 小时前
        merged, st = B._merge_vector_cache(stale, fresh)
        n_other = len(_split(merged)[1])
        assert n_other == 500, (
            "陈旧非RSS 没被淘汰：合并后仍有 %d 条（应只剩 500 条新鲜的）" % n_other)
        assert st["evicted_stale"] >= 500, "evicted_stale 计数必须真实发生，实际 %s" % st.get("evicted_stale")

    def test_rss_window_is_the_168h_it_declares(self):
        """RSS 用 168h，不能悄悄跟非RSS 共用一个窗口。"""
        old_rss = [_mk("rss", i, 20 * 24) for i in range(300)]    # 20 天 > 168h
        fresh_rss = [_mk("rss", 800000 + i, 3) for i in range(50)]
        merged, st = B._merge_vector_cache(old_rss, fresh_rss)
        rss, _ = _split(merged)
        assert len(rss) == 50, "超 168h 的 RSS 应被淘汰，实际留下 %d 条" % len(rss)

    def test_undated_chunks_are_dropped_not_kept_forever(self):
        """无日期条目一律丢弃：留着就是当初"无日期回退成 now"那类永生条目的复活。"""
        undated = [_mk("hot", i, None) for i in range(40)]
        fresh = [_mk("rss", 700000 + i, 1) for i in range(10)]
        merged, st = B._merge_vector_cache(undated, fresh)
        assert all(c.get("pub_date") for c in merged), "合并结果里仍有无日期 chunk ⇒ 会永生"
        assert st.get("dropped_undated", 0) >= 40, \
            "判不了龄的数量必须单独计数上报，否则淘汰原因不可归因"


class TestDedupAndAlignment:
    def test_today_version_wins_over_stale_cache_copy(self):
        a_old = _mk("rss", 1, 5)
        a_old["title"] = "旧标题"
        a_new = _mk("rss", 1, 1)
        a_new["title"] = "新标题"
        merged, st = B._merge_vector_cache([a_old], [a_new])
        assert len(merged) == 1, "同一 content_hash 出现两次 ⇒ 去重没做"
        assert merged[0]["title"] == "新标题", "必须保留本场版本，不能保留旧缓存副本"

    def test_dropped_hashes_are_the_exact_complement(self):
        """淘汰清单要能与留存清单逐条对上，防止"统计说丢了、实际没丢"。"""
        old = [_mk("hot", i, 40 * 24 if i % 2 else 1) for i in range(200)]
        merged, st = B._merge_vector_cache(old, [])
        kept = {c["content_hash"] for c in merged}
        dropped = set(st["dropped_hashes"])
        assert not (kept & dropped), "同一 hash 既在留存又在淘汰 ⇒ 统计是编的"
        assert len(kept) + len(dropped) == 200, \
            "留存 %d + 淘汰 %d != 输入 200 ⇒ 有 chunk 静默消失" % (len(kept), len(dropped))

    def test_row_assembly_stays_aligned_after_eviction(self):
        """淘汰必须不破坏 chunk↔向量对齐：装配要按 content_hash 取行，不能按位置。

        这是改这个函数最容易出的事故 —— 一旦按位置切片，检索会拿错向量，
        而且**不会报错**（FAISS 只看形状），比现在的问题更隐蔽。
        """
        old = [_mk("hot", i, 30 * 24 if i < 100 else 1) for i in range(150)]
        merged, st = B._merge_vector_cache(old, [])
        kept = {c["content_hash"] for c in merged}
        assert len(kept) == len(merged) == 50
        # 装配出的向量数必须与留存 chunk 数严格相等
        vec_rows = [i for i, c in enumerate(old) if c["content_hash"] in kept]
        assert len(vec_rows) == len(merged), "向量行号与留存 chunk 数不等 ⇒ 会错位"
        assert all(old[i]["content_hash"] in kept for i in vec_rows), "行号指向了被淘汰的 chunk"


class TestNoSideStarved:
    """两个方向都要锁。只锁 RSS 会把偏见翻到另一边：本期 12 个事件 12/12 来自 agihunt，
    非RSS 一旦被清零，报告就直接退化成一个 RSS 摘要器。"""

    def test_minuscule_non_rss_side_is_not_rounded_away(self):
        rss = [_mk("rss", i, 1) for i in range(10000)]
        other = [_mk("hot", 1, 1)]
        merged, st = B._merge_vector_cache([], rss + other, cap=1000)
        assert st["kept_other"] == 1, (
            "非RSS 只有 1 条时被比例取整抹成 0 ⇒ 保底额没生效（kept_other=%d）"
            % st["kept_other"])
        assert len(merged) == 1000, "省下的名额必须回填给另一侧，实际留了 %d" % len(merged)

    def test_backfill_does_not_break_the_cap(self):
        rss = [_mk("rss", i, 1) for i in range(50)]
        other = [_mk("aihot", i, 1) for i in range(100000)]
        merged, st = B._merge_vector_cache([], rss + other, cap=30000)
        assert len(merged) == 30000, "回填后仍须严格封顶，实际 %d" % len(merged)
        assert st["kept_rss"] >= 1 and st["kept_other"] >= 1



class TestTrimOrder:
    def test_trim_under_pressure_keeps_the_newest(self):
        """名额不够时必须丢最旧的 —— 方向反过来（留最旧）在别的判据里看不出来。

        这条是推演变异体时补的：只断言"总数不超上限"或"淘汰计数>0"都抓不到排序反向，
        而反向的后果恰恰是池子塞满陈年旧文、本场新内容进不来，与今天的故障同形。
        """
        n = B.MAX_EMBED_CHUNKS + 8000
        # 全部落在 48h 窗口内（最大 40h），确保"少下来的那些"只能是上限截断造成的，
        # 不是超龄淘汰 —— 否则这条判据在测两件不同的事，失败时无法归因。
        old = [_mk("hot", i, 40.0 * i / n) for i in range(n)]
        merged, st = B._merge_vector_cache(old, [])
        assert len(merged) == B.MAX_EMBED_CHUNKS, \
            "留存 %d 条，应为恰好封顶 %d 条" % (len(merged), B.MAX_EMBED_CHUNKS)
        assert st["evicted_stale"] == 0, "本例不该触发超龄淘汰，实际 %d" % st["evicted_stale"]
        kept_age = [_age_h(c) for c in merged]
        dropped = set(st["dropped_hashes"])
        dropped_age = [40.0 * i / n for i in range(n) if ("hh_%06d" % i) in dropped]
        assert len(dropped_age) == n - B.MAX_EMBED_CHUNKS
        assert max(kept_age) < min(dropped_age), (
            "留存里最旧的（%.2fh）不比被淘汰里最新的（%.2fh）更新 ⇒ 截断方向反了"
            % (max(kept_age), min(dropped_age)))


class TestEmbedBudget:
    """每场新增 embedding 的预算闸。

    起因：预算上线后第一场实测（run 35597071115）
      12:19:14 需新增 embedding 21488 → 12:52:49 才嵌完（33.5 分钟），
      整场超 60 分钟被下一场 cancel-in-progress 取消 ⇒ 缓存存不下来 ⇒ 每场重嵌 ⇒ 站点停止更新。
    """

    def test_budget_constant_is_sized_by_measured_throughput(self):
        assert B.MAX_NEW_EMBED_PER_BUILD > 0
        # 实测 ≈10.7 条/秒 ⇒ 6,000 条 ≈ 9.3 分钟。留够整场余量，不许拍一个天文数字回去。
        assert B.MAX_NEW_EMBED_PER_BUILD <= 6000, (
            "预算 %d 条按实测吞吐要 %.1f 分钟，仍可能撞取消" % (
                B.MAX_NEW_EMBED_PER_BUILD, B.MAX_NEW_EMBED_PER_BUILD / 10.7))

    def test_uncached_are_capped_and_cached_are_untouched(self):
        cached = [_mk("rss", i, 1) for i in range(500)]
        fresh = [_mk("hot", 500000 + i, 1) for i in range(4000)]
        merged, st = B._merge_vector_cache(cached, fresh, embed_budget=1000)
        assert st["uncached_now"] == 1000, "预算没生效：本轮仍要嵌 %d 条" % st["uncached_now"]
        assert st["deferred_uncached"] == 3000
        kept_hashes = {c["content_hash"] for c in merged}
        assert all(c["content_hash"] in kept_hashes for c in cached), \
            "预算把已有向量的条目也挤掉了 ⇒ 白白重嵌"

    def test_budget_defers_the_oldest_uncached_first(self):
        """被推迟的必须是较旧的。上一版这里写成 `ages[-1] < 100`，太松 —— 变异体"留最旧"
        照样通过（最旧 50 条的龄期恰好也在那个界内），所以改成逐条比对身份。
        """
        fresh = [_mk("hot", 600000 + i, i * 0.5) for i in range(200)]   # i 越大越旧
        merged, st = B._merge_vector_cache([], fresh, embed_budget=50)
        assert st["uncached_now"] == 50
        kept = {c["content_hash"] for c in merged}
        # 最新 50 条 = i∈[0,49]，其 hash 由 idx 唯一决定
        expect = {"h" + "h" + "_%06d" % (600000 + i) for i in range(50)}
        assert kept == expect, (
            "留下的不是最新 50 条；实际龄期 %s ⇒ 预算在推迟最新内容"
            % sorted(int(h.split('_')[-1]) - 600000 for h in kept)[:5])
        assert max(_age_h(c) for c in merged) < 25.0

    def test_production_call_site_passes_the_budget(self):
        """光有常量和默认值不算接通：调用点不传 embed_budget，闸门就是装饰品。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "embed_budget=MAX_NEW_EMBED_PER_BUILD" in src, \
            "_merge_vector_cache 的调用点没传预算 ⇒ 预算只在测试里生效，生产仍会重嵌两万条"


    def test_warmup_converges_so_it_is_not_permanent_starvation(self):
        """预算只能是"推迟"，不能变成"永远进不来"：下一场它们已在缓存里，必须全部命中。"""
        fresh = [_mk("rss", i, 1) for i in range(300)]
        m1, s1 = B._merge_vector_cache([], fresh, embed_budget=100)
        assert s1["uncached_now"] == 100 and s1["deferred_uncached"] == 200
        # 第二场：上一场留下的 merged 就是新的 old_chunks，且 300 条仍然在场
        m2, s2 = B._merge_vector_cache(m1, fresh, embed_budget=100)
        assert s2["uncached_now"] <= 100
        assert s2["deferred_uncached"] == 0 or s2["kept"] >= s1["kept"], \
            "收敛失败：已在缓存里的条目又被推迟了 %s" % s2["deferred_uncached"]

    def test_no_zero_vector_placeholder_left(self):
        """零向量占位必须消失：它与 merged 数量一致，形状检查发现不了，只会静默污染检索。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "_np.zeros(EMBED_DIM" not in src, "组装向量时仍在塞零向量占位"


class TestAccountingConservation:
    """条数守恒：读日志的人必须能把手上的数加回同一个分母（任务 #32 的核心一条）。

    现场（run 35609254121，14:21 场）日志印成
    `→ 留存 10040（…｜ RSS 21802 + 非RSS 7271，上限 30000）`
    —— 21802+7271=29,073 是**预算闸之前**的额度分配，10,040 是之后的 kept，
    两个分母混在同一行。我自己第一读就把"RSS 只剩 1 万"当成新缺陷去查，白花时间；
    而 `kept_rss/kept_other` 这名字叫"留下了多少"，值却是"截断前分到多少额度"，
    任何下游消费者拿它做判断都会错。
    """

    def _fixtures(self):
        # 一个 fixture 要让五个桶全部非零，否则守恒等式在"什么都没发生"上恒真
        old = ([_mk("rss", i, 10) for i in range(30)]
               + [_mk("rss", 900 + i, None) for i in range(5)]      # 判不了龄
               + [_mk("rss", 950 + i, 500) for i in range(5)])     # 超龄
        new = ([_mk("rss", 100 + i, 1) for i in range(60)]
               + [_mk("hot", i, 1) for i in range(40)]
               # 跨构建重复：hash 只由 type+idx 决定，这 10 条与 old 里的撞键。
               # 没有这一组的话，pool_size 写错（例如退回两侧相加）也能让下面的
               # 守恒等式成立 —— 我在 CM2 变异体上实测到它只被去重那条用例杀掉。
               + [_mk("rss", i, 1) for i in range(10, 20)]
               + [_mk("hot", 800 + i, None) for i in range(5)])
        return old, new

    def _scenario(self):
        old, new = self._fixtures()
        merged, st = B._merge_vector_cache(old, new, cap=80, embed_budget=20)
        return merged, st

    def test_split_sums_to_kept(self):
        merged, st = self._scenario()
        assert st["kept"] == len(merged)
        assert st["kept_rss"] + st["kept_other"] == st["kept"], (
            "kept_rss/kept_other 必须是**预算闸之后**的实际留存分项，"
            "否则日志里 kept 与两边分项加不回去（本轮 kept=%s rss=%s other=%s）"
            % (st["kept"], st["kept_rss"], st["kept_other"]))

    def test_every_chunk_is_accounted_once(self):
        merged, st = self._scenario()
        for k in ("dropped_undated", "evicted_stale", "dropped_over_cap",
                  "deferred_uncached", "kept", "dedup_removed"):
            assert st[k] > 0, "fixture 没让 %s 动起来，守恒等式会在空集上恒真" % k
        total = (st["kept"] + st["deferred_uncached"] + st["dropped_over_cap"]
                 + st["evicted_stale"] + st["dropped_undated"])
        assert total == st["pool_size"], (
            "条数不守恒：去重后池子 %s 条，各桶相加 %s 条（各桶 %s）" % (
                st["pool_size"], total,
                {k: st[k] for k in ("kept", "deferred_uncached", "dropped_over_cap",
                                    "evicted_stale", "dropped_undated")}))
        # 分母必须拿**独立算出来的**期望去比：`pool == 旧+新-去重` 是实现的定义式，
        # 断它等于自摸（2026-09-21 对抗审查 I4 命中）。
        expected_pool = len({c["content_hash"] for c in sum(self._fixtures(), [])})
        assert st["pool_size"] == expected_pool, (
            "去重后池子 %s，按 fixture 独立算应是 %s" % (st["pool_size"], expected_pool))
        assert st["dedup_removed"] == st["input_old"] + st["input_new"] - expected_pool

    def test_hashless_records_are_not_counted_as_dedup(self):
        """没有 content_hash 的记录是"进不了缓存"，不许混进"去重"里。

        混计的代价是守恒等式看着成立、实际把一类数据丢失藏进去重数里 ——
        而"去重多少"是会被当正常数看的桶。
        """
        bad = _mk("rss", 0, 1)
        bad = dict(bad)
        bad["content_hash"] = ""
        merged, st = B._merge_vector_cache([bad], [_mk("rss", 1, 1)])
        assert st["dropped_no_hash"] == 1, st
        assert st["dedup_removed"] == 0, (
            "无 hash 的记录被算成去重了：%s" % st["dedup_removed"])
        assert (st["kept"] + st["dropped_no_hash"] + st["dedup_removed"]
                + st["dropped_undated"] + st["evicted_stale"] + st["dropped_over_cap"]
                + st["deferred_uncached"] == st["input_old"] + st["input_new"]), st

    def test_dedup_is_reported_separately(self):
        """同一 content_hash 在两侧都出现时必须算作一条，且差额可核对。"""
        shared = _mk("rss", 0, 10)
        merged, st = B._merge_vector_cache([shared], [dict(shared)], embed_budget=None)
        assert st["pool_size"] == 1, (
            "去重后池子算成 %s 条 ⇒ 守恒等式的分母就是错的" % st["pool_size"])
        assert st["dedup_removed"] == 1

    def test_cap_zero_keeps_nothing_and_still_balances(self):
        """cap=0 时 `rss[:-1]` 这种负数切片会**留下**几乎全部内容（第二轮审查 M1）。

        生产不可达（上限是 30000），但它说明"上限"这个参数在极端输入下会反向生效：
        越小留越多。这类形状一旦被误配置就是静默的无限增长。
        """
        new = [_mk("rss", i, 1) for i in range(10)] + [_mk("hot", i, 1) for i in range(10)]
        merged, st = B._merge_vector_cache([], new, cap=0, embed_budget=None)
        assert st["kept"] == 0, "cap=0 却留下 %d 条（负数切片的典型症状）" % st["kept"]
        assert len(merged) == 0
        self._assert_balances(st)

    def test_cap_smaller_than_floor_does_not_wipe_one_side(self):
        """cap=1 且两侧都有内容时：不许出现负数名额把一侧整个清掉、也不许多留。"""
        new = [_mk("rss", i, 1) for i in range(20)] + [_mk("hot", i, 1) for i in range(20)]
        merged, st = B._merge_vector_cache([], new, cap=1, embed_budget=None)
        assert st["kept"] == 1, "cap=1 却留了 %d 条" % st["kept"]
        assert st["kept_rss"] + st["kept_other"] == 1
        self._assert_balances(st)

    def test_negative_budget_defers_everything(self):
        """embed_budget 传成负数时不许"比 0 更宽松"（`uncached[:-5]` 会留下几乎全部）。"""
        new = [_mk("rss", i, 1) for i in range(10)]
        merged, st = B._merge_vector_cache([], new, cap=100, embed_budget=-5)
        assert st["kept"] == 0, "负预算却嵌了 %d 条" % st["uncached_now"]
        assert st["deferred_uncached"] == 10, st
        self._assert_balances(st)

    def _assert_balances(self, st):
        total = (st["kept"] + st["deferred_uncached"] + st["dropped_over_cap"]
                 + st["evicted_stale"] + st["dropped_undated"] + st["dropped_no_hash"])
        assert total == st["pool_size"], (
            "极端参数下条数不守恒：池 %s vs 各桶相加 %s（%s）" % (
                st["pool_size"], total, {k: st[k] for k in (
                    "kept", "deferred_uncached", "dropped_over_cap", "evicted_stale",
                    "dropped_undated", "dropped_no_hash")}))

    def test_merge_log_prints_the_reconciling_denominator(self):
        """日志必须印 `pool_size` 这个分母，否则守恒只活在测试里。

        锁整句而不是锁字面量：上一版印的是 input_old + input_new（未去重），
        数字看着齐全却永远加不回去 —— 这正是 14:21 场让我误判一场的原因。
        """
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        i = src.index("向量缓存合并")
        stmt = src[i:src.index("% (", i)]
        assert '去重后池 %d 条' in stmt, (
            "合并日志的分母没写成去重后池子，四个丢弃桶加不回去；当前句子：%s"
            % stmt.replace("\n", " ")[:160])
        args = src[src.index("% (", i): i + 1500]
        assert '_vc["pool_size"]' in args and '_vc["kept"]' in args, (
            "合并日志没引用 pool_size/kept ⇒ stats 修对了但印出来的还是旧口径")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
