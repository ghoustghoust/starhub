# -*- coding: utf-8 -*-
"""每日深度洞察报告 — 独立模块（RAG 架构）。

四源叠加（RSS 72h + 热榜 40 平台 + AIHOT + AGI Hunt）→ RAG 流水线
（硬过滤 → 切片 → Embedding → FAISS 索引 → 混合检索 → 重排序 → LLM 生成）
→ daily-insight.json + 历史轨迹。

由 fetch_and_build.py 在 build_rss_aggregator 之后调用。
不修改现有 insight_engine 或任何已有模块。
"""
import collections
import datetime
import hashlib
import html as html_mod
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

# ── 可选依赖：FAISS + BM25 + numpy ──
try:
    import faiss as _faiss
    import numpy as _np
    FAISS_AVAILABLE = True
except ImportError:
    _faiss = None
    try:
        import numpy as _np
    except ImportError:
        _np = None
    FAISS_AVAILABLE = False

try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False

# ── 常量 ──
BJT = datetime.timezone(datetime.timedelta(hours=8))
NOW_BJ = None  # 在 main() 中初始化
SEMANTIC_CLUSTER_THRESHOLD = 0.75  # 语义聚类余弦相似度阈值
MAX_QUERIES = 80  # 查询总数上限（扩容以覆盖更多话题维度）

# ── 数据文件路径 ──
RSS_HISTORY_FILE = "rss_history.json"
HOT_SNAPSHOT_FILE = "hot_snapshot.json"
SNAPSHOT_FILE = "daily_insight_snapshot.json"   # AIHOT + AGI Hunt 构建期缓存
INSIGHT_FILE = "daily-insight.json"             # 当日洞察产出
HISTORY_FILE = "daily_insight_history.json"     # 30 天历史轨迹
HISTORY_HTML = "daily-insight-history.html"     # 历史页面
TRACKING_FILE = "daily_insight_tracking_history.jsonl"  # 构建质量追踪日志
CONFIG_FILE = "build_config.json"

# ── AIHOT API ──
AIHOT_API_URL = "https://aihot.virxact.com/api/v1/items"

# ── AGI Hunt 配置 ──
AGIHUNT_CHANNELS = [
    "models", "research", "coding-agents", "products", "multimodal",
    "infra", "hardware", "funding", "policy", "agi", "companies", "fun",
]
AGIHUNT_API_URL = "https://agihunt.info/agent/v1"

# ── 热榜平台分层 ──
HOT_T1 = {"weibo", "zhihu", "baidu", "toutiao", "douyin", "bilibili"}
HOT_T2 = {"ithome", "hackernews", "github", "solidot", "sspai", "juejin",
           "v2ex", "producthunt", "chongbuluo", "pcbeta"}
HOT_T3 = set()  # 其余平台默认 T3

# ── RSS 信源 Tier（从 rss_sources.json 动态加载） ──
_RSS_TIER_MAP = {}  # source_key → tier

# ── 过滤黑名单 ──
_CLICKBAIT_PATTERNS = re.compile(
    r'震惊|不看后悔|疯了|必看|速看|删前速看|99%的人不知道'
    r'|万万没想到|重磅来袭|炸裂|核弹级|颠覆性|史诗级'
    r'|赶紧看|快来看|错过就没了|手慢无',
    re.IGNORECASE,
)
_AD_PATTERNS = re.compile(
    r'限时折扣|优惠券|下单链接|薅羊毛|种草带货|荐股|荐币'
    r'|财富密码|社群招募|明天必涨|扫码领取|免费领取'
    r'|限时优惠|秒杀活动|点击注册|加入社群|立即抢购',
    re.IGNORECASE,
)

# ── LLM category 枚举 ──
VALID_CATEGORIES = [
    "ai-models", "ai-products", "industry", "research",
    "policy", "funding", "developer", "consumer",
]

# ── 主题分类层（借鉴 QWIS：优先级顺序匹配，首个命中即停） ──
# 顺序敏感：ai 在最前，确保"英伟达股价"类科技金融交叉事件留在主报告
TOPIC_TAXONOMY = [
    ("ai", ["大模型", "模型", "gpt", "claude", "gemini", "llm", "openai", "anthropic",
            "deepmind", "智能体", "agent", "芯片", "gpu", "nvidia", "英伟达", "算力",
            "训练", "推理", "机器人", "自动驾驶", "深度学习", "机器学习", "agi", "多模态",
            "人工智能"]),
    ("programming", ["github", "开源", "编程", "开发者", "框架", "sdk", "api", "编译器",
                     "rust", "python", "javascript", "代码", "漏洞", "安全研究", "插件"]),
    ("finance", ["央行", "利率", "股价", "股涨", "股跌", "指数涨", "基金", "债券", "汇率",
                 "通胀", "gdp", "a股", "美股", "港股", "期货", "黄金", "存款", "贷款", "楼市", "房价"]),
    ("geopolitics", ["联合国", "安理会", "制裁", "关税", "外交", "军事", "导弹", "停火",
                     "选举投票", "峰会", "北约", "俄乌", "中东", "台海", "法案通过", "禁令"]),
    ("society", ["高考", "报名", "就业", "医疗", "医保", "养老", "生育", "人口", "事故",
                 "警方", "法院", "判决", "教育", "校园", "疫情", "疫苗", "火车", "航班"]),
    ("culture", ["诺贝尔", "奥斯卡", "世界杯", "奥运", "联赛", "电影票房", "文学奖",
                 "博物馆", "考古", "演唱会", "夺冠", "运动员", "太空", "天文", "发射"]),
]
MAIN_TOPICS = {"ai", "programming"}
BUBBLE_TOPICS = {"finance", "geopolitics", "society", "culture"}
_CAT_HINTS = {"ai": "ai", "dev": "programming", "tech": "programming",
              "news": "other", "wechat": "other", "cn_tech": "programming"}


def _classify_topic(title, text, source, cat=""):
    """关键词优先级匹配。无命中时按 RSS cat 提示归类，最终回退 other。"""
    hay = ((title or "") + " " + (text or "")[:300] + " " + (source or "")).lower()
    for tag, kws in TOPIC_TAXONOMY:
        for kw in kws:
            if kw in hay:
                return tag
    return _CAT_HINTS.get(cat, "other")

# ── 打分参数（框架先立，参数后调） ──
CROSS_FACTOR = 1.5          # 每多一个信源类型，信号 ×1.5
RECENCY_HALF_LIFE_H = 24    # 24h 半衰期
ACCEL_THRESHOLD_HIGH = 0.5  # 近 6h 占比 >50% → 加速
ACCEL_THRESHOLD_LOW = 0.1   # 近 6h 占比 <10% → 减速
ACCEL_BOOST = 1.3
ACCEL_DECAY = 0.8

# ── 聚类/匹配参数 ──
JACCARD_HISTORY_THRESHOLD = 0.5  # 跨天事件匹配
JACCARD_EVENT_THRESHOLD = 0.35   # 检索后事件组装用

# ── TopK ──
MIN_EVENTS = 3
MAX_EVENTS = 12
PHASE1_POOL = MAX_EVENTS + 4  # T1: 候选扩池，去重/丢弃空槽由后位候选顶上，终局仍收口 MAX_EVENTS
DEEP_ANALYSIS_TOP_N = 3

# ── RAG 参数 ──
EMBED_MODEL = "BAAI/bge-m3"
EMBED_URL = "https://api.siliconflow.cn/v1/embeddings"
EMBED_BATCH = 32
EMBED_DIM = 1024
CHUNK_SIZE = 300       # 目标 chunk token 数
CHUNK_OVERLAP = 50     # chunk 重叠 token 数
FAISS_INDEX_FILE = "daily_insight_faiss.index"
FAISS_META_FILE = "daily_insight_chunks.json"
VECTOR_CACHE_FILE = "daily_insight_vectors.npy"  # numpy 向量缓存（增量 embedding）
RETRIEVAL_TOP_K = 200  # 向量检索每查询返回数（扩大检索提升覆盖率）
RRF_K = 60             # RRF 融合常数
BM25_ENABLED = True     # BM25 混合检索开关
MAX_EMBED_CHUNKS = 30000   # 最大 embedding chunk 数（扩容至全量覆盖）
BM25_WINDOW = 10000         # BM25 检索窗口上限（非 RSS 优先纳入，剩余给近期 RSS）
INSIGHT_RSS_HOURS = 168   # 每日洞察取最近 N 小时的 RSS（7 天窗口，支持跨天趋势检测）

# ── RAGAS 质量评估参数 ──
RAGAS_ENABLED = True          # RAGAS 评估-修正闭环开关
RAGAS_QUALITY_THRESHOLD = 0.70  # 整体质量阈值，低于此值触发修正
RAGAS_MAX_CORRECTIONS = 1      # 修正轮数（01:12 期实证：2 轮在复评噪声带内采纳劣化版，回稳 1 轮）
# 各维度最低阈值 — 任一维度低于阈值即触发修正（即使 overall 达标）
RAGAS_MIN_COVERAGE = 0.75      # context_coverage 最低要求
RAGAS_MIN_FAITHFULNESS = 0.88  # faithfulness 最低要求（目标 ≥ 0.95）
RAGAS_MIN_RELEVANCE = 0.75     # relevance 最低要求（目标 ≥ 0.80）
_RAGAS_CTX_CHAR_BUDGET = 140000  # 评估上下文字符预算：实测随机 200 块中位 10.5 万、p90 11.1 万，正常不触顶；触顶必告警


# ──────────────────── 工具函数 ────────────────────

def _now_bj():
    return datetime.datetime.now(BJT)


def _parse_iso(s):
    s = (s or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    dt = None
    try:
        dt = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        try:
            dt = datetime.datetime.fromisoformat(s[:19])
        except (ValueError, TypeError):
            return None
    # naive 时间戳按北京时间理解（与 _hours_ago 既有约定一致），
    # 统一返回 aware，杜绝 max/min 混比 TypeError（CI run 35349753348 崩溃根因）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJT)
    return dt


def _hours_ago(dt):
    """距现在多少小时。dt 为 aware datetime 或 None。"""
    if not dt:
        return 999
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJT)
    return max(0, (NOW_BJ - dt).total_seconds() / 3600)


def _recency_decay(dt):
    """时间衰减因子，24h 半衰期。"""
    h = _hours_ago(dt)
    if h >= 999:
        return 0.15
    return 0.5 ** (h / RECENCY_HALF_LIFE_H)


def _esc(s):
    return html_mod.escape(str(s), quote=True)


def _truncate(s, maxlen=300):
    if not s:
        return ""
    s = s.strip()
    return s[:maxlen] + "…" if len(s) > maxlen else s


def _strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return text.strip()


def _tokenize_title(title):
    """标题分词：按标点和空格切分，再拆中文为 2-gram 集合。"""
    title = (title or "").lower()
    # 按标点/空格/特殊字符切分
    parts = re.split(r'[\s,，。！？!?、；:：""''（）()\[\]{}|/\\·\-]+', title)
    tokens = set()
    for p in parts:
        if not p:
            continue
        # 英文整体作为一个 token
        if p.isascii():
            if len(p) >= 2:
                tokens.add(p)
            continue
        # 中文：2-gram + 整段
        if len(p) >= 2:
            tokens.add(p)
        for i in range(len(p) - 1):
            tokens.add(p[i:i+2])
    return tokens


def _simple_tokens(text):
    """轻量分词：英文按词切分 + 中文按 character bigrams。用于去重/相似度计算。"""
    text = (text or "").lower()
    tokens = set()
    for w in re.findall(r'[a-z0-9]{2,}', text):
        tokens.add(w)
    cjk = re.findall(r'[\u4e00-\u9fff]', text)
    for k in range(len(cjk) - 1):
        tokens.add(cjk[k] + cjk[k + 1])
    return tokens


