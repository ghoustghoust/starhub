# -*- coding: utf-8 -*-
"""Generate ai-daily.html from AIHOT 公开 API v1（https://aihot.virxact.com/api/v1/items，
匿名只读、无需 Key）+ 多渠道快讯（Hacker News / The Verge / TechCrunch / arXiv /
36氪 / Redis / AtlasNote）。
API 失败时依次回退 RSS（feed.xml）与本地 ai_daily.json。
仅依赖 Python 标准库。由 fetch_and_build.py 调用或独立运行。
每次构建自动拉取最新数据，筛选近 36 小时条目生成晨报。
多渠道抓取移植自 WorkBuddy ai-news-daily，已完全云端化、脱离本地定时任务。
页面风格：晨报编辑部（报纸式刊头 + 今日要闻头条区 + 双栏新闻流，冷绿强调色）。
"""
import html as html_mod
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

RSS_URL = "https://aihot.virxact.com/feed.xml"  # API 失败时的降级回退
API_URL = "https://aihot.virxact.com/api/v1/items"  # 公开 API v1，匿名只读
OUT = "ai-daily.html"
FALLBACK_SRC = "ai_daily.json"  # RSS 失败时回退到本地 JSON

# 多渠道快讯配置（HN / Verge / TechCrunch / arXiv / 36氪 / Redis / AtlasNote）
HN_QUERIES = ["AI", "LLM", "OpenAI", "GPT", "Claude", "machine learning", "Anthropic"]
VERGE_RSS = "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"
TECHCRUNCH_RSS = "https://techcrunch.com/category/artificial-intelligence/feed/"
ARXIV_CATS = ["cs.AI", "cs.CL", "cs.CV", "cs.LG", "cs.NE"]
# Redis 官方博客：内容以向量检索/LLM/Agent 为主，周更 2-3 篇，标准 RSS 可直连，无 36h 窗口命中属正常
# 36氪 AI 资讯流：官方 RSS 有人机验证反爬墙，经 RSSHub 公共镜像中转。
# 部分镜像封数据中心 IP（GitHub Actions / Vercel 出口），镜像链按可用性排序，依次尝试。
KR36_FEEDS = [
    "https://rsshub.ktachibana.party/36kr/information/AI",
    "https://rsshub.woodland.cafe/36kr/information/AI",
    "https://rsshub.rssforever.com/36kr/information/AI",
    "https://hub.slarker.me/36kr/information/AI",
]
REDIS_RSS = "https://redis.io/feed/"
# AtlasNote（atlasnote.ai）深度文章：AI 架构/创业/研究方法论的长文分析，周更 2-4 篇。
# 中英双语发布同一文章（/en/ 与 /zh-CN/ 两条），按 slug 归并偏好中文版（_atlasnote_items）。
ATLASNOTE_RSS = "https://atlasnote.ai/rss.xml"
UA = {"User-Agent": "Mozilla/5.0 (starhub-auto-update)"}

# RSS 分类 → 配色
CAT_COLOR = {
    "AI 模型": "#2563eb",
    "AI 产品": "#7c3aed",
    "行业动态": "#0891b2",
    "海外热点": "#0d9488",
    "论文": "#d97706",
    "技巧观点": "#dc2626",
}
# 分类排序权重（按此顺序展示）
CAT_ORDER = ["AI 模型", "AI 产品", "行业动态", "海外热点", "论文", "技巧观点"]

# API v1 分类 key → 晨报表演分类（与 RSS <category> 文本对齐）
API_CAT = {
    "ai-models": "AI 模型",
    "ai-products": "AI 产品",
    "industry": "行业动态",
    "paper": "论文",
    "tip": "技巧观点",
}

_ROMAN = ["", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
          "xi", "xii"]


def _now_bj():
    """北京时间 now。"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _roman(n):
    """分类编号：小写罗马数字（超出预置表用阿拉伯数字）。"""
    return _ROMAN[n] if 0 < n < len(_ROMAN) else str(n)


def _parse_rss_date(s):
    """解析 RSS pubDate（RFC 2822 格式）→ UTC datetime。"""
    s = s.strip()
    # 先处理 GMT → +0000（Windows 下 %z 不一定识别 GMT）
    s = s.replace(" GMT", " +0000")
    try:
        return datetime.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        pass
    # 回退：无时区信息
    try:
        return datetime.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S")
    except ValueError:
        return None


def _strip_html(text):
    """从 HTML 片段中提取纯文本。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    return text.strip()


def _extract_source(author_text):
    """从 RSS author 字段提取来源名。"""
    m = re.search(r"\(([^)]+)\)", author_text or "")
    return m.group(1) if m else ""


def _truncate(s, maxlen=120):
    if not s:
        return ""
    s = s.strip()
    if len(s) <= maxlen:
        return s
    cut = -1
    for i, ch in enumerate(s[:maxlen]):
        if ch in "。！？；":
            cut = i
    if cut >= 30:
        return s[:cut + 1]
    return s[:maxlen] + "…"


def _esc(s):
    return html_mod.escape(str(s), quote=True)


def fetch_api():
    """拉取 AIHOT 公开 API v1 精选条目（匿名只读，无需 Key）。
    取最近 7 天精选，由 _filter_today 统一做 36h 窗口筛选（与 RSS 行为一致）。
    返回 items 列表，失败返回 None。"""
    url = API_URL + "?mode=selected&window=7d&limit=100"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (starhub-auto-update)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read(2000000).decode("utf-8", errors="replace"))
    except Exception as e:
        print("[AI晨报] API 拉取失败: %s" % e, file=sys.stderr)
        return None

    items = []
    for it in data.get("items") or []:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        links = it.get("links") or {}
        # 原文优先，无原文链接时回退站内阅读页
        link = links.get("original") or links.get("aihot") or "#"
        source = ((it.get("source") or {}).get("name") or "").strip()
        # 过滤窗口与网页收录节奏对齐用 discoveredAt（RSS pubDate 同为收录时间）
        pub = it.get("discoveredAt") or it.get("publishedAt") or ""
        items.append({
            "title": title,
            "link": link,
            "category": API_CAT.get(it.get("category"), "行业动态"),
            "source": source,
            "summary": _truncate(it.get("summary") or ""),
            "pub_date": _parse_iso(pub),
            # AIHOT 编辑评分：参与头条评选打分，其余路径缺省 0
            "score": float(it.get("score") or 0),
        })
    return items or None


