#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Star 收藏台 —— 自动更新脚本
在 GitHub Actions 中每天运行：拉取 ghoustghoust 的 star 列表 → 智能分类 → 重新生成 index.html。
仅依赖 Python 标准库，无需安装第三方包。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline"))
from starhub_pipeline.config import site_value  # noqa: E402
from starhub_pipeline.build.attention import (  # noqa: E402
    prune_star_state,
    render_star_attention,
    star_attention_items,
)
from starhub_pipeline.build.contracts import write_site_contracts  # noqa: E402
from starhub_pipeline.build.stars import assemble_entries as _assemble_entries  # noqa: E402
from starhub_pipeline.build.trending import (  # noqa: E402
    TRENDING_BOARD,
    build_trending as _build_trending,
    trending_board_payload,
)
from starhub_pipeline.enrich.embed import embed_star_entries  # noqa: E402
from starhub_pipeline.enrich.summarize import (  # noqa: E402
    build_category_guides as _build_category_guides,
    generate_ai_summary as _generate_ai_summary,
)
from starhub_pipeline.render.index import build_index_html  # noqa: E402
from starhub_pipeline.sources.github import (  # noqa: E402
    fetch_following_events as _fetch_following_events,
    fetch_stars,
)
from starhub_pipeline.taxonomy import CATS  # noqa: E402

# Fast path for CI while the refactored pipeline is being built: skip
# translation/LLM/embedding enrichment instead of waiting on free endpoints.
FAST = os.environ.get("STARHUB_FAST") == "1"

USER = site_value("github_user", "ghoustghoust")
PAGES_URL = site_value("pages_url", "https://ghoustghoust.github.io/starhub/")


def assemble_entries(repos, known, descriptions, cat_label, token, notes=None):
    return _assemble_entries(
        repos,
        known,
        descriptions,
        cat_label,
        token,
        notes=notes,
        cats=CATS,
        fast=FAST,
    )


def generate_ai_summary(rising):
    return _generate_ai_summary(rising, fast=FAST)


def build_category_guides(cats, known):
    return _build_category_guides(cats, known, fast=FAST)


def build_trending(token, descriptions):
    return _build_trending(
        token,
        descriptions,
        ai_topics=AI_TOPICS,
        ai_min_stars=AI_MIN_STARS,
        new_min_stars=NEW_MIN_STARS,
        trend_top=TREND_TOP,
        trend_max_stars=TREND_MAX_STARS,
        fast=FAST,
    )


def fetch_following_events(token):
    return _fetch_following_events(token, USER)


AI_TOPICS = ["ai", "machine-learning", "deep-learning", "llm", "gpt", "agent"]
AI_MIN_STARS = 500       # AI 项目池最小星标
NEW_MIN_STARS = 50       # 新秀榜最小星标
TREND_TOP = 20           # 每榜展示数量
TREND_MAX_STARS = 50000  # 涨星榜排除超过此星标的巨头项目（避免 tensorflow/pytorch 霸榜）

BUILD_CONFIG_FILE = "build_config.json"

# 限流场的首页兜底源：Pages 域（静态托管，不吃 GitHub API 的限流），实测首页 292 KB 左右，
# 所以小于 40 KB 的一律不当"上一版首页"用 —— 把错误页/半截页当首页发出去，比缺件更糟：
# 缺件至少会让 Stage 判失败并红，错误页却会被当成一次成功发布。
PAGES_INDEX_URL = PAGES_URL.rstrip("/") + "/index.html"
MIN_REAL_PAGE_BYTES = 40000
PLACEHOLDER_INDEX = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub Star 收藏台 · 星标数据暂缺</title></head>
<body style="font-family:system-ui,sans-serif;max-width:38em;margin:6rem auto;padding:0 1.2em;line-height:1.7">
<h1 style="font-size:1.15rem">星标数据暂缺</h1>
<p>本场构建拉取 GitHub 星标失败（多半是 API 限流），收藏列表沿用不到，因此这一页暂时没有内容。</p>
<p>其余栏目不受影响：<a href="ai-daily.html">AI 晨报</a>。</p>
</body></html>
"""


def fetch_live_index(url):
    """取回线上首页正文；取不到返回 None。测试里打桩它（本机这条通道要走代理，CI 直连可用）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "starhub-build/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if getattr(r, "status", 200) != 200:
                return None
            return r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print("[首页兜底] 取线上首页失败：%s" % exc, file=sys.stderr)
        return None


