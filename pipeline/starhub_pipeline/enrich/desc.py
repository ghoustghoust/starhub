"""Description normalization for trending entries."""

from __future__ import annotations

from .translate import has_cn, translate_to_zh


def desc_zh(full_name: str, description: str | None, cache: dict[str, str], fast: bool = False) -> str:
    if not description:
        return ""
    if cache.get(full_name):
        return cache[full_name]
    if has_cn(description):
        cache[full_name] = description
        return description
    if fast:
        return description
    translated = translate_to_zh(description)
    if translated:
        cache[full_name] = translated
        return translated
    return description
