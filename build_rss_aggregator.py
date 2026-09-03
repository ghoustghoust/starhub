# -*- coding: utf-8 -*-
"""Generate rss-aggregator.html — 多路 RSS 新闻源聚合阅读器。
构建时由 fetch_and_build.py 调用，Python 标准库抓取 RSS → 翻译 → 生成静态 HTML。

布局：卡片墙 + 抽屉阅读器（信源面板 | 卡片墙 | 阅读抽屉）
功能：信源分类筛选、全局搜索、标题/摘要翻译、响应式三端适配、主题切换。
"""
import html as html_mod
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

OUT = "rss-aggregator.html"
UA = {"User-Agent": "Mozilla/5.0 (starhub-rss-aggregator)"}

# ── 缓存配置 ──
TRANS_CACHE_FILE = "translations.json"
RSS_CACHE_FILE = "rss_cache.json"
RSS_CACHE_TTL = 1800  # RSS 缓存有效期：30 分钟

# ── 缓存数据 ──
_trans_cache = {}  # {text_hash: translated_text}
_rss_cache = {}    # {source_key: {"items": [...], "fetched_at": timestamp}}

# ── 72 小时内容累积 ──
RSS_HISTORY_FILE = "rss_history.json"
RSS_HISTORY_HOURS = 72
_rss_history = {}  # {link: {source, source_key, cat, color, title, title_zh, summary, summary_zh, pub_date, time_str}}

# ── 翻译统计 
_TRANS_STATS = {"google": 0, "bing": 0, "mymemory": 0, "dict": 0, "skip": 0, "fail": 0, "cache_hit": 0}


def _load_caches():
    """加载翻译和 RSS 缓存"""
    global _trans_cache, _rss_cache
    # 加载翻译缓存
    if os.path.exists(TRANS_CACHE_FILE):
        try:
            with open(TRANS_CACHE_FILE, "r", encoding="utf-8") as f:
                _trans_cache = json.load(f)
            print("[缓存] 加载翻译缓存: %d 条" % len(_trans_cache))
        except Exception as e:
            print("[缓存] 加载翻译缓存失败: %s" % e, file=sys.stderr)
    # 加载 RSS 缓存
    if os.path.exists(RSS_CACHE_FILE):
        try:
            with open(RSS_CACHE_FILE, "r", encoding="utf-8") as f:
                _rss_cache = json.load(f)
            print("[缓存] 加载 RSS 缓存: %d 个源" % len(_rss_cache))
        except Exception as e:
            print("[缓存] 加载 RSS 缓存失败: %s" % e, file=sys.stderr)


def _save_caches():
    """保存翻译和 RSS 缓存"""
    try:
        with open(TRANS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_trans_cache, f, ensure_ascii=False, indent=2)
        print("[缓存] 保存翻译缓存: %d 条" % len(_trans_cache))
    except Exception as e:
        print("[缓存] 保存翻译缓存失败: %s" % e, file=sys.stderr)
    try:
        with open(RSS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_rss_cache, f, ensure_ascii=False, indent=2)
        print("[缓存] 保存 RSS 缓存: %d 个源" % len(_rss_cache))
    except Exception as e:
        print("[缓存] 保存 RSS 缓存失败: %s" % e, file=sys.stderr)

def _load_history():
    """加载 72 小时文章历史"""
    global _rss_history
    if os.path.exists(RSS_HISTORY_FILE):
        try:
            with open(RSS_HISTORY_FILE, "r", encoding="utf-8") as f:
                _rss_history = json.load(f)
            print("[历史] 加载文章历史: %d 篇" % len(_rss_history))
        except Exception as e:
            print("[历史] 加载失败: %s" % e, file=sys.stderr)
            _rss_history = {}


def _save_history():
    """保存 72 小时文章历史"""
    try:
        with open(RSS_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_rss_history, f, ensure_ascii=False)
        print("[历史] 保存文章历史: %d 篇" % len(_rss_history))
    except Exception as e:
        print("[历史] 保存失败: %s" % e, file=sys.stderr)


