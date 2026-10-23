"""Stale-name attention block and safe pruning of the category map."""

from __future__ import annotations

from html import escape

ATTENTION_KIND_STAR_ORPHAN = "star_orphan"
ATTENTION_TITLE = "分类表里有 %d 个名字不在当前收藏中——本场未自动清理（拉取条数低于门槛），请核对收藏是否完整拉到"
ATTENTION_SAME_HINT = "同名候选（仅参考，同名不等于同一仓库）"
ATTENTION_MAX_ROWS = 25
PRUNE_LIVE_MIN_RATIO = 0.8


def prune_star_state(known: dict[str, str], live_names) -> list[str]:
    live = set(live_names)
    if not live or len(live) < len(known) * PRUNE_LIVE_MIN_RATIO:
        return []
    dropped = sorted(name for name in known if name not in live)
    for name in dropped:
        del known[name]
    return dropped


def star_attention_items(repos, known, notes=None, desc_zh=None):
    notes = notes or {}
    desc_zh = desc_zh or {}
    live = {(repo.get("full_name") or "") for repo in repos}
    live.discard("")
    if not live:
        return []
    by_base = {}
    for full_name in live:
        by_base.setdefault(full_name.split("/")[-1].lower(), []).append(full_name)
    items = []
    for full_name in sorted(set(known) - live):
        base = full_name.split("/")[-1].lower()
        same = sorted(name for name in by_base.get(base, []) if name != full_name)
        items.append({
            "kind": ATTENTION_KIND_STAR_ORPHAN,
            "item": full_name,
            "hint": {
                "category": known.get(full_name) or "",
                "has_note": bool((notes.get(full_name) or "").strip()),
                "has_desc": bool((desc_zh.get(full_name) or "").strip()),
                "same_base_name_live": same,
            },
        })
    return items


def render_star_attention(items) -> str:
    if not items:
        return ""
    shown = items[:ATTENTION_MAX_ROWS]
    rest = len(items) - len(shown)
    rows = []
    for item in shown:
        hint = item.get("hint") or {}
        name = item.get("item") or ""
        bits = []
        if hint.get("category"):
            bits.append("分类 " + str(hint["category"]))
        bits.append("点评" + ("有" if hint.get("has_note") else "无"))
        bits.append("中文简介" + ("有" if hint.get("has_desc") else "无"))
        same = hint.get("same_base_name_live") or []
        same_html = ""
        if same:
            same_html = (
                '<span class="attn-same">%s：%s</span>'
                % (escape(ATTENTION_SAME_HINT, quote=False), escape(", ".join(same), quote=False))
            )
        rows.append(
            '<li class="attn-row"><a class="attn-name" href="https://github.com/%s"'
            ' target="_blank" rel="noopener">%s</a>'
            '<span class="attn-meta">%s</span>%s</li>'
            % (escape(name), escape(name), escape(" · ".join(bits), quote=False), same_html)
        )
    more = (
        '<p class="attn-more">另有 %d 条未列出——清单再长下去会把体积写进每场重写的首页。</p>' % rest
    ) if rest else ""
    return (
        '<details class="attn" data-attention="star">'
        '<summary class="attn-sum">%s</summary>'
        '<ul class="attn-list">%s</ul>%s'
        '<p class="attn-note">本场未自动清理：拉到的收藏条数低于分类表门槛。'
        '两种可能——某一页没拉全，或你一次取关了太多。核对 GitHub 收藏后重新构建即可收敛。'
        '收藏条数另有对账，当前收藏与本页条目双向零差。</p>'
        '</details>'
        % (escape(ATTENTION_TITLE % len(items), quote=False), "".join(rows), more)
    )
