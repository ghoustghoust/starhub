# -*- coding: utf-8 -*-
"""第9轮：事件合并评审 agent（用户拍板：语义判定 agent 主判，机械阈值退居兜底）。
提示词以生产真实判例为示范标准：07:13 华为对(该并)、Grok 双产品(禁并)、9/17 错标(禁并)。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


def _c(label, cat, summary="摘要", bad=False):
    return {"label": label, "category": cat, "id": label[:4],
            "summary": ("素材未提供具体数据，无法生成包含具体数据的摘要。" if bad else summary),
            "significance": "意义", "items": [{"url": "https://x/" + label[:3]}],
            "source_types": ["rss"], "score": 5.0}


class _FakeLLM:
    """第一次调用返回合并提案；第二次（二审）默认全组放行。"""
    def __init__(self, plan, verdicts="approve"):
        self._p = json.dumps(plan, ensure_ascii=False) if not isinstance(plan, str) else plan
        self._v = verdicts
        self.prompts = []
        self.n = 0

    def complete(self, messages, temperature=0.1, max_tokens=1200):
        self.n += 1
        self.prompts.append(messages[-1]["content"])
        if self.n == 1:
            return self._p
        if self._v == "approve":
            return json.dumps({"verdicts": [{"group": i, "same_event": True} for i in (1, 2, 3)]})
        return self._v


def test_huawei_pair_merged_survivor_keeps_good_fields():
    cs = [_c("华为董事长罕见公开承认 AI 芯片产能不足以满足国内需求", "infra", bad=True),
          _c("华为董事长承认AI芯片产能不足", "industry", summary="华为董事长公开承认AI芯片产量受严重限制，国内算力需求都难以满足。"),
          _c("英伟达发布新代数据中心芯片", "infra"),
          _c("谷歌上线购物摘要", "ai-products"),
          _c("Meta 发布桌面智能体", "ai-products")]
    llm = _FakeLLM({"merges": [{"keep": 2, "merge": [1], "reason": "同一表态"}]})
    out = B._llm_consolidate_events(llm, cs)
    assert len(out) == 4
    survivor = [c for c in out if "华为" in c["label"]][0]
    assert survivor["label"].startswith("华为董事长承认AI芯片产能不足")
    assert not B._is_insufficient(survivor["summary"])
    assert len(survivor["items"]) == 2


def test_survivor_degenerate_swapped_to_merged_side():
    """agent 选了劣化方做 keep：应用层仍按事实完整度换主（07:13 教训双保险）。"""
    cs = [_c("事件甲", "research", bad=True), _c("事件乙", "policy", summary="完整事实。"),
          _c("事件丙", "research"), _c("事件丁", "industry")]
    out = B._llm_consolidate_events(_FakeLLM({"merges": [{"keep": 1, "merge": [2]}]}), cs)
    assert len(out) == 3
    assert out[0]["label"] == "事件乙", "keep 方劣化时以事实完整方为准"


def test_invalid_plan_ignored_and_capped():
    cs = [_c("事件%d" % i, "research") for i in range(6)]
    plan = {"merges": [
        {"keep": 99, "merge": [1]},                      # 越界
        {"keep": 1, "merge": [1]},                        # 自并
        {"keep": 2, "merge": [3]}, {"keep": 4, "merge": [5]}, {"keep": 5, "merge": [6]},
        {"keep": 6, "merge": [2]},                        # keep 已被并掉
    ]}
    out = B._llm_consolidate_events(_FakeLLM(plan), cs)
    assert len(out) >= 6 - 4                              # 封顶生效，绝不越删
    assert len(out) < 6


def test_parse_failure_and_no_llm_fallback():
    cs = [_c("事件甲", "research"), _c("事件乙", "policy")]
    assert B._llm_consolidate_events(_FakeLLM("not json"), cs) == cs
    assert B._llm_consolidate_events(None, cs) == cs


def test_prompt_carries_real_casebook():
    cs = [_c("事件甲", "research"), _c("事件乙", "policy")]
    llm = _FakeLLM({"merges": []})
    B._llm_consolidate_events(llm, cs)
    p = llm.prompts[0]
    for marker in ("华为董事长", "Grok 图像编辑", "同一公司不同发布物", "错标",
                   "拿不准就不并", "事实最完整", "登顶流式转写榜", "一周后因缺陷被撤回",
                   "传闻与确认", "搭车", "不相关主题"):
        assert marker in p, "判例/规则缺失: %s" % marker


def test_wired_after_phase1_before_phase2():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "build_daily_insight.py"), encoding="utf-8").read()
    i_filter = src.find("_filtered.append(c)")
    i_cons = src.find("clusters = _llm_consolidate_events(llm, clusters)")
    i_p2 = src.find("# Phase 2 LLM: Top N 深度解读")
    assert -1 < i_filter < i_cons < i_p2, "评审须在 Phase1 过滤后、Phase2 深读前"


class TestReviewFixesR9:
    """对抗审查修复批的可红测试：地板/单组上限/label 锚定/significance swap/
    0-based/同组去重/畸形组隔离/bool 拒绝/原子性 + 429 轮询。"""

    def _cs(self, n=6, bad=None):
        out = []
        for i in range(n):
            s = ("素材未提供具体数据，无法生成包含具体数据的摘要。"
                 if bad == i else "事实%d，具体数据充分。" % i)
            out.append({"label": "事件%d" % i, "category": "research", "id": "e%d" % i,
                        "summary": s, "significance": "意义%d" % i,
                        "items": [{"url": "https://x/%d" % i, "title": "事件%d" % i}],
                        "source_types": ["rss"], "score": 3.0 + i})
        return out

    def _run(self, plan, cs=None):
        return B._llm_consolidate_events(_FakeLLM(plan), cs or self._cs())

    def test_min_events_floor_voids_greedy_plan(self):
        cs = self._cs(4)
        plan = {"merges": [{"keep": 1, "merge": [2, 3, 4]}]}
        out = self._run(plan, cs)
        assert len(out) == 4, "合并后事件数跌破 MIN_EVENTS 必须整案作废（地板）"

    def test_per_group_absorb_capped_at_3(self):
        plan = {"merges": [{"keep": 1, "merge": [2, 3, 4, 5]}]}
        out = self._run(plan)
        assert len(out) == 6 - 3, "单组最多并 3 条"

    def test_unanchored_agent_label_rejected(self):
        cs = self._cs(5)
        out = self._run({"merges": [{"keep": 1, "merge": [2], "label": "重磅更新"}]}, cs)
        surv = [c for c in out if c["label"] in ("事件0", "重磅更新")]
        assert surv[0]["label"] == "事件0", "无锚定词的合并名必须回退原标题"
        out2 = self._run({"merges": [{"keep": 1, "merge": [2], "label": "事件1后续"}]}, cs)
        assert any(c["label"] == "事件1后续" for c in out2), "有锚定词应接受"

    def test_significance_only_degenerate_swaps(self):
        cs = self._cs(5)
        cs[0]["significance"] = "[信息不足] 无背景"
        out = self._run({"merges": [{"keep": 1, "merge": [2]}]}, cs)
        assert len(out) == 4
        assert not B._is_insufficient(out[0]["significance"]), "significance 劣化也要触发换主"

    def test_zero_based_plan_shifted(self):
        plan = {"merges": [{"keep": 0, "merge": [1]}]}
        out = self._run(plan, self._cs(4))
        assert len(out) == 3, "0-based 方案应平移生效而非整组丢弃"

    def test_duplicate_merge_entries_deduped(self):
        plan = {"merges": [{"keep": 1, "merge": [2, 2]}]}
        out = self._run(plan, self._cs(4))
        surv = [c for c in out if c["label"] == "事件0"][0]
        assert len(surv["items"]) == 2, "同组重复项不得重复并入 items"

    def test_one_malformed_group_does_not_void_rest(self):
        plan = {"merges": [{"keep": 1, "merge": 2}, {"keep": 3, "merge": [4]}]}
        out = self._run(plan, self._cs(5))
        assert len(out) == 4, "畸形组跳过，合法组照常应用"

    def test_bool_index_rejected(self):
        plan = {"merges": [{"keep": True, "merge": [2]}]}
        out = self._run(plan, self._cs(4))
        assert len(out) == 4, "keep=true 不得被当成 1"

    def test_score_max_and_status_transfer(self):
        cs = self._cs(5)
        cs[2]["status"] = "ongoing"
        cs[2]["prev_summary"] = "昨日进展"
        out = self._run({"merges": [{"keep": 1, "merge": [3]}]}, cs)
        surv = [c for c in out if c["label"] == "事件0"][0]
        assert surv["score"] == 5.0 and surv.get("status") == "ongoing"
        assert surv.get("prev_summary") == "昨日进展"

    def test_casebook_covers_version_and_release_ranking(self):
        cs = self._cs(2)
        llm = _FakeLLM({"merges": []})
        B._llm_consolidate_events(llm, cs)
        p = llm.prompts[0]
        assert "910C" in p and "同产品不同版本" in p
        assert "源）" in p and "（1 源）" in p, "评审输入须带 items 源数信号"


class TestLLM429Polling:
    """用户点名：429 限流要轮询退避，不能立刻放弃。"""

    def _mk(self, monkeypatch, seq, poll_sec=2):
        monkeypatch.setattr(B, "_LLM_429_POLL_SEC", poll_sec, raising=False)
        llm = B._LLM("k1", extra_keys=["k2"])
        calls = []

        def fake_try(messages, temperature=0.3, max_tokens=2000):
            calls.append(1)
            r = seq[min(len(calls) - 1, len(seq) - 1)]
            return r if isinstance(r, tuple) else (r, False)
        monkeypatch.setattr(llm, "_try", fake_try)
        monkeypatch.setattr(B.time, "sleep", lambda s: None)
        return llm, calls

    def test_recovers_after_429_rounds(self, monkeypatch):
        llm, calls = self._mk(monkeypatch, [(None, True), (None, True), "结果"])
        assert llm.complete([{"role": "user", "content": "x"}]) == "结果"
        assert len(calls) == 3

    def test_gives_up_at_deadline_with_empty(self, monkeypatch):
        import time as _t
        clock = [1000.0]
        monkeypatch.setattr(_t, "time", lambda: clock[0])
        llm, calls = self._mk(monkeypatch, [(None, True)])
        orig_sleep = B.time.sleep

        def adv_sleep(s):
            clock[0] += max(s, 1)
        monkeypatch.setattr(B.time, "sleep", adv_sleep)
        assert llm.complete([{"role": "user", "content": "x"}]) == ""
        assert len(calls) >= 3, "预算内必须多轮轮询而非一次即弃"


class TestVerificationPass:
    """05:48 期实证：评审 agent 把「英王查尔斯警告」并入「Noam Brown 言论」
    （主题同、主体不同）。合并方案须过二审：主体+发生同一才放行，二审失败整组作废。"""

    def _cs(self):
        return [
            {"label": "OpenAI研究员称气隙隔离难防失控AI", "category": "policy", "id": "a",
             "summary": "Noam Brown在播客提出气隙隔离失效观点引发安全圈激辩。",
             "significance": "g", "items": [{"url": "https://a/1"}], "source_types": ["rss"], "score": 6.0},
            {"label": "英王查尔斯三世警告AI生存性威胁", "category": "policy", "id": "b",
             "summary": "英王查尔斯敦促AI巨头加强管控生存性风险。",
             "significance": "g", "items": [{"url": "https://b/1"}], "source_types": ["rss"], "score": 5.0},
            {"label": "英伟达发布新代数据中心芯片", "category": "industry", "id": "c",
             "summary": "英伟达发布Rubin架构芯片。",
             "significance": "g", "items": [{"url": "https://c/1"}], "source_types": ["rss"], "score": 4.0},
            {"label": "某足球俱乐部宣布主教练下课", "category": "industry", "id": "d",
             "summary": "俱乐部官方公告换帅。",
             "significance": "g", "items": [{"url": "https://d/1"}], "source_types": ["rss"], "score": 3.0},
            {"label": "Meta 发布桌面智能体", "category": "ai-products", "id": "e",
             "summary": "Meta Muse for Mac 上线。",
             "significance": "g", "items": [{"url": "https://e/1"}], "source_types": ["rss"], "score": 2.0},
        ]

    def test_verifier_rejects_different_subject(self, monkeypatch):
        calls = []

        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                calls.append(messages[-1]["content"])
                if self.n == 1:  # 提案：把 1 和 2 并了
                    return json.dumps({"merges": [{"keep": 1, "merge": [2], "reason": "同论战"}]},
                                      ensure_ascii=False)
                # 二审：判定主体不同
                return json.dumps({"verdicts": [{"group": 1, "same_event": False,
                                                 "reason": "主体不同：研究员vs英王"}]},
                                  ensure_ascii=False)
        out = B._llm_consolidate_events(Seq(), self._cs())
        assert len(out) == 5, "二审否决的合并必须撤销"
        assert len(calls) == 2, "必须发生二审调用"

    def test_verifier_approves_keeps_merge(self, monkeypatch):
        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                if self.n == 1:
                    return json.dumps({"merges": [{"keep": 1, "merge": [2]}]}, ensure_ascii=False)
                return json.dumps({"verdicts": [{"group": 1, "same_event": True,
                                                 "reason": "同一表态及反响"}]}, ensure_ascii=False)
        out = B._llm_consolidate_events(Seq(), self._cs())
        assert len(out) == 4

    def test_verifier_unparseable_voids_all_merges(self, monkeypatch):
        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                if self.n == 1:
                    return json.dumps({"merges": [{"keep": 1, "merge": [2]}]}, ensure_ascii=False)
                return "garbage"
        out = B._llm_consolidate_events(Seq(), self._cs())
        assert len(out) == 5, "二审不可解析时保守作废（错并比漏并严重）"

    def test_casebook_has_today_mismerge_examples(self):
        cs = self._cs()[:2]
        llm = _FakeLLM({"merges": []})
        B._llm_consolidate_events(llm, cs)
        p = llm.prompts[0]
        assert "英王查尔斯" in p and "不同主体的各自表态" in p and "05:48 期误并实证" in p


class TestVerifyEdgeCases:
    """定向审查修复：二审组号 0-based 漂移、多组部分否决、字符串 false、infra 全否决。"""

    def _cs(self):
        return TestVerificationPass()._cs()

    def test_zero_based_verdict_group_shifted(self):
        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                if self.n == 1:
                    return json.dumps({"merges": [{"keep": 1, "merge": [2]},
                                                  {"keep": 3, "merge": [4]}]},
                                      ensure_ascii=False)
                return json.dumps({"verdicts": [{"group": 0, "same_event": False},
                                                {"group": 1, "same_event": True}]})
        out = B._llm_consolidate_events(Seq(), self._cs())
        # 平移后：组1(1<-2 英王误并)否决、组2(3<-4)放行应用
        assert len(out) == 4
        assert not any(c["label"].startswith("OpenAI研究员") and "英王" in
                       " ".join(i.get("title", "") for i in c["items"]) for c in out)

    def test_multi_group_partial_rejection_keeps_dropped_consistent(self):
        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                if self.n == 1:
                    return json.dumps({"merges": [{"keep": 1, "merge": [2]},
                                                  {"keep": 3, "merge": [4]}]},
                                      ensure_ascii=False)
                return json.dumps({"verdicts": [{"group": 1, "same_event": False},
                                                {"group": 2, "same_event": True}]})
        cs = self._cs()
        out = B._llm_consolidate_events(Seq(), cs)
        assert len(out) == 4
        labels = [c["label"] for c in out]
        assert any(l.startswith("OpenAI研究员") for l in labels), "被否决组的成员必须都还在"
        assert any(l.startswith("英王") for l in labels)
        assert not any(l.startswith("某足球") for l in labels), "放行组应正常合并（被并方消失、保留方存续）"

    def test_string_false_verdict_rejected(self):
        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                if self.n == 1:
                    return json.dumps({"merges": [{"keep": 1, "merge": [2]}]},
                                      ensure_ascii=False)
                return json.dumps({"verdicts": [{"group": 1, "same_event": "false"}]})
        out = B._llm_consolidate_events(Seq(), self._cs())
        assert len(out) == 5, "same_event 非布尔 true 一律视为否决"

    def test_empty_verdict_response_voids_all(self):
        class Seq:
            def __init__(self): self.n = 0
            def complete(self, messages, temperature=0.1, max_tokens=1200):
                self.n += 1
                if self.n == 1:
                    return json.dumps({"merges": [{"keep": 1, "merge": [2]}]},
                                      ensure_ascii=False)
                return ""
        out = B._llm_consolidate_events(Seq(), self._cs())
        assert len(out) == 5
