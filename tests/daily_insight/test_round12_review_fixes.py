# -*- coding: utf-8 -*-
"""R12 对抗审查修复批：合成 0.5 必须自证降级、judge 默认值不得回落到 403 模型、上下文预算与归因计数。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Judge:
    model = "agnes-2.5-flash"
    _call_log = []

    def complete(self, messages, temperature=0.2, max_tokens=4000):
        raise AssertionError("本用例不应真正调用 LLM")


_CLUSTERS = [{"label": "事件甲", "summary": "正文甲", "category": "model_release", "items": []}]
_CHUNKS = [{"url": "http://a/1", "title": "t1", "text": "内容" * 30,
            "source_type": "rss", "retrieval_score": 1.0}]


def test_all_fail_eval_is_marked_degraded(monkeypatch):
    """评估全失败时写出的合成 0.5 必须与真实 1 样本 0.5 可区分（04:00 期假崩分实证）。"""
    monkeypatch.setattr(B, "_robust_quality_eval", lambda *a, **k: None)
    monkeypatch.setattr(B, "_self_correct_events", lambda *a, **k: [])
    _, ev = B._ragas_evaluate_and_correct(_Judge(), _CLUSTERS, "主题", _CHUNKS,
                                         {"daily_insight_max_corrections": 0})
    assert ev["overall"] == 0.5
    meta = ev["meta"]
    assert meta.get("degraded") is True, "全失败合成分必须标 degraded"
    assert meta["eval_samples"] == 0 and meta["prompt_variants"] == 0


def test_real_eval_is_not_marked_degraded(monkeypatch):
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: {"context_coverage": 0.8, "faithfulness": 0.9,
                                         "relevance": 0.8, "overall": 0.85, "feedback": "f",
                                         "weak_events": [], "_eval_samples": 3,
                                         "_actual_variants": 2})
    _, ev = B._ragas_evaluate_and_correct(_Judge(), _CLUSTERS, "主题", _CHUNKS,
                                         {"daily_insight_max_corrections": 0})
    assert ev["meta"].get("degraded") is False
    assert ev["meta"]["eval_samples"] == 3


def test_judge_defaults_no_longer_point_at_mimo(monkeypatch):
    """配置缺失/损坏时不得静默回到从未通过的 mimo（403 全历史实证）。"""
    built = {}
    monkeypatch.setattr(B, "_init_llm", lambda: "agnes-instance")

    class _BoomMimo:
        def __init__(self, *a, **k):
            built["mimo"] = True

    monkeypatch.setattr(B, "_MimoLLM", _BoomMimo)
    assert B._init_judge_llm({}) == "agnes-instance"
    assert "mimo" not in built, "空配置不得构造 mimo 主判"


def test_dead_fallback_config_key_removed():
    """judge_fallback / openrouter_models 无任何代码读取（审查 P1-1），留着会假装存在降级链。"""
    cfg = json.load(open(os.path.join(_ROOT, "build_config.json"), encoding="utf-8"))
    src = open(os.path.join(_ROOT, "build_daily_insight.py"), encoding="utf-8").read()
    for dead in ("daily_insight_judge_fallback", "daily_insight_openrouter_models"):
        assert dead not in cfg, "%s 是死配置，不得留在文件里冒充开关" % dead
        assert ('"%s"' % dead) not in src, "%s 不得被任何代码读取" % dead


def test_ragas_context_respects_char_budget():
    chunks = [{"url": "http://a/%d" % i, "title": "t", "text": "长" * 1000,
               "source_type": "rss", "retrieval_score": float(1000 - i)}
              for i in range(400)]
    stats = {}
    ctx = B._build_ragas_context(_CLUSTERS, chunks, stats=stats)
    assert len(ctx) <= B._RAGAS_CTX_CHAR_BUDGET
    assert 0 < stats["chunks"] < 400, "预算收口后必须记录实际入选块数"


def test_ragas_context_stats_counts_chunks_exactly():
    """旧口径用 count('---')+1 计数，正文含 --- 的块会虚报（审查 P2-5）。"""
    chunks = [{"url": "http://a/%d" % i, "title": "t", "text": "段落---分隔---继续",
               "source_type": "rss", "retrieval_score": 1.0} for i in range(5)]
    stats = {}
    ctx = B._build_ragas_context(_CLUSTERS, chunks, stats=stats)
    assert stats["chunks"] == 5
    assert ctx.count("---") > 5, "样本正文必须真的含 ---，否则这条断言无意义"


def test_meta_context_chunks_uses_accurate_count(monkeypatch):
    stats_calls = {}

    def _fake_build(clusters, chunks, stats=None):
        if stats is not None:
            stats.update({"chunks": 7, "chars": 700})
        stats_calls.update(stats or {})
        return "ctx" * 100

    monkeypatch.setattr(B, "_build_ragas_context", _fake_build)
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: {"context_coverage": 0.8, "faithfulness": 0.9,
                                         "relevance": 0.8, "overall": 0.85, "feedback": "f",
                                         "weak_events": [], "_eval_samples": 2,
                                         "_actual_variants": 1})
    _, ev = B._ragas_evaluate_and_correct(_Judge(), _CLUSTERS, "主题", _CHUNKS,
                                         {"daily_insight_max_corrections": 0})
    assert ev["meta"]["context_chunks"] == 7
    assert ev["meta"]["context_chars"] == 700
    assert stats_calls.get("chunks") == 7


def test_shipped_confirm_uses_accurate_context_stats(monkeypatch):
    seen = {}

    def _fake_build(clusters, chunks, stats=None):
        if stats is not None:
            stats.update({"chunks": 9, "chars": 900})
        return "ctx"

    monkeypatch.setattr(B, "_build_ragas_context", _fake_build)
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: {"context_coverage": 0.8, "faithfulness": 0.9,
                                         "relevance": 0.8, "overall": 0.85, "feedback": "f",
                                         "missed_points": [], "weak_events": []})
    draft = {"overall": 0.5, "context_coverage": 0.5, "faithfulness": 0.5, "relevance": 0.5,
             "missed_points": ["漏点"], "meta": {"context_chunks": 999}}
    out = B._confirm_shipped_report_eval(_Judge(), _CLUSTERS, "主题", _CHUNKS, draft)
    assert out["meta"]["context_chunks"] == 9 and out["meta"]["context_chars"] == 900
    assert out["meta"]["draft_eval"]["missed_points"] == ["漏点"]
