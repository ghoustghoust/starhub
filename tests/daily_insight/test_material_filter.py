# -*- coding: utf-8 -*-
"""FIX-4: Phase 2 前素材相关性过滤测试。"""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class TestFilterClusterItems(unittest.TestCase):
    """_filter_cluster_items 应过滤与 label 不相关的 items。"""

    def test_relevant_items_kept(self):
        """与 label 有 token 重叠的 items 应保留。"""
        cluster = {"label": "Claude Design 重写", "items": [
            {"title": "Claude Design 被 Anthropic 重写", "text": "相关内容"},
            {"title": "OpenAI 发布新模型", "text": "完全不同的主题"},
        ]}
        result = B._filter_cluster_items(cluster, "Claude Design 重写")
        titles = [it["title"] for it in result["items"]]
        self.assertIn("Claude Design 被 Anthropic 重写", titles)

    def test_irrelevant_items_removed(self):
        """与 label 无 token 重叠的 items 应被移除。"""
        cluster = {"label": "Claude Design 重写", "items": [
            {"title": "Claude Design 被 Anthropic 重写", "text": "相关内容"},
            {"title": "量子计算重大突破", "text": "完全不相关的主题"},
        ]}
        result = B._filter_cluster_items(cluster, "Claude Design 重写")
        titles = [it["title"] for it in result["items"]]
        self.assertNotIn("量子计算重大突破", titles)

    def test_at_least_one_item_kept(self):
        """即使所有 items 都不相关，至少保留 1 个。"""
        cluster = {"label": "AI 新闻", "items": [
            {"title": "量子计算突破", "text": "无关内容A"},
            {"title": "区块链新高", "text": "无关内容B"},
        ]}
        result = B._filter_cluster_items(cluster, "AI 新闻")
        self.assertGreaterEqual(len(result["items"]), 1)

    def test_empty_label_no_filter(self):
        """空 label 不过滤。"""
        cluster = {"label": "", "items": [
            {"title": "A", "text": "x"},
            {"title": "B", "text": "y"},
        ]}
        result = B._filter_cluster_items(cluster, "")
        self.assertEqual(len(result["items"]), 2)

    def test_text_content_used_for_matching(self):
        """title 无重叠但 text 有重叠 → 保留。"""
        cluster = {"label": "GPT-6 发布", "items": [
            {"title": "某新闻标题", "text": "关于 GPT-6 的详细报道"},
        ]}
        result = B._filter_cluster_items(cluster, "GPT-6 发布")
        self.assertEqual(len(result["items"]), 1)


if __name__ == "__main__":
    unittest.main()
