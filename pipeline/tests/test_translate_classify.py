from __future__ import annotations

import json
import urllib.error
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

    @staticmethod
    def _url_router(responses):
        """按 URL 分发的 opener mock：key 命中哪个端点就回哪个 payload。"""
        opener = mock.MagicMock()

        def open_side_effect(request, timeout=None):
            url = request.full_url
            for key, payload in responses.items():
                if key in url:
                    return _Response(payload)
            raise AssertionError("unexpected url: " + url)

        opener.open.side_effect = open_side_effect
        return opener

    def test_fast_mode_no_key_and_mymemory_down(self) -> None:
        # 无 key、MyMemory 不可达 → fast 返回 None（全程不碰 gtx）
        opener = mock.MagicMock()
        opener.open.side_effect = RuntimeError("network down")
        with mock.patch.object(translate.llm, "key_pool", return_value=[]), \
             mock.patch.object(translate, "_SAFE_OPENER", opener), \
             mock.patch.object(translate.netguard, "assert_safe_url"):
            self.assertIsNone(translate.translate_to_zh("hello", fast=True))

    def test_agnes_translation(self) -> None:
        payload = {"choices": [{"message": {"content": "你好世界"}}]}
        opener = mock.MagicMock()
        opener.open.return_value = _Response(payload)
        with mock.patch.object(translate.llm, "key_pool", return_value=["key"]), \
             mock.patch.object(translate.llm, "_SAFE_OPENER", opener), \
             mock.patch.object(translate.llm.netguard, "assert_safe_url"), \
             mock.patch.object(translate.time, "sleep"):
            self.assertEqual(translate.translate_to_zh("hello world"), "你好世界")

    def test_fast_mode_llm_success_skips_free_endpoints(self) -> None:
        # fast：LLM 成功就返回，免费端点一次都不碰
        payload = {"choices": [{"message": {"content": "你好世界"}}]}
        opener = mock.MagicMock()
        opener.open.return_value = _Response(payload)
        free_opener = mock.MagicMock()
        with mock.patch.object(translate.llm, "key_pool", return_value=["key"]), \
             mock.patch.object(translate.llm, "_SAFE_OPENER", opener), \
             mock.patch.object(translate.llm.netguard, "assert_safe_url"), \
             mock.patch.object(translate, "_SAFE_OPENER", free_opener), \
             mock.patch.object(translate.time, "sleep"):
            self.assertEqual(translate.translate_to_zh("hello world", fast=True), "你好世界")
        free_opener.open.assert_not_called()

    def test_fast_mode_mymemory_fallback(self) -> None:
        # fast：Agnes 被数据中心 429 时，兜底是 MyMemory，且绝不撞 gtx
        llm_opener = mock.MagicMock()
        llm_opener.open.side_effect = urllib.error.HTTPError(
            "https://apihub.agnes-ai.com/v1/chat/completions", 429, "rate limited", None, None)
        free_opener = self._url_router({
            "mymemory.translated.net": {"responseData": {"translatedText": "你好"}},
        })
        requested = []
        router_open = free_opener.open.side_effect

        def spy(request, timeout=None):
            requested.append(request.full_url)
            return router_open(request, timeout=timeout)

        free_opener.open.side_effect = spy
        with mock.patch.object(translate.llm, "key_pool", return_value=["key"]), \
             mock.patch.object(translate.llm, "_SAFE_OPENER", llm_opener), \
             mock.patch.object(translate.llm.netguard, "assert_safe_url"), \
             mock.patch.object(translate, "_SAFE_OPENER", free_opener), \
             mock.patch.object(translate.netguard, "assert_safe_url"), \
             mock.patch.object(translate.time, "sleep"):
            self.assertEqual(translate.translate_to_zh("hello world", fast=True), "你好")
        self.assertTrue(all("google" not in url for url in requested), requested)

    def test_google_translation(self) -> None:
        # 非 fast：LLM 无 key、MyMemory 给不出中文 → gtx 兜底
        free_opener = self._url_router({
            "translate.googleapis.com": [[["你好世界", "hello world", None, None]]],
            "mymemory.translated.net": {"responseData": {"translatedText": ""}},
        })
        with mock.patch.object(translate.llm, "key_pool", return_value=[]), \
             mock.patch.object(translate, "_SAFE_OPENER", free_opener), \
             mock.patch.object(translate.netguard, "assert_safe_url"):
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
