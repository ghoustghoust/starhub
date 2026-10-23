"""Render the main page from the template and assembled data."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import site_value
from ..taxonomy import DEFAULT_FAVS, LANG_COLORS

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = ROOT / "template.html"


def safe_json(payload) -> str:
    # Keep inline data from closing the surrounding <script> tag.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def build_index_html(
    cats,
    trending=None,
    feed=None,
    updated: str | None = None,
    ai_summary_html: str = "",
    attention_html: str = "",
    template_path: str | Path | None = None,
    api_base: str | None = None,
) -> str:
    path = Path(template_path) if template_path else TEMPLATE_PATH
    template = path.read_text(encoding="utf-8")
    if updated is None:
        updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    asset_version = re.sub(r"\D", "", updated) or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if api_base is None:
        api_base = site_value("api_base", "")
    return (
        template.replace("__CATS__", safe_json(cats))
        .replace("__LANGS__", safe_json(LANG_COLORS))
        .replace("__FAVS__", safe_json(DEFAULT_FAVS))
        .replace("__API_BASE__", api_base)
        .replace("__ASSET_VERSION__", asset_version)
        .replace("__TRENDING__", safe_json(trending or {}))
        .replace("__FEED__", safe_json(feed or []))
        .replace("__ATTENTION__", attention_html)
        .replace("__UPDATED__", updated)
        .replace("__AI_SUMMARY__", ai_summary_html or "")
    )