def _accumulate_history(sources_with_items):
    """将新抓取的文章合并到 72 小时历史，裁剪过期内容，返回重组后的 sources_with_items"""
    now_bj = _now_bj()
    cutoff = now_bj.replace(tzinfo=None) - datetime.timedelta(hours=RSS_HISTORY_HOURS)

    # 合并新文章（按 link 去重，新数据覆盖旧数据）
    new_count = 0
    for src in sources_with_items:
        for it in src.get("items", []):
            link = it.get("link", "")
            if not link:
                continue
            pd_str = it.get("pub_date", "")
            # 解析日期：无日期或解析失败时使用当前时间作为回退
            pd_bj = now_bj.replace(tzinfo=None)  # 默认当前时间
            if pd_str:
                try:
                    pd = datetime.datetime.fromisoformat(pd_str)
                    if pd.tzinfo:
                        pd_bj = pd.astimezone(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
                    else:
                        pd_bj = pd
                except ValueError:
                    pass  # 保持默认当前时间
            if pd_bj < cutoff:
                continue  # 过期文章跳过
            _rss_history[link] = {
                "link": link,
                "source": src["name"], "source_key": src["key"],
                "cat": src["cat"], "color": src["color"],
                "title": it.get("title", ""), "title_zh": it.get("title_zh", ""),
                "summary": it.get("summary", ""), "summary_zh": it.get("summary_zh", ""),
                "pub_date": pd_str,
            }
            new_count += 1

    # 裁剪超过 72 小时的旧文章
    before = len(_rss_history)
    expired = []
    for link, item in _rss_history.items():
        pd_str = item.get("pub_date", "")
        pd_bj = now_bj.replace(tzinfo=None)  # 默认当前时间
        if pd_str:
            try:
                pd = datetime.datetime.fromisoformat(pd_str)
                if pd.tzinfo:
                    pd_bj = pd.astimezone(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
                else:
                    pd_bj = pd
            except ValueError:
                pass  # 保持默认当前时间
        if pd_bj < cutoff:
            expired.append(link)
    for link in expired:
        del _rss_history[link]

    # 按源重组，更新相对时间
    src_map = {}
    for src in sources_with_items:
        src_map[src["key"]] = {
            "key": src["key"], "name": src["name"],
            "cat": src["cat"], "color": src["color"], "items": [],
        }
    for item in _rss_history.values():
        sk = item["source_key"]
        if sk in src_map:
            entry = dict(item)
            entry["time_str"] = _fmt_rel_time(entry.get("pub_date"))
            src_map[sk]["items"].append(entry)

    result = list(src_map.values())
    total = sum(len(s["items"]) for s in result)
    pruned = before - len(_rss_history)
    print("[历史] 合并 %d 篇新文，裁剪 %d 篇过期，保留 %d 篇（%d 小时窗口）" % (
        new_count, pruned, len(_rss_history), RSS_HISTORY_HOURS))
    return result, total


def _save_api_snapshot(sources_with_items):
    """生成 API 快照 JSON，供 /api/rss 直接返回，避免实时抓取丢失历史累积数据"""
    snapshot_sources = []
    for src in sources_with_items:
        items = []
        for it in src.get("items", []):
            items.append({
                "t": it.get("title_zh", "") or it.get("title", ""),
                "u": it.get("link", "#"),
                "s": it.get("summary_zh", "") or it.get("summary", ""),
                "d": it.get("pub_date", ""),
            })
        snapshot_sources.append({
            "key": src["key"], "name": src["name"],
            "cat": src["cat"], "color": src["color"],
            "items": items,
        })
    snapshot = {
        "t": _now_bj().isoformat(),
        "sources": snapshot_sources,
    }
    try:
        with open("rss_api_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        total_items = sum(len(s["items"]) for s in snapshot_sources)
        print("[快照] 保存 API 快照: %d 源, %d 篇" % (len(snapshot_sources), total_items))
    except Exception as e:
        print("[快照] 保存失败: %s" % e, file=sys.stderr)


# ── RSS 信源配置（按分类组织，111 个精选源，已去除公众号/失效/重复/停更源） ─
RSS_SOURCES = [
    # ── 科技资讯 (14) ──
    {"key": "juliaevans_0", "name": "Julia Evans", "cat": "tech", "url": "https://jvns.ca/atom.xml", "color": "#e31937"},
    {"key": "overreacted_2", "name": "Overreacted", "cat": "tech", "url": "https://overreacted.io/rss.xml", "color": "#b31b1b"},
    {"key": "webdev_3", "name": "web.dev", "cat": "tech", "url": "https://web.dev/feed.xml", "color": "#000000"},
    {"key": "engadget_4", "name": "Engadget", "cat": "tech", "url": "http://www.engadget.com/rss.xml", "color": "#2563eb"},
    {"key": "joshcomeau_5", "name": "Josh Comeau", "cat": "tech", "url": "https://www.joshwcomeau.com/rss.xml", "color": "#7c3aed"},
    {"key": "hackernews_6", "name": "Hacker News", "cat": "tech", "url": "https://hnrss.org/frontpage", "color": "#ff6600"},
    {"key": "techcrunch_7", "name": "TechCrunch", "cat": "tech", "url": "https://techcrunch.com/feed/", "color": "#0a9e01"},
    {"key": "techcrunchai_7b", "name": "TechCrunch AI", "cat": "tech", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "color": "#d97706"},
    {"key": "theverge_8", "name": "The Verge", "cat": "tech", "url": "https://www.theverge.com/rss/index.xml", "color": "#e61919"},
    {"key": "thevergeai_8b", "name": "The Verge AI", "cat": "tech", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "color": "#dc2626"},
    {"key": "wired_10", "name": "WIRED", "cat": "tech", "url": "https://www.wired.com/feed/rss", "color": "#4f46e5"},
    {"key": "atlasnote_11", "name": "AtlasNote", "cat": "tech", "url": "https://atlasnote.ai/rss.xml", "color": "#ca8a04"},
    {"key": "redisblog_12", "name": "Redis Blog", "cat": "tech", "url": "https://redis.io/feed/", "color": "#0891b2"},
    {"key": "arxiv_cs_14", "name": "arXiv CS", "cat": "tech", "url": "https://export.arxiv.org/rss/cs", "color": "#b31b1b"},

    # ── 中文科技 (23) ──
    {"key": "美团技术团队_0", "name": "美团技术团队", "cat": "cn_tech", "url": "https://tech.meituan.com/feed", "color": "#0055ff"},
    {"key": "v2ex_1", "name": "V2EX", "cat": "cn_tech", "url": "https://v2ex.com/index.xml", "color": "#d7434e"},
    {"key": "机核_2", "name": "机核", "cat": "cn_tech", "url": "https://www.gcores.com/rss", "color": "#1a1a1a"},
    {"key": "solidot_3", "name": "Solidot", "cat": "cn_tech", "url": "https://www.solidot.org/index.rss", "color": "#0066ff"},
    {"key": "少数派_4", "name": "少数派", "cat": "cn_tech", "url": "https://sspai.com/feed", "color": "#336699"},
    {"key": "爱范儿_5", "name": "爱范儿", "cat": "cn_tech", "url": "https://www.ifanr.com/feed", "color": "#333333"},
    {"key": "小众软件_8", "name": "小众软件", "cat": "cn_tech", "url": "https://www.appinn.com/feed/", "color": "#FFD43B"},
    {"key": "构建被动收入_9", "name": "构建被动收入", "cat": "cn_tech", "url": "https://www.bmpi.dev/index.xml", "color": "#00bc74"},
    {"key": "虎嗅_11", "name": "虎嗅", "cat": "cn_tech", "url": "https://rsshub.bestblogs.dev/huxiu/article", "color": "#3a85ff"},
    {"key": "it之家_13", "name": "IT之家", "cat": "cn_tech", "url": "https://www.ithome.com/rss/", "color": "#1e88e5"},
    {"key": "月光博客_15", "name": "月光博客", "cat": "cn_tech", "url": "http://www.williamlong.info/rss.xml", "color": "#e53935"},
    {"key": "理想生活实验室_18", "name": "理想生活实验室", "cat": "cn_tech", "url": "https://www.toodaylab.com/feed", "color": "#43a047"},
    {"key": "游戏研究社_19", "name": "游戏研究社", "cat": "cn_tech", "url": "https://www.yystv.cn/rss/feed", "color": "#5c6bc0"},
    {"key": "潮流周刊_21", "name": "潮流周刊", "cat": "cn_tech", "url": "https://weekly.tw93.fun/rss.xml", "color": "#ff5722"},
    {"key": "扯氮集_25", "name": "扯氮集", "cat": "cn_tech", "url": "http://weiwuhui.com/feed", "color": "#6a1b9a"},
    {"key": "deepzz_26", "name": "Deepzz", "cat": "cn_tech", "url": "https://deepzz.com/feed", "color": "#d32f2f"},
    {"key": "mit科技评论_28", "name": "MIT科技评论", "cat": "cn_tech", "url": "https://plink.anyfeeder.com/mittrchina/hot", "color": "#0097a7"},
    {"key": "疯投圈_29", "name": "疯投圈", "cat": "cn_tech", "url": "https://crazy.capital/feed", "color": "#c62828"},
    {"key": "超能网_31", "name": "超能网", "cat": "cn_tech", "url": "https://plink.anyfeeder.com/expreview", "color": "#0091ea"},
    {"key": "钛媒体_38", "name": "钛媒体", "cat": "cn_tech", "url": "https://www.tmtpost.com/feed", "color": "#4a90d9"},
    {"key": "人人都是产品经理_39", "name": "人人都是产品经理", "cat": "cn_tech", "url": "https://www.woshipm.com/feed", "color": "#00aa55"},
    {"key": "cnbeta_41", "name": "cnBeta", "cat": "cn_tech", "url": "https://plink.anyfeeder.com/cnbeta", "color": "#00bc74"},
    {"key": "v2ex技术_44", "name": "V2EX技术", "cat": "cn_tech", "url": "https://www.v2ex.com/feed/tab/tech.xml", "color": "#3177cf"},

    # ── 开发者博客 (33) ──
    {"key": "阮一峰的网络日志_0", "name": "阮一峰的网络日志", "cat": "dev", "url": "https://www.ruanyifeng.com/blog/atom.xml", "color": "#dc382d"},
    {"key": "太隐_4", "name": "太隐", "cat": "dev", "url": "https://wangyurui.com/feed.xml", "color": "#336699"},
    {"key": "云风的blog_5", "name": "云风的BLOG", "cat": "dev", "url": "http://blog.codingnow.com/atom.xml", "color": "#4CAF50"},
    {"key": "胡涂说_6", "name": "胡涂说", "cat": "dev", "url": "https://hutusi.com/feed.xml", "color": "#8e44ad"},
    {"key": "程序员的喵_7", "name": "程序员的喵", "cat": "dev", "url": "https://catcoding.me/atom.xml", "color": "#6c5ce7"},
    {"key": "oldjblog_8", "name": "oldj blog", "cat": "dev", "url": "https://oldj.net/feed", "color": "#2d3436"},
    {"key": "randy'sblog_12", "name": "Randy's Blog", "cat": "dev", "url": "https://lutaonan.com/rss.xml", "color": "#e67e22"},
    {"key": "卡瓦邦噶_13", "name": "卡瓦邦噶", "cat": "dev", "url": "https://www.kawabangga.com/feed", "color": "#3498db"},
    {"key": "风雪之隅_14", "name": "风雪之隅", "cat": "dev", "url": "http://www.laruence.com/feed", "color": "#1abc9c"},
    {"key": "离别歌_15", "name": "离别歌", "cat": "dev", "url": "https://www.leavesongs.com/feed/", "color": "#9b59b6"},
    {"key": "hellogithub_17", "name": "HelloGitHub", "cat": "dev", "url": "http://hellogithub.com/rss", "color": "#16a085"},
    {"key": "张鑫旭_18", "name": "张鑫旭", "cat": "dev", "url": "https://www.zhangxinxu.com/wordpress/feed/", "color": "#f39c12"},
    {"key": "maxos_19", "name": "maxOS", "cat": "dev", "url": "https://maxoxo.me/rss/", "color": "#dc382d"},
    {"key": "轶哥博客_20", "name": "轶哥博客", "cat": "dev", "url": "https://www.wyr.me/rss.xml", "color": "#6366f1"},
    {"key": "geekplux_21", "name": "GeekPlux", "cat": "dev", "url": "https://geekplux.com/feed.xml", "color": "#e34f26"},
    {"key": "mactalk池建强_22", "name": "MacTalk池建强", "cat": "dev", "url": "http://macshuo.com/?feed=rss2", "color": "#663399"},
    {"key": "xuanwo_23", "name": "Xuanwo", "cat": "dev", "url": "https://xuanwo.io/index.xml", "color": "#336699"},
    {"key": "反斗限免_24", "name": "反斗限免", "cat": "dev", "url": "http://free.apprcn.com/feed/", "color": "#4CAF50"},
    {"key": "halfrost_25", "name": "Halfrost", "cat": "dev", "url": "http://halfrost.com/rss/", "color": "#8e44ad"},
    {"key": "infoq推荐_26", "name": "InfoQ推荐", "cat": "dev", "url": "https://plink.anyfeeder.com/infoq/recommend", "color": "#6c5ce7"},
    {"key": "二丫讲梵_27", "name": "二丫讲梵", "cat": "dev", "url": "https://wiki.eryajf.net/rss.xml", "color": "#2d3436"},
    {"key": "唐巧博客_28", "name": "唐巧博客", "cat": "dev", "url": "http://blog.devtang.com/atom.xml", "color": "#e74c3c"},
    {"key": "baiyun_30", "name": "BAI YUN", "cat": "dev", "url": "https://baiyun.me/feed", "color": "#2c3e50"},
    {"key": "elmagnifico_31", "name": "elmagnifico", "cat": "dev", "url": "http://elmagnifico.tech/feed.xml", "color": "#e67e22"},
    {"key": "tonybai_33", "name": "Tony Bai", "cat": "dev", "url": "http://tonybai.com/feed/", "color": "#1abc9c"},
    {"key": "全栈应用开发_36", "name": "全栈应用开发", "cat": "dev", "url": "https://www.phodal.com/blog/feeds/rss/", "color": "#16a085"},
    {"key": "tinyprojects_37", "name": "Tiny Projects", "cat": "dev", "url": "https://tinyprojects.dev/feed.xml", "color": "#f39c12"},
    {"key": "笨方法学写作_38", "name": "笨方法学写作", "cat": "dev", "url": "https://www.cnfeat.com/feed.xml", "color": "#dc382d"},
    {"key": "西秦公子_39", "name": "西秦公子", "cat": "dev", "url": "https://www.ixiqin.com/feed/", "color": "#6366f1"},
    {"key": "涛叔_41", "name": "涛叔", "cat": "dev", "url": "https://taoshu.in/feed.xml", "color": "#663399"},
    {"key": "小球飞鱼_42", "name": "小球飞鱼", "cat": "dev", "url": "https://mantyke.icu/index.xml", "color": "#336699"},
    {"key": "王登科dk_43", "name": "王登科DK", "cat": "dev", "url": "https://greatdk.com/feed", "color": "#4CAF50"},
    {"key": "小胡子哥_44", "name": "小胡子哥", "cat": "dev", "url": "http://www.barretlee.com/rss2.xml", "color": "#8e44ad"},
    {"key": "dbanotes_45", "name": "DBA Notes", "cat": "dev", "url": "http://dbanotes.net/feed", "color": "#6c5ce7"},

    # ── 技术社区 (12) ──
    {"key": "v2ex_all_50", "name": "V2EX 全站最新", "cat": "dev", "url": "https://www.v2ex.com/index.xml", "color": "#d7434e"},
    {"key": "v2ex_new_51", "name": "V2EX 最新", "cat": "dev", "url": "https://www.v2ex.com/feed/tab/all.xml", "color": "#e53935"},
    {"key": "v2ex_creative_52", "name": "V2EX 创意", "cat": "dev", "url": "https://www.v2ex.com/feed/tab/creative.xml", "color": "#8e24aa"},
    {"key": "v2ex_play_53", "name": "V2EX 好玩", "cat": "dev", "url": "https://www.v2ex.com/feed/tab/play.xml", "color": "#43a047"},
    {"key": "nodeseek_54", "name": "NodeSeek", "cat": "dev", "url": "https://rss.nodeseek.com/", "color": "#1e88e5"},
    {"key": "naixi_55", "name": "奶昔论坛", "cat": "dev", "url": "https://forum.naixi.net/forum.php?mod=rss", "color": "#f06292"},
    {"key": "hn_newest_56", "name": "Hacker News 最新", "cat": "dev", "url": "https://hnrss.org/newest", "color": "#ff6600"},
    {"key": "hn_ask_57", "name": "Hacker News Ask", "cat": "dev", "url": "https://hnrss.org/ask", "color": "#ef6c00"},
    {"key": "hn_show_58", "name": "Hacker News Show", "cat": "dev", "url": "https://hnrss.org/show", "color": "#f57c00"},
    {"key": "linuxdo_latest_59", "name": "LinuxDo 最新话题", "cat": "dev", "url": "https://linux.do/latest.rss", "color": "#2d8cff"},
    {"key": "linuxdo_top_60", "name": "LinuxDo 热门话题", "cat": "dev", "url": "https://linux.do/top.rss", "color": "#1a73e8"},
    {"key": "linuxdo_posts_61", "name": "LinuxDo 最新帖子", "cat": "dev", "url": "https://linux.do/posts.rss", "color": "#4a90d9"},

    # ── AI 日报 (20) ──
    {"key": "agihunt_0", "name": "AGI Hunt", "cat": "ai", "url": "https://agihunt.info/feed.xml", "color": "#6366f1"},
    {"key": "openai_blog_1", "name": "OpenAI 博客", "cat": "ai", "url": "https://openai.com/news/rss.xml", "color": "#10a37f"},
    {"key": "google_deepmind_2", "name": "Google DeepMind", "cat": "ai", "url": "https://deepmind.google/blog/rss.xml", "color": "#4285f4"},
    {"key": "google_ai_blog_3", "name": "Google AI Blog", "cat": "ai", "url": "https://blog.google/technology/ai/rss/", "color": "#34a853"},
    {"key": "arxiv_ai_4", "name": "arXiv AI", "cat": "ai", "url": "https://rss.arxiv.org/rss/cs.AI", "color": "#b31b1b"},
    {"key": "arxiv_ml_5", "name": "arXiv 机器学习", "cat": "ai", "url": "https://rss.arxiv.org/rss/cs.LG", "color": "#c62828"},
    {"key": "arxiv_nlp_6", "name": "arXiv NLP", "cat": "ai", "url": "https://rss.arxiv.org/rss/cs.CL", "color": "#d84315"},
    {"key": "hn_ai_7", "name": "Hacker News AI", "cat": "ai", "url": "https://hnrss.org/newest?q=AI", "color": "#ff6600"},
    {"key": "hn_llm_8", "name": "Hacker News LLM", "cat": "ai", "url": "https://hnrss.org/newest?q=LLM", "color": "#ef6c00"},
    {"key": "hn_openclaw_9", "name": "Hacker News OpenClaw", "cat": "ai", "url": "https://hnrss.org/newest?q=OpenClaw", "color": "#f57c00"},
    {"key": "google_research_10", "name": "Google Research Blog", "cat": "ai", "url": "https://research.google/blog/rss/", "color": "#4285f4"},
    {"key": "huggingface_11", "name": "Hugging Face 博客", "cat": "ai", "url": "https://huggingface.co/blog/feed.xml", "color": "#ffd21e"},
    {"key": "simonwillison_12", "name": "Simon Willison's Blog", "cat": "ai", "url": "https://simonwillison.net/atom/everything/", "color": "#5c6bc0"},
    {"key": "openclaw_rel_13", "name": "OpenClaw Releases", "cat": "ai", "url": "https://github.com/openclaw/openclaw/releases.atom", "color": "#7e57c2"},
    {"key": "openclaw_commits_14", "name": "OpenClaw Commits", "cat": "ai", "url": "https://github.com/openclaw/openclaw/commits/main.atom", "color": "#9575cd"},
    {"key": "codex_rel_15", "name": "OpenAI Codex Releases", "cat": "ai", "url": "https://github.com/openai/codex/releases.atom", "color": "#10a37f"},
    {"key": "claude_code_rel_16", "name": "Claude Code Releases", "cat": "ai", "url": "https://github.com/anthropics/claude-code/releases.atom", "color": "#d4a574"},
    {"key": "gemini_cli_rel_17", "name": "Gemini CLI Releases", "cat": "ai", "url": "https://github.com/google-gemini/gemini-cli/releases.atom", "color": "#4285f4"},
    {"key": "mcp_spec_rel_18", "name": "MCP Specification Releases", "cat": "ai", "url": "https://github.com/modelcontextprotocol/specification/releases.atom", "color": "#7c3aed"},
    {"key": "mcp_servers_rel_19", "name": "MCP Servers Releases", "cat": "ai", "url": "https://github.com/modelcontextprotocol/servers/releases.atom", "color": "#6d28d9"},

    # ── 科技媒体 (5) ──
    {"key": "arstechnica_60", "name": "Ars Technica", "cat": "tech", "url": "https://feeds.arstechnica.com/arstechnica/index", "color": "#ff6600"},
    {"key": "mit_tech_review_61", "name": "MIT Technology Review", "cat": "tech", "url": "https://www.technologyreview.com/feed/", "color": "#a50034"},
    {"key": "krebs_62", "name": "Krebs on Security", "cat": "tech", "url": "https://krebsonsecurity.com/feed/", "color": "#1a1a1a"},
    {"key": "thehackernews_63", "name": "The Hacker News", "cat": "tech", "url": "https://feeds.feedburner.com/TheHackersNews", "color": "#e53935"},
    {"key": "schneier_64", "name": "Schneier on Security", "cat": "tech", "url": "https://www.schneier.com/feed/", "color": "#37474f"},

    # ── 大厂技术博客 (12) ──
    {"key": "github_blog_70", "name": "GitHub Blog", "cat": "tech", "url": "https://github.blog/feed/", "color": "#24292e"},
    {"key": "github_changelog_71", "name": "GitHub Changelog", "cat": "tech", "url": "https://github.blog/changelog/feed/", "color": "#2d333b"},
    {"key": "github_copilot_72", "name": "GitHub Copilot Changelog", "cat": "tech", "url": "https://github.blog/changelog/label/copilot/feed/", "color": "#6e40c9"},
    {"key": "netflix_tech_73", "name": "Netflix Tech Blog", "cat": "tech", "url": "https://netflixtechblog.com/feed", "color": "#e50914"},
    {"key": "aws_blog_74", "name": "AWS Blog", "cat": "tech", "url": "https://aws.amazon.com/blogs/aws/feed/", "color": "#ff9900"},
    {"key": "cloudflare_blog_75", "name": "Cloudflare Blog", "cat": "tech", "url": "https://blog.cloudflare.com/rss/", "color": "#f38020"},
    {"key": "google_dev_76", "name": "Google Developers", "cat": "tech", "url": "https://developers.googleblog.com/feeds/posts/default/", "color": "#4285f4"},
    {"key": "mozilla_hacks_77", "name": "Mozilla Hacks", "cat": "tech", "url": "https://hacks.mozilla.org/feed/", "color": "#000000"},
    {"key": "vercel_blog_78", "name": "Vercel Blog", "cat": "tech", "url": "https://vercel.com/atom", "color": "#000000"},
    {"key": "supabase_blog_79", "name": "Supabase Blog", "cat": "tech", "url": "https://supabase.com/rss.xml", "color": "#3ecf8e"},
    {"key": "stripe_blog_80", "name": "Stripe Blog", "cat": "tech", "url": "https://stripe.com/blog/feed.rss", "color": "#635bff"},
    {"key": "meta_eng_81", "name": "Meta Engineering", "cat": "tech", "url": "https://engineering.fb.com/feed/", "color": "#0668E1"},

    # ── 技术周刊 (4) ──
    {"key": "js_weekly_90", "name": "JavaScript Weekly", "cat": "dev", "url": "https://javascriptweekly.com/rss/", "color": "#f7df1e"},
    {"key": "rust_weekly_91", "name": "This Week in Rust", "cat": "dev", "url": "https://this-week-in-rust.org/atom.xml", "color": "#dea584"},
    {"key": "golang_weekly_92", "name": "Golang Weekly", "cat": "dev", "url": "https://golangweekly.com/rss/", "color": "#00add8"},
    {"key": "bytebytego_93", "name": "ByteByteGo", "cat": "dev", "url": "https://blog.bytebytego.com/feed", "color": "#e53935"},

    # ── 综合新闻 (14) ──
    {"key": "idaily_1", "name": "iDaily", "cat": "news", "url": "https://plink.anyfeeder.com/idaily/today", "color": "#003399"},
    {"key": "中国日报双语_2", "name": "中国日报双语", "cat": "news", "url": "https://plink.anyfeeder.com/chinadaily/dual", "color": "#cc0000"},
    {"key": "知乎日报anyfeeder_3", "name": "知乎日报anyfeeder", "cat": "news", "url": "https://plink.anyfeeder.com/zhihu/daily", "color": "#d32f2f"},
    {"key": "法广中文_4", "name": "法广中文", "cat": "news", "url": "https://plink.anyfeeder.com/rfi/cn", "color": "#1a1a1a"},
    {"key": "bbc中文_5", "name": "BBC中文", "cat": "news", "url": "https://plink.anyfeeder.com/bbc/cn", "color": "#cc0000"},
    {"key": "财富中文网_6", "name": "财富中文网", "cat": "news", "url": "https://plink.anyfeeder.com/fortunechina", "color": "#d32f2f"},
    {"key": "澎湃新闻_7", "name": "澎湃新闻", "cat": "news", "url": "https://plink.anyfeeder.com/thepaper", "color": "#43a047"},
    {"key": "人民网_8", "name": "人民网", "cat": "news", "url": "https://plink.anyfeeder.com/people", "color": "#1565c0"},
    {"key": "南方周末anyfeeder_9", "name": "南方周末anyfeeder", "cat": "news", "url": "https://plink.anyfeeder.com/infzm/news", "color": "#0097a7"},
    {"key": "纽约时报中文网_10", "name": "纽约时报中文网", "cat": "news", "url": "http://cn.nytimes.com/rss/news.xml", "color": "#1a1a1a"},
    {"key": "喷嚏网铂程斋_11", "name": "喷嚏网铂程斋", "cat": "news", "url": "https://plink.anyfeeder.com/dapenti/xilei", "color": "#1565c0"},
    {"key": "雪球热帖_12", "name": "雪球热帖", "cat": "news", "url": "https://xueqiu.com/hots/topic/rss", "color": "#0097a7"},
    {"key": "bbc英语教学_13", "name": "BBC英语教学", "cat": "news", "url": "https://plink.anyfeeder.com/bbc/learningenglish", "color": "#bb1919"},
    {"key": "求是网_14", "name": "求是网", "cat": "news", "url": "https://plink.anyfeeder.com/qstheory", "color": "#6a1b9a"},

    # ── 播客 (6) ──
    {"key": "42章经_1", "name": "42章经", "cat": "podcast", "url": "https://feed.xyzfm.space/evgg6xle9rdc", "color": "#0891b2"},
    {"key": "三点下班_4", "name": "三点下班", "cat": "podcast", "url": "https://feed.xyzfm.space/tlel9j4tg3eu", "color": "#e11d48"},
    {"key": "卫诗婕商业漫谈_6", "name": "卫诗婕商业漫谈", "cat": "podcast", "url": "https://rsshub.bestblogs.dev/xiaoyuzhou/podcast/6627fda4b56459544087d86a", "color": "#7c3aed"},
    {"key": "得意忘形_7", "name": "得意忘形", "cat": "podcast", "url": "https://feed.xyzfm.space/klaak6nmc3ux", "color": "#0891b2"},
    {"key": "起朱楼宴宾客_8", "name": "起朱楼宴宾客", "cat": "podcast", "url": "https://rsshub.bestblogs.dev/xiaoyuzhou/podcast/65253bf350cf691d245b29aa", "color": "#1e40af"},
    {"key": "tedradiohour_10", "name": "TED Radio Hour", "cat": "podcast", "url": "https://feeds.npr.org/510298/podcast.xml", "color": "#e11d48"},

]

# 分类标签
CATEGORY_LABELS = {
    "ai":      "AI 日报",
    "tech":    "科技资讯",
    "cn_tech": "中文科技",
    "dev":     "开发者",
    "news":    "综合新闻",
    "podcast": "播客",
}

ITEMS_PER_SOURCE = 30
FETCH_TIMEOUT = 8
TRANSLATE_TIMEOUT = 4


# ──────────────────────────── 工具函数 ────────────────────────────

def _now_bj():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _esc(s):
    return html_mod.escape(str(s), quote=True)


def _strip_html(text):
    text = re.sub(r"<[^>]+>", "", text)
    # Use html.unescape to decode all HTML entities (&rsquo; &mdash; &hellip; etc.)
    text = html_mod.unescape(text)
    return text.strip()


def _truncate(s, maxlen=500):
    if not s:
        return ""
    s = s.strip()
    if len(s) <= maxlen:
        return s
    cut = -1
    for i, ch in enumerate(s[:maxlen]):
        if ch in "\u3002\uff01\uff1f\uff1b":
            cut = i
    if cut >= 30:
        return s[:cut + 1]
    return s[:maxlen] + "\u2026"


def _parse_iso(s):
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


_RSS_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
               "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

def _parse_rss_date(s):
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"\w+,\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s+(\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?", s)
    if m:
        day, mon, year, timestr, tz = int(m.group(1)), _RSS_MONTHS.get(m.group(2), 1), int(m.group(3)), m.group(4), m.group(5) or "+0000"
        h, mi, sec = map(int, timestr.split(":"))
        tz_sign = 1 if tz[0] == "+" else -1
        tz_h, tz_m = int(tz[1:3]), int(tz[3:5])
        tz_offset = datetime.timedelta(hours=tz_sign * tz_h, minutes=tz_sign * tz_m)
        try:
            return datetime.datetime(year, mon, day, h, mi, sec, tzinfo=datetime.timezone(tz_offset))
        except ValueError:
            return None
    return _parse_iso(s)


def _fmt_rel_time(dt):
    if dt is None:
        return ""
    # 缓存命中时 pub_date 已是 ISO 字符串（主流程会将其序列化），先反序列化
    if isinstance(dt, str):
        try:
            dt = datetime.datetime.fromisoformat(dt)
        except ValueError:
            return ""
    # Convert to Beijing time, handling both aware and naive datetimes
    if dt.tzinfo:
        bj = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
        bj_naive = bj.replace(tzinfo=None)
    else:
        bj_naive = dt
    now = _now_bj().replace(tzinfo=None)
    diff = now - bj_naive
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return bj_naive.strftime("%m-%d %H:%M")
    if seconds < 60:
        return "%d秒前" % seconds
    minutes = seconds // 60
    if minutes < 60:
        return "%d分钟前" % minutes
    hours = minutes // 60
    if hours < 24:
        return "%d小时前" % hours
    days = hours // 24
    if days < 30:
        return "%d天前" % days
    return bj_naive.strftime("%Y-%m-%d")


# ──────────────────────────── 翻译 ────────────────────────────

def _translate_to_zh(text, timeout=TRANSLATE_TIMEOUT):
    """四端点降级翻译链：Google → MyMemory → Google dict-chrome（带缓存）。"""
    if not text:
        return ""
    # 先清理 HTML 标签
    text = _strip_html(text)
    if not text:
        return ""
    # 如果已经是中文为主，跳过
    cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    if cn_chars > len(text) * 0.3:
        _TRANS_STATS["skip"] += 1
        return text

    # 查缓存（使用 MD5 确保跨进程一致性）
    text_hash = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()
    if text_hash in _trans_cache:
        _TRANS_STATS["cache_hit"] += 1
        return _trans_cache[text_hash]

    encoded = urllib.parse.quote(text[:500])

    # 1) Google gtx
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=" + encoded
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if data and data[0]:
            result = "".join(part[0] for part in data[0] if part[0])
            if result and len(result) > len(text) * 0.3:
                _TRANS_STATS["google"] += 1
                _trans_cache[text_hash] = result  # 写入缓存
                return result
    except Exception:
        pass

    # 2) MyMemory
    try:
        url = "https://api.mymemory.translated.net/get?q=" + encoded + "&langpair=en|zh-CN"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        result = data.get("responseData", {}).get("translatedText", "")
        if result and not result.startswith("MYMEMORY"):
            _TRANS_STATS["mymemory"] += 1
            _trans_cache[text_hash] = result  # 写入缓存
            return result
    except Exception:
        pass

    # 3) Google dict-chrome
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=dict-chrome&sl=auto&tl=zh-CN&q=" + encoded
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if data and data.get("sentences"):
            result = "".join(s.get("trans", "") for s in data["sentences"])
            if result:
                _TRANS_STATS["dict"] += 1
                _trans_cache[text_hash] = result  # 写入缓存
                return result
    except Exception:
        pass

    _TRANS_STATS["fail"] += 1
    return text  # 翻译失败保留原文


# ──────────────────────────── RSS 抓取 ────────────────────────────

def _fetch_url(url, timeout=FETCH_TIMEOUT, accept=None):
    headers = dict(UA)
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1000000).decode("utf-8", errors="replace")


def _fetch_rss(source):
    name = source["name"]
    url = source["url"]
    key = source["key"]

    # 检查缓存
    if key in _rss_cache:
        cached = _rss_cache[key]
        age = time.time() - cached.get("fetched_at", 0)
        if age < RSS_CACHE_TTL:
            print("[RSS聚合] %s 使用缓存 (%.0f秒前)" % (name, age))
            return cached.get("items", [])

    try:
        raw = _fetch_url(url, timeout=FETCH_TIMEOUT, accept="application/rss+xml, application/xml, text/xml, application/atom+xml")
    except Exception as ex:
        print("[RSS聚合] %s 拉取失败: %s" % (name, ex), file=sys.stderr)
        return []

    root = None
    raw_str = None
    # 尝试直接解析
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as ex:
        orig_err = ex
        # 仅在检测到未闭合 CDATA 时尝试回退
        raw_str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if "unclosed CDATA" in str(orig_err):
            try:
                text = raw_str.replace("<![CDATA[", "").replace("]]>", "")
                root = ET.fromstring(text)
            except ET.ParseError:
                pass  # 回退也失败，使用原始错误
        if root is None:
            print("[RSS聚合] %s 解析失败: %s" % (name, orig_err), file=sys.stderr)
            return []
    except Exception as ex:
        print("[RSS聚合] %s 异常: %s" % (name, ex), file=sys.stderr)
        return []

    items = []
    ns = "{http://www.w3.org/2005/Atom}"

    if root.tag.endswith("feed"):
        for e in root.findall(ns + "entry"):
            title = _strip_html(e.findtext(ns + "title") or "")
            link_el = e.find(ns + "link")
            link = (link_el.get("href") if link_el is not None else (e.findtext(ns + "id") or "")).strip()
            desc = _strip_html(e.findtext(ns + "summary") or e.findtext(ns + "content") or "")
            pub = e.findtext(ns + "updated") or e.findtext(ns + "published") or ""
            if not title or not link:
                continue
            items.append({
                "title": title, "link": link, "summary": _truncate(desc),
                "pub_date": _parse_iso(pub), "source": name, "source_key": source["key"],
                "cat": source["cat"],
            })
    else:
        ch = root.find("channel")
        if ch is None:
            for it in root.findall(".//item"):
                _parse_rss_item(it, name, source["key"], source["cat"], items)
        else:
            for it in ch.findall("item"):
                _parse_rss_item(it, name, source["key"], source["cat"], items)

    # 写入缓存
    _rss_cache[key] = {
        "items": items[:ITEMS_PER_SOURCE],
        "fetched_at": time.time()
    }

    return items[:ITEMS_PER_SOURCE]


def _parse_rss_item(it, source_name, source_key, cat, items):
    title = _strip_html(it.findtext("title") or "")
    link = (it.findtext("link") or "").strip()
    desc = _strip_html(it.findtext("description") or "")
    pub = (it.findtext("pubDate") or "").strip()
    if not title or not link:
        return
    items.append({
        "title": title, "link": link, "summary": _truncate(desc),
        "pub_date": _parse_rss_date(pub), "source": source_name, "source_key": source_key,
        "cat": cat,
    })


# ──────────────────────────── HTML 生成 ────────────────────────────

def _build_css():
    return """
:root {
  --bg:#faf9f7; --card:#fffdf9; --card-2:#f3efe6; --card-3:#ebe6db;
  --ink:#1c1917; --muted:#5f594c; --faint:#857e74;
  --line:#ddd6c9; --line-strong:#b9b0a2;
  --brand:#2f5d8a; --brand-strong:#24496e; --brand-line:#b9cde0; --brand-weak:#e7eef4;
  --display:"Noto Serif SC","Georgia","Times New Roman","Songti SC","SimSun","STSong",serif;
  --body:"Noto Sans SC",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --mono:"IBM Plex Mono","SF Mono","Fira Code","Consolas",monospace;
  --radius:8px;
  --shadow:0 1px 2px rgba(28,25,23,.05);
  --shadow-lift:0 10px 26px rgba(0,0,0,.10),0 2px 4px rgba(0,0,0,.06);
  --cat-tech:#2f5d8a; --cat-cn_tech:#c2434d; --cat-dev:#7052c9; --cat-ai:#b06a10; --cat-news:#8a6d1f; --cat-podcast:#2e7d5f;
}
[data-theme="dark"] {
  --bg:#161412; --card:#1d1a17; --card-2:#262019; --card-3:#2f2820;
  --ink:#ece7df; --muted:#a59d90; --faint:#98907f;
  --line:#37312a; --line-strong:#4a4339;
  --brand:#8fb3d9; --brand-strong:#b0cbe6; --brand-line:#3d5a78; --brand-weak:#22303f;
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-lift:0 10px 26px rgba(0,0,0,.5),0 2px 4px rgba(0,0,0,.4);
  --cat-tech:#8fb3d9; --cat-cn_tech:#e08790; --cat-dev:#a894e8; --cat-ai:#d3a15c; --cat-news:#cbb26a; --cat-podcast:#6cba9c;
}
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
html { scroll-behavior:smooth; }
body { font-family:var(--body); background:var(--bg); color:var(--ink); line-height:1.55; font-size:14px; -webkit-font-smoothing:antialiased; }
a { color:inherit; text-decoration:none; }
button { font-family:inherit; cursor:pointer; border:none; background:none; color:inherit; }

/* ── Header ── */
header { position:sticky; top:0; z-index:40; background:rgba(250,249,247,.94); backdrop-filter:blur(10px); border-bottom:1px solid var(--line); }
[data-theme="dark"] header { background:rgba(22,20,18,.94); }
.hd { max-width:1560px; margin:0 auto; padding:9px 20px; display:flex; align-items:center; gap:12px; }
.hd .logo { display:flex; align-items:center; gap:8px; flex:none; font-family:var(--display); font-weight:900; font-size:16px; }
.hd .logo .sub { font-size:11px; color:var(--muted); font-weight:400; margin-left:2px; font-family:var(--body); }
.hd .nav-links { display:flex; align-items:center; gap:2px; flex:1; }
.hd .nav-links a { display:inline-flex; align-items:center; gap:5px; white-space:nowrap; padding:5px 12px; border-radius:999px; font-size:12.5px; font-weight:500; border:1px solid transparent; transition:all .15s; }
.hd .nav-links a:hover { background:var(--card); border-color:var(--line); }
.hd .nav-links a.active { background:var(--brand-weak); border-color:var(--brand-line); color:var(--brand-strong); font-weight:600; }
.hd .nav-links a .icon { width:13px; height:13px; }
.theme-btn { width:30px; height:30px; border-radius:999px; background:var(--card); border:1px solid var(--line); display:flex; align-items:center; justify-content:center; flex:none; transition:all .15s; }
.theme-btn:hover { border-color:var(--brand-line); }
.theme-btn svg { width:14px; height:14px; }

/* ── Toolbar ── */
.toolbar { max-width:1560px; margin:0 auto; padding:14px 20px 4px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.toolbar h1 { font-family:var(--display); font-size:19px; font-weight:900; margin-right:2px; }
.src-btn { display:inline-flex; align-items:center; gap:6px; padding:5px 13px; border-radius:999px; font-size:12.5px; font-weight:600; border:1px solid var(--brand-line); background:var(--brand-weak); color:var(--brand-strong); transition:all .15s; }
.src-btn:hover { background:var(--brand-line); color:#fff; }
.src-btn svg { width:13px; height:13px; }
.src-btn .cnt { font-family:var(--mono); font-size:10px; opacity:.8; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { padding:4px 12px; border-radius:999px; font-size:12px; font-weight:500; border:1px solid var(--line); background:var(--card); color:var(--muted); transition:all .15s; white-space:nowrap; }
.chip:hover { border-color:var(--line-strong); color:var(--ink); }
.chip.on { background:var(--brand-weak); border-color:var(--brand-line); color:var(--brand-strong); font-weight:600; }
.chip .n { font-family:var(--mono); font-size:10px; opacity:.75; margin-left:3px; }
.fpill { display:inline-flex; align-items:center; gap:6px; padding:4px 6px 4px 12px; border-radius:999px; font-size:12px; font-weight:600; background:var(--ink); color:var(--bg); }
.fpill .x { width:16px; height:16px; border-radius:999px; background:rgba(255,255,255,.18); display:flex; align-items:center; justify-content:center; font-size:11px; cursor:pointer; }
.fpill .x:hover { background:rgba(255,255,255,.34); }
.tool-meta { margin-left:auto; font-family:var(--mono); font-size:11px; color:var(--faint); white-space:nowrap; }

/* ── Card wall ── */
.wall-wrap { max-width:1560px; margin:0 auto; padding:14px 20px 60px; }
.wall { columns:4 300px; column-gap:14px; }
.card { break-inside:avoid; margin-bottom:14px; background:var(--card); border:1px solid var(--line); border-radius:var(--radius); padding:15px 17px 12px; cursor:pointer; position:relative; transition:box-shadow .18s, border-color .18s, transform .18s; }
.card:hover { border-color:var(--brand-line); box-shadow:var(--shadow-lift); transform:translateY(-2px); }
.card.open { border-color:var(--brand); box-shadow:0 0 0 1px var(--brand-line); }
.card-top { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
.cat-tag { font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
.cat-tag::before { content:""; display:inline-block; width:7px; height:7px; border-radius:2px; margin-right:5px; vertical-align:1px; background:var(--cc); }
.card-time { font-family:var(--mono); font-size:10.5px; color:var(--faint); margin-left:auto; }
.ext-btn { flex:none; width:22px; height:22px; border-radius:6px; display:flex; align-items:center; justify-content:center; color:var(--faint); border:1px solid transparent; transition:all .15s; }
.ext-btn:hover { color:var(--brand-strong); border-color:var(--brand-line); background:var(--brand-weak); }
.ext-btn svg { width:11px; height:11px; }
.card-title { font-family:var(--display); font-size:15.5px; font-weight:700; line-height:1.45; margin-bottom:7px; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; transition:color .15s; }
.card:hover .card-title { color:var(--brand-strong); }
.card-summary { font-size:12.5px; color:var(--muted); line-height:1.7; display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
.card-foot { display:flex; align-items:center; gap:6px; margin-top:11px; padding-top:9px; border-top:1px solid var(--line); font-size:11px; color:var(--faint); }
.src-dot { width:8px; height:8px; border-radius:999px; flex:none; background:var(--sc); }
[data-theme="dark"] .src-dot { filter:brightness(1.7) saturate(.85); }
.src-name { font-weight:600; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.foot-meta { margin-left:auto; font-family:var(--mono); font-size:10.5px; display:flex; gap:8px; align-items:center; white-space:nowrap; }
.no-ft { font-size:10px; color:var(--faint); border:1px dashed var(--line-strong); border-radius:4px; padding:0 5px; }
.card.visited .card-title { color:var(--faint); }
.card.visited .card-title::after { content:"\\5df2 \\8bfb"; font-family:var(--body); font-size:9px; font-weight:600; color:var(--faint); border:1px solid var(--line-strong); border-radius:4px; padding:0 4px; margin-left:6px; vertical-align:2px; }
.pod-chip { display:inline-flex; align-items:center; gap:4px; font-size:10.5px; color:var(--cat-podcast); background:color-mix(in srgb, var(--cat-podcast) 10%, transparent); border-radius:4px; padding:1px 6px; font-weight:600; }
.empty-hint { text-align:center; color:var(--faint); font-size:13px; padding:60px 0; line-height:2; }
.load-more-wrap { text-align:center; padding:30px 0 10px; }
.load-more-btn { display:inline-flex; align-items:center; gap:8px; padding:10px 36px; border-radius:999px; font-size:13px; font-weight:600; border:1px solid var(--brand-line); background:var(--brand-weak); color:var(--brand-strong); cursor:pointer; transition:all .2s; }
.load-more-btn:hover { background:var(--brand-line); color:#fff; transform:translateY(-1px); box-shadow:var(--shadow-lift); }
.load-more-progress { font-family:var(--mono); font-size:11px; color:var(--faint); margin-top:10px; }

/* ── Source panel (left drawer) ── */
.src-panel { position:fixed; top:0; left:0; bottom:0; width:min(340px,90vw); z-index:80; background:var(--card); border-right:1px solid var(--line); transform:translateX(-103%); transition:transform .28s cubic-bezier(.32,.72,.28,1); display:flex; flex-direction:column; box-shadow:18px 0 50px rgba(0,0,0,.12); }
body.src-open .src-panel { transform:none; }
.sp-head { flex:none; padding:14px 14px 10px; border-bottom:1px solid var(--line); }
.sp-head .row { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
.sp-head h2 { font-family:var(--display); font-size:15px; font-weight:900; flex:1; }
.sp-close { width:26px; height:26px; border-radius:999px; border:1px solid var(--line); display:flex; align-items:center; justify-content:center; color:var(--muted); transition:all .15s; }
.sp-close:hover { border-color:var(--line-strong); color:var(--ink); }
.sp-close svg { width:12px; height:12px; }
.sp-search { display:flex; align-items:center; gap:6px; padding:6px 10px; border-radius:8px; background:var(--bg); border:1px solid var(--line); }
.sp-search svg { width:12px; height:12px; color:var(--faint); flex:none; }
.sp-search input { border:0; background:transparent; outline:none; font-size:12px; color:var(--ink); font-family:var(--body); width:100%; }
.sp-search input::placeholder { color:var(--faint); }
.sp-list { flex:1; overflow-y:auto; padding-bottom:20px; }
.sp-all { display:flex; align-items:center; gap:8px; padding:9px 16px; font-size:13px; font-weight:600; cursor:pointer; color:var(--muted); border-left:3px solid transparent; transition:all .1s; }
.sp-all:hover { background:var(--bg); color:var(--ink); }
.sp-all.on { color:var(--brand-strong); background:var(--brand-weak); border-left-color:var(--brand); }
.sp-all .n { margin-left:auto; font-family:var(--mono); font-size:10.5px; color:var(--faint); }
.sp-cat { position:sticky; top:0; background:var(--card); padding:10px 16px 4px; font-size:10.5px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--cc); display:flex; align-items:center; gap:5px; cursor:pointer; user-select:none; }
.sp-cat .arrow { font-size:9px; color:var(--faint); transition:transform .15s; }
.sp-cat.folded .arrow { transform:rotate(-90deg); }
.sp-cat .n { margin-left:auto; font-family:var(--mono); font-size:10px; color:var(--faint); }
.sp-cat-body { }
.sp-src { display:flex; align-items:center; gap:8px; padding:7px 16px; font-size:12.5px; cursor:pointer; color:var(--muted); border-left:3px solid transparent; transition:all .1s; }
.sp-src:hover { background:var(--bg); color:var(--ink); }
.sp-src.on { color:var(--brand-strong); background:var(--brand-weak); border-left-color:var(--brand); font-weight:600; }
.sp-src .nm { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sp-src .n { margin-left:auto; font-family:var(--mono); font-size:10.5px; color:var(--faint); flex:none; }
.sp-src .src-dot { width:9px; height:9px; }
.sp-none { padding:14px 16px; font-size:12px; color:var(--faint); }

/* ── Scrim ── */
.scrim { position:fixed; inset:0; background:rgba(28,25,23,.45); opacity:0; pointer-events:none; transition:opacity .25s; z-index:60; }
body.reading .scrim, body.src-open .scrim { opacity:1; pointer-events:auto; }

/* ── Reader drawer (right) ── */
.reader2 { position:fixed; top:0; right:0; bottom:0; width:min(760px,100%); z-index:70; background:var(--bg); border-left:1px solid var(--line); box-shadow:-18px 0 50px rgba(0,0,0,.16); transform:translateX(103%); transition:transform .3s cubic-bezier(.32,.72,.28,1); display:flex; flex-direction:column; }
body.reading .reader2 { transform:none; }
.r2-top { flex:none; display:flex; align-items:center; gap:10px; padding:10px 18px; border-bottom:1px solid var(--line); background:var(--card); position:relative; }
.r2-progress { position:absolute; left:0; bottom:-1px; height:2px; background:var(--brand); width:0%; transition:width .1s linear; }
.r2-back { display:flex; align-items:center; gap:5px; font-size:12.5px; font-weight:600; color:var(--muted); padding:5px 10px 5px 6px; border-radius:999px; border:1px solid transparent; white-space:nowrap; transition:all .15s; }
.r2-back:hover { color:var(--ink); border-color:var(--line); background:var(--bg); }
.r2-back svg { width:14px; height:14px; }
.r2-src { display:flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); min-width:0; }
.r2-src .src-dot { width:9px; height:9px; }
.r2-src b { color:var(--ink); font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.r2-acts { margin-left:auto; display:flex; align-items:center; gap:8px; }
.seg { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; background:var(--bg); }
.seg button { padding:4px 13px; font-size:12px; font-weight:500; color:var(--muted); white-space:nowrap; transition:all .15s; }
.seg button.on { background:var(--brand-weak); color:var(--brand-strong); font-weight:600; }
.seg button[disabled] { opacity:.4; cursor:not-allowed; }
.r2-open { display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:600; color:var(--brand-strong); background:var(--brand-weak); border:1px solid var(--brand-line); padding:4px 12px; border-radius:8px; white-space:nowrap; transition:all .15s; }
.r2-open:hover { background:var(--brand-line); color:#fff; }
.r2-body { flex:1; overflow-y:auto; }
.r2-inner { max-width:680px; margin:0 auto; padding:30px 34px 80px; }
.r2-title { font-family:var(--display); font-size:23px; font-weight:900; line-height:1.42; margin-bottom:12px; }
.r2-title a { color:var(--ink); }
.r2-title a:hover { color:var(--brand-strong); }
.r2-meta { display:flex; align-items:center; gap:10px; font-size:12px; color:var(--faint); padding-bottom:16px; margin-bottom:22px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.r2-meta .src-dot { width:9px; height:9px; }
.r2-meta .cat { font-weight:700; letter-spacing:.06em; font-size:10.5px; text-transform:uppercase; color:var(--cc); }
.r2-summary { font-size:15px; line-height:1.9; color:var(--ink); }
.r2-summary .lead { font-size:16.5px; line-height:1.85; font-weight:500; margin-bottom:1em; }
.r2-lang-toggle { display:inline-flex; align-items:center; gap:0; margin-top:16px; border-radius:var(--radius); overflow:hidden; border:1px solid var(--line); font-size:12px; }
.r2-lang-toggle button { padding:4px 12px; border:none; background:transparent; color:var(--muted); cursor:pointer; transition:all .15s; font-family:var(--body); font-size:12px; }
.r2-lang-toggle button.active { background:var(--brand); color:#fff; }
.r2-lang-toggle button:hover:not(.active) { background:var(--brand-weak); }
.fallback-card { border:1px dashed var(--line-strong); border-radius:10px; padding:22px; text-align:center; background:var(--card); margin-top:6px; }
.fallback-card .fb-ico { font-size:22px; margin-bottom:8px; }
.fallback-card p { font-size:13px; color:var(--muted); margin-bottom:16px; line-height:1.7; }
.fb-btn { display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:600; padding:9px 20px; border-radius:8px; background:var(--brand-weak); color:var(--brand-strong); border:1px solid var(--brand-line); transition:all .15s; }
.fb-btn:hover { background:var(--brand-line); color:#fff; }
.r2-foot-hint { text-align:center; font-family:var(--mono); font-size:10.5px; color:var(--faint); padding:26px 0 6px; border-top:1px solid var(--line); margin-top:34px; }

/* ── Footer ── */
.footer { text-align:center; padding:6px 0; font-size:11px; color:var(--faint); font-family:var(--mono); border-top:1px solid var(--line); background:var(--bg); }

/* ── Build bar ── */
.build-bar { position:sticky; top:45px; z-index:30; background:var(--bg); max-width:1560px; margin:0 auto; padding:2px 20px 8px; display:flex; align-items:center; gap:10px; font-family:var(--mono); font-size:11px; color:var(--faint); border-bottom:1px solid var(--line); }

/* ── Responsive ── */
@media (max-width:900px) {
  .hd .logo .sub { display:none; }
  .toolbar { padding:12px 14px 2px; }
  .wall-wrap { padding:12px 14px 50px; }
  .r2-inner { padding:22px 20px 70px; }
  .tool-meta { display:none; }
  .build-bar { padding:2px 14px 6px; }
}
@media (max-width:700px) {
  .hd .nav-links a { padding:5px 9px; font-size:12px; }
  .chips { overflow-x:auto; flex-wrap:nowrap; max-width:100%; padding-bottom:4px; }
  .chip { white-space:nowrap; flex:none; }
  .r2-top { padding:8px 12px; }
  .r2-back span { display:none; }
  .r2-src b { max-width:110px; }
  .r2-open { padding:4px 9px; }
}
"""


def _build_header():
    return """
<header>
  <div class="hd">
    <a class="logo" href="index.html">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 11a7 7 0 0 1 14 0"/><path d="M4 11v4a2 2 0 0 0 2 2h1a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1H4"/><path d="M18 11v4a2 2 0 0 1-2 2h-1a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h3"/></svg>
      <span>StarHub<span class="sub">GitHub 收藏台</span></span>
    </a>
    <nav class="nav-links">
      <a href="index.html">收藏池</a>
      <a href="ai-daily.html">AI 晨报</a>
      <a href="rss-aggregator.html" class="active">RSS 聚合</a>
    </nav>
    <button class="theme-btn" id="btnTheme" title="切换主题">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
    </button>
  </div>
</header>
"""


def _build_js(sources_with_items, build_ts_ms=0):
    """Generate core JS for card wall + drawer reader."""
    data_json = json.dumps(sources_with_items, ensure_ascii=False)
    cat_labels_json = json.dumps(CATEGORY_LABELS, ensure_ascii=False)
    return """
<script>
(function(){
  var BUILD_TS = """ + str(int(build_ts_ms)) + """;
  var SOURCES = """ + data_json + """;
  var CAT_LABELS = """ + cat_labels_json + """;

  /* ── Data ── */
  var CAT_ORDER = ['ai','tech','cn_tech','dev','news','podcast'];
  var ART = [];
  SOURCES.forEach(function(s){
    s.items.forEach(function(it){
      ART.push({t:it.title_zh||it.title, s:it.summary_zh||it.summary||'',
        src:s.name, sk:s.key, c:s.cat, sc:s.color,
        time:it.time_str, date:it.pub_date, u:it.link||'#'});
    });
  });
  ART.sort(function(a,b){ return (b.date||'').localeCompare(a.date||''); });
  interleaveArts();
  function estRead(a){ return Math.max(1,Math.round((a.s||'').length/90))+' min'; }

  /* ── 信源轮询交错排序：round-robin 避免高频信源垄断 ── */
  function interleaveArts(){
    var buckets={}, keys=[];
    ART.forEach(function(a){
      if(!buckets[a.sk]){buckets[a.sk]=[];keys.push(a.sk);}
      buckets[a.sk].push(a);
    });
    keys.forEach(function(k){
      buckets[k].sort(function(a,b){return(b.date||'').localeCompare(a.date||'');});
    });
    var ptrs={}, i;
    for(i=0;i<keys.length;i++) ptrs[keys[i]]=0;
    var result=[];
    while(true){
      var added=false;
      for(i=0;i<keys.length;i++){
        var k=keys[i];
        if(ptrs[k]<buckets[k].length){
          result.push(buckets[k][ptrs[k]++]);
          added=true;
        }
      }
      if(!added)break;
    }
    ART=result;
  }

  /* ── State ── */
  var visited = {};
  try { visited = JSON.parse(localStorage.getItem('rss_read_v2')||'{}'); } catch(e){}
  var filter = {type:'all', cat:null, src:null};
  var curArt = null, rMode = 'summary';
  var wallLimit = 120, WALL_STEP = 80;

  /* ── Theme ── */
  var themeKey='wb_starhub_theme_v1';
  var t=localStorage.getItem(themeKey)||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  document.documentElement.dataset.theme=t;
  var btn=document.getElementById('btnTheme');
  if(btn) btn.onclick=function(){
    var nt=document.documentElement.dataset.theme==='dark'?'light':'dark';
    try{localStorage.setItem(themeKey,nt);}catch(e){}
    document.documentElement.dataset.theme=nt;
  };

  /* ── Toolbar ── */
  function renderChips(){
    var counts={}; ART.forEach(function(a){counts[a.c]=(counts[a.c]||0)+1;});
    var h='';
    CAT_ORDER.forEach(function(c){
      if(!counts[c]) return;
      var label=CAT_LABELS[c]||c, on=filter.type==='cat'&&filter.cat===c;
      h+='<button class="chip'+(on?' on':'')+'" data-c="'+c+'">'+label+' <span class="n">'+counts[c]+'</span></button>';
    });
    document.getElementById('chips').innerHTML=h;
    document.querySelectorAll('.chip').forEach(function(el){
      el.onclick=function(){
        var c=this.dataset.c;
        if(filter.type==='cat'&&filter.cat===c) filter={type:'all'};
        else filter={type:'cat',cat:c,src:null};
        curArt=null; wallLimit=WALL_STEP; renderChips(); renderWall(); renderPanel(); window.scrollTo({top:0});
      };
    });
    var pw=document.getElementById('fpillWrap');
    if(filter.type==='src'){
      var s=SRC_OBJ(filter.src);
      pw.innerHTML=s?'<button class="fpill"><span class="src-dot" style="--sc:'+s.color+'"></span>'+esc(s.name)+'<span class="x" onclick="clearSrcF(event)">\u2715</span></button>':'';
    } else pw.innerHTML='';
    document.getElementById('srcCnt').textContent=SOURCES.length;
    var ftN=ART.filter(function(a){return (a.s||'').length>60;}).length;
    document.getElementById('toolMeta').textContent=ART.length+' \u7bc7 \u00b7 \u5168\u6587\u8986\u76d6 '+ftN+'/'+ART.length;
  }
  function SRC_OBJ(k){ return SOURCES.find(function(s){return s.key===k;}); }
  window.clearSrcF=function(e){e.stopPropagation();filter={type:'all'};curArt=null;wallLimit=WALL_STEP;renderChips();renderWall();renderPanel();};

  /* ── Source panel ── */
  function toggleSrcPanel(){ document.body.classList.toggle('src-open'); }
  window.toggleSrcPanel=toggleSrcPanel;
  function selectSrc(key){
    if(!key){filter={type:'all'};} else {filter={type:'src',src:key};}
    curArt=null; wallLimit=WALL_STEP; document.body.classList.remove('src-open');
    renderChips(); renderWall(); renderPanel(); window.scrollTo({top:0});
  }
  window.selectSrc=selectSrc;
  function renderPanel(){
    var q=(document.getElementById('spSearch').value||'').trim().toLowerCase();
    var h='<div class="sp-all'+(filter.type!=='src'?' on':'')+'" onclick="selectSrc(null)">\u2630 \u5168\u90e8\u4fe1\u6e90<span class="n">'+ART.length+'</span></div>';
    var byCat={};
    SOURCES.forEach(function(s){
      if(!q||s.name.toLowerCase().indexOf(q)>=0){
        var cnt=s.items.length;
        if(cnt>0)(byCat[s.cat]=byCat[s.cat]||[]).push(s);
      }
    });
    var any=false;
    CAT_ORDER.forEach(function(c){
      var arr=byCat[c]; if(!arr||!arr.length) return; any=true;
      var label=CAT_LABELS[c]||c;
      h+='<div class="sp-cat" data-cat="'+c+'"><span class="arrow">\u25bc</span>'+label+'<span class="n">'+arr.length+'</span></div>';
      h+='<div class="sp-cat-body" data-body="'+c+'">';
      arr.forEach(function(s){
        var on=filter.type==='src'&&filter.src===s.key;
        h+='<div class="sp-src'+(on?' on':'')+'" data-k="'+s.key+'"><span class="src-dot" style="--sc:'+s.color+'"></span><span class="nm">'+esc(s.name)+'</span><span class="n">'+s.items.length+'</span></div>';
      });
      h+='</div>';
    });
    if(!any) h+='<div class="sp-none">\u6ca1\u6709\u5339\u914d\u300c'+esc(q)+'\u300d\u7684\u4fe1\u6e90</div>';
    document.getElementById('spList').innerHTML=h;
    document.querySelectorAll('.sp-cat').forEach(function(el){
      el.onclick=function(){
        this.classList.toggle('folded');
        var body=document.querySelector('.sp-cat-body[data-body="'+this.dataset.cat+'"]');
        if(body) body.style.display=this.classList.contains('folded')?'none':'';
      };
    });
    document.querySelectorAll('.sp-src').forEach(function(el){
      el.onclick=function(){ selectSrc(this.dataset.k); };
    });
  }

  /* ── Card wall ── */
  function visibleArts(){
    return ART.filter(function(a){
      if(filter.type==='cat') return a.c===filter.cat;
      if(filter.type==='src') return a.sk===filter.src;
      return true;
    });
  }
  function artKey(a){ return a.sk+'|'+(a.u&&a.u!=='#'?a.u:a.t); }
  function renderWall(){
    var list=visibleArts(), wall=document.getElementById('wall');
    if(!list.length){
      wall.innerHTML='<div class="empty-hint">\u8be5\u7b5b\u9009\u4e0b\u6ca1\u6709\u6587\u7ae0</div>';
      return;
    }
    var start=wall.querySelectorAll('.card').length;
    var shouldReset = start===0 || wallLimit<=WALL_STEP;
    if(shouldReset){
      wall.innerHTML=''; start=0;
    }
    var end=Math.min(start===0?wallLimit:start+WALL_STEP, list.length);
    if(start===0) wallLimit=Math.min(wallLimit,list.length);
    var h='';
    for(var i=start;i<end;i++){
      var a=list[i], k=artKey(a), isVis=!!visited[k];
      var isOpen=curArt&&artKey(curArt)===k;
      h+='<article class="card'+(isVis?' visited':'')+(isOpen?' open':'')+'" data-k="'+esc(k)+'" style="--cc:var(--cat-'+a.c+')">';
      h+='<div class="card-top"><span class="cat-tag" style="color:var(--cat-'+a.c+')">'+(CAT_LABELS[a.c]||a.c)+'</span>';
      h+='<span class="card-time">'+esc(a.time)+'</span>';
      h+='<span class="ext-btn" title="\u539f\u7ad9"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg></span></div>';
      h+='<h3 class="card-title">'+esc(a.t)+'</h3>';
      if(a.s) h+='<p class="card-summary">'+esc(a.s)+'</p>';
      h+='<div class="card-foot"><span class="src-dot" style="--sc:'+a.sc+'"></span><span class="src-name">'+esc(a.src)+'</span>';
      h+='<span class="foot-meta"><span>'+estRead(a)+'</span></span></div>';
      h+='</article>';
    }
    if(end<list.length){
      h+='<div class="load-more-wrap" id="loadMoreWrap">';
      h+='<button class="load-more-btn" id="loadMoreBtn">\u52a0\u8f7d\u66f4\u591a \u2193</button>';
      h+='<div class="load-more-progress">'+end+' / '+list.length+' \u7bc7</div>';
      h+='</div>';
    }
    if(start===0) wall.innerHTML=h;
    else {
      var old=wall.querySelector('#loadMoreWrap'); if(old)old.remove();
      wall.insertAdjacentHTML('beforeend',h);
    }
    wallLimit=end;
  }
  /* 事件委托：一次性绑定，增量追加无需重新绑定 */
  document.getElementById('wall').addEventListener('click',function(e){
    var card=e.target.closest('.card'); if(!card)return;
    var k=card.dataset.k;
    var a=ART.find(function(x){return artKey(x)===k;});
    if(!a) return;
    if(e.target.closest('.ext-btn')){markRead(a);window.open(a.u,'_blank');return;}
    openReader(a);
  });
  function loadMore(){
    var list=visibleArts();
    if(wallLimit>=list.length)return;
    var old=wallLimit; wallLimit=Math.min(wallLimit+WALL_STEP,list.length);
    renderWall();
  }
  window.loadMore=loadMore;

  /* ── Reader ── */
  function markRead(a){visited[artKey(a)]=1;try{localStorage.setItem('rss_read_v2',JSON.stringify(visited));}catch(e){}}
  function openReader(a){
    curArt=a; markRead(a); rMode=(a.s||'').length>60?'summary':'summary';
    renderReader(); document.body.classList.add('reading');
    document.body.classList.remove('src-open');
    document.getElementById('r2Body').scrollTop=0; renderWall();
  }
  window.closeReader=function(){document.body.classList.remove('reading');curArt=null;renderWall();};
  window.closeOverlays=function(){document.body.classList.remove('src-open');window.closeReader();};
  function renderReader(){
    var a=curArt; if(!a) return;
    document.getElementById('r2Src').innerHTML='<span class="src-dot" style="--sc:'+a.sc+'"></span><b>'+esc(a.src)+'</b><span>\u00b7</span><span>'+esc(a.time)+'</span>';
    var openEl=document.getElementById('r2Open'); openEl.href=a.u;
    var segBtns=document.querySelectorAll('#r2Seg button');
    segBtns.forEach(function(b){
      b.classList.toggle('on',b.dataset.m===rMode);
      b.onclick=function(){rMode=this.dataset.m;renderReader();};
    });
    var h='<h1 class="r2-title">'+esc(a.t)+'</h1>';
    h+='<div class="r2-meta" style="--cc:var(--cat-'+a.c+')"><span class="cat">'+(CAT_LABELS[a.c]||a.c)+'</span>';
    h+='<span class="src-dot" style="--sc:'+a.sc+'"></span><span>'+esc(a.src)+'</span>';
    h+='<span>\u00b7</span><span>'+esc(a.time)+'</span><span>\u00b7</span><span>'+estRead(a)+'</span></div>';
    if(a.s){
      h+='<div class="r2-summary"><p>'+esc(a.s)+'</p></div>';
      if(!isMostlyZh(a.s)){
        h+='<div class="r2-lang-toggle">';
        h+='<button class="active" id="btnOrig">\u539f\u6587</button>';
        h+='<button id="btnTrans">\u7ffb\u8bd1</button></div>';
      }
    } else {
      h+='<div class="fallback-card"><div class="fb-ico">🔗</div>';
      h+='<p>\u8be5\u6587\u7ae0\u6682\u65e0\u6458\u8981<br>\u53ef\u524d\u5f80\u539f\u7ad9\u7ee7\u7eed\u9605\u8bfb</p>';
      h+='<a class="fb-btn" href="'+esc(a.u)+'" target="_blank" rel="noopener">\u539f\u7ad9 \u2197</a></div>';
    }
    h+='<div class="r2-foot-hint">J / K \u6216 \u2190 \u2192 \u5207\u6362\u6587\u7ae0 \u00b7 ESC \u8fd4\u56de</div>';
    document.getElementById('r2Inner').innerHTML=h;
    var btnT=document.getElementById('btnTrans');
    if(btnT) btnT.onclick=function(){
      var el=document.querySelector('.r2-summary p');
      if(!el) return; el.textContent='\u7ffb\u8bd1\u4e2d\u2026';
      _clientTranslate(a.s,function(tr){
        var cur=document.querySelector('.r2-summary p');
        if(cur) cur.textContent=tr;
      });
    };
  }
  document.getElementById('r2Body').addEventListener('scroll',function(){
    var el=this,max=el.scrollHeight-el.clientHeight;
    document.getElementById('r2Progress').style.width=(max>0?el.scrollTop/max*100:0)+'%';
  });

  /* ── Client translate ── */
  var _ctCache={},_ctPend={};
  function _clientTranslate(text,cb){
    if(!text||isMostlyZh(text)){cb(text);return;}
    var k=text.substring(0,100);
    if(_ctCache[k]){cb(_ctCache[k]);return;}
    if(_ctPend[k]){_ctPend[k].push(cb);return;}
    _ctPend[k]=[cb];
    var url='https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q='+encodeURIComponent(text.substring(0,500));
    fetch(url).then(function(r){return r.json();}).then(function(d){
      var res='';if(d&&d[0])for(var i=0;i<d[0].length;i++)if(d[0][i]&&d[0][i][0])res+=d[0][i][0];
      var tr=(res&&res.length>text.length*0.3)?res:text;
      _ctCache[k]=tr;var p=_ctPend[k]||[];delete _ctPend[k];p.forEach(function(f){f(tr);});
    }).catch(function(){
      _ctCache[k]=text;var p=_ctPend[k]||[];delete _ctPend[k];p.forEach(function(f){f(text);});
    });
  }

  // ── Window exports ──
  window.closeReader = function(){ document.body.classList.remove('reading'); curArt=null; renderWall(); };
  window.closeOverlays = function(){ document.body.classList.remove('src-open'); window.closeReader(); };
  window.clearSrcF = function(e){ e.stopPropagation(); filter={type:'all'}; curArt=null; renderChips(); renderWall(); renderPanel(); };
  window.toggleSrcPanel = toggleSrcPanel;
  window.selectSrc = selectSrc;

  /* ── Keyboard nav ── */
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape'){window.closeOverlays();return;}
    if(e.target.tagName==='INPUT') return;
    var order=visibleArts();
    if(!curArt){if(e.key==='j'||e.key==='ArrowRight'){if(order[0])openReader(order[0]);}return;}
    var pos=-1;
    for(var i=0;i<order.length;i++){if(artKey(order[i])===artKey(curArt)){pos=i;break;}}
    if(e.key==='j'||e.key==='ArrowRight'){if(pos<order.length-1)openReader(order[pos+1]);}
    if(e.key==='k'||e.key==='ArrowLeft'){if(pos>0)openReader(order[pos-1]);}
  });

  /* ── Helpers ── */
  function esc(s){var d=document.createElement('div');d.appendChild(document.createTextNode(s||''));return d.innerHTML;}
  function adjColor(hex){
    if(document.documentElement.dataset.theme!=='dark')return hex;
    var m=/^#?([0-9a-fA-F]{6})$/.exec(hex||'');if(!m)return hex;
    var n=parseInt(m[1],16),r=(n>>16)&255,g=(n>>8)&255,b=n&255;
    var lum=(0.299*r+0.587*g+0.114*b)/255;
    if(lum>=0.35)return hex;
    r=Math.round(r+(255-r)*0.45);g=Math.round(g+(255-g)*0.45);b=Math.round(b+(255-b)*0.45);
    return '#'+((1<<24)+(r<<16)+(g<<8)+b).toString(16).slice(1);
  }
  function isMostlyZh(s){
    if(!s)return true;var c=0,n=0;
    for(var i=0;i<s.length;i++){var ch=s.charCodeAt(i);if(ch>=0x4e00&&ch<=0x9fff)c++;if(ch>32)n++;}
    return n===0||c/n>0.2;
  }

  /* ── URL hash ── */
  function updateHash(){
    var p='#view='+(filter.type==='cat'?'cat&c='+filter.cat:filter.type==='src'?'src&s='+encodeURIComponent(filter.src):'all');
    try{history.replaceState(null,'',p);}catch(e){}
  }
  function restoreFromHash(){
    var h=(location.hash||'').replace(/^#/,'');if(!h)return false;
    var p={};h.split('&').forEach(function(kv){var s=kv.split('=');if(s[0])p[s[0]]=decodeURIComponent(s[1]||'');});
    if(p.view==='cat'&&p.c){filter={type:'cat',cat:p.c};return true;}
    if(p.view==='src'&&p.s){filter={type:'src',src:p.s};return true;}
    return false;
  }

  /* ── Init ── */
  var restored = restoreFromHash();
  renderChips(); renderWall(); renderPanel();
  /* 无限滚动：接近底部自动加载更多 */
  window.addEventListener('scroll',function(){
    var list=visibleArts();
    if(wallLimit>=list.length)return;
    var wrap=document.querySelector('.wall-wrap');
    if(!wrap)return;
    if(wrap.getBoundingClientRect().bottom<window.innerHeight*3){
      loadMore();
    }
  },{passive:true});
  var relEl = document.getElementById('buildRel');
  if(relEl && BUILD_TS){
    var mins=Math.max(0,Math.round((Date.now()-BUILD_TS)/60000));
    relEl.textContent=(mins<60?mins+' \u5206\u949f\u524d':mins<1440?Math.round(mins/60)+' \u5c0f\u65f6\u524d':Math.round(mins/1440)+' \u5929\u524d');
  }

  /* ── Relative time formatter ── */
  function _fmtRelTime(dt) {
    if (!dt) return '';
    var d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    var mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
    return mins < 60 ? mins + ' 分钟前' : mins < 1440 ? Math.round(mins/60) + ' 小时前' : Math.round(mins/1440) + ' 天前';
  }

  /* ── Live RSS update (API-First) ── */
  var liveEl = document.getElementById('liveStatus');
  if(liveEl) liveEl.textContent='\u00b7 \u52a0\u8f7d\u4e2d\u2026';
  (function(){
    var ctrl=new AbortController();
    var tid=setTimeout(function(){ctrl.abort();},30000);
    fetch('https://starhub-refresh.vercel.app/api/rss',{signal:ctrl.signal}).then(function(r){
      clearTimeout(tid);if(!r.ok)throw new Error('API '+r.status);return r.json();
    }).then(function(data){
      if(!data.sources)return;
      // 用实时数据替换静态构建数据
      data.sources.forEach(function(live){
        if(!live.items||!live.items.length)return;
        var src=SOURCES.find(function(s){return s.key===live.key;});
        if(src){
          // API 返回缩写字段：t=title, u=url, s=summary, d=date
          live.items.forEach(function(it){
            it.title = it.t || it.title;
            it.link = it.u || it.link;
            it.summary = it.s || it.summary;
            it.pub_date = it.d || it.pub_date;
            it.title_zh = it.title;
            it.summary_zh = it.summary || '';
            it.time_str = _fmtRelTime(it.pub_date);
          });
          src.items = live.items;
        }
      });
      // 重建 ART 数组
      ART=[];
      SOURCES.forEach(function(s){s.items.forEach(function(it){
        ART.push({t:it.title_zh||it.title,s:it.summary_zh||it.summary||'',
          src:s.name,sk:s.key,c:s.cat,sc:s.color,
          time:it.time_str,date:it.pub_date,u:it.link||'#'});
      });});
      ART.sort(function(a,b){return(b.date||'').localeCompare(a.date||'');});
      interleaveArts();
      // 重新渲染
      wallLimit=WALL_STEP;renderChips();renderWall();renderPanel();
      if(liveEl){var now=new Date();liveEl.textContent='\u2713 \u5b9e\u65f6\u6570\u636e '+now.getHours().toString().padStart(2,'0')+':'+now.getMinutes().toString().padStart(2,'0');}
    }).catch(function(e){
      if(liveEl)liveEl.textContent='\u00b7 \u9759\u6001\u6784\u5efa\u6570\u636e';
    });
  })();
})();
</script>
"""


def build_html(sources_with_items, build_time, total_items, build_ts_ms=0):
    """生成完整 HTML 页面 — 卡片墙 + 抽屉阅读器。"""
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@700;900&family=Noto+Sans+SC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">\n'
        '<title>RSS 聚合阅读器 · StarHub</title>\n'
        '<style>' + _build_css() + '</style>\n'
        '</head>\n<body>\n'
        + _build_header() +
        '<div class="toolbar">\n'
        '<h1>时间线</h1>\n'
        '<button class="src-btn" onclick="toggleSrcPanel()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h13M4 18h9"/></svg> 信源 <span class="cnt" id="srcCnt"></span></button>\n'
        '<div class="chips" id="chips"></div>\n'
        '<span id="fpillWrap"></span>\n'
        '<span class="tool-meta" id="toolMeta"></span>\n'
        '</div>\n'
        '<div class="build-bar">\u81ea\u52a8\u751f\u6210\u4e8e ' + _esc(build_time) + '\uff08\u5317\u4eac\u65f6\u95f4\uff09\u00b7 \u5171 ' + str(total_items) + ' \u7bc7 \u00b7 <span id="buildRel"></span><span id="liveStatus"></span></div>\n'
        '<div class="wall-wrap"><div class="wall" id="wall"></div></div>\n'
        '<div class="scrim" onclick="closeOverlays()"></div>\n'
        '<aside class="src-panel" id="srcPanel">\n'
        '<div class="sp-head"><div class="row"><h2>信源</h2>\n'
        '<button class="sp-close" onclick="toggleSrcPanel()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>\n'
        '</div><label class="sp-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>\n'
        '<input id="spSearch" placeholder="搜索信源…" autocomplete="off"></label></div>\n'
        '<div class="sp-list" id="spList"></div></aside>\n'
        '<aside class="reader2" id="reader2">\n'
        '<div class="r2-top"><button class="r2-back" onclick="closeReader()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M19 12H5M11 18l-6-6 6-6"/></svg><span>返回</span></button>\n'
        '<span class="r2-src" id="r2Src"></span>\n'
        '<div class="r2-acts"><div class="seg" id="r2Seg"><button data-m="summary">快览</button><button data-m="full">全文</button></div>\n'
        '<a class="r2-open" id="r2Open" href="#" target="_blank" rel="noopener">原站 ↗</a></div>\n'
        '<div class="r2-progress" id="r2Progress"></div></div>\n'
        '<div class="r2-body" id="r2Body"><div class="r2-inner" id="r2Inner"></div></div></aside>\n'
        + _build_js(sources_with_items, build_ts_ms) +
        '</body>\n</html>'
    )


