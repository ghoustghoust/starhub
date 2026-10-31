from __future__ import annotations

import json
import unittest
import urllib.error
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
    def _opener(self, payload=None, error=None):
        opener = mock.MagicMock()
        if error is not None:
            opener.open.side_effect = error
        else:
            opener.open.return_value = _Response(payload)
        return opener

    def test_fetches_and_normalizes(self) -> None:
        payload = [{"starred_at": "2026-10-20T00:00:00Z", "repo": {"full_name": "owner/repo"}}]
        with mock.patch.object(github, "_assert_safe_url"), \
             mock.patch.object(github, "_SAFE_OPENER", self._opener(payload)):
            repos = github.fetch_stars(token="token", username="owner")
        self.assertEqual(repos[0]["full_name"], "owner/repo")
        self.assertEqual(repos[0]["starred_at"], "2026-10-20T00:00:00Z")

    def test_failure_returns_none(self) -> None:
        with mock.patch.object(github, "_assert_safe_url"), \
             mock.patch.object(github, "_SAFE_OPENER",
                               self._opener(error=RuntimeError("network down"))):
            self.assertIsNone(github.fetch_stars(token="token", username="owner"))


class ReleaseDownloadsTest(unittest.TestCase):
    def _opener_with(self, payloads):
        opener = mock.MagicMock()
        opener.open.side_effect = [_Response(payload) for payload in payloads]
        return opener

    def test_sums_assets_across_pages(self) -> None:
        page1 = [{"assets": [{"download_count": 1}]} for _ in range(100)]
        page2 = [{"assets": [{"download_count": 5}, {}]}]
        opener = self._opener_with([page1, page2])
        with mock.patch.object(github, "_SAFE_OPENER", opener), \
             mock.patch.object(github, "_assert_safe_url"), \
             mock.patch.object(github.time, "sleep"):
            total = github.fetch_release_downloads("owner/repo", token="token")
        self.assertEqual(total, 105)
        self.assertEqual(opener.open.call_count, 2)

    def test_invalid_name_short_circuits(self) -> None:
        opener = mock.MagicMock()
        with mock.patch.object(github, "_SAFE_OPENER", opener):
            self.assertIsNone(github.fetch_release_downloads("bad name/../x"))
        opener.open.assert_not_called()

    def test_rate_limit_stops_scan_with_partial_totals(self) -> None:
        opener = mock.MagicMock()
        opener.open.side_effect = [
            _Response([{"assets": [{"download_count": 7}]}]),
            urllib.error.HTTPError("https://api.github.com/repos/b/two/releases", 403, "rate limit", None, None),
        ]
        with mock.patch.object(github, "_SAFE_OPENER", opener), \
             mock.patch.object(github, "_assert_safe_url"), \
             mock.patch.object(github.time, "sleep"):
            totals = github.fetch_download_totals("token", ["a/one", "b/two", "c/three"])
        self.assertEqual(totals, {"a/one": 7})

    def test_fetch_failure_skips_repo(self) -> None:
        opener = mock.MagicMock()
        opener.open.side_effect = RuntimeError("network down")
        with mock.patch.object(github, "_SAFE_OPENER", opener), \
             mock.patch.object(github, "_assert_safe_url"):
            self.assertIsNone(github.fetch_release_downloads("a/one", token="token"))

    def test_url_guard_rejects_non_github_hosts(self) -> None:
        for url in ("http://api.github.com/repos/a/b", "https://127.0.0.1/x",
                    "https://api.github.com.evil.com/x", "ftp://api.github.com/x"):
            with self.assertRaises(ValueError, msg=url):
                github._assert_safe_url(url)


if __name__ == "__main__":
    unittest.main()
