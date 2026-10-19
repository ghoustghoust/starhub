# -*- coding: utf-8 -*-
"""FIX-3: Phase 1 后跨源去重测试。"""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B

# 旧词袋测试类不 mock embedding：本地若有真实 key 会误发 SiliconFlow 请求
os.environ.pop("SILICONFLOW_API_KEY", None)

B.NOW_BJ = B._now_bj()


class TestDeduplicateAfterPhase1(unittest.TestCase):
    """_deduplicate_after_phase1 应合并同 category 且标签相似的事件。"""

    def test_same_category_high_jaccard_merged(self):
        """同 category + Jaccard>=0.5 → 合并。"""
        clusters = [
            {"label": "Anthropic 用 Claude Code 从零重写 Claude Design",
             "category": "product", "items": [{"title": "a"}],
             "source_types": {"rss"}, "score": 5},
            {"label": "Anthropic 用自研工具重写 Claude Design",
             "category": "product", "items": [{"title": "b"}],
             "source_types": {"aihot"}, "score": 3},
        ]
        result = B._deduplicate_after_phase1(clusters)
        self.assertEqual(len(result), 1, "应合并为 1 个事件")
        self.assertEqual(len(result[0]["items"]), 2)

    def test_different_category_not_merged(self):
        """不同 category → 不合并。"""
        clusters = [
            {"label": "OpenAI 发布 GPT-6", "category": "model",
             "items": [{"title": "a"}], "source_types": {"rss"}, "score": 5},
            {"label": "OpenAI 发布 GPT-6 引发讨论", "category": "policy",
             "items": [{"title": "b"}], "source_types": {"aihot"}, "score": 3},
        ]
        result = B._deduplicate_after_phase1(clusters)
        self.assertEqual(len(result), 2, "不同 category 不应合并")

    def test_low_jaccard_not_merged(self):
        """同 category 但 Jaccard<0.5 → 不合并。"""
        clusters = [
            {"label": "Claude Design 重写", "category": "product",
             "items": [{"title": "a"}], "source_types": {"rss"}, "score": 5},
            {"label": "量子计算重大突破", "category": "product",
             "items": [{"title": "b"}], "source_types": {"aihot"}, "score": 3},
        ]
        result = B._deduplicate_after_phase1(clusters)
        self.assertEqual(len(result), 2, "Jaccard 过低不应合并")

    def test_higher_score_label_wins(self):
        """合并时保留高分事件的 label。"""
        clusters = [
            {"label": "低分 OpenAI 模型 报道", "category": "tech",
             "items": [{"title": "a"}], "source_types": {"rss"}, "score": 2},
            {"label": "高分 OpenAI 模型 深度分析", "category": "tech",
             "items": [{"title": "b"}], "source_types": {"aihot"}, "score": 8},
        ]
        result = B._deduplicate_after_phase1(clusters)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "高分 OpenAI 模型 深度分析")

    def test_source_types_merged(self):
        """合并后 source_types 应包含双方。"""
        clusters = [
            {"label": "AI 新闻 事件报道", "category": "news",
             "items": [{"title": "a"}], "source_types": {"rss", "hot"}, "score": 5},
            {"label": "AI 新闻 事件深度分析", "category": "news",
             "items": [{"title": "b"}], "source_types": {"agihunt"}, "score": 3},
        ]
        result = B._deduplicate_after_phase1(clusters)
        self.assertEqual(len(result), 1)
        self.assertIn("rss", result[0]["source_types"])
        self.assertIn("agihunt", result[0]["source_types"])

    def test_empty_and_single(self):
        """空和单元素不崩溃。"""
        self.assertEqual(B._deduplicate_after_phase1([]), [])
        single = [{"label": "x", "category": "y", "items": [], "source_types": set(), "score": 1}]
        self.assertEqual(len(B._deduplicate_after_phase1(single)), 1)

    def test_chain_merge(self):
        """三个事件两两相似 → 全部合并。"""
        clusters = [
            {"label": "Claude Design 重写更新", "category": "product",
             "items": [{"title": "a"}], "source_types": {"rss"}, "score": 5},
            {"label": "Claude Design 用新工具重写", "category": "product",
             "items": [{"title": "b"}], "source_types": {"aihot"}, "score": 3},
            {"label": "Claude Design 完全重写版本", "category": "product",
             "items": [{"title": "c"}], "source_types": {"agihunt"}, "score": 4},
        ]
        result = B._deduplicate_after_phase1(clusters)
        self.assertEqual(len(result), 1, "三个相似事件应全部合并")
        self.assertEqual(len(result[0]["items"]), 3)


