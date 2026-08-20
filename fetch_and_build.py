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
    """对未知新项目做关键词规则分类（已有项目走 known_categories.json 保持稳定）。
    规则要点：避免泛词子串误伤（如「蒸馏」「思维」「chat」），用语义更明确的短语。"""
    text = (fn + " " + (desc or "") + " " + " ".join(topics or [])).lower()
    if any(k in text for k in ["trading", "finance", "fincept", "quant", "金融", "交易", "bloomberg"]):
        return "finance"
    if any(k in text for k in ["tvbox", "iptv", "直播", "电视", "crawler", "爬虫", "download", "下载",
                               "translator", "翻译", "汉化", "网盘", "pan", "mpv", "userscript"]):
        return "tools"
    # 内容蒸馏类提前（如「把视频蒸馏成技能」类项目同时含视频/蒸馏字样，语义上属蒸馏）
    if any(k in text for k in ["distill", "思维蒸馏", "知识蒸馏", "内容蒸馏", "蒸馏成", "蒸馏出", "蒸馏任何",
                               "认知植入", "思维方式", "心智模型", "第一性", "first-principles",
                               "方法论", "nuwa", "女娲", "cangjie", "仓颉", "文风"]):
        return "distill"
    # 视频创作类（画布/视频生成工具；置于 tools 之后，避免爬虫/下载器含「视频」字样误伤）
    if any(k in text for k in ["video", "视频", "短剧", "drama", "film", "anime", "动漫",
                               "movie", "montage", "hyperframe", "shot", "画布", "canvas"]):
        return "video"
    if any(k in text for k in ["open-design", "baoyu-design", "awesome-design", "design.md",
                               "design-system", "design system", "frontend"]):
        return "frontend"
    if any(k in text for k in ["ppt", "powerpoint", "排版", "公众号", "wechat", "写作", "write",
                               "typeset", "editor"]):
        return "content"
    if any(k in text for k in ["book", "书籍", "教程", "guide", "指南", "from-scratch", "llms",
                               "learning", "入门", "weekly", "hellogithub", "实践", "tutorial",
                               "dive-into"]):
        return "learning"
    if any(k in text for k in ["code-review", "officecli", "reasonix", "sub2api", "freellmapi",
                               "2api", "中转", "mimo", "cc-connect", "coding", "编程"]):
        return "coding"
    if any(k in text for k in ["chatbot", "chatgpt", "assistant", "助手", "librechat", "astrbot",
                               "qwenpaw", "nuwax", "opensquilla", "workspace", "desktop", "agent-os"]):
        return "assistant"
    if any(k in text for k in ["opc", "one-person", "一人公司", "创业", "startup", "growth", "增长",
                               "business", "软著", "copyright", "专利", "patent", "合规",
                               "compliance", "legal"]):
        return "business"
    return "agent"


def fetch_stars(token=None):
    repos = []
    page = 1
    while True:
        url = "https://api.github.com/users/%s/starred?per_page=100&page=%d" % (USER, page)
        req = urllib.request.Request(url, headers=_api_headers(token))
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


# ==================== README 简介兜底 ====================
def _readme_first_sentence(text):
    """从 README 原文提取第一句像样的简介（清洗 markdown 噪音），失败返回 None。"""
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        # 跳过标题 / 代码块围栏 / 引用块（多为项目名或导航，无信息量）
        if re.match(r"^#{1,6}\s", s) or s.startswith(("```", "~~~")) or s.startswith(">"):
            continue
        # 去掉图片、链接（保留链接文字）、行内代码、HTML 标签
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"`[^`]*`", "", s)
        # 去掉行内加粗/斜体标记（先于行首符号剥离，保证 **xxx** 成对匹配）
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        s = re.sub(r"\*([^*]+)\*", r"\1", s)
        # 去掉行首列表符/强调符，压缩空白
        s = re.sub(r"^[\s\-*+]+", "", s).strip()
        s = re.sub(r"\s+", " ", s)
        low = s.lower()
        # 跳过徽章/导航/状态行等噪音
        if any(k in low for k in ("img.shields.io", "badge", "build passing", "build status",
                                  "license", "contributors", "中文", "english", "stars", "downloads")):
            continue
        if len(s) >= 10:
            # 中等长度也收敛为第一句（README 首段常是多句长段）
            if len(s) > 80:
                for sep in (". ", "。", "！", "? "):
                    idx = s.find(sep)
                    if 10 < idx <= 150:
                        return s[:idx].strip()
            # 超长截断到 150 字符，优先在句子边界截断
            if len(s) > 150:
                cut = s[:150]
                for sep in (". ", "。", "，", ", "):
                    idx = cut.rfind(sep)
                    if idx > 30:
                        return cut[:idx].strip()
                return cut.strip() + "…"
            return s
    return None


