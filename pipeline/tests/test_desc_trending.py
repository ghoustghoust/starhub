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
                 mock.patch.object(trending, "fetch_new_repos", return_value=[]), \
                 mock.patch.object(trending, "fetch_download_totals", return_value={}):
                result = trending.build_trending(
                    "token", {}, ai_topics=["ai"], ai_min_stars=1, new_min_stars=1,
                    trend_top=20, trend_max_stars=50000, fast=True, snapshot_path=str(snapshot),
                )
            self.assertEqual(result["total"][0]["full_name"], "b/two")
            self.assertEqual(result["downloads"], [])
            self.assertTrue(snapshot.exists())

    def test_downloads_board_sorted_and_translated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap.json"
            pool = [
                {"full_name": "a/one", "name": "one", "owner": "a",
                 "html_url": "https://github.com/a/one", "stars": 100, "desc": "one"},
                {"full_name": "b/two", "name": "two", "owner": "b",
                 "html_url": "https://github.com/b/two", "stars": 200, "desc": "two"},
            ]
            with mock.patch.object(trending, "fetch_ai_pool", return_value=pool), \
                 mock.patch.object(trending, "fetch_trending_daily", return_value=None), \
                 mock.patch.object(trending, "fetch_new_repos", return_value=[]), \
                 mock.patch.object(trending, "fetch_download_totals",
                                   return_value={"a/one": 12000, "b/two": 500}) as totals:
                result = trending.build_trending(
                    "token", {"a/one": "中文一"}, ai_topics=["ai"], ai_min_stars=1, new_min_stars=1,
                    trend_top=20, trend_max_stars=50000, fast=True, snapshot_path=str(snapshot),
                )
            downloads = result["downloads"]
            self.assertEqual([row["full_name"] for row in downloads], ["a/one", "b/two"])
            self.assertEqual(downloads[0]["downloads"], 12000)
            self.assertEqual(downloads[0]["reason"], "累计下载 1.2万次")
            self.assertEqual(downloads[0]["desc"], "中文一")
            self.assertEqual(downloads[1]["reason"], "累计下载 500次")
            totals.assert_called_once_with("token", ["b/two", "a/one"])

    def test_downloads_board_skips_zero_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snap.json"
            pool = [{"full_name": "a/one", "stars": 100, "desc": "one"}]
            with mock.patch.object(trending, "fetch_ai_pool", return_value=pool), \
                 mock.patch.object(trending, "fetch_trending_daily", return_value=None), \
                 mock.patch.object(trending, "fetch_new_repos", return_value=[]), \
                 mock.patch.object(trending, "fetch_download_totals", return_value={"a/one": 0}):
                result = trending.build_trending(
                    "token", {}, ai_topics=["ai"], ai_min_stars=1, new_min_stars=1,
                    trend_top=20, trend_max_stars=50000, fast=True, snapshot_path=str(snapshot),
                )
            self.assertEqual(result["downloads"], [])
            self.assertEqual(len(result["total"]), 1)


if __name__ == "__main__":
    unittest.main()