# ──────────────────────────── Main ────────────────────────────

def main():
    now = _now_bj()
    build_time = now.strftime("%Y-%m-%d %H:%M")
    # 构建时间 UTC 毫秒时间戳（供页脚相对时间）
    build_ts_ms = now.timestamp() * 1000

    # 加载缓存
    _load_caches()
    _load_history()

    sources_with_items = []
    total_items = 0
    ok_count = 0

    # 串行抓取 RSS（短超时，失败快速跳过）
    for src in RSS_SOURCES:
        items = _fetch_rss(src)
        n = len(items)
        if n > 0:
            ok_count += 1

        # 翻译标题和摘要
        for it in items:
            it["title_zh"] = _translate_to_zh(it["title"]) if it["title"] else it["title"]
            it["summary_zh"] = _translate_to_zh(it.get("summary", "")) if it.get("summary") else ""
            it["time_str"] = _fmt_rel_time(it.get("pub_date"))
            # 保留 pub_date 用于前端时间线排序（转为 ISO 字符串）
            pd = it.get("pub_date")
            if pd and hasattr(pd, 'isoformat'):
                it["pub_date"] = pd.isoformat()

        src_data = {
            "key": src["key"], "name": src["name"], "cat": src["cat"],
            "color": src["color"], "items": items,
        }
        sources_with_items.append(src_data)
        total_items += n
        print("[RSS聚合] %s: %d 条" % (src["name"], n))

    if total_items == 0:
        print("[RSS聚合] 所有源均失败，尝试使用历史数据", file=sys.stderr)

    # 累积到 72 小时历史，用累积数据替换当次抓取
    sources_with_items, total_items = _accumulate_history(sources_with_items)

    # 生成 API 快照（供 /api/rss 直接返回，避免实时抓取丢失历史累积数据）
    _save_api_snapshot(sources_with_items)

    html_doc = build_html(sources_with_items, build_time, total_items, build_ts_ms)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html_doc)

    # 生成 rss_sources.json（供 /api/rss 使用）
    sources_json = json.dumps([{"key": s["key"], "name": s["name"], "cat": s["cat"],
        "color": s["color"], "url": s["url"]} for s in RSS_SOURCES], ensure_ascii=False)
    with open("rss_sources.json", "w", encoding="utf-8") as f:
        f.write(sources_json)

    print("[RSS聚合] 生成完成 → %s（%d 源成功，共 %d 篇）" % (OUT, ok_count, total_items))

    # 打印翻译统计
    print("[翻译统计] 缓存命中: %d, Google: %d, MyMemory: %d, Dict: %d, 跳过: %d, 失败: %d" % (
        _TRANS_STATS["cache_hit"], _TRANS_STATS["google"], _TRANS_STATS["mymemory"],
        _TRANS_STATS["dict"], _TRANS_STATS["skip"], _TRANS_STATS["fail"]
    ))

    # 保存缓存
    _save_caches()
    _save_history()

    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