def fetch_readme_summary(fn, token):
    """无简介项目：抓 README 提取一句简介（失败/无 README 返回 None）。"""
    url = "https://api.github.com/repos/%s/readme" % fn
    headers = {"Accept": "application/vnd.github.raw", "User-Agent": "starhub-auto-update"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(30000).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    summary = _readme_first_sentence(raw)
    if summary:
        time.sleep(1)  # 限流：GitHub 建议相邻请求间隔 ≥1s
    return summary


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


# ==================== Trending 涨星榜（真实 stars today） ====================
# 2026 年起 GitHub Trending 每页仅展示 7~20 个仓库，因此抓多语言页合并去重凑量
TREND_LANG_PAGES = ["", "python", "typescript", "javascript", "rust", "go", "java",
                    "c++", "c", "swift", "kotlin", "ruby", "php", "c#", "shell",
                    "jupyter-notebook", "vue", "dart", "elixir", "haskell"]

# AI 分类关键词（ 词边界匹配 full_name + 描述，命中即视为 AI 类项目）
_AI_RE = re.compile(
    r"\b(ai|ml|llm|llms|gpt|nlp|rag|agent|agents|agentic|llama|claude|openai|anthropic|"
    r"gemini|copilot|diffusion|neural|transformer|chatbot|assistant|embedding|inference|"
    r"genai|generative|vision|speech|voice|machine.?learning|deep.?learning|model|models)\b")


def _is_ai_repo(fn, desc):
    """Trending 项目是否属于 AI 分类（关键词过滤 name + 描述）。"""
    return bool(_AI_RE.search((fn + " " + (desc or "")).lower()))


def _parse_trending(html):
    """解析 Trending 页单个语言维度的仓库卡片。"""
    out = []
    for b in re.findall(r'<article[^>]*class="[^"]*Box-row[^"]*"[\s\S]*?</article>', html):
        m = re.search(r'<h2[^>]*>[\s\S]*?href="/([^"/]+/[^"/]+)"', b)
        if not m:
            continue
        fn = m.group(1)
        if fn.startswith("sponsors/"):  # 赞助商卡片，跳过
            continue
        s = re.search(r'([\d,]+)\s+stars?\s+today', b)
        lang = re.search(r'itemprop="programmingLanguage"[^>]*>([^<]+)<', b)
        d = re.search(r'<h2[\s\S]*?</h2>[\s\S]*?<p[^>]*>([\s\S]*?)</p>', b)
        # 总星标：stargazers 链接内最后一个 </svg> 后的数字（2026 新版页面数字前有换行空格）
        st = None
        si = b.find('stargazers')
        if si != -1:
            ei = b.find('</a>', si)
            if ei != -1:
                m2 = re.search(r'</svg>\s*([\d,]+)', b[si:ei])
                if m2:
                    st = m2.group(1)
        desc = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', d.group(1))).strip() if d else ""
        owner, name = fn.split("/", 1)
        out.append({
            "name": name, "owner": owner, "full_name": fn,
            "html_url": "https://github.com/" + fn,
            "desc": desc, "language": lang.group(1) if lang else None,
            "stars": int(st.replace(",", "")) if st else 0,
            "stars_today": int(s.group(1).replace(",", "")) if s else 0,
        })
    return out


def fetch_trending_daily(token=None):
    """抓 GitHub Trending daily（多语言页合并去重）；全部失败返回 None 触发降级。"""
    pool = {}
    for lp in TREND_LANG_PAGES:
        url = "https://github.com/trending/%s?since=daily" % lp
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                               " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", errors="replace")
            for p in _parse_trending(html):
                if p["full_name"] not in pool:
                    pool[p["full_name"]] = p
        except Exception as e:  # noqa: BLE001
            print("[Trending %s 失败] %s" % (lp or "全部", e), file=sys.stderr)
        time.sleep(0.5)
    return list(pool.values()) if pool else None


