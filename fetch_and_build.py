#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Star 收藏台 —— 自动更新脚本
在 GitHub Actions 中每天运行：拉取 Kwei168 的 star 列表 → 智能分类 → 重新生成 index.html。
仅依赖 Python 标准库，无需安装第三方包。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER = "ghoustghoust"

CATS = [
    {"key": "agent",     "label": "AI Agent & Skills",      "color": "#0550ae", "dark": "#4493f8"},
    {"key": "distill",   "label": "思维蒸馏 & 认知",        "color": "#8250df", "dark": "#a371f7"},
    {"key": "video",     "label": "AI 视频创作",            "color": "#cf222e", "dark": "#f85149"},
    {"key": "coding",    "label": "AI 编程 & 工具链",       "color": "#1a7f37", "dark": "#3fb950"},
    {"key": "content",   "label": "内容创作 & 排版",        "color": "#d33982", "dark": "#e577b2"},
    {"key": "learning",  "label": "AI 学习 & 教程",         "color": "#b58400", "dark": "#d4a72c"},
    {"key": "assistant", "label": "AI 助手 & 应用",         "color": "#1b7c83", "dark": "#39c5cf"},
    {"key": "tools",     "label": "实用工具 & 资源",        "color": "#57606a", "dark": "#8b949e"},
    {"key": "finance",   "label": "金融 & 交易",            "color": "#c29700", "dark": "#e3b341"},
    {"key": "business",  "label": "商业 · 一人公司与知产",  "color": "#d4600a", "dark": "#f0883e"},
    {"key": "frontend",  "label": "前端 & 设计系统",        "color": "#0a7ea4", "dark": "#39a0c5"},
]

LANG_COLORS = {
    "Python": "#3572A5", "TypeScript": "#3178c6", "JavaScript": "#f1e05a",
    "HTML": "#e34c26", "Shell": "#89e051", "Go": "#00ADD8", "Java": "#b07219",
    "Jupyter Notebook": "#DA5B0B", "Vue": "#41b883", "PowerShell": "#012456",
    "CSS": "#563d7c", "C#": "#178600", "C++": "#f34b7d",
}

DEFAULT_FAVS = ["react/react", "github/spec-kit", "521xueweihan/HelloGitHub"]

# 已知的空描述项目，补充一句中文说明
FALLBACK_DESC = {
    "llazyl/TVBox": "TVBox 影视聚合播放器",
    "q215613905/TVBoxOS": "TVBoxOS 影视播放系统",
    "FongMi/TV": "基于 media3/ffmpeg/mpv 的开源影视播放器",
}