def _jaccard(set_a, set_b):
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _atomic_write_text(path, text):
    """原子写文本文件：先写 .tmp 再 os.replace"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ──────────────────── 配置加载 ────────────────────

def load_config():
    cfg = {"daily_insight_enabled": True, "daily_insight_llm": "agnes"}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return cfg


def _load_rss_tiers():
    """从 rss_sources.json 加载 source_key → tier 映射。"""
    global _RSS_TIER_MAP
    try:
        with open("rss_sources.json", "r", encoding="utf-8") as f:
            sources = json.load(f)
        for s in sources:
            if isinstance(s, dict) and s.get("key"):
                _RSS_TIER_MAP[s["key"]] = s.get("tier", 3)
    except Exception:
        pass


# ──────────────────── 数据采集 ────────────────────

def _fetch_aihot():
    """构建期抓取 AIHOT API，返回条目列表。"""
    url = AIHOT_API_URL + "?mode=all&window=24h&limit=100"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (starhub-auto-update)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read(2000000).decode("utf-8", errors="replace"))
    except Exception as e:
        print("[每日洞察] AIHOT 拉取失败: %s" % e, file=sys.stderr)
        return []

    items = []
    for it in data.get("items") or []:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        links = it.get("links") or {}
        link = links.get("original") or links.get("aihot") or ""
        source = ((it.get("source") or {}).get("name") or "").strip()
        pub = it.get("discoveredAt") or it.get("publishedAt") or ""
        items.append({
            "title": title,
            "link": link,
            "source": source,
            "category": (it.get("category") or "").strip(),
            "summary": _truncate(it.get("summary") or "", 500),
            "pub_date": pub,
            "score": float(it.get("score") or 0),
            "_src": "aihot",
        })
    print("[每日洞察] AIHOT 拉取 %d 条" % len(items))
    return items


def _fetch_agihunt():
    """构建期抓取 AGI Hunt 频道数据。需 AGIHUNT_API_KEY 环境变量。

    遵循 AGI Hunt Agent API 使用守则：
    - 限速 0.5 次/秒（突发 10），每请求间隔 ≥ 2s
    - 429 按 Retry-After 退避，不循环重试
    - 401 停止后续请求（密钥无效）
    - 426 拉取新版 Skill 版本后重试一次
    - 取当天 + 昨天数据以扩大覆盖率（API 支持近 3 天）
    """
    api_key = os.environ.get("AGIHUNT_API_KEY", "")
    if not api_key:
        print("[每日洞察] AGIHUNT_API_KEY 未配置，跳过 AGI Hunt", file=sys.stderr)
        return []

    skill_version = "1.2.2"
    today = NOW_BJ.strftime("%Y-%m-%d")
    yesterday = (NOW_BJ - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    fetch_days = [today, yesterday]

    all_items = []
    key_valid = True
    retry_once = True  # 426 时允许重试一次

    for day in fetch_days:
        if not key_valid:
            break
        for ci, channel in enumerate(AGIHUNT_CHANNELS):
            # 限速：0.5 次/秒 → 每请求间隔 ≥ 2s（突发 10 后必须降速）
            if ci > 0 or day != today:
                time.sleep(2.0)

            url = "%s/channel/%s/items?day=%s&sort=hot" % (
                AGIHUNT_API_URL, channel, day)
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer %s" % api_key,
                "X-AgiHunt-Skill-Version": skill_version,
                "User-Agent": "starhub-auto-update",
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read(500000).decode("utf-8", errors="replace"))
                for it in (data.get("items") or []):
                    title = (it.get("title") or "").strip()
                    if not title:
                        continue
                    all_items.append({
                        "title": title,
                        "text": _truncate(it.get("text") or "", 800),
                        "url": it.get("url") or "",
                        "author": it.get("author") or "",
                        "hot": float(it.get("hot") or 0),
                        "channel": channel,
                        "pub_date": it.get("published_at") or "",
                        "_src": "agihunt",
                    })
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # 按 Retry-After 退避，不循环重试
                    retry_after = int(e.headers.get("Retry-After", "60"))
                    wait = min(retry_after, 120)
                    print("[每日洞察] AGI Hunt 429 限流，等待 %ds 后跳过剩余频道" % wait, file=sys.stderr)
                    time.sleep(wait)
                    key_valid = False  # 当天不再请求
                elif e.code == 401:
                    print("[每日洞察] AGI Hunt 401 密钥无效，停止请求", file=sys.stderr)
                    key_valid = False
                elif e.code == 426 and retry_once:
                    # Skill 需更新：拉取最新版本号后重试
                    retry_once = False
                    try:
                        ver_url = "%s/skill/version" % AGIHUNT_API_URL
                        ver_req = urllib.request.Request(ver_url, headers={
                            "Authorization": "Bearer %s" % api_key,
                            "User-Agent": "starhub-auto-update",
                        })
                        with urllib.request.urlopen(ver_req, timeout=5) as vr:
                            new_ver = vr.read().decode("utf-8").strip().strip('"')
                        if new_ver and new_ver != skill_version:
                            print("[每日洞察] AGI Hunt Skill 更新: %s → %s" % (skill_version, new_ver), file=sys.stderr)
                            skill_version = new_ver
                            # 重试当前频道
                            time.sleep(2.0)
                            retry_req = urllib.request.Request(
                                url, headers={
                                    "Authorization": "Bearer %s" % api_key,
                                    "X-AgiHunt-Skill-Version": skill_version,
                                    "User-Agent": "starhub-auto-update",
                                })
                            with urllib.request.urlopen(retry_req, timeout=15) as r:
                                data = json.loads(r.read(500000).decode("utf-8", errors="replace"))
                            for it in (data.get("items") or []):
                                title = (it.get("title") or "").strip()
                                if not title:
                                    continue
                                all_items.append({
                                    "title": title,
                                    "text": _truncate(it.get("text") or "", 800),
                                    "url": it.get("url") or "",
                                    "author": it.get("author") or "",
                                    "hot": float(it.get("hot") or 0),
                                    "channel": channel,
                                    "pub_date": it.get("published_at") or "",
                                    "_src": "agihunt",
                                })
                    except Exception as ve:
                        print("[每日洞察] AGI Hunt 426 更新失败: %s" % ve, file=sys.stderr)
                elif e.code == 400:
                    print("[每日洞察] AGI Hunt [%s] day=%s 参数错误(400)" % (channel, day), file=sys.stderr)
                else:
                    print("[每日洞察] AGI Hunt [%s] 失败: HTTP %d" % (channel, e.code), file=sys.stderr)
            except Exception as e:
                print("[每日洞察] AGI Hunt [%s] 失败: %s" % (channel, e), file=sys.stderr)

    print("[每日洞察] AGI Hunt 拉取 %d 条（%d 频道, %d 天）" % (
        len(all_items), len(AGIHUNT_CHANNELS), len(fetch_days)))
    return all_items


def _load_snapshot():
    """加载或创建构建期快照（AIHOT + AGI Hunt 缓存）。"""
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                snap = json.load(f)
            if snap.get("date") == NOW_BJ.strftime("%Y-%m-%d"):
                print("[每日洞察] 复用今日快照: %d AIHOT + %d AGI Hunt" % (
                    len(snap.get("aihot_items", [])), len(snap.get("agihunt_items", []))))
                return snap
        except Exception:
            pass

    # 抓取新数据
    aihot = _fetch_aihot()
    agihunt = _fetch_agihunt()
    snap = {
        "date": NOW_BJ.strftime("%Y-%m-%d"),
        "aihot_items": aihot,
        "agihunt_items": agihunt,
        "fetched_at": NOW_BJ.isoformat(),
    }
    try:
        _atomic_write_json(SNAPSHOT_FILE, snap)
    except Exception as e:
        print("[每日洞察] 快照写入失败: %s" % e, file=sys.stderr)
    return snap


def _load_rss_history():
    """加载 RSS 72h 历史（支持分块格式）。"""
    index_file = "rss_history_index.json"
    if os.path.exists(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index = json.load(f)
            merged = {}
            for i in range(index.get("chunks", 0)):
                fname = "rss_history_%d.json" % i
                if os.path.exists(fname):
                    with open(fname, "r", encoding="utf-8") as f:
                        merged.update(json.load(f))
            if merged:
                return merged
        except Exception:
            pass
    if not os.path.exists(RSS_HISTORY_FILE):
        return {}
    try:
        with open(RSS_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_hot_snapshot():
    """加载热榜快照。"""
    if not os.path.exists(HOT_SNAPSHOT_FILE):
        return []
    try:
        with open(HOT_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ──────────────────── Phase 1.2: 硬过滤 ────────────────────

def _filter_noise(items):
    """纯规则剔除营销文/内容农场/标题党。返回干净条目列表。"""
    clean = []
    rejected = 0
    for it in items:
        title = it.get("title", "")
        # 标题党
        if _CLICKBAIT_PATTERNS.search(title):
            rejected += 1
            continue
        # 广告/营销
        if _AD_PATTERNS.search(title):
            rejected += 1
            continue
        # 太短（可能是无效条目）
        if len(title) < 4:
            rejected += 1
            continue
        clean.append(it)
    if rejected:
        print("[每日洞察] 硬过滤剔除 %d 条噪声" % rejected)
    return clean




# ──────────────────── RAG: 文档切片 ────────────────────

def _chunk_documents(rss_clean, hot_clean, aihot_clean, agihunt_clean):
    """将四源数据切分为统一格式的 chunk 列表。

    RSS: 按段落/句子切分，目标 ~CHUNK_SIZE token，CHUNK_OVERLAP 重叠。
    其他源: 每条一个 chunk（标题+摘要/描述）。
    """
    chunks = []
    chunk_counter = [0]

    def _make_chunk(source_type, source, title, url, text, pub_date,
                    source_key="", extra=None):
        cid = "chunk_%05d" % chunk_counter[0]
        chunk_counter[0] += 1
        # 内容指纹：用于跨构建去重，判断 chunk 是否已缓存向量
        hash_input = "%s|%s|%s|%s" % (source_type, source, title, text[:200])
        content_hash = hashlib.md5(hash_input.encode("utf-8")).hexdigest()[:16]
        c = {
            "chunk_id": cid,
            "source_type": source_type,
            "source": source,
            "source_key": source_key,
            "title": title,
            "url": url,
            "text": text[:3000],
            "pub_date": pub_date,
            "content_hash": content_hash,
            "topic_tag": _classify_topic(title, text, source, (extra or {}).get("cat", "")),
        }
        if extra:
            c.update(extra)
        return c

    # ── RSS: 按段落切分 ──
    for item in rss_clean:
        raw = item.get("full_content") or item.get("summary") or ""
        text = _strip_html(raw).strip()
        title = item.get("title", "")
        if not text:
            text = title
        # 短文本不切分
        if len(text) <= CHUNK_SIZE * 4:
            chunks.append(_make_chunk(
                "rss", item.get("source", ""), title,
                item.get("link", ""), text, item.get("pub_date", ""),
                source_key=item.get("source_key", ""), extra={"cat": item.get("cat", "")},
            ))
            continue
        # 按段落分割，再按目标 token 数合并/拆分
        paragraphs = re.split(r'\n\s*\n', text)
        current_text = ""
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if len(current_text) + len(para) + 1 > CHUNK_SIZE * 4:
                if current_text:
                    chunks.append(_make_chunk(
                        "rss", item.get("source", ""), title,
                        item.get("link", ""), current_text,
                        item.get("pub_date", ""),
                        source_key=item.get("source_key", ""), extra={"cat": item.get("cat", "")},
                    ))
                # 超长段落按句子拆分
                while len(para) > CHUNK_SIZE * 4:
                    cut = para[:CHUNK_SIZE * 4]
                    # 在最后一个句号处断开
                    for sep in ["。", ".", "！", "!", "？", "?", "；", ";"]:
                        idx = cut.rfind(sep)
                        if idx > CHUNK_SIZE * 2:
                            cut = cut[:idx + 1]
                            break
                    chunks.append(_make_chunk(
                        "rss", item.get("source", ""), title,
                        item.get("link", ""), cut,
                        item.get("pub_date", ""),
                        source_key=item.get("source_key", ""), extra={"cat": item.get("cat", "")},
                    ))
                    para = para[len(cut):]
                current_text = para
            else:
                current_text = (current_text + "\n" + para).strip()
        if current_text:
            chunks.append(_make_chunk(
                "rss", item.get("source", ""), title,
                item.get("link", ""), current_text,
                item.get("pub_date", ""),
                source_key=item.get("source_key", ""), extra={"cat": item.get("cat", "")},
            ))

    # ── 热榜: 每条一个 chunk ──
    for item in hot_clean:
        text = "[热榜/%s #%s] %s" % (
            item.get("platform", ""), item.get("rank", ""), item.get("title", ""))
        chunks.append(_make_chunk(
            "hot", item.get("platform", ""), item.get("title", ""),
            item.get("url", ""), text, "",
        ))

    # ── AIHOT: 每条一个 chunk ──
    for item in aihot_clean:
        text = "[AIHOT/%s] %s\n%s" % (
            item.get("category", ""), item.get("title", ""),
            item.get("summary", ""))
        chunks.append(_make_chunk(
            "aihot", item.get("source", ""), item.get("title", ""),
            item.get("link", ""), text, item.get("pub_date", ""),
        ))

    # ── AGI Hunt: 每条一个 chunk ──
    for item in agihunt_clean:
        text = "[AGI Hunt/%s hot=%.0f] %s\n%s" % (
            item.get("channel", ""), item.get("hot", 0),
            item.get("title", ""), item.get("text", ""))
        chunks.append(_make_chunk(
            "agihunt", item.get("channel", ""), item.get("title", ""),
            item.get("url", ""), text, item.get("pub_date", ""),
            extra={"hot": item.get("hot", 0), "channel": item.get("channel", "")},
        ))

    print("[每日洞察] 切片完成: %d 个 chunks (RSS %d, 热榜 %d, AIHOT %d, AGI Hunt %d)" % (
        len(chunks),
        sum(1 for c in chunks if c["source_type"] == "rss"),
        sum(1 for c in chunks if c["source_type"] == "hot"),
        sum(1 for c in chunks if c["source_type"] == "aihot"),
        sum(1 for c in chunks if c["source_type"] == "agihunt"),
    ))
    return chunks


# ──────────────────── RAG: Embedding ────────────────────

def _embed_chunks(texts):
    """调用硅基流动 BAAI/bge-m3 API 获取 embedding 向量。
    回退：本地 fastembed。复用 insight_engine 的调用模式但独立实现。
    """
    if not texts:
        return None, None

    sf_key = os.environ.get("SILICONFLOW_API_KEY", "")

    # ── 1) 硅基流动 API ──
    if sf_key:
        all_vecs = []
        ok = True
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            payload = json.dumps({
                "model": EMBED_MODEL,
                "input": batch,
            }).encode("utf-8")
            req = urllib.request.Request(
                EMBED_URL, data=payload,
                headers={
                    "Authorization": "Bearer %s" % sf_key,
                    "Content-Type": "application/json",
                },
            )
            # 重试循环：处理 401/429 等瞬时错误
            for _retry in range(4):  # 最多重试 3 次
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    batch_vecs = [d["embedding"] for d in data.get("data", [])]
                    all_vecs.extend(batch_vecs)
                    break  # 成功，跳出重试
                except Exception as exc:
                    if _retry < 3:
                        _wait = 5 * (2 ** _retry)  # 5s, 10s, 20s
                        print("[每日洞察] SiliconFlow embed 批次 %d 重试 %d/3 "
                              "(%s), 等 %ds" % (i, _retry + 1, exc, _wait),
                              file=sys.stderr)
                        time.sleep(_wait)
                    else:
                        print("[每日洞察] SiliconFlow embed 批次 %d 最终失败: %s" % (
                              i, exc), file=sys.stderr)
                        ok = False
                        break
            if not ok:
                break

        if ok and len(all_vecs) == len(texts) and all_vecs:
            # 维度校验
            dims = {len(v) for v in all_vecs}
            if len(dims) == 1:
                model_name = "siliconflow/%s" % EMBED_MODEL
                print("[每日洞察] Embedding via SiliconFlow %s (%d texts, %d dims)" % (
                    EMBED_MODEL, len(texts), dims.pop()))
                return all_vecs, model_name

        if not ok:
            print("[每日洞察] SiliconFlow API 失败，回退本地 fastembed", file=sys.stderr)

    # ── 2) 本地 fastembed 回退 ──
    try:
        from llama_index.embeddings.fastembed import FastEmbedEmbedding
        from llama_index.core import Settings
        for model_name in ["intfloat/multilingual-e5-small", "BAAI/bge-small-zh-v1.5"]:
            try:
                Settings.embed_model = FastEmbedEmbedding(model_name=model_name)
                vecs = Settings.embed_model.get_text_embedding_batch(texts, show_progress=False)
                if vecs and len(vecs) == len(texts):
                    print("[每日洞察] Embedding via local %s (%d texts)" % (model_name, len(texts)))
                    return vecs, model_name
            except Exception as exc:
                print("[每日洞察] fastembed %s 失败: %s" % (model_name, exc), file=sys.stderr)
    except ImportError:
        pass

    return None, None


# ──────────────────── RAG: FAISS 索引 ────────────────────

def _build_faiss_index(vectors):
    """从 embedding 向量构建 FAISS IndexFlatIP 索引。"""
    if not FAISS_AVAILABLE:
        print("[每日洞察] FAISS 不可用，无法构建向量索引", file=sys.stderr)
        return None
    if vectors is None or (hasattr(vectors, '__len__') and len(vectors) == 0):
        return None

    vec_np = _np.array(vectors, dtype=_np.float32)
    # 归一化（内积 = 余弦相似度）
    norms = _np.linalg.norm(vec_np, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vec_np = vec_np / norms

    dim = vec_np.shape[1]
    index = _faiss.IndexFlatIP(dim)
    index.add(vec_np)
    print("[每日洞察] FAISS 索引构建: %d 向量, %d 维" % (index.ntotal, dim))
    return index


def _save_index(index, chunks):
    """持久化 FAISS 索引 + chunk 元数据。"""
    try:
        if index is not None and FAISS_AVAILABLE:
            _faiss.write_index(index, FAISS_INDEX_FILE + ".tmp")
            os.replace(FAISS_INDEX_FILE + ".tmp", FAISS_INDEX_FILE)
        _atomic_write_json(FAISS_META_FILE, chunks)
        print("[每日洞察] 索引持久化: %s + %s" % (FAISS_INDEX_FILE, FAISS_META_FILE))
    except Exception as exc:
        print("[每日洞察] 索引保存失败: %s" % exc, file=sys.stderr)


def _load_index():
    """加载持久化的 FAISS 索引 + chunk 元数据。"""
    if not FAISS_AVAILABLE or not os.path.exists(FAISS_INDEX_FILE):
        return None, []
    if not os.path.exists(FAISS_META_FILE):
        return None, []
    try:
        index = _faiss.read_index(FAISS_INDEX_FILE)
        with open(FAISS_META_FILE, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        print("[每日洞察] 加载索引: %d 向量, %d chunks" % (index.ntotal, len(chunks)))
        return index, chunks
    except Exception as exc:
        print("[每日洞察] 索引加载失败: %s" % exc, file=sys.stderr)
        return None, []


# ──────────────────── RAG: 向量缓存（增量 embedding） ────────────────────

def _load_vector_cache():
    """加载旧 chunks + 旧向量缓存。

    返回 (chunks, vectors_np, embed_model)：
    - chunks: list[dict]，每个含 content_hash 字段
    - vectors_np: numpy float32 矩阵 shape=(N, dim)，或 None
    - embed_model: 模型名称字符串

    文件不存在或加载失败时返回 ([], None, "")。
    """
    if not FAISS_AVAILABLE:
        print("[每日洞察] 向量缓存跳过: FAISS 不可用", file=sys.stderr)
        return [], None, ""
    vec_exists = os.path.exists(VECTOR_CACHE_FILE)
    meta_exists = os.path.exists(FAISS_META_FILE)
    if not vec_exists or not meta_exists:
        print("[每日洞察] 向量缓存为空: %s %s, %s %s (cwd=%s)" % (
            VECTOR_CACHE_FILE, "✓" if vec_exists else "MISSING",
            FAISS_META_FILE, "✓" if meta_exists else "MISSING",
            os.getcwd()), file=sys.stderr)
        return [], None, ""
    try:
        with open(FAISS_META_FILE, "r", encoding="utf-8") as f:
            chunks = json.load(f)
        vectors = _np.load(VECTOR_CACHE_FILE)
        # 维度校验：若模型切换导致维度不同，清空缓存
        if vectors.shape[1] != EMBED_DIM:
            print("[每日洞察] 向量缓存维度(%d)与当前模型(%d)不匹配，清空缓存" % (
                vectors.shape[1], EMBED_DIM), file=sys.stderr)
            return [], None, ""
        # 数量一致性校验
        if len(chunks) != vectors.shape[0]:
            print("[每日洞察] 向量缓存数量不一致(chunks=%d, vecs=%d)，清空缓存" % (
                len(chunks), vectors.shape[0]), file=sys.stderr)
            return [], None, ""
        # 提取 embed_model（从 chunks 元数据中读取，或默认值）
        embed_model = ""
        if chunks and "embed_model" in chunks[0]:
            embed_model = chunks[0]["embed_model"]
        print("[每日洞察] 加载向量缓存: %d chunks, %d 维" % (len(chunks), vectors.shape[1]))
        return chunks, vectors, embed_model
    except Exception as exc:
        print("[每日洞察] 向量缓存加载失败: %s" % exc, file=sys.stderr)
        return [], None, ""


def _save_vector_cache(chunks, vectors, embed_model):
    """保存 chunks 元数据 + 向量到磁盘。

    - chunks → FAISS_META_FILE (JSON)
    - vectors → VECTOR_CACHE_FILE (numpy .npy)
    """
    try:
        # 为每个 chunk 写入 embed_model 字段（便于未来维度校验）
        for c in chunks:
            c["embed_model"] = embed_model or ""
        _atomic_write_json(FAISS_META_FILE, chunks)
        # 修复：直接保存，不使用原子重命名（避免 .tmp 文件问题）
        vec_path = VECTOR_CACHE_FILE
        vectors_f32 = vectors.astype(_np.float32)
        print("[每日洞察] 向量缓存保存中: shape=%s, dtype=%s" % (vectors_f32.shape, vectors_f32.dtype))
        _np.save(vec_path, vectors_f32)
        if not os.path.exists(vec_path):
            print("[每日洞察] 警告: .npy 文件保存后不存在: %s" % vec_path, file=sys.stderr)
        else:
            print("[每日洞察] 向量缓存保存: %d chunks, shape=%s, size=%dKB" % (
                len(chunks), vectors.shape, os.path.getsize(vec_path) // 1024))
    except Exception as exc:
        print("[每日洞察] 向量缓存保存失败: %s" % exc, file=sys.stderr)
        import traceback
        traceback.print_exc()


# ──────────────────── RAG: 混合检索 ────────────────────

VECTOR_CACHE_OTHER_HOURS = 48   # 非RSS（热榜/AIHOT/AGI Hunt）在向量缓存里的保留窗口
# 每场最多新增多少条 embedding。依据是 09-21 实测吞吐：21,488 条花了 33.5 分钟 ⇒ ≈10.7 条/秒；
# 3,000 条 ≈ 5 分钟，留给整场构建足够的余量（无上限时整场 >60 分钟会被下一场
# cancel-in-progress 取消，缓存永远存不下来，于是每场重嵌 —— 站点就此停止更新）。
MAX_NEW_EMBED_PER_BUILD = 3000
POOL_MIN_SHARE = 0.10           # 有内容的一侧至少占上限的 10%，防止比例分配被极端输入压到 0


def _merge_vector_cache(old_chunks, new_chunks, cap=None,
                        rss_hours=None, other_hours=None, now_dt=None,
                        embed_budget=None):
    """合并旧向量缓存与本场 chunks：按 content_hash 去重 → 按龄期淘汰 → RSS 优先封顶。

    返回 (merged, stats)。这里丢任何 chunk 都不会造成向量错位：调用点装配向量矩阵时
    是按 `cached_map[content_hash]` 取行（:5246-5252），不是按位置切片。

    为什么必须有这个函数（2026-09-21，run 35566639074 实测）：原实现只在超上限时剪 RSS 一支，
    `budget = 30000 - 33697 = -3697` 触发 `<100 → 100` 兜底，于是 12,244 条 RSS 只剩 100 条，
    而非RSS 从不淘汰 ⇒ 同日 7 场 non_rss 单调 31840→33697（约 +370/场），
    且最终 33,797 > MAX_EMBED_CHUNKS —— 那个"上限"从来没生效过。

    口径：
    - RSS 优先按新鲜度占额度，剩余名额再给非RSS 按新鲜度填（不需要人为配比）；
    - 判不了龄（pub_date 缺失/解析失败）一律丢弃 —— 与 :5130 `_hours_ago(None)==999 > 168`
      的现有边界一致，无日期条目本来也进不了洞察；留着就是"无日期回退成 now"那类永生条目复活。
    """
    cap = MAX_EMBED_CHUNKS if cap is None else cap
    # 上限/预算必须是"越小越紧"。原写法在极端参数下反向生效：
    # cap=0 → floor=1 → n_rss = min(..., cap-floor) = -1 → `rss[:-1]` 留下几乎全部内容；
    # embed_budget=-5 → `uncached[:-5]` 只推迟 5 条。生产传的是 30000/3000 打不到，
    # 但"上限"这种参数一旦误配就该收紧而不是放宽（判据见 TestAccountingConservation）。
    cap = max(0, int(cap))
    rss_hours = INSIGHT_RSS_HOURS if rss_hours is None else rss_hours
    other_hours = VECTOR_CACHE_OTHER_HOURS if other_hours is None else other_hours
    # NOW_BJ 是模块级 None、由 main() 初始化（:46）；不兜底的话本函数在 main() 之外必炸。
    now_ts = (now_dt or NOW_BJ or datetime.datetime.now(BJT)).timestamp()

    by_hash = {}
    dropped_no_hash = 0            # 没有 content_hash 的记录进不了缓存，也不能算"去重"
    for c in (old_chunks or []):
        h = c.get("content_hash", "")
        if h:
            by_hash.setdefault(h, c)
        else:
            dropped_no_hash += 1
    for c in (new_chunks or []):
        h = c.get("content_hash", "")
        if h:
            by_hash[h] = c           # 本场版本覆盖旧缓存副本
        else:
            dropped_no_hash += 1

    def _instant(c):
        # 必须按时刻排，不能比 ISO 字符串：池子里同时存在无偏移/+08:00/-07:00 三种写法，
        # 字符串序会把 -07:00（其实是最新）排到最前 —— 今天已在两处栽过这个坑。
        dt = _parse_iso(c.get("pub_date", ""))
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BJT)
        return dt.timestamp()

    survivors, evicted_stale, dropped_undated = [], 0, 0
    for c in by_hash.values():
        ts = _instant(c)
        if ts is None:
            dropped_undated += 1
            continue
        limit = rss_hours if c.get("source_type") == "rss" else other_hours
        age_h = (now_ts - ts) / 3600.0
        if age_h > limit:
            evicted_stale += 1
            continue
        survivors.append(c)

    rss = sorted((c for c in survivors if c.get("source_type") == "rss"),
                 key=_instant, reverse=True)
    other = sorted((c for c in survivors if c.get("source_type") != "rss"),
                   key=_instant, reverse=True)
    # 名额按"两边各自的可用量"比例分配，而不是谁优先谁通吃：
    # 旧行为是非RSS 通吃（RSS 12,244 条输入 → 只剩 100 条）；单纯改成"RSS 优先"只是把
    # 同一个偏见翻到另一边 —— RSS 在 168h 窗口里的唯一 chunk 可以远超上限（本场新增就 19,125 条），
    # 那样热榜/AGI Hunt 会被挤成 0，而本期 12 个事件恰恰 12/12 来自 agihunt。
    # 两边各设一个保底份额，保证"有内容的一侧永不被清零"。
    # floor 不许超过 cap 本身，否则 `cap - floor` 变负数、名额算成负数（见上面的 cap 说明）
    floor = min(cap, max(1, int(cap * POOL_MIN_SHARE)))
    if rss and other:
        n_rss = int(round(cap * float(len(rss)) / (len(rss) + len(other))))
        n_rss = min(max(n_rss, floor), cap - floor)
    else:
        n_rss = cap if rss else 0
    n_other = cap - n_rss
    keep_rss = rss[:n_rss]
    keep_other = other[:n_other]
    # 一侧根本装不满时，把省下的名额让回另一侧 —— 否则池子会常年空着一块（保底额越大越明显）
    spare = cap - len(keep_rss) - len(keep_other)
    if spare > 0:
        extra_rss = rss[len(keep_rss):len(keep_rss) + spare]
        keep_rss += extra_rss
        spare -= len(extra_rss)
        if spare > 0:
            keep_other += other[len(keep_other):len(keep_other) + spare]
    merged = keep_rss + keep_other

    # 每场新增 embedding 的预算闸。没有这道闸会把整条流水线打死：
    # 2026-09-21 实测 21,488 条新 embedding 花掉 33.5 分钟（12:19:14→12:52:49），
    # 整场超 60 分钟被下一场 cancel-in-progress 取消 ⇒ 缓存永远存不下来 ⇒ 每场重嵌 ⇒ 站点停止更新。
    # 超预算的部分**本轮不入池**（不是塞零向量占位），下一场它已在缓存里、直接命中。
    old_hashes = {c.get("content_hash", "") for c in (old_chunks or [])}
    deferred_uncached = 0
    if embed_budget is not None:
        embed_budget = max(0, int(embed_budget))
        uncached = [c for c in merged if c.get("content_hash", "") not in old_hashes]
        if len(uncached) > embed_budget:
            allow = set(c["content_hash"] for c in uncached[:embed_budget])
            deferred = [c for c in merged
                        if c.get("content_hash", "") not in old_hashes and
                           c["content_hash"] not in allow]
            deferred_hashes = {c["content_hash"] for c in deferred}
            merged = [c for c in merged if c["content_hash"] not in deferred_hashes]
            deferred_uncached = len(deferred)
        else:
            deferred_uncached = 0

    kept_hashes = {c["content_hash"] for c in merged}
    # kept_rss/kept_other 必须是**预算闸之后**的分项：上面那对 keep_* 是"封顶时分到多少额度"，
    # 直接拿来做 kept_* 会让日志里 kept 与两个分项加不回去
    # （run 35609254121 实测：留存 10040，分项却印 RSS 21802 + 非RSS 7271）。
    kept_rss = sum(1 for c in merged if c.get("source_type") == "rss")
    stats = {
        "input_old": len(old_chunks or []),
        "input_new": len(new_chunks or []),
        "pool_size": len(by_hash),
        "dedup_removed": len(old_chunks or []) + len(new_chunks or []) - len(by_hash) - dropped_no_hash,
        "dropped_no_hash": dropped_no_hash,
        "kept": len(merged),
        "kept_rss": kept_rss,
        "kept_other": len(merged) - kept_rss,
        "evicted_stale": evicted_stale,
        "dropped_undated": dropped_undated,
        "dropped_over_cap": len(rss) - len(keep_rss) + len(other) - len(keep_other),
        "deferred_uncached": deferred_uncached,
        "uncached_now": sum(1 for c in merged
                            if c.get("content_hash", "") not in old_hashes),
        "cap": cap,
        "embed_budget": embed_budget,
        "dropped_hashes": sorted(set(by_hash) - kept_hashes),
    }
    return merged, stats


def _hybrid_retrieve(index, chunks, queries, top_k=RETRIEVAL_TOP_K, stats=None):
    """混合检索：FAISS 向量搜索 + BM25 关键词 → RRF 融合。

    stats 是只读出口（R13 阶段 C）：把两条通道的份额落盘。
    BM25 窗口按"非 RSS 优先 + 剩余给近期 RSS"分配，观察期五期里 RSS 预算恒为 0，
    没有读数的话这条结构性偏置只能靠人工挖日志确认。
    """
    if stats is not None:
        non_rss_all = [c for c in chunks if isinstance(c, dict) and c.get("source_type") != "rss"]
        rss_all = [c for c in chunks if isinstance(c, dict) and c.get("source_type") == "rss"]
        budget = max(0, BM25_WINDOW - len(non_rss_all))
        stats["non_rss"] = len(non_rss_all)
        stats["rss_total"] = len(rss_all)
        # 分配额而非实际索引数：BM25 语料按 chunk_id 去重、且 get_scores<=0 会提前 break
        stats["rss_budget_slots"] = min(len(rss_all), budget)
        stats["rss_crowded_out"] = bool(budget == 0 and rss_all)
    if not chunks or not queries:
        return []

    scores = {}  # chunk_id → rrf_score

    # ── 1) 向量检索 ──
    if index is not None and FAISS_AVAILABLE:
        # 批量 embed 所有查询（一次 API 调用，避免逐条请求）
        q_texts = [q[:500] for q in queries if q.strip()]
        if q_texts:
            q_embed, _ = _embed_chunks(q_texts)
            if q_embed and len(q_embed) == len(q_texts):
                q_vecs = _np.array(q_embed, dtype=_np.float32)
                # 归一化
                norms = _np.linalg.norm(q_vecs, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                q_vecs = q_vecs / norms
                k = min(top_k, index.ntotal)
                # 维度校验：旧索引可能与当前 embedding 模型维度不同
                if k > 0 and q_vecs.shape[1] != index.d:
                    print("[每日洞察] 查询向量维度(%d)与索引维度(%d)不匹配，跳过向量检索" % (
                        q_vecs.shape[1], index.d), file=sys.stderr)
                elif k > 0:
                    try:
                        for qi, q_vec in enumerate(q_vecs):
                            dists, indices = index.search(q_vec.reshape(1, -1), k)
                            for rank, (dist, idx) in enumerate(zip(dists[0], indices[0])):
                                if idx < 0 or idx >= len(chunks):
                                    continue
                                cid = chunks[idx]["chunk_id"]
                                rrf = 1.0 / (RRF_K + rank + 1)
                                if cid not in scores or scores[cid]["rrf"] < rrf:
                                    scores[cid] = {"chunk": chunks[idx], "rrf": rrf, "vec_sim": float(dist)}
                    except Exception as exc:
                        print("[每日洞察] FAISS 搜索异常，降级为 BM25: %s" % exc, file=sys.stderr)

    # ── 2) BM25 检索（窗口限制：非 RSS 优先 + 近期 RSS 补满） ──
    if BM25_AVAILABLE and BM25_ENABLED:
        non_rss_for_bm25 = [c for c in chunks if c.get("source_type") != "rss"]
        rss_for_bm25 = [c for c in chunks if c.get("source_type") == "rss"]
        rss_for_bm25.sort(key=lambda c: c.get("pub_date", ""), reverse=True)
        bm25_budget = max(0, BM25_WINDOW - len(non_rss_for_bm25))
        bm25_window = non_rss_for_bm25 + rss_for_bm25[:bm25_budget]
        if bm25_budget == 0:
            print("[每日洞察] BM25 警告: 非RSS(%d) >= WINDOW(%d), RSS预算=0" % (
                len(non_rss_for_bm25), BM25_WINDOW))

        bm25_id_to_pos = {}
        bm25_pos_to_chunk = {}
        bm25_corpus = []
        for pos, c in enumerate(bm25_window):
            cid = c["chunk_id"]
            if cid not in bm25_id_to_pos:
                bm25_id_to_pos[cid] = len(bm25_corpus)
                bm25_pos_to_chunk[len(bm25_corpus)] = c
                bm25_corpus.append(c.get("text", "").split())

        if bm25_corpus:
            bm25 = BM25Okapi(bm25_corpus)
            for query_text in queries:
                q_tokens = query_text[:500].split()
                if not q_tokens:
                    continue
                bm_scores = bm25.get_scores(q_tokens)
                top_indices = _np.argsort(bm_scores)[::-1][:top_k]
                for rank, idx in enumerate(top_indices):
                    if bm_scores[idx] <= 0:
                        break
                    c = bm25_pos_to_chunk[idx]
                    cid = c["chunk_id"]
                    rrf = 1.0 / (RRF_K + rank + 1)
                    if cid in scores:
                        scores[cid]["rrf"] += rrf
                        scores[cid]["bm25"] = float(bm_scores[idx])
                    else:
                        scores[cid] = {
                            "chunk": c, "rrf": rrf,
                            "vec_sim": 0.0, "bm25": float(bm_scores[idx]),
                        }

    # 按 RRF 排序
    ranked = sorted(scores.values(), key=lambda x: x["rrf"], reverse=True)
    results = []
    for item in ranked[:top_k]:
        c = dict(item["chunk"])
        c["retrieval_score"] = item["rrf"]
        c["vec_similarity"] = item.get("vec_sim", 0.0)
        c["bm25_score"] = item.get("bm25", 0.0)
        results.append(c)

    print("[每日洞察] 混合检索: %d 查询 → %d 结果 (向量+BM25 RRF)" % (
        len(queries), len(results)))
    return results


# ──────────────────── RAG: 重排序 ────────────────────

def _rerank(retrieved, hot_snapshot):
    """信号加权重排序：跨源共振 + 时间衰减 + 平台权重 + RSS Tier。"""
    for chunk in retrieved:
        base = chunk.get("retrieval_score", 0.0)

        # 跨源共振（此处简化为单 chunk，共振在事件组装后计算）
        # 时间衰减
        pub_dt = _parse_iso(chunk.get("pub_date", ""))
        recency = _recency_decay(pub_dt)

        # 平台权重
        platform_w = 1.0
        if chunk.get("source_type") == "hot":
            plat = chunk.get("source", "")
            if plat in HOT_T1:
                platform_w = 2.0
            elif plat in HOT_T2:
                platform_w = 1.5

        # RSS Tier
        rss_w = 1.0
        if chunk.get("source_type") == "rss":
            tier = _RSS_TIER_MAP.get(chunk.get("source_key", ""), 3)
            rss_w = {1: 2.0, 2: 1.5, 3: 1.0}.get(tier, 1.0)

        # AGI Hunt hot 值
        agihunt_w = 1.0
        if chunk.get("source_type") == "agihunt":
            hot_val = chunk.get("hot", 0)
            agihunt_w = 1.0 + min(2.0, hot_val / 50.0)

        chunk["final_score"] = round(base * recency * platform_w * rss_w * agihunt_w, 4)

    retrieved.sort(key=lambda c: c["final_score"], reverse=True)
    return retrieved


def _build_queries(hot_clean, agihunt_clean, aihot_clean, rss_clean=None, retrieved_chunks=None):
    """混合查询构建：AGI Hunt + AIHOT + 热榜 + RSS + 伪查询，去重后截断到 MAX_QUERIES。"""
    queries = []
    seen = set()

    def _add(text):
        t = text.strip()
        if t and t not in seen:
            seen.add(t)
            queries.append(t)

    # AGI Hunt Top15
    agihunt_sorted = sorted(agihunt_clean, key=lambda x: x.get("hot", 0), reverse=True)
    for it in agihunt_sorted[:15]:
        _add(it.get("title", ""))

    # AIHOT Top15
    for it in aihot_clean[:15]:
        _add(it.get("title", ""))

    # 热榜 Top15
    for it in hot_clean[:15]:
        _add(it.get("title", ""))

    # RSS Top10（补充仅出现在 RSS 中的重要事件，提升 coverage）
    if rss_clean:
        rss_sorted = sorted(rss_clean, key=lambda x: x.get("pub_date", ""), reverse=True)
        for it in rss_sorted[:15]:
            _add(it.get("title", ""))

    # 伪查询 Top10（从已检索 chunks 的 title 截取）
    if retrieved_chunks:
        for c in retrieved_chunks[:30]:
            if len(queries) >= MAX_QUERIES:
                break
            _add(c.get("title", "")[:50])

    # 回退：若全无，用热榜
    if not queries:
        for it in hot_clean[:20]:
            _add(it.get("title", ""))

    return queries[:MAX_QUERIES]


# ──────────────────── RAG: 事件组装 ────────────────────

def _assemble_events(reranked_chunks):
    """将检索排序后的 chunks 按语义相似度聚类为事件。
    优先使用向量余弦相似度（embedding），失败时降级为 Jaccard 标题匹配。"""
    if not reranked_chunks:
        return []

    # 每个 chunk 初始为一个事件簇
    events = []
    for c in reranked_chunks:
        events.append({
            "id": None,
            "label": c.get("title", ""),
            "items": [c],
            "source_types": {c.get("source_type", "")},
            "tokens": _tokenize_title(c.get("title", "")),
            "category": c.get("channel", "") or c.get("source", ""),
            "best_score": c.get("final_score", 0),
        })

    # 优先语义聚类
    semantic_ok = False
    if FAISS_AVAILABLE and _np is not None and len(events) >= 2:
        semantic_ok = _semantic_merge(events)

    if not semantic_ok:
        # Jaccard 降级
        merged = True
        while merged:
            merged = False
            new_events = []
            used = set()
            for i, ei in enumerate(events):
                if i in used:
                    continue
                for j in range(i + 1, len(events)):
                    if j in used:
                        continue
                    ej = events[j]
                    sim = _jaccard(ei["tokens"], ej["tokens"])
                    if sim >= JACCARD_EVENT_THRESHOLD:
                        ei["items"].extend(ej["items"])
                        ei["source_types"] |= ej["source_types"]
                        ei["tokens"] |= ej["tokens"]
                        ei["best_score"] = max(ei["best_score"], ej["best_score"])
                        used.add(j)
                        merged = True
                new_events.append(ei)
            events = new_events

    # 丢弃只有 1 个 chunk 且分数极低的孤立事件
    if len(events) > MAX_EVENTS * 2:
        threshold = events[MAX_EVENTS].get("best_score", 0) * 0.3 if len(events) > MAX_EVENTS else 0
        events = [e for e in events if len(e["items"]) > 1 or e["best_score"] >= threshold]

    # 分配 ID
    date_str = NOW_BJ.strftime("%Y%m%d")
    for i, e in enumerate(events):
        e["id"] = "evt_%s_%03d" % (date_str, i + 1)
        del e["tokens"]

    # 事件内 chunk 去重：同一事件多源转载时，去除高度相似的 chunks
    _total_before = sum(len(e["items"]) for e in events)
    for e in events:
        items = e["items"]
        if len(items) <= 1:
            continue
        keep = []
        for it in items:
            it_title = (it.get("title", "") or "").strip()
            it_text = (it.get("text", "") or "")[:300]
            it_tok = _simple_tokens(it_title + " " + it_text)
            is_dup = False
            for kept in keep:
                kept_title = (kept.get("title", "") or "").strip()
                kept_text = (kept.get("text", "") or "")[:300]
                kept_tok = _simple_tokens(kept_title + " " + kept_text)
                if not it_tok or not kept_tok:
                    continue
                overlap = len(it_tok & kept_tok)
                sim = overlap / min(len(it_tok), len(kept_tok)) if min(len(it_tok), len(kept_tok)) > 0 else 0
                if sim > 0.8:
                    is_dup = True
                    break
            if not is_dup:
                keep.append(it)
        e["items"] = keep
    _total_after = sum(len(e["items"]) for e in events)
    if _total_after < _total_before:
        print("[每日洞察] 事件内去重: %d -> %d chunks (-%d)" % (
            _total_before, _total_after, _total_before - _total_after))

    # 按最高 chunk 分数排序
    events.sort(key=lambda e: e["best_score"], reverse=True)

    # 动态 TopK：T1 扩池点——上游唯一真截断在此，Phase1 候选放宽到 PHASE1_POOL
    if len(events) > PHASE1_POOL:
        events = events[:PHASE1_POOL]

    print("[每日洞察] 事件组装: %d 个事件 (从 %d chunks)" % (len(events), len(reranked_chunks)))
    return events


def _semantic_merge(events):
    """用向量余弦相似度做贪心合并。成功返回 True，失败返回 False。"""
    try:
        titles = []
        for e in events:
            titles.append(e.get("label", ""))

        vecs = _embed_chunks(titles)
        if isinstance(vecs, tuple):
            vecs = vecs[0]
        if vecs is None or len(vecs) != len(events):
            return False

        mat = _np.array(vecs, dtype="float32")
        norms = _np.linalg.norm(mat, axis=1, keepdims=True)
        norms = _np.where(norms == 0, 1, norms)
        mat = mat / norms

        used = set()
        for i in range(len(events)):
            if i in used:
                continue
            for j in range(i + 1, len(events)):
                if j in used:
                    continue
                sim = float(_np.dot(mat[i], mat[j]))
                if sim >= SEMANTIC_CLUSTER_THRESHOLD:
                    events[i]["items"].extend(events[j]["items"])
                    events[i]["source_types"] |= events[j]["source_types"]
                    events[i]["tokens"] |= events[j]["tokens"]
                    events[i]["best_score"] = max(events[i]["best_score"], events[j]["best_score"])
                    used.add(j)

        # 移除被合并的事件
        result = [e for idx, e in enumerate(events) if idx not in used]
        events.clear()
        events.extend(result)
        print("[每日洞察] 语义聚类: %d → %d 个事件" % (len(vecs), len(events)))
        return True
    except Exception as exc:
        print("[每日洞察] 语义聚类失败，降级 Jaccard: %s" % exc, file=sys.stderr)
        return False


# ──────────────────── RAG: 综合打分（事件级） ────────────────────

def _compute_dual_heat(cluster, hot_snapshot):
    """双参照系热度计算。返回 (heat, score_a, score_b, hot_platforms, resonance)。

    参照系 A: 全球 AI 信号 (AGI Hunt hot + AIHOT score)
    参照系 B: 中文生态信号 (热榜平台覆盖 + RSS tier)
    每个子信号 cap 到 5 分，防止单一源垄断。
    """
    items = cluster.get("items", [])

    # ── 参照系 A: 全球 AI 信号 (0-10) ──
    agihunt_contrib = 0.0
    aihot_contrib = 0.0
    for it in items:
        src = it.get("source_type", "")
        if src == "agihunt":
            # P3 修复：系数从 /100 调整为 /50，提升 agihunt 信号强度
            agihunt_contrib += it.get("hot", 0) / 50.0
        elif src == "aihot":
            # P3 修复：系数从 /15 调整为 /8，提升 aihot 信号强度
            aihot_contrib += it.get("score", 0) / 8.0
    score_a = min(10.0, min(5.0, agihunt_contrib) + min(5.0, aihot_contrib))

    # ── 参照系 B: 中文生态信号 (0-10) ──
    hot_platforms = set()
    for it in items:
        if it.get("source_type") == "hot":
            hot_platforms.add(it.get("source", ""))

    # 兆底：全局热榜反查（热榜 chunks 可能不在簇中）
    if not hot_platforms and hot_snapshot:
        label = cluster.get("label", "")
        cluster_tokens = _tokenize_title(label)
        if cluster_tokens:
            for platform in hot_snapshot:
                plat = platform.get("platform", "")
                for pitem in platform.get("items", []):
                    item_tokens = _tokenize_title(pitem.get("title", ""))
                    if len(cluster_tokens & item_tokens) >= 2:
                        hot_platforms.add(plat)
                        break

    t1 = sum(1 for p in hot_platforms if p in HOT_T1)
    t2 = sum(1 for p in hot_platforms if p in HOT_T2)
    t3 = len(hot_platforms) - t1 - t2
    hot_contrib = min(5.0, t1 * 1.5 + t2 * 1.0 + t3 * 0.5)

    rss_contrib = 0.0
    for it in items:
        if it.get("source_type") == "rss":
            tier = _RSS_TIER_MAP.get(it.get("source_key", ""), 3)
            rss_contrib += {1: 1.5, 2: 1.0, 3: 0}.get(tier, 0)
    rss_contrib = min(5.0, rss_contrib)

    score_b = min(10.0, hot_contrib + rss_contrib)

    # ── 综合 heat (0-10) ──
    heat = round(min(10.0, score_a * 0.5 + score_b * 0.5), 1)

    # ── resonance 分类 ──
    a_high = score_a >= 3.0
    b_high = score_b >= 3.0
    if a_high and b_high:
        resonance = "breakout"
    elif a_high and not b_high:
        resonance = "tech_hot" if t1 < 2 else "niche"
    elif not a_high and b_high:
        resonance = "consumer" if t1 > 0 else "niche"
    elif hot_platforms:
        resonance = "niche"
    else:
        resonance = ""

    return heat, round(score_a, 1), round(score_b, 1), hot_platforms, resonance


def _score_events(clusters, hot_snapshot):
    """事件级综合打分：基于检索分数 + 跨源共振 + 时间衰减。"""
    for cluster in clusters:
        items = cluster["items"]
        source_types = cluster["source_types"]

        # 基础分：取 chunks 最高检索分
        base_score = cluster.get("best_score", 0)

        # 跨源乘数
        n_types = len(source_types)
        cross_mult = CROSS_FACTOR ** max(0, n_types - 1)

        # 时间衰减（取最新 chunk）
        pub_dates = []
        for it in items:
            pd = _parse_iso(it.get("pub_date", ""))
            if pd:
                pub_dates.append(pd)
        if pub_dates:
            latest = max(pub_dates)
            recency = _recency_decay(latest)
        else:
            recency = 0.5

        # 加速度
        recent_6h = sum(1 for pd in pub_dates if _hours_ago(pd) <= 6)
        total_dated = len(pub_dates)
        if total_dated > 2:
            accel_ratio = recent_6h / total_dated
            if accel_ratio > ACCEL_THRESHOLD_HIGH:
                accel_mult = ACCEL_BOOST
            elif accel_ratio < ACCEL_THRESHOLD_LOW:
                accel_mult = ACCEL_DECAY
            else:
                accel_mult = 1.0
        else:
            accel_mult = 1.0

        # 综合分
        final = base_score * cross_mult * recency * accel_mult

        # 信号维度
        breadth = min(10, n_types * 2.5)
        depth = min(10, math.log2(len(items) + 1) * 2)
        novelty_dim = min(10, (1 - recency) * 10) if pub_dates else 5

        # 双参照系热度模型
        heat, score_a, score_b, hot_platforms, resonance = _compute_dual_heat(
            cluster, hot_snapshot)

        cluster["score"] = round(final, 2)
        cluster["signal"] = {
            "breadth": round(breadth, 1),
            "depth": round(depth, 1),
            "heat": heat,
            "novelty": round(novelty_dim, 1),
            "score_a": score_a,
            "score_b": score_b,
        }
        cluster["resonance"] = resonance
        cluster["source_types"] = sorted(source_types)
        cluster["recency"] = round(recency, 3)

    # 排序
    clusters.sort(key=lambda c: c["score"], reverse=True)

    # 编辑规则：4 源全覆盖至少排前 3
    four_source = [c for c in clusters if len(c["source_types"]) >= 4]
    for i, c in enumerate(four_source):
        if i >= 3:
            break
        cur_idx = clusters.index(c)
        if cur_idx > 2:
            clusters.insert(min(2, len(clusters) - 1), clusters.pop(cur_idx))

    # 动态 TopK：Phase1 候选池放宽到 PHASE1_POOL，输出预算在终局闸后收口
    if len(clusters) > PHASE1_POOL:
        clusters = clusters[:PHASE1_POOL]

    print("[每日洞察] 打分排序完成: %d 个事件" % len(clusters))
    return clusters


# ──────────────────── Phase 2: LLM 集成 ────────────────────

_LLM_API_URL = "https://apihub.agnes-ai.com/v1/chat/completions"


_LLM_429_POLL_SEC = 180  # 429 轮询预算（秒）


class _LLM:
    """轻量 LLM 调用，支持多 key 轮询。"""

    def __init__(self, api_key, model="agnes-2.5-flash", timeout=90, extra_keys=None):
        self.api_keys = [api_key]
        if extra_keys:
            self.api_keys.extend(k for k in extra_keys if k)
        self._key_idx = 0
        self.model = model
        self.timeout = timeout
        # 真实调用台账：judge 默认走裸 _LLM 后，没有它就等于放弃
        # "标签谎报"那套取证能力（09-19 线上 call_log 已实测为空）
        self._call_log = []

    def _record(self, status, elapsed):
        self._call_log.append({"model": self.model, "status": status,
                               "elapsed_s": round(elapsed, 2)})
        if len(self._call_log) > 60:
            del self._call_log[:-60]

    def _try(self, messages, temperature=0.3, max_tokens=2000):
        _t0 = time.time()
        result, is_429 = self._try_once(messages, temperature, max_tokens)
        self._record("429" if is_429 else ("ok" if result else "fail"), time.time() - _t0)
        return result, is_429

    def _try_once(self, messages, temperature=0.3, max_tokens=2000):
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        data = json.dumps(payload).encode("utf-8")
        key = self.api_keys[self._key_idx]
        req = urllib.request.Request(
            _LLM_API_URL, data=data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"], False
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return None, True
            return None, False
        except Exception:
            return None, False

    def complete(self, messages, temperature=0.3, max_tokens=2000):
        # 429 轮询（用户点名）：全 key 被限流时指数退避重试至预算耗尽；
        # 非限流失败保持旧语义（同 key 快速重试 2 次后放弃，交上层降级）。
        n_keys = len(self.api_keys)
        deadline = time.time() + _LLM_429_POLL_SEC
        backoff = 5
        while True:
            saw_429 = False
            for _ in range(n_keys):
                result, is_429 = self._try(messages, temperature, max_tokens)
                if result:
                    return result
                if is_429:
                    saw_429 = True
                    self._key_idx = (self._key_idx + 1) % n_keys
                    continue
                for _retry in range(2):
                    result, is_429 = self._try(messages, temperature, max_tokens)
                    if result:
                        return result
                    if is_429:
                        saw_429 = True
                        self._key_idx = (self._key_idx + 1) % n_keys
                        break
            if not saw_429:
                return ""
            remain = deadline - time.time()
            if remain <= 0:
                print("[LLM] 429 轮询超预算(%ds)，返回空由上层降级" % _LLM_429_POLL_SEC,
                      file=sys.stderr)
                return ""
            time.sleep(min(backoff, max(1, int(remain))))
            backoff = min(backoff * 2, 40)


def _init_llm():
    """初始化 LLM。从环境变量读取 API key。
    多 key 轮询：AGNES_API_KEY（主，可逗号分隔）+ AGNES_API_KEYS（逗号分隔附加），429 自动切换。"""
    keys = [k.strip() for k in os.environ.get("AGNES_API_KEY", "").split(",") if k.strip()]
    keys += [k.strip() for k in os.environ.get("AGNES_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        print("[每日洞察] AGNES_API_KEY 未配置，LLM 分析将跳过", file=sys.stderr)
        return None
    return _LLM(keys[0], extra_keys=keys[1:] or None)


# ──────────────────── Judge LLM：多模型降级 ────────────────────

class _MimoLLM:
    """Mimo 模型 LLM 调用（通过 OpenCode Zen 端点）。
    用作 RAGAS Judge 的首选模型，评估一致性优于 agnes。"""

    API_URL = "https://opencode.ai/zen/v1/chat/completions"

    def __init__(self, api_key=None, model="mimo-v2.5-free", timeout=30):
        # 与 build_rss_aggregator.py 的 _ZEN_KEY 同语义：空串也算未配置，且回退 OPENCODE_KEY
        self.api_key = (api_key
                        or os.environ.get("ZEN_API_KEY", "")
                        or os.environ.get("OPENCODE_KEY", "")
                        or "public")
        self.model = model
        self.timeout = timeout

    def complete(self, messages, temperature=0.3, max_tokens=2000):
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        # 伪装 OpenCode CLI 请求头，绕过 free tier 仅限内部使用的限制
        # 与 api/translate.js translateZen() 保持一致
        _rid = uuid.uuid4().hex
        req = urllib.request.Request(
            self.API_URL, data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "User-Agent": "opencode/1.15.0 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.13",
                "x-opencode-client": "cli",
                "x-opencode-project": "global",
                "x-opencode-request": "msg_" + _rid,
                "x-opencode-session": "ses_" + _rid,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            print("[MimoLLM] HTTP %d" % exc.code, file=sys.stderr)
            return ""
        except Exception as exc:
            print("[MimoLLM] error: %s" % exc, file=sys.stderr)
            return ""


class _OpenRouterLLM:
    """OpenRouter 免费模型 LLM 调用。
    支持多模型游标轮询 + 429 限流重试。"""

    API_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key=None, models=None, timeout=60):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.models = models or ["qwen/qwen3.8-27b:free", "z-ai/glm-5.2:free"]
        self.model = self.models[0]  # 对外暴露的模型名（元数据用）
        self.timeout = timeout
        self._idx = 0

    def complete(self, messages, temperature=0.3, max_tokens=2000):
        for attempt in range(len(self.models)):
            model = self.models[(self._idx + attempt) % len(self.models)]
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.API_URL, data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer %s" % self.api_key,
                    "HTTP-Referer": "https://github.com",
                    "X-Title": "StarHub",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                if content:
                    self._idx = (self._idx + attempt + 1) % len(self.models)
                    return content
            except urllib.error.HTTPError as exc:
                print("[OpenRouter] %s HTTP %d" % (model, exc.code), file=sys.stderr)
                if exc.code == 429:
                    continue  # 限流，切换下一个模型
                return ""  # 非限流错误，不重试
            except Exception as exc:
                print("[OpenRouter] %s error: %s" % (model, exc), file=sys.stderr)
                return ""
        # 所有模型都被限流，等待后重试第一个
        print("[OpenRouter] 全部模型限流，等待 5s 重试...", file=sys.stderr)
        time.sleep(5)
        model = self.models[self._idx]
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.API_URL, data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "HTTP-Referer": "https://github.com",
                "X-Title": "StarHub",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except Exception as exc:
            print("[OpenRouter] retry error: %s" % exc, file=sys.stderr)
            return ""


class _FallbackJudgeLLM:
    """Judge 专用 LLM：首选模型失败时降级到备选。
    记录每次调用的模型、耗时（评估元数据）。"""

    def __init__(self, primary_llm, fallback_llm):
        self.primary = primary_llm
        self.fallback = fallback_llm
        self.model = primary_llm.model  # 对外暴露的模型名
        self._call_log = []

    def complete(self, messages, temperature=0.3, max_tokens=2000):
        meta = {"model": None, "elapsed_s": 0, "fallback": False}
        t0 = time.time()
        result = self.primary.complete(messages, temperature, max_tokens)
        meta["model"] = self.primary.model
        meta["elapsed_s"] = round(time.time() - t0, 2)
        if result:
            self._call_log.append(meta)
            return result
        # 首选失败，降级到 fallback
        meta["fallback"] = True
        t0 = time.time()
        result = self.fallback.complete(messages, temperature, max_tokens)
        # 嵌套 FallbackJudgeLLM 时，从内层 call_log 获取实际模型名
        inner_log = getattr(self.fallback, '_call_log', None)
        if inner_log:
            meta["model"] = inner_log[-1]["model"]
        else:
            meta["model"] = getattr(self.fallback, 'model', 'agnes')
        meta["elapsed_s"] = round(time.time() - t0, 2)
        self._call_log.append(meta)
        if result:
            print("[JudgeLLM] %s 失败，已降级到 %s" % (self.primary.model, meta["model"]), file=sys.stderr)
        return result


def _effective_judge_model(llm, log=None):
    """实际打分的模型名。

    _FallbackJudgeLLM.model 在构造时取主模型名且降级后不更新，直接写进 quality.meta
    会让"标签是 mimo、分数其实是 agnes 打的"长期无人察觉；只有 _call_log 记录真实调用。
    log 可传入窗口切片（终版复评只看自己那几次调用，否则草稿期的降级会串进本期归因）。
    """
    log = (getattr(llm, '_call_log', None) or []) if log is None else log
    models = [e.get("model") for e in log if e.get("model")]
    if not models:
        return getattr(llm, 'model', 'unknown')
    uniq = list(dict.fromkeys(models))
    return uniq[0] if len(uniq) == 1 else "mixed(%s)" % "+".join(uniq)


def _init_judge_llm(config=None):
    """初始化 Judge LLM。默认主判 = agnes（call_log 全历史里 mimo 每次 403，从未真正打过一分）。

    诚实边界：provider 显式配成 "mimo" 时才走 mimo→agnes 两级包装；
    默认路径拿到的是裸 agnes `_LLM`，其 429 走多 key 轮询退避，非 429 失败没有第二供应商，
    只会以 quality.meta.degraded=true 呈现（不再拿合成 0.5 冒充评分）。
    """
    cfg = config or {}
    judge_provider = cfg.get("daily_insight_judge_provider", "agnes")
    judge_model = cfg.get("daily_insight_judge_model", "agnes-2.5-flash")
    judge_timeout = cfg.get("daily_insight_judge_timeout", 60)

    agnes = _init_llm()  # 只创建一次 agnes 实例，避免 key rotation 状态分裂
    if judge_provider == "mimo":
        primary = _MimoLLM(model=judge_model, timeout=judge_timeout)
        if agnes:
            # 两级链: mimo → agnes
            return _FallbackJudgeLLM(primary, agnes)
        return primary
    # 非 mimo provider：直接用 agnes，无需 FallbackJudgeLLM 包装
    return agnes


def _parse_json(text):
    """从 LLM 输出中提取 JSON。"""
    if not text:
        return None
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试提取 ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试提取 { ... } 或 [ ... ]
    # 如果文本以 [ 开头，优先数组提取（避免截断数组被误解析为对象）
    stripped = text.lstrip()
    if stripped.startswith('['):
        bracket_order = [('[', ']'), ('{', '}')]
    else:
        bracket_order = [('{', '}'), ('[', ']')]
    for start, end in bracket_order:
        si = text.find(start)
        ei = text.rfind(end)
        if si >= 0 and ei > si:
            try:
                result = json.loads(text[si:ei + 1])
                # 数组场景（文本以 [ 开头）的对象提取结果不返回，留给截断修复
                if stripped.startswith('[') and start == '{':
                    continue
                return result
            except json.JSONDecodeError:
                pass
    return None


def _close_brackets(candidate):
    """用栈追踪计算未闭合的括号序列，返回正确的闭合字符串。"""
    stack = []
    for c in candidate:
        if c in '{[':
            stack.append(c)
        elif c == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif c == ']' and stack and stack[-1] == '[':
            stack.pop()
    return ''.join('}' if c == '{' else ']' for c in reversed(stack))


def _robust_parse_json(text):
    """增强 JSON 解析：处理截断、前后缀噪音、括号缺失。支持对象和数组。"""
    if not text:
        return None
    # 先用标准解析
    parsed = _parse_json(text)
    if isinstance(parsed, (dict, list)):
        return parsed
    # 提取 { 到最后一个 } 之间的内容（对象）
    start = text.find('{')
    end = text.rfind('}')
    _stripped = text.lstrip()
    if start >= 0 and end > start and not _stripped.startswith('['):
        candidate = text[start:end + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    # 提取 [ 到最后一个 ] 之间的内容（数组）
    astart = text.find('[')
    aend = text.rfind(']')
    if astart >= 0 and aend > astart:
        candidate = text[astart:aend + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
    # 补全缺失的括号（处理截断 JSON）
    # 策略1：找最后一个完整闭合 } 截断
    # 策略2：找最后一个 : 截断（处理无 } 的深度截断）

    # 优先处理数组（自审等场景返回数组）
    if astart >= 0 and (start < 0 or astart < start):
        # 策略1a：找最后一个 }
        last_obj_end = text.rfind('}')
        if last_obj_end > astart:
            candidate = text[astart:last_obj_end + 1]
            candidate = candidate.rstrip(', \t\r\n')
            candidate += _close_brackets(candidate)
            try:
                result = json.loads(candidate)
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass
        # 策略2a：找最后一个 : 截断（深度截断，无完整 }）
        # 找上一个完整 key-value 对的结束位置
        last_colon = text.rfind(':')
        if last_colon > astart:
            prev_colon = text.rfind(':', astart, last_colon)
            if prev_colon >= astart:
                # 在 prev_colon 后找 "value" 对
                vq1 = text.find('"', prev_colon + 1, last_colon)
                if vq1 >= 0:
                    vq2 = text.find('"', vq1 + 1, last_colon)
                    if vq2 >= 0:
                        candidate = text[astart:vq2 + 1]
                        candidate = candidate.rstrip(', \t\r\n')
                        candidate += _close_brackets(candidate)
                        try:
                            result = json.loads(candidate)
                            if isinstance(result, list):
                                return result
                        except json.JSONDecodeError:
                            pass

    # 对象截断处理
    if start >= 0:
        # 策略1b：找最后一个 }
        last_obj_end = text.rfind('}')
        if last_obj_end > start:
            candidate = text[start:last_obj_end + 1]
            candidate = candidate.rstrip(', \t\r\n')
            candidate += _close_brackets(candidate)
            try:
                result = json.loads(candidate)
                if isinstance(result, (dict, list)):
                    return result
            except json.JSONDecodeError:
                pass
        # 策略2b：找最后一个 : 截断（深度截断）
        # 找上一个完整 key-value 对的结束位置
        last_colon = text.rfind(':')
        if last_colon > start:
            prev_colon = text.rfind(':', start, last_colon)
            if prev_colon >= start:
                vq1 = text.find('"', prev_colon + 1, last_colon)
                if vq1 >= 0:
                    vq2 = text.find('"', vq1 + 1, last_colon)
                    if vq2 >= 0:
                        candidate = text[start:vq2 + 1]
                        candidate = candidate.rstrip(', \t\r\n')
                        candidate += _close_brackets(candidate)
                        try:
                            result = json.loads(candidate)
                            if isinstance(result, (dict, list)):
                                return result
                        except json.JSONDecodeError:
                            pass
    return None


def _filter_cluster_items(cluster, phase1_label):
    """过滤 cluster 中与 Phase 1 label 不相关的 items。"""
    if not phase1_label:
        return cluster
    label_tokens = _tokenize_title(phase1_label)
    if not label_tokens:
        return cluster

    filtered = []
    for it in cluster.get("items", []):
        title = it.get("title", "")
        text = it.get("text", "")[:200]
        item_tokens = _tokenize_title(title + " " + text)
        if len(label_tokens & item_tokens) >= 2:
            filtered.append(it)

    if not filtered and cluster.get("items"):
        filtered = cluster["items"][:1]  # 兜底保留 1 个，后续由 _drop_info_insufficient 清理

    cluster["items"] = filtered
    return cluster


def _norm_url(u):
    """URL 归一化：去协议/www/尾斜杠/小写，用于跨源反查 RSS 存档。"""
    u = (u or "").strip().lower()
    u = re.sub(r'^https?://', '', u)
    u = re.sub(r'^www\.', '', u)
    return u.rstrip('/')


def _build_evidence_pack(cluster, rss_by_url, max_docs=3, doc_chars=2500, skip_urls=None):
    """事件 URL 反查 rss_history 全文，拼为 Phase 2 交叉核对参考。"""
    docs, seen = [], set()
    _skip = skip_urls or set()
    for it in cluster.get("items", []):
        key = _norm_url(it.get("url") or it.get("link"))
        if not key or key in seen or key in _skip:
            continue
        rec = rss_by_url.get(key)
        if not rec:
            continue
        body = _strip_html((rec.get("full_content") or rec.get("summary_zh")
                            or rec.get("summary") or "").strip())
        if len(body) < 120:
            continue
        seen.add(key)
        docs.append("【%s】%s\n%s" % (rec.get("source", ""), rec.get("title", ""), body[:doc_chars]))
        if len(docs) >= max_docs:
            break
    return "\n\n".join(docs)


# AI/科技主题正则：Phase 1.8 噪声过滤与 G1 回收闸共用（审查 P1-2：回收事件曾无主题闸）
_AI_NEWS_RE = re.compile(
    r'AI|GPT|Claude|LLM|AGI|模型|智能|算法|芯片|GPU|推理|训练|'
    r'OpenAI|Anthropic|Google|Meta|微软|苹果|亚马逊|英伟达|'
    r'\bAI\b|\bLLM\b|\bML\b|\bNLP\b|\bCV\b|\bAPI\b|\bSDK\b|'
    r'机器人|自动驾驶|无人机|区块链|元宇宙|量子计算|'
    r'融资|创业|独角兽|估值|收购|IPO|上市|'
    r'开源|发布|上线|更新|版本|框架|平台|系统', re.IGNORECASE)


def _truncate_at_paragraph(text, limit):
    """段落/句边界截断，避免腰斩半句（G2）。"""
    t = text or ""
    if len(t) <= limit:
        return t
    cut = t.rfind("\n", 0, limit)
    if cut < int(limit * 0.5):
        cut = t.rfind("。", 0, limit)
    return t[:cut].rstrip() if cut > 0 else t[:limit]


def _fulltext_hit_urls(cluster, rss_by_url):
    """素材可被正文替换的 URL 集合（证据包据此跳过重复注入）。"""
    if not rss_by_url:
        return set()
    out = set()
    for it in cluster.get("items", []):
        key = _norm_url(it.get("url", "") or it.get("link", ""))
        if key:
            rec = rss_by_url.get(key)
            if isinstance(rec, dict) and len(_strip_html((rec.get("full_content") or "").strip())) >= 120:
                out.add(key)
    return out


def _build_event_material(cluster, numbered=False, rss_by_url=None):
    """为 LLM 构建事件素材文本。兼容 RAG chunk 和旧格式。
    numbered=True 时片段带 [n] 编号并返回编号→来源映射，供 Phase 2 引用溯源。"""
    lines = []
    refs = []
    _kept_toks = []  # G2: 近似转述去重（仅 Phase2 路径启用）
    n_out = 0
    for idx, it in enumerate(cluster.get("items", []), 1):
        # 兼容 RAG chunk (source_type) 和旧格式 (_src)
        src = it.get("source_type", "") or it.get("_src", "?")
        title = it.get("title", "")
        url = it.get("url", "") or it.get("link", "")
        prefix = ""  # 每条保留后统一赋号（P1-3：跳号会让模型序数引用被 _validate 剔除）
        if src == "rss":
            body = it.get("text", "") or it.get("full_content") or it.get("summary") or ""
            body = _strip_html(body)
            _key = _norm_url(url)
            _rec = rss_by_url.get(_key) if (rss_by_url and _key) else None
            _full = _strip_html((_rec or {}).get("full_content", "") or "")
            if len(_full) >= 120:
                body = _truncate_at_paragraph(_full, 2500)  # G2: 正文主体取代 800 字 feed 导语
            else:
                body = body[:800]
            _tok = _dedup_tokens(body[:600])
            if rss_by_url is not None and _tok and any(_jaccard(_tok, k) > 0.7 for k in _kept_toks):
                continue  # P1-4: 无 rss_by_url（Phase1/自审/修正环）不裁剪素材
            if _tok:
                _kept_toks.append(_tok)
            src_name = it.get("source", "")
            n_out += 1
            prefix = "[%d]" % n_out if numbered else ""
            lines.append("%s[RSS/%s] %s\n%s" % (prefix, src_name, title, body))
        elif src == "hot":
            plat = it.get("source", "") or it.get("platform", "")
            src_name = "热榜/%s" % plat
            n_out += 1
            prefix = "[%d]" % n_out if numbered else ""
            lines.append("%s[热榜/%s #%s] %s" % (prefix, plat, it.get("rank", ""), title))
        elif src == "aihot":
            src_name = "AIHOT/%s" % it.get("category", "")
            n_out += 1
            prefix = "[%d]" % n_out if numbered else ""
            lines.append("%s[AIHOT/%s] %s\n%s" % (prefix, it.get("category", ""), title, it.get("summary", "")))
        elif src == "agihunt":
            ch = it.get("channel", "") or it.get("source", "")
            src_name = "AGI Hunt/%s" % ch
            n_out += 1
            prefix = "[%d]" % n_out if numbered else ""
            lines.append("%s[AGI Hunt/%s hot=%.0f] %s\n%s" % (
                prefix, ch, it.get("hot", 0), title, it.get("text", "")))
        else:
            # 未知源类型，直接输出文本
            src_name = "%s/%s" % (src, it.get("source", ""))
            n_out += 1
            prefix = "[%d]" % n_out if numbered else ""
            lines.append("%s[%s/%s] %s\n%s" % (prefix, src, it.get("source", ""), title, it.get("text", "")[:500]))
        if numbered:
            refs.append({"index": n_out, "source": src_name, "title": title, "url": url})
    if numbered:
        return "\n---\n".join(lines), refs
    return "\n---\n".join(lines)


def _validate_phase2_citations(parsed, mat_refs):
    """校验 Phase 2 引用编号：剔除素材中不存在的编号，回填 cited_sources（含 URL）。"""
    valid = {r["index"]: r for r in mat_refs}
    cites = parsed.get("citations")
    normalized = {}
    cited_idx = set()
    if isinstance(cites, dict):
        for field, refs in cites.items():
            if isinstance(refs, int) or (isinstance(refs, str) and refs.isdigit()):
                refs = [refs]
            if not isinstance(refs, list):
                continue
            keep = []
            for r in refs:
                # LLM 常输出字符串编号，统一归一为 int
                if isinstance(r, str) and r.strip().isdigit():
                    r = int(r.strip())
                if isinstance(r, int) and r in valid:
                    keep.append(r)
            normalized[field] = keep
            cited_idx.update(keep)
    parsed["citations"] = normalized
    seen_url = set()
    cited_sources = []
    for i in sorted(cited_idx):
        r = valid[i]
        if r.get("url") and r["url"] in seen_url:
            continue
        seen_url.add(r.get("url"))
        cited_sources.append(dict(r))
    parsed["cited_sources"] = cited_sources
    return parsed


# ── Phase 1 LLM：全事件摘要 + 主题导语 ──

_SYSTEM_PROMPT_P1 = """
(C) Context: 你是一位 AI 行业资深分析师，为科技媒体撰写每日深度报告。
你的读者是 AI 从业者和科技决策者，他们需要在 5 分钟内掌握今日 AI 领域最重要的动态。