def build_trending(token, desc_zh):
    snap = {}
    try:
        snap = json.load(open("trending_snapshot.json", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    pool = fetch_ai_pool(token)

    # 总榜：按总星标排序
    total = sorted(pool, key=lambda x: x["stars"], reverse=True)[:TREND_TOP]

    # 涨星榜：优先 Trending daily 真实 stars today（AI 分类过滤）
    rising = []
    trend_rows = fetch_trending_daily(token)
    if trend_rows:
        rising = [p for p in trend_rows if _is_ai_repo(p["full_name"], p["desc"])]
        rising.sort(key=lambda x: x["stars_today"], reverse=True)
        rising = rising[:TREND_TOP]
        for p in rising:
            p["delta"] = p["stars_today"]  # 前端 delta 徽标直接展示 stars today
    else:
        # 降级：Trending 抓取失败，回退到快照差值模式（排除超巨头，只看有昨日基线的项目）
        print("[涨星榜] Trending 抓取失败，降级为快照差值模式", file=sys.stderr)
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

    # 快照覆盖为今日星标数（作为明日基线）；AI 池为空说明本次构建异常（限流/网络故障），
    # 此时覆盖会清空全部基线且无法自愈，因此保留旧快照
    if pool:
        json.dump({p["full_name"]: p["stars"] for p in pool},
                  open("trending_snapshot.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    else:
        print("[警告] AI 池为空，跳过快照更新，保留旧基线", file=sys.stderr)

    return {"rising": rising, "total": total, "new": new_repos,
            "source": "trending" if trend_rows else "snapshot"}


def _today_cn():
    """北京时间今天的日期字符串 YYYY-MM-DD。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def _cn_dt(utc_str):
    """UTC 时间字符串 → 北京时间 datetime（解析失败返回 None）。"""
    if not utc_str:
        return None
    try:
        return datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
    except Exception:  # noqa: BLE001
        return None


def _cn_date(utc_str):
    """UTC 时间字符串 → 北京时间日期 YYYY-MM-DD。"""
    dt = _cn_dt(utc_str)
    return dt.strftime("%Y-%m-%d") if dt else (utc_str[:10] if utc_str else "")


def _cn_time(utc_str):
    """UTC 时间字符串 → 北京时间 HH:MM。"""
    dt = _cn_dt(utc_str)
    return dt.strftime("%H:%M") if dt else (utc_str[11:16] if utc_str else "")


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
    """聚合关注账号动态（滚动 24 小时窗口）：新仓库 / star / 关注 / PR / 版本发布 / 公开仓库 / 提交更新。"""
    now_cn = datetime.now(timezone(timedelta(hours=8)))
    cutoff = now_cn - timedelta(hours=24)  # 滚动窗口：最近 24 小时
    today = now_cn.strftime("%Y-%m-%d")
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
                dt = _cn_dt(e.get("created_at"))
                if dt is None or dt < cutoff:
                    continue
                d = dt.strftime("%Y-%m-%d")
                t = e.get("type")
                payload = e.get("payload") or {}
                actor = (e.get("actor") or {}).get("login", "")
                repo = (e.get("repo") or {}).get("name", "")
                tm = dt.strftime("%H:%M")
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
            last_dt = _cn_dt((evs[-1].get("created_at") or ""))
            if last_dt is None or last_dt < cutoff:
                break
            time.sleep(1)  # 限流：GitHub 建议相邻请求间隔 ≥1s，避免二级速率限制
    feed.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)
    return feed


def _safe_json(obj):
    # 转义 < 防止 </script> 注入：ensure_ascii=False 时 json.dumps 不转义 <、>，
    # 数据内联进 <script> 块后浏览器会在第一个 </script> 处提前闭合标签执行任意 JS。
    # \u003c 是合法 JSON 转义，json.loads 可还原，不破坏 dev_render.py 的提取流程。
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


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

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repos = fetch_stars(token)
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
        # 无简介项目：从 README 提取一句简介，结果持久化到 desc_zh 避免重复抓取
        if not desc and fn not in desc_zh:
            summary = fetch_readme_summary(fn, token)
            if summary:
                if not has_cn(summary):
                    translated = translate_to_zh(summary)
                    if translated:
                        summary = translated
                desc = summary
                desc_zh[fn] = summary
                print("[README简介] %s -> %s" % (fn, summary[:60]))
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

    trending = build_trending(token, desc_zh)
    feed = fetch_following_events(token)

    template = open("template.html", encoding="utf-8").read()
    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    html = (template
            .replace("__DATA__", _safe_json(out))
            .replace("__CATS__", _safe_json(CATS))
            .replace("__LANGS__", _safe_json(LANG_COLORS))
            .replace("__FAVS__", _safe_json(DEFAULT_FAVS))
            .replace("__TRENDING__", _safe_json(trending))
            .replace("__FEED__", _safe_json(feed))
            .replace("__UPDATED__", updated))

    open("index.html", "w", encoding="utf-8").write(html)
    json.dump(known, open("known_categories.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(desc_zh, open("descriptions_zh.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("更新完成：共 %d 个项目" % len(out))


if __name__ == "__main__":
    main()
