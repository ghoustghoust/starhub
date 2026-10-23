"""Repository classification: known map -> LLM -> deterministic rules."""

from __future__ import annotations

import sys
import time

from . import llm

_CATEGORY_CRITERIA = """判定准则（与类目标签冲突时以本准则为准）：
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
    text = (fn + " " + (desc or "") + " " + " ".join(topics or [])).lower()
    if any(k in text for k in ["trading", "finance", "金融", "交易", "bloomberg"]):
        return "finance"
    if any(k in text for k in ["tvbox", "iptv", "直播", "电视", "crawler", "爬虫", "download", "下载",
                               "translator", "翻译", "汉化", "网盘", "pan", "mpv", "userscript"]):
        return "tools"
    if any(k in text for k in ["distill", "思维蒸馏", "知识蒸馏", "内容蒸馏", "蒸馏成", "蒸馏出", "蒸馏任何",
                               "认知植入", "思维方式", "心智模型", "第一性", "first-principles",
                               "方法论", "nuwa", "女娲", "cangjie", "仓颉", "文风"]):
        return "distill"
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
                               "learning", "入门", "weekly", "实践", "tutorial", "dive-into"]):
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
        if category != "agent":
            return category, ""
        return "tools", ""
    except Exception as exc:  # noqa: BLE001
        print(f"[classify] {fn}: {exc}", file=sys.stderr)
        return "tools", ""