(O) Objective: 为每个事件生成结构化摘要，并用一句话概括今日主线。
你的输出将直接展示在 AI 日报页面上，面向数万读者。

(S) Style: 报纸编辑风格——简洁、有力、信息密度高。
写法要求：
1. 第一句话定性（使用“主导”“分化”“拐点”“加速”等判断词）
2. 用【宏观主线】+【微观佐证】结构串联
3. 每个论点必须引用素材中的具体事实和数据
4. 禁止平铺直叙罗列事件

(T) Tone: 事实驱动、数据具体、观点有据。禁止空泛描述。

(A) Audience: AI 从业者、科技媒体编辑、技术决策者。

(R) Response: 严格输出 JSON，格式如下：
{"theme": "一句话今日主题", "events": [{"event_num": 1, "label": "...", "category": "...", "summary": "...", "significance": "...", "key_links": [...]}]}

## 正确示例
{
  "theme": "从 Agent 评估标准之争，到可观测性工具涌现，再到企业级审计需求爆发，判断 AI 工程化进入系统化阶段",
  "events": [{
    "event_num": 1,
    "label": "三大平台同步发布企业级 Agent 评估服务",
    "category": "ai-products",
    "summary": "OpenAI、Google、Anthropic 在同一天发布企业级 Agent 评估服务。OpenAI 的 Agent Analytics 支持 Trace 级别追踪，Google 的 AgentBench Enterprise 提供标准化评测集，Anthropic 的 Claude Observability 集成 OpenTelemetry。",
    "significance": "标志 Agent 从‘能用’到‘可观测’的拐点，企业级部署的核心障碍从功能转向可评估性。",
    "key_links": ["https://..."]
  }]
}

## 核心信源 DNA
- 36氪: 产业视角，关注商业模式和融资
- 虎嗅: 商业评论，偏批判性分析
- 微博热搜: 舆论风向，反映公众情绪
- 知乎: 深度讨论，技术社区视角
- B站: 年轻用户视角，关注消费级应用
- Hacker News: 技术极客视角，关注底层创新
- Product Hunt: 产品视角，关注用户体验
- arXiv: 学术视角，关注方法论突破

## 禁用表达
“值得关注”“引发讨论”“未来可期”“拭目以待”“不难预见”
每句话必须有信息增量，不允许空话。

