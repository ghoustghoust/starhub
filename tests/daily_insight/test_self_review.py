"""T3.3 — 自审-修正环节测试。

验证 Phase 1 输出经过自审后质量提升，且高质量输出不被过度修改。
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi


class TestSelfReviewDetection:
    """自审检测能力测试。"""

    def test_self_review_detects_empty_summary(self):
        """空泛 summary 被标记为低质量。"""
        events = [
            {"id": "e1", "label": "某AI产品发布", "summary": "引发广泛关注",
             "significance": "值得关注", "category": "ai-products"}
        ]
        issues = bdi._review_event_quality(events)
        assert len(issues) > 0
        # 应检测到空泛表达
        assert any("空话" in iss or "empty" in iss.lower() or "数据" in iss
                    for iss in issues)

    def test_self_review_detects_missing_data(self):
        """缺少具体数据的 summary 被标记。"""
        events = [
            {"id": "e1", "label": "OpenAI融资",
             "summary": "OpenAI完成了新一轮融资，金额巨大",
             "significance": "对行业有重要影响", "category": "funding"}
        ]
        issues = bdi._review_event_quality(events)
        assert len(issues) > 0

    def test_self_review_skips_good_output(self):
        """高质量输出不被标记。"""
        events = [
            {"id": "e1", "label": "OpenAI完成100亿美元融资",
             "summary": "OpenAI完成100亿美元D轮融资，估值达2000亿美元，由软银领投。资金将用于AGI研发和基础设施建设。",
             "significance": "标志AI领域融资规模新里程碑，软银大举入场改变竞争格局。",
             "category": "funding"}
        ]
        issues = bdi._review_event_quality(events)
        # 高质量事件不应有太多问题（允许0-1个小问题）
        assert len(issues) <= 1


class TestSelfReviewCorrection:
    """自审修正逻辑测试。"""

    def test_self_review_no_crash_without_llm(self):
        """无 LLM 时自审不崩溃，返回原始事件。"""
        events = [
            {"id": "e1", "label": "test", "summary": "test summary with data 100M",
             "significance": "test sig", "category": "ai-products"}
        ]
        result = bdi._self_review_phase1(None, events, "素材文本")
        assert len(result) == len(events)
        assert result[0]["label"] == events[0]["label"]

    def test_self_review_max_one_iteration(self):
        """自审最多执行 1 次（不无限循环）。"""
        events = [
            {"id": "e1", "label": "test", "summary": "good summary with 50% improvement",
             "significance": "significant change", "category": "research"}
        ]
        # 无 LLM 时应立即返回
        result = bdi._self_review_phase1(None, events, "素材")
        assert result is events  # 应返回原列表

    def test_self_review_preserves_structure(self):
        """自审保留事件结构完整性。"""
        events = [
            {"id": "e1", "label": "test1", "summary": "s1", "significance": "sig1",
             "category": "ai-models", "score": 8.0},
            {"id": "e2", "label": "test2", "summary": "s2", "significance": "sig2",
             "category": "industry", "score": 6.0},
        ]
        result = bdi._self_review_phase1(None, events, "素材")
        assert len(result) == 2
        for e in result:
            assert "id" in e
            assert "label" in e
            assert "summary" in e


class TestBannedPhrases:
    """禁用表达检测测试。"""

    def test_banned_phrases_detected(self):
        """检测常见禁用表达。"""
        banned = bdi._BANNED_PHRASES if hasattr(bdi, '_BANNED_PHRASES') else []
        assert len(banned) > 0
        # 应包含常见禁用表达
        assert any("值得关注" in p for p in banned)
        assert any("未来可期" in p for p in banned)

    def test_banned_phrases_in_text(self):
        """检测文本中的禁用表达。"""
        text = "这项技术值得关注，未来发展未来可期"
        found = [p for p in bdi._BANNED_PHRASES if p in text]
        assert len(found) >= 2