def _looks_like_real_index(body):
    return (bool(body) and body.lstrip().startswith("<!DOCTYPE html>")
            and "</html>" in body and len(body) >= MIN_REAL_PAGE_BYTES)


def write_index_fallback(out_dir="."):
    """缺首页时才补：先拿回上一版，拿不到写占位页。已有合格首页则一律不碰。

    为什么必须有这一步（2026-10-03 现取）：`if stars_ok:` 罩子里才有 `open("index.html","w")`，
    而页面 HTML 已退出 git ⇒ 限流场工作树里根本没有首页；Stage 的必检清单含 index.html，
    缺件会让 `Upload/Deploy Pages` 双双跳过 —— **代价不是"首页旧一小时"，是那一小时整站不发布**。
    判据：tests/rss_history/test_index_fallback_on_star_ratelimit.py（含"不许挪回罩子里"的 AST 反向控制）。
    """
    path = os.path.join(out_dir, "index.html")
    if os.path.exists(path) and os.path.getsize(path) >= MIN_REAL_PAGE_BYTES:
        return None
    body = fetch_live_index(PAGES_INDEX_URL)
    if _looks_like_real_index(body):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        print("[首页兜底] star 数据缺失，已用线上上一版顶上（%d B）⇒ Pages 不缺件" % len(body))
        return "live"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PLACEHOLDER_INDEX)
    print("[首页兜底] 取不到上一版首页，写占位页（宁可不美，不可缺件）")
    return "placeholder"



def load_build_config():
    """读取 build_config.json 覆盖榜单参数；文件缺失/损坏/字段非法时逐项回退内置默认值（零回归）。"""
    defaults = {
        "trend_top": TREND_TOP,
        "ai_min_stars": AI_MIN_STARS,
        "new_min_stars": NEW_MIN_STARS,
        "trend_max_stars": TREND_MAX_STARS,
        "ai_topics": list(AI_TOPICS),
        "ai_summary_enabled": True,
    }
    try:
        cfg = json.load(open(BUILD_CONFIG_FILE, encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except FileNotFoundError:
        cfg = {}
    except Exception as e:  # noqa: BLE001
        print("[配置] %s 解析失败（%s），使用内置默认值" % (BUILD_CONFIG_FILE, e), file=sys.stderr)
        cfg = {}
    merged = {}
    for key, dv in defaults.items():
        if key in cfg:
            v = cfg[key]
            if key == "ai_topics":
                ok = isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) and x.strip() for x in v)
            elif key == "ai_summary_enabled":
                ok = isinstance(v, bool)
            else:
                ok = isinstance(v, int) and not isinstance(v, bool) and v > 0
            if ok:
                merged[key] = v
                print("[配置] %s = %r" % (key, v))
                continue
            print("[配置] 字段 %s 非法，回退默认值" % key, file=sys.stderr)
        merged[key] = dv
    return merged


