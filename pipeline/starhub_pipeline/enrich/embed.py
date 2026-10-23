"""Optional semantic vectors for the star collection.

Embeddings are best-effort: without a provider key the pipeline keeps working
and the frontend falls back to keyword search.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any, Iterable

_ENDPOINT = "https://api.siliconflow.cn/v1/embeddings"
_MODEL = "BAAI/bge-m3"


def embed_texts(texts: Iterable[str], api_key: str | None = None) -> list[list[float]] | None:
    key = (api_key if api_key is not None else os.environ.get("SILICONFLOW_API_KEY", "")).strip()
    texts = list(texts)
    if not key or not texts:
        return None
    payload = json.dumps({"model": _MODEL, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        _ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
        items = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item.get("embedding") for item in items]
        if len(vectors) != len(texts) or any(not isinstance(v, list) or not v for v in vectors):
            print(f"[embed] shape mismatch: {len(vectors)} vs {len(texts)}", file=sys.stderr)
            return None
        return vectors
    except Exception as exc:  # noqa: BLE001
        print(f"[embed] request failed: {exc}", file=sys.stderr)
        return None


def quantize_normalized(vector: list[float]) -> list[int]:
    norm = (sum(value * value for value in vector) ** 0.5) or 1.0
    return [max(-127, min(127, round(value / norm * 127))) for value in vector]


def embed_star_entries(
    entries: list[dict[str, Any]],
    fast: bool = False,
    batch: int = 64,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    if fast or not entries:
        return entries
    try:
        texts = [(entry.get("desc") or entry.get("full_name") or "")[:512] for entry in entries]
        vectors: list[list[float] | None] = [None] * len(entries)
        for index in range(0, len(texts), batch):
            part = embed_texts(texts[index:index + batch], api_key=api_key)
            if not part:
                print(f"[embed] batch {index // batch + 1} failed; keeping keyword fallback", file=sys.stderr)
                return entries
            vectors[index:index + batch] = part
        for entry, vector in zip(entries, vectors):
            entry["emb"] = quantize_normalized(vector)
        print(f"[embed] {len(entries)} vectors inlined")
    except Exception as exc:  # noqa: BLE001
        print(f"[embed] skipped: {exc}", file=sys.stderr)
    return entries
