# -*- coding: utf-8 -*-
"""主题句必须只说正文里真有的事（任务 #28，2026-09-21）。

现场（远端 daily-insight.json，generated_at=2026-09-21T14:13:24）：
  theme = 「从阿里开源7B轻量生图模型，到四大巨头因呼吁放缓被诉，再到加州率先立法紧急停止机制，
           判断AI正在加速从技术落地走向监管收紧与商业博弈的深水区」
  meta.per_event 的 12 条事件里没有"四大巨头被诉"，也没有"加州紧急停止机制"
  ⇒ 主题三个从句里两个在正文中不存在。

三条独立成因，缺一处都是半修复：
  (a) build_daily_insight.py:2974 的 Phase 1 prompt 明写
      「如果全局上下文中有重要信息未被任何事件覆盖，请在 theme 中提及」—— 模型是照做的。
      这条指示先把整份报告推到"正文没有的事也算数"的口径上，与"报告只讲它给了证据的事"冲突。
  (b) theme 在 :5508 的 `clusters[:MAX_EVENTS]`（16→12）收口**之前**生成，之后从不重生成/校验。
  (c) :5513 终版复评 `_confirm_shipped_report_eval(judge, clusters, theme, ...)` 沿用同一个旧 theme，
      于是 cov=0.60/rel=0.60 是"拿正文里没有的事件去核对正文"打出来的 —— 度量本身被污染。

本文件只锁一个不依赖 LLM 的确定性性质：出厂 theme 的每个事实从句都必须能在最终事件集里
找到承载者；找不到就裁掉，裁到没有剩。校验必须在终版复评**之前**发生。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B


def _ev(label, summary=""):
    return {"label": label, "summary": summary, "items": [], "source_types": {"agihunt"},
            "best_score": 1.0}


GOOD_THEME = ("从阿里开源Qwen-Image-2.1统一生图与编辑，到陶哲轩罕见呼吁放慢AI发展节奏，"
              "再到Ezra Klein疾呼阻止AI递归自我改进，判断AI正走向监管收紧与商业博弈的深水区")
CLUSTERS = [
    _ev("阿里开源Qwen-Image-2.1统一生图与编辑", "阿里Qwen团队发布并开源Qwen-Image-2.1，7B架构"),
    _ev("陶哲轩呼吁放慢AI发展节奏", "菲尔兹奖得主陶哲轩在加州理工发表主题演讲"),
    _ev("Ezra Klein疾呼阻止AI递归自我改进", "纽约时报专栏作家Ezra Klein发表长文警告"),
]


class TestPromptNoLongerAsksForUncovered:
    def test_phase1_prompt_does_not_tell_theme_to_name_uncovered_items(self):
        """:2974 那条指示必须消失 —— 它让头版判断句合法地引用正文没有的事。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        assert "未被任何事件覆盖，请在 theme 中提及" not in src, (
            "Phase 1 prompt 仍在要求 theme 提及未被事件覆盖的信息，"
            "主题与正文不一致是它主动制造的")


