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
    _is_entry_recent,
    _is_recent,
    _DEFAULTS,
    _SimpleDoc,
    _group_sentences,
    _build_hierarchical_index,
    _retrieve_with_context,
    _evaluate_insight_quality,
    _self_correct_insights,
    _evaluate_and_correct,
    _refine_labels_with_index,
    _extract_cluster_label,
)

from datetime import datetime, timedelta, timezone

_BJT = timezone(timedelta(hours=8))


def _recent_bjt_iso(hours_ago=10):
    """Timestamp inside the 72h recency window, relative to now (hardcoded dates rot)."""
    return (datetime.now(_BJT) - timedelta(hours=hours_ago)).isoformat()


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
        self.assertEqual(llm.api_keys[0], "test-key-123")
        self.assertEqual(llm.model, "agnes-2.5-flash")
        self.assertEqual(llm.timeout, 60)

    def test_init_custom_params(self):
        """Custom model and timeout."""
        llm = AgnesLLM(api_key="key", model="agnes-pro", timeout=60)
        self.assertEqual(llm.model, "agnes-pro")
        self.assertEqual(llm.timeout, 60)

    def test_init_multi_keys(self):
        """Multiple API keys for rotation."""
        llm = AgnesLLM(api_key="key1", extra_keys=["key2", "key3"])
        self.assertEqual(len(llm.api_keys), 3)
        self.assertEqual(llm.api_keys, ["key1", "key2", "key3"])
        self.assertEqual(llm._key_idx, 0)

    def test_init_extra_keys_filter_empty(self):
        """Empty extra keys are filtered out."""
        llm = AgnesLLM(api_key="key1", extra_keys=["key2", "", None])
        self.assertEqual(len(llm.api_keys), 2)
        self.assertEqual(llm.api_keys, ["key1", "key2"])

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
        llm.api_keys = ["key"]
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
        self.assertEqual(_DEFAULTS["insight_max_documents"], 500)
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
        """No AGNES_API_KEY / AGNES_API_KEYS → MockLLM."""
        # Ensure keys are not set
        os.environ.pop("AGNES_API_KEY", None)
        os.environ.pop("AGNES_API_KEYS", None)
        cfg = {"insight_llm_provider": "agnes"}
        llm = configure_llm(cfg)
        self.assertIsInstance(llm, MockLLM)

    @patch.dict(os.environ, {"AGNES_API_KEY": "test-key-xyz"})
    def test_agnes_config_with_key(self):
        """With AGNES_API_KEY → AgnesLLM."""
        cfg = {"insight_llm_provider": "agnes"}
        llm = configure_llm(cfg)
        self.assertIsInstance(llm, AgnesLLM)
        self.assertEqual(llm.api_keys[0], "test-key-xyz")

    @patch.dict(os.environ, {"AGNES_API_KEY": "k1", "AGNES_API_KEYS": ""}, clear=False)
    def test_agnes_single_key_pool_has_no_extras(self):
        """Only a primary key → pool of 1, extra_keys must stay None (rotation depends on it)."""
        llm = configure_llm({"insight_llm_provider": "agnes"})
        self.assertIsInstance(llm, AgnesLLM)
        self.assertEqual(llm.api_keys, ["k1"])

    @patch.dict(os.environ, {"AGNES_API_KEY": "k1,k2,k3", "AGNES_API_KEYS": ""}, clear=False)
    def test_agnes_comma_separated_primary(self):
        """Comma-separated AGNES_API_KEY expands into the pool."""
        llm = configure_llm({"insight_llm_provider": "agnes"})
        self.assertEqual(llm.api_keys, ["k1", "k2", "k3"])

    @patch.dict(os.environ, {"AGNES_API_KEY": " k1 ", "AGNES_API_KEYS": " , k2 ,,k3, k4 "},
                clear=False)
    def test_agnes_pool_merges_primary_and_extras(self):
        """Primary + AGNES_API_KEYS merge into one pool, whitespace/empty segments dropped."""
        llm = configure_llm({"insight_llm_provider": "agnes"})
        self.assertEqual(llm.api_keys, ["k1", "k2", "k3", "k4"])

    @patch.dict(os.environ, {"AGNES_API_KEY": "", "AGNES_API_KEYS": "k2,k3"}, clear=False)
    def test_agnes_extras_used_when_primary_unset(self):
        """No primary but extras present → still AgnesLLM, no key silently unused."""
        llm = configure_llm({"insight_llm_provider": "agnes"})
        self.assertIsInstance(llm, AgnesLLM)
        self.assertEqual(llm.api_keys, ["k2", "k3"])

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
        recent = _recent_bjt_iso()
        return {
            "item1": {"title": "AI News", "summary": "Latest AI developments", "cat": "ai", "pub_date": recent},
            "item2": {"title": "Tech Update", "summary": "New tech releases", "cat": "tech", "pub_date": recent},
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

    def test_is_entry_recent_pub_date(self):
        """Entry with recent pub_date is recent."""
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(_is_entry_recent({"pub_date": recent}))

    def test_is_entry_recent_first_seen_fallback(self):
        """Entry without pub_date but with recent first_seen is recent."""
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(_is_entry_recent({"first_seen": recent}))

    def test_is_entry_recent_expired(self):
        """Entry with old pub_date and old first_seen is not recent."""
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        self.assertFalse(_is_entry_recent({"pub_date": old, "first_seen": old}))

    def test_is_entry_recent_no_dates(self):
        """Entry without any date fields is not recent."""
        self.assertFalse(_is_entry_recent({}))

    def test_load_documents_filters_old_rss(self):
        """load_documents filters out RSS entries older than 72h."""
        old = "2026-01-01T00:00:00+08:00"
        recent = _recent_bjt_iso()
        rss = {
            "a": {"title": "old article", "pub_date": old, "cat": "tech"},
            "b": {"title": "new article", "pub_date": recent, "cat": "tech"},
        }
        docs = load_documents([], rss, [], max_documents=100)
        texts = [d.text for d in docs]
        self.assertTrue(any("new article" in t for t in texts))
        self.assertFalse(any("old article" in t for t in texts))

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
        # AI texts should cluster together (label contains topic-related words)
        ai_cluster = [c for c in clusters if any(kw in c["label"].lower() for kw in ["ai", "machine", "learning"])]
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
        topic_clusters = [{"label": "AI", "count": 3, "items": ["AI news 1", "AI news 2"]}]
        result = generate_deep_insights(llm, topic_clusters, keywords=["ai", "llm", "agent"])
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
        topic_clusters = [{"label": "AI", "count": 2, "items": ["AI news"]}]
        result = generate_deep_insights(llm, topic_clusters, keywords=["ai", "llm"])
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
        """Keywords are in [word, score] format, with rss and hot pools."""
        hot, rss, trending, config = self._make_data()
        result = run_analysis(hot, rss, trending, config)
        # global keywords
        global_kw = result["keywords"]["global"]
        self.assertIsInstance(global_kw, list)
        if global_kw:
            self.assertEqual(len(global_kw[0]), 2)  # [word, score]
        # source-specific keywords
        self.assertIn("rss", result["keywords"])
        self.assertIn("hot", result["keywords"])
        self.assertIsInstance(result["keywords"]["rss"], list)
        self.assertIsInstance(result["keywords"]["hot"], list)

    def test_run_analysis_source_keywords_separate(self):
        """RSS and hot keywords are extracted separately."""
        hot = [
            {"platform": "weibo", "items": [{"title": "微博热搜话题"}]},
        ]
        rss = {
            "item1": {"title": "OpenAI GPT-5", "summary": "new model release", "cat": "ai"},
        }
        config = dict(_DEFAULTS)
        config["insight_llm_provider"] = "mock"
        result = run_analysis(hot, rss, {}, config)
        # Both pools should exist (MockLLM returns mock keywords)
        self.assertIsInstance(result["keywords"]["rss"], list)
        self.assertIsInstance(result["keywords"]["hot"], list)
        # Global should be merge of both
        self.assertIsInstance(result["keywords"]["global"], list)

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


# ═══════════════════ Hierarchical Index Tests ═════════════════

class TestHierarchicalIndex(unittest.TestCase):
    """小索引大窗口：层级索引与检索测试。"""

    def test_group_sentences_basic(self):
        """Sentence grouping splits text into chunks."""
        text = "这是第一句话。这是第二句话。这是第三句话。这是第四句话。"
        groups = _group_sentences(text, token_target=10)
        self.assertIsInstance(groups, list)
        self.assertGreater(len(groups), 0)
        # 所有句子都被包含
        combined = " ".join(groups)
        self.assertIn("第一句", combined)
        self.assertIn("第四句", combined)

    def test_group_sentences_empty(self):
        """Empty text returns empty list."""
        groups = _group_sentences("", token_target=100)
        self.assertEqual(groups, [])

    def test_group_sentences_single(self):
        """Single sentence returns one group."""
        groups = _group_sentences("只有一句话。", token_target=100)
        self.assertEqual(len(groups), 1)
        self.assertIn("一句话", groups[0])

    def test_group_sentences_respects_target(self):
        """Groups respect token target approximately."""
        # 长文本应被分成多个组
        text = "。".join([f"这是第{i}个句子的内容" for i in range(20)]) + "。"
        groups = _group_sentences(text, token_target=30)
        self.assertGreater(len(groups), 1)

    def test_build_hierarchical_index_no_docs(self):
        """Empty documents returns None."""
        index, parent_docs, child_vecs = _build_hierarchical_index([])
        self.assertIsNone(index)
        self.assertEqual(parent_docs, {})
        self.assertIsNone(child_vecs)

    def test_build_hierarchical_index_with_docs(self):
        """Build index from documents (may skip if llama-index unavailable)."""
        docs = [_SimpleDoc("这是测试文档一。包含多个句子。用于验证层级索引。"),
                _SimpleDoc("这是测试文档二。另一个主题的内容。测试检索功能。")]
        index, parent_docs, child_vecs = _build_hierarchical_index(docs)
        # 在无 llama-index 环境中应返回 None
        if not __import__('insight_engine').LLAMA_INDEX_AVAILABLE:
            self.assertIsNone(index)
            self.assertEqual(parent_docs, {})
        else:
            self.assertIsNotNone(index)
            self.assertEqual(len(parent_docs), 2)

    def test_retrieve_with_context_no_index(self):
        """Retrieve returns None when parent_docs is empty."""
        result = _retrieve_with_context({}, None, None, None)
        self.assertIsNone(result)

    def test_retrieve_with_context_empty_map(self):
        """Retrieve returns None when parent_docs is empty."""
        result = _retrieve_with_context({}, [0.1, 0.2], ["a", "b"], [0.3, 0.4])
        self.assertIsNone(result)


class TestRAGASEvaluation(unittest.TestCase):
    """RAGAS-inspired 评估与自我修正测试。"""

    def test_evaluate_returns_all_dimensions(self):
        """Evaluation returns context_coverage, faithfulness, relevance, overall, feedback."""
        llm = MockLLM()
        insights = {"narrative": "AI技术发展迅速", "causal_chains": ["A→B"],
                    "signals": [], "outlook": "继续看好"}
        result = _evaluate_insight_quality(llm, insights, "some context", keywords=["ai"])
        self.assertIn("context_coverage", result)
        self.assertIn("faithfulness", result)
        self.assertIn("relevance", result)
        self.assertIn("overall", result)
        self.assertIn("feedback", result)
        # Scores should be floats in [0, 1]
        for key in ("context_coverage", "faithfulness", "relevance"):
            self.assertIsInstance(result[key], float)
            self.assertGreaterEqual(result[key], 0.0)
            self.assertLessEqual(result[key], 1.0)

    def test_evaluate_empty_context(self):
        """Evaluation with empty context returns defaults."""
        llm = MockLLM()
        result = _evaluate_insight_quality(llm, {"narrative": "test"}, "", keywords=[])
        self.assertEqual(result["overall"], 0.5)

    def test_evaluate_empty_insights(self):
        """Evaluation with empty insights returns defaults."""
        llm = MockLLM()
        result = _evaluate_insight_quality(llm, {}, "context", keywords=[])
        self.assertEqual(result["overall"], 0.5)

    def test_self_correct_no_low_dims(self):
        """Self-correction returns original when all dims are high."""
        llm = MockLLM()
        insights = {"narrative": "original", "causal_chains": [], "signals": [], "outlook": ""}
        evaluation = {"context_coverage": 0.9, "faithfulness": 0.9,
                      "relevance": 0.9, "overall": 0.9, "feedback": "good"}
        result = _self_correct_insights(llm, insights, "context", evaluation, keywords=[])
        # Should return original (no correction needed)
        self.assertEqual(result["narrative"], "original")

    def test_evaluate_and_correct_disabled(self):
        """When insight_self_correct=False, returns original insights."""
        llm = MockLLM()
        insights = {"narrative": "test", "causal_chains": [], "signals": [], "outlook": ""}
        config = {"insight_self_correct": False}
        result, eval_result = _evaluate_and_correct(
            llm, insights, [], None, None, {}, [], config)
        self.assertEqual(result["narrative"], "test")
        self.assertEqual(eval_result, {})

    def test_evaluate_and_correct_enabled(self):
        """When enabled, evaluation runs and returns scores."""
        llm = MockLLM()
        insights = {"narrative": "AI技术发展", "causal_chains": ["A→B"],
                    "signals": [], "outlook": "继续"}
        clusters = [{"label": "AI", "count": 3, "items": ["AI news 1", "AI news 2"]}]
        config = {"insight_self_correct": True, "insight_quality_threshold": 0.3,
                  "insight_max_corrections": 0}
        result, eval_result = _evaluate_and_correct(
            llm, insights, clusters, None, None, {}, ["ai"], config)
        self.assertIsInstance(result, dict)
        self.assertIn("narrative", result)
        # eval_result should have scores (even if no index)
        self.assertIsInstance(eval_result, dict)


class TestLabelRefinement(unittest.TestCase):
    """Task 4.5: LLM label refinement via index + large window."""

    def test_mock_llm_returns_unchanged(self):
        """MockLLM should skip refinement, labels unchanged."""
        llm = MockLLM()
        clusters = [{"label": "built", "count": 3, "items": ["item1", "item2"]}]
        result = _refine_labels_with_index(clusters, llm,
                                           child_vecs=[[0.1]*5],
                                           child_nodes=[MagicMock()],
                                           parent_docs={"p1": "doc"})
        self.assertEqual(result[0]["label"], "built")

    def test_no_index_returns_unchanged(self):
        """Without index components, labels should remain unchanged."""
        llm = MagicMock()
        clusters = [{"label": "google", "count": 2, "items": ["item1"]}]
        result = _refine_labels_with_index(clusters, llm)
        self.assertEqual(result[0]["label"], "google")
        llm.complete.assert_not_called()

    def test_good_chinese_label_skipped(self):
        """Labels with Chinese chars and len > 6 should not be refined."""
        llm = MagicMock()
        clusters = [{"label": "人工智能发展趋势", "count": 3, "items": ["i1"]}]
        result = _refine_labels_with_index(clusters, llm,
                                           child_vecs=[[0.1]*5],
                                           child_nodes=[MagicMock()],
                                           parent_docs={"p1": "doc"})
        self.assertEqual(result[0]["label"], "人工智能发展趋势")
        llm.complete.assert_not_called()

    def test_empty_clusters(self):
        """Empty cluster list should return empty."""
        result = _refine_labels_with_index([], MagicMock(),
                                           child_vecs=[[0.1]],
                                           child_nodes=[MagicMock()],
                                           parent_docs={"p1": "d"})
        self.assertEqual(result, [])

    def test_tier4_truncation_20_chars(self):
        """Tier 4 fallback should truncate at 20 chars, not 10."""
        # All English text → Tier 1/2 skip (no Chinese), Tier 3 needs frequency
        # With single item, Tier 3 won't fire (needs >=2 frequency)
        texts = ["This is a very long english title about artificial intelligence"]
        label = _extract_cluster_label(texts)
        self.assertLessEqual(len(label), 20)
        self.assertGreater(len(label), 10)  # Would have been 10 before


if __name__ == "__main__":
    unittest.main()
