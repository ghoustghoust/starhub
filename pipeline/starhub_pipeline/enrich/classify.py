"""Repository classification: known map -> LLM -> deterministic rules."""

from __future__ import annotations

import sys
import time

from . import llm

_CATEGORY_CRITERIA = """判定准则（与类目标签冲突时以本准则为准）：
- agent：Agent 框架/运行时/编排/多智能体系统/MCP 服务端，以及 Claude Code/Codex 的 skills 技能集合与插件等【可运行软件】。Agent 主题的教程/书/清单不归此类。
- coding：AI 编程助手/CLI/开发工具链/脚手架/代码生成/模型网关与中转代理（gateway/proxy/2api）。
- assistant：面向最终用户的 AI 助手/聊天客户端/桌面或移动 AI 应用/账号切换器。
- prompt：提示词优化与管理工具、提示词库/期刊、图像提示词逆向与扩写（prompt optimizer/manager/library）。
- video：AI 视频生成/剪辑/数字人/虚拟形象/口播/字幕/故事板/分镜（含 Seedance/Remotion/Sora 类）。
- image：AI 图像生成与工作流（ComfyUI/SD/Flux/Qwen-Image）、图像参考图库/姿势/换装节点。
- media：音乐生成与处理/TTS/声音克隆/播客/音乐 API 与播放器。
- download：下载器/网盘直链/爬虫/数据采集/视频嗅探等抓取类工具。
- ocr：OCR 文字识别/文档识别/截图取字工具。
- content：内容创作与排版/PPT/社交卡片/公众号运营/写作辅助/营销素材。
- distill：思维模型/认知方法论/决策框架/文风蒸馏/把书或长内容蒸馏成 skill。
- learning：教程/路线图/学习指南/实战手册/书籍/课程/面试题/测验抽认卡/知识库型 README。任何主题的"学习资料"一律归此类。
- info：资讯/周刊/Newsletter/新闻聚合/RSS/资源导航/awesome 合集/导航站/热榜日报。
- business：一人公司/独立开发变现/商业化方法论/知识产权（软著/专利）/营销增长/出海合规。
- finance：量化交易/投资/股票/加密货币/金融数据。
- frontend：前端框架/组件库/设计系统/UI 工程。
- tools：通用效率工具/便签/翻译汉化/浏览器插件/文件处理等实用工具（不限 AI），无法归入上述能力类时的兜底。
总裁决：①内容形态优先于主题（软件→能力类，资料→learning，资讯合集→info）；②教程属性压过工具属性——讲"怎么用 X"的仓库跟 X 走，讲"怎么学 X"的归 learning；③图像工作流归 image、视频/数字人归 video、音乐/TTS 归 media，不再混为一谈。"""

_LLM_FAIL_STREAK = 0
_LLM_BLOCK_UNTIL = 0.0
_LLM_FAIL_LIMIT = 3
_LLM_COOLDOWN = 300


def reset_circuit() -> None:
    global _LLM_FAIL_STREAK, _LLM_BLOCK_UNTIL
    _LLM_FAIL_STREAK = 0
    _LLM_BLOCK_UNTIL = 0.0


def llm_available() -> bool:
    return time.time() >= _LLM_BLOCK_UNTIL


def _record(ok: bool) -> None:
    global _LLM_FAIL_STREAK, _LLM_BLOCK_UNTIL
    if ok:
        _LLM_FAIL_STREAK = 0
        return
    _LLM_FAIL_STREAK += 1
    if _LLM_FAIL_STREAK >= _LLM_FAIL_LIMIT:
        _LLM_BLOCK_UNTIL = time.time() + _LLM_COOLDOWN
        print(
            f"[classify] circuit open after {_LLM_FAIL_STREAK} failures",
            file=sys.stderr,
        )


