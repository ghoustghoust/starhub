# -*- coding: utf-8 -*-
"""R14-9C：追踪行的 `passed` 必须与它自己那一行写的分数说同一句话。

依据链：
- 第 12 轮把 `overall/cov/faith/rel` 换成终版口径，但 `passed` 仍取草稿环的 overall 判定；
- 观察期 7 期实测：`passed=true` 而同行三维未达线的行有 24/43 —— 拿它算达标率会把 1/7 读成 56%；
- 本会话已三次把 CI 门禁 B 的"红"报成"绿"（`continue-on-error` 让步骤结论为 success），
  同一类错误：**结论字段与证据字段不同源**。
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


def _entry(ragas_eval):
    return B._build_tracking_entry(ragas_eval, [{"label": "A", "items": []}], "T", 1.0,
                                   10, 2, 1, 3, 100, 50, True)


def _ev(overall=0.72, cov=0.70, faith=0.85, rel=0.60, meta=None):
    return {"overall": overall, "context_coverage": cov, "faithfulness": faith,
            "relevance": rel, "feedback": "f", "iterations": 0, "meta": meta or {}}


# 第 7 期真实数值：overall 达线但三维未达线 → 旧代码记 passed=True
def test_passed_agrees_with_dims_written_in_same_row():
    e = _entry(_ev())
    assert e["overall"] >= e["threshold"], "前提：overall 本身是达线的"
    assert e["context_coverage"] < B.RAGAS_MIN_COVERAGE
    assert e["passed"] is False, "同行 cov/rel 未达线却记 passed=True，就是 24/43 行的成因"


def test_all_dims_green_marks_passed_true():
    e = _entry(_ev(overall=0.83, cov=0.75, faith=0.95, rel=0.80))
    assert e["passed"] is True


def test_missing_value_is_unknown_not_pass():
    e = _entry(_ev(cov=None))
    assert e["passed"] is None, "维度缺失必须记 None（未知）。把未知塌成 False 会污染达标率的分母"
    e2 = _entry(_ev(overall=None))
    assert e2["passed"] is None


def test_passed_draft_keeps_the_draft_loop_verdict():
    """终版与草稿不同源时，草稿结论要另存，否则跨期看不出"草稿达标、终版失守"这一整类。"""
    ragas = _ev(meta={"eval_stage": "shipped_report",
                      "draft_eval": {"overall": 0.85, "context_coverage": 0.80,
                                     "faithfulness": 0.95, "relevance": 0.75}})
    e = _entry(ragas)
    assert e["passed"] is False
    assert e.get("passed_draft") is True, "草稿三维全达线，必须能单独读出来"


def test_no_draft_eval_means_no_draft_claim():
    e = _entry(_ev(meta={"eval_stage": "draft_only"}))
    assert "passed_draft" in e, "字段要在，值可为 None（区分「没做草稿评估」与「草稿未达标」）"
    assert e["passed_draft"] is None


def test_ledger_and_pipeline_share_one_threshold_definition():
    """追踪行的判定与修正环的 `_dims_pass_thresholds` 必须同向 —— 否则又长出第二套刻度。"""
    vectors = [
        (0.90, 0.75, 0.95, 0.80),   # 全绿
        (0.72, 0.70, 0.85, 0.60),   # 第 7 期：overall 绿、三维红
        (0.71, 0.75, 0.88, 0.75),   # 压在门槛线上（等于阈值算达线）
        (0.69, 0.75, 0.95, 0.80),   # 只有 overall 不达标
        (0.80, 0.74, 0.95, 0.80),   # 只有 cov 差一点
    ]
    for overall, cov, faith, rel in vectors:
        ev = _ev(overall=overall, cov=cov, faith=faith, rel=rel)
        with contextlib.redirect_stdout(io.StringIO()):
            gate = B._dims_pass_thresholds(dict(ev))
        e = _entry(ev)
        expect = bool(gate) and overall >= B.RAGAS_QUALITY_THRESHOLD
        assert e["passed"] == expect, (overall, cov, faith, rel, e["passed"], gate)


def test_threshold_constants_are_not_hardcoded_in_the_row():
    """改阈值时追踪行必须跟着动（观察期用的就是这三个常量）。"""
    orig = (B.RAGAS_MIN_COVERAGE, B.RAGAS_MIN_FAITHFULNESS, B.RAGAS_MIN_RELEVANCE)
    try:
        B.RAGAS_MIN_COVERAGE = 0.95
        assert _entry(_ev(overall=0.90, cov=0.75, faith=0.95, rel=0.80))["passed"] is False
    finally:
        B.RAGAS_MIN_COVERAGE, B.RAGAS_MIN_FAITHFULNESS, B.RAGAS_MIN_RELEVANCE = orig


# ── 以下为 fresh-eyes 对抗审查复修批（P0-1 / P1-1 / P1-4 / 阈值口径分叉）──

class _RawJudge:
    """原样返回给定文本（不走 json.dumps），用于喂裸 NaN 这类非法但可解析的 JSON。"""

    def __init__(self, text):
        self._t = text
        self._call_log = []

    def complete(self, messages, temperature=0.2, max_tokens=4000):
        return self._t


def test_nan_scores_cannot_be_read_as_perfect():
    """json.loads 默认接受裸 NaN，而 min(1.0, nan)==1.0 —— 旧 _clamp 会把"评不出来"洗成满分，
    于是台账写下 passed=true。非有限值必须落到解析失败哨兵，且不得被记为达标。"""
    assert not float("nan") < 1.0, "前提：NaN 的比较恒为假，min/max 会偏袒第一个参数"
    judge = _RawJudge('{"scores": {"context_coverage": NaN, "faithfulness": NaN,'
                      ' "relevance": NaN, "overall": NaN}, "feedback": "f"}')
    out = B._evaluate_report_quality(judge, [{"label": "x", "summary": "y"}], "t", "ctx")
    for k in ("context_coverage", "faithfulness", "relevance", "overall"):
        v = out.get(k)
        assert v is not None and v == v, "%s 不得带着 NaN 出门" % k
        assert v < 1.0, "%s 被 NaN 洗成了满分: %r" % (k, v)
    assert B._verdict(out, B.RAGAS_QUALITY_THRESHOLD) is not True


def test_gate_treats_missing_or_nan_dim_as_fail():
    """未知在修正环闸这一侧必须走"不通过"（继续修），否则缺维度会让闭环第一轮就停。"""
    assert B._dims_pass_thresholds({"context_coverage": 0.9, "faithfulness": 0.9}) is False
    assert B._dims_pass_thresholds({"context_coverage": float("nan"),
                                    "faithfulness": 0.95, "relevance": 0.9}) is False
    assert B._dims_reached({"context_coverage": True, "faithfulness": 0.95,
                            "relevance": 0.9}) is None, "bool 不是分数，不得当 1.0 用"


def test_draft_missing_dim_is_unknown_not_false():
    """draft_eval 里没评出的维度必须留 None：填 0 会让"没评出来"冒充"评了且不及格"。"""
    e = _entry(_ev(meta={"eval_stage": "shipped_report",
                         "draft_eval": {"overall": 0.85}}))
    assert e["passed_draft"] is None


def test_draft_eval_keeps_unknown_at_the_construction_site(monkeypatch):
    """上一条只测了"已经拼好的 draft_eval"，测不到真正会填 0 的那处（审查 M7 变异体存活）。
    这条走 `_confirm_shipped_report_eval`，从缺维度的草稿评估现场构造 draft_eval。"""
    shipped = {"overall": 0.81, "context_coverage": 0.78, "faithfulness": 0.95,
               "relevance": 0.75, "feedback": "终版", "missed_points": [], "weak_events": []}
    monkeypatch.setattr(B, "_robust_quality_eval", lambda *a, **k: dict(shipped))

    class _L:
        model = "agnes-2.5-flash"

        def complete(self, *a, **k):
            raise AssertionError("本测试不需要真实 judge 调用")

    draft = {"overall": 0.72, "faithfulness": 0.90, "missed_points": [], "meta": {}}
    out = B._confirm_shipped_report_eval(
        _L(), [{"label": "事件一", "summary": "s"}], "主题",
        [{"url": "http://a", "text": "正文" * 40, "retrieval_score": 1.0}], draft)
    de = out["meta"]["draft_eval"]
    assert "context_coverage" not in draft, "前提：草稿评估本来就没有这一维"
    assert de.get("context_coverage") is None, "构造点不得把缺失填成 0"
    assert _entry(out)["passed_draft"] is None, "未知不得冒充「评了且不及格」"


def test_row_uses_the_effective_threshold():
    """threshold 可被 build_config 覆盖；台账若写死常量，行内就会出现两个互相矛盾的数。"""
    e = B._build_tracking_entry(_ev(overall=0.68, cov=0.80, faith=0.95, rel=0.80),
                                [{"label": "A", "items": []}], "T", 1.0,
                                10, 2, 1, 3, 100, 50, True, threshold=0.65)
    assert e["threshold"] == 0.65
    assert e["passed"] is True, "实际门限 0.65 时 0.68 应记达标"


def test_missing_dim_is_listed_as_unevaluated_in_correction_prompt():
    """薄弱维度提示词与闸同向：缺维度要参与修正，且如实写"未评出"而不是谎报 0.00。"""
    class _Recorder:
        def __init__(self):
            self.prompts = []

        def complete(self, messages, temperature=0.3, max_tokens=1500):
            self.prompts.append(messages[1]["content"])
            return "{}"

    rec = _Recorder()
    cl = [{"label": "A", "summary": "摘要", "items": [
        {"url": "http://a/1", "title": "标题", "text": "素材正文内容", "source_type": "rss"}]}]
    B._self_correct_events(rec, cl, "ctx", {"feedback": "f", "weak_events": [1],
                                           "context_coverage": None,
                                           "faithfulness": 0.95, "relevance": 0.80})
    assert rec.prompts, "维度缺失时不得静默跳过修正"
    assert "context_coverage（当前未评出" in rec.prompts[0]
    assert "context_coverage（当前0.00" not in rec.prompts[0]

