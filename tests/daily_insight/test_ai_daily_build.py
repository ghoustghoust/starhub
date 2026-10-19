# -*- coding: utf-8 -*-
"""build_ai_daily 文本管线与模板的单元测试（不联网、不写根目录产物）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_ai_daily as A


class TestTruncate:
    def test_short_unchanged(self):
        assert A._truncate("短摘要", 120) == "短摘要"

    def test_sentence_cut_no_ellipsis(self):
        s = "长" * 60 + "。" + "x" * 200
        out = A._truncate(s, 120)
        assert out.endswith("。")
        assert len(out) <= 121
        assert "…" not in out

    def test_soft_cut_adds_ellipsis(self):
        s = "z" * 40 + "，" + "z" * 130
        out = A._truncate(s, 120)
        assert out == "z" * 40 + "，…"   # 切点含软标点本身

    def test_hard_cut(self):
        assert A._truncate("y" * 300, 120) == "y" * 120 + "…"

    def test_trailing_ellipsis_stripped(self):
        assert A._truncate("正常的句子。……", 120) == "正常的句子。"
        assert A._truncate("短标题...", 120) == "短标题"

    def test_early_sentence_end_not_used(self):
        s = "短。" + "z" * 130
        out = A._truncate(s, 120)
        assert out != "短。"          # <30 的句号不做收口
        assert out.endswith("…")

    def test_whitespace_collapsed(self):
        assert A._truncate("  句一。\n句二  ", 120) == "句一。 句二"

    def test_punct_exactly_at_maxlen_no_fake_truncation(self):
        # R1 审查 P0：标点恰在 maxlen 时不得输出比原文更长的"全文+…"
        out = A._truncate("a" * 120 + "，", 120)
        assert out == "a" * 120 + "…"
        out2 = A._truncate("a" * 120 + "。", 120)
        assert len(out2) <= 121 and out2.endswith("…")


class TestHnSummary:
    def test_high_score(self):
        assert A._hn_summary(273, "allisdust") == "273 分 · allisdust 发起的讨论"

    def test_low_score_no_points(self):
        assert A._hn_summary(1, "tim333") == "tim333 发起的讨论"
        assert A._hn_summary(3, "x") == "x 发起的讨论"

    def test_missing_author(self):
        assert A._hn_summary(10, "") == "10 分 · 匿名发起的讨论"

    def test_summary_no_longer_has_triangle(self):
        s = A._hn_summary(72, "allisdust")
        assert "▲" not in s and "points" not in s


class TestArxivTag:
    def test_item_html_src_line_carries_tag(self):
        it = {"title": "Point2Part：统一3D分割", "link": "http://arxiv.org/abs/1",
              "category": "论文", "source": "arXiv", "summary": "摘要",
              "pub_date": None, "tag": "cs.CV"}
        html = A._item_html(it)
        assert "arXiv · cs.CV" in html
        assert "[cs.CV]" not in html

    def test_item_html_without_tag_unchanged(self):
        it = {"title": "普通条目", "link": "https://a.com", "category": "AI 模型",
              "source": "AIHOT", "summary": "s", "pub_date": None}
        html = A._item_html(it)
        assert "<span class=\"item-src\">AIHOT</span>" in html


def _fixture_grouped():
    def _it():
        return {"title": "测试标题", "link": "https://a.com/1", "category": "AI 模型",
                "source": "AIHOT", "summary": "摘要。", "pub_date": None}
    return [("AI 模型", [_it(), _it()]), ("论文", [_it()])]


class TestBuildHtmlTemplate:
    def test_numbers_use_accent_green_only(self):
        html = A.build_html(_fixture_grouped(), "2026年10月1日 星期四",
                            "数据截至 2026-10-01 08:00（北京时间）")
        assert html.count('style="color:var(--accent)"') >= 3
        for hex_ in ("#2563eb", "#7c3aed", "#0891b2", "#0d9488", "#d97706", "#dc2626"):
            assert hex_ not in html

    def test_slogan_and_window_wording(self):
        html = A.build_html(_fixture_grouped(), "d", "数据截至 t")
        assert "AI 资讯 · DAILY" in html
        assert "每早八时" not in html

    def test_footer_deduped(self):
        html = A.build_html(_fixture_grouped(), "2026年10月1日 星期四", "w",
                            sources_note=" · Hacker News")
        assert "今日信源" not in html
        assert "共 <strong>3</strong> 条" in html
        assert html.count("内容版权归原作者") == 1  # 只在 mast-strip

    def test_item_desc_clamped_and_src_readable(self):
        html = A.build_html(_fixture_grouped(), "d", "w")
        assert "-webkit-line-clamp:3" in html
        assert ".item-src{\n  font-size:11px;color:var(--muted);" in html


class TestHistoryPageUnified:
    def test_history_cards_use_same_anatomy(self, monkeypatch, tmp_path):
        import build_daily_insight as B
        day = {"date": "2026-10-01", "theme": "主题",
               "stats": {"total_events": 1},
               "events": [{"label": "历史事件", "summary": "摘[aihot]", "category": "industry",
                           "score": 0.2, "status": "new", "resonance": "niche",
                           "sources": ["rss"], "key_links": ["https://h.io/1"],
                           "deep_analysis": None}]}
        monkeypatch.setattr(B, "_load_history", lambda: {"days": [day]})
        monkeypatch.setattr(B, "HISTORY_HTML", str(tmp_path / "h.html"))
        B._build_history_html()
        html = open(B.HISTORY_HTML, encoding="utf-8").read()
        assert '<article class="di-card">' in html
        assert ">行业</span>" in html
        assert "[aihot]" not in html
        assert "深度解读 · ANALYSIS" not in html  # 无 deep 不渲染面板
        assert B._DI_CSS in html                  # 与日报洞察共用同一份 CSS


class TestZhTypo:
    def test_cjk_latin_boundary(self):
        assert A._zh_typo("Google宣布11月17日以Skills取代Gems") == \
            "Google 宣布 11 月 17 日以 Skills 取代 Gems"

    def test_existing_space_not_doubled(self):
        assert A._zh_typo("OpenAI 发布 GPT-6") == "OpenAI 发布 GPT-6"

    def test_percent_boundary(self):
        assert A._zh_typo("毛利率80%的护城河") == "毛利率 80% 的护城河"

    def test_paired_quotes_converted(self):
        assert A._zh_typo('称为"超级智能"的时代') == "称为“超级智能”的时代"

    def test_unbalanced_quotes_untouched(self):
        assert A._zh_typo('他说"然后') == '他说"然后'

    def test_empty(self):
        assert A._zh_typo("") == ""


class TestChannelConfig:
    def test_redis_channel_removed(self):
        """用户裁决：Redis 源整档移除（2026-10-01）。防止回潮。"""
        import inspect
        src = inspect.getsource(A.fetch_multi_channel)
        assert "Redis" not in src
        assert not hasattr(A, "REDIS_RSS")