def has_cn(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def translate_to_zh(text):
    """把英文简介翻译成中文；全部端点失败返回 None（保留原文）。"""
    if not text:
        return None
    # 端点 1：Google 翻译非官方接口
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
        req = urllib.request.Request(
            "https://translate.googleapis.com/translate_a/single?" + params,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        result = "".join(seg[0] for seg in data[0] if seg[0]).strip()
        if result and has_cn(result):
            return result
    except Exception:  # noqa: BLE001
        pass
    # 端点 2：MyMemory 免费接口
    try:
        params = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
        req = urllib.request.Request(
            "https://api.mymemory.translated.net/get?" + params,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        result = (data.get("responseData", {}).get("translatedText") or "").strip()
        if result and has_cn(result) and "MYMEMORY WARNING" not in result:
            return result
    except Exception:  # noqa: BLE001
        pass
    return None


def classify_new(fn, desc, lang, topics):
    """对未知新项目做关键词规则分类（已有项目走 known_categories.json 保持稳定）。"""
    text = (fn + " " + (desc or "") + " " + " ".join(topics or [])).lower()
    if any(k in text for k in ["trading", "finance", "fincept", "quant", "金融", "交易", "bloomberg"]):
        return "finance"
    if any(k in text for k in ["tvbox", "iptv", "直播", "电视", "crawler", "爬虫", "download", "下载",
                               "translator", "翻译", "汉化", "网盘", "pan", "mpv", "ffmpeg", "userscript"]):
        return "tools"
    if any(k in text for k in ["open-design", "baoyu-design", "awesome-design", "design.md",
                               "design-system", "design system", "frontend", "react"]):
        return "frontend"
    if any(k in text for k in ["ppt", "powerpoint", "排版", "公众号", "wechat", "写作", "write",
                               "typeset", "editor", "design"]):
        return "content"
    if any(k in text for k in ["video", "短剧", "drama", "film", "anime", "动漫", "movie", "montage",
                               "hyperframe", "影视", "shot"]):
        return "video"
    if any(k in text for k in ["book", "书籍", "教程", "guide", "指南", "from-scratch", "llms",
                               "learning", "入门", "weekly", "hellogithub", "实践", "tutorial",
                               "dive-into", "deep"]):
        return "learning"
    if any(k in text for k in ["distill", "蒸馏", "认知", "思维", "nuwa", "女娲", "cangjie", "仓颉",
                               "first-principles", "第一性", "perspective", "文风", "methodology"]):
        return "distill"
    if any(k in text for k in ["code-review", "officecli", "reasonix", "sub2api", "freellmapi",
                               "2api", "中转", "mimo", "cc-connect", "coding", "编程"]):
        return "coding"
    if any(k in text for k in ["chat", "assistant", "助手", "chatbot", "librechat", "astrbot",
                               "qwenpaw", "nuwax", "opensquilla", "workspace", "desktop", "agent-os"]):
        return "assistant"
    if any(k in text for k in ["opc", "one-person", "一人公司", "创业", "startup", "growth", "增长",
                               "fde", "business", "软著", "copyright", "专利", "patent", "合规",
                               "compliance", "legal"]):
        return "business"
    return "agent"


def fetch_stars():
    repos = []
    page = 1
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "starhub-auto-update"}
    while True:
        url = "https://api.github.com/users/%s/starred?per_page=100&page=%d" % (USER, page)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("拉取失败: %s" % e, file=sys.stderr)
            return None
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.5)
    return repos


# ==================== 每日 AI 排行榜 ====================
AI_TOPICS = ["ai", "machine-learning", "deep-learning", "llm", "gpt", "agent"]
AI_MIN_STARS = 500       # AI 项目池最小星标
NEW_MIN_STARS = 50       # 新秀榜最小星标
TREND_TOP = 20           # 每榜展示数量
TREND_MAX_STARS = 50000  # 涨星榜排除超过此星标的巨头项目（避免 tensorflow/pytorch 霸榜）


def _api_headers(token):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "starhub-auto-update"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _search_repos(q, token, per_page=100, sort="stars"):
    url = ("https://api.github.com/search/repositories?q=%s&sort=%s&order=desc&per_page=%d"
           % (urllib.parse.quote(q), sort, per_page))
    req = urllib.request.Request(url, headers=_api_headers(token))
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8")).get("items", [])


def _pick(item):
    return {
        "name": item.get("name"),
        "owner": (item.get("full_name") or "").split("/")[0],
        "full_name": item.get("full_name"),
        "html_url": item.get("html_url"),
        "desc": " ".join((item.get("description") or "").split()),
        "language": item.get("language"),
        "stars": item.get("stargazers_count"),
        "created_at": (item.get("created_at") or "")[:10],
    }


def fetch_ai_pool(token):
    """多 topic 查询合并 AI 项目池（去重）。"""
    pool = {}
    for topic in AI_TOPICS:
        try:
            for item in _search_repos("topic:%s stars:>%d" % (topic, AI_MIN_STARS), token):
                fn = item.get("full_name")
                if fn and fn not in pool:
                    pool[fn] = _pick(item)
        except Exception as e:  # noqa: BLE001
            print("[AI池 %s 失败] %s" % (topic, e), file=sys.stderr)
        time.sleep(1)
    return list(pool.values())


def fetch_new_repos(token):
    """新秀榜：最近 7 天新建的 AI 项目，按星标排序。"""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    out = []
    try:
        for item in _search_repos("topic:ai created:>%s stars:>%d" % (since, NEW_MIN_STARS), token, per_page=TREND_TOP):
            out.append(_pick(item))
    except Exception as e:  # noqa: BLE001
        print("[新秀榜失败] %s" % e, file=sys.stderr)
    return out


def _desc_zh(fn, desc, desc_zh):
    """排行榜项目简介：优先缓存中文，未命中则翻译并回写。"""
    if not desc:
        return ""
    if desc_zh.get(fn):
        return desc_zh[fn]
    if has_cn(desc):
        desc_zh[fn] = desc
        return desc
    translated = translate_to_zh(desc)
    if translated:
        desc_zh[fn] = translated
        return translated
    return desc


def _fmt_zh(n):
    """星标数中文格式化：>=10000 显示「万」。"""
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(n)


def build_trending(token, desc_zh):
    snap = {}
    try:
        snap = json.load(open("trending_snapshot.json", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    pool = fetch_ai_pool(token)

    # 总榜：按总星标排序
    total = sorted(pool, key=lambda x: x["stars"], reverse=True)[:TREND_TOP]

    # 涨星榜：排除超巨头，只看有昨日基线的项目
    rising = []
    for p in pool:
        prev = snap.get(p["full_name"])
        if prev is not None and p["stars"] <= TREND_MAX_STARS:
            p["delta"] = p["stars"] - prev
            rising.append(p)
    rising.sort(key=lambda x: x["delta"], reverse=True)
    rising = rising[:TREND_TOP]

    # 首次运行无基线：涨星榜 fallback 到总榜，delta=None（页面显示"新上榜"）
    if not rising:
        rising = [dict(p, delta=None) for p in total]

    # 新秀榜
    new_repos = fetch_new_repos(token)

    for p in rising + total + new_repos:
        p["desc"] = _desc_zh(p["full_name"], p["desc"], desc_zh)

    # 排名依据（中文说明）
    for p in rising:
        if p.get("delta") is not None:
            p["reason"] = ("今日涨星 +%d" % p["delta"]) if p["delta"] >= 0 else ("今日涨星 %d" % p["delta"])
        else:
            p["reason"] = "新上榜 · %s星标" % _fmt_zh(p["stars"])
    for p in total:
        p["reason"] = "累计 %s星标" % _fmt_zh(p["stars"])
    for p in new_repos:
        p["reason"] = "近 7 天新建 · %s星标" % _fmt_zh(p["stars"])

    # 快照覆盖为今日星标数（作为明日基线）
    json.dump({p["full_name"]: p["stars"] for p in pool},
              open("trending_snapshot.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    return {"rising": rising, "total": total, "new": new_repos}


def _today_cn():
    """北京时间今天的日期字符串 YYYY-MM-DD。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _cn_date(utc_str):
    """UTC 时间字符串 → 北京时间日期 YYYY-MM-DD。"""
    if not utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return utc_str[:10]


def _cn_time(utc_str):
    """UTC 时间字符串 → 北京时间 HH:MM。"""
    if not utc_str:
        return ""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
        return dt.strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return utc_str[11:16]


def fetch_following(token):
    """列出关注的账号 login 列表。"""
    out = []
    page = 1
    while True:
        url = "https://api.github.com/users/%s/following?per_page=100&page=%d" % (USER, page)
        req = urllib.request.Request(url, headers=_api_headers(token))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("[关注列表失败] %s" % e, file=sys.stderr)
            break
        if not data:
            break
        out.extend(x.get("login") for x in data if x.get("login"))
        if len(data) < 100:
            break
        page += 1
        time.sleep(1)
    return out


def fetch_following_events(token):
    """聚合关注账号动态（昨日 0 点至今）：新仓库 / star / 关注 / PR / 版本发布 / 公开仓库 / 提交更新。"""
    now_cn = datetime.now(timezone(timedelta(hours=8)))
    today = now_cn.strftime("%Y-%m-%d")
    yesterday = (now_cn - timedelta(days=1)).strftime("%Y-%m-%d")
    following = fetch_following(token)
    feed = []
    for user in following:
        url = "https://api.github.com/users/%s/events/public?per_page=100&page=%%d" % user
        for page in (1, 2):
            req = urllib.request.Request(url % page, headers=_api_headers(token))
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    evs = json.loads(r.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                print("[事件拉取失败 %s] %s" % (user, e), file=sys.stderr)
                break
            if not evs:
                break
            for e in evs:
                d = _cn_date(e.get("created_at"))
                if not d or d < yesterday:
                    continue
                t = e.get("type")
                payload = e.get("payload") or {}
                actor = (e.get("actor") or {}).get("login", "")
                repo = (e.get("repo") or {}).get("name", "")
                tm = _cn_time(e.get("created_at"))
                day = "今天" if d == today else "昨天"
                item = None
                if t == "CreateEvent" and payload.get("ref_type") == "repository":
                    item = {"kind": "repo", "actor": actor, "repo": repo, "time": tm, "day": day, "date": d,
                            "url": "https://github.com/" + repo}
                elif t == "WatchEvent" and payload.get("action") == "started":
                    item = {"kind": "star", "actor": actor, "repo": repo, "time": tm, "day": day, "date": d,
                            "url": "https://github.com/" + repo}
                elif t == "FollowEvent":
                    target = (payload.get("target") or {}).get("login", "")
                    item = {"kind": "follow", "actor": actor, "target": target, "time": tm, "day": day, "date": d,
                            "url": "https://github.com/" + target}
                elif t == "PullRequestEvent" and payload.get("action") == "opened":
                    pr = payload.get("pull_request") or {}
                    item = {"kind": "pr", "actor": actor, "repo": repo,
                            "title": (pr.get("title") or "")[:60],
                            "url": pr.get("html_url", "https://github.com/" + repo),
                            "time": tm, "day": day, "date": d}
                elif t == "ReleaseEvent" and payload.get("action") == "published":
                    release = payload.get("release") or {}
                    item = {"kind": "release", "actor": actor, "repo": repo,
                            "tag": release.get("tag_name", ""),
                            "url": release.get("html_url", "https://github.com/" + repo + "/releases"),
                            "time": tm, "day": day, "date": d}
                elif t == "PublicEvent":
                    item = {"kind": "public", "actor": actor, "repo": repo,
                            "url": "https://github.com/" + repo,
                            "time": tm, "day": day, "date": d}
                elif t == "PushEvent":
                    size = payload.get("size", 0)
                    if size > 0:
                        item = {"kind": "push", "actor": actor, "repo": repo, "size": size,
                                "url": "https://github.com/" + repo + "/commits",
                                "time": tm, "day": day, "date": d}
                if item:
                    feed.append(item)
            # 事件按时间倒序返回：本页最早一条早于窗口起点则无需继续翻页
            last = evs[-1].get("created_at") or ""
            if last and _cn_date(last) < yesterday:
                break
            time.sleep(1)  # 限流：GitHub 建议相邻请求间隔 ≥1s，避免二级速率限制
    feed.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)
    return feed


def main():
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

    repos = fetch_stars()
    if repos is None:
        print("拉取 star 失败，保持现有 index.html 不变")
        return

    cat_label = {c["key"]: c["label"] for c in CATS}
    out = []
    for r in repos:
        fn = r.get("full_name")
        if not fn:
            continue
        cat = known.get(fn) or classify_new(fn, r.get("description"), r.get("language"), r.get("topics"))
        known[fn] = cat
        desc = (desc_zh.get(fn) or r.get("description") or FALLBACK_DESC.get(fn, "")).strip()
        if desc:
            desc = " ".join(desc.split())
        # 新项目英文简介自动翻译为中文，并持久化到 desc_zh 避免重复翻译
        if desc and not has_cn(desc) and fn not in desc_zh:
            translated = translate_to_zh(desc)
            if translated:
                desc = translated
                desc_zh[fn] = translated
                print("[翻译] %s -> %s" % (fn, translated[:60]))
            else:
                print("[翻译失败-保留原文] %s" % fn)
        out.append({
            "id": fn,
            "name": r.get("name"),
            "owner": fn.split("/")[0],
            "full_name": fn,
            "html_url": r.get("html_url"),
            "desc": desc,
            "language": r.get("language"),
            "stars": r.get("stargazers_count"),
            "topics": r.get("topics", []),
            "pushed_at": (r.get("pushed_at") or "")[:10],
            "updated_today": _cn_date(r.get("pushed_at")) == _today_cn(),
            "category": cat,
            "categoryLabel": cat_label[cat],
        })

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    trending = build_trending(token, desc_zh)
    feed = fetch_following_events(token)

    template = open("template.html", encoding="utf-8").read()
    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    html = (template
            .replace("__DATA__", json.dumps(out, ensure_ascii=False, separators=(",", ":")))
            .replace("__CATS__", json.dumps(CATS, ensure_ascii=False, separators=(",", ":")))
            .replace("__LANGS__", json.dumps(LANG_COLORS, ensure_ascii=False, separators=(",", ":")))
            .replace("__FAVS__", json.dumps(DEFAULT_FAVS, ensure_ascii=False, separators=(",", ":")))
            .replace("__TRENDING__", json.dumps(trending, ensure_ascii=False, separators=(",", ":")))
            .replace("__FEED__", json.dumps(feed, ensure_ascii=False, separators=(",", ":")))
            .replace("__UPDATED__", updated))

    open("index.html", "w", encoding="utf-8").write(html)
    json.dump(known, open("known_categories.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(desc_zh, open("descriptions_zh.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("更新完成：共 %d 个项目" % len(out))


if __name__ == "__main__":
    main()
