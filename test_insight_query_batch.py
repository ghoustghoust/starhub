# -*- coding: utf-8 -*-
"""T-D2 查询批化测试（spec: docs/superpowers/specs/2026-09-14-insight-optimization-spec.md）

T-D2-1: generate_deep_insights 15 簇 → 检索嵌入 API 恰 1 次批量调用（原 ≤5 次单条）
T-D2-2: _evaluate_and_correct 5 簇 → 检索嵌入恰 1 次批量调用（原 ≤5 次单条）

零网络：mock _get_embeddings（计数+确定性向量）、_retrieve_with_context（桩）、LLM（按提示词内容应答）。
"""
import json
import sys

import insight_engine as ie


class FakeLLM:
    """按提示词内容应答的桩 LLM。"""

    def complete(self, prompt, system_prompt=None, temperature=0.3, max_tokens=600):
        if "causal_chains" in prompt:
            return json.dumps({
                "causal_chains": ["A→B→C"],
                "signals": [{"signal": "信号", "confidence": 0.9}],
                "outlook": "前瞻文本",
            })
        if "评估" in prompt or "质量" in prompt:
            return json.dumps({
                "overall": 0.9,
                "context_coverage": 0.9,
                "faithfulness": 0.9,
                "relevance": 0.9,
                "feedback": "",
            })
        if "narrative" in prompt or "故事线" in prompt:
            return "这是叙事文本，包含因果与时间线。"
        if "技术趋势" in prompt or "core_trends" in prompt:
            return "这是核心趋势文本。"
        if "开源" in prompt or "rss" in prompt.lower():
            return "这是开源动态文本。"
        return "通用中文分析文本，长度足够触发接受条件。"


def make_clusters(n):
    return [
        {
            "label": "簇%d" % i,
            "items": ["条目%d-甲的技术分析" % i, "条目%d-乙的架构观察" % i, "条目%d-丙" % i],
        }
        for i in range(n)
    ]


class EmbedCounter:
    """替换 _get_embeddings：记录每次批量的文本，返回确定性 8 维向量。"""

    def __init__(self):
        self.batches = []

    def __call__(self, texts):
        self.batches.append(list(texts))
        return [[0.01 * ((len(t) * 7 + ord(t[0])) % 13) + 0.05 for _ in range(8)] for t in texts], "mock"


def install_counter():
    counter = EmbedCounter()
    orig = ie._get_embeddings
    ie._get_embeddings = counter
    return counter, orig


def main():
    llm = FakeLLM()
    child_vecs, child_nodes, parent_docs = [1.0], [{"text": "子块"}], [{"text": "父文档"}]
    keywords = ["关键词甲", "关键词乙"]

    # ── T-D2-1 ──
    counter, orig = install_counter()
    try:
        clusters = make_clusters(15)
        result = ie.generate_deep_insights(llm, clusters, child_vecs, child_nodes, parent_docs, keywords)
        n_batches = len(counter.batches)
        q_texts = [t for b in counter.batches for t in b]
        assert n_batches == 1, "T-D2-1 FAIL: 检索嵌入被拆成 %d 次调用（期望 1 次批量）" % n_batches
        assert len(q_texts) == 5, "T-D2-1 FAIL: 批量内查询文本 %d 条（函数上限 5 簇，期望 5 条）" % len(q_texts)
        assert isinstance(result, dict) and result.get("narrative"), "T-D2-1 FAIL: 深度洞察结果异常"
        print("PASS T-D2-1 深度洞察: 15 簇输入（上限 5）检索嵌入合并为 %d 次批量（5 条查询文本），结果完整" % n_batches)
    finally:
        ie._get_embeddings = orig

    # ── T-D2-2 ──
    counter, orig = install_counter()
    orig_retrieve = ie._retrieve_with_context
    ie._retrieve_with_context = lambda *a, **k: "检索上下文文本"  # 桩检索，隔离批化逻辑
    try:
        clusters = make_clusters(5)
        deep = ie._normalize_deep_insights({
            "narrative": "n", "core_trends": "t", "rss_insights": "r",
            "causal_chains": [], "signals": [], "outlook": "o",
        })
        cfg = {"insight_self_correct": True, "insight_quality_threshold": 0.6, "insight_max_corrections": 1}
        _, evaluation = ie._evaluate_and_correct(
            llm, deep, clusters, child_vecs, child_nodes, parent_docs, keywords, cfg)
        n_batches = len(counter.batches)
        q_texts = [t for b in counter.batches for t in b]
        assert n_batches == 1, "T-D2-2 FAIL: 评估上下文嵌入被拆成 %d 次调用（期望 1 次批量）" % n_batches
        assert len(q_texts) == 5, "T-D2-2 FAIL: 批量内查询文本 %d 条（期望 5 簇各 1 条）" % len(q_texts)
        assert evaluation.get("overall", 0) >= 0.6, "T-D2-2 FAIL: 评估结果异常 %r" % evaluation
        print("PASS T-D2-2 评估上下文: 5 簇检索嵌入合并为 %d 次批量（5 条查询文本），评估通过" % n_batches)
    finally:
        ie._get_embeddings = orig
        ie._retrieve_with_context = orig_retrieve

    print("T-D2 全部通过")


if __name__ == "__main__":
    main()
