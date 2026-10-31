"""AI summary and category guide enrichment."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import llm

ROOT = Path(__file__).resolve().parents[3]
GUIDES_CACHE_PATH = ROOT / "category_guides.json"


def generate_ai_summary(rising_top10, fast: bool = False) -> str | None:
    if fast:
        return None
    keys = llm.key_pool()
    if not keys:
        print("[AI摘要] 跳过: 未配置 AGNES_API_KEY / AGNES_API_KEYS", file=sys.stderr)
        return None
    if not rising_top10:
        print("[AI摘要] 跳过: rising 列表为空", file=sys.stderr)
        return None
    lines = []
    for project in rising_top10[:10]:
        name = project.get("full_name", "")
        delta = project.get("delta")
        lines.append("%s (+%d)" % (name, delta) if delta is not None else name)
    prompt = "用一句话（30字以内）概括今日 GitHub AI/开源生态态势，基于以下涨星项目：" + "、".join(lines)
    payload = json.dumps({
        "model": llm.MODEL,
        "messages": [
            {"role": "system", "content": "你是一个简洁的 AI 开源态势分析师，回答不超过30字。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 200,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    try:
        for index, key in enumerate(keys):
            request = urllib.request.Request(
                llm.API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + key,
                    "User-Agent": "starhub-auto-update",
                },
            )
            try:
                with llm.safe_fetch(request) as response:
                    data = json.loads(response.read().decode("utf-8"))
                choices = data.get("choices") or []
                if not choices:
                    print("[AI摘要] 响应无 choices（key %d/%d）" % (index + 1, len(keys)), file=sys.stderr)
                    continue
                content = (choices[0].get("message", {}).get("content") or "").strip()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    print("[AI摘要] 密钥无效 HTTP %s，停止轮询" % exc.code, file=sys.stderr)
                    return None
                if exc.code == 429 or exc.code >= 500:
                    print("[AI摘要] HTTP %s（key %d/%d），换 key 重试" % (exc.code, index + 1, len(keys)),
                          file=sys.stderr)
                    continue
                print("[AI摘要] API 调用失败: HTTP %s" % exc.code, file=sys.stderr)
                return None
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                print("[AI摘要] 请求异常（key %d/%d）: %s" % (index + 1, len(keys), exc), file=sys.stderr)
                continue
            if content and len(content) <= 100:
                print("[AI摘要] %s" % content)
                return content
            print("[AI摘要] 响应不合格: content_len=%d finish_reason=%r usage=%s"
                  % (len(content), choices[0].get("finish_reason"), data.get("usage")), file=sys.stderr)
            break
    except Exception as exc:  # noqa: BLE001
        print("[AI摘要] 失败: %s" % exc, file=sys.stderr)
    return None


def build_category_guides(cats, known, cache_path=None, fast: bool = False) -> dict[str, str]:
    if fast:
        return {}
    path = Path(cache_path) if cache_path else GUIDES_CACHE_PATH
    cache = {}
    try:
        if path.exists():
            cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}
    by_cat = {}
    for full_name, category in (known or {}).items():
        by_cat.setdefault(category, []).append(full_name)
    changed = False
    result = {}
    for category in (cats or []):
        key = category.get("key")
        members = sorted(by_cat.get(key, []))
        if not members:
            continue
        digest = hashlib.md5("\n".join(members).encode("utf-8")).hexdigest()
        hit = cache.get(key)
        if hit and hit.get("members_hash") == digest and hit.get("text"):
            result[key] = hit["text"]
            continue
        sample = "、".join(member.replace("/", "/ ") for member in members[:25])
        text = llm.generate(
            "你是技术选型顾问。下面是一个 GitHub 收藏类目和它包含的仓库，"
            "用不超过 120 字的中文写一段该类目的选型导览：这类工具适合什么场景、"
            "挑选时看什么、如有明显代表项目可以点名（只点仓库名）。直接输出正文，不要标题和列表。",
            "类目：%s\n包含仓库：%s" % (category.get("label", key), sample),
        )
        if text:
            result[key] = text[:400]
            cache[key] = {"members_hash": digest, "text": result[key]}
            changed = True
        time.sleep(0.8)
    if changed:
        try:
            tmp = str(path) + ".tmp"
            Path(tmp).write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            print("[guides] 缓存写回失败: %s" % exc, file=sys.stderr)
    return result