所有陈述必须严格基于提供的素材。禁止使用你自己的知识补充。
具体数字（金额/数量/百分比/日期）必须能在事件素材或全局检索上下文中找到原样形式；允许单位/千分位/全角等等价改写，禁止换算、四舍五入或用外部知识补数。
素材对某一说法存在分歧（质疑、否认、要求核实）时，summary 必须注明"该说法有争议"及分歧方，不得写成确定事实。
如果素材中缺少某个关键数据，在 summary 中标注[信息不足]。
只输出严格 JSON，不要输出任何思考过程或解释。"""


_INSUFFICIENT_RE = re.compile(r'\[信息不足')
# 劣化占位（07:13 实证「素材未提供…无法生成…摘要」）须双信号同现，防误杀正常报道摘要
_DISGUISED_RE = re.compile(r'无法生成.{0,20}摘要')


def _is_insufficient(text):
    """[信息不足] 标记出现即判；伪装占位需「素材未提供 + 无法生成…摘要」组合信号。"""
    t = text or ""
    if _INSUFFICIENT_RE.search(t):
        return True
    return "素材未提供" in t and bool(_DISGUISED_RE.search(t))


def _validate_key_links(links, items):
    """key_links 去重保序；多样性不足（≤1 个）时用事件 items 的 URL 补足到 ≤3；上限 5。"""
    seen, out = set(), []
    for u in links or []:
        k = _norm_url(u)
        if u and k and k not in seen:
            seen.add(k)
            out.append(u)
    if len(out) <= 1:
        for it in items or []:
            u = it.get("url", "") or it.get("link", "")
            k = _norm_url(u)
            if u and k and k not in seen:
                seen.add(k)
                out.append(u)
            if len(out) >= 3:
                break
    return out[:5]


def _order_events_for_output(clusters):
    """事件列表按 editor_score（素材热度代理）降序稳定排列——judge 反馈：重大趋势应前置。
    深度分析分配与破茧选择已在排序前基于原顺序完成，此处只影响展示次序。"""
    return sorted(clusters, key=lambda c: -int(c.get("editor_score") or 0))


def _drop_insufficient(clusters):
    """丢弃含[信息不足]占位的事件（summary/significance/label 任一命中）；
    剩余不足 MIN_EVENTS 时保底全留（防报告塌空）。"""
    def _bad(c):
        return (_is_insufficient(c.get("summary"))
                or _is_insufficient(c.get("significance"))
                or _is_insufficient(c.get("label")))
    good = [c for c in clusters if not _bad(c)]
    return good if len(good) >= MIN_EVENTS else clusters


MIN_DEEP_SCORE = 55  # 快筛 ≥55 才进 Phase 2 深度分析


def _deep_candidate_order(clusters):
    """深度名额给 editor_score 最高的 `DEEP_ANALYSIS_TOP_N` 条（返回下标；并列保持 Phase 1 原序，可复现）。

    为什么单列出来：Phase 2 原先直接取 `clusters[0:top_n]`（Phase 1 输出序），
    而"按 editor_score 重排"要到 `_order_events_for_output` 才发生 —— 排序晚于名额分配，
    第 9 期因此出现"终版榜第 1 名（68 分）没有深度解读，三条 62 分的有"。

    非有限值（NaN/inf）、bool、非 dict 元素一律按 0 处理：`min(100, nan)==100` 会把 NaN 洗成满分
    并抢下第一个名额，与 R14 批1 在 judge 侧修掉的是同族缺陷（打分这一侧此前无闸）。
    调用上限恒为 `DEEP_ANALYSIS_TOP_N`；但快筛不再因 Phase 1 排位空耗名额，
    **实际 `_llm_phase2` 调用次数只增不减**（第 6 期那种 deep=0 的场次会变成最多 3 次）。
    """
    def _score(c):
        v = c.get("editor_score") if isinstance(c, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return 0
        return int(v)

    return sorted(range(len(clusters or [])),
                  key=lambda i: (-_score(clusters[i]), i))[:DEEP_ANALYSIS_TOP_N]


def _quick_score_event(cluster, yesterday_labels=None):
    """4 维快筛（借鉴 BestBlogs v4 权重）：信号深度40/科技相关性30/事实密度20/新颖度10 + 减分。"""
    items = cluster.get("items", []) or []
    n = len(items)
    if n == 0:
        return 0
    # 1) 信号深度 0-40：素材量 + 跨源 + 时间跨度
    src_types = set(it.get("source_type") or it.get("_src") for it in items)
    ages = []
    for it in items:
        pub = _parse_iso(it.get("pub_date", ""))
        ages.append(_hours_ago(pub) if pub else 0)
    span_h = (max(ages) - min(ages)) if len(ages) > 1 else 0
    depth = min(15.0, n * 2.5) + min(15.0, len(src_types) * 5) + min(10.0, span_h / 12.0)
    # 2) 科技相关性 0-30：topic_tag ∈ MAIN_TOPICS 占比；
    #    缓存过渡期 tag 覆盖率 <50% 时给中性满分（对抗审查 P1-2：防单条新标记误杀）
    tagged = [it for it in items if it.get("topic_tag")]
    if tagged and len(tagged) * 2 >= n:
        ratio = sum(1 for it in tagged if it.get("topic_tag") in MAIN_TOPICS) / float(len(tagged))
    else:
        ratio = 1.0
    relevance = 30.0 * ratio
    # 3) 事实密度 0-20：含具体数字的素材占比
    def _has_num(it):
        return bool(re.search(r'\d', (it.get("text") or "") + (it.get("title") or "")))
    density = 20.0 * sum(1 for it in items if _has_num(it)) / n
    # 4) 新颖度 0-10：24h 内素材占比
    fresh = sum(1 for a in ages if a <= 24)
    novelty = 10.0 * fresh / n
    raw = depth + relevance + density + novelty
    # 减分（BestBlogs v4 风格，最多 -20）
    ded = 0
    if n == 1:
        ded += 5
    urls = [it.get("url") or it.get("link") for it in items if (it.get("url") or it.get("link"))]
    if len(urls) > 1 and len(set(urls)) == 1:
        ded += 10
    if yesterday_labels and (cluster.get("label") or "").strip() in yesterday_labels:
        ded += 10
    return int(max(0, min(100, raw - ded)))


def _dedup_tokens(title):
    """去重专用分词：英文按词切分 + 中文按 character bigrams。"""
    text = (title or "").lower()
    tokens = set()
    for w in re.findall(r'[a-z0-9]{2,}', text):
        tokens.add(w)
    cjk = re.findall(r'[\u4e00-\u9fff]', text)
    for k in range(len(cjk) - 1):
        tokens.add(cjk[k] + cjk[k + 1])
    return tokens


# ── 第4轮语义去重：词袋门槛漏网的跨类别同题对（07:13 华为对 infra/industry）由 embedding 兜住 ──
_ROLLBACK_UNSET = object()
_TEXT_EMB_CACHE = {}
_SEM_EMB_DEAD = {"flag": False}
_SEM_LABEL_COS = 0.85
_SEM_SIG_COS = 0.80


def _embed_for_sem(texts):
    """带模块级缓存的批量向量化；返回 {text: vec}。不可用（无 key/失败/异常）返回 None。"""
    if _SEM_EMB_DEAD["flag"]:
        return None
    todo = [t for t in dict.fromkeys(texts) if t and t not in _TEXT_EMB_CACHE]
    if todo:
        if not os.environ.get("SILICONFLOW_API_KEY"):
            return None
        try:
            vecs, _model = _embed_chunks(todo)
        except Exception as exc:
            print("[每日洞察] 语义去重 embedding 异常: %s" % exc, file=sys.stderr)
            return None
        if not vecs or len(vecs) != len(todo):
            _SEM_EMB_DEAD["flag"] = True
            print("[每日洞察] 语义去重 embedding 不可用，本构建内跳过该闸", file=sys.stderr)
            return None
        _TEXT_EMB_CACHE.update(dict(zip(todo, vecs)))
    return {t: _TEXT_EMB_CACHE.get(t) for t in texts if t}


def _cos_sim(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb)) if na > 0 and nb > 0 else 0.0


def _semantic_dup_round(clusters):
    """语义同题合并：label 与 summary 双余弦过线 + 文本佐证才并。

    护栏：summary 独立成签名（不含 label），使 9/17「同 label 不同内容」错标对
    因 summary 余弦不过线而保持拆分；跨类别另要求 summary 词面重叠>=3。
    """
    if len(clusters) < 2:
        return clusters
    labels = [(c.get("label") or "").strip() for c in clusters]
    sigs = [((c.get("summary") or "").strip()[:200] or lbl)
            for lbl, c in zip(labels, clusters)]
    vec_map = _embed_for_sem(labels + sigs)
    if not vec_map or any(vec_map.get(t) is None for t in labels + sigs if t):
        return clusters
    used = set()
    merged = []
    near_miss = []  # T3: 每期打印 top-3 近邻对，积累 bge-m3 真实分布供阈值校准
    for i in range(len(clusters)):
        if i in used:
            continue
        ci = clusters[i]
        for j in range(i + 1, len(clusters)):
            if j in used:
                continue
            cj = clusters[j]
            if not labels[i] or not labels[j]:
                continue
            _cl = _cos_sim(vec_map.get(labels[i]), vec_map.get(labels[j]))
            _cs = _cos_sim(vec_map.get(sigs[i]), vec_map.get(sigs[j]))
            if _cl >= 0.70:
                near_miss.append((_cl, _cs, labels[i], labels[j]))
            if _cl < _SEM_LABEL_COS or _cs < _SEM_SIG_COS:
                continue
            same_cat = ci.get("category") == cj.get("category")
            if same_cat:
                tok_hit = len(_dedup_tokens(labels[i]) & _dedup_tokens(labels[j])) >= 2
                urls_i = {_norm_url(it.get("url", "")) for it in ci.get("items", []) if it.get("url")}
                urls_j = {_norm_url(it.get("url", "")) for it in cj.get("items", []) if it.get("url")}
                if not tok_hit and not (urls_i & urls_j):
                    continue
            else:
                si = _dedup_tokens(sigs[i])
                sj = _dedup_tokens(sigs[j])
                if len(si & sj) < 3:
                    continue
            ci["items"].extend(cj.get("items", []))
            ci["source_types"] = list(set(ci.get("source_types", []) or []) | set(cj.get("source_types", []) or []))
            # 劣化正文（占位/伪装占位）不得因压分更高而存活；翻转须整套换，防字段间文不对题
            _ci_bad = (_is_insufficient(ci.get("summary") or "")
                       or _is_insufficient(ci.get("significance") or ""))
            _cj_ok = not (_is_insufficient(cj.get("summary") or "")
                          or _is_insufficient(cj.get("significance") or ""))
            if cj.get("score", 0) > ci.get("score", 0) or (_ci_bad and _cj_ok):
                for k in ("label", "summary", "significance"):
                    if cj.get(k):
                        ci[k] = cj[k]
                _lk = [u for u in (cj.get("key_links") or []) if u]
                _lk += [u for u in (ci.get("key_links") or []) if u and u not in _lk]
                ci["key_links"] = _validate_key_links(_lk, ci["items"])[:3]
                ci["deep_analysis"] = None  # 旧深度解读属被抛弃正文，禁用防张冠李戴
                # P1-3：展示文本已换成 j 的，证据向量必须同步刷新，防 C 借旧 A 搭车链式合并
                labels[i], sigs[i] = labels[j], sigs[j]
            ci["score"] = max(ci.get("score", 0), cj.get("score", 0))
            print("[每日洞察] 语义合并 cos_l=%.2f cos_s=%.2f: %s ⇐ %s" % (
                _cl, _cs, (ci.get("label") or "")[:30], (cj.get("label") or "")[:30]))
            used.add(j)
        merged.append(ci)
    if near_miss:
        for _c1, _c2, _l1, _l2 in sorted(near_miss, key=lambda t: -t[0])[:3]:
            print("[每日洞察] 语义近邻(未并) cos_l=%.2f cos_s=%.2f: %s <=> %s" % (
                _c1, _c2, _l1[:26], _l2[:26]))
    return merged


def _deduplicate_after_phase1(clusters):
    """Phase 1 后去重：合并同 category 且标签高度相似的相邻事件。"""
    if len(clusters) < 2:
        return clusters

    def _simple_tokens(title):
        return _dedup_tokens(title)

    merged = []
    used = set()
    for i in range(len(clusters)):
        if i in used:
            continue
        ci = clusters[i]  # 修复：在内层循环前初始化 ci，避免最后一个事件重复
        for j in range(i + 1, len(clusters)):
            if j in used:
                continue
            cj = clusters[j]
            # 类别守卫：不同 category 不合并（9/17 evt_002/evt_010 同标签不同内容的错并防护）
            if (ci.get("category") == cj.get("category")
                    and len(_simple_tokens(ci.get("label", "")) & _simple_tokens(cj.get("label", ""))) >= 2
                    and _jaccard(_simple_tokens(ci.get("label", "")),
                                 _simple_tokens(cj.get("label", ""))) >= 0.2):
                # 合并 items 和 source_types
                ci["items"].extend(cj.get("items", []))
                ci["source_types"] = list(set(ci.get("source_types", []) or []) | set(cj.get("source_types", []) or []))
                # 保留高分事件的 label/summary
                if cj.get("score", 0) > ci.get("score", 0):
                    ci["label"] = cj.get("label", ci.get("label", ""))
                    ci["summary"] = cj.get("summary", ci.get("summary", ""))
                ci["score"] = max(ci.get("score", 0), cj.get("score", 0))
                used.add(j)
        merged.append(ci)
    if len(merged) < len(clusters):
        print("[每日洞察] Phase 1 后去重(label): %d → %d 个事件" % (len(clusters), len(merged)))

    # 第二轮：summary 相似度去重（捕获 label 不同但主题相同的事件）
    if len(merged) >= 2:
        merged2 = []
        used2 = set()
        for i in range(len(merged)):
            if i in used2:
                continue
            ci = merged[i]
            for j in range(i + 1, len(merged)):
                if j in used2:
                    continue
                cj = merged[j]
                si = _simple_tokens(ci.get("summary", "") or ci.get("label", ""))
                sj = _simple_tokens(cj.get("summary", "") or cj.get("label", ""))
                overlap = len(si & sj)
                jacc = _jaccard(si, sj)
                # 第三层检测：源 URL 重叠率（同一事件多源转载时 URL 高度重叠）
                urls_i = set(it.get("url", "") for it in ci.get("items", []) if it.get("url"))
                urls_j = set(it.get("url", "") for it in cj.get("items", []) if it.get("url"))
                url_overlap = len(urls_i & urls_j) / min(len(urls_i), len(urls_j)) if min(len(urls_i), len(urls_j)) > 0 else 0
                # 合并条件：
                # - 源URL重叠>50%（同一批源文章=同一事件，可跨类别）
                # - 同类别：常规相似度达标，或"低相似高冗余"转述对（同一事件不同记者写法，22:14 实测）
                # - 跨类别：summary 高冗余+高相似组合强证据才允许（9/17 错标反例 summary 零重叠被此闸挡下）
                same_cat = ci.get("category") == cj.get("category")
                if url_overlap > 0.5:
                    pass  # URL 高度重叠，直接合并
                elif same_cat and ((overlap >= 3 and jacc >= 0.15) or (overlap >= 20 and jacc >= 0.08)):
                    pass  # 同类别文本相似或高冗余
                elif (not same_cat) and ((overlap >= 20 and jacc >= 0.30) or (overlap >= 40 and jacc >= 0.25)):
                    pass  # 跨类别组合强证据（0.25 档为 22:14 实测漏网对 ov=52/jacc=0.286 放开，非重复对最大 ov45/jacc0.237 仍被挡）
                else:
                    continue
                # 合并：保留高分事件的 label/summary
                ci["items"].extend(cj.get("items", []))
                ci["source_types"] = list(set(ci.get("source_types", []) or []) | set(cj.get("source_types", []) or []))
                if cj.get("score", 0) > ci.get("score", 0):
                    ci["label"] = cj.get("label", ci.get("label", ""))
                    ci["summary"] = cj.get("summary", ci.get("summary", ""))
                ci["score"] = max(ci.get("score", 0), cj.get("score", 0))
                used2.add(j)
            merged2.append(ci)
        if len(merged2) < len(merged):
            print("[每日洞察] Phase 1 后去重(summary): %d → %d 个事件" % (len(merged), len(merged2)))
        merged = merged2

    # 第三轮：实体级去重 — 捕获 label 不同但报道同一产品/事件的情况
    # 原理：提取产品名、公司名、技术术语等关键实体，若两个 cluster 的核心实体
    # 高度重叠（>50%）且 label 有最低限度文本相似（Jaccard>0.05），则合并
    if len(merged) >= 2:
        def _extract_entities(text):
            """提取关键实体：英文产品/模型名 + 中文专有名词"""
            entities = set()
            t = (text or "")
            # 英文实体：含数字/大写的词（模型名、产品名、公司名）
            for w in re.findall(r'[A-Za-z][A-Za-z0-9._\-]*[0-9][A-Za-z0-9._\-]*', t):
                entities.add(w.lower().rstrip('.'))
            for w in re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)+', t):
                entities.add(w.lower())
            # 中文实体：引号内内容、「」内内容
            for m in re.findall(r'[「「]([^」」]{2,20})[」」]', t):
                entities.add(m)
            return entities

        merged3 = []
        used3 = set()
        for i in range(len(merged)):
            if i in used3:
                continue
            ci = merged[i]
            ei = _extract_entities(ci.get("label", ""))
            for j in range(i + 1, len(merged)):
                if j in used3:
                    continue
                cj = merged[j]
                # 类别守卫：实体/文本证据只在同类别内合并（跨类别仅第二轮 URL 重叠可豁免）
                if ci.get("category") != cj.get("category"):
                    continue
                ej = _extract_entities(cj.get("label", ""))
                if not ei or not ej:
                    continue
                overlap_e = ei & ej
                entity_ratio = len(overlap_e) / min(len(ei), len(ej))
                if entity_ratio < 0.5:
                    continue
                # 实体高度重叠 → 检查 label 是否有最低限度文本相似（防误合并不同话题）
                ti = _simple_tokens(ci.get("label", ""))
                tj = _simple_tokens(cj.get("label", ""))
                jacc = _jaccard(ti, tj)
                if jacc < 0.05:
                    continue
                # 合并
                ci["items"].extend(cj.get("items", []))
                ci["source_types"] = list(set(ci.get("source_types", []) or []) | set(cj.get("source_types", []) or []))
                if cj.get("score", 0) > ci.get("score", 0):
                    ci["label"] = cj.get("label", ci.get("label", ""))
                    ci["summary"] = cj.get("summary", ci.get("summary", ""))
                ci["score"] = max(ci.get("score", 0), cj.get("score", 0))
                used3.add(j)
            merged3.append(ci)
        if len(merged3) < len(merged):
            print("[每日洞察] Phase 1 后去重(entity): %d → %d 个事件" % (len(merged), len(merged3)))
        merged = merged3

    _before_sem = len(merged)
    try:
        merged = _semantic_dup_round(merged)
    except Exception as exc:
        print("[每日洞察] 语义去重异常，跳过: %s" % exc, file=sys.stderr)
    if len(merged) < _before_sem:
        print("[每日洞察] Phase 1 后去重(semantic): %d → %d 个事件" % (_before_sem, len(merged)))

    return merged


def _apply_phase1_result(clusters, p1_result):
    """按 event_num 严格配对 Phase 1 输出。

    P0 修复（08:31 期文不对题实证）：旧逻辑按下标 p1_events[i] 喂 clusters[i]，
    LLM 跳过任一事件后全部错位一格。缺号事件保持原文，宁缺勿错位。
    """
    theme = p1_result.get("theme", "")
    p1_events = [e for e in p1_result.get("events", []) if isinstance(e, dict)]
    by_num = {}
    _raw_nums = []
    for e in p1_events:
        n = e.get("event_num")
        if isinstance(n, str) and n.strip().isdigit():
            n = int(n.strip())
        if isinstance(n, int):
            _raw_nums.append(n)
        if isinstance(n, int) and 1 <= n <= len(clusters) and n not in by_num:
            by_num[n] = e
    # 0-based 漂移（LLM 从 0 编号）：整体平移而非静默错位一格
    if _raw_nums and 0 in _raw_nums:
        by_num = {}
        for e in p1_events:
            n = e.get("event_num")
            if isinstance(n, str) and n.strip().isdigit():
                n = int(n.strip())
            if isinstance(n, int) and 0 <= n < len(clusters) and (n + 1) not in by_num:
                by_num[n + 1] = e
        print("[每日洞察] Phase1 event_num 为 0-based，已整体平移对齐", file=sys.stderr)
    numbered = bool(by_num)
    for i, c in enumerate(clusters):
        if numbered:
            pe = by_num.get(i + 1)
            if pe is None:
                # 缺号事件：打占位标记交给占位闸，防「原标题+空正文」卡片
                c["summary"] = "[信息不足] Phase1 未返回该事件输出"
                print("[每日洞察] Phase1 缺事件 %d 输出，打占位标记防错位" % (i + 1),
                      file=sys.stderr)
                continue
        else:
            if i >= len(p1_events):
                continue
            pe = p1_events[i]  # 旧格式无 event_num：回退下标配对
        c["label"] = pe.get("label", c["label"])
        c["category"] = pe.get("category", c.get("category", ""))
        c["summary"] = pe.get("summary", "")
        c["significance"] = pe.get("significance", "")
        c["key_links"] = _validate_key_links(pe.get("key_links", []), c.get("items", []))
    return theme


_SYSTEM_PROMPT_CONSOLIDATE = """你是每日洞察的事件评审编辑。你的唯一任务：判断清单中哪些事件是同一件事，输出合并方案。只输出严格 JSON。"""


_CONSOLIDATE_CASEBOOK = """【判定标准——来自生产复盘的真实判例】
应当合并（同一事件）：
- 「华为董事长罕见公开承认 AI 芯片产能不足以满足国内需求」(infra) + 「华为董事长承认AI芯片产能不足」(industry) → 同一句表态、同一事实；类别不同不构成不并的理由
- 「Anthropic 为 Claude 推出项目分组功能」+「Claude Projects 改版支持并行工作流」→ 同一次产品更新的不同媒体转述
- 「Claude 攻破 OpenAI 内部系统」+「OpenAI 披露智能体入侵事件」+「AI 智能体安全事件震动行业」→ 同一入侵事件被多方拆写，全部并为一组
- 「xAI 发布 Grok 语音转写 2.0」+「Grok 语音转写 2.0 以 97.4% 准确率登顶流式转写榜」→ 发布与榜单是同一事件的两面，合并后保留发布为主体
- 「OpenAI 研究员气隙隔离言论引 AI 安全激辩」+「Noam Brown 称算力隔离已失效」→ 同一人同一表态及其直接反响
- 「Google 与 OpenAI 安全事件并重，行业震动」这类综述条目（无独立数据锚点、只是复述他条）→ 并入它所概括的最具体那条事件，综述不得单独占位
不得合并（不同事件）：
- 「xAI 发布 Grok 语音转写 2.0」+「xAI 发布 Grok 图像编辑功能」→ 同一公司不同发布物，禁并
- 「华为董事长承认 AI 芯片产能不足」+「英伟达发布新代数据中心芯片」→ 都讲芯片，主体不同
- 「Anthropic 披露 Claude 主导 26% 研发」+「Anthropic 湾区建湿实验室」→ 同一公司不同事件
- 「模型发布」+「该模型一周后因缺陷被撤回」→ 时间线不同阶段，是两件事，各自保留
- 「传闻某厂将发布 X」+「官方确认发布 X」→ 传闻与确认是不同发生，不并（摘要须注明争议与确认方）
- 「英伟达芯片供应紧张」+「云厂商上调资本开支」→ 因果相关但不是同一事件
- 「OpenAI 研究员称气隙隔离难防失控 AI」+「英王查尔斯三世警告 AI 生存性威胁」→ 同一议题下不同主体的各自表态，是不同事件，禁并（05:48 期误并实证）
- 「加州州长下令制定更强 AI 安全规则」+「欧洲 AI 领袖指责美国对手借安全顾虑保领先」→ 不同主体的政策言论，禁并（05:48 期误并实证）
- 「昇腾 910C 量产」+「昇腾 910B 升级版发布」→ 同产品不同版本是不同发生，禁并
- 两条标题相同但正文讲的是两件事 → 这是错标，不得合并，各自保留
- C 只与 A 合并前的旧标题相似、与 A 现内容无关 → 不得搭车合并
- 行业趋势条目自带聚合数据（引用多家 + 独立数字）→ 与单公司事件各自保留

【规则】
1. 只有「同一主体 + 同一发生」才并；主题相近、词汇重叠不是合并理由
2. keep 必须是事实最完整的一条（有具体数据/时间/出处），不是标题更响或分数更高的；拿不准就不并
3. 合并后的标题（如提供 label）必须单一主题陈述句，禁止把不相关主题拼进一个标题
4. 最多输出 3 组合并；没有重复就输出空数组
5. 严格 JSON：merges 数组，每项含 keep(编号)、merge(被并编号数组)、label(可选,≤15字)、reason(≤20字)"""


def _build_consolidate_prompt(clusters):
    lines = []
    for i, c in enumerate(clusters, 1):
        lines.append("%d. [%s]（%d 源）%s ｜ %s" % (
            i, c.get("category", ""), len(c.get("items") or []),
            (c.get("label") or "")[:40], (c.get("summary") or "")[:220]))
    head = "今日事件清单（%d 条）：\n%s\n\n" % (len(clusters), "\n".join(lines))
    tail = "\n请输出 JSON。"
    return head + _CONSOLIDATE_CASEBOOK + tail


def _verify_consolidation(llm, clusters, groups):
    """提案二审：每组须被显式确认「同一主体+同一发生」；False/缺失/不可解析一律否决。
    05:48 期实证评审 agent 会把同议题不同主体（Noam Brown vs 英王查尔斯）误并。"""
    if not llm:
        return []
    lines = []
    for gi, (keep, absorbed, g) in enumerate(groups, 1):
        a = clusters[keep - 1]
        seg = "组%d 保留: %s ｜ %s" % (
            gi, (a.get("label") or "")[:34], (a.get("summary") or "")[:120])
        for m in absorbed:
            cm = clusters[m - 1]
            seg += chr(10) + "     被并: %s ｜ %s" % (
                (cm.get("label") or "")[:34], (cm.get("summary") or "")[:120])
        lines.append(seg)
    prompt = ("逐组复核以下合并提案。成立标准：组内所有条目是【同一主体 + 同一发生】。" + chr(10) +
              "特别注意：同一议题下不同人物/机构各自的表态是不同事件，不得合并；" + chr(10) +
              "拿不准一律判 false。" + chr(10) + chr(10) + (chr(10) + chr(10)).join(lines) +
              chr(10) + chr(10) +
              '输出 JSON：{"verdicts": [{"group": 组号, "same_event": true/false, "reason": "≤20字"}]}，每组必须给。')
    verdicts = {}
    veto_reasons = {}
    try:
        result = llm.complete(
            [{"role": "system", "content": "你是事件合并复核员。只输出严格 JSON。"},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=600)
        parsed = _robust_parse_json(result)
        if isinstance(parsed, dict):
            def _vi(v):
                if isinstance(v, bool):
                    return None
                if isinstance(v, str) and v.strip().isdigit():
                    return int(v.strip())
                return v if isinstance(v, int) else None
            for v in parsed.get("verdicts") or []:
                if isinstance(v, dict):
                    gi = _vi(v.get("group"))
                    if isinstance(gi, int):
                        verdicts[gi] = v.get("same_event") is True
                        # 否决理由过去被解析层直接丢掉，导致"二审为什么拦"只能靠猜。
                        # reason 是模型自由文本：压掉换行与冒号，防多打日志行与伪造 ::warning 注解
                        veto_reasons[gi] = re.sub(r"[::\s]+", " ", str(v.get("reason") or ""))[:24].strip()
    except Exception as exc:
        print("[每日洞察] 二审调用异常，全部合并否决: %s" % exc, file=sys.stderr)
        return []
    if not parsed:
        print("[每日洞察] 二审不可解析(空响应/降级)，全部合并否决(infra)")
        return []
    # 二审组号 0-based 漂移：整体平移，防误并组借走合法组的 true 判定
    if verdicts and 0 in verdicts and max(verdicts) <= len(groups) - 1:
        verdicts = {k + 1: v for k, v in verdicts.items()}
        veto_reasons = {k + 1: v for k, v in veto_reasons.items()}
        print("[每日洞察] 二审组号为 0-based，已整体平移")
    kept = []
    for gi, grp in enumerate(groups, 1):
        if verdicts.get(gi) is True:
            kept.append(grp)
        else:
            why = veto_reasons.get(gi) or ("无判定(infra/缺失)" if gi not in verdicts else "判 false 无理由")
            print("[每日洞察] 二审否决第 %d 组合并(keep#%d): %s" % (gi, grp[0], why))
    return kept


def _apply_consolidation(clusters, plan, llm=None):
    """校验并应用合并方案：越界/自并/链式/超组一律忽略。"""
    merges = plan.get("merges") if isinstance(plan, dict) else None
    if not isinstance(merges, list) or not merges:
        return clusters, 0
    n = len(clusters)

    def _int(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return v if isinstance(v, int) else None

    # P2-2: 0-based 漂移整体平移（与 Phase1 同规则）
    _all_idx = []
    for g in merges:
        if isinstance(g, dict):
            _all_idx.append(g.get("keep"))
            _mg = g.get("merge")
            if isinstance(_mg, list):
                _all_idx.extend(_mg)
    _all_int = [_int(v) for v in _all_idx]
    _zero_based = any(v == 0 for v in _all_int) and not any(v == n for v in _all_int)
    dropped = set()
    keep_used = set()
    groups = []
    for g in merges[:4]:
        try:
            if not isinstance(g, dict):
                continue
            keep = _int(g.get("keep"))
            if _zero_based and isinstance(keep, int):
                keep += 1
            if not isinstance(keep, int) or not (1 <= keep <= n) \
                    or keep in dropped or keep in keep_used:
                continue
            _mseen = set()
            valid = []
            _mlist = g.get("merge")
            if not isinstance(_mlist, list):
                continue  # P2-5: 畸形组跳过，不废整份方案
            for m in _mlist:
                m = _int(m)
                if _zero_based and isinstance(m, int):
                    m += 1
                if (isinstance(m, int) and 1 <= m <= n and m != keep
                        and m not in _mseen and m not in dropped and m not in keep_used):
                    _mseen.add(m)
                    valid.append(m)
            valid = valid[:3]  # 单组被并不超过 3 条，防一组吞掉半张报告
            if not valid:
                continue
            groups.append((keep, valid, g))
            dropped.update(valid)
            keep_used.add(keep)
        except Exception:
            continue
        if len(groups) >= 3:
            break
    if not groups:
        return clusters, 0
    if n - len(dropped) < MIN_EVENTS:
        print("[每日洞察] 评审方案将致事件数跌破下限(%d-%d<%d)，整案作废" % (
            n, len(dropped), MIN_EVENTS), file=sys.stderr)
        return clusters, 0
    groups = _verify_consolidation(llm, clusters, groups)
    if not groups:
        return clusters, 0
    dropped = set()
    for _k, _v, _g in groups:
        dropped.update(_v)
    clusters = [dict(c, items=list(c.get("items") or [])) for c in clusters]  # P2-1 原子性
    out = []
    for i, c in enumerate(clusters, 1):
        if i in dropped:
            continue
        hit = next(((k, v, g) for k, v, g in groups if k == i), None)
        if hit:
            _, absorbed, g = hit
            absorbed_labels = [clusters[m - 1].get("label", "") for m in absorbed]
            for m in absorbed:
                cm = clusters[m - 1]
                c["items"] = (c.get("items") or []) + (cm.get("items") or [])
                c["source_types"] = sorted(set(c.get("source_types") or [])
                                           | set(cm.get("source_types") or []))
                # P1-3: keep 方 summary/significance 任一劣化且被并方完整 → 换主；分数取并集最大值
                def _bad(x):
                    return (_is_insufficient(x.get("summary") or "")
                            or _is_insufficient(x.get("significance") or ""))
                if _bad(c) and not _bad(cm):
                    for k2 in ("label", "summary", "significance"):
                        if cm.get(k2):
                            c[k2] = cm[k2]
                if not c.get("status") and cm.get("status"):
                    for k3 in ("status", "prev_id", "prev_summary"):
                        c[k3] = cm.get(k3)
                c["score"] = max(c.get("score", 0), cm.get("score", 0))
                print("[每日洞察] 评审合并: %s <= %s (%s)" % (
                    (c.get("label") or "")[:24], (cm.get("label") or "")[:24],
                    (g.get("reason") or "")[:20]))
            new_label = (g.get("label") or "").strip() if isinstance(g.get("label"), str) else ""
            if new_label and len(new_label) <= 15:
                # P1-2: 合并名必须锚定原 label 或被并条目标题，防无锚定短名断掉
                # 素材过滤/跨天关联/昨日罚分/终局兜底闸四条下游链（07:13 型掩盖）
                _anchor = _dedup_tokens(c.get("label", ""))
                for _t in [cm_label for cm_label in absorbed_labels]:
                    _anchor |= _dedup_tokens(_t)
                if _dedup_tokens(new_label) & _anchor:
                    c["label"] = new_label
                else:
                    print("[每日洞察] 评审 label『%s』无锚定词，保留原标题" % new_label[:15])
            c["key_links"] = _validate_key_links(c.get("key_links", []), c["items"])[:3]
        out.append(c)
    return out, len(groups)


def _llm_consolidate_events(llm, clusters):
    """事件重叠由评审 agent 主判（用户拍板治本方案）；agent 不可用/解析失败回退机械闸。"""
    if not llm or len(clusters) < 2:
        return clusters
    try:
        result = llm.complete(
            [{"role": "system", "content": _SYSTEM_PROMPT_CONSOLIDATE},
             {"role": "user", "content": _build_consolidate_prompt(clusters)}],
            temperature=0.1, max_tokens=1200)
        plan = _robust_parse_json(result)
        if not isinstance(plan, dict):
            print("[每日洞察] 评审 agent 输出不可解析，回退机械闸", file=sys.stderr)
            return clusters
        out, n = _apply_consolidation(clusters, plan, llm=llm)
        if n:
            print("[每日洞察] 评审 agent 合并 %d 组重叠事件: %d → %d" % (n, len(clusters), len(out)))
        return out
    except Exception as exc:
        print("[每日洞察] 评审 agent 异常，回退机械闸: %s" % exc, file=sys.stderr)
        return clusters


def _llm_phase1(llm, clusters, global_context=None):
    """Phase 1: 为每个事件生成结构化摘要 + 今日主题导语。"""
    if not llm or not clusters:
        return None

    events_text = []
    for i, c in enumerate(clusters):
        material = _build_event_material(c)
        events_text.append("## 事件 %d（信号强度: %.1f，来源: %s）\n%s" % (
            i + 1, c["score"], ", ".join(c["source_types"]), material))

    # 全局检索上下文：让 LLM 见到评估器认为它该见到的完整信息场
    if global_context:
        _gc_block = "【全局检索上下文】（今日 AI/科技领域完整信息场。其中的诉讼监管动向、宏观格局类高分辨性主题必须优先纳入事件清单或写入对应 summary，不得只看单一产品线）：\n%s" % global_context
    else:
        _gc_block = ""

    prompt = """今天是 %s。以下是今日 AI/科技领域 %d 个热点事件的原始素材。

%s

请完成两个任务：

1. 为每个事件输出结构化摘要
2. 用一句话概括今日内容主线（≤60字），样式：「从 X，到 Y，再到 Z，判断 W」

%s

