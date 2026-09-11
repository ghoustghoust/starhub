"""
test_insight_engine.py — Tests for insight_engine module (Tasks 1-5).
"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from io import BytesIO

# Ensure the module under test is importable
sys.path.insert(0, os.path.dirname(__file__))

from insight_engine import (
    AgnesLLM,
    MockLLM,
    load_config,
    configure_llm,
    load_documents,
    build_index,
    extract_keywords_llm,
    cluster_topics_embedding,
    cross_platform_semantic,
    generate_deep_insights,
    run_analysis,
    _try_parse_json,
    _cosine_similarity,
    _average_vector,
    _fallback_cluster,
    _is_recent,
    _DEFAULTS,
    _SimpleDoc,
)


# ═══════════════════ Task 1 Tests ════════════════════════════

class TestAgnesLLM(unittest.TestCase):
    """Task 1: AgnesLLM wrapper + MockLLM tests."""

    def test_init_requires_api_key(self):
        """ValueError when no api_key provided."""
        with self.assertRaises(ValueError):
            AgnesLLM(api_key="")
        with self.assertRaises(ValueError):
            AgnesLLM(api_key=None)

    def test_init_with_api_key(self):
        """Success with valid api_key."""
        llm = AgnesLLM(api_key="test-key-123")
        self.assertEqual(llm.api_key, "test-key-123")
        self.assertEqual(llm.model, "agnes-2.5-flash")
        self.assertEqual(llm.timeout, 30)

    def test_init_custom_params(self):
        """Custom model and timeout."""
        llm = AgnesLLM(api_key="key", model="agnes-pro", timeout=60)
        self.assertEqual(llm.model, "agnes-pro")
        self.assertEqual(llm.timeout, 60)

    def test_metadata(self):
        """Metadata property returns correct dict."""
        llm = AgnesLLM(api_key="key")
        meta = llm.metadata
        self.assertEqual(meta["model_name"], "agnes-2.5-flash")
        self.assertEqual(meta["context_window"], 8192)

    @patch("insight_engine.urllib.request.urlopen")
    def test_complete_returns_string(self, mock_urlopen):
        """Mocked urllib returns string response."""
        response_body = json.dumps({
            "choices": [{"message": {"content": "Hello, world!"}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__ = lambda s: BytesIO(response_body)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        llm = AgnesLLM(api_key="test-key")
        result = llm.complete("Say hello")
        self.assertIsInstance(result, str)
        self.assertEqual(result, "Hello, world!")

    @patch("insight_engine.urllib.request.urlopen")
    def test_complete_with_system_prompt(self, mock_urlopen):
        """complete() sends system_prompt in messages."""
        response_body = json.dumps({
            "choices": [{"message": {"content": "response"}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__ = lambda s: BytesIO(response_body)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        llm = AgnesLLM(api_key="key")
        result = llm.complete("prompt", system_prompt="You are helpful")
        self.assertIsInstance(result, str)

    def test_stream_complete_yields_result(self):
        """stream_complete yields the complete result."""
        llm = AgnesLLM.__new__(AgnesLLM)
        llm.api_key = "key"
        llm.model = "test"
        llm.timeout = 30
        # Patch complete on the instance
        llm.complete = MagicMock(return_value="streamed result")
        results = list(llm.stream_complete("test"))
        self.assertEqual(results, ["streamed result"])


class TestMockLLM(unittest.TestCase):
    """Task 1: MockLLM tests."""

    def test_complete_returns_mock_response(self):
        """MockLLM returns a non-empty string."""
        llm = MockLLM()
        result = llm.complete("Tell me something")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_keyword_prompt_returns_json_array(self):
        """MockLLM returns JSON array for keyword prompts."""
        llm = MockLLM()
        result = llm.complete("请提取关键词")
        parsed = json.loads(result)
        self.assertIsInstance(parsed, list)
        self.assertTrue(len(parsed) > 0)

    def test_insight_prompt_returns_json_object(self):
        """MockLLM returns JSON object for insight prompts."""
        llm = MockLLM()
        result = llm.complete("请生成深度洞察")
        parsed = json.loads(result)
        self.assertIsInstance(parsed, dict)
        self.assertIn("narrative", parsed)

    def test_metadata(self):
        """MockLLM metadata property."""
        llm = MockLLM()
        meta = llm.metadata
        self.assertIn("model_name", meta)
        self.assertIn("context_window", meta)

    def test_stream_complete(self):
        """stream_complete yields result."""
        llm = MockLLM()
        results = list(llm.stream_complete("test"))
        self.assertEqual(len(results), 1)


# ═══════════════════ Task 2 Tests ════════════════════════════

class TestConfigAndFactory(unittest.TestCase):
    """Task 2: Config layer + LLM factory function tests."""

    def test_defaults_exist(self):
        """_DEFAULTS has all required keys."""
        self.assertTrue(_DEFAULTS["insight_engine_enabled"])
        self.assertEqual(_DEFAULTS["insight_llm_provider"], "agnes")
        self.assertEqual(_DEFAULTS["insight_max_documents"], 200)
        self.assertEqual(_DEFAULTS["insight_top_keywords"], 30)
        self.assertEqual(_DEFAULTS["insight_top_topics"], 15)

    def test_load_config_returns_defaults(self):
        """load_config with missing file returns defaults."""
        cfg = load_config("nonexistent_file.json")
        self.assertTrue(cfg["insight_engine_enabled"])
        self.assertEqual(cfg["insight_llm_provider"], "agnes")

    def test_load_config_merges_existing(self):
        """load_config merges existing build_config.json."""
        cfg = load_config("build_config.json")
        # Should have both build_config keys and defaults
        self.assertIn("trend_top", cfg)
        self.assertIn("insight_engine_enabled", cfg)

    @patch.dict(os.environ, {}, clear=False)
    def test_default_config_returns_mock_when_no_key(self):
        """No AGNES_API_KEY → MockLLM."""
        # Ensure key is not set
        os.environ.pop("AGNES_API_KEY", None)
        cfg = {"insight_llm_provider": "agnes"}
        llm = configure_llm(cfg)
        self.assertIsInstance(llm, MockLLM)

    @patch.dict(os.environ, {"AGNES_API_KEY": "test-key-xyz"})
    def test_agnes_config_with_key(self):
        """With AGNES_API_KEY → AgnesLLM."""
        cfg = {"insight_llm_provider": "agnes"}
        llm = configure_llm(cfg)
        self.assertIsInstance(llm, AgnesLLM)
        self.assertEqual(llm.api_key, "test-key-xyz")

    def test_unknown_provider_returns_mock(self):
        """Unknown provider → MockLLM."""
        cfg = {"insight_llm_provider": "unknown_provider"}
        llm = configure_llm(cfg)
        self.assertIsInstance(llm, MockLLM)


# ═══════════════════ Task 3 Tests ════════════════════════════

class TestDataLoading(unittest.TestCase):
    """Task 3: Data loading tests."""

    def _make_hot_snapshot(self):
        return [
            {"platform": "weibo", "name": "weibo", "items": [
                {"rank": 1, "title": "测试热搜标题"},
                {"rank": 2, "title": "第二条热搜"},
            ]},
            {"platform": "zhihu", "name": "zhihu", "items": [
                {"rank": 1, "title": "知乎热门问题"},
            ]},
        ]

    def _make_rss_history(self):
        return {
            "item1": {"title": "AI News", "summary": "Latest AI developments", "cat": "ai"},
            "item2": {"title": "Tech Update", "summary": "New tech releases", "cat": "tech"},
        }

    def _make_trending_data(self):
        return {"user/repo1": 1000, "user/repo2": 2000}

    def test_load_documents_hot_items(self):
        """Hot items are converted to documents with correct prefix."""
        docs = load_documents(self._make_hot_snapshot(), None, None)
        texts = [d.text for d in docs]
        self.assertTrue(any("[热榜/weibo]" in t for t in texts))
        self.assertTrue(any("测试热搜标题" in t for t in texts))

    def test_load_documents_rss_items(self):
        """RSS items are converted with [RSS/cat] prefix."""
        docs = load_documents(None, self._make_rss_history(), None)
        texts = [d.text for d in docs]
        self.assertTrue(any("[RSS/ai]" in t for t in texts))
        self.assertTrue(any("AI News" in t for t in texts))

    def test_load_documents_trending(self):
        """Trending items have [Trending] prefix."""
        docs = load_documents(None, None, self._make_trending_data())
        texts = [d.text for d in docs]
        self.assertTrue(any("[Trending]" in t for t in texts))
        self.assertTrue(any("repo1" in t for t in texts))

    def test_load_documents_priority_order(self):
        """Hot > Trending > RSS priority ordering."""
        docs = load_documents(
            self._make_hot_snapshot(),
            self._make_rss_history(),
            self._make_trending_data(),
        )
        # First docs should be hot (priority 0)
        self.assertIn("[热榜/", docs[0].text)

    def test_load_documents_max_truncation(self):
        """max_documents parameter limits total count."""
        hot = [{"platform": "test", "items": [{"title": f"item{i}"} for i in range(100)]}]
        docs = load_documents(hot, None, None, max_documents=10)
        self.assertEqual(len(docs), 10)

    def test_load_documents_empty_data(self):
        """Empty data returns empty list."""
        docs = load_documents(None, None, None)
        self.assertEqual(len(docs), 0)

    def test_build_index_returns_none_without_deps(self):
        """build_index returns None when deps are unavailable (test env)."""
        # In test env, llama-index may not be installed
        docs = [_SimpleDoc("test document")]
        result = build_index(docs)
        # If LLAMA_INDEX_AVAILABLE is False, result should be None
        from insight_engine import LLAMA_INDEX_AVAILABLE, FASTEMBED_AVAILABLE
        if not LLAMA_INDEX_AVAILABLE or not FASTEMBED_AVAILABLE:
            self.assertIsNone(result)


# ═══════════════════ Task 4 Tests ════════════════════════════

class TestHelpers(unittest.TestCase):
    """Task 4: Helper function tests."""

    def test_try_parse_json_valid(self):
        """Valid JSON is parsed correctly."""
        result = _try_parse_json('["a", "b"]')
        self.assertEqual(result, ["a", "b"])

    def test_try_parse_json_with_fences(self):
        """JSON with markdown code fences is parsed."""
        result = _try_parse_json('```json\n{"key": "value"}\n```')
        self.assertEqual(result, {"key": "value"})

    def test_try_parse_json_invalid(self):
        """Invalid JSON returns None."""
        result = _try_parse_json("not json at all")
        self.assertIsNone(result)

    def test_try_parse_json_empty(self):
        """Empty string returns None."""
        self.assertIsNone(_try_parse_json(""))
        self.assertIsNone(_try_parse_json(None))

    def test_cosine_similarity_identical(self):
        """Identical vectors have similarity 1.0."""
        v = [1.0, 2.0, 3.0]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0)

    def test_cosine_similarity_orthogonal(self):
        """Orthogonal vectors have similarity ~0."""
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertAlmostEqual(_cosine_similarity(a, b), 0.0)

    def test_cosine_similarity_empty(self):
        """Empty vectors return 0."""
        self.assertEqual(_cosine_similarity([], []), 0.0)

    def test_average_vector(self):
        """Average of vectors is computed correctly."""
        vecs = [[1.0, 2.0], [3.0, 4.0]]
        avg = _average_vector(vecs)
        self.assertAlmostEqual(avg[0], 2.0)
        self.assertAlmostEqual(avg[1], 3.0)

    def test_average_vector_empty(self):
        """Empty list returns empty."""
        self.assertEqual(_average_vector([]), [])

    def test_fallback_cluster(self):
        """Character overlap clustering groups similar texts."""
        texts = [
            "AI artificial intelligence machine learning",
            "AI machine learning deep learning",
            "cooking recipe food restaurant",
            "cooking food kitchen recipe",
        ]
        clusters = _fallback_cluster(texts, max_topics=5, threshold=0.3)
        self.assertTrue(len(clusters) >= 1)
        # AI texts should cluster together
        ai_cluster = [c for c in clusters if "AI" in c["label"] or "machine" in c["label"]]
        self.assertTrue(len(ai_cluster) >= 1)


class TestExtractKeywords(unittest.TestCase):
    """Task 4: Keyword extraction tests."""

    def test_extract_keywords_with_mock_llm(self):
        """MockLLM returns keyword list."""
        llm = MockLLM()
        texts = ["AI is changing the world", "New LLM models released"]
        keywords = extract_keywords_llm(llm, texts, top_n=10)
        self.assertIsInstance(keywords, list)
        self.assertTrue(len(keywords) > 0)

    def test_extract_keywords_empty_texts(self):
        """Empty texts returns empty list."""
        llm = MockLLM()
        result = extract_keywords_llm(llm, [], top_n=10)
        self.assertEqual(result, [])


class TestClusterTopics(unittest.TestCase):
    """Task 4: Topic clustering tests."""

    def test_cluster_empty(self):
        """Empty articles returns empty."""
        result = cluster_topics_embedding([])
        self.assertEqual(result, [])

    def test_cluster_fallback(self):
        """Without embeddings, falls back to char-overlap clustering."""
        articles = [
            "AI artificial intelligence deep learning",
            "AI deep learning neural network",
            "football match result today",
            "football game score result",
        ]
        result = cluster_topics_embedding(articles, max_topics=5)
        self.assertIsInstance(result, list)


class TestCrossPlatform(unittest.TestCase):
    """Task 4: Cross-platform semantic tests."""

    def test_cross_platform_empty_input(self):
        """Empty snapshot returns empty."""
        result = cross_platform_semantic(None)
        self.assertEqual(result, [])

    def test_cross_platform_single_platform(self):
        """Single platform returns empty."""
        snapshot = [{"platform": "weibo", "items": [{"title": "test"}]}]
        result = cross_platform_semantic(snapshot)
        self.assertEqual(result, [])


# ═══════════════════ Task 5 Tests ════════════════════════════

class TestDeepInsights(unittest.TestCase):
    """Task 5: Deep insights generation tests."""

    def test_generate_deep_insights_with_mock(self):
        """MockLLM generates structured insights."""
        llm = MockLLM()
        context = {"keywords": ["ai", "llm", "agent"], "topic_count": 5}
        result = generate_deep_insights(llm, context)
        self.assertIsInstance(result, dict)
        self.assertIn("narrative", result)
        self.assertIn("causal_chains", result)
        self.assertIn("signals", result)
        self.assertIn("outlook", result)

    def test_generate_deep_insights_fallback(self):
        """Fallback when LLM returns non-JSON."""
        llm = MockLLM()
        # Override complete to return non-JSON
        llm.complete = MagicMock(return_value="not json")
        context = {"keywords": ["ai", "llm"]}
        result = generate_deep_insights(llm, context)
        self.assertIsInstance(result, dict)
        self.assertIn("narrative", result)
        self.assertIn("ai", result["narrative"])


class TestIsRecent(unittest.TestCase):
    """Task 5: _is_recent helper tests."""

    def test_recent_item(self):
        """Item with recent pub_date is recent."""
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        item = {"pub_date": recent}
        self.assertTrue(_is_recent(item, hours=24))

    def test_old_item(self):
        """Item with old pub_date is not recent."""
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        item = {"pub_date": old}
        self.assertFalse(_is_recent(item, hours=24))

    def test_no_date_assumed_recent(self):
        """Item without date field is assumed recent."""
        item = {"title": "no date"}
        self.assertTrue(_is_recent(item, hours=24))


class TestRunAnalysis(unittest.TestCase):
    """Task 5: run_analysis integration tests."""

    def _make_data(self):
        hot = [
            {"platform": "weibo", "items": [{"title": "热搜测试"}]},
        ]
        rss = {
            "item1": {"title": "AI News", "summary": "test", "cat": "ai"},
        }
        trending = {"user/repo": 1000}
        config = dict(_DEFAULTS)
        # Force MockLLM to avoid real API calls in tests
        config["insight_llm_provider"] = "mock"
        return hot, rss, trending, config

    def test_run_analysis_disabled(self):
        """Returns None when disabled."""
        hot, rss, trending, config = self._make_data()
        config["insight_engine_enabled"] = False
        result = run_analysis(hot, rss, trending, config)
        self.assertIsNone(result)

    def test_run_analysis_returns_dict(self):
        """Returns analysis dict when enabled."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        self.assertIsInstance(result, dict)

    def test_run_analysis_has_old_format_fields(self):
        """Output includes all old-format fields."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        for key in ("generated_at", "keywords", "rising", "topics", "summary", "stats"):
            self.assertIn(key, result, f"Missing old-format key: {key}")

    def test_run_analysis_has_new_fields(self):
        """Output includes new insight engine fields."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        self.assertIn("deep_insights", result)
        self.assertIn("topic_clusters", result)
        self.assertIn("meta", result)

    def test_run_analysis_summary_format(self):
        """Summary uses old 4-section format."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        summary = result["summary"]
        for key in ("core_trends", "signals", "rss_insights", "outlook"):
            self.assertIn(key, summary)

    def test_run_analysis_keywords_format(self):
        """Keywords are in [word, score] format."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        global_kw = result["keywords"]["global"]
        self.assertIsInstance(global_kw, list)
        if global_kw:
            self.assertEqual(len(global_kw[0]), 2)  # [word, score]

    def test_run_analysis_meta_fields(self):
        """Meta contains engine info."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        meta = result["meta"]
        self.assertEqual(meta["engine"], "insight_engine")
        self.assertIn("llm_provider", meta)
        self.assertIn("llm_available", meta)
        self.assertIn("index_built", meta)

    def test_run_analysis_with_prev_keywords(self):
        """Rising detection works with prev_keywords."""
        hot, rss, trending, config = self._make_data()
        prev_kw = ["old_keyword"] + ["ai"] * 20  # ai was ranked low
        result = run_analysis(hot, rss, trending, config, prev_keywords=prev_kw)
        self.assertIsInstance(result["rising"], list)

    def test_run_analysis_has_cross_category(self):
        """Output includes cross_category field (even if empty)."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        self.assertIn("cross_category", result)
        self.assertIsInstance(result["cross_category"], list)


if __name__ == "__main__":
    unittest.main()
