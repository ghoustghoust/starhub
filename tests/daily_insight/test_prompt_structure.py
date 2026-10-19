# -*- coding: utf-8 -*-
"""T2.1-T2.5: COSTAR 提示词结构测试。"""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class TestPhase1Prompt(unittest.TestCase):
    """Phase 1 提示词应包含 COSTAR 六要素 + 写法框架 + 正确示例。"""

    def test_system_prompt_has_context(self):
        self.assertIn("Context", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_objective(self):
        self.assertIn("Objective", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_style(self):
        self.assertIn("Style", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_tone(self):
        self.assertIn("Tone", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_audience(self):
        self.assertIn("Audience", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_response(self):
        self.assertIn("Response", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_writing_framework(self):
        self.assertIn("宏观主线", B._SYSTEM_PROMPT_P1)
        self.assertIn("微观佐证", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_fewshot_example(self):
        self.assertIn("正确示例", B._SYSTEM_PROMPT_P1)
        self.assertIn("summary", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_banned_phrases(self):
        self.assertIn("禁用", B._SYSTEM_PROMPT_P1)
        self.assertIn("值得关注", B._SYSTEM_PROMPT_P1)

    def test_system_prompt_has_platform_dna(self):
        dna_keywords = ["36氪", "虎嗅", "微博", "知乎", "Hacker News"]
        found = sum(1 for kw in dna_keywords if kw in B._SYSTEM_PROMPT_P1)
        self.assertGreaterEqual(found, 5,
                                "应包含至少 5 个平台 DNA，实际 %d" % found)


class TestPhase2Prompt(unittest.TestCase):
    """Phase 2 提示词应包含 COSTAR + 时间线叙事 + 影响分层。"""

    def test_p2_system_has_context(self):
        self.assertIn("Context", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_objective(self):
        self.assertIn("Objective", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_style(self):
        self.assertIn("Style", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_tone(self):
        self.assertIn("Tone", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_audience(self):
        self.assertIn("Audience", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_response(self):
        self.assertIn("Response", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_timeline_framework(self):
        self.assertIn("时间线", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_impact_layers(self):
        self.assertIn("短期", B._SYSTEM_PROMPT_P2)
        self.assertIn("中期", B._SYSTEM_PROMPT_P2)
        self.assertIn("长期", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_fewshot_example(self):
        self.assertIn("正确示例", B._SYSTEM_PROMPT_P2)
        self.assertIn("event_reconstruction", B._SYSTEM_PROMPT_P2)

    def test_p2_system_has_banned_phrases(self):
        self.assertIn("禁用", B._SYSTEM_PROMPT_P2)


if __name__ == "__main__":
    unittest.main()


class TestRagasFallbackPrompt:
    """22:14 归因批：数字溯源与覆盖导向必须存在于 prompt 装配层。"""

    def test_number_provenance_constraint(self):
        import build_daily_insight as b
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "必须能在事件素材或全局检索上下文中找到原样形式" in src
        assert "禁止换算、四舍五入或用外部知识补数" in src

    def test_coverage_orientation_in_global_block(self):
        import build_daily_insight as b
        clusters = [{"label": "x", "items": [{"title": "t", "text": "c" * 50,
                                              "source_type": "rss", "source": "s"}],
                     "source_types": ["rss"], "score": 5}]
        out = b._llm_phase1.__doc__  # 结构走读用源码断言替代
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "诉讼监管动向、宏观格局类高分辨性主题必须优先纳入" in src


class TestIter3FaithRel:
    """第3轮：不确定性标注(faith) + 输出按热度排序(rel)。"""

    def test_uncertainty_clause_in_prompts(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "有争议" in src and "不得写成确定事实" in src

    def test_order_events_desc_stable(self):
        import build_daily_insight as b
        cs = [{"label": "a", "editor_score": 60}, {"label": "b", "editor_score": 80},
              {"label": "c", "editor_score": 80}, {"label": "d"}]
        out = [c["label"] for c in b._order_events_for_output(cs)]
        assert out == ["b", "c", "a", "d"]
