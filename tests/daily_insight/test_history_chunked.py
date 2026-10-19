# -*- coding: utf-8 -*-
"""FIX-6: rss_history 分块存储测试。"""
import os
import sys
import json
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

import build_rss_aggregator as R


class TestSaveHistoryChunked(unittest.TestCase):
    """_save_history_chunked 应在超限时分块。"""

    def test_small_history_uses_chunk_format(self):
        """09-19 重设计：小历史也恒走分块（主文件不再写，_load 以 index 为准）。"""
        with tempfile.TemporaryDirectory() as td:
            orig_file = R.RSS_HISTORY_FILE
            try:
                R.RSS_HISTORY_FILE = os.path.join(td, "rss_history.json")
                R._rss_history = {"link1": {"title": "a"}, "link2": {"title": "b"}}
                R._save_history_chunked()
                self.assertFalse(os.path.exists(R.RSS_HISTORY_FILE),
                                 "主文件不应再被小历史改写（index 优先加载会忽略它）")
                idx = json.load(open(os.path.join(td, "rss_history_index.json"), encoding="utf-8"))
                self.assertEqual(idx, {"chunks": 1, "total": 2})
                chunk = json.load(open(os.path.join(td, "rss_history_0.json"), encoding="utf-8"))
                self.assertEqual(len(chunk), 2)
                loaded = R._load_history_chunked(hist_file=R.RSS_HISTORY_FILE,
                                                 index_file=os.path.join(td, "rss_history_index.json"))
                self.assertEqual(sorted(loaded.keys()), ["link1", "link2"])
            finally:
                R.RSS_HISTORY_FILE = orig_file

    def test_large_history_chunked(self):
        """超阈值数据应分块保存。"""
        with tempfile.TemporaryDirectory() as td:
            orig_file = R.RSS_HISTORY_FILE
            try:
                big_history = {}
                for i in range(10000):
                    big_history["link_%d" % i] = {
                        "title": "x" * 2000,
                        "full_content": "y" * 3000,
                    }
                R._rss_history = big_history
                R.RSS_HISTORY_FILE = os.path.join(td, "rss_history.json")
                R._save_history_chunked(max_size=1024 * 1024)  # 1MB 阈值
                index_file = os.path.join(td, "rss_history_index.json")
                self.assertTrue(os.path.exists(index_file))
                index = json.load(open(index_file, "r", encoding="utf-8"))
                self.assertGreater(index["chunks"], 1)
                self.assertEqual(index["total"], 10000)
            finally:
                R.RSS_HISTORY_FILE = orig_file


class TestLoadHistoryChunked(unittest.TestCase):
    """_load_history_chunked 应兼容新旧格式。"""

    def test_load_legacy_single_file(self):
        """旧格式单文件应正常加载。"""
        with tempfile.TemporaryDirectory() as td:
            legacy = {"link1": {"title": "a"}, "link2": {"title": "b"}}
            hist_file = os.path.join(td, "rss_history.json")
            with open(hist_file, "w", encoding="utf-8") as f:
                json.dump(legacy, f)
            result = R._load_history_chunked(hist_file, os.path.join(td, "rss_history_index.json"))
            self.assertEqual(len(result), 2)
            self.assertIn("link1", result)

    def test_load_chunked_files(self):
        """分块格式应正确合并。"""
        with tempfile.TemporaryDirectory() as td:
            chunk0 = {"link_a": {"title": "A"}}
            chunk1 = {"link_b": {"title": "B"}}
            hist_file = os.path.join(td, "rss_history.json")
            index_file = os.path.join(td, "rss_history_index.json")
            with open(os.path.join(td, "rss_history_0.json"), "w", encoding="utf-8") as f:
                json.dump(chunk0, f)
            with open(os.path.join(td, "rss_history_1.json"), "w", encoding="utf-8") as f:
                json.dump(chunk1, f)
            with open(index_file, "w", encoding="utf-8") as f:
                json.dump({"chunks": 2, "total": 2}, f)
            result = R._load_history_chunked(hist_file, index_file)
            self.assertEqual(len(result), 2)
            self.assertIn("link_a", result)
            self.assertIn("link_b", result)

    def test_load_empty(self):
        """无文件时返回空 dict。"""
        with tempfile.TemporaryDirectory() as td:
            result = R._load_history_chunked(
                os.path.join(td, "nonexist.json"),
                os.path.join(td, "nonexist_index.json"))
            self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
