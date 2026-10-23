from __future__ import annotations

import json
import unittest
from unittest import mock

from starhub_pipeline.sources import github


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NormalizeTest(unittest.TestCase):
    def test_wrapped_item(self) -> None:
        repo = github.normalize_star_item(
            {"starred_at": "2026-10-20T00:00:00Z", "repo": {"full_name": "owner/repo"}}
        )
        self.assertEqual(repo["full_name"], "owner/repo")
        self.assertEqual(repo["starred_at"], "2026-10-20T00:00:00Z")

    def test_legacy_item(self) -> None:
        repo = github.normalize_star_item({"full_name": "owner/repo"})
        self.assertEqual(repo["starred_at"], "")


class FetchTest(unittest.TestCase):
    def test_fetches_and_normalizes(self) -> None:
        payload = [{"starred_at": "2026-10-20T00:00:00Z", "repo": {"full_name": "owner/repo"}}]
        with mock.patch("starhub_pipeline.sources.github.urllib.request.urlopen", return_value=_Response(payload)):
            repos = github.fetch_stars(token="token", username="owner")
        self.assertEqual(repos[0]["full_name"], "owner/repo")
        self.assertEqual(repos[0]["starred_at"], "2026-10-20T00:00:00Z")

    def test_failure_returns_none(self) -> None:
        with mock.patch(
            "starhub_pipeline.sources.github.urllib.request.urlopen",
            side_effect=RuntimeError("network down"),
        ):
            self.assertIsNone(github.fetch_stars(token="token", username="owner"))


if __name__ == "__main__":
    unittest.main()
