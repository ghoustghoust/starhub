# -*- coding: utf-8 -*-
"""Task 4: Phase 2 证据包 — URL 归一化反查 RSS 全文存档。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi


class TestNormUrl:
    def test_protocol_and_www_insensitive(self):
        assert bdi._norm_url("https://www.ithome.com/1/004.htm") == \
               bdi._norm_url("http://ithome.com/1/004.htm/")

    def test_case_insensitive(self):
        assert bdi._norm_url("https://A.COM/X") == "a.com/x"

    def test_empty_safe(self):
        assert bdi._norm_url(None) == ""


def _hist(link, body, source="Reddit", title="帖标题"):
    return {link: {"link": link, "full_content": body, "source": source, "title": title}}


class TestEvidencePack:
    def test_hit_returns_fulltext(self):
        url = "https://www.reddit.com/r/ai/comments/1x/"
        cluster = {"items": [{"url": "https://reddit.com/r/ai/comments/1x"}]}
        by_url = {bdi._norm_url(k): v for k, v in _hist(url, "讨" * 500).items()}
        pack = bdi._build_evidence_pack(cluster, by_url)
        assert "讨" * 100 in pack
        assert "Reddit" in pack

    def test_short_body_skipped(self):
        url = "https://a.com/1"
        cluster = {"items": [{"url": url}]}
        by_url = {bdi._norm_url(k): v for k, v in _hist(url, "太短了" * 10).items()}
        assert bdi._build_evidence_pack(cluster, by_url) == ""

    def test_no_match_returns_empty(self):
        assert bdi._build_evidence_pack({"items": [{"url": "https://no/1"}]}, {}) == ""

    def test_capped_by_doc_chars(self):
        url = "https://x.com/s/status/1"
        by_url = {bdi._norm_url(k): v for k, v in _hist(url, "字" * 9000).items()}
        pack = bdi._build_evidence_pack({"items": [{"url": url}]}, by_url, doc_chars=2500)
        assert pack.count("字") <= 2500

    def test_max_docs(self):
        cluster = {"items": [{"url": "https://m/%d" % i} for i in range(5)]}
        by_url = {}
        for i in range(5):
            by_url.update({bdi._norm_url(k): v for k, v in
                           _hist("https://m/%d" % i, "内" * 300).items()})
        pack = bdi._build_evidence_pack(cluster, by_url, max_docs=3)
        assert pack.count("内") <= 300 * 3

    def test_falls_back_to_summary(self):
        url = "https://s/1"
        rec = {url: {"link": url, "full_content": "", "summary": "摘" * 200,
                     "source": "BBC", "title": "t"}}
        by_url = {bdi._norm_url(k): v for k, v in rec.items()}
        pack = bdi._build_evidence_pack({"items": [{"url": url}]}, by_url)
        assert "摘" * 100 in pack


class TestPhase2PromptInjection:
    def test_evidence_block_in_prompt(self):
        captured = {}

        class FakeLLM:
            def complete(self, messages, **kw):
                captured["prompt"] = messages[1]["content"]
                return ('{"event_reconstruction":"r","impact_analysis":"i",'
                        '"source_divergence":"d","quote":"q","outlook":"o",'
                        '"confidence":"high","citations":{"quote":1}}')

        if bdi.NOW_BJ is None:
            bdi.NOW_BJ = bdi._now_bj()
        cluster = {"items": [{"title": "t", "text": "素材正文", "source_type": "agihunt",
                              "channel": "agi", "hot": 5, "url": "https://u/1"}]}
        bdi._llm_phase2(FakeLLM(), cluster, {"label": "L", "summary": "S"},
                        evidence="【Reddit】帖标题\n全文证据段内容" * 10)
        assert "补充全文参考" in captured["prompt"]
        assert "全文证据段内容" in captured["prompt"]

    def test_no_evidence_no_block(self):
        captured = {}

        class FakeLLM:
            def complete(self, messages, **kw):
                captured["prompt"] = messages[1]["content"]
                return ('{"event_reconstruction":"r","impact_analysis":"i",'
                        '"source_divergence":"d","quote":"q","outlook":"o",'
                        '"confidence":"high","citations":{}}')

        if bdi.NOW_BJ is None:
            bdi.NOW_BJ = bdi._now_bj()
        cluster = {"items": [{"title": "t", "text": "素材正文", "source_type": "agihunt",
                              "channel": "agi", "hot": 5, "url": "https://u/1"}]}
        bdi._llm_phase2(FakeLLM(), cluster, {"label": "L", "summary": "S"})
        assert "补充全文参考" not in captured["prompt"]
