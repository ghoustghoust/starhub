"""Atomic JSON state and minimal contract checks.

Contracts are intentionally validated without third-party dependencies so the
same checks work in GitHub Actions and on a clean Windows machine.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def atomic_write_json(path: str | Path, payload: Any, indent: int = 1) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=indent) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)


def validate_contract(name: str, payload: Any) -> list[str]:
    """Return contract errors; an empty list means the payload is usable."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [f"{name}: payload is not an object"]
    if payload.get("schema_version") != 2:
        errors.append(f"{name}: schema_version must be 2")
    if name in {"stars", "events", "ai-feed"}:
        items = payload.get("items")
        if not isinstance(items, list):
            errors.append(f"{name}: items must be a list")
        else:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(f"{name}.items[{index}]: not an object")
                    continue
                if name == "stars" and not item.get("full_name"):
                    errors.append(f"{name}.items[{index}]: missing full_name")
                if name != "stars" and not item.get("url"):
                    errors.append(f"{name}.items[{index}]: missing url")
    if name == "categories":
        if not isinstance(payload.get("map"), dict):
            errors.append("categories: map must be an object")
        if not isinstance(payload.get("labels"), dict):
            errors.append("categories: labels must be an object")
    if name == "trending":
        boards = payload.get("boards")
        if not isinstance(boards, dict):
            errors.append("trending: boards must be an object")
        else:
            for board in ("rising", "total", "new"):
                if not isinstance(boards.get(board), list):
                    errors.append(f"trending: boards.{board} must be a list")
    if name == "ai-daily":
        report = payload.get("report")
        if not isinstance(report, dict):
            errors.append("ai-daily: report must be an object")
        elif not isinstance(report.get("sections"), list):
            errors.append("ai-daily: report.sections must be a list")
    return errors


def validate_files(files: Iterable[tuple[str, str | Path]]) -> list[str]:
    errors: list[str] = []
    for name, path in files:
        payload = read_json(path)
        if payload is None:
            errors.append(f"{name}: missing {path}")
            continue
        errors.extend(validate_contract(name, payload))
    return errors
