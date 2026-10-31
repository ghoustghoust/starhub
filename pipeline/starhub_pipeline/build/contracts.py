"""Atomic writers for the site data contracts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def atomic_json_write(path: str, payload: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    os.replace(tmp, path)


def contract_item(item: dict[str, Any]) -> dict[str, Any]:
    health = item.get("health")
    entry = {
        "id": item.get("full_name"),
        "full_name": item.get("full_name"),
        "html_url": item.get("html_url"),
        "description": item.get("desc") or "",
        "language": item.get("language"),
        "stars": int(item.get("stars") or 0),
        "forks": int(item.get("forks") or 0),
        "license": item.get("license") or "",
        "topics": item.get("topics") or [],
        "pushed_at": item.get("pushed_at") or "",
        "starred_at": item.get("starred_at") or "",
        "category": item.get("category") or "",
        "category_label": item.get("categoryLabel") or item.get("category") or "",
        "note": item.get("note") or "",
        "health": {
            "level": health if isinstance(health, str) else "unknown",
            "stale": bool(item.get("stale")),
        },
        "updated_today": bool(item.get("updated_today")),
    }
    if item.get("emb"):
        entry["embedding"] = item["emb"]
    return entry


def write_pool_script(items: list[dict[str, Any]], updated: str, data_dir: str = "data") -> None:
    """Write the frontend pool as a versioned external script.

    Keeping the collection out of index.html makes the document size independent
    of the number of stars; the script is cacheable and can be prefetched early.
    """
    path = os.path.join(data_dir, "pool.js")
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    payload = {"schema_version": 2, "updated": updated, "items": items}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = body.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("window.__STARHUB_POOL_DATA__=")
        handle.write(body)
        handle.write(";\n")
    os.replace(tmp, path)


def write_site_contracts(
    items: list[dict[str, Any]],
    known: dict[str, str],
    trending: dict[str, Any] | None,
    summary_text: str | None,
    updated: str,
    cats: list[dict[str, str]],
    data_dir: str = "data",
) -> None:
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    boards = trending or {}
    atomic_json_write(os.path.join(data_dir, "stars.json"), {
        "schema_version": 2,
        "generated_at": generated_at,
        "items": [contract_item(item) for item in items],
    })
    atomic_json_write(os.path.join(data_dir, "categories.json"), {
        "schema_version": 2,
        "updated_at": generated_at,
        "map": known,
        "labels": {category["key"]: category["label"] for category in cats},
    })
    atomic_json_write(os.path.join(data_dir, "trending.json"), {
        "schema_version": 2,
        "generated_at": generated_at,
        "updated": updated,
        "summary": {"text": summary_text or ""},
        "boards": {
            "rising": boards.get("rising", []),
            "total": boards.get("total", []),
            "new": boards.get("new", []),
            "downloads": boards.get("downloads", []),
        },
    })
    atomic_json_write(os.path.join(data_dir, "build-status.json"), {
        "schema_version": 2,
        "last_success_at": generated_at,
        "sources": {
            "github": {"status": "ok", "item_count": len(items)},
            "ai_daily": {"status": "unknown"},
        },
    })
