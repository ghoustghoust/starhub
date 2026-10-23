"""Trending board construction and payload."""

from __future__ import annotations

import json
import sys

from ..enrich.desc import desc_zh
from ..sources.github import fetch_ai_pool, fetch_new_repos, fetch_trending_daily, is_ai_repo

TRENDING_BOARD = "trending_board.json"


def _fmt_zh(number: int) -> str:
    if number >= 10000:
        return "%.1f万" % (number / 10000)
    return str(number)


def build_trending(
    token,
    descriptions: dict[str, str],
    *,
    ai_topics: list[str],
    ai_min_stars: int,
    new_min_stars: int,
    trend_top: int,
    trend_max_stars: int,
    fast: bool = False,
    snapshot_path: str = "trending_snapshot.json",
) -> dict:
    snapshot = {}
    try:
        snapshot = json.load(open(snapshot_path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    pool = fetch_ai_pool(token, ai_topics, ai_min_stars)
    total = sorted(pool, key=lambda item: item["stars"], reverse=True)[:trend_top]

    rising = []
    trend_rows = fetch_trending_daily(token)
    if trend_rows:
        rising = [item for item in trend_rows if is_ai_repo(item["full_name"], item["desc"])]
        rising.sort(key=lambda item: item["stars_today"], reverse=True)
        rising = rising[:trend_top]
        for item in rising:
            item["delta"] = item["stars_today"]
    else:
        print("[涨星榜] Trending 抓取失败，降级为快照差值模式", file=sys.stderr)
        for item in pool:
            previous = snapshot.get(item["full_name"])
            if previous is not None and item["stars"] <= trend_max_stars:
                item["delta"] = item["stars"] - previous
                rising.append(item)
        rising.sort(key=lambda item: item["delta"], reverse=True)
        rising = rising[:trend_top]

    if not rising:
        rising = [dict(item, delta=None) for item in total]

    new_repos = fetch_new_repos(token, new_min_stars, trend_top)

    for item in rising + total + new_repos:
        item["desc"] = desc_zh(item["full_name"], item["desc"], descriptions, fast=fast)

    for item in rising:
        if item.get("delta") is not None:
            item["reason"] = ("今日涨星 +%d" % item["delta"]) if item["delta"] >= 0 else ("今日涨星 %d" % item["delta"])
        else:
            item["reason"] = "新上榜 · %s星标" % _fmt_zh(item["stars"])
    for item in total:
        item["reason"] = "累计 %s星标" % _fmt_zh(item["stars"])
    for item in new_repos:
        item["reason"] = "近 7 天新建 · %s星标" % _fmt_zh(item["stars"])

    if pool:
        with open(snapshot_path, "w", encoding="utf-8") as handle:
            json.dump({item["full_name"]: item["stars"] for item in pool}, handle, ensure_ascii=False, indent=1)
    else:
        print("[警告] AI 池为空，跳过快照更新，保留旧基线", file=sys.stderr)

    return {"rising": rising, "total": total, "new": new_repos,
            "source": "trending" if trend_rows else "snapshot"}


def trending_board_payload(trending, ai_summary_html, updated):
    return {
        "trending": trending or {},
        "ai_summary_html": ai_summary_html or "",
        "updated": updated or "",
    }