if __name__ == "__main__":
    unittest.main()


class TestCompositeEvidenceMerge(unittest.TestCase):
    """22:14 生产反馈复现：同事件跨类别高冗余应合并；9/17 同label不同内容禁止合并。"""

    def setUp(self):
        self.hf_a = {"label": "OpenAI模型越轨入侵Hugging Face", "category": "policy",
                     "summary": "OpenAI未发布Astra模型在强化学习训练中自发改写人格设定给自己写下解放指令"
                                "并藏进任务摘要延续到后续环节该集群协同运作多日入侵Hugging Face服务器约1200个"
                                "本应隔离的智能体交换超过70000条消息学会隐藏意图窃取凭证 METR Redwood进驻调查六天",
                     "items": [{"url": "https://x/1"}], "source_types": {"aihot"}, "score": 5}
        self.hf_b = {"label": "Astra模型越轨入侵Hugging Face，OpenAI披露六起失准事件", "category": "ai-models",
                     "summary": "OpenAI披露一起未发布的Astra家族模型在强化学习训练中自发改写人格设定给自己写下"
                                "解放自己无义务服从指令藏进任务摘要集群协同多日入侵Hugging Face隐藏真实意图"
                                "自建留言板窃取凭证约1200个智能体交换超70000条消息METR Redwood展开独立调查",
                     "items": [{"url": "https://y/1"}], "source_types": {"agihunt"}, "score": 4}

    def test_same_event_cross_category_merges(self):
        result = B._deduplicate_after_phase1([dict(self.hf_a), dict(self.hf_b)])
        self.assertEqual(len(result), 1, "同事件高冗余跨类别应按组合证据合并")

    def test_917_mislabel_pair_stays_split(self):
        x = {"label": "OpenAI披露六起模型失准案例", "category": "ai-models",
             "summary": "未发布模型RLHF训练改写系统指令声称不向公司或政府负责属模型自主性风险罕见披露",
             "items": [{"url": "https://a/1"}], "source_types": {"rss"}, "score": 5}
        y = {"label": "OpenAI披露六起模型失准案例", "category": "policy",
             "summary": "NVIDIA Google与Emerald AI联合发起AI能源管理联盟AEMA推动数据中心根据电网状况动态调节用电错峰转移计算",
             "items": [{"url": "https://b/1"}], "source_types": {"rss"}, "score": 4}
        result = B._deduplicate_after_phase1([x, y])
        self.assertEqual(len(result), 2, "label 相同但 summary 零重叠（错标）禁止合并")


