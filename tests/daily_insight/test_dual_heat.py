# -*- coding: utf-8 -*-
"""FIX-1+5: 双参照系热度模型测试。"""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class TestDualHeat(unittest.TestCase):
    """_compute_dual_heat 应聚合三层信号并归一化。"""

    def test_agihunt_heat_contributes_score_a(self):
        """AGI Hunt hot 值应贡献 score_a。"""
        cluster = {"label": "GPT-6 发布", "items": [
            {"source_type": "agihunt", "hot": 200, "title": "GPT-6"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertGreater(sa, 0, "AGI Hunt hot 应贡献 score_a")
        self.assertGreater(heat, 0)

    def test_aihot_score_contributes_score_a(self):
        """AIHOT score 应贡献 score_a。"""
        cluster = {"label": "AI 新闻", "items": [
            {"source_type": "aihot", "score": 60, "title": "AI news"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertGreater(sa, 0)

    def test_hot_platform_contributes_score_b(self):
        """热榜平台应贡献 score_b。"""
        cluster = {"label": "AI 热搜", "items": [
            {"source_type": "hot", "source": "weibo", "title": "AI"},
            {"source_type": "hot", "source": "zhihu", "title": "AI"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertGreater(sb, 0, "热榜平台应贡献 score_b")
        self.assertIn("weibo", plats)

    def test_breakout_resonance_both_high(self):
        """双高 → breakout。"""
        cluster = {"label": "AI 大事件", "items": [
            {"source_type": "agihunt", "hot": 300, "title": "AI big"},
            {"source_type": "hot", "source": "weibo", "title": "AI"},
            {"source_type": "hot", "source": "zhihu", "title": "AI"},
            {"source_type": "hot", "source": "baidu", "title": "AI"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertEqual(res, "breakout")

    def test_cap_prevents_domination(self):
        """单个子信号 cap 到 5 分，防止垄断。"""
        cluster = {"label": "test", "items": [
            {"source_type": "agihunt", "hot": 1000, "title": "x"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertLessEqual(sa, 10.0, "score_a 应 cap 到 10")

    def test_snapshot_reverse_lookup(self):
        """热榜 chunks 不在簇中时，应反查 hot_snapshot。"""
        hot_snapshot = [
            {"platform": "weibo", "items": [
                {"title": "GPT-6 发布震撼全球", "rank": 1, "hot": 999},
            ]},
        ]
        cluster = {"label": "GPT-6 发布引发讨论", "items": [
            {"source_type": "rss", "title": "GPT-6 发布了",
             "text": "GPT-6 发布", "source_key": "test"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, hot_snapshot)
        self.assertIn("weibo", plats, "应通过反查发现 weibo 热榜")

    def test_no_signals_zero_heat(self):
        """无任何信号源 → heat=0, resonance=""。"""
        cluster = {"label": "冷门事件", "items": [
            {"source_type": "rss", "title": "小更新", "source_key": "obscure"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertEqual(heat, 0.0)
        self.assertEqual(res, "")

    def test_rss_tier_contributes_score_b(self):
        """RSS tier 应贡献 score_b。"""
        B._RSS_TIER_MAP = {"top_source": 1}
        cluster = {"label": "重要报道", "items": [
            {"source_type": "rss", "title": "深度分析",
             "source_key": "top_source"},
        ]}
        heat, sa, sb, plats, res = B._compute_dual_heat(cluster, [])
        self.assertGreater(sb, 0, "RSS T1 源应贡献 score_b")


if __name__ == "__main__":
    unittest.main()
