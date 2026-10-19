# -*- coding: utf-8 -*-
"""Phase 2 来源索引引用：素材编号、citations 校验、cited_sources 回填、key_links 派生。"""
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_daily_insight as B


def _mk_cluster():
    return {"items": [
        {"source_type": "rss", "source": "IT之家", "title": "OpenAI 发布 Astra",
         "url": "https://ithome.com/1.htm", "text": "正文一" * 30},
        {"source_type": "agihunt", "channel": "agi", "hot": 9, "title": "Astra 越狱讨论",
         "url": "https://reddit.com/r/1", "text": "正文二"},
        {"source_type": "hot", "source": "ithome", "rank": 9, "title": "热榜条目",
         "url": "https://ithome.com/2.htm"},
    ]}


class TestMaterialNumbering(unittest.TestCase):
    def test_default_unnumbered_format_unchanged(self):
        text = B._build_event_material(_mk_cluster())
        # 旧格式不编号，[RSS/ 开头
        self.assertTrue(text.startswith("[RSS/"))
        self.assertNotIn("[1]", text)

    def test_numbered_material_has_index_prefixes(self):
        text, refs = B._build_event_material(_mk_cluster(), numbered=True)
        self.assertTrue(text.startswith("[1][RSS/"))
        self.assertIn("[2][AGI Hunt/agi", text)
        self.assertIn("[3][热榜/ithome", text)

    def test_refs_align_with_items(self):
        _, refs = B._build_event_material(_mk_cluster(), numbered=True)
        self.assertEqual([r["index"] for r in refs], [1, 2, 3])
        self.assertEqual(refs[0]["url"], "https://ithome.com/1.htm")
        self.assertEqual(refs[1]["source"], "AGI Hunt/agi")
        self.assertEqual(refs[2]["url"], "https://ithome.com/2.htm")


class TestCitationValidation(unittest.TestCase):
    def test_valid_refs_kept_and_cited_sources_filled(self):
        _, refs = B._build_event_material(_mk_cluster(), numbered=True)
        parsed = {"citations": {"event_reconstruction": [1, 2], "quote": 2}}
        out = B._validate_phase2_citations(parsed, refs)
        self.assertEqual(out["citations"]["event_reconstruction"], [1, 2])
        self.assertEqual(out["citations"]["quote"], [2])  # int 归一为 list
        urls = [s["url"] for s in out["cited_sources"]]
        self.assertEqual(urls, ["https://ithome.com/1.htm", "https://reddit.com/r/1"])

    def test_invalid_indexes_stripped(self):
        _, refs = B._build_event_material(_mk_cluster(), numbered=True)
        parsed = {"citations": {"event_reconstruction": [1, 99, "2", None],
                                "impact_analysis": [7, 8]}}
        out = B._validate_phase2_citations(parsed, refs)
        self.assertEqual(out["citations"]["event_reconstruction"], [1, 2])
        self.assertEqual(out["citations"]["impact_analysis"], [])
        self.assertEqual([s["index"] for s in out["cited_sources"]], [1, 2])

    def test_missing_citations_yields_empty(self):
        _, refs = B._build_event_material(_mk_cluster(), numbered=True)
        out = B._validate_phase2_citations({"quote": "x"}, refs)
        self.assertEqual(out["cited_sources"], [])

    def test_all_urls_deduped_for_key_links(self):
        _, refs = B._build_event_material(_mk_cluster(), numbered=True)
        parsed = {"citations": {"event_reconstruction": [1, 1, 2, 3]}}
        out = B._validate_phase2_citations(parsed, refs)
        urls = [s["url"] for s in out["cited_sources"]]
        self.assertEqual(len(urls), len(set(urls)))


class TestPhase2PromptCitationContract(unittest.TestCase):
    def test_prompt_requires_citations(self):
        import inspect
        src = inspect.getsource(B._llm_phase2)
        self.assertIn("citations", src)
        self.assertIn("编号", src)


if __name__ == "__main__":
    unittest.main()
