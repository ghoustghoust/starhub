# -*- coding: utf-8 -*-
"""FIX-2: RAGAS JSON 解析容错测试。"""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class TestRobustParseJson(unittest.TestCase):
    """_robust_parse_json 应处理截断、前后缀噪音。"""

    def test_valid_json(self):
        text = ('{"context_coverage": 0.8, "faithfulness": 0.9,'
                ' "relevance": 0.7, "feedback": "good", "weak_events": []}')
        result = B._robust_parse_json(text)
        self.assertIsInstance(result, dict)
        self.assertAlmostEqual(result["context_coverage"], 0.8)

    def test_json_with_prefix_text(self):
        text = ('好的，以下是评估结果：\n'
                '{"context_coverage": 0.8, "faithfulness": 0.9,'
                ' "relevance": 0.7, "feedback": "ok", "weak_events": []}')
        result = B._robust_parse_json(text)
        self.assertIsInstance(result, dict)
        self.assertAlmostEqual(result["faithfulness"], 0.9)

    def test_truncated_json_missing_braces(self):
        text = ('{"context_coverage": 0.8, "faithfulness": 0.9,'
                ' "relevance": 0.7, "feedback": "needs improvement",'
                ' "weak_events": [2')
        result = B._robust_parse_json(text)
        self.assertIsInstance(result, dict)
        self.assertAlmostEqual(result["context_coverage"], 0.8)

    def test_empty_input(self):
        self.assertIsNone(B._robust_parse_json(""))
        self.assertIsNone(B._robust_parse_json(None))

    def test_no_json_at_all(self):
        self.assertIsNone(B._robust_parse_json("just plain text"))

    def test_json_with_suffix(self):
        text = ('{"context_coverage": 0.7, "faithfulness": 0.8,'
                ' "relevance": 0.6, "feedback": "ok", "weak_events": []}'
                '\n以上是评估结果。')
        result = B._robust_parse_json(text)
        self.assertIsInstance(result, dict)
        self.assertAlmostEqual(result["context_coverage"], 0.7)


if __name__ == "__main__":
    unittest.main()


class TestAdoptionNoiseGuard:
    """01:12 期实证：复评基线漂移 0.05 内，'改善即采纳'会采纳劣化版。
    要求修正版超过 pre_score + 0.02 才采纳，且修正轮数回退 1。"""

    def test_adoption_margin_and_single_round(self):
        import os
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "pre_score + 0.02" in src
        assert "RAGAS_MAX_CORRECTIONS = 1" in src


class TestFaithFinalGate:
    """R2: faith 终局防线——采纳须 faith 不劣化 + 修正环后二次核查 + 证据包跨事件禁令。"""

    def _src(self):
        import os
        return open(os.path.join(os.path.dirname(__file__), "..", "..",
                                 "build_daily_insight.py"), encoding="utf-8").read()

    def test_adoption_requires_faith_non_degrade(self):
        src = self._src()
        assert "pre_faith - 0.02" in src, "采纳闸必须含 faith 不劣化约束"

    def test_evidence_header_bans_cross_event(self):
        assert "严禁把其事实或数字嫁接到本事件主角" in self._src()

    def test_recheck_helper_behavior(self):
        import build_daily_insight as B
        calls = []
        bak = B._verify_faithfulness
        B._verify_faithfulness = lambda llm, clusters, ctx=None: calls.append(1)
        try:
            assert B._final_faith_recheck(None, [{"label": "x"}], []) is False
            assert calls == []
            assert B._final_faith_recheck(object(), [{"label": "x"}], []) is True
            assert len(calls) == 1
            assert B._final_faith_recheck(object(), [], []) is False

            def boom(llm, clusters, ctx=None):
                raise RuntimeError("llm down")
            B._verify_faithfulness = boom
            assert B._final_faith_recheck(object(), [{"label": "x"}], []) is False
        finally:
            B._verify_faithfulness = bak

    def test_recheck_wired_after_correction_dedup(self):
        src = self._src()
        i_dedup = src.find("去重必须在其后再跑一轮")
        i_recheck = src.find("_final_faith_recheck(llm, clusters, retrieved_for_ragas)")
        i_score = src.find("c[\"editor_score\"] = _quick_score_event(c, yesterday_labels=_yday_labels)", i_recheck)
        assert -1 < i_dedup < i_recheck < i_score, "终局核查须在修正后去重与分数刷新之间"


