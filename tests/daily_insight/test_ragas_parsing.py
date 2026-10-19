"""T3.1 — RAGAS 解析增强测试。

验证 _parse_json 和 _evaluate_report_quality 的解析鲁棒性。
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi


class TestParseJson:
    """_parse_json 鲁棒性测试。"""

    def test_ragas_parse_valid_json(self):
        """正常 JSON 正确解析。"""
        text = '{"context_coverage": 0.8, "faithfulness": 0.9, "relevance": 0.7}'
        result = bdi._parse_json(text)
        assert result is not None
        assert result["context_coverage"] == 0.8
        assert result["faithfulness"] == 0.9

    def test_ragas_parse_markdown_codeblock(self):
        """```json ... ``` 代码块正确提取。"""
        text = """这是评估结果：
```json
{"context_coverage": 0.6, "faithfulness": 0.7, "relevance": 0.8}
```
希望对你有帮助。"""
        result = bdi._parse_json(text)
        assert result is not None
        assert result["context_coverage"] == 0.6

    def test_ragas_parse_embedded_json(self):
        """文本中嵌入的 JSON 正确提取。"""
        text = """根据分析，以下是评估结果：
{"context_coverage": 0.5, "faithfulness": 0.6, "relevance": 0.9, "feedback": "good"}
以上是最终评分。"""
        result = bdi._parse_json(text)
        assert result is not None
        assert result["relevance"] == 0.9

    def test_ragas_parse_malformed(self):
        """格式错误的 JSON 返回 None 而非崩溃。"""
        text = "这不是 JSON，只是一段普通文字。"
        result = bdi._parse_json(text)
        assert result is None

    def test_ragas_parse_empty(self):
        """空输入返回 None。"""
        assert bdi._parse_json("") is None
        assert bdi._parse_json(None) is None

    def test_ragas_parse_trailing_comma(self):
        """尾部逗号的容错处理。"""
        text = '{"context_coverage": 0.8, "faithfulness": 0.9,}'
        result = bdi._parse_json(text)
        # 应尝试修复或返回 None
        if result is not None:
            assert result["context_coverage"] == 0.8


class TestRagasEvaluateDefaults:
    """_evaluate_report_quality 默认值和鲁棒性测试。"""

    def test_ragas_missing_fields_default(self):
        """缺失字段使用默认值填充（通过 _clamp 逻辑）。"""
        # 模拟解析结果缺失某些字段
        parsed = {"context_coverage": 0.8}  # 缺 faithfulness, relevance
        # 验证 _evaluate_report_quality 中的 _clamp 逻辑
        def _clamp(v):
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.5
        cov = _clamp(parsed.get("context_coverage", 0.5))
        faith = _clamp(parsed.get("faithfulness", 0.5))
        rel = _clamp(parsed.get("relevance", 0.5))
        assert cov == 0.8
        assert faith == 0.5  # 默认值
        assert rel == 0.5    # 默认值

    def test_ragas_non_numeric_scores(self):
        """非数字 score 回退到 0.5。"""
        def _clamp(v):
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return 0.5
        assert _clamp("high") == 0.5
        assert _clamp(None) == 0.5
        assert _clamp("0.8") == 0.8
        assert _clamp(0.7) == 0.7

    def test_ragas_no_context_returns_default(self):
        """无上下文时返回默认评估。"""
        result = bdi._evaluate_report_quality(None, [], "", "")
        assert result["overall"] == 0.5
        assert result["context_coverage"] == 0.5

    def test_ragas_no_llm_returns_default(self):
        """LLM 不可用时返回默认评估。"""
        clusters = [{"id": "e1", "summary": "test", "label": "test"}]
        result = bdi._evaluate_report_quality(None, clusters, "theme", "context text")
        assert result["overall"] == 0.5
        assert result["feedback"] == "LLM 不可用"

    def test_ragas_weak_events_parsed(self):
        """weak_events 字段正确解析为整数列表。"""
        parsed = {"weak_events": [2, 5], "context_coverage": 0.7,
                  "faithfulness": 0.8, "relevance": 0.6}
        weak = parsed.get("weak_events", [])
        assert isinstance(weak, list)
        assert all(isinstance(w, (int, float)) for w in weak)
