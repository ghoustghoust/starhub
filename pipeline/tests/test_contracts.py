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

    def test_writes_all_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contracts.write_site_contracts(
                [{"full_name": "owner/repo", "html_url": "https://github.com/owner/repo"}],
                {"owner/repo": "tools"},
                {"rising": [], "total": [], "new": []},
                "summary",
                "2026-10-23 10:00",
                [{"key": "tools", "label": "效率工具"}],
                data_dir=tmp,
            )
            stars = json.loads(Path(tmp, "stars.json").read_text(encoding="utf-8"))
            categories = json.loads(Path(tmp, "categories.json").read_text(encoding="utf-8"))
            self.assertEqual(stars["items"][0]["full_name"], "owner/repo")
            self.assertEqual(categories["labels"]["tools"], "效率工具")
            self.assertTrue(Path(tmp, "trending.json").exists())
            self.assertTrue(Path(tmp, "build-status.json").exists())


if __name__ == "__main__":
    unittest.main()