def classify_new(fn: str, desc: str | None, lang: str | None, topics: list[str] | None) -> str:
    repo = fn.split("/")[-1].lower()
    text = (fn + " " + (desc or "") + " " + " ".join(topics or [])).lower()

    def has(*keys: str) -> bool:
        return any(k in text for k in keys)

    # 顺序即优先级：强信号能力类在前，兜底 agent/tools 在后。
    if has("trading", "finance", "金融", "交易", "bloomberg", "量化", "股票", "加密货币"):
        return "finance"
    if "ocr" in repo or has("光学字符", "文档识别"):
        return "ocr"
    if has("downloader", "下载器", "下载工具", "批量下载", "crawler", "爬虫", "sniffer", "采集",
           "网盘", "netdisk", "直链", "downkyi", "mediacrawler"):
        return "download"
    if has("pdf") and has("翻译", "translation", "translate"):
        return "tools"
    if has("open-design", "设计系统", "设计引擎", "ui-generator"):
        return "frontend"
    if has("ppt", "powerpoint", "幻灯片", "slides", "排版", "公众号", "social-card", "社交卡片",
           "小红书", "typeset", "海报"):
        return "content"
    if "prompt" in repo or has("提示词优化", "提示词管理", "prompt optimizer", "prompt management"):
        return "prompt"
    if has("distill", "蒸馏", "文风蒸馏", "文风档案", "写作风格", "writing-dna", "心智模型", "第一性",
           "first-principles", "方法论", "认知植入", "思维方式", "nuwa", "女娲", "cangjie", "仓颉",
           "buffett", "superpowers"):
        return "distill"
    if has("one-person", "一人公司", "创业", "startup", "growth", "增长", "软著", "copyright",
           "专利", "patent", "合规", "compliance", "marketing", "营销", "seo"):
        return "business"
    if has("book", "书籍", "教程", "guide", "指南", "from-scratch", "tutorial", "课程", "入门",
           "learning", "learn-", "路线图", "实践", "dive-into", "quiz", "flashcard", "抽认卡",
           "学习包", "学习路径", "橙皮书", "handbook", "wiki"):
        return "learning"
    if has("rss", "newsletter", "周刊", "资讯", "新闻聚合", "news-aggregator", "aggregator",
           "导航", "navigation", "热榜", "日报", "daily-digest", "top-rss"):
        return "info"
    if has("数字人", "digital-human", "digital human", "avatar", "vtuber", "live2d",
           "talking-head", "talking-avatar", "lip-sync", "数字生命"):
        return "video"
    if has("video", "视频", "短剧", "drama", "film", "anime", "动漫", "movie", "montage",
           "hyperframe", "storyboard", "故事板", "分镜", "seedance", "remotion", "shot", "cinematic"):
        return "video"
    if has("music", "音乐", "tts", "text-to-speech", "voice-clone", "voice-cloning", "声音克隆",
           "歌曲", "网易云", "kugou", "lxmusic", "播客", "podcast", "ai-music", "music-generation"):
        return "media"
    if has("comfyui", "stable-diffusion", "image-generation", "图像生成", "图片生成", "ai-image",
           "text-to-image", "img2img", "nano-banana", "qwen image", "图库", "pose-gallery", "参考图"):
        return "image"
    if repo == "skills" or repo.endswith("-skills") or has(
            "agent-skills", "claude-skills", "codex-skills", "claude-code-skills", "skill-md"):
        return "agent"
    if has("便签", "note-taking", "translator", "翻译", "汉化", "userscript", "mpv", "iptv", "tvbox"):
        return "tools"
    if has("frontend", "前端", "component-library", "组件库", "组件", "ui-library", "ui-components"):
        return "frontend"
    if has("coding", "编程", "code-review", "gateway", "网关", "proxy", "中转", "2api", "sub2api",
           "coding-agent", "cli-tool", "quantization"):
        return "coding"
    if has("chatbot", "chatgpt", "assistant", "助手", "librechat", "astrbot", "desktop-app",
           "桌面", "客户端", "client", "openwebui", "aihub", "switch", "切换"):
        return "assistant"
    if has("ai-agent", "ai-agents", "agent-framework", "multi-agent", "agentic", "agent-workflows",
           "智能体", "代理", "mcp-server", "mcp"):
        return "agent"
    return "tools"


def classify_llm(fn: str, desc: str | None, lang: str | None, topics: list[str] | None, cats: list[dict]):
    valid_keys = {category["key"] for category in (cats or [])}
    categories = "；".join(f"{category['key']}={category['label']}" for category in (cats or []))
    messages = [
        {
            "role": "system",
            "content": (
                "你是 GitHub 仓库分类器。根据仓库信息从给定类目中选一个最合适的类目key，"
                "并用不超过%d字的中文给一句点评。只输出一个 JSON 对象，"
                '格式：{"category": "<类目key>", "note": "<点评>"}，'
                "禁止解释、禁止 markdown 代码块。可选类目key：%s\n%s"
                % (llm.NOTE_MAX_CHARS, categories, _CATEGORY_CRITERIA)
            ),
        },
        {
            "role": "user",
            "content": "仓库名：%s\n简介：%s\n主语言：%s\nTopics：%s"
                       % (fn, desc or "（无）", lang or "（未知）", "、".join(topics or []) or "（无）"),
        },
    ]
    content = llm.chat(messages, temperature=0.2, max_tokens=200)
    if content is None:
        return None
    parsed = llm.parse_json(content)
    if parsed is None:
        print(f"[classify] unparsable response: {content[:80]}", file=sys.stderr)
        return None
    category = str(parsed.get("category") or "").strip()
    if not category or (valid_keys and category not in valid_keys):
        return None
    note = str(parsed.get("note") or "").strip()[:llm.NOTE_MAX_CHARS]
    return {"category": category, "note": note}


def classify_repo(
    fn: str,
    desc: str | None,
    lang: str | None,
    topics: list[str] | None,
    known: dict[str, str] | None,
    cats: list[dict],
    fast: bool = False,
):
    try:
        category = (known or {}).get(fn)
        if category:
            return category, ""
        if not fast and llm_available():
            result = classify_llm(fn, desc, lang, topics, cats)
            _record(bool(result and result.get("category")))
            if result and result.get("category"):
                return result["category"], (result.get("note") or "")[:llm.NOTE_MAX_CHARS]
        category = classify_new(fn, desc, lang, topics)
        return category, ""
    except Exception as exc:  # noqa: BLE001
        print(f"[classify] {fn}: {exc}", file=sys.stderr)
        return "tools", ""