class TestAdoptionRollback:
    """P1-1 实证：_self_correct_events 原地写回 clusters，
    仅拒绝采纳不回滚文本 = 装饰品；拒绝分支必须快照还原。"""

    def _run(self, monkeypatch, faith_after):
        import build_daily_insight as B
        clusters = [{"label": "L1", "summary": "原摘要", "significance": "意义",
                     "category": "ai-models", "score": 5}]
        evals = [
            {"overall": 0.60, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.60, "feedback": "f", "weak_events": [1]},
            {"overall": 0.78, "context_coverage": 0.80, "faithfulness": faith_after,
             "relevance": 0.78, "feedback": "f", "weak_events": []},
            {"overall": 0.60, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.60, "feedback": "f", "weak_events": [1]},
        ]

        def fake_correct(llm, cls, ctx, ev):
            cls[0]["summary"] = "劣化摘要"
            return cls

        # T2 后非 CV 路径每组评估双采样：每群消费 2 次再前进
        state = {"i": 0, "r": 0}

        def fake_eval(llm, cls, theme, ctx, prompt_variant=None):
            d = dict(evals[min(state["i"], 2)])
            state["r"] += 1
            if state["r"] % 2 == 0:
                state["i"] += 1
            return d

        monkeypatch.setattr(B, "_self_correct_events", fake_correct)
        monkeypatch.setattr(B, "_evaluate_report_quality", fake_eval)
        chunks = [{"text": "素材内容" * 60, "title": "ctx", "source_type": "rss",
                   "retrieval_score": 1.0}]
        out_clusters, ev = B._ragas_evaluate_and_correct(
            object(), clusters, "主题", chunks, {})
        return out_clusters, ev

    def test_rejected_correction_restores_original_text(self, monkeypatch):
        out, ev = self._run(monkeypatch, faith_after=0.55)
        assert out[0]["summary"] == "原摘要", "拒绝采纳后必须回滚修正环的原文本"
        assert ev["faithfulness"] == 0.95

    def test_adopted_correction_keeps_new_text(self, monkeypatch):
        out, ev = self._run(monkeypatch, faith_after=0.96)
        assert out[0]["summary"] == "劣化摘要", "达标采纳时保留修正文本"


class TestRollbackNoNullInjection:
    """第7轮回归：回滚快照不得把「原本不存在的字段」写成 None
    （test_daily_insight 9i 实证：significance=None 让复评 prompt 构建 TypeError）。"""

    def test_missing_key_not_materialized_as_none(self, monkeypatch):
        import build_daily_insight as B
        clusters = [{"label": "L", "summary": "原摘要", "category": "research",
                     "score": 5}]  # 注意：无 significance 键
        groups = [
            {"overall": 0.60, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.60, "feedback": "f", "weak_events": [1]},
            {"overall": 0.78, "context_coverage": 0.80, "faithfulness": 0.55,
             "relevance": 0.78, "feedback": "f", "weak_events": []},
            {"overall": 0.60, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.60, "feedback": "f", "weak_events": [1]},
        ]
        state = {"i": 0, "r": 0}

        def fake_correct(llm, cls, ctx, ev):
            cls[0]["summary"] = "劣化"
            return cls

        def fake_eval(llm, cls, theme, ctx, prompt_variant=None):
            d = dict(groups[min(state["i"], 2)])
            state["r"] += 1
            if state["r"] % 2 == 0:
                state["i"] += 1
            return d

        monkeypatch.setattr(B, "_self_correct_events", fake_correct)
        monkeypatch.setattr(B, "_evaluate_report_quality", fake_eval)
        out, ev = B._ragas_evaluate_and_correct(
            object(), clusters, "t",
            [{"text": "素材" * 80, "title": "x", "source_type": "rss",
              "retrieval_score": 1.0}], {})
        assert out[0]["summary"] == "原摘要"
        assert "significance" not in out[0], \
            "回滚不能把缺失字段物化为 None——下游一律 get(k, \"\") 拿键存在即真"


