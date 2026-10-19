# -*- coding: utf-8 -*-
"""T1.1: 语义事件聚类测试。
通过 mock _embed_chunks 返回预设向量，验证聚类逻辑。
"""
import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

B.NOW_BJ = B._now_bj()


def _make_chunk(title, source_type="rss", source="test", score=0.5, text=""):
    return {
        "title": title,
        "text": text or title,
        "source_type": source_type,
        "source": source,
        "channel": "ai",
        "final_score": score,
        "pub_date": B.NOW_BJ.isoformat(),
    }


def _make_vec(label, dim=64):
    """根据标签生成伪向量。相似标签产生相近向量。"""
    import hashlib
    h = hashlib.md5(label.encode()).hexdigest()
    vec = [0.0] * dim
    for i in range(0, min(len(h), dim * 2), 2):
        idx = int(h[i:i+2], 16) % dim
        vec[idx] += 1.0
    # 归一化
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _similar_vecs(base_label, noise_labels, dim=64):
    """生成一组向量：第一个是基准，其余与基准高度相似（余弦 > 0.85）。"""
    import random
    base = _make_vec(base_label, dim)
    vecs = [base[:]]
    for nl in noise_labels:
        v = base[:]
        # 添加少量噪声
        rng = random.Random(nl)
        for i in range(dim):
            v[i] += rng.gauss(0, 0.1)
        # 重新归一化
        norm = sum(x * x for x in v) ** 0.5
        if norm > 0:
            v = [x / norm for x in v]
        vecs.append(v)
    # 确保不相似标签的向量与基准差异大
    return vecs


class TestSemanticClustering(unittest.TestCase):
    """语义聚类应替代 Jaccard 字符串匹配。"""

    def setUp(self):
        self.orig_embed = B._embed_chunks
        self.orig_faiss = B.FAISS_AVAILABLE

    def tearDown(self):
        B._embed_chunks = self.orig_embed
        B.FAISS_AVAILABLE = self.orig_faiss

    def _mock_embed(self, titles):
        """mock embedding：返回预设向量。"""
        vecs = [_make_vec(t) for t in titles]
        return vecs, None

    def _mock_embed_similar(self, groups):
        """mock embedding：同组标题产生高相似度向量。
        groups: list of list of titles，每组内部相似，组间不相似。
        """
        import random
        dim = 64
        title_to_vec = {}
        for gi, group in enumerate(groups):
            # 每组一个基准方向
            base = [0.0] * dim
            base[gi * 4 % dim] = 1.0
            base[(gi * 4 + 1) % dim] = 0.8
            for title in group:
                v = base[:]
                rng = random.Random(title)
                for i in range(dim):
                    v[i] += rng.gauss(0, 0.05)
                norm = sum(x * x for x in v) ** 0.5
                if norm > 0:
                    v = [x / norm for x in v]
                title_to_vec[title] = v

        def embed_fn(titles):
            return [title_to_vec.get(t, _make_vec(t, dim)) for t in titles], None
        return embed_fn

    def test_semantic_merge_semantic_similar(self):
        """语义相似但字符串不同的标题应合并。"""
        B.FAISS_AVAILABLE = True
        # GPT-6 相关标题应产生相似向量
        groups = [
            ["GPT-6 发布", "OpenAI 发布 GPT-6 新模型"],
            ["苹果 WWDC 发布新 iPhone"],
        ]
        B._embed_chunks = self._mock_embed_similar(groups)

        chunks = [
            _make_chunk("GPT-6 发布", score=0.8),
            _make_chunk("OpenAI 发布 GPT-6 新模型", score=0.7),
            _make_chunk("苹果 WWDC 发布新 iPhone", score=0.6),
        ]
        events = B._assemble_events(chunks)
        # GPT-6 相关的两个应合并为一个事件
        self.assertLessEqual(len(events), 2,
                             "相似标题应合并为 ≤2 个事件，实际 %d" % len(events))

    def test_semantic_keep_distinct(self):
        """语义完全不同的标题不应合并。"""
        B.FAISS_AVAILABLE = True
        groups = [
            ["GPT-6 发布 参数量 10 万亿"],
            ["苹果 WWDC 发布新 iPhone"],
            ["美联储加息 50 个基点"],
        ]
        B._embed_chunks = self._mock_embed_similar(groups)

        chunks = [
            _make_chunk("GPT-6 发布 参数量 10 万亿", score=0.9),
            _make_chunk("苹果 WWDC 发布新 iPhone", score=0.8),
            _make_chunk("美联储加息 50 个基点", score=0.7),
        ]
        events = B._assemble_events(chunks)
        self.assertGreaterEqual(len(events), 2,
                                "完全不同的标题应保持独立，实际 %d 个事件" % len(events))

    def test_semantic_cluster_chain(self):
        """传递性合并：A≈B, B≈C → {A,B,C} 同一簇。"""
        B.FAISS_AVAILABLE = True
        groups = [
            ["OpenAI 发布 GPT-6", "GPT-6 正式发布 新模型", "GPT-6 来了 OpenAI 最新模型"],
        ]
        B._embed_chunks = self._mock_embed_similar(groups)

        chunks = [
            _make_chunk("OpenAI 发布 GPT-6", score=0.9),
            _make_chunk("GPT-6 正式发布 新模型", score=0.8),
            _make_chunk("GPT-6 来了 OpenAI 最新模型", score=0.7),
        ]
        events = B._assemble_events(chunks)
        self.assertEqual(len(events), 1,
                         "链式相似的 3 个标题应合并为 1 个事件，实际 %d" % len(events))

    def test_semantic_empty_input(self):
        """空输入返回空列表。"""
        events = B._assemble_events([])
        self.assertEqual(events, [])

    def test_semantic_single_chunk(self):
        """单个 chunk 直接返回一个事件。"""
        B.FAISS_AVAILABLE = True
        B._embed_chunks = self._mock_embed
        chunks = [_make_chunk("GPT-6 发布", score=0.9)]
        events = B._assemble_events(chunks)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["items"]), 1)

    def test_semantic_output_format(self):
        """输出格式必须包含必要字段。"""
        B.FAISS_AVAILABLE = True
        B._embed_chunks = self._mock_embed
        chunks = [
            _make_chunk("GPT-6 发布", score=0.9),
            _make_chunk("苹果 WWDC", score=0.8),
        ]
        events = B._assemble_events(chunks)
        for evt in events:
            self.assertIn("id", evt)
            self.assertIn("label", evt)
            self.assertIn("items", evt)
            self.assertIn("source_types", evt)
            self.assertIn("best_score", evt)

    def test_semantic_fallback_to_jaccard(self):
        """embedding 失败时应降级为 Jaccard（不崩溃）。"""
        B.FAISS_AVAILABLE = True
        B._embed_chunks = lambda texts: None
        chunks = [
            _make_chunk("GPT-6 发布 OpenAI", score=0.9),
            _make_chunk("GPT-6 发布 OpenAI 新模型", score=0.8),
            _make_chunk("苹果 WWDC 发布新 iPhone", score=0.7),
        ]
        events = B._assemble_events(chunks)
        self.assertIsInstance(events, list)
        self.assertGreater(len(events), 0)

    def test_semantic_fallback_no_faiss(self):
        """FAISS 不可用时应降级为 Jaccard。"""
        B.FAISS_AVAILABLE = False
        chunks = [
            _make_chunk("GPT-6 发布 OpenAI", score=0.9),
            _make_chunk("GPT-6 发布 OpenAI 新模型", score=0.8),
        ]
        events = B._assemble_events(chunks)
        self.assertIsInstance(events, list)


if __name__ == "__main__":
    unittest.main()