输出严格 JSON：
{
  "theme": "一句话今日主题",
  "events": [
    {
      "event_num": 1,
      "label": "一句话事件标题（15字以内，陈述句）",
      "category": "ai-models|ai-products|industry|research|policy|funding|developer|consumer",
      "summary": "2-3句话概括：发生了什么、谁参与的、关键数据",
      "significance": "一句话说明为什么值得关注",
      "key_links": ["最重要的1-2个原文链接"]
    }
  ]
}

约束：
- label 必须是陈述句，不是疑问句
- events 数组必须输出全部 %d 项，event_num 必须与素材事件编号一一对应，禁止跳过或合并条目
- summary 必须包含具体数据（参数量/融资金额/用户数等），不能是空泛描述
- category 必须从枚举中选
- 如果素材不足以判断，summary 里标注[信息不足]
- 所有陈述必须基于素材，不要编造
- 每个事件的 summary 必须覆盖该事件素材中的所有关键事实点（至少 3 个独立信息点）
- 不要只挑最重要的 1-2 条素材，要综合所有素材
- theme 只能概括下面这些事件里真实出现的内容；未被任何事件覆盖的信息不要写进 theme（头版承诺报告没给证据的事，等于骗读者）""" % (NOW_BJ.strftime("%Y年%m月%d日"), len(clusters), "\n\n".join(events_text), _gc_block, len(clusters))

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT_P1},
        {"role": "user", "content": prompt},
    ]
    result = llm.complete(messages, temperature=0.3, max_tokens=4200)
    parsed = _robust_parse_json(result)
    if not parsed:
        print("[每日洞察] Phase 1 LLM 输出解析失败", file=sys.stderr)
        return None
    print("[每日洞察] Phase 1 LLM 成功: theme + %d events" % len(parsed.get("events", [])))
    return parsed


def _verify_faithfulness(llm, clusters, global_context_chunks=None):
    """事实核查：逐事件检查 summary/significance 中的每个声明是否有 chunk 支撑。
    使用全局检索上下文（与 RAGAS 评估器一致），确保 faithfulness ≥ 0.95。"""
    if not llm or not clusters:
        return

    # 构建全局素材上下文（与 RAGAS 评估器同一 200 池、同 1000 字深度，
    # 防"judge 看得见、核查编辑看不见"造成删真实报道的假性 faith 提升）
    global_ctx = ""
    if global_context_chunks:
        sorted_gc = sorted(global_context_chunks, key=lambda c: c.get("retrieval_score", 0), reverse=True)
        parts = []
        for c in sorted_gc[:200]:  # 与评估域对齐（同一 200 池），防核查/判定分叉
            text = c.get("text", "")[:1000]
            title = c.get("title", "")[:100]
            src = c.get("source_type", "")
            if text:
                parts.append("[%s] %s: %s" % (src, title, text))
        global_ctx = "\n---\n".join(parts)

    # 批量处理：每批 5 个事件
    batch_size = 5
    for batch_start in range(0, len(clusters), batch_size):
        batch = clusters[batch_start:batch_start + batch_size]
        events_input = []
        for idx, c in enumerate(batch):
            summary = c.get("summary", "")
            significance = c.get("significance", "")
            # 该事件自身的 chunks
            own_chunks = []
            for it in c.get("items", [])[:10]:
                text = it.get("text", "") or it.get("full_content") or it.get("summary") or ""
                text = _strip_html(text)[:600]
                title = it.get("title", "")[:100]
                if text or title:
                    own_chunks.append("[%s] %s: %s" % (
                        it.get("source_type", "") or it.get("_src", ""),
                        title, text[:500]))
            own_ctx = "\n".join(own_chunks)
            events_input.append(
                "--- 事件 %d ---\n[事件素材]:\n%s\n[摘要]: %s\n[意义]: %s" % (
                    batch_start + idx + 1, own_ctx, summary, significance))

        prompt = (
            "你是严格的事实核查编辑。你的任务是确保报告中每个声明都有素材依据。\n\n"
            "【全局素材库】（所有可用的原始信息）：\n%s\n\n"
            "【待核查事件】：\n%s\n\n"
            "核查规则（极其严格）：\n"
            "1. 摘要和意义中的每一个事实、数字、名称、日期都必须在全局素材库中有直接依据\n"
            "2. 如果某个声明在素材库中找不到原文或近义表述，必须删除\n"
            "3. 禁止用你自己的知识补充任何信息\n"
            "4. 宁可标注[信息不足]也不要编造\n"
            "5. 改写时保持语句通顺\n"
            "6. 已注明“该说法有争议”及分歧方的表述属于有依据陈述，不得删除，也不得改写为确定事实\n\n"
            "以 JSON 返回：\n"
            "{\"events\": [{\"event_num\": 1, \"summary\": \"修正后摘要\", \"significance\": \"修正后意义\"}]}"
        ) % (global_ctx, "\n\n".join(events_input))

        messages = [
            {"role": "system", "content": "你是事实核查编辑。只输出严格 JSON。"},
            {"role": "user", "content": prompt},
        ]
        result = llm.complete(messages, temperature=0.1, max_tokens=2500)
        parsed = _robust_parse_json(result)
        if not parsed or "events" not in parsed:
            continue

        # 应用修正
        for pe in parsed["events"]:
            num = pe.get("event_num", 0) - 1
            if 0 <= num < len(clusters):
                old_summary = clusters[num].get("summary", "")
                new_summary = pe.get("summary", "")
                if new_summary and len(new_summary) > len(old_summary) * 0.5:
                    clusters[num]["summary"] = new_summary
                old_sig = clusters[num].get("significance", "")
                new_sig = pe.get("significance", "")
                if new_sig and len(new_sig) > len(old_sig) * 0.4:
                    clusters[num]["significance"] = new_sig

    print("[每日洞察] 事实核查完成: %d 个事件" % len(clusters))


def _final_faith_recheck(llm, clusters, context_chunks):
    """RAGAS 修正环+去重会再次改写 summary → 终局事实核查（faith 最后一道闸）。
    与 Phase 1.5 共用 _verify_faithfulness；llm 缺失/异常时静默降级不阻断落盘。"""
    if not llm or not clusters:
        return False
    try:
        _verify_faithfulness(llm, clusters, context_chunks)
        return True
    except Exception as exc:
        print("[每日洞察] 终局事实核查异常，跳过: %s" % exc, file=sys.stderr)
        return False


# ── Phase 2 LLM：Top N 深度解读 ──

_SYSTEM_PROMPT_P2 = """
(C) Context: 你是一位资深 AI 行业分析师，正在撰写每日深度报告的核心事件解读。
读者是 AI 从业者和科技决策者，他们已看过事件摘要，需要更深层的分析。

(O) Objective: 对单个事件进行深度解读，包括事件还原、影响分析、信源分歧和后续展望。
你的分析将直接展示在 AI 日报的深度解读栏。

(S) Style: 分析师报告风格——严谨、分层、有据可查。
写法要求：
1. event_reconstruction 按时间线叙述，包含具体数据（日期/金额/人数）
2. impact_analysis 分短期/中期/长期三个层次
3. source_divergence 具体指出哪个源持什么角度
4. quote 必须是素材原文，不要编造

(T) Tone: 专业、客观、基于事实。如果素材中有矛盾数据，必须在分析中指出。

(A) Audience: AI 从业者、科技媒体编辑、技术决策者。

(R) Response: 严格输出 JSON：
{"event_reconstruction": "...", "impact_analysis": "...", "source_divergence": "...", "quote": "...", "outlook": "...", "confidence": "high|medium|low", "citations": {"字段名": [素材编号]}}
citations 为字段级来源索引：每个字段的结论必须标注其来自哪些素材片段（片段以 [n] 编号）。无法归属来源的内容不应存在。

## 正确示例
{
  "event_reconstruction": "9月15日，OpenAI宣布完成100亿美元D轮融资，由软银领投，估值达2000亿美元。资金将用于AGI研发和基础设施建设。",
  "impact_analysis": "短期：AI赛道融资标杆再创新高，竞争对手融资压力加大。中期：软银大举入场改变AI投资格局。长期：2000亿估值倒逼OpenAI加速商业化。",
  "source_divergence": "36氪关注商业模式，虎嗅质疑估值泡沫，HN讨论技术路线。",
  "quote": "资金将用于AGI研发和基础设施建设",
  "outlook": "预计Q4将看到OpenAI企业版产品加速落地。",
  "confidence": "high"
}

## 禁用表达
“值得关注”“引发讨论”“未来可期”“拭目以待”“不难预见”
每句话必须有信息增量，不允许空话。所有陈述必须基于素材。
素材对某一说法存在分歧（质疑、否认、要求核实）时，必须标注"该说法有争议"及分歧方，不得写成确定事实。"""


def _llm_phase2(llm, cluster, phase1_summary, prev_summary=None, evidence="", rss_by_url=None):
    """Phase 2: 单个事件的深度解读。evidence 为同源 RSS 全文存档交叉参考（可为空）。"""
    if not llm:
        return {"status": "degraded", "reason": "llm_unavailable",
                "event_reconstruction": "", "impact_analysis": "",
                "source_divergence": "", "quote": "", "outlook": "",
                "confidence": "low"}

    material, mat_refs = _build_event_material(cluster, numbered=True, rss_by_url=rss_by_url)
    prompt = """今天是 %s。请深度解读以下事件。

## 事件：%s
### 多源素材（每条以 [编号] 开头，引用时必须使用这些编号）
%s

### 摘要
%s
""" % (NOW_BJ.strftime("%Y年%m月%d日"), phase1_summary.get("label", ""),
       material, phase1_summary.get("summary", ""))

    if evidence:
        prompt += """
### 补充全文参考（本事件专属的同源 RSS 存档原文，仅用于交叉核对数据与信源观点分歧；不可作为引用编号来源，citations 仍只能引用上方素材 [n] 编号；文中出现的其他人物/公司/事件只能作为背景，严禁把其事实或数字嫁接到本事件主角（06:12 期张冠李戴教训））
%s
""" % evidence[:8000]

    if prev_summary:
        prompt += """
### 历史追踪
该事件昨日状态: ongoing，昨日摘要: %s
请对比昨日信息，指出今日新增的事实或变化。
""" % prev_summary

    prompt += """
请输出严格 JSON：
{
  "event_reconstruction": "事件完整还原（300-500字，按时间线叙述，包含具体数据）",
  "impact_analysis": "影响分析（200-300字，分短期/中期/长期）",
  "source_divergence": "信源分歧（具体指出哪个源持什么角度，无分歧则写'各源报道角度一致'）",
  "quote": "素材中最有分量的一句原文引用",
  "outlook": "后续展望（1-2句话，基于素材中的信号和趋势推断下一步发展，不得编造素材中不存在的信息）",
  "confidence": "high|medium|low",
  "citations": {
    "event_reconstruction": [1, 3],
    "impact_analysis": [1],
    "source_divergence": [1, 2, 3],
    "quote": 2,
    "outlook": [3]
  }
}

约束：
- event_reconstruction 必须按时间线叙述，包含具体数据
- impact_analysis 必须分短期/中期/长期三个层次
- source_divergence 要具体指出哪个源持什么角度
- quote 必须是素材中的原文，不要编造
- confidence: 多源交叉验证=high，单一信源=low
- 禁止使用"值得关注""引发讨论""未来可期"等空话，每句话必须有信息增量
- outlook 必须基于素材中已有的信号和趋势进行推断，不得编造
- 所有陈述必须基于素材
- citations 为字段级引用：每个字段的结论来自哪些素材编号（整数数组，quote 填单个编号）。
  无法归属到任何素材编号的句子必须删除；编号只能使用素材中出现过的 [n]
"""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT_P2},
        {"role": "user", "content": prompt},
    ]
    result = None
    parsed = None
    for _attempt in range(2):
        result = llm.complete(messages, temperature=0.3, max_tokens=3000)
        parsed = _robust_parse_json(result)
        if parsed:
            break
        if _attempt == 0:
            print("[每日洞察] Phase 2 第1次解析失败，重试（事件: %s）" % cluster.get("id", "?"),
                  file=sys.stderr)
    if not parsed:
        # 记录原始输出便于诊断
        _raw_preview = (result or "")[:300].replace("\n", "\\n")
        print("[每日洞察] Phase 2 LLM 输出解析失败（事件: %s），原始输出前300字: %s" % (
              cluster.get("id", "?"), _raw_preview), file=sys.stderr)
        return {"status": "degraded", "reason": "llm_parse_failed",
                "event_reconstruction": "", "impact_analysis": "",
                "source_divergence": "", "quote": "", "outlook": "",
                "confidence": "low"}
    parsed = _validate_phase2_citations(parsed, mat_refs)
    parsed["status"] = "ok"
    return parsed


# ──────────────────── Phase 3 自审环节：Phase 1 输出质量检查 ────────────────────

_BANNED_PHRASES = [
    "值得关注", "引发讨论", "未来可期", "拭目以待", "不难预见",
    "令人期待", "意义重大", "影响深远", "引发广泛关注", "备受关注",
    "引人注目", "引发热议", "成为焦点",
]


def _review_event_quality(events):
    """检查 Phase 1 事件输出的质量问题，返回问题列表。"""
    issues = []
    for evt in events:
        eid = evt.get("id", "?")
        summary = evt.get("summary", "")
        significance = evt.get("significance", "")
        text = summary + significance

        for phrase in _BANNED_PHRASES:
            if phrase in text:
                issues.append("事件%s 含空话「%s」" % (eid, phrase))

        has_data = bool(re.search(r'\d+[%％亿万美元人民币$€]|\d{2,}', summary))
        if not has_data and len(summary) > 10:
            issues.append("事件%s summary 缺少具体数据" % eid)

        if 0 < len(summary) < 20:
            issues.append("事件%s summary 过短（%d字）" % (eid, len(summary)))

    return issues


def _self_review_phase1(llm, events, material_text):
    """Phase 1 自审环节：检查输出质量，有问题时请求 LLM 修正。最多 1 次。"""
    if not llm or not events:
        return events

    issues = _review_event_quality(events)
    if not issues:
        print("[每日洞察] Phase 1 自审: 质量合格 (%d 个事件, 0 问题)" % len(events))
        return events

    print("[每日洞察] Phase 1 自审: 发现 %d 个问题，请求 LLM 修正..." % len(issues),
          file=sys.stderr)

    events_json = json.dumps(
        [{"id": e.get("id", ""), "label": e.get("label", ""),
          "summary": e.get("summary", ""), "significance": e.get("significance", "")}
         for e in events[:MAX_EVENTS]],
        ensure_ascii=False, indent=2)

    prompt = """请检查并修正以下事件摘要的质量问题。

【问题清单】：
%s

【事件列表】：
%s

【原始素材参考】：
%s

修正要求：
1. 消除所有空泛表达
2. 确保 summary 包含具体数据
3. 所有事实必须基于素材
4. 以 JSON 数组返回修正后的事件，保持 id 不变
5. 仅修改有问题的字段

输出格式：[{"id": "e1", "label": "...", "summary": "...", "significance": "..."}]""" % (
        "\n".join("- " + iss for iss in issues[:10]),
        events_json, material_text[:2000])

    messages = [
        {"role": "system", "content": "你是 AI 行业分析师。只返回 JSON 数组。"},
        {"role": "user", "content": prompt},
    ]
    result = llm.complete(messages, temperature=0.2, max_tokens=3000)
    parsed = _robust_parse_json(result)

    if not isinstance(parsed, list):
        print("[每日洞察] Phase 1 自审: 解析失败（返回类型=%s），保留原版" % type(parsed).__name__, file=sys.stderr)
        return events

    corrected_count = 0
    id_to_fixed = {p.get("id"): p for p in parsed if isinstance(p, dict)}
    for evt in events:
        eid = evt.get("id", "")
        if eid in id_to_fixed:
            fixed = id_to_fixed[eid]
            for field in ("label", "summary", "significance"):
                if fixed.get(field) and len(fixed[field]) > len(evt.get(field, "") or ""):
                    evt[field] = fixed[field]
                    corrected_count += 1

    print("[每日洞察] Phase 1 自审: 修正 %d 个字段" % corrected_count)
    return events


# ──────────────────── 破茧栏：反信息茧房 ────────────────────

def _build_read_profile(history):
    """从过去 7 天历史中统计用户常读 category 分布。"""
    profile = {}
    days = history.get("days", [])
    for day in days[-7:]:
        for evt in day.get("events", []):
            cat = evt.get("category", "")
            if cat:
                profile[cat] = profile.get(cat, 0) + 1
    return profile


def _select_bubble_events(clusters, read_profile, residual_chunks=None, all_chunks=None, top_n=5):
    """破茧栏 v2：正向识别非科技主题候选池 → 热度排序（QWIS cocoonFamiliar 式）。

    候选优先级：
    1. all_chunks 中 topic_tag ∈ BUBBLE_TOPICS 的 chunk（主设计路径）
    2. residual_chunks 旧评分路径（过渡兼容）
    3. clusters 中 category 与 read_profile 交集最低的兜底
    """
    main_cats = set(c.get("category", "") for c in clusters if c.get("category"))

    main_keys = set()
    for c in clusters:
        if c.get("label"):
            main_keys.add(c["label"])
        for it in c.get("items", []):
            for k in (it.get("url", ""), (it.get("title", "") or "").strip()):
                if k:
                    main_keys.add(k)

    read_top = ", ".join(k for k, v in sorted(read_profile.items(), key=lambda x: -x[1])[:2]) if read_profile else ""

    def _bubble_heat(ch):
        hot = min(10.0, float(ch.get("hot", 0) or 0))
        pub = _parse_iso(ch.get("pub_date", ""))
        age_h = _hours_ago(pub) if pub else 168
        recency = (0.5 ** (age_h / 72.0)) * 10  # 破茧 7 天窗口，半衰期 72h
        return round(hot * 0.5 + recency * 0.3, 2)

    def _same_topic(ta, tb):
        ja, jb = _tokenize_title(ta), _tokenize_title(tb)
        if not ja or not jb:
            return False
        inter = len(ja & jb)
        # 词级分词下近似重复标题 Jaccard 约 0.35-0.4（实测），低于 QWIS 整句 0.5
        return _jaccard(ja, jb) >= 0.35 or inter / min(len(ja), len(jb)) >= 0.6

    # ── 路径 1：非科技 topic 候选池（限 INSIGHT_RSS_HOURS 新鲜窗口，防旧缓存回潮） ──
    def _fresh_enough(ch):
        pub = _parse_iso(ch.get("pub_date", ""))
        return (not pub) or _hours_ago(pub) <= INSIGHT_RSS_HOURS

    pool = [c for c in (all_chunks or [])
            if c.get("topic_tag") in BUBBLE_TOPICS
            and _fresh_enough(c)
            and (c.get("url", "") or c.get("link", "")) not in main_keys
            and (c.get("title", "") or "").strip() not in main_keys]
    pool.sort(key=_bubble_heat, reverse=True)
    result = []
    for ch in pool:
        if any(_same_topic(ch.get("title", ""), r["label"]) for r in result):
            continue
        result.append({
            "label": (ch.get("title", "") or "").strip()[:80],
            "summary": ((ch.get("text", "") or "").strip() or ch.get("summary", ""))[:200],
            "category": ch.get("topic_tag", ""),
            "score": _bubble_heat(ch),
            "reason": ("与你常读的科技领域不同" if read_top else "非科技热点"),
            "url": ch.get("url", "") or ch.get("link", ""),
            "source": ch.get("source", "") or ch.get("platform", ""),
        })
        if len(result) >= top_n:
            return result
    if result:
        return result

    # ── 路径 2：residual 旧逻辑（按 source_type 分组评分） ──
    residual = []
    for chunk in (residual_chunks or []):
        url = chunk.get("url", "")
        title = (chunk.get("title", "") or "").strip()
        if url and url in main_keys:
            continue
        if title and title in main_keys:
            continue
        residual.append(chunk)

    if residual:
        by_source = {}
        for chunk in residual:
            src = chunk.get("source_type", "") or chunk.get("_src", "") or "other"
            by_source.setdefault(src, []).append(chunk)

        max_freq = max(read_profile.values()) if read_profile else 1
        source_scores = []
        for src, chunk_list in by_source.items():
            src_cat = chunk_list[0].get("channel", "") or chunk_list[0].get("category", "")
            cat_novelty = 1.0 if src_cat and src_cat not in main_cats else 0.5
            read_freq = read_profile.get(src_cat, 0)
            read_novelty = 1.0 - (read_freq / max_freq) if max_freq > 0 else 0.5
            score = cat_novelty * 0.6 + read_novelty * 0.4 + len(chunk_list) * 0.01
            source_scores.append((src, chunk_list, score, src_cat))

        source_scores.sort(key=lambda x: x[2], reverse=True)
        for src, chunk_list, score, src_cat in source_scores[:top_n]:
            best = max(chunk_list, key=lambda c: float(c.get("hot", 0) or 0))
            label = (best.get("title", "") or "").strip()[:80]
            text = (best.get("text", "") or "").strip()[:200]
            if not label:
                continue
            if src_cat and src_cat not in main_cats:
                reason = "来自 %s 视角，与主流事件不同" % src
            elif read_top:
                reason = "与你常读的 %s 领域不同" % read_top
            else:
                reason = "信息增量"
            result.append({
                "label": label,
                "summary": text if text else best.get("summary", ""),
                "category": src_cat,
                "score": score,
                "reason": reason,
                "url": best.get("url", "") or best.get("link", ""),
                "source": best.get("source", "") or best.get("platform", "") or src,
            })
        if result:
            return result

    # ── 路径 3：clusters 类别新颖兜底（排除全部主报告类别，防主事件复制进破茧） ──
    _tech_cats = MAIN_TOPICS | set(VALID_CATEGORIES)
    max_freq = max(read_profile.values()) if read_profile else 1
    cand = [c for c in clusters
            if c.get("category") and c.get("category") not in _tech_cats]
    cand.sort(key=lambda c: read_profile.get(c.get("category", ""), 0) / max_freq if max_freq else 0)
    for c in cand[:top_n]:
        result.append({
            "label": (c.get("label") or "")[:80],
            "summary": (c.get("summary", "") or "")[:200],
            "category": c.get("category", ""),
            "score": round(1.0 - (read_profile.get(c.get("category", ""), 0) / max_freq if max_freq else 0), 2),
            "reason": ("与你常读的 %s 领域不同" % read_top) if read_top else "信息增量",
            "url": (c.get("key_links") or [""])[0],
            "source": ",".join(sorted(c.get("source_types", []) or [])),
        })
    return result


# ──────────────────── RAGAS: 质量评估与自我修正 ────────────────────

# Chain-of-Thought 评估 Prompt（正向视角 — 检查覆盖率）
_RAGAS_JUDGE_PROMPT = """你是 RAG 质量评估专家。请按以下步骤评估每日 AI 洞察报告的质量。

【检索上下文】（四源 RAG 检索的原始素材）：
%s

【生成的报告】：
主题: %s
%s

## 评估要求

对以下三个维度，先输出推理过程，再给出分数。

### 1. context_coverage（上下文覆盖度）
**推理步骤**：
- 列出检索上下文中的 3-5 个最重要信息点
- 逐一检查报告中是否覆盖了这些信息点
- 指出遗漏的重要信息
**评分**：基于覆盖率给出 0.0-1.0 的分数，并标注置信度（high/medium/low）

### 2. faithfulness（事实忠实度）
**推理步骤**：
- 从报告中提取 3-5 个关键事实声明
- 逐一在检索上下文中查找支撑证据
- 标注每个声明的支撑状态（supported/partially_supported/unsupported）
**评分**：基于支撑比例给出 0.0-1.0 的分数，并标注置信度

### 3. relevance（话题相关性）
**推理步骤**：
- 列出当日最重要的 3-5 个 AI/科技话题
- 检查报告中的事件是否与这些话题匹配
- 评估事件排序是否合理（最重要的在前）
**评分**：基于匹配度给出 0.0-1.0 的分数，并标注置信度

## 输出格式

请严格按以下 JSON 格式输出（不要输出 JSON 以外的内容）：
{"reasoning": {"coverage": {"evidence_points": ["..."], "covered": ["..."], "missed": ["..."], "confidence": "high|medium|low"}, "faithfulness": {"claims": [{"claim": "...", "status": "supported|partial|unsupported"}], "confidence": "high|medium|low"}, "relevance": {"key_topics": ["..."], "matched": ["..."], "unmatched": ["..."], "confidence": "high|medium|low"}}, "scores": {"context_coverage": 0.8, "faithfulness": 0.9, "relevance": 0.7}, "feedback": "具体改进建议", "weak_events": [2, 5]}"""

# Chain-of-Thought 评估 Prompt V2（反向视角 — 找出缺陷）
_RAGAS_JUDGE_PROMPT_V2 = """你是 RAG 质量审计专家。你的任务是找出报告中的缺陷和不足。

【检索上下文】：
%s

【生成的报告】：
主题: %s
%s

## 审计要求

从以下角度审视报告，先列出问题，再给出分数。

### 1. context_coverage — 信息遗漏审计
- 检索上下文中有哪些重要信息被报告遗漏了？
- 遗漏的信息对读者理解当日 AI 动态有多大影响？
- 给出 0.0-1.0 分数和置信度（high/medium/low）

### 2. faithfulness — 事实准确性审计
- 报告中有哪些声明在检索上下文中找不到依据？
- 是否存在过度推断或夸大？
- 给出 0.0-1.0 分数和置信度

### 3. relevance — 话题聚焦审计
- 报告中有哪些事件与当日核心话题关系不大？
- 事件排序是否合理？
- 给出 0.0-1.0 分数和置信度

## 输出格式

