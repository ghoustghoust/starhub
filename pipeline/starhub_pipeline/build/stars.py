"""Star entry assembly and health scoring."""

from __future__ import annotations

from datetime import datetime, timezone

from ..enrich.classify import classify_repo
from ..enrich.translate import has_cn, translate_to_zh
from ..sources.github import cn_date, fetch_readme_summary, today_cn
from ..taxonomy import FALLBACK_DESC


def health_score(repo: dict) -> dict:
    now = datetime.now(timezone.utc)
    try:
        pushed = (repo.get("pushed_at") or "").strip()
        if not pushed:
            return {"tier": "unknown", "stale": False}
        dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (now - dt).days
    except Exception:  # noqa: BLE001
        return {"tier": "unknown", "stale": False}
    stale = age_days > 365
    if age_days > 365:
        tier = "red"
    elif age_days > 90:
        tier = "yellow"
    else:
        tier = "green"
    if bool(repo.get("archived")):
        return {"tier": "red", "stale": stale}
    stars = repo.get("stargazers_count") or 0
    forks = repo.get("forks") or 0
    if stars < 50 and forks < 5 and tier != "red":
        tier = "yellow" if tier == "green" else "red"
    if not (repo.get("description") or "") and not (repo.get("topics") or []) and tier != "red":
        tier = "yellow" if tier == "green" else "red"
    return {"tier": tier, "stale": stale}


def assemble_entries(repos, known, descriptions, cat_label, token, notes=None, *, cats, fast=False):
    out = []
    for repo in repos:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        category, note = classify_repo(
            full_name,
            repo.get("description"),
            repo.get("language"),
            repo.get("topics"),
            known,
            cats=cats,
            fast=fast,
        )
        if not note:
            note = (notes or {}).get(full_name) or ""
        known[full_name] = category
        description = (descriptions.get(full_name) or repo.get("description") or FALLBACK_DESC.get(full_name, "")).strip()
        if not fast and not description and full_name not in descriptions:
            summary = fetch_readme_summary(full_name, token)
            if summary:
                if not has_cn(summary):
                    translated = translate_to_zh(summary)
                    if translated:
                        summary = translated
                description = summary
                descriptions[full_name] = summary
                print("[README简介] %s -> %s" % (full_name, summary[:60]))
        if description:
            description = " ".join(description.split())
        if not fast and description and not has_cn(description) and full_name not in descriptions:
            translated = translate_to_zh(description)
            if translated:
                description = translated
                descriptions[full_name] = translated
                print("[翻译] %s -> %s" % (full_name, translated[:60]))
            else:
                print("[翻译失败-保留原文] %s" % full_name)
        health = health_score(repo)
        out.append({
            "id": full_name,
            "name": repo.get("name"),
            "owner": full_name.split("/")[0],
            "full_name": full_name,
            "html_url": repo.get("html_url"),
            "desc": description,
            "language": repo.get("language"),
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks") or 0,
            "license": ((repo.get("license") or {}).get("spdx_id") or ""),
            "topics": repo.get("topics", []),
            "pushed_at": (repo.get("pushed_at") or "")[:10],
            "updated_today": cn_date(repo.get("pushed_at")) == today_cn(),
            "category": category,
            "categoryLabel": cat_label.get(category, category),
            "note": note,
            "health": health["tier"],
            "stale": health["stale"],
            "starred_at": repo.get("starred_at") or "",
        })
    return out