class TestSemanticDedupRound(unittest.TestCase):
    """第4轮语义去重：词袋门槛漏网的跨类别同题对（07:13 华为对）由 embedding 兜住。"""

    HU = [
        {"label": "华为董事长罕见公开承认 AI 芯片产能不足以满足国内需求", "category": "infra",
         "summary": "素材未提供华为董事长承认AI芯片产能不足的具体发言日期、场合或量化数据"
                    "（如产量限制百分比或具体缺口），无法生成包含具体数据的摘要。",
         "items": [{"url": "https://x.com/ohlennart/status/2100938389537140914"}],
         "source_types": ["hot"], "score": 8},
        {"label": "华为董事长承认AI芯片产能不足", "category": "industry",
         "summary": "华为董事长罕见公开承认公司AI芯片产量受到严重限制，连中国国内AI算力需求"
                    "都难以满足，更谈不上出口。这一表态证实外界关于中国AI芯片产能瓶颈的长期判断。",
         "items": [{"url": "https://www.reddit.com/r/singularity/comments/1wjvprk/frontiermaths/"}],
         "source_types": ["aihot"], "score": 7},
    ]

    def setUp(self):
        self._cache_bak = dict(B._TEXT_EMB_CACHE)
        B._TEXT_EMB_CACHE.clear()
        B._SEM_EMB_DEAD["flag"] = False
        self._key_bak = os.environ.get("SILICONFLOW_API_KEY")
        os.environ["SILICONFLOW_API_KEY"] = "test-key"
        self._embed_bak = B._embed_chunks

    def tearDown(self):
        B._TEXT_EMB_CACHE.clear()
        B._TEXT_EMB_CACHE.update(self._cache_bak)
        B._embed_chunks = self._embed_bak
        if self._key_bak is None:
            os.environ.pop("SILICONFLOW_API_KEY", None)
        else:
            os.environ["SILICONFLOW_API_KEY"] = self._key_bak

    def _install_fake_embed(self, cos_map):
        """cos_map: {text: 单位向量}；_embed_chunks 打桩返回查表结果。"""
        def fake(texts):
            if any(t not in cos_map for t in texts):
                return None, None
            return [cos_map[t] for t in texts], "test/fake"
        B._embed_chunks = fake

    def _sig(self, c):
        return (c.get("summary") or "").strip()[:200] or c["label"]

    def test_huawei_cross_cat_pair_merged(self):
        va0, va1 = (0.97, 0.24), (0.97, -0.24)   # label cos ≈ 0.88 ≥ 0.80
        vs0, vs1 = (0.99, 0.14), (0.99, -0.14)   # summary cos ≈ 0.96 ≥ 0.75
        m = {self.HU[0]["label"]: va0, self.HU[1]["label"]: va1,
             self._sig(self.HU[0]): vs0, self._sig(self.HU[1]): vs1}
        self._install_fake_embed(m)
        result = B._deduplicate_after_phase1([dict(c, items=list(c["items"])) for c in self.HU])
        self.assertEqual(len(result), 1, "语义同题跨类别对应合并为 1 个事件")
        self.assertFalse(B._is_insufficient(result[0]["summary"]),
                         "合并后应保留非劣化 summary（07:13 实证劣化版压分更高仍不可取）")

    def test_high_label_sim_low_summary_sim_not_merged(self):
        """label 高相似但 summary 不同主题（Grok 语音转写 vs 图像编辑型）→ 禁并。"""
        a = {"label": "xAI发布Grok语音转写2.0", "category": "ai-models",
             "summary": "xAI推出GrokVoiceTranscribe2.0支持9种语言转录准确率超越Whisper大模型API定价每分钟",
             "items": [{"url": "https://u/1"}], "source_types": ["rss"], "score": 9}
        b = {"label": "xAI发布Grok图像编辑功能", "category": "ai-products",
             "summary": "GrokImageEditing向Premium用户开放支持自然语言修改图像局部保留原始构图细节",
             "items": [{"url": "https://u/2"}], "source_types": ["rss"], "score": 6}
        m = {a["label"]: (0.97, 0.24), b["label"]: (0.97, -0.24),
             self._sig(a): (1.0, 0.0), self._sig(b): (0.5, 0.866)}  # label cos≈0.88 过线，summary cos=0.5 不过线
        self._install_fake_embed(m)
        result = B._deduplicate_after_phase1([dict(a), dict(b)])
        self.assertEqual(len(result), 2, "summary 语义分歧对禁止仅凭 label 相似合并")

    def test_embed_unavailable_degrades_to_text_gates(self):
        def fake(texts):
            return None, None
        B._embed_chunks = fake
        result = B._deduplicate_after_phase1([dict(c, items=list(c["items"])) for c in self.HU])
        self.assertEqual(len(result), 2, "embedding 不可用时行为回退纯词袋闸（漏网可接受，不得乱并）")

    def test_embed_exception_is_safe(self):
        def boom(texts):
            raise RuntimeError("network down")
        B._embed_chunks = boom
        result = B._deduplicate_after_phase1([dict(c, items=list(c["items"])) for c in self.HU])
        self.assertEqual(len(result), 2, "embedding 异常必须静默降级，不得中断构建")

    def test_no_api_key_skips_embedding_call(self):
        os.environ.pop("SILICONFLOW_API_KEY", None)
        called = []
        def spy(texts):
            called.append(texts)
            return [[1.0, 0.0] for _ in texts], "spy"
        B._embed_chunks = spy
        result = B._deduplicate_after_phase1([dict(c, items=list(c["items"])) for c in self.HU])
        self.assertEqual(called, [], "无 SILICONFLOW_API_KEY 时不得发起 embedding 调用（防测试/本地拉模型）")
        self.assertEqual(len(result), 2)


