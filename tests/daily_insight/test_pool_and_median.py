# -*- coding: utf-8 -*-
"""第7轮 T1 事件预算扩池 / T2 judge 多样本中位 / T3 语义近邻校准日志。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B


def _mkcluster(i):
    return {"label": "事件%02d" % i, "items": [{"url": "https://s/%d" % i,
            "title": "t%d" % i, "source_type": "rss"}],
            "source_types": {"rss"}, "best_score": 1.0 + i * 0.01}


class TestEventPool:
    def test_score_events_keeps_expanded_pool(self):
        out = B._score_events([_mkcluster(i) for i in range(20)], {})
        assert len(out) == B.PHASE1_POOL == B.MAX_EVENTS + 4

    def test_final_cap_wired_after_last_sort(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        i_sort = src.find("clusters = _order_events_for_output(clusters)  # 闸与分数刷新后重排")
        i_cap = src.find("clusters = clusters[:MAX_EVENTS]", i_sort)
        i_write = src.find("_write_insight_json(clusters", i_sort)
        assert -1 < i_sort < i_cap < i_write, "终局收口须在最后重排与写盘之间"

    def test_phase1_max_tokens_raised(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        i = src.find("def _llm_phase1")
        assert "max_tokens=4200" in src[i:i + 4000]


def _ev(overall, cov, faith, rel):
    return {"overall": overall, "context_coverage": cov, "faithfulness": faith,
            "relevance": rel, "feedback": "f%.2f" % overall, "weak_events": [1]}


class TestMedianEval:
    def test_median_of_three(self):
        m = B._median_eval_samples([_ev(0.80, 0.9, 0.9, 0.5), _ev(0.63, 0.4, 0.6, 0.8),
                                   _ev(0.78, 0.7, 0.8, 0.7)])
        assert m["overall"] == 0.78 and m["context_coverage"] == 0.7
        assert "f0.78" in m["feedback"]

    def test_even_samples_average_middles(self):
        m = B._median_eval_samples([_ev(0.8, 0.8, 0.9, 0.7), _ev(0.6, 0.6, 0.7, 0.5)])
        assert m["overall"] == 0.7

    def test_robust_eval_double_samples(self, monkeypatch):
        seq = [_ev(0.80, 0.8, 0.9, 0.7), _ev(0.60, 0.6, 0.7, 0.5)]
        monkeypatch.setattr(B, "_evaluate_report_quality",
                            lambda *a, **k: dict(seq.pop(0)))
        r = B._robust_quality_eval(object(), [], "theme", "ctx", use_cv=False)
        assert r["overall"] == 0.70 and r["_eval_samples"] == 2
        assert r["_actual_variants"] == 1

    def test_robust_eval_cv_median_of_three(self, monkeypatch):
        seq = [_ev(0.80, 0.8, 0.9, 0.7), _ev(0.63, 0.6, 0.7, 0.5), _ev(0.78, 0.7, 0.8, 0.6)]
        monkeypatch.setattr(B, "_evaluate_report_quality",
                            lambda *a, **k: dict(seq.pop(0)))
        r = B._robust_quality_eval(object(), [], "theme", "ctx", use_cv=True)
        assert r["overall"] == 0.78
        assert r["cross_validation"]["v1_overall"] == 0.80
        assert r["_eval_samples"] == 3 and r["_actual_variants"] == 2

    def test_robust_eval_partial_failure_degrades(self, monkeypatch):
        def flaky(llm, cls, theme, ctx, prompt_variant=None):
            if prompt_variant == "v2":
                return None
            return _ev(0.7, 0.7, 0.9, 0.7)
        monkeypatch.setattr(B, "_evaluate_report_quality", flaky)
        r = B._robust_quality_eval(object(), [], "theme", "ctx", use_cv=True)
        assert r["overall"] == 0.70 and r["_actual_variants"] == 1


class TestSemanticNearMissLog:
    def test_near_miss_pairs_logged(self, capsys, monkeypatch):
        os.environ["SILICONFLOW_API_KEY"] = "test-key"
        B._TEXT_EMB_CACHE.clear()
        B._SEM_EMB_DEAD["flag"] = False
        a = {"label": "英伟达发布新一代GPU", "category": "industry",
             "summary": "英伟达推出B300加速卡面向推理集群功耗降低四成",
             "items": [{"url": "https://n/1"}], "source_types": ["rss"], "score": 6}
        b = {"label": "英伟达公布季度财报", "category": "industry",
             "summary": "英伟达数据中心业务营收同比增长两成毛利率维持高位",
             "items": [{"url": "https://n/2"}], "source_types": ["rss"], "score": 5}
        import math

        def v(deg):
            return (math.cos(math.radians(deg)), math.sin(math.radians(deg)))
        m = {a["label"]: v(0), b["label"]: v(38), a["summary"]: v(0), b["summary"]: v(38)}
        monkeypatch.setattr(B, "_embed_chunks",
                            lambda ts: (None, None) if any(t not in m for t in ts)
                            else ([m[t] for t in ts], "test/fake"))
        try:
            out = B._deduplicate_after_phase1([a, b])
        finally:
            os.environ.pop("SILICONFLOW_API_KEY", None)
            B._TEXT_EMB_CACHE.clear()
        assert len(out) == 2
        captured = capsys.readouterr().out
        assert "语义近邻(未并)" in captured and "cos_l=0.79" in captured


class TestPoolWiring:
    def test_assemble_events_uses_expanded_pool(self):
        """P1-1 修复锚点：真正的上游截断在 _assemble_events，扩池必须落在那里。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        i = src.find("def _assemble_events")
        j = src.find("def ", i + 10)
        seg = src[i:j]
        assert "events[:PHASE1_POOL]" in seg and "events[:MAX_EVENTS]" not in seg

    def test_median_empty_samples_guarded(self):
        assert B._median_eval_samples([]) == {}
