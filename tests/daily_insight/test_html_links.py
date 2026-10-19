# -*- coding: utf-8 -*-
"""日报页洞察板块：每个事件必须渲染可跳转的原文链接（用户红线）。"""
import os
import re
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B


STUB = ('<html><body><nav class="index" aria-label="今日分类索引"></nav>'
        '<footer class="foot">f</footer></body></html>')


def _inject(tmp, clusters, bubble=None):
    target = os.path.join(tmp, "ai-daily.html")
    with open(target, "w", encoding="utf-8") as f:
        f.write(STUB)
    bak = B._AI_DAILY_FILE
    B._AI_DAILY_FILE = target
    try:
        B._inject_into_ai_daily(clusters, "测试主题", bubble_breaker=bubble)
    finally:
        B._AI_DAILY_FILE = bak
    with open(target, encoding="utf-8") as f:
        return f.read()


class TestInsightHtmlLinks:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_card_has_jumpable_link(self):
        clusters = [
            {"label": "事件甲", "summary": "摘要甲", "category": "ai-models", "score": 9,
             "status": "new", "source_types": ["rss"], "items": [{"url": "https://a.com/x"}],
             "key_links": ["https://a.com/x", "https://b.com/y"]},
            {"label": "事件乙", "summary": "摘要乙", "category": "policy", "score": 7,
             "status": "new", "source_types": ["hot"], "items": [{"url": "https://c.com/z"}],
             "key_links": []},
        ]
        html = _inject(self.tmp, clusters)
        # 新报纸排版卡片容器是 <article class="di-card">（旧版 div，正则两者兼容）
        cards = re.findall(r'<(?:div|article) class="di-card">.*?(?=<(?:div|article) class="di-card">|<!-- daily-insight-end)',
                           html, re.S)
        assert len(cards) == 2
        for c in cards:
            # 红线：每个洞察必须带可跳转原文引用——简报卡走 di-link，深读特稿卡走 di-cite
            assert 'class="di-link"' in c or 'class="di-cite"' in c, "洞察卡片缺失可跳转原文引用"
        assert "https://c.com/z" in html  # key_links 空时回退 items URL

    def test_newspaper_anatomy(self):
        clusters = [
            {"label": "特稿事件", "summary": "特稿摘要[agihunt]", "category": "ai-models",
             "score": 0.3, "status": "new", "resonance": "niche",
             "source_types": ["rss"], "key_links": ["https://a.com/x"],
             "deep_analysis": {"status": "ok", "event_reconstruction": "r",
                               "impact_analysis": "i", "source_divergence": "d",
                               "quote": "金句", "outlook": "o", "confidence": "high",
                               "cited_sources": [{"index": 1, "url": "https://src.io/a"}]}},
            {"label": "简报事件", "summary": "简报摘要", "category": "policy",
             "score": 0.1, "status": "new", "source_types": ["rss"],
             "items": [{"url": "https://c.com/z"}], "key_links": []},
        ]
        html = _inject(self.tmp, clusters)
        assert '<section id="sec-7" class="sec di-sec">' in html
        assert "深度洞察" in html and "2 条 · 1 篇深读" in html
        assert '<div class="di-feat">' in html
        # 眉线只留对读者有意义的中文分类；status/resonance/score 是管线内部信号
        assert ">AI 模型</span>" in html and ">政策</span>" in html
        assert "新 · 垂直" not in html and "评分" not in html
        assert "[agihunt]" not in html and "特稿摘要" in html   # 语料标记剥除，正文保留
        feat = html.split('<div class="di-feat">')[1].split('<div class="cols">')[0]
        assert "特稿事件" in feat and "简报事件" not in feat
        assert 'class="di-deep"' in feat and "置信度" not in feat
        assert 'class="di-quote"' in feat
        assert "vii</span>深度洞察" in html   # 日报索引补第 vii 项

    def test_bubble_newspaper_shell(self):
        bubble = [{"label": "破界", "summary": "s", "reason": "r", "url": "https://b.org/x"}]
        html = _inject(self.tmp, [], bubble=bubble)
        assert 'class="di-bubble-head"' in html
        assert "破茧栏 · 信息增量" in html
        assert 'class="di-bubble-grid"' in html

    def test_javascript_scheme_filtered(self):
        clusters = [{"label": "事件", "summary": "s", "category": "research", "score": 5,
                     "status": "new", "source_types": ["rss"],
                     "items": [{"url": "javascript:alert(1)"}],
                     "key_links": ["javascript:alert(1)", "https://real.cn/p"]}]
        html = _inject(self.tmp, clusters)
        assert "javascript:" not in html
        assert "https://real.cn/p" in html

    def test_deep_cited_sources_rendered(self):
        clusters = [{"label": "事件", "summary": "s", "category": "research", "score": 5,
                     "status": "new", "source_types": ["rss"],
                     "key_links": ["https://real.cn/p"],
                     "deep_analysis": {"status": "ok", "event_reconstruction": "r",
                                       "impact_analysis": "i", "source_divergence": "d",
                                       "outlook": "o", "confidence": "高",
                                       "cited_sources": [{"index": 2, "url": "https://src.io/a",
                                                          "title": "t"}]}}]
        html = _inject(self.tmp, clusters)
        assert 'class="di-cite"' in html
        assert "https://src.io/a" in html
        assert "[2]" in html

    def test_bubble_card_link(self):
        bubble = [{"label": "非科技", "summary": "s", "reason": "r", "url": "https://bubble.org/x"}]
        html = _inject(self.tmp, [], bubble=bubble)
        assert 'href="https://bubble.org/x"' in html
