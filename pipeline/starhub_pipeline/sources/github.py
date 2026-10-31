"""GitHub star collection.

The adapter only fetches and normalizes; classification/translation belong to
the enrichment layer.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import site_value
from . import netguard

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
            with _safe_fetch(request, _TIMEOUT_SECONDS) as response:
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


def search_repos(query: str, token: str | None, per_page: int = 100, sort: str = "stars") -> list[dict[str, Any]]:
    url = (
        "https://api.github.com/search/repositories?q=%s&sort=%s&order=desc&per_page=%d"
        % (urllib.parse.quote(query), sort, per_page)
    )
    request = urllib.request.Request(url, headers=api_headers(token))
    with _safe_fetch(request, 30) as response:
        return json.loads(response.read().decode("utf-8")).get("items", [])


def pick(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "owner": (item.get("full_name") or "").split("/")[0],
        "full_name": item.get("full_name"),
        "html_url": item.get("html_url"),
        "desc": " ".join((item.get("description") or "").split()),
        "language": item.get("language"),
        "stars": item.get("stargazers_count"),
        "created_at": (item.get("created_at") or "")[:10],
    }


def fetch_ai_pool(token: str | None, topics: list[str], min_stars: int) -> list[dict[str, Any]]:
    pool: dict[str, dict[str, Any]] = {}
    for topic in topics:
        try:
            for item in search_repos("topic:%s stars:>%d" % (topic, min_stars), token):
                full_name = item.get("full_name")
                if full_name and full_name not in pool:
                    pool[full_name] = pick(item)
        except Exception as exc:  # noqa: BLE001
            print("[AI池 %s 失败] %s" % (topic, exc), file=sys.stderr)
        time.sleep(1)
    return list(pool.values())


def fetch_new_repos(token: str | None, min_stars: int, top: int) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    result = []
    try:
        for item in search_repos("topic:ai created:>%s stars:>%d" % (since, min_stars), token, per_page=top):
            result.append(pick(item))
    except Exception as exc:  # noqa: BLE001
        print("[新秀榜失败] %s" % exc, file=sys.stderr)
    return result


RELEASES_MAX_PAGES = 5  # 单仓库最多翻的 releases 页数（每页 100 条，控制 API 预算）


# 下载量通道的出网护栏走共享的 netguard：主机钉死 api.github.com，解析地址全公网、
# 重定向逐跳复检、校验过的 IP 直连（防 DNS rebinding）。
_API_HOST = "api.github.com"
_SAFE_OPENER = netguard.build_safe_opener({_API_HOST})


def _assert_safe_url(url: str) -> None:
    netguard.assert_safe_url(url, {_API_HOST})


def _safe_fetch(request, timeout):
    """本模块所有出网的唯一出口：白名单校验后走钉住通道。"""
    _assert_safe_url(request.full_url)
    return _SAFE_OPENER.open(request, timeout=timeout)


class _RateLimited(Exception):
    """GitHub 对 releases 接口限流（403/429）；扫描应整体收手而不是逐个硬闯。"""


def fetch_release_downloads(full_name: str, token: str | None = None,
                            max_pages: int = RELEASES_MAX_PAGES) -> int | None:
    """Sum release asset download counts; None when the fetch fails.

    GitHub 只在 Release 资产上公开下载量，这是仓库下载量唯一可用的官方口径；
    没有 release 的仓库返回 0，由调用方决定要不要展示。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name or ""):
        return None
    total = 0
    for page in range(1, max_pages + 1):
        url = f"{_API_ROOT}/repos/{full_name}/releases?per_page=100&page={page}"
        _assert_safe_url(url)
        request = urllib.request.Request(url, headers=api_headers(token))
        try:
            with _SAFE_OPENER.open(request, timeout=_TIMEOUT_SECONDS) as response:
                releases = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise _RateLimited(str(exc)) from exc
            print("[下载量 %s 失败] %s" % (full_name, exc), file=sys.stderr)
            return None
        except Exception as exc:  # noqa: BLE001
            print("[下载量 %s 失败] %s" % (full_name, exc), file=sys.stderr)
            return None
        if not releases:
            break
        for release in releases:
            for asset in release.get("assets") or []:
                total += asset.get("download_count") or 0
        if len(releases) < 100:
            break
        time.sleep(_PAGE_DELAY_SECONDS)
    return total


def fetch_download_totals(token: str | None, full_names: list[str]) -> dict[str, int]:
    """Best-effort download totals for a candidate list; failed repos are skipped."""
    totals: dict[str, int] = {}
    try:
        for index, full_name in enumerate(full_names):
            total = fetch_release_downloads(full_name, token)
            if total is None:
                continue
            totals[full_name] = total
            if index < len(full_names) - 1:
                time.sleep(_PAGE_DELAY_SECONDS)
    except _RateLimited as exc:
        print("[下载量] 限流收手：%s（已拿到 %d 个仓库的量）" % (exc, len(totals)), file=sys.stderr)
    return totals


