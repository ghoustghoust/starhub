"""Minimal Agnes chat client shared by classification and summaries."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

API_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
MODEL = "agnes-2.5-flash"
TIMEOUT_SECONDS = 30
NOTE_MAX_CHARS = 30


def key_pool() -> list[str]:
    keys = [key.strip() for key in (os.environ.get("AGNES_API_KEY") or "").split(",") if key.strip()]
    keys += [key.strip() for key in (os.environ.get("AGNES_API_KEYS") or "").split(",") if key.strip()]
    pool, seen = [], set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            pool.append(key)
    return pool


def parse_json(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        text = match.group(1)
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        return None


def chat(messages: list[dict[str, str]], temperature: float = 0.4, max_tokens: int = 300) -> str | None:
    pool = key_pool()
    if not pool:
        return None
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = json.dumps(payload).encode("utf-8")
    for key in pool:
        request = urllib.request.Request(
            API_URL,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"] or ""
            return content.strip() or None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                continue
            print(f"[llm] HTTP {exc.code}", file=sys.stderr)
            return None
        except Exception as exc:  # noqa: BLE001
            print(f"[llm] request failed: {exc}", file=sys.stderr)
            return None
    return None


def generate(system: str, user: str, max_tokens: int = 300) -> str | None:
    return chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.4,
        max_tokens=max_tokens,
    )
