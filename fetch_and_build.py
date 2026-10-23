#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Star 收藏台 —— 自动更新脚本
在 GitHub Actions 中每天运行：拉取 ghoustghoust 的 star 列表 → 智能分类 → 重新生成 index.html。
仅依赖 Python 标准库，无需安装第三方包。
"""
import hashlib
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
from starhub_pipeline.enrich.embed import embed_star_entries  # noqa: E402
from starhub_pipeline.sources.github import fetch_stars  # noqa: E402

# Fast path for CI while the refactored pipeline is being built: skip
# translation/LLM/embedding enrichment instead of waiting on free endpoints.
FAST = os.environ.get("STARHUB_FAST") == "1"

USER = site_value("github_user", "ghoustghoust")
PAGES_URL = site_value("pages_url", "https://ghoustghoust.github.io/starhub/")

CATS = [
    {"key": "agent",     "label": "AI Agent & Skills",      "color": "#0550ae", "dark": "#4493f8"},
    {"key": "assistant", "label": "AI 助手 & 应用",         "color": "#1b7c83", "dark": "#39c5cf"},
    {"key": "coding",    "label": "AI 编程 & 工具链",       "color": "#1a7f37", "dark": "#3fb950"},
    {"key": "video",     "label": "AI 视频创作",            "color": "#cf222e", "dark": "#f85149"},
    {"key": "content",   "label": "内容创作 & 排版",        "color": "#d33982", "dark": "#e577b2"},
    {"key": "distill",   "label": "思维蒸馏 & 认知",        "color": "#8250df", "dark": "#a371f7"},
    {"key": "learning",  "label": "学习 & 教程",            "color": "#b58400", "dark": "#d4a72c"},
    {"key": "info",      "label": "资讯聚合 & 信息抓取",    "color": "#0969da", "dark": "#58a6ff"},
    {"key": "tools",     "label": "效率工具",               "color": "#57606a", "dark": "#8b949e"},
    {"key": "media",     "label": "影音 & IPTV",            "color": "#bf3989", "dark": "#db61a2"},
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

DEFAULT_FAVS = []

# 已知的空描述项目，补充一句中文说明
FALLBACK_DESC = {
    "llazyl/TVBox": "TVBox 影视聚合播放器",
    "q215613905/TVBoxOS": "TVBoxOS 影视播放系统",
    "FongMi/TV": "基于 media3/ffmpeg/mpv 的开源影视播放器",
}


def has_cn(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


# 翻译熔断：连续 5 次全端点失败后暂停翻译请求 5 分钟（避免上游故障时的请求风暴与构建拖长）
_TRANS_FAIL_STREAK = 0
_TRANS_BLOCK_UNTIL = 0.0


def translate_to_zh(text):
    """把英文简介翻译成中文；全部端点失败返回 None（保留原文）。
    熔断保护：连续多次全端点失败后暂停请求，期间直接返回 None（调用方保留原文）。"""
    global _TRANS_FAIL_STREAK, _TRANS_BLOCK_UNTIL
    if FAST:
        return None
    if time.time() < _TRANS_BLOCK_UNTIL:
        return None
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
            _TRANS_FAIL_STREAK = 0
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
            _TRANS_FAIL_STREAK = 0
            return result
    except Exception:  # noqa: BLE001
        pass
    _TRANS_FAIL_STREAK += 1
    if _TRANS_FAIL_STREAK >= 5:
        _TRANS_BLOCK_UNTIL = time.time() + 300
        print("[翻译熔断] 连续 %d 次全端点失败，暂停翻译请求 5 分钟" % _TRANS_FAIL_STREAK, file=sys.stderr)
    return None


def classify_new(fn, desc, lang, topics):
    """对未知新项目做关键词规则分类（已有项目走 known_categories.json 保持稳定）。
    规则要点：避免泛词子串误伤（如「蒸馏」「思维」「chat」），用语义更明确的短语。
    点名式补丁词已删（查表与 LLM 接管）；末尾的 "agent" 仅代表"规则未命中"，
    统一入口 classify_repo 会把它收口为默认 tools。"""
    text = (fn + " " + (desc or "") + " " + " ".join(topics or [])).lower()
    # 不再用裸 "quant"：它会子串误伤 quantization（NVIDIA/Model-Optimizer 实锤进 finance），
    # 模型量化归入 coding；真·量化交易项目仍由 trading/金融/交易 等通用词覆盖。
    if any(k in text for k in ["trading", "finance", "金融", "交易", "bloomberg"]):
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
                               "learning", "入门", "weekly", "实践", "tutorial",
                               "dive-into"]):
        return "learning"
    if any(k in text for k in ["code-review", "quantization", "中转", "coding", "编程"]):
        return "coding"
    if any(k in text for k in ["chatbot", "chatgpt", "assistant", "助手", "librechat", "astrbot",
                               "workspace", "desktop", "agent-os"]):
        return "assistant"
    if any(k in text for k in ["opc", "one-person", "一人公司", "创业", "startup", "growth", "增长",
                               "business", "软著", "copyright", "专利", "patent", "合规",
                               "compliance", "legal"]):
        return "business"
    return "agent"


# ==================== LLM 智能分类（Agnes，§8.14 key 池惯例）====================
_LLM_API_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
_LLM_MODEL = "agnes-2.5-flash"
_LLM_TIMEOUT_SEC = 30
_NOTE_MAX_CHARS = 30

# 13 类判定准则（2026-10-04 定版，用户裁决 FDE 归 learning/tools 两档）。
# 背景：此前 prompt 只给 key=label 裸表，语义边界题（FDE/awesome 合集/书籍）方差极大——
# 实测 FDE 15 仓散 8 类、awesome 14 仓散 7 类。注入准则后 classify_llm 与
# tools/reclassify_all.py（同函数）两条路径共用同一把尺子。
# 总原则：**内容形态优先于主题**——同一主题，可运行软件归能力类，资料归 learning，资讯归 info。
_CAT_CRITERIA = """判定准则（与类目标签冲突时以本准则为准）：
- agent：Agent 框架/运行时/编排/多智能体系统/MCP 服务端等【可运行软件】。Agent 主题的教程/书/清单不归此类。
- coding：AI 编程助手/CLI 工具/开发工具链/脚手架/代码生成。
- learning：教程/路线图/学习指南/实战手册/书籍/wiki/课程/面试题/知识库型 README。FDE、Agent、RAG 等任何主题的"学习资料"一律归此类。
- info：资讯/周刊/Newsletter/博客/新闻聚合/资源导航/awesome 合集/导航站/书签集。
- tools：通用效率工具/下载器/解析器/转换器/文件处理/浏览器插件等实用工具（不限 AI）。
- assistant：面向最终用户的 AI 助手/聊天客户端/桌面或移动 AI 应用。
- video：AI 视频生成/剪辑/数字人/口播/字幕/图像生成工作流（ComfyUI/SD/Flux）。
- media：播客/音频/TTS/语音识别/音乐生成与处理。
- content：内容创作与排版/写作辅助/公众号运营/文档排版/营销素材。
- distill：思维模型/认知方法论/决策框架/个人知识管理/笔记方法。
- business：一人公司/独立开发变现/商业化方法论/知识产权/出海。
- finance：量化交易/投资/股票/加密货币/金融数据。
- frontend：前端框架/组件库/设计系统/UI 工程。
总裁决：①内容形态优先于主题（软件→能力类，资料→learning，资讯合集→info）；②awesome/合集/导航一律 info；③教程属性压过工具属性——讲"怎么用 X"的仓库跟 X 走，讲"怎么学 X"的归 learning。"""


def _agnes_key_pool():
    """AGNES_API_KEY（主，可逗号分隔）+ AGNES_API_KEYS（逗号分隔附加）合并成 key 池（去重保序）。"""
    keys = [k.strip() for k in (os.environ.get("AGNES_API_KEY") or "").split(",") if k.strip()]
    keys += [k.strip() for k in (os.environ.get("AGNES_API_KEYS") or "").split(",") if k.strip()]
    pool, seen = [], set()
    for k in keys:
        if k not in seen:
            seen.add(k)
            pool.append(k)
    return pool


def _parse_llm_json(content):
    """解析模型回复里的 JSON 对象；容忍 ```json 围栏与前后寒暄，失败返回 None。"""
    if not content:
        return None
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    # 直接解析失败时退回正则抽最外层 {...}（容忍 JSON 前后的寒暄文字）
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def classify_llm(fn, desc, lang, topics, cats):
    """LLM 智能分类：成功返回 {"category": "<类目key>", "note": "<≤30字中文点评>"}，失败返回 None。

    Agnes chat completions（URL/headers/payload 形状对齐 build_daily_insight 的 _LLM 惯例，
    不 import 该文件）。单次尝试不重试不阻塞（构建侧熔断兜底由后续任务接入）：
    - key 池先建后判空，池空直接 None、一个请求都不发；
    - 429 换池内下一个 key（单轮轮转，全限流则 None）；
    - 401/403 key 无效：换 key 无意义，立即停；
    - payload 带 enable_thinking:false，要求模型只回 JSON，解析失败返回 None。"""
    pool = _agnes_key_pool()
    if not pool:
        return None
    valid_keys = {c["key"] for c in (cats or [])}
    cats_desc = "；".join("%s=%s" % (c["key"], c["label"]) for c in (cats or []))
    payload = {
        "model": _LLM_MODEL,
        "messages": [
            {"role": "system", "content": (
                "你是 GitHub 仓库分类器。根据仓库信息从给定类目中选一个最合适的类目key，"
                "并用不超过%d字的中文给一句点评。只输出一个 JSON 对象，"
                '格式：{"category": "<类目key>", "note": "<点评>"}，'
                "禁止解释、禁止 markdown 代码块。可选类目key：%s\n%s"
                % (_NOTE_MAX_CHARS, cats_desc, _CAT_CRITERIA))},
            {"role": "user", "content": (
                "仓库名：%s\n简介：%s\n主语言：%s\nTopics：%s"
                % (fn, desc or "（无）", lang or "（未知）", "、".join(topics or []) or "（无）"))},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = json.dumps(payload).encode("utf-8")
    for key in pool:
        req = urllib.request.Request(
            _LLM_API_URL, data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_SEC) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                continue  # 限流：换下一个 key（单轮，不等待）
            if exc.code in (401, 403):
                return None  # key 无效：直接停
            print("[classify_llm] HTTP %s" % exc.code, file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001
            print("[classify_llm] 请求失败: %s" % e, file=sys.stderr)
            return None
        parsed = _parse_llm_json(content)
        if parsed is None:
            print("[classify_llm] 回复解析失败: %s" % (content[:80],), file=sys.stderr)
            return None
        cat = str(parsed.get("category") or "").strip()
        if not cat or (valid_keys and cat not in valid_keys):
            return None  # 类目 key 必须落在给定类目内，否则交给规则兜底
        note = str(parsed.get("note") or "").strip()[:_NOTE_MAX_CHARS]
        return {"category": cat, "note": note}
    return None  # 池内所有 key 都被限流


# LLM 分类熔断：连续失败暂停（与翻译熔断同款形状）——LLM 侧故障不得拖垮构建
_LLM_FAIL_STREAK = 0
_LLM_BLOCK_UNTIL = 0.0
_LLM_FAIL_LIMIT = 3
_LLM_COOLDOWN = 300


def _llm_available():
    """熔断窗口内返回 False（跳过 LLM 直走规则）。"""
    return time.time() >= _LLM_BLOCK_UNTIL


def _llm_record(ok):
    """记录一次 LLM 分类成败；连续失败达限触发暂停，成功即清零。"""
    global _LLM_FAIL_STREAK, _LLM_BLOCK_UNTIL
    if ok:
        _LLM_FAIL_STREAK = 0
        return
    _LLM_FAIL_STREAK += 1
    if _LLM_FAIL_STREAK >= _LLM_FAIL_LIMIT:
        _LLM_BLOCK_UNTIL = time.time() + _LLM_COOLDOWN
        print("[LLM熔断] 连续 %d 次分类失败，暂停 LLM 分类 %d 分钟（期间走规则兜底）"
              % (_LLM_FAIL_STREAK, _LLM_COOLDOWN // 60), file=sys.stderr)


def classify_repo(fn, desc, lang, topics, known):
    """统一分类入口（重分类工具与快车道复用）：查表 → LLM → 规则 → 默认 ("tools", "")。

    - 查表命中直接返回（known_categories.json 的存量映射保持稳定，不烧 LLM）；
    - LLM 结果采信条件：返回 dict 且 category 落在 CATS 内；
    - 规则兜底用 classify_new，其 "agent" 返回仅代表"规则未命中"，这里按 SPEC T1
      收口为默认 tools（agent 只能由查表/LLM 给出）；
    - 任何异常不向上抛：单个仓库的分类失败绝不拖垮构建。"""
    try:
        cat = (known or {}).get(fn)
        if cat:
            return (cat, "")
        if not FAST and _llm_available():
            llm = classify_llm(fn, desc, lang, topics, CATS)
            _llm_record(bool(llm and llm.get("category")))
            if llm and llm.get("category"):
                return (llm["category"], (llm.get("note") or "")[:_NOTE_MAX_CHARS])
        cat = classify_new(fn, desc, lang, topics)
        if cat != "agent":
            return (cat, "")
        return ("tools", "")
    except Exception as e:  # noqa: BLE001
        print("[classify_repo] %s 分类异常: %s" % (fn, e), file=sys.stderr)
        return ("tools", "")


def health_score(repo):
    """收藏健康分（github-search-mirror 四维借鉴，数据全部来自已拉取字段）：
    活跃度=pushed_at 距今（≤90 天 green / ≤365 yellow / 其余 red）、
    安全=archived 直接 red、社区=stars+forks 降档、文档=desc+topics 降档。
    缺 pushed_at 或解析异常 → unknown（不渲染徽章）；stale = 距今 >365 天。"""
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




def _agnes_generate(system, user, max_tokens=300):
    """通用 Agnes 生成（单轮 key 池轮转，形状与 classify_llm 一致）；失败返回 None。"""
    pool = _agnes_key_pool()
    if not pool:
        return None
    payload = {
        "model": _LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = json.dumps(payload).encode("utf-8")
    for key in pool:
        req = urllib.request.Request(
            _LLM_API_URL, data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % key},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_SEC) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"] or ""
            return content.strip() or None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                continue
            print("[agnes_generate] HTTP %s" % exc.code, file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001
            print("[agnes_generate] 请求失败: %s" % e, file=sys.stderr)
            return None
    return None


_GUIDES_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "category_guides.json")


def build_category_guides(cats, known, cache_path=None):
    """类目 AI 导览：每个类目按成员清单生成 2-3 句选型导览。
    成员清单 md5 未变的类目直接吃 category_guides.json 缓存（13 次调用只在类目内容变化时发生）。
    LLM 失败的类目跳过（guide 缺省，前端静默不渲染）；永不抛出、永不阻塞构建。"""
    if FAST:
        return {}
    cache_path = cache_path or _GUIDES_CACHE_PATH
    cache = {}
    try:
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
    except Exception:  # noqa: BLE001
        cache = {}
    by_cat = {}
    for fn, cat in (known or {}).items():
        by_cat.setdefault(cat, []).append(fn)
    changed = False
    out = {}
    for c in (cats or []):
        key = c.get("key")
        members = sorted(by_cat.get(key, []))
        if not members:
            continue
        digest = hashlib.md5("\n".join(members).encode("utf-8")).hexdigest()
        hit = cache.get(key)
        if hit and hit.get("members_hash") == digest and hit.get("text"):
            out[key] = hit["text"]
            continue
        sample = "、".join(m.replace("/", "/ ") for m in members[:25])
        text = _agnes_generate(
            "你是技术选型顾问。下面是一个 GitHub 收藏类目和它包含的仓库，"
            "用不超过 120 字的中文写一段该类目的选型导览：这类工具适合什么场景、"
            "挑选时看什么、如有明显代表项目可以点名（只点仓库名）。直接输出正文，不要标题和列表。",
            "类目：%s\n包含仓库：%s" % (c.get("label", key), sample))
        if text:
            out[key] = text[:400]
            cache[key] = {"members_hash": digest, "text": out[key]}
            changed = True
        time.sleep(0.8)
    if changed:
        try:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
            os.replace(tmp, cache_path)
        except Exception as e:  # noqa: BLE001
            print("[guides] 缓存写回失败: %s" % e, file=sys.stderr)
    return out


# ==================== 每日 AI 排行榜 ====================
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


def generate_ai_summary(rising_top10):
    """调用 Agnes AI API 生成 AI 态势一句话摘要。失败返回 None（静默降级）。"""
    if FAST:
        return None
    # 多 key 轮询：AGNES_API_KEY（主，可逗号分隔）+ AGNES_API_KEYS（逗号分隔附加）
    # 必须先建池再判空：否则主 key 缺失/全逗号时会漏掉附加 key 或无任何输出地返回
    keys = [k.strip() for k in os.environ.get("AGNES_API_KEY", "").split(",") if k.strip()]
    keys += [k.strip() for k in os.environ.get("AGNES_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        print("[AI摘要] 跳过: 未配置 AGNES_API_KEY / AGNES_API_KEYS", file=sys.stderr)
        return None
    if not rising_top10:
        print("[AI摘要] 跳过: rising 列表为空", file=sys.stderr)
        return None
    lines = []
    for p in rising_top10[:10]:
        name = p.get("full_name", "")
        delta = p.get("delta")
        if delta is not None:
            lines.append("%s (+%d)" % (name, delta))
        else:
            lines.append(name)
    prompt = "用一句话（30字以内）概括今日 GitHub AI/开源生态态势，基于以下涨星项目：" + "、".join(lines)
    payload = json.dumps({
        "model": "agnes-2.5-flash",
        "messages": [
            {"role": "system", "content": "你是一个简洁的 AI 开源态势分析师，回答不超过30字。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 200,
        "temperature": 0.7,
        # agnes-2.5-flash 是思考型模型：不关闭思考时 max_tokens 会被推理耗尽，content 为空
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    try:
        for _ki, _key in enumerate(keys):
            req = urllib.request.Request(
                "https://apihub.agnes-ai.com/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + _key,
                    "User-Agent": "starhub-auto-update",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                choices = data.get("choices") or []
                if not choices:
                    print("[AI摘要] 响应无 choices（key %d/%d）" % (_ki + 1, len(keys)), file=sys.stderr)
                    continue
                content = (choices[0].get("message", {}).get("content") or "").strip()
            except urllib.error.HTTPError as e:
                # 仅限流与上游故障值得换 key；认证类错误换 key 也是白烧
                if e.code in (401, 403):
                    print("[AI摘要] 密钥无效 HTTP %s，停止轮询" % e.code, file=sys.stderr)
                    return None
                if e.code == 429 or e.code >= 500:
                    print("[AI摘要] HTTP %s（key %d/%d），换 key 重试" % (e.code, _ki + 1, len(keys)),
                          file=sys.stderr)
                    continue
                print("[AI摘要] API 调用失败: HTTP %s" % e.code, file=sys.stderr)
                return None
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                # 超时/DNS/JSON 解析失败都是可重试的瞬时故障，不能让它打死剩余 key
                print("[AI摘要] 请求异常（key %d/%d）: %s" % (_ki + 1, len(keys), e), file=sys.stderr)
                continue
            if content and len(content) <= 100:
                print("[AI摘要] %s" % content)
                return content
            # content 为空/超长：换 key 无意义，打印诊断后结束
            print("[AI摘要] 响应不合格: content_len=%d finish_reason=%r usage=%s"
                  % (len(content), choices[0].get("finish_reason"), data.get("usage")), file=sys.stderr)
            break
    except Exception as e:
        print("[AI摘要] 失败: %s" % e, file=sys.stderr)
    return None


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
            time.sleep(1)  # 限流：同一用户分页间 ≥1s，避免二级速率限制
        time.sleep(0.5)  # 限流：不同用户间 ≥0.5s，防止连续请求触发 GitHub 二级速率限制 (403)
    feed.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)
    return feed


def _safe_json(obj):
    # 转义 < 防止 </script> 注入：ensure_ascii=False 时 json.dumps 不转义 <、>，
    # 数据内联进 <script> 块后浏览器会在第一个 </script> 处提前闭合标签执行任意 JS。
    # \u003c 是合法 JSON 转义，json.loads 可还原，不破坏 dev_render.py 的提取流程。
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _atomic_json_write(path, payload):
    """Write a JSON contract atomically; a failed build must not truncate the
    last usable snapshot."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def _contract_item(item):
    health = item.get("health")
    entry = {
        "id": item.get("full_name"),
        "full_name": item.get("full_name"),
        "html_url": item.get("html_url"),
        "description": item.get("desc") or "",
        "language": item.get("language"),
        "stars": int(item.get("stars") or 0),
        "forks": int(item.get("forks") or 0),
        "license": item.get("license") or "",
        "topics": item.get("topics") or [],
        "pushed_at": item.get("pushed_at") or "",
        "starred_at": item.get("starred_at") or "",
        "category": item.get("category") or "",
        "category_label": item.get("categoryLabel") or item.get("category") or "",
        "note": item.get("note") or "",
        "health": {
            "level": health if isinstance(health, str) else "unknown",
            "stale": bool(item.get("stale")),
        },
        "updated_today": bool(item.get("updated_today")),
    }
    if item.get("emb"):
        entry["embedding"] = item["emb"]
    return entry


def _write_site_contracts(items, known, trending, summary_text, updated):
    """Emit the refactor's data boundary. The legacy page keeps using inline
    data; the new frontend reads these files directly."""
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    boards = trending or {}
    _atomic_json_write("data/stars.json", {
        "schema_version": 2,
        "generated_at": generated_at,
        "items": [_contract_item(item) for item in items],
    })
    _atomic_json_write("data/categories.json", {
        "schema_version": 2,
        "updated_at": generated_at,
        "map": known,
        "labels": {c["key"]: c["label"] for c in CATS},
    })
    _atomic_json_write("data/trending.json", {
        "schema_version": 2,
        "generated_at": generated_at,
        "updated": updated,
        "summary": {"text": summary_text or ""},
        "boards": {
            "rising": boards.get("rising", []),
            "total": boards.get("total", []),
            "new": boards.get("new", []),
        },
    })
    _atomic_json_write("data/build-status.json", {
        "schema_version": 2,
        "last_success_at": generated_at,
        "sources": {
            "github": {"status": "ok", "item_count": len(items)},
            "ai_daily": {"status": "unknown"},
        },
    })
    print("[contracts] 写入 data/（stars=%d）" % len(items))


# ==================== 首页顶部「未自动清理」提醒条 ====================
# 来由（2026-10-04 实测）：known_categories.json 有 304 个键，而 GitHub 公开 starred 全集与
# 线上 DATA 都是 293 条、双向零差 ⇒ 多出的 11 个是**旧名残留**（仓库改名/转移后真身 id 仍在
# 收藏，或旧路径已消失），不是收藏缺失。判同一性只能用 repository id，名字只能当线索，
# 所以这里只把"表里有、收藏里没有"如实列出来交给人判定：不猜结论，也不写"丢了"。
#
# 2026-10-12 角色变更（见下面 prune_star_state 的来由）：常态下旧名在算提醒之前就被剪掉了，
# 所以这块从"每日常驻的待办清单"变成"剪枝门槛触发时的报警器"。文案随之改成异常口吻——
# 它现在意味着"这一场收藏没拉全，所以没敢清理"，而不再是"请你去核对哪个仓库改名了"。
# 禁止词表（tests/site_nav/test_star_attention_bar.py::LOST_WORDS）仍然成立：门槛触发时
# 收藏也确实一条没少，少的只是拉取。
ATTENTION_KIND_STAR_ORPHAN = "star_orphan"
ATTENTION_TITLE = "分类表里有 %d 个名字不在当前收藏中——本场未自动清理（拉取条数低于门槛），请核对收藏是否完整拉到"
ATTENTION_SAME_HINT = "同名候选（仅参考，同名不等于同一仓库）"
# 页面层最多列几条：index.html 每场重写进 git，无上限的清单就是把体积增长留在历史里。
ATTENTION_MAX_ROWS = 25

# 剪枝门槛：当前收藏条数必须 ≥ 分类表键数 × 此比例，否则本场一个都不删。
PRUNE_LIVE_MIN_RATIO = 0.8


def prune_star_state(known, live_names):
    """把分类表剪成当前收藏的投影：删掉不在 live_names 里的键，原地改，返回被删的键。

    口径是用户 2026-10-12 的裁定：「取消关注这个行为本身就是删除，重新关注是另一个行为」。
    改名与主动取关不必区分——处置相同，而重新关注回来的新名会照常走 classify_repo 重新分类
    （实测 4 条改名仓库的新名都已自动分类并生成点评）。所以这里既不需要人工表，也不必去问
    GitHub 某个旧名是改名还是消失，那条"不在 CI 里逐场探测 301/404"的界仍然成立。

    唯一能删错人的路径是拉取缩水：表 344 键而分页只拉到 100 条 ⇒ 差集 244，照剪等于清空分类表，
    下一场全量重烧 LLM。故空集与不足门槛一律不剪，且门槛不许严到永不生效（判据两头都钉）。
    """
    live = set(live_names)
    if not live or len(live) < len(known) * PRUNE_LIVE_MIN_RATIO:
        return []
    dropped = sorted(fn for fn in known if fn not in live)
    for fn in dropped:
        del known[fn]
    return dropped


def star_attention_items(repos, known, notes=None, desc_zh=None):
    """列「表里有而当前收藏没有」的旧名。

    repos 传原始 API 响应或已组装条目都行（只取 full_name）；纯函数，不发网络、不做定性。
    剪枝（见下面 prune_star_state）生效后这里常态为空：旧名在算提醒之前就被清掉了，只有门槛
    拦下那一场会带内容，此时它的含义是"本场没敢清理"，不再是"请你判定哪个仓库改名了"。
    """
    notes = notes or {}
    desc_zh = desc_zh or {}
    live = {(r.get("full_name") or "") for r in repos}
    live.discard("")
    if not live:
        # 收藏为空 = 拉取异常（正常路径下 fetch_stars 失败会让 stars_ok=False 而不发布首页，
        # 但这里不能指望调用方）：此时「表−收藏」= 整张表，会把三百多个名字全报成待办 ——
        # 那是比无警更糟的假警风暴，所以一律不报。
        return []
    by_base = {}
    for fn in live:
        by_base.setdefault(fn.split("/")[-1].lower(), []).append(fn)
    items = []
    for fn in sorted(set(known) - live):
        base = fn.split("/")[-1].lower()
        same = sorted(x for x in by_base.get(base, []) if x != fn)
        items.append({
            "kind": ATTENTION_KIND_STAR_ORPHAN,
            "item": fn,
            "hint": {
                "category": known.get(fn) or "",
                "has_note": bool((notes.get(fn) or "").strip()),
                "has_desc": bool((desc_zh.get(fn) or "").strip()),
                "same_base_name_live": same,
            },
        })
    return items


def render_star_attention(items):
    """把提醒条目渲染成 HTML 片段；空清单返回空串，页面上不留任何节点。

    折叠用原生 <details> ⇒ 零新增内联 JS（首页上次白屏就是内联 JS 少一个闭括号整段坏死，
    这块能不用 JS 就不用）。名字一律转义：它们原样来自表键，既进文本也进 href。
    """
    if not items:
        return ""
    from html import escape as esc
    shown = items[:ATTENTION_MAX_ROWS]
    rest = len(items) - len(shown)
    rows = []
    for it in shown:
        h = it.get("hint") or {}
        name = it.get("item") or ""
        bits = []
        if h.get("category"):
            bits.append("分类 " + str(h["category"]))
        bits.append("点评" + ("有" if h.get("has_note") else "无"))
        bits.append("中文简介" + ("有" if h.get("has_desc") else "无"))
        same = h.get("same_base_name_live") or []
        same_html = ""
        if same:
            same_html = ('<span class="attn-same">%s：%s</span>'
                         % (esc(ATTENTION_SAME_HINT, quote=False),
                            esc(", ".join(same), quote=False)))
        rows.append('<li class="attn-row"><a class="attn-name" href="https://github.com/%s"'
                    ' target="_blank" rel="noopener">%s</a>'
                    '<span class="attn-meta">%s</span>%s</li>'
                    % (esc(name), esc(name), esc(" · ".join(bits), quote=False), same_html))
    more = ('<p class="attn-more">另有 %d 条未列出——清单再长下去会把体积写进每场重写的首页。</p>'
            % rest) if rest else ""
    return ('<details class="attn" data-attention="star">'
            '<summary class="attn-sum">%s</summary>'
            '<ul class="attn-list">%s</ul>%s'
            '<p class="attn-note">本场未自动清理：拉到的收藏条数低于分类表门槛。'
            '两种可能——某一页没拉全，或你一次取关了太多。核对 GitHub 收藏后重新构建即可收敛。'
            '收藏条数另有对账，当前收藏与本页条目双向零差。</p>'
            '</details>'
            % (esc(ATTENTION_TITLE % len(items), quote=False), "".join(rows), more))


TRENDING_BOARD = "trending_board.json"


def trending_board_payload(trending, ai_summary_html, updated):
    """排行榜数据出口的**单一形状**：{trending, ai_summary_html, updated}。

    为什么单独一个函数：出口由 CI 写、由前端读、还要被 Stage 拷进制品——三处各拼一份
    迟早分叉（运行时翻译判据就是为这种分叉补的）。这里不重算任何数据，榜单是
    `build_trending()` 已经算好的结果，所以零 GitHub 配额。
    """
    return {"trending": trending or {}, "ai_summary_html": ai_summary_html or "",
            "updated": updated or ""}


def build_index_html(repos, cats, trending=None, feed=None, updated=None, ai_summary_html="",
                     attention_html=""):
    """把组装好的 star 条目渲染成完整 index.html 文本（纯字符串函数：不写文件、不发网络）。

    repos 是 main 流程组装好的条目数组（含 desc/category/categoryLabel 等），不是原始
    API 响应；cats 是 CATS。trending/feed/updated/ai_summary_html 由调用方注入，缺省渲染
    空态 —— 快车道（fast_refresh）与重分类工具复用同一渲染出口，首页只有一种生成方式。
    文件写出与 stars_ok 兜底判断留在 main（拉取失败不得用旧模板覆盖站点）。"""
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()
    if updated is None:
        updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    asset_version = re.sub(r"\D", "", updated) or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return (template
            .replace("__DATA__", _safe_json(repos))
            .replace("__CATS__", _safe_json(cats))
            .replace("__LANGS__", _safe_json(LANG_COLORS))
            .replace("__FAVS__", _safe_json(DEFAULT_FAVS))
            .replace("__API_BASE__", site_value("api_base", ""))
            .replace("__ASSET_VERSION__", asset_version)
            .replace("__TRENDING__", _safe_json(trending or {}))
            .replace("__FEED__", _safe_json(feed or []))
            .replace("__ATTENTION__", attention_html)
            .replace("__UPDATED__", updated)
            .replace("__AI_SUMMARY__", ai_summary_html or ""))


def assemble_entries(repos, known, desc_zh, cat_label, token, notes=None):
    """把原始 star 响应组装成 DATA 条目数组（main 与快车道 fast_refresh 共用的唯一组装出口）。

    行为与抽取前逐字一致：classify_repo 分类（查表不烧 LLM）→ 简介回退链
    （缓存 → 描述 → FALLBACK_DESC → README 摘要）→ 空白归一 → 英文翻译并持久化
    → health/stale → 注入 note/health/stale/starred_at。known/desc_zh 原地更新
    （调用方负责写回持久化）。
    notes = known_notes.json 的存量点评表：查表命中（存量仓主路径）时 classify_repo
    恒返回 note=""，不回填它 T2 写库的 289 条点评就永远不会上线（2026-10-04 端到端
    验收实锤）。LLM 新鲜点评优先，文件值只补空。"""
    out = []
    for r in repos:
        fn = r.get("full_name")
        if not fn:
            continue
        cat, note = classify_repo(fn, r.get("description"), r.get("language"), r.get("topics"), known)
        if not note:
            note = (notes or {}).get(fn) or ""
        known[fn] = cat
        desc = (desc_zh.get(fn) or r.get("description") or FALLBACK_DESC.get(fn, "")).strip()
        # 无简介项目：从 README 提取一句简介，结果持久化到 desc_zh 避免重复抓取
        if not FAST and not desc and fn not in desc_zh:
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
        if not FAST and desc and not has_cn(desc) and fn not in desc_zh:
            translated = translate_to_zh(desc)
            if translated:
                desc = translated
                desc_zh[fn] = translated
                print("[翻译] %s -> %s" % (fn, translated[:60]))
            else:
                print("[翻译失败-保留原文] %s" % fn)
        _hs = health_score(r)
        out.append({
            "id": fn,
            "name": r.get("name"),
            "owner": fn.split("/")[0],
            "full_name": fn,
            "html_url": r.get("html_url"),
            "desc": desc,
            "language": r.get("language"),
            "stars": r.get("stargazers_count"),
            "forks": r.get("forks") or 0,
            "license": ((r.get("license") or {}).get("spdx_id") or ""),
            "topics": r.get("topics", []),
            "pushed_at": (r.get("pushed_at") or "")[:10],
            "updated_today": _cn_date(r.get("pushed_at")) == _today_cn(),
            "category": cat,
            "categoryLabel": cat_label.get(cat, cat),
            "note": note,
            "health": _hs["tier"],
            "stale": _hs["stale"],
            "starred_at": r.get("starred_at") or "",
        })
    return out


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
            _write_site_contracts(out, known, trending, ai_summary, updated)
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