class TestNoRedundantReeval:
    """04:00 期实证：拒绝采纳回滚后，iter1 对同一内容重评，judge 全量降级 agnes
    打出全维 0.50 覆盖掉 iter0 的 0.80——回滚即终稿，重评是纯噪声采样。"""

    def _fake(self, monkeypatch, groups):
        import build_daily_insight as B
        state = {"calls": 0}

        def fake_eval(llm, cls, theme, ctx, prompt_variant=None):
            d = dict(groups[min(state["calls"] // 2, len(groups) - 1)])
            state["calls"] += 1
            return d

        def fake_correct(llm, cls, ctx, ev):
            cls[0]["summary"] = "劣化"
            return cls
        monkeypatch.setattr(B, "_evaluate_report_quality", fake_eval)
        monkeypatch.setattr(B, "_self_correct_events", fake_correct)
        return state

    def test_reject_breaks_without_reeval(self, monkeypatch):
        import build_daily_insight as B
        groups = [
            {"overall": 0.60, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.60, "feedback": "f", "weak_events": [1]},
            {"overall": 0.78, "context_coverage": 0.80, "faithfulness": 0.55,
             "relevance": 0.78, "feedback": "f", "weak_events": []},
            {"overall": 0.50, "context_coverage": 0.50, "faithfulness": 0.50,
             "relevance": 0.50, "feedback": "collapse", "weak_events": []},
        ]
        st = self._fake(monkeypatch, groups)
        clusters = [{"label": "L", "summary": "原摘要", "significance": "g",
                     "category": "research", "score": 5}]
        out, ev = B._ragas_evaluate_and_correct(
            object(), clusters, "t", [{"text": "素材" * 80}], {})
        assert out[0]["summary"] == "原摘要"
        assert ev["faithfulness"] == 0.95, "必须保留回滚内容对应的 iter0 评估"
        assert st["calls"] == 4, "回滚后不得再发起第三组重评（每多一组=多一次降级采样机会）"

    def test_adopt_dims_fail_breaks_without_reeval(self, monkeypatch):
        import build_daily_insight as B
        groups = [
            {"overall": 0.60, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.60, "feedback": "f", "weak_events": [1]},
            {"overall": 0.75, "context_coverage": 0.60, "faithfulness": 0.95,
             "relevance": 0.78, "feedback": "f", "weak_events": []},
            {"overall": 0.50, "context_coverage": 0.50, "faithfulness": 0.50,
             "relevance": 0.50, "feedback": "collapse", "weak_events": []},
        ]
        st = self._fake(monkeypatch, groups)
        clusters = [{"label": "L", "summary": "原摘要", "significance": "g",
                     "category": "research", "score": 5}]
        out, ev = B._ragas_evaluate_and_correct(
            object(), clusters, "t", [{"text": "素材" * 80}], {})
        assert out[0]["summary"] == "劣化", "采纳分支保留修正文本"
        assert ev["overall"] == 0.75 and st["calls"] == 4


class TestEvidenceDomainAlignment:
    """14:29 期实证 faith 0.50：judge 只看 top80 chunk，事件证据在第 81-200 位
    ——判定域必须覆盖装配域（同一 200 池），否则真实报道被判幻觉。"""

    def test_ragas_context_covers_full_pool(self):
        import build_daily_insight as B
        chunks = [{"text": "正文%d" % i, "title": "T%d" % i, "source_type": "rss",
                   "retrieval_score": 1.0 - i * 0.001} for i in range(200)]
        ctx = B._build_ragas_context([{"label": "x"}], chunks)
        assert "T199" in ctx, "评估上下文须覆盖全池，含尾部低分 chunk"
        assert ctx.count("---") >= 190

    def test_verify_faithfulness_covers_full_pool(self):
        import build_daily_insight as B
        seen = {}

        class L:
            def complete(self, messages, temperature=0.1, max_tokens=2500):
                seen["p"] = messages[-1]["content"]
                return "{}"
        chunks = [{"text": "正文%d" % i, "title": "T%d" % i, "source_type": "rss",
                   "retrieval_score": 1.0 - i * 0.001} for i in range(200)]
        B._verify_faithfulness(L(), [{"label": "a", "summary": "s", "items": []}], chunks)
        assert "T199" in seen["p"], "事实核查素材域须与评估域一致（同一 200 池）"

    def test_verify_faithfulness_depth_matches_judge(self):
        """核查编辑与 judge 同用 1000 字深度：700-1000 区间的支撑必须可见。"""
        import build_daily_insight as B
        seen = {}

        class L:
            def complete(self, messages, temperature=0.1, max_tokens=2500):
                seen["p"] = messages[-1]["content"]
                return "{}"
        filler = "经" * 900 + "关键证据句"
        chunks = [{"text": filler, "title": "T", "source_type": "rss",
                   "retrieval_score": 1.0}]
        B._verify_faithfulness(L(), [{"label": "a", "summary": "s", "items": []}], chunks)
        assert "关键证据句" in seen["p"], "900-1000 字区间的证据不得被截掉（旧 [:700] 输出缺陷）"

    def test_meta_records_context_footprint(self, monkeypatch):
        import build_daily_insight as B
        groups = [{"overall": 0.80, "context_coverage": 0.8, "faithfulness": 0.9,
                   "relevance": 0.8, "feedback": "f", "weak_events": []}]

        def fake_eval(llm, cls, theme, ctx, prompt_variant=None):
            return dict(groups[0])
        monkeypatch.setattr(B, "_evaluate_report_quality", fake_eval)
        clusters = [{"label": "L", "summary": "s", "significance": "g",
                     "category": "research", "score": 5}]
        chunks = [{"text": "正文" * 500, "title": "T%d" % i, "source_type": "rss",
                   "retrieval_score": 1.0 - i * 0.001} for i in range(120)]
        _, ev = B._ragas_evaluate_and_correct(object(), clusters, "t", chunks, {})
        assert ev["meta"]["context_chunks"] >= 100
        assert ev["meta"]["context_chars"] > 10000


class _AgnesSentinel:
    model = "agnes-2.5-flash"
    _call_log = []


class _BoomMimo:
    def __init__(self, *a, **k):
        raise AssertionError("默认配置下不得构造 mimo 主判（它会白打一次 403 并谎报 judge 身份）")


class TestJudgeIsAgnesByConfig:
    """09-19 数据核实：call_log 记录以来所有真实打分全部由 agnes 完成
    （mimo 403 从未生效，旧 meta"judge=mimo"是标签谎报）。
    生产配置必须与实际一致：judge_provider=agnes，不再伪装 mimo→agnes 链。

    注意 CI 门禁 B 会把 AGNES_API_KEY 等全部清空（防测试打网络），
    所以这里必须注入 _init_llm 哨兵只验"分支选择"，绝不能依赖真实 key。"""

    def test_production_config_resolves_plain_agnes(self, monkeypatch):
        import json as _json
        import os
        import build_daily_insight as B
        cfg = _json.load(open(os.path.join(os.path.dirname(__file__), "..", "..",
                                           "build_config.json"), encoding="utf-8"))
        assert cfg.get("daily_insight_judge_provider") == "agnes", \
            "配置仍钉 mimo 会让每期白打一次 403 并谎报 judge 身份"
        monkeypatch.setattr(B, "_init_llm", lambda: _AgnesSentinel())
        monkeypatch.setattr(B, "_MimoLLM", _BoomMimo)
        llm = B._init_judge_llm(cfg)
        assert llm is not None and not isinstance(llm, B._FallbackJudgeLLM)
        assert "agnes" in getattr(llm, "model", "")

    def test_keyless_ci_env_degrades_without_crash(self, monkeypatch):
        """无 key 是 CI 门禁 B 的真实环境：拿不到 agnes 就返回 None，不得抛错。"""
        import build_daily_insight as B
        monkeypatch.setattr(B, "_init_llm", lambda: None)
        assert B._init_judge_llm({"daily_insight_judge_provider": "agnes"}) is None