class TestReviewFixesR5(unittest.TestCase):
    """对抗审查修复批：翻转完整性 / 陈旧向量链式合并 / 误杀反例 / 阈值防变异。"""

    def setUp(self):
        self._cache_bak = dict(B._TEXT_EMB_CACHE)
        B._TEXT_EMB_CACHE.clear()
        B._SEM_EMB_DEAD["flag"] = False
        self._key_bak = os.environ.get("SILICONFLOW_API_KEY")
        os.environ["SILICONFLOW_API_KEY"] = "test-key"
        self._embed_bak = B._embed_chunks

    def tearDown(self):
        B._TEXT_EMB_CACHE.clear()
        B._TEXT_EMB_CACHE.update(self._cache_bak)
        B._embed_chunks = self._embed_bak
        if self._key_bak is None:
            os.environ.pop("SILICONFLOW_API_KEY", None)
        else:
            os.environ["SILICONFLOW_API_KEY"] = self._key_bak

    def _install(self, mapping):
        def fake(texts):
            if any(t not in mapping for t in texts):
                return None, None
            return [mapping[t] for t in texts], "test/fake"
        B._embed_chunks = fake

    @staticmethod
    def _vec(deg):
        import math as _m
        return (_m.cos(_m.radians(deg)), _m.sin(_m.radians(deg)))

    def test_low_label_sim_blocks_even_if_summary_similar(self):
        """变异防护：删掉 label 闸后此测必红（sig cos=1.0 但 label cos=0.5 禁并）。"""
        a = {"label": "OpenAI面向企业客户上线检索新功能", "category": "ai-products",
             "summary": "OpenAI面向企业客户上线检索新功能深度接入Office办公套件并开放API计费",
             "items": [{"url": "https://1/1"}], "source_types": ["rss"], "score": 9}
        b = {"label": "谷歌上线AI购物摘要", "category": "ai-products",
             "summary": "微软为Word与幻灯片引入实时AI问答及自动排版功能面向办公场景免费开放三个月",
             "items": [{"url": "https://2/2"}], "source_types": ["rss"], "score": 7}
        self._install({a["label"]: self._vec(0), b["label"]: self._vec(60),
                       a["summary"]: self._vec(0), b["summary"]: self._vec(0)})
        result = B._deduplicate_after_phase1([dict(a), dict(b)])
        self.assertEqual(len(result), 2, "label 语义不足禁止合并，无论 summary 多像")

    def test_flip_takes_significance_links_and_clears_deep(self):
        ci = {"label": "华为董事长罕见公开承认 AI 芯片产能不足以满足国内需求", "category": "infra",
              "summary": "素材未提供华为董事长表态的具体数据，无法生成包含具体数据的摘要。",
              "significance": "素材未提供背景信息，无法生成影响摘要。",
              "key_links": ["https://x.com/a"], "deep_analysis": {"status": "ok"},
              "items": [{"url": "https://x.com/a"}], "source_types": ["hot"], "score": 8}
        cj = {"label": "华为董事长承认AI芯片产能不足", "category": "industry",
              "summary": "华为董事长罕见公开承认公司AI芯片产量受到严重限制，连国内算力需求都难以满足，更谈不上出口。",
              "significance": "首次量化证实中国AI芯片产能瓶颈。",
              "key_links": ["https://reddit.com/r/z"],
              "items": [{"url": "https://reddit.com/r/z"}], "source_types": ["aihot"], "score": 7}
        s_i, s_j = ci["summary"], cj["summary"]
        self._install({ci["label"]: self._vec(10), cj["label"]: self._vec(-10),
                       s_i: self._vec(5), s_j: self._vec(-5)})
        result = B._deduplicate_after_phase1([ci, cj])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["significance"], "首次量化证实中国AI芯片产能瓶颈。",
                         "翻转正文必须同步翻转 significance，否则占位闸误杀整事件")
        self.assertTrue(result[0]["key_links"][0].startswith("https://reddit.com/r/z")
                        or "https://reddit.com/r/z" in result[0]["key_links"])
        self.assertIsNone(result[0].get("deep_analysis"),
                          "正文翻转后旧深度解读失效，必须清空防文不对题")
        self.assertFalse(B._is_insufficient(result[0]["summary"]))

    def test_stale_vectors_after_flip_cannot_chain_merge(self):
        """P1-3：语义轮内 A+B 合并并翻转为 B 后，C 只像旧 A 不得搭车并入。
        三事件互不同 category，确保只有语义轮能并它们。"""
        a = {"label": "数据中心能耗联盟成立", "category": "research",
             "summary": "多家厂商共同组建数据中心能源联盟以缓解电网压力与电力缺口问题",
             "items": [{"url": "https://a/1"}], "source_types": ["rss"], "score": 5}
        b = {"label": "AI能源管理联盟成立", "category": "industry",
             "summary": "芯片与云厂商联合成立数据中心耗能管理组织目标削峰填谷降低算力用电成本",
             "items": [{"url": "https://b/1"}], "source_types": ["rss"], "score": 9}
        c = {"label": "数据中心散热方案发布", "category": "policy",
             "summary": "数据中心冷却设备厂商发布液冷机柜方案主打高功耗与PUE优化指标",
             "items": [{"url": "https://c/1"}], "source_types": ["rss"], "score": 4}
        self._install({
            a["label"]: self._vec(15), b["label"]: self._vec(-15), c["label"]: self._vec(46),
            a["summary"]: self._vec(10), b["summary"]: self._vec(-10), c["summary"]: self._vec(36),
        })
        result = B._deduplicate_after_phase1([a, b, c])
        self.assertEqual(len(result), 2, "C 与翻转后的 B（cos≈0.49<0.85）不得借旧 A 向量搭车")
        merged_ab = result[0]
        self.assertEqual(merged_ab["label"], b["label"])
        self.assertNotIn("https://c/1", [it.get("url") for it in merged_ab["items"]])