class TestThemeAligner:
    def test_aligner_exists(self):
        assert hasattr(B, "_align_theme_to_events"), \
            "没有确定性对齐器：主题与正文的一致性就只能靠 LLM 自觉"

    def test_clean_theme_is_left_alone(self):
        out, dropped = B._align_theme_to_events(GOOD_THEME, CLUSTERS)
        assert dropped == [], "合法主题被误裁：%s" % dropped
        assert "Qwen-Image-2.1" in out and "陶哲轩" in out and "Ezra Klein" in out

    def test_clauses_without_a_carrier_are_dropped(self):
        """复刻今天那份出厂主题：三个从句里两个正文没有。"""
        bad = ("从阿里开源Qwen-Image-2.1统一生图与编辑，到四大巨头因呼吁放缓被诉，"
               "再到加州率先立法紧急停止机制，判断AI正在走向监管收紧与商业博弈的深水区")
        out, dropped = B._align_theme_to_events(bad, CLUSTERS)
        assert len(dropped) == 2, "应裁掉两条无承载从句，实际 %d：%s" % (len(dropped), dropped)
        for frag in ("四大巨头", "加州"):
            assert frag not in out, "裁完仍留着「%s」⇒ 头版还在说正文没有的事" % frag
        assert "Qwen-Image-2.1" in out, "有承载的从句不能跟着被裁"
        assert "深水区" in out, (
            "末尾的判断句被一起裁了 ⇒ 裁的是"
            "「无承载的事实从句」，不是对全天的解读句")
        # 判断句（最后一句）与留存从句必须拼得回去，不能留下悬空的「，再到」
        assert "，再到，" not in out and "，到，" not in out, "裁剪后拼接出空洞：%s" % out

    def test_returns_a_usable_line_when_every_clause_fails(self):
        """全裁光的退化情形：不能返回空串，否则页面上方直接空一格。"""
        bad = "从元宇宙泡沫破裂，到室温超导复现失败，判断行业进入收缩期"
        out, dropped = B._align_theme_to_events(bad, CLUSTERS)
        assert len(dropped) >= 2
        assert out.strip(), "退化成空主题 ⇒ 页面顶部空白"
        assert any((c["label"][:6]) in out for c in CLUSTERS), \
            "兜底主题必须由最终事件重建，实际 %r" % out


    def test_no_events_at_all_still_returns_a_line(self):
        """TA4 存活暴露的空白分支：上一条评论用的 clusters 非空，走的是"重建"，
        根本没经过"无事件可依据"那条退回路径 —— 那才是最容易返回空串的地方。
        """
        out, dropped = B._align_theme_to_events(
            "从元宇宙泡沫破裂，到室温超导复现失败，判断行业进入收缩期", [])
        assert out.strip(), "clusters 为空时返回了空串 ⇒ 页面顶部空一格"
        assert "判断" in out, "无事件可依据时应退回判断句，实际 %r" % out

    def test_empty_theme_does_not_crash(self):
        out, dropped = B._align_theme_to_events("", CLUSTERS)
        assert out == "" and dropped == []


class TestWiring:

    def test_aligner_runs_before_the_shipped_regrade(self):
        """对齐必须发生在终版复评之前 —— 否则 cov/rel 还在用旧主题打分，污染照旧。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        i_align = src.find("_align_theme_to_events(")
        i_regrade = src.find("_confirm_shipped_report_eval(")
        assert i_align > -1 and i_regrade > -1
        # 定义处先出现，调用点必须在复评调用之前
        i_def = src.find("def _align_theme_to_events(")
        assert i_def < i_align < i_regrade, (
            "对齐调用（第 %d 字符）没有落在终版复评（第 %d 字符）之前 ⇒ 分数仍描述旧主题"
            % (i_align, i_regrade))

    def test_writer_receives_the_aligned_theme(self):
        """写盘用的必须是裁后的 theme，不能只裁了不用。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                "build_daily_insight.py"), encoding="utf-8").read()
        m = re.search(r"^\s*theme(?:,\s*[A-Za-z_]\w*)? = _align_theme_to_events\(", src, re.M)
        assert m, "对齐结果没有回写给 theme 变量 ⇒ 裁了等于没裁"
        i_align = m.start()
        # 必须排掉定义行：`def _write_insight_json(clusters, theme, ...)` 也含这个字面量，
        # 用 find() 会拿到定义的位置（比调用点还早），顺序断言就变成在比"定义 vs 调用"，永远红/永远绿都没意义。
        w = re.search(r"(?<!def )_write_insight_json\(clusters, theme", src)
        assert w, "找不到 _write_insight_json 的调用点"
        i_write = w.start()
        assert i_align > -1, "对齐结果没有回写给 theme 变量"
        assert i_align < i_write, "写盘仍在用对齐前的 theme"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
