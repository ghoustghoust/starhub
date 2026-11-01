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
    # fast 只是不撞免费端点的长重试；有 key 时 LLM 翻译照常走（否则 CI 里新收藏永远没中文）
    translated = translate_to_zh(description, fast=fast)
    if translated:
        cache[full_name] = translated
        return translated
    return description
