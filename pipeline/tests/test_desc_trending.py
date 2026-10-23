from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starhub_pipeline.build import trending
from starhub_pipeline.enrich import desc


class DescTest(unittest.TestCase):
    def test_cache_and_fast(self) -> None:
        cache = {}
        self.assertEqual(desc.desc_zh("a/b", "中文介绍", cache), "中文介绍")
        self.assertEqual(cache["a/b"], "中文介绍")
        self.assertEqual(desc.desc_zh("a/c", "English description", cache, fast=True), "English description")

    def test_translation(self) -> None:
        cache = {}
        with mock.patch.object(desc, "translate_to_zh", return_value="中文"):
            self.assertEqual(desc.desc_zh("a/b", "english", cache), "中文")
        self.assertEqual(cache["a/b"], "中文")


class TrendingTest(unittest.TestCase):
    def test_builds_boards_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap.json"
            with mock.patch.object(trending, "fetch_ai_pool", return_value=[
                {"full_name": "a/one", "stars": 100, "desc": "one"},
                {"full_name": "b/two", "stars": 200, "desc": "two"},
            ]), mock.patch.object(trending, "fetch_trending_daily", return_value=None), \
                 mock.patch.object(trending, "fetch_new_repos", return_value=[]):
                result = trending.build_trending(
                    "token", {}, ai_topics=["ai"], ai_min_stars=1, new_min_stars=1,
                    trend_top=20, trend_max_stars=50000, fast=True, snapshot_path=str(snapshot),
                )
            self.assertEqual(result["total"][0]["full_name"], "b/two")
            self.assertTrue(snapshot.exists())


if __name__ == "__main__":
    unittest.main()
