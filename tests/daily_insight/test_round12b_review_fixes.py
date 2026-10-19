# -*- coding: utf-8 -*-
"""R12b：第12轮对抗审查 P1x3/P2x2 复修批——判定域必须覆盖引用域、合成分不得冒充样本、审计链不得断。"""
import io
import json
import os
import re
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = open(os.path.join(_ROOT, "build_daily_insight.py"), encoding="utf-8").read()


def _chunk(url, text="正文内容", score=1.0, title="t"):
    return {"url": url, "title": title, "text": text, "source_type": "rss",
            "retrieval_score": score}


# ── P1-1 判定域 ⊇ 引用域 ──

def test_context_includes_cited_chunks_missing_from_pool():
    """回收事件的引用块可能不在 retrieved_for_ragas 里，judge 看不到就会把真实报道判成幻觉。"""
    pool = [_chunk("http://pool/%d" % i, score=float(200 - i)) for i in range(200)]
    cited = _chunk("http://outside/x", text="失语患者用原声说我爱你")
    clusters = [{"label": "Neuralink 进展", "summary": "s", "items": [cited]}]
    stats = {}
    ctx = B._build_ragas_context(clusters, pool, stats=stats)
    assert "失语患者用原声说我爱你" in ctx, "引用块必须进判定域"
    assert stats.get("cited_extra") == 1


def test_context_budget_reports_truncation(monkeypatch):
    """预算咬人时不得静默丢块：要么记 truncated+warning，要么根本不咬。"""
    monkeypatch.setattr(B, "_RAGAS_CTX_CHAR_BUDGET", 3000)
    pool = [_chunk("http://pool/%d" % i, text="长" * 900, score=float(200 - i))
            for i in range(200)]
    stats = {}
    buf = io.StringIO()
    with redirect_stdout(buf):
        ctx = B._build_ragas_context([{"label": "x", "summary": "y", "items": []}],
                                     pool, stats=stats)
    assert len(ctx) <= 3000
    assert stats.get("truncated") is True, "静默丢块会让 faith 假阴（R11 同型故障）"
    assert "::warning" in buf.getvalue()


# ── P1-2 合成/守卫分不得冒充样本 ──

def test_guard_returns_are_marked():
    out = B._evaluate_report_quality(None, [{"label": "x", "summary": "y"}], "t", "")
    assert out["_guard"] is True, "空上下文/无 LLM 的 0.5 必须标守卫"


def test_guard_samples_are_excluded_from_median(monkeypatch):
    calls = {"n": 0}

    def _fake(llm, clusters, theme, ctx, prompt_variant="v1"):
        calls["n"] += 1
        return {"context_coverage": 0.5, "faithfulness": 0.5, "relevance": 0.5,
                "overall": 0.5, "feedback": "LLM 不可用", "weak_events": [], "_guard": True}

    monkeypatch.setattr(B, "_evaluate_report_quality", _fake)
    assert B._robust_quality_eval(object(), [{"label": "x"}], "t", "ctx", True) is None, \
        "全守卫样本等于没有样本"


def test_confirm_skips_when_context_empty(monkeypatch):
    hits = []
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: hits.append(1) or None)
    draft = {"overall": 0.7, "context_coverage": 0.6, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": [], "meta": {"eval_samples": 3}}
    out = B._confirm_shipped_report_eval(object(), [{"label": "x", "summary": "y"}],
                                         "t", [], draft)
    assert out["meta"]["eval_stage"] == "draft_only"
    assert out["overall"] == 0.7
    assert hits == [], "上下文为空时不该发起复评（会写出冒充 shipped 的合成分）"


# ── P1-3 趋势日志口径可核 ──

def test_tracking_entry_records_eval_stage_and_draft():
    entry = B._build_tracking_entry(
        {"overall": 0.78, "context_coverage": 0.7, "faithfulness": 0.95, "relevance": 0.7,
         "feedback": "f", "iterations": 1,
         "meta": {"eval_stage": "shipped_report", "degraded": False,
                  "draft_eval": {"overall": 0.72}}},
        [{"label": "x"}], "主题", 100, 10, 5, 5, 5, 200, 10, True)
    assert entry["eval_stage"] == "shipped_report"
    assert entry["draft_overall"] == 0.72
    assert entry["iterations"] == 1, "终版分必须带上草稿环的修正轮数，否则每期都是 null"


def test_shipped_eval_carries_iterations_forward(monkeypatch):
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: {"context_coverage": 0.8, "faithfulness": 0.9,
                                         "relevance": 0.8, "overall": 0.83, "feedback": "f",
                                         "missed_points": [], "weak_events": [],
                                         "_eval_samples": 3, "_actual_variants": 2})
    draft = {"overall": 0.7, "context_coverage": 0.6, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": [], "iterations": 1, "meta": {}}
    out = B._confirm_shipped_report_eval(object(), [{"label": "x", "summary": "y"}],
                                         "t", [_chunk("http://a")], draft)
    assert out["iterations"] == 1
    assert out["meta"]["prompt_variants"] == 2 and out["meta"]["eval_samples"] == 3, \
        "样本数取自复评真实计数，不得靠 pop 默认值蒙对"


# ── P2-4 审计链与发布安全 ──

class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_primary_llm_keeps_call_log(monkeypatch):
    """judge 默认改 agnes 后拿到的是裸 _LLM，call_log 一旦为空，标签谎报就再也查不出来。"""
    monkeypatch.setattr(B.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp({"choices": [{"message": {"content": "hi"}}]}))
    llm = B._LLM("k1", model="agnes-2.5-flash")
    assert llm.complete([{"role": "user", "content": "x"}]) == "hi"
    log = llm._call_log
    assert log and log[-1]["model"] == "agnes-2.5-flash"
    assert log[-1]["status"] == "ok" and "elapsed_s" in log[-1]


def test_shipped_meta_call_log_only_covers_shipped_window(monkeypatch):
    class _L:
        model = "agnes-2.5-flash"
        _call_log = [{"model": "agnes-2.5-flash", "status": "ok"}] * 4

    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: (a[0]._call_log.append(
                            {"model": "agnes-2.5-flash", "status": "ok"}),
                            {"context_coverage": 0.8, "faithfulness": 0.9, "relevance": 0.8,
                             "overall": 0.84, "feedback": "f", "missed_points": [],
                             "weak_events": [], "_eval_samples": 3, "_actual_variants": 2})[1])
    draft = {"overall": 0.7, "context_coverage": 0.6, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": [], "meta": {}}
    out = B._confirm_shipped_report_eval(_L(), [{"label": "x", "summary": "y"}],
                                         "t", [_chunk("http://a")], draft)
    assert len(out["meta"]["call_log"]) == 1, \
        "call_log 须切片到终版窗口（只含本次复评），混入草稿调用会让降级归因再次失真"


def test_main_wraps_shipped_confirm_in_try():
    """复评只是观测项，绝不能因为打印/格式化异常挡住产物写盘。"""
    body = _SRC.split("def main():")[1].split('if __name__')[0]
    block = body[body.rindex("clusters = _drop_insufficient(clusters)"):
                 body.index("_write_insight_json(")]
    assert "_confirm_shipped_report_eval(" in block
    assert "try:" in block and re.search(r"\n\s*except\b", block), \
        "收口段必须有 try/except 包住终版复评"
