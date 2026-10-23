"""Stable site taxonomy and display constants."""

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

DEFAULT_FAVS: list[str] = []

FALLBACK_DESC = {
    "llazyl/TVBox": "TVBox 影视聚合播放器",
    "q215613905/TVBoxOS": "TVBoxOS 影视播放系统",
    "FongMi/TV": "基于 media3/ffmpeg/mpv 的开源影视播放器",
}
