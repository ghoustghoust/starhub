from __future__ import annotations

import json
import unittest
from unittest import mock

from starhub_pipeline.enrich import classify, llm, translate


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TranslateTest(unittest.TestCase):
    def setUp(self) -> None:
        translate.reset_circuit()

    def test_fast_mode_skips_network(self) -> None:
        self.assertIsNone(translate.translate_to_zh("hello", fast=True))

    def test_google_translation(self) -> None:
        payload = [[["你好世界", "hello world", None, None]]]
        with mock.patch("starhub_pipeline.enrich.translate.urllib.request.urlopen", return_value=_Response(payload)):
            self.assertEqual(translate.translate_to_zh("hello world"), "你好世界")

    def test_has_cn(self) -> None:
        self.assertTrue(translate.has_cn("中文"))
        self.assertFalse(translate.has_cn("English"))


class ClassifyTest(unittest.TestCase):
    def setUp(self) -> None:
        classify.reset_circuit()

    def test_rule_classification(self) -> None:
        self.assertEqual(classify.classify_new("owner/video-agent", "video generation", None, []), "video")
        self.assertEqual(classify.classify_new("owner/foo", "unknown thing", None, []), "agent")

    def test_known_category_wins(self) -> None:
        result = classify.classify_repo("owner/repo", "video", None, [], {"owner/repo": "coding"}, [], fast=True)
        self.assertEqual(result, ("coding", ""))

    def test_fast_fallback_defaults_to_tools(self) -> None:
        result = classify.classify_repo("owner/foo", "unknown thing", None, [], {}, [], fast=True)
        self.assertEqual(result, ("tools", ""))

    def test_llm_result_is_validated(self) -> None:
        cats = [{"key": "tools", "label": "效率工具"}]
        with mock.patch.object(llm, "chat", return_value='{"category":"tools","note":"好用"}'):
            result = classify.classify_llm("owner/repo", "desc", "Python", [], cats)
        self.assertEqual(result, {"category": "tools", "note": "好用"})

    def test_llm_rejects_unknown_category(self) -> None:
        cats = [{"key": "tools", "label": "效率工具"}]
        with mock.patch.object(llm, "chat", return_value='{"category":"unknown","note":""}'):
            self.assertIsNone(classify.classify_llm("owner/repo", "desc", "Python", [], cats))


if __name__ == "__main__":
    unittest.main()