def main(mode="full"):
    global AI_TOPICS, AI_MIN_STARS, NEW_MIN_STARS, TREND_TOP, TREND_MAX_STARS
    cfg = load_build_config()
    AI_TOPICS = cfg["ai_topics"]
    AI_MIN_STARS = cfg["ai_min_stars"]
    NEW_MIN_STARS = cfg["new_min_stars"]
    TREND_TOP = cfg["trend_top"]
    TREND_MAX_STARS = cfg["trend_max_stars"]

    known = {}
    try:
        known = json.load(open("known_categories.json", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    desc_zh = {}
    try:
        desc_zh = json.load(open("descriptions_zh.json", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    notes = {}
    try:
        notes = json.load(open("known_notes.json", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repos = fetch_stars(token, USER)
    stars_ok = repos is not None
    if not stars_ok:
        print("::error::[Star] 拉取 star 失败（可能 API 限流）⇒ 首页改走兜底（见 write_index_fallback），"
              "其余栏目照常产出；这句 error 是给 CI 看的，不影响本场发布")

    cat_label = {c["key"]: c["label"] for c in CATS}
    out = assemble_entries(repos or [], known, desc_zh, cat_label, token, notes=notes)

    if stars_ok:
        # 表 = 收藏的投影：先剪掉已不在收藏的旧名，再算提醒条（剪干净了它自然归零）。
        # 必须挂在 stars_ok 里面：拉取失败时 out 是空表，"表−收藏"=整张 known，那时剪下去
        # 就是清空分类表 —— 门槛那道保护量的是 live 条数，拿不到条数时它也没法判。
        dropped = prune_star_state(known, {e["full_name"] for e in out})
        if dropped:
            print("[剪枝] 分类表移除 %d 个已不在收藏的名字：%s"
                  % (len(dropped), ", ".join(dropped[:8])))
        trending = build_trending(token, desc_zh)

        # AI 态势一句话：构建时生成，注入涨星榜区域
        ai_summary = ""
        if cfg.get("ai_summary_enabled", True):
            print("[AI摘要] enabled, rising=%d" % len(trending.get("rising", [])))
            ai_summary = generate_ai_summary(trending.get("rising", [])[:10]) or ""
        else:
            print("[AI摘要] 已禁用 (ai_summary_enabled=false)")

        feed = fetch_following_events(token)

        updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        # AI 摘要占位符替换：非空则渲染为带样式的摘要条，空则不显示
        if ai_summary:
            ai_summary_html = ('<div class="ai-summary">'
                               '<span class="ai-summary-icon">AI</span>'
                               '<span class="ai-summary-text">' + ai_summary + '</span></div>')
        else:
            ai_summary_html = ""
        # 数据面增强（全部失败降级、不阻塞发布）：语义向量内联 + 类目导览附加进 CATS 副本
        embed_star_entries(out, fast=FAST)
        try:
            guides = build_category_guides(CATS, known)
        except Exception as e:  # noqa: BLE001
            print("[guides] 生成失败，本场无导览: %s" % e, file=sys.stderr)
            guides = {}
        cats_with_guides = [dict(c, guide=guides.get(c["key"], "")) for c in CATS]
        # 提醒块与页面同源：条目算完了再算旧名。软失败——它只是提示，不许挡住首页发布。
        try:
            attention_html = render_star_attention(
                star_attention_items(out, known, notes, desc_zh))
        except Exception as e:  # noqa: BLE001
            print("[attention] 提醒条生成失败，本场无提醒: %s" % e, file=sys.stderr)
            attention_html = ""
        html = build_index_html(out, cats_with_guides, trending=trending, feed=feed,
                                updated=updated, ai_summary_html=ai_summary_html,
                                attention_html=attention_html)

        open("index.html", "w", encoding="utf-8").write(html)
        # 排行榜数据出口：浏览器同源 fetch 它（见 template.html 的 refreshTrendingBoard）。
        # 有了它，快车道重建整页就伤不到排行榜——内联那份只是首屏兜底。
        json.dump(trending_board_payload(trending, ai_summary_html, updated),
                  open(TRENDING_BOARD, "w", encoding="utf-8"), ensure_ascii=False)
        json.dump(known, open("known_categories.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(desc_zh, open("descriptions_zh.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        try:
            write_site_contracts(out, known, trending, ai_summary, updated, cats=CATS)
        except Exception as e:  # noqa: BLE001
            print("[contracts] 写入失败: %s" % e, file=sys.stderr)

    # 限流场也必须交得出首页：页面 HTML 已退出 git ⇒ 成功分支不写就是真的没有文件，
    # 而 Stage 的必检清单含 index.html ⇒ 缺件会让这一小时整站不发布（不只是首页旧）。
    # 只在"确实没拉到 star 数据"时兜底：拿 size 猜"有没有首页"会把瘦身后的新页误覆盖成线上旧页。
    # 判据：tests/rss_history/test_index_fallback_on_star_ratelimit.py
    if not stars_ok:
        write_index_fallback()

    # AI 晨报：数据源是 AIHOT（自带 API→RSS→本地 ai_daily.json 三级回退），与 star 数据无关，
    # 所以刻意不挂进上面的 if stars_ok: 分支 —— 限流场也必须产出 ai-daily.html：
    # 这 4 个页面 HTML 已退出 git（无兜底副本），缺件会让 Stage Pages 步 exit 1 且零输出，
    # 结果是整次 Pages 发布被静默跳过。判据：tests/rss_history/test_generator_decoupling.py
    try:
        import build_ai_daily
        build_ai_daily.main()
    except Exception as e:
        print("[AI晨报] 生成失败: %s" % e, file=sys.stderr)

    print("更新完成：共 %d 个项目" % len(out))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    main(mode)
