"""Site configuration loading.

The repository root stays the working directory for legacy build scripts;
this module only provides a typed view of config/site.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "site.json"

_REQUIRED_SITE_KEYS = ("name", "owner", "github_user", "repository", "pages_url")


def load_site_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else CONFIG_PATH
    data = json.loads(config_path.read_text(encoding="utf-8"))
    site = data.get("site")
    if not isinstance(site, dict):
        raise ValueError(f"{config_path}: missing site object")
    missing = [key for key in _REQUIRED_SITE_KEYS if not site.get(key)]
    if missing:
        raise ValueError(f"{config_path}: missing site keys: {', '.join(missing)}")
    return data


def site_value(key: str, default: Any = None, path: str | Path | None = None) -> Any:
    return load_site_config(path).get("site", {}).get(key, default)
