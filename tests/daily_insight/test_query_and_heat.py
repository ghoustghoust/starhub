# -*- coding: utf-8 -*-
"""T1.3: 混合查询构建测试 + T1.5: 热榜热度信号测试。"""
import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class TestQueryBuilder(unittest.TestCase):
    """混合查询应包含多源标题，避免单一源偏倚。"""

    def test_query_includes_hot_titles(self):
        """热榜标题应出现在查询中。"""
        hot_items = [
            {"title": "微博热搜: AI 写代码", "source": "weibo", "hot": 100},
            {"title": "知乎热榜: GPT-6", "source": "zhihu", "hot": 90},
        ]
        agihunt = []
        aihot = []
        queries = B._build_queries(hot_items, agihunt, aihot, retrieved_chunks=[])
        hot_titles = [q for q in queries if "微博" in q or "知乎" in q]
        self.assertGreater(len(hot_titles), 0, "热榜标题应出现在查询中")

    def test_query_includes_agihunt_titles(self):
        """AGI Hunt 标题应出现在查询中。"""
        agihunt = [{"title": "OpenAI GPT-6", "hot": 100}]
        queries = B._build_queries([], agihunt, [], retrieved_chunks=[])
        self.assertTrue(any("GPT-6" in q for q in queries))

    def test_query_includes_pseudo_queries(self):
        """已检索 chunk 应生成伪查询。"""
        chunks = [
            {"title": "Claude Agent SDK 深度评测", "final_score": 0.9},
            {"title": "Gemini 多模态架构分析", "final_score": 0.8},
        ]
        queries = B._build_queries([], [], [], retrieved_chunks=chunks)
        pseudo = [q for q in queries if "Claude" in q or "Gemini" in q]
        self.assertGreater(len(pseudo), 0, "已检索 chunk 应生成伪查询")

    def test_query_no_duplication(self):
        """查询应去重。"""
        hot = [{"title": "GPT-6 发布", "source": "weibo", "hot": 100}]
        agihunt = [{"title": "GPT-6 发布", "hot": 90}]
        queries = B._build_queries(hot, agihunt, [], retrieved_chunks=[])
        # 相同标题不应重复
        self.assertEqual(len(queries), len(set(queries)))

    def test_query_max_limit(self):
        """查询总数不超过上限。"""
        hot = [{"title": "热榜 %d" % i, "source": "weibo", "hot": 100 - i} for i in range(50)]
        agihunt = [{"title": "AGI %d" % i, "hot": 100 - i} for i in range(50)]
        aihot = [{"title": "AIHOT %d" % i, "hot": 100 - i} for i in range(50)]
        queries = B._build_queries(hot, agihunt, aihot, retrieved_chunks=[])
        self.assertLessEqual(len(queries), B.MAX_QUERIES)

    def test_query_fallback_when_no_sources(self):
        """无 AGI Hunt/AIHOT 时降级到热榜。"""
        hot = [{"title": "微博热搜", "source": "weibo", "hot": 100}]
        queries = B._build_queries(hot, [], [], retrieved_chunks=[])
        self.assertGreater(len(queries), 0)


class TestHeatSignal(unittest.TestCase):
    """热榜热度信号应按平台 Tier 加权。"""

    def _make_cluster(self, hot_platforms):
        """构造一个含热榜 items 的 cluster。"""
        items = []
        for p in hot_platforms:
            items.append({"source_type": "hot", "source": p, "title": "test",
                          "final_score": 0.5, "pub_date": B.NOW_BJ.isoformat()})
        return {
            "items": items,
            "source_types": {"hot"},
            "best_score": 0.5,
        }

    def test_heat_nonzero_with_hot_items(self):
        """包含热榜 items 的事件 heat > 0。"""
        cluster = self._make_cluster(["weibo", "zhihu"])
        B._score_events([cluster], {})
        self.assertGreater(cluster["signal"]["heat"], 0)

    def test_heat_scales_with_platforms(self):
        """更多平台 = 更高 heat。"""
        c1 = self._make_cluster(["weibo"])
        c2 = self._make_cluster(["weibo", "zhihu", "bilibili"])
        B._score_events([c1, c2], {})
        self.assertGreater(c2["signal"]["heat"], c1["signal"]["heat"])

    def test_heat_t1_boost(self):
        """Tier 1 平台（微博/知乎/B站）比 Tier 3 有更高 heat。"""
        c_t1 = self._make_cluster(["weibo"])  # T1
        c_t3 = self._make_cluster(["unknown_platform"])  # T3
        B._score_events([c_t1, c_t3], {})
        self.assertGreater(c_t1["signal"]["heat"], c_t3["signal"]["heat"])

    def test_heat_zero_without_hot(self):
        """无热榜 items 时 heat = 0。"""
        cluster = {
            "items": [{"source_type": "rss", "source": "test", "title": "t",
                        "final_score": 0.5, "pub_date": B.NOW_BJ.isoformat()}],
            "source_types": {"rss"},
            "best_score": 0.5,
        }
        B._score_events([cluster], {})
        self.assertEqual(cluster["signal"]["heat"], 0)


if __name__ == "__main__":
    unittest.main()


class TestTimezoneRobustness:
    """CI run 35349753348 崩溃回归：mixed naive/aware pub_dates → max() TypeError。"""

    def test_parse_iso_naive_becomes_aware_bjt(self):
        dt = B._parse_iso("2026-09-18T10:00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 8 * 3600

    def test_parse_iso_aware_preserved(self):
        dt = B._parse_iso("2026-09-18T10:00:00+00:00")
        assert dt.utcoffset().total_seconds() == 0

    def test_score_events_mixed_tz_no_crash(self):
        import datetime as _dt
        if B.NOW_BJ is None:
            B.NOW_BJ = B._now_bj()
        # 相对时间构造：防 recency 衰减导致未来某日 score 归零假红
        naive_iso = (B.NOW_BJ - _dt.timedelta(hours=11)).replace(tzinfo=None).isoformat()
        aware_iso = (B.NOW_BJ - _dt.timedelta(hours=9)).isoformat()
        clusters = [{
            "label": "混合时区事件", "items": [
                {"pub_date": naive_iso, "source_type": "rss"},
                {"pub_date": aware_iso, "source_type": "hot"},
            ],
            "source_types": {"rss", "hot"}, "best_score": 1.0, "status": "new",
        }]
        out = B._score_events(clusters, [])
        assert len(out) == 1 and out[0]["score"] > 0
