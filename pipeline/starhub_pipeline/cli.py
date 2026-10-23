"""Command line entry points for the StarHub pipeline."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ROOT, load_site_config
from .state import validate_files

_CONTRACTS = {
    "stars": "data/stars.json",
    "categories": "data/categories.json",
    "trending": "data/trending.json",
}


def _validate() -> int:
    load_site_config()
    files = [(name, ROOT / path) for name, path in _CONTRACTS.items()]
    errors = validate_files(files)
    if errors:
        for error in errors:
            print(f"[contract] {error}", file=sys.stderr)
        return 1
    print("[contract] data contracts OK")
    return 0


def _update() -> int:
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    import fetch_and_build  # noqa: PLC0415

    fetch_and_build.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="starhub-pipeline")
    parser.add_argument("command", choices=("update", "validate"))
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate()
    return _update()


if __name__ == "__main__":
    raise SystemExit(main())