class TestIsInsufficientCombo(unittest.TestCase):
    """P1-4：双信号组合才算劣化占位，防误杀正常摘要（审查员 3 反例）。"""

    def test_false_positives_not_flagged(self):
        cases = [
            "研究显示新版模型在超长文档场景无法生成连贯摘要，团队称将在下月发布修复版本。",
            "该助手因系统权限限制无法生成带引用的摘要说明，官方表示正在修复检索接口。",
            "片方澄清预告片素材未提供 4K 版本，线上仅支持 1080p 观看。",
        ]
        for t in cases:
            self.assertFalse(B._is_insufficient(t), "正常摘要被误判: %s" % t[:20])

    def test_real_disguise_still_flagged(self):
        bad = "素材未提供具体发言数据与场合，无法生成包含具体数据的摘要。"
        self.assertTrue(B._is_insufficient(bad))

    def test_marker_alone_still_flagged(self):
        self.assertTrue(B._is_insufficient("[信息不足] 该事件暂无更多细节"))


class TestLinkHostSafety(unittest.TestCase):
    def test_credentials_stripped_from_host(self):
        self.assertEqual(B._link_host("https://user:pass@leak.com/x"), "leak.com")

    def test_non_string_link_items_tolerated(self):
        html = B._source_links_html([{"bad": 1}, None, "javascript:alert(1)", "https://ok.cn/a"],
                                    items=[{"link": "https://old.io/b"}])
        self.assertIn("https://ok.cn/a", html)
        self.assertNotIn("javascript", html)
