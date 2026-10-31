from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starhub_pipeline.enrich import summarize


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SummarizeTest(unittest.TestCase):
    def test_fast_modes_skip_network(self) -> None:
        self.assertIsNone(summarize.generate_ai_summary([], fast=True))
        self.assertEqual(summarize.build_category_guides([], {}, fast=True), {})

    def test_summary_with_mocked_llm(self) -> None:
        payload = {"choices": [{"message": {"content": "今日 AI 生态活跃"}, "finish_reason": "stop"}], "usage": {}}
        opener = mock.MagicMock()
        opener.open.return_value = _Response(payload)
        with mock.patch.object(summarize.llm, "key_pool", return_value=["key"]), \
             mock.patch.object(summarize.llm, "_SAFE_OPENER", opener), \
             mock.patch.object(summarize.llm.netguard, "assert_safe_url"):
            result = summarize.generate_ai_summary([{"full_name": "owner/repo", "delta": 10}])
        self.assertEqual(result, "今日 AI 生态活跃")

    def test_guides_cache_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(summarize.llm, "generate", return_value="适合做工具"), \
             mock.patch.object(summarize.time, "sleep", return_value=None):
            result = summarize.build_category_guides(
                [{"key": "tools", "label": "效率工具"}],
                {"a/b": "tools"},
                cache_path=Path(tmp) / "guides.json",
            )
        self.assertEqual(result, {"tools": "适合做工具"})


if __name__ == "__main__":
    unittest.main()