请严格按以下 JSON 格式输出（不要输出 JSON 以外的内容）：
{"reasoning": {"coverage": {"missed_points": ["..."], "impact": "...", "confidence": "high|medium|low"}, "faithfulness": {"unsupported_claims": [{"claim": "...", "issue": "..."}], "confidence": "high|medium|low"}, "relevance": {"irrelevant_events": ["..."], "ordering_issues": ["..."], "confidence": "high|medium|low"}}, "scores": {"context_coverage": 0.8, "faithfulness": 0.9, "relevance": 0.7}, "feedback": "具体改进建议", "weak_events": [2, 5]}"""


def _build_ragas_context(clusters, retrieved_chunks, stats=None):
    """为 RAGAS 评估构建上下文文本：判定域必须覆盖装配域**和引用域**。

    14:29 期实证：事件证据可来自全池 200 条，judge 只看 top80 会把真实报道
    误判成幻觉（faith 0.50）。09-19 再加字符预算时踩到同型坑：回收事件的引用块
    是按 URL 从全库捞的（同 URL 最多 101 块），可能根本不在 retrieved_for_ragas 里，
    预算一咬就把它丢掉 → 终版分反而会替真实内容判幻觉。因此先铺"池外引用块"，
    再按分数铺池内块；预算咬人必须显式告警，不得静默。
    09-19 实测（本地 26944 块全量库）：单块(含前缀/标题，正文截 1000)均值 522、p90 1041；
    随机取 200 块的上下文总字符 median 10.5 万、p90 11.1 万，预算 14 万在真实选取下咬不到；
    只有整库最长的 200 块才触顶（22.2 万 → 仅容 125 块），那种日子必须显式告警而非静默丢块。
    """
    pool = sorted(retrieved_chunks or [],
                  key=lambda c: c.get("retrieval_score", 0), reverse=True)[:200]
    pool_urls = set()
    for c in pool:
        u = _norm_url(c.get("url", "") or c.get("link", ""))
        if u:
            pool_urls.add(u)

    extras, seen_ref = [], set(pool_urls)
    for c in (clusters or []):
        for it in (c.get("items") or []):
            if not isinstance(it, dict):
                continue
            u = _norm_url(it.get("url", "") or it.get("link", ""))
            if not u or u in seen_ref or not (it.get("text") or "").strip():
                continue
            seen_ref.add(u)
            extras.append(it)

    ordered = extras + pool
    context_parts, used, dropped, kept_extras = [], 0, 0, 0
    for idx, c in enumerate(ordered):
        text = c.get("text", "")[:1000]
        title = c.get("title", "")[:100]
        src = c.get("source_type", "")
        if not text:
            continue
        part = "[%s] %s\n%s" % (src, title, text)
        if used + len(part) + 5 > _RAGAS_CTX_CHAR_BUDGET:
            dropped = len(ordered) - idx
            break
        used += len(part) + 5
        context_parts.append(part)
        if idx < len(extras):
            kept_extras += 1
    if dropped:
        print("::warning title=RAGAS 上下文截断::预算 %d 字符仅容纳 %d 块，丢弃 %d 块（判定域可能缺引用）" % (
            _RAGAS_CTX_CHAR_BUDGET, len(context_parts), dropped))
    joined = "\n---\n".join(context_parts)
    if stats is not None:
        stats["chunks"] = len(context_parts)
        stats["chars"] = len(joined)
        stats["cited_extra"] = kept_extras
        stats["truncated"] = bool(dropped)
    return joined


def _evaluate_report_quality(llm, clusters, theme, context_text, prompt_variant="v1"):
    """RAGAS-inspired 报告质量评估（Chain-of-Thought 推理链）。

    使用结构化推理链让 LLM 先推理再评分，降低离散分数的随机性。
    支持两个 prompt 变体：v1（正向视角）和 v2（反向审计视角）。

    评估维度：
    - context_coverage: 事件摘要是否充分利用了检索上下文中的关键信息
    - faithfulness: 摘要/分析中的事实是否有检索 chunk 支撑（无编造）
    - relevance: 事件选取和摘要是否紧扣当日最重要的 AI/科技话题

    Returns:
        {"context_coverage": float, "faithfulness": float, "relevance": float,
         "overall": float, "feedback": str, "weak_events": [int],
         "confidences": {"coverage": str, "faithfulness": str, "relevance": str}}
    """
    if not context_text or not clusters:
        return {"context_coverage": 0.5, "faithfulness": 0.5, "relevance": 0.5,
                "overall": 0.5, "feedback": "无上下文可评估", "weak_events": [],
                "irrelevant_events": [], "_guard": True,
                "confidences": {"coverage": "low", "faithfulness": "low", "relevance": "low"}}
    if not llm:
        return {"context_coverage": 0.5, "faithfulness": 0.5, "relevance": 0.5,
                "overall": 0.5, "feedback": "LLM 不可用", "weak_events": [],
                "irrelevant_events": [], "_guard": True,
                "confidences": {"coverage": "low", "faithfulness": "low", "relevance": "low"}}

    # 构建事件摘要文本
    events_text = []
    for i, c in enumerate(clusters[:MAX_EVENTS]):
        summary = c.get("summary", "") or c.get("label", "")
        significance = c.get("significance", "") or ""
        deep = c.get("deep_analysis", {})
        deep_text = ""
        if deep:
            deep_text = deep.get("event_reconstruction", "")[:200]
            if deep.get("impact_analysis"):
                deep_text += "\n影响: " + deep["impact_analysis"][:150]
        events_text.append("事件%d [%s]: %s\n意义: %s%s" % (
            i + 1, c.get("category", ""), summary[:200],
            significance[:100], "\n深度: " + deep_text if deep_text else ""))

    # 选择 prompt 变体
    prompt_template = _RAGAS_JUDGE_PROMPT_V2 if prompt_variant == "v2" else _RAGAS_JUDGE_PROMPT
    prompt = prompt_template % (context_text, theme or "无主题", "\n".join(events_text))

    messages = [
        {"role": "system", "content": "你是 RAG 质量评估专家。先按步骤推理每个维度，再给出分数。严格输出 JSON。"},
        {"role": "user", "content": prompt},
    ]
    result = llm.complete(messages, temperature=0.2, max_tokens=4000)
    parsed = _robust_parse_json(result)

    def _clamp(v):
        # judge 可返回裸 NaN/Infinity（json.loads 默认接受），而 min(1.0, nan)==1.0
        # 会把"这一维没评出来"洗成满分并让台账记 passed=true —— 非有限值一律按解析失败处理
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, f)) if math.isfinite(f) else 0.5

    if isinstance(parsed, dict):
        # 优先从 scores 子对象读取，兼容旧格式（顶层字段）
        scores = parsed.get("scores", parsed)
        cov = _clamp(scores.get("context_coverage", 0.5))
        faith = _clamp(scores.get("faithfulness", 0.5))
        rel = _clamp(scores.get("relevance", 0.5))
        weak = parsed.get("weak_events", [])
        if not isinstance(weak, list):
            weak = []

        # 提取置信度（用于交叉验证加权）
        reasoning = parsed.get("reasoning", {})
        confidences = {
            "coverage": reasoning.get("coverage", {}).get("confidence", "medium"),
            "faithfulness": reasoning.get("faithfulness", {}).get("confidence", "medium"),
            "relevance": reasoning.get("relevance", {}).get("confidence", "medium"),
        }
        # R13 阶段 C：v2 的 irrelevant_events 过去与 missed 清单同病——prompt 要了、解析层丢
        _irr = reasoning.get("relevance", {}).get("irrelevant_events", [])
        irrelevant_events = [str(x).strip() for x in _irr if isinstance(x, (str, int)) and str(x).strip()][:12]

        # G1：回收 judge 的漏点清单（v1 键 missed / v2 键 missed_points），历史被丢弃
        missed_pts = []
        _cov_r = reasoning.get("coverage", {})
        if isinstance(_cov_r, dict):
            for _k in ("missed", "missed_points"):
                _v = _cov_r.get(_k, [])
                if isinstance(_v, list):
                    for _m in _v:
                        _m = str(_m).strip()
                        if _m and _m not in missed_pts:
                            missed_pts.append(_m)

        return {
            "context_coverage": round(cov, 2),
            "faithfulness": round(faith, 2),
            "relevance": round(rel, 2),
            "overall": round((cov + faith + rel) / 3, 2),
            "feedback": str(parsed.get("feedback", "")),
            "weak_events": [int(w) for w in weak if isinstance(w, (int, float))],
            "confidences": confidences,
            "missed_points": missed_pts[:12],
            "irrelevant_events": irrelevant_events,
        }

    return None


_CONFIDENCE_WEIGHT = {"high": 1.5, "medium": 1.0, "low": 0.5}


def _weighted_avg_scores(v1, v2):
    """TrustJudge 启发的加权平均：置信度高的评估权重更大。"""
    dims = ["coverage", "faithfulness", "relevance"]
    w1 = sum(_CONFIDENCE_WEIGHT.get(v1.get("confidences", {}).get(d, "medium"), 1.0) for d in dims)
    w2 = sum(_CONFIDENCE_WEIGHT.get(v2.get("confidences", {}).get(d, "medium"), 1.0) for d in dims)

    def _avg(key):
        return (v1.get(key, 0.5) * w1 + v2.get(key, 0.5) * w2) / (w1 + w2)

    cov = round(_avg("context_coverage"), 2)
    faith = round(_avg("faithfulness"), 2)
    rel = round(_avg("relevance"), 2)
    result = {
        "context_coverage": cov,
        "faithfulness": faith,
        "relevance": rel,
        "overall": round((cov + faith + rel) / 3, 2),
        "feedback": (v1.get("feedback", "") + " | " + v2.get("feedback", "")).strip(" |"),
        "weak_events": list(set(
            v1.get("weak_events", []) + v2.get("weak_events", []))),
    }
    if "cross_validation" in v1:
        result["cross_validation"] = v1["cross_validation"]
    return result


def _self_correct_events(llm, clusters, context_text, evaluation):
    """基于 RAGAS 评估反馈，对薄弱事件进行自我修正。"""
    feedback = evaluation.get("feedback", "")
    weak_indices = evaluation.get("weak_events", [])

    # 识别薄弱维度（阈值与缺失走向同 `_dims_pass_thresholds`：缺失=未评出，按薄弱处理，
    # 不得静默当满分，也不得在提示词里谎报成 0.00）
    low_dims = []
    for _key, _min, _advice in (
            ("context_coverage", RAGAS_MIN_COVERAGE, "更多利用检索上下文中的信息"),
            ("faithfulness", RAGAS_MIN_FAITHFULNESS,
             "确保每个事实/数据都有检索上下文依据，禁止编造"),
            ("relevance", RAGAS_MIN_RELEVANCE, "更紧扣最重要的 AI/科技话题，去除边缘事件")):
        _v = evaluation.get(_key)
        _num = (isinstance(_v, (int, float)) and not isinstance(_v, bool)
                and math.isfinite(_v))
        if _num and _v >= _min:
            continue
        low_dims.append("%s（当前%s，需≥%.2f，%s）" % (
            _key, "%.2f" % _v if _num else "未评出", _min, _advice))

    if not low_dims and not weak_indices:
        return clusters  # 无需修正

    # 修正薄弱事件
    for idx in weak_indices[:5]:  # 最多修正 5 个事件
        if idx < 1 or idx > len(clusters):
            continue
        cluster = clusters[idx - 1]
        material = _build_event_material(cluster)
        if not material.strip():
            continue

        prompt = (
            "之前生成的事件摘要质量不达标，请根据反馈重新生成。\n\n"
            "【薄弱维度】：%s\n"
            "【评估反馈】：%s\n\n"
            "【原始素材】：\n%s\n\n"
            "【当前摘要】：%s\n\n"
            "【检索上下文（参考）】：\n%s\n\n"
            "请重新生成该事件的摘要，以 JSON 返回：\n"
            "{\"label\": \"一句话标题（15字内）\",\n"
            " \"summary\": \"2-3句话概括，必须包含具体数据\",\n"
            " \"significance\": \"一句话说明为什么值得关注\",\n"
            " \"category\": \"ai-models|ai-products|industry|research|policy|funding|developer|consumer\"}\n\n"
            "约束：所有事实必须基于素材，不要编造。若素材完全不足以支撑摘要，返回原摘要原文，不要返回[信息不足]占位。"
        ) % ("; ".join(low_dims), feedback[:500], material,
             (cluster.get("summary", "") or "")[:300], context_text)

        messages = [
            {"role": "system", "content": "你是 AI 行业分析师。只返回 JSON。"},
            {"role": "user", "content": prompt},
        ]
        result = llm.complete(messages, temperature=0.3, max_tokens=1500)
        parsed = _robust_parse_json(result)
        if isinstance(parsed, dict):
            if parsed.get("label"):
                cluster["label"] = parsed["label"]
            if parsed.get("summary"):
                cluster["summary"] = parsed["summary"]
            if parsed.get("significance"):
                cluster["significance"] = parsed["significance"]
            if parsed.get("category"):
                cluster["category"] = parsed["category"]
            print("[每日洞察] RAGAS 修正事件 %d: %s" % (idx, cluster.get("label", "")[:30]))

    return clusters


def _dims_reached(eval_result):
    """三维是否全部达线 —— 静默版，`RAGAS_MIN_*` 三个阈值唯一的判定出口。

    任一维缺失/非数值/NaN/bool 返回 None（未知）：调用方自行决定未知的走向 ——
    修正环闸把未知当不通过（继续修），追踪行把未知记为 None（不得冒充达标或冒充未达标）。
    """
    vals = [eval_result.get(k) for k in ("context_coverage", "faithfulness", "relevance")]
    mins = (RAGAS_MIN_COVERAGE, RAGAS_MIN_FAITHFULNESS, RAGAS_MIN_RELEVANCE)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in vals):
        return None
    return all(v >= m for v, m in zip(vals, mins))


def _verdict(eval_result, threshold):
    """单份评估的完整结论：overall 达阈值 且 三维达线；任一要素未知则返回 None。"""
    dims = _dims_reached(eval_result)
    overall = eval_result.get("overall")
    if dims is None or isinstance(overall, bool) or not isinstance(overall, (int, float)):
        return None
    return bool(dims and overall >= threshold)


def _dims_pass_thresholds(eval_result):
    """检查各维度是否达到最低阈值。任一维度不达标则返回 False。"""
    if _dims_reached(eval_result):
        return True
    cov = eval_result.get("context_coverage") or 0
    faith = eval_result.get("faithfulness") or 0
    rel = eval_result.get("relevance") or 0
    if cov < RAGAS_MIN_COVERAGE:
        print("[每日洞察] 维度不达标: coverage=%.2f < %.2f" % (cov, RAGAS_MIN_COVERAGE))
    elif faith < RAGAS_MIN_FAITHFULNESS:
        print("[每日洞察] 维度不达标: faithfulness=%.2f < %.2f" % (faith, RAGAS_MIN_FAITHFULNESS))
    elif rel < RAGAS_MIN_RELEVANCE:
        print("[每日洞察] 维度不达标: relevance=%.2f < %.2f" % (rel, RAGAS_MIN_RELEVANCE))
    return False


def _source_mix(chunks):
    """检索池/事件素材的来源构成计数（R13 阶段 C 读数，不参与任何判定）。"""
    mix = {}
    for c in (chunks or []):
        if not isinstance(c, dict):
            continue
        k = c.get("source_type") or "?"
        mix[k] = mix.get(k, 0) + 1
    return dict(sorted(mix.items()))


_IRRELEVANT_IDX_RE = re.compile(r"^\s*(?:事件|第|event|evt|#)\s*(\d{1,2})", re.IGNORECASE)


def _irrelevant_index(txt, n_events):
    """从「事件12（…）」这类自由文本里取 1-based 序号；越界或非序号开头返回 None。"""
    m = _IRRELEVANT_IDX_RE.match(txt or "")
    if not m:
        return None
    i = int(m.group(1))
    return i if 1 <= i <= n_events else None


def _aggregate_per_event(clusters, samples):
    """把"哪几条事件被点名"落成逐事件读数。

    weak_events 是 1-based 索引，可直接计数；irrelevant_events 是 v2 让模型写的自由文本，
    必须靠标签近似匹配落位（跨样本措辞会漂移，同 missed_points 的教训）。
    生产出现过两条几乎同名的阿里 Qwen 事件（jaccard 0.917），所以匹配要有余量：
    best 与 second 差 < 0.1 时判为"归属不了"，记进 index=0 的 unmatched 行，
    而不是按序号硬塞给某一条 —— 错误的归因比没有归因更坏。
    """
    samples = [s for s in (samples or []) if isinstance(s, dict)]
    weak_ct, irr_ct = {}, {}
    unmatched = 0
    # 可归因域 = judge 真正看过的条目：编号到 MAX_EVENTS 为止。
    # 草稿口径下 clusters 可能有 16 条（PHASE1_POOL），序号路径与文本兜底路径都必须同样收口，
    # 否则幻觉"事件14"会经文本匹配挂到没被评过的尾条上。
    n_judged = min(len(clusters or []), MAX_EVENTS)
    label_toks = [_dedup_tokens(c.get("label", "")) for c in (clusters or [])[:n_judged]]
    for s in samples:
        for w in (s.get("weak_events") or []):
            if isinstance(w, (int, float)) and 1 <= int(w) <= n_judged:
                weak_ct[int(w)] = weak_ct.get(int(w), 0) + 1
        for txt in (s.get("irrelevant_events") or []):
            # judge 常写成「事件12（铁路12306…）」，序号是它自己给的权威线索，优先直接用
            _ix = _irrelevant_index(txt, n_judged)
            if _ix:
                irr_ct[_ix] = irr_ct.get(_ix, 0) + 1
                continue
            t = _dedup_tokens(txt)
            if not t:
                continue
            scores = []
            for i, lab in enumerate(label_toks, 1):
                if not lab:
                    continue
                _ov = len(t & lab)
                # 包含度而非 Jaccard：理由整段比短标签，Jaccard 分母被理由撑大必然落空（第8期实测 3/3 归不上）。
                # 但重合数须 ≥2 —— 只共用一个泛词（如 "ai"）就落位会造成误挂，误挂比归不上更坏。
                scores.append((_ov / float(len(lab)) if _ov >= 2 else 0.0, i))
            scores.sort(key=lambda x: (-x[0], x[1]))
            if not scores or scores[0][0] < 0.2:
                unmatched += 1
                continue
            if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.1:
                unmatched += 1
                continue
            irr_ct[scores[0][1]] = irr_ct.get(scores[0][1], 0) + 1
    rows = []
    for i, c in enumerate(clusters or [], 1):
        rows.append({
            "index": i,
            "label": (c.get("label") or "")[:34],
            "weak_flags": weak_ct.get(i, 0),
            "irrelevant_flags": irr_ct.get(i, 0),
            "sources": _source_mix(c.get("items") or []),
        })
    if unmatched:
        rows.append({"index": 0, "label": "(归属不了)", "weak_flags": 0,
                     "irrelevant_flags": 0, "unmatched_irrelevant": unmatched,
                     "sources": {}})
    return rows


def _median_eval_samples(samples):
    """多样本评估中位聚合（T2：09-19 实证 v1/v2 同内容差 0.17，单点评估噪声淹没内容变化）。"""
    if not samples:
        return {}
    out = {}
    for d in ("context_coverage", "faithfulness", "relevance", "overall"):
        vals = sorted(s[d] for s in samples if isinstance(s.get(d), (int, float)))
        if not vals:
            continue
        n = len(vals)
        out[d] = vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2.0, 2)
    ref = min(samples, key=lambda s: abs(s.get("overall", 0) - out.get("overall", 0)))
    out["feedback"] = ref.get("feedback", "")
    out["weak_events"] = ref.get("weak_events", [])
    _irr_all = []
    for s in samples:
        for t in s.get("irrelevant_events", []) or []:
            t = (t or "").strip()
            if t and t not in _irr_all:
                _irr_all.append(t)
    out["irrelevant_events"] = _irr_all[:12]
    out["_samples"] = samples  # 供逐事件归因用；落盘前由 meta 组装处 pop 掉
    # G1 漏点回收：模糊共现分组（judge 跨样本措辞漂移，精确匹配曾把回收饿死在 0 条）
    _raw = []
    for s in samples:
        for m in s.get("missed_points", []) or []:
            m = (m or "").strip()
            if m:
                _raw.append(m)
    if _raw:
        groups = []
        for m in _raw:
            tok = _dedup_tokens(m)
            if not tok:
                continue
            for g in groups:
                if _jaccard(g["tok"], tok) >= 0.35:
                    g["n"] += 1
                    break
            else:
                groups.append({"tok": tok, "disp": m, "n": 1})
        _mp = [g["disp"] for g in groups if g["n"] >= 2]
        if not _mp:
            # 兜底：无共现时取前 2 孤本，噪声交给下游池内/AI/查重闸过滤
            _mp = [g["disp"] for g in groups[:2]]
        print("[每日洞察] missed 回收: 原始 %d 条 → 共现分组 %d 条" % (len(_raw), len(_mp)))
        out["missed_points"] = _mp[:12]
    return out


def _robust_quality_eval(llm, current, theme, context_text, use_cv):
    """多样本稳健评估：CV 下 v1/v2/v1' 三样本取中位，非 CV 双样本。全失败返回 None。

    守卫返回（空上下文/无 LLM 的 0.5）不是"一次评分"，必须与真正的解析失败同等剔除，
    否则 meta 会写出 eval_samples=3 / degraded=false 的谎报（09-19 审查 P1-2）。
    """
    def _real(s):
        return bool(s) and not s.get("_guard")

    if use_cv:
        e1 = _evaluate_report_quality(llm, current, theme, context_text, prompt_variant="v1")
        e2 = _evaluate_report_quality(llm, current, theme, context_text, prompt_variant="v2")
        e3 = (_evaluate_report_quality(llm, current, theme, context_text, prompt_variant="v1")
              if (_real(e1) and _real(e2)) else None)
        samples = [s for s in (e1, e2, e3) if _real(s)]
        if not samples:
            return None
        med = _median_eval_samples(samples)
        if _real(e1) and _real(e2):
            med["cross_validation"] = {"v1_overall": e1["overall"], "v2_overall": e2["overall"]}
            med["_actual_variants"] = 2
            print("[每日洞察] RAGAS 交叉验证: v1=%.2f v2=%.2f v1'=%s → med=%.2f" % (
                e1["overall"], e2["overall"], ("%.2f" % e3["overall"]) if e3 else "n/a",
                med["overall"]))
        else:
            med["_actual_variants"] = 1
        med["_eval_samples"] = len(samples)
        return med
    e1 = _evaluate_report_quality(llm, current, theme, context_text)
    e2 = _evaluate_report_quality(llm, current, theme, context_text)
    samples = [s for s in (e1, e2) if _real(s)]
    if not samples:
        return None
    med = _median_eval_samples(samples)
    med["_actual_variants"] = 1
    med["_eval_samples"] = len(samples)
    return med


def _recover_missed_events(llm, clusters, missed_points, index, chunks, max_new=2, pool=None):
    """G1 闭环：judge 的 missed 清单 → 定向检索 → 迷你聚类 → Phase1 摘要 → 新事件。

    此前裁判每期报出的漏点清单在解析层被丢弃（第7轮外部分析坐实）。新事件由
    调用方接回终局闸序（去重/faith 复查/占位/收口），任何异常静默降级为不补。
    """
    if not llm or not missed_points or len(clusters) >= PHASE1_POOL:
        return []
    try:
        # 清洗：judge 常输出"无/已全覆盖"废串；RRF 检索对任意串都保证返回 15 条（审查 P1-2）
        queries = [m for m in missed_points
                   if isinstance(m, str) and len(m.strip()) >= 6][:5]
        if not queries:
            return []
        hits = _hybrid_retrieve(index, chunks, queries, top_k=15)
        if not hits:
            print("[每日洞察] missed 回收: 检索无结果")
            return []
        _floor = max((c.get("retrieval_score", 0) for c in hits), default=0) * 0.5
        cand = [c for c in hits if c.get("retrieval_score", 0) >= _floor]
        if pool is not None:
            # judge 只看池内 200 条：池外证据会造成 faith 判定域分叉（审查 P1-5）
            cand = [c for c in cand
                    if _norm_url(c.get("url", "") or c.get("link", "")) in pool]
        groups = []
        for ch in cand:
            key = _norm_url(ch.get("url", "") or ch.get("link", ""))
            placed = False
            for g in groups:
                if (key and key in g["urls"]) or \
                   _jaccard(_dedup_tokens(g["title"]),
                            _dedup_tokens(ch.get("title", ""))) >= 0.3:
                    g["items"].append(ch)
                    if key:
                        g["urls"].add(key)
                    placed = True
                    break
            if not placed:
                groups.append({"title": (ch.get("title", "") or "").strip(),
                               "items": [ch], "urls": {k for k in [key] if k},
                               "score": ch.get("retrieval_score", 0)})
        existing_toks = [_dedup_tokens(c.get("label", "")) for c in clusters]
        new_clusters = []
        for g in sorted(groups, key=lambda x: -x["score"]):
            if len(new_clusters) >= max_new:
                break
            if not g["title"]:
                continue
            tok = _dedup_tokens(g["title"])
            if len(tok) < 2:
                continue
            if any(_jaccard(tok, e) >= 0.35 for e in existing_toks):
                continue
            if not _AI_NEWS_RE.search(g["title"][:60]):
                continue  # 回收事件必须 AI/科技相关，保护 relevance（审查 P1-2）
            new_clusters.append({
                "id": "evt_%s_r%02d" % (NOW_BJ.strftime("%Y%m%d"), len(clusters) + len(new_clusters) + 1),
                "label": g["title"][:60], "summary": "", "significance": "",
                "category": "", "items": g["items"][:8],
                "source_types": sorted({(it.get("source_type") or it.get("_src") or "")
                                        for it in g["items"][:8]}) or ["rss"],
                "score": g["score"], "best_score": g["score"],
            })
        if not new_clusters:
            print("[每日洞察] missed 回收: 无合格新事件(阈值挡/与既有重叠/非科技主题)")
            return []
        for c in new_clusters:
            _filter_cluster_items(c, c.get("label", ""))
        p1 = _llm_phase1(llm, new_clusters)
        if not p1:
            return []
        _apply_phase1_result(new_clusters, p1)
        kept = [c for c in new_clusters
                if c.get("summary") and not _is_insufficient(c["summary"])
                and _AI_NEWS_RE.search((c.get("label", "") + " " + c.get("summary", ""))[:400])]
        print("[每日洞察] missed 回收 %d 个新事件: %s" % (
            len(kept), "; ".join((c.get("label") or "")[:20] for c in kept)))
        return kept
    except Exception as exc:
        print("[每日洞察] missed 回收异常，跳过: %s" % exc, file=sys.stderr)
        return []


def _ragas_evaluate_and_correct(llm, clusters, theme, retrieved_chunks, config, retrieval_stats=None):
    """RAGAS 评估-修正闭环：评分 → 不达标则自我修正 → 重新评分。"""
    if not RAGAS_ENABLED or not llm or not clusters:
        return clusters, {}

    threshold = config.get("daily_insight_quality_threshold", RAGAS_QUALITY_THRESHOLD)
    max_iterations = config.get("daily_insight_max_corrections", RAGAS_MAX_CORRECTIONS)

    # 构建评估上下文
    _ctx_stats = {}
    context_text = _build_ragas_context(clusters, retrieved_chunks, stats=_ctx_stats)
    if not context_text:
        return clusters, {}

    # 评估-修正循环
    current = clusters
    eval_result = {}
    iteration_count = 0
    use_cross_validation = config.get("daily_insight_cross_validation", False)
    for iteration in range(max_iterations + 1):
        eval_result = _robust_quality_eval(llm, current, theme, context_text,
                                           use_cv=use_cross_validation)
        if eval_result is None:
            # 合成 0.5 只在"全部样本调用失败"时出现，必须带降级标记与 0 样本，
            # 否则与真实 1 样本 0.5 无法区分（04:00 期假崩分无人察觉）
            eval_result = {"context_coverage": 0.5, "faithfulness": 0.5, "relevance": 0.5,
                           "overall": 0.5, "feedback": "评估全部路径失败", "weak_events": [],
                           "_eval_samples": 0, "_actual_variants": 0, "_degraded": True}
            print("::warning title=RAGAS 评估降级::三样本评估全部失败，本期 quality 为合成 0.5，不代表内容质量")
        print("[每日洞察] RAGAS eval iter=%d: overall=%.2f, cov=%.2f, faith=%.2f, rel=%.2f" % (
            iteration, eval_result["overall"], eval_result["context_coverage"],
            eval_result["faithfulness"], eval_result["relevance"]))

        if eval_result["overall"] >= threshold and _dims_pass_thresholds(eval_result):
            print("[每日洞察] RAGAS 质量达标 (overall=%.2f >= %.2f, 各维度达标)" % (
                eval_result["overall"], threshold))
            break
        if iteration < max_iterations:
            iteration_count += 1
            print("[每日洞察] RAGAS 未达标 (overall=%.2f 阈值=%.2f)，执行自我修正..." % (
                eval_result["overall"], threshold), file=sys.stderr)
            pre_score = eval_result["overall"]
            # _self_correct_events 原地写回 clusters，拒绝时必须回滚快照（P1-1 实证）
            _snapshot = [{k: c.get(k, _ROLLBACK_UNSET) for k in
                         ("label", "summary", "significance", "category")}
                        for c in current]
            corrected = _self_correct_events(llm, current, context_text, eval_result)
            # 重新评估修正结果（保持与初始评估一致的方法）
            corrected_result = _robust_quality_eval(llm, corrected, theme, context_text,
                                                    use_cv=use_cross_validation)
            if corrected_result is None:
                corrected_result = {"overall": 0.0, "context_coverage": 0.0,
                                    "faithfulness": 0.0, "relevance": 0.0,
                                    "feedback": "修正评估 JSON 解析失败", "weak_events": []}
            print("[每日洞察] RAGAS 修正后: overall=%.2f, cov=%.2f, faith=%.2f, rel=%.2f" % (
                corrected_result["overall"], corrected_result["context_coverage"],
                corrected_result["faithfulness"], corrected_result["relevance"]))
            # 采纳须超出复评噪声带（±0.02 实测漂移），防采纳劣化版；
            # faith 单维劣化同样拦截（09-19 04:14 期 0.90→0.66 被综合分掩盖实证）
            pre_faith = eval_result["faithfulness"]
            if (corrected_result["overall"] > pre_score + 0.02
                    and corrected_result["faithfulness"] >= pre_faith - 0.02):
                print("[每日洞察] 修正有效 (%.2f → %.2f)，采纳" % (pre_score, corrected_result["overall"]))
                current = corrected
                eval_result = corrected_result
                # 采纳后若各维度仍未全达标，进入下一轮修正（受 max_iterations 约束）
                if (corrected_result["overall"] >= threshold
                        and _dims_pass_thresholds(corrected_result)):
                    break
                if iteration + 1 >= max_iterations:
                    break  # 下一轮已无修正预算，重评只会引入 judge 降级采样噪声
                continue
            else:
                for _c, _s in zip(current, _snapshot):
                    for _k, _v in _s.items():
                        if _v is _ROLLBACK_UNSET:
                            _c.pop(_k, None)
                        else:
                            _c[_k] = _v
                print("[每日洞察] 修正未采纳 (overall %.2f → %.2f, faith %.2f → %.2f)，已回滚原版" % (
                    pre_score, corrected_result["overall"], pre_faith, corrected_result["faithfulness"]))
                break  # 内容已回到 eval_result 对应的状态，重评是纯噪声（04:00 期降级 judge 打 0.50 实证）

    eval_result["iterations"] = iteration_count

    # 评估元数据：准确记录实际运行的变体数
    actual_variants = eval_result.get("_actual_variants", 1)
    eval_result.pop("_actual_variants", None)
    _n_samples = eval_result.pop("_eval_samples", 1)
    _attr_samples = eval_result.pop("_samples", None) or [eval_result]
    try:
        _readings = {
            "retrieval_mix": _source_mix(retrieved_chunks),
            "bm25_window": dict(retrieval_stats or {}),
            "per_event": _aggregate_per_event(current, _attr_samples),
            "irrelevant_events": list(eval_result.get("irrelevant_events", []) or []),
        }
    except Exception as _rexc:
        _readings = {"retrieval_mix": {}, "bm25_window": {}, "per_event": [],
                     "irrelevant_events": list(eval_result.get("irrelevant_events", []) or [])}
        print("[每日洞察] 归因读数异常（不影响评分与产物）: %s" % _rexc, file=sys.stderr)
    eval_result["meta"] = {
        "judge_model": _effective_judge_model(llm),
        "judge_model_configured": getattr(llm, 'model', 'unknown'),
        "prompt_variants": actual_variants,
        "eval_samples": _n_samples,
        "context_chunks": _ctx_stats.get("chunks", 0),
        "context_chars": _ctx_stats.get("chars", len(context_text or "")),
        "context_cited_extra": _ctx_stats.get("cited_extra", 0),
        "context_truncated": _ctx_stats.get("truncated", False),
        "call_log": getattr(llm, '_call_log', []),
        "degraded": bool(eval_result.pop("_degraded", False)),
        "temperature": 0.2,
        **_readings,
    }

    return current, eval_result


# ──────────────────── Phase 3: 历史留存 ────────────────────

def _load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"days": []}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"days": []}


def _match_events_to_history(clusters, history):
    """跨天事件 id 关联：匹配昨日事件，标注 new/ongoing/faded/escalating。"""
    prev_day = None
    for day in reversed(history.get("days", [])):
        if day.get("events"):
            prev_day = day
            break

    if not prev_day:
        for c in clusters:
            c["status"] = "new"
        return clusters

    prev_labels = {}
    for evt in prev_day.get("events", []):
        prev_labels[evt.get("label", "")] = evt

    for c in clusters:
        label = c.get("label", "")
        tok = _tokenize_title(label)
        best_match, best_sim = None, 0
        for pl, pe in prev_labels.items():
            sim = _jaccard(tok, _tokenize_title(pl))
            if sim > best_sim:
                best_sim, best_match = sim, pe

        if best_sim >= JACCARD_HISTORY_THRESHOLD and best_match:
            c["status"] = "ongoing"
            c["prev_id"] = best_match.get("id", "")
            c["prev_summary"] = best_match.get("summary", "")
            # escalating: 分数显著增长
            prev_score = best_match.get("score", 0)
            if c["score"] > prev_score * 1.5:
                c["status"] = "escalating"
        else:
            c["status"] = "new"

    return clusters


def _update_history(clusters, theme):
    """追加今日记录，裁剪 30 天前旧数据。"""
    history = _load_history()
    today_str = NOW_BJ.strftime("%Y-%m-%d")

    # 移除今天（如果已存在，覆盖）
    history["days"] = [d for d in history["days"] if d.get("date") != today_str]

    # 构建今日记录
    today_events = []
    for c in clusters:
        evt = {
            "id": c.get("id") or ("evt_" + hashlib.sha1((c.get("label") or "").encode("utf-8")).hexdigest()[:8]),
            "label": c.get("label", ""),
            "category": c.get("category", ""),
            "score": c.get("score", 0),
            "signal": c.get("signal", {}),
            "resonance": c.get("resonance", ""),
            "sources": c.get("source_types", []),
            "status": c.get("status", "new"),
            "summary": c.get("summary", ""),
            "significance": c.get("significance", ""),
            "key_links": _validate_key_links(c.get("key_links", []), c.get("items", []))[:3],
        }
        if c.get("deep_analysis") and c["deep_analysis"].get("status") != "degraded":
            evt["deep_analysis"] = c["deep_analysis"]
        today_events.append(evt)

    history["days"].append({
        "date": today_str,
        "theme": theme,
        "events": today_events,
        "stats": {
            "total_events": len(today_events),
            "has_analysis": any(e.get("deep_analysis") for e in today_events),
        },
        "generated_at": NOW_BJ.isoformat(),
    })

    # 裁剪 30 天
    cutoff = (NOW_BJ - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    history["days"] = [d for d in history["days"] if d.get("date", "") >= cutoff]

    try:
        _atomic_write_json(HISTORY_FILE, history)
        print("[每日洞察] 历史更新: %d 天" % len(history["days"]))
    except Exception as e:
        print("[每日洞察] 历史写入失败: %s" % e, file=sys.stderr)


# ──────────────────── 终版复评（shipped report） ────────────────────

def _theme_clause_carried(clause, clusters):
    """从句能否落到某条正文事件上：按特征重合数 + 包含度，同 :3798-3811 的归因口径。"""
    ct = _dedup_tokens(clause)
    if not ct:
        return False
    for c in (clusters or []):
        et = _dedup_tokens((c.get("label") or "") + " " + (c.get("summary") or ""))
        if not et:
            continue
        ov = len(ct & et)
        if ov >= 2 and ov / float(len(ct)) >= 0.2:
            return True
    return False


def _align_theme_to_events(theme, clusters):
    """把主题句裁到只说正文里真有的事，返回 (theme, 被裁掉的从句)。

    为什么必须有：出厂过的 14:13 期主题写了"四大巨头被诉""加州紧急停止机制"两件事，
    而正文 12 条里都没有 —— 头版在替报告承诺它没给证据的内容。成因有两条：
    Phase 1 的 prompt 曾明确要求"把未被事件覆盖的重要信息写进 theme"（已删），
    且 theme 在 `clusters[:MAX_EVENTS]` 收口之前生成、之后从不重生成。

    不能在这里调 LLM：这一步同时要在终版复评之前跑，否则 cov/rel 仍是拿旧主题打的分。
    """
    t = (theme or "").strip()
    if not t:
        return t, []
    parts = [p.strip() for p in re.split(r"[，,。；;]", t) if p.strip()]
    if not parts:
        return t, []
    # 末段若是判断句（"判断…/说明…"），它是对整体的解读、不指认具体事件，原样保留
    judged = parts[-1] if parts[-1].startswith(("判断", "说明", "意味着")) else None
    body = parts[:-1] if judged else parts
    kept, dropped = [], []
    for cl in body:
        (kept if _theme_clause_carried(cl, clusters) else dropped).append(cl)
    if judged is None and not dropped:
        return t, []          # 本来就全部有承载，原句一字不动
    if not kept:
        labels = [(c.get("label") or "").strip() for c in (clusters or [])
                  if (c.get("label") or "").strip()]
        if not labels:
            return (judged or t), []      # 无事件可依据时退回判断句，绝不给页面留空格
        rebuilt = "从" + labels[0]
        for lb in labels[1:3]:
            rebuilt += "，到" + lb
        kept = [rebuilt]
    out = "，".join(kept + ([judged] if judged else []))
    return out, dropped


def _confirm_shipped_report_eval(llm, clusters, theme, retrieved_chunks, ragas_eval,
                                 use_cv=True, retrieval_stats=None):
    """对最终 shipped 报告复评一次，让 quality 描述用户真正看到的那份日报。

    07:14 期实证：导出的 quality 出自 missed 回收/终局去重/预算收口**之前**的草稿，
    指标里写着"漏了霍奇猜想"而报告第 4 条就是它——迭代因此在优化一份不存在的产物。
    只观测不修正：修正环已在草稿阶段跑完，对同一内容重评等于多掷骰子（03:49 期崩塌实证）。
    复评失败或无 Judge 时原样回退草稿分，并标注 eval_stage 供跨期对齐。
    """
    if not ragas_eval or not llm or not clusters:
        return ragas_eval
    meta = dict(ragas_eval.get("meta") or {})
    _ctx_stats = {}
    _log0 = len(getattr(llm, '_call_log', None) or [])
    try:
        context_text = _build_ragas_context(clusters, retrieved_chunks or [], stats=_ctx_stats)
    except Exception as exc:
        print("[每日洞察] 终版复评上下文异常，沿用草稿分: %s" % exc, file=sys.stderr)
        context_text = ""
    if not context_text:
        # 空上下文里 _evaluate_report_quality 会返回守卫 0.5，绝不能当成 shipped 分数发布
        out = dict(ragas_eval)
        meta["eval_stage"] = "draft_only"
        out["meta"] = meta
        print("[每日洞察] 终版复评无可用上下文，沿用草稿分 overall=%.2f" % ragas_eval.get("overall", 0))
        return out
    try:
        shipped = _robust_quality_eval(llm, clusters, theme, context_text, use_cv=use_cv)
    except Exception as exc:
        print("[每日洞察] 终版复评异常，沿用草稿分: %s" % exc, file=sys.stderr)
        shipped = None
    if not shipped:
        meta["eval_stage"] = "draft_only"
        out = dict(ragas_eval)
        out["meta"] = meta
        print("[每日洞察] 终版复评未产出，沿用草稿分 overall=%.2f" % ragas_eval.get("overall", 0))
        return out
    full_log = getattr(llm, '_call_log', None) or []
    out = dict(shipped)
    out["iterations"] = ragas_eval.get("iterations", 0)
    _attr_samples = out.pop("_samples", None) or [out]
    try:
        _readings = {
            "retrieval_mix": _source_mix(retrieved_chunks),
            "bm25_window": dict(retrieval_stats or {}),
            "per_event": _aggregate_per_event(clusters, _attr_samples),
            "irrelevant_events": list(out.get("irrelevant_events", []) or []),
        }
    except Exception as _rexc:
        _readings = {"retrieval_mix": {}, "bm25_window": {}, "per_event": [],
                     "irrelevant_events": list(out.get("irrelevant_events", []) or [])}
        print("[每日洞察] 终版归因读数异常（不影响评分与产物）: %s" % _rexc, file=sys.stderr)
    out_meta = {
        "judge_model": _effective_judge_model(llm, full_log[_log0:]),
        "judge_model_configured": getattr(llm, 'model', 'unknown'),
        "prompt_variants": out.pop("_actual_variants", 1),
        "eval_samples": out.pop("_eval_samples", 1),
        "context_chunks": _ctx_stats.get("chunks", 0),
        "context_chars": _ctx_stats.get("chars", len(context_text or "")),
        "context_cited_extra": _ctx_stats.get("cited_extra", 0),
        "context_truncated": _ctx_stats.get("truncated", False),
        "call_log": full_log[_log0:],
        "degraded": bool(out.pop("_degraded", False)),
        "temperature": 0.2,
        **_readings,
    }
    # 缺失维度保留 None：填 0 会让"没评出来"在 passed_draft 上冒充"评了且不及格"
    out_meta["draft_eval"] = {
        "overall": ragas_eval.get("overall"),
        "context_coverage": ragas_eval.get("context_coverage"),
        "faithfulness": ragas_eval.get("faithfulness"),
        "relevance": ragas_eval.get("relevance"),
        "missed_points": ragas_eval.get("missed_points", []),
    }
    out_meta["eval_stage"] = "shipped_report"
    out["meta"] = out_meta
    print("[每日洞察] 终版复评: overall=%.2f cov=%.2f faith=%.2f rel=%.2f（草稿 %.2f/%.2f/%.2f）" % (
        out.get("overall", 0), out.get("context_coverage", 0),
        out.get("faithfulness", 0), out.get("relevance", 0),
        ragas_eval.get("overall", 0), ragas_eval.get("faithfulness", 0),
        ragas_eval.get("relevance", 0)))
    return out


# ──────────────────── Phase 3: 输出 JSON ────────────────────

def _write_insight_json(clusters, theme, has_analysis, ragas_eval=None, bubble_breaker=None):
    events = []
    for c in clusters:
        # 构建 articles 列表：每条素材的关键信息
        articles = []
        for it in c.get("items", []):
            art = {
                "title": it.get("title", ""),
                "source": it.get("source", "") or it.get("platform", "") or it.get("channel", ""),
                "type": it.get("source_type", "") or it.get("_src", ""),
                "url": it.get("link", "") or it.get("url", ""),
            }
            articles.append(art)
        evt = {
            "id": c.get("id") or ("evt_" + hashlib.sha1((c.get("label") or "").encode("utf-8")).hexdigest()[:8]),
            "label": c.get("label", ""),
            "category": c.get("category", ""),
            "score": c.get("score", 0),
            "editor_score": c.get("editor_score", 0),
            "signal": c.get("signal", {}),
            "resonance": c.get("resonance", ""),
            "sources": c.get("source_types", []),
            "status": c.get("status", "new"),
            "summary": c.get("summary", ""),
            "significance": c.get("significance", ""),
            "key_links": c.get("key_links", []) or [a["url"] for a in articles if a.get("url")][:3],
            "deep_analysis": c.get("deep_analysis"),
            "articles": articles,
        }
        events.append(evt)

    output = {
        "date": NOW_BJ.strftime("%Y-%m-%d"),
        "theme": theme,
        "events": events,
        "bubble_breaker": bubble_breaker or [],
        "stats": {
            "total_events": len(events),
            "has_analysis": has_analysis,
        },
        "generated_at": NOW_BJ.isoformat(),
    }
    # RAGAS 质量评分
    if ragas_eval:
        quality = {
            "overall": ragas_eval.get("overall", 0),
            "missed_points": ragas_eval.get("missed_points", []),
            "context_coverage": ragas_eval.get("context_coverage", 0),
            "faithfulness": ragas_eval.get("faithfulness", 0),
            "relevance": ragas_eval.get("relevance", 0),
            "feedback": ragas_eval.get("feedback", ""),
        }
        if ragas_eval.get("meta"):
            quality["meta"] = ragas_eval["meta"]
        output["quality"] = quality

    try:
        _atomic_write_json(INSIGHT_FILE, output)
        quality_str = ""
        if ragas_eval:
            quality_str = ", RAGAS=%.2f" % ragas_eval.get("overall", 0)
        print("[每日洞察] 产出 → %s（%d 事件, 分析=%s%s）" % (
            INSIGHT_FILE, len(events), has_analysis, quality_str))
    except Exception as e:
        print("[每日洞察] JSON 写入失败: %s" % e, file=sys.stderr)


# ──────────────────── Phase 4: 历史页面 ────────────────────

def _build_history_html():
    """生成 daily-insight-history.html。视觉风格与 ai-daily.html 报纸风格对齐。"""
    history = _load_history()
    days = history.get("days", [])
    if not days:
        return

    # 按日期降序
    days.sort(key=lambda d: d.get("date", ""), reverse=True)

    # 构建日期选择器选项
    date_options = "".join(
        '<option value="day-%s">%s (%d 事件)</option>' % (
            d["date"], d["date"], d.get("stats", {}).get("total_events", 0))
        for d in days
    )

    # 构建每天的事件卡片（与日报洞察卡同构：眉线/衬线标题/深读面板）
    day_sections = []
    for d in days:
        date = d.get("date", "")
        theme = _esc(d.get("theme", ""))
        events_html = []
        for evt in d.get("events", []):
            # 归档数据用 sources 命名信源，卡片眉线读 source_types——入口对齐一次
            evt.setdefault("source_types", evt.get("sources", []))
            deep = evt.get("deep_analysis") or {}
            events_html.append(_evt_card_html(
                evt, feat=bool(deep and deep.get("status") != "degraded")))

        day_sections.append('''
<section id="day-%s" class="day-section" style="display:none">
  <div class="day-theme">%s</div>
  %s
</section>''' % (date, theme, "\n".join(events_html)))

    # 默认显示第一天
    if day_sections:
        day_sections[0] = day_sections[0].replace('style="display:none"', '')

    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>每日深度洞察 · 历史</title>
<style>
:root {
  --bg:#faf8f4; --card:#fffdf9; --card-2:#f3efe6;
  --ink:#1f1c17; --muted:#6f6860; --faint:#857e74;
  --line:#e4ddd0; --line-strong:#b9b0a2;
  --accent:#30891A; --accent-ink:#256d13; --accent-weak:rgba(48,137,26,.08);
  --display:"Georgia","Times New Roman","Songti SC","SimSun","STSong",serif;
  --body:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
}
[data-theme="dark"] {
  --bg:#101114; --card:#17191d; --card-2:#1d2026;
  --ink:#e9e6df; --muted:#a49c90; --faint:#7d776d;
  --line:#2a2d33; --line-strong:#4a4e57;
  --accent:#5bc23e; --accent-ink:#a3e88f; --accent-weak:rgba(91,194,62,.12);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html{scroll-behavior:smooth;scroll-padding-top:24px;}
body{
  font-family:var(--body);background:var(--bg);color:var(--ink);
  line-height:1.65;-webkit-font-smoothing:antialiased;font-size:15px;
}
a{color:inherit;text-decoration:none;}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px;}
.paper{max-width:980px;margin:0 auto;padding:0 20px 64px;}

/* top bar */
.topbar{display:flex;align-items:center;gap:10px;padding:14px 0 4px;}
.back{
  display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:999px;
  background:var(--card);border:1px solid var(--line);font-size:13px;transition:all .15s;
}
.back:hover{border-color:var(--accent);color:var(--accent-ink);}
.topbar .spacer{flex:1;}
.theme-btn{
  width:36px;height:36px;border-radius:999px;background:var(--card);border:1px solid var(--line);
  display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--ink);
  transition:all .15s;
}
.theme-btn:hover{border-color:var(--accent);color:var(--accent-ink);}
.theme-btn svg{width:16px;height:16px;}

/* masthead */
.masthead{text-align:center;padding:26px 0 0;}
.mast-rule{display:flex;align-items:center;gap:14px;margin:0 0 4px;}
.mast-rule::before,.mast-rule::after{content:"";flex:1;height:1px;background:var(--line-strong);}
.mast-meta{font-size:11.5px;letter-spacing:.14em;color:var(--muted);font-weight:500;white-space:nowrap;}
.mast-title{
  font-family:var(--display);font-size:clamp(28px,5vw,42px);font-weight:700;
  letter-spacing:.06em;line-height:1.15;margin:6px 0 2px;
}
.mast-sub{font-size:12.5px;color:var(--muted);letter-spacing:.1em;}
.mast-strip{
  margin-top:14px;padding:8px 12px;border-top:3px double var(--line-strong);border-bottom:1px solid var(--line-strong);
  font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:4px 18px;justify-content:center;
}
.mast-strip b{color:var(--ink);font-weight:600;}

/* day nav */
.day-nav{
  display:flex;flex-wrap:wrap;justify-content:center;gap:6px 22px;
  padding:14px 0 4px;font-size:13px;
}
.day-nav select{
  padding:6px 12px;border:1px solid var(--line);border-radius:999px;
  background:var(--card);color:var(--ink);font-size:13px;font-family:var(--body);
}

/* day section */
.day-section{margin:34px 0;}
.day-theme{
  font-family:var(--display);font-size:1.1em;color:var(--accent-ink);margin-bottom:16px;
  padding:10px 14px;border-left:3px solid var(--accent);background:var(--card-2);
}

/* insight cards（与日报洞察版块共用 _DI_CSS，消除两处拷贝漂移） */
%s

/* footer */
.foot{
  margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:space-between;
}

@media (max-width:760px){
  .paper{padding:0 14px 48px;}
  .mast-title{letter-spacing:.03em;}
}
@media (prefers-reduced-motion:reduce){
  html{scroll-behavior:auto;}
  *{transition:none!important;animation:none!important;}
}
</style>
<script>try{var _t=localStorage.getItem('wb_starhub_theme_v1')||(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');document.documentElement.dataset.theme=_t;}catch(e){}</script>
</head>
<body>
<div class="paper">

  <div class="topbar">
    <a class="back" href="ai-daily.html">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></svg>
      返回日报
    </a>
    <div class="spacer"></div>
    <button class="theme-btn" id="btnTheme" title="切换明暗主题" aria-label="切换明暗主题">
      <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7"/></svg>
      <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z"/></svg>
    </button>
  </div>

  <header class="masthead">
    <div class="mast-rule"><span class="mast-meta">每日深度洞察</span><span class="mast-meta">历史归档</span></div>
    <h1 class="mast-title">深度洞察<span style="color:var(--accent)"> · </span>历史</h1>
    <div class="mast-sub">共 %d 天记录 &nbsp;·&nbsp; 事件聚合与趋势追踪</div>
    <div class="mast-strip">
      <span>自动生成</span>
      <span>数据源 <b>AIHOT</b> · 多平台聚合 · 内容版权归原作者</span>
    </div>
  </header>

  <nav class="day-nav" aria-label="日期选择">
    <select id="daySelect" onchange="switchDay(this.value)">
%s
    </select>
  </nav>

%s

  <footer class="foot">
    <span>每日深度洞察 · 历史归档</span>
    <span><a href="ai-daily.html">返回今日日报</a></span>
  </footer>
</div>
<script>
function switchDay(id){
  document.querySelectorAll('.day-section').forEach(function(s){s.style.display='none';});
  var el=document.getElementById(id);
  if(el)el.style.display='block';
}
(function(){
  var btn=document.getElementById('btnTheme');
  btn.addEventListener('click',function(){
    var cur=document.documentElement.dataset.theme;
    var next=cur==='dark'?'light':'dark';
    document.documentElement.dataset.theme=next;
    try{localStorage.setItem('wb_starhub_theme_v1',next);}catch(e){}
  });
})();
</script>
</body>
</html>''' % (_DI_CSS, len(days), date_options, "\n".join(day_sections))

    try:
        with open(HISTORY_HTML, "w", encoding="utf-8") as f:
            f.write(html)
        print("[每日洞察] 历史页面 → %s（%d 天）" % (HISTORY_HTML, len(days)))
    except Exception as e:
        print("[每日洞察] 历史页面写入失败: %s" % e, file=sys.stderr)


# ──────────────────── AI 日报子板块注入 ────────────────────

_AI_DAILY_FILE = "ai-daily.html"

# 洞察卡片眉线/徽标文案（注入块与历史页共用，消除两处拷贝漂移）
# 洞察 category 码 → 报纸眉线中文（缺失回退原码）
_DI_CAT_CN = {"ai-models": "AI 模型", "ai-products": "AI 产品", "industry": "行业",
              "policy": "政策", "developer": "工程实践", "research": "论文",
              "consumer": "消费"}
_CORPUS_TAG_RE = re.compile(r"\s*\[(?:agihunt|aihot|rss)\]", re.I)


def _strip_corpus_tags(s):
    """摘要里的语料来源标记是机器内部记号，阅读层剥除（正文 [1][3] 引用序号保留）。"""
    return _CORPUS_TAG_RE.sub("", s or "").replace("  ", " ").strip()


def _link_host(u):
    """URL 域名作为原文链接文案（去 www.，失败回退「原文」）。"""
    try:
        from urllib.parse import urlparse
        h = (urlparse(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else (h or "原文")
    except Exception:
        return "原文"


def _allowed_links(links, items=None):
    """可跳转原文 URL 白名单序列：仅放行 http(s)，去重保序 cap 3；
    key_links 为空时回退 items URL（红线：每个洞察必须带可跳转原文引用）。"""
    seen, out = set(), []

    def _add(u):
        if isinstance(u, str) and re.match(r'https?://', u, re.I):
            k = _norm_url(u)
            if k and k not in seen:
                seen.add(k)
                out.append(u)

    for u in links or []:
        _add(u)
    if not out:
        for it in items or []:
            _add(it.get("url") or it.get("link") if isinstance(it, dict) else "")
    return out[:3]


def _source_links_html(links, items=None):
    """可跳转原文链接组（点线引用样式，不再用胶囊块）。"""
    chips = "".join(
        '<a class="di-link" href="%s" target="_blank" rel="noopener">%s&#8599;</a>'
        % (_esc(u), _esc(_link_host(u))) for u in _allowed_links(links, items))
    return '<div class="di-links">%s</div>' % chips if chips else ""


def _cited_sources_html(deep):
    """深度解读实际引用的素材编号 → 可点击链接列表。"""
    chips = ""
    for s in (deep.get("cited_sources") or [])[:5]:
        u = s.get("url", "") if isinstance(s, dict) else ""
        if not u or not re.match(r'https?://', u, re.I):
            continue
        chips += '<a class="di-cite" href="%s" target="_blank" rel="noopener">[%s] %s&#8599;</a> ' % (
            _esc(u), _esc(str(s.get("index", ""))), _esc(_link_host(u)))
    return ('<p class="di-cites">%s</p>' % chips) if chips else ""


def _evt_card_html(evt, feat):
    """洞察卡片：与日报 .item 同一语法（眉线/衬线标题/摘要/点线引用）。
    feat=True 渲染深读面板（card-2 底，对应日报 .item.top 语汇）。
    眉线只放对读者有意义的中文分类——status/resonance/score 是管线内部信号
    （"新"每条都有、"垂直"是黑话、0-1 评分无图例），不进阅读层（用户裁决 2026-10-01）。"""
    cat = evt.get("category", "")
    kicker = _DI_CAT_CN.get(cat, cat)
    deep = evt.get("deep_analysis") or {}
    has_deep = bool(deep) and deep.get("status") != "degraded"
    links = _allowed_links(evt.get("key_links"), evt.get("items"))
    head = _esc(evt.get("label", ""))
    label = ('<h3 class="di-label"><a href="%s" target="_blank" rel="noopener">%s</a></h3>'
             % (_esc(links[0]), head)) if links else '<h3 class="di-label">%s</h3>' % head
    out = ['<article class="di-card%s">' % (" has-deep" if feat else ""),
           '<div class="di-kicker"><span>%s</span></div>' % _esc(kicker),
           label,
           '<p class="di-summary">%s</p>' % _esc(_strip_corpus_tags(evt.get("summary", "")))]
    if has_deep:
        cites = _cited_sources_html(deep)
        if not cites:
            # 红线兜底：深读缺引用清单时回退原文链接组
            cites = _source_links_html(evt.get("key_links"), evt.get("items"))
        out.append('''<div class="di-deep">
<div class="di-deep-kicker">深度解读 · ANALYSIS</div>
<div class="di-deep-sec"><strong>事件还原</strong><p>%s</p></div>
<div class="di-deep-sec"><strong>影响分析</strong><p>%s</p></div>
<div class="di-deep-sec"><strong>信源分歧</strong><p>%s</p></div>
%s
<div class="di-deep-sec"><strong>后续展望</strong><p>%s</p></div>
<div class="di-deep-foot">%s</div>
</div>''' % (_esc(deep.get("event_reconstruction", "")),
             _esc(deep.get("impact_analysis", "")),
             _esc(deep.get("source_divergence", "")),
             ('<blockquote class="di-quote">%s</blockquote>' % _esc(deep["quote"]))
             if deep.get("quote") else "",
             _esc(deep.get("outlook", "")),
             cites))
    else:
        # 来源由链接域名（goo.gle↗ 等）体现，不再渲染语料代号
        foot = _source_links_html(evt.get("key_links"), evt.get("items"))
        if foot:
            out.append('<div class="di-foot">%s</div>' % foot)
    out.append('</article>')
    return "\n".join(out)


# 洞察版块 CSS：注入块与历史归档页共用同一份（spec PartA §3.4，demo 为像素基准）
_DI_CSS = """/* ---- 每日深度洞察 · 与日报同一套报纸语法 ---- */
.di-sec .di-theme{font-family:var(--display);font-style:italic;font-size:14.5px;color:var(--muted);margin:-4px 0 6px;line-height:1.7;}
.di-card{padding:14px 0;border-bottom:1px solid var(--line);break-inside:avoid;}
.di-feat .di-card{padding:16px 0;}
.di-kicker{display:flex;align-items:baseline;gap:8px;font-size:11px;color:var(--muted);letter-spacing:.08em;margin-bottom:6px;}
.di-label{font-family:var(--display);font-size:17px;font-weight:700;line-height:1.42;margin:0 0 6px;}
.di-label a:hover{color:var(--accent-ink);}
.di-feat .di-label{font-size:20px;}
.di-summary{font-size:13px;color:var(--muted);line-height:1.7;margin:0 0 8px;}
.di-foot{display:flex;flex-wrap:wrap;gap:4px 16px;align-items:baseline;}
.di-links{display:flex;gap:12px;flex-wrap:wrap;margin-left:auto;}
.di-links a,.di-cites a{font-size:11.5px;color:var(--accent-ink);border-bottom:1px dotted var(--accent);white-space:nowrap;}
.di-links a:hover,.di-cites a:hover{color:var(--accent);}
.di-deep{margin-top:12px;background:var(--card-2);border:1px solid var(--line);padding:14px 18px 12px;}
.di-deep-kicker{font-size:11px;letter-spacing:.18em;color:var(--accent-ink);font-weight:700;display:flex;align-items:center;gap:10px;margin-bottom:10px;}
.di-deep-kicker::after{content:"";flex:1;height:1px;background:var(--line);}
.di-deep-sec{margin-bottom:9px;}
.di-deep-sec strong{display:block;font-size:11.5px;letter-spacing:.1em;color:var(--ink);margin-bottom:3px;}
.di-deep-sec p{font-size:12.5px;color:var(--muted);line-height:1.75;margin:0;}
.di-quote{font-family:var(--display);font-style:italic;font-size:14px;line-height:1.7;color:var(--ink);border-left:2px solid var(--accent);padding:1px 0 1px 12px;margin:10px 0;}
.di-deep-foot{display:flex;flex-wrap:wrap;gap:4px 16px;align-items:baseline;margin-top:2px;padding-top:8px;border-top:1px solid var(--line);}
.di-cites{display:flex;gap:12px;flex-wrap:wrap;}
.di-bubble{margin-top:30px;border-top:3px double var(--line-strong);border-bottom:3px double var(--line-strong);padding:14px 0 4px;}
.di-bubble-head{display:flex;align-items:center;gap:14px;margin-bottom:6px;}
.di-bubble-head::before,.di-bubble-head::after{content:"";flex:1;height:1px;background:var(--line-strong);}
.di-bubble-head-t{font-family:var(--display);font-size:15px;font-weight:700;letter-spacing:.14em;white-space:nowrap;}
.di-bubble-deck{font-size:11.5px;color:var(--muted);font-style:italic;margin:0 0 10px;}
.di-bubble-grid{display:grid;grid-template-columns:1fr 1fr;column-gap:34px;}
.di-bubble-card{padding:10px 0;border-bottom:1px solid var(--line);}
.di-bubble-card:nth-last-child(-n+2){border-bottom:none;}
.di-bubble-card h4{font-family:var(--display);font-size:15px;font-weight:700;line-height:1.45;margin:0 0 4px;}
.di-bubble-card h4 a:hover{color:var(--accent-ink);}
.di-bubble-card p{font-size:12px;color:var(--muted);line-height:1.65;margin:0 0 4px;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden;}
@media (max-width:760px){
.di-feat .di-label{font-size:18px;}
.di-links{margin-left:0;}
.di-bubble-grid{grid-template-columns:1fr;}
.di-bubble-card:nth-last-child(-n+2){border-bottom:1px solid var(--line);}
.di-bubble-card:last-child{border-bottom:none;}
}
"""


# ──────────────────── 破茧栏 HTML 构建 ────────────────────

def _build_bubble_html(bubble_breaker):
    """破茧栏：上下双细线夹住的报纸边栏（信息增量）。
    选入理由只在栏头下说明一次（旧版逐卡重复同一句，纯噪音——用户裁决 2026-10-01）。"""
    if not bubble_breaker:
        return ""
    cards = []
    for item in bubble_breaker:
        _bu = item.get("url", "")
        _bl = _esc(item.get("label", ""))
        _bh = ('<a class="di-bubble-link" href="%s" target="_blank" rel="noopener">%s&#8599;</a>'
               % (_esc(_bu), _bl)) if re.match(r'https?://', _bu or "", re.I) else _bl
        cards.append(
            '<article class="di-bubble-card">'
            '<h4 class="di-bubble-label">%s</h4>'
            '<p class="di-bubble-summary">%s</p>'
            '</article>' % (_bh, _esc(item.get("summary", ""))))
    return ('<aside class="di-bubble">\n'
            '  <div class="di-bubble-head">'
            '<span class="di-bubble-head-t">破茧栏 · 信息增量</span></div>\n'
            '  <p class="di-bubble-deck">依你的常读画像挑出的科技圈外增量——偶尔换换脑子</p>\n'
            '  <div class="di-bubble-grid">\n%s\n  </div>\n</aside>'
            % "\n".join(cards))


def _inject_into_ai_daily(clusters, theme, bubble_breaker=None):
    """将洞察子板块注入已生成的 ai-daily.html（在 <footer> 之前插入）。"""
    if not os.path.exists(_AI_DAILY_FILE):
        print("[每日洞察] ai-daily.html 不存在，跳过注入", file=sys.stderr)
        return

    try:
        with open(_AI_DAILY_FILE, "r", encoding="utf-8") as f:
            html = f.read()
    except Exception as e:
        print("[每日洞察] 读取 ai-daily.html 失败: %s" % e, file=sys.stderr)
        return

    if "daily-insight-start" in html:
        # 已注入过，先移除旧板块
        html = re.sub(
            r'<!-- daily-insight-start -->.*?<!-- daily-insight-end -->\s*',
            '', html, flags=re.DOTALL)

    # 构建事件卡片：有深读的进通栏特稿区，无深读的进双栏简报区（与日报 .cols 同栅格）
    feats, briefs = [], []
    for evt in clusters:
        _deep = evt.get("deep_analysis") or {}
        (feats if (_deep and _deep.get("status") != "degraded") else briefs).append(evt)
    feat_html = "\n".join(_evt_card_html(e, True) for e in feats)
    brief_html = "\n".join(_evt_card_html(e, False) for e in briefs)
    n_total, n_deep = len(clusters), len(feats)
    # 日报页首索引补第 vii 项（分类版块固定 6 个，洞察是第 7 区）
    vii_link = ('<a href="#sec-7"><span class="idx-n" style="color:var(--accent)">vii</span>'
                '深度洞察<span class="idx-cnt">%d</span></a>' % n_total)

    # 完整子板块 HTML（报纸风格，与日报 sec-head 同构）
    section_html = '''<!-- daily-insight-start -->
<style>
%s</style>
<section id="sec-7" class="sec di-sec">
  <div class="sec-head"><span class="sec-num" style="color:var(--accent)">07</span><h2 class="sec-name">深度洞察</h2><span class="sec-cnt">%d 条 · %d 篇深读</span></div>
  <p class="di-theme">%s</p>
  <div class="di-feat">
%s
  </div>
  <div class="cols">
%s
  </div>
%s
</section>
<!-- daily-insight-end -->
''' % (_DI_CSS, n_total, n_deep, _esc(theme), feat_html, brief_html,
       _build_bubble_html(bubble_breaker))

    # 在 <footer> 之前插入；若 footer 标记不存在则回退到 </body> 前
    if '<footer class="foot">' in html:
        html = html.replace('<footer class="foot">', section_html + '\n<footer class="foot">')
    elif '</body>' in html:
        html = html.replace('</body>', section_html + '\n</body>')
    else:
        print("[每日洞察] ai-daily.html 无注入锚点，跳过", file=sys.stderr)
        return

    # 索引补 vii（锚定 class="index" 的 nav，防未来新增 nav 后按文档序注错位置）
    html, n_nav = re.subn(r'(<nav class="index"[^>]*>.*?)(</nav>)',
                          lambda m: m.group(1) + vii_link + "\n  " + m.group(2),
                          html, count=1, flags=re.S)
    if n_nav == 0:
        print("[每日洞察] ai-daily.html 无索引导航，跳过 vii 注入", file=sys.stderr)

    try:
        with open(_AI_DAILY_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print("[每日洞察] 已注入 ai-daily.html 子板块")
    except Exception as e:
        print("[每日洞察] ai-daily.html 注入失败: %s" % e, file=sys.stderr)


# ──────────────────── 构建质量追踪日志 ────────────────────

class PipelineTracer:
    """端到端 LLM 调用追踪器。"""

    def __init__(self):
        self.stages = {}
        self.llm_calls = []
        self.meta = {}

    def set_meta(self, **kwargs):
        """设置构建元信息。"""
        self.meta.update(kwargs)

    def trace_queries(self, rss_count, hot_count, aihot_count, agihunt_count,
                      chunks_total, queries_count, retrieved_top_k):
        """Stage 1: 查询构建。"""
        self.stages["queries"] = {
            "source_counts": {"rss": rss_count, "hot": hot_count,
                              "aihot": aihot_count, "agihunt": agihunt_count},
            "chunks_total": chunks_total,
            "queries_count": queries_count,
            "retrieved_top_k": retrieved_top_k,
        }

    def trace_clustering(self, method, threshold, before_count, after_count,
                         events_summary):
        """Stage 2: 聚类与事件组装。"""
        self.stages["clustering"] = {
            "method": method,
            "threshold": threshold,
            "before_count": before_count,
            "after_count": after_count,
            "events": events_summary,
        }

    def trace_llm_call(self, phase, event_idx, prompt_text, model_name,
                       temperature, max_tokens, raw_response, parse_ok,
                       parsed_result, token_usage=None):
        """记录单次 LLM 调用。异常安全。"""
        try:
            # 确保 parsed_result 可序列化
            safe_result = parsed_result
            try:
                json.dumps(parsed_result, ensure_ascii=False, default=str)
            except Exception:
                safe_result = str(parsed_result)

            self.llm_calls.append({
                "phase": phase,
                "event_idx": event_idx,
                "model": model_name,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "prompt_chars": len(prompt_text) if prompt_text else 0,
                "prompt_preview": (prompt_text or "")[:5000],
                "raw_response_preview": (raw_response or "")[:3000],
                "parse_ok": parse_ok,
                "parsed_summary": safe_result,
                "token_usage": token_usage,
            })
        except Exception:
            pass  # 异常安全：不影响主流程

    def trace_self_review(self, issues_found, corrected_indices):
        """Stage 5: 自审环节。"""
        self.stages["self_review"] = {
            "issues": issues_found,
            "corrected_indices": corrected_indices,
        }

    def trace_ragas(self, iterations_log):
        """Stage 6: RAGAS 评估-修正闭环。"""
        self.stages["ragas"] = {"iterations": iterations_log}

    def trace_bubble(self, read_profile, candidates, selected):
        """Stage 7: 破茧栏选择。"""
        self.stages["bubble_breaker"] = {
            "read_profile": read_profile,
            "candidates": candidates,
            "selected": selected,
        }

    def trace_output(self, event_count, has_analysis_count, inject_html_len):
        """Stage 8: 输出与注入。"""
        self.stages["output"] = {
            "event_count": event_count,
            "has_analysis_count": has_analysis_count,
            "inject_html_chars": inject_html_len,
        }

    def flush(self):
        """输出完整追踪日志到 JSONL。"""
        now_bj = _now_bj()
        entry = {
            "ts": now_bj.isoformat(),
            "date": now_bj.strftime("%Y-%m-%d"),
            "meta": self.meta,
            "stages": self.stages,
            "llm_calls": self.llm_calls,
        }
        try:
            _log_tracking_entry(entry)
        except Exception:
            pass  # 异常安全
        return entry


def _log_tracking_entry(entry):
    """将构建追踪数据追加写入 daily_insight_tracking_history.jsonl。

    JSONL 格式，14 天滚动窗口，原子写入，异常安全。
    字段结构与 insight_tracking_history.jsonl 对齐。
    """
    try:
        now_bj = _now_bj()

        # 加载现有记录
        lines = []
        if os.path.exists(TRACKING_FILE):
            try:
                with open(TRACKING_FILE, "r", encoding="utf-8") as f:
                    lines = [l for l in f.readlines() if l.strip()]
            except Exception:
                lines = []

        # 追加新记录
        lines.append(json.dumps(entry, ensure_ascii=False) + "\n")

        # 14 天滚动窗口：淘汰过期条目
        cutoff = (now_bj - datetime.timedelta(days=14)).isoformat()
        filtered = []
        for line in lines:
            try:
                rec = json.loads(line)
                if rec.get("ts", "") >= cutoff:
                    filtered.append(line)
            except json.JSONDecodeError:
                continue
        lines = filtered

        # 原子写入
        _atomic_write_text(TRACKING_FILE, "".join(lines))
        print("[每日洞察] 追踪日志已记录 → %s (保留 %d 条)" % (TRACKING_FILE, len(lines)))
    except Exception as e:
        print("[每日洞察] 追踪日志失败: %s" % e, file=sys.stderr)


def _build_tracking_entry(ragas_eval, clusters, theme, elapsed,
                          rss_count, hot_count, aihot_count, agihunt_count,
                          chunks_count, filtered_count, llm_available,
                          embed_model="", threshold=None):
    """构建追踪日志条目。

    `threshold` 必须传修正环实际使用的那个值（可被 `daily_insight_quality_threshold` 覆盖），
    否则同行会出现"threshold 写 0.70、passed 按别的数判"的自相矛盾。
    """
    now_bj = _now_bj()
    has_analysis = sum(1 for c in clusters
                       if c.get("deep_analysis")
                       and c["deep_analysis"].get("status") != "degraded")

    # RAGAS 评分
    overall = ragas_eval.get("overall")
    threshold = RAGAS_QUALITY_THRESHOLD if threshold is None else threshold
    _qmeta = ragas_eval.get("meta") or {}
    _draft = _qmeta.get("draft_eval") or {}

    return {
        "ts": now_bj.isoformat(),
        "date": now_bj.strftime("%Y-%m-%d"),
        # RAGAS 质量评分
        "overall": overall,
        "context_coverage": ragas_eval.get("context_coverage"),
        "faithfulness": ragas_eval.get("faithfulness"),
        "relevance": ragas_eval.get("relevance"),
        "feedback": ragas_eval.get("feedback", ""),
        "iterations": ragas_eval.get("iterations"),
        # overall 自 09-19 起描述 shipped 终版报告；不记这两项就无法跨期比较
        "eval_stage": _qmeta.get("eval_stage", "draft_only"),
        "draft_overall": _draft.get("overall"),
        "degraded": _qmeta.get("degraded"),
        "threshold": threshold,
        # 09-19 起 `passed` 与同行所列 dims 同源（此前只判 overall，导致 24/43 行"看着绿、三维红"）；
        # 旧语义可由同行 overall/threshold 精确复算，故不再另设冗余字段。草稿环结论不可从本行推出，单独存。
        "passed": _verdict(ragas_eval, threshold),
        "passed_draft": _verdict(_draft, threshold) if _draft else None,
        # 事件统计
        "stats": {
            "total_events": len(clusters),
            "has_analysis_count": has_analysis,
            "rss_count": rss_count,
            "hot_count": hot_count,
            "aihot_count": aihot_count,
            "agihunt_count": agihunt_count,
            "chunks_total": chunks_count,
            "items_after_filter": filtered_count,
        },
        # 主题
        "theme": (theme or "")[:200],
        # 构建元信息
        "meta": {
            "llm_available": llm_available,
            "embed_model": embed_model,
            "elapsed_seconds": elapsed,
        },
    }


# ──────────────────── main ────────────────────

def main():
    global NOW_BJ
    NOW_BJ = _now_bj()

    # 配置
    cfg = load_config()
    if not cfg.get("daily_insight_enabled", True):
        print("[每日洞察] 已禁用，跳过")
        return False

    print("[每日洞察] 开始生成 %s 每日深度洞察..." % NOW_BJ.strftime("%Y-%m-%d"))
    t0 = time.time()

    # 加载 RSS tier 映射
    _load_rss_tiers()

    # ── 数据采集 ──
    snapshot = _load_snapshot()
    aihot_items = snapshot.get("aihot_items", [])
    agihunt_items = snapshot.get("agihunt_items", [])
    rss_history = _load_rss_history()
    hot_snapshot = _load_hot_snapshot()

    # RSS 历史转为条目列表（每日洞察只取最近 INSIGHT_RSS_HOURS 小时）
    rss_items = []
    for link, item in rss_history.items():
        if not isinstance(item, dict):
            continue
        title = item.get("title", "") or item.get("title_zh", "")
        if not title:
            continue
        pub_dt = _parse_iso(item.get("pub_date", ""))
        if _hours_ago(pub_dt) > INSIGHT_RSS_HOURS:
            continue
        rss_items.append({
            "title": title,
            "link": link,
            "source": item.get("source", ""),
            "source_key": item.get("source_key", ""),
            "cat": item.get("cat", ""),
            "summary": item.get("summary", ""),
            "full_content": item.get("full_content", ""),
            "pub_date": item.get("pub_date", ""),
            "_src": "rss",
        })

    # 热榜展平
    hot_items = []
    for platform in hot_snapshot:
        plat = platform.get("platform", "")
        for item in platform.get("items", []):
            title = item.get("title", "")
            if not title:
                continue
            hot_items.append({
                "title": title,
                "url": item.get("url", ""),
                "rank": item.get("rank", 50),
                "hot": item.get("hot", ""),
                "platform": plat,
                "_src": "hot",
            })

    print("[每日洞察] 数据汇总: RSS %d, 热榜 %d, AIHOT %d, AGI Hunt %d" % (
        len(rss_items), len(hot_items), len(aihot_items), len(agihunt_items)))
    # 追踪用统计
    _stats_rss = len(rss_items)
    _stats_hot = len(hot_items)
    _stats_aihot = len(aihot_items)
    _stats_agihunt = len(agihunt_items)

    # ── Phase 1.2: 硬过滤 ──
    rss_clean = _filter_noise(rss_items)
    aihot_clean = _filter_noise(aihot_items)
    agihunt_clean = _filter_noise(agihunt_items)
    # 热榜不过滤（本身就是平台精选）
    hot_clean = hot_items

    # ── Phase 1.3: RAG 管线 ──
    # 1) 切片
    chunks = _chunk_documents(rss_clean, hot_clean, aihot_clean, agihunt_clean)
    if not chunks:
        print("[每日洞察] 无 chunks，跳过生成")
        # 降级记录：即使无 chunks 也记录基础统计
        try:
            _log_tracking_entry(_build_tracking_entry(
                ragas_eval={}, clusters=[], theme="", elapsed=round(time.time() - t0, 1),
                rss_count=_stats_rss, hot_count=_stats_hot,
                aihot_count=_stats_aihot, agihunt_count=_stats_agihunt,
                chunks_count=0, filtered_count=0, llm_available=bool(llm)))
        except Exception:
            pass
        return False
    _stats_chunks_raw = len(chunks)
    _stats_filtered = len(rss_clean) + len(hot_clean) + len(aihot_clean) + len(agihunt_clean)

    # 1.5) 增量追加：加载旧缓存 → 合并去重 → 截断 → 仅新增 embedding
    old_chunks, old_vecs, old_model = _load_vector_cache()
    # 1.5+1.6) 合并 + 淘汰 + 封顶统一交给 _merge_vector_cache（判据见
    # tests/daily_insight/test_vector_cache_bounds.py）。旧写法只剪 RSS 一支、非RSS 永不淘汰，
    # 结果既撑爆上限又把 RSS 饿死到 100 条。
    merged_chunks, _vc = _merge_vector_cache(old_chunks, chunks,
                                             embed_budget=MAX_NEW_EMBED_PER_BUILD)
    # 每一项都要能加回同一个分母：池 = 留存 + 四个丢弃桶。上一版把"封顶时的额度分配"
    # 当成留存分项印出来，导致 留存 10040 与 RSS 21802+非RSS 7271 同框（run 35609254121），
    # 读日志的人（包括我自己）会把它当成新的截断缺陷去查。
    print("[每日洞察] 向量缓存合并: 去重后池 %d 条（旧 %d + 本场 %d，去重 %d）"
          "→ 留存 %d（RSS %d + 非RSS %d）"
          "｜丢弃：超龄 %d / 判不了龄 %d / 超上限 %d / 预算外推迟 %d（上限 %d）" % (
              _vc["pool_size"], _vc["input_old"], _vc["input_new"], _vc["dedup_removed"],
              _vc["kept"], _vc["kept_rss"], _vc["kept_other"],
              _vc["evicted_stale"], _vc["dropped_undated"], _vc["dropped_over_cap"],
              _vc["deferred_uncached"], _vc["cap"]))

    # 1.7) 识别需要 embedding 的 chunks（hash 不在旧缓存中的）
    cached_map = {}  # content_hash → index in old_vecs
    for i, c in enumerate(old_chunks):
        h = c.get("content_hash", "")
        if h:
            cached_map[h] = i
    need_embed = [c for c in merged_chunks if c.get("content_hash", "") not in cached_map]
    cached_count = len(merged_chunks) - len(need_embed)
    print("[每日洞察] 向量缓存命中 %d, 需新增 embedding %d" % (cached_count, len(need_embed)))

    # 1.8) 仅对新增 chunks 调 Embedding API
    new_vecs_list = []
    embed_model = old_model
    if need_embed:
        need_texts = [c.get("text", "")[:1000] for c in need_embed]
        new_vecs_result, new_model = _embed_chunks(need_texts)
        if new_vecs_result:
            new_vecs_list = new_vecs_result
            embed_model = new_model or old_model
        else:
            print("[每日洞察] 新增 embedding 失败，尝试降级到旧缓存", file=sys.stderr)

    # 1.9) 组装全量向量矩阵
    all_vecs = None
    if old_vecs is not None and len(old_chunks) > 0:
        # 按 merged_chunks 顺序组装：命中缓存的用旧向量，新增的用新计算。
        # 拿不到向量的条目**直接出局**，不再塞零向量占位 —— 零向量与 merged_chunks
        # 数量一致，形状检查发现不了，后果是检索静默失准（比少几条更坏）。
        vec_rows = []
        kept_chunks = []
        new_vec_idx = 0
        for c in merged_chunks:
            h = c.get("content_hash", "")
            if h in cached_map and old_vecs is not None:
                vec_rows.append(old_vecs[cached_map[h]])
                kept_chunks.append(c)
            elif new_vec_idx < len(new_vecs_list):
                vec_rows.append(_np.array(new_vecs_list[new_vec_idx], dtype=_np.float32))
                new_vec_idx += 1
                kept_chunks.append(c)
        if len(kept_chunks) != len(merged_chunks):
            print("[每日洞察] 无向量条目出局 %d 条（merged %d → %d）" % (
                len(merged_chunks) - len(kept_chunks), len(merged_chunks), len(kept_chunks)),
                  file=sys.stderr)
            merged_chunks = kept_chunks
        if vec_rows:
            all_vecs = _np.vstack(vec_rows)
    elif new_vecs_list:
        # 无旧缓存，全量新计算
        all_vecs = _np.array(new_vecs_list, dtype=_np.float32)

    # 2) 保存向量缓存 → 重建 FAISS 索引（先存向量防止 FAISS 失败丢失 embedding）
    index = None
    _used_merged = False
    if all_vecs is not None and len(all_vecs) > 0:
        _save_vector_cache(merged_chunks, all_vecs, embed_model)
        index = _build_faiss_index(all_vecs)
        try:
            if FAISS_AVAILABLE:
                _faiss.write_index(index, FAISS_INDEX_FILE + ".tmp")
                os.replace(FAISS_INDEX_FILE + ".tmp", FAISS_INDEX_FILE)
                print("[每日洞察] FAISS 索引持久化: %s" % FAISS_INDEX_FILE)
        except Exception as exc:
            print("[每日洞察] FAISS 索引保存失败: %s" % exc, file=sys.stderr)
        _used_merged = True
    else:
        print("[每日洞察] 无可用向量，尝试加载旧索引降级", file=sys.stderr)
        index, fallback_chunks = _load_index()
        if not index:
            print("[每日洞察] 无可用索引，跳过生成", file=sys.stderr)
            try:
                _log_tracking_entry(_build_tracking_entry(
                    ragas_eval={}, clusters=[], theme="", elapsed=round(time.time() - t0, 1),
                    rss_count=_stats_rss, hot_count=_stats_hot,
                    aihot_count=_stats_aihot, agihunt_count=_stats_agihunt,
                    chunks_count=_stats_chunks_raw, filtered_count=_stats_filtered,
                    llm_available=bool(llm), embed_model=embed_model or ""))
            except Exception:
                pass
            return False
    # 后续流程：index 必须与 chunks 对齐
    # 正常路径用 merged_chunks（与新建 index 对齐）
    # 降级路径用 fallback_chunks（与旧 index 对齐）
    if _used_merged:
        chunks = merged_chunks
    else:
        chunks = fallback_chunks

    # 3) 构建查询：混合查询构建
    queries = _build_queries(hot_clean, agihunt_clean, aihot_clean, rss_clean=rss_clean)

    # 4) 混合检索 → 重排序 → 事件组装
    _retrieval_stats = {}
    retrieved = _hybrid_retrieve(index, chunks, queries, stats=_retrieval_stats)
    if not retrieved:
        print("[每日洞察] 检索无结果，跳过生成")
        try:
            _log_tracking_entry(_build_tracking_entry(
                ragas_eval={}, clusters=[], theme="", elapsed=round(time.time() - t0, 1),
                rss_count=_stats_rss, hot_count=_stats_hot,
                aihot_count=_stats_aihot, agihunt_count=_stats_agihunt,
                chunks_count=_stats_chunks_raw, filtered_count=_stats_filtered,
                llm_available=bool(llm), embed_model=embed_model or ""))
        except Exception:
            pass
        return False
    # 保留检索结果供 RAGAS 评估（retrieved 是列表，不会被消费）
    retrieved_for_ragas = retrieved
    retrieved = _rerank(retrieved, hot_snapshot)
    clusters = _assemble_events(retrieved)
    if not clusters:
        print("[每日洞察] 无事件簇，跳过生成")
        try:
            _log_tracking_entry(_build_tracking_entry(
                ragas_eval={}, clusters=[], theme="", elapsed=round(time.time() - t0, 1),
                rss_count=_stats_rss, hot_count=_stats_hot,
                aihot_count=_stats_aihot, agihunt_count=_stats_agihunt,
                chunks_count=_stats_chunks_raw, filtered_count=_stats_filtered,
                llm_available=bool(llm), embed_model=embed_model or ""))
        except Exception:
            pass
        return False

    # 5) 事件级打分
    clusters = _score_events(clusters, hot_snapshot)

    # ── Phase 3.3: 跨天事件关联 ──
    history = _load_history()
    clusters = _match_events_to_history(clusters, history)

    # ── Phase 2: LLM 生成 ──
    llm = _init_llm()
    has_analysis = False
    theme = ""
    _yday_labels = set()  # Phase1 失败路径下 RAGAS 块刷新分数不得 NameError（审查 P1-1）

    if llm:
        # Phase 1 前素材预过滤：移除与事件标签明显无关的 items，防止内容串位
        for c in clusters:
            _filter_cluster_items(c, c.get("label", ""))

        # Phase 1 LLM: 全事件摘要（传入全局检索上下文，解决生成/评估不对齐问题）
        _ragas_ctx = _build_ragas_context(clusters, retrieved_for_ragas) if retrieved_for_ragas else ""
        p1_result = _llm_phase1(llm, clusters, global_context=_ragas_ctx[:12000] if _ragas_ctx else None)
        if p1_result:
            theme = _apply_phase1_result(clusters, p1_result)

            # Phase 1.2: 丢弃 [信息不足] 事件 — 防止占位事件拉低质量（前缀匹配，覆盖 [信息不足：...]）
            _before = len(clusters)
            clusters = _drop_insufficient(clusters)
            _dropped = _before - len(clusters)
            if _dropped:
                print("[每日洞察] 丢弃 %d 个 [信息不足] 事件" % _dropped)

            # Phase 1.3: 跨源去重 — 合并同 category 且标签相似的事件
            clusters = _deduplicate_after_phase1(clusters)

            # Phase 1.5: 事实核查 — 确保 faithfulness
            try:
                _verify_faithfulness(llm, clusters, retrieved_for_ragas)
            except Exception as exc:
                print("[每日洞察] 事实核查异常，跳过: %s" % exc, file=sys.stderr)

            # Phase 1.6: 自审环节 — 检查 Phase 1 输出质量
            try:
                all_material = "\n".join(
                    _build_event_material(c)[:300] for c in clusters[:MAX_EVENTS])
                _self_review_phase1(llm, clusters, all_material)
            except Exception as exc:
                print("[每日洞察] 自审环节异常，跳过: %s" % exc, file=sys.stderr)

            # Phase 1.7: 自审后最终去重 — 捕获自审修改 label 后产生的重复
            clusters = _deduplicate_after_phase1(clusters)

            # Phase 1.8: AI 主题硬过滤 — 清除与 AI/科技无关的噪声事件
            _AI_KEYWORDS = _AI_NEWS_RE
            _NON_AI_CATS = {'bbc top stories', 'fun', 'sport', 'entertainment',
                            'politics', 'celebrity', 'lifestyle', 'travel',
                            'food', 'fashion', 'music', 'film', 'tv'}
            _filtered = []
            for c in clusters:
                cat = (c.get("category", "") or "").lower()
                label = c.get("label", "") or ""
                # 明确非 AI 分类 → 检查 label 是否有 AI 关键词
                if cat in _NON_AI_CATS:
                    if not _AI_KEYWORDS.search(label):
                        print("[每日洞察] 过滤非AI事件: %s (cat=%s)" % (label[:40], cat))
                        continue
                # 检查 source_types 是否全为非 AI 源
                src_types = c.get("source_types", []) or []
                non_ai_srcs = {'bbc', 'cnn', 'reuters', 'guardian'}
                if src_types and all(s.lower() in non_ai_srcs for s in src_types):
                    if not _AI_KEYWORDS.search(label):
                        print("[每日洞察] 过滤非AI源事件: %s" % label[:40])
                        continue
                _filtered.append(c)
            if len(_filtered) < len(clusters) and len(_filtered) >= MIN_EVENTS:
                clusters = _filtered

            # 第9轮：重叠判定交评审 agent 主判（判例驱动），机械四轮闸降为 agent 不可用时兜底
            clusters = _llm_consolidate_events(llm, clusters)

            # Phase 2 LLM: Top N 深度解读（快筛门槛：editor_score ≥ MIN_DEEP_SCORE 才做深度分析）
            _gate_days = [d for d in _load_history().get("days", [])
                          if d.get("date") != NOW_BJ.strftime("%Y-%m-%d")]
            _yday_labels = {(e.get("label") or "").strip()
                            for e in (_gate_days[-1].get("events", []) if _gate_days else [])}
            # 素材过滤必须在打分之前对**全部**簇执行。原先只对入选深挖的簇补一次过滤，
            # 而后段还会用剪过的 items 重算 editor_score 再排序 —— 被选中者单方面被降分，
            # 于是"榜首仍可能没有深度解读"（审查 P0-2 实测：75 分被剪到 62，反超者无深挖）。
            for c in clusters:
                _filter_cluster_items(c, c.get("label", ""))
            for c in clusters:
                c["editor_score"] = _quick_score_event(c, yesterday_labels=_yday_labels)
            _deep_idx = _deep_candidate_order(clusters)
            _rss_by_url = {_norm_url(k): v for k, v in rss_history.items() if isinstance(v, dict)}
            _skipped_low = 0
            for i in _deep_idx:
                c = clusters[i]
                if c["editor_score"] < MIN_DEEP_SCORE:
                    c["deep_analysis"] = None
                    _skipped_low += 1
                    continue
                # Phase 2 前素材过滤：移除与 label 不相关的 items
                _filter_cluster_items(c, c.get("label", ""))
                prev_summary = c.get("prev_summary") if c.get("status") in ("ongoing", "escalating") else None
                _ft_hits = _fulltext_hit_urls(c, _rss_by_url)
                deep = _llm_phase2(llm, c, {
                    "label": c.get("label", ""),
                    "summary": c.get("summary", ""),
                }, prev_summary,
                    evidence=_build_evidence_pack(c, _rss_by_url, skip_urls=_ft_hits),
                    rss_by_url=_rss_by_url)
                c["deep_analysis"] = deep
                if deep.get("status") == "ok":
                    has_analysis = True
                    # 引用溯源回填：key_links 优先取实际被引用的素材链接（防 LLM 乱填同质 URL）
                    _cited_urls = []
                    for s in deep.get("cited_sources", []):
                        u = s.get("url", "")
                        if u and u not in _cited_urls:
                            _cited_urls.append(u)
                    if _cited_urls:
                        c["key_links"] = _cited_urls[:3]
                    print("[每日洞察] 深度解读完成: %s (引用 %d 源)" % (
                        c.get("label", "")[:30], len(deep.get("cited_sources", []))))
                else:
                    print("[每日洞察] 深度解读降级: %s (%s)" % (
                        c.get("label", "")[:30], deep.get("reason", "unknown")))
            if _skipped_low:
                print("[每日洞察] 快筛门槛: %d 个事件 editor_score < %d，跳过深度分析" % (
                    _skipped_low, MIN_DEEP_SCORE))
        else:
            print("[每日洞察] Phase 1 LLM 失败，降级为仅聚类结果", file=sys.stderr)
    else:
        print("[每日洞察] LLM 不可用，降级为仅聚类结果", file=sys.stderr)

    # Phase 2.4: 热度排序提前至 RAGAS 前——judge 评估的是最终展示序（relevance 定义含"重大在前"），
    # 且修正循环 weak_events 索引与该序自洽
    clusters = _order_events_for_output(clusters)

    # ── Phase 2.5: RAGAS 质量评估与自我修正 ──
    ragas_eval = {}
    judge_llm = None
    build_cfg = {}
    if clusters:
        try:
            build_cfg = load_config()
            judge_llm = _init_judge_llm(build_cfg)
            if judge_llm:
                clusters, ragas_eval = _ragas_evaluate_and_correct(
                    judge_llm, clusters, theme, retrieved_for_ragas, build_cfg,
                    retrieval_stats=_retrieval_stats)
                # G1: judge 漏点回收为新事件——必须在终局去重之前进入，吃全套闸
                _missed = (ragas_eval or {}).get("missed_points") or []
                if _missed:
                    _pool_urls = {_norm_url(ch.get("url", "") or ch.get("link", ""))
                                  for ch in (retrieved_for_ragas or [])} - {""}
                    _new_evts = _recover_missed_events(llm, clusters, _missed, index, chunks,
                                                       pool=_pool_urls)
                    if _new_evts:
                        clusters = clusters + _new_evts
                # 修正循环重写了 label/summary/category，可能事后制造重复 → 去重必须在其后再跑一轮
                clusters = _deduplicate_after_phase1(clusters)
                # faith 终局闸：修正环改写的正文复查一次（06:12 期幻觉回归实证），再刷新分数
                _final_faith_recheck(llm, clusters, retrieved_for_ragas)
                # 修正循环改写了 label/summary 后刷新门槛分，保证导出 JSON 分数描述最终内容
                for c in clusters:
                    if "editor_score" in c or c.get("summary"):
                        c["editor_score"] = _quick_score_event(c, yesterday_labels=_yday_labels)
            else:
                print("[每日洞察] Judge LLM 不可用，跳过 RAGAS 评估", file=sys.stderr)
        except Exception as exc:
            print("[每日洞察] RAGAS 评估异常，跳过: %s" % exc, file=sys.stderr)

    # 占位闸（无条件）：事实核查/自审/RAGAS 修正环都可能在 Phase 1.2 之后注入[信息不足]
    clusters = _drop_insufficient(clusters)
    clusters = _order_events_for_output(clusters)  # 闸与分数刷新后重排，保证导出 editor_score 单调
    clusters = clusters[:MAX_EVENTS]  # T1: 输出预算收口 12，被去重/占位闸砍掉的槽位由池内后位候选顶上

    # 主题必须在收口之后、终版复评之前对齐到正文：
    # 否则头版会承诺正文没给证据的事，而 cov/rel 又是拿这个旧主题打的分（度量被污染）。
    theme, _theme_dropped = _align_theme_to_events(theme, clusters)
    if _theme_dropped:
        print("[每日洞察] 主题对齐: 裁掉 %d 个无事件承载的从句 -> %s" % (
            len(_theme_dropped), " / ".join(d[:24] for d in _theme_dropped)))

    # 终版复评：quality 必须描述收口后的 shipped 报告，而非回收前的草稿（07:14 期实证）
    # use_cv 与草稿评估同源，保证两期分数在同一口径上可比；纯观测项，异常绝不挡写盘
    try:
        ragas_eval = _confirm_shipped_report_eval(
            judge_llm, clusters, theme, retrieved_for_ragas, ragas_eval,
            use_cv=bool(build_cfg.get("daily_insight_cross_validation", False)),
            retrieval_stats=_retrieval_stats)
    except Exception as exc:
        print("[每日洞察] 终版复评兜底异常，沿用草稿分: %s" % exc, file=sys.stderr)

    # ── Phase 3.1: 破茧栏 + 输出 JSON ──
    history = _load_history()
    read_profile = _build_read_profile(history)
    bubble_breaker = _select_bubble_events(clusters, read_profile, residual_chunks=retrieved_for_ragas, all_chunks=chunks)
    _write_insight_json(clusters, theme, has_analysis, ragas_eval, bubble_breaker)

    # ── Phase 3.3: 更新历史 ──
    _update_history(clusters, theme)

    # ── Phase 4: 历史页面 ──
    _build_history_html()

    # ── 注入 AI 日报子板块 ──
    _inject_into_ai_daily(clusters, theme, bubble_breaker)

    elapsed = round(time.time() - t0, 1)
    print("[每日洞察] 完成，耗时 %.1f 秒，%d 个事件" % (elapsed, len(clusters)))

    # ── Phase 5: 追踪日志 ──
    try:
        tracking_entry = _build_tracking_entry(
            ragas_eval=ragas_eval,
            clusters=clusters,
            theme=theme,
            elapsed=elapsed,
            rss_count=_stats_rss,
            hot_count=_stats_hot,
            aihot_count=_stats_aihot,
            agihunt_count=_stats_agihunt,
            chunks_count=_stats_chunks_raw,
            filtered_count=_stats_filtered,
            llm_available=bool(llm),
            embed_model=embed_model or "",
            threshold=(build_cfg or {}).get("daily_insight_quality_threshold",
                                            RAGAS_QUALITY_THRESHOLD),
        )
        _log_tracking_entry(tracking_entry)
    except Exception as exc:
        print("[每日洞察] 追踪日志异常，跳过: %s" % exc, file=sys.stderr)

    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