def readme_first_sentence(text: str) -> str | None:
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        if re.match(r"^#{1,6}\s", value) or value.startswith(("```", "~~~")) or value.startswith(">"):
            continue
        value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
        value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
        value = re.sub(r"<[^>]+>", "", value)
        value = re.sub(r"`[^`]*`", "", value)
        value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
        value = re.sub(r"\*([^*]+)\*", r"\1", value)
        value = re.sub(r"^[\s\-*+]+", "", value).strip()
        value = re.sub(r"\s+", " ", value)
        low = value.lower()
        if any(k in low for k in ("img.shields.io", "badge", "build passing", "build status",
                                  "license", "contributors", "中文", "english", "stars", "downloads")):
            continue
        if len(value) >= 10:
            if len(value) > 80:
                for separator in (". ", "。", "！", "? "):
                    index = value.find(separator)
                    if 10 < index <= 150:
                        return value[:index].strip()
            if len(value) > 150:
                cut = value[:150]
                for separator in (". ", "。", "，", ", "):
                    index = cut.rfind(separator)
                    if index > 30:
                        return cut[:index].strip()
                return cut.strip() + "…"
            return value
    return None


def fetch_readme_summary(full_name: str, token: str | None) -> str | None:
    url = "https://api.github.com/repos/%s/readme" % full_name
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "starhub-auto-update"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        request = urllib.request.Request(url, headers=headers)
        with _safe_fetch(request, 20) as response:
            raw = response.read(30000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    summary = readme_first_sentence(raw)
    if summary:
        time.sleep(1)
    return summary


TREND_LANG_PAGES = ["", "python", "typescript", "javascript", "rust", "go", "java",
                    "c++", "c", "swift", "kotlin", "ruby", "php", "c#", "shell",
                    "jupyter-notebook", "vue", "dart", "elixir", "haskell"]

_AI_RE = re.compile(
    r"\b(ai|ml|llm|llms|gpt|nlp|rag|agent|agents|agentic|llama|claude|openai|anthropic|"
    r"gemini|copilot|diffusion|neural|transformer|chatbot|assistant|embedding|inference|"
    r"genai|generative|vision|speech|voice|machine.?learning|deep.?learning|model|models)\b")


def is_ai_repo(full_name: str, description: str | None) -> bool:
    return bool(_AI_RE.search((full_name + " " + (description or "")).lower()))


def parse_trending(html: str) -> list[dict[str, Any]]:
    result = []
    for block in re.findall(r'<article[^>]*class="[^"]*Box-row[^"]*"[\s\S]*?</article>', html):
        match = re.search(r'<h2[^>]*>[\s\S]*?href="/([^"/]+/[^"/]+)"', block)
        if not match:
            continue
        full_name = match.group(1)
        if full_name.startswith("sponsors/"):
            continue
        stars_today = re.search(r"([\d,]+)\s+stars?\s+today", block)
        language = re.search(r'itemprop="programmingLanguage"[^>]*>([^<]+)<', block)
        description = re.search(r"<h2[\s\S]*?</h2>[\s\S]*?<p[^>]*>([\s\S]*?)</p>", block)
        total = None
        stars_index = block.find("stargazers")
        if stars_index != -1:
            end = block.find("</a>", stars_index)
            if end != -1:
                stars_match = re.search(r"</svg>\s*([\d,]+)", block[stars_index:end])
                if stars_match:
                    total = stars_match.group(1)
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", description.group(1))).strip() if description else ""
        owner, name = full_name.split("/", 1)
        result.append({
            "name": name,
            "owner": owner,
            "full_name": full_name,
            "html_url": "https://github.com/" + full_name,
            "desc": desc,
            "language": language.group(1) if language else None,
            "stars": int(total.replace(",", "")) if total else 0,
            "stars_today": int(stars_today.group(1).replace(",", "")) if stars_today else 0,
        })
    return result


def fetch_trending_daily(token: str | None = None) -> list[dict[str, Any]] | None:
    pool: dict[str, dict[str, Any]] = {}
    for page in TREND_LANG_PAGES:
        url = "https://github.com/trending/%s?since=daily" % page
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                              " (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            })
            with _safe_fetch(request, 20) as response:
                html = response.read().decode("utf-8", errors="replace")
            for item in parse_trending(html):
                if item["full_name"] not in pool:
                    pool[item["full_name"]] = item
        except Exception as exc:  # noqa: BLE001
            print("[Trending %s 失败] %s" % (page or "全部", exc), file=sys.stderr)
        time.sleep(0.5)
    return list(pool.values()) if pool else None


def today_cn() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def cn_dt(utc_str: str | None) -> datetime | None:
    if not utc_str:
        return None
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
    except Exception:  # noqa: BLE001
        return None


def cn_date(utc_str: str | None) -> str:
    value = cn_dt(utc_str)
    return value.strftime("%Y-%m-%d") if value else ((utc_str or "")[:10])


def cn_time(utc_str: str | None) -> str:
    value = cn_dt(utc_str)
    return value.strftime("%H:%M") if value else ((utc_str or "")[11:16])


def fetch_following(token: str | None, username: str | None = None) -> list[str]:
    user = username or site_value("github_user")
    result = []
    page = 1
    while True:
        url = "https://api.github.com/users/%s/following?per_page=100&page=%d" % (user, page)
        request = urllib.request.Request(url, headers=api_headers(token))
        try:
            with _safe_fetch(request, 30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            print("[关注列表失败] %s" % exc, file=sys.stderr)
            break
        if not data:
            break
        result.extend(item.get("login") for item in data if item.get("login"))
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.5)
    return result


def fetch_following_events(token: str | None, username: str | None = None) -> list[dict[str, Any]]:
    now_cn = datetime.now(timezone(timedelta(hours=8)))
    cutoff = now_cn - timedelta(hours=24)
    today = now_cn.strftime("%Y-%m-%d")
    following = fetch_following(token, username)
    feed = []
    for user in following:
        url = "https://api.github.com/users/%s/events/public?per_page=100&page=%%d" % user
        for page in (1, 2):
            request = urllib.request.Request(url % page, headers=api_headers(token))
            try:
                with _safe_fetch(request, 30) as response:
                    events = json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                print("[事件拉取失败 %s] %s" % (user, exc), file=sys.stderr)
                break
            if not events:
                break
            for event in events:
                dt = cn_dt(event.get("created_at"))
                if dt is None or dt < cutoff:
                    continue
                date = dt.strftime("%Y-%m-%d")
                event_type = event.get("type")
                payload = event.get("payload") or {}
                actor = (event.get("actor") or {}).get("login", "")
                repo = (event.get("repo") or {}).get("name", "")
                tm = dt.strftime("%H:%M")
                day = "今天" if date == today else "昨天"
                item = None
                if event_type == "CreateEvent" and payload.get("ref_type") == "repository":
                    item = {"kind": "repo", "actor": actor, "repo": repo, "time": tm, "day": day, "date": date,
                            "url": "https://github.com/" + repo}
                elif event_type == "WatchEvent" and payload.get("action") == "started":
                    item = {"kind": "star", "actor": actor, "repo": repo, "time": tm, "day": day, "date": date,
                            "url": "https://github.com/" + repo}
                elif event_type == "FollowEvent":
                    target = (payload.get("target") or {}).get("login", "")
                    item = {"kind": "follow", "actor": actor, "target": target, "time": tm, "day": day, "date": date,
                            "url": "https://github.com/" + target}
                elif event_type == "PullRequestEvent" and payload.get("action") == "opened":
                    pr = payload.get("pull_request") or {}
                    item = {"kind": "pr", "actor": actor, "repo": repo,
                            "title": (pr.get("title") or "")[:60],
                            "url": pr.get("html_url", "https://github.com/" + repo),
                            "time": tm, "day": day, "date": date}
                elif event_type == "ReleaseEvent" and payload.get("action") == "published":
                    release = payload.get("release") or {}
                    item = {"kind": "release", "actor": actor, "repo": repo,
                            "tag": release.get("tag_name", ""),
                            "url": release.get("html_url", "https://github.com/" + repo + "/releases"),
                            "time": tm, "day": day, "date": date}
                elif event_type == "PublicEvent":
                    item = {"kind": "public", "actor": actor, "repo": repo,
                            "url": "https://github.com/" + repo,
                            "time": tm, "day": day, "date": date}
                elif event_type == "PushEvent":
                    size = payload.get("size", 0)
                    if size > 0:
                        item = {"kind": "push", "actor": actor, "repo": repo, "size": size,
                                "url": "https://github.com/" + repo + "/commits",
                                "time": tm, "day": day, "date": date}
                if item:
                    feed.append(item)
            last_dt = cn_dt((events[-1].get("created_at") or ""))
            if last_dt is None or last_dt < cutoff:
                break
            time.sleep(1)
        time.sleep(0.5)
    feed.sort(key=lambda item: (item.get("date", ""), item.get("time", "")), reverse=True)
    return feed
