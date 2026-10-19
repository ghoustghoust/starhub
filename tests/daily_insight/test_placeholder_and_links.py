# -*- coding: utf-8 -*-
"""Task 1: [信息不足] 前缀匹配 + key_links 多样性校验。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi


class TestIsInsufficient:
    def test_exact_marker(self):
        assert bdi._is_insufficient("xx[信息不足]xx") is True

    def test_colon_variant(self):
        # LLM 实际输出 [信息不足：解释...]，旧 `'[]' in` 判断漏过（9/18 evt_004 实锤）
        assert bdi._is_insufficient("[信息不足：素材库中面板数据分属不同来源]") is True

    def test_normal_text(self):
        assert bdi._is_insufficient("OpenAI 发布了新模型") is False

    def test_none_safe(self):
        assert bdi._is_insufficient(None) is False


class TestValidateKeyLinks:
    def test_keeps_diverse_links(self):
        links = ["https://a.com/1", "https://b.com/2"]
        assert bdi._validate_key_links(links, []) == links

    def test_dedups_same_link(self):
        assert bdi._validate_key_links(["https://a.com/1", "https://a.com/1"], []) == ["https://a.com/1"]

    def test_all_identical_falls_back_to_item_urls(self):
        # 9/18 实锤：全事件 key_links 指向同一 AIHOT URL
        same = ["https://x.com/s"]
        items = [{"url": "https://r1.com/a"}, {"url": "https://r2.com/b"}, {"url": "https://x.com/s"}]
        out = bdi._validate_key_links(same, items)
        assert len(out) == 3 and "https://x.com/s" in out

    def test_empty_links_uses_items(self):
        out = bdi._validate_key_links([], [{"link": "https://i.com/1"}])
        assert out == ["https://i.com/1"]

    def test_capped_at_5(self):
        links = ["https://i.com/%d" % i for i in range(10)]
        assert len(bdi._validate_key_links(links, [])) == 5

    def test_url_variants_count_as_one(self):
        # 对抗审查 P2-8：仅协议/www/尾斜杠不同的同一 URL 不应算 2 个多样链接
        variants = ["http://a.com/x/", "https://www.a.com/x"]
        out = bdi._validate_key_links(variants, [{"url": "https://b.com/y"}])
        assert out == ["http://a.com/x/", "https://b.com/y"]


class TestDropInsufficient:
    """RAGAS 修正环可事后注入[信息不足]占位——修正后必须再过一次占位闸。"""

    def _mk(self, n_good, n_bad):
        return ([{"label": "g%d" % i, "summary": "正常摘要内容%d" % i} for i in range(n_good)]
                + [{"label": "b%d" % i, "summary": "[信息不足] 素材库中无相关报道"} for i in range(n_bad)])

    def test_drops_when_above_min(self):
        out = bdi._drop_insufficient(self._mk(6, 2))
        assert len(out) == 6

    def test_guard_keeps_all_when_below_min(self):
        # MIN_EVENTS=3：2 good < 3 → 全保留防塌空
        out = bdi._drop_insufficient(self._mk(2, 2))
        assert len(out) == 4

    def test_correction_prompt_forbids_placeholder(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "不要返回[信息不足]占位" in src


class TestIter4Hardening:
    def test_drop_checks_significance_too(self):
        cs = [{"label": "ok1", "summary": "s", "significance": "[信息不足] 无法判断"}]
        cs += [{"label": "g%d" % i, "summary": "正常", "significance": "有意义"} for i in range(3)]
        out = bdi._drop_insufficient(cs)
        assert all("信息不足" not in (c.get("significance") or "") for c in out)

    def test_correction_prompt_includes_current_summary(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "【当前摘要】" in src

    def test_ragas_loop_can_second_round(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        seg = src[src.index("修正有效 ("):src.index("eval_result[\"iterations\"]")]
        assert "continue" in seg, "未达标时应进入下一轮而非 break"


class TestDisguisedPlaceholder:
    """07:13 实证：「素材未提供…无法生成…摘要」型劣化输出绕过了 [信息不足] 闸。"""

    def test_disguised_placeholder_detected(self):
        bad = ("素材未提供华为董事长承认AI芯片产能不足的具体发言日期、场合或量化数据"
               "（如产量限制百分比或具体缺口），无法生成包含具体数据的摘要。")
        assert bdi._is_insufficient(bad)

    def test_normal_summary_not_hit(self):
        good = "Anthropic 披露 Claude 已主导内部 26% 的研发工作，编码速度提升 64%。"
        assert not bdi._is_insufficient(good)
        edge = "该报告指出模型评测数据未提供公开基准，但摘要显示能力显著提升。"
        assert not bdi._is_insufficient(edge)
