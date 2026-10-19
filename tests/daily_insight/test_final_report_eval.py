# -*- coding: utf-8 -*-
"""R12: 产物 quality 必须描述最终 shipped 报告——07:14 期实证 quality 出自 missed 回收前的草稿。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "build_daily_insight.py"), encoding="utf-8").read()

_DRAFT = {"overall": 0.72, "context_coverage": 0.65, "faithfulness": 0.90,
          "relevance": 0.62, "feedback": "草稿评价", "missed_points": ["漏点A"],
          "weak_events": [1, 2], "meta": {"call_log": [], "judge_model": "agnes-2.5-flash"}}

_SHIPPED = {"overall": 0.81, "context_coverage": 0.78, "faithfulness": 0.95,
            "relevance": 0.75, "feedback": "终版评价", "missed_points": [],
            "weak_events": [], "meta": {"call_log": [], "judge_model": "agnes-2.5-flash"}}


class _StubLLM:
    model = "agnes-2.5-flash"

    def complete(self, messages, temperature=0.2, max_tokens=4000):
        raise AssertionError("终版复评不应直接调 complete()")


def test_confirm_uses_shipped_numbers_and_keeps_draft(monkeypatch):
    calls = {}

    def _fake_eval(llm, clusters, theme, context_text, use_cv):
        calls["clusters"] = clusters
        calls["context_text"] = context_text
        calls["use_cv"] = use_cv
        return dict(_SHIPPED)

    monkeypatch.setattr(B, "_robust_quality_eval", _fake_eval)
    clusters = [{"label": "事件一", "summary": "s1"}, {"label": "事件二", "summary": "s2"}]
    chunks = [{"url": "http://a", "text": "正文" * 40, "retrieval_score": 1.0}]

    out = B._confirm_shipped_report_eval(_StubLLM(), clusters, "主题", chunks, dict(_DRAFT))

    assert out["overall"] == 0.81 and out["relevance"] == 0.75
    draft = out["meta"]["draft_eval"]
    assert draft["overall"] == 0.72 and draft["missed_points"] == ["漏点A"]
    assert out["meta"]["eval_stage"] == "shipped_report"
    # 复评对象必须是 shipped 终版事件列表，上下文仍来自全池检索
    assert [c["label"] for c in calls["clusters"]] == ["事件一", "事件二"]
    assert "正文" in calls["context_text"]
    assert calls["use_cv"] is True
    # 归因字段必须随终版分一起落盘，否则跨期口径又要靠猜
    assert out["meta"]["judge_model"] == "agnes-2.5-flash"
    assert out["meta"]["context_chunks"] >= 1 and out["meta"]["context_chars"] > 0


def test_shipped_quality_reaches_json(monkeypatch, tmp_path):
    """写盘链路：quality 落的是终版分，草稿分留在 meta.draft_eval 供趋势对齐。"""
    import json
    out_file = tmp_path / "di.json"
    monkeypatch.setattr(B, "INSIGHT_FILE", str(out_file))
    monkeypatch.setattr(B, "_load_history", lambda: {"days": []})
    monkeypatch.setattr(B, "_robust_quality_eval", lambda *a, **k: dict(_SHIPPED))
    shipped = B._confirm_shipped_report_eval(
        _StubLLM(), [{"label": "x", "summary": "y"}], "t",
        [{"url": "u", "text": "正文" * 40, "retrieval_score": 1.0}], dict(_DRAFT))
    ev = [{"label": "L", "summary": "s", "significance": "", "category": "research",
           "items": [], "source_types": ["rss"], "score": 1.0}]
    B._write_insight_json(ev, "T", False, shipped)
    doc = json.loads(out_file.read_text(encoding="utf-8"))
    assert doc["quality"]["overall"] == 0.81
    assert doc["quality"]["meta"]["eval_stage"] == "shipped_report"
    assert doc["quality"]["meta"]["draft_eval"]["overall"] == 0.72


def test_confirm_falls_back_to_draft_when_eval_fails(monkeypatch):
    monkeypatch.setattr(B, "_robust_quality_eval", lambda *a, **k: None)
    out = B._confirm_shipped_report_eval(_StubLLM(), [{"label": "x", "summary": "y"}],
                                         "t", [{"url": "u", "text": "z", "retrieval_score": 1}],
                                         dict(_DRAFT))
    assert out["overall"] == 0.72
    assert out.get("missed_points") == ["漏点A"]
    assert out["meta"]["eval_stage"] == "draft_only"


def test_confirm_survives_eval_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("judge 挂了")

    monkeypatch.setattr(B, "_robust_quality_eval", _boom)
    out = B._confirm_shipped_report_eval(_StubLLM(), [{"label": "x", "summary": "y"}],
                                         "t", [{"url": "u", "text": "z", "retrieval_score": 1}],
                                         dict(_DRAFT))
    assert out["overall"] == 0.72
    assert out["meta"]["eval_stage"] == "draft_only"


def test_confirm_skips_without_judge_or_clusters(monkeypatch):
    hits = []
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: hits.append(1) or dict(_SHIPPED))
    assert B._confirm_shipped_report_eval(None, [{"label": "x"}], "t", [], dict(_DRAFT))["overall"] == 0.72
    assert B._confirm_shipped_report_eval(_StubLLM(), [], "t", [], dict(_DRAFT))["overall"] == 0.72
    assert not hits


def test_draft_only_path_has_no_shipped_eval():
    """ragas_eval 为空（Judge 不可用）时不得凭空造出 quality。"""
    out = B._confirm_shipped_report_eval(_StubLLM(), [{"label": "x", "summary": "y"}],
                                         "t", [{"url": "u", "text": "z", "retrieval_score": 1}], None)
    assert out is None or out == {}


def test_confirm_honours_use_cv(monkeypatch):
    """终版分与草稿分必须同口径，否则跨期趋势不可比（审查 P1-2 同源问题）。"""
    seen = {}

    def _spy(llm, clusters, theme, context_text, use_cv=None):
        seen["cv"] = use_cv
        return dict(_SHIPPED)

    monkeypatch.setattr(B, "_robust_quality_eval", _spy)
    B._confirm_shipped_report_eval(_StubLLM(), [{"label": "x", "summary": "y"}], "t",
                                   [{"url": "u", "text": "正文", "retrieval_score": 1}],
                                   dict(_DRAFT), use_cv=False)
    assert seen["cv"] is False
    body = _SRC.split("def main():")[1].split('if __name__')[0]
    call = body[body.index("_confirm_shipped_report_eval("):].split(")")[0]
    assert "daily_insight_cross_validation" in call, "挂点必须按配置传 use_cv，不得硬编码"


def test_main_flow_confirms_after_cap_and_before_write():
    body = _SRC.split("def main():")[1].split('if __name__')[0]
    i_recover = body.index("_recover_missed_events(")
    i_cap = body.rindex("clusters[:MAX_EVENTS]")  # 终局收口（main 里更早还有一处预算收口）
    i_confirm = body.index("_confirm_shipped_report_eval(")
    i_write = body.index("_write_insight_json(")
    assert i_recover < i_cap < i_confirm < i_write, "终版复评必须位于收口之后、写盘之前"
    assert body.count("_confirm_shipped_report_eval(") == 1, "终版复评只能挂一处，避免重复计费"
