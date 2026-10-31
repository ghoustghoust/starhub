from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starhub_pipeline.build import contracts


class ContractsTest(unittest.TestCase):
    def test_contract_item_shape(self) -> None:
        entry = contracts.contract_item({
            "full_name": "owner/repo",
            "html_url": "https://github.com/owner/repo",
            "desc": "描述",
            "stars": "12",
            "health": "green",
            "emb": [1, 2],
        })
        self.assertEqual(entry["id"], "owner/repo")
        self.assertEqual(entry["stars"], 12)
        self.assertEqual(entry["health"], {"level": "green", "stale": False})
        self.assertEqual(entry["embedding"], [1, 2])

    def test_writes_pool_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contracts.write_pool_script(
                [{"full_name": "owner/repo", "desc": "line\u2028separator"}],
                "2026-10-23 10:00",
                data_dir=tmp,
            )
            text = Path(tmp, "pool.js").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.__STARHUB_POOL_DATA__="))
            payload = json.loads(text[len("window.__STARHUB_POOL_DATA__="):].rstrip(";\n"))
            self.assertEqual(payload["items"][0]["full_name"], "owner/repo")
            self.assertEqual(payload["updated"], "2026-10-23 10:00")
            self.assertIn("\\u2028", text)

    def test_writes_all_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contracts.write_site_contracts(
                [{"full_name": "owner/repo", "html_url": "https://github.com/owner/repo"}],
                {"owner/repo": "tools"},
                {"rising": [], "total": [], "new": [],
                 "downloads": [{"full_name": "a/one", "downloads": 12000}]},
                "summary",
                "2026-10-23 10:00",
                [{"key": "tools", "label": "效率工具"}],
                data_dir=tmp,
            )
            stars = json.loads(Path(tmp, "stars.json").read_text(encoding="utf-8"))
            categories = json.loads(Path(tmp, "categories.json").read_text(encoding="utf-8"))
            trending = json.loads(Path(tmp, "trending.json").read_text(encoding="utf-8"))
            self.assertEqual(stars["items"][0]["full_name"], "owner/repo")
            self.assertEqual(categories["labels"]["tools"], "效率工具")
            self.assertEqual(trending["boards"]["downloads"][0]["downloads"], 12000)
            self.assertTrue(Path(tmp, "trending.json").exists())
            self.assertTrue(Path(tmp, "build-status.json").exists())

    def test_trending_defaults_to_empty_downloads_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contracts.write_site_contracts(
                [], {}, {"rising": [], "total": [], "new": []}, "", "2026-10-23 10:00",
                [], data_dir=tmp,
            )
            trending = json.loads(Path(tmp, "trending.json").read_text(encoding="utf-8"))
            self.assertEqual(trending["boards"]["downloads"], [])


if __name__ == "__main__":
    unittest.main()
