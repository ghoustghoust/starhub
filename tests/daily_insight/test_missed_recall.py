# -*- coding: utf-8 -*-
"""G1: judge missed 清单回收 → 定向检索 → 新增事件闭环（此前 missed 被解析层丢弃）。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class _LLM:
    def __init__(self, payload):
        self._p = json.dumps(payload, ensure_ascii=False)

    def complete(self, messages, temperature=0.2, max_tokens=4000):
        return self._p


_JUDGE_V1 = {"reasoning": {"coverage": {"missed": ["微软高管称AI抓取为最大劳动窃取",
                                                "西班牙数据泄露"],
                                       "confidence": "high"},
                           "faithfulness": {"confidence": "high"},
                           "relevance": {"confidence": "medium"}},
             "scores": {"context_coverage": 0.6, "faithfulness": 0.9, "relevance": 0.7},
             "feedback": "f", "weak_events": [1]}
_JUDGE_V2 = {"reasoning": {"coverage": {"missed_points": ["英王查尔斯AI安全警告"],
                                        "confidence": "high"},
                           "faithfulness": {"confidence": "high"},
                           "relevance": {"confidence": "high"}},
             "scores": {"context_coverage": 0.6, "faithfulness": 0.9, "relevance": 0.7},
             "feedback": "f", "weak_events": []}


def test_parse_missed_v1_and_v2():
    r1 = B._evaluate_report_quality(_LLM(_JUDGE_V1), [{"label": "x", "summary": "s"}],
                                    "t", "ctx", prompt_variant="v1")
    assert r1["missed_points"] == ["微软高管称AI抓取为最大劳动窃取", "西班牙数据泄露"]
    r2 = B._evaluate_report_quality(_LLM(_JUDGE_V2), [{"label": "x", "summary": "s"}],
                                    "t", "ctx", prompt_variant="v2")
    assert r2["missed_points"] == ["英王查尔斯AI安全警告"]


def test_median_keeps_cooccurring_missed_points():
    """共现降噪：两样本共享的漏点保留，孤本丢弃（v1/v2 措辞差异大的并集是噪声）。"""
    a = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
         "missed_points": ["英王查尔斯AI安全警告", "量子计算新进展甲"], "weak_events": [], "feedback": ""}
    b = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
         "missed_points": ["英王查尔斯ai安全警告", "足球俱乐部换帅乙"], "weak_events": [], "feedback": ""}
    m = B._median_eval_samples([a, b])
    assert m["missed_points"] == ["英王查尔斯AI安全警告"], "共现判定须大小写不敏感"


def _chunk(t, u, score):
    return {"title": t, "url": u, "text": t + " 正文素材内容", "source_type": "rss",
            "retrieval_score": score}


class TestRecoverMissedEvents:
    def setup_method(self):
        self._bp1 = B._llm_phase1

    def teardown_method(self):
        B._llm_phase1 = self._bp1

    def _existing(self, n=6):
        return [{"label": "既有事件%d" % i, "summary": "摘要%d" % i, "score": 5.0,
                 "items": [], "source_types": ["rss"]} for i in range(n)]

    def test_happy_path_adds_new_event_with_links(self, monkeypatch):
        hits = [_chunk("微软高管称AI抓取为史上最大劳动窃取", "https://m/1", 0.03),
                _chunk("微软CEO补充AI抓取争议细节", "https://m/2", 0.028)]
        monkeypatch.setattr(B, "_hybrid_retrieve", lambda idx, ch, q, top_k=15: hits)
        def fake_p1(llm, clusters, global_context=None):
            return {"theme": "", "events": [{"event_num": 1, "label": "微软高管批AI抓取",
                     "category": "policy", "summary": "微软CEO称AI抓取构成史上最大劳动窃取，"
                     "引发内容平台与模型厂商对立。", "significance": "重塑数据授权格局。",
                     "key_links": ["https://m/1"]}]}
        monkeypatch.setattr(B, "_llm_phase1", fake_p1)
        out = B._recover_missed_events(object(), self._existing(),
                                       ["微软高管称AI抓取为最大劳动窃取"], None, None)
        assert len(out) == 1
        assert out[0]["key_links"] == ["https://m/1"]
        assert not B._is_insufficient(out[0]["summary"])

    def test_already_covered_missed_not_added(self, monkeypatch):
        hits = [_chunk("OpenAI发布GPT61模型引发开发者讨论", "https://d/1", 0.03)]
        monkeypatch.setattr(B, "_hybrid_retrieve", lambda idx, ch, q, top_k=15: hits)
        called = []
        def spy_p1(llm, clusters, global_context=None):
            called.append(1)
        monkeypatch.setattr(B, "_llm_phase1", spy_p1)
        existing = self._existing()
        existing[0]["label"] = "OpenAI发布GPT61模型引发开发者讨论"
        out = B._recover_missed_events(object(), existing, ["某missed"], None, None)
        assert out == [] and called == []

    def test_budget_full_skips(self, monkeypatch):
        monkeypatch.setattr(B, "_hybrid_retrieve",
                            lambda idx, ch, q, top_k=15: [_chunk("OpenAI发布全新企业搜索产品接入办公场景", "https://a", 0.03)])
        out = B._recover_missed_events(object(), self._existing(B.PHASE1_POOL),
                                       ["OpenAI企业搜索发布"], None, None)
        assert out == [], "池已达 PHASE1_POOL 才该空转（MAX_EVENTS 判断是旧缺陷）"

    def test_low_score_floor_blocks_hallucination(self, monkeypatch):
        hits = [_chunk("OpenAI发布高相关新模型", "https://h/1", 0.03),
                _chunk("OpenAI发布低相关杂讯", "https://h/2", 0.001)]
        monkeypatch.setattr(B, "_hybrid_retrieve", lambda idx, ch, q, top_k=15: hits)
        seen = {}
        def cap_p1(llm, clusters, global_context=None):
            seen["titles"] = [c["label"] for c in clusters]
            return {"theme": "", "events": [{"event_num": i + 1, "label": c["label"],
                     "category": "industry", "summary": "摘要内容具体数据充分。",
                     "significance": "意义", "key_links": ["https://h/1"]}
                     for i, c in enumerate(clusters)]}
        monkeypatch.setattr(B, "_llm_phase1", cap_p1)
        B._recover_missed_events(object(), self._existing(), ["missed"], None, None)
        assert "OpenAI发布低相关杂讯" not in seen.get("titles", [])
        assert "OpenAI发布高相关新模型" in seen.get("titles", []), "下限闸不应误杀高分项"

    def test_no_llm_or_no_missed_degrades_empty(self):
        assert B._recover_missed_events(None, self._existing(), ["m"], None, None) == []
        assert B._recover_missed_events(object(), self._existing(), [], None, None) == []

    def test_retrieve_exception_degrades_empty(self, monkeypatch):
        def boom(idx, ch, q, top_k=15):
            raise RuntimeError("index down")
        monkeypatch.setattr(B, "_hybrid_retrieve", boom)
        assert B._recover_missed_events(object(), self._existing(), ["m"], None, None) == []


def test_recovery_wired_before_final_dedup():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "build_daily_insight.py"), encoding="utf-8").read()
    i_rec = src.find("_recover_missed_events(llm, clusters")
    i_dedup = src.find("去重必须在其后再跑一轮")
    i_drop = src.find("clusters = _drop_insufficient(clusters)\n    clusters = _order_events_for_output(clusters)")
    assert -1 < i_rec < i_dedup < i_drop, "回收必须发生在终局去重之前、占位闸之前"


class TestReviewFixesG1:
    """对抗审查修复：id 缺失(P0)/_yday_labels 未绑定(P1-1)/AI 相关性与池内约束(P1-2)/
    预算闸位置(P2-1)/missed 清洗与共现降噪(P2-3)。"""

    def _hits(self, monkeypatch, title="OpenAI发布企业搜索产品接入办公套件", url="https://p/1"):
        hits = [{"title": title, "url": url, "text": title + " 正文", "source_type": "rss",
                 "retrieval_score": 0.03}]
        monkeypatch.setattr(B, "_hybrid_retrieve", lambda idx, ch, q, top_k=15: hits)
        def fake_p1(llm, clusters, global_context=None):
            return {"theme": "", "events": [{"event_num": i + 1, "label": c["label"],
                     "category": "ai-products", "summary": "OpenAI 面向企业发布搜索产品，"
                     "接入办公套件并开放 API。", "significance": "重塑企业检索格局。",
                     "key_links": [c["items"][0]["url"]]} for i, c in enumerate(clusters)]}
        monkeypatch.setattr(B, "_llm_phase1", fake_p1)
        return hits

    def test_recovered_event_has_id(self, monkeypatch):
        self._hits(monkeypatch)
        out = B._recover_missed_events(object(), _six(), ["OpenAI企业搜索发布"], None, None)
        assert out and all(c.get("id") for c in out), "回收事件必须带 id，否则写盘 KeyError"

    def test_write_insight_json_tolerates_missing_id(self, monkeypatch, tmp_path):
        monkeypatch.setattr(B, "INSIGHT_FILE", str(tmp_path / "di.json"))
        monkeypatch.setattr(B, "_load_history", lambda: {"days": []})
        monkeypatch.setattr(B, "_atomic_write_json", lambda *a, **k: None)
        ev = [{"label": "L", "summary": "s", "significance": "", "category": "research",
               "items": [], "source_types": ["rss"], "score": 1.0}]  # 无 id
        B._write_insight_json(ev, "T", False, {})  # 不得抛 KeyError

    def test_non_ai_missed_candidate_rejected(self, monkeypatch):
        self._hits(monkeypatch, title="某足球俱乐部宣布主教练下课")
        out = B._recover_missed_events(object(), _six(), ["俱乐部主教练下课"], None, None)
        assert out == [], "非 AI/科技主题回收必须被相关性闸挡下（rel 保护）"

    def test_out_of_pool_hits_rejected(self, monkeypatch):
        self._hits(monkeypatch)
        out = B._recover_missed_events(object(), _six(), ["OpenAI企业搜索发布"], None, None,
                                       pool=set())
        assert out == [], "judge 只看池内 200 条：池外证据回收会造成 faith 判定域分叉"

    def test_garbage_missed_strings_cleaned(self, monkeypatch):
        hits = self._hits(monkeypatch)
        B._recover_missed_events(object(), _six(), ["无", "已全覆盖"], None, None)
        assert True  # 不崩即可；配合 queries 为空的提前返回断言：
        out = B._recover_missed_events(object(), _six(), ["无"], None, None)
        assert out == []

    def test_budget_gate_allows_recovery_at_12(self, monkeypatch):
        self._hits(monkeypatch)
        out = B._recover_missed_events(object(), _six(12), ["OpenAI企业搜索发布"], None, None)
        assert len(out) == 1, "输出 12 事件时回收仍应工作（终局 cap 负责取舍），预算闸在 PHASE1_POOL"

    def test_missed_points_need_cooccurrence(self):
        a = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": ["共同漏点", "a独有甲"], "weak_events": [], "feedback": ""}
        b = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": ["共同漏点", "b独有乙"], "weak_events": [], "feedback": ""}
        m = B._median_eval_samples([a, b])
        assert m["missed_points"] == ["共同漏点"], "双样本以上漏点须共现降噪（v1/v2 措辞差异大，噪声并集不可用）"

    def test_yday_labels_initialized_before_phase1(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        i_init = src.find("_yday_labels = set()")
        i_gate = src.find("_yday_labels = {(e.get(\"label\")")
        i_refresh = src.find("if \"editor_score\" in c or c.get(\"summary\"):")
        assert -1 < i_init < i_gate < i_refresh, "须先于 Phase2 门槛块初始化，防 p1 失败路径 NameError"


def _six(n=6):
    return [{"label": "既有事件%d" % i, "summary": "摘要%d" % i, "score": 5.0,
             "items": [], "source_types": ["rss"], "id": "evt_%02d" % i} for i in range(n)]


class TestFuzzyCooccurrence:
    """04:38 期实证：精确字符串共现把 missed 饿死成 0 条——改模糊分组。"""

    def test_wording_drift_same_topic_groups(self):
        a = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": ["英王查尔斯AI安全警告"], "weak_events": [], "feedback": ""}
        b = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": ["英国国王查尔斯就AI安全发出警告"], "weak_events": [], "feedback": ""}
        m = B._median_eval_samples([a, b])
        assert len(m["missed_points"]) == 1, "措辞漂移的同主题漏点应归为一组并共现保留"

    def test_all_disjoint_falls_back_to_singletons(self):
        a = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": ["量子计算突破新进展"], "weak_events": [], "feedback": ""}
        b = {"overall": 0.7, "context_coverage": 0.7, "faithfulness": 0.9, "relevance": 0.7,
             "missed_points": ["某足球俱乐部换帅风波"], "weak_events": [], "feedback": ""}
        m = B._median_eval_samples([a, b])
        assert len(m["missed_points"]) == 2, "无共现时兜底放行孤本（下游池内/AI/查重闸负责过滤）"

    def test_quality_json_exposes_missed_points(self, monkeypatch, tmp_path):
        import json as _json
        monkeypatch.setattr(B, "INSIGHT_FILE", str(tmp_path / "di.json"))
        monkeypatch.setattr(B, "_load_history", lambda: {"days": []})
        written = {}
        monkeypatch.setattr(B, "_atomic_write_json",
                            lambda path, data: written.update({path: data}))
        ev = [{"label": "L", "summary": "s", "significance": "", "category": "research",
               "items": [], "source_types": ["rss"], "score": 1.0, "id": "e1"}]
        B._write_insight_json(ev, "T", False,
                              {"overall": 0.8, "context_coverage": 0.75,
                               "faithfulness": 0.9, "relevance": 0.7,
                               "missed_points": ["漏点甲"], "feedback": ""})
        data = list(written.values())[0]
        assert data["quality"]["missed_points"] == ["漏点甲"]
