from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starhub_pipeline.config import load_site_config
from starhub_pipeline.state import atomic_write_json, read_json, validate_contract, validate_files


class ConfigTest(unittest.TestCase):
    def test_loads_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "site.json"
            path.write_text(
                json.dumps(
                    {
                        "site": {
                            "name": "StarHub",
                            "owner": "owner",
                            "github_user": "owner",
                            "repository": "owner/repo",
                            "pages_url": "https://example.com/",
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_site_config(path)["site"]["owner"], "owner")

    def test_rejects_missing_site_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "site.json"
            path.write_text('{"site": {"name": "StarHub"}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_site_config(path)


class StateTest(unittest.TestCase):
    def test_atomic_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            atomic_write_json(path, {"hello": "世界"})
            self.assertEqual(read_json(path), {"hello": "世界"})
            self.assertFalse(path.with_name("state.json.tmp").exists())

    def test_stars_contract(self) -> None:
        payload = {
            "schema_version": 2,
            "generated_at": "2026-10-23T00:00:00Z",
            "items": [{"full_name": "owner/repo", "html_url": "https://github.com/owner/repo"}],
        }
        self.assertEqual(validate_contract("stars", payload), [])
        broken = {**payload, "schema_version": 1}
        self.assertTrue(validate_contract("stars", broken))

    def test_categories_and_trending(self) -> None:
        categories = {"schema_version": 2, "map": {}, "labels": {}}
        self.assertEqual(validate_contract("categories", categories), [])
        trending = {
            "schema_version": 2,
            "boards": {"rising": [], "total": [], "new": []},
        }
        self.assertEqual(validate_contract("trending", trending), [])

    def test_missing_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            errors = validate_files([("stars", Path(tmp) / "missing.json")])
            self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
