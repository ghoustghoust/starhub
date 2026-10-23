"""Lightweight Chinese translation with a circuit breaker.

The pipeline only needs short repository descriptions; free endpoints are used
best-effort and failures fall back to the original text.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

_FAIL_STREAK = 0
_BLOCK_UNTIL = 0.0
_FAIL_LIMIT = 5
_COOLDOWN_SECONDS = 300


def has_cn(text: str | None) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in (text or ""))


def reset_circuit() -> None:
    global _FAIL_STREAK, _BLOCK_UNTIL
    _FAIL_STREAK = 0
    _BLOCK_UNTIL = 0.0


def translate_to_zh(text: str | None, fast: bool = False) -> str | None:
    global _FAIL_STREAK, _BLOCK_UNTIL
    if fast or not text or time.time() < _BLOCK_UNTIL:
        return None

    params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + params,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = "".join(segment[0] for segment in payload[0] if segment[0]).strip()
        if result and has_cn(result):
            _FAIL_STREAK = 0
            return result
    except Exception:  # noqa: BLE001
        pass

    params = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
    request = urllib.request.Request(
        "https://api.mymemory.translated.net/get?" + params,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = (payload.get("responseData", {}).get("translatedText") or "").strip()
        if result and has_cn(result) and "MYMEMORY WARNING" not in result:
            _FAIL_STREAK = 0
            return result
    except Exception:  # noqa: BLE001
        pass

    _FAIL_STREAK += 1
    if _FAIL_STREAK >= _FAIL_LIMIT:
        _BLOCK_UNTIL = time.time() + _COOLDOWN_SECONDS
        print(f"[translate] circuit open after {_FAIL_STREAK} failures", file=sys.stderr)
    return None
