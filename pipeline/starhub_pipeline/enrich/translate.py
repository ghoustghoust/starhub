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

from ..sources import netguard
from . import llm

_FAIL_STREAK = 0
_BLOCK_UNTIL = 0.0
_FAIL_LIMIT = 5
_COOLDOWN_SECONDS = 300

# 翻译端点出网护栏：主机白名单 + 公网解析校验 + 钉住通道（共享 netguard）
_HOSTS = frozenset({"translate.googleapis.com", "api.mymemory.translated.net"})
_SAFE_OPENER = netguard.build_safe_opener(_HOSTS)


def _safe_fetch(request, timeout=10):
    """免费端点出网的唯一出口：先校验 URL，再走钉住通道。"""
    netguard.assert_safe_url(request.full_url, _HOSTS)
    return _SAFE_OPENER.open(request, timeout=timeout)


def _translate_via_llm(text: str) -> str | None:
    """Agnes LLM 翻译。免费端点会被数据中心 IP 封锁（429/timeout），LLM 是 CI 的主通道；
    本地构建它同样优先（质量最好）。限流/偶发失败按 2s/4s/6s 退避重试，请求间留间隔
    防止按分钟计的配额被连发打穿。失败返回 None。"""
    result = None
    for attempt in range(3):
        time.sleep(2 * (attempt + 1))
        result = llm.chat(
            [
                {"role": "system",
                 "content": "你是翻译引擎。把用户输入的英文翻译成简体中文，只输出译文，不要解释。"},
                {"role": "user", "content": text[:1500]},
            ],
            temperature=0.2,
            max_tokens=400,
        )
        if result:
            return result
    return result


def has_cn(text: str | None) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in (text or ""))


def reset_circuit() -> None:
    global _FAIL_STREAK, _BLOCK_UNTIL
    _FAIL_STREAK = 0
    _BLOCK_UNTIL = 0.0


def _record_failure() -> None:
    """全链失败计数：连败 _FAIL_LIMIT 次开冷却，避免对封锁中的端点逐仓硬闯。"""
    global _FAIL_STREAK, _BLOCK_UNTIL
    _FAIL_STREAK += 1
    if _FAIL_STREAK >= _FAIL_LIMIT:
        _BLOCK_UNTIL = time.time() + _COOLDOWN_SECONDS
        print(f"[translate] circuit open after {_FAIL_STREAK} failures", file=sys.stderr)


def _via_gtx(text: str) -> str | None:
    """Google gtx 免费端点。数据中心 IP 已知封锁（429/timeout），只给本地构建用。"""
    params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
    request = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + params,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with _safe_fetch(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = "".join(segment[0] for segment in payload[0] if segment[0]).strip()
        if result and has_cn(result):
            return result
    except Exception:  # noqa: BLE001
        pass
    return None


def _via_mymemory(text: str) -> str | None:
    """MyMemory 免费端点。质量一般但按量配额，做 LLM 之后的兜底。"""
    params = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
    request = urllib.request.Request(
        "https://api.mymemory.translated.net/get?" + params,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with _safe_fetch(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = (payload.get("responseData", {}).get("translatedText") or "").strip()
        if result and has_cn(result) and "MYMEMORY WARNING" not in result:
            return result
        print("[translate] MyMemory 未产出中文: %r" % (result[:60],), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print("[translate] MyMemory 失败: %s" % exc, file=sys.stderr)
    return None


def translate_to_zh(text: str | None, fast: bool = False) -> str | None:
    """英译中；全部端点失败返回 None（调用方保留原文）。
    端点链：Agnes LLM → MyMemory →（仅非 fast）Google gtx。
    fast=True 是 CI 快速模式：跳过已被数据中心 IP 封锁的 gtx，LLM 失败仍有
    MyMemory 兜底；本地构建走全链。"""
    global _FAIL_STREAK, _BLOCK_UNTIL
    if not text or time.time() < _BLOCK_UNTIL:
        return None

    # ── 端点 0：Agnes LLM（CI 与本地共同的主通道）──
    if llm.key_pool():
        cand = _translate_via_llm(text)
        if cand and has_cn(cand):
            _FAIL_STREAK = 0
            return cand
        # 三条静默失败（429/空响应/没翻出中文）的现场，配合 llm 的 429/空响应日志定位
        print("[translate] LLM 未产出中文: %r" % ((cand or "")[:60]), file=sys.stderr)

    # ── 端点 1：MyMemory（fast 模式的兜底；可达性由 CI 日志留证）──
    cand = _via_mymemory(text)
    if cand:
        _FAIL_STREAK = 0
        return cand

    if fast:
        # 快速模式到此为止：不撞已知被封锁的 gtx
        _record_failure()
        return None

    # ── 端点 2：Google gtx（仅本地构建）──
    cand = _via_gtx(text)
    if cand:
        _FAIL_STREAK = 0
        return cand
    _record_failure()
    return None
