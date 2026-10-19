# -*- coding: utf-8 -*-
"""R13 阶段 C：把"这期分数是谁拖的、证据是哪来的"变成产物里的读数（纯观测，不改产报）。

依据链：
- 第 8 轮前：judge 的 missed 清单在解析层被丢弃 → 已修，本文件是同型缺陷的其余三处；
- 第 11/12 轮：判定域必须 ⊇ 装配域/引用域 → 需要知道池子构成才验得动；
- 观察期 1-5 期：rel 掉 3 次、cov 掉 1 次、第 4 期只剩 8 条上页，
  每期都要人工挖日志才能点名失分事件 → 本文件把它变成落盘字段。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


def _cluster(i, label, src="rss", n_items=1, text_len=900):
    items = [{"url": "http://a/%d-%d" % (i, k), "title": label, "source_type": src,
              "text": "内" * text_len, "retrieval_score": 1.0 - k * 0.01}
             for k in range(n_items)]
    return {"id": "evt_%d" % i, "label": label, "summary": "摘要" * 20,
            "significance": "", "category": "ai-models", "score": 0.5,
            "editor_score": 62, "items": items, "key_links": [],
            "source_types": sorted({it["source_type"] for it in items})}


# ── 检索通道构成：BM25 窗口里 RSS 到底有没有份额 ──

def test_hybrid_retrieve_reports_window_mix(monkeypatch):
    chunks = ([{"chunk_id": "r%d" % i, "source_type": "rss", "title": "标题", "text": "正文内容",
                "url": "http://r/%d" % i, "pub_date": "2026-09-19", "retrieval_score": 1.0}
               for i in range(5)] +
              [{"chunk_id": "n%d" % i, "source_type": "hot", "title": "榜", "text": "榜项",
                "url": "http://n/%d" % i, "retrieval_score": 1.0} for i in range(3)])
    st = {}
    B._hybrid_retrieve(None, chunks, ["正文内容"], top_k=4, stats=st)
    assert st.get("non_rss") == 3 and st.get("rss_total") == 5, st
    assert st.get("rss_budget_slots") == 5, "窗口 10000 装得下全部 5 条 RSS"
    assert st.get("rss_crowded_out") is False

    monkeypatch.setattr(B, "BM25_WINDOW", 3)
    st2 = {}
    B._hybrid_retrieve(None, chunks, ["正文内容"], top_k=4, stats=st2)
    assert st2["rss_budget_slots"] == 0 and st2["rss_crowded_out"] is True, \
        "非 RSS 占满窗口时 RSS 份额必须被记为 0 —— 观察期五期全是这个值"


def test_stats_are_optional_and_do_not_change_result():
    """stats 只是旁路读数：给不给都必须返回同一份检索结果，且不得新增异常面。

    注：非 dict chunk 会让 BM25 分支的既有 `c.get(...)` 先炸（早于本次改动的既有行为，
    本文件不负责它）；这里用正常输入验证旁路语义。
    """
    chunks = [{"chunk_id": "r1", "source_type": "rss", "title": "标题", "text": "正文内容",
               "url": "http://r/1", "pub_date": "2026-09-19", "retrieval_score": 1.0}]
    plain = B._hybrid_retrieve(None, chunks, ["正文内容"], top_k=1)
    with_stats = {}
    got = B._hybrid_retrieve(None, chunks, ["正文内容"], top_k=1, stats=with_stats)
    assert [c["chunk_id"] for c in plain] == [c["chunk_id"] for c in got]
    assert with_stats.get("non_rss") == 0 and with_stats.get("rss_total") == 1


# ── 事件证据来源构成：产物里本来就有（键名 sources），锁住契约防回退 ──

def test_event_sources_field_is_exported(monkeypatch, tmp_path):
    """观察期我误判过"来源构成没导出"——实际键名是 sources。
    这条测试把它钉成契约：每事件必须带来源列表，且不得因内部字段改名而静默变空。"""
    monkeypatch.setattr(B, "INSIGHT_FILE", str(tmp_path / "di.json"))
    monkeypatch.setattr(B, "_load_history", lambda: {"days": []})
    ev = [_cluster(1, "OpenAI 发布新模型", src="rss"),
          _cluster(2, "热榜条目事件", src="hot")]
    B._write_insight_json(ev, "T", False, {})
    doc = json.loads((tmp_path / "di.json").read_text(encoding="utf-8"))
    got = [e.get("sources") for e in doc["events"]]
    assert got == [["rss"], ["hot"]], got


# ── 逐事件判定：v2 的 irrelevant_events 过去被丢掉 ──

class _Judge:
    def __init__(self, payload):
        self._p = json.dumps(payload, ensure_ascii=False)

    def complete(self, messages, temperature=0.2, max_tokens=4000):
        return self._p


_V2_PAYLOAD = {
    "reasoning": {
        "coverage": {"missed_points": ["Neuralink 失语患者原声说话"], "confidence": "high"},
        "faithfulness": {"confidence": "high"},
        "relevance": {"irrelevant_events": ["亚运泰队抱怨午餐", "不存在的条目"],
                      "ordering_issues": [], "confidence": "high"},
    },
    "scores": {"context_coverage": 0.7, "faithfulness": 0.95, "relevance": 0.7},
    "feedback": "f", "weak_events": [1],
}


def test_irrelevant_events_are_parsed():
    out = B._evaluate_report_quality(_Judge(_V2_PAYLOAD), [_cluster(1, "OpenAI 发布新模型")],
                                     "t", "上下文" * 50, prompt_variant="v2")
    assert out["irrelevant_events"] == ["亚运泰队抱怨午餐", "不存在的条目"]


def test_per_event_aggregation_counts_across_samples():
    clusters = [_cluster(1, "OpenAI 发布新模型"), _cluster(2, "亚运泰队抱怨午餐"),
                _cluster(3, "AMD 发布 256 核处理器")]
    samples = [
        {"weak_events": [2], "irrelevant_events": ["亚运泰队抱怨午餐"], "confidences": {}},
        {"weak_events": [2, 3], "irrelevant_events": [], "confidences": {}},
        {"weak_events": [], "irrelevant_events": ["AMD 256 核处理器"], "confidences": {}},
    ]
    rows = B._aggregate_per_event(clusters, samples)
    by_idx = {r["index"]: r for r in rows}
    assert by_idx[2]["weak_flags"] == 2 and by_idx[2]["irrelevant_flags"] == 1
    assert by_idx[3]["irrelevant_flags"] == 1, "跨样本标签匹配要能容忍措辞漂移"
    assert by_idx[1]["weak_flags"] == 0 and by_idx[1]["irrelevant_flags"] == 0
    assert rows, "至少要有行，否则字段等于没加"
    assert set(by_idx[2]) >= {"index", "label", "weak_flags", "irrelevant_flags", "sources"}


def test_per_event_reaches_shipped_meta(monkeypatch):
    def _fake_eval(llm, clusters, theme, context_text, use_cv):
        return {"context_coverage": 0.75, "faithfulness": 0.95, "relevance": 0.70,
                "overall": 0.80, "feedback": "f", "missed_points": [],
                "weak_events": [2], "irrelevant_events": ["亚运泰队抱怨午餐"],
                "_eval_samples": 1, "_actual_variants": 1}

    monkeypatch.setattr(B, "_robust_quality_eval", _fake_eval)
    clusters = [_cluster(1, "OpenAI 发布新模型"), _cluster(2, "亚运泰队抱怨午餐", src="hot")]
    draft = {"overall": 0.8, "context_coverage": 0.75, "faithfulness": 0.95, "relevance": 0.8,
             "missed_points": [], "meta": {}}
    out = B._confirm_shipped_report_eval(object(), clusters, "T", clusters[0]["items"], draft)
    per = out["meta"]["per_event"]
    assert [r["index"] for r in per] == [1, 2]
    assert per[1]["weak_flags"] == 1 and per[1]["sources"]["hot"] == 1
    assert per[0]["sources"]["rss"] >= 1
    assert out["meta"]["eval_stage"] == "shipped_report"


def test_pool_mix_in_meta(monkeypatch):
    monkeypatch.setattr(B, "_robust_quality_eval",
                        lambda *a, **k: {"context_coverage": 0.75, "faithfulness": 0.95,
                                         "relevance": 0.7, "overall": 0.8, "feedback": "f",
                                         "missed_points": [], "weak_events": [],
                                         "irrelevant_events": [],
                                         "_eval_samples": 3, "_actual_variants": 2})
    chunks = [_cluster(1, "A", src="rss")["items"][0], _cluster(2, "B", src="hot")["items"][0]]
    draft = {"overall": 0.8, "context_coverage": 0.75, "faithfulness": 0.95, "relevance": 0.8,
             "missed_points": [], "meta": {}}
    out = B._confirm_shipped_report_eval(object(), [_cluster(1, "A")], "T", chunks, draft)
    mix = out["meta"]["retrieval_mix"]
    assert mix.get("rss") == 1 and mix.get("hot") == 1, "池子构成必须落盘，才能验 BM25/检索偏置假设"


def test_per_event_counts_across_three_real_samples(monkeypatch):
    clusters = [_cluster(1, "OpenAI 发布新模型"), _cluster(2, "亚运泰队抱怨午餐", src="hot")]
    n = {"i": 0}

    def _sample(llm, cl, theme, ctx, prompt_variant="v1"):
        n["i"] += 1
        return {"context_coverage": 0.6, "faithfulness": 0.95, "relevance": 0.6,
                "overall": 0.72, "feedback": "f", "weak_events": [2],
                "irrelevant_events": ["亚运泰队抱怨午餐"], "confidences": {},
                "missed_points": []}

    monkeypatch.setattr(B, "_evaluate_report_quality", _sample)
    _, ev = B._ragas_evaluate_and_correct(object(), clusters, "T",
                                          [c["items"][0] for c in clusters],
                                          {"daily_insight_max_corrections": 0,
                                           "daily_insight_cross_validation": True})
    assert n["i"] == 3, "CV 下应取三样本"
    per = {r["index"]: r for r in ev["meta"]["per_event"]}
    assert per[2]["weak_flags"] == 3 and per[2]["irrelevant_flags"] == 3
    assert per[1]["weak_flags"] == 0
    assert "_samples" not in ev["meta"], "临时键不得进 meta（注意不能用子串判断，eval_samples 里就含 _samples）"


# ── 审查复修：P1-3 归因要余量 / P1-2 reason 不得污染日志 / P1-1 读数不得挡发布 ──

def test_irrelevant_flag_requires_margin_over_second_best():
    """生产真实出现过两条几乎同名的阿里 Qwen 事件（jaccard 0.917）。
    判不出归属时必须落到 unmatched，而不是按序号塞给某一条 —— 否则归因数据自己是错的。"""
    clusters = [
        _cluster(1, "阿里发布全模态Agent模型Qwen3.8-Omni-Flash"),
        _cluster(2, "阿里发布Qwen3.8-Omni-Flash全模态模型"),
        _cluster(3, "OpenAI 发布新模型"),
    ]
    rows = B._aggregate_per_event(clusters, [{"weak_events": [],
                                              "irrelevant_events": ["Qwen3.8-Omni-Flash 发布"]}])
    assert sum(r["irrelevant_flags"] for r in rows) == 0, "两条都像时不得硬归属"
    assert sum(r.get("unmatched_irrelevant", 0) for r in rows) == 1


def test_veto_reason_cannot_forge_log_lines_or_warnings(capsys):
    """二审 reason 是模型自由文本：换行会多打一行，"::warning" 会被 GitHub 当注释解析。"""
    class _L:
        model = "agnes-2.5-flash"
        _call_log = []

        def complete(self, messages, temperature=0.1, max_tokens=600):
            return json.dumps({"verdicts": [{"group": 1, "same_event": False,
                                             "reason": "不同主体\n::warning title=伪造::注入"}]},
                              ensure_ascii=False)

    groups = [(1, [2], {"keep": 1, "merge": [2]})]
    clusters = [_cluster(1, "OpenAI 发布新模型"), _cluster(2, "AMD 发布 256 核处理器")]
    kept = B._verify_consolidation(_L(), clusters, groups)
    out = capsys.readouterr().out
    assert kept == []
    assert out.count("二审否决") == 1, "一组只能一行"
    assert "::warning" not in out, "模型文本不得拼出 GitHub 日志指令"


def test_readings_never_block_draft_eval(monkeypatch):
    """三个新读数（retrieval_mix/bm25_window/per_event）任何异常都不得让 Phase 2.5 整体跳过。"""
    def _sample(llm, cl, theme, ctx, prompt_variant="v1"):
        return {"context_coverage": 0.8, "faithfulness": 0.95, "relevance": 0.8,
                "overall": 0.85, "feedback": "f", "weak_events": [1],
                "irrelevant_events": ["x"], "confidences": {}, "missed_points": []}

    monkeypatch.setattr(B, "_evaluate_report_quality", _sample)
    monkeypatch.setattr(B, "_aggregate_per_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("读数炸了")))
    chunks = [_cluster(1, "OpenAI 发布新模型")["items"][0]]
    _, ev = B._ragas_evaluate_and_correct(object(), [_cluster(1, "OpenAI 发布新模型")],
                                          "T", chunks, {"daily_insight_max_corrections": 0})
    assert ev["meta"]["eval_samples"] >= 1, "读数异常时评分照常产出"
    assert ev["meta"].get("per_event") == []