def fetch_rss():
    """降级回退：拉取 AIHOT RSS feed，返回 items 列表或 None。"""
    req = urllib.request.Request(RSS_URL, headers={
        "User-Agent": "Mozilla/5.0 (starhub-auto-update)",
        "Accept": "application/rss+xml, application/xml, text/xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(500000).decode("utf-8", errors="replace")
    except Exception as e:
        print("[AI晨报] RSS 拉取失败: %s" % e, file=sys.stderr)
        return None

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print("[AI晨报] RSS 解析失败: %s" % e, file=sys.stderr)
        return None
    except Exception as e:
        print("[AI晨报] RSS 处理异常: %s" % e, file=sys.stderr)
        return None

    channel = root.find("channel")
    if channel is None:
        print("[AI晨报] RSS 格式异常: 无 channel", file=sys.stderr)
        return None

    items = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        category = (item.findtext("category") or "").strip()
        source = _extract_source(item.findtext("author") or "")
        desc_raw = item.findtext("description") or ""
        # RSS 的 <link> 是站内阅读页，原文藏在 description 的「阅读原文」锚点里，原文优先
        m = re.search(r'<a href="([^"]+)"[^>]*>阅读原文</a>', desc_raw)
        if m and m.group(1).strip():
            link = m.group(1).strip()
        summary = _truncate(_strip_html(desc_raw))
        pub_date_str = (item.findtext("pubDate") or "").strip()
        pub_date = _parse_rss_date(pub_date_str)
        if title:
            items.append({
                "title": title,
                "link": link,
                "category": category or "其他",
                "source": source,
                "summary": summary,
                "pub_date": pub_date,
            })
    return items


def _fallback_json():
    """RSS 失败时从本地 ai_daily.json 加载。"""
    if not os.path.exists(FALLBACK_SRC):
        return None
    try:
        with open(FALLBACK_SRC, encoding="utf-8") as f:
            data = json.load(f)
        report = data.get("report") or data
        items = []
        for sec in report.get("sections", []):
            label = sec.get("label", "")
            cat_map = {
                "模型发布/更新": "AI 模型",
                "产品发布/更新": "AI 产品",
                "行业动态": "行业动态",
                "论文研究": "论文",
                "技巧与观点": "技巧观点",
            }
            cat = cat_map.get(label, label)
            for it in sec.get("items") or []:
                links = it.get("links") or {}
                items.append({
                    "title": (it.get("title") or "").strip(),
                    "link": links.get("original") or links.get("aihot") or "#",
                    "category": cat,
                    "source": (it.get("source") or {}).get("name", ""),
                    "summary": _truncate(it.get("summary") or ""),
                    "pub_date": None,
                })
        return items
    except Exception as e:
        print("[AI晨报] JSON 回退失败: %s" % e, file=sys.stderr)
        return None


# ---------------------------- 多渠道快讯 ----------------------------
# 移植自 WorkBuddy ai-news-daily（纯标准库），云端构建时直接抓取，
# 不再依赖本地定时任务与 news.json。X/头条/微信因反爬不可直接抓取，不纳入；
# 36氪官方 RSS 同样有反爬墙，改经 RSSHub 公共镜像中转（_36kr_items）。

def _fetch_url(url, timeout=20, accept=None):
    headers = dict(UA)
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1000000).decode("utf-8", errors="replace")


def _parse_iso(s):
    """ISO 时间（HN / arXiv）→ datetime，失败返回 None。"""
    s = (s or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.datetime.fromisoformat(s[:19])
        except ValueError:
            return None


def _hn_items():
    """Hacker News：Algolia API 按关键词检索近 48h 故事，按热度取前 10。"""
    since = int((datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=48)).timestamp())
    out, seen = [], set()
    for q in HN_QUERIES:
        try:
            url = ("https://hn.algolia.com/api/v1/search_by_date?query=%s"
                   "&tags=story&numericFilters=created_at_i>%d&hitsPerPage=8"
                   % (urllib.parse.quote(q), since))
            data = json.loads(_fetch_url(url, timeout=15))
            for h in data.get("hits", []):
                oid = h.get("objectID")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                link = h.get("url") or ("https://news.ycombinator.com/item?id=" + oid)
                out.append({
                    "title": title,
                    "link": link,
                    "category": "海外热点",
                    "source": "Hacker News",
                    "summary": "▲ %s points · by %s" % (h.get("points", 0), h.get("author", "?")),
                    "pub_date": _parse_iso(h.get("created_at") or ""),
                })
        except Exception as ex:
            print("[AI晨报] HN 拉取失败 (%s): %s" % (q, ex), file=sys.stderr)
    out.sort(key=lambda x: int(re.sub(r"\D", "", x["summary"]) or 0), reverse=True)
    return out[:10]


def _feed_items(url, source, limit=6):
    """RSS/Atom 订阅源（The Verge / TechCrunch）。"""
    try:
        raw = _fetch_url(url, timeout=20,
                         accept="application/rss+xml, application/xml, text/xml")
        root = ET.fromstring(raw)
    except Exception as ex:
        print("[AI晨报] %s 拉取失败: %s" % (source, ex), file=sys.stderr)
        return []
    items = []
    ns = "{http://www.w3.org/2005/Atom}"
    if root.tag.endswith("feed"):  # Atom（The Verge 等）
        for e in root.findall(ns + "entry"):
            title = _strip_html(e.findtext(ns + "title") or "")
            link_el = e.find(ns + "link")
            link = (link_el.get("href") if link_el is not None
                    else (e.findtext(ns + "id") or "")).strip()
            desc = _truncate(_strip_html(e.findtext(ns + "summary") or ""))
            pub = e.findtext(ns + "updated") or e.findtext(ns + "published") or ""
            if not title or not link:
                continue
            items.append({"title": title, "link": link, "category": "海外热点",
                          "source": source, "summary": desc,
                          "pub_date": _parse_iso(pub)})
    else:  # RSS 2.0
        ch = root.find("channel")
        if ch is None:
            return []
        for it in ch.findall("item"):
            title = _strip_html(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            desc = _truncate(_strip_html(it.findtext("description") or ""))
            pub = (it.findtext("pubDate") or "").strip()
            if not title or not link:
                continue
            items.append({"title": title, "link": link, "category": "海外热点",
                          "source": source, "summary": desc,
                          "pub_date": _parse_rss_date(pub)})
    return items[:limit]


def _parse_rss2(raw, source, category, limit):
    """RSS 2.0 XML 文本 → 晨报条目结构；解析失败返回空列表。"""
    try:
        root = ET.fromstring(raw)
    except Exception as ex:
        print("[AI晨报] %s 解析异常: %s" % (source, ex), file=sys.stderr)
        return []
    ch = root.find("channel")
    if ch is None:
        return []
    items = []
    for it in ch.findall("item"):
        title = _strip_html(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        desc = _truncate(_strip_html(it.findtext("description") or ""))
        pub = (it.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        items.append({"title": title, "link": link, "category": category,
                      "source": source, "summary": desc,
                      "pub_date": _parse_rss_date(pub)})
    return items[:limit]


def _rss_channel_items(url, source, category, limit, timeout=12):
    """通用 RSS 2.0 频道：拉取 + 解析，失败返回空列表。"""
    try:
        raw = _fetch_url(url, timeout=timeout,
                         accept="application/rss+xml, application/xml, text/xml")
    except Exception as ex:
        print("[AI晨报] %s 拉取失败: %s" % (source, ex), file=sys.stderr)
        return []
    return _parse_rss2(raw, source, category, limit)


def _36kr_items(limit=8):
    """36氪 AI 资讯流：经 RSSHub 镜像链依次尝试，命中即止，归入「行业动态」。中文内容无需翻译。"""
    for url in KR36_FEEDS:
        try:
            cand = _fetch_url(url, timeout=12,
                              accept="application/rss+xml, application/xml, text/xml")
        except Exception:
            continue
        if "<item>" in cand:
            return _parse_rss2(cand, "36氪", "行业动态", limit)
    print("[AI晨报] 36氪镜像链全部失败", file=sys.stderr)
    return []


def _atlasnote_items(limit=6):
    """AtlasNote 深度文章：标准 RSS 2.0，中英双语各发一条（URL 仅 /en/ 与 /zh-CN/ 之别）。
    按 slug 归并双语条目，同文优先保留中文版（标题/简介免翻译）；论文解读类归「论文」，
    其余归「技巧观点」。周更 2-4 篇，36h 窗口未命中属正常。"""
    items = _rss_channel_items(ATLASNOTE_RSS, "AtlasNote", "技巧观点", limit * 2)
    merged = {}
    for it in items:
        slug = urllib.parse.urlsplit(it["link"]).path.rstrip("/").split("/")[-1]
        prev = merged.get(slug)
        zh = "/zh-CN/" in it["link"]
        if prev is None or (zh and "/zh-CN/" not in prev["link"]):
            merged[slug] = it
    out = list(merged.values())
    for it in out:
        blob = it["title"] + " " + it["summary"]
        if re.search(r"论文|preprint|Paper", blob, re.IGNORECASE):
            it["category"] = "论文"
    return out[:limit]


def _arxiv_items():
    """arXiv：AI 相关分类按提交时间倒序，取前 8 条归入「论文」。带 2 次重试。"""
    q = " OR ".join("cat:" + c for c in ARXIV_CATS)
    url = ("https://export.arxiv.org/api/query?search_query=%s"
           "&sortBy=submittedDate&sortOrder=descending&max_results=20"
           % urllib.parse.quote(q))
    xml_text = None
    for attempt in range(3):
        try:
            xml_text = _fetch_url(url, timeout=30)
            break
        except Exception as ex:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))  # 3s, 6s
            else:
                print("[AI晨报] arXiv 拉取失败(重试3次): %s: %s" % (type(ex).__name__, ex), file=sys.stderr)
                return []
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception as ex:
        print("[AI晨报] arXiv XML 解析失败: %s" % ex, file=sys.stderr)
        return []
    ns = "{http://www.w3.org/2005/Atom}"
    items, seen = [], set()
    for e in root.findall(ns + "entry"):
        title = _strip_html(re.sub(r"\s+", " ", e.findtext(ns + "title") or ""))
        id_el = e.find(ns + "id")
        link = ((id_el.text or "").strip() if id_el is not None else "")
        if not title or not link or link in seen:
            continue
        seen.add(link)
        cat_el = e.find(ns + "category")
        cat = cat_el.get("term", "cs.AI") if cat_el is not None else "cs.AI"
        items.append({
            "title": "[%s] %s" % (cat, title),
            "link": link,
            "category": "论文",
            "source": "arXiv",
            "summary": _truncate(_strip_html(e.findtext(ns + "summary") or "")),
            "pub_date": _parse_iso(e.findtext(ns + "published") or ""),
        })
    return items[:8]


def fetch_multi_channel():
    """拉取多渠道快讯，返回 (items, names, statuses)。
    names 为成功信源名；statuses 为 (信源名, 成败) 全量清单，供页脚信源状态栏展示，
    单渠道失败不影响整体。"""
    items, names, statuses = [], [], []
    channels = [
        ("Hacker News", _hn_items),
        ("The Verge", lambda: _feed_items(VERGE_RSS, "The Verge", 6)),
        ("TechCrunch", lambda: _feed_items(TECHCRUNCH_RSS, "TechCrunch", 6)),
        ("arXiv", _arxiv_items),
        ("36氪", _36kr_items),
        ("Redis", lambda: _rss_channel_items(REDIS_RSS, "Redis", "行业动态", 3)),
        ("AtlasNote", _atlasnote_items),
    ]
    for name, fn in channels:
        try:
            got = fn()
        except Exception as ex:
            print("[AI晨报] %s 渠道异常: %s" % (name, ex), file=sys.stderr)
            got = []
        if got:
            items.extend(got)
            names.append(name)
            statuses.append((name, True))
            print("[AI晨报] %s 拉取 %d 条" % (name, len(got)))
        else:
            statuses.append((name, False))
            print("[AI晨报] %s 未拉到内容" % name, file=sys.stderr)
    return items, names, statuses


def _norm_title(t):
    return re.sub(r"[\W_]+", "", (t or "").lower())


# ---------------------------- 英文内容翻译 ----------------------------
# 四端点降级链：Google gtx → Bing 网页版 → MyMemory（带重试）→ Google dict-chrome。
# 晨报不做跨构建持久化缓存（每日内容不重复），仅构建内去重缓存。

def _has_cn(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


_TRANS_CACHE = {}
_TRANS_STATS = {"google": 0, "bing": 0, "mymemory": 0, "google_alt": 0, "fail": 0, "skip": 0}
_BING_TOKENS = None  # 构建内缓存，token 有效期 1 小时


def _fetch_bing_tokens():
    """访问 bing.com/translator 提取防滥用 token（IG / IID / key / token）。"""
    global _BING_TOKENS
    if _BING_TOKENS:
        return _BING_TOKENS
    req = urllib.request.Request(
        "https://www.bing.com/translator",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    r = urllib.request.urlopen(req, timeout=15)
    html = r.read().decode("utf-8")
    ig = re.search(r'IG:"([^"]+)"', html)
    iid = re.search(r'data-iid="([^"]+)"', html)
    tok = re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),"([^"]+)"', html)
    if not all([ig, iid, tok]):
        raise RuntimeError("Bing token 提取失败")
    _BING_TOKENS = {"IG": ig.group(1), "IID": iid.group(1), "key": tok.group(1), "token": tok.group(2)}
    return _BING_TOKENS


def _bing_translate(text):
    """Bing 网页版翻译（免费，无需 API key）。token 缓存复用。"""
    try:
        tokens = _fetch_bing_tokens()
    except Exception as ex:
        print("[AI晨报] Bing token 获取失败: %s" % ex, file=sys.stderr)
        return None
    data = urllib.parse.urlencode({
        "fromLang": "en", "text": text, "to": "zh-Hans",
        "token": tokens["token"], "key": tokens["key"],
    }).encode("utf-8")
    url = ("https://www.bing.com/ttranslatev3?isVertical=1&" +
           urllib.parse.urlencode({"IG": tokens["IG"], "IID": tokens["IID"]}))
    try:
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.bing.com/translator",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode("utf-8"))
        if isinstance(result, list) and result:
            trans = result[0].get("translations", [{}])
            if trans:
                return trans[0].get("text", "") or None
        # token 过期 → 清缓存下次重试
        if isinstance(result, dict) and "statusCode" in result:
            global _BING_TOKENS
            _BING_TOKENS = None
    except Exception as ex:
        print("[AI晨报] Bing 翻译失败: %s: %s" % (type(ex).__name__, ex), file=sys.stderr)
    return None


def _translate_to_zh(text):
    """英译中；全部端点失败返回 None（调用方保留原文）。
    端点 1: Google gtx（429 限流时退避重试）
    端点 2: Bing 网页版（免费，token 缓存 1h）
    端点 3: MyMemory（带 2 次重试 + 指数退避）
    端点 4: Google dict-chrome（备用 client ID）
    """
    if not text:
        return None
    hit = _TRANS_CACHE.get(text)
    if hit is not None:
        return hit or None
    result = None

    # ── 端点 1：Google gtx ──
    params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                "https://translate.googleapis.com/translate_a/single?" + params,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            cand = "".join(seg[0] for seg in data[0] if seg[0]).strip()
            if cand and _has_cn(cand):
                result = cand
                _TRANS_STATS["google"] += 1
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(3)
                continue
            if attempt == 1:
                print("[AI晨报] Google-gtx 端点失败: HTTP %s" % e.code, file=sys.stderr)
            break
        except Exception as ex:
            if attempt == 1:
                print("[AI晨报] Google-gtx 端点失败: %s: %s" % (type(ex).__name__, ex), file=sys.stderr)
            break

    # ── 端点 2：Bing 网页版（免费，无需 API key）──
    if not result:
        cand = _bing_translate(text)
        if cand and _has_cn(cand):
            result = cand
            _TRANS_STATS["bing"] += 1

    # ── 端点 3：MyMemory（带重试 + 指数退避）──
    if not result:
        for attempt in range(3):
            try:
                params = urllib.parse.urlencode({"q": text[:480], "langpair": "en|zh-CN"})
                req = urllib.request.Request(
                    "https://api.mymemory.translated.net/get?" + params,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8"))
                cand = (data.get("responseData", {}) or {}).get("translatedText", "").strip()
                if cand and _has_cn(cand) and "MYMEMORY WARNING" not in cand:
                    result = cand
                    _TRANS_STATS["mymemory"] += 1
                    break
                # 返回了但无中文 → 可能是限流警告
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s
            except Exception as ex:
                if attempt == 0:
                    print("[AI晨报] MyMemory 首次失败: %s: %s" % (type(ex).__name__, ex), file=sys.stderr)
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s

    # ── 端点 4：Google 备用 client=dict-chrome ──
    if not result:
        try:
            params = urllib.parse.urlencode({"client": "dict-chrome", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text})
            req = urllib.request.Request(
                "https://translate.googleapis.com/translate_a/single?" + params,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            cand = "".join(seg[0] for seg in data[0] if seg[0]).strip()
            if cand and _has_cn(cand):
                result = cand
                _TRANS_STATS["google_alt"] += 1
        except Exception as ex:
            print("[AI晨报] Google-dict-chrome 备用端点失败: %s: %s" % (type(ex).__name__, ex), file=sys.stderr)

    if not result:
        _TRANS_STATS["fail"] += 1
    _TRANS_CACHE[text] = result or ""
    return result


def _tr(text):
    """英文文本 → 中文；已含中文或翻译失败时原样返回。相邻请求间隔 0.5s 防限流。"""
    if not text or _has_cn(text):
        _TRANS_STATS["skip"] += 1
        return text
    time.sleep(0.5)
    translated = _translate_to_zh(text)
    if not translated:
        print("[AI晨报] 翻译失败-保留原文: %s" % text[:50], file=sys.stderr)
    return translated or text


def translate_extra_items(items):
    """多渠道快讯英文条目标题与简介翻译成中文（含 arXiv 摘要）。
    arXiv 标题的 [分类] 前缀保留；HN 的点数摘要为元信息不翻译。"""
    n = 0
    for it in items:
        if it["source"] == "arXiv":
            m = re.match(r"^(\[[^\]]+\]\s*)(.*)$", it["title"])
            prefix, body = (m.group(1), m.group(2)) if m else ("", it["title"])
            if not _has_cn(body):
                t = _tr(body)
                if t != body:
                    it["title"] = prefix + t
                    n += 1
        elif not _has_cn(it["title"]):
            t = _tr(it["title"])
            if t != it["title"]:
                it["title"] = t
                n += 1
        if it.get("summary") and not it["summary"].startswith("▲"):
            s = _tr(it["summary"])
            if s != it["summary"]:
                it["summary"] = s
    # 翻译统计：按端点报告成败，方便在 GA 日志中定位哪个端点哑了
    s = _TRANS_STATS
    detail = "Google=%d Bing=%d MyMemory=%d Google-chrome=%d 失败=%d 跳过=%d" % (
        s["google"], s["bing"], s["mymemory"], s["google_alt"], s["fail"], s["skip"])
    print("[AI晨报] 英文条目翻译完成，%d 条标题已中文化（%s）" % (n, detail))


def _norm_url(u):
    """URL 规范化：去协议、www 前缀、utm_* 追踪参数与末尾斜杠，供同文异标题判断。
    其余 query 保留（如 YouTube /watch?v= 仅凭参数区分视频，不可整段丢弃）。"""
    p = urllib.parse.urlsplit(u or "")
    if not p.netloc:
        return ""
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith("utm_")]
    host = re.sub(r"^www\.", "", p.netloc.lower())
    return host + p.path.rstrip("/") + \
        ("?" + urllib.parse.urlencode(q) if q else "")


def _summ_key(it):
    """摘要规范化指纹：≥30 字符才可用于互含判断（HN 点数等元信息天然被排除）。"""
    s = _norm_title(it.get("summary") or "")
    return s if len(s) >= 30 else ""


def _dedupe(base, extra):
    """跨源去重（四层递进）：① 规范化标题精确相同；② 标题包含关系（短标题 ≥ 8 字符且为长标题子串，
    覆盖同一新闻中文/编译标题差异）；③ 规范化 URL 相同（覆盖同文标题被改写后的跨源收录，
    如 AIHOT 原文链与渠道 RSS 链指向同一篇）；④ 规范化摘要互为包含（覆盖标题与 URL 全异、
    摘要转录一致的同文转载）。不引入「同域同天」判重——同一信源当天会发布多篇不同文章。"""
    seen_titles = [_norm_title(it["title"]) for it in base]
    seen_urls = {u for u in (_norm_url(it["link"]) for it in base) if u}
    seen_sums = [s for s in (_summ_key(it) for it in base) if s]
    out = []
    for it in extra:
        t = _norm_title(it["title"])
        dup = t in seen_titles
        if not dup:
            dup = any(
                (len(t) >= 8 and len(s) >= 8 and (t in s or s in t))
                for s in seen_titles
            )
        if not dup:
            nu = _norm_url(it["link"])
            dup = bool(nu) and nu in seen_urls
        if not dup:
            sk = _summ_key(it)
            dup = bool(sk) and any(sk in s or s in sk for s in seen_sums)
        if dup:
            continue
        seen_titles.append(t)
        nu = _norm_url(it["link"])
        if nu:
            seen_urls.add(nu)
        sk = _summ_key(it)
        if sk:
            seen_sums.append(sk)
        out.append(it)
    return out


# 头条评分分类权重：新闻类 > 观点/产品 > 论文（快讯性质更强的分类更宜当头条）
_LEAD_CAT_W = {"行业动态": 1.2, "AI 模型": 1.15, "AI 产品": 1.0,
               "海外热点": 1.0, "技巧观点": 0.9, "论文": 0.7}
# 信源权重：有编辑精选机制的信源略高；权重差刻意压小，避免评分被单一信源垄断
_LEAD_SRC_W = {"Hacker News": 1.1, "AIHOT": 1.05, "36氪": 1.05}


def _headline_score(it):
    """头条候选评分 = AIHOT 编辑分 ×0.6 + 跨源报道数 ×25 + 信源权重 ×10 + 分类权重 ×10。
    跨源报道数来自去重前的标题频次统计：被多信源报道 = 更重大。"""
    now = _now_bj()
    pub = it.get("pub_date")
    fresh = 0
    if pub is not None:
        try:
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            fresh = max(0, 10 - (now - pub).total_seconds() / 3600.0)
        except Exception:
            fresh = 0
    return (it.get("score", 0) * 0.6
            + it.get("cov", 1) * 25
            + _LEAD_SRC_W.get(it.get("source", ""), 1.0) * 10
            + _LEAD_CAT_W.get(it.get("category", ""), 1.0) * 10
            + fresh * 0.3)


def _filter_today(items):
    """筛选今日日报条目：往前回溯 36 小时窗口。"""
    now = _now_bj()
    cutoff = now - datetime.timedelta(hours=36)
    out = []
    for it in items:
        if it["pub_date"] is None:
            continue  # 无法确定日期，跳过
        pd = it["pub_date"]
        if pd.tzinfo is not None:
            pd = pd.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        else:
            pd = pd.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        if pd >= cutoff:
            out.append(it)
    return out


def _group_by_cat(items):
    """按分类分组，返回 [(cat, [items])] 按 CAT_ORDER 排序。"""
    groups = {}
    for it in items:
        cat = it["category"]
        groups.setdefault(cat, []).append(it)
    result = []
    for cat in CAT_ORDER:
        if cat in groups:
            result.append((cat, groups.pop(cat)))
    for cat, its in groups.items():
        result.append((cat, its))
    return result


def _item_html(it, top=False):
    """单条报道 HTML（晨报编辑部样式）。"""
    src = _esc(it["source"])
    title = _esc(it["title"])
    link = _esc(it["link"])
    summary = _esc(it["summary"])
    cls = ' class="item top"' if top else ' class="item"'
    return (
        f'<article{cls}>'
        f'<span class="item-src">{src}</span>'
        f'<h3 class="item-title"><a href="{link}" target="_blank" rel="noopener">{title}</a></h3>'
        f'<p class="item-desc">{summary}</p>'
        f'</article>'
    )


def build_html(grouped, date_human, window_human, sources_note="", status_line=""):
    """生成晨报编辑部风格 HTML 页面。
    sources_note：附加数据源标注（如多渠道快讯）；status_line：页脚信源成败状态栏。"""
    total = sum(len(its) for _, its in grouped)

    # ---- 今日索引（报纸式编号导航条）----
    idx_items = ""
    for i, (cat, its) in enumerate(grouped, 1):
        color = CAT_COLOR.get(cat, "#57606a")
        idx_items += (
            f'<a href="#sec-{i}">'
            f'<span class="idx-n" style="color:{color}">{_roman(i)}</span>'
            f'{_esc(cat)}<span class="idx-cnt">{len(its)}</span>'
            f'</a>'
        )

    # ---- 分栏内容 ----
    sections_html = ""
    for i, (cat, its) in enumerate(grouped, 1):
        color = CAT_COLOR.get(cat, "#57606a")
        anchor = f"sec-{i}"
        # 列表项：每类第一条为"类内头条"（top 样式）
        items_html = ""
        for j, it in enumerate(its):
            items_html += _item_html(it, top=(j == 0))

        sections_html += (
            f'<section id="{anchor}" class="sec">'
            f'<div class="sec-head">'
            f'<span class="sec-num" style="color:{color}">{i:02d}</span>'
            f'<h2 class="sec-name">{_esc(cat)}</h2>'
            f'<span class="sec-cnt">{len(its)} 篇</span>'
            f'</div>'
            f'<div class="cols">{items_html}</div>'
            f'</section>'
        )

    # ---- 今日要闻头条区：大头条按评分从全部分类中选出（跨源报道数 + 编辑分 + 信源/分类权重 + 新鲜度），
    # 不再固定取「行业动态」第一条；次头条从大头条未覆盖的分类各取一条 ----
    all_items = [it for _, its in grouped for it in its]
    lead_main = max(all_items, key=_headline_score) if all_items else None
    lead_sides = []
    seen_links = set()
    if lead_main:
        seen_links.add(lead_main["link"])
    for cat, _its in grouped:
        if len(lead_sides) >= 2:
            break
        if lead_main and cat == lead_main["category"]:
            continue
        for c in _its:
            if c["link"] not in seen_links:
                lead_sides.append(c)
                seen_links.add(c["link"])
                break

    front_html = ""
    if lead_main:
        src = _esc(lead_main["source"])
        title = _esc(lead_main["title"])
        link = _esc(lead_main["link"])
        summary = _esc(lead_main["summary"])
        side_html = ""
        for it in lead_sides:
            side_html += (
                f'<article class="side-item">'
                f'<span class="side-src">{_esc(it["source"])}</span>'
                f'<h3><a href="{_esc(it["link"])}" target="_blank" rel="noopener">{_esc(it["title"])}</a></h3>'
                f'</article>'
            )
        front_html = (
            f'<section class="front" aria-label="今日要闻">'
            f'<div class="front-kicker">今日要闻 · TOP STORIES</div>'
            f'<div class="lead-row">'
            f'<article class="lead-main">'
            f'<span class="lead-src">{src}</span>'
            f'<h2><a href="{link}" target="_blank" rel="noopener">{title}</a></h2>'
            f'<p>{summary}</p>'
            f'</article>'
            f'<div class="lead-side">{side_html}</div>'
            f'</div>'
            f'</section>'
        )

    date_vol = date_human.replace("年", ".").replace("月", ".").replace("日", "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AIHOT 日报 · {date_human}</title>
<!-- font-stack: 中文衬线走系统 Songti/SimSun，数字走 Georgia（离线单文件，不引外部字体） -->
<style>
:root {{
  --bg:#faf8f4; --card:#fffdf9; --card-2:#f3efe6;
  --ink:#1f1c17; --muted:#6f6860; --faint:#857e74;
  --line:#e4ddd0; --line-strong:#b9b0a2;
  --accent:#30891A; --accent-ink:#256d13; --accent-weak:rgba(48,137,26,.08);
  --display:"Georgia","Times New Roman","Songti SC","SimSun","STSong",serif;
  --body:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
}}
[data-theme="dark"] {{
  --bg:#101114; --card:#17191d; --card-2:#1d2026;
  --ink:#e9e6df; --muted:#a49c90; --faint:#7d776d;
  --line:#2a2d33; --line-strong:#4a4e57;
  --accent:#5bc23e; --accent-ink:#a3e88f; --accent-weak:rgba(91,194,62,.12);
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
html{{scroll-behavior:smooth;scroll-padding-top:24px;}}
body{{
  font-family:var(--body);background:var(--bg);color:var(--ink);
  line-height:1.65;-webkit-font-smoothing:antialiased;font-size:15px;
}}
a{{color:inherit;text-decoration:none;}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px;}}
.paper{{max-width:980px;margin:0 auto;padding:0 20px 64px;}}

/* top bar */
.topbar{{display:flex;align-items:center;gap:10px;padding:14px 0 4px;}}
.back{{
  display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:999px;
  background:var(--card);border:1px solid var(--line);font-size:13px;transition:all .15s;
}}
.back:hover{{border-color:var(--accent);color:var(--accent-ink);}}
.topbar .spacer{{flex:1;}}
.theme-btn{{
  width:36px;height:36px;border-radius:999px;background:var(--card);border:1px solid var(--line);
  display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--ink);
  transition:all .15s;
}}
.theme-btn:hover{{border-color:var(--accent);color:var(--accent-ink);}}
.theme-btn svg{{width:16px;height:16px;}}

/* masthead */
.masthead{{text-align:center;padding:26px 0 0;}}
.mast-rule{{display:flex;align-items:center;gap:14px;margin:0 0 4px;}}
.mast-rule::before,.mast-rule::after{{content:"";flex:1;height:1px;background:var(--line-strong);}}
.mast-meta{{font-size:11.5px;letter-spacing:.14em;color:var(--muted);font-weight:500;white-space:nowrap;}}
.mast-title{{
  font-family:var(--display);font-size:clamp(40px,7vw,58px);font-weight:700;
  letter-spacing:.06em;line-height:1.15;margin:6px 0 2px;
}}
.mast-title .tick{{color:var(--accent);}}
.mast-sub{{font-size:12.5px;color:var(--muted);letter-spacing:.1em;}}
.mast-strip{{
  margin-top:14px;padding:8px 12px;border-top:3px double var(--line-strong);border-bottom:1px solid var(--line-strong);
  font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:4px 18px;justify-content:center;
}}
.mast-strip b{{color:var(--ink);font-weight:600;}}

/* index strip */
.index{{
  display:flex;flex-wrap:wrap;justify-content:center;gap:6px 22px;
  padding:14px 0 4px;font-size:13px;
}}
.index a{{color:var(--muted);transition:color .15s;display:inline-flex;gap:6px;align-items:baseline;}}
.index a:hover{{color:var(--accent-ink);}}
.index .idx-n{{font-family:var(--display);font-style:italic;font-size:12px;}}
.index .idx-cnt{{font-size:11px;color:var(--faint);}}

/* front page lead */
.front{{margin:22px 0 8px;border-top:4px solid var(--ink);border-bottom:1px solid var(--line-strong);padding:18px 0 20px;}}
.front-kicker{{
  font-size:11.5px;letter-spacing:.18em;color:var(--accent-ink);font-weight:700;margin-bottom:10px;
  display:flex;align-items:center;gap:10px;
}}
.front-kicker::after{{content:"";flex:1;height:1px;background:var(--line);}}
.lead-row{{display:grid;grid-template-columns:1.6fr 1fr;gap:26px;}}
.lead-main .lead-src{{font-size:11px;color:var(--muted);background:var(--card);border:1px solid var(--line);padding:2px 8px;border-radius:3px;display:inline-block;margin-bottom:10px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;}}
.lead-main h2{{
  font-family:var(--display);font-size:clamp(24px,3.4vw,34px);font-weight:700;line-height:1.28;margin-bottom:10px;
}}
.lead-main h2 a:hover{{color:var(--accent-ink);}}
.lead-main p{{font-size:14px;color:var(--muted);}}
.lead-side{{display:flex;flex-direction:column;gap:14px;border-left:1px solid var(--line);padding-left:22px;}}
.lead-side .side-item{{padding-bottom:12px;border-bottom:1px solid var(--line);}}
.lead-side .side-item:last-child{{border-bottom:none;padding-bottom:0;}}
.side-item .side-src{{font-size:10.5px;color:var(--faint);display:block;margin-bottom:5px;letter-spacing:.04em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.side-item h3{{font-family:var(--display);font-size:17px;font-weight:700;line-height:1.4;}}
.side-item h3 a:hover{{color:var(--accent-ink);}}

/* sections */
.sec{{margin:34px 0 0;}}
.sec-head{{
  display:flex;align-items:baseline;gap:14px;padding-bottom:8px;
  border-bottom:3px double var(--line-strong);margin-bottom:16px;
}}
.sec-num{{font-size:18px;font-weight:800;flex:none;}}
.sec-name{{font-family:var(--display);font-size:22px;font-weight:700;letter-spacing:.04em;margin:0;line-height:1.3;}}
.sec-cnt{{margin-left:auto;font-size:12px;color:var(--muted);white-space:nowrap;}}
.cols{{display:grid;grid-template-columns:1fr 1fr;column-gap:34px;row-gap:0;}}
.item{{
  padding:13px 0;border-bottom:1px solid var(--line);
  break-inside:avoid;
}}
.item-src{{
  font-size:10.5px;color:var(--faint);letter-spacing:.05em;display:block;margin-bottom:5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}}
.item-title{{font-size:15px;font-weight:700;line-height:1.5;margin:0 0 4px;}}
.item-title a:hover{{color:var(--accent-ink);}}
.item-desc{{font-size:12.5px;color:var(--muted);line-height:1.6;}}
.item.top{{background:var(--card-2);border:1px solid var(--line);padding:12px 14px;}}
.item.top .item-title{{font-size:16px;}}

/* footer */
.foot{{
  margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:space-between;
}}
.foot a{{color:var(--accent-ink);font-weight:500;}}
.foot a:hover{{text-decoration:underline;}}

@media (max-width:760px){{
  .paper{{padding:0 14px 48px;}}
  .lead-row{{grid-template-columns:1fr;gap:16px;}}
  .lead-side{{border-left:none;padding-left:0;border-top:1px solid var(--line);padding-top:12px;}}
  .cols{{grid-template-columns:1fr;}}
  .mast-title{{letter-spacing:.03em;}}
  .index{{gap:6px 14px;}}
}}
@media (prefers-reduced-motion:reduce){{
  html{{scroll-behavior:auto;}}
  *{{transition:none!important;animation:none!important;}}
}}
</style>
<script>try{{var _t=localStorage.getItem('wb_starhub_theme_v1')||(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');document.documentElement.dataset.theme=_t;}}catch(e){{}}</script>
</head>
<body>
<div class="paper">

  <div class="topbar">
    <a class="back" href="index.html">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></svg>
      StarHub
    </a>
    <div class="spacer"></div>
    <button class="theme-btn" id="btnTheme" title="切换明暗主题" aria-label="切换明暗主题">
      <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7"/></svg>
      <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z"/></svg>
    </button>
  </div>

  <header class="masthead">
    <div class="mast-rule"><span class="mast-meta">{date_human}</span><span class="mast-meta">每早八时 · DAILY</span></div>
    <h1 class="mast-title">AIHOT<span class="tick"> · </span>日报</h1>
    <div class="mast-sub">VOL.{date_vol} &nbsp;·&nbsp; 今日 {total} 篇报道</div>
    <div class="mast-strip">
      <span>{window_human}</span>
      <span>数据源 <b>AIHOT</b>{sources_note} · 内容版权归原作者</span>
    </div>
  </header>

  <nav class="index" aria-label="今日分类索引">
    {idx_items}
  </nav>

  {front_html}

  <main>
    {sections_html}
  </main>

  <footer class="foot">
    <span>共 <strong>{total}</strong> 条 · 数据源：<a href="https://aihot.virxact.com" target="_blank" rel="noopener">AIHOT</a>{sources_note}</span>
    <span>{status_line}</span>
    <span>{date_human} · 内容版权归原作者</span>
  </footer>
</div>

<script>
  var btn=document.getElementById('btnTheme');
  function syncIcons(){{
    var d=document.documentElement.dataset.theme==='dark';
    var sun=document.querySelector('.icon-sun'),moon=document.querySelector('.icon-moon');
    if(sun)sun.style.display=d?'none':'block';
    if(moon)moon.style.display=d?'block':'none';
  }}
  if(btn) btn.onclick=function(){{
    var t=document.documentElement.dataset.theme==='dark'?'light':'dark';
    try{{localStorage.setItem('wb_starhub_theme_v1',t);}}catch(e){{}}
    document.documentElement.dataset.theme=t;
    syncIcons();
  }};
  syncIcons();
</script>
</body>
</html>"""


def main():
    now = _now_bj()
    date_human = f"{now.year}年{now.month}月{now.day}日 " + \
        ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]

    items = fetch_api()
    if not items:
        print("[AI晨报] API 失败，回退 RSS", file=sys.stderr)
        items = fetch_rss()
    if items:
        print("[AI晨报] AIHOT 拉取成功，共 %d 条" % len(items))
        filtered = _filter_today(items)
        print("[AI晨报] 今日筛选后 %d 条" % len(filtered))
        # 今日无内容时回退展示全部（数据可能是前一天晚上更新的）
        if not filtered:
            print("[AI晨报] 今日无条目，回退展示全部精选", file=sys.stderr)
        items = filtered or items
    else:
        print("[AI晨报] API/RSS 均失败，尝试本地 JSON 回退", file=sys.stderr)
        items = _fallback_json()
        if items:
            print("[AI晨报] JSON 回退成功，共 %d 条" % len(items))

    # 多渠道快讯（HN / Verge / TechCrunch / arXiv / 36氪 / Redis / AtlasNote），云端直接抓取，脱离 WorkBuddy
    extra, extra_names, chan_statuses = fetch_multi_channel()
    extra_today = _filter_today(extra)
    # 跨源报道数：在去重前统计规范化标题频次，供头条评分使用（被多源报道 = 更重大）
    cov = {}
    for it in extra_today:
        k = _norm_title(it["title"])
        cov[k] = cov.get(k, 0) + 1
    for it in extra_today:
        it["cov"] = cov.get(_norm_title(it["title"]), 1)
    extra = _dedupe(items or [], extra_today)
    if extra:
        translate_extra_items(extra)
        items = (items or []) + extra
        print("[AI晨报] 多渠道新增 %d 条（%s）" % (len(extra), " / ".join(extra_names)))

    if not items:
        print("[AI晨报] 无可用数据源，跳过生成", file=sys.stderr)
        return False

    grouped = _group_by_cat(items)
    sources_note = (" · " + " / ".join(extra_names)) if extra_names else ""
    # 页脚信源状态栏：主源（API/RSS/JSON 回退）+ 各快讯渠道成败，一眼看出哪个源哑了
    statuses = [("AIHOT·主源", bool(items))] + chan_statuses
    status_line = "今日信源：" + " · ".join(
        "%s %s" % (n, "✓" if ok else "✗") for n, ok in statuses
    )
    window_human = f"自动生成于 {now.strftime('%Y-%m-%d %H:%M')}（北京时间）"

    html_doc = build_html(grouped, date_human, window_human, sources_note, status_line)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html_doc)

    total = sum(len(its) for _, its in grouped)
    print("[AI晨报] 生成完成 → %s（%d 条，%d 个分类）" % (OUT, total, len(grouped)))
    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
