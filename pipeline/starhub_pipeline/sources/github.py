"""GitHub star collection.

The adapter only fetches and normalizes; classification/translation belong to
the enrichment layer.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import site_value

_API_ROOT = "https://api.github.com"
_TIMEOUT_SECONDS = 30
_PAGE_DELAY_SECONDS = 0.5


def api_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "starhub-pipeline",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def normalize_star_item(item: dict[str, Any]) -> dict[str, Any]:
    """Accept both the star+json wrapper and the legacy bare repository shape."""
    if isinstance(item.get("repo"), dict):
        repo = dict(item["repo"])
        repo["starred_at"] = item.get("starred_at") or ""
        return repo
    repo = dict(item)
    repo.setdefault("starred_at", "")
    return repo


def fetch_stars(token: str | None = None, username: str | None = None) -> list[dict[str, Any]] | None:
    """Fetch every starred repository; return None on a fetch failure."""
    user = username or site_value("github_user")
    if not user:
        raise ValueError("github_user is not configured")

    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{_API_ROOT}/users/{user}/starred?per_page=100&page={page}"
        headers = api_headers(token)
        headers["Accept"] = "application/vnd.github.star+json"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[github] star fetch failed: {exc}", file=sys.stderr)
            return None
        if not payload:
            break
        repos.extend(normalize_star_item(item) for item in payload if isinstance(item, dict))
        if len(payload) < 100:
            break
        page += 1
        time.sleep(_PAGE_DELAY_SECONDS)
    return repos
