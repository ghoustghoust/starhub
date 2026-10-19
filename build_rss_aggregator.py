# -*- coding: utf-8 -*-
"""Generate rss-aggregator.html — 多路 RSS 新闻源聚合阅读器。
构建时由 fetch_and_build.py 调用，Python 标准库抓取 RSS → 翻译 → 生成静态 HTML。

布局：卡片墙 + 抽屉阅读器（信源面板 | 卡片墙 | 阅读抽屉）
功能：信源分类筛选、全局搜索、标题/摘要翻译、响应式三端适配、主题切换。
"""
import collections
import functools
import concurrent.futures
import html as html_mod
import datetime
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import uuid
import build_logger
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

if sys.stdout and hasattr(sys.stdout, 'buffer'):
    # 仅为 Windows 控制台 GBK 问题重挂 UTF-8 流。closefd=False 是必须的：直接
    # open(fileno) 得到的对象被回收时会关掉共用 fd，pytest 恢复自己的捕获流后
    # 再读就报 Bad file descriptor，整场 CI 在收尾处崩掉。
    try:
        sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8',
                          buffering=1, closefd=False)
    except (OSError, ValueError):
        pass
if sys.stderr and hasattr(sys.stderr, 'buffer'):
    try:
        sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8',
                          buffering=1, closefd=False)
    except (OSError, ValueError):
        pass

OUT = "rss-aggregator.html"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", "Accept": "application/rss+xml, application/xml, text/xml, */*"}

# ── 缓存配置 ──
TRANS_CACHE_FILE = "translations.json"
# 翻译缓存条数上限。它是**已入库文件**（update.yml 的 add 清单里有 translations.json），
# 生产实测 101,643 条 / 20,731,886 字节且从不裁剪；同函数里的 RSS 缓存早就按 TTL 裁剪了，
# 两边机制必须一致，否则"防止文件无限膨胀"只对一半。
# 取 30,000 的依据：单场命中约 8,400 条不同文本，30k ≈ 3.5 倍单场工作集；
# 被逐出的都是"72h 窗口里再也不会遇到"的历史标题，代价是个别重遇时重译一次（1 次 API 调用）。
TRANS_CACHE_MAX = 30000
RSS_CACHE_FILE = "rss_cache.json"
RSS_CACHE_TTL = 1800  # RSS 缓存有效期：30 分钟

# 派生显示字段：值能由 pub_date 现算，烘进产物就等于"每场全量重写"。
# 实测（远端相邻两场 8b45cc9d/7966a25d）：9,115 条共同条目里 51% 只因 time_str
# 不同而对不上，把它移出条目后相同率 44.5% -> 89.9%。
# 它是唯一事实源：装配处不再写，出厂处按它剥离 —— 两处必须同时改，只改一处等于没挡。
ITEM_DERIVED_FIELDS = frozenset({"time_str"})


def _read_rel_time_js():
    """读取页面唯一的时间口径实现（lib/rel_time.js），构建期原样内联进产物。

    为什么读文件而不是写在模板里：这条口径过去有两个执行点（构建期 Python 与页面显示），
    历史上"两边各写一遍"已经栽过（留存规则、抽屉日期）。写成一处文件 + 一条对等测试，
    改的人就不需要"记得同步"。判据：tests/rss_history/test_time_label_parity.py
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "rel_time.js")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        # 不咽：缺这个文件页面时间位会整片空，宁可在构建期当场炸
        raise RuntimeError("缺少 lib/rel_time.js，页面相对时间无处可算: %s" % e)


def _strip_derived_items(items):
    """出厂条目的派生字段剥离。返回副本列表，历史里那份对象不被改动。"""
    out = []
    for it in items:
        if isinstance(it, dict) and (set(it) & ITEM_DERIVED_FIELDS):
            out.append({k: v for k, v in it.items() if k not in ITEM_DERIVED_FIELDS})
        else:
            out.append(it)
    return out


# 翻译缓存的账本：与历史侧 `_LAST_HISTORY_ACCOUNT` 同一形态。
# 约定也一致：字段为 None 表示"这一步没跑到"，不许用 0 冒充"跑到了但没丢"。
_LAST_TRANS_ACCOUNT = {}

# ── newsnow 热榜快照 ──
# API 格式: /api/s?id={platform}，每平台单独请求
NEWSNOW_API_TMPL = "https://newsnow.busiyi.world/api/s?id=%s"
NEWSNOW_TIMEOUT = 10
HOT_SNAPSHOT_FILE = "hot_snapshot.json"
HOT_HISTORY_FILE = "hot_history.json"  # 热榜轨迹历史（保留 7 天）
# 期望的热榜源（全量接入 newsnow 可用源，故障源 zhihu-daily/36kr/linuxdo/ghxi/kuaishou/smzdm/freebuf 不纳入）
NEWSNOW_PLATFORMS = [
    # ── 综合热搜 ──
    "weibo", "zhihu", "baidu", "bilibili", "douyin",
    # ── 科技/开发者/AI ──
    "ithome", "hackernews", "github", "solidot", "sspai",
    "juejin", "v2ex", "producthunt", "aihot",
    "chongbuluo", "pcbeta", "nowcoder", "coolapk",
    # ── 商业/财经 ──
    "xueqiu", "zaobao", "wallstreetcn", "cls", "jin10",
    "gelonghui", "fastbull",
    # ── 综合资讯 ──
    "toutiao", "tencent", "thepaper", "ifeng",
    "cankaoxiaoxi", "sputniknewscn", "kaopu", "mktnews",
    # ── 生活/娱乐/体育 ──
    "douban", "tieba", "hupu", "steam",
    "iqiyi", "qqvideo", "dongqiudi",
]

# ── 缓存数据 ──
_trans_cache = {}  # {text_hash: translated_text}
_rss_cache = {}    # {source_key: {"items": [...], "fetched_at": timestamp}}

# ── 智能分析配置 ──
ANALYSIS_SNAPSHOT_FILE = "analysis_snapshot.json"
SOURCE_QUALITY_FILE = "source_quality.json"
RSS_TREND_HISTORY_FILE = "rss_trend_history.json"  # RSS 内容趋势历史（保留 14 天）
TRENDING_SNAPSHOT_FILE = "trending_snapshot.json"  # GitHub Trending 数据
INSIGHT_TRACKING_FILE = "insight_tracking_history.jsonl"  # 洞察板块全量追踪日志
# 中英文停用词表（关键词提取时过滤）
_STOP_WORDS_ZH = set("的了是在我有和就不人都一个上也这到说们为你会对" +
    "他就是那要被她它自己什么没有可以已经还是或者虽然但是因此如果" +
    "而且并且或者以及不过然后所以因为于在与及等和而关于中从把被让给" +
    "向由按照根据为了因作为以更最非常再又还已经正在才刚各每全部" +
    "表示指出认为透露宣布发布推出上线下线升级更新修复" +  # 新闻动词（过于通用）
    "据悉据报道消息称知情人士透露" +  # 新闻套话
    "相关有关涉及方面部门机构组织" +  # 泛化名词
    "进行开展实施推进落实加强深化" +  # 公文动词
    "重要重大显著明显突出关键核心" +  # 泛化形容词
    "发展建设改革完善优化提升推动促进")  # 泛化动词
_STOP_WORDS_EN = set(("the a an is are was were be been being have has had do does did will would shall should may might can could " +
    "i me my we our you your he him his she her it its they them their " +
    "this that these those there here what which who whom whose when where why how " +
    "and or but not no nor so yet for to of in on at by with from as into about through during before after above below between under " +
    "again further then once also just than very too only same other some such all each every both few more most " +
    "new old first last long great little own right big high small next early young important public bad able " +
    # URL / HTML 残留噪声
    "https http ftp com org net edu gov io co www " +
    "id item html htm url href src png jpg gif css js xml json nbsp div span class " +
    "amp lt gt quot mdash ndash laquo raquo " +
    "aihot ycombinator").split())
_STOP_WORDS = _STOP_WORDS_ZH | _STOP_WORDS_EN

# ── 分析功能开关（从 build_config.json 读取） ──
# 配置路径按本文件位置解析，不用相对路径：相对路径依赖进程 CWD，
# 从别的目录导入本模块时会静默读到另一个同名 build_config.json
# （实测在诱饵 CWD 下读到 {enabled: False, window_minutes: 45}，而非回落默认值）。
_BUILD_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_config.json")


def _load_analysis_enabled():
    """从 build_config.json 读取 analysis_enabled 开关，文件缺失或损坏时默认开启。"""
    try:
        with open(_BUILD_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("analysis_enabled", True))
    except Exception:
        return True

ANALYSIS_ENABLED = _load_analysis_enabled()


def _load_diverse_config():
    """从 build_config.json 读取 diverse 排序配置，文件缺失或损坏时返回默认值。"""
    try:
        with open(_BUILD_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return {
            "enabled": bool(cfg.get("diverse_enabled", True)),
            "window_minutes": int(cfg.get("diverse_window_minutes", 120)),
        }
    except Exception:
        return {"enabled": True, "window_minutes": 120}


DIVERSE_CFG = _load_diverse_config()

# ── 72 小时内容累积 ──
RSS_HISTORY_FILE = "rss_history.json"
RSS_HISTORY_HOURS = 72
_rss_history = {}  # {link: {source, source_key, cat, color, title, title_zh, summary, summary_zh, pub_date, time_str}}
_LAST_UNRELIABLE_SRCS = set()  # D4：最近一场 bad_date 审计判定的不可信日期源（关键词降权消费）
_LAST_RETENTION_STATS = {}     # 最近一场留存闸门的统计（构建日志消费：闸后口径唯一可见面）

# ── Fix 4: T4 长尾层常量 ──
# T4 源：低优先级/长尾源，使用更宽松的跳过阈值和更短的历史保留窗口
# 目的：给频繁失败的低优先级源更多恢复机会，同时避免它们占用过多历史空间
_TIER4_HISTORY_HOURS = 48              # T4 源历史保留窗口：48h（默认 72h）
_TIER_PROMOTION_THRESHOLD = 3          # 连续 3 次成功抓取后自动升级到 T3
_tier4_success_streak = {}             # {source_key: 连续成功次数}

# ── 翻译统计 
_TRANS_STATS = {"agnes": 0, "zen": 0, "google": 0, "bing": 0, "mymemory": 0, "dict": 0, "skip": 0, "fail": 0, "cache_hit": 0}
# GA 免费翻译端点已被数据中心 IP 封锁（429/timeout），AGNES_API_KEY 存在时首选 Agnes AI。
# 多 key 轮询：AGNES_API_KEY（主）+ AGNES_API_KEYS（逗号分隔附加），429 自动切换
_AGNES_KEYS = [k.strip() for k in os.environ.get("AGNES_API_KEY", "").split(",") if k.strip()]
_extra_agnes = os.environ.get("AGNES_API_KEYS", "")
if _extra_agnes:
    _AGNES_KEYS.extend(k.strip() for k in _extra_agnes.split(",") if k.strip())
_AGNES_KEY_IDX = 0
# Agnes 免费但限流：HTTP 错误与连续空响应都按连续违规指数退避（5→10→20→40 分钟，封顶 1h），
# 期间直接走后续端点，不浪费每次 0.4s 的撞墙（对齐 Zen Z1 账本语义）
_AGNES_BLOCK_UNTIL = 0.0
_AGNES_OFFENSES = 0
_AGNES_EMPTY_STREAK = 0
# 多 key 轮转时的"这一圈是不是全吃了 429"连击计数：轮完一整圈仍全 429 才记罚期。
# 实现方式是"之后不再发请求"（调用方与本函数入口都查 BLOCK_UNTIL），**不是 sleep** ——
# 构建期等待会拉长整场构建，而少打几次网关只会让构建变快。
_AGNES_429_STREAK = 0
_AGNES_EMPTY_LIMIT = 3  # 连续空响应阈值（模型偶发拒绝 vs 端点异常的分界）
# OpenCode Zen 免费模型（https://opencode.ai/docs/zen/）：OpenAI 兼容端点，免费档需 OpenCode 客户端会话头。
# 实测（2026-09-13）：ling 2.4s / big-pickle 5.6s / mimo 13.1s 可用；muse-spark 稳定 500、nemotron 两款 88s+，不入轮询。
_ZEN_KEY = os.environ.get("ZEN_API_KEY", "") or os.environ.get("OPENCODE_KEY", "")
_ZEN_MODELS = ([m.strip() for m in os.environ["ZEN_TRANSLATE_MODEL"].split(",") if m.strip()]
               if os.environ.get("ZEN_TRANSLATE_MODEL")
               else ["ling-3.0-flash-fin-free", "big-pickle", "mimo-v2.5-free"])
_ZEN_URL = "https://opencode.ai/zen/v1/chat/completions"
_ZEN_MODEL_BLOCK = {}   # model → 自封截止时间戳
_ZEN_MODEL_OFFENSES = {}  # model → 连续自封次数（自封期翻倍：5→10→20→40 分钟，封顶 1h；成功清零）
_ZEN_MODEL_IDX = 0     # 轮询游标（翻译线程池共享，_TRANS_LOCK 保护）
_ZEN_AUTH_STICKY = None  # 本场构建实测可用的鉴权 token；key 撞 401/403 后粘性回退 "public"
_ZEN_TIMEOUT_STREAK = {}  # model → 连续超时/网络失败计数（≥2 自封；成功重置）
# 翻译熔断：连续 5 次全端点失败后暂停翻译请求 5 分钟（避免上游故障时的请求风暴与构建拖长）
_TRANS_FAIL_STREAK = 0
_TRANS_BLOCK_UNTIL = 0.0
_TRANS_LOCK = threading.Lock()  # 并行翻译线程池下的熔断状态保护


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


def _atomic_write_json(path, obj, **dump_kw):
    """原子写 JSON：先写 .tmp 再 os.replace，进程中断不会留下半截文件"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, **dump_kw)
    os.replace(tmp, path)


def _atomic_write_text(path, text):
    """原子写文本文件：先写 .tmp 再 os.replace"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _strip_oss_signature(text):
    """剥离 URL 中的 OSS/云存储签名参数，防止 GitHub Push Protection 误报密钥泄露。
    移除 OSSAccessKeyId、Signature、Expires、X-Amz-* 等查询参数。
    同时处理 HTML 编码（&amp; / &#38;）的查询参数分隔符。"""
    if not text or not isinstance(text, str):
        return text
    # 匹配 ? 或 & 或 &amp; 或 &#38; 后面的签名参数
    _sep = r'(?:[?&]|&amp;|&#38;)'
    _params = r'(?:OSSAccessKeyId|Signature|Expires|X-Amz-[A-Za-z0-9-]+)=[^&\s"\'<>;]*'
    text = re.sub(_sep + _params, '', text)
    # 清理残留
    text = re.sub(r'\?&amp;', '?', text)
    text = re.sub(r'\?&#38;', '?', text)
    text = re.sub(r'\?&', '?', text)
    text = re.sub(r'&&', '&', text)
    text = re.sub(r'\?[\"\'>\s]', '', text)
    return text


def _trans_touch(text_hash):
    """命中即移到 dict 末尾：Python 保序，末尾=最近使用，头部=最久未用。

    不 touch 的话 LRU 会退化成"插入序"，一条被高频命中的老译文反而最先被裁掉。
    """
    if text_hash in _trans_cache:
        _trans_cache[text_hash] = _trans_cache.pop(text_hash)


def _trim_trans_cache():
    """把翻译缓存裁到 TRANS_CACHE_MAX，返回丢弃条数（并记账）。"""
    before = len(_trans_cache)
    if before > TRANS_CACHE_MAX:
        # 整体重建而不是逐 key pop：pop 版要在循环里反复构造 set，
        # 3 万条规模下是 O(n·cap) 的自杀式写法。clear+update 保序且线性。
        tail = list(_trans_cache.items())[-TRANS_CACHE_MAX:]
        _trans_cache.clear()
        _trans_cache.update(tail)
    dropped = before - len(_trans_cache)
    _LAST_TRANS_ACCOUNT["size"] = len(_trans_cache)
    _LAST_TRANS_ACCOUNT["trimmed"] = dropped
    return dropped


def _save_trans_cache():
    """翻译缓存：先按 LRU 裁剪再紧凑落盘。

    indent=2 去掉 —— 这个文件只有本脚本自己读（全仓核过：前端与 api/ 都不消费），
    20.7MB 里约三成是缩进空白，每场整文件重新入库，直接喂给已经 6.8GB 的 .git。
    """
    try:
        trimmed = _trim_trans_cache()
        _atomic_write_json(TRANS_CACHE_FILE, _trans_cache, ensure_ascii=False,
                           separators=(",", ":"))
        print("[缓存] 保存翻译缓存: %d 条%s" % (
            len(_trans_cache),
            "（LRU 裁剪 %d 条，上限 %d）" % (trimmed, TRANS_CACHE_MAX) if trimmed else ""))
    except Exception as e:
        _LAST_TRANS_ACCOUNT["size"] = None
        _LAST_TRANS_ACCOUNT["trimmed"] = None
        print("[缓存] 保存翻译缓存失败: %s" % e, file=sys.stderr)


def _save_caches():
    """保存翻译和 RSS 缓存（两者写盘前都裁剪，防止文件无限膨胀）。"""
    _save_trans_cache()
    # RSS 缓存仅保留 TTL 内条目（跨 run 基本全过期，裁剪+紧凑序列化使文件从几十 MB 降至 MB 级）；
    # 该文件已移出 git 跟踪（见 .gitignore），仅服务单次运行内的抓取加速
    pruned = {k: v for k, v in _rss_cache.items()
              if time.time() - v.get("fetched_at", 0) < RSS_CACHE_TTL}
    dropped = len(_rss_cache) - len(pruned)
    try:
        _atomic_write_json(RSS_CACHE_FILE, pruned, ensure_ascii=False, separators=(",", ":"))
        print("[缓存] 保存 RSS 缓存: %d 个源%s" % (len(pruned), "（裁剪过期 %d 个）" % dropped if dropped else ""))
    except Exception as e:
        print("[缓存] 保存 RSS 缓存失败: %s" % e, file=sys.stderr)


def fetch_newsnow_snapshot():
    """构建时逐平台抓取 newsnow 热榜快照，失败返回空列表（不阻塞构建）。
    新 API: GET /api/s?id={platform} -> {status, id, items: [{id,title,url,mobileUrl,extra}]}
    """
    result = []
    for plat in NEWSNOW_PLATFORMS:
        url = NEWSNOW_API_TMPL % plat
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=NEWSNOW_TIMEOUT)
            ct = resp.headers.get("Content-Type", "")
            if "json" not in ct.lower():
                print("[热榜] %s 返回非JSON (%s)，跳过" % (plat, ct), file=sys.stderr)
                continue
            data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print("[热榜] %s 拉取失败: %s" % (plat, e), file=sys.stderr)
            continue
        # 新格式: {status, id, updatedTime, items: [{id, title, url, mobileUrl, extra}], info}
        items = data.get("items") if isinstance(data, dict) else None
        if not items or not isinstance(items, list):
            print("[热榜] %s 无items，跳过" % plat, file=sys.stderr)
            continue
        entries = []
        for idx, it in enumerate(items[:10]):
            _t = it.get("title", "")
            # 上游把纯数字标题写成 JSON number（10-14 pcbeta "26300.9711" 连红三场），
            # 热榜又不过 _filter_noise ⇒ 只收"真值且非 str"；None/0/"" 原样留着，别改条目取舍。
            entries.append({
                "rank": idx + 1,
                "title": _t if (not _t or isinstance(_t, str)) else str(_t),
                "url": it.get("url", ""),
                "hot": "",
            })
        if entries:
            result.append({"platform": plat, "name": plat, "items": entries})
    print("[热榜] newsnow 快照: %d 个平台" % len(result))
    return result

def _load_history():
    """加载 72 小时文章历史（支持分块格式）"""
    global _rss_history
    try:
        _rss_history = _load_history_chunked()
        if _rss_history:
            print("[历史] 加载文章历史: %d 篇" % len(_rss_history))
    except Exception as e:
        print("[历史] 加载失败: %s" % e, file=sys.stderr)
        _rss_history = {}


def _save_history_chunked(max_size=40 * 1024 * 1024):
    """分块保存文章历史。

    始终走分块格式（含小历史）：跨构建持久化由 CI 的 actions/cache 承载 chunk+index，
    若小历史改写主文件而留下上一代 index，_load_history_chunked 会优先信 index 而忽略
    刚写好的主文件。
    """
    data = json.dumps(_rss_history, ensure_ascii=False, separators=(",", ":"))
    total_bytes = len(data.encode("utf-8"))
    n_chunks = max(1, (total_bytes + max_size - 1) // max_size)

    buckets = [{} for _ in range(n_chunks)]
    for link, record in _rss_history.items():
        bucket_idx = int(hashlib.md5(link.encode()).hexdigest()[:8], 16) % n_chunks
        buckets[bucket_idx][link] = record

    base_dir = os.path.dirname(RSS_HISTORY_FILE) or "."
    for i, bucket in enumerate(buckets):
        _atomic_write_json(os.path.join(base_dir, "rss_history_%d.json" % i),
                           bucket, ensure_ascii=False)

    _atomic_write_json(os.path.join(base_dir, "rss_history_index.json"),
                       {"chunks": n_chunks, "total": len(_rss_history)},
                       ensure_ascii=False)

    import glob
    for old_file in glob.glob(os.path.join(base_dir, "rss_history_*.json")):
        try:
            num = int(os.path.basename(old_file).replace("rss_history_", "").replace(".json", ""))
            if num >= n_chunks:
                os.remove(old_file)
                print("[历史] 清理旧分块: %s" % old_file)
        except ValueError:
            pass

    print("[历史] 分块保存: %d 篇 → %d 个文件" % (len(_rss_history), n_chunks))


def _load_history_chunked(hist_file=None, index_file=None):
    """加载分块的文章历史，向后兼容旧格式。"""
    hist_file = hist_file or RSS_HISTORY_FILE
    base_dir = os.path.dirname(hist_file) or "."
    index_file = index_file or os.path.join(base_dir, "rss_history_index.json")

    if os.path.exists(index_file):
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                index = json.load(f)
            merged = {}
            for i in range(index.get("chunks", 0)):
                fname = os.path.join(base_dir, "rss_history_%d.json" % i)
                if os.path.exists(fname):
                    with open(fname, "r", encoding="utf-8") as f:
                        merged.update(json.load(f))
            # index 存在即以它为准：回落到主文件会读出上一代被冻结的旧历史
            # （rss_history.json 已不参与跨构建持久化）。数量对不上说明只还原了半套
            # 分块，按无历史处理让守卫逼出全量，否则缺的那部分会被当成正常数据继续跳源。
            expected = int(index.get("total", -1))
            if len(merged) != expected:
                print("[历史] 索引声明 %d 篇、实读 %d 篇（分块缺失或缓存半套），按无历史处理"
                      % (expected, len(merged)), file=sys.stderr)
                return {}
            return merged
        except Exception:
            pass

    if os.path.exists(hist_file):
        try:
            with open(hist_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_history():
    """保存 72 小时文章历史（分块防超 40MB）"""
    try:
        _save_history_chunked()
        print("[历史] 保存文章历史: %d 篇" % len(_rss_history))
    except Exception as e:
        print("[历史] 保存失败: %s" % e, file=sys.stderr)


# ── 留存出口闸门（设计见 docs/superpowers/specs/2026-09-20-rss-72h-retention-design.md）──
# 为什么要有这道闸门：上面那套 72h 裁剪只遍历 _rss_history，而当次抓取的条目在
# src_map[key]["items"] = list(_fetched) 处原样进输出、从未与 cutoff 比较。
# 线上实测产物 20,904 条里 59% 超过 72h（中位 137h、p99 4 年），主体就是上游 feed
# 自己挂着的老文章 —— 它们走的是「当次抓取」这条旁路，调历史裁剪参数治不到。
RETENTION_BASE_H = RSS_HISTORY_HOURS      # 基线窗口 = 用户可见契约 72h
RETENTION_MAX_WINDOW_H = 168              # 慢源最多放宽到 7 天，再长就不算「近期」了
RETENTION_COEF = 2.0                      # 窗口 = 系数 × 该源中位发布间隔（实测系数不敏感）
RETENTION_FLOOR = 3                       # 每源最少留几条：没有它实测 286/938 源会变空
RETENTION_FLOOR_MAX_H = RETENTION_MAX_WINDOW_H
# ↑ 保底自己的龄限。线上实测：不设这道限时「保底」一个人放进产物 1,012 条 >168h 的内容
#   （最老 41,791h ≈ 4.7 年的 CNN 旧文，散在 426 个源上）—— 那和被判死的「无日期回退成 now」
#   是同一个 bug 换了件衣服。保底的目的是别让卡片整块消失，不是给停更源发永生签证。
#   代价实测可接受：钳在 168h 后出厂 7,075 条，246 个源颗粒无收，但覆盖率统计
#   (sources_with_data / per_source) 全部取闸门**之前**的抓取阶段数字，门禁不会因此变红；
#   那 246 个源是停更源，归信源清单健康度那条线处理，不该由留存策略替它们遮掩。
RETENTION_MAX_ITEMS = 120                 # 每源条数上限：防高频源把全站可见性吃光
RETENTION_MIN_SAMPLES = 3                 # 样本不足 3 条不放宽 —— 两条间隔不构成「本就来得慢」
RETENTION_EPOCH_YEAR = 1971               # 0001-01-01 这类占位值不是发布时间
RETENTION_OVER_NAME_CAP = 5               # 超龄条目点名上限：破口成百时不许把构建日志刷爆（计数仍全量）

# 缓存侧只有一条体积约束：**72h 时间窗**。2026-09-20 曾加过一道"每源上限 400 条"，
# 当天就撤销，三条理由全部来自生产实测（台账 §13）：
#   1) 时间窗本身就把体积框住了 —— 上限上线前的 93 场历史斜率是 **-46.9 条/场**（在往下走）。
#      我先前看到的 +230/场 只是窗口填装期的暂态，把暂态当成趋势是错的。
#   2) 条数上限并不省体积：上线后 10 场斜率反而 **+65.7 条/场**（被腾空的名额又填满），
#      同期却累计淘汰 2,231 条窗内条目 —— 代价是持续的，省下的是暂时的。
#   3) 它切的几乎全是窗内内容：首落地那场淘汰 1,547 条，同场真过期的只有 21 条。
# 换留下来的"每源深度哨兵"也已于 2026-09-21 退役：它扫全库只报三项，其中"最老龄期"与
# 裁剪共用一只表后恒等于把 cutoff 念一遍。体积侧从此只剩一条账 —— 真过期裁掉多少。
_LAST_HISTORY_ACCOUNT = {}                # 本场历史账：只剩 expired 一条，构建日志消费


def _retention_ref_dt(item, offsets=None, source_key=None):
    """闸门判龄用的时间基准（北京时间 aware）；两条时间线索都不可得时返回 None。

    一律回到 _rss_history 里那份**原始** pub_date 再套声明偏移：出口处那份可能已被
    _accumulate_history 第二段平移过，在这里再套一次就是二次校正（条目凭空挪 ±偏移）。
    pub_date 缺失、是纪元占位值、或晚于 first_seen 超容忍（物理上不可能，已被证伪）时
    改用 first_seen —— 那是我方记录的真实收录时刻。**不再回退成 now**，那正是永生条目的来源。

    降级键（date_fallback）不需要单独认：它只在历史条目缺 pub_date 时才写，所以
    「历史里没有 pub_date」就等于「出口那份是降级键」，此时基准直接取 first_seen（不套偏移）。

    source_key 必传兜底：当次抓取的 item 字典里没有 source_key（只有历史条目有），
    少了它就取不到该源声明的 pub_date_offset_min —— 合并阶段是用 src["key"] 现拼的。
    """
    _raw = _rss_history.get(item.get("link", "")) or {}
    in_hist = bool(_raw)
    fs = _parse_hist_dt(_raw.get("first_seen") or item.get("first_seen"))
    # 原始 pub_date 只认两个来源：历史里那份（一定是原始值），或压根没入过历史的本轮抓取值。
    # 绝不能「历史没有就退回 item 的」：第二轮会给历史条目写上 first_seen 派生的降级键，
    # 那串不是发布时间，再套一次源偏移就凭空挪 ±偏移（对抗审查 P1-3 实测 68h → 76h）。
    raw_pd = _raw.get("pub_date") if in_hist else item.get("pub_date", "")
    if not raw_pd:
        return fs
    probe = {"pub_date": raw_pd,
             "source_key": item.get("source_key") or _raw.get("source_key") or source_key}
    d = _effective_pub_dt(probe, offsets)
    if d is None or d.year <= RETENTION_EPOCH_YEAR:
        return fs
    # 证伪判据用**我方自己的** first_seen：已入历史的条目必须与本轮实际抓取时刻比对 ——
    # 降级条目的 first_seen 恰是被评估的那个值，d - fs 恒 <= 0，"还没抓到就已发布"的假日期
    # 反而永远判不倒（实测 d - first_seen = 48h 也放行）。
    fs_ref = _parse_hist_dt(_raw.get("first_seen")) if in_hist else fs
    if fs_ref is not None and (d - fs_ref) > datetime.timedelta(minutes=FUTURE_DATE_TOLERANCE_MIN):
        return fs_ref
    return d


def _retention_age_h(item, offsets=None, now_bj=None, source_key=None):
    """条目的龄（小时）。返回 None 表示判不了龄 —— 闸门按丢弃处理。"""
    d = _retention_ref_dt(item, offsets, source_key)
    if d is None:
        return None
    return ((now_bj or _now_bj()) - d).total_seconds() / 3600.0


def _source_window_h(items, offsets=None, source_key=None):
    """该源的保留窗口（小时）：min(168, max(72, 系数 × 中位发布间隔))，样本不足不放宽。

    放宽是给低频源（周更博客、播客一期 30 条）的：一刀切 72h 会把它们整源清空。
    中位口径与 _detect_pub_date_anomalies 一致（取排序后中位偏上那个）。
    """
    ds = []
    for it in items:
        d = _retention_ref_dt(it, offsets, source_key)
        if d is not None:
            ds.append(d)
    if len(ds) < RETENTION_MIN_SAMPLES:
        return float(RETENTION_BASE_H)
    ds.sort()
    gaps = sorted((ds[i + 1] - ds[i]).total_seconds() / 3600.0 for i in range(len(ds) - 1))
    med = gaps[len(gaps) // 2]
    return min(float(RETENTION_MAX_WINDOW_H),
               max(float(RETENTION_BASE_H), RETENTION_COEF * med))


def _apply_retention(sources, offsets=None, now_bj=None):
    """出口闸门：窗口 → 每源保底（受硬上限约束）→ 每源上限。返回 (过滤后的 sources, 统计)。

    顺序固定为「窗口 → 保底 → 上限」，且**只删条目**：过滤后按原始下标回填，
    因为 write_data_chunks 按顺序取首屏前 CHUNK0_SIZE 条，打乱顺序就是改首页。
    """
    now_bj = now_bj or _now_bj()
    before = after = undatable = widened = floored = capped = 0
    b_le72 = b_mid = b_over = 0
    over_names = []             # 超龄条目的身份 + 两把尺的读数，见 RETENTION_OVER_NAME_CAP 与 A2 判据
    emptied = []
    emptied_detail = []       # 与 emptied 同批：名单 + 各自最后一条的龄期 + 它拿到的窗口
    out = []
    for src in sources:
        items = src.get("items", [])
        before += len(items)
        if not items:
            out.append(src)
            continue
        window = _source_window_h(items, offsets, src.get("key"))
        if window > RETENTION_BASE_H:
            widened += 1
        dated = []          # (原始下标, 龄, item)
        for idx, it in enumerate(items):
            age = _retention_age_h(it, offsets, now_bj, src.get("key"))
            if age is None:
                undatable += 1
            else:
                dated.append((idx, age, it))
        sel = [t for t in dated if t[1] <= window]
        # 保底只从「可判龄且没超硬上限」的条目里取最新的，凑不满就算了：
        # 拿无日期条目填空等于留后门，拿 7 天前的旧文填空等于把保底改成永生通道。
        pad = [t for t in dated if t[1] <= RETENTION_FLOOR_MAX_H]
        if len(sel) < RETENTION_FLOOR and len(pad) > len(sel):
            chosen = {t[0] for t in sel}
            for t in sorted(pad, key=lambda x: x[1]):
                if len(sel) >= RETENTION_FLOOR:
                    break
                if t[0] not in chosen:
                    sel.append(t)
                    chosen.add(t[0])
            floored += 1
        if len(sel) > RETENTION_MAX_ITEMS:
            sel = sorted(sel, key=lambda x: x[1])[:RETENTION_MAX_ITEMS]
            capped += 1
        retained = sorted(sel, key=lambda x: x[0])
        kept = [t[2] for t in retained]
        # 分项按**出厂那串 pub_date**独立算，不复用上面的判定龄：
        # 判龄走原始 pub_date + 偏移，降级条目（date_fallback）出厂的是收录时刻，两者可以差一个
        # 偏移量 —— 复用判定龄会得到一个"和用户看到的不一样"的分布（实测显示 20h 的条目被归进
        # 72-168h 桶）。独立算既对齐外显，也让 ">168h 应为 0" 这条不变量真的可证伪。
        for _t in retained:
            _disp = _parse_hist_dt(_t[2].get("pub_date"))
            if _disp is None:
                continue
            _dage = (now_bj - _disp).total_seconds() / 3600.0
            if _dage <= RETENTION_BASE_H:
                b_le72 += 1
            elif _dage <= RETENTION_MAX_WINDOW_H:
                b_mid += 1
            else:
                b_over += 1
                if len(over_names) < RETENTION_OVER_NAME_CAP:
                    # 两把尺各读多少都要留下：出厂分项按出厂那串算，闸门判龄走 _retention_ref_dt，
                    # 一条超龄条目能出厂就说明这两者对不上 —— 对上在哪断的，才是 ② 要改的地方。
                    _ref = _retention_ref_dt(_t[2], offsets, src.get("key"))
                    over_names.append({
                        "link": _t[2].get("link") or "?",
                        "src": src.get("key") or "?",
                        "ship": _t[2].get("pub_date") or "",
                        "ship_age_h": round(_dage, 1),
                        "ref_age_h": None if _ref is None
                        else round((now_bj - _ref).total_seconds() / 3600.0, 1),
                        "dfb": bool(_t[2].get("date_fallback")),
                    })
        if items and not kept:
            emptied.append(src.get("name") or src.get("key", "?"))
            # 名单要带"为什么"：只有名字的话，下一句必然是"这个源是停更还是被判龄砍了"，
            # 而那只能再拿手工探针回答（10-05 定案 12 个 megaphone 源就是这么来的）。
            # newest_age_h 与 window_h 并排放，两个读数一对照就能看出是"上游没货"
            # （最新一条已远超窗）还是"窗口给小了"（最新一条只差一点点）。
            _ages = [t[1] for t in dated]
            emptied_detail.append({
                "key": src.get("key") or "?",
                "name": src.get("name") or "?",
                "n_items": len(items),                    # 闸门**前**抓到几条
                "newest_age_h": None if not _ages else round(min(_ages), 1),
                "window_h": round(window, 1),
                "undatable": len(items) - len(dated),     # 判不了龄因而直接丢弃的那部分
            })
        src["items"] = kept
        after += len(kept)
        out.append(src)
    stats = {"before": before, "after": after, "dropped": before - after,
             "undatable": undatable, "widened": widened,
             "floored": floored, "capped": capped, "emptied": emptied,
             "emptied_detail": emptied_detail,
             "sources_after": sum(1 for s in out if s.get("items")),
             "le72": b_le72, "mid": b_mid, "over": b_over, "over_names": over_names}
    print("[留存] 出口闸门：进 %d 条 → 留 %d 条（丢弃 %d，其中判不了龄 %d）"
          "｜窗口放宽 %d 源｜保底 %d 源｜封顶 %d 源" % (
              before, after, stats["dropped"], undatable, widened, floored, capped))
    # 分项直接进构建日志：验收要的是「出厂内容里没有超硬上限的」，
    # 这个数字不能只存在于本地一次性脚本里，否则下一场构建就没人知道了。
    print("[留存] 出厂龄期分项：≤%dh %d 条｜%d-%dh %d 条｜>%dh %d 条" % (
        RETENTION_BASE_H, b_le72, RETENTION_BASE_H, RETENTION_MAX_WINDOW_H, b_mid,
        RETENTION_MAX_WINDOW_H, b_over))
    if b_over:
        # 点名先于 warning：只报一个数字的告警查不动 —— 一天里往往就 0~1 条，等它再冒出来时
        # 产物里已经找不到那条了（10-03 实测：04:11 场 >168h 1 条，16:4x 现取 rss-data-6.js 已 0 命中）。
        for _i, _n in enumerate(over_names, 1):
            _r = "-" if _n["ref_age_h"] is None else "%.1f" % _n["ref_age_h"]
            print("[留存] 超龄点名 %d/%d: %s｜源 %s｜出厂串 %s｜出厂龄 %.1fh｜判龄基准龄 %sh｜dfb=%s" % (
                _i, b_over, _n["link"], _n["src"], _n["ship"], _n["ship_age_h"], _r, _n["dfb"]))
        # 不变量破了要出声。沉默的 0 和沉默的非 0 一样没人看见 —— 本仓库为这类"静默退化"
        # 付过两次代价（每日洞察连败数天、72h 存档断两天）。
        print("::warning title=RSS 留存超硬上限::出厂内容里有 %d 条龄期超过 %dh（7 天硬上限被绕过）"
              % (b_over, RETENTION_MAX_WINDOW_H))
    if emptied:
        print("[留存] 无内容可出厂的源 %d 个（停更或全源判不了龄，交信源清单健康度跟进）: %s" % (
            len(emptied), ", ".join(emptied[:10]) + (" ..." if len(emptied) > 10 else "")),
            file=sys.stderr)
    global _LAST_RETENTION_STATS
    _LAST_RETENTION_STATS = dict(stats, emptied=list(emptied))
    return out, stats


def _accumulate_history(sources_with_items):
    """将新抓取的文章合并到 72 小时历史，裁剪过期内容，返回重组后的 sources_with_items

    返回值已过一道留存出口闸门（_apply_retention）：抓取侧与历史侧同一口径，超窗即丢。
    """
    now_bj = _now_bj()
    cutoff = now_bj.replace(tzinfo=None) - datetime.timedelta(hours=RSS_HISTORY_HOURS)

    # ── pub_date 时区校正（声明式）与同源系统性倒挂发现 ──
    # 校正量来自源配置的 pub_date_offset_min（见 _load_pub_date_offsets 里的取舍说明）；
    # 检测器只报告不落库，避免「猜出来的偏移」静默改写日期。
    # 后续合并 / 裁剪 / 审计 / 输出四处统一用 _effective_pub_dt，避免多套口径。
    _pd_offsets = _load_pub_date_offsets()
    if _pd_offsets:
        print("[pub_date] 已声明时区校正 %d 个源: %s" % (
            len(_pd_offsets),
            ", ".join("%s %+dmin" % (k, v) for k, v in list(_pd_offsets.items())[:8])))
    _anomalies = _detect_pub_date_anomalies(_rss_history, _pd_offsets)
    for _a in _anomalies:
        if _a["kind"] == "tz":
            print("[pub_date] 疑似时区误标 %s: n=%d 倒挂=%.0f%% 中位=%+.0fmin "
                  "→ 建议 pub_date_offset_min=%d（需人工确认后写入 rss_sources.json）" % (
                      _a["key"], _a["n"], _a["inverted_ratio"] * 100,
                      _a["median_delta_min"], _a["suggest_min"]), file=sys.stderr)
        else:
            print("[pub_date] 日期异常（非时区）%s: n=%d 倒挂=%.0f%% 中位=%+.0fmin "
                  "→ 需要前移 %.0fh，远超真实时区范围，不作时区校正" % (
                      _a["key"], _a["n"], _a["inverted_ratio"] * 100,
                      _a["median_delta_min"], _a["need_min"] / 60.0), file=sys.stderr)

    # 合并新文章（按 link 去重，新数据覆盖旧数据）
    new_count = 0
    genuinely_new = 0  # 真正新增的链接（非覆盖）
    for src in sources_with_items:
        for it in src.get("items", []):
            link = it.get("link", "")
            if not link:
                continue
            pd_str = it.get("pub_date", "")
            # 解析日期：优先 pub_date（按源声明的偏移做时区校正），无日期或不可解析时
            # 使用 first_seen（首次抓取时间），仅当 first_seen 也不可用时才回退当前时间。
            pd_bj = now_bj.replace(tzinfo=None)  # 默认当前时间
            _eff = _effective_pub_dt({"pub_date": pd_str, "source_key": src["key"]}, _pd_offsets)
            if _eff is not None:
                pd_bj = _eff.replace(tzinfo=None)
            else:
                # 无 pub_date 时使用 first_seen 作为时间基准
                fs_str = _rss_history.get(link, {}).get("first_seen", "")
                if fs_str:
                    try:
                        fs = datetime.datetime.fromisoformat(fs_str)
                        pd_bj = fs.replace(tzinfo=None) if fs.tzinfo else fs
                    except ValueError:
                        pass  # first_seen 也解析失败，保持当前时间
            if pd_bj < cutoff:
                # 已过期：若历史里已存在同链接条目（典型是「pub_date 为空、靠 first_seen
                # 续命」的陈旧条目），用本轮拿到的真实日期覆盖它，让紧随其后的裁剪循环
                # 按真实日期清掉 —— 否则它会被降级键渲染成「收录 X 前」长期留在页面上。
                _old = _rss_history.get(link)
                if _old is not None:
                    _old["pub_date"] = pd_str
                continue  # 过期文章跳过
            if link not in _rss_history:
                genuinely_new += 1
            _rss_history[link] = {
                "link": link,
                "source": src["name"], "source_key": src["key"],
                "cat": src.get("cat", "other"), "color": src.get("color", "#6366f1"),
                "title": it.get("title", ""), "title_zh": it.get("title_zh", ""),
                "summary": it.get("summary", ""), "summary_zh": it.get("summary_zh", ""),
                "full_content": it.get("full_content", ""),
                "image": it.get("image", ""),
                "media_url": it.get("media_url", ""),
                "media_type": it.get("media_type", ""),
                "pub_date": pd_str,
                "first_seen": _rss_history.get(link, {}).get("first_seen", now_bj.replace(tzinfo=None).isoformat()),
            }
            new_count += 1

    # 裁剪超过 72 小时的旧文章
    before = len(_rss_history)
    expired = []
    for link, item in _rss_history.items():
        # 参照日必须是**真取到的**时刻，不能拿 now 兜底：兜底等于「判不了龄就永生」。
        # 这条与出口闸门 _retention_ref_dt 同一口径（那边判不了龄返回 None 直接丢）。
        # 2026-09-20 对抗审查实测：线上确实存在 pub_date 为空且 first_seen 也拿不到值的条目
        # （本地 18,037 条历史里 68 条 pub_date 为空），旧写法对它们永久跳过。
        # 判龄与出口闸门共用一只表。旧写法只看 pub_date（含时区校正），于是
        # 「pub_date 被证伪（晚于 first_seen 超容差）」的条目在裁剪眼里还年轻、在闸门眼里
        # 已经 140h —— 缓存在替假日期的死重续命（真实重放实测保留最老 140.1h）。
        # 两只时间线索都取不到 ⇒ 按过期删除：合并阶段会用本轮 first_seen 重新入库，
        # 寿命重新从真实收录时刻起算 72h —— 有界，而不是永生。
        _ref = _retention_ref_dt(item, _pd_offsets, item.get("source_key"))
        if _ref is None or _ref.replace(tzinfo=None) < cutoff:
            expired.append(link)
    for link in expired:
        del _rss_history[link]

    # 过期裁剪的净效果当场结清成一条独立的账。体积侧已不存在第二条删路
    # （每源条数上限 09-20 撤销、深度哨兵 09-21 退役，理由见台账 §18），所以这个数就是"真过期被丢掉多少"，
    # 不再需要跟淘汰数做减法。
    # 口径必须是"删掉的条数"而不是历史首尾差：后者在生产全天 12 场实测全是负数
    # （-264/-201/.../-235），因为每场新抓进来的比删掉的多 —— 那本账从来没存在过。
    pruned_expired = before - len(_rss_history)
    _LAST_HISTORY_ACCOUNT["expired"] = pruned_expired
    # 体积侧到这里就没有第二条账了：条数上限（09-20）与深度哨兵（09-21）都已退役，
    # 真过期多少由上面那句「裁剪 N 篇过期」播报，日志只带 history_expired 一个字段。
    # 退役理由是量出来的（台账 §18）：真实 18,037 条快照上裁剪判龄中位 68ms，
    # 而哨兵还要再扫一遍全库（68.5ms）报三项 —— 其中「保留最老龄期」与这里共用一只表，
    # 存活条目按构造满足 ref >= cutoff，报出来等于把 cutoff 念一遍。

    # 按源重组，更新相对时间
    # 构建 source_key -> url 映射（用于识别 BestBlogs 源）
    _src_url_map = {s["key"]: s.get("url", "") for s in sources_with_items}
    # 补充：从 RSS_SOURCES 查找缺失的 URL（sources_with_items 可能不含 url）
    for _rs in RSS_SOURCES:
        if _rs["key"] not in _src_url_map or not _src_url_map[_rs["key"]]:
            _src_url_map[_rs["key"]] = _rs.get("url", "")
    src_map = {}
    # Fix 2: 保留当次抓取结果，避免被空壳替换
    # 先用当次抓取的 items 初始化 src_map，后续历史回填时按 link 去重补充
    _fetch_items_by_key = {}
    for src in sources_with_items:
        _items = src.get("items", [])
        if _items:
            _fetch_items_by_key[src["key"]] = {it.get("link", ""): it for it in _items if it.get("link")}
    _n_capture_filled = 0
    for src in sources_with_items:
        _bb = 'bestblogs.dev' in (_src_url_map.get(src["key"]) or '')
        _fetched = src.get("items", [])
        # 无日期的当次条目在这里补收录时刻：下面 list(_fetched) 就是出厂的那份
        _n_capture_filled += sum(1 for _it in _fetched if _fill_undated_from_capture(_it))
        src_map[src["key"]] = {
            "key": src["key"], "name": src["name"],
            "cat": src.get("cat", "other"), "color": src.get("color", "#6366f1"),
            "tier": src.get("tier", 3), "items": list(_fetched),
        }
        if _bb:
            src_map[src["key"]]["bb"] = True
    # --- 信源级 pub_date 质量自动审查 ---
    # 第一遍：按源统计 pub_date 异常比例
    #   异常模式 A: pub_date ≈ first_seen (|delta|<10min) → pub_date 大概率是抓取时间
    #   异常模式 B: pub_date > first_seen (delta<-10min) → 日期倒挂
    #   注：公众号源已不再硬编码为不可信——实测 wechat2rss 的 pub_date 均为真实发布时间，
    #       统一由异常率阈值判定（与其余类目一视同仁）
    _src_date_stats = {}  # source_key -> {total, anomaly_a, anomaly_b}
    for item in _rss_history.values():
        sk = item["source_key"]
        if sk not in src_map:
            continue
        if sk not in _src_date_stats:
            _src_date_stats[sk] = {"total": 0, "anomaly_a": 0, "anomaly_b": 0}
        _src_date_stats[sk]["total"] += 1
        # 审计必须跑在**校正后**的发布时间上：否则一个纯时区解析问题会被判成
        # 「日期不可信」，进而把该源的关键词按 0.3 倍降权（内容质量问题），
        # 并把卡片改成显示绝对日期 —— 两处都是误伤。
        pd = _effective_pub_dt(item, _pd_offsets)
        fs = _parse_hist_dt(item.get("first_seen"))
        if pd is not None and fs is not None:
            try:
                delta_sec = (fs - pd).total_seconds()
                if abs(delta_sec) < 600:      # 模式 A: < 10 分钟
                    _src_date_stats[sk]["anomaly_a"] += 1
                elif delta_sec < -600:         # 模式 B: 日期倒挂
                    _src_date_stats[sk]["anomaly_b"] += 1
            except (ValueError, TypeError):
                pass

    # 判定不可信源：异常率 > 30%
    _unreliable_srcs = set()
    BAD_DATE_ANOMALY_THRESHOLD = 0.3
    for sk, stats in _src_date_stats.items():
        if stats["total"] == 0:
            continue
        anomaly_ratio = (stats["anomaly_a"] + stats["anomaly_b"]) / stats["total"]
        if anomaly_ratio > BAD_DATE_ANOMALY_THRESHOLD:
            _unreliable_srcs.add(sk)  # 自动检测为不可信
    if _unreliable_srcs:
        names = [src_map[sk]["name"] for sk in _unreliable_srcs if sk in src_map][:10]
        print("[bad_date] 不可信信源 %d 个: %s%s" % (
            len(_unreliable_srcs), ", ".join(names),
            " ..." if len(_unreliable_srcs) > 10 else ""))
    # D4：暴露给趋势快照与关键词降权消费
    global _LAST_UNRELIABLE_SRCS
    _LAST_UNRELIABLE_SRCS = set(_unreliable_srcs)

    # 模式 C：同日期伪造检测（知乎日报/愆伏/梅之夏等）
    _uniform_flagged = _detect_uniform_dates(
        [_rss_history[k] for k in _rss_history],
        min_items=5, threshold=0.8
    )
    if _uniform_flagged:
        _unreliable_srcs |= _uniform_flagged
        _LAST_UNRELIABLE_SRCS = set(_unreliable_srcs)
        _names = [src_map[sk]["name"] for sk in _uniform_flagged if sk in src_map][:5]
        print("[bad_date-C] 同日期伪造源 %d 个: %s" % (
            len(_uniform_flagged), ", ".join(_names)))
        # 立即标记条目
        for item in _rss_history.values():
            if item.get("source_key") in _uniform_flagged:
                item["bad_date"] = True

    # 第二遍：时区校正 → 未来日期处置 → 标记 bad_date → 重组
    # 判伪条目的回拉位移先算好（按「源 + 抓取批次」分组，保序不折叠 —— 见 _plan_falsify_shifts）
    _cutoff_aware = cutoff.replace(tzinfo=_BJ_TZ)
    _falsify_shifts = _plan_falsify_shifts(
        _rss_history, _pd_offsets, src_keys=set(src_map.keys()))
    _n_tz_fixed = 0
    _n_falsified = 0
    _n_shifted = 0
    for item in _rss_history.values():
        sk = item["source_key"]
        if sk in src_map:
            entry = dict(item)   # 副本：下面的降级改写不得污染历史（历史只存原始 pub_date）
            entry["bad_date"] = sk in _unreliable_srcs
            # (1) 源级时区校正：把上游标错时区的时间还原成真实发布时间。
            #     这是根因层修复 —— 校正后条目不再「出生于未来」，
            #     也就不需要前端钳制介入（钳制会把多条折叠成同一时刻 → 整块钉首屏）。
            _off = _pd_offsets.get(sk)
            if _off and entry.get("pub_date"):
                _shifted = _shift_iso_minutes(entry["pub_date"], _off)
                if _shifted != entry["pub_date"]:
                    entry["pub_date"] = _shifted
                    _n_tz_fixed += 1
            # (2) 取不到 pub_date 时给一个可排序的降级键（= first_seen，我方首次收录该链接的真实时刻）。
            #     没有它，条目在默认排序里只能沉底 → 用户永远看不到（2026-09-14：201 条因此不可见）。
            _fb = _fallback_date_iso(entry)
            if not _fb and _pub_date_falsified(entry, _pd_offsets):
                # (3) 有 pub_date，但晚于 first_seen 超过容忍阈值 —— 我们「在文章发布前抓到了它」，
                #     物理不可能 ⇒ 该日期已被证伪，不能拿来排序。改用它自己的 first_seen：
                #     这是一个我方记录的真实时刻，既诚实（前端显示「收录」）又能参与排序。
                #     不选「钳到 now」：那会让多条条目获得同一时刻，正是首屏同刻扎堆的成因。
                #     也不能整批都写同一个 first_seen（first_seen 是批次戳，实测 8411 条只有
                #     122 个取值）—— 故按 _falsify_shifts 保序回拉，最新的一条锚在 first_seen，
                #     组内相对先后保留，且下限钳在 72h cutoff 上。
                _shift = _falsify_shifts.get(item.get("link"), 0)
                _fb = _shift_iso_minutes(_first_seen_iso(entry), _shift)
                _fbd = _parse_hist_dt(_fb)
                if _fbd is None or _fbd < _cutoff_aware:
                    _fb = _cutoff_aware.replace(microsecond=0).isoformat()
                if _fb:
                    _n_falsified += 1
                    if _shift:
                        _n_shifted += 1
            if _fb:
                entry["pub_date"] = _fb
                entry["date_fallback"] = True
            # 不再烘 time_str：相对时间是"看"的函数，不是事实（口径见 lib/rel_time.js）
            # Fix 2: 去重 — 跳过当次抓取已存在的同链接条目
            _link = entry.get("link", "")
            if _link and sk in _fetch_items_by_key and _link in _fetch_items_by_key[sk]:
                continue
            src_map[sk]["items"].append(entry)

    if _n_tz_fixed or _n_falsified:
        print("[pub_date] 口径修正：时区校正 %d 条 / 未来日期改用收录时刻 %d 条"
              "（其中 %d 条按批次保序回拉，未折叠为同一时刻）" % (
                  _n_tz_fixed, _n_falsified, _n_shifted))

    result = list(src_map.values())
    if _n_capture_filled:
        print("[出口] 无发布日期的当次条目 %d 条改用收录时刻（前端标「收录」，不冒充发布时间）"
              % _n_capture_filled)
    print("[历史] 合并 %d 篇新文（其中 %d 篇为新增链接，%d 篇为覆盖更新），裁剪 %d 篇过期，保留 %d 篇（%d 小时窗口）" % (
        new_count, genuinely_new, new_count - genuinely_new,
        pruned_expired, len(_rss_history), RSS_HISTORY_HOURS))
    # 出口闸门：窗口 → 保底 → 上限。放在 return 之前，本函数的三个消费者
    # （_save_api_snapshot / build_html / write_data_chunks）共用同一个返回值，
    # 所以**构建期产物**不会出现「页面裁了 API 没裁」的双标口径。
    # 注意边界：运行时 api/rss.js 的 ?batch / ?source 自己发 HTTP 抓取，不吃这些产物，
    # 那条出口要单独过闸（对抗审查 P0-2），别把这句注释当成它已经管住了全站。
    result, _ret_stats = _apply_retention(result, _pd_offsets, now_bj)
    total = _ret_stats["after"]
    return result, total


def _save_api_snapshot(sources_with_items):
    """生成 API 快照 JSON（分块），供 /api/rss 直接返回，避免实时抓取丢失历史累积数据。
    按 ~80MB 上限拆分为 rss_api_snapshot.json + rss_api_snapshot_1.json ...，
    避免超过 GitHub 100MB 单文件限制。"""
    MAX_SNAPSHOT_BYTES = 80 * 1024 * 1024

    # ── 快照级去重：清理历史累积中的残留重复 ──
    # 历史数据可能包含修复前抓取的重复条目（如 V2EX #replyN）
    for src in sources_with_items:
        src["items"] = _dedup_source_items(src.get("items", []), src.get("key", ""))

    snapshot_sources = []
    for src in sources_with_items:
        items = []
        for it in src.get("items", []):
            item = {
                "t": it.get("title_zh", "") or it.get("title", ""),
                "u": _strip_oss_signature(it.get("link", "#")),
                # hnrss 的模板行（Article URL/Comments URL/Points/# Comments）在卡片上是纯噪声：
                # 解析入口包过一层还不够 —— summary_zh 存在 translations.json 里、跨场复用且
                # 不会被重新解析，所以快照出口要再过一次同一把刀（判据：
                # tests/rss_history/test_hn_summary_rewrite.py::test_snapshot_exit_does_not_re_expose_meta_lines）。
                # _rewrite_hn_summary 幂等，已重写过的一行不会被吃第二遍。
                # _strip_glued_url（Task 11）同因接在这里：老缓存里的 summary_zh 是**剥之前**的
                # "URL+正文"粘连形状，只在解析入口剥捞不到它们；这条也幂等（判据
                # test_snapshot_exit_strips_glued_url_from_cached_translation / _is_idempotent）。
                "s": _strip_oss_signature(_strip_glued_url(
                    _rewrite_hn_summary(
                        it.get("summary_zh", "") or it.get("summary", ""), it.get("link", "")))),
                "d": it.get("pub_date", ""),
            }
            if it.get("bad_date"):
                item["bad_date"] = True
            if it.get("date_fallback"):
                item["date_fallback"] = True
            _tags = it.get("tags")
            if _tags:
                item["tags"] = _tags
            fc = it.get("full_content", "")
            if fc:
                # _cap_body 而不是裸切片：`fc[:50000]` 能正好落在 `<img src="htt` 中间，
                # 出厂一条静默变形的半截标签（api/rss.js 直接 read 这份快照）。
                item["fc"] = _strip_oss_signature(_cap_body(fc))
            img = it.get("image", "")
            if img:
                item["img"] = _strip_oss_signature(img)
            mu = it.get("media_url", "")
            if mu:
                item["mu"] = _strip_oss_signature(mu)
                item["mt"] = it.get("media_type", "")
            items.append(item)
        snapshot_sources.append({
            "key": src["key"], "name": src["name"],
            "cat": src.get("cat", "other"), "color": src.get("color", "#6366f1"),
            "tier": src.get("tier", 3), "items": items,
        })

    # 估算每源体积，按体积均匀分桶
    src_sizes = []
    for s in snapshot_sources:
        try:
            sz = len(json.dumps(s, ensure_ascii=False).encode("utf-8"))
        except Exception:
            sz = 1024
        src_sizes.append(sz)
    total_bytes = sum(src_sizes)
    n_chunks = max(1, (total_bytes + MAX_SNAPSHOT_BYTES - 1) // MAX_SNAPSHOT_BYTES)

    buckets = [[] for _ in range(n_chunks)]
    bucket_sizes = [0] * n_chunks
    for i, (s, sz) in enumerate(zip(snapshot_sources, src_sizes)):
        min_idx = bucket_sizes.index(min(bucket_sizes))
        buckets[min_idx].append(s)
        bucket_sizes[min_idx] += sz

    total_items = sum(len(s["items"]) for s in snapshot_sources)
    try:
        for idx, bucket in enumerate(buckets):
            fname = "rss_api_snapshot.json" if idx == 0 else "rss_api_snapshot_%d.json" % idx
            chunk_data = {"t": _now_bj().isoformat(), "sources": bucket}
            if idx == 0:
                chunk_data["_total_chunks"] = n_chunks
            _atomic_write_json(fname, chunk_data, ensure_ascii=False)
            size_mb = bucket_sizes[idx] / 1024 / 1024
            print("[快照] %s: %d 源 %.1f MB" % (fname, len(bucket), size_mb))

        # 清理旧的多余快照文件
        import glob
        for old_file in glob.glob("rss_api_snapshot_*.json"):
            try:
                num = int(old_file.replace("rss_api_snapshot_", "").replace(".json", ""))
                if num >= n_chunks:
                    os.remove(old_file)
                    print("[快照] 清理旧文件: %s" % old_file)
            except ValueError:
                pass

        print("[快照] 保存 API 快照: %d 源 %d 篇 (%d 块)" % (len(snapshot_sources), total_items, n_chunks))
    except Exception as e:
        print("[快照] 保存失败: %s" % e, file=sys.stderr)


# ── RSS 信源配置（从 rss_sources.json 读取，tier 分层） ─
_SOURCES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rss_sources.json")
try:
    with open(_SOURCES_FILE, "r", encoding="utf-8") as _f:
        RSS_SOURCES = json.load(_f)
    # 补全缺失 tier/color（兼容旧格式；前端与聚合代码直接读 color，缺失会 KeyError）
    # 丢弃缺 key/name/url 的坏条目：单条坏配置不应炸整场构建（对抗性审查 P3）
    _BAD = []
    _COLOR_PALETTE = ["#6366f1", "#10a37f", "#4285f4", "#e61919", "#ff6600", "#7c3aed", "#0891b2", "#d97706", "#d32f2f", "#24292e"]
    for _s in RSS_SOURCES:
        if not isinstance(_s, dict):
            _BAD.append(str(_s)[:30] + "(非dict)")
            continue
        if not _s.get("key") or not _s.get("name") or not _s.get("url"):
            _BAD.append(_s.get("key") or "(无key)")
    RSS_SOURCES = [s for s in RSS_SOURCES if isinstance(s, dict) and s.get("key") and s.get("name") and s.get("url")]
    for _s in RSS_SOURCES:
        _s.setdefault("tier", 3)
        _s.setdefault("cat", "other")
        if not _s.get("color"):
            _s["color"] = _COLOR_PALETTE[int(hashlib.md5(_s.get("key", "").encode("utf-8")).hexdigest(), 16) % len(_COLOR_PALETTE)]
    if _BAD:
        print("[RSS] 丢弃 %d 个缺 key/name/url 的坏配置: %s" % (len(_BAD), _BAD[:5]), file=sys.stderr)
    print("[RSS] 从 rss_sources.json 加载 %d 个源" % len(RSS_SOURCES))
except (FileNotFoundError, json.JSONDecodeError) as _e:
    print("[RSS] rss_sources.json 不可用 (%s)，RSS 聚合将跳过" % _e, file=sys.stderr)
    RSS_SOURCES = []


# 分类标签
CATEGORY_LABELS = {
    "wechat": "公众号",
    "ai":      "AI 日报",
    "tech":    "科技资讯",
    "cn_tech": "中文科技",
    "dev":     "开发者",
    "news":    "综合新闻",
    "podcast": "播客",
    "twitter": "Twitter/X",
}

# 分类排序（公众号置顶）
CATEGORY_ORDER = ["wechat", "ai", "tech", "cn_tech", "dev", "news", "twitter", "podcast"]

# ── 公众号信源自动归类：wechat2rss URL → cat=wechat ──
for _s in RSS_SOURCES:
    if "wechat2rss" in _s.get("url", ""):
        _s["cat"] = "wechat"

ITEMS_PER_SOURCE = 30
FETCH_TIMEOUT = 8
# 单个 feed 的读取上限。曾经写死 5_000_000，把 4 个健康源（实测 10.5/7.8/7.4/5.5MB）
# 切在 CDATA 中间 → ET 报 "unclosed CDATA" → 整源 0 条，看起来像上游 XML 坏了。
# 12MB 覆盖已观测最大值(10.5MB)只留 14% 余量，但代价是悬崖式的：踩线即整源归零。
# 内存由并发度决定而非源数：实测 12.2MB 的 feed 走完 read→decode→建树→抽字段
# 峰值约 71MB（≈原文 5.8 倍），全局并发 12 同级 ≈ 850MB；再叠上下面那段 CDATA
# 回退会复制全文并二次建树。要抬这个数，必须同时把回退改成不复制全文。
MAX_FEED_BYTES = 12 * 1024 * 1024
TRANSLATE_TIMEOUT = 3


# ──────────────────────────── 工具函数 ────────────────────────────

def _now_bj():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def _esc(s):
    return html_mod.escape(str(s), quote=True)


def _strip_html(text):
    text = html_mod.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"<[^>]*$", "", text)  # 移除末尾未闭合的标签片段
    return text.strip()


_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"'\s>]+)[\"']", re.IGNORECASE)

def _extract_img_from_html(text):
    """从 HTML 片段提取首个可外链的图片 URL（供卡片封面降级用）"""
    if not text:
        return ""
    m = _IMG_SRC_RE.search(text)
    if not m:
        return ""
    src = html_mod.unescape(m.group(1)).strip()
    if not re.match(r"^https?://", src, re.IGNORECASE):
        return ""
    return src


def _pick_item_image(it, desc_raw, content_raw):
    """RSS item 图片提取优先级：enclosure > media:content > media:thumbnail > 正文首图 > 描述首图"""
    try:
        enc = it.find("enclosure")
        if enc is not None:
            etype = (enc.get("type") or "").lower()
            eurl = (enc.get("url") or "").strip()
            if eurl and (etype.startswith("image") or re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", eurl, re.IGNORECASE)):
                return eurl
        mrss = "{http://search.yahoo.com/mrss/}"
        for tag in ("content", "thumbnail"):
            m_el = it.find(mrss + tag)
            if m_el is not None and (m_el.get("url") or "").strip():
                mtype = (m_el.get("type") or m_el.get("medium") or "").lower()
                if not mtype or mtype.startswith("image") or mtype == "photo":
                    return m_el.get("url").strip()
        img = _extract_img_from_html(content_raw) or _extract_img_from_html(desc_raw)
        return img
    except Exception:
        return ""


# ── 封面"必挂域名"判空表（③#1）───────────────────────────────
# 证据：docs/排查记录.md 2026-10-01 逐源实测。只收录**浏览器(带系统代理)与 Node 直连
# 两条通道都取不到**的域名 —— 只有代理侧挂、直连侧通的（pbs.twimg.com / cdn.hk01.com 等）
# 不进这张表，否则会把能显示的封面一起清掉。按域名而非按 URL 是因为已明确不做构建期探活。
_BAD_COVER_HOSTS = frozenset({
    "ichef.bbci.co.uk",          # BBC 全系 305/305 挂：DNS 投毒 + schannel 握手失败
    "i.guim.co.uk",              # 卫报 92 张 401，换 UA/Referer 四种组合同挂
    "npr.brightspotcdn.com",     # NPR 54 张 403
    "external-preview.redd.it",  # Reddit 预览图 24 张 ECONNRESET
    "ops.xhyun.news.cn",         # 新华社内网 OSS，公网 NXDOMAIN
})

_bad_cover_hits = {}
_bad_cover_lock = threading.Lock()


def _cover_host(url):
    """封面 URL 的 host（小写、去 userinfo 与端口）；非 http(s) 返回空串。"""
    s = str(url or "").strip()
    if s.partition("//")[0].lower() not in ("http:", "https:"):
        return ""
    netloc = (s.partition("//")[2].split("/")[0] or "").split("@")[-1].split(":")[0]
    return netloc.lower()


def _host_matched(host, table):
    """host 命中表内某域名（精确或作为子域）；空 host 不命中，避免误伤相对路径。"""
    if not host:
        return False
    for d in table:
        if host == d or host.endswith("." + d):
            return True
    return False


def _drop_unloadable_cover(url):
    """必挂域名的封面出厂写空（卡片退化为纯文字紧凑卡），其余原样返回。

    这里**不设**"带 Referer 才放行"的例外分支：那种域名（实测只有财富中文网）靠页面侧
    `referrerpolicy` 就能出图，本表不该收它；一旦有人把它加进来，判空会静默吞掉 ③#2，
    所以由判据 `test_referer_hosts_survive_the_bad_table` 直接红，而不是在运行时分优先级。
    """
    if not url:
        return url
    host = _cover_host(url)
    if not _host_matched(host, _BAD_COVER_HOSTS):
        return url
    with _bad_cover_lock:
        _bad_cover_hits[host] = _bad_cover_hits.get(host, 0) + 1
    return ""


def _purge_bad_covers_in_history(history):
    """清掉历史缓存里必挂域名的封面，返回清理条数。

    不清的话，未被再次抓取的旧条目每一场都会被重新判空一次，播报数字会钉在一个
    与"本场新增坏封面"无关的常量上；缓存本身也白占体积。
    """
    n = 0
    for _hit in history.values():
        _cached = _hit.get("image") or ""
        if _cached:
            _new = _drop_unloadable_cover(_cached)
            if _new != _cached:
                _hit["image"] = _new
                n += 1
    return n


# 历史摘要清扫的轮数护栏（不是语义）：`_clean_history_summary` 每轮都是"HN 刀 → 粘连刀"
# 这个**唯一**顺序，收敛即返回；真数据实测一轮就收敛（见 `_renormalize_history_fulltext`
# 的 docstring：18,037 条第二遍 0 改动）。
_SUMMARY_SWEEP_MAX_ROUNDS = 4
# 历史清扫的分账：与 `_LAST_HISTORY_ACCOUNT` 同一形态，只由主流程播报一次。
# 必须可观测 —— 命中数掉到 0 要么上游换了形状、要么刀被摘了，两种都需要有人知道
# （同 `_bad_cover_hits` 那条理由）。
_LAST_HISTORY_SWEEP = {"entries": 0, "fulltext": 0, "body_delink": 0,
                       "summary_hn": 0, "summary_glued": 0}
# 出厂副本那一趟的分账（对抗审查 ②）。与上面那本同形但**另立**：历史侧播的是"存盘那份
# 改了多少条"，这一本播的是"三个消费者真正读的那份改了多少条"—— 混成一本就答不出
# 本条修复要观测的那个量（掉到 0 的两种解释同样都需要有人知道）。
_LAST_OUTGOING_SWEEP = {"entries": 0, "fulltext": 0, "body_delink": 0,
                        "summary_hn": 0, "summary_glued": 0}


def _clean_history_summary(text, link, tally=None):
    """历史里存的摘要过两把摘要刀：**先 HN 模板重写、再粘连裸链接**，滚到不动点。

    顺序与构建期逐字一致（解析入口 `_truncate(_strip_glued_url(_rewrite_hn_summary(...)))`、
    快照 `s` 出口、运行时 `api/rss.js` 的 `cleanSummary` 三处都是这个序），倒过来会先被
    粘连刀改掉 HN 刀要看的行首形状 —— 两把互相吞。

    为什么要**滚到不动点**而不是各过一遍：这两把刀会互相"让位"。粘连刀剥掉开头那条裸链接
    之后，原先被 URL 前缀挡住的那一行才第一次长成模板行的形状
    （`https://a/积分：3\n# 评论数：4\n正文` 过完一遍仍是脏的：第一轮 HN 刀只数到 1 行、
    顶不住"至少两行"的门槛，粘连刀随后才把 `积分：3` 露出来）。历史清扫的契约是"扫完即出厂
    形状"，一遍过完还不规范等于没扫；轮数上限是护栏不是语义 —— 真数据实测一遍就收敛
    （18,037 条历史 / 1,762 个摘要字段，第二遍 0 改动）。
    不做 `_truncate`：历史里存的已经是 `_truncate` 之后的形状，再过一次会按当前表长重切，
    那是一条没被派工的行为变更。
    `tally` 是可选的分账（`_LAST_HISTORY_SWEEP`）：**按每一轮真实开火计数**，而不是只看第一轮
    —— 让位那一条的 HN 刀是在第二轮开的火，只数第一轮就把分账播少了。
    """
    cur = text
    for _ in range(_SUMMARY_SWEEP_MAX_ROUNDS):
        _before = cur
        _hn = _rewrite_hn_summary(cur, link)
        if _hn != cur:
            if tally is not None:
                tally["summary_hn"] = tally.get("summary_hn", 0) + 1
            cur = _hn
        _nxt = _strip_glued_url(cur)
        if _nxt != cur:
            if tally is not None:
                tally["summary_glued"] = tally.get("summary_glued", 0) + 1
            cur = _nxt
        if cur == _before:
            return cur
    return cur


def _renormalize_history_fulltext(history, tally=None):
    """把历史里"按入库那一刻的规则出厂"的条目重扫成当前规则的形状，返回**改动条数**。

    与 `_purge_bad_covers_in_history` 同一条理由、同一个调用点：72h 窗口内未被再次抓取的
    旧条目不会被重新抓取，不重写的话它们就一直按入库那一刻的规则出厂。

    扫三类值、四把刀（顺序与出厂链路一致）：
      - `full_content`：`_normalize_body_html` → `_delink_nav_links`（规范化先把相对 URL
        绝对化，脱链接才看得见 host 之后的第一个路径段；只认相对或只认绝对会漏一半）
      - `summary` / `summary_zh`：`_clean_history_summary`（HN 模板刀 → 粘连裸链刀）
      `summary_zh` 必须一起扫：它是 `translations.json` 里跨场复用的缓存，不会被重新解析，
      快照 `s` 取的又是 `summary_zh or summary` —— 只扫 `summary` 的那半边是断链。

    但**收益要按实测说，别按预期说**（2026-10-05 在同一份 `rss_history.json` 上跑的：
    18,037 条、其中 5,405 条带正文；整表 0.73 s，第二遍 0 条改动）：
      - 正文侧规范化改动 **62 条，全部是相对 URL 绝对化，图片一条都没捞回来**。原因是历史里
        存的是 sanitize **之后**的正文 —— `_KEPT_ATTRS["img"]` 只留 src/alt，data-src 属性连同
        "没有 src 的整枚 img"在出厂前就没了（同一份数据里 `data-src` 命中 0 条），规范化无从提升。
      - 正文导航脱链 **36 条**（爱范儿_5×10、security_affairs_662×6、mit_tech_review_61×4、
        flowingdata_673×4、nasa_674×4、theverge_8/arstechnica_60 各 2 …），外部正文链接一条没动。
      - 摘要侧改动 **1,762 个字段**（`summary` 1,149、`summary_zh` 613）：HN 模板刀 1,750、
        粘连刀 **12**（12 条全在 nodeseek_54，与 Task 11 入库时的读数一致）。
        源分布 hn_newest_56×949、hn_ai_7×537、hn_show_58×113、hn_ask_57×99、hn_llm_8×47、
        nodeseek_54×12、hackernews_6×5 —— 落点就是这两个族，没有误伤面。
      - 三类并起来的净改动是 **1,244 条**（`_renormalize_history_fulltext` 的返回值口径是
        "条"不是"字段"，一条命中里 summary 与 summary_zh 都改也只算一条）。
    ⇒ 公众号那批正文图的"回来"发生在**解析入口**（`_fetch_rss` / `_parse_rss_item`），
    条目被重新抓取后在 `_accumulate_history`（:760）整条覆盖历史；这条历史重写挡的是
    "没被重抓、且形态仍可修"的那一类，并保证 `full_content` 与摘要出厂即规范这条**契约**成立
    （Task 4/7 之后运行时通道写进来的正文不走 sanitize，那时它才真正吃到图）。
    幂等实测：第二遍 0 条改动、分账全 0（判据
    tests/rss_history/test_history_knife_sweep.py::test_history_sweep_is_idempotent_second_pass_changes_zero
    与 ::test_sweep_reaches_a_fixed_point_when_knives_unmask_each_other 钉住）。
    `tally` 传别的字典就把分账记到那里 —— `_renormalize_outgoing_fulltext` 就是靠这一个
    参数复用同一批刀手（出厂副本那一格，对抗审查 ②）：**刀手清单全仓只有这一处**，
    两处各写一遍就是本仓点名的"两份实现分叉"第 4 个来源。
    """
    n = 0
    _t = _LAST_HISTORY_SWEEP if tally is None else tally
    _t.update({"entries": 0, "fulltext": 0, "body_delink": 0,
               "summary_hn": 0, "summary_glued": 0})
    for _hit in history.values():
        _link = _hit.get("link") or ""
        changed = False
        _fc = _hit.get("full_content") or ""
        if _fc:
            _norm = _normalize_body_html(_fc, _link)
            if _norm != _fc:
                _t["fulltext"] += 1
            _new = _delink_nav_links(_norm)
            if _new != _norm:
                _t["body_delink"] += 1
            if _new != _fc:
                _hit["full_content"] = _new
                changed = True
        for _field in ("summary", "summary_zh"):
            _old = _hit.get(_field) or ""
            if not _old:
                continue
            _new = _clean_history_summary(_old, _link, _t)
            if _new != _old:
                _hit[_field] = _new
                changed = True
        if changed:
            n += 1
    _t["entries"] = n
    return n


def _renormalize_outgoing_fulltext(sources_with_items, tally=None):
    """同一批刀也必须落在**真正出厂的那一份**上（对抗审查 ②）。

    缺陷形状（2026-10-05 实测复现，判据
    tests/rss_history/test_history_knife_sweep.py::test_outgoing_copies_are_swept_not_just_the_history_dict）：
    `_accumulate_history` 在 :909 用 `entry = dict(item)` 给每条历史命中造**出厂副本**，
    主流程的清扫（:9478）跑在那之后、且只改 `_rss_history` 里的那份字典 ⇒
    三个消费者（`_save_api_snapshot` / `build_html` / `write_data_chunks`）读到的副本
    仍是入库那一刻的形状：清扫后历史 `fc="<p>正文</p>Python"`、出厂 `fc` 仍是
    `<a href="/tag/python">Python</a>`，`entry is hist=False`，出厂 `s` 也还是模板行。
    同文件 :9461-9473 早就专门为 image 在副本上重跑了一遍 —— 那是本仓既有做法，照它修。

    **刀手清单不重写**：这里把出厂副本按对象 id 组成一张临时表，喂给同一个
    `_renormalize_history_fulltext`，两份对象共用一格实现 ⇒ 不存在"只改了一处"的分叉面
    （本仓同类事故：`_needs_translation` 两份、封面判空两份）。
    按 id 而不是按 link 建键：出厂列表里可能没有 link，也可能同 link 出现两次
    （跨源重复），按 link 建键会把其中一条静默丢掉。

    幂等：出厂副本里当次抓取的那批条目本来就过了同一批刀（解析入口），历史-derived 的
    副本过了这一格之后，第二遍改动数为 0（判据 ::test_outgoing_sweep_is_idempotent_second_pass_changes_zero）。
    分账另立一本（`_LAST_OUTGOING_SWEEP`），不与历史侧混：混在一格里就没法回答
    "出厂那份到底改了多少条"这个问题，而那正是本条修复要观测的量。
    """
    _by_id = {str(id(_it)): _it
              for _src in sources_with_items
              for _it in (_src.get("items") or [])}
    return _renormalize_history_fulltext(_by_id,
                                         _LAST_OUTGOING_SWEEP if tally is None else tally)


# ── XGO (Twitter/X) 信源内容清洗 ──────────────────────────────
_XGO_TWEET_TEXT_RE = re.compile(
    r"<div[^>]*\bwhite-space:\s*pre-wrap[^>]*>([\s\S]*?)</div>",
    re.IGNORECASE,
)
_XGO_ENGAGEMENT_RE = re.compile(
    r"💬|🔄|❤️|👀|📊|⚡\s*Powered\s+by\s+xgo\.ing|View\s+(?:Quoted\s+)?Tweet\s*🔗|🔗\s*View",
    re.IGNORECASE,
)


def _is_xgo_content(text):
    """检测是否为 xgo.ing Twitter/X 代理的推文 HTML"""
    if not text:
        return False
    return ("xgo.ing" in text or "Powered by xgo" in text) and "white-space: pre-wrap" in text


def _clean_xgo_content(text):
    """清洗 xgo.ing 推文 HTML：仅保留推文正文与引用推文文本，移除互动数据/头像/页脚"""
    if not text:
        return ""
    # 1. 提取所有推文文本块（white-space: pre-wrap 的 div）
    blocks = _XGO_TWEET_TEXT_RE.findall(text)
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        # 移除 "View Quoted Tweet" / "View on Twitter" 链接
        block = re.sub(
            r"<a[^>]*>[^<]*View\s+(?:Quoted\s+)?Tweet[^<]*</a>",
            "",
            block,
            flags=re.IGNORECASE,
        )
        # 移除剩余 HTML 标签
        plain = re.sub(r"<[^>]+>", "", block)
        plain = html_mod.unescape(plain).strip()
        if plain:
            parts.append(plain)
    return "\n\n".join(parts)


def _extract_tweet_media(text):
    """从 xgo.ing 推文 HTML 中提取真实推文媒体图片（排除 profile_images 头像）"""
    if not text:
        return ""
    for m in _IMG_SRC_RE.finditer(text):
        src = html_mod.unescape(m.group(1)).strip()
        if not re.match(r"^https?://", src, re.IGNORECASE):
            continue
        # 跳过 profile_images（用户头像），仅保留 /media/ 和 tweet_video_thumb
        if "profile_images" in src:
            continue
        if "/media/" in src or "tweet_video_thumb" in src:
            return src
    return ""


# ── 图片质量自动升级系统 ──────────────────────────────────────
# 规则注册表：新增 CDN 只需在此追加一行
# 格式: (域名子串, 匹配正则, 替换模板)
_IMG_UPGRADE_RULES = [
    # Phys.org CDN: /tmb/ 是 90×90 缩略图，/800/ 是 800px 高清图
    ("scx1.b-cdn.net",     r"/tmb/",                              r"/800/"),
    # Twitter/X 头像: _normal (48×48) → _400x400
    ("pbs.twimg.com",      r"_normal\.(jpg|png|gif)$",             r"_400x400.\1"),
]

# 需要回调函数的特殊规则（简单字符串替换无法正确处理）
def _query_width_upgrade(url):
    """保留原始查询参数分隔符（? 或 &），避免产生双 ?"""
    def _replace(m):
        sep = m.group(1)  # 保留原始 ? 或 &
        return f"{sep}width=640"
    return re.sub(r"([?&])width=\d+", _replace, url)

_IMG_UPGRADE_SPECIAL = [
    # Reddit 预览图: width=N 查询参数升级（external-preview.redd.it 已被封面判空表清掉，
    # 这里剩下的真实目标是 preview.redd.it）
    ("redd.it",          _query_width_upgrade),
]

# 升级追踪（线程安全，供构建审计使用）
_img_upgrade_log = []
_img_upgrade_lock = threading.Lock()


def _upgrade_img_url(url):
    """必挂域名先判空，再把已知低分辨率图片 URL 升级到高清版本并记录事件"""
    if not url:
        return url
    url = _drop_unloadable_cover(url)
    if not url:
        return ""
    original = url
    # 简单规则：正则替换
    for domain_match, pattern, replacement in _IMG_UPGRADE_RULES:
        if domain_match in url:
            new_url = re.sub(pattern, replacement, url)
            if new_url != url:
                url = new_url
    # 特殊规则：回调函数（保持宽高比/查询参数完整性）
    for domain_match, func in _IMG_UPGRADE_SPECIAL:
        if domain_match in url:
            new_url = func(url)
            if new_url != url:
                url = new_url
    if url != original:
        with _img_upgrade_lock:
            _img_upgrade_log.append({"from": original, "to": url})
    return url


# ── 通用缩略图检测器（发现未知 CDN 的低分辨率模式）──
# 策略：仅检测高置信度模式（查询参数小尺寸 + 缩略图关键词），
# 不做路径数字段匹配（日期 /15/、hash /25/ 等误报率极高）。
# 新 CDN 的缩略图由构建审计发现后，手动加入 _IMG_UPGRADE_RULES 即可。
_THUMB_QUERY_RE = re.compile(r"[?&](?:w|h|width|height|size)=(\d+)", re.IGNORECASE)
_THUMB_KEYWORD_RE = re.compile(
    r"/(?:thumb|thumbnail|tiny|mini|avatar)(?:[/.]|$)"
    r"|_normal\.(?:jpg|png|gif)",
    re.IGNORECASE,
)
# 排除误报：日期路径、商品图、emoji、大尺寸参数、/display/ 路径等
_THUMB_EXCLUDE_RE = re.compile(
    r"/(?:19|20)\d{2}/"       # 年份路径
    r"|s\d{3,4}x\d{3,4}"     # 商品图 s1440x1440
    r"|/emoji/"               # emoji 图标
    r"|width=\d{4,}"          # 大尺寸参数 width=800+
    r"|/display/",            # France24 等 /display/ 路径
    re.IGNORECASE,
)


def _detect_lowres_img(url):
    """通用检测：URL 是否疑似缩略图（用于审计告警，不修改 URL）"""
    if not url:
        return None
    if _THUMB_EXCLUDE_RE.search(url):
        return None
    # 查询参数中的小尺寸（≤300）
    m = _THUMB_QUERY_RE.search(url)
    if m:
        num = int(m.group(1))
        if num <= 300:
            return f"query ={num}"
    # 缩略图关键词
    if _THUMB_KEYWORD_RE.search(url):
        return "thumb keyword"
    return None


def _audit_image_quality(sources_with_items):
    """构建时图片质量审计：报告升级统计与未解决的疑似低分辨率图片"""
    total_imgs = 0
    upgraded_count = len(_img_upgrade_log)
    # 按域名统计升级分布
    upgrade_by_domain = collections.Counter()
    for ev in _img_upgrade_log:
        domain = ev["from"].split("/")[2] if ev["from"].startswith("http") else "unknown"
        upgrade_by_domain[domain] += 1
    # 扫描所有图片，检测未升级的疑似缩略图
    suspect = []  # (source_name, url, reason)
    suspect_by_source = collections.Counter()
    for src in sources_with_items:
        src_name = src.get("name", "")
        for it in src.get("items", []):
            img = it.get("image", "")
            if not img:
                continue
            total_imgs += 1
            reason = _detect_lowres_img(img)
            if reason:
                suspect.append((src_name, img, reason))
                suspect_by_source[src_name] += 1
    # 输出报告
    print(f"[图片审计] 总计 {total_imgs} 张, 已升级 {upgraded_count} 张")
    if upgrade_by_domain:
        for domain, count in upgrade_by_domain.most_common(10):
            print(f"  升级: {domain} ({count} 张)")
    if suspect:
        print(f"[图片审计] ⚠ {len(suspect)} 张疑似低分辨率（{len(suspect_by_source)} 个源）:")
        for src_name, count in suspect_by_source.most_common(20):
            sample = next(u for s, u, _ in suspect if s == src_name)
            print(f"  {src_name}: {count} 张 — {sample[:90]}")
    else:
        print("[图片审计] ✓ 无残留低分辨率图片")
    return {"total": total_imgs, "upgraded": upgraded_count, "suspect": len(suspect)}


_SAFE_TAGS = re.compile(
    r"^(/?(p|br|img|a|b|i|em|strong|h[1-6]|ul|ol|li|blockquote|pre|code"
    r"|figure|figcaption|table|tr|td|th|thead|tbody|span|div|hr|sup|sub|dl|dt|dd"
    r"|audio|video|source|iframe))$",
    re.IGNORECASE,
)
_EVT_ATTR = re.compile(r"^on[a-z]+$", re.IGNORECASE)
_TAG_NAME = re.compile(r"^</?(\w[\w-]*)")
# 正文出厂规范（Task 1/3 的唯一实现处；JS 端口在 lib/body_rules.js，对账判据
# tests/site_nav/test_body_rules_parity.py —— 改这里必须同步改那边，否则对账红）。
# 元组顺序**就是**优先级：_fix_img 按这个元组取第一个非空的值，与标签里属性的书写顺序无关。
# JS 侧 _LAZY_SRC_ATTRS 必须与这个元组逐项同序（顺序不同 = 两侧挑到不同的镜像地址）。
_LAZY_SRC_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-croporisrc")
_BODY_CAP = 50000
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_OPEN_RE = re.compile(r"^<img\b", re.IGNORECASE)
# 这里必须是 (?<![\w-]) 而不是 \b：`-` 不是词字符，`\bsrc` 会命中 data-src 里的那个 src，
# 于是"只有 data-src 的 img"被误判为已有真图，提升整步静默失效（JS 的 \b 同病，
# Task 3 端口照搬时两侧要一起带守卫，否则对账会绿而行为两边都错）。
# 它同时是**唯一**的"src 属性"口径：读现值、判存在、改写都复用这一个已编译常量
# （M3：旧版另写了 `\ssrc\s*=\s*"[^"]*"` 的 search + 同式 sub，三套正则一个概念）。
_IMG_SRC_ATTR_RE = re.compile(r'(?<![\w-])src\s*=\s*"([^"]*)"', re.IGNORECASE)
# (?<![\w-]) 是防 `data-src` 被当成 `src` 的关键，删掉它 = 凭空造出第二个 src。
_URL_ATTR_RE = re.compile(r'(?<![\w-])(src|href|poster)\s*=\s*"([^"]*)"', re.IGNORECASE)
# 属性名/值对：与 _sanitize_html 里的 ([\w-]+)\s*=\s*"([^"]*)" **逐字同形**（M3：懒加载
# 属性解析不再另立带冒号的第四种写法，JS 端口也因此只有一套属性语法要对）。
_ATTR_VALUE_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
# 尾部**未闭合**的标签片段：模式里不能出现收尾的 `>`（写成 `<[^>]*>$` 只会命中完整标签，
# `<img src="htt` 这种半截反而逃掉，与仓内 _strip_html/_sanitize_html 用的 `<[^>]*$` 也不一致）。
# 写成 `>?$` 更糟：切点正好落在完整标签的 `>` 上时会把整枚标签一起吞掉（少留内容）。
_DANGLING_TAG_RE = re.compile(r"<[^>]*$")
# 允许 0 个后继字符：截断正好停在 `&` 上也是残缺实体（第一版写成 `&[A-Za-z#]…` 时，
# 尾部裸 `&` 逃过剥离，而配套测试同样要求"至少一个字符"，于是判据与缺陷同形 —— 假绿）。
# 反过来收窄成 `&$` 也不许：尾部挂 1~8 个实体名字符（`</p>&a`）同样是残缺。
_DANGLING_ENTITY_RE = re.compile(r"&[#A-Za-z0-9]{0,8}$")
# **只有"第一次动刀"能用上面那条**（它允许 `&名字`，额度只有一次）。第二刀起、以及刀一
# 什么都没剥到的时候，只许剥这条"无歧义形态"：尾部裸 `&`、`&#` 加十进制、`&#x` 加十六进制。
# 为什么必须分开：`&名字` 是**有歧义**的 —— `A&B`、`AT&T`、`R&D`、`Q&A` 全是合法文字。
# NEW-1 的修法把"退尾部空白"放进了刀二的每一轮循环，却没同时收窄形态，于是回溯之后又拿
# `&[#A-Za-z0-9]{0,8}$` 剥了一次，把合法文字吃了（实测 `cap("A&B &am ", 7)` → `'A'`，应为
# `'A&B'`；`cap("a&&b&am", 6)` 级联三层 → `'a'`，应为 `'a&&b'`）。⇒ 实体名段一生至多剥 1 段。
# 量词写成 ASCII 显式类而不是 `\d`：这条正位在 `_cap_body` 里由**右往左扫描**等价实现（见
# `_unambiguous_entity_start`），扫描器只能按 ASCII 判；`\d` 在 str 模式下还认 Unicode 十进制
# 数字，两边会分叉。收窄的方向是"少剥"，与 NEW-3 保内容的取向一致。
_DANGLING_ENTITY_UNAMBIG_RE = re.compile(r"&(?:#[0-9]*|#x[0-9a-fA-F]*)?$")
# 这两份字符集**只被扫描器**（`_unambiguous_entity_start`）引用；上面那条正则写的是内联
# 字面类（`[0-9]` / `[0-9a-fA-F]`），并没有引用它们 ⇒ "同一份真相"并不成立，两边等价是
# 由对账判据 `test_cap_unambiguous_scan_is_the_same_predicate_as_the_regex` 在**行为上**兜住的。
# ⇒ 改这两个集合必须同时改正则，否则红在那条判据上、而不是红在编译期。
_DIGIT_ENTITY_CHARS = frozenset("0123456789")
_HEX_ENTITY_CHARS = frozenset("0123456789abcdefABCDEF")
# 窗口宽度是**推**出来的不是拍的：`_DANGLING_ENTITY_RE = &[#A-Za-z0-9]{0,8}$` 的最长匹配
# = 1 枚 `&` + 8 个实体名字符 = `_ENTITY_TAIL_WINDOW = 1 + 8 = 9`，且任何匹配都以串尾结束
# ⇒ 起点一定落在最后 9 个字符内 ⇒ 只看这 9 个字符 = 看整串。改量词必须同时改这里，
# 判据 `test_cap_entity_window_is_wide_enough_for_eight_name_chars` 直接从正则里读上界对账。
# Task 3 的 JS 端口照**推导**抄（窗口 = 1 + 量词上界），别只抄字面 9 而把量词改窄。
# 为什么要有窗口：旧版 while+re.sub 每轮重扫整串，`&`×50000 实测 14.24 s / 本机 18.77 s。
_ENTITY_TAIL_WINDOW = 9
# 占位 src 的文件名词干精确等于这几个词才算占位（I6 收窄；误判方向一律"保留原 src"）。
_PLACEHOLDER_SRC_NAMES = ("blank", "lazy", "placeholder", "spacer")
# 词干里含"被非字母数字包围"的 1x1/0x0 成分才算占位：`spacer_1x1` 命中，
# mmbiz 的 base64 段 `…LlmTcx1x1h4q…`、目录段 `/0x0/` 不命中（IGNORECASE 下
# `[^0-9a-z]` 同样排除大写，别把这条写成 [^0-9a-z] 大小写敏感的版本）。
_PLACEHOLDER_DIM_RE = re.compile(r"(?:^|[^0-9a-z])(?:1x1|0x0)(?:$|[^0-9a-z])", re.IGNORECASE)
# I8（上一轮复核移交）：正文以**转义态**到达的形态。wechat2rss 一类代理会把已经 HTML 化的
# content:encoded 再转义一层，出厂时收到的是 `&lt;img data-src=&quot;…&quot;&gt;`。
# 两条判据同时成立才动手解一层，见 `_unescape_escaped_body`：
#   ① 有转义标签形态（`&lt;p&gt;` / `&lt;img …` / `&lt;/div&gt;`）；
#   ② 整段里没有未转义的真标签 —— 这是"这串是正文，而不是带代码样例的正文"的证据。
# 名字段后必须紧跟空白、`/` 或 `&gt;`：裸 `&lt;imgx` 这类不算标签起始。
_ESCAPED_TAG_HINT_RE = re.compile(r"&lt;/?[a-z][a-z0-9-]*(?:[\s/]|&gt;)", re.IGNORECASE)
_REAL_TAG_OPEN_RE = re.compile(r"<[a-zA-Z]")
# 仅保留渲染正文结构必需的属性：WeChat 段落带巨型内联 style，全量保留会撑爆快照
_KEPT_ATTRS = {
    "img": ("src", "alt"), "a": ("href",),
    "iframe": ("src", "width", "height", "frameborder", "allowfullscreen"),
    "audio": ("src", "controls", "preload"),
    "video": ("src", "controls", "preload", "poster", "width", "height"),
    "source": ("src", "type"),
}
_BOOL_ATTRS = {"controls"}
_URL_SCHEME = re.compile(r"^\s*(?:https?:|mailto:|/|#|data:image/)", re.IGNORECASE)
_SAFE_IFRAME_RE = re.compile(
    r'<iframe\s[^>]*src\s*=\s*"([^"]*(?:youtube\.com|youtu\.be|vimeo\.com)[^"]*)"[^>]*>\s*</iframe>',
    re.IGNORECASE | re.DOTALL,
)
_IFRAME_TAG_RE = re.compile(r'<iframe[^>]*>', re.IGNORECASE | re.DOTALL)
_MEDIA_ATTRS = re.compile(r'(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]+)')


def _pick_item_media(it):
    """提取 enclosure / media:content 中的音频视频 URL（跳过图片类型）"""
    try:
        enc = it.find("enclosure")
        if enc is not None:
            etype = (enc.get("type") or "").lower()
            eurl = (enc.get("url") or "").strip()
            if eurl and (etype.startswith("audio") or etype.startswith("video")):
                return eurl, etype
        mrss = "{http://search.yahoo.com/mrss/}"
        m_el = it.find(mrss + "content")
        if m_el is not None and (m_el.get("url") or "").strip():
            mtype = (m_el.get("type") or m_el.get("medium") or "").lower()
            if mtype in ("audio", "video") or mtype.startswith("audio") or mtype.startswith("video"):
                return m_el.get("url").strip(), mtype
    except Exception:
        pass
    return "", ""


def _sanitize_html(text):
    if not text:
        return ""
    text = html_mod.unescape(text)
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
    # 提取安全 iframe（仅 YouTube / Vimeo），替换为占位符，清洗后还原
    safe_iframes = []
    def _save_iframe(m):
        safe_iframes.append(m.group(0))
        return '\x00IFRAME%d\x00' % (len(safe_iframes) - 1)
    while _SAFE_IFRAME_RE.search(text):
        text = _SAFE_IFRAME_RE.sub(_save_iframe, text, count=1)
    # 移除剩余非安全 iframe
    text = re.sub(r"<iframe[^>]*>[\s\S]*?</iframe>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<form[^>]*>[\s\S]*?</form>", "", text, flags=re.IGNORECASE)

    def _clean_tag(m):
        full = m.group(0)
        m_name = _TAG_NAME.match(full)
        if not m_name:
            return ""
        tag = m_name.group(1)
        if not _SAFE_TAGS.match(tag):
            return ""
        kept = _KEPT_ATTRS.get(tag.lower(), ())
        safe_attrs = []
        if kept:
            for k, v in re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', full):
                if k.lower() not in kept or _EVT_ATTR.match(k):
                    continue
                # URL scheme 检查仅对 src/href/poster 等 URL 属性
                if k.lower() in ("src", "href", "poster") and not _URL_SCHEME.match(v):
                    continue
                safe_attrs.append('%s="%s"' % (k.lower(), v.replace("&", "&amp;")))
            # 布尔属性（如 controls）无 ="value"，单独检测
            for battr in ("controls",):
                if battr in kept and re.search(r'\b' + battr + r'(?:\s|>|/)', full, re.IGNORECASE):
                    safe_attrs.append(battr)
        if tag.lower() == "img" and not any(a.startswith("src=") for a in safe_attrs):
            return ""
        # iframe 二次校验：仅放行 YouTube / Vimeo 域名
        if tag.lower() == "iframe":
            src_val = ""
            for a in safe_attrs:
                if a.startswith("src="):
                    src_val = a[5:].replace("&amp;", "&")
                    break
            if not re.search(r'youtube\.com|youtu\.be|vimeo\.com', src_val, re.IGNORECASE):
                return ""
        if safe_attrs:
            return "<%s %s>" % (tag, " ".join(safe_attrs))
        if full.startswith("</"):
            return "</%s>" % tag
        return "<%s>" % tag

    text = re.sub(r"<[^>]+>", _clean_tag, text)
    text = re.sub(r"<[^>]*$", "", text)
    # 还原安全 iframe
    for i, iframe_tag in enumerate(safe_iframes):
        text = text.replace('\x00IFRAME%d\x00' % i, iframe_tag)
    return text.strip()


def _is_placeholder_src(value):
    """src 是不是"占位图"（I6 收窄）。判据一律朝**保留原 src**那一侧收：
    把真图误判成占位 = 用懒加载镜像地址换掉能显示的图，比不修更糟。

    旧版是 `re.search(r"(?:^data:|blank|lazy|placeholder|1x1|spacer)", 整条 URL)`，
    在本仓真实数据上大面积误杀：img-proxy 链接尾的 `%26wx_lazy%3D1`（5600+ 条）、
    `https://image.pseudoyu.com/images/lazy_cat_pic.png`、mmbiz base64 段里撞出来的
    `1x1`/`0x0`、`https://image.hkhl.hk/f/1024p0/0x0/100/…/New_Project_76__0.jpg`
    的缩放目录段。现在只认三种确定形态：
      ① `data:` 前缀（微信的 1px 内联占位）；
      ② 路径**最后一段**的文件名词干精确等于 `_PLACEHOLDER_SRC_NAMES` 之一；
      ③ 同一个词干里含被非字母数字包围的 1x1/0x0 成分（`spacer_1x1` 命中、
         `j7RlD5l5q1x1NoXsN` 不命中）。
    查询串与片段先剥掉，所以 `?…wx_lazy%3D1` 不算；Task 3 的 JS 端口必须同口径。
    """
    v = (value or "").strip()
    if not v:
        return False
    if v[:5].lower() == "data:":
        return True
    path = v.split("?", 1)[0].split("#", 1)[0]
    stem = path.rsplit("/", 1)[-1].partition(".")[0].lower()
    if not stem:
        return False
    return stem in _PLACEHOLDER_SRC_NAMES or bool(_PLACEHOLDER_DIM_RE.search(stem))


def _unescape_escaped_body(text):
    """I8：整段被转义的正文补解一层，好让 `_normalize_body_html` 看得见标签。

    为什么必须有它：`_sanitize_html` 第一句就是 `html_mod.unescape(text)`（:1539），而规范化
    按契约必须跑在 sanitize **之前** —— 于是双重转义的正文（`&lt;img data-src=&quot;…&quot;&gt;`）
    在规范化眼里一个标签都没有，原样返回；sanitize 再解出 `<img data-src=…>`，因为 img 没有
    src 被整枚丢弃（`_clean_tag` 末尾 `img and not any(src=…)` 那两行）。上一轮实测：
    `normalize(escaped)` 不变、`sanitize(normalize(escaped))` = `''`；同一串未转义时能拿到
    `<img src=…>`。⇒ 规范化必须建立在"已解一层"的文本之上。

    为什么不是无条件 unescape：正文里合法写着 `&amp;lt;img&amp;gt;`（要展示代码的字面量）时，
    多解一层会把它**激活**成真的 `<img>`，凭空多出一张文章里没有的图。判据
    `test_literal_escaped_code_sample_is_not_treated_as_escaped_body` 钉的就是这一侧。

    动手条件两条同时成立（见 `_ESCAPED_TAG_HINT_RE` / `_REAL_TAG_OPEN_RE`）：
      ① 有转义标签形态；② 整段没有未转义的真标签 —— ② 是"这串是正文，而不是带代码样例的
      正文"的证据。激活风险侧**零新增**：sanitize 本来就会无条件 unescape 一次，本函数只在
      "根本没有真标签"的输入上把同一件事提前一步做掉。
    """
    if not text:
        return text
    if _REAL_TAG_OPEN_RE.search(text):
        return text
    if not _ESCAPED_TAG_HINT_RE.search(text):
        return text
    return html_mod.unescape(text)


def _normalize_body_html(text, base_url):
    """正文出厂前的规范化：① 懒加载图片的真地址提到 src；② 相对 URL 按条目链接绝对化。

    必须在 _sanitize_html **之前**调用：那一步只认 src，data-src 不在 _KEPT_ATTRS 里，
    而 img 一旦没有 src 会被整枚丢掉（_clean_tag 末尾 `img and not any(src=…)` 那两行）
    —— 先洗后规范等于什么都没修。
    """
    if not text:
        return ""
    base = (base_url or "").strip()
    # M4：base 里带裸 `"` 时，urljoin 的产物会把引号一起带进属性值
    # （`src="https://e.com/d"q/b.html"`），下游 _sanitize_html 的属性提取 `"([^"]*)"`
    # 在第一个引号处截断 → 出厂一条静默变形的坏 URL。按"没有 base"处理：
    # 相对路径原样保留，宁可 404 也不要变形。
    has_base = base.lower().startswith(("http://", "https://")) and '"' not in base

    def _fix_img(m):
        tag = m.group(0)
        # I5：优先级按 _LAZY_SRC_ATTRS 的**元组顺序**取第一个非空值，与标签里属性的
        # 书写顺序无关（旧版按出现顺序 break，元组顺序对行为零影响，注释就是假的）。
        seen = {}
        for name, val in _ATTR_VALUE_RE.findall(tag):
            seen.setdefault(name.lower(), val.strip())
        lazy = ""
        for name in _LAZY_SRC_ATTRS:
            v = seen.get(name, "")
            if v:
                lazy = v
                break
        if not lazy:
            return tag
        cur = _IMG_SRC_ATTR_RE.search(tag)
        cur_v = cur.group(1).strip() if cur else ""
        if cur_v and not _is_placeholder_src(cur_v):
            return tag          # 真图已在 src，懒加载属性只是镜像，不动
        # C1：lazy 来自 feed，是**不可信数据**。旧版把它拼进 re.sub 的 replacement 模板，
        # 于是 `\u0026` 抛 bad escape \u、`\1` 抛 invalid group reference（无 src 分支更坏：
        # 静默把捕获到的 `<img` 注进属性值）。回调式替换返回拼好的字符串，不过模板解析。
        if cur:
            return _IMG_SRC_ATTR_RE.sub(lambda _m: 'src="%s"' % lazy, tag, count=1)
        return _IMG_OPEN_RE.sub(lambda _m: _m.group(0) + ' src="%s"' % lazy, tag, count=1)

    def _fix_url(m):
        name, val = m.group(1), m.group(2)
        v = val.strip()
        if not v or not has_base:
            return m.group(0)
        if v[0] == "#" or v.lower().startswith(("http:", "https:", "mailto:", "tel:", "data:", "//")):
            return m.group(0)
        joined = urllib.parse.urljoin(base, v)
        if not joined.lower().startswith(("http://", "https://")):
            return m.group(0)
        return '%s="%s"' % (name, joined)

    text = _IMG_TAG_RE.sub(_fix_img, text)
    return _URL_ATTR_RE.sub(_fix_url, text)


def _unambiguous_entity_start(s, end):
    r"""返回 `s[:end]` 上"无歧义残缺实体"匹配的**最左起点**；没有匹配返回 None。

    与 `_DANGLING_ENTITY_UNAMBIG_RE.search(s[:end])` 逐字等价（判据
    `test_cap_unambiguous_scan_is_the_same_predicate_as_the_regex` 对小字母表做穷举对账，
    差分自证又在 12000 条随机截断上把"整串定点参考"拿来比过，不等价 0），但刻意写成
    **右往左扫描**：正则版每轮整串重扫，对 `&`×50000 这类退化输入是二次方（M1，旧实测
    18.77 s）；扫描版每轮只吃切点左边那一段数字/十六进制，而各轮扫过的区间互不相交
    ⇒ 刀二+ 整体仍是 O(n)。
    三条形态与常量旁的注释同源（字符集共用 `_DIGIT_ENTITY_CHARS` / `_HEX_ENTITY_CHARS`）：
      `&$` 裸 &  /  `&#` + 十进制*  /  `&#x` + 十六进制*
    十进制与十六进制各至多一个合法起点（`&` 之后必须全在对应字符集里，而 `#` 与 `#x`
    互斥），所以"先试十进制、再试十六进制"就等于正则的最左匹配。
    """
    if end <= 0:
        return None
    last = s[end - 1]
    if last == "&":
        return end - 1
    if last == "#":
        return end - 2 if end >= 2 and s[end - 2] == "&" else None
    if last == "x":
        return end - 3 if end >= 3 and s[end - 2] == "#" and s[end - 3] == "&" else None
    if last in _DIGIT_ENTITY_CHARS:
        d = end - 1
        while d > 0 and s[d - 1] in _DIGIT_ENTITY_CHARS:
            d -= 1
        if d >= 2 and s[d - 1] == "#" and s[d - 2] == "&":
            return d - 2
    if last in _HEX_ENTITY_CHARS:
        h = end - 1
        while h > 0 and s[h - 1] in _HEX_ENTITY_CHARS:
            h -= 1
        if h >= 3 and s[h - 1] == "x" and s[h - 2] == "#" and s[h - 3] == "&":
            return h - 3
    return None


def _cap_body(text, limit=_BODY_CAP):
    """按**码点**截断，并且**只在真的截断了**才抹尾部的半个标签/半个实体。

    口径逐条如下，两边（Python / lib/body_rules.js）必须逐条对齐：
      1. `len(text) <= limit` → 原样返回（I7）。没截断就没有"半个"可言，旧版无条件收口：
         `Tom&Jerry`→`Tom`、`ends with <span`→`ends with`、尾部空白被顺手 rstrip。
      2. 按码点计数：Python 的 len 与切片本来就是码点，`text[:limit]` 与 JS 端口
         `Array.from(s).slice(0, limit).join("")` 同口径（JS 直接 String.slice 会把 emoji
         的 UTF-16 代理对劈成两半），不许再写 `"".join(list(text)[:limit])`（M2）。
      3. 两刀的顺序与判据不变：刀一 `_DANGLING_TAG_RE`、刀二（实体刀）见第 5 条。
         刀一只可能命中一次（切点之后不会再有残缺标签），实体刀可以连着命中（`&&` 连排），
         但每步只在**尾部 `_ENTITY_TAIL_WINDOW` 字符的窗口**里定位、只移动切点，
         既不重扫整串也不逐轮复制字符串 —— 旧版 `while True` + 整串 `re.sub` 对
         `&`×50000 是二次方（实测 18.77 s，复核机器 14.24 s），一条畸形 feed 就能拖住构建。
         窗口视野只对"名段那一刀"成立（它最长 9 个字符）；无歧义那一刀走
         `_unambiguous_entity_start` 的右往左扫描，与整串正则等价且同样 O(n)。
      4. 刀二每一轮都**先把切点上的尾部空白退掉再取窗口**（`str.isspace()`，与 `rstrip()`
         同口径），否则 `$` 锚定的实体正则在"空白结尾"的窗口上永远匹配不到，切点停在空白上，
         出口 rstrip 一退空白就把残缺实体重新暴露。**靠这一退才不漏的形态**（实测出口）：`& &m`→`''`、`x&  &ab`→`'x'`、
         `& &m `→`''`；把退空白那两行删掉，同样本变成 `'&'`、`'x&'`、`'& &m'` —— 全是漏剥。
         **反过来不靠这一退也留在尾上、已知未修的形态**（取向是"少剥"，宁残不误剥）：`&\xa0&nbsp`
         以及它前面还挂着正文的变体 —— NBSP 不在实体名字符类里，两条 `$` 锚定的
         正则都够不着那个 `&`；宁可留一条看着像残缺实体的尾巴，也不去剥 `&` 后面可能合法的
         文字。**旧稿在这里登记的是 `Tom &am `、`A&B &am `**：现实现下它们分别出口 `Tom`、
         `A&B`，**两条都不漏** —— 属于文档与实现不符，已按实测换成上面这一组。
         刀与刀之间的空白照旧要剥（`&am <b` 这种被标签片段隔开的形态要看得见），
         但剥完仍要保证内容守恒：切点落在**完整**标签的 `>` 上时一个字都不许多剥。
      5. 退完空白之后守 NEW-3：**实体名段至多剥 1 段**，而**无歧义形态一律剥净**：
           · `&名字`（`_DANGLING_ENTITY_RE`，允许 1~8 个名符）只许当**第一次实体刀**，
             错过就没有下一次 —— 即"名段至多剥 1 段"；
           · 裸 `&` / `&#`+十进制 / `&#x`+十六进制（`_DANGLING_ENTITY_UNAMBIG_RE` 的正位，
             实现是 `_unambiguous_entity_start`）每一轮都查、不耗额度，连名段那一刀
             什么都没剥到时也查 —— 不变量①a 要的就是"截断过的出口绝不挂这三条尾巴"。
         否则 `cap("A&B &am ", 7)` 会把合法文字 `A&B` 吃成 `A`、`cap("a&&b&am", 6)`
         级联三层吃成 `a`、`cap("x&xab&am", 7)` 把 `&xab` 当成 `&#x…` 吃掉（NEW-1 的修法
         自己引入的回归）。**第一次实体刀的既有契约不变**：`Tom&Jerryzz`/7 修复前后同为
         `'Tom'` —— `&X` 被吃这件事本来就在，本轮只收"第二段 `&名字`"这一条权限。
      6. 出口的 `out[:cut].rstrip()` 是"刀二停稳后唯一还在退空白的一刀"，不许省。
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    out = text[:limit]
    m = _DANGLING_TAG_RE.search(out)
    cut = len(out) if m is None else m.start()
    # NEW-3：`&名字` 只许当**第一次实体刀**（契约第 1 条"第一刀不变"的字面含义）；
    # 无歧义形态（裸 `&` / `&#`数字 / `&#x`十六进制）则**每一轮都查**、不耗额度 ——
    # 不变量①a 要的是"截断过的出口绝不挂着这三条尾巴"，所以那一刀不许缺席。
    first_entity_knife = True
    while cut > 0:
        # 刀二定位前必须先把切点上的尾部空白退掉。`_DANGLING_ENTITY_RE` 是 `$` 锚定的，
        # 窗口末字符是空白时它必然匹配不到 → break → 出口那记 rstrip 再把空白剥掉 →
        # 残缺实体（实测靠这一退才不漏的是 `& &m`、`x&  &ab`、`& &m `；`&\xa0&nbsp` 是契约第 4 条点名的"已知未修"（NBSP 挡的是名字字符类，与这行退空白无关）；
        # 旧稿在这里还登记了 `Tom &am `、`A&B &am `，现实现下分别出口 `Tom`、`A&B`，
        # 两条都不漏 —— 文档与实现不符，已按实测划掉）重新挂回尾巴。
        # 退空白的口径必须和出口的 `str.rstrip()` 一致，所以用 str.isspace()：
        # 写成 `out[i] <= ' '` 会放过 `\xa0`/`\u2028`（NBSP 在中文正文里到处都是）= 同一条回归。
        # 摊还是 O(n)：每轮退的区间 (tail, cut] 互不相交，而 cut 每轮严格左移 ≥1。
        tail = cut
        while tail > 0 and out[tail - 1].isspace():
            tail -= 1
        # 第一次实体刀：允许 `&名字`（`_DANGLING_ENTITY_RE`，窗口内最左匹配 = 整串最左匹配，
        # 因为它的任何匹配都 ≤ `_ENTITY_TAIL_WINDOW` 个字符且必然结束在 tail 上）。
        m = None
        if first_entity_knife:
            window = out[max(0, tail - _ENTITY_TAIL_WINDOW):tail]
            m = _DANGLING_ENTITY_RE.search(window)
            first_entity_knife = False     # 名段额度只有这一次，剥没剥到都不回来
        if m is not None:
            # 匹配必然以窗口末尾结束 ⇒ 绝对切点 = 窗口起点 + 匹配起点
            cut = tail - (len(window) - m.start())
            continue
        # 之后（以及第一刀什么都没剥到时）只查无歧义形态，且**每一轮都查**：不变量①a 要求
        # "截断过的正文出口绝不挂裸 & / &#数字 / &#x十六进制 的尾巴"，而这三条后面不可能再接
        # 合法文字 ⇒ 剥它们零风险。`a&#000000000`（数字尾巴 >9 字符、名段那一刀看不见）就是
        # 靠这一查兜住的 —— 把它做成"只在名段剥过之后才查"会漏，见差分自证的 token 生成器。
        nxt = _unambiguous_entity_start(out, tail)
        if nxt is None:
            break
        cut = nxt
    # 这记 rstrip 是"刀二停稳后唯一还在退空白的地方"（V13 的靶）：上面 break 时 cut 可能
    # 正停在一串空白上（那串空白之后没有实体，所以循环没动它），出口必须把它退掉。
    return out[:cut].rstrip()


# ── 站内导航链接去链接（Task 10 的规则 2.5；R43 抽成模块级以便与 JS 端口逐条对账）──
# 正文里的 "Kubernetes" 这类词常被包成 /tag/ 链接，整枚删除等于删正文 —— 判据
# test_internal_nav_link_loses_href_but_keeps_text / test_nav_delink_keeps_surrounding_prose_verbatim
# 钉住这条边界，防下一个人图省事改成"删掉所有 <a>"（外部正文链接那条反例判据会当场红）。
# 判定只看 **host 之后的第一个路径段**，不做整串子串匹配：
#   `https://about.fb.com/news/…`（about 在 host 上）、
#   `https://lists.gnu.org/archive/html/…`、`…/blob/main/docs/author/x.md`（导航词在路径中段）
# 都不是站内导航。brief 原样的 `href="[^"]*(?:/about|/archive…)[^"]*"` 会把这三种一起吃掉，
# 真数据实测（那份快照 3140 条出厂正文、2159 条外部 href）带走 142 条外链 —— 违反
# "外部正文链接不许被动"这条红线。两条反例判据
# test_host_named_after_a_nav_word_is_not_delinked /
# test_nav_word_outside_first_path_segment_is_not_delinked 在收紧前红、收紧后绿；
# 反向守卫 test_nav_first_segment_still_delinked_with_deeper_path_and_query 挡住
# "收成整段精确等于 /tag"那种过度收窄。
# 相对形状 `/tag/x` 也要认：`_normalize_body_html` 在真实链路上已把它绝对化成
# `https://blog.example/tag/x`，只认相对或只认绝对都会漏一半。
# `categor`/`author`/`tag` 单复数并列不是冗余：brief 写的 `categories?` 只能匹
# "categorie(s)"，**匹不到单数 `category`**（-y→-ies 不是加 s），
# 照抄会让判据 test_author_and_category_links_too 永久红。
#
# 词表在运行时那一侧的同名导出是 `lib/body_rules.js` 的 `NAV_LINK_SEGMENTS`，
# 两侧同值由 tests/site_nav/test_body_rules_parity.py 钉住（改一边必须改另一边）。
_NAV_LINK_SEGMENTS = ("tag", "tags", "category", "categories", "author", "authors",
                      "about", "subscribe", "donate", "archive")


def _delink_nav_links(text):
    """把站内导航链接脱成纯文字，外部正文链接一条不动，链接里的内层标签原样保留。"""
    def _delink_nav_link(m):
        path = re.sub(r"^(?:[a-z][a-z0-9+.-]*:)?//[^/]*", "", m.group("href"), flags=re.IGNORECASE)
        if not path.startswith("/"):
            return m.group(0)      # 纯相对（"tag/x"）、mailto:、空值：一律不动，宁可少剥
        return (m.group("text") if re.split(r"[/?#]", path[1:], 1)[0].lower() in _NAV_LINK_SEGMENTS
                else m.group(0))
    return re.sub(
        r'<a\b[^>]*?href="(?P<href>[^"]*)"[^>]*>(?P<text>[\s\S]*?)</a>',
        _delink_nav_link, text, flags=re.IGNORECASE)


def _deep_clean_html(text):
    """深度清洗 RSS 正文：移除广告、推广、引导关注等噪音，保留正文结构"""
    if not text:
        return ""
    # 1. 【已删除】按 class/id 整枚删除推荐块/署名块的规则（原词表 ads?|advert|banner|…|
    #    related-posts|recommend|read-next|article-tags|author-box|byline|toc-|meta-info…）。
    #    删除理由（2026-10-05 裁定，R42）：这条在本函数的**两个生产调用点**上都开不了火。
    #    唯一入口链是 `_deep_clean_html(_sanitize_html(_normalize_body_html(...)))`，
    #    而 `_sanitize_html` 的属性白名单 `_KEPT_ATTRS` 不保留 class/id —— 等本函数拿到
    #    `text` 时 `<div class="related-posts">` 早就是 `<div>`，任何按 class/id 匹配的规则
    #    命中恒为 0。留它就是"看着有防线、实际为 0"的假绿形状（本仓点名的自欺形状之一）。
    #    真在起作用的是下面 2.5/2.6 两条**按 href 判**的规则（实测：这份历史语料上脱掉
    #    36 条导航 href、清掉 1 个推广块）。
    #    闸：tests/rss_history/test_no_inert_deep_clean_rules.py —— 每条 re.sub 规则都必须
    #    在"生产顺序 + 仓库真语料"上至少真的改写过一条样本，否则当场红；
    #    口径事实另由 test_sanitize_drops_class_so_rule1_is_inert_on_the_entry_chain 钉住
    #    （哪天给 `_KEPT_ATTRS` 加上 class，那条会红，届时才允许重新考虑容器名这一刀）。
    # 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）
    text = re.sub(
        r'<a[^>]*href="[^"]*(?:link-proxy|wechat2rss|mp\.weixin\.qq\.com)[^"]*"[^>]*>[^<]*</a>',
        '', text, flags=re.IGNORECASE)
    text = re.sub(
        r'<a[^>]*>[^<]*跳转微信[^<]*</a>',
        '', text, flags=re.IGNORECASE)
    # 2. 逐块检测：剥离内联标签后匹配推广模式
    _promo_re = re.compile(
        r'代开关注|长按二维码|扫码关注|扫一扫关注|微信搜索.*关注|关注公众号|关注我们'
        r'|立即购买|点击领取|点击注册|限时优惠|秒杀活动|加入社群|加入我们'
        r'|勾选关注|长按关注|识别二维码|二维码|长按识别'
        r'|关注.*公众号|关注.*微信|点击.*订阅|订阅.*频道|订阅.*邮件|加入.*邮件列表'
        r'|微博.*关注|关注.*微博|分享.*好友|转发.*朋友'
        r'|转发.*关注|关注.*转发|点赞.*关注|关注.*点赞|点赞.*在看'
        r'|喜欢.*关注|喜欢.*点赞|觉得.*关注|觉得.*有用|精彩.*不错过'
        r'|请长按|请扫码|点击原文|点击.*原文|点击.*查看原文'
        r'|buy now|subscribe (?:now|today)|limited.?time|click here to'
        r'|sign up (?:now|today)|special offer|discount code|use code|free trial'
        r'|donate (?:now|today)|support us|follow us (?:on|for)|join our'
        r'|share this (?:article|post)',
        re.IGNORECASE)
    def _strip_and_check(m):
        block = m.group(0)
        plain = re.sub(r'<[^>]+>', '', block)
        return '' if _promo_re.search(plain) else block
    text = re.sub(r'<(p|div)\b[^>]*>[\s\S]*?</\1>', _strip_and_check, text, flags=re.IGNORECASE)
    # 2.5 站内导航链接：**去链接留文字**（Task 10）。R43 起这条刀抽成模块级
    #     `_delink_nav_links`：运行时通道 `api/rss.js` 要有它的 JS 端口
    #     （`lib/body_rules.js` 的 `delinkNavLinks`），而"逐条比输出"的对账判据需要一个
    #     能与 JS 端口一一对应的 Python 函数 —— 长在 `_deep_clean_html` 里面没法单独喂样本。
    #     抽取**没有**改变清洗顺序（还是 2.5 这一格）、没有改判定，完整理由见它的 docstring。
    text = _delink_nav_links(text)
    # 2.6 空锚点 / 站点根链接（"阅读更多""返回首页"那类壳）整枚去掉。
    #     只吃 `href="#"` 与 `href="/"` 两种**精确**形状：`href="#sec-2"` 是有内容的页内锚点，
    #     放宽成 `href="#[^"]*"` 会把正文里的"见下节"一起删掉（判据
    #     test_in_page_fragment_link_is_not_dropped）。放在规则 3 之前 —— 这两把刀删空的
    #     `<p><a href="#">…</a></p>` 壳要由规则 3 收尾。
    text = re.sub(r'<a\b[^>]*href="(?:#|/)"[^>]*>[\s\S]*?</a>', '', text, flags=re.IGNORECASE)
    # 3. 移除清洗后残留的空块元素
    text = re.sub(
        r'<(?:p|div|span)\b[^>]*>\s*(?:<br\s*/?>\s*)*</(?:p|div|span)>',
        '', text, flags=re.IGNORECASE)
    # 4. 压缩连续空行（保留段落间距）
    text = re.sub(r'(?:\s*\n){3,}', '\n\n', text)
    return text.strip()


# ── hnrss 模板摘要重写（Task 8）─────────────────────────────────────
# hnrss 的 <description> 不是摘要，是四行投稿元数据：
#     Article URL: …\nComments URL: …\nPoints: 254\n# Comments: 162
# 运行时翻译后还有中文变体（文章网址/评论网址/积分/评论数）。整段进阅读器就是用户报的
# "全是链接"。判据按**形状**而非 source_key：源改名、`hn_newest_56` 那类新增镜像、
# 翻译版都要命中；按 key 判等于把"HN 这个源"写死进清洗层。
# 四条正则都整行锚定（^…$）：正文里的 "reports Points: 254 as the best score" 不是模板行。
# 冒号两可：半角 ':' 与全角 '\uFF1A'（中文变体用的是后者）。这里**必须**写码位转义而不是
# 直接贴字形 —— 全角冒号在编辑器里与半角看不出区别，brief 那行抄进文件时就悄悄变成了两个
# 半角冒号，中文版模板从此永不命中（判据 test_zh_translated_variant_also_matched 是当场抓到的）。
# 数字段写 `[0-9]` 而不是 `\\d`（R43 对账口径）：Python 的 `\\d` 在 str 模式里是 **Unicode**
# 十进制数字（`Points: ２５４` 全角数字也算命中），而 JS 端口按裁定只用 ASCII；
# 两侧统一成 ASCII 才对得上（对账语料里有全角数字那一条，用它钉住这个口径）。
#
# ── R45：词表来自真语料清点，不是凭想象 ────────────────────────────────
# 原来每个槽位只收了一个中文词（`评论数`），而运行时翻译层对同一槽位会给出一族同义词，
# 实测最大缺口就是 `# 评论: 0`（半角冒号、词是"评论"不是"评论数"，18,037 条里 551 行）。
# 下面四张词表是把 `rss_history.json` 的 `summary`/`summary_zh` **逐行按行首形态聚合**数出来的，
# 收词只有两条依据：
#   ① 该词在 HN 条目的模板槽位上真实出现 ≥1 次（没有词是凭空加的）；
#   ② 该词在全部 18,037 条的**非 HN 条目**上命中数为 0（判据
#      test_hn_word_list_has_no_non_hn_surface 拿真语料复核这条，防的是"得分/热度"
#      这类通用词把游戏评测正文的 `得分：9` 当成模板行）。
# 长词排在前只为了读起来顺，判决靠整行锚定与回溯，不靠顺序。
# 两条**刻意没收**的形态（收它们要动的不是词表而是值段语义，属于"为多剥而放宽"）：
#   `评论数：1条`（数量词后缀，实测 2 行）；被 `_truncate` 截断成单行的模板块（实测 21 行，
#   门槛"至少两行"本就该放它过去 —— 判据 test_truncated_single_line_block_is_left_alone 钉住）。
_HN_ART_WORDS = (u"article url", u"文章网址", u"文章链接", u"文章URL", u"文章地址")
_HN_CMT_WORDS = (u"comments url", u"评论网址", u"评论区链接", u"评论区网址", u"评论链接",
                 u"评论URL", u"评论地址", u"评论页面", u"讨论区链接", u"讨论链接")
_HN_PTS_WORDS = (u"points", u"积分", u"当前得分", u"得分", u"评分", u"点赞数",
                 u"热度值", u"热度", u"分数", u"点数", u"分值", u"关注度")
_HN_CMTS_WORDS = (u"comments", u"评论数量", u"评论数", u"评论")


def _hn_alt(words):
    """词表 → 非捕获交替式。每个词都过 `re.escape`：词表是从不可信的翻译产物里清出来的，
    哪天混进 `.`/`+`/`(` 也不该把整条行的锚定炸掉（那会把"锚定"这件事静默降级成 search）。"""
    return u"|".join(re.escape(w) for w in words)


_HN_ART_URL = re.compile(r"^(?:%s)\s*[:\uFF1A]\s*(\S+)\s*$" % _hn_alt(_HN_ART_WORDS), re.IGNORECASE)
_HN_CMT_URL = re.compile(r"^(?:%s)\s*[:\uFF1A]\s*(\S+)\s*$" % _hn_alt(_HN_CMT_WORDS), re.IGNORECASE)
_HN_POINTS = re.compile(r"^(?:%s)\s*[:\uFF1A]\s*([0-9]+)\s*$" % _hn_alt(_HN_PTS_WORDS), re.IGNORECASE)
_HN_COMMENTS = re.compile(r"^#?\s*(?:%s)\s*[:\uFF1A]\s*([0-9]+)\s*$" % _hn_alt(_HN_CMTS_WORDS), re.IGNORECASE)
# R47：上游翻译层会把 `Article URL:` / `Comments URL:` 的**标签整个丢掉**，于是进来的正文
# 就是 `https://… + https://… + 积分：2 + # 评论：0` 这一形：四槽刀照旧开火（积分与评论两行
# 就够门槛），那两行 URL 却以正文行的身份留在摘要里 —— 用户报的原症状「HN 里全是链接」到这一
# 步只清了一半（实测 rss_history.json 18,037 条：HN 形状出口仍剩 5 行裸 URL、涉及 4 个字段；
# 实时 `?source=` 通道每场都会把它们重新长出来）。
# **只在已判定为 HN 形状的那条分支里动手**（门槛已过、pts 或 cmt 必有一格），判据是「整行就是
# 一个 `http(s)://…`」。值段限 **ASCII 可打印** `[\x21-\x7e]` 而不是 `\S`，与 `_GLUED_URL_RE`
# 同一个口径：`\S` 会把 `https://ex.com/a看了大佬的文章` 这种 **URL 与正文粘连**的行也算成
# 「整行裸 URL」一口吃掉 —— 本刀派工初版就踩过这一格，是
# test_order_matters_when_meta_lines_come_before_the_glued_line 当场抓成回归的。URL 里带 CJK
# 的少数形态宁可少剥一条，不少剥错一行。
# 通用摘要一条都不许剥：很多源的正常摘要就是单独一行链接，真语料里「刀没开火」的字段整整放着
# 38 行这一形（值段改用 `\S` 去数是 52 行）—— 做成通用剥离就是在吃这些正文，反例判据
# test_bare_url_line_from_an_ordinary_summary_is_kept 钉住这条边界。
_HN_BARE_URL_LINE = re.compile(r"^https?://[\x21-\x7e]+$", re.IGNORECASE)


def _rewrite_hn_summary(desc, link):
    """把 hnrss 的模板元数据压成一行，正文文字一个字不动、裸链接一条不留。

    门槛是"**至少命中两行**、且必须含 Points 或 Comments URL"：单行 `Points: 3` 不足以
    判定，否则任何英文文章的评分句都会被当成模板吃掉（判据
    test_one_meta_line_alone_is_not_enough / test_prose_mentioning_points_is_not_eaten）。
    R47 补的第二件事也挂在这条分支里：标签被翻译层丢掉时，那两行 URL 是以正文身份进来
    的，门槛过后一并剥掉（`_HN_BARE_URL_LINE`）；门槛没过者一个字都不动。
    Points 与评论数的**数字保留**，Article/Comments URL 两行删掉不丢信息 —— 条目本身的
    `link` 就是 Article URL（讨论页入口由前端 a.u 承担，见 Task 9 的 `_isHnDiscussion`）。
    正文按行原样保留（含段落空行），元数据行追加在最后。
    幂等：重写结果里不再有模板行，过第二把刀返回原值 —— 快照出口对 `summary` 与
    `summary_zh` 两条链路都会调，不幂等就会把"254 分"再吃一遍。
    """
    if not desc:
        return desc
    art = cmt = pts = cmts = ""
    keep = []
    for ln in desc.replace("\r\n", "\n").split("\n"):
        ln = ln.strip()
        if not ln:
            if keep:
                keep.append(ln)
            continue
        m = _HN_ART_URL.match(ln)
        if m:
            art = m.group(1)
            continue
        m = _HN_CMT_URL.match(ln)
        if m:
            cmt = m.group(1)
            continue
        m = _HN_POINTS.match(ln)
        if m:
            pts = m.group(1)
            continue
        m = _HN_COMMENTS.match(ln)
        if m:
            cmts = m.group(1)
            continue
        keep.append(ln)
    while keep and not keep[-1]:
        keep.pop()
    hits = (1 if art else 0) + (1 if cmt else 0) + (1 if pts else 0) + (1 if cmts else 0)
    if hits < 2:
        return desc
    if not (pts or cmt):
        return desc
    # R47：走到这里就是"已判定为 HN 形状"，此时才剥整行裸 URL；门槛没过的那条分支在上面就
    # return 了，通用摘要因此一条都碰不到。剥完再退一次尾部空行（正文后面跟一个空行再跟
    # 一条裸 URL 那种形状留下的空洞），否则每次构建对同一字段重写都会多/少一个空行，
    # 幂等口径（快照出口每场重跑）就破了。
    keep = [ln for ln in keep if not _HN_BARE_URL_LINE.match(ln)]
    while keep and not keep[-1]:
        keep.pop()
    meta = []
    if pts:
        meta.append("%s 分" % pts)
    if cmts:
        meta.append("%s 评论" % cmts)
    meta.append("Hacker News")
    meta_line = " · ".join(meta)
    return "\n".join(keep + [meta_line]) if keep else meta_line


# "链接+正文"粘连：URL 后**紧跟**汉字/假名（无空白分隔）。NodeSeek 这类 feed 的 description 形状
# （实测 nodeseek_54：`https://www.nodeseek.com/post-957723-1看了大佬的IX文章…`）。
# URL 字符类用 `[\x21-\x7e]`（可打印 ASCII、无空白）而不是 brief 原样的 `\S`：
# `\S` 会把紧跟在 URL 后面的全角逗号/句号也当 URL 字符吃掉（`…com/a，这是正文` 会连标点一起剥），
# 而汉字/全角标点本来就不是这条 URL 的组成部分 —— 判据
# test_url_followed_by_fullwidth_punctuation_is_not_glued / test_url_followed_by_newline_is_not_glued 钉住。
# 先行断言是**唯一**动手条件：有空白/换行分隔、整条就是 URL、链接出现在句中，一律原样返回。
_GLUED_URL_RE = re.compile(r"^\s*https?://[\x21-\x7e]+?(?=[\u4e00-\u9fff\u3040-\u30ff])")


def _strip_glued_url(text):
    """剥掉摘要开头与正文粘连的裸链接。

    只在"URL 后紧跟汉字"这一种形状上动手：有空白分隔、整条就是 URL、
    或链接出现在句中，都原样返回（判据里三条反例就是为了挡住过度匹配）。

    幂等（剥完的串以汉字开头，再过一次返回原值）—— 这不是洁癖：本刀接在**解析入口**
    （`summary` 出厂即规范）与**快照 `s` 出口**（`summary_zh` 是上一场翻译的缓存、跨场复用、
    不会被重新解析）两处，同一条链路会过两遍，与 Task 8 的 `_rewrite_hn_summary` 同形。
    `link` 参数不参与判决：粘连与否只看 `text` 自己的形状，与条目链接是否同源无关。
    """
    if not text:
        return text
    m = _GLUED_URL_RE.match(text)
    if not m:
        return text
    # 不写 brief 里那句 `.lstrip()`：匹配终点按构造就是**汉字**，剥完的串第一个字符必是汉字，
    # 永远没有前导空白可退 —— 那是条任何判据都打不到的死分支（本批变异 ④d 实测它存活），
    # 留着只会让下一批以为"退空白"这件事有判据在守。
    return text[m.end():]


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

# RFC 822 字母时区（小时偏移）。注意 CST 在 RFC 822 中定义为 -0600（美国中部）；
# 中国的 CST(+0800) 在真实 feed 里一律写作 +0800 / GMT+0800，不靠裸 "CST" 表达。
_RSS_TZ_NAMED = {"UT":0,"UTC":0,"GMT":0,"Z":0,
                 "EST":-5,"EDT":-4,"CST":-6,"CDT":-5,
                 "MST":-7,"MDT":-6,"PST":-8,"PDT":-7}

# 覆盖真实世界出现过的 RSS pubDate 写法（2026-09-14 实测样本见 tests/rss_date/）：
#   "Fri Oct 27 2023 15:13:00 GMT+0800 (China Standard Time)"   ← JS Date.toString()，月在前
#   "13 Sep 2026 07:28:00 EST"                                  ← 日在前 + 字母时区，无星期前缀
#   "Mon, 18 May 2020"                                          ← 仅日期
#   "Mon, 14 Sep 2026 01:18:00 GMT"                             ← 标准 RFC 822
_RSS_DATE_RE = re.compile(
    r"^(?:\w{3,9},?\s+)?"                                       # 可选星期前缀（Mon, / Monday ）
    r"(?:"
    r"(?P<d1>\d{1,2})\s+(?P<m1>[A-Za-z]{3,9})"                  # 词序 A：日 月
    r"|"
    r"(?P<m2>[A-Za-z]{3,9})\.?\s+(?P<d2>\d{1,2}),?"             # 词序 B：月 日（可带点/逗号）
    r")"
    r"\s+(?P<y>\d{4})"                                          # 年
    r"(?:\s+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?"    # 可选时间（秒可省略）
    # 时区：四位偏移(+0800/+08:00)、**裸两位偏移(+08/-05，plink.anyfeeder 就这么写)**、
    # 或字母时区(GMT/CST/UT)。少了两位那一支会让整串匹配失败 —— 日期明明是真实存在的，
    # 却被判成"上游没给日期"，进而降级去靠 first_seen 撑龄期（2026-09-21 超能网 68/68 的真相）。
    r"(?:\s*(?:GMT|UTC|UT)?\s*(?P<tz>[+-]\d{1,2}(?::?\d{2})?|[A-Za-z]{2,4}))?"
    r"(?:\s*\(.*\))?$"                                          # 忽略 "(China Standard Time)"
)


def _rss_tz_offset(tz):
    """把时区串变成 timedelta。无法识别时保守返回 UTC（宁可靠近而非丢弃）。"""
    if not tz:
        return datetime.timedelta(0)
    if tz[0] in "+-":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        try:
            hh = int(digits[:2])
            mm = int(digits[2:4]) if len(digits) >= 4 else 0
        except ValueError:
            return datetime.timedelta(0)
        return datetime.timedelta(hours=sign * hh, minutes=sign * mm)
    hours = _RSS_TZ_NAMED.get(tz.upper())
    if hours is None:
        return datetime.timedelta(0)
    return datetime.timedelta(hours=hours)


def _parse_rss_date(s):
    s = (s or "").strip()
    if not s:
        return None
    m = _RSS_DATE_RE.match(s)
    if m:
        mon = _RSS_MONTHS.get((m.group("m1") or m.group("m2"))[:3].title())
        if mon:
            h, mi, sec = int(m.group("H") or 0), int(m.group("M") or 0), int(m.group("S") or 0)
            try:
                return datetime.datetime(int(m.group("y")), mon, int(m.group("d1") or m.group("d2")),
                                         h, mi, sec, tzinfo=datetime.timezone(_rss_tz_offset(m.group("tz"))))
            except ValueError:
                return None
    return _parse_iso(s)


_BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _detect_uniform_dates(items, min_items=5, threshold=0.8):
    """检测同日期伪造（bad_date 模式 C）。

    同一源 ≥min_items 条条目中，超过 threshold 比例共享完全相同的 pub_date 字符串
    → 判定为日期伪造，返回该源 key 集合。

    典型场景：知乎日报 anyfeeder feed 无 pubDate，_fallback_date_iso 用 first_seen
    填充 → 同批次所有条目获得完全相同的时间戳。
    """
    from collections import Counter, defaultdict
    src_dates = defaultdict(list)
    for item in items:
        sk = item.get("source_key", "")
        pd = item.get("pub_date", "")
        if sk and pd:
            src_dates[sk].append(pd)

    flagged = set()
    for sk, dates in src_dates.items():
        if len(dates) < min_items:
            continue
        cnt = Counter(dates)
        most_common_date, most_common_count = cnt.most_common(1)[0]
        ratio = most_common_count / len(dates)
        if ratio >= threshold:
            flagged.add(sk)
    return flagged


def _parse_hist_dt(s):
    """历史/输出里的时间串 → 北京时间 aware datetime（裸值按北京时间理解）。

    历史文件里 pub_date 可能是 aware（+08:00 / Z）也可能是裸值，
    历史上各处各写一份解析逻辑（至少 4 处），口径容易漂移 —— 统一到这里。
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d.replace(tzinfo=_BJ_TZ) if d.tzinfo is None else d.astimezone(_BJ_TZ)


def _first_seen_iso(entry):
    """first_seen（我方首次收录该链接的时刻）的 ISO 形式，解析失败返回空串。"""
    d = _parse_hist_dt(entry.get("first_seen"))
    return d.isoformat() if d else ""


def _fill_undated_from_capture(item):
    """出厂副本缺 pub_date 时改用我方收录时刻并打降级标记，返回是否改动。

    为什么需要这一句：历史条目的降级在合并循环里做（entry["pub_date"] = _fb），
    但同一个链接**当次又抓到**时走的是 `continue` —— 出厂的是抓取字典那份，
    它从没被兜过底。生产实测这样有 9 条卡片 pub_date 与 time_str 双空，
    连续两场是同一批链接（不是"本轮新增、下场自愈"）。

    只改出厂副本、绝不写回历史的 pub_date：_retention_ref_dt 的推导前提是
    「历史里没有 pub_date 就等于出口那份是降级键」，写回去等于让收录时刻去套
    源级时区偏移（二次校正），那正是 P1-3 实测过 68h → 76h 的形态。
    """
    if item.get("pub_date"):
        return False
    iso = _first_seen_iso(_rss_history.get(item.get("link", "")) or {})
    if not iso:
        return False
    item["pub_date"] = iso
    item["date_fallback"] = True
    return True


def _load_pub_date_offsets(sources=None):
    """读取源配置里**声明**的 pub_date 时区校正量（单位：分钟；负值 = 把 pub_date 往前挪）。

    为什么是声明式而非自动推断：物理约束（我们不可能在文章发布前抓到它）只能给出
    偏移的**下界** `need = max(first_seen - pub_date)`，而满足下界的合法时区偏移往往不止一个。
    实测超能网 need = 444.7min 时，480 / 525 / 570min 全都物理可行 —— 统计上无法唯一确定。
    按「最小候选」猜会静默改写日期，属于数据污染风险，故改由配置显式声明；
    自动检测降级为**发现器**（见 _detect_pub_date_anomalies），只报告不落库。
    """
    out = {}
    for s in (sources if sources is not None else RSS_SOURCES):
        if not isinstance(s, dict):
            continue
        try:
            v = int(s.get("pub_date_offset_min") or 0)
        except (TypeError, ValueError):
            continue
        if v:
            out[s.get("key")] = v
    return out


def _shift_iso_minutes(iso_str, minutes):
    """把 ISO 时间串按分钟平移（负值 = 往前）。解析失败原样返回（不制造新数据）。"""
    d = _parse_hist_dt(iso_str)
    if d is None:
        return iso_str
    return (d + datetime.timedelta(minutes=minutes)).isoformat()


def _effective_pub_dt(item, offsets=None):
    """条目**校正后**的发布时间（北京时间 aware）。声明了偏移的源按偏移平移。

    合并 / 72h 裁剪 / 审计 / 输出四处都必须用本函数，否则会出现多套口径
    （2026-09-14：构建期切块曾用字符串比较、审计用时区安全比较，导致选片错位）。
    """
    d = _parse_hist_dt(item.get("pub_date"))
    if d is None:
        return None
    off = (offsets if offsets is not None else _load_pub_date_offsets()).get(item.get("source_key"))
    if off:
        d = d + datetime.timedelta(minutes=off)
    return d


# pub_date 晚于 first_seen 超过该阈值 → 判为「已被证伪」。
# 容忍 1 小时两端时钟漂移；超过即说明我们「在文章发布之前抓到了它」，物理上不可能。
FUTURE_DATE_TOLERANCE_MIN = 60


def _pub_date_falsified(entry, offsets=None):
    """pub_date 是否已被物理事实证伪（晚于 first_seen 超过容忍阈值）。

    注意与「无 pub_date」区分：那是缺失，走既有降级路径；
    这里是**有一个明确错误的值**，不能拿来排序（否则条目「出生于未来」，
    前端钳制会把多条折叠成同一时刻 → 整块钉在首屏顶部）。
    """
    fs = _parse_hist_dt(entry.get("first_seen"))
    pd = _effective_pub_dt(entry, offsets)
    if fs is None or pd is None:
        return False
    return (pd - fs) > datetime.timedelta(minutes=FUTURE_DATE_TOLERANCE_MIN)


def _plan_falsify_shifts(history, offsets=None, src_keys=None):
    """为「将被证伪、须改用收录时刻」的条目规划保序回拉位移量（分钟，<=0），返回 {link: shift}。

    为什么不能统统写成同一个 first_seen：
      first_seen 是我方**按抓取批次**打的时间戳 —— 实测全量历史 8411 条只有 122 个取值，
      最大一个批次覆盖 1857 条。把一批被判伪的条目统统赋成同一个 first_seen，
      这一批就退化成「同一时刻」；前端按时间降序排完就是一大块同源同刻卡片，
      与 2026-09-14 用户报告的「同刻扎堆」是同一形态（只是源从超能网换成了 arxiv/product_hunt）。
      实测：60min 阈值下 101 条判伪里有 68 条落在「同批次多条」上，最大单组 27 条
      （arxiv_ai_4）、次大 24 条（product_hunt_797）——即一次修好、一次又造出来。

    对策（与前端 L3 钳制同一语义：**保序回拉**，不是折叠）：
      保留 feed 自身的相对先后（若干条目间的声称时间差是有信息的），整批后移，
      使**最新的一条**恰好落在 first_seen，其余按其原间隔往前排开。
    边界：
      · 只处理「有 pub_date 且被证伪」的条目；本来就无日期者没有相对信息，保持原行为；
      · 任何一条都不得早于 cutoff —— 72h 窗口是用户可见契约，不允许被位移越界。
    """
    groups = {}
    for link, item in history.items():
        sk = item.get("source_key")
        if src_keys is not None and sk not in src_keys:
            continue
        if not item.get("pub_date"):
            continue
        fs = _parse_hist_dt(item.get("first_seen"))
        if fs is None or not _pub_date_falsified(item, offsets):
            continue
        pd = _effective_pub_dt(item, offsets)
        groups.setdefault((sk, item.get("first_seen") or ""), []).append(
            (link, (pd - fs).total_seconds() / 60.0))

    shifts = {}
    for arr in groups.values():
        newest_adv = max(a for _l, a in arr)   # 组内最「未来」的那条 → 锚到 first_seen
        for link, adv in arr:
            shifts[link] = -int(round(newest_adv - adv))
    return shifts


def _detect_pub_date_anomalies(history, offsets=None):
    """发现器：找出「pub_date 系统性晚于 first_seen」的源，只报告不修改数据。

    返回 [{key, n, inverted_ratio, min_delta_min, median_delta_min, need_min, kind, suggest_min}]：
      kind='tz'    → 物理可行的最小前移量落在真实时区范围内 ⇒ 可能是时区标注错误，
                     suggest_min 给出最小的合法候选（供人工确认后写进源配置）
      kind='other' → 需要前移几十小时，远超任何真实时区 ⇒ 不是时区问题
                     （典型是播客/Newsletter 的「预约发布」，或源侧日期本身错乱），不给建议

    为什么只报告不落库：见 _load_pub_date_offsets 的说明（物理约束只给下界，可能多解）。
    """
    buckets = {}
    for item in history.values():
        sk = item.get("source_key")
        pd = _effective_pub_dt(item, offsets)
        fs = _parse_hist_dt(item.get("first_seen"))
        if not sk or pd is None or fs is None:
            continue
        buckets.setdefault(sk, []).append((fs - pd).total_seconds() / 60.0)

    out = []
    for sk, deltas in buckets.items():
        if sk in (offsets or {}) or len(deltas) < TZ_ANOMALY_MIN_ITEMS:
            continue
        inverted = sum(1 for d in deltas if d < -10)
        ratio = inverted / float(len(deltas))
        if ratio < TZ_ANOMALY_INVERT_RATIO:
            continue
        srt = sorted(deltas)
        need = -srt[0]
        kind, suggest = "other", None
        if need <= TZ_MAX_OFFSET_MIN:
            cands = [c for c in _TZ_LEGAL_OFFSETS_MIN if c >= need - 1e-9]
            if cands:
                kind, suggest = "tz", -cands[0]
        out.append({
            "key": sk, "n": len(deltas), "inverted_ratio": ratio,
            "min_delta_min": srt[0], "median_delta_min": srt[len(srt) // 2],
            "need_min": need, "kind": kind, "suggest_min": suggest,
        })
    out.sort(key=lambda x: -x["n"])
    return out


TZ_ANOMALY_MIN_ITEMS = 8       # 少于此条数不足以判断「系统性」
TZ_ANOMALY_INVERT_RATIO = 0.9  # 倒挂率门槛（arxiv 51.7% / product_hunt 80% 均被排除）
TZ_MAX_OFFSET_MIN = 840        # +14:00 是现实中最大的 UTC 偏移；超过即不可能是时区标注错误
# 现实在用的 UTC 偏移（分钟）。不能只取「任何 0.5h 倍数」：+7:30 已无地区使用，
# 若把它列进候选，「最小候选」规则会把 超能网 的 8h 误判成 7.5h。
_TZ_LEGAL_OFFSETS_MIN = tuple(sorted(set(
    [h * 60 for h in range(-12, 15)] + [330, 345, 390, 525, 570, 630, 765, -210, -570])))


def _fallback_date_iso(entry):
    """条目拿不到 pub_date 时的降级排序键 = first_seen（我方首次收录该链接的真实时刻）。

    为什么必须给一个键：空 pub_date 会让条目在默认排序里沉底 → 用户永远看不到
    （2026-09-14 实测：201 条因此不可见）。降级键只负责「让它按时间参与排序」，
    语义上不等于发布时间，因此调用方需用 date_fallback 标记，前端据此外显区分。
    """
    if entry.get("pub_date"):
        return ""
    return _first_seen_iso(entry)


# 相对时间标签的唯一口径已移到 lib/rel_time.js（构建期不再烘 time_str）。
# 参照快照见 tests/rss_history/goldens/rel_time_labels.json；
# 页面与快照的对等判据见 tests/rss_history/test_time_label_parity.py。

# 这里刻意不留 Python 版实现：两条口径并存迟早只被改到一条。


def _detect_lang(text):
    """检测文本主要语言，返回 MyMemory langpair 代码。"""
    hiragana = sum(1 for c in text if '\u3040' <= c <= '\u309f')
    katakana = sum(1 for c in text if '\u30a0' <= c <= '\u30ff')
    hangul = sum(1 for c in text if '\uac00' <= c <= '\ud7af')
    arabic = sum(1 for c in text if '\u0600' <= c <= '\u06ff')
    cyrillic = sum(1 for c in text if '\u0400' <= c <= '\u04ff')
    latin = sum(1 for c in text if 'a' <= c.lower() <= 'z')
    total = max(len(text), 1)
    if (hiragana + katakana) / total > 0.1:
        return 'ja'
    if hangul / total > 0.1:
        return 'ko'
    if arabic / total > 0.1:
        return 'ar'
    if cyrillic / total > 0.1:
        return 'ru'
    if latin / total > 0.3:
        return 'en'
    return 'zh-CN'


def _agnes_translate(text, timeout=20):
    """Agnes AI 翻译（OpenAI 兼容接口，agnes-2.5-flash）。失败返回 None。"""
    global _AGNES_BLOCK_UNTIL, _AGNES_OFFENSES, _AGNES_EMPTY_STREAK, _AGNES_KEY_IDX, _AGNES_429_STREAK
    # 罚期闸放在入口：以前只有调用方 _translate_to_zh 记得查，函数自己照发请求
    # ⇒ 任何新调用点都会绕过退避。停手必须是函数的默认行为，不是调用者的自觉。
    if _AGNES_BLOCK_UNTIL > time.time():
        return None
    payload = json.dumps({
        "model": "agnes-2.5-flash",
        "messages": [
            {"role": "system", "content": "你是翻译引擎。把用户输入翻译成简体中文，只输出译文，不要解释。"},
            {"role": "user", "content": text[:1500]},
        ],
        "max_tokens": 400,
        "temperature": 0.2,
        # 思考型模型：关闭思考避免 token 被推理耗尽，也加速响应
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://apihub.agnes-ai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _AGNES_KEYS[_AGNES_KEY_IDX],
            "User-Agent": "starhub-auto-update",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or None
        with _TRANS_LOCK:
            if out:
                _AGNES_OFFENSES = 0
                _AGNES_EMPTY_STREAK = 0
                _AGNES_429_STREAK = 0      # 任一 key 成功就不该把早先的 429 攒成退避
            else:
                # 200 但空 content（模型对特定输入的拒绝/截断）：连续 3 次视为端点异常入罚期
                _AGNES_EMPTY_STREAK += 1
                empty_hit = _AGNES_EMPTY_STREAK >= _AGNES_EMPTY_LIMIT
                if empty_hit and _AGNES_BLOCK_UNTIL <= time.time():
                    block_s = min(300 * (2 ** _AGNES_OFFENSES), 3600)
                    _AGNES_BLOCK_UNTIL = time.time() + block_s
                    _AGNES_OFFENSES += 1
                    _AGNES_EMPTY_STREAK = 0
                    print("[翻译] Agnes 连续 %d 次空响应，暂停直连 %d 分钟" % (_AGNES_EMPTY_LIMIT, block_s // 60), file=sys.stderr)
        return out
    except urllib.error.HTTPError as e:
        # 429 限流：先轮转到下一个 key；**轮完一整圈仍全 429** 才记一次罚期
        # （旧写法只轮转、从不记账 ⇒ 所有 key 一起被限流时，每条待翻文本都再撞一圈墙）
        if e.code == 429 and len(_AGNES_KEYS) > 1:
            with _TRANS_LOCK:
                _AGNES_KEY_IDX = (_AGNES_KEY_IDX + 1) % len(_AGNES_KEYS)
                _AGNES_429_STREAK += 1
                cycled = _AGNES_429_STREAK >= len(_AGNES_KEYS)
                if cycled:
                    _AGNES_429_STREAK = 0
                    if _AGNES_BLOCK_UNTIL <= time.time():
                        block_s = min(300 * (2 ** _AGNES_OFFENSES), 3600)
                        _AGNES_BLOCK_UNTIL = time.time() + block_s
                        _AGNES_OFFENSES += 1
                        print("[翻译] Agnes %d 个 key 全限流，暂停直连 %d 分钟"
                              % (len(_AGNES_KEYS), block_s // 60), file=sys.stderr)
            if not cycled:
                print("[翻译] Agnes 429，轮转到 key[%d]" % _AGNES_KEY_IDX, file=sys.stderr)
            return None
        # 其他 HTTP 错误 / 全部 key 已轮转完：入账本罚期
        with _TRANS_LOCK:
            if _AGNES_BLOCK_UNTIL > time.time():
                return None
            block_s = min(300 * (2 ** _AGNES_OFFENSES), 3600)
            _AGNES_BLOCK_UNTIL = time.time() + block_s
            _AGNES_OFFENSES += 1
            print("[翻译] Agnes HTTP %d，暂停直连 %d 分钟" % (e.code, block_s // 60), file=sys.stderr)
        return None
    except Exception:
        return None


def _zen_call_model(text, model, timeout, auth):
    """单模型单鉴权调用。返回 (译文|None, 是否自封该模型, 是否鉴权类失败 401/403)。"""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是翻译引擎。把用户输入翻译成简体中文，只输出译文，不要解释。"},
            {"role": "user", "content": text[:1500]},
        ],
        "max_tokens": 400,
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(_ZEN_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + auth,
        # 免费档要求 OpenCode 客户端身份，session/request 为每次调用生成的唯一 ID
        "User-Agent": "opencode/1.15.0 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.13",
        "x-opencode-client": "cli",
        "x-opencode-project": "global",
        "x-opencode-request": "msg_" + uuid.uuid4().hex,
        "x-opencode-session": "ses_" + uuid.uuid4().hex,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip() or None
        if out:
            with _TRANS_LOCK:
                _ZEN_TIMEOUT_STREAK[model] = 0
                _ZEN_MODEL_OFFENSES[model] = 0
            return out, False, False
        # HTTP 200 但空 content：与超时同账本，连续 2 次自封（防恒空模型零成本占用全池轮询）
        with _TRANS_LOCK:
            _ZEN_TIMEOUT_STREAK[model] = _ZEN_TIMEOUT_STREAK.get(model, 0) + 1
            streak = _ZEN_TIMEOUT_STREAK[model]
        return None, streak >= 2, False
    except urllib.error.HTTPError as e:
        # 任何 HTTP 错误都自封该模型（401/403=key 不被接受，429=限流，5xx=故障），避免逐文本反复撞墙
        return None, True, e.code in (401, 403)
    except Exception:
        # 超时/网络故障不计 HTTP 错误，但连续挂起同样拖死管线：连续 2 次自封该模型
        with _TRANS_LOCK:
            _ZEN_TIMEOUT_STREAK[model] = _ZEN_TIMEOUT_STREAK.get(model, 0) + 1
            streak = _ZEN_TIMEOUT_STREAK[model]
        return None, streak >= 2, False


def _zen_translate(text, timeout=25):
    """OpenCode Zen 免费模型轮询翻译：游标挑一个未自封的模型，HTTP 错误/连续超时自封该模型
    （指数退避 5→10→20→40 分钟封顶 1h，成功清零）并切换下一个；个人 key 撞 401/403 时
    当场回退 Bearer public 并粘性记住。全部自封返回 None。"""
    global _ZEN_MODEL_IDX, _ZEN_AUTH_STICKY
    with _TRANS_LOCK:
        auth = _ZEN_AUTH_STICKY
    fallback_auth = None
    if not auth:
        auth = _ZEN_KEY or "public"
        fallback_auth = "public" if auth != "public" else None
    for _attempt in range(len(_ZEN_MODELS)):
        model = None
        with _TRANS_LOCK:
            for _k in range(len(_ZEN_MODELS)):
                cand = _ZEN_MODELS[_ZEN_MODEL_IDX % len(_ZEN_MODELS)]
                _ZEN_MODEL_IDX += 1
                if _ZEN_MODEL_BLOCK.get(cand, 0) <= time.time():
                    model = cand
                    break
        if model is None:
            return None
        out, to_block, auth_fail = _zen_call_model(text, model, timeout, auth)
        if auth_fail and fallback_auth and not out:
            out, to_block, auth_fail = _zen_call_model(text, model, timeout, fallback_auth)
            if out:
                with _TRANS_LOCK:
                    _ZEN_AUTH_STICKY = fallback_auth
                print("[翻译] Zen key 被拒，粘性回退 Bearer public", file=sys.stderr)
        if out:
            return out
        if to_block:
            with _TRANS_LOCK:
                if _ZEN_MODEL_BLOCK.get(model, 0) > time.time():
                    pass  # 已在罚期内：同波并发失败只记一次违规（P1 竞态修复）
                else:
                    offenses = _ZEN_MODEL_OFFENSES.get(model, 0)
                    block_s = min(300 * (2 ** offenses), 3600)
                    _ZEN_MODEL_BLOCK[model] = time.time() + block_s
                    _ZEN_TIMEOUT_STREAK[model] = 0
                    _ZEN_MODEL_OFFENSES[model] = offenses + 1
                    print("[翻译] Zen %s 限流/故障，自封 %d 分钟并切换下一模型" % (model, block_s // 60), file=sys.stderr)
    return None


_ANSI_RE = re.compile(r"\u001b?\[[0-9;]{1,4}[A-Za-z]")
_URL_RE = re.compile(r"https?://\S+", re.I)
_META_STUB_RE = re.compile(
    r"^(article url|comments url|points|submitted by|# comments|permalink|story link)\b", re.I)
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))
_KANA_RANGES = ((0x3040, 0x309F), (0x30A0, 0x30FF))


def _in_ranges(ch, ranges):
    o = ord(ch)
    return any(a <= o <= b for a, b in ranges)


def _needs_translation(text):
    """这条文本该不该送去翻译（构建期口径，与运行时 JS 的 _needsTranslation 同规则）。

    旧口径 `cn_chars > len(text) * 0.3` 的分母是**整串长度**，所以中文里夹一串 `+` 或 ANSI
    残片（`[1m`、`[36m`）会把中文占比稀释到阈值以下 ⇒ 当成"需要翻译"真发请求；
    2026-10-01 探针实测：中文垃圾串、纯 URL 都各占用了一次外呼。
    现在先剥 ANSI/URL/CJK，再要求**存在非中文的字母**且中文不占多数，并跳过 HN 元数据样板。
    门槛刻意不是"有没有拉丁词"：谚文/西里尔/阿拉伯文一个拉丁字母都没有，用拉丁词做门槛会把它们
    一起跳过（我第一版就是这样，被 test_korean_still_sent 当场打死）。
    含平假名/片假名的日文一律要翻 —— 那是既有规则（原来用 has_kana 排除误跳），不能改坏。
    """
    if any(_in_ranges(c, _KANA_RANGES) for c in text):
        return True
    clean = _URL_RE.sub(" ", _ANSI_RE.sub(" ", text))
    clean = "".join(c for c in clean if not _in_ranges(c, _CJK_RANGES))
    other_letters = sum(1 for c in clean if c.isalpha())
    if not other_letters:
        return False                                  # 除中文外没有任何文字：数字/标点/纯符号/纯 URL
    cjk = sum(1 for c in text if _in_ranges(c, _CJK_RANGES))
    if cjk >= other_letters:
        return False                                  # 中文已占多数
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) >= 3:
        stubs = sum(1 for l in lines if _META_STUB_RE.match(l.strip()))
        if stubs * 2 >= len(lines):
            return False                              # 六成以上是 URL/点数样板，翻出来只有链接
    return True


def _translate_to_zh(text, timeout=TRANSLATE_TIMEOUT):
    """翻译降级链：Agnes → Google gtx → Zen 免费模型轮询 → MyMemory → dict-chrome（带缓存+熔断）。"""
    if not text:
        return ""
    # 先清理 HTML 标签
    text = _strip_html(text)
    if not text:
        return ""
    # 已经是中文为主 / 纯 URL / 元数据样板 → 跳过（日文除外：见 _needs_translation 里的 kana 规则）
    if not _needs_translation(text):
        _TRANS_STATS["skip"] += 1
        return text

    # 查缓存（使用 MD5 确保跨进程一致性）
    text_hash = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()
    if text_hash in _trans_cache:
        _TRANS_STATS["cache_hit"] += 1
        _trans_touch(text_hash)   # 命中要记账给 LRU，否则高频译文会先被裁掉
        return _trans_cache[text_hash]

    # 熔断：连续多次全端点失败后暂停请求，期间未命中缓存的文本直接返回原文
    global _TRANS_FAIL_STREAK, _TRANS_BLOCK_UNTIL
    if time.time() < _TRANS_BLOCK_UNTIL:
        _TRANS_STATS["fail"] += 1
        return text

    encoded = urllib.parse.quote(text[:500])

    # 0) Agnes AI（首选，免费但限流：429 后暂停直连，期间直接走后续端点）
    if _AGNES_KEYS and time.time() >= _AGNES_BLOCK_UNTIL:
        try:
            cand = _agnes_translate(text, timeout=timeout)
            if cand and len(cand) > len(text) * 0.2:
                _TRANS_STATS["agnes"] += 1
                _TRANS_FAIL_STREAK = 0
                _trans_cache[text_hash] = cand  # 写入缓存
                return cand
        except Exception:
            pass

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
                _TRANS_FAIL_STREAK = 0
                _trans_cache[text_hash] = result  # 写入缓存
                return result
    except Exception:
        pass

    # 1.5) OpenCode Zen 免费模型轮询（gtx 限流时的接力；单模型 429/5xx 自封 5 分钟并自动切换）
    try:
        cand = _zen_translate(text, timeout=25)
        if cand and len(cand) > len(text) * 0.2:
            _TRANS_STATS["zen"] += 1
            _TRANS_FAIL_STREAK = 0
            _trans_cache[text_hash] = cand  # 写入缓存
            return cand
    except Exception:
        pass

    # 2) MyMemory（自动检测源语言，避免硬编码 en 导致非英语源翻译质量差）
    try:
        src_lang = _detect_lang(text)
        url = "https://api.mymemory.translated.net/get?q=" + encoded + "&langpair=%s|zh-CN" % src_lang
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        result = data.get("responseData", {}).get("translatedText", "")
        if result and not result.startswith("MYMEMORY"):
            _TRANS_STATS["mymemory"] += 1
            _TRANS_FAIL_STREAK = 0
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
                _TRANS_FAIL_STREAK = 0
                _trans_cache[text_hash] = result  # 写入缓存
                return result
    except Exception:
        pass

    _TRANS_STATS["fail"] += 1
    with _TRANS_LOCK:
        _TRANS_FAIL_STREAK += 1
        if _TRANS_FAIL_STREAK >= 5:
            _TRANS_BLOCK_UNTIL = time.time() + 300
            print("[翻译熔断] 连续 %d 次全端点失败，暂停翻译请求 5 分钟" % _TRANS_FAIL_STREAK, file=sys.stderr)
    return text  # 翻译失败保留原文


def _translate_source_items(items):
    """翻译单个源的全部条目（在翻译线程池中执行）：仅翻译标题，摘要保留原文。附 ISO 日期。"""
    for it in items:
        it["title_zh"] = _translate_to_zh(it["title"]) if it["title"] else it["title"]
        # 摘要不再构建时翻译，前端按需走浏览器 GTX
        # 相对时间不再烘进条目（ITEM_DERIVED_FIELDS）：页面按 pub_date + BUILD_TS 现算
        # 保留 pub_date 用于前端时间线排序（转为 ISO 字符串）
        pd = it.get("pub_date")
        if pd and hasattr(pd, "isoformat"):
            it["pub_date"] = pd.isoformat()


# ──────────────────────────── RSS 抓取 ────────────────────────────

def _dedup_pub_dt(v):
    """把条目的 pub_date 归一成可比对的 aware datetime；取不到可靠时间返回 None。

    条目里的 pub_date 有三种形态：_parse_rss_date/_parse_iso 产出的 datetime、
    上游原样字符串、以及没有日期时的空串。按字符串统一处理会在 datetime 对象上
    调 .replace("Z", ...) —— datetime.replace 把它当位置参数，抛 TypeError。
    """
    if isinstance(v, datetime.datetime):
        d = v
    elif isinstance(v, str) and v.strip():
        d = _parse_iso(v) or _parse_rss_date(v)
    else:
        d = None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d


def _dedup_source_items(items, source_key):
    """单源内去重：URL 归一化 + 内容重复检测。

    处理三类重复：
    1. V2EX #replyN 锚点（同一帖子多回复）
    2. 微信公众号重复群发（同 __biz + 标题 + 摘要，不同 mid/sn）
    3. 通用 URL 变体（tracking 参数、fragment）

    微信公众号策略：标题+摘要都相同才视为重复（避免误删每日专栏）。
    其他源策略：标题归一化去重（48h 窗口保护每日专栏）。
    """
    if len(items) < 2:
        return items

    orig_count = len(items)

    # ── Pass 1: URL 归一化 ──
    for it in items:
        link = it.get("link", "")
        # V2EX: 剥离 #replyN
        if "v2ex" in source_key:
            link = re.sub(r"#reply\d+$", "", link)
        # 通用: 剥离尾部 fragment 和常见 tracking 参数
        link = re.sub(r"#(?!reply\d+$)[^#]*$", "", link)
        link = re.sub(r"[?&](utm_source|utm_medium|utm_campaign|utm_content|at_medium|at_campaign)=[^&]*", "", link)
        link = re.sub(r"[?&]$", "", link)
        it["link"] = link

    # ── Pass 2: 完全相同 URL 去重（保留首条）──
    seen_urls = {}
    url_deduped = []
    for it in items:
        link = it.get("link", "")
        if link and link in seen_urls:
            continue
        if link:
            seen_urls[link] = True
        url_deduped.append(it)
    items = url_deduped

    # ── Pass 3: 内容去重 ──
    # 检测是否微信源
    is_wechat = any("mp.weixin.qq.com" in (it.get("link", "") or "") for it in items[:5])

    def _norm_text(t):
        """归一化文本：去空白、小写、去标点"""
        t = re.sub(r"\s+", "", t).lower()
        t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
        return t[:80]

    def _wechat_biz(link):
        m = re.search(r"__biz=([A-Za-z0-9=]+)", link)
        return m.group(1) if m else ""

    if is_wechat:
        # 微信策略：标题+摘要都相同才视为重复
        seen_content = {}  # key → True
        content_deduped = []
        for it in items:
            title = (it.get("title") or "").strip()
            summary = (it.get("summary") or "").strip()
            link = it.get("link", "")

            if not title:
                content_deduped.append(it)
                continue

            # 去重键：__biz + 归一化标题 + 归一化摘要
            dedup_key = _wechat_biz(link) + "|" + _norm_text(title) + "|" + _norm_text(summary)

            if dedup_key in seen_content:
                continue  # 标题+摘要都相同，真重复

            seen_content[dedup_key] = True
            content_deduped.append(it)
        items = content_deduped
    else:
        # 其他源：标题归一化去重（48h 窗口保护）
        def _norm_title(t):
            t = re.sub(r"\s+", "", t).lower()
            t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
            return t[:50]

        seen_titles = {}  # 归一化标题 → 已保留的同名条目列表
        title_deduped = []
        for it in items:
            title = (it.get("title") or "").strip()
            if not title:
                title_deduped.append(it)
                continue

            dedup_key = _norm_title(title)
            kept = seen_titles.setdefault(dedup_key, [])
            cur_dt = _dedup_pub_dt(it.get("pub_date", ""))
            if cur_dt is not None:
                # 与任一已保留的同名条目相差 48h 以内即视为重复；超出则当作另一篇
                if any(abs((cur_dt - d).total_seconds()) <= 48 * 3600
                       for d in (_dedup_pub_dt(k.get("pub_date", "")) for k in kept)
                       if d is not None):
                    continue
            else:
                # 时间取不到时退回「标题 + 摘要都相同」才算重复群发：与微信源同一口径。
                # 一律保留会让重复条目挤占 ITEMS_PER_SOURCE 名额、把真条目挡在外面；
                # 一律折叠又会并掉每日专栏（标题同、内容不同）。
                _sum = _norm_text(it.get("summary") or "")
                if any(_norm_text(k.get("summary") or "") == _sum for k in kept):
                    continue
            kept.append(it)
            title_deduped.append(it)
        items = title_deduped

    removed = orig_count - len(items)
    if removed > 0:
        print("[去重] %s: %d → %d 条（移除 %d 条重复）"
              % (source_key, orig_count, len(items), removed))

    return items


def _fetch_url(url, timeout=FETCH_TIMEOUT, accept=None, ua=None):
    headers = dict(UA)
    if ua:
        # 少数站点按 UA 拦（Engadget 的 CloudFront 对 Chrome 串稳定 403、对 curl 串 200），
        # 这类源在 rss_sources.json 里带一个 ua 字段单独覆盖，不动全局默认。
        headers["User-Agent"] = ua
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_FEED_BYTES).decode("utf-8", errors="replace")


def _fetch_rss(source, timeout=None):
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
        raw = _fetch_url(url, timeout=timeout or FETCH_TIMEOUT, accept="application/rss+xml, application/xml, text/xml, application/atom+xml",
                         ua=source.get("ua"))
    except Exception as ex:
        # 429 原样抛出：它不是"这个源坏了"，是"这个域被限速了"，
        # 一律折成 None 会让它和断连/403 在状态层同形，进而被域熔断连坐（台账 §21）。
        if getattr(ex, "code", None) == 429:
            raise
        print("[RSS聚合] %s 拉取失败: %s" % (name, ex), file=sys.stderr)
        return None  # 上游侧失败：与「成功但 0 条」区分开，前者才可计入域名熔断

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
            return None
    except Exception as ex:
        print("[RSS聚合] %s 异常: %s" % (name, ex), file=sys.stderr)
        return None

    items = []
    ns = "{http://www.w3.org/2005/Atom}"

    if root.tag.endswith("feed"):
        for e in root.findall(ns + "entry"):
            title = _strip_html(e.findtext(ns + "title") or "")
            link_el = e.find(ns + "link")
            link = (link_el.get("href") if link_el is not None else (e.findtext(ns + "id") or "")).strip()
            summary_raw = e.findtext(ns + "summary") or ""
            desc = _strip_html(summary_raw)
            content_raw = e.findtext(ns + "content") or ""
            # 出厂规范链路（顺序即契约，逐条不许换）：
            #   _unescape_escaped_body 把双重转义的正文解一层（I8）→ _normalize_body_html
            #   提懒加载 src / 绝对化相对 URL（必须在 sanitize 之前，sanitize 只认 src）
            #   → _sanitize_html 白名单 → _deep_clean_html 去推广噪音。
            atom_content = _deep_clean_html(_sanitize_html(_normalize_body_html(
                _unescape_escaped_body(content_raw), link)))
            full_content = atom_content if len(atom_content) > len(desc) else ""
            pub = e.findtext(ns + "updated") or e.findtext(ns + "published") or ""
            if not title or not link:
                continue
            media_url, media_type = _pick_item_media(e)
            # 与 RSS 入口同一套刀序（`_rewrite_hn_summary` 在内、`_strip_glued_url` 在外、
            # 最外层 `_truncate`）；两段代码各自写 summary，漏一个另一半照旧露粘连。
            items.append({
                "title": title, "link": link, "summary": _truncate(_strip_glued_url(_rewrite_hn_summary(desc, link))),
                "full_content": full_content, "image": _upgrade_img_url(_pick_item_image(e, summary_raw, content_raw)),
                "pub_date": _parse_iso(pub), "source": name, "source_key": source["key"],
                "cat": source["cat"],
                "media_url": media_url, "media_type": media_type,
            })
    else:
        ch = root.find("channel")
        rdf_ns = "{http://purl.org/rss/1.0/}"
        if ch is None and root.find(rdf_ns + "channel") is None:
            for it in root.findall(".//item"):
                _parse_rss_item(it, name, source["key"], source["cat"], items)
        elif ch is not None:
            for it in ch.findall("item"):
                _parse_rss_item(it, name, source["key"], source["cat"], items)
        else:
            # RSS 1.0 RDF format (e.g. DW)
            for it in root.findall(".//" + rdf_ns + "item"):
                _parse_rss_item(it, name, source["key"], source["cat"], items)

    # ── 通用去重阶段 ──
    items = _dedup_source_items(items, source["key"])

    # 写入缓存
    _rss_cache[key] = {
        "items": items[:ITEMS_PER_SOURCE],
        "fetched_at": time.time()
    }

    return items[:ITEMS_PER_SOURCE]


# W3C 规范 URI 里日期段是斜杠（…/1999/02/22-rdf-syntax-ns#），肉眼分不清 - 与 /，
# 改动这两行请用码点核对而不是看渲染结果。
_RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
_RDF_RESOURCE = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource"


def _rss_text(it, tag):
    """取子元素文本，兼容带默认命名空间的 feed（RSS 1.0/RDF 等）。

    RDF 文档常用 xmlns="http://purl.org/rss/1.0/" 声明默认命名空间，此时 <title>
    的真实标签是 {…}title，不带前缀的 findtext 取不到 → title 为空被 `if not title`
    丢掉每一条，于是 DW/Nature/Science 这类上游有 37~137 条的源在我方解析出 0 条。
    用 {*} 而非写死 rss1.0，是为了覆盖 RSS 0.9x 之类同样带默认命名空间的变体。
    """
    v = it.findtext(tag)
    if v is None:
        v = it.findtext("{*}" + tag)
    return v or ""


def _parse_rss_item(it, source_name, source_key, cat, items):
    title = _strip_html(_rss_text(it, "title"))
    link = (_rss_text(it, "link") or "").strip()
    if not link:
        _le = it.find("link")
        if _le is None:
            _le = it.find("{*}link")
        if _le is not None:
            link = (_le.get(_RDF_RESOURCE) or "").strip()
    if not link:
        link = (it.get(_RDF_ABOUT) or "").strip()
    if not link:
        # 有些 feed 把永久链接只写在 <guid> 里（安全客 api.anquanke.com/data/v1/rss 就是：
        # raw 有 20 个 item、我方解析 0 条，日志记成 empty，看着像"上游没内容"）。
        # 只接受 http(s) 且未被显式声明 isPermaLink="false" 的 guid —— 否则宁可为空：
        # 一个看起来能点、点开 404 的链接比空链接更坏。
        _ge = it.find("guid")
        if _ge is not None:
            _gv = (_ge.text or "").strip()
            if _gv.lower().startswith(("http://", "https://")) and \
                    (_ge.get("isPermaLink") or "").strip().lower() != "false":
                link = _gv
    desc_raw = _rss_text(it, "description")
    pub = (_rss_text(it, "pubDate") or "").strip()
    # RSS 1.0 RDF: date is in dc:date
    if not pub:
        pub = (it.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
    dc_content = "{http://purl.org/rss/1.0/modules/content/}"
    content_raw = it.findtext(dc_content + "encoded") or ""
    if not title or not link:
        return
    media_url, media_type = _pick_item_media(it)
    # ── XGO (Twitter/X) 信源专门清洗 ──
    # xgo.ing 代理的 description 包含完整 HTML 卡片（推文正文 + 引用推文 +
    # 头像 + 互动数据 + 页脚），直接 _strip_html 会把所有噪音混入摘要。
    if _is_xgo_content(desc_raw):
        cleaned = _clean_xgo_content(desc_raw)
        desc = cleaned or _strip_html(desc_raw)
        full_content = ""  # 推文正文已在 desc 中，无需全文加载
        # 不套封面判空：_extract_tweet_media 只认 /media/ 与 tweet_video_thumb 两种形态，
        # 产出的 host 恒为 pbs.twimg.com（不在必挂表里，且它只在"无代理直连"口径下挂）。
        img = _extract_tweet_media(desc_raw)
    else:
        desc = _strip_html(desc_raw)
        # 出厂规范链路：与 Atom 入口同一套顺序与理由（见 `_fetch_rss` 里那条注释）。
        content_encoded = _deep_clean_html(_sanitize_html(_normalize_body_html(
            _unescape_escaped_body(content_raw), link)))
        full_content = content_encoded if len(content_encoded) > len(desc) else ""
        img = _upgrade_img_url(_pick_item_image(it, desc_raw, content_raw))
    # summary 出口的两把刀**顺序是契约**：先 `_rewrite_hn_summary`（hnrss 模板压成一行）、
    # 再 `_strip_glued_url`（剥与正文粘连的裸链接）。倒过来会先改掉 HN 重写要看的行首形状，
    # 两条互相吞（判据 test_order_matters_when_meta_lines_come_before_the_glued_line 给的是行为证据，
    # test_rss_parse_entry_wraps_glued_strip_in_the_right_order 给的是链形）。
    items.append({
        "title": title, "link": link, "summary": _truncate(_strip_glued_url(_rewrite_hn_summary(desc, link))),
        "full_content": full_content, "image": img,
        "pub_date": _parse_rss_date(pub) or _parse_iso(pub), "source": source_name, "source_key": source_key,
        "cat": cat,
        "media_url": media_url, "media_type": media_type,
    })


# ──────────────────────────── HTML 生成 ────────────────────────────

def _build_css():
    return """
:root {
  --bg:#faf9f7; --card:#fffdf9; --card-2:#f3efe6; --card-3:#ebe6db;
  --ink:#1c1917; --muted:#5f594c; --faint:#6e685e; /* A4 修复：加深弱化灰至 ≥4.5:1 对比度 */
  --line:#ddd6c9; --line-strong:#b9b0a2;
  --brand:#2f5d8a; --brand-strong:#24496e; --brand-line:#b9cde0; --brand-weak:#e7eef4;
  --display:"Noto Serif SC","Georgia","Times New Roman","Songti SC","SimSun","STSong",serif;
  --body:"Noto Sans SC",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  --mono:"IBM Plex Mono","SF Mono","Fira Code","Consolas",monospace;
  --radius:8px;
  --shadow:0 1px 2px rgba(28,25,23,.05);
  --shadow-lift:0 10px 26px rgba(0,0,0,.10),0 2px 4px rgba(0,0,0,.06);
  --cat-tech:#2f5d8a; --cat-cn_tech:#c2434d; --cat-dev:#7052c9; --cat-ai:#b06a10; --cat-news:#8a6d1f; --cat-podcast:#2e7d5f; --cat-youtube:#e0245e;
  --unread:#177a37; --read-badge:#c0392b; /* A4 修复：未读绿加深至 ≥4.5:1 */
}
[data-theme="dark"] {
  --bg:#161412; --card:#1d1a17; --card-2:#262019; --card-3:#2f2820;
  --ink:#ece7df; --muted:#a59d90; --faint:#a8a090; /* A4 修复：暗色模式弱化灰加深 */
  --line:#37312a; --line-strong:#4a4339;
  --brand:#8fb3d9; --brand-strong:#b0cbe6; --brand-line:#3d5a78; --brand-weak:#22303f;
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-lift:0 10px 26px rgba(0,0,0,.5),0 2px 4px rgba(0,0,0,.4);
  --cat-tech:#8fb3d9; --cat-cn_tech:#e08790; --cat-dev:#a894e8; --cat-ai:#d3a15c; --cat-news:#cbb26a; --cat-podcast:#6cba9c; --cat-youtube:#ff4d79;
  --unread:#3ddc78; --read-badge:#f87171; /* A4 修复：暗色未读绿微调 */
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
.src-btn:hover { background:var(--brand-strong); color:#fff; }
.src-btn svg { width:13px; height:13px; }
.src-btn .cnt { font-family:var(--mono); font-size:10px; opacity:.8; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chip { padding:4px 12px; border-radius:999px; font-size:12px; font-weight:500; border:1px solid var(--line); background:var(--card); color:var(--muted); transition:all .15s; white-space:nowrap; }
.chip:hover { border-color:var(--line-strong); color:var(--ink); }
.chip.on { background:var(--brand-weak); border-color:var(--brand-line); color:var(--brand-strong); font-weight:600; }
.refresh-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--muted);transition:all .15s;cursor:pointer;}
.refresh-btn:hover{border-color:var(--brand-line);color:var(--brand-strong);background:var(--brand-weak);}
.refresh-btn.loading{pointer-events:none;opacity:.7;}
.refresh-btn.loading svg{animation:spin 1s linear infinite;}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
.refresh-btn svg{width:13px;height:13px;}
.unread-toggle{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--muted);transition:all .15s;cursor:pointer;}
.unread-toggle:hover{border-color:var(--brand-line);color:var(--brand-strong);background:var(--brand-weak);}
.unread-toggle.on{background:var(--brand-weak);border-color:var(--brand-line);color:var(--brand-strong);}
.unread-toggle svg{width:13px;height:13px;}
.back-top{position:fixed;bottom:24px;right:24px;width:40px;height:40px;border-radius:50%;background:var(--card);border:1px solid var(--line);color:var(--muted);display:flex;align-items:center;justify-content:center;cursor:pointer;opacity:0;pointer-events:none;transition:all .2s;z-index:90;box-shadow:0 2px 8px rgba(0,0,0,.08);}
.back-top.show{opacity:1;pointer-events:auto;}
/* 抽屉面板打开时隐藏回到顶部按钮，避免浮在面板内容之上 */
body.ai-open .back-top{opacity:0;pointer-events:none;}
.back-top:hover{color:var(--brand-strong);border-color:var(--brand-line);background:var(--brand-weak);}
.back-top svg{width:18px;height:18px;}
.bm-btn{width:22px;height:22px;border-radius:6px;display:inline-flex;align-items:center;justify-content:center;color:var(--faint);border:1px solid transparent;transition:all .15s;cursor:pointer;background:none;padding:0;flex:none;position:relative;}
.bm-btn::after{content:'';position:absolute;inset:-5px;}
.bm-btn:hover{color:var(--brand-strong);border-color:var(--brand-line);background:var(--brand-weak);}
.bm-btn.on{color:var(--brand-strong);}
.bm-btn svg{width:12px;height:12px;}
.bm-btn.on svg{fill:currentColor;}
.r2-bm{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;color:var(--muted);background:none;border:1px solid var(--line);padding:3px 10px;border-radius:8px;cursor:pointer;transition:all .15s;}
.r2-bm:hover{border-color:var(--brand-line);color:var(--brand-strong);background:var(--brand-weak);}
.r2-bm.on{color:var(--brand-strong);border-color:var(--brand-line);background:var(--brand-weak);}
.r2-bm svg{width:13px;height:13px;}
.r2-bm.on svg{fill:currentColor;}
.chip.bm-chip{color:var(--muted);border-color:var(--line);}
.chip.bm-chip.on{background:var(--brand-weak);border-color:var(--brand-line);color:var(--brand-strong);}
.chip.disc-chip{border-style:dashed;color:#0891b2;border-color:#0891b2;}
.chip.disc-chip.on{background:#ecfeff;border-color:#0891b2;color:#0e7490;}
@media(prefers-color-scheme:dark){.chip.disc-chip{color:#22d3ee;border-color:#22d3ee;}.chip.disc-chip.on{background:#164e63;border-color:#22d3ee;color:#67e8f9;}}
.chip .n { font-family:var(--mono); font-size:10px; opacity:.75; margin-left:3px; }
/* A2 修复：模态焦点可见环 */
[role="dialog"] :focus-visible,.share-modal :focus-visible{outline:2px solid var(--brand);outline-offset:2px;}
/* U6 修复：搜索信源匹配标签 */
.src-match-label{font-size:11px;color:var(--brand-strong);padding:2px 0 0 4px;font-weight:500;}
.fpill { display:inline-flex; align-items:center; gap:6px; padding:4px 6px 4px 12px; border-radius:999px; font-size:12px; font-weight:600; background:var(--ink); color:var(--bg); }
.fpill .x { width:16px; height:16px; border-radius:999px; background:rgba(255,255,255,.18); display:flex; align-items:center; justify-content:center; font-size:11px; cursor:pointer; }
.fpill .x:hover { background:rgba(255,255,255,.34); }
.tool-meta { margin-left:auto; font-family:var(--mono); font-size:11px; color:var(--faint); white-space:nowrap; }

/* ── Search row ── */
.search-row { max-width:1560px; margin:0 auto; padding:10px 20px 0; display:flex; align-items:center; gap:10px; }
.global-search { position:relative; flex:1; min-width:0; }
.global-search input { width:100%; padding:10px 36px 10px 16px; border-radius:12px; border:1.5px solid var(--line); background:var(--card); font-size:14px; color:var(--ink); font-family:var(--body); outline:none; transition:border-color .15s, box-shadow .15s; }
.global-search input:focus { border-color:var(--brand); box-shadow:0 0 0 3px var(--brand-weak); }
.global-search input:focus-visible { outline:2px solid var(--brand); outline-offset:1px; }
button:focus-visible, .chip:focus-visible, .card:focus-visible, a:focus-visible { outline:2px solid var(--brand); outline-offset:2px; border-radius:var(--radius); }
.global-search input::placeholder { color:var(--faint); font-size:13.5px; }
.global-search .sx { position:absolute; right:10px; top:50%; transform:translateY(-50%); width:22px; height:22px; border-radius:999px; background:var(--line); color:var(--muted); display:none; align-items:center; justify-content:center; font-size:11px; cursor:pointer; transition:all .15s; }
.global-search .sx:hover { background:var(--line-strong); color:var(--ink); }
.global-search.has-q .sx { display:flex; }
.global-search.has-q input { border-color:var(--brand-line); background:var(--brand-weak); }
.global-search.src-hit input { border-color:var(--brand-strong); background:var(--brand-weak); box-shadow:0 0 0 3px var(--brand-weak); }
.global-search.src-hit::after { content:'\u2316'; position:absolute; left:4px; top:50%; transform:translateY(-50%); font-size:14px; color:var(--brand-strong); pointer-events:none; }
.sort-select { padding:10px 32px 10px 14px; border-radius:12px; border:1.5px solid var(--line); background:var(--card); font-size:13px; font-weight:500; color:var(--muted); font-family:var(--body); outline:none; cursor:pointer; transition:border-color .15s; appearance:none; -webkit-appearance:none; background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23857e74' stroke-width='2.5' stroke-linecap='round'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 10px center; flex:none; }
.sort-select:hover { border-color:var(--brand-line); }
.sort-select:focus { border-color:var(--brand); box-shadow:0 0 0 3px var(--brand-weak); }

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
.ext-btn { flex:none; width:22px; height:22px; border-radius:6px; display:flex; align-items:center; justify-content:center; color:var(--faint); border:1px solid transparent; transition:all .15s; position:relative; }
.ext-btn::after { content:''; position:absolute; inset:-5px; }
.ext-btn:hover { color:var(--brand-strong); border-color:var(--brand-line); background:var(--brand-weak); }
.ext-btn svg { width:11px; height:11px; }
.card-title { font-family:var(--display); font-size:15.5px; font-weight:700; line-height:1.45; margin-bottom:7px; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; color:var(--unread); transition:color .15s; }
.card:hover .card-title { color:var(--brand-strong); }
.card-summary { font-size:12.5px; color:var(--muted); line-height:1.7; display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }
.card-tags { display:flex; flex-wrap:wrap; gap:4px; margin-top:6px; }
.ctag { font-size:10px; color:var(--brand); background:color-mix(in srgb, var(--brand) 10%, transparent); border-radius:4px; padding:1px 6px; line-height:1.6; white-space:nowrap; }
.card-foot { display:flex; align-items:center; gap:6px; margin-top:11px; padding-top:9px; border-top:1px solid var(--line); font-size:11px; color:var(--faint); }
.src-dot { width:8px; height:8px; border-radius:999px; flex:none; background:var(--sc); }
[data-theme="dark"] .src-dot { filter:brightness(1.7) saturate(.85); }
.src-name { font-weight:600; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.foot-meta { margin-left:auto; font-family:var(--mono); font-size:10.5px; display:flex; gap:8px; align-items:center; white-space:nowrap; }
.no-ft { font-size:10px; color:var(--faint); border:1px dashed var(--line-strong); border-radius:4px; padding:0 5px; }
.card.visited { border-left:3px solid var(--line-strong); background:color-mix(in srgb, var(--card-2) 50%, var(--card)); }
.card.visited .card-title { color:var(--faint); opacity:.72; }
.card.visited .card-title::after { content:"\\5df2 \\8bfb"; font-family:var(--body); font-size:9px; font-weight:600; color:#fff; background:var(--read-badge); border-radius:4px; padding:0 5px; margin-left:6px; vertical-align:2px; }
.card.visited .card-summary { opacity:.65; }
.pod-chip { display:inline-flex; align-items:center; gap:4px; font-size:10.5px; color:var(--cat-podcast); background:color-mix(in srgb, var(--cat-podcast) 10%, transparent); border-radius:4px; padding:1px 6px; font-weight:600; }
.empty-hint { text-align:center; color:var(--faint); font-size:13px; padding:60px 0; line-height:2; }
mark{background:var(--brand-weak);color:var(--brand-strong);padding:0 2px;border-radius:3px;}
.search-banner{padding:8px 16px;font-size:13px;color:var(--muted);background:var(--brand-weak);border:1px solid var(--brand-line);border-radius:10px;margin-bottom:8px;}

/* ── 封面卡：上图片 / 标题 / 内容介绍（无图降级为分类色渐变 + 首字） ── */
.card.cover-card { padding:0; overflow:hidden; }
.cover-card .cover { display:block; position:relative; height:var(--cover-h,150px); background:var(--card-2); border-bottom:1px solid var(--line); }
.cover-card .cover-img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; display:block; transition:transform .25s ease; }
.cover-card .cover-fallback { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-family:var(--display); font-size:40px; font-weight:700; color:#fff; background:linear-gradient(135deg, var(--cc), color-mix(in srgb, var(--cc) 45%, #191919)); }
@supports not (background:color-mix(in srgb, red, blue)) { .cover-card .cover-fallback { background:var(--cc); } }
.cover-card .card-top { margin:0; padding:10px 13px 0; }
.cover-card .card-title { margin:8px 13px 0; }
.cover-card .card-summary { margin:6px 13px 0; }
.cover-card .card-foot { margin:10px 13px 0; padding:9px 0 12px; }
.cover-card.visited { border-left:3px solid var(--line-strong); }
.cover-card.visited .cover-img, .cover-card.visited .cover-fallback { opacity:.55; }
@media (hover:hover) and (pointer:fine) { .cover-card:hover .cover-img { transform:scale(1.04); } }
[data-theme="dark"] .cover-card .cover-img { filter:brightness(.88) saturate(.92); }
@media (max-width:640px) { .cover-card .cover { height:118px; } }


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

/* ── Reader modal (centered) ─ */
.reader2 { position:fixed; top:50%; left:50%; width:min(640px,92vw); max-height:88vh; z-index:70; background:var(--bg); border-radius:14px; box-shadow:0 24px 80px rgba(0,0,0,.22); transform:translate(-50%,-50%) scale(.96); opacity:0; pointer-events:none; transition:transform .25s cubic-bezier(.32,.72,.28,1), opacity .2s; display:flex; flex-direction:column; overflow:hidden; }
body.reading .reader2 { transform:translate(-50%,-50%) scale(1); opacity:1; pointer-events:auto; }
.r2-top { flex:none; display:flex; align-items:center; gap:10px; padding:10px 18px; border-bottom:1px solid var(--line); background:var(--card); position:relative; }
.r2-progress { position:absolute; left:0; bottom:-1px; height:2px; background:var(--brand); width:0%; transition:width .1s linear; }
.r2-close { display:flex; align-items:center; justify-content:center; width:30px; height:30px; border-radius:999px; border:1px solid transparent; color:var(--muted); transition:all .15s; flex:none; }
.r2-close:hover { color:var(--read-badge); border-color:var(--line); background:var(--bg); }
.r2-close svg { width:14px; height:14px; }
.r2-src { display:flex; align-items:center; gap:7px; font-size:12px; color:var(--muted); min-width:0; }
.r2-src .src-dot { width:9px; height:9px; }
.r2-src b { color:var(--ink); font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.r2-acts { margin-left:auto; display:flex; align-items:center; gap:8px; }
.r2-open { display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:600; color:var(--brand-strong); background:var(--brand-weak); border:1px solid var(--brand-line); padding:4px 12px; border-radius:8px; white-space:nowrap; transition:all .15s; }
.r2-open:hover { background:var(--brand-strong); color:#fff; }
.r2-body { flex:1; min-height:0; overflow-y:auto; -webkit-overflow-scrolling:touch; }
.r2-body.fs-sm .r2-summary { font-size:13.5px; }
.r2-body.fs-md .r2-summary { font-size:15px; }
.r2-body.fs-lg .r2-summary { font-size:17.5px; }
.r2-body.fs-sm .r2-title { font-size:20px; }
.r2-body.fs-lg .r2-title { font-size:26px; }
.r2-body.fs-sm .r2-fulltext { font-size:13.5px; }
.r2-body.fs-lg .r2-fulltext { font-size:17.5px; }
.r2-fs-btns { display:inline-flex; align-items:center; gap:2px; margin-right:4px; }
.r2-fs-btn { width:26px; height:26px; border-radius:6px; display:inline-flex; align-items:center; justify-content:center; font-weight:700; font-size:11px; border:1px solid var(--line); background:var(--card); color:var(--muted); cursor:pointer; transition:all .15s; font-family:var(--body); }
.r2-fs-btn:hover { border-color:var(--brand-line); color:var(--brand-strong); background:var(--brand-weak); }
.r2-fs-btn.active { background:var(--brand-weak); border-color:var(--brand-line); color:var(--brand-strong); }
.r2-inner { max-width:680px; margin:0 auto; padding:30px 34px 80px; }
.r2-title { font-family:var(--display); font-size:23px; font-weight:900; line-height:1.42; margin-bottom:12px; }
.r2-title a { color:var(--ink); }
.r2-title a:hover { color:var(--brand-strong); }
.r2-meta { display:flex; align-items:center; gap:10px; font-size:12px; color:var(--faint); padding-bottom:16px; margin-bottom:22px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
.r2-meta .src-dot { width:9px; height:9px; }
.r2-meta .cat { font-weight:700; letter-spacing:.06em; font-size:10.5px; text-transform:uppercase; color:var(--cc); }
.r2-summary { font-size:15px; line-height:1.9; color:var(--ink); }
.r2-summary p { margin:0 0 1.2em 0; }
.r2-summary p:last-child { margin-bottom:0; }
.r2-fulltext { margin-top:20px; padding-top:20px; border-top:1px solid var(--line); font-size:15px; line-height:1.9; color:var(--ink); }
.r2-fulltext p { margin:0 0 1.2em 0; }
.r2-fulltext p:last-child { margin-bottom:0; }
.r2-fulltext img { max-width:100%; height:auto; border-radius:var(--radius); margin:1em 0; }
.r2-fulltext pre { background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:14px 16px; overflow-x:auto; font-size:13px; line-height:1.6; margin:1em 0; }
.r2-fulltext code { font-family:var(--mono); font-size:0.9em; background:var(--bg); padding:1px 5px; border-radius:4px; }
.r2-fulltext pre code { background:none; padding:0; }
.r2-fulltext blockquote { border-left:3px solid var(--brand-line); margin:1em 0; padding:4px 16px; color:var(--muted); background:var(--brand-weak); border-radius:0 8px 8px 0; }
.r2-fulltext h1,.r2-fulltext h2,.r2-fulltext h3 { font-family:var(--display); margin:1.5em 0 0.6em; }
.r2-fulltext h1 { font-size:20px; } .r2-fulltext h2 { font-size:18px; } .r2-fulltext h3 { font-size:16px; }
.r2-fulltext a { color:var(--brand-strong); text-decoration:underline; text-underline-offset:2px; }
.r2-fulltext ul,.r2-fulltext ol { margin:0.8em 0; padding-left:1.5em; }
.r2-fulltext li { margin-bottom:0.4em; }
.r2-fulltext table { border-collapse:collapse; width:100%; margin:1em 0; font-size:14px; }
.r2-fulltext th,.r2-fulltext td { border:1px solid var(--line); padding:6px 10px; text-align:left; }
.r2-fulltext th { background:var(--bg); font-weight:600; }
.r2-ft-loading { display:flex; align-items:center; gap:8px; padding:16px 0; color:var(--faint); font-size:13px; }
.r2-ft-loading svg { width:16px; height:16px; animation:spin 1s linear infinite; }
@keyframes spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
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
.build-bar { background:var(--bg); max-width:1560px; margin:0 auto; padding:2px 20px 8px; display:flex; align-items:center; gap:10px; font-family:var(--mono); font-size:11px; color:var(--faint); border-bottom:1px solid var(--line); }

/* ── Reduced motion ── */
@media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition-duration:0s!important; animation-duration:0s!important; } }

/* ── Responsive ── */
@media (max-width:1100px) { .wall { columns:3 280px; } }
@media (max-width:900px) {
  .hd .logo .sub { display:none; }
  .toolbar { padding:12px 14px 2px; }
  .search-row { padding:8px 14px 0; }
  .wall-wrap { padding:12px 14px 50px; }
  .wall { columns:2 260px; }
  .r2-inner { padding:22px 20px 70px; }
  .tool-meta { display:none; }
  .build-bar { padding:2px 14px 6px; }
}
@media (max-width:700px) {
  .hd .nav-links a { padding:5px 9px; font-size:12px; }
  .chips { overflow-x:auto; flex-wrap:nowrap; max-width:100%; padding-bottom:4px; scroll-snap-type:x proximity; -webkit-mask-image:linear-gradient(to right,#000 90%,transparent 100%); mask-image:linear-gradient(to right,#000 90%,transparent 100%); }
  .chip { white-space:nowrap; flex:none; scroll-snap-align:start; }
  .search-row { flex-wrap:wrap; }
  .sort-select { flex:0 0 auto; min-width:0; padding:6px 22px 6px 8px; font-size:12px; background-position:right 6px center; }
  .wall { columns:1 minmax(0,1fr); }
  .reader2 { width:96vw; max-height:92vh; border-radius:12px; }
  .r2-top { padding:8px 10px; gap:8px; }
  /* 移动端工具栏：统一控件高度 28px，防溢出挤压变形 */
  .r2-close, .r2-bm, .r2-open { height:28px; flex:none; align-items:center; }
  .r2-close { width:28px; height:28px; }
  .r2-fs-btn { width:28px; height:28px; }
  .r2-fs-btns { margin-right:0; }
  .r2-src { flex:1; min-width:0; }
  .r2-src b { max-width:none; flex:0 1 auto; }
  .r2-src span:not(.src-dot) { display:none; } /* 隐藏 · 与绝对时间（正文中可见），源名弹性收缩 */
  .r2-open { padding:0 9px; }
  .r2-inner { padding:18px 16px 60px; }
  .r2-title { font-size:19px; }
  .r2-summary { font-size:14px; line-height:1.8; }
  .r2-summary p { margin-bottom:1em; }
}

/* ── Share button (card wall) ── */
.share-btn{flex:none;width:20px;height:20px;border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--brand);border:1px solid transparent;transition:all .15s;background:none;cursor:pointer;padding:0;position:relative;}
.share-btn::after{content:'';position:absolute;inset:-6px;}
.share-btn:hover{color:var(--brand-strong);border-color:var(--brand-line);background:var(--brand-weak);}
.share-btn.loading{pointer-events:none;opacity:.5;}
.share-btn svg{width:13px;height:13px;}
.copy-btn{display:inline-flex;align-items:center;gap:4px;color:var(--faint);font-size:11px;background:none;border:none;cursor:pointer;padding:2px 4px;border-radius:4px;font-family:var(--mono);transition:all .15s;}
.copy-btn:hover{color:var(--brand-strong);background:var(--brand-weak);}
.copy-btn svg{width:12px;height:12px;}

/* ── Share action bar (reader body bottom) ── */
.r2-actions-bottom{display:flex;justify-content:center;padding:22px 0 4px;}
.r2-share-btn{display:inline-flex;align-items:center;gap:8px;padding:9px 20px;border-radius:999px;font-size:13px;font-weight:600;border:1px solid var(--brand-line);background:var(--brand-weak);color:var(--brand-strong);cursor:pointer;transition:all .15s;}
.r2-share-btn:hover{background:var(--brand-strong);color:#fff;border-color:var(--brand-strong);}
.r2-share-btn.loading{pointer-events:none;opacity:.6;}
.r2-share-btn svg{width:15px;height:15px;}

/* ── Share modal ── */
.share-modal{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;padding:16px;}
.share-modal.open{display:flex;}
.share-backdrop{position:absolute;inset:0;background:rgba(28,25,23,.55);}
/* 面板高度自适应内容且封顶视口（vh→dvh 双写兜底旧浏览器）：
   超出时图片区内部滚动（overflow-y:auto），头部/提示/按钮区 flex:none 恒定可见，长文分享不再截断操作按钮 */
.share-panel{position:relative;z-index:1;width:min(400px,90vw);max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);display:flex;flex-direction:column;overflow:hidden;background:var(--bg);border-radius:16px;box-shadow:0 24px 80px rgba(0,0,0,.25);padding:20px;text-align:center;}
.share-hd{flex:none;display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.share-hd h3{font-family:var(--display);font-size:15px;font-weight:900;margin:0;}
.share-close{width:28px;height:28px;border-radius:999px;border:1px solid var(--line);background:var(--card);font-size:16px;color:var(--muted);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .15s;}
.share-close:hover{border-color:var(--line-strong);color:var(--ink);}
.share-img-wrap{flex:0 1 auto;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;border-radius:10px;border:1px solid var(--line);background:var(--card);}
.share-textcard{display:none;text-align:left;white-space:pre-wrap;word-break:break-word;background:var(--card);border:1px dashed var(--line-strong);border-radius:10px;padding:16px;font-size:13px;line-height:1.7;color:var(--ink);flex:0 1 auto;min-height:0;max-height:340px;overflow:auto;font-family:var(--body);}
.share-img-wrap img{width:100%;display:block;}
.share-hint{flex:none;font-size:12px;color:var(--faint);margin:12px 0 14px;}
.share-actions{flex:none;display:flex;flex-wrap:wrap;gap:10px;justify-content:center;}
.btn-share-save,.btn-share-copy{padding:8px 22px;border-radius:8px;font-size:13px;font-weight:600;border:1px solid var(--brand-line);transition:all .15s;cursor:pointer;font-family:var(--body);}
.btn-share-save{background:var(--brand-strong);color:#fff;}
.btn-share-save:hover{opacity:.9;}
.btn-share-copy{background:var(--brand-weak);color:var(--brand-strong);}
.btn-share-copy:hover{background:var(--brand-line);color:#fff;}

/* ── Toast ── */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--ink);color:var(--bg);font-size:13px;padding:8px 20px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:200;white-space:nowrap;}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);pointer-events:auto;}

/* ── Source panel export ── */
.sp-export{width:28px;height:28px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--muted);display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .15s;}
.sp-export:hover{border-color:var(--brand-line);color:var(--brand-strong);}
.sp-export svg{width:15px;height:15px;}

/* ── Keyboard help ── */
.kbd-help{position:fixed;inset:0;z-index:100;display:none;align-items:center;justify-content:center;}
.kbd-help.open{display:flex;}
.kbd-help-backdrop{position:absolute;inset:0;background:rgba(28,25,23,.55);}
.kbd-help-panel{position:relative;z-index:1;width:min(360px,88vw);background:var(--bg);border-radius:16px;box-shadow:0 24px 80px rgba(0,0,0,.25);padding:20px;}
.kbd-help-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}
.kbd-help-hd h3{font-family:var(--display);font-size:15px;font-weight:900;margin:0;}
.kbd-help-body table{width:100%;border-collapse:collapse;}
.kbd-help-body td{padding:6px 0;font-size:13px;color:var(--muted);vertical-align:middle;}
.kbd-help-body td:first-child{width:120px;}
.kbd-help-body kbd{display:inline-block;min-width:22px;text-align:center;padding:2px 6px;border-radius:5px;border:1px solid var(--line);background:var(--card);font-family:var(--mono);font-size:12px;color:var(--ink);line-height:1.5;}
.boot-loading{display:flex;align-items:center;justify-content:center;gap:10px;padding:72px 16px;color:var(--muted);font-size:13px;}
.boot-spin{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--brand);border-radius:50%;animation:bootspin .8s linear infinite;}
@keyframes bootspin{to{transform:rotate(360deg)}}

/* ── AI Feed button (toolbar) ── */
.ai-feed-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 13px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--brand-line);background:var(--brand-weak);color:var(--brand-strong);transition:all .15s;cursor:pointer;}
.ai-feed-btn:hover{background:var(--brand-strong);color:#fff;}
.ai-feed-btn.on{background:var(--brand-strong);color:#fff;}
.ai-feed-btn svg{width:13px;height:13px;}

/* ── AI Feed panel (right side drawer) ── */
.ai-feed-panel{position:fixed;top:0;right:0;bottom:0;width:min(400px,92vw);z-index:95;background:var(--card);border-left:1px solid var(--line);transform:translateX(103%);transition:transform .28s cubic-bezier(.32,.72,.28,1);display:flex;flex-direction:column;box-shadow:-18px 0 50px rgba(0,0,0,.12);}
body.ai-open .ai-feed-panel{transform:none;}
body.ai-open .scrim{opacity:1;pointer-events:auto;}
.af-head{flex:none;padding:14px 16px 10px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px;}
/* ── 面板主 Tab：AI 快讯 / 全网热榜（选中态用 --ink/--bg 反转对，明暗主题下文字均保证可读） ── */
.af-tabs{flex:1;display:flex;gap:5px;min-width:0;}
.af-tab{flex:1;max-width:132px;height:30px;padding:0 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--muted);font-size:12.5px;font-weight:600;font-family:inherit;cursor:pointer;transition:all .15s;white-space:nowrap;}
.af-tab:hover{border-color:var(--line-strong);color:var(--ink);}
.af-tab.on{background:var(--ink);border-color:var(--ink);color:var(--bg);}
/* Tab 内容切换：默认显示 AI 快讯区；body.af-tab-hot 时热榜区接管面板。
   overflow:hidden 兜底：内容超出时由 hp-body 内部滚动，绝不溢出面板可视区 */
.hp-wrap{display:none;flex-direction:column;flex:1;min-height:0;overflow:hidden;}
body.af-tab-hot .hp-wrap{display:flex;}
body.af-tab-hot .af-sub,body.af-tab-hot .af-filter,body.af-tab-hot .af-body,body.af-tab-hot .af-more{display:none!important;}
.af-close{width:26px;height:26px;border-radius:999px;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;color:var(--muted);transition:all .15s;flex:none;}
.af-close:hover{border-color:var(--line-strong);color:var(--ink);}
.af-close svg{width:12px;height:12px;}
.af-refresh{width:26px;height:26px;border-radius:999px;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;color:var(--muted);transition:all .15s;flex:none;cursor:pointer;background:transparent;}
.af-refresh:hover{border-color:var(--line-strong);color:var(--ink);}
.af-refresh.loading{pointer-events:none;opacity:.7;}
.af-refresh.loading svg{animation:spin 1s linear infinite;}
.af-refresh svg{width:12px;height:12px;}
.af-sub{flex:none;padding:6px 16px;font-size:11px;color:var(--faint);font-family:var(--mono);border-bottom:1px solid var(--line);}
.af-filter{display:flex;flex-wrap:wrap;gap:5px;padding:8px 16px 4px;flex:none;}
.af-filter .fchip{display:inline-flex;align-items:center;gap:4px;height:20px;padding:0 8px;border:1px solid var(--line);border-radius:999px;background:transparent;font-size:11px;color:var(--muted);cursor:pointer;transition:all .15s;font-family:inherit;}
.af-filter .fchip:hover{border-color:var(--line-strong);color:var(--ink);}
.af-filter .fchip.on{background:var(--ink);border-color:var(--ink);color:var(--bg);font-weight:600;}
.af-filter .fchip .n{opacity:.6;font-family:var(--mono);font-size:10px;}
.af-body{flex:1;overflow-y:auto;padding:4px 16px 16px;-webkit-overflow-scrolling:touch;}
.af-item{display:flex;align-items:flex-start;gap:8px;padding:8px 0;border-bottom:1px solid var(--line);font-size:12.5px;min-width:0;}
.af-item:last-child{border-bottom:0;}
.af-item .cat{flex:none;margin-top:1px;height:16px;line-height:16px;padding:0 6px;border-radius:2px;font-size:10.5px;font-weight:600;white-space:nowrap;}
.af-item .body{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;}
.af-item .t{color:var(--ink);font-weight:500;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.af-item .t:hover{color:var(--brand-strong);}
.af-item .meta{color:var(--faint);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono);}
.af-share{flex:none;width:20px;height:20px;border-radius:6px;display:flex;align-items:center;justify-content:center;color:var(--faint);border:1px solid transparent;background:none;cursor:pointer;padding:0;margin-top:1px;transition:all .15s;}
.af-share:hover{color:var(--brand-strong);border-color:var(--brand-line);background:var(--brand-weak);}
.af-share.loading{pointer-events:none;opacity:.5;}
.af-share svg{width:12px;height:12px;}
.af-more{flex:none;margin:8px 16px;height:32px;border:1px dashed var(--line);border-radius:var(--radius);background:transparent;font-size:12px;color:var(--muted);cursor:pointer;transition:all .15s;font-family:var(--body);}
.af-more:hover{color:var(--brand-strong);border-color:var(--brand-line);background:var(--brand-weak);}
.af-more:disabled{opacity:.5;cursor:not-allowed;}
.af-empty{color:var(--faint);font-size:13px;text-align:center;padding:40px 16px;}
.af-loading{display:flex;align-items:center;justify-content:center;gap:8px;padding:40px 16px;color:var(--muted);font-size:13px;}
.af-spin{width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--brand);border-radius:50%;animation:bootspin .8s linear infinite;}

/* ── AI Feed 常驻侧栏（桌面 ≥1280px）：面板移入 .wall-wrap 左侧，sticky 常驻、独立滚动；<1280px 保持右侧抽屉 + 按钮切换 ── */
@media (min-width:1280px) {
  .wall-wrap { display:flex; align-items:flex-start; gap:14px; }
  .wall { flex:1; min-width:0; }
  .ai-feed-panel { position:sticky; top:58px; left:auto; right:auto; bottom:auto; transform:none; width:300px; flex:none; height:calc(100vh - 78px); z-index:auto; border-left:none; border-right:1px solid var(--line); box-shadow:none; }
  body.ai-open .ai-feed-panel { transform:none; }
  body.ai-open .scrim { opacity:0; pointer-events:none; }
  .af-close { display:none; }
  .ai-feed-btn { display:none; }
  .hot-btn { display:none; }
}

@media (max-width:700px) {
  .ai-feed-panel{width:100vw;}
  .af-head{padding:12px 14px 8px;}
  .af-body{padding:4px 14px 14px;}
}

/* ── Hot toolbar button（移动端快捷入口：打开 AI 面板并切到热榜 Tab；桌面端面板常驻后隐藏，直接点 Tab） ── */
.hot-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 13px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid #f59e0b33;background:#f59e0b14;color:#b45309;transition:all .15s;cursor:pointer;}
.hot-btn:hover{background:#f59e0b;color:#fff;}
.hot-btn.on{background:#f59e0b;color:#fff;}
.hot-btn svg{width:13px;height:13px;}
.tb-divider{width:1px;height:16px;background:var(--line-strong);margin:0 4px;flex:none;}

/* ── 热榜内容区：维度分层（分类 + 平台）+ 卡片列表（WS-A） ── */
/* 维度行：行首标签 + 内容区 */
.dim-row{display:flex;align-items:flex-start;gap:8px;padding:10px 12px 0;}
.dim-label{flex:none;width:28px;font-size:10px;font-weight:700;color:var(--faint);letter-spacing:1px;padding-top:5px;user-select:none;}
.dim-body{flex:1;min-width:0;}
.dim-sep{border:none;border-top:1px solid var(--line);margin:10px 12px 0;}
/* 分类：分段控件（WS-A2） */
.hp-seg{display:inline-flex;flex-wrap:nowrap;overflow-x:auto;max-width:100%;background:var(--hover);border-radius:8px;padding:2px;gap:2px;scrollbar-width:none;}
.hp-seg::-webkit-scrollbar{display:none;}
.hp-seg-btn{flex:none;height:22px;padding:0 11px;border:none;border-radius:6px;background:transparent;color:var(--muted);font-size:11px;font-weight:600;transition:all .15s;white-space:nowrap;font-family:inherit;cursor:pointer;}
.hp-seg-btn:hover{color:var(--ink);}
.hp-seg-btn.on{background:var(--brand);color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.15);}
/* 平台行：彩点 chips（WS-A3） */
.plat-wrap{display:flex;align-items:center;gap:6px;min-width:0;}
.plat-row{display:flex;flex-wrap:wrap;gap:5px;min-width:0;flex:1;}
.plat-chip{flex:none;height:24px;padding:0 10px;border:1px solid var(--line);border-radius:999px;background:var(--hover);color:var(--muted);font-size:11.5px;font-weight:600;display:inline-flex;align-items:center;gap:5px;transition:all .15s;white-space:nowrap;font-family:inherit;cursor:pointer;}
.plat-chip:hover{border-color:var(--line-strong);color:var(--ink);}
.plat-chip .dot{width:7px;height:7px;border-radius:50%;flex:none;}
.plat-chip.on{color:#fff;border-color:transparent;}
.plat-more{flex:none;height:24px;padding:0 10px;border:1px dashed var(--line-strong);border-radius:999px;background:transparent;color:var(--faint);font-size:11px;font-weight:600;display:none;align-items:center;gap:3px;white-space:nowrap;font-family:inherit;cursor:pointer;}
.plat-more:hover{color:var(--ink);border-color:var(--faint);}
.plat-more svg{width:10px;height:10px;transition:transform .2s;}
.plat-more.open svg{transform:rotate(180deg);}
/* 分类切换淡入动画（WS-A5） */
.hp-swap{animation:hpFade .18s ease;}
@keyframes hpFade{from{opacity:.2}to{opacity:1;}}
.hp-body{flex:1;overflow-y:auto;padding:8px 12px 16px;-webkit-overflow-scrolling:touch;}
.hp-item{display:flex;align-items:flex-start;gap:8px;padding:9px 10px;margin-bottom:6px;border:1px solid var(--line);border-radius:var(--radius);background:var(--bg);cursor:pointer;transition:all .12s;text-decoration:none;color:inherit;}
.hp-item:hover{border-color:var(--line-strong);background:var(--hover);}
.hp-rank{flex:none;width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;font-family:var(--mono);background:var(--hover);color:var(--faint);}
.hp-rank.top3{background:var(--brand-weak);color:var(--brand-strong);}
.hp-title{flex:1;font-size:13px;line-height:1.5;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word;}
.hp-hot{flex:none;font-size:11px;color:var(--faint);font-family:var(--mono);margin-top:3px;}
.hp-empty{padding:40px 16px;text-align:center;color:var(--faint);font-size:13px;}
@media (max-width:900px){
  .plat-row.collapsed{flex-wrap:nowrap;overflow-x:auto;scroll-snap-type:x proximity;max-width:100%;-webkit-mask-image:linear-gradient(to right,#000 88%,transparent);mask-image:linear-gradient(to right,#000 88%,transparent);scrollbar-width:none;}
  .plat-row.collapsed::-webkit-scrollbar{display:none;}
  .plat-row.collapsed .plat-chip{scroll-snap-align:start;}
  .plat-wrap .plat-more{display:inline-flex;}
}
@media (max-width:700px) {
  .plat-row{padding:8px 0 2px;}
  .hp-body{padding:6px 10px 14px;}
  .dim-label{width:24px;}
}

/* ── Insight Panel（每日洞察面板 · WS-B 容器化） ── */
.insight-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 13px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid #6366f133;background:#6366f114;color:#4338ca;transition:all .15s;cursor:pointer;}
.insight-btn:hover{background:#6366f1;color:#fff;}
.insight-btn.on{background:#6366f1;color:#fff;}
.insight-btn svg{width:13px;height:13px;}
/* 洞察面板：PC 右抽屉 / 移动底部 Sheet（同一组件） */
.insight-panel{position:fixed;z-index:70;background:var(--card);display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,.14);visibility:hidden;}
@media (min-width:900px){
  .insight-panel{top:0;right:0;bottom:0;width:min(520px,92vw);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .25s ease,visibility .25s;}
  .insight-panel.open{transform:none;visibility:visible;}
}
@media (max-width:899px){
  .insight-panel{left:0;right:0;bottom:0;max-height:88vh;border-radius:16px 16px 0 0;border-top:1px solid var(--line);transform:translateY(100%);transition:transform .25s ease,visibility .25s;}
  .insight-panel.open{transform:none;visibility:visible;}
}
.ip-head{display:flex;align-items:center;gap:8px;padding:14px 18px 10px;border-bottom:1px solid var(--line);flex:none;}
.ip-head h2{margin:0;font-size:15px;font-weight:800;display:flex;align-items:center;gap:7px;}
.ip-head h2 svg{width:16px;height:16px;color:var(--brand);}
.ip-gen{font-size:10.5px;color:var(--faint);font-family:var(--mono);}
.ip-close{margin-left:auto;background:none;border:none;color:var(--faint);padding:6px;border-radius:8px;cursor:pointer;transition:all .15s;}
.ip-close:hover{background:var(--hover);color:var(--ink);}
.ip-close svg{width:17px;height:17px;}
/* 锚点导航条（吸顶） */
.ip-nav{display:flex;gap:4px;padding:8px 18px;border-bottom:1px solid var(--line);overflow-x:auto;flex:none;background:var(--card);scrollbar-width:none;position:sticky;top:0;z-index:2;}
.ip-nav::-webkit-scrollbar{display:none;}
.ip-nav button{flex:none;height:24px;padding:0 11px;border:none;border-radius:999px;background:transparent;color:var(--muted);font-size:11.5px;font-weight:600;font-family:inherit;cursor:pointer;}
.ip-nav button.on{background:var(--brand-weak);color:var(--brand-strong);}
.ip-body{flex:1;overflow-y:auto;padding:14px 18px 24px;-webkit-overflow-scrolling:touch;}
.ip-sec{margin-bottom:22px;scroll-margin-top:44px;}
.ip-label{font-size:11px;font-weight:800;color:var(--brand-strong);letter-spacing:1px;text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:6px;}
.ip-label svg{width:13px;height:13px;}
/* 统计行 */
.ip-stats{display:flex;gap:18px;flex-wrap:wrap;padding:2px 0 0;}
.ip-stats span{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;}
.ip-stats svg{width:13px;height:13px;color:var(--faint);}
/* 核心态势摘要 */
.ip-lede{font-size:13.5px;line-height:1.8;color:var(--ink);padding:10px 14px;background:var(--brand-weak);border-radius:8px;border-left:3px solid var(--brand);}
/* 情报子网格 */
.ip-sub-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.ip-sub{padding:10px 13px;border-radius:8px;border-left:3px solid var(--brand);background:color-mix(in srgb,var(--brand) 4%,transparent);font-size:12.5px;line-height:1.7;}
.ip-sub.full{grid-column:1/-1;}
.ip-sub b{display:block;font-size:10.5px;font-weight:800;letter-spacing:.5px;color:var(--brand-strong);margin-bottom:4px;}
/* 因果链 */
.ip-chain{padding:9px 13px;border-radius:8px;background:var(--bg);border:1px solid var(--line);font-size:12.5px;line-height:1.7;margin-bottom:8px;}
.ip-chain .arrow{color:var(--brand);font-weight:700;padding:0 2px;}
/* 信号看板 */
.ip-sig{padding:9px 13px;border-radius:8px;background:#f59e0b0a;border:1px solid #f59e0b30;margin-bottom:8px;}
.ip-sig-top{display:flex;align-items:center;gap:8px;margin-bottom:6px;}
.ip-sig-txt{flex:1;font-size:12.5px;font-weight:600;color:#b45309;}
.ip-sig-conf{flex:none;font-size:10.5px;font-family:var(--mono);color:var(--faint);font-variant-numeric:tabular-nums;}
.ip-sig-bar{height:3px;border-radius:2px;background:#f59e0b2e;overflow:hidden;}
.ip-sig-bar i{display:block;height:100%;border-radius:2px;background:#f59e0b;}
/* 关键词 */
.ip-kw-list{display:flex;flex-wrap:wrap;gap:6px;}
.ip-kw{padding:3px 11px;border-radius:999px;font-size:12px;border:1px solid var(--line);background:var(--bg);color:var(--muted);}
.ip-kw.rising{border-color:#f59e0b55;background:#f59e0b0e;color:#b45309;font-weight:600;}
.ip-kw.rising::after{content:"\u2191";margin-left:3px;font-size:10px;font-weight:800;}
/* 去重提示 */
.ip-dedup{margin-top:4px;padding:8px 12px;border-radius:8px;background:var(--hover);font-size:11px;color:var(--faint);display:flex;align-items:center;gap:6px;}
.ip-dedup svg{width:12px;height:12px;flex:none;color:#059669;}
.ip-empty{padding:24px;text-align:center;color:var(--faint);font-size:12.5px;}
/* 保留旧 ib-* 样式（话题聚类/关键词/趋势等仍由 renderInsight 使用） */
.ib-summary{font-size:13.5px;line-height:1.7;color:var(--ink);padding:8px 12px;background:var(--brand-weak);border-radius:8px;border-left:3px solid var(--brand);}
.ib-sub-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.ib-sub-section{padding:8px 12px;border-radius:8px;border-left:3px solid var(--brand);font-size:13px;line-height:1.65;}
.ib-sub-section.sub-core{background:#6366f10a;}
.ib-sub-section.sub-signal{background:#f59e0b0a;}
.ib-sub-section.sub-rss{background:#10b9810a;}
.ib-sub-section.sub-outlook{background:#ef44440a;}
.ib-sub-label{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;opacity:.7;color:var(--brand-strong);}
.ib-narrative{padding:10px 14px;border-radius:10px;background:linear-gradient(135deg,#6366f10a,#8b5cf60a);border-left:3px solid #7c3aed;font-size:13px;line-height:1.7;color:var(--ink);margin-bottom:10px;}
.ib-chain-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;margin-bottom:10px;}
.ib-chain-card{padding:8px 12px;border-radius:8px;background:var(--card);border:1px solid var(--line);font-size:12px;line-height:1.6;}
.ib-chain-title{font-weight:600;color:var(--brand);margin-bottom:4px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;}
.ib-chain-steps{color:var(--ink);}
.ib-signal-grid{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;}
.ib-signal-card{padding:6px 12px;border-radius:8px;background:#f59e0b0a;border:1px solid #f59e0b30;font-size:12px;color:#b45309;}
.ib-outlook{padding:10px 14px;border-radius:10px;background:#10b9810a;border-left:3px solid #10b981;font-size:13px;line-height:1.7;color:var(--ink);}
.ib-cluster-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px;}
.ib-cluster-card{padding:10px 14px;border-radius:10px;background:var(--card);border:1px solid var(--line);cursor:pointer;transition:all .15s;}
.ib-cluster-card:hover{border-color:var(--brand);box-shadow:0 2px 8px #0001;}
.ib-cluster-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}
.ib-cluster-label{font-weight:600;font-size:13px;color:var(--ink);}
.ib-cluster-count{font-size:11px;color:var(--muted);background:var(--bg);padding:2px 8px;border-radius:999px;}
.ib-cluster-bar{height:4px;border-radius:2px;background:var(--line);overflow:hidden;}
.ib-cluster-bar-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--brand),#8b5cf6);transition:width .3s;}
.ib-sub-text{color:var(--ink);}
.ib-kw-list{display:flex;flex-wrap:wrap;gap:6px;}
.ib-kw{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:500;border:1px solid var(--line);background:var(--bg);color:var(--muted);cursor:pointer;transition:all .12s;}
.ib-kw:hover{border-color:var(--brand);color:var(--brand);background:var(--brand-weak);}
.ib-kw.rising{border-color:#f59e0b55;background:#f59e0b0e;color:#b45309;}
.ib-kw.rising::after{content:"\u2191";margin-left:3px;font-size:10px;font-weight:700;}
.ib-topic-list{display:flex;flex-direction:column;gap:6px;}
.ib-topic{display:flex;align-items:center;gap:8px;padding:7px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);cursor:pointer;transition:all .12s;}
.ib-topic:hover{border-color:var(--brand);background:var(--brand-weak);}
.ib-topic-rank{flex:none;width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;font-family:var(--mono);background:var(--hover);color:var(--faint);}
.ib-topic-rank.top3{background:var(--brand-weak);color:var(--brand-strong);}
.ib-topic-title{flex:1;font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ib-topic-meta{flex:none;font-size:11px;color:var(--faint);font-family:var(--mono);}
.ib-stats{display:flex;gap:16px;padding:6px 0;font-size:12px;color:var(--faint);font-family:var(--mono);}
.ib-stats span{display:flex;align-items:center;gap:4px;}
.ib-no-data{padding:20px;text-align:center;color:var(--faint);font-size:13px;}
.ib-section{margin-bottom:14px;}
.ib-section:last-child{margin-bottom:0;}
.ib-label{font-size:11.5px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;}
@media (max-width:899px){
  .ip-sub-grid{grid-template-columns:1fr;}
}
@media (max-width:700px) {
  .ip-body{padding:10px 12px 14px;}
  .ip-kw-list{gap:4px;}
  .ip-kw{padding:2px 8px;font-size:11px;}
}

/* ── Source Quality Badge（信源质量指示器） ── */
.sq-badge{flex:none;display:inline-flex;align-items:center;justify-content:center;min-width:28px;height:18px;padding:0 5px;border-radius:9px;font-size:10px;font-weight:700;font-family:var(--mono);line-height:1;}
.sq-badge.sq-high{background:#10b98118;color:#059669;border:1px solid #10b98133;}
.sq-badge.sq-mid{background:#f59e0b18;color:#b45309;border:1px solid #f59e0b33;}
.sq-badge.sq-low{background:#ef444418;color:#dc2626;border:1px solid #ef444433;}
.sp-health{display:flex;align-items:center;gap:8px;padding:8px 14px;font-size:11.5px;color:var(--muted);border-bottom:1px solid var(--line);}
.sp-health-dot{width:8px;height:8px;border-radius:50%;}
.sp-health-dot.good{background:#10b981;}
.sp-health-dot.warn{background:#f59e0b;}
.sp-health-dot.bad{background:#ef4444;}

/* ── Hot Trend Arrows（热榜趋势箭头） ── */
.hp-trend{flex:none;display:inline-flex;align-items:center;gap:2px;font-size:10px;font-weight:700;font-family:var(--mono);padding:1px 5px;border-radius:4px;line-height:1;}
.hp-trend.t-up{color:#dc2626;background:#dc26260e;}
.hp-trend.t-down{color:#6b7280;background:#6b72800e;}
.hp-trend.t-new{color:#059669;background:#0596690e;}
/* ── Cross Platform（跨平台共振） ── */
.ib-cross{margin-top:10px;}
.ib-cross-list{display:flex;flex-direction:column;gap:5px;}
.ib-cross-item{display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);font-size:12.5px;}
.ib-cross-label{flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ib-cross-plats{display:flex;gap:4px;}
.ib-cross-plat{font-size:10px;padding:1px 6px;border-radius:4px;background:var(--hover);color:var(--faint);font-weight:600;}
.ib-trend{margin-top:12px;}
.ib-trend-title{font-size:12px;font-weight:600;color:var(--faint);margin-bottom:6px;}
.ib-trend-list{display:flex;flex-direction:column;gap:4px;}
.ib-trend-item{display:flex;align-items:center;gap:8px;padding:5px 10px;border:1px solid var(--line);border-radius:8px;font-size:12px;}
.ib-trend-label{flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ib-trend-count{font-size:11px;color:var(--faint);min-width:40px;text-align:right;}
.ib-trend-arrow{font-weight:700;font-size:13px;}
.ib-trend-arrow.t-up{color:#e74c3c;}
.ib-trend-arrow.t-down{color:#95a5a6;}
.ib-trend-arrow.t-hot{color:#e67e22;}
.ib-trend-arrow.t-new{color:#27ae60;}
.ib-trend-lc{font-size:10px;padding:1px 6px;border-radius:4px;font-weight:600;}
.ib-trend-lc.lc-emerging{background:#eaf4fe;color:#2980b9;border:1px solid #b3d7f7;}
.ib-trend-lc.lc-hot{background:#fef5e7;color:#e67e22;border:1px solid #f5cba7;}
.ib-trend-lc.lc-cooling{background:#f4f6f7;color:#7f8c8d;border:1px solid #d5d8dc;}
.ib-trend-lc.lc-cold{background:#f9f9f9;color:#bdc3c7;border:1px solid #e5e8e8;}
.ib-ccat{margin-top:12px;}
.ib-ccat-list{display:flex;flex-wrap:wrap;gap:5px;}
.ib-ccat-item{display:flex;align-items:center;gap:5px;padding:4px 10px;border:1px solid var(--line);border-radius:8px;font-size:12px;background:var(--bg);}
.ib-ccat-kw{font-weight:600;color:var(--fg);}
.ib-ccat-cats{display:flex;gap:3px;}
.ib-ccat-cat{font-size:10px;padding:1px 5px;border-radius:4px;background:var(--hover);color:var(--faint);}
.kw-tag{position:relative;cursor:default;}
.kw-tag.kw-emergent{border-color:#3498db !important;color:#2980b9 !important;}
.kw-tag.kw-rising{border-color:#e67e22 !important;color:#d35400 !important;}
.kw-tag.kw-peaking{border-color:#27ae60 !important;color:#1e8449 !important;}
.kw-tag.kw-declining{border-color:#95a5a6 !important;color:#7f8c8d !important;}
.kw-tag.kw-gone{border-color:#bdc3c7 !important;color:#bdc3c7 !important;text-decoration:line-through;}
.kw-tag::after{content:'';position:absolute;top:-2px;right:-2px;width:6px;height:6px;border-radius:50%;}
.kw-tag.kw-rising::after{background:#e67e22;}
.kw-tag.kw-emergent::after{background:#3498db;}
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


def _inline_json(text):
    """内联进 <script> 的 JSON 必须走这一道：与数据分块的 `_dump` 同一口径。

    为什么必须有：`ANALYSIS_DATA` 里混着**上游文章标题**（关键词、topic_clusters.items），
    原样拼接时任一格子里出现 `</script><img src=x onerror=...>` 就会被浏览器当成真标签，
    下一场构建起对每个访问者执行。现网产物实测该段已含裸 `<` 28 个（还没凑出 `</script>`，
    但门是开的）。
    只转 `<` 与 `_dump` 一致：`</script>` 的触发字符是 `<`，裸 `>` 开不了标签边界。
    本函数不动 JSON 语义 —— `\u003c` 在 JS 字符串字面量里解回 `<`，读回的对象逐字节相同。
    其余注入点逐个核过：BUILD_TS/DIVERSE_WINDOW 是 int、CAT_LABELS/CAT_ORDER 来自静态常量、
    qr_lib 是第三方库原文，均不含上游可控文本。
    """
    return str(text).replace("<", "\\u003c")


def _build_js(sources_with_items, build_ts_ms=0, analysis_json='', diverse_window_minutes=120):
    """Generate core JS for card wall + drawer reader."""
    cat_labels_json = json.dumps(CATEGORY_LABELS, ensure_ascii=False)
    rel_time_js = _read_rel_time_js()
    # 内嵌 QR 生成库：国内移动端 jsdelivr/unpkg 常不可达且请求会长时间挂起，
    # 导致分享模态框数十秒不出现甚至永远无反应。构建时直接内嵌 vendor 库。
    qr_lib = ''
    try:
        _qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor_qrcode.min.js')
        with open(_qr_path, 'r', encoding='utf-8') as _f:
            qr_lib = _f.read().split('//# sourceMappingURL=')[0].strip()
    except Exception:
        qr_lib = ''
    return """
<script>
""" + qr_lib + """
;(function(){
  var BUILD_TS = """ + str(int(build_ts_ms)) + """;
""" + rel_time_js + """
  var DIVERSE_WINDOW = """ + str(int(diverse_window_minutes)) + """;
  var SOURCES = [];
  var CAT_LABELS = """ + cat_labels_json + """;
  var ANALYSIS_DATA = """ + (_inline_json(analysis_json) if analysis_json else 'null') + """;

  /* ── Data ── */
  var CAT_ORDER = """ + json.dumps(CATEGORY_ORDER, ensure_ascii=False) + """;
  var ART = [];
  var now=new Date().toISOString();
  /* 全量数据按日期降序后由构建脚本切成 rss-data-0.js（首屏）与
     rss-data-1.js…rss-data-N.js（后台合并，按 MAX_CHUNK_BYTES 分片，不固定块数），
     页面不再内嵌数据（31MB→约0.15MB）。前端按索引逐块加载、遇到 404 停止，所以块数可以变。
     首屏严格时间排序；chunk1 合并后或刷新时应用 tier 交织。 */
  /* tags 归一化（issue M5）：只接受「非空数组」，其余（undefined / null / [] / 脏值）
     一律归一为 null。两条通道（构建期 chunk、刷新通道）共用同一函数，
     避免只修一条导致同一篇文章在不同通道下形状不一致。 */
  function _tagsOf(x){
    return (x && Object.prototype.toString.call(x.tags) === '[object Array]' && x.tags.length) ? x.tags : null;
  }
  function buildArt(){
    /* 渲染前全局去重：以 源key|链接 为唯一键，防止任何合并路径（chunk/快照/远程刷新）
       造成的同源同链文章重复渲染 */
    ART=[];var _seen={};
    SOURCES.forEach(function(s){
      s.items.forEach(function(it){
        var _u=it.link||'';
        var _k=(s.key||'')+'|'+(_u&&_u!=='#'?_u:(it.title_zh||it.title||''));
        if(_seen[_k])return; _seen[_k]=1;
        ART.push({t:it.title_zh||it.title, s:it.summary_zh||it.summary||'',
          src:s.name, sk:s.key, c:s.cat, sc:s.color, ti:s.tier||3,
          /* fc 兼容两条通道：chunk 通道字段名为 full_content，远程刷新通道为 fc */
          /* a.time 现算，口径唯一（lib/rel_time.js）。
             注意卡片显示走 _dynTime(a)，a.time 只是它的兜底分支（pub_date 缺失/非法/坏日期）
             外加书签快照与分享文案的消费者 —— 所以这里基准取 BUILD_TS 而不是访客时钟：
             它与改造前烘进产物的那一串逐字相等，兜底位才不会漂。 */
          time:STARHUB_REL_TIME.fmtRelTime(it.pub_date, BUILD_TS/1000), date:it.pub_date, u:it.link||'#', fc:it.fc||it.full_content||'',
          img:it.image||it.img||'', mu:it.mu||'', mt:it.mt||'',
          bad_date:!!it.bad_date, bb:!!s.bb, dfb:!!it.date_fallback, tags:_tagsOf(it)});
      });
    });
    /* 时区安全钳制（根因修复：旧实现用字符串比较判断未来日期，对 +08:00/Z 混合格式
       误判时区，把早间公众号文章改写成打开页面的时刻——09-14 08:15 事件）
       2026-09-14 二次修复：旧实现把**每一条**未来日期都改写成 now，N 条因此获得
       完全相同的时刻；默认降序排序下同刻并列，整块钉在首屏顶部
       （实测：超能网 6 条占据前 7 位中的 6 位）。
       改为「保序回拉」：最新的一条落到 now，其余按原始间距排在它之前 ——
       既不越界，也不制造人为并列；残留顺序信息一点不丢。
       注：构建期已对源级时区误标与「晚于 first_seen 的未来日期」做处置，
       本钳制是刷新通道（tier-1 由服务端 JS 重解析）的最后一道网。 */
    var nowMs=Date.now(), _fut=[];
    ART.forEach(function(a){
      if(!a.date) return;
      var _t=new Date(a.date).getTime();
      if(!isNaN(_t)&&_t>nowMs) _fut.push(a);
    });
    if(_fut.length){
      var _fmax=0;
      _fut.forEach(function(a){ var _t=new Date(a.date).getTime(); if(_t>_fmax) _fmax=_t; });
      _fut.forEach(function(a){
        a.date=new Date(nowMs-(_fmax-new Date(a.date).getTime())).toISOString();
      });
    }
    applySort();
    window.ART = ART;
  }
  /* ── Sort ── */
  /* sortMode 白名单：localStorage 里的脏值（旧版本写入 / 手工篡改）必须在此规范化。
     没有这一步，非法值会被默认分支一路吃下去，与 #sortSelect 的显示不一致，
     且未来新增的 else-if 分支可能被脏值意外命中。 */
  var SORT_MODES = ['newest','oldest','active','quality','diverse'];
  function _normSortMode(v){
    return (v && SORT_MODES.indexOf(v) >= 0) ? v : 'diverse';
  }
  var sortMode = _normSortMode(localStorage.getItem('rss_sort_mode'));
  /* 时区安全日期比较【仅用于升序】（D3 修复：+08:00/Z 混合格式的字符串比较是时区盲的）；
     无日期沉底，两个无日期视为相等。
     注意：本函数的 NaN 分支是为升序写的，降序调用会反转语义（无日期置顶）。
     任何降序排序一律使用 _dateCmpDesc，不要反向调用本函数。 */
  function _dateCmp(x,y){
    var tx=x?new Date(x).getTime():NaN, ty=y?new Date(y).getTime():NaN;
    if(isNaN(tx)&&isNaN(ty)) return 0;
    if(isNaN(tx)) return 1;
    if(isNaN(ty)) return -1;
    return tx-ty;
  }
  /* 时区安全日期比较【仅用于降序】：无日期一律沉底，语义与调用方向无关。
     修复前 newest/active 反向调用 _dateCmp，导致 201 条无日期条目被顶到首屏。 */
  function _dateCmpDesc(x,y){
    var tx=x?new Date(x).getTime():NaN, ty=y?new Date(y).getTime():NaN;
    if(isNaN(tx)&&isNaN(ty)) return 0;
    if(isNaN(tx)) return 1;      /* x 无日期 → x 靠后 */
    if(isNaN(ty)) return -1;
    if(tx===ty) return 0;
    return tx<ty?1:-1;           /* 新的在前 */
  }
  /* F1 修复：'active' 按信源最近更新时间排序 */
  function applySort(){
    if(sortMode==='oldest') ART.sort(function(a,b){ return _dateCmp(a.date,b.date); });
    else if(sortMode==='active'){
      var srcLatest={};
      ART.forEach(function(a){ if(a.date){ var cur=srcLatest[a.sk]; if(!cur||_dateCmp(a.date,cur)>0) srcLatest[a.sk]=a.date; }});
      ART.sort(function(a,b){
        var sa=srcLatest[a.sk]||'', sb=srcLatest[b.sk]||'';
        /* _dateCmpDesc 自身即为降序语义，参数不可再反转（反转会使无日期源置顶） */
        if(sa!==sb) return _dateCmpDesc(sa,sb);
        return _dateCmpDesc(a.date,b.date);
      });
      _applyRunCap(ART, 3, SOURCE_FAMILIES);
    }
    /* 集成契约（对齐 docs/superpowers/plans/2026-09-14-composite-sort-spec.md Phase 5）：
       - sortMode 单值 → diverse 与 topic 天然互斥，不可能同时触发
       - diverse 只重排窗口内次序；tierInterleave() 由合并/刷新路径在其之后调用，不受影响
       - topic 分支（由主题聚类方案实现）不得调用 weightedShuffle，主题视图自带分桶逻辑 */
    else if(sortMode==='diverse'){
      var _qMap = (ANALYSIS_DATA && ANALYSIS_DATA.quality) ? ANALYSIS_DATA.quality : {};
      var _win = (typeof DIVERSE_WINDOW !== 'undefined') ? DIVERSE_WINDOW : 120;
      /* 原地替换而非重新绑定 ART：window.ART / 各闭包持有的是同一个数组引用，
         若在此处整体替换引用，后续重排对持有旧引用的调用方不可见。 */
      var _sh = weightedShuffle(ART, _win, _qMap);
      ART.length = 0;
      for(var _i=0;_i<_sh.length;_i++) ART.push(_sh[_i]);
    }
    else if(sortMode==='quality' && ANALYSIS_DATA && ANALYSIS_DATA.quality){
      var qm=ANALYSIS_DATA.quality;
      ART.sort(function(a,b){ return (qm[b.sk]||0)-(qm[a.sk]||0); });
      _applyRunCap(ART, 5, SOURCE_FAMILIES);
    }
    else {
      ART.sort(function(a,b){ return _dateCmpDesc(a.date,b.date); });
      _applyRunCap(ART, 3, SOURCE_FAMILIES);
    }
  }
  var _sortEl = document.getElementById('sortSelect');
  if(_sortEl){ _sortEl.value=sortMode; _sortEl.addEventListener('change',function(){ sortMode=_normSortMode(this.value); localStorage.setItem('rss_sort_mode',sortMode); applySort(); wallLimit=WALL_STEP; curArt=null; renderWall(); }); }
  /* A6 修复：中文阅读速度约 400 字/分钟 */
  function estRead(a){ var mins=Math.max(1,Math.round((a.s||'').length/400)); return mins+' min'; }

  /* ── 信誉打散：时间窗口内加权采样 + 同源抑制，避免单源霸屏 ── */
  /* 同源抑制间隔（issue C1）：同一源在最近 SRC_GAP 位内出现过的，本轮不再选。
     缺陷背景：旧实现只有「窗口内按权重重排」，没有任何抑制同源相邻的项 ——
     「打散」（避免单源霸屏）等于没做。

     可证的界（精确表述，不要读成「最长连续恒 ≤ 2」）：
       · 池非空 ⇒ 同一源在最近 SRC_GAP 位内不会被再次选中 ⇒ 相邻必异源。
       · 池为空（组内剩余条目的源都恰好落在最近 SRC_GAP 位里）⇒ 退化为
         「最久未出现优先」；若此时只剩单一源，连出长度 = 该源剩余条目数，不可避。
     实测（2026-09-14 快照，9092 条 / 509 源）：
       前 200 位同源最长连续 9（纯时间降序）→ 1；前 20 位单源最多 13 → 4；
       前 200 位覆盖源数 36；平均时间位移与旧实现持平（103.1 vs 103.1）。
       残留：全长最长连出 11（人民网，第 7480 位 / 82% 深处），落在 g28 组内
       （该组 140 条 / 57 源 / 人民网 46 条，构成**可行**：46 ≤ floor(142/3)=47），
       根因是贪心消耗不均衡使组尾塌成单源，不是数据所迫。
       已实测的替代规则（池内改为「剩余量优先」）能把全程 maxRun 压到 1，
       但前 200 位单源占比 12%→25%、覆盖源数 36→13、前 20 位源质量均值 82.45→78.08，
       牺牲首屏多样性换深层不可见指标，净亏，故不改，如实记录为已知限制。

     报告建议的 seeded PRNG 解决不了它：随机化不提供「不相邻」的任何保证，
     而且按 quality 加权采样会把首屏推向纯降序（前轮读数：首屏均值 99.26 vs
     时间序 82.20，本轮未复测），与「保持时间可读性」冲突。
     只有位置约束能给出可证的界。 */

  /* 源族群定义：同族源共享打散配额，防止 V2EX 4 子源交替出现霸屏。
     key = source_key, value = family_key。未列出的源不参与族群 cap。 */
  var SOURCE_FAMILIES = {
    'v2ex_all_50': 'v2ex', 'v2ex技术_44': 'v2ex',
    'nodeseek_54': 'nodeseek'
  };
  /* 族群级 cap：family_key → 最大连续条数。未列出的族用单源 cap 值。 */
  var FAMILY_CAPS = { 'v2ex': 6, 'nodeseek': 4 };

  /* 同源连续抑制（run cap）：扫描已排序数组，将同源连续超过 cap 的
     多余条目与后续最近的异源条目交换。O(n) 典型 / O(n²) 最坏（3-pass 有界），
     不改变数组长度或元素集合。
     Level 1: 单源 cap（原有逻辑，用 _gk 替代直接 sk 比较）
     Level 2: 族群 cap（families[sk] 存在时，用 FAMILY_CAPS[gk] 作为上限）
     用于 active / newest / quality 模式，作为 weightedShuffle SRC_GAP
     之外的轻量补充（diverse 模式不需要，已有 SRC_GAP=2）。 */
  function _applyRunCap(arr, cap, families) {
    if (!arr || arr.length <= cap) return;
    families = families || {};
    var _gk = function(sk) { return families[sk] || sk; };
    var n = arr.length;
    for (var pass = 0; pass < 3; pass++) {
      var improved = false;
      for (var i = cap; i < n; i++) {
        var sk = arr[i].sk;
        var gk = _gk(sk);

        // ── Level 1: 单源 cap ──
        if (arr[i].sk === arr[i-1].sk) {
          var runStart = i - 1;
          while (runStart > 0 && arr[runStart-1].sk === sk) runStart--;
          var runLen = i - runStart + 1;
          if (runLen > cap) {
            var swapped = false;
            for (var j = i + 1; j < Math.min(i + 15, n); j++) {
              if (_gk(arr[j].sk) !== gk && (j === 0 || _gk(arr[j].sk) !== _gk(arr[j-1].sk))) {
                var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
                swapped = true; improved = true; break;
              }
            }
            if (!swapped) {
              for (var j = i - 1; j >= 0; j--) {
                if (_gk(arr[j].sk) !== gk) {
                  var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
                  improved = true; break;
                }
              }
            }
            continue;
          }
        }

        // ── Level 2: 族群 cap ──
        if (families[sk]) {
          var famCap = (typeof FAMILY_CAPS !== 'undefined' && FAMILY_CAPS[gk]) || cap;
          if (_gk(arr[i-1].sk) === gk) {
            var fStart = i - 1;
            while (fStart > 0 && _gk(arr[fStart-1].sk) === gk) fStart--;
            var fLen = i - fStart + 1;
            if (fLen > famCap) {
              var fSwapped = false;
              for (var j = i + 1; j < Math.min(i + 20, n); j++) {
                if (_gk(arr[j].sk) !== gk) {
                  var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
                  fSwapped = true; improved = true; break;
                }
              }
              if (!fSwapped) {
                for (var j = i - 1; j >= 0; j--) {
                  if (_gk(arr[j].sk) !== gk) {
                    var tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
                    improved = true; break;
                  }
                }
              }
            }
          }
        }
      }
      if (!improved) break;
    }
  }

  var SRC_GAP = 2;

  /* 距末尾最近一次出现的距离；从未出现返回大值（恒可用）。
     用 lastAt 映射做到 O(1)，避免每次重扫输出数组。 */
  function _srcGap(lastAt, len, sk) {
    var at = lastAt[sk];
    return (at === undefined) ? 1e9 : (len - at);
  }

  var TIER_MULT = { 1: 1.5, 2: 1.2 };
  function _srcWeight(x, qm) {
    var q = (qm[x.sk] !== undefined && qm[x.sk] > 0) ? qm[x.sk] : 50;
    var tm = TIER_MULT[x.ti] || 1.0;
    return q * tm;
  }

  function weightedShuffle(articles, windowMinutes, qualityMap) {
    if (!articles || articles.length <= 1) return articles ? articles.slice() : [];
    if (!windowMinutes || windowMinutes <= 0) {
      // 窗口=0 → 纯时间降序（退化为现有行为）
      return articles.slice().sort(function(a,b){ return _dateCmpDesc(a.date, b.date); });
    }
    var qm = qualityMap || {};
    // 1. 先按时间降序排列（拷贝，不改原数组）
    var sorted = articles.slice().sort(function(a,b){ return _dateCmpDesc(a.date, b.date); });
    // 2. 锚定分段：组内每一条与组锚点（组内第一篇）的时间差 ≤ 窗口
    var groups = [], cur = [sorted[0]];
    for (var i = 1; i < sorted.length; i++) {
      var tBase = new Date(cur[0].date).getTime();
      var tCur  = new Date(sorted[i].date).getTime();
      if (!isNaN(tBase) && !isNaN(tCur) && (tBase - tCur) <= windowMinutes * 60000) {
        cur.push(sorted[i]);
      } else {
        groups.push(cur);
        cur = [sorted[i]];
      }
    }
    groups.push(cur);
    // 3. 窗口内加权采样（确定性 + 同源抑制）
    var result = [];
    var lastAt = {};   // sk → 上一次进入 result 的下标（跨组保留，防跨组边界处连出）
    for (var g = 0; g < groups.length; g++) {
      var remaining = groups[g].slice();
      while (remaining.length > 0) {
        // 3a 先取「最近 SRC_GAP 位内未出现」的条目作为可选池
        var pool = [], k, gk;
        for (k = 0; k < remaining.length; k++) {
          if (_srcGap(lastAt, result.length, remaining[k].sk) > SRC_GAP) pool.push(remaining[k]);
        }
        if (!pool.length) {
          // 3b 可选池为空：该源在本窗口占绝对多数，异源条目已用尽。
          //     退而取「最久未出现」的源（而不是接着选刚用过的源），
          //     否则会用 fallback 把刚压下去的连续段重新接起来。
          var maxGap = -1;
          for (k = 0; k < remaining.length; k++) {
            gk = _srcGap(lastAt, result.length, remaining[k].sk);
            if (gk > maxGap) maxGap = gk;
          }
          for (k = 0; k < remaining.length; k++) {
            if (_srcGap(lastAt, result.length, remaining[k].sk) === maxGap) pool.push(remaining[k]);
          }
        }
        // 3c 池内确定性加权选取
        var totalW = 0;
        for (k = 0; k < pool.length; k++) totalW += _srcWeight(pool[k], qm);
        var chosen;
        if (totalW <= 0) {
          chosen = pool[0];       // 所有权重为 0 → 按输入顺序取
        } else {
          /* 取「累计权重跨过总量一半」的那一个（池内顺序上的权重中位点）。
             注意这是确定性算法，不是随机采样：同一输入必然产出同一排列（D8 守卫）。
             本注释修正自旧的「使用确定性伪随机」—— 旧实现里没有任何 PRNG，注释与实现不符。 */
          var threshold = totalW * 0.5, cumW = 0, picked = -1;
          for (k = 0; k < pool.length; k++) {
            cumW += _srcWeight(pool[k], qm);
            if (cumW >= threshold && picked < 0) picked = k;
          }
          chosen = pool[picked < 0 ? 0 : picked];
        }
        result.push(chosen);
        lastAt[chosen.sk] = result.length - 1;
        for (k = 0; k < remaining.length; k++) {
          if (remaining[k] === chosen) { remaining.splice(k, 1); break; }
        }
      }
    }
    return result;
  }
  /* ── weightedShuffle end ── */

  /* ── 分层交织：每 4 篇高频文章穿插 1 篇低频文章 ─ */
  function tierInterleave(){
    var hi=[], lo=[];
    ART.forEach(function(a){ (a.ti<=2 ? hi : lo).push(a); });
    var result=[], i=0, j=0;
    while(i<hi.length || j<lo.length){
      var he=Math.min(4, hi.length-i);
      for(var k=0;k<he;k++) result.push(hi[i++]);
      if(j<lo.length) result.push(lo[j++]);
    }
    ART=result;
  }

  /* ── State ── */
  var visited = {};
  try { visited = JSON.parse(localStorage.getItem('rss_read_v2')||'{}'); } catch(e){}
  var filter = {type:'all', cats:{}, src:null, unreadOnly:false, filterBm:false};
  var curArt = null;
  window.curArt = null;
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
    // 收藏芯片置首：手机端 chips 横向滚动时保证入口始终可见
    var bmCnt=Object.keys(_bookmarks).length;
    if(bmCnt>0) h+='<button class="chip bm-chip'+(filter.filterBm?' on':'')+'" id="bmChip" onclick="toggleBmFilter()">\u2605 \u6536\u85cf <span class="n">'+bmCnt+'</span></button>';
    CAT_ORDER.forEach(function(c){
      if(!counts[c]) return;
      var label=CAT_LABELS[c]||c, on=filter.type==='cat'&&filter.cats[c];
      h+='<button class="chip'+(on?' on':'')+'" data-c="'+c+'">'+label+' <span class="n">'+counts[c]+'</span></button>';
    });
    /* ── 发现：长尾高质量源 ── */
    var _discKeys=_discoverKeys();
    var discCnt=0; ART.forEach(function(a){if(_discKeys[a.sk])discCnt++;});
    if(discCnt>0){
      var discOn=filter.type==='discover';
      h+='<button class="chip disc-chip'+(discOn?' on':'')+'" data-c="__disc">\U0001f50d \u53d1\u73b0 <span class="n">'+discCnt+'</span></button>';
    }
    document.getElementById('chips').innerHTML=h;
    document.querySelectorAll('.chip').forEach(function(el){
      if(!el.dataset.c) return; /* \u65e0 data-c \u7684\u82af\u7247\uff08\u5982\u6536\u85cf\uff09\u4fdd\u7559\u5185\u8054 onclick\uff0c\u907f\u514d\u8986\u76d6\u6210 c=undefined */
      el.onclick=function(){
        var c=this.dataset.c, uo=filter.unreadOnly, bm=filter.filterBm;
        /* 发现 chip：切换 discover 模式 */
        if(c==='__disc'){
          filter=filter.type==='discover'?{type:'all',cats:{},unreadOnly:uo,filterBm:bm}:{type:'discover',unreadOnly:uo,filterBm:bm};
          curArt=null; wallLimit=WALL_STEP; renderChips(); renderWall(); renderPanel(); window.scrollTo({top:0}); updateTitle(); updateHash(); updateUnreadBtn(); updateBmChip();
          return;
        }
        var cats=filter.type==='cat'?Object.assign({},filter.cats):{};
        if(cats[c]) delete cats[c]; else cats[c]=true;
        var keys=Object.keys(cats);
        if(keys.length===0) filter={type:'all',cats:{},unreadOnly:uo,filterBm:bm};
        else filter={type:'cat',cats:cats,src:null,unreadOnly:uo,filterBm:bm};
        curArt=null; wallLimit=WALL_STEP; renderChips(); renderWall(); renderPanel(); window.scrollTo({top:0}); updateTitle(); updateHash(); updateUnreadBtn(); updateBmChip();
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
  function updateTitle(){
    var h1=document.querySelector('.toolbar h1');
    if(!h1)return;
    if(filter.type==='cat'){var labels=Object.keys(filter.cats).map(function(c){return CAT_LABELS[c]||c;});h1.textContent=labels.join(' + ');}
    else if(filter.type==='src'){var s=SRC_OBJ(filter.src);h1.textContent=s?s.name:'\u4fe1\u6e90';}
    else if(filter.type==='discover'){h1.textContent='\U0001f50d \u53d1\u73b0';}
    else h1.textContent='\u65f6\u95f4\u7ebf';
  }
  function updateMeta(){
    var el=document.getElementById('toolMeta');if(!el)return;
    if(globalSearch){
      var list=visibleArts();
      el.textContent='\u547d\u4e2d '+list.length+' \u7bc7';return;
    }
    var ftN=ART.filter(function(a){return(a.s||'').length>60;}).length;
    el.textContent=ART.length+' \u7bc7 \u00b7 \u5168\u6587\u8986\u76d6 '+ftN+'/'+ART.length;
  }
  function SRC_OBJ(k){ return SOURCES.find(function(s){return s.key===k}); }
  window.clearSrcF=function(e){e.stopPropagation();var uo=filter.unreadOnly,bm=filter.filterBm;filter={type:'all',unreadOnly:uo,filterBm:bm};curArt=null;window.curArt=null;wallLimit=WALL_STEP;renderChips();renderWall();renderPanel();updateTitle();updateHash();updateUnreadBtn();updateBmChip();};

  /* ── Source panel ── */
  var _srcBtnEl=null;
  function toggleSrcPanel(){
    var opening=!document.body.classList.contains('src-open');
    if(opening)_srcBtnEl=document.activeElement;
    document.body.classList.toggle('src-open');
    if(opening){var si=document.getElementById('spSearch');if(si)si.focus();}
    else{if(_srcBtnEl){try{_srcBtnEl.focus();}catch(e){}_srcBtnEl=null;}}
  }
  window.toggleSrcPanel=toggleSrcPanel;
  /* API 压缩条目(t/s/u/d) → src.items 形状，且**必须与构建期那一条合并**而不是整条替换。
     ?source= 这一路（api/rss.js 的 fetchOne）不发封面、不发话题标签、不发坏日期标记、
     不发全文、也不做标题翻译；直接替换会让刷新过的卡片掉封面/掉标签/退回英文标题
     （2026-09-21 对抗审查 C1/C2）。另外上游这次没给 pubDate 时 API 的 d 是**抓取时刻**，
     盖到构建期已知的真实发布时间上，等于一边冒充发布时间一边让运行时永不淘汰它。
     翻译类字段（title_zh/summary_zh）只在"原文没变"时才沿用旧的，否则上游改了标题
     会永远显示旧译文。date_fallback 与快照同用长名。
     判据：tests/rss_history/test_dfb_drawer_map_wire.py（真跑函数，含脏 link 那一类）。 */
  function _apiMergeTo(oldItems){
    var by={}; (oldItems||[]).forEach(function(o){ if(o&&o.link) by[o.link]=o; });
    return function(it){
      var o=by[it.u]||null;
      var t=it.t||'';
      var a={title:t,title_zh:t,summary_zh:it.s||'',link:it.u||'#',pub_date:it.d||'',
             fc:it.fc||'',image:it.img||'',mu:it.mu||'',mt:it.mt||'',
             date_fallback:!!it.date_fallback};
      if(!o) return a;
      if(o.title===t&&o.title_zh) a.title_zh=o.title_zh;
      if(o.summary===it.s&&o.summary_zh) a.summary_zh=o.summary_zh;
      if(!a.image&&o.image) a.image=o.image;
      if(!a.fc&&o.full_content) a.fc=o.full_content;
      if(!a.mu&&(o.media_url||o.mu)){a.mu=o.media_url||o.mu;a.mt=o.media_type||o.mt||'';}
      if(o.tags) a.tags=o.tags;
      if(o.bad_date) a.bad_date=o.bad_date;
      /* time_str 已不再进产物（ITEM_DERIVED_FIELDS）：标签由 pub_date + BUILD_TS 现算，
         这里再搬一次等于把"每场全量重写"从后门搬回来。发布时间本身的正确性由下一行管。 */
      if(a.date_fallback&&o.pub_date&&!o.date_fallback){a.pub_date=o.pub_date;a.date_fallback=false;}
      return a;
    };
  }
  function selectSrc(key){
    var uo=filter.unreadOnly,bm=filter.filterBm;
    if(!key){filter={type:'all',unreadOnly:uo,filterBm:bm};} else {filter={type:'src',src:key,unreadOnly:uo,filterBm:bm};}
    curArt=null; wallLimit=WALL_STEP; document.body.classList.remove('src-open');
    renderChips(); renderWall(); renderPanel(); window.scrollTo({top:0}); updateTitle(); updateHash(); updateUnreadBtn(); updateBmChip();
    /* 单源实时刷新：后台静默拉取该源最新内容，成功则替换并重建，失败无感知 */
    if(key){
      fetch('https://starhub-refresh.vercel.app/api/rss?source='+encodeURIComponent(key),
        (function(){var c=window.AbortController?new AbortController():null;if(c)setTimeout(function(){c.abort();},8000);return c?{signal:c.signal}:{};}())
      )
      .then(function(r){return r.ok?r.json():null;})
      .then(function(data){
        if(!data||!data.items||!data.items.length) return;
        var src=SOURCES.find(function(s){return s.key===key;});
        if(!src) return;
        /* 映射 API 字段(t/s/u/d)到 buildArt 期望格式，按 link 去重 */
        var have={}; data.items.forEach(function(it){if(it.u&&it.u!=='#')have[it.u]=1;});
        var oldItems=src.items.filter(function(it){return !it.link||!have[it.link];});
        var newItems=data.items.map(_apiMergeTo(src.items));
        src.items=newItems.concat(oldItems);
        buildArt(); renderChips(); renderWall(); renderPanel();
        var n=data.items.length; toast('已更新 '+src.name+'（'+n+' 篇最新）');
      }).catch(function(){});
    }
  }
  window.selectSrc=selectSrc;
  function renderPanel(){
    var q=(document.getElementById('spSearch').value||'').trim().toLowerCase();
    // 信源健康度总览
    var healthHtml='';
    if(ANALYSIS_DATA&&ANALYSIS_DATA.quality){
      var qm=ANALYSIS_DATA.quality,vals=Object.values(qm),n=vals.length;
      if(n>0){
        var avg=vals.reduce(function(a,b){return a+b;},0)/n;
        var good=vals.filter(function(v){return v>=70;}).length;
        var warn=vals.filter(function(v){return v>=40&&v<70;}).length;
        var bad=vals.filter(function(v){return v<40;}).length;
        healthHtml='<div class="sp-health">';
        healthHtml+='<span class="sp-health-dot good"></span>'+good+' \u5065\u5eb7';
        healthHtml+='<span class="sp-health-dot warn"></span>'+warn+' \u8b66\u544a';
        healthHtml+='<span class="sp-health-dot bad"></span>'+bad+' \u5f02\u5e38';
        healthHtml+='<span style="margin-left:auto;color:var(--faint)">\u5747\u5206 '+Math.round(avg)+'</span>';
        healthHtml+='</div>';
      }
    }
    var h=healthHtml+'<div class="sp-all'+(filter.type!=='src'?' on':'')+'" onclick="selectSrc(null)">\u2630 \u5168\u90e8\u4fe1\u6e90<span class="n">'+ART.length+'</span></div>';
    var byCat={};
    SOURCES.forEach(function(s){
      if(!q||s.name.toLowerCase().indexOf(q)>=0){
        var cnt=s.items.length;
        if(cnt>0)(byCat[s.cat]=byCat[s.cat]||[]).push(s);
      }
    });
    Object.keys(byCat).forEach(function(c){
      byCat[c].sort(function(a,b){ return b.items.length - a.items.length; });
    });
    var any=false;
    CAT_ORDER.forEach(function(c){
      var arr=byCat[c]; if(!arr||!arr.length) return; any=true;
      var label=CAT_LABELS[c]||c;
      h+='<div class="sp-cat" data-cat="'+c+'"><span class="arrow">\u25bc</span>'+label+'<span class="n">'+arr.length+'</span></div>';
      h+='<div class="sp-cat-body" data-body="'+c+'">';
      arr.forEach(function(s){
        var on=filter.type==='src'&&filter.src===s.key;
        h+='<div class="sp-src'+(on?' on':'')+'" data-k="'+s.key+'"><span class="src-dot" style="--sc:'+s.color+'"></span><span class="nm">'+esc(s.name)+'</span><span class="n">'+s.items.length+'</span>';
        if(ANALYSIS_DATA&&ANALYSIS_DATA.quality){var qs=ANALYSIS_DATA.quality[s.key];if(qs!=null){var qc=qs>=70?'sq-high':qs>=40?'sq-mid':'sq-low';h+='<span class="sq-badge '+qc+'">'+Math.round(qs)+'</span>';}}
        h+='</div>';
      });
      h+='</div>';
    });
    if(!any) h+='<div class="sp-none">\u6ca1\u6709\u5339\u914d\u300c'+esc(q)+'\u300d\u7684\u4fe1\u6e90</div>';
    document.getElementById('spList').innerHTML=h;
    document.getElementById('spSearch').oninput=function(){ renderPanel(); };
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
  var globalSearch = '';
  var _searchSrcMatch = null; // 搜索匹配到的信源 key
  var _topicBigrams = null;   // 话题标签二元组（insightSearch 设置）
  /* 长尾高质量源 key 集合：条目 ≤5 且 quality ≥60 */
  function _discoverKeys(){
    var srcCnt={};
    ART.forEach(function(a){srcCnt[a.sk]=(srcCnt[a.sk]||0)+1;});
    var qm=(ANALYSIS_DATA&&ANALYSIS_DATA.quality)?ANALYSIS_DATA.quality:{};
    var ks={};
    for(var sk in srcCnt){
      if(srcCnt[sk]<=5 && (qm[sk]||0)>=60) ks[sk]=true;
    }
    return ks;
  }
  function visibleArts(){
    var q = globalSearch;
    var _dk=(filter.type==='discover')?_discoverKeys():null;
    return ART.filter(function(a){
      if(filter.type==='cat' && !filter.cats[a.c]) return false;
      if(filter.type==='src' && a.sk!==filter.src) return false;
      if(filter.type==='discover' && !_dk[a.sk]) return false;
      if(filter.filterBm && !_bookmarks[artKey(a)]) return false;
      if(filter.unreadOnly && visited[artKey(a)]) return false;
      if(q) {
        if(_searchSrcMatch) return a.sk === _searchSrcMatch;
        if(_topicBigrams&&_topicBigrams.length>=2){
          var tl=(a.t||'').toLowerCase(),hits=0;
          for(var i=0;i<_topicBigrams.length;i++){if(tl.indexOf(_topicBigrams[i])>=0)hits++;}
          return hits>=2&&hits/_topicBigrams.length>=0.15;
        }
        var ql=q.toLowerCase(); return (a.t||'').toLowerCase().indexOf(ql)>=0 || (a.s||'').toLowerCase().indexOf(ql)>=0 || (a.src||'').toLowerCase().indexOf(ql)>=0;
      }
      return true;
    });
  }
  function toggleUnread(){
    filter.unreadOnly=!filter.unreadOnly;
    wallLimit=WALL_STEP; curArt=null;
    renderWall(); renderPanel(); updateUnreadBtn();
    var em=document.getElementById('wall').querySelector('.empty-hint');
    if(filter.unreadOnly && em) em.textContent='\u6240\u6709\u6587\u7ae0\u5df2\u8bfb';
  }
  /* U1 修复：未读计数 */
  function _countUnread(){ var n=0; ART.forEach(function(a){ if(!visited[artKey(a)]) n++; }); return n; }
  function updateUnreadBtn(){
    var b=document.getElementById('unreadToggle');
    if(!b) return;
    b.classList.toggle('on',filter.unreadOnly);
    /* A5 修复：aria-pressed */
    b.setAttribute('aria-pressed', filter.unreadOnly?'true':'false');
    /* U1 修复：显示未读计数 */
    var cnt=_countUnread();
    var cntEl=b.querySelector('.unread-cnt');
    if(!cntEl){ cntEl=document.createElement('span'); cntEl.className='unread-cnt'; cntEl.style.cssText='font-family:var(--mono);font-size:10px;opacity:.8;margin-left:2px;'; b.appendChild(cntEl); }
    cntEl.textContent=cnt>0?' '+cnt:'';
    _updateMarkAllReadBtn();
  }
  var FS_KEY='rss_reader_fontsize', FS_SIZES=['sm','md','lg'];
  function setFontSize(sz){
    var body=document.getElementById('r2Body');
    if(!body) return;
    body.classList.remove('fs-sm','fs-md','fs-lg');
    body.classList.add('fs-'+sz);
    try{localStorage.setItem(FS_KEY,sz);}catch(e){}
    var btns=document.querySelectorAll('.r2-fs-btn');
    for(var i=0;i<btns.length;i++) btns[i].classList.toggle('active',FS_SIZES[i]===sz);
  }
  function initFontSize(){
    var sz='md';
    try{var s=localStorage.getItem(FS_KEY);if(s&&FS_SIZES.indexOf(s)>=0)sz=s;}catch(e){}
    var body=document.getElementById('r2Body');
    if(body) body.classList.add('fs-'+sz);
    var btns=document.querySelectorAll('.r2-fs-btn');
    for(var i=0;i<btns.length;i++) btns[i].classList.toggle('active',FS_SIZES[i]===sz);
  }
  /* A8 修复：BM_MAX 死代码已移除 */
  var _bookmarks={}, BM_KEY='rss_bookmarks';
  function loadBookmarks(){try{_bookmarks=JSON.parse(localStorage.getItem(BM_KEY)||'{}');}catch(e){_bookmarks={};}}
  function saveBookmarks(){try{localStorage.setItem(BM_KEY,JSON.stringify(_bookmarks));}catch(e){}}
  function isBookmarked(k){return !!_bookmarks[k];}
  function toggleBookmark(a){
    var k=artKey(a);
    if(_bookmarks[k]){delete _bookmarks[k];}
    else{_bookmarks[k]={t:a.t,u:a.u,src:a.src,sk:a.sk,c:a.c,sc:a.sc,time:a.time};}
    saveBookmarks();
    var cards=document.querySelectorAll('#wall .card');
    for(var i=0;i<cards.length;i++){
      var b=cards[i].querySelector('.bm-btn');
      if(b&&cards[i].dataset.k===k) b.classList.toggle('on',!!_bookmarks[k]);
    }
    updateBmBtn();
    renderChips();
    if(filter.filterBm){wallLimit=WALL_STEP;renderWall();renderPanel();}
  }
  function toggleBmFilter(){
    filter.filterBm=!filter.filterBm;
    wallLimit=WALL_STEP; curArt=null;
    renderWall(); renderPanel(); updateBmChip();
  }
  function updateBmBtn(){
    var b=document.getElementById('r2Bm');
    if(!b||!curArt) return;
    var on=isBookmarked(artKey(curArt));
    b.classList.toggle('on',on);
    var sp=b.querySelector('span');
    if(sp) sp.textContent=on?'\u5df2\u6536\u85cf':'\u6536\u85cf';
  }
  function updateBmChip(){
    var b=document.getElementById('bmChip');
    if(b) b.classList.toggle('on',filter.filterBm);
  }
  function highlightEsc(text,q){
    var e=esc(text);if(!q)return e;
    var re=new RegExp('('+q.replace(/[.*+?^${}()|[\\\\]/g,'\\\\$&')+')','gi');
    return e.replace(re,'<mark>$1</mark>');
  }
  function artKey(a){ return a.sk+'|'+(a.u&&a.u!=='#'?a.u:a.t); }
  /* 封面 referrer 策略。默认仍 `no-referrer`（多数图床靠它避免 Referer 泄露），但**有的图床
     是"不带 Referer 就不给图"**：2026-10-01 实测 `images1.caifuzhongwen.com` 带 Referer 即 200、
     不带 403 —— 全局一刀切等于自己把图关掉（用户记录里把这称为"已确认的一处自我伤害"）。
     名单按**主机名后缀**匹配，绝不做子串匹配，否则 `http://evil.com/?x=caifuzhongwen.com` 会冒充命中。
     判据：tests/site_nav/test_orphan_requests.py（含那条冒充样本与"改成子串匹配"的变异）。 */
  var _REF_HOSTS = ['caifuzhongwen.com'];
  var _BAD_COVERS = """ + json.dumps(sorted(_BAD_COVER_HOSTS), ensure_ascii=False) + """;
  function _coverHost(u){
    /* 必须 trim + 整体小写：Python 侧 _cover_host 是 `partition('//')[0].lower()` 判 scheme
       再把 host 小写，大写 scheme（HTTPS://…）在两边都该算同一个 host。
       少这一步就是"构建期判空、渲染层放行"的分叉，与今天上午修掉的翻译判据同一种形状。 */
    var s = String(u || '').trim().toLowerCase();
    if (s.lastIndexOf('http://', 0) !== 0 && s.lastIndexOf('https://', 0) !== 0) return '';
    var rest = s.substring(s.indexOf('//') + 2);
    return ((rest.split('/')[0] || '').split('@').pop().split(':')[0] || '');
  }
  /* 精确或作为子域命中；空 host 不命中。与构建期 Python 侧 _host_matched 同一条规则。 */
  function _hostInTable(host, table){
    if (!host) return false;
    for (var i = 0; i < table.length; i++) {
      var d = table[i];
      if (host === d || host.slice(-(d.length + 1)) === '.' + d) return true;
    }
    return false;
  }
  /* 渲染层再过滤一次必挂域名：构建期已把封面写空，这里兜的是实时链路
     (?source= / ?batch=) 现抓回来的条目。表只有一份，住在 _BAD_COVER_HOSTS，
     由 _BAD_COVERS 注入 —— 实时侧再抄一份就是第二个会分叉的事实来源。 */
  function _dropBadCover(u){
    return _hostInTable(_coverHost(u), _BAD_COVERS) ? '' : (u || '');
  }
  function _coverRefPolicy(u){
    return _hostInTable(_coverHost(u), _REF_HOSTS) ? '' : 'no-referrer';
  }
  function renderWall(){
    var list=visibleArts(), wall=document.getElementById('wall');
    if(!list.length){
      var em=globalSearch?(_searchSrcMatch?'\u4fe1\u6e90 \u00ab'+esc(SRC_OBJ(_searchSrcMatch)?SRC_OBJ(_searchSrcMatch).name:globalSearch)+'\u00bb \u6682\u65e0\u6587\u7ae0':'\u672a\u627e\u5230\u4e0e\u300c'+esc(globalSearch)+'\u300d\u76f8\u5173\u7684\u6587\u7ae0'):(filter.filterBm?'\u6682\u65e0\u6536\u85cf\u6587\u7ae0':(filter.unreadOnly?'\u6240\u6709\u6587\u7ae0\u5df2\u8bfb':'\u8be5\u7b5b\u9009\u4e0b\u6ca1\u6709\u6587\u7ae0'));
      /* U8 修复：空态加 CTA 按钮 */
      var cta='';
      if(globalSearch) cta='<br><button onclick="document.getElementById(&quot;globalSearchClear&quot;).click()" style="margin-top:8px;padding:6px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:13px;cursor:pointer;font-family:var(--body)">清除搜索</button>';
      else if(filter.type==='cat'||filter.type==='src') cta='<br><button onclick="clearSrcF(event)" style="margin-top:8px;padding:6px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:13px;cursor:pointer;font-family:var(--body)">查看全部文章</button>';
      else if(filter.unreadOnly) cta='<br><button onclick="toggleUnread()" style="margin-top:8px;padding:6px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:13px;cursor:pointer;font-family:var(--body)">显示全部文章</button>';
      wall.innerHTML='<div class="empty-hint">'+em+cta+'</div>';
      wallLimit=0;
      return;
    }
    var end=Math.min(wallLimit, list.length);
    var h='';
    if(globalSearch) {
      var bannerTxt = _searchSrcMatch ? '\u4fe1\u6e90 \u00ab' + esc(SRC_OBJ(_searchSrcMatch)?SRC_OBJ(_searchSrcMatch).name:globalSearch) + '\u00bb \u2014 ' + list.length + ' \u7bc7' : '\u641c\u7d22 \u00ab' + esc(globalSearch) + '\u00bb \u2014 \u547d\u4e2d ' + list.length + ' \u7bc7';
      h += '<div class="search-banner">' + bannerTxt + '</div>';
    }
    for(var i=0;i<end;i++){
      var a=list[i], k=artKey(a), isVis=!!visited[k];
      var isOpen=curArt&&artKey(curArt)===k;
      var _cv=_dropBadCover(a.img);
      var hasImg=!!_cv;
      /* 有可用封面图才走封面卡；无图（或被必挂域名表过滤掉）回退纯文字紧凑卡，避免渐变占位浪费空间 */
      /* A1 修复：卡片加 tabindex 使键盘可达 */
      h+='<article class="card'+(hasImg?' cover-card':'')+(isVis?' visited':'')+(isOpen?' open':'')+'" data-k="'+esc(k)+'" tabindex="0" style="--cc:var(--cat-'+a.c+')">';
      if(hasImg){
        h+='<a class="cover" href="'+esc(a.u)+'" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true" onclick="event.stopPropagation()">';
        h+='<span class="cover-fallback">'+esc((a.t||'#').charAt(0).toUpperCase())+'</span>';
        // 取不到就摘掉自己，露出下面那张 .cover-fallback 首字占位；
        // 线上实测新华网 OSS 是 http=502/https TLS 失败、BBC 图在大陆直接连不通，
        // 没有这一句时卡片上就是一个破图。
        h+='<img class="cover-img" src="'+esc(_cv)+'" alt="" loading="lazy" decoding="async" referrerpolicy="'+esc(_coverRefPolicy(_cv))+'" onerror="this.remove()">';
        h+='</a>';
      }
      h+='<div class="card-top"><span class="cat-tag" style="color:var(--cat-'+a.c+')">'+(CAT_LABELS[a.c]||a.c)+'</span>';
      h+='<span class="card-time" title="'+esc(a.date||'')+'">'+_dynTime(a)+'</span>';
      h+='<button class="bm-btn'+(isBookmarked(k)?' on':'')+'" data-k="'+esc(k)+'" title="\u6536\u85cf"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg></button>';
      h+='<a class="ext-btn" href="'+esc(a.u)+'" target="_blank" rel="noopener" title="\u539f\u7ad9" onclick="event.stopPropagation()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/></svg></a></div>';
      h+='<h3 class="card-title">'+highlightEsc(a.t,globalSearch)+'</h3>';
      if(a.s) h+='<p class="card-summary">'+highlightEsc(a.s,globalSearch)+'</p>';
      if(a.tags && a.tags.length){
        h+='<div class="card-tags">';
        for(var _ti=0;_ti<Math.min(a.tags.length,3);_ti++){
          if(_ti>0) h+=' ';
          h+='<span class="ctag">'+esc(a.tags[_ti])+'</span>';
        }
        if(a.tags.length>3) h+='<span class="ctag">+' + (a.tags.length-3) + '</span>';
        h+='</div>';
      }
      h+='<div class="card-foot"><span class="src-dot" style="--sc:'+a.sc+'"></span><span class="src-name">'+esc(a.src)+'</span>';
      h+='<span class="foot-meta"><button class="copy-btn" data-k="'+esc(k)+'" title="\u590d\u5236\u6807\u9898\u4e0e\u94fe\u63a5"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>\u590d\u5236</button><button class="share-btn" data-k="'+esc(k)+'" title="\u5206\u4eab\u6587\u7ae0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button><span>'+estRead(a)+'</span></span></div>';
      h+='</article>';
    }
    wall.innerHTML=h;
    wallLimit=end;
    _scheduleWallTranslate();
  }
  /* 轻量更新：仅更新卡片已读/打开状态的 CSS 类，不重建 DOM */
  function updateCardStates(){
    var cards=document.querySelectorAll('#wall .card');
    for(var i=0;i<cards.length;i++){
      var k=cards[i].dataset.k;
      var isVis=!!visited[k];
      var isOpen=curArt&&artKey(curArt)===k;
      cards[i].classList.toggle('visited',isVis);
      cards[i].classList.toggle('open',isOpen);
    }
  }
  /* 封面图加载失败：隐藏 img 让分类色 fallback 露出（error 不冒泡，捕获阶段拦截） */
  document.getElementById('wall').addEventListener('error',function(e){
    var t=e.target;
    if(t&&t.classList&&t.classList.contains('cover-img')){ t.style.display='none'; }
  },true);
  /* A1 修复：键盘 Enter/Space 打开卡片（仅当焦点在卡片本身时，不劫持子控件） */
  document.getElementById('wall').addEventListener('keydown',function(e){
    if(e.key==='Enter'||e.key===' '){
      var card=e.target.closest('.card'); if(!card||e.target!==card)return;
      e.preventDefault(); card.click();
    }
  });
  /* 事件委托：一次性绑定，无需重新绑定 */
  document.getElementById('wall').addEventListener('click',function(e){
    var card=e.target.closest('.card'); if(!card)return;
    var k=card.dataset.k;
    var a=ART.find(function(x){return artKey(x)===k;});
    if(!a) return;
    if(e.target.closest('.ext-btn')){markRead(a);updateCardStates();return;}
    if(e.target.closest('.bm-btn')){toggleBookmark(a);return;}
    if(e.target.closest('.copy-btn')){copyArticleInfo(k);return;}
    if(e.target.closest('.share-btn')){shareArticle(a,null,e.target.closest('.share-btn'));return;}
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
  var _articleCache={};
  /* /api/article 的失败码 → 用户能看懂的一句人话。宁可写长也别写成只有码名的那种提示。
     这份表与 api/article.js 的 Task 5 契约一一对应（7 格，不多不少；timeout 由服务端
     AbortError 动态产生，不在那边写成字面量）。判据：tests/site_nav/test_reader_fulltext_errors.py */
  var _FT_NOTES={
    'challenge_page':'该站点要求浏览器人机验证，StarHub 无法代为抓取正文。可点下方"打开原文"自行阅读。',
    'no_body':'没能从该页面提取到正文（可能是纯 JS 渲染的页面），下面保留的是信源自带摘要。',
    'timeout':'抓取原文超时（对方站点响应过慢），下面保留的是信源自带摘要。',
    'fetch_failed':'原文站点拒绝访问或不存在，下面保留的是信源自带摘要。',
    'extraction_failed':'页面结构无法解析，下面保留的是信源自带摘要。',
    'invalid_url':'这条内容没有可抓取的外部链接。',
    'missing_url':'缺少原文地址。'
  };
  var _FT_NOTE_FALLBACK='未能获取正文，以下为信源摘要。';
  function _showFulltextNote(inner,text){
    if(!inner||inner.querySelector('.r2-ft-note')) return;
    var n=document.createElement('div');
    n.className='r2-ft-note';
    n.style.cssText='margin:10px 0;padding:9px 11px;border-left:3px solid var(--brand-line);background:var(--panel-soft,rgba(0,0,0,.03));font-size:13px;line-height:1.7;color:var(--muted)';
    n.innerHTML=esc(text);
    var hint=inner.querySelector('.r2-foot-hint');
    if(hint) inner.insertBefore(n,hint); else inner.appendChild(n);
  }
  /* ── HN 讨论页（Task 9）：那是评论列表，没有"正文"可抽 ──
     api/article.js 对 item 页跑 Readability 抽出来的是"用户名 + 评论锚点"，
     正是用户报的"阅读器里全是链接"。裁定：不发 /api/article，摘要当正文显示 + 给讨论入口。 */
  function _isHnDiscussion(u){
    /* 只按 host 边界认 news.ycombinator.com（精确或其子域）。绝不对整条 URL 做子串匹配：
       `http://evil.com/?x=news.ycombinator.com`、`https://news.ycombinator.com.evil.com/`、
       `https://news.ycombinator.com@evil.com/item` 都不算 —— 与封面 referrer 表那条同形状。
       走 new URL 而不是手拆：port / userinfo / 大小写 / 非 http(s) scheme 一次处理干净；
       解析不了（相对路径、空串、老浏览器没有 URL）就当"不是讨论页"，最多少一层短路，不会误吞正文。 */
    var HN='news.ycombinator.com';
    var SUB='.'+HN;
    var s=String(u||'').trim();
    if(!s) return false;
    var host='';
    try{ host=String(new URL(s).hostname||'').toLowerCase(); }
    catch(e){ return false; }
    if(!host) return false;
    return host===HN||host.slice(-SUB.length)===SUB;
  }
  function _showSummaryAsBody(inner,a){
    /* 讨论页的"正文"就是信源摘要（Ask/Show HN 文本帖）或一句计数（纯链接投稿），
       再加一个能点回讨论区的出口。摘要按**文本**转义后显示：Task 8 之后 a.s 里仍可能有
       上游自带的 HTML/尖括号，直接拼进 innerHTML 就是自我 XSS 面。
       href 是属性上下文：esc() 走 text-node 序列化，不转引号，所以这里补一道引号。
       换行用 \\n 双写 —— 这段 JS 活在非 raw 的 Python 三引号串里，单写会被 Python 先解成
       真换行，产物里就变成跨行的正则字面量（判据 test_summary_newlines_become_br_in_the_artifact）。 */
    if(!inner||inner.querySelector('.r2-fulltext')) return;
    var div=document.createElement('div');
    div.className='r2-fulltext';
    var txt=esc(a.s||'（该条目无文字内容）').replace(/\\n/g,'<br>');
    div.innerHTML='<p>'+txt+'</p><p style="margin-top:10px"><a href="'
      +esc(a.u||'').replace(/"/g,'&quot;')
      +'" target="_blank" rel="noopener" style="color:var(--brand-strong)">在 Hacker News 查看讨论 ↗</a></p>';
    var hint=inner.querySelector('.r2-foot-hint');
    if(hint) inner.insertBefore(div,hint); else inner.appendChild(div);
  }
  function fetchFullArticle(a){
    if(!a.u||a.u==='#') return;
    var inner=document.getElementById('r2Inner');
    if(!inner) return;
    if(a.fc&&a.fc.length>100){
      _insertFulltext(a.fc);
      return;
    }
    /* 判断必须在 fetch **之前**：放进 .then 里等于白判（请求已经发出去了，
       用户先看到一屏评论链接列表）。有真全文时先看全文，所以这一格排在 a.fc 之后。 */
    if(_isHnDiscussion(a.u)){ _showSummaryAsBody(inner,a); return; }
    if(_articleCache[a.u]){
      var cc=_articleCache[a.u];
      /* 命中缓存也要判 ok：分享通道（shareArt）往同一张表里写 d 时不判 ok，
         直接插就是把 undefined 当正文喂给 innerHTML。失败项在这里出声。 */
      if(cc&&cc.ok&&cc.content) _insertFulltext(cc.content,cc.degraded);
      else _showFulltextNote(inner,_FT_NOTES[cc&&cc.error]||_FT_NOTE_FALLBACK);
      return;
    }
    var old=inner.querySelector('.r2-ft-loading');
    if(old) old.remove();
    var ld=document.createElement('div');
    ld.className='r2-ft-loading';
    ld.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-dasharray="30 70" stroke-linecap="round"/></svg> \u6b63\u5728\u52a0\u8f7d\u5168\u6587\u2026';
    var hint=inner.querySelector('.r2-foot-hint');
    if(hint) inner.insertBefore(ld,hint); else inner.appendChild(ld);
    var apiBase='https://starhub-refresh.vercel.app/api/article';
    fetch(apiBase+'?url='+encodeURIComponent(a.u)).then(function(r){return r.json();}).then(function(d){
      var cur=inner.querySelector('.r2-ft-loading'); if(cur) cur.remove();
      if(d.ok&&d.content){
        /* 只缓存成功：把失败也缓存的话，会话内每次重试都拿回同一份失败并永久静默 */
        _articleCache[a.u]=d;
        _insertFulltext(d.content,d.degraded);
        return;
      }
      /* ok:false 是服务端的**回答**，不是网络故障：过去这里没有 else，转圈被上一行删掉后
         阅读器既不显示正文也不显示原因（用户报的"部分内容加载不出"）。 */
      _showFulltextNote(inner,_FT_NOTES[d.error]||_FT_NOTE_FALLBACK);
    }).catch(function(){
      var cur=inner.querySelector('.r2-ft-loading'); if(cur) cur.remove();
      /* F3 修复：全文加载失败时显示行内提示与重试按钮 */
      if(!inner.querySelector('.r2-ft-error')){
        var err=document.createElement('div');err.className='r2-ft-error';
        err.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;animation:spin 1s linear infinite"><circle cx="12" cy="12" r="10" stroke-dasharray="30 70" stroke-linecap="round"/></svg> 全文加载失败 <button onclick="fetchFullArticle(curArt)" style="color:var(--brand-strong);background:none;border:1px solid var(--brand-line);border-radius:6px;padding:2px 10px;font-size:12px;cursor:pointer;font-family:var(--body)">重试</button>';
        var hint2=inner.querySelector('.r2-foot-hint');if(hint2)inner.insertBefore(err,hint2);else inner.appendChild(err);
      }
    });
  }
  /* ── 媒体嵌入：YouTube iframe + 播客/音频播放器 ── */
  function extractYouTubeId(url){
    if(!url) return null;
    var m=url.match(/(?:youtube\.com\/watch\?.*v=|youtu\.be\/|youtube\.com\/shorts\/)([a-zA-Z0-9_-]{6,})/);
    return m?m[1]:null;
  }
  function _renderMedia(a,container){
    if(!a||!container) return;
    var vid=extractYouTubeId(a.u);
    var hasIframe=container.querySelector('iframe[src*="youtube.com"],iframe[src*="youtu.be"]');
    if(vid&&!hasIframe){
      var ifr=document.createElement('iframe');
      ifr.src='https://www.youtube.com/embed/'+vid;
      ifr.setAttribute('loading','lazy');
      ifr.setAttribute('allowfullscreen','');
      ifr.setAttribute('allow','accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture');
      ifr.setAttribute('sandbox','allow-scripts allow-same-origin allow-popups');
      ifr.setAttribute('title','YouTube video player');
      ifr.style.cssText='width:100%;aspect-ratio:16/9;border:0;border-radius:8px;margin-bottom:12px';
      container.insertBefore(ifr,container.firstChild);
    }
    if(a.mu){
      var hasAudio=container.querySelector('audio,video');
      if(!hasAudio){
        var isAudio=a.mt&&a.mt.indexOf('audio')===0;
        var el=document.createElement(isAudio?'audio':'video');
        el.controls=true;el.preload='none';el.src=a.mu;
        el.style.cssText='width:100%;margin-bottom:12px;border-radius:8px';
        el.addEventListener('error',function(){
          el.style.display='none';
          var fb=document.createElement('div');
          fb.className='r2-media-error';
          fb.innerHTML='<p style="color:var(--muted);font-size:13px;margin:8px 0">\u5a92\u4f53\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u76f4\u63a5\u8bbf\u95ee\u539f\u59cb\u94fe\u63a5</p><a href="'+a.u+'" target="_blank" rel="noopener" style="color:var(--brand-strong);font-size:13px">\u6253\u5f00\u539f\u59cb\u94fe\u63a5 \u2197</a>';
          container.insertBefore(fb,container.firstChild);
        });
        container.insertBefore(el,container.firstChild);
      }
    }
  }
  /* ── 媒体清理：关闭/切换文章时停止播放 ── */
  function _cleanupMedia(){
    var body=document.getElementById('r2Body');
    if(!body) return;
    var audios=body.querySelectorAll('audio,video');
    for(var i=0;i<audios.length;i++){try{audios[i].pause();audios[i].src='';audios[i].load();}catch(e){}}
    var iframes=body.querySelectorAll('iframe');
    for(var j=0;j<iframes.length;j++){try{iframes[j].src='about:blank';}catch(e){}}
  }
  /* 正文内联图片的渲染层兜底：只改属性、不碰结构。
     为什么构建期规范之外还要这一道：文章接口（api/article.js）现抓的正文压根不过构建期规范，
     Readability 的输出格式由站点决定，而双重转义那一类正文在运行时通道上服务端
     压根拿不回 `data-src`（见 task-4-5-report.md 的 L1）—— 只能在浏览器里救。
     referrer 复用封面那一份表（_coverRefPolicy/_coverHost/_hostInTable/_REF_HOSTS），
     本函数里不写任何域名：第二张表就是第 4 个会分叉的事实来源。
     判据：tests/site_nav/test_reader_body_images.py（含运行时通道 data-src 的行为样本）。 */
  function _hardenBodyImages(root,baseUrl){
    if(!root||!root.querySelectorAll) return;
    var lazyAttrs=['data-src','data-original','data-lazy-src','data-croporisrc'];
    var imgs=root.querySelectorAll('img');
    var base=(baseUrl&&/^https?:/i.test(baseUrl))?baseUrl:'';
    for(var i=0;i<imgs.length;i++){
      var im=imgs[i];
      var src=(im.getAttribute('src')||'').trim();
      /* 空 src 与占位图都去懒加载属性里找真地址：老快照与现抓正文都常见这一形。 */
      if(!src||src.slice(0,5)==='data:'){
        for(var k=0;k<lazyAttrs.length;k++){
          var v=(im.getAttribute(lazyAttrs[k])||'').trim();
          if(v){ src=v; break; }
        }
      }
      /* 相对/协议相对地址按**这条内容自己的原文链接**落地；补不出 http(s) 就当没有图。 */
      if(src&&src.slice(0,11)!=='data:image/'&&!/^https?:/i.test(src)){
        var abs='';
        try{ abs=new URL(src,base||location.href).href; }catch(e){ abs=''; }
        src=/^https?:/i.test(abs)?abs:'';
      }
      /* 没有可用地址就整枚摘掉：留在正文里的破图占位比没有图更糟（一枚碎图标压在段落中间）。 */
      if(!src){ if(im.parentNode) im.parentNode.removeChild(im); continue; }
      im.setAttribute('src',src);
      im.setAttribute('loading','lazy');
      im.setAttribute('decoding','async');
      var pol=_coverRefPolicy(src);
      /* pol 为空串=这家图床要带 Referer 才给图：必须 removeAttribute，
         写成 attr="" 的属性值语义在浏览器间并不一致。 */
      if(pol) im.setAttribute('referrerpolicy',pol); else im.removeAttribute('referrerpolicy');
      if(!im._ftErr){
        im._ftErr=1;
        im.addEventListener('error',function(){this.style.display='none';});
      }
    }
  }
  function _insertFulltext(html,degraded){
    var inner=document.getElementById('r2Inner');
    if(!inner||inner.querySelector('.r2-fulltext')) return;
    /* degraded 是服务端**给了内容但不足以称"全文"**的标注（值只可能 paywall/short，
       判定住在 lib/body_rules.js 的 classifyBody）：照旧显示正文，但不许再假装它是全文。 */
    if(degraded==='paywall') _showFulltextNote(inner,'该站点为付费内容，以下仅为可公开的试读部分。');
    else if(degraded==='short') _showFulltextNote(inner,'正文可能不完整（站点仅提供短摘要或需 JS 渲染）。');
    var div=document.createElement('div');
    div.className='r2-fulltext';
    
    // Auto-format: detect if content lacks paragraph structure
    var hasParagraphs=/<p[\s>]/i.test(html);
    if(!hasParagraphs){
      // Plain text or minimal HTML - split into paragraphs
      var blocks=html.split(/\\n\\s*\\n/);
      var formatted=blocks.map(function(block){
        block=block.trim();
        if(!block) return '';
        // Check if block contains only an image
        if(/^<img\s/i.test(block)&&block.match(/^<img\s[^>]*>$/i)){
          return block;
        }
        // Wrap text blocks in <p> tags
        return '<p>'+block.replace(/\\n/g,'<br>')+'</p>';
      }).filter(function(b){return b;}).join('\\n');
      div.innerHTML=formatted;
    } else {
      // Already has proper HTML structure
      div.innerHTML=html;
    }
    
    var hint=inner.querySelector('.r2-foot-hint');
    if(hint) inner.insertBefore(div,hint); else inner.appendChild(div);
    /* 正文翻译切换按钮：非中文内容时显示 */
    var ftText=div.textContent||'';
    if(ftText&&!isMostlyZh(ftText)){
      var tog=document.createElement('div');
      tog.className='r2-lang-toggle';
      tog.innerHTML='<button class="active" id="btnFtOrig">原文</button><button id="btnFtTrans">翻译</button>';
      if(hint) inner.insertBefore(tog,hint); else inner.appendChild(tog);
      document.getElementById('btnFtTrans').onclick=function(){_translateFulltext(div);};
      document.getElementById('btnFtOrig').onclick=function(){
        div.innerHTML=div._origHtml||div.innerHTML;
        var bs=tog.querySelectorAll('button');bs[0].classList.add('active');bs[1].classList.remove('active');
      };
    }
    /* 正文内联图片兜底（referrer/懒加载/破图），必须在媒体嵌入之前跑：
       _renderMedia 会往容器里插 iframe/audio，那是我们不该管的另一批元素。 */
    _hardenBodyImages(div,curArt&&curArt.u);
    /* 媒体嵌入：YouTube 视频 / 播客音频 */
    _renderMedia(curArt,div);
  }
  /* 正文翻译：提取文本→分块翻译→重建段落 */
  function _translateFulltext(ftDiv){
    if(!ftDiv) return;
    if(!ftDiv._origHtml) ftDiv._origHtml=ftDiv.innerHTML;
    var tog=ftDiv.nextElementSibling;
    if(tog&&tog.classList&&tog.classList.contains('r2-lang-toggle')){
      var bs=tog.querySelectorAll('button');bs[0].classList.remove('active');bs[1].classList.add('active');
    }
    ftDiv.innerHTML='<p>翻译中…</p>';
    var text=ftDiv._origText||(ftDiv._origText=(ftDiv._origHtml||ftDiv.innerHTML).replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim());
    _clientTranslate(text,function(tr){
      var cur=document.querySelector('.r2-fulltext');
      if(cur){
        var lines=tr.split(/。|！|？|\.\s+/).filter(function(s){return s.trim();});
        cur.innerHTML=lines.map(function(s){return '<p>'+esc(s.trim())+'</p>';}).join('')||'<p>'+esc(tr)+'</p>';
      }
    });
  }
  function markRead(a){visited[artKey(a)]=1;try{localStorage.setItem('rss_read_v2',JSON.stringify(visited));}catch(e){}}
  function markAllRead(){
    var list=visibleArts(),cnt=0;
    for(var i=0;i<list.length;i++){var k=artKey(list[i]);if(!visited[k]){visited[k]=1;cnt++;}}
    try{localStorage.setItem('rss_read_v2',JSON.stringify(visited));}catch(e){}
    updateCardStates();
    if(cnt>0) toast('\u5df2\u6807\u8bb0 '+cnt+' \u7bc7\u4e3a\u5df2\u8bfb');
    if(filter.unreadOnly){wallLimit=WALL_STEP;renderWall();}
    renderChips(); /* U1: 刷新未读计数 */
  }
  /* F4 修复：全部已读入口——在未读模式激活时显示按钮 */
  function _updateMarkAllReadBtn(){
    var btn=document.getElementById('markAllReadBtn');
    if(!btn) return;
    btn.style.display=filter.unreadOnly?'':'none';
  }
  /* A2 修复：焦点陷阱工具函数 */
  var _prevFocusEl=null;
  function _trapFocus(container,e){
    var foc=container.querySelectorAll('button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])');
    if(!foc.length)return;var first=foc[0],last=foc[foc.length-1];
    if(e.shiftKey){if(document.activeElement===first){e.preventDefault();last.focus();}}
    else{if(document.activeElement===last){e.preventDefault();first.focus();}}
  }
  function openReader(a){
    curArt=a; window.curArt=a; markRead(a);
    _prevFocusEl=document.activeElement;
    renderReader(); document.body.classList.add('reading');
    document.body.classList.remove('src-open');
    document.getElementById('r2Body').scrollTop=0; updateCardStates();
    var closeBtn=document.querySelector('.r2-close');if(closeBtn)closeBtn.focus();
  }
  function _navReader(dir){
    if(!curArt)return;
    var order=visibleArts();var pos=-1;
    for(var i=0;i<order.length;i++){if(artKey(order[i])===artKey(curArt)){pos=i;break;}}
    if(pos===-1)return;
    var np=pos+dir;
    if(np>=0&&np<order.length)openReader(order[np]);
  }
  window._navReader=_navReader;
  function renderReader(){
    var a=curArt; if(!a) return;
    _cleanupMedia();
    document.getElementById('r2Src').innerHTML='<span class="src-dot" style="--sc:'+a.sc+'"></span><b>'+esc(a.src)+'</b><span>\u00b7</span><span title="'+esc(a.date||'')+'">'+_dynTime(a)+'</span>';
    var openEl=document.getElementById('r2Open'); openEl.href=a.u;
    var h='<h1 class="r2-title">'+esc(a.t)+'</h1>';
    h+='<div class="r2-meta" style="--cc:var(--cat-'+a.c+')"><span class="cat">'+(CAT_LABELS[a.c]||a.c)+'</span>';
    h+='<span class="src-dot" style="--sc:'+a.sc+'"></span><span>'+esc(a.src)+'</span>';
    h+='<span>\u00b7</span><span title="'+esc(a.date||'')+'">'+_dynTime(a)+'</span><span>\u00b7</span><span>'+estRead(a)+'</span></div>';
    if(a.s){
      /* 症状③的最后一条通道：互方把同一段话既塞进 description 又塞进 content:encoded 时，
         下面 _insertFulltext(a.fc) 还会再渲染一遍 ⇒ 同一段话屏幕上出现两遍。
         已发布产物 15,632 条里这样的 191 条（1.22%），其中 151 条正文里还带图 ⇒
         不能丢正文，只能把这块重复的摘要隐掉；翻译/原文按钮在正文那块上仍然在。
         口径只看剥标签后的可见文字：**逐字相等**或**正好是正文开头的前缀**都算重复
         （出口把摘要截到 200 字会补省略号，比之前先摘掉尾巴那个 U+2026，否则现抓通道判不出来）；
         不引入第三套正文规则。
         频度会随第二遍解码被删掉而上升：这一批上线后现网一次读到 93/2,412（3.8%）——
         旧出口把两侧解成不一样的形状，所以以前是漏报，不是没有。 */
      var _dupFt = _summaryDuplicatedByFulltext(a.s, a.fc);
      if(!_dupFt){
      // Auto-format summary into paragraphs
      var formattedSummary = formatSummary(a.s);
      h+='<div class="r2-summary">'+formattedSummary+'</div>';
      if(!isMostlyZh(a.s)){
        h+='<div class="r2-lang-toggle">';
        h+='<button class="active" id="btnOrig">\u539f\u6587</button>';
        h+='<button id="btnTrans">\u7ffb\u8bd1</button></div>';
      }
      }
    } else {
      h+='<div class="fallback-card"><div class="fb-ico">🔗</div>';
      h+='<p>\u8be5\u6587\u7ae0\u6682\u65e0\u6458\u8981<br>\u53ef\u524d\u5f80\u539f\u7ad9\u7ee7\u7eed\u9605\u8bfb</p>';
      h+='<a class="fb-btn" href="'+esc(a.u)+'" target="_blank" rel="noopener">\u539f\u7ad9 \u2197</a></div>';
    }
    h+='<div class="r2-foot-hint">J / K \u6216 \u2190 \u2192 \u5207\u6362\u6587\u7ae0 \u00b7 ESC \u8fd4\u56de</div>';
    h+='<div class="r2-actions-bottom">';
    h+='<button class="r2-nav-btn" onclick="_navReader(-1)" title="\u4e0a\u4e00\u7bc7 (K)" style="display:inline-flex;align-items:center;gap:5px;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;font-family:var(--body)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M19 12H5M11 18l-6-6 6-6"/></svg> \u4e0a\u4e00\u7bc7</button>';
    h+='<button class="r2-share-btn" onclick="r2ShareClick()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg><span>\u5206\u4eab\u672c\u6587</span></button>';
    h+='<button class="r2-nav-btn" onclick="_navReader(1)" title="\u4e0b\u4e00\u7bc7 (J)" style="display:inline-flex;align-items:center;gap:5px;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;transition:all .15s;font-family:var(--body)">\u4e0b\u4e00\u7bc7 <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>';
    h+='</div>';
    document.getElementById('r2Inner').innerHTML=h;
    var btnT=document.getElementById('btnTrans');
    if(btnT) btnT.onclick=function(){
      var el=document.querySelector('.r2-summary');
      if(!el) return; el.innerHTML='<p>\u7ffb\u8bd1\u4e2d\u2026</p>';
      _clientTranslate(a.s,function(tr){
        var cur=document.querySelector('.r2-summary');
        if(cur) cur.innerHTML=formatSummary(tr);
      });
    };
    updateBmBtn();
    fetchFullArticle(a);
  }

  // Format summary text into readable paragraphs
  /* 摘要是否已被内嵌全文逐字重复（只比可见文字，不做正文规范）。
     判据：tests/site_nav/test_reader_summary_dedupe.py */
  function _summaryDuplicatedByFulltext(sum, fc){
    if(!sum || !fc) return false;
    /* 门槛必须与 fetchFullArticle 的 `if(a.fc&&a.fc.length>100)` 对齐（比的是**原始**长度、带标签）：
       短于它的 fc 根本不会入屏（转去现抓或出降级提示），这时再隐掉摘要 = 整块面板一个字都不剩。
       线上今天 93 条同段全部是长正文 ⇒ 那是数据巧合，不是代码保证，所以这条要写死。 */
    if(String(fc).length <= 100) return false;
    var vis = function(x){ return String(x).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(); };
    /* 摘掉出口截断补的那个省略号（`truncate` 到 200 字会加 U+2026），否则现抓/批量通道上
       "摘要=正文开头"那一格永远比不中 ⇒ 用户还是读两遍。 */
    var a = vis(sum).replace(/\\u2026+$/, '');
    var f = vis(fc);
    return a.length > 24 && f.slice(0, a.length) === a;
  }

  function formatSummary(text){
    if(!text) return '';
    // Step 1: Normalize line endings
    var normalized = text.replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n');
    
    // Step 2: If no newlines exist, split by sentence-ending punctuation (Chinese + English)
    if(normalized.indexOf('\\n') === -1){
      // Chinese punctuation: always split after 。！？
      normalized = normalized.replace(/([。！？])/g, '$1\\n');
      
      // English punctuation: only split after .!? when followed by space+uppercase or end of string
      // Avoid splitting decimals (129.3), versions (3.7), domains (example.com), abbreviations
      normalized = normalized.replace(/(\\.)(\\s+[A-Z\\u4e00-\\u9fff])/g, '$1\\n$2')  // Period before uppercase/Chinese
        .replace(/([!?])(\\s+)/g, '$1\\n$2');  // !? before space
    }
    
    // Step 3: Split by newlines and wrap each non-empty line in <p>
    var lines = normalized.split('\\n');
    var html = lines.filter(function(line){ return line.trim().length > 0; })
      .map(function(line){ return '<p>' + esc(line.trim()) + '</p>'; })
      .join('');
    return html || '<p>' + esc(text) + '</p>';
  }
  document.getElementById('r2Body').addEventListener('scroll',function(){
    var el=this,max=el.scrollHeight-el.clientHeight;
    document.getElementById('r2Progress').style.width=(max>0?el.scrollTop/max*100:0)+'%';
  });

  /* ── Client translate ── */
  var _ctCache={},_ctPend={};
  /* 全文/摘要翻译：Agnes API 主力（服务端代理，密钥不落前端），失败块由服务端 GTX 兜底（mode:'full'），
     最终兜底原文。2026-09-08 实证修正：gtx 端点响应带 ACAO:*（浏览器可直连），当年「必遭 CORS」
     实为 GFW/网络因素误判；但全文按钮仍走服务端（质量优先 Agnes），浏览器直连仅用于批量补翻主力。 */
  function _clientTranslate(text,cb){
    if(!text||isMostlyZh(text)){cb(text);return;}
    var k=text.substring(0,100);
    if(_ctCache[k]){cb(_ctCache[k]);return;}
    if(_ctPend[k]){_ctPend[k].push(cb);return;}
    _ctPend[k]=[cb];
    /* 长文本分块翻译：每块 450 字，Agnes 每请求 ≤20 块，批间串行 */
    var chunks=[],pos=0;
    while(pos<text.length){var end=Math.min(pos+450,text.length);chunks.push(text.substring(pos,end));pos=end;}
    var _finished=0;
    function _finish(tr){ if(_finished) return; _finished=1; _ctCache[k]=tr; var p=_ctPend[k]||[]; delete _ctPend[k]; p.forEach(function(f){f(tr);}); }
    (function _agiBatch(bi){
      if(_finished) return;
      var start=bi*20,end=Math.min(start+20,chunks.length);
      if(start>=chunks.length){ _finish(chunks.join('')); return; }
      var ctrl=(typeof AbortController==='function')?new AbortController():null;
      /* 服务端 full 最坏路径（非 429 慢挂起情形）= Agnes 12s×2+400ms + GTX 6s×2+600ms ≈ 37s，
         前端 25s 超时只保证一轮 Agnes+GTX 在用户侧可见；429 快速失败路径 <10s 必然可见，
         慢路径的 GTX 兜底仍会在服务端完成并写缓存（下次点击命中） */
      var tmr=ctrl?setTimeout(function(){ctrl.abort();},25000):null;
      fetch(TR_API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({texts:chunks.slice(start,end),mode:'full'}),signal:ctrl?ctrl.signal:undefined})
      .then(function(r){ if(tmr)clearTimeout(tmr); return r.ok?r.json():Promise.reject(new Error('http '+r.status)); })
      .then(function(j){
        if(!j||!j.ok||!j.translations||j.translations.length!==(end-start)) throw new Error('bad payload');
        for(var i=start;i<end;i++){ var t=(j.translations[i-start]||'').trim(); if(t)chunks[i]=t; }
        _agiBatch(bi+1);
      }).catch(function(){ if(tmr)clearTimeout(tmr); _agiBatch(bi+1); });
    })(0);
  }

  // ── OPML export ──
  /* 先从 rss_sources.json 获取 url 映射（chunk 数据可能不含 url），
     获取失败则回退到 SOURCES 自带的 url（下次构建后 chunk 含 url） */
  function exportOPML(){
    var _doExport=function(urlMap){
      var groups={};
      SOURCES.forEach(function(s){
        var cat=s.cat||'other';
        if(!groups[cat]) groups[cat]=[];
        groups[cat].push(s);
      });
      var xml='<?xml version="1.0" encoding="UTF-8"?>\\n';
      xml+='<opml version="2.0">\\n<head><title>StarHub RSS \\u4fe1\\u6e90</title><dateCreated>'+new Date().toUTCString()+'</dateCreated></head>\\n<body>\\n';
      CAT_ORDER.forEach(function(c){
        if(!groups[c]||!groups[c].length) return;
        var label=CAT_LABELS[c]||c;
        xml+='  <outline text="'+esc(label)+'" title="'+esc(label)+'">\\n';
        groups[c].forEach(function(s){
          var u=s.url||urlMap[s.key]||'';
          xml+='    <outline type="rss" text="'+esc(s.name)+'" title="'+esc(s.name)+'" xmlUrl="'+esc(u)+'"/>\\n';
        });
        xml+='  </outline>\\n';
      });
      xml+='</body>\\n</opml>';
      var blob=new Blob([xml],{type:'text/xml;charset=utf-8'});
      var a=document.createElement('a');
      a.href=URL.createObjectURL(blob);
      a.download='starhub-rss-sources.opml';
      a.click();
      URL.revokeObjectURL(a.href);
      toast('\\u5df2\\u5bfc\\u51fa '+SOURCES.length+' \\u4e2a\\u4fe1\\u6e90');
    };
    /* 尝试从 rss_sources.json 获取 url 映射 */
    fetch('rss_sources.json').then(function(r){return r.ok?r.json():null;}).then(function(srcs){
      var m={}; if(srcs)srcs.forEach(function(s){if(s.key&&s.url)m[s.key]=s.url;});
      _doExport(m);
    }).catch(function(){ _doExport({}); });
  }

  // ── Keyboard help ──
  function toggleKbdHelp(){
    var m=document.getElementById('kbdHelp');
    if(!m){m=document.createElement('div');m.id='kbdHelp';m.className='kbd-help';
    m.innerHTML='<div class="kbd-help-backdrop" onclick="toggleKbdHelp()"></div><div class="kbd-help-panel"><div class="kbd-help-hd"><h3>\u5feb\u6377\u952e</h3><button class="share-close" onclick="toggleKbdHelp()">\u00d7</button></div><div class="kbd-help-body"><table><tr><td><kbd>j</kbd> / <kbd>\u2192</kbd></td><td>\u4e0b\u4e00\u7bc7\u6587\u7ae0</td></tr><tr><td><kbd>k</kbd> / <kbd>\u2190</kbd></td><td>\u4e0a\u4e00\u7bc7\u6587\u7ae0</td></tr><tr><td><kbd>/</kbd></td><td>\u805a\u7126\u641c\u7d22\u6846</td></tr><tr><td><kbd>d</kbd></td><td>\u5207\u6362\u4e3b\u9898</td></tr><tr><td><kbd>Esc</kbd></td><td>\u5173\u95ed\u9605\u8bfb\u5668/\u9762\u677f</td></tr><tr><td><kbd>?</kbd></td><td>\u663e\u793a\u5feb\u6377\u952e\u5e2e\u52a9</td></tr></table></div></div></div>';
    document.body.appendChild(m);}
    m.classList.toggle('open');
  }

  // ── Window exports ──
  window.ART = ART;
  window.closeReader = function(){ _cleanupMedia(); document.body.classList.remove('reading'); curArt=null; window.curArt=null; updateCardStates(); if(_prevFocusEl){try{_prevFocusEl.focus();}catch(e){}_prevFocusEl=null;} };
  window.closeOverlays = function(){ document.body.classList.remove('src-open'); window.closeReader(); closeInsight(); };
  window.clearSrcF = function(e){ e.stopPropagation(); var uo=filter.unreadOnly,bm=filter.filterBm; filter={type:'all',unreadOnly:uo,filterBm:bm}; curArt=null; wallLimit=WALL_STEP; renderChips(); renderWall(); renderPanel(); updateTitle(); updateHash(); updateUnreadBtn(); updateBmChip(); };
  window.toggleSrcPanel = toggleSrcPanel;
  window.selectSrc = selectSrc;
  window.toggleUnread = toggleUnread;
  window.setFontSize = setFontSize;
  window.toggleBookmark = toggleBookmark;
  window.toggleBmFilter = toggleBmFilter;
  window.markAllRead = markAllRead;
  window.exportOPML = exportOPML;
  window.toggleKbdHelp = toggleKbdHelp;

  /* ── Global search ── */
  var gsInput = document.getElementById('globalSearch');
  var gsWrap = document.getElementById('globalSearchWrap');
  var gsClear = document.getElementById('globalSearchClear');
  var _searchTimer=0;
  if(gsInput) {
    gsInput.addEventListener('input', function(){
      globalSearch = this.value.trim();
      _searchSrcMatch = null;
      _topicBigrams = null;
      if(globalSearch.length >= 1) {
        var ql = globalSearch.toLowerCase();
        var matched = SOURCES.filter(function(s){ return s.name.toLowerCase().indexOf(ql) >= 0; });
        if(matched.length === 1) _searchSrcMatch = matched[0].key;
        else if(matched.length > 1 && matched.length <= 5) {
          var exact = matched.find(function(s){ return s.name.toLowerCase() === ql; });
          if(exact) _searchSrcMatch = exact.key;
        }
      }
      gsWrap.classList.toggle('has-q', globalSearch.length > 0);
      gsWrap.classList.toggle('src-hit', !!_searchSrcMatch);
      var _sml=document.getElementById('srcMatchLabel');
      if(_sml){if(_searchSrcMatch){var _sn=SRC_OBJ(_searchSrcMatch);_sml.textContent='\u4fe1\u6e90\u5339\u914d\uff1a'+(_sn?_sn.name:_searchSrcMatch);_sml.style.display='';}else{_sml.style.display='none';}}
      clearTimeout(_searchTimer);
      _searchTimer=setTimeout(function(){ curArt = null; wallLimit = WALL_STEP; renderWall(); updateMeta(); },300);
    });
  }
  if(gsClear) {
    gsClear.addEventListener('click', function(){
      gsInput.value = ''; globalSearch = '';
      _searchSrcMatch = null;
      _topicBigrams = null;
      gsWrap.classList.remove('has-q', 'src-hit');
      curArt = null; wallLimit = WALL_STEP;
      renderWall(); updateMeta(); gsInput.focus();
    });
  }

  /* ── Keyboard nav ── */
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape'){
      var kh=document.getElementById('kbdHelp');
      if(kh&&kh.classList.contains('open')){toggleKbdHelp();return;}
      var sm=document.getElementById('shareModal');
      if(sm&&sm.classList.contains('open')){closeShareModal();return;}
      window.closeOverlays();return;
    }
    /* A2 修复：模态对话框焦点陷阱 */
    if(e.key==='Tab'){
      var sm=document.getElementById('shareModal');
      if(sm&&sm.classList.contains('open')){_trapFocus(sm,e);return;}
      var rd=document.getElementById('reader2');
      if(document.body.classList.contains('reading')&&rd){_trapFocus(rd,e);return;}
      var sp=document.getElementById('srcPanel');
      if(document.body.classList.contains('src-open')&&sp){_trapFocus(sp,e);return;}
    }
    if(e.target.tagName==='INPUT') return;
    if(e.key==='/'){var gs=document.getElementById('globalSearch');if(gs){gs.focus();}return;}
    if(e.key==='d'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){var bt=document.getElementById('btnTheme');if(bt)bt.click();return;}
    if(e.key==='?'){toggleKbdHelp();return;}
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
    var p='#view='+(filter.type==='cat'?'cat&c='+Object.keys(filter.cats).join(','):filter.type==='src'?'src&s='+encodeURIComponent(filter.src):filter.type==='discover'?'discover':'all');
    try{history.replaceState(null,'',p);}catch(e){}
  }
  function restoreFromHash(){
    var h=(location.hash||'').replace(/^#/,'');if(!h)return false;
    var p={};h.split('&').forEach(function(kv){var s=kv.split('=');if(s[0])p[s[0]]=decodeURIComponent(s[1]||'');});
    if(p.view==='cat'&&p.c){var cats={};p.c.split(',').forEach(function(x){if(x)cats[x]=true;});filter={type:'cat',cats:cats};return true;}
    if(p.view==='src'&&p.s){filter={type:'src',src:p.s};return true;}
    if(p.view==='discover'){filter={type:'discover'};return true;}
    return false;
  }

  /* ── Init ── */
  var restored = restoreFromHash();
  /* 启动：chunk0 在 body 末尾同步加载（此时 DOM 已渲染，骨架可见不白屏）。
     不依赖数据的部分先行；数据到达后渲染首屏；chunk1 后台静默合并。 */
  loadBookmarks(); initFontSize(); updateHash();
  var _bootEl=document.getElementById('bootLoading');
  function _bootFinish(){ if(_bootEl&&_bootEl.parentNode)_bootEl.parentNode.removeChild(_bootEl); }
  function _bootWith(cs){
    SOURCES=cs||[];
    buildArt();
    renderChips(); renderWall(); renderPanel(); updateTitle(); updateUnreadBtn(); updateBmBtn();
    _bootFinish();
    /* 让首屏先稳定可交互，再在空闲时依次加载后台分块 chunk1/2/3...，避免一次性加载阻塞主线程 */
    var _loadRest=function(){
      var _loaded=0;
      /* 终点由 chunk0 的 payload 声明（_total = 本次构建写出的块数，含 chunk0 自己）。
         读不到声明 = 用户手里还是旧 chunk0 缓存 ⇒ 退回"探到失败为止"，绝不停在第一块：
         那正是 2026-09-30 "页面只剩 61 个源"的形状，比一次 404 严重得多。 */
      var _total=(window.__CHUNKS&&window.__CHUNKS[0]&&window.__CHUNKS[0]._total)||0;
      function _finish(){ if(_loaded>0){ toast('已加载全部 '+ART.length+' 篇内容（新增 '+_loaded+' 篇）'); } }
      var _loadNext=function(idx){
        loadChunk(idx).then(function(){
          if(window.__CHUNKS&&window.__CHUNKS[idx]){
            var n=_mergeChunk(window.__CHUNKS[idx]);
            _loaded+=n;
          }
          if(_total && idx+1>=_total){ _finish(); return; }   // 按声明收口：不再请求注定 404 的下一块
          _loadNext(idx+1);
        }).catch(function(e){
          // chunk 不存在或加载失败
          if(idx===1){
            // chunk1 是必需的，缺失则警告
            if(!(window.__CHUNKS&&window.__CHUNKS[1])){
              console.warn('[starhub] chunk1 onload 但 __CHUNKS[1] 缺失（文件截断或内容异常）');
              toast('剩余内容数据异常，刷新页面可重试');
              return;
            }
          }
          // 所有 chunk 加载完成
          if(_loaded>0){
            toast('已加载全部 '+ART.length+' 篇内容（新增 '+_loaded+' 篇）');
          }
        });
      };
      _loadNext(1);
    };
    if(window.requestIdleCallback)window.requestIdleCallback(_loadRest,{timeout:5000});
    else setTimeout(_loadRest,1200);
  }
  /* chunk0 同款守卫（复审 P2）：onload 不等于数据完好，截断/损坏时显式报错而非静默空站 */
  function _bootFail(msg){_bootFinish();console.warn('[starhub]',msg);var w=document.getElementById('wall');if(w)w.innerHTML='<div class="empty-hint">'+msg+'</div>';}
  if(window.__CHUNKS&&window.__CHUNKS[0]&&window.__CHUNKS[0].sources&&window.__CHUNKS[0].sources.length){try{_bootWith(window.__CHUNKS[0].sources);}catch(e){console.warn('[starhub] boot 异常:',e);_bootFail('首屏渲染异常，请刷新重试');}}
  else if(window.__CHUNKS&&window.__CHUNKS[0]){_bootFail('首屏数据异常，请刷新重试');}
  else{loadChunk(0).then(function(){
        var _cs=window.__CHUNKS&&window.__CHUNKS[0]&&window.__CHUNKS[0].sources;
        if(!_cs||!_cs.length){_bootFail('首屏数据异常，请刷新重试');return;}
        try{_bootWith(_cs);}catch(e){console.warn('[starhub] boot 异常:',e);_bootFail('首屏渲染异常，请刷新重试');}}).catch(function(e){
    _bootFail('内容加载失败，请检查网络后刷新重试');
  });}
  /* 无限滚动：接近底部自动加载更多（带节流锁） */
  var _scrollLock=false;
  window.addEventListener('scroll',function(){
    if(_scrollLock)return;
    var list=visibleArts();
    if(wallLimit>=list.length)return;
    var wrap=document.querySelector('.wall-wrap');
    if(!wrap)return;
    if(wrap.getBoundingClientRect().bottom<window.innerHeight*3){
      _scrollLock=true;
      loadMore();
      setTimeout(function(){_scrollLock=false;},150);
    }
  },{passive:true});
  /* 返回顶部按钮 */
  window.addEventListener('scroll',function(){
    var bt=document.getElementById('backTop');
    if(bt) bt.classList.toggle('show',window.scrollY>window.innerHeight*3);
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

  /* 旧“同域快照替换”机制已移除：chunk0+chunk1 已包含同一构建的全量数据，
     快照（约 30MB）冗余下载且与 chunk1 合并存在竞态——若快照先整体替换
     src.items、chunk1 后无去重 concat，同源同链文章会全部重复。 */

  /* ── Refresh: 后台增量更新 —— 不整页 reload，避免重新下载18MB 页面导致长时间白屏 ── */
  var _refreshing=false, _lastTotal=ART.length;
  /* ── Dynamic relative time: computed from a.date at render time, never frozen ─ */
  /* 日期缺失时 a.date 由 first_seen（首次收录时刻）降级而来，见 _fallback_date_iso；
     这类条目必须与「发布时间」区分显示，不能把收录时间冒充成发布时间。 */
  function _dynTime(a){
    var s=_dynTimeCore(a);
    return (a&&a.dfb&&s)?('\u6536\u5f55 '+s):s;
  }
  function _dynTimeCore(a) {
    if (!a || !a.date) return a && a.time ? a.time : '';
    // bad_date: pub_date 不可信（构建时自动检测：wechat类目/日期倒挂/抓取时间冒充），显示绝对日期
    if (a.bad_date) {
      var d = new Date(a.date);
      if (!isNaN(d.getTime())) {
        var mo = (d.getMonth()+1).toString().padStart(2,'0');
        var da = d.getDate().toString().padStart(2,'0');
        var hh = d.getHours().toString().padStart(2,'0');
        var mm = d.getMinutes().toString().padStart(2,'0');
        return mo + '-' + da + ' ' + hh + ':' + mm;
      }
      return a.time || '';
    }
    var d = new Date(a.date);
    if (isNaN(d.getTime())) return a.time || '';
    var diff = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (diff < 60) return '\u521a\u521a';
    if (diff < 3600) return Math.floor(diff / 60) + ' \u5206\u949f\u524d';
    if (diff < 86400) return Math.floor(diff / 3600) + ' \u5c0f\u65f6\u524d';
    if (diff < 172800) return '\u6628\u5929';
    return Math.floor(diff / 86400) + ' \u5929\u524d';
  }
  function _fmtRel(dstr){
    if(!dstr) return '';
    var d=new Date(dstr); if(isNaN(d.getTime())) return '';
    var diff=(Date.now()-d.getTime())/1000; if(diff<0) diff=0;
    if(diff<60) return '刚刚';
    if(diff<3600) return Math.floor(diff/60)+' 分钟前';
    if(diff<86400) return Math.floor(diff/3600)+' 小时前';
    if(diff<172800) return '昨天';
    return Math.floor(diff/86400)+' 天前';
  }
    /* ── 数据分块加载与合并（rss-data-0/1.js）── */
  function loadChunk(i){
    return new Promise(function(resolve,reject){
      try{
        if(window.__CHUNKS&&window.__CHUNKS[i])return resolve();
        var sc=document.createElement('script'),done=false;
        /* 大 chunk（37MB 级）在慢网络下下载可超 45s：超时只提示、绝不 reject——
           reject 会让 .catch 静默吞掉合并，数据到位也无人消费（线上实证 bug）；
           onload/onerror 才是脚本状态的唯一事实源 */
        var t=setTimeout(function(){if(!done){toast('内容仍在后台加载，完成后自动合并…');}},45000);
        sc.src='rss-data-'+i+'.js?v='+BUILD_TS;
        sc.onload=function(){if(!done){done=true;clearTimeout(t);resolve();}};
        sc.onerror=function(){if(!done){done=true;clearTimeout(t);reject(new Error('chunk '+i+' fail'));}};
        try{document.head.appendChild(sc);}catch(e){clearTimeout(t);throw e;}
      }catch(e){reject(e);}
    });
  }
  /* 富字段分块合并：chunk1 是构建时从 chunk0 切出的剩余项；
     按 link 去重后追加，正常路径零丢失，防御任何来源的数据重叠 */
  function _mergeChunk(pack){
    try{
      var cs=pack&&pack.sources;if(!cs||!cs.length)return 0;
      var idx={};SOURCES.forEach(function(s,i){idx[s.key]=i;});
      var added=0;
      cs.forEach(function(s){
        if(!s||!s.items||!s.items.length)return;
        var i=idx[s.key];
        var _have={};
        if(i!==undefined)SOURCES[i].items.forEach(function(_it){if(_it&&_it.link)_have[_it.link]=1;});
        /* 无 link 的 item 保留，只过滤已知重复 link，避免任何丢数据 */
        var fresh=s.items.filter(function(_it){return _it&&(!_it.link||!_have[_it.link]);});
        if(!fresh.length)return;
        if(i===undefined){s.items=fresh;SOURCES.push(s);idx[s.key]=SOURCES.length-1;added+=fresh.length;return;}
        SOURCES[i].items=SOURCES[i].items.concat(fresh);
        added+=fresh.length;
      });
      /* EV-16 修复：后台合并后仅对新增内容交织，首屏已见内容不换位 */
      if(added>0){buildArt();
        var _oldLimit=wallLimit;
        if(sortMode!=='diverse') tierInterleave();
        wallLimit=Math.min(ART.length,Math.max(wallLimit,120));
        renderChips();renderWall();renderPanel();
        /* 恢复用户已滚动到的位置，避免已见内容换位 */
        if(_oldLimit>WALL_STEP) wallLimit=Math.max(wallLimit,_oldLimit);
      }
      return added;
    }catch(e){console.warn('[starhub] chunk 合并失败（数据在 __CHUNKS，刷新可重试）:', e);return 0;}
  }
  function _mergeRemoteSources(j){
    try{
      if(!j||!j.sources||!j.sources.length) return 0;
      var known={}; for(var i=0;i<ART.length;i++) known[artKey(ART[i])]=1;
      var added=[];
      j.sources.forEach(function(s){
        if(!s||!s.items||!s.items.length) return;
        s.items.forEach(function(it){
          if(!it||!it.u||it.u==='#') return;
          var a={t:it.t_zh||it.t||'', s:it.s||'', src:s.name, sk:s.key, c:s.cat, sc:s.color, ti:s.tier||3,
                 time:_fmtRel(it.d), date:it.d||'', u:it.u, fc:it.fc||'', img:it.img||'', mu:it.mu||'', mt:it.mt||'', bad_date:!!it.bad_date, dfb:!!it.date_fallback, tags:_tagsOf(it)};
          // 远程通道日期钳制：未来日期回拉到 now
          if (a.date) {
            var _at = new Date(a.date).getTime();
            if (!isNaN(_at) && _at > Date.now()) {
              a.date = new Date(Date.now()).toISOString();
            }
          }
          if(!a.t) return;
          var k=artKey(a);
          if(known[k]) return;
          known[k]=1; added.push(a);
        });
      });
      if(!added.length){
        /* 无新增内容时也要纠错：若可见首屏混入无日期条目（NaN 置顶缺陷的指纹），
           重新规范化排序。健康状态下该分支不触发，因此不会造成可见跳变。
           例外：quality 模式按源质量重排，天然会把高质量源的无日期条目顶回首屏，
           谓词会恒真 → 每次刷新都重渲染，故排除该模式。 */
        var _head=Math.min(ART.length, wallLimit||0), _bad=0;
        for(var h=0;h<_head;h++){ var _d=ART[h].date; if(!_d||isNaN(new Date(_d).getTime())) _bad++; }
        if(sortMode!=='quality' && _head>0 && _bad*2>_head){ applySort(); renderChips(); renderWall(); renderPanel(); }
        return 0;
      }
      ART = added.concat(ART);
      /* 不再对 added 单独预排序：紧接着的 applySort() 会对整个 ART 重排，
         预排序对最终顺序无影响（原为 localeCompare，已随口径统一移除） */
      applySort();
      if(sortMode!=='diverse') tierInterleave();
      wallLimit=Math.min(ART.length, Math.max(wallLimit, WALL_STEP));
      window.ART = ART;
      return added.length;
    }catch(e){ return 0; }
  }
  function _applyRemote(j, manual){
    var n=_mergeRemoteSources(j);
    _lastTotal=ART.length;
    if(n>0){
      renderChips(); renderWall(); renderPanel();
    }
    return n;
  }
  /* Fix P2: 分批实时刷新 —— T1 实时 + T2/T3 分批抓取，逐批合并渲染 */
  var _API_BASE='https://starhub-refresh.vercel.app/api/rss';
  window.refreshRss=function(manual){
    if(manual===undefined) manual=true;
    if(_refreshing){ if(manual) toast('正在刷新中…'); return; }
    _refreshing=true;
    var btn=document.getElementById('refreshBtn');
    if(btn) btn.classList.add('loading');
    if(manual) toast('正在实时刷新…');
    var totalAdded=0, done=false;
    var ctrl=window.AbortController?new AbortController():null;
    /* 整体超时 3 分钟（T1 + 多批 T2/T3） */
    var timer=setTimeout(function(){
      if(done) return; done=true;
      if(ctrl) ctrl.abort(); _refreshing=false;
      if(btn) btn.classList.remove('loading');
      if(manual) toast(totalAdded>0?'已更新 '+totalAdded+' 篇（部分批次超时）':'刷新超时');
      if(totalAdded>0){ renderChips(); renderWall(); renderPanel(); }
    }, 180000);
    function fetchJson(url){
      return fetch(url, ctrl?{signal:ctrl.signal}:{}).then(function(r){
        if(!r.ok) throw new Error('HTTP '+r.status);
        return r.json();
      });
    }
    /* Step 1: T1 实时刷新 */
    fetchJson(_API_BASE+'?refresh=1').then(function(j){
      if(done) return;
      totalAdded+=_applyRemote(j, false);
      _lastTotal=ART.length;
      if(totalAdded>0){ renderChips(); renderWall(); renderPanel(); }
      /* Step 2: 获取批次信息 */
      return fetchJson(_API_BASE+'?batch_info=1');
    }).then(function(info){
      if(done) return;
      if(!info||!info.totalBatches) throw new Error('no batch info');
      if(manual) toast('T1 完成，正在抓取 T2/T3（0/'+info.totalBatches+' 批）…');
      /* Step 3: 逐批抓取 T2/T3 */
      var chain=Promise.resolve();
      for(var b=0;b<info.totalBatches;b++){
        (function(batchIdx){
          chain=chain.then(function(){
            if(done) return;
            return fetchJson(_API_BASE+'?batch='+batchIdx).then(function(bj){
              if(done) return;
              var n=_applyRemote(bj, false);
              totalAdded+=n;
              _lastTotal=ART.length;
              if(n>0){ renderChips(); renderWall(); renderPanel(); }
              if(manual) toast('实时刷新 '+((batchIdx+1)*100/info.totalBatches).toFixed(0)+'%…');
            });
          });
        })(b);
      }
      return chain;
    }).then(function(){
      if(done) return; done=true;
      clearTimeout(timer); _refreshing=false;
      if(btn) btn.classList.remove('loading');
      if(manual) toast(totalAdded>0?'已更新 '+totalAdded+' 篇新文章':'已是最新内容');
    }).catch(function(err){
      if(done) return; done=true;
      clearTimeout(timer); _refreshing=false;
      if(btn) btn.classList.remove('loading');
      console.warn('[refreshRss] error:', err);
      if(manual) toast(totalAdded>0?'已更新 '+totalAdded+' 篇（部分失败）':'刷新失败，请稍后重试');
      if(totalAdded>0){ renderChips(); renderWall(); renderPanel(); }
    });
  };
  /* 自动刷新：每 5 分钟用轻量 meta 接口探测新构建，仅当有新内容时才拉取合并（页面隐藏时跳过） */
  setInterval(function(){
    if(document.hidden || _refreshing) return;
    fetch(_API_BASE+'?meta=1').then(function(r){ return r.ok?r.json():null; }).then(function(m){
      if(!m || typeof m.total!=='number') return;
      if(m.total>_lastTotal) window.refreshRss(false);
    }).catch(function(){});
  }, 5*60*1000);

  /* ══════════════════════════════════════════
     Share module: Canvas card + QR code + modal
     ══════════════════════════════════════════ */
  var _qrLoaded=typeof qrcode==='function', _shareDataURL='', _toastTimer;
  function toast(msg){var t=document.getElementById('toast');if(!t)return;t.textContent=msg;t.classList.add('show');clearTimeout(_toastTimer);_toastTimer=setTimeout(function(){t.classList.remove('show');},2000);}
  function copyArticleInfo(k){
    var a=ART.find(function(x){return artKey(x)===k;});
    if(!a)return;
    var text=(a.t||'')+'\\n'+(a.u&&a.u!=='#'?a.u:'');
    function done(){toast('\u5df2\u590d\u5236\u6807\u9898\u4e0e\u94fe\u63a5');}
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(done).catch(function(){
        try{var ta=document.createElement('textarea');ta.value=text;ta.style.cssText='position:fixed;left:-9999px';document.body.appendChild(ta);ta.select();if(document.execCommand('copy'))done();else toast('\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u590d\u5236');document.body.removeChild(ta);}catch(e){toast('\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u590d\u5236');}
      });
    } else {
      try{var ta2=document.createElement('textarea');ta2.value=text;ta2.style.cssText='position:fixed;left:-9999px';document.body.appendChild(ta2);ta2.select();if(document.execCommand('copy'))done();else toast('\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u590d\u5236');document.body.removeChild(ta2);}catch(e){toast('\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u590d\u5236');}
    }
  }

  /* QR 库已构建时内嵌（typeof qrcode==='function' 即同步可用）；
     此函数仅作为内嵌缺失时的 CDN 兜底，带 8s 超时防止 CDN 挂起 */
  function loadQRLib(){
    if(_qrLoaded) return Promise.resolve();
    return new Promise(function(resolve,reject){
      var settled=false;
      var timer=setTimeout(function(){ if(!settled){settled=true;reject(new Error('qr cdn timeout'));} },8000);
      var s=document.createElement('script');
      s.src='https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js';
      s.onload=function(){_qrLoaded=true;settled=true;clearTimeout(timer);resolve();};
      s.onerror=function(){
        if(settled)return;
        var s2=document.createElement('script');
        s2.src='https://unpkg.com/qrcode-generator@1.4.4/qrcode.min.js';
        s2.onload=function(){_qrLoaded=true;settled=true;clearTimeout(timer);resolve();};
        s2.onerror=function(){ if(!settled){settled=true;clearTimeout(timer);reject(new Error('qr cdn failed'));} };
        document.head.appendChild(s2);
      };
      document.head.appendChild(s);
    });
  }

  function wrapText(ctx,text,maxWidth){
    var lines=[],line='';
    for(var i=0;i<text.length;i++){
      var test=line+text[i];
      if(ctx.measureText(test).width>maxWidth&&line){lines.push(line);line=text[i];}
      else{line=test;}
    }
    if(line) lines.push(line);
    return lines;
  }

  // Strip HTML tags → plain text, preserve paragraph breaks
  function stripHtmlForCanvas(html){
    if(!html) return '';
    var t = html;
    // Remove script/style
    t = t.replace(/<script[\s\S]*?<\/script>/gi, '');
    t = t.replace(/<style[\s\S]*?<\/style>/gi, '');
    // Block elements → double newline
    t = t.replace(/<\/?\s*(p|div|blockquote|pre|h[1-6]|li|tr|br)\s*[^>]*>/gi, '\\n');
    // Inline images → alt text or [image]
    t = t.replace(/<img[^>]*alt\s*=\s*"([^"]*)"[^>]*>/gi, ' $1 ');
    t = t.replace(/<img[^>]*>/gi, ' [\u56fe\u7247] ');
    // Links → keep text
    t = t.replace(/<a[^>]*>([\s\S]*?)<\/a>/gi, '$1');
    // Remaining tags
    t = t.replace(/<[^>]+>/g, '');
    // HTML entities
    t = t.replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
         .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')
         .replace(/&#\d+;/g, '').replace(/&[a-z]+;/gi, '');
    // Collapse whitespace within lines, keep paragraph breaks
    t = t.split('\\n').map(function(l){ return l.replace(/\s+/g, ' ').trim(); }).filter(function(l){ return l.length > 0; }).join('\\n');
    return t.trim();
  }

  function getThemeColors(){
    var dark=document.documentElement.dataset.theme==='dark';
    return {
      bg:      dark?'#161412':'#faf9f7',
      title:   dark?'#ece7df':'#1c1917',
      summary: dark?'#a59d90':'#5f594c',
      line:    dark?'#37312a':'#ddd6c9',
      qrFg:    dark?'#ece7df':'#1c1917',
      qrBg:    dark?'#1d1a17':'#fffdf9',
      brand:   dark?'#98907f':'#857e74'
    };
  }

  function drawShareCard(a, fullText){
    var W=750, PAD=40, GAP_T=28, GAP_S=16, GAP_M=24;
    var c=document.createElement('canvas');
    var ctx=c.getContext('2d');
    var col=getThemeColors();
    var font=getComputedStyle(document.body).fontFamily;
    var catColor=a.sc||'#2f5d8a';
    if(document.documentElement.dataset.theme==='dark') catColor=adjColor(a.sc||'#8fb3d9');

    // ─ Resolve content: full text preferred, fallback to summary ──
    var BODY_FONT = 14;
    var BODY_LH = 1.7;
    var MAX_CONTENT_CHARS = 3000;
    var MAX_LINES = 78; /* 限制画布高度，超出移动端 canvas 尺寸上限会导致绘制空白 */
    var contentText = '';
    var useFull = false;
    if(fullText && fullText.length > 60){
      useFull = true;
      contentText = fullText;
      if(contentText.length > MAX_CONTENT_CHARS){
        contentText = contentText.slice(0, MAX_CONTENT_CHARS);
        var lastNl = contentText.lastIndexOf('\\n');
        if(lastNl > MAX_CONTENT_CHARS * 0.7) contentText = contentText.slice(0, lastNl);
        contentText += '\\n\\n\u2026\u2026 \u5168\u6587\u8bf7\u626b\u63cf\u4e8c\u7ef4\u7801\u9605\u8bfb';
      }
    }

    // ── Measure pass ──
    ctx.font='26px '+font;
    var titleLines=wrapText(ctx, a.t||'\u65e0\u6807\u9898\u6587\u7ae0', W-PAD*2);
    var titleH=titleLines.length*(26*1.45);
    var contentLines=[];
    var contentH=0;
    if(useFull){
      ctx.font=BODY_FONT+'px '+font;
      var paras = contentText.split('\\n');
      for(var pi=0;pi<paras.length;pi++){
        var pLines = wrapText(ctx, paras[pi], W-PAD*2);
        for(var li=0;li<pLines.length;li++) contentLines.push(pLines[li]);
        if(pi<paras.length-1) contentLines.push(''); // blank line between paragraphs
      }
      if(contentLines.length > MAX_LINES){
        contentLines = contentLines.slice(0, MAX_LINES);
        contentLines.push('');
        contentLines.push('\u2026\u2026 \u5168\u6587\u8bf7\u626b\u63cf\u4e8c\u7ef4\u7801\u9605\u8bfb');
      }
      contentH = contentLines.length * (BODY_FONT * BODY_LH);
    } else {
      ctx.font='15px '+font;
      if(a.s) contentLines=wrapText(ctx, a.s, W-PAD*2);
      contentH = contentLines.length*(15*1.7);
    }
    var H=PAD+50+GAP_T+titleH+GAP_S+contentH+(contentH>0?GAP_M:0)+1+GAP_M+110+36;

    // ── Create canvas at 2x ──
    c.width=W*2; c.height=H*2;
    ctx.scale(2,2);

    // ── Background with rounded corners ──
    var R=16;
    ctx.beginPath();
    ctx.moveTo(R,0);ctx.lineTo(W-R,0);ctx.quadraticCurveTo(W,0,W,R);
    ctx.lineTo(W,H-R);ctx.quadraticCurveTo(W,H,W-R,H);
    ctx.lineTo(R,H);ctx.quadraticCurveTo(0,H,0,H-R);
    ctx.lineTo(0,R);ctx.quadraticCurveTo(0,0,R,0);
    ctx.closePath();ctx.fillStyle=col.bg;ctx.fill();

    var y=PAD;
    // ── Top bar ──
    ctx.fillStyle=catColor;
    ctx.beginPath();ctx.arc(PAD+3.5,y+6,3.5,0,Math.PI*2);ctx.fill();
    ctx.font='bold 12px '+font;
    ctx.fillStyle=catColor;
    ctx.fillText((CAT_LABELS[a.c]||a.c).toUpperCase(),PAD+14,y+10);
    ctx.font='12px '+font;
    ctx.fillStyle=col.brand;
    ctx.textAlign='right';
    ctx.fillText('StarHub RSS \u805a\u5408',W-PAD,y+10);
    ctx.textAlign='left';
    y+=50+GAP_T;

    // ── Title (never truncated) ──
    ctx.font='bold 26px '+font;
    ctx.fillStyle=col.title;
    for(var i=0;i<titleLines.length;i++){
      ctx.fillText(titleLines[i],PAD,y);
      y+=26*1.45;
    }
    y+=GAP_S;

    // ── Content: full text or summary ──
    if(contentLines.length){
      if(useFull){
        ctx.font=BODY_FONT+'px '+font;
        ctx.fillStyle=col.summary;
        for(var i=0;i<contentLines.length;i++){
          if(contentLines[i]==='') { y+=BODY_FONT*BODY_LH*0.6; continue; }
          ctx.fillText(contentLines[i],PAD,y);
          y+=BODY_FONT*BODY_LH;
        }
      } else {
        ctx.font='15px '+font;
        ctx.fillStyle=col.summary;
        for(var i=0;i<contentLines.length;i++){
          ctx.fillText(contentLines[i],PAD,y);
          y+=15*1.7;
        }
      }
      y+=GAP_M;
    }

    // ── Separator ──
    ctx.strokeStyle=col.line;ctx.lineWidth=1;
    ctx.beginPath();ctx.moveTo(PAD,y);ctx.lineTo(W-PAD,y);ctx.stroke();
    y+=GAP_M;

    // ── Bottom: source + QR ─
    ctx.fillStyle=a.sc||'#2f5d8a';
    ctx.beginPath();ctx.arc(PAD+4,y+6,4,0,Math.PI*2);ctx.fill();
    ctx.font='bold 12px '+font;
    ctx.fillStyle=col.title;
    var srcTxt=a.src||'';
    ctx.fillText(srcTxt,PAD+16,y+10);
    ctx.font='12px '+font;
    ctx.fillStyle=col.summary;
    ctx.fillText('\u00b7 '+_dynTime(a),PAD+16+ctx.measureText(srcTxt).width+6,y+10);

    ctx.font='11px '+font;
    ctx.fillStyle=col.brand;
    ctx.fillText('\u957f\u6309\u8bc6\u522b \u00b7 \u9605\u8bfb\u539f\u6587',PAD,y+40);

    var qrSize=90, qrX=W-PAD-qrSize, qrY=y;
    if(typeof qrcode==='function'){
      try{
        var qrUrl=(a.u&&a.u!=='#')?a.u:location.href;
        var qr=qrcode(0,'M');
        qr.addData(qrUrl);qr.make();
        var cnt=qr.getModuleCount();
        var cell=qrSize/cnt;
        ctx.fillStyle=col.qrBg;
        ctx.fillRect(qrX-4,qrY-4,qrSize+8,qrSize+8);
        ctx.fillStyle=col.qrFg;
        for(var r=0;r<cnt;r++)for(var c2=0;c2<cnt;c2++){
          if(qr.isDark(r,c2)) ctx.fillRect(qrX+c2*cell,qrY+r*cell,Math.ceil(cell),Math.ceil(cell));
        }
      }catch(e){
        ctx.fillStyle=col.summary;ctx.font='11px '+font;
        ctx.textAlign='center';ctx.fillText('\u4e8c\u7ef4\u7801\u6682\u4e0d\u53ef\u7528',qrX+qrSize/2,qrY+qrSize/2);
        ctx.textAlign='left';
      }
    } else {
      ctx.fillStyle=col.summary;ctx.font='11px '+font;
      ctx.textAlign='center';ctx.fillText('\u4e8c\u7ef4\u7801\u6682\u4e0d\u53ef\u7528',qrX+qrSize/2,qrY+qrSize/2);
      ctx.textAlign='left';
    }

    return c.toDataURL('image/png');
  }

  function shareArticle(a,platform,btn){
    if(!a) return;
    if(btn) btn.classList.add('loading');
    // Gather full text from available sources
    var fullText = '';
    var ftEl = document.querySelector('.r2-fulltext');
    if(ftEl && !a._af) fullText = stripHtmlForCanvas(ftEl.innerText || ftEl.textContent);
    if(!fullText && a.fc && a.fc.length > 100) fullText = stripHtmlForCanvas(a.fc);
    if(!fullText && _articleCache[a.u] && _articleCache[a.u].ok) fullText = stripHtmlForCanvas(_articleCache[a.u].content);

    function doShare(text){
      /* 内嵌 QR 后同步出图：模态框立即弹出，不再等待任何网络请求 */
      var url='';
      try{ url = drawShareCard(a, text); }catch(e){}
      if(btn) btn.classList.remove('loading');
      if(!url){
        /* Canvas 不可用（getContext null / toDataURL 异常）：降级纯文本信息卡 */
        toast('\u56fe\u7247\u751f\u6210\u5931\u8d25\uff0c\u5df2\u8f6c\u4e3a\u6587\u5b57\u5206\u4eab');
        showShareTextCard(a, text);
        return;
      }
      _shareDataURL = url;
      showShareModal(url);
      if(!_qrLoaded) loadQRLib().catch(function(){});
    }

    if(fullText){ doShare(fullText); return; }

    // Fetch full text from API — 3s 超时：vercel 域名国内移动端常不可达，快速降级为摘要分享
    if(a.u && a.u !== '#'){
      var apiBase = 'https://starhub-refresh.vercel.app/api/article';
      var settled=false;
      function once(text){ if(settled)return; settled=true; doShare(text); }
      var ctrl = (typeof AbortController==='function') ? new AbortController() : null;
      var timer = ctrl ? setTimeout(function(){ once(''); }, 3000) : null;
      fetch(apiBase + '?url=' + encodeURIComponent(a.u), ctrl?{signal:ctrl.signal}:{}).then(function(r){ return r.json(); }).then(function(d){
        if(timer) clearTimeout(timer);
        var text='';
        if(d.ok && d.content){
          text = stripHtmlForCanvas(d.content);
          if(_articleCache) _articleCache[a.u] = d;
        }
        once(text);
      }).catch(function(){ if(timer) clearTimeout(timer); once(''); });
    } else {
      doShare('');
    }
  }

  function showShareModal(url){
    var m=document.getElementById('shareModal');
    var img=document.getElementById('shareImg');
    img.src=url; img.style.display='';
    var tc=document.getElementById('shareTextCard');
    if(tc) tc.style.display='none';
    var bs=document.getElementById('btnShareSave'); if(bs) bs.style.display='';
    var bc=document.getElementById('btnShareCopy'); if(bc) bc.style.display='';
    var ct=document.getElementById('btnShareCopyText'); if(ct) ct.style.display='none';
    m.classList.add('open');
    var closeBtn=m.querySelector('.share-close');
    if(closeBtn) closeBtn.focus();
  }

  /* ── Canvas 不可用时的纯文本降级卡 ── */
  function _shareTextCard(a, text){
    var lines=[];
    lines.push('\u3010'+(CAT_LABELS[a.c]||a.c||'')+'\u3011'+(a.t||'\u65e0\u6807\u9898\u6587\u7ae0'));
    lines.push('');
    if(text&&text.length>60){ lines.push(text.slice(0,1500)); lines.push(''); }
    else if(a.s){ lines.push(a.s); lines.push(''); }
    lines.push('\u6765\u6e90\uff1a'+(a.src||'')+(a.time?(' \u00b7 '+a.time):''));
    lines.push('\u539f\u6587\uff1a'+((a.u&&a.u!=='#')?a.u:location.href));
    lines.push('\u2014\u2014 StarHub RSS \u805a\u5408');
    return lines.join('\\n');
  }

  function showShareTextCard(a, text){
    _shareDataURL='';
    var img=document.getElementById('shareImg');
    if(img) img.style.display='none';
    var tc=document.getElementById('shareTextCard');
    if(tc){ tc.textContent=_shareTextCard(a,text); tc.style.display='block'; }
    var bs=document.getElementById('btnShareSave'); if(bs) bs.style.display='none';
    var bc=document.getElementById('btnShareCopy'); if(bc) bc.style.display='none';
    var ct=document.getElementById('btnShareCopyText'); if(ct) ct.style.display='';
    document.getElementById('shareModal').classList.add('open');
  }

  function copyShareText(){
    var tc=document.getElementById('shareTextCard');
    if(!tc||!tc.textContent) return;
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(tc.textContent).then(function(){toast('\u6587\u5b57\u5df2\u590d\u5236');}).catch(function(){toast('\u590d\u5236\u5931\u8d25');});
    } else { toast('\u5f53\u524d\u6d4f\u89c8\u5668\u4e0d\u652f\u6301\u590d\u5236'); }
  }

  function closeShareModal(){
    document.getElementById('shareModal').classList.remove('open');
    if(_prevFocusEl){try{_prevFocusEl.focus();}catch(e){}_prevFocusEl=null;}
  }

  function saveShareImage(){
    if(!_shareDataURL) return;
    var a=document.createElement('a');
    a.href=_shareDataURL;a.download='starhub-share.png';
    document.body.appendChild(a);a.click();document.body.removeChild(a);
    toast('\u56fe\u7247\u5df2\u4e0b\u8f7d');
  }

  function copyShareImage(){
    if(!_shareDataURL) return;
    fetch(_shareDataURL).then(function(r){return r.blob();}).then(function(blob){
      if(navigator.clipboard&&window.ClipboardItem){
        navigator.clipboard.write([new ClipboardItem({'image/png':blob})]).then(function(){
          toast('\u56fe\u7247\u5df2\u590d\u5236\u5230\u526a\u8d34\u677f');
        }).catch(function(){toast('\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u957f\u6309\u56fe\u7247\u624b\u52a8\u4fdd\u5b58');});
      } else {toast('\u5f53\u524d\u6d4f\u89c8\u5668\u4e0d\u652f\u6301\u590d\u5236\u56fe\u7247');}
    }).catch(function(){toast('\u590d\u5236\u5931\u8d25');});
  }

  function copyFullHtml(){
    var a = curArt;
    if(!a){ toast('\u8bf7\u5148\u6253\u5f00\u6587\u7ae0'); return; }
    var ftEl = document.querySelector('.r2-fulltext');
    var contentHtml = '';
    if(ftEl){ contentHtml = ftEl.innerHTML; }
    else if(a.fc && a.fc.length > 100){ contentHtml = a.fc; }
    else if(_articleCache[a.u] && _articleCache[a.u].ok){ contentHtml = _articleCache[a.u].content; }
    var srcName = esc(a.src||'');
    var catLabel = CAT_LABELS[a.c]||a.c;
    var fullHtml = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
      + '<meta name="viewport" content="width=device-width,initial-scale=1">'
      + '<title>' + esc(a.t) + ' \u00b7 StarHub</title>'
      + '<style>'
      + 'body{max-width:720px;margin:40px auto;padding:0 20px;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans SC",sans-serif;line-height:1.8;color:#1c1917;background:#fafaf9;}'
      + '.meta{display:flex;align-items:center;gap:8px;font-size:13px;color:#78716c;margin-bottom:24px;flex-wrap:wrap;}'
      + '.meta .cat{color:#2563eb;font-weight:600;}'
      + '.meta .dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:' + (a.sc||'#78716c') + ';}'
      + 'h1{font-size:28px;font-weight:900;line-height:1.3;margin:0 0 16px;}'
      + 'img{max-width:100%;height:auto;border-radius:8px;margin:16px 0;}'
      + 'p{margin:0 0 16px;}a{color:#2563eb;}blockquote{border-left:3px solid #d6d3d1;padding-left:16px;color:#57534e;margin:16px 0;}'
      + 'pre{background:#f5f5f4;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;}'
      + 'code{background:#f5f5f4;padding:2px 6px;border-radius:4px;font-size:13px;}'
      + '.footer{margin-top:40px;padding-top:20px;border-top:1px solid #e7e5e4;font-size:12px;color:#a8a29e;}'
      + '.footer a{color:#78716c;text-decoration:none;}'
      + '</style></head><body>'
      + '<h1>' + esc(a.t) + '</h1>'
      + '<div class="meta"><span class="cat">' + catLabel + '</span>'
      + '<span class="dot"></span><span>' + srcName + '</span>'
      + '<span>\u00b7</span><span title="' + esc(a.date||'') + '">' + _dynTime(a) + '</span></div>';
    if(contentHtml){ fullHtml += '<div class="content">' + contentHtml + '</div>'; }
    else if(a.s){ fullHtml += '<div class="content"><p>' + esc(a.s) + '</p></div>'; }
    fullHtml += '<div class="footer">\u6765\u6e90\uff1a<a href="' + esc(a.u) + '" target="_blank">' + srcName + ' \u2197</a> \u00b7 StarHub RSS \u805a\u5408</div>'
      + '</body></html>';
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(fullHtml).then(function(){
        toast('\u5168\u6587 HTML \u5df2\u590d\u5236\u5230\u526a\u8d34\u677f');
      }).catch(function(){ _fallbackCopy(fullHtml); });
    } else { _fallbackCopy(fullHtml); }
  }
  function _fallbackCopy(text){
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.cssText = 'position:fixed;left:-9999px';
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); toast('\u5168\u6587 HTML \u5df2\u590d\u5236'); }
    catch(e){ toast('\u590d\u5236\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u590d\u5236'); }
    document.body.removeChild(ta);
  }

  window.shareArticle=shareArticle;
  window.r2ShareClick=function(){ shareArticle(curArt,null,document.querySelector('.r2-share-btn')); };
  window.closeShareModal=closeShareModal;
  window.saveShareImage=saveShareImage;
  window.copyShareImage=copyShareImage;
  window.copyFullHtml=copyFullHtml;
  window.toast=toast;

  /* ══════ AI 动态流面板（AIHOT + AGI Hunt） ══════ */
  var AIHOT_API = 'https://aihot.virxact.com/api/v1/items?mode=all&window=24h&limit=40';
  var AGIHUNT_API = 'https://starhub-refresh.vercel.app/api/agihunt';
  var AIHOT_CATS = {'ai-models':['AI \u6a21\u578b','#2563eb'],'ai-products':['AI \u4ea7\u54c1','#7c3aed'],industry:['\u884c\u4e1a\u52a8\u6001','#0891b2'],paper:['\u8bba\u6587','#d97706'],tip:['\u6280\u5de7\u89c2\u70b9','#dc2626']};
  var afLoaded = false;
  var afItems = [], afCursor = '', afFilter = 'all';
  var afAgiSort = 'hot';  // hot | new
  var afRefreshing = false;
  var AGIHUNT_CHANNELS = [
    ['models','\u6a21\u578b','#2563eb'],['research','\u7814\u7a76','#7c3aed'],['coding-agents','\u7f16\u7a0b&Agent','#059669'],
    ['products','\u5e94\u7528','#b45309'],['multimodal','\u591a\u6a21\u6001','#db2777'],['infra','Infra','#475569'],
    ['hardware','\u5177\u8eab','#0891b2'],['funding','\u521b\u6295','#a16207'],['policy','\u5b89\u5168','#dc2626'],
    ['agi','\u6f2b\u8bddAGI','#c026d3'],['companies','\u516c\u53f8\u548c\u4eba','#4d7c0f'],['fun','Fun','#ea580c']
  ];

  function _escH(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
  function _normT(s){ return (s||'').toLowerCase().replace(/[^\p{L}\p{N}]+/gu,''); }
  // 是否需要翻译 = 剥掉 ANSI/URL/标点/符号之后**真有拉丁词**，且中文不占多数，且不是 HN 元数据样板。
  // 旧口径 `cjk < t.replace(/[\s\d\p{P}]/gu,'').length*0.3` 把 + = < ~ 这类数学符号（属于 \p{S}，
  // 不在旧的排除集里）和 ANSI 残片 [1m 一起算进"非中文长度"：线上实测一条中文垃圾文本
  // （💻基本信息 + 78 个 `+` + [1m硬件质量体检报告…）被稀释成"需要翻译"，直连与兜底各发一次。
  var _CJK_R = /[\\u4e00-\\u9fff\\u3400-\\u4dbf]/g;
  var _KANA_R = /[\\u3040-\\u309f\\u30a0-\\u30ff]/;
  var _LETTER_R = /\\p{L}/gu;
  // 注意 `\\b`：这段 JS 活在非 raw 的三引号串里，单写 \b 会被 Python 变成退格符，
  // 词边界整个失效（判据 test_hn_metadata_stub_is_not_translated 就是这么抓到的）。
  var _META_STUB = /^(article url|comments url|points|submitted by|# comments|permalink|story link)\\b/i;
  function _needsTranslation(t){
    if(!t) return false;
    var raw=String(t);
    /* 与构建期 _needs_translation 同一条规则（双向对照判据见
       tests/rss_translate/test_buildtime_skip_guard.py::test_runtime_and_buildtime_predicates_agree）。
       旧版这里用"有没有 ≥2 字母的拉丁词"当门槛 ⇒ 谚文/西里尔/阿拉伯文一个拉丁字母都没有，
       整类被跳过；而摘要是运行时专属（09-16 起移出构建期），等于日韩俄阿的摘要永不翻。
       门槛改成"剥掉 CJK 之后是否还剩任何字母"（\\p{L}，等价于 Python 的 str.isalpha），
       并把"含假名一律翻"这条构建期规则补齐。 */
    if(_KANA_R.test(raw)) return true;
    var cjk=(raw.match(_CJK_R)||[]).length;
    var clean=raw.replace(/\\u001b?\[[0-9;]{1,4}m/g,' ')
                 .replace(/https?:\\/\\S+/gi,' ')
                 .replace(_CJK_R,' ')
                 .replace(/[\s\d\p{P}\p{S}]/gu,' ');
    var letters=(clean.match(_LETTER_R)||[]).length;
    if(!letters) return false;
    if(cjk>=letters) return false;
    var lines=raw.split(/\\r?\\n/).filter(function(l){ return l.trim(); });
    if(lines.length>=3 && lines.filter(function(l){ return _META_STUB.test(l.trim()); }).length*2>=lines.length)
      return false;   // HN 链接投稿的"正文"就是 URL/点数：翻出来只有 URL 和数字，没有信息量
    return true;
  }
  // 批量翻译 AI 动态流英文标题：主力 = 浏览器端 GTX 直连（用户本地 IP，端点响应带 ACAO:* 实证开放；
  // 服务端共享 DC 出口反而会被 Google 频率限流——线上实测 gtx 429）；失败条目再走服务端 API 兜底
  // （api/translate mode:'bulk'：GTX 尽力 → Agnes 限量）。引擎分流策略（用户定版）：Agnes 仅留
  // 给全文/摘要按钮（mode:'full'）与兜底，绝不作为批量主力。
  var TR_API = 'https://starhub-refresh.vercel.app/api/translate';
  // 浏览器 GTX 断路器。两条独立判据，方向相反，不能合成一条：
  //   · 连接层不可达（TypeError / Failed to fetch）= 直连对这个客户端不存在
  //     —— 本仓实测大陆浏览器 8 条文本 / 16 次请求 / 0 成功 ⇒ 第一批就熔断。
  //   · HTTP 4xx/5xx（限流、临时故障）= 能连通 ⇒ 仍要"连续 2 批全败"才熔断，
  //     否则一次抖动就把可用用户的直连掐了，全压到服务端 Agnes（Agnes 是限量兜底，不是主力）。
  // 熔断状态按"今天"存 localStorage：失败条目进不了 _trCache（那里只存成功译文），
  // 不跨访问记住就会每次重访都重发同一批必败请求（线上控制台正是这样刷屏的）。
  var GTX_ABORT_MS = 3500;   // 8s 是给"连通但慢"设的；连不通时等满 8s 只是白等
  var _bgtxDead=false, _bgtxFailStreak=0;
  function _gtxDayKey(){ try{ return localStorage.getItem('_gtxDead'); }catch(e){ return null; } }
  function _gtxDeadToday(){ var d=_gtxDayKey(); return !!d && d===new Date().toISOString().slice(0,10); }
  function _markGtxDead(){ try{ localStorage.setItem('_gtxDead', new Date().toISOString().slice(0,10)); }catch(e){} }
  if(_gtxDeadToday()) _bgtxDead=true;
  function _gtxUnreachable(e){
    var n=String((e&&e.name)||''), m=String((e&&e.message)||e||'');
    if(n==='AbortError'||/abort/i.test(m)) return false;   // 超时不算"不可达"，否则慢网络会被永久误判
    return n==='TypeError'||/failed to fetch|networkerror|cors/i.test(m);
  }
  // 客户端翻译缓存：localStorage 持久化，回访用户直接命中，不再发任何翻译请求
  var _trCache=(function(){
    try{return JSON.parse(localStorage.getItem('_trCache')||'{}');}catch(e){return {};}
  })();
  function _saveTrCache(){
    try{var s=JSON.stringify(_trCache);if(s.length<400000)localStorage.setItem('_trCache',s);}catch(e){}
  }
  /* 浏览器端 GTX 批量直译：并发 3，返回与 texts 等长的译文数组（失败为 ''，由调用方决定服务端兜底）
     + 客户端缓存命中直接返回 + 断路器触发后跳过直连 */
  var _bgtxUnreachable=false;
  /* 截断落在词边界：500 字符硬切会把 "developed" 切成 "develop"，
     线上实测发出过 "...Hundreds of trekkers on the " 这种半句。 */
  function _cutW(s,n){
    s=String(s); if(s.length<=n) return s;
    var c=s.slice(0,n), i=Math.max(c.lastIndexOf(' '), c.lastIndexOf('\\n'));
    return i>40 ? c.slice(0,i) : c;
  }
  function _browserGtx(texts,_ctrlSink){
    var out=[],uncached=[],uncachedIdx=[],_seen={},_dup={};
    // ① 客户端缓存命中：直接填入，不发请求
    for(var k=0;k<texts.length;k++){
      var key=String(texts[k]).slice(0,200),hit=_trCache[key];
      if(hit){out[k]=hit;}
      else if(Object.prototype.hasOwnProperty.call(_seen,key)){ _dup[k]=_seen[k]; out[k]=''; }
      else{_seen[key]=k; out[k]=''; uncached.push(texts[k]); uncachedIdx.push(k);}
    }
    if(!uncached.length) return Promise.resolve(out);
    // ② 断路器已触发（本会话熔断，或今天已经判过不可达）：直接返回空串，由调用方走服务端兜底。
    // 判据放在函数入口而不是只放在加载时：加载时那次读 localStorage 早于任何写入，会漏判。
    if(_bgtxDead || _gtxDeadToday()){ _bgtxDead=true; return Promise.resolve(out); }

    var i=0,done=0,batchFail=true;
    return new Promise(function(resolve){
      function one(){
        if(i>=uncached.length) return;
        var idx=i++;
        var ctrl=(typeof AbortController==='function')?new AbortController():null;
        // 把控制器交出去：换窗口时要能打断**在飞**的那一批（只丢结果不够，请求照扣带宽与配额）
        if(ctrl && _ctrlSink) _ctrlSink.push(ctrl);
        var tmr=ctrl?setTimeout(function(){ctrl.abort();},GTX_ABORT_MS):null;
        fetch('https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q='+encodeURIComponent(_cutW(uncached[idx],500)),{signal:ctrl?ctrl.signal:undefined})
        .then(function(r){ if(tmr)clearTimeout(tmr); return r.ok?r.json():Promise.reject(new Error('gtx '+r.status)); })
        .then(function(j){
          var tr=((j[0]||[]).map(function(x){ return (x&&x[0])||''; }).join('')||'').trim();
          if(tr){ out[uncachedIdx[idx]]=tr; _trCache[String(uncached[idx]).slice(0,200)]=tr; batchFail=false; _bgtxUnreachable=false; }
        }).catch(function(e){ if(tmr)clearTimeout(tmr); if(_gtxUnreachable(e)) _bgtxUnreachable=true; })
        .then(function(){ done++; if(done>=uncached.length){
          for(var q=0;q<texts.length;q++){ if(_dup[q]!==undefined) out[q]=out[_dup[q]]; }
          if(batchFail){ _bgtxFailStreak++; } else { _bgtxFailStreak=0; }
          // 不可达：一批就熔断并跨访问记住；限流：仍要连续 2 批，别把能连通的用户掐了
          if(_bgtxUnreachable){ _bgtxDead=true; _markGtxDead(); }
          else if(_bgtxFailStreak>=2){ _bgtxDead=true; }
          _saveTrCache();
          resolve(out);
        } else { one(); } });
      }
      for(var w=0;w<Math.min(3,uncached.length);w++) one();
    });
  }
  function _translateAfItems(){
    var toTranslate = afItems.filter(function(it){ return !it._zh && _needsTranslation(it.title); });
    if(!toTranslate.length) return;
    // 每批 15 条（与服务端兜底上限匹配），最多 8 批（120 条，覆盖 AIHOT+AGI 全量）
    var batches = []; for(var i=0;i<toTranslate.length && batches.length<8;i+=15) batches.push(toTranslate.slice(i,i+15));
    var applied = 0;
    function _apply(trs, batch){
      batch.forEach(function(it, idx){
        var zh = (trs[idx]||'').trim();
        if(zh && !it._zh){ it._zh = zh; applied++; }
      });
    }
    /* 服务端兜底：仅浏览器端 GTX 失败的零星条目（TR_API bulk：GTX 尽力 → Agnes 限量） */
    function _serverFallback(texts){
      return new Promise(function(resolve){
        var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
        var tmr = ctrl ? setTimeout(function(){ ctrl.abort(); }, 12000) : null;
        fetch(TR_API, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ texts: texts, mode: 'bulk' }),
          signal: ctrl ? ctrl.signal : undefined
        }).then(function(r){
          if(tmr) clearTimeout(tmr);
          return r.ok ? r.json() : Promise.reject(new Error('http ' + r.status));
        }).then(function(j){
          if(!j || !j.ok || !j.translations || j.translations.length !== texts.length) throw new Error('bad payload');
          resolve(j.translations);
        }).catch(function(){
          if(tmr) clearTimeout(tmr);
          resolve(null);
        });
      });
    }
    // 逐批推进：浏览器直连主力，批间 300ms 温和节奏（服务端仅承接零星失败）
    (async function(){
      for(var b=0;b<batches.length;b++){
        var batch = batches[b];
        var trs = await _browserGtx(batch.map(function(it){ return it.title; }));
        _apply(trs, batch);
        var failed = [];
        trs.forEach(function(z, idx){ if(!z) failed.push(idx); });
        if(failed.length){
          var fb = await _serverFallback(failed.map(function(k){ return batch[k].title; }));
          if(fb) failed.forEach(function(k, fi){ var zh=(fb[fi]||'').trim(); if(zh && !batch[k]._zh){ batch[k]._zh = zh; applied++; } });
        }
        if(b+1<batches.length) await new Promise(function(rs){ setTimeout(rs,300); });
      }
      if(applied) _renderAll();
    })();
  }
  /* ── RSS 卡片墙运行时翻译兜底：构建期翻译熔断/漏网的英文条目，挂载于 renderWall 末尾；
     主力 = 浏览器端 GTX 直连，失败条目走服务端 API 兜底，完成后重渲染刷新卡片。

     队列范围刻意取**当前渲染窗口**而不是整条 ART（2026-10-01 实测，HANDOFF §10.25）：
     全库 25766 条里标题只有 5.5% 仍需翻（构建期预翻已覆盖 100% 的 title_zh），摘要 33% 需翻；
     旧写法从 ART 头上遍历，把 9902 条待翻文本按每批 10 条 / 2.5s 走满 ≈41 分钟，
     绝大部分花在看不见的条目上；而 GTX 在大陆不可达时这些整批转 /api/translate
     ⇒ 等于每个访客替全库消耗服务端翻译配额。窗口化后一次首屏 ≈36 个文本，总量随浏览而非全库。

     打断语义（用户 2026-10-01 选定）：窗口指纹一变（切排序 / 改筛选 / 滚动扩窗）就换代、
     abort 在飞请求，并且**旧批次回来的结果一律丢弃** —— 否则会把上一个窗口的译文
     写进用户此刻正看着的条目（串台）。 */
  var _wallTrBusy=0;
  var _wallGen=0, _wallFp='', _wallCtrls=[];
  var WALL_SUMMARY_LIMIT = 30;      // 摘要只翻窗口前 30 条：长文本才是任务量的大头
  function _wallWindow(){
    var w = (typeof visibleArts === 'function') ? visibleArts() : ART;
    if(!w || !w.length) return [];
    return w.slice(0, Math.max(0, wallLimit || 0));
  }
  function _wallFingerprint(win){
    var ft = (typeof filter !== 'undefined' && filter && filter.type) || '';
    var fs = (typeof filter !== 'undefined' && filter && filter.src) || '';
    var sm = (typeof sortMode !== 'undefined' && sortMode) || '';
    /* 指纹只取"窗口头部是谁" + 排序/筛选，**不含 wallLimit 与长度**：
       滚动扩窗是往同一个窗口里追加（不该打断在飞批次，否则快速滚动的人永远翻不完），
       而切排序/换筛选是把窗口整个换掉（必须打断，否则旧结果会串台到新窗口）。 */
    return [sm, ft, fs, win.length ? artKey(win[0]) : ''].join('|');
  }
  function _abortWallInflight(){
    for(var i=0;i<_wallCtrls.length;i++){ try{ _wallCtrls[i].abort(); }catch(e){} }
    _wallCtrls = [];
  }
  function _scheduleWallTranslate(){ setTimeout(_translateWallItems,120); }
  function _applyWallTr(map, idxs, trs, myGen){
    if(myGen !== _wallGen) return;              // 已被新窗口打断：结果作废
    idxs.forEach(function(idx, k){
      var m = map[idx]; if(!m) return;
      var zh = (String(trs[k] || '')).trim(); if(!zh) return;
      if(m.f === 't' && _needsTranslation(m.a.t)) m.a.t = zh;
      else if(m.f === 's' && m.a.s && _needsTranslation(m.a.s)) m.a.s = zh;
    });
  }
  function _translateWallItems(){
    var win = _wallWindow();
    var fp = _wallFingerprint(win);
    if(fp !== _wallFp){                         // 窗口换了：先打断在飞的，再允许立刻重开一轮
      _wallFp = fp; _wallGen++; _abortWallInflight(); _wallTrBusy = 0;
    }
    if(_wallTrBusy) return;
    var myGen = _wallGen;
    var cands = win.filter(function(a){
      return !a._zhTried && (_needsTranslation(a.t) || (a.s && _needsTranslation(a.s)));
    });
    if(!cands.length) return;
    _wallTrBusy = 1;
    var texts = [], map = [];
    cands.forEach(function(a){
      a._zhTried = 1;                            // 打过标记就不再重发（失败条目保留原文）
      if(_needsTranslation(a.t)){ texts.push(a.t); map.push({a:a, f:'t'}); }
    });
    cands.slice(0, WALL_SUMMARY_LIMIT).forEach(function(a){
      if(a.s && _needsTranslation(a.s)){ texts.push(a.s); map.push({a:a, f:'s'}); }
    });
    if(!texts.length){ _wallTrBusy = 0; return; }
    var ctrls = [];
    _wallCtrls = ctrls;                          // 交给换代时 abort 的那份引用
    (async function(){
      var trs = await _browserGtx(texts, ctrls);
      if(myGen !== _wallGen) return;
      var failed = [], okIdx = [];
      trs.forEach(function(zhRaw, idx){
        if(String(zhRaw || '').trim()) okIdx.push(idx); else failed.push(idx);
      });
      _applyWallTr(map, okIdx, trs, myGen);
      if(failed.length){
        /* 服务端兜底：按 15 条分片（bulk 上限），每片同样受代数管制 */
        for(var s = 0; s < failed.length; s += 15){
          var part = failed.slice(s, s + 15);
          var fbTexts = part.map(function(k){ return texts[k]; });
          var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
          if(ctrl) ctrls.push(ctrl);
          var tmr = ctrl ? setTimeout(function(){ ctrl.abort(); }, 12000) : null;
          try{
            var resp = await fetch(TR_API, {method:'POST', headers:{'Content-Type':'application/json'},
                                            body: JSON.stringify({texts: fbTexts, mode: 'bulk'}),
                                            signal: ctrl ? ctrl.signal : undefined});
            if(tmr) clearTimeout(tmr);
            var j = (resp && resp.ok) ? (await resp.json().catch(function(){ return null; })) : null;
            if(j && j.ok && j.translations && j.translations.length === fbTexts.length){
              _applyWallTr(map, part, j.translations, myGen);
            }
          }catch(e){ if(tmr) clearTimeout(tmr); }
          if(myGen !== _wallGen) return;
        }
      }
      _wallTrBusy = 0;
      renderWall();
      setTimeout(_translateWallItems, 2000);
    })();
  }
  /* 运行时翻译诊断：摘要翻译自 2026-09-16 起移出构建期，"到底翻没翻"只有浏览器知道，
     构建日志看不到这一半。带 ?transdiag=1 自动跑一次，或在 console 里调 transDiag()。 */
  function _transDiag(){
    var cjk = function(s){ return /[一-鿿]/.test(String(s || '')); };
    var rep = { at: new Date().toISOString(), total: ART.length };
    var sNeed = 0, sDone = 0, tNeed = 0, tDone = 0, tried = 0;
    ART.forEach(function(a){
      if (a.s && String(a.s).trim()) { if (cjk(a.s)) sDone++; else sNeed++; }
      if (a.t) { if (cjk(a.t)) tDone++; else tNeed++; }
      if (a._zhTried) tried++;
    });
    rep.summary_english = sNeed; rep.summary_cjk = sDone;
    rep.title_english = tNeed; rep.title_cjk = tDone;
    rep.tried = tried; rep.untried = ART.length - tried;
    function probe(name, url, opts){
      var t0 = Date.now();
      return fetch(url, opts).then(function(r){
        return r.text().then(function(t){
          return { name: name, ok: r.ok, status: r.status, ms: Date.now() - t0, head: String(t).slice(0, 36) };
        });
      }).catch(function(e){
        return { name: name, ok: false, ms: Date.now() - t0, err: String(e).slice(0, 56) };
      });
    }
    var sample = 'the riverbed hosts coral reef and betta fish';
    var p1 = probe('gtx_direct ', 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q=' + encodeURIComponent(sample));
    var p2 = probe('vercel_bulk', TR_API, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ texts: [sample], mode: 'bulk' }) });
    Promise.all([p1, p2]).then(function(rs){
      rep.channels = rs;
      try { console.log('[transDiag] ' + JSON.stringify(rep)); } catch (e) {}
      _renderDiag(rep);
    });
    return rep;
  }
  function _renderDiag(rep){
    var NL = String.fromCharCode(10);
    var d = document.getElementById('transDiag');
    if (!d) {
      d = document.createElement('div'); d.id = 'transDiag';
      d.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:9999;max-width:92vw;background:#111;color:#eee;font:11px/1.5 monospace;padding:8px 10px;border:1px solid #444;border-radius:6px;white-space:pre-wrap';
      document.body.appendChild(d);
    }
    var lines = ['运行时翻译诊断 ' + rep.at,
      '条目 ' + rep.total + ' ｜ 摘要：英文 ' + rep.summary_english + ' / 中文 ' + rep.summary_cjk,
      '标题：英文 ' + rep.title_english + ' / 中文 ' + rep.title_cjk,
      '补翻已尝试 ' + rep.tried + ' / 未尝试 ' + rep.untried];
    (rep.channels || []).forEach(function(c){
      lines.push(c.name + (c.ok ? ' HTTP ' + c.status : ' 失败') + ' ' + c.ms + 'ms ' + (c.head || c.err || ''));
    });
    d.textContent = lines.join(NL);
  }
  window.transDiag = _transDiag;
  if (location.search.indexOf('transdiag=1') >= 0) setTimeout(_transDiag, 12000);
  function _fmtRel(s){ if(!s) return ''; try{ var d=new Date(s),n=Date.now(),diff=n-d.getTime(); if(diff<0)return ''; var m=Math.floor(diff/60000); if(m<1)return '\u521a\u521a'; if(m<60)return m+' \u5206\u949f\u524d'; var h=Math.floor(m/60); if(h<24)return h+' \u5c0f\u65f6\u524d'; return Math.floor(h/24)+' \u5929\u524d'; }catch(e){return '';} }

  function toggleAiFeed(){
    var open = document.body.classList.toggle('ai-open');
    document.getElementById('btnAiFeed').classList.toggle('on', open);
    var hb=document.getElementById('btnHot'); if(hb) hb.classList.toggle('on', open && afTab==='hot');
    if(open && !afLoaded) _loadAll();
  }
  window.toggleAiFeed = toggleAiFeed;

  /* ── 刷新（参照主页 refreshRss 模式） ── */
  function refreshAiFeed(){
    /* D6: 热榜 Tab 下刷新热榜数据 */
    if(afTab==='hot'){
      _hotLoaded=false; _hotLoading=false;
      loadHotSnapshot();
      return;
    }
    if(afRefreshing) return;
    if(!afLoaded) { _loadAll(); return; }
    afRefreshing = true;
    var btn = document.getElementById('afRefreshBtn');
    if(btn) btn.classList.add('loading');
    var settled = false;
    var timer = setTimeout(function(){
      if(settled) return; settled = true; afRefreshing = false;
      if(btn) btn.classList.remove('loading');
    }, 30000);
    _loadAll(true).then(function(){
      if(settled) return; settled = true; afRefreshing = false;
      clearTimeout(timer);
      if(btn) btn.classList.remove('loading');
    }).catch(function(){
      if(settled) return; settled = true; afRefreshing = false;
      clearTimeout(timer);
      if(btn) btn.classList.remove('loading');
    });
  }
  window.refreshAiFeed = refreshAiFeed;



  /* ── 统一加载 AIHOT + AGI Hunt（渐进式渲染：先到先渲染，全部加超时兜底） ── */
  async function _loadAll(isRefresh){
    var list = document.getElementById('afList');
    var upd = document.getElementById('afUpdated');
    if(!isRefresh){
      list.innerHTML = '<div class="af-loading"><span class="af-spin"></span> \u52a0\u8f7d\u4e2d\u2026</div>';
      upd.textContent = '\u52a0\u8f7d\u4e2d\u2026';
    }
    var seen = new Set();
    var merged = [];
    var rendered = false;

    // \u2460 AIHOT \u8bf7\u6c42\uff1a\u8d85\u65f6 10s\uff08\u79fb\u52a8\u7aef\u5f31\u7f51\u5146\u5e95\uff09
    var aiCtrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var aiTimer = aiCtrl ? setTimeout(function(){ aiCtrl.abort(); }, 10000) : null;
    var aihotP = fetch(AIHOT_API, aiCtrl ? { signal: aiCtrl.signal } : {}).then(function(r){
      if(!r.ok) throw new Error('http '+r.status);
      return r.json();
    }).then(function(j){
      if(aiTimer) clearTimeout(aiTimer);
      afCursor = (j.page&&j.page.hasMore) ? (j.page.nextCursor||'') : '';
      (j.items||[]).forEach(function(it){
        var k=_normT(it.title);
        if(!seen.has(k)){seen.add(k); merged.push(Object.assign({},it,{_src:'aihot'}));}
      });
      // AIHOT \u5148\u5230\u5148\u6e32\u67d3\uff0c\u4e0d\u7b49 AGI Hunt
      if(!rendered && merged.length){
        rendered = true;
        afItems = merged.slice();
        afItems.sort(function(a,b){return (b.publishedAt||b.published_at||'').localeCompare(a.publishedAt||a.published_at||'');});
        _renderAll();
        _translateAfItems();
        upd.textContent = 'AI \u52a8\u6001\u6d41 \u00b7 ' + afItems.length + ' \u6761 \u00b7 \u66f4\u65b0\u4e8e ' + new Date().toLocaleTimeString('zh-CN',{hour12:false});
      }
    }).catch(function(){ if(aiTimer) clearTimeout(aiTimer); afCursor=''; });

    // \u2461 AGI Hunt \u8bf7\u6c42\uff08\u987a\u5e8f\u8bf7\u6c42\uff0c\u907f\u514d\u79fb\u52a8\u7aef\u5e76\u53d1\u8fde\u63a5\u69fd\u6392\u961f\uff09\uff1a\u5355\u8bf7\u6c42 5s \u8d85\u65f6 + \u603b\u8d85\u65f6 30s
    var agiCtrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var agiTimer = agiCtrl ? setTimeout(function(){ agiCtrl.abort(); }, 30000) : null;
    var today = new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    var agihuntP = (async function(){
      for(var ci=0; ci<AGIHUNT_CHANNELS.length; ci++){
        if(agiCtrl && agiCtrl.signal.aborted) break;
        var ch = AGIHUNT_CHANNELS[ci];
        var chCtrl = (typeof AbortController === 'function') ? new AbortController() : null;
        var chTimer = chCtrl ? setTimeout(function(){ chCtrl.abort(); }, 5000) : null;
        try{
          var r = await fetch(AGIHUNT_API+'?channel='+ch[0]+'&day='+today+'&sort='+afAgiSort,
            chCtrl ? { signal: chCtrl.signal } : (agiCtrl ? { signal: agiCtrl.signal } : {}));
          if(chTimer) clearTimeout(chTimer);
          if(!r.ok) continue;
          var j = await r.json();
          (j&&j.items||[]).forEach(function(it){
            var k=_normT(it.title);
            if(!seen.has(k)){seen.add(k); merged.push(Object.assign({},it,{_src:'agihunt',_ch:ch[0]}));}
          });
        }catch(e){ if(chTimer) clearTimeout(chTimer); }
      }
      if(agiTimer) clearTimeout(agiTimer);
    })();

    // \u7b49\u5f85 AIHOT + AGI Hunt \u5168\u90e8 settle\uff08\u5404\u81ea\u5df2\u6709\u8d85\u65f6\u4fdd\u62a4\uff09
    await Promise.allSettled([aihotP, agihuntP]);
    afItems = merged;
    afItems.sort(function(a,b){return (b.publishedAt||b.published_at||'').localeCompare(a.publishedAt||a.published_at||'');});
    afLoaded = true;
    _renderAll();
    _translateAfItems();
    if(!afItems.length){
      upd.textContent = 'AI \u52a8\u6001\u6d41';
      list.innerHTML = '<div class="af-empty">\u6682\u65e0\u52a8\u6001\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5</div>';
    } else {
      upd.textContent = 'AI \u52a8\u6001\u6d41 \u00b7 ' + afItems.length + ' \u6761 \u00b7 \u66f4\u65b0\u4e8e ' + new Date().toLocaleTimeString('zh-CN',{hour12:false});
    }

    // \u2462 \u5f02\u6b65\u8ffd\u52a0 /api/news\uff1a\u8d85\u65f6 12s
    var newsCtrl = (typeof AbortController === 'function') ? new AbortController() : null;
    var newsTimer = newsCtrl ? setTimeout(function(){ newsCtrl.abort(); }, 12000) : null;
    fetch('https://starhub-refresh.vercel.app/api/news', newsCtrl ? { signal: newsCtrl.signal } : {}).then(function(r){
      if(newsTimer) clearTimeout(newsTimer);
      return r.ok?r.json():null;
    }).then(function(nj){
      if(!nj||!nj.items||!nj.items.length) return;
      var cutoff=Date.now()-24*3600000;
      var seen2=new Set(afItems.map(function(x){return _normT(x.title);}));
      nj.items.filter(function(it){return it.title&&it.link&&(!it.publishedAt||new Date(it.publishedAt).getTime()>=cutoff);})
        .slice(0,15).forEach(function(it){
          var k=_normT(it.title); if(!seen2.has(k)){seen2.add(k);
            afItems.push({title:it.title,category:'industry',source:{name:it.source||'36\u6c2a'},links:{original:it.link},publishedAt:it.publishedAt||'',selected:false,_src:'aihot'});
          }
        });
      afItems.sort(function(a,b){return (b.publishedAt||b.published_at||'').localeCompare(a.publishedAt||a.published_at||'');});
      _renderAll();
    }).catch(function(){ if(newsTimer) clearTimeout(newsTimer); });
  }

  /* ── AI 动态条目 → 分享卡片 ART 结构（复用卡片墙分享链路） ── */
  function _afToArt(it){
    if(!it) return null;
    if(it._src==='agihunt'){
      var chInfo = AGIHUNT_CHANNELS.filter(function(c){return c[0]===it._ch;})[0];
      return {t:it.title||'', s:'', c:chInfo?chInfo[1]:'AGI Hunt', sc:(chInfo&&chInfo[2])||'#6366f1',
        src:it.author||'AGI Hunt', u:it.url||'', time:_fmtRel(it.published_at), _af:true};
    }
    var ci = AIHOT_CATS[it.category] || ['\u52a8\u6001','#8b949e'];
    return {t:it.title||'', s:'', c:ci[0], sc:ci[1],
      src:(it.source&&it.source.name)||'AIHOT', u:(it.links&&(it.links.original||it.links.aihot))||'',
      time:_fmtRel(it.publishedAt), _af:true};
  }

  function _renderAll(){
    var list = document.getElementById('afList');
    var filterBox = document.getElementById('afFilter');
    var moreBtn = document.getElementById('afLoadMore');
    // 筛选条：全部 / AIHOT分类 / AGI Hunt频道
    var filtered = afItems;
    if(afFilter !== 'all'){
      if(afFilter.startsWith('ch:')){
        var ch = afFilter.slice(3);
        filtered = afItems.filter(function(it){return it._ch===ch;});
      } else {
        filtered = afItems.filter(function(it){return it._src==='aihot'&&it.category===afFilter;});
      }
    }
    if(afItems.length){
      filterBox.style.display = '';
      var chips = [['all','\u5168\u90e8',afItems.length]];
      // AIHOT 分类
      Object.keys(AIHOT_CATS).forEach(function(k){
        var n = afItems.filter(function(it){return it._src==='aihot'&&it.category===k;}).length;
        if(n) chips.push([k,AIHOT_CATS[k][0],n]);
      });
      // AGI Hunt 频道
      AGIHUNT_CHANNELS.forEach(function(c){
        var n = afItems.filter(function(it){return it._ch===c[0];}).length;
        if(n) chips.push(['ch:'+c[0],c[1],n]);
      });
      filterBox.innerHTML = chips.map(function(c){
        return '<button class="fchip'+(afFilter===c[0]?' on':'')+'" data-cat="'+c[0]+'">'+c[1]+' <span class="n">'+c[2]+'</span></button>';
      }).join('');
      // AGI Hunt 排序切换（仅选中频道时显示）
      if(afFilter.indexOf('ch:')===0){
        filterBox.innerHTML += '<button class="fchip" data-sort="'+(afAgiSort==='hot'?'new':'hot')+'" style="margin-left:auto">'+(afAgiSort==='hot'?'\u2192 \u6700\u65b0':'\u2192 \u6700\u70ed')+'</button>';
      }
      filterBox.querySelectorAll('.fchip').forEach(function(chip){
        chip.addEventListener('click', function(){
          var cat = chip.getAttribute('data-cat');
          if(cat){ afFilter=cat; _renderAll(); }
          var sort = chip.getAttribute('data-sort');
          if(sort){ afAgiSort=sort; _refetchAgiChannel(); }
        });
      });
    } else { filterBox.style.display = 'none'; }
    // 列表
    if(!filtered.length){ list.innerHTML = '<div class="af-empty">\u6682\u65e0\u52a8\u6001</div>'; moreBtn.style.display='none'; return; }
    list.innerHTML = filtered.map(function(it, i){
      var isAgi = it._src==='agihunt';
      var cat, url, src, tm;
      if(isAgi){
        var chInfo = AGIHUNT_CHANNELS.filter(function(c){return c[0]===it._ch;})[0];
        cat = [chInfo?chInfo[1]:'\u52a8\u6001',(chInfo&&chInfo[2])||'#6366f1'];
        url = it.url || '#';
        src = it.author || 'AGI Hunt';
        tm = _fmtRel(it.published_at);
      } else {
        cat = AIHOT_CATS[it.category] || ['\u52a8\u6001','#8b949e'];
        url = (it.links&&(it.links.original||it.links.aihot)) || '#';
        src = (it.source&&it.source.name)||'';
        tm = _fmtRel(it.publishedAt);
      }
      var title = it._zh || it.title || '';
      return '<div class="af-item"><span class="cat" style="color:'+cat[1]+';background:'+cat[1]+'1a">'+_escH(cat[0])+'</span>'
        +'<div class="body"><a class="t" href="'+_escH(url)+'" target="_blank" rel="noopener" title="'+_escH(it.title||'')+'">'+_escH(title)+'</a>'
        +'<span class="meta">'+_escH(src)+(src&&tm?' \u00b7 ':'')+_escH(tm)+(it.selected?' \u00b7 \u2605 \u7cbe\u9009':'')+'</span></div>'
        +'<button class="af-share" data-i="'+i+'" title="\u5206\u4eab"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button></div>';
    }).join('');
    // 分享：转为 ART 结构后走统一分享链路（全文 API + 3s 超时降级摘要）
    list.querySelectorAll('.af-share').forEach(function(btn){
      btn.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation();
        var art = _afToArt(filtered[parseInt(btn.getAttribute('data-i'),10)]||null);
        if(art) shareArticle(art, null, btn);
      });
    });
    moreBtn.style.display = (afCursor && afFilter==='all') ? '' : 'none';
  }

  // 加载更多（仅 AIHOT 游标分页）
  document.getElementById('afLoadMore').addEventListener('click', async function(){
    var btn = this; if(!afCursor) return;
    btn.disabled = true; btn.textContent = '\u52a0\u8f7d\u4e2d\u2026';
    try{
      var r = await fetch(AIHOT_API + '&cursor=' + encodeURIComponent(afCursor));
      if(!r.ok) throw new Error('http '+r.status);
      var j = await r.json();
      afCursor = (j.page&&j.page.hasMore) ? (j.page.nextCursor||'') : '';
      var seen = new Set(afItems.map(function(x){return _normT(x.title);}));
      (j.items||[]).forEach(function(it){var k=_normT(it.title);if(!seen.has(k)){seen.add(k);afItems.push(Object.assign({},it,{_src:'aihot'}));}});
      _renderAll();
      _translateAfItems();
    }catch(e){ /* ignore */ }
    btn.disabled = false; btn.textContent = '\u52a0\u8f7d\u66f4\u591a';
  });

  /* ── 重取当前 AGI Hunt 频道（排序切换） ── */
  async function _refetchAgiChannel(){
    var ch = afFilter.slice(3);
    var today = new Date(Date.now()+8*3600000).toISOString().slice(0,10);
    try{
      var r = await fetch(AGIHUNT_API+'?channel='+ch+'&day='+today+'&sort='+afAgiSort);
      if(!r.ok) return;
      var j = await r.json();
      var newItems = (j&&j.items||[]).map(function(it){return Object.assign({},it,{_src:'agihunt',_ch:ch});});
      afItems = afItems.filter(function(it){return !(it._src==='agihunt'&&it._ch===ch);});
      var seen = new Set(afItems.map(function(x){return _normT(x.title);}));
      newItems.forEach(function(it){
        var k=_normT(it.title);
        if(!seen.has(k)){seen.add(k); afItems.push(it);}
      });
      afItems.sort(function(a,b){return (b.publishedAt||b.published_at||'').localeCompare(a.publishedAt||a.published_at||'');});
      _renderAll();
      _translateAfItems();
    }catch(e){ /* ignore */ }
  }

  /* ── 自动刷新：每 5 分钟（面板打开时；桌面端 ≥1280px 常驻侧栏始终视为打开） ── */
  function _aiDesktop(){ return window.matchMedia('(min-width:1280px)').matches; }
  setInterval(function(){
    if(document.hidden || afRefreshing) return;
    if(!document.body.classList.contains('ai-open') && !_aiDesktop()) return;
    refreshAiFeed();
  }, 5*60*1000);
  /* 桌面端常驻侧栏：进入页面即加载 AI 动态（移动端保持点击按钮后加载） */
  if(_aiDesktop() && !afLoaded) _loadAll();

  /* ══════════════════════════════════════════
     面板 Tab：AI 快讯 / 全网热榜
     ══════════════════════════════════════════ */
  var afTab='feed';
  var _hotLoaded=false,_hotLoading=false,_hotData=null;
  function switchAfTab(tab){
    afTab=tab;
    document.body.classList.toggle('af-tab-hot',tab==='hot');
    var tf=document.getElementById('afTabFeed'),th=document.getElementById('afTabHot');
    if(tf){tf.classList.toggle('on',tab==='feed');tf.setAttribute('aria-selected',tab==='feed'?'true':'false');}
    if(th){th.classList.toggle('on',tab==='hot');th.setAttribute('aria-selected',tab==='hot'?'true':'false');}
    var hw=document.getElementById('hpWrap');
    if(hw) hw.style.display=(tab==='hot')?'flex':'none';
    var rb=document.getElementById('afRefreshBtn'); if(rb) rb.style.display=''; /* D6: 双 Tab 均显示刷新按钮 */
    var hb=document.getElementById('btnHot'); if(hb) hb.classList.toggle('on',tab==='hot'&&document.body.classList.contains('ai-open'));
    if(tab==='hot'&&!_hotLoaded) loadHotSnapshot();
  }
  window.switchAfTab=switchAfTab;
  var _hotPlatformNames={weibo:'微博',zhihu:'知乎',baidu:'百度',bilibili:'B站',douyin:'抖音',ithome:'IT之家',hackernews:'Hacker News',github:'GitHub',solidot:'Solidot',sspai:'少数派',juejin:'掘金',v2ex:'V2EX',producthunt:'Product Hunt',aihot:'AIHOT',chongbuluo:'虫部落',pcbeta:'远景论坛',nowcoder:'牛客',coolapk:'酷安',xueqiu:'雪球',zaobao:'联合早报',wallstreetcn:'华尔街见闻',cls:'财联社',jin10:'金十数据',gelonghui:'格隆汇',fastbull:'法布财经',toutiao:'今日头条',tencent:'腾讯新闻',thepaper:'澎湃新闻',ifeng:'凤凰网',cankaoxiaoxi:'参考消息',sputniknewscn:'卫星通讯社',kaopu:'靠谱',mktnews:'市场资讯',douban:'豆瓣',tieba:'贴吧',hupu:'虎扑',steam:'Steam',iqiyi:'爱奇艺',qqvideo:'腾讯视频',dongqiudi:'懂球帝'};
  /* dot：平台品牌色（未选中态圆点，提供平台色差区分）；deep：选中态实底色（加深变体，白字对比 ≥4.4:1，明暗主题均可读） */
  var _hotPlatformColors={weibo:'#ff4400',zhihu:'#0066ff',baidu:'#2932e1',bilibili:'#fb7299',douyin:'#fe2c55',ithome:'#d32f2f',hackernews:'#ff6600',github:'#6e5491',solidot:'#4caf50',sspai:'#da3325',juejin:'#1e80ff',v2ex:'#778087',producthunt:'#da552f',aihot:'#0891b2',chongbuluo:'#558b2f',pcbeta:'#1565c0',nowcoder:'#0097a7',coolapk:'#10b981',xueqiu:'#1e88e5',zaobao:'#c62828',wallstreetcn:'#1565c0',cls:'#0277bd',jin10:'#ef6c00',gelonghui:'#00897b',fastbull:'#f57c00',toutiao:'#d32f2f',tencent:'#1976d2',thepaper:'#c62828',ifeng:'#e64a19',cankaoxiaoxi:'#ad1457',sputniknewscn:'#283593',kaopu:'#2e7d32',mktnews:'#4e342e',douban:'#007722',tieba:'#4caf50',hupu:'#d32f2f',steam:'#1b2838',iqiyi:'#00be07',qqvideo:'#ff6900',dongqiudi:'#2e7d32'};
  var _hotPlatformDeep={weibo:'#d5380f',zhihu:'#0052cc',baidu:'#1f28b8',bilibili:'#c73a6c',douyin:'#d9284a',ithome:'#b71c1c',hackernews:'#cc5200',github:'#4a3769',solidot:'#2e7d32',sspai:'#b71c1c',juejin:'#1565c0',v2ex:'#5a5f66',producthunt:'#b5441f',aihot:'#067090',chongbuluo:'#33691e',pcbeta:'#0d47a1',nowcoder:'#006064',coolapk:'#047857',xueqiu:'#1565c0',zaobao:'#8e0000',wallstreetcn:'#0d47a1',cls:'#01579b',jin10:'#e65100',gelonghui:'#00695c',fastbull:'#ef6c00',toutiao:'#b71c1c',tencent:'#0d47a1',thepaper:'#8e0000',ifeng:'#bf360c',cankaoxiaoxi:'#78002e',sputniknewscn:'#1a237e',kaopu:'#1b5e20',mktnews:'#3e2723',douban:'#004400',tieba:'#2e7d32',hupu:'#b71c1c',steam:'#0d1117',iqiyi:'#007a07',qqvideo:'#cc5400',dongqiudi:'#1b5e20'};
  /* 分类筛选：key→中文名，members 为平台 ID 数组 */
  var _hotCats=[
    {key:'all',name:'\u5168\u90e8',members:[]},
    {key:'hot',name:'\u70ed\u641c',members:['weibo','zhihu','baidu','bilibili','douyin']},
    {key:'tech',name:'\u79d1\u6280',members:['ithome','hackernews','github','solidot','sspai','juejin','v2ex','producthunt','aihot','chongbuluo','pcbeta','nowcoder','coolapk']},
    {key:'biz',name:'\u8d22\u7ecf',members:['xueqiu','zaobao','wallstreetcn','cls','jin10','gelonghui','fastbull']},
    {key:'news',name:'\u8d44\u8baf',members:['toutiao','tencent','thepaper','ifeng','cankaoxiaoxi','sputniknewscn','kaopu','mktnews']},
    {key:'life',name:'\u751f\u6d3b',members:['douban','tieba','hupu','steam','iqiyi','qqvideo','dongqiudi']}
  ];
  var _hotCatKey='all';
  window.toggleHotPanel=function(){
    var panelOpen=document.body.classList.contains('ai-open');
    if(panelOpen&&afTab==='hot'){toggleAiFeed();return;} /* 已在热榜 Tab→再次点击关闭抽屉（移动端习惯） */
    if(!panelOpen) toggleAiFeed();
    switchAfTab('hot');
  };
  var _platExpanded=false;
  function _applyPlatCollapse(){
    var row=document.getElementById('hpTabs');
    if(row) row.classList.toggle('collapsed',window.innerWidth<900&&!_platExpanded);
  }
  /* 平台→分类映射：根据平台 ID 返回所属分类 key */
  function _platCat(p){
    for(var i=0;i<_hotCats.length;i++){
      if(_hotCats[i].members&&_hotCats[i].members.indexOf(p)!==-1) return _hotCats[i].key;
    }
    return 'all';
  }
  function loadHotSnapshot(){
    if(_hotLoading || _hotLoaded) return;
    _hotLoading=true;
    var list=document.getElementById('hotList');
    if(!list) return;
    list.innerHTML=\'<div class="hp-empty">加载中…</div>\';
    fetch(\'hot_snapshot.json\',{cache:\'no-cache\'}).then(function(r){
      if(!r.ok) throw new Error(\'HTTP \'+r.status);
      return r.json();
    }).then(function(data){
      _hotLoaded=true;_hotLoading=false;
      _hotData=(data||[]).filter(function(s){return s.items&&s.items.length;});
      if(!_hotData.length){list.innerHTML=\'<div class="hp-empty">\u6682\u65e0\u70ed\u699c\u6570\u636e</div>\';return;}
      /* 分类筛选行 → 分段控件（WS-A2） */
      var segEl=document.getElementById(\'hpCats\');
      if(segEl){
        segEl.className=\'hp-seg\';
        segEl.setAttribute(\'role\',\'tablist\');
        segEl.innerHTML=_hotCats.map(function(c){
          return \'<button class="hp-seg-btn\'+(c.key===\'all\'?\' on\':\'\')+\'" role="tab" aria-selected="\'+(c.key===\'all\')+\'" data-cat="\'+c.key+\'">\'+c.name+\'</button>\';
        }).join(\'\');
        segEl.querySelectorAll(\'.hp-seg-btn\').forEach(function(b){
          b.addEventListener(\'click\',function(){_filterHotCat(b.getAttribute(\'data-cat\'));});
        });
      }
      /* 平台 chips（WS-A3） */
      var row=document.getElementById(\'hpTabs\');
      row.className=\'plat-row collapsed\';
      row.innerHTML=_hotData.map(function(s){
        var p=s.platform;
        return \'<button class="plat-chip" data-p="\'+p+\'" data-cat="\'+_platCat(p)+\'"><span class="dot" style="background:\'+(_hotPlatformColors[p]||\'#888\')+\'"></span>\'+(_hotPlatformNames[p]||p)+\'</button>\';
      }).join(\'\');
      /* 展开/收起按钮（WS-A4 移动端折叠） */
      var moreHtml=\'<button class="plat-more" id="btnPlatMore" aria-expanded="false"><span>全部 \'+_hotData.length+\' 个</span>\';
      moreHtml+=\'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></button>\';
      var wrap=row.parentNode.querySelector(\'.plat-wrap\');
      if(wrap){var existing=document.getElementById(\'btnPlatMore\');if(!existing) row.insertAdjacentHTML(\'afterend\',moreHtml);}
      var moreBtn=document.getElementById(\'btnPlatMore\');
      if(moreBtn){
        moreBtn.onclick=function(){
          _platExpanded=!_platExpanded;
          row.classList.toggle(\'collapsed\',window.innerWidth<900&&!_platExpanded);
          moreBtn.querySelector(\'span\').textContent=_platExpanded?\'收起\':\'全部 \'+_visiblePlats().length+\' 个\';
          moreBtn.classList.toggle(\'open\',_platExpanded);
          moreBtn.setAttribute(\'aria-expanded\',_platExpanded);
        };
      }
      row.querySelectorAll(\'.plat-chip\').forEach(function(b){
        b.addEventListener(\'click\',function(){renderHotPlat(b.getAttribute(\'data-p\'),true);});
      });
      renderHotPlat(_hotData[0].platform);
      _applyPlatCollapse();
    }).catch(function(){
      _hotLoading=false;
      list.innerHTML=\'<div class="hp-empty">加载失败，请稍后重试</div>\';
    });
  }
  function _visiblePlats(){
    return _hotData.filter(function(s){return _hotCatKey===\'all\'||_platCat(s.platform)===_hotCatKey;});
  }
  /* 分类筛选：切换平台 chips 可见性（WS-A5 交互反馈） */
  function _filterHotCat(cat){
    _hotCatKey=cat;
    var segEl=document.getElementById(\'hpCats\');
    if(segEl) segEl.querySelectorAll(\'.hp-seg-btn\').forEach(function(b){
      var on=b.getAttribute(\'data-cat\')===cat;
      b.classList.toggle(\'on\',on);
      b.setAttribute(\'aria-selected\',on);
    });
    var chips=document.getElementById(\'hpTabs\');
    var firstVisible=null;
    chips.querySelectorAll(\'.plat-chip\').forEach(function(b){
      var show=(cat===\'all\'||b.getAttribute(\'data-cat\')===cat);
      b.style.display=show?\'\':\'none\';
      if(show&&!firstVisible) firstVisible=b;
    });
    /* 更新展开按钮计数 */
    var moreBtn=document.getElementById(\'btnPlatMore\');
    if(moreBtn&&!_platExpanded){
      var vis=_visiblePlats();
      moreBtn.querySelector(\'span\').textContent=\'全部 \'+vis.length+\' 个\';
    }
    /* 如果当前选中的平台被隐藏，自动切到第一个可见平台（带 fade 反馈） */
    var curChip=chips.querySelector(\'.plat-chip.on\');
    if(curChip&&curChip.style.display===\'none\'&&firstVisible){
      renderHotPlat(firstVisible.getAttribute(\'data-p\'),true);
    }
    /* 分类切换后平台行滚回起点 */
    chips.scrollLeft=0;
  }
  function renderHotPlat(p,swap){
    var chips=document.getElementById(\'hpTabs\');
    chips.querySelectorAll(\'.plat-chip\').forEach(function(b){
      var on=b.getAttribute(\'data-p\')===p;
      b.classList.toggle(\'on\',on);
      b.style.background=on?(_hotPlatformDeep[p]||\'#555\'):\'\';
    });
    var list=document.getElementById(\'hotList\');
    var src=null;
    for(var i=0;i<_hotData.length;i++){ if(_hotData[i].platform===p){src=_hotData[i];break;} }
    if(!src){list.innerHTML=\'<div class="hp-empty">暂无热榜数据</div>\';return;}
    var h=\'\';
    var platTrends=(ANALYSIS_DATA&&ANALYSIS_DATA.hot_trends&&ANALYSIS_DATA.hot_trends[p])||{};
    for(var j=0;j<src.items.length;j++){
      var it=src.items[j],rk=it.rank||(j+1),cls=rk<=3?\' top3\':\'\';
      var hotTxt=it.hot?(\'\'+it.hot).replace(/^(\d+)(\d{4,})$/,function(m,a,b){return a+\'\u4e07\';}):\'\';
      h+=\'<a class="hp-item" href="\'+_escH(it.url||\'#\')+\'" target="_blank" rel="noopener">\';
      h+=\'<span class="hp-rank\'+cls+\'">\'+rk+\'</span>\';
      h+=\'<span class="hp-title">\'+_escH(it.title||\'\')+\'</span>\';
      if(hotTxt) h+=\'<span class="hp-hot">\'+hotTxt+\'</span>\';
      var td=platTrends[it.title||\'\'];
      if(td&&td.trend===\'rising\') h+=\'<span class="hp-trend t-up">\u2191\'+td.rise+\'</span>\';
      else if(td&&td.trend===\'falling\') h+=\'<span class="hp-trend t-down">\u2193\'+Math.abs(td.rise)+\'</span>\';
      else if(td&&td.trend===\'new\') h+=\'<span class="hp-trend t-new">\u65b0</span>\';
      h+=\'</a>\';
    }
    list.innerHTML=h||\'<div class="hp-empty">暂无热榜数据</div>\';
    /* 分类切换 fade 动画（WS-A5） */
    if(swap&&!matchMedia(\'(prefers-reduced-motion:reduce)\').matches){
      list.classList.remove(\'hp-swap\');void list.offsetWidth;list.classList.add(\'hp-swap\');
    }
  }
  window.addEventListener('resize',function(){ _applyPlatCollapse(); });

  /* ── Insight Panel（每日洞察 · WS-B 容器化） ── */
  var _insightScrollY=0;
  function toggleInsight(){
    var panel=document.getElementById('insightPanel');
    var btn=document.getElementById('btnInsight');
    if(!panel)return;
    var opening=!panel.classList.contains('open');
    if(opening){ renderInsight(); }
    panel.classList.toggle('open',opening);
    if(btn){
      btn.classList.toggle('on',opening);
      btn.setAttribute('aria-expanded',opening);
    }
    /* 遮罩 + 滚动锁 */
    var scrim=document.getElementById('scrim');
    if(scrim) scrim.classList.toggle('on',opening);
    if(opening){
      _insightScrollY=window.scrollY;
      document.body.style.overflow='hidden';
      panel.querySelector('.ip-close').focus();
    } else {
      document.body.style.overflow='';
      window.scrollTo(0,_insightScrollY);
      if(btn) btn.focus();
    }
  }
  window.toggleInsight=toggleInsight;
  function closeInsight(){
    var p=document.getElementById('insightPanel');
    if(p&&p.classList.contains('open')) toggleInsight();
  }

  function renderInsight(){
    var el=document.getElementById('insightBody');
    if(!el||!ANALYSIS_DATA)return;
    var d=ANALYSIS_DATA, h='';
    /* D2: SVG 图标替代 emoji，与站内线性图标体系一致 */
    var _ico={chart:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 5-6"/></svg>',bell:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',signal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M2 20h.01"/><path d="M7 20v-4"/><path d="M12 20v-8"/><path d="M17 20V8"/><path d="M22 4v16"/></svg>',clock:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',search:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>',bolt:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',crystal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',tag:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>',flame:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="width:13px;height:13px"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>'};
    /* WS-C 前端去重兆底 */
    var _dedupSeen=new Set(), _dedupCount=0;
    function _dedup(text){ var t=String(text||'').trim(); if(!t) return ''; var key=t.slice(0,120); if(_dedupSeen.has(key)){_dedupCount++;return '';} _dedupSeen.add(key);return t; }
    /* signals 规范化 */
    function _normSignals(raw){
      return (Array.isArray(raw)?raw:[]).map(function(s){
        if(s&&typeof s==='object') return {signal:s.signal||s.label||s.name||s.text||'',conf:s.confidence};
        var m=String(s).match(/['"]?signal['"]?\s*:\s*['"]([^'"]+)['"]/);
        var c=String(s).match(/['"]?confidence['"]?\s*:\s*([\d.]+)/);
        return {signal:m?m[1]:String(s),conf:c?parseFloat(c[1]):null};
      }).filter(function(x){return x.signal;});
    }
    // 统计摘要
    if(d.stats||d.generated_at){
      h+='<div class="ib-stats">';
      if(d.stats){
        h+='<span>'+_ico.chart+' \u6587\u7ae0: '+d.stats.total_articles+'</span>';
        h+='<span>'+_ico.bell+' \u8fd124h: '+d.stats.recent_count+'</span>';
        h+='<span>'+_ico.signal+' \u4fe1\u6e90: '+d.stats.source_count+'</span>';
      }
      if(d.generated_at) h+='<span>'+_ico.clock+' '+(d.generated_at||'').slice(0,16).replace('T',' ')+'</span>';
      /* D3 轻量场：标注深度分析时间（generated_at 即为上次重分析时间） */
      if(d.stale) h+='<span style="color:var(--faint,#94a3b8)">\U0001f50d \u6df1\u5ea6\u5206\u6790: '+(d.generated_at||'').slice(11,16)+'</span>';
      h+='</div>';
      h+='<div style="font-size:10.5px;color:var(--faint);padding:0 0 6px;">\u6570\u636e\u8303\u56f4: \u8fd1 72 \u5c0f\u65f6\u6eda\u52a8\u7a97\u53e3</div>';
    }
    // AI 摘要（支持结构化 dict 和旧格式 string）
    if(d.summary){
      if(typeof d.summary==='object'&&!Array.isArray(d.summary)){
        h+='<div class="ib-section"><div class="ib-label">AI 情报分析</div>';
        h+='<div class="ib-sub-grid">';
        var sections=[
          {k:'core_trends',cls:'sub-core',l:'\u6838\u5fc3\u6001\u52bf'},
          {k:'signals',cls:'sub-signal',l:'\u5f02\u52a8\u4fe1\u53f7'},
          {k:'rss_insights',cls:'sub-rss',l:'RSS\u6d1e\u5bdf'},
          {k:'outlook',cls:'sub-outlook',l:'\u7814\u5224\u5efa\u8bae'}
        ];
        sections.forEach(function(s){
          if(d.summary[s.k]){
            h+='<div class="ib-sub-section '+s.cls+'">';
            h+='<div class="ib-sub-label">'+s.l+'</div>';
            h+='<div class="ib-sub-text">'+esc(d.summary[s.k])+'</div>';
            h+='</div>';
          }
        });
        h+='</div></div>';
      } else if(typeof d.summary==='string'){
        h+='<div class="ib-section"><div class="ib-label">AI \u6458\u8981</div>';
        h+='<div class="ib-summary">'+esc(d.summary)+'</div></div>';
      }
    }
    // \u6df1\u5ea6\u6d1e\u5bdf (deep_insights)
    if(d.deep_insights){
      var di=d.deep_insights;
      if(di.narrative){
        h+='<div class="ib-section"><div class="ib-label">'+_ico.search+' \u53d9\u4e8b\u8109\u7edc</div>';
        h+='<div class="ib-narrative">'+esc(di.narrative)+'</div></div>';
      }
      if(di.causal_chains&&di.causal_chains.length){
        h+='<div class="ib-section"><div class="ib-label">'+_ico.bolt+' \u56e0\u679c\u94fe</div><div class="ib-chain-grid">';
        di.causal_chains.forEach(function(c){
          h+='<div class="ib-chain-card">';
          if(typeof c==='string'){
            h+='<div class="ib-chain-steps">'+esc(c)+'</div>';
          } else {
            h+='<div class="ib-chain-title">'+esc(c.title||c.name||'\u56e0\u679c\u5173\u7cfb')+'</div>';
            h+='<div class="ib-chain-steps">'+esc(c.chain||c.description||'')+'</div>';
          }
          h+='</div>';
        });
        h+='</div></div>';
      }
      if(di.signals&&di.signals.length){
        h+='<div class="ib-section"><div class="ib-label">'+_ico.signal+' \u4fe1\u53f7\u770b\u677f</div><div class="ib-signal-grid">';
        di.signals.forEach(function(s){
          h+='<div class="ib-signal-card">'+esc(s.label||s.name||s.text||s.signal||'')+'</div>';
        });
        h+='</div></div>';
      }
      if(di.outlook){
        h+='<div class="ib-section"><div class="ib-label">'+_ico.crystal+' \u8d8b\u52bf\u5c55\u671b</div>';
        h+='<div class="ib-outlook">'+esc(di.outlook)+'</div></div>';
      }
    }
    // \u8bdd\u9898\u805a\u7c7b (topic_clusters)
    if(d.topic_clusters&&d.topic_clusters.length){
      var maxCnt=0;
      d.topic_clusters.forEach(function(c){if(c.count>maxCnt)maxCnt=c.count;});
      h+='<div class="ib-section"><div class="ib-label">'+_ico.chart+' \u8bdd\u9898\u805a\u7c7b</div><div class="ib-cluster-list">';
      d.topic_clusters.slice(0,12).forEach(function(c){
        var pct=maxCnt>0?Math.round(c.count/maxCnt*100):0;
        h+='<div class="ib-cluster-card" data-kw="'+esc(c.label||'')+'" onclick="insightSearch(this.dataset.kw)">';
        h+='<div class="ib-cluster-head"><span class="ib-cluster-label">'+esc(c.label||'\u672a\u547d\u540d')+'</span>';
        h+='<span class="ib-cluster-count">'+c.count+' \u7bc7</span></div>';
        h+='<div class="ib-cluster-bar"><div class="ib-cluster-bar-fill" style="width:'+pct+'%"></div></div>';
        h+='</div>';
      });
      h+='</div></div>';
    }
    // 热门关键词（带生命周期标记）
    if(d.keywords&&d.keywords.global&&d.keywords.global.length){
      var risingSet={};
      if(d.rising) d.rising.forEach(function(r){risingSet[r.word]=r.rise;});
      var kwTraj=(d.rss_trajectories&&d.rss_trajectories.keywords)||{};
      h+='<div class="ib-section"><div class="ib-label">\u70ed\u95e8\u5173\u952e\u8bcd</div><div class="ib-kw-list">';
      d.keywords.global.slice(0,30).forEach(function(kw,idx){
        var w=kw[0],score=kw[1],isRising=risingSet[w];
        var traj=kwTraj[w]||{};
        var lc=traj.lifecycle||'';
        var tip='#'+(idx+1)+' \u5f97\u5206:'+score.toFixed(2);
        if(isRising) tip+=' (\u4e0a\u5347'+isRising+'\u4f4d)';
        if(lc) tip+=' | \u8f68\u8ff9:'+lc;
        if(traj.first_seen) tip+=' | \u9996\u6b21:'+traj.first_seen.slice(5,16).replace('T',' ');
        if(traj.duration) tip+=' | \u6301\u7eed:'+traj.duration+'\u5468\u671f';
        var cls='ib-kw';
        if(lc==='emergent') cls+=' kw-emergent kw-tag';
        else if(lc==='rising') cls+=' kw-rising kw-tag';
        else if(lc==='peaking') cls+=' kw-peaking kw-tag';
        else if(lc==='declining') cls+=' kw-declining kw-tag';
        else if(lc==='gone') cls+=' kw-gone kw-tag';
        else if(isRising) cls+=' rising';
        h+='<span class="'+cls+'" data-kw="'+esc(w)+'" title="'+tip+'" onclick="insightSearch(this.dataset.kw)">'+esc(w)+'</span>';
      });
      h+='</div></div>';
    }
    // 升温关键词
    if(d.rising&&d.rising.length){
      h+='<div class="ib-section"><div class="ib-label">\u2b06\ufe0f 升温词</div><div class="ib-kw-list">';
      d.rising.forEach(function(r){
        h+='<span class="ib-kw rising" data-kw="'+esc(r.word)+'" onclick="insightSearch(this.dataset.kw)" title="排名上升 '+r.rise+' 位">'+esc(r.word)+'</span>';
      });
      h+='</div></div>';
    }
    // 今日话题
    if(d.topics&&d.topics.length){
      h+='<div class="ib-section"><div class="ib-label">今日话题</div><div class="ib-topic-list">';
      d.topics.slice(0,12).forEach(function(t,i){
        var rk=i+1, cls=rk<=3?' top3':'';
        var tl=(t.label||(t.labels&&t.labels.length?t.labels.join(' '):''));
        var src=t.sources?(Array.isArray(t.sources)?t.sources.length:t.sources):0;
        h+='<div class="ib-topic" data-kw="'+esc(tl)+'" onclick="insightSearch(this.dataset.kw)">';
        h+='<span class="ib-topic-rank'+cls+'">'+rk+'</span>';
        h+='<span class="ib-topic-title">'+esc(tl)+'</span>';
        h+='<span class="ib-topic-meta">'+t.count+' 篇 · '+src+' 源</span>';
        h+='</div>';
      });
      h+='</div></div>';
    }
    // 话题趋势（跨周期轨迹）— 只展示有轨迹信号的话题，与「今日话题」差异化
    var topicTraj=(d.rss_trajectories&&d.rss_trajectories.topics)||{};
    if(d.topics&&d.topics.length&&Object.keys(topicTraj).length>0){
      var trendTopics=[];
      d.topics.forEach(function(t){
        var tl=(t.label||(t.labels&&t.labels.length?t.labels.join(' '):''));
        var traj=topicTraj[tl];
        if(traj&&(traj.lifecycle||traj.latest_count)) trendTopics.push({topic:t,label:tl,traj:traj});
      });
      trendTopics.sort(function(a,b){return (b.traj.latest_count||0)-(a.traj.latest_count||0);});
      if(trendTopics.length>0){
        h+='<div class="ib-section ib-trend"><div class="ib-trend-title">\U0001f4c8 \u8bdd\u9898\u8d8b\u52bf</div><div class="ib-trend-list">';
        trendTopics.slice(0,8).forEach(function(item){
          var t=item.topic,tl=item.label,traj=item.traj;
          var lc=traj.lifecycle||'emerging';
          var cnt=traj.latest_count||t.count||0;
          var arrow='', arrowCls='';
          if(lc==='hot'){arrow='\U0001f525';arrowCls='t-hot';}
          else if(lc==='emerging'){arrow='\u2b06';arrowCls='t-up';}
          else if(lc==='cooling'){arrow='\u2b07';arrowCls='t-down';}
          else if(lc==='cold'){arrow='\u2744';arrowCls='t-down';}
          var lcLabel={emerging:'\u65b0\u5174',hot:'\u706b\u7206',cooling:'\u964d\u6e29',cold:'\u51b7\u5374'}[lc]||lc;
          h+='<div class="ib-trend-item" data-kw="'+esc(tl)+'" onclick="insightSearch(this.dataset.kw)">';
          h+='<span class="ib-trend-label">'+esc(tl)+'</span>';
          h+='<span class="ib-trend-count">'+cnt+'\u7bc7</span>';
          if(arrow) h+='<span class="ib-trend-arrow '+arrowCls+'">'+arrow+'</span>';
          h+='<span class="ib-trend-lc lc-'+lc+'">'+lcLabel+'</span>';
          h+='</div>';
        });
        h+='</div></div>';
      }
    }
    // 跨平台共振
    if(d.cross_platform&&d.cross_platform.length){
      h+='<div class="ib-section ib-cross"><div class="ib-label">\u8de8\u5e73\u53f0\u5171\u632f</div><div class="ib-cross-list">';
      d.cross_platform.slice(0,8).forEach(function(m){
        h+='<div class="ib-cross-item">';
        if(m.platforms&&m.platforms.length){
          h+='<span class="ib-cross-label">'+esc(m.label)+'</span>';
          h+='<span class="ib-cross-plats">';
          m.platforms.forEach(function(p){
            h+='<span class="ib-cross-plat">'+esc(p.name)+'</span>';
          });
          h+='</span>';
        } else {
          h+='<span class="ib-cross-label">'+esc(m.title_a||m.label||'')+'</span>';
          h+='<span class="ib-cross-plats">';
          h+='<span class="ib-cross-plat">'+esc(m.platform_a||'')+'</span>';
          h+='<span class="ib-cross-plat">'+esc(m.platform_b||'')+'</span>';
          h+='</span>';
        }
        h+='</div>';
      });
      h+='</div></div>';
    }
    // 跨分类热点
    if(d.cross_category&&d.cross_category.length){
      h+='<div class="ib-section ib-ccat"><div class="ib-label">\U0001f310 \u8de8\u5206\u7c7b\u70ed\u70b9</div><div class="ib-ccat-list">';
      d.cross_category.slice(0,12).forEach(function(cc){
        h+='<div class="ib-ccat-item" data-kw="'+esc(cc.keyword)+'" onclick="insightSearch(this.dataset.kw)">';
        h+='<span class="ib-ccat-kw">'+esc(cc.keyword)+'</span>';
        h+='<span class="ib-ccat-cats">';
        cc.categories.forEach(function(c){
          h+='<span class="ib-ccat-cat">'+esc(c)+'</span>';
        });
        h+='</span></div>';
      });
      h+='</div></div>';
    }
    if(!h) h='<div class="ib-no-data">\u6682\u65e0\u5206\u6790\u6570\u636e</div>';
    /* WS-C 去重提示 */
    if(_dedupCount>0) h+='<div class="ip-dedup"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 6L9 17l-5-5"/></svg>\u5df2\u81ea\u52a8\u8df3\u8fc7 '+_dedupCount+' \u6bb5\u4e0e\u4e0a\u6587\u91cd\u590d\u7684\u5185\u5bb9</div>';
    el.innerHTML=h;
    /* 锚点导航（WS-B2） */
    var navEl=document.getElementById('ipNav');
    if(navEl){
      var secs=el.querySelectorAll('.ib-section, .ip-sec');
      var navHtml='';
      secs.forEach(function(s,i){
        var label=(s.querySelector('.ib-label, .ip-label')||{}).textContent||'';
        if(!label) return;
        if(!s.id) s.id='ip-sec-'+i;
        navHtml+='<button data-t="'+s.id+'" class="'+(i===0?'on':'')+'">'+label+'</button>';
      });
      navEl.innerHTML=navHtml;
      navEl.querySelectorAll('button').forEach(function(b){
        b.onclick=function(){
          navEl.querySelectorAll('button').forEach(function(x){x.classList.remove('on');});
          b.classList.add('on');
          var target=document.getElementById(b.dataset.t);
          if(target) target.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth',block:'start'});
        };
      });
    }
  }

  function insightSearch(keyword){
    var bg=null;
    // 长标签（话题/趋势）→ 提取字符二元组做模糊匹配
    var clean=keyword.replace(/^\[RSS\/\w+\]\s*/,'');
    if(clean.length>15){
      var low=clean.toLowerCase(),arr=[];
      for(var i=0;i<low.length-1;i++){
        var c=low[i],n=low[i+1];
        if(c.trim()&&n.trim()&&c!==n) arr.push(c+n);
      }
      var seen={},uniq=[];
      arr.forEach(function(b){if(!seen[b]){seen[b]=1;uniq.push(b);}});
      if(uniq.length>=2) bg=uniq;
    }
    var si=document.getElementById('globalSearch');
    if(si){si.value=keyword;si.dispatchEvent(new Event('input'));}
    // input handler 会重置 _topicBigrams=null，所以在 dispatch 之后赋值
    _topicBigrams=bg;
    window.scrollTo({top:0,behavior:'smooth'});
    setTimeout(function(){
      var first=document.querySelector('.wall .card');
      if(first) first.scrollIntoView({behavior:'smooth',block:'center'});
    },150);
  }
  window.insightSearch=insightSearch;

})();
</script>
"""


# ──────────────────────────── 数据分块 ────────────────────────────

CHUNK0_SIZE = 360  # 首屏块文章数（复刻前端 ART 排序后取前 N 篇）

def _chrono_key(item):
    """切块排序键：按解析后的**绝对时间**降序（与前端 _dateCmpDesc 同口径）。

    不能直接比 pub_date 字符串：数据里 "+08:00" 与 "+00:00/Z" 混用，字符串比较是
    时区盲的（实测 "…T09:00:00+08:00" 会被判为比 "…T02:00:00+00:00" 更新，导致首屏
    选片选错）。无日期/非法日期返回 (0, 0) → 一律沉底进 chunk1。
    """
    s = item.get("pub_date") or ""
    try:
        d = datetime.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return (0, 0.0)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))  # 裸值按北京时间
    try:
        return (1, d.timestamp())
    except (OverflowError, OSError, ValueError):
        return (0, 0.0)


def _split_data_chunks(sources, chunk0_size=CHUNK0_SIZE):
    """把全量文章切成两段：首屏 chunk0 按严格时间排序（不交织），后台段由调用方再分块；
    后台 chunk1 包含剩余文章；前端合并 chunk1 后再应用 tier 交织。
    两块均保持 sources 富字段结构，前端可原样消费。"""
    flat = []
    for s in sources:
        for it in s.get("items", []):
            flat.append((s, it))
    flat.sort(key=lambda x: _chrono_key(x[1]), reverse=True)
    seen = set()
    c0_items = {}
    for s, it in flat[:chunk0_size]:
        seen.add(id(it))
        c0_items.setdefault(s.get("key"), []).append(it)
    chunk0, chunk1 = [], []
    for s in sources:
        k = s.get("key")
        # 标记 BestBlogs 源（URL 含 bestblogs.dev）
        _is_bb = 'bestblogs.dev' in (s.get('url') or '')
        if k in c0_items:
            c0 = dict(s); c0["items"] = _strip_derived_items(c0_items[k])
            if _is_bb: c0["bb"] = True
            chunk0.append(c0)
            rest = [it for it in s.get("items", []) if id(it) not in seen]
            if rest:
                c1 = dict(s); c1["items"] = _strip_derived_items(rest)
                if _is_bb: c1["bb"] = True
                chunk1.append(c1)
        elif s.get("items"):
            # 不改动入参对象：剥派生字段要生成副本，直接写 s 会污染内存里的历史条目
            s = dict(s)
            s["items"] = _strip_derived_items(s.get("items", []))
            if _is_bb: s["bb"] = True
            chunk1.append(s)
    return chunk0, chunk1

def _empty_stale_chunk(path, num, dump):
    """旧块**清空而不是删除**。CI 的提交步骤是 `git add ... rss-data-*.js`（磁盘 glob 展开）：
    文件一旦被 os.remove 掉就匹配不到，删除永远进不了提交 ⇒ 旧块留在仓库里继续被分发，
    页面照样把它合并进来（2026-09-21 实测：闸门报"出厂 9,045 条 / >168h 0 条"，
    而残留的 rss-data-2.js 里还挂着 11,486 条旧数据，其中 5,060 条 >168h）。
    留着空文件才是可提交的变更，内容清空后合并进来也不带任何旧条目。
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write("/* StarHub data chunk %d emptied (kept so git can commit the change) */" % num + chr(10))
        f.write("(window.__CHUNKS=window.__CHUNKS||[])[" + str(num) + "]=" + dump({"sources": []}) + ";" + chr(10))


def _retire_stale_chunks(out_dir, n_chunks, dump):
    """把上一场多出来的旧块**清空**（不是删除）。CI 的提交步骤是 `git add ... rss-data-*.js`
    按磁盘 glob 展开：文件被 os.remove 掉就匹配不到，删除永远进不了提交 ⇒ 旧块留在仓库
    继续被 Pages/Vercel 分发，页面照样把它合并进来（真实事故见
    tests/rss_history/test_stale_chunk_guard.py 的 docstring）。
    """
    import glob as _glob
    emptied = 0
    for old_file in _glob.glob(os.path.join(out_dir, "rss-data-*.js")):
        basename = os.path.basename(old_file)
        try:
            num = int(basename.replace("rss-data-", "").replace(".js", ""))
        except ValueError:
            continue
        if num > n_chunks:
            _empty_stale_chunk(old_file, num, dump)
            emptied += 1
    if emptied:
        print("[数据分块] 清空 %d 个多余旧块（删除不会被 git add 的 glob 匹配到）" % emptied)


def write_data_chunks(sources, chunk0_size=CHUNK0_SIZE):
    """写出 rss-data-0.js + rss-data-{1..N}.js（与 OUT 同目录），页面经 script 标签加载。
    chunk1+ 按 MAX_CHUNK_BYTES 分片；单块过大 = 慢网络取不到 = 页面只剩首屏。"""
    out_dir = os.path.dirname(os.path.abspath(OUT)) or "."
    chunk0, chunk1 = _split_data_chunks(sources, chunk0_size)

    def _dump(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    # chunk0 最后写：它要在 payload 里声明总块数，而总数只有等 chunk1 的分桶数定了才知道。
    # 声明写在 JSON 里而不是追加一条 `window.__CHUNKS[0]._total=..` 语句 ——
    # tests/rss_history/test_chunk_budget.py 的 _payload() 按"整个文件是一个 JSON"解析，追加会打爆它。
    n0 = sum(len(s.get("items", [])) for s in chunk0)

    def _write_chunk0(total):
        with open(os.path.join(out_dir, "rss-data-0.js"), "w", encoding="utf-8") as f:
            f.write("/* StarHub data chunk 0 (first screen) - auto generated, do not edit */\n")
            f.write("(window.__CHUNKS=window.__CHUNKS||[])[0]="
                    + _dump({"sources": chunk0, "_total": total}) + ";\n")

    # chunk1+: 按每块上限拆分。
    # 2026-09-30 线上实证：这里曾写 80MB，而后台数据实测 50.6MB ⇒ n_chunks 恒为 1，
    # "分块"退化成一块 60MB 巨石；慢网络取不到时 script 的 onload 照样触发但
    # __CHUNKS[1] 从未赋值，页面只剩 chunk0 首屏的 61 个源（控制台明写"文件截断"）。
    # 6MB/块 ≈ 9 块，单块失败只丢尾部一块而不是整块后台数据；上限 24 块防文件数失控。
    MAX_CHUNK_BYTES = 6 * 1024 * 1024
    MAX_CHUNKS = 24
    n1_total = sum(len(s.get("items", [])) for s in chunk1)
    if not chunk1:
        # 无剩余数据，写空 chunk1
        with open(os.path.join(out_dir, "rss-data-1.js"), "w", encoding="utf-8") as f:
            f.write("/* StarHub data chunk 1 - auto generated, do not edit */\n")
            f.write("(window.__CHUNKS=window.__CHUNKS||[])[1]={sources:[]};\n")
        _retire_stale_chunks(out_dir, 1, _dump)
        _write_chunk0(2)      # 文件仍是两个（0 和空的 1），声明必须跟实际文件数一致
        print("[数据分块] chunk0 %d 篇 / chunk1 0 篇（共 %d）" % (n0, n0))
        return

    # 估算每源 JSON 体积，按体积均匀分桶
    src_sizes = []
    for s in chunk1:
        try:
            sz = len(json.dumps(s, ensure_ascii=False).encode("utf-8"))
        except Exception:
            sz = 1024
        src_sizes.append(sz)
    total_bytes = sum(src_sizes)
    n_chunks = max(1, (total_bytes + MAX_CHUNK_BYTES - 1) // MAX_CHUNK_BYTES)
    n_chunks = min(n_chunks, MAX_CHUNKS)
    _write_chunk0(1 + n_chunks)
    target_per_chunk = total_bytes / n_chunks

    buckets = [[] for _ in range(n_chunks)]
    bucket_sizes = [0] * n_chunks
    for i, (s, sz) in enumerate(zip(chunk1, src_sizes)):
        # 放入当前最小的桶
        min_idx = bucket_sizes.index(min(bucket_sizes))
        buckets[min_idx].append(s)
        bucket_sizes[min_idx] += sz

    for idx, bucket in enumerate(buckets):
        chunk_idx = idx + 1
        fname = "rss-data-%d.js" % chunk_idx
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write("/* StarHub data chunk %d (background merge) - auto generated, do not edit */\n" % chunk_idx)
            f.write("(window.__CHUNKS=window.__CHUNKS||[])[%d]=" % chunk_idx + _dump({"sources": bucket}) + ";\n")
        n_items = sum(len(s.get("items", [])) for s in bucket)
        size_mb = bucket_sizes[idx] / 1024 / 1024
        print("[数据分块] %s: %d 源 %d 篇 %.1f MB" % (fname, len(bucket), n_items, size_mb))

    _retire_stale_chunks(out_dir, n_chunks, _dump)

    print("[数据分块] chunk0 %d 篇 / %d 个后台块 %d 篇（共 %d）" % (n0, n_chunks, n1_total, n0 + n1_total))


def build_html(sources_with_items, build_time, total_items, build_ts_ms=0, analysis_data=None,
               diverse_window_minutes=120, diverse_enabled=True):
    """生成完整 HTML 页面 — 卡片墙 + 抽屉阅读器。"""
    # 构建洞察面板 HTML（仅当分析数据存在时显示按钮）
    insight_btn = ''
    insight_bar = ''
    if analysis_data:
        insight_btn = (
            '<button class="insight-btn" id="btnInsight" onclick="toggleInsight()" title="每日洞察">'
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
            '<path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>'
            '</svg> 洞察</button>\n'
        )
        insight_bar = (
            '<aside class="insight-panel" id="insightPanel" role="dialog" aria-modal="true" aria-label="\u6bcf\u65e5\u6d1e\u5bdf">\n'
            '<div class="ip-head">\n'
            '<h2><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
            '<path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'
            ' \u6bcf\u65e5\u6d1e\u5bdf</h2>\n'
            '<span class="ip-gen" id="ipGen"></span>\n'
            '<button class="ip-close" onclick="toggleInsight()" aria-label="\u5173\u95ed\u6d1e\u5bdf"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>\n'
            '</div>\n'
            '<nav class="ip-nav" id="ipNav" aria-label="\u6d1e\u5bdf\u7ae0\u8282\u5bfc\u822a"></nav>\n'
            '<div class="ip-body" id="insightBody"></div>\n'
            '</aside>\n'
        )
    # 分析数据 JSON 注入到 JS
    analysis_json = ''
    if analysis_data:
        analysis_json = json.dumps(analysis_data, ensure_ascii=False, separators=(',', ':'))
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@700;900&family=Noto+Sans+SC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">\n'
        '<title>RSS 聚合阅读器 · StarHub</title>\n'
        '<style>' + _build_css() + '</style>\n'
        + '<link rel="preload" href="rss-data-0.js?v=' + str(int(build_ts_ms)) + '" as="script">\n'
        # 这里原本还有一行 preload hot_snapshot.json：消费方是 `fetch(…,{cache:'no-cache'})`，
        # no-cache 明确绕过预载那份（且 preload 带 crossorigin、fetch 不带 credentials，键也对不上）
        # ⇒ 每次加载白下 63KB，控制台每次都报 "preloaded but not used"。删预载，不删 no-cache（那是数据新鲜度）。
        '</head>\n<body>\n'
        + _build_header() +
        '<div class="toolbar">\n'
        '<h1>时间线</h1>\n'
        '<button class="src-btn" onclick="toggleSrcPanel()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h13M4 18h9"/></svg> 信源 <span class="cnt" id="srcCnt"></span></button>\n'
        '<button class="refresh-btn" id="refreshBtn" onclick="refreshRss()" title="后台检查更新，不刷新页面"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/></svg></button>\n'
        '<div class="chips" id="chips"></div>\n'
        '<button class="unread-toggle" id="unreadToggle" onclick="toggleUnread()" title="\u4ec5\u663e\u793a\u672a\u8bfb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3" fill="currentColor"/></svg> \u672a\u8bfb</button>\n'
                '<button class="unread-toggle" id="markAllReadBtn" onclick="markAllRead()" title="\u5168\u90e8\u6807\u8bb0\u5df2\u8bfb" style="display:none"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg> \u5168\u90e8\u5df2\u8bfb</button>\n'
        '<button class="ai-feed-btn" id="btnAiFeed" onclick="toggleAiFeed()" title="AI \u52a8\u6001\u6d41"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg> AI \u52a8\u6001</button>\n'
        '<button class="hot-btn" id="btnHot" onclick="toggleHotPanel()" title="\u5168\u7f51\u70ed\u699c"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2c1 3-2 5-2 8a4 4 0 0 0 8 0c0-3-2-5-2-8"/><path d="M8.5 14.5A5 5 0 0 0 12 22a5 5 0 0 0 3.5-7.5"/></svg> \u70ed\u699c</button>\n'
        '<span class="tb-divider" aria-hidden="true"></span>\n'
        + insight_btn +
        
        '<span id="fpillWrap"></span>\n'
        '<span class="tool-meta" id="toolMeta"></span>\n'
        '</div>\n'
        '<div class="search-row">\n'
        '<span class="global-search" id="globalSearchWrap"><input id="globalSearch" placeholder="\u641c\u7d22\u6587\u7ae0\u6807\u9898\u3001\u6458\u8981\u6216\u4fe1\u606f\u6e90\u540d\u79f0\u2026" autocomplete="off"><span class="sx" id="globalSearchClear">\u2715</span></span>\n'
                '<span class="src-match-label" id="srcMatchLabel" style="display:none" aria-live="polite"></span>\n'
        '<select class="sort-select" id="sortSelect" title="\u6392\u5e8f\u65b9\u5f0f"><option value="newest">\u6700\u65b0\u53d1\u5e03</option><option value="oldest">\u6700\u65e9\u53d1\u5e03</option><option value="active">\u6700\u8fd1\u6d3b\u8dc3</option><option value="quality">\u4fe1\u6e90\u8d28\u91cf</option>'
        + ('<option value="diverse">\u591a\u6837\u63a8\u8350</option>' if diverse_enabled else '')
        + '</select>\n'
        '</div>\n'
        + insight_bar +
        '<div class="build-bar">\u81ea\u52a8\u751f\u6210\u4e8e ' + _esc(build_time) + '\uff08\u5317\u4eac\u65f6\u95f4\uff09\u00b7 \u5171 ' + str(total_items) + ' \u7bc7 \u00b7 <span id="buildRel"></span><span id="liveStatus"></span></div>\n'
        '<div class="wall-wrap">\n'
        '<aside class="ai-feed-panel" id="aiFeedPanel">\n'
        '<div class="af-head">\n'
        '<div class="af-tabs" role="tablist">\n'
        '<button class="af-tab on" id="afTabFeed" role="tab" aria-selected="true" onclick="switchAfTab(\'feed\')">AI \u5feb\u8baf</button>\n'
        '<button class="af-tab" id="afTabHot" role="tab" aria-selected="false" onclick="switchAfTab(\'hot\')">\u70ed\u699c</button>\n'
        '</div>\n'
        '<button class="af-refresh" id="afRefreshBtn" onclick="refreshAiFeed()" title="\u5237\u65b0"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/></svg></button>\n'
        '<button class="af-close" onclick="toggleAiFeed()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>\n'
        '<div class="hp-wrap" id="hpWrap">\n'
        '<div class="dim-row"><span class="dim-label">分类</span><div class="dim-body"><div class="hp-seg" id="hpCats" role="tablist" aria-label="热榜分类筛选"></div></div></div>\n'
        '<hr class="dim-sep">\n'
        '<div class="dim-row" style="padding-top:0"><span class="dim-label">平台</span><div class="dim-body plat-wrap"><div class="plat-row collapsed" id="hpTabs"></div></div></div>\n'
        '<div class="hp-body" id="hotList"><div class="hp-empty">点击加载热榜</div></div></div>\n'
        '<div class="af-sub" id="afUpdated"></div>\n'
        '<div class="af-filter" id="afFilter" style="display:none"></div>\n'
        '<div class="af-body" id="afList"><div class="af-empty">\u70b9\u51fb\u67e5\u770b AI \u52a8\u6001</div></div>\n'
        '<button class="af-more" id="afLoadMore" style="display:none">\u52a0\u8f7d\u66f4\u591a</button></aside>\n'
        '<div class="wall" id="wall" role="feed" aria-label="\u6587\u7ae0\u5217\u8868"><div class="boot-loading" id="bootLoading"><span class="boot-spin"></span>\u6b63\u5728\u52a0\u8f7d\u5185\u5bb9\u2026</div></div>\n'
        '</div>\n'
        '<div class="scrim" aria-hidden="true" onclick="closeOverlays()"></div>\n'
        '<aside class="src-panel" id="srcPanel" role="dialog" aria-modal="true" aria-label="\u4fe1\u6e90\u9762\u677f">\n'
        '<div class="sp-head"><div class="row"><h2>信源</h2>\n'
        '<button class="sp-export" onclick="exportOPML()" title="\u5bfc\u51fa OPML"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button>\n'
        '<button class="sp-close" onclick="toggleSrcPanel()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>\n'
        '</div><label class="sp-search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>\n'
        '<input id="spSearch" placeholder="搜索信源…" autocomplete="off"></label></div>\n'
        '<div class="sp-list" id="spList"></div></aside>\n'
        '<aside class="reader2" id="reader2" role="dialog" aria-modal="true" aria-label="\u6587\u7ae0\u9605\u8bfb\u5668">\n'
        '<div class="r2-top"><button class="r2-close" onclick="closeReader()" title="关闭阅读器" aria-label="关闭"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg></button>\n'
        '<span class="r2-src" id="r2Src"></span>\n'
        '<div class="r2-acts"><div class="r2-fs-btns"><button class="r2-fs-btn" onclick="setFontSize(\'sm\')" title="\u5c0f\u5b57\u53f7">A-</button><button class="r2-fs-btn" onclick="setFontSize(\'md\')" title="\u9ed8\u8ba4\u5b57\u53f7">A</button><button class="r2-fs-btn" onclick="setFontSize(\'lg\')" title="\u5927\u5b57\u53f7">A+</button></div><button class="r2-bm" id="r2Bm" onclick="if(curArt)toggleBookmark(curArt)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg><span>\u6536\u85cf</span></button><a class="r2-open" id="r2Open" href="#" target="_blank" rel="noopener">\u539f\u7ad9 \u2197</a></div>\n'
        '<div class="r2-progress" id="r2Progress"></div></div>\n'
        '<div class="r2-body" id="r2Body"><div class="r2-inner" id="r2Inner"></div></div></aside>\n'
        '<div class="toast" id="toast" role="status" aria-live="polite"></div>\n'
        '<button class="back-top" id="backTop" onclick="window.scrollTo({top:0,behavior:\'smooth\'})" aria-label="\u8fd4\u56de\u9876\u90e8"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg></button>\n'
        '<div class="share-modal" id="shareModal" role="dialog" aria-modal="true" aria-label="分享文章">\n'
        '<div class="share-backdrop" onclick="closeShareModal()"></div>\n'
        '<div class="share-panel">\n'
        '<div class="share-hd"><h3>分享图片已生成</h3><button class="share-close" onclick="closeShareModal()" aria-label="关闭">×</button></div>\n'
        '<div class="share-img-wrap"><img id="shareImg" alt="分享图片"/></div>\n'
        '<div class="share-textcard" id="shareTextCard"></div>\n'
        '<p class="share-hint">长按图片保存，发送至微信好友或朋友圈</p>\n'
        '<div class="share-actions">\n'
        '<button class="btn-share-save" id="btnShareSave" onclick="saveShareImage()">保存图片</button>\n'
        '<button class="btn-share-copy" id="btnShareCopy" onclick="copyShareImage()">复制图片</button>\n'
        '<button class="btn-share-copy" id="btnShareCopyText" onclick="copyShareText()" style="display:none;">复制文字</button>\n'
        '<button class="btn-share-html" id="btnShareHtml" onclick="copyFullHtml()" style="margin-left:6px;padding:8px 22px;border-radius:8px;font-size:13px;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;transition:all .15s;font-family:var(--body);">复制全文HTML</button>\n'
        '</div>\n'
        '</div>\n'
        '</div>\n'
        + '<script src="rss-data-0.js?v=' + str(int(build_ts_ms)) + '"></' + 'script>\n'
+ _build_js(sources_with_items, build_ts_ms, analysis_json=analysis_json) +
        '</body>\n</html>'
    )


# ──────────────────────────── 智能分析模块 ────────────────────────────

# 科技/AI/热点领域术语词典（硬编码，按长度降序排列用于贪心最长匹配）
_TECH_DICT = sorted({
    # AI / 大模型
    '大模型', '人工智能', '机器学习', '深度学习', '自然语言处理', '神经网络',
    '多模态', '扩散模型', '生成式', 'Transformer', '注意力机制', '预训练',
    '微调', '提示词', 'Agent', '智能体', 'RAG', '检索增强', '向量数据库',
    '大语言模型', '通用人工智能', 'AGI', 'LLM', 'GPT',
    # 芯片 / 硬件
    '半导体', '芯片', '制程', '光刻机', '先进封装', 'HBM', '存储芯片',
    'GPU', 'CPU', 'NPU', '算力', '数据中心', '服务器',
    # 互联网 / 科技
    '自动驾驶', '机器人', '元宇宙', '数字孪生', '边缘计算', '云计算',
    '区块链', 'Web3', '去中心化', '开源', '操作系统', '浏览器',
    '智能手机', '折叠屏', '卫星通信', '低轨卫星', '6G', '5G',
    # 热点 / 社会
    '泥石流', '地震', '台风', '洪水', '应急响应', '救援',
    '高考', '考研', '就业', '裁员', 'IPO', '上市', '融资',
    '监管', '合规', '反垄断', '数据安全', '隐私保护',
    # 国际 / 政治
    '首相', '总统', '大选', '制裁', '关税', '贸易', '地缘政治',
    # 金融 / 经济
    '股市', 'A股', '港股', '美股', '比特币', '加密货币', '央行',
    '降息', '加息', '通胀', 'GDP', 'CPI',
    # 通用高频词（避免被切散）
    '审核', '一审', '二审', '判决', '法院', '检察院', '立案',
    '报道', '据悉', '表示', '指出', '认为', '透露', '宣布',
    '发布', '推出', '上线', '下线', '升级', '更新', '修复',
    '漏洞', '安全', '攻击', '黑客', '恶意软件', '病毒',
    '用户', '开发者', '程序员', '工程师', '科学家', '研究员',
    '公司', '企业', '机构', '政府', '部门', '组织',
    '中国', '美国', '日本', '欧洲', '全球', '国内', '海外',
    '科技', '技术', '创新', '突破', '进展', '成果',
    '产品', '服务', '平台', '应用', '软件', '硬件', '系统',
    '数据', '算法', '模型', '训练', '推理', '部署',
    '网络', '互联网', '移动', '无线', '通信', '信号',
    '能源', '电池', '电动车', '新能源', '光伏', '风电',
    '医疗', '健康', '生物', '基因', '疫苗', '药物',
    '教育', '学校', '大学', '研究', '学术', '论文',
    '文化', '娱乐', '电影', '音乐', '游戏', '电竞',
    '体育', '奥运', '足球', '篮球', '赛事',
    '环境', '气候', '碳排放', '绿色', '可持续',
    '军事', '国防', '武器', '导弹', '演习',
    '外交', '谈判', '协议', '条约', '峰会',
}, key=len, reverse=True)

# 已知无意义双字组合（滑动窗口回退时过滤）
_NOISE_BIGRAMS = {'军一', '核被', '审一', '已故', '人称', '据报',
                  '的的', '了了', '是是', '在在', '有有', '和和',
                  # 日期/时间碎片
                  '月日', '年月', '日年', '时分', '秒分',
                  # 无意义组合
                  '地址', '网址', '链接', '点击', '查看', '阅读', '原文'}


def _is_valid_ngram(word):
    """判断 n-gram 是否有意义（质量过滤）。"""
    if not word or len(word) < 2:
        return False
    # 排除连续相同字符（被被、一一、审审）
    if len(set(word)) == 1:
        return False
    # 排除停用词
    if word in _STOP_WORDS:
        return False
    # 排除已知无意义组合
    if word in _NOISE_BIGRAMS:
        return False
    # 含空格 → 拒绝（如 "月 日"、"址  "）
    if ' ' in word or '\u3000' in word:
        return False
    # 含特殊符号 → 拒绝（如 "文 ·"、"评论:"）
    if re.search(r'[·:：.,，;；!?！？()（）\[\]【】{}<>]', word):
        return False
    # 纯数字 → 拒绝
    if word.isdigit():
        return False
    return True


def _has_repeated_chars(text, min_repeats=2, min_run=2):
    """检测文本中是否有过多连续重复字符（OCR 错误 / 病句信号）。
    例如 '被被' '审审' '一一' 等。"""
    count = 0
    for i in range(len(text) - 1):
        if text[i] == text[i + 1] and '\u4e00' <= text[i] <= '\u9fff':
            count += 1
    return count >= min_repeats


def _tokenize(text):
    """分词：术语词典优先 + 贪心最长匹配 + 英文单词。返回小写 token 列表。"""
    if not text:
        return []
    text = text.lower()
    tokens = []

    # 1. 英文单词（保持原逻辑）
    en_words = re.findall(r'[a-z][a-z0-9_-]{1,}', text)
    tokens.extend(w for w in en_words if w not in _STOP_WORDS and len(w) > 1)

    # 2. 中文：术语词典优先匹配，剩余用改进的 n-gram
    cn_text = re.sub(r'[a-z0-9_\-\s]+', ' ', text)  # 去掉英文片段
    cn_text = re.sub(r'[\u3000-\u303f\uff00-\uffef]', ' ', cn_text)  # 去掉全角标点

    i = 0
    chars = list(cn_text)
    while i < len(chars):
        ch = chars[i]
        # 跳过非中文字符
        if not ('\u4e00' <= ch <= '\u9fff'):
            i += 1
            continue
        # 贪心最长匹配：从最长术语开始尝试
        matched = False
        for term in _TECH_DICT:
            if i + len(term) <= len(chars):
                candidate = ''.join(chars[i:i + len(term)])
                if candidate == term:
                    tokens.append(term)
                    i += len(term)
                    matched = True
                    break
        if not matched:
            # 未命中词典：尝试 2-3 字 n-gram（仅保留有意义的）
            found_ngram = False
            for span in [3, 2]:
                if i + span <= len(chars):
                    ngram = ''.join(chars[i:i + span])
                    if _is_valid_ngram(ngram):
                        tokens.append(ngram)
                        i += span
                        found_ngram = True
                        break
            if not found_ngram:
                i += 1  # 跳过单字
    return tokens


@functools.lru_cache(maxsize=65536)
def _tokenize_cached(text):
    """tokenize 结果缓存：D4 语料放大后同一文本多份复现，避免重复分词（rev8 P1-3）。"""
    return tuple(_tokenize(text))


def _tfidf_keywords(texts, top_n=50, per_doc_top=10):
    """从文本列表中提取全局 TF-IDF 关键词。返回 [(keyword, score), ...]。"""
    if not texts:
        return []
    # 每篇文章的 token 集合（缓存：重复语料只分词一次）
    doc_tokens = [list(_tokenize_cached(t)) for t in texts]
    n_docs = len(doc_tokens)
    # DF: 每个 token 出现在多少篇文档中
    df = collections.Counter()
    for tokens in doc_tokens:
        unique = set(tokens)
        for t in unique:
            df[t] += 1
    # TF: 全局词频
    tf = collections.Counter()
    for tokens in doc_tokens:
        tf.update(tokens)
    # TF-IDF 评分
    scores = {}
    for word, freq in tf.items():
        if len(word) < 2 or df[word] < 2:
            continue  # 过滤太短或只出现一次的词
        idf = math.log(1 + n_docs / (1 + df[word]))
        scores[word] = freq * idf
    # 过滤无意义关键词
    scores = {w: s for w, s in scores.items() if _is_valid_ngram(w)}
    return sorted(scores.items(), key=lambda x: -x[1])[:top_n]


def _extract_keywords(rss_history, now_bj, unreliable_srcs=None):
    """从 72h 历史中提取关键词。返回 {global: [...], by_cat: {cat: [...]}}。

    D4：bad_date 不可信源（unreliable_srcs）的条目按 0.3 权重精确降权——
    以 10 倍放大实现整数化配比（正常基线 10 份、不可信 3 份），
    tfidf 频次与文本份数线性映射，权重比逐字面成立。
    """
    _W_BASE, _W_RECENT, _UNRELIABLE_FACTOR = 10, 2, 3
    unreliable = unreliable_srcs or set()
    cutoff_24h = now_bj.replace(tzinfo=None) - datetime.timedelta(hours=24)
    # 近 24h 文章用于热点提取
    recent_texts = []
    recent_count = 0
    cat_texts = collections.defaultdict(list)
    for item in rss_history.values():
        pd_str = item.get('pub_date', '')
        if pd_str:
            try:
                pd = datetime.datetime.fromisoformat(pd_str)
                if pd.tzinfo:
                    pd = pd.astimezone(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
            except ValueError:
                pd = now_bj.replace(tzinfo=None)
        else:
            pd = now_bj.replace(tzinfo=None)
        title = item.get('title_zh', '') or item.get('title', '')
        summary = item.get('summary_zh', '') or item.get('summary', '')
        text = (title + ' ' + summary).strip()
        if not text:
            continue
        is_recent = pd >= cutoff_24h
        if is_recent:
            recent_count += 1  # 真实近24h条数（与语料复制解耦，rev8 P1-1）
        # D4 权重以语料份额计（精确 0.3）：正常 recent = W_BASE*W_RECENT 份 + W_BASE 份；
        # 不可信 = 正常 × UNRELIABLE_FACTOR/10（0.3 倍）
        if item.get('source_key', '') in unreliable:
            if is_recent:
                recent_texts.extend([text] * (_W_BASE * _W_RECENT * _UNRELIABLE_FACTOR // 10))
            cat_texts[item.get('cat', 'other')].extend([text] * (_W_BASE * _UNRELIABLE_FACTOR // 10))
        else:
            if is_recent:
                recent_texts.extend([text] * (_W_BASE * _W_RECENT))
            cat_texts[item.get('cat', 'other')].extend([text] * _W_BASE)
    # 全局关键词（近 24h 优先，但用全部 72h 数据）
    all_texts = recent_texts * 2 + [t for cat_ts in cat_texts.values() for t in cat_ts]  # 近 24h 权重 x2
    global_kw = _tfidf_keywords(all_texts, top_n=50)
    # 按分类关键词
    by_cat = {}
    for cat, texts in cat_texts.items():
        if len(texts) >= 3:
            by_cat[cat] = _tfidf_keywords(texts, top_n=20)
    return {"global": global_kw, "by_cat": by_cat, "recent_count": recent_count}


def _score_sources(sources_with_items, rss_history):
    """信源质量评分。返回 {source_key: {score, metrics}}。"""
    result = {}
    # 按源统计历史数据
    src_stats = collections.defaultdict(lambda: {
        'total': 0, 'has_summary': 0, 'has_fullcontent': 0,
        'bad_date_count': 0, 'total_with_date': 0,
    })
    for item in rss_history.values():
        sk = item.get('source_key', '')
        if not sk:
            continue
        s = src_stats[sk]
        s['total'] += 1
        summary = item.get('summary', '') or item.get('summary_zh', '')
        if len(summary) > 60:
            s['has_summary'] += 1
        fc = item.get('full_content', '')
        if fc and len(fc) > 100:
            s['has_fullcontent'] += 1
        if item.get('pub_date'):
            s['total_with_date'] += 1
        if item.get('bad_date'):
            s['bad_date_count'] += 1
    for src in sources_with_items:
        sk = src['key']
        s = src_stats.get(sk, {'total': 0, 'has_summary': 0, 'has_fullcontent': 0, 'bad_date_count': 0, 'total_with_date': 0})
        n = s['total']
        if n == 0:
            # 72h 历史无数据：用 tier 推断默认分（新增源/低频源/被跳过源）
            _tier = src.get('tier', 3)
            _default = {1: 60, 2: 40}.get(_tier, 25)
            result[sk] = {
                'score': _default,
                'metrics': {},
                'article_count': 0,
                'inferred': True,
            }
            continue
        freshness = min(1.0, n / 10.0)              # 72h 内文章数 / 10 篇满分
        coverage = s['has_summary'] / n if n > 0 else 0
        date_trust = 1.0 - (s['bad_date_count'] / s['total_with_date'] if s['total_with_date'] > 0 else 0)
        fulltext = s['has_fullcontent'] / n if n > 0 else 0
        # 抓取成功率从 sources_with_items 的 items 是否为空推断
        reliability = 1.0 if n > 0 else 0.0
        score = (freshness * 0.25 + coverage * 0.20 + date_trust * 0.20 +
                 fulltext * 0.15 + reliability * 0.20) * 100
        result[sk] = {
            'score': round(score, 1),
            'metrics': {
                'freshness': round(freshness * 100, 1),
                'coverage': round(coverage * 100, 1),
                'date_trust': round(date_trust * 100, 1),
                'fulltext': round(fulltext * 100, 1),
                'reliability': round(reliability * 100, 1),
            },
            'article_count': n,
        }
    return result


# ──────────────────── 话题标签提取（issue C2） ────────────────────
# 旧实现直接取 _tokenize 的词频 Top3。但 _tokenize 是「从位置 0 起逐位贪心吞 3 字」
# 的扫描器，词典术语会被前一位置的误吞切断，且切点落在词中间：
#     '苹果发布会'   → ['苹果发', '布会']
#     '关于人工智能' → ['关于人', '工智能']      （'工智能' 是 '人工智能' 的真子串）
#     '出席金砖国家' → ['出席金', '砖国家']
# 真实语料实测（27076 个标签）：16.7% 的标签首/尾是虚词功能字
# （'的星尘' / '或让' / '将瞄准' / '定 价' / '大  '），4.2% 含空白，
# 台标与栏目名（rfi / cnn / hn / 问与答）进入 Top 15 —— 与「出版社样板污染」同源。
#
# 新口径（顺序即优先级）：
#   P1 词典术语整词命中（绝不切碎）
#   P2 英文词：原文含大写（品牌/缩写）或命中词典，且非台标/样板
#   P3 长度 4..上界的边界对齐中文整段（'苹果发布会' 这类短段整取，不切）
#   P4 其余中文段（2~3 字、或超过上界）用过滤后的词频兜底
#   P5 按源样板抑制：某 token 出现在该源 ≥60% 篇目中 → 台标/栏目名，整源剔除
_TAG_MAX_LEN = 6                 # 中文标签长度上界；超过这个长度的整段是句子片段，不是标签
_TAG_BOILER_MIN_ITEMS = 10       # 源内至少这么多篇才做样板词统计（样本太少无统计意义）
_TAG_BOILER_RATIO = 0.6          # 出现在该源 ≥60% 篇目 → 判为台标/栏目名

# 首/尾出现这些字即为「切在词中间」的切片（'的星尘' / '或让' / '将瞄准'）
_TAG_EDGE_NOISE = set(
    "的了是在我有和就不都一个上也这到说们为你对被把让给用从向"
    "该此其本如与及或并等将已正未很更最又还只仅才再也都吗呢吧啊着过据称当以对"
)
# 样板/泛化词：命中词典也不作标签（否则 '发布'/'公司'/'技术' 会占领标签表）
_TAG_BLOCKLIST = set("""
报道 据悉 表示 指出 认为 透露 宣布 发布 推出 上线 下线 升级 更新 修复
完成 使用 进行 实现 相关 有关 目前 近日 今日 昨日 未来 正在 已经 可以 需要
公司 企业 机构 政府 部门 组织 用户 产品 服务 平台 应用 软件 硬件
系统 数据 技术 创新 突破 进展 成果 市场 行业 领域 情况 问题 方面 内容 方式
为什么 有没有 一个 做了 第一个 第二十 十周年 评论 询问 突发 分享 观点 消息
记者 编辑 来源 版权 转载 声明 观察 分析 解读 盘点 汇总 速览 早报 晚报
网址 链接 原文 全文 本文 本站 译者 校对 责编 发自 综合报道 文章来源
文章网址 评论网址 阅读全文 查看更多 推荐阅读 相关阅读 点击查看 详情
""".split())
# 英文样板词：多为栏目名/停用词，且不在 _TECH_DICT 内
_TAG_EN_BLOCKLIST = set("""
hn pro fix show duo flash ask news de le der web france fr en es
the and for with from that this new top best how why what when who
you your his her its our their all any can may not but
""".split())
_TAG_DICT_LOWER = set(t.lower() for t in _TECH_DICT)
_TAG_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_TAG_EN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._+\-]{1,19}")
_TAG_WS = re.compile(r"\s")


def _tag_src_name_norm(name):
    """源名归一化：只留字母/数字/汉字（用于台标自指判断）。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (name or "").lower())


def _is_tag_station(tok, src_norm):
    """台标判定：标签是源名的一部分，或源名是标签的一部分（'cnn' ⊂ 'CNN'）。"""
    if not src_norm or len(tok) < 2:
        return False
    tl = tok.lower()
    return tl in src_norm or (len(src_norm) >= 3 and src_norm in tl)


def _tag_edge_ok(tok):
    """标签首/尾不得是虚词功能字（'的星尘' / '或让' 这类切片的判据）。"""
    return len(tok) >= 2 and tok[0] not in _TAG_EDGE_NOISE and tok[-1] not in _TAG_EDGE_NOISE


def _tag_ok(tok, src_norm, low_text):
    """标签通用过滤：长度/空白/样板/边界/数值单位/台标/词典术语真子串。"""
    if len(tok) < 2 or _TAG_WS.search(tok):
        return False
    if tok in _TAG_BLOCKLIST or tok in _TAG_EN_BLOCKLIST:
        return False
    if not _tag_edge_ok(tok) or _is_numeric_unit_phrase(tok):
        return False
    if _is_tag_station(tok, src_norm):
        return False
    if not tok.isascii() and len(tok) > _TAG_MAX_LEN:
        return False
    # 词典术语的真子串：'工智能' ⊂ '人工智能'（同一文本里 '人工智能' 也在）
    if any(tok != d and tok in d and d in low_text for d in _TAG_DICT_LOWER):
        return False
    return True


def _tag_candidates(text, src_norm, allow_runs=True):
    """按优先级产出候选标签（可能超过 3 个，由调用方截断）。

    allow_runs=False 时只走 P1/P2（词典术语 + 英文词），不切中文段：
    摘要是一整段散文，长中文段里没有可用词典术语时只能靠 n-gram 硬切，
    实测切出来的是 '自微信' / '际广播' / '文章网址' 这类东西。
    标题本身短且是名词短语，才值得做边界对齐的整段取词。
    """
    text = text or ""
    if not text.strip():
        return []
    low = text.lower()
    picks = []

    # P1 词典术语：整词命中。按出现位置排，同位置取更长者（避免短词吃掉长词的位）
    hits = []
    for term in _TECH_DICT:
        k = low.find(term.lower())
        if k >= 0:
            hits.append((term.lower(), k, len(term)))
    hits.sort(key=lambda x: (x[1], -x[2]))
    for w, _k, _L in hits:
        if _tag_ok(w, src_norm, low):
            picks.append(w)

    # P2 英文词：原文含大写（品牌/缩写如 OpenAI/iPhone），或命中词典（ai/llm/gpt）
    for m in _TAG_EN_WORD.finditer(text):
        raw = m.group(0)
        w = raw.lower()
        if w in _TAG_EN_BLOCKLIST or w in _TAG_BLOCKLIST:
            continue
        if w not in _TAG_DICT_LOWER and not any(c.isupper() for c in raw):
            continue
        if _is_tag_station(w, src_norm):
            continue
        picks.append(w)

    # P3/P4 中文段：短段（4..上界）整取，其余用过滤后的词频兜底
    if allow_runs:
        for run in _TAG_CJK_RUN.findall(text):
            L = len(run)
            if L < 2:
                continue
            # 「本段有可用词典术语」时不再切段 —— 切碎只会产生碎片（'出席金'/'砖国家'）。
            # 两种情形交给 P1 整词发出：
            #   a) 段太长（>上界）：整段不可用，只能靠词典术语，切碎只会更差；
            #   b) 段内某个可用术语几乎占满整段（len ≥ L-1）：该段基本就是这个术语 + 杂字。
            # 反例（必须走整段）：'苹果发布会' 段内的 '发布' 是样板词、不可用，
            # 此时整段本身就是最好的短语，切碎才会得到 '苹果发'/'布会'。
            eff = [d for d in _TAG_DICT_LOWER if d in run and _tag_ok(d, src_norm, low)]
            has_any = any(d in run for d in _TAG_DICT_LOWER)
            if has_any and (L > _TAG_MAX_LEN or (eff and max(len(d) for d in eff) >= L - 1)):
                continue
            if 4 <= L <= _TAG_MAX_LEN and _tag_ok(run, src_norm, low):
                picks.append(run)
                continue
            toks = []
            for t in _tokenize_cached(run):
                t = t.strip()
                if _tag_ok(t, src_norm, low):
                    toks.append(t)
            for w, _ in collections.Counter(toks).most_common(3):
                picks.append(w)

    seen, out = set(), []
    for w in picks:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _tag_articles(sources_with_items):
    """为每篇文章提取 1-3 个话题标签。

    标签来源：标题（title_zh/title/t，构建期与刷新通道两种契约）优先，
    不足 3 个时用摘要前 200 字（summary_zh/summary/s）补齐。
    口径见上方常量区注释（C2 修复）。
    原地修改 item，新增 tags 字段。
    """
    for src in sources_with_items:
        items = src.get("items", [])
        if not items:
            continue
        sn = _tag_src_name_norm(src.get("name"))
        # 第一遍：逐条产出候选
        raw = []
        for it in items:
            title = it.get("title_zh") or it.get("title") or it.get("t") or ""
            summary = (it.get("summary_zh") or it.get("summary") or it.get("s") or "")[:200]
            if not (title or summary).strip():
                raw.append([])
                continue
            tags = []
            # 标题：完整管线（含边界对齐整段取词）；
            # 摘要：只取词典术语与英文词（散文长段硬切只会得到 '自微信'/'际网址' 这类碎片）
            for pool, allow_runs in ((title, True), (summary, False)):
                if len(tags) >= 3:
                    break
                for w in _tag_candidates(pool, sn, allow_runs=allow_runs):
                    if len(tags) >= 3:
                        break
                    if w not in tags:
                        tags.append(w)
            raw.append(tags)
        # 第二遍：按源统计样板词（台标/栏目名），整源剔除
        # 判据是「在这个源里几乎每篇都出现」——话题词不会这么均匀，栏目名会。
        boiler = set()
        if len(items) >= _TAG_BOILER_MIN_ITEMS:
            df = collections.Counter()
            for tags in raw:
                for t in set(tags):
                    df[t] += 1
            need = max(3, _TAG_BOILER_RATIO * len(items))
            boiler = set(t for t, c in df.items() if c >= need)
        for it, tags in zip(items, raw):
            it["tags"] = [t for t in tags if t not in boiler]


def _is_numeric_unit_phrase(s):
    """判断字符串是否为纯数值单位短语（如 '亿欧元'、'万美元'、'千亿元'）。
    这类短语不适合作为话题标签。
    """
    # 匹配模式：可选数字 + 单位词（亿/万/千/百）+ 货币/量词
    if re.match(r'^[\d零一二三四五六七八九十百千万]*[亿万万千百]?[元美元欧元英镑日元份项笔台架艘辆匹头只条块片张本座栋层等级场次局盘局]+$', s):
        return True
    # 纯数字 + 单位
    if re.match(r'^[\d零一二三四五六七八九十百千万]+[个只条台架艘辆匹头元美元欧元英镑日元份项笔]+$', s):
        return True
    return False


def _is_ascii_alpha(ch):
    """判断字符是否为 ASCII 字母（a-z, A-Z）。"""
    return 'a' <= ch <= 'z' or 'A' <= ch <= 'Z'


def _find_cn_boundary(text, pos):
    """从 pos 位置开始，找到下一个中文语义边界的位置。

    边界定义：标点符号、空格、数字、ASCII 字母、或字符串末尾。
    用于确保截取的标签片段在语义上完整，不会在词组中间断开。
    返回边界位置的索引（即边界字符的位置），若 pos 已在边界则返回 pos。
    """
    if pos >= len(text):
        return pos
    ch = text[pos]
    # 当前字符本身就是边界
    if ch in '，。：！？、；""''（）()【】[]《》<>,. \t\n\r':
        return pos
    if ch.isdigit() or _is_ascii_alpha(ch):
        return pos
    # 从 pos 开始向后找第一个边界
    for i in range(pos, len(text)):
        c = text[i]
        if c in '，。：！？、；""''（）()【】[]《》<>,. \t\n\r':
            return i
        if c.isdigit() or _is_ascii_alpha(c):
            return i
    return len(text)


def _extract_meaningful_phrase(title, max_len=14):
    """从单个标题中提取一个语义完整的短语（不超过 max_len 字符）。

    优先在标点/空格/数字/拉丁字母处截断，确保不会出现半截词组。
    """
    if not title or len(title) <= max_len:
        return title
    # 在 max_len 附近找最近的语义边界
    # 先检查 max_len 位置是否已经是边界
    boundary = _find_cn_boundary(title, max_len)
    if boundary == max_len:
        return title[:max_len]
    # 如果边界在 max_len 之后不远（<= 3 字符），延伸到边界
    if boundary > max_len and boundary - max_len <= 3:
        return title[:boundary]
    # 否则在 max_len 之前找最近的边界（往回找最多 5 字符）
    for i in range(max_len - 1, max(0, max_len - 6), -1):
        c = title[i]
        if c in '，。：！？、；""''（）()【】[]《》<>,. \t\n\r':
            return title[:i]
        if c.isdigit() or _is_ascii_alpha(c):
            return title[:i]
    # 找不到好边界，就截取到 max_len
    return title[:max_len]


def _clean_topic_label(label, max_len=20):
    """清洗话题标签：截断过长标签、移除未完成引号、去除尾部标点。"""
    if not label:
        return ''
    # 限制最大长度
    if len(label) > max_len:
        label = label[:max_len].rstrip()
    # 移除未完成的引号（截断导致的）
    label = re.sub(r'["\'"\'「『【\(（]$', '', label)
    # 移除尾部标点
    label = re.sub(r'[，。：；！？,\.:;!?\s]+$', '', label)
    # 如果标签太短或为空，返回空
    if len(label) < 2:
        return ''
    return label


def _extract_topic_label_from_titles(cluster_articles):
    """从簇内文章标题中提取语义通顺的话题标签。

    优先级：
    1. 术语词典精确命中（标题中出现最多的 _TECH_DICT 词条）
    2. 最长公共子串 + 边界延伸（跨标题重复的连续中文片段，合并相邻 token 保留邻接关系）
    3. 标题内相邻 2-3 个高质量 token 拼接（分词错误检测 + 边界感知过滤）
    4. 兜底：从代表性标题中智能截取语义完整片段
    返回 (label_str, labels_list)。
    """
    _LABEL_NOISE_LOCAL = set('的了是在我有和就不都一个上也这到说们为你对被把让给用从向')

    # 收集合格标题（去重、去病句）
    good_titles = []
    seen_titles = set()
    for art in cluster_articles:
        t = art.get('title', '')
        if not t or len(t) < 4 or _has_repeated_chars(t, min_repeats=2, min_run=2):
            continue
        t_key = t[:30]  # 前 30 字去重
        if t_key in seen_titles:
            continue
        seen_titles.add(t_key)
        good_titles.append(t)

    if not good_titles:
        return '', []

    # ── 策略 1+2 联合决策：先算公共子串（带边界延伸），再与词典术语比较覆盖度 ──

    # 策略 2：最长公共子串 + 边界延伸
    best_substr = None
    best_substr_cnt = 0
    if len(good_titles) >= 2:
        # 预计算去空格标题：用于子串匹配和延伸，确保标签不含空格
        titles_stripped = [t.replace(' ', '').replace('\u3000', '') for t in good_titles]
        cn_fragments = []
        for ts in titles_stripped:
            # 逐 token 提取中文部分，再合并相邻 token 的中文片段
            # 这样 '多模态 发布全新' → 去空格 '多模态发布全新' → 单一片段
            # 保留跨空格的邻接关系，避免 '多模态' 和 '发布' 被拆成独立片段
            cn_only = re.sub(r'[^\u4e00-\u9fff]', '', ts)
            if len(cn_only) >= 3:
                cn_fragments.append(cn_only)
        substr_score = collections.Counter()
        for frag in cn_fragments:
            max_len = min(len(frag), 12)
            for slen in range(2, max_len + 1):
                for start in range(len(frag) - slen + 1):
                    sub = frag[start:start + slen]
                    if sub[0] in _LABEL_NOISE_LOCAL or sub[-1] in _LABEL_NOISE_LOCAL:
                        continue
                    cnt = sum(1 for ts in titles_stripped if sub in ts)
                    if cnt >= 2:
                        # 评分改为覆盖度优先：cnt × len，避免短子串因位置多而得分虚高
                        substr_score[sub] = cnt * slen
        if substr_score:
            # 覆盖度优先 + 同覆盖度选最长：先找最高覆盖度，再在同等覆盖度中选最长子串
            max_cnt = 0
            for sub in substr_score:
                cnt = sum(1 for ts in titles_stripped if sub in ts)
                if cnt > max_cnt:
                    max_cnt = cnt
            # 在最高覆盖度的子串中选最长的
            best_candidates = [(sub, len(sub)) for sub, score in substr_score.items()
                               if sum(1 for ts in titles_stripped if sub in ts) == max_cnt]
            best_candidates.sort(key=lambda x: x[1], reverse=True)
            # 对每个候选尝试边界延伸，然后选最优
            for candidate, cand_len in best_candidates[:20]:  # 最多处理前20个最长候选
                cand_cnt = max_cnt
                # 尝试延伸子串到语义边界
                extended = candidate
                extended_cnt = cand_cnt
                # 在去空格标题中找到 candidate，尝试向后延伸到边界
                for ts in titles_stripped:
                    idx = ts.find(candidate)
                    if idx < 0:
                        continue
                    end_pos = idx + len(candidate)
                    if end_pos >= len(ts):
                        continue  # 已在标题末尾
                    next_ch = ts[end_pos]
                    # 如果下一个字符是数字/拉丁字母，延伸到边界
                    if next_ch.isdigit() or _is_ascii_alpha(next_ch):
                        boundary_pos = _find_cn_boundary(ts, end_pos)
                        if boundary_pos > end_pos and boundary_pos - end_pos <= 4:
                            ext = ts[idx:boundary_pos]
                            ext_cnt = sum(1 for tt in titles_stripped if ext in tt)
                            if ext_cnt >= max(2, cand_cnt - 1) and len(ext) > len(extended):
                                extended = ext
                                extended_cnt = ext_cnt
                        break  # 只用第一个匹配标题来延伸
                    # 下一个是中文字符：尝试延伸 1-10 个字符
                    if '\u4e00' <= next_ch <= '\u9fff':
                        for ext_len in range(1, 11):
                            if end_pos + ext_len > len(ts):
                                break
                            ext = ts[idx:end_pos + ext_len]
                            ext_cnt = sum(1 for tt in titles_stripped if ext in tt)
                            # 延伸后覆盖度不能下降太多（允许降 1）
                            if ext_cnt >= max(2, cand_cnt - 1):
                                extended = ext
                                extended_cnt = ext_cnt
                                # 检查延伸后的末尾是否是边界
                                if end_pos + ext_len >= len(ts):
                                    break
                                next_ch2 = ts[end_pos + ext_len]
                                if next_ch2.isdigit() or _is_ascii_alpha(next_ch2):
                                    break
                                if not ('\u4e00' <= next_ch2 <= '\u9fff'):
                                    break
                            else:
                                break
                    else:
                        # 下一个是标点等，自然边界
                        pass
    
                # 用延伸后的结果与当前最优比较
                if extended_cnt > best_substr_cnt or (
                    extended_cnt == best_substr_cnt and len(extended) > len(best_substr or '')
                ):
                    best_substr = extended
                    best_substr_cnt = extended_cnt

    # 策略 2 纯中文结果优先：如果公共子串是纯中文且 >= 2 字，直接返回（避免策略 1 产生混合语言标签）
    if best_substr and len(best_substr) >= 2:
        cn_chars_in_substr = re.sub(r'[^\u4e00-\u9fff]', '', best_substr)
        if len(cn_chars_in_substr) == len(best_substr):
            # 纯中文子串，仅检查数值单位过滤
            if len(cn_chars_in_substr) <= 2 and _is_numeric_unit_phrase(best_substr):
                pass  # 纯数值单位（如 '美元'），跳过，进入策略 1
            else:
                return best_substr, [best_substr]

    # 策略 1：术语词典精确匹配
    dict_term_counter = collections.Counter()
    for title in good_titles:
        title_lower = title.lower()
        seen_in_doc = set()
        for term in _TECH_DICT:
            tl = term.lower()
            if tl in title_lower and len(term) >= 2 and tl not in seen_in_doc:
                dict_term_counter[term] += 1
                seen_in_doc.add(tl)
    if dict_term_counter:
        top_term, top_cnt = dict_term_counter.most_common(1)[0]
        # 仅当词典术语的标题覆盖度严格优于公共子串，或覆盖度相同但术语更长时才优先返回
        if top_cnt > best_substr_cnt or (top_cnt == best_substr_cnt and len(top_term) >= len(best_substr or '')):
            second = None
            for term, _ in dict_term_counter.most_common(5):
                if term != top_term:
                    second = term
                    break
            if second and len(second) >= 2:
                return '%s %s' % (top_term, second), [top_term, second]
            return top_term, [top_term]

    # 公共子串有效则返回（纯中文已在上方提前返回；此处处理含数字/拉丁的混合子串）
    if best_substr and len(best_substr) >= 2:
        cn_chars_in_substr = re.sub(r'[^\u4e00-\u9fff]', '', best_substr)
        if len(cn_chars_in_substr) <= 2 and _is_numeric_unit_phrase(best_substr):
            pass  # 纯数值单位，跳过，进入策略 3
        else:
            return best_substr, [best_substr]

    # ─ 策略 3：标题内相邻 token 高频组合（边界感知） ──
    ngram_counter = collections.Counter()
    for title in good_titles:
        tokens = _tokenize(title)
        quality_tokens = [t for t in tokens
                          if len(t) >= 2 and t not in _LABEL_NOISE_LOCAL and _is_valid_ngram(t)]
        # 连续 2-gram 和 3-gram
        for span in (2, 3):
            for j in range(len(quality_tokens) - span + 1):
                phrase = ' '.join(quality_tokens[j:j + span])
                # 至少有一个 token 长度 >= 3（避免两个短 token 拼出碎片）
                if any(len(quality_tokens[j + k]) >= 3 for k in range(span)):
                    # 额外检查：拼接后的总中文字符数 >= 4（避免 "多模态 发布" 这种松散组合）
                    cn_chars = re.sub(r'[^\u4e00-\u9fff]', '', phrase)
                    if len(cn_chars) >= 4:
                        # 质量过滤：含空格的短语需要额外验证
                        if ' ' in phrase:
                            no_space = phrase.replace(' ', '')
                            # 如果去空格后的组合在源标题中出现，说明空格是分词错误
                            if any(no_space in t for t in good_titles):
                                continue  # 跳过：分词错误，应为一个完整词组
                            # 所有 token 都是纯中文 → 松散拼接，跳过
                            parts_check = phrase.split()
                            if all(all('\u4e00' <= c <= '\u9fff' for c in part) for part in parts_check):
                                continue  # 跳过：纯中文 token 松散拼接
                        ngram_counter[phrase] += 1
    if ngram_counter:
        best_phrase = ngram_counter.most_common(1)[0][0]
        # 过滤数值单位短语
        cn_only = re.sub(r'[^\u4e00-\u9fff]', '', best_phrase)
        if len(cn_only) <= 4 and _is_numeric_unit_phrase(cn_only):
            pass  # 跳过，进入策略 4
        else:
            parts = best_phrase.split()
            return best_phrase, parts[:3]

    # ── 策略 4：兜底 — 从代表性标题中智能截取语义完整片段 ──
    # 选最长的合格标题作为代表性标题（信息量最大）
    representative = max(good_titles, key=len)
    phrase = _extract_meaningful_phrase(representative, max_len=14)
    if phrase and len(phrase) >= 4:
        return phrase, [phrase]

    # 最终兜底：直接取第一个合格标题的前 12 字
    for title in good_titles:
        if len(title) >= 6:
            truncated = title[:12].rstrip()
            for sep in ('，', '。', '：', '！', '？', ',', '.', ' ', ' '):
                idx = truncated.find(sep)
                if idx > 2:
                    truncated = truncated[:idx]
                    break
            return truncated, [truncated]

    return '', []


def _cluster_topics(rss_history, now_bj, max_topics=20, min_cluster=3):
    """基于标题+摘要关键词 Jaccard 相似度的话题聚类。返回话题列表。"""
    cutoff_24h = now_bj.replace(tzinfo=None) - datetime.timedelta(hours=24)
    # 通用单字过滤表（比 _STOP_WORDS 更严格，用于标签过滤）
    _LABEL_NOISE = set('的了是在我有和就不人都一个上也这到说们为你对被把让给用从向')
    # 收集近 24h 文章（标题 + 摘要关键词集合）
    articles = []
    for link, item in rss_history.items():
        pd_str = item.get('pub_date', '')
        if pd_str:
            try:
                pd = datetime.datetime.fromisoformat(pd_str)
                if pd.tzinfo:
                    pd = pd.astimezone(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
            except ValueError:
                pd = now_bj.replace(tzinfo=None)
        else:
            pd = now_bj.replace(tzinfo=None)
        if pd < cutoff_24h:
            continue
        title = item.get('title_zh', '') or item.get('title', '')
        if not title:
            continue
        # 标题质量过滤：跳过明显病句（连续重复字 >= 2 处）
        if _has_repeated_chars(title, min_repeats=2, min_run=2):
            continue
        title_tokens = set(_tokenize(title))
        # 摘要 token 仅保留与标题有交集的（避免过长摘要稀释标题信号）
        summary = item.get('summary_zh', '') or item.get('summary', '')
        if summary and len(summary) > 30:
            summary_tokens = set(_tokenize(summary[:300]))  # 截断避免过长
            tokens = title_tokens | (summary_tokens & title_tokens)
        else:
            tokens = title_tokens
        if len(tokens) < 2:
            continue
        articles.append({
            'link': link,
            'title': title,
            'source': item.get('source', ''),
            'source_key': item.get('source_key', ''),
            'cat': item.get('cat', ''),
            'tokens': tokens,
            'title_tokens': title_tokens,
        })
    if len(articles) < min_cluster:
        return []
    # 贪心聚类
    clusters = []
    for art in articles:
        best_idx = -1
        best_sim = 0
        for i, cl in enumerate(clusters):
            # Jaccard 相似度: 文章 tokens 与簇质心（所有 tokens 的并集的高频子集）
            inter = len(art['tokens'] & cl['centroid'])
            union = len(art['tokens'] | cl['centroid'])
            sim = inter / union if union > 0 else 0
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_sim >= 0.25 and best_idx >= 0:
            clusters[best_idx]['articles'].append(art)
            # 更新质心：保留出现最多的 top 15 词
            all_tokens = collections.Counter()
            for a in clusters[best_idx]['articles']:
                all_tokens.update(a['tokens'])
            clusters[best_idx]['centroid'] = set(w for w, _ in all_tokens.most_common(15))
        else:
            clusters.append({
                'articles': [art],
                'centroid': set(art['tokens']),
            })
    # 过滤小簇，提取标签
    topics = []
    for cl in clusters:
        if len(cl['articles']) < min_cluster:
            continue
        # ── 标题召回机制：优先从标题中提取语义通顺的标签 ──
        label, labels = _extract_topic_label_from_titles(cl['articles'])
        # 清洗标签
        label = _clean_topic_label(label)
        labels = [_clean_topic_label(lb) for lb in labels if _clean_topic_label(lb)]
        # 如果标题召回失败，回退到原始 token 频率逻辑
        if not label:
            title_counter = collections.Counter()
            all_counter = collections.Counter()
            for a in cl['articles']:
                title_counter.update(a.get('title_tokens', a['tokens']))
                all_counter.update(a['tokens'])
            labels = [w for w, _ in title_counter.most_common(10)
                      if len(w) >= 2 and w not in _LABEL_NOISE and _is_valid_ngram(w)]
            if len(labels) < 2:
                labels = [w for w, _ in all_counter.most_common(10)
                          if len(w) >= 2 and w not in _LABEL_NOISE and _is_valid_ngram(w)]
            label = ' '.join(labels[:3]) if labels else ''
        # 去重源
        sources = list(set(a['source'] for a in cl['articles'] if a['source']))
        topics.append({
            'label': label,
            'labels': labels[:3],
            'count': len(cl['articles']),
            'sources': sources[:5],
            'links': [a['link'] for a in cl['articles'][:10]],
            'cats': list(set(a['cat'] for a in cl['articles'])),
        })
    # 按文章数排序，取前 max_topics 个
    topics.sort(key=lambda t: -t['count'])
    return topics[:max_topics]


def _generate_daily_summary(keywords, topics, stats, rising=None):
    """生成每日态势摘要。优先 Agnes AI 结构化 4 板块分析，降级为模板生成。
    返回 dict（结构化）或 str（降级兼容）。"""
    top_kw = [w for w, _ in keywords[:15]]
    top_topics = [t for t in topics[:8] if t.get('labels')]
    topic_desc = '; '.join(
        '%s(%d篇/%d源)' % (t['labels'][0], t['count'], len(t.get('sources', [])))
        for t in top_topics
    )
    rising_desc = ''
    if rising:
        rising_desc = ', '.join('%s(+%d)' % (r['word'], r['rise']) for r in rising[:8])
    # ── 降级方案：模板生成（返回 string，前端按旧格式渲染） ──
    def _template_summary():
        parts = []
        if top_kw:
            parts.append("今日关键词: " + "、".join(top_kw[:8]))
        if top_topics:
            parts.append("热点话题: " + "、".join(t['labels'][0] for t in top_topics if t['labels']))
        total = stats.get('total_articles', 0)
        recent = stats.get('recent_count', 0)
        parts.append("共 %d 篇文章，其中近 24 小时新增 %d 篇" % (total, recent))
        return "。".join(parts)
    # ── Agnes AI 结构化摘要 ──
    if not _AGNES_KEYS:
        return _template_summary()
    system_prompt = (
        "你是一名高级科技情报分析师。你的核心能力是从海量碎片化信息中提炼核心逻辑，"
        "识别被大众忽略的弱信号。\n\n"
        "## 思维模型\n"
        "1. 见微知著：从散点关键词和话题中寻找底层共性叙事（如多个话题共同指向'AI应用落地'）。\n"
        "2. 交叉验证：RSS 专业视角与大众热榜的差异往往隐藏认知套利机会。\n"
        "3. 反直觉思考：当全网叫好时寻找风险，当全网恐慌时寻找机会。拒绝平庸共识。\n\n"
        "## 核心原则\n"
        "1. 直击要害：拒绝'综上所述'、'众所周知'等废话，直接输出结论。\n"
        "2. 逻辑闭环：不仅描述'发生了什么'，必须解释'为什么发生'以及'未来会怎样'。\n"
        "3. 去情绪化：可以分析舆论情绪，但你的分析必须冷静、客观。\n\n"
        "## 数据字段解读\n"
        "- 关键词：按 TF-IDF 评分排序，排名越靠前表示该词在近 24h 文章中越突出。\n"
        "- 升温词：括号内 +N 表示排名上升 N 位，数值越大表示升温越显著。\n"
        "- 话题簇：格式为 标签(文章数/信源数)，文章数越多表示该话题覆盖越广。\n\n"
        "## 输出格式（严格遵守 JSON）\n"
        "以 JSON 格式返回 4 个板块，所有值为纯文本字符串，禁止 Markdown/emoji：\n"
        '{"core_trends": "核心态势(100字内): 一句话定性 + 宏观主线 + 微观佐证",\n'
        ' "signals": "异动信号(80字内): 升温话题 + 异常波动",\n'
        ' "rss_insights": "RSS深度洞察(80字内): 专业视角补充的硬核信息",\n'
        ' "outlook": "研判建议(50字内): 具体建议，禁止空话"}\n\n'
        "## 正确示例\n"
        'core_trends: "Agent工程化进入系统化阶段。OpenAI、Google、Anthropic 同步推出企业级评估服务，'
        '标志从\'能用\'到\'可观测\'的拐点。"\n'
        'signals: "Agent评估服务升温(+15位)、LangSmith Trace Analytics 发布、CrewAI 集成 OpenTelemetry"\n'
        'outlook: "Agent基础设施竞争加剧，关注评估标准统一化进程"'
    )
    user_prompt = (
        "请分析以下 RSS 聚合数据：\n\n"
        "## 数据概览\n"
        "- 文章总数: %d, 近24h新增: %d\n"
        "- 信源数: %d\n\n"
        "## Top 关键词\n%s\n\n"
        "## 升温词\n%s\n\n"
        "## 热点话题\n%s\n\n"
        "请撰写分析报告，以 JSON 格式返回 4 个板块。"
        % (stats.get('total_articles', 0), stats.get('recent_count', 0),
           stats.get('source_count', 0),
           "、".join(top_kw[:12]),
           rising_desc or "暂无升温词数据",
           topic_desc or "暂无话题数据")
    )
    payload = json.dumps({
        "model": "agnes-2.5-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 600,
        "temperature": 0.3,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://apihub.agnes-ai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _AGNES_KEYS[_AGNES_KEY_IDX],
            "User-Agent": "starhub-auto-update",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
        cand = (data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
        if cand and len(cand) > 30:
            # 尝试提取 JSON（AI 可能在 JSON 前后加文字）
            parsed = _extract_json_from_text(cand)
            if parsed and isinstance(parsed, dict):
                # 验证至少有一个板块非空
                valid_keys = ['core_trends', 'signals', 'rss_insights', 'outlook']
                if any(parsed.get(k) for k in valid_keys):
                    total_chars = sum(len(str(parsed.get(k, ''))) for k in valid_keys)
                    print("[分析] AI 结构化摘要生成成功 (%d 字, %d 板块)" % (
                        total_chars, sum(1 for k in valid_keys if parsed.get(k))))
                    return parsed
            # JSON 解析失败但文本有效，尝试作为单段摘要（旧格式兼容）
            print("[分析] AI 返回非 JSON，作为单段摘要 (%d 字)" % len(cand))
            return cand
    except Exception as e:
        print("[分析] AI 摘要失败: %s，降级为模板" % e, file=sys.stderr)
    return _template_summary()


def _extract_json_from_text(text):
    """从 AI 返回文本中提取 JSON 对象。支持 markdown 代码块包裹和纯 JSON。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # 尝试提取 ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*\n?(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    # 尝试提取第一个 { ... } 块
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _load_prev_analysis():
    """加载前一天的分析快照。返回 dict；损坏/非 dict（rev9 P3①）返回 None。"""
    if not os.path.exists(ANALYSIS_SNAPSHOT_FILE):
        return None
    try:
        with open(ANALYSIS_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_analysis_snapshot(analysis_data):
    """保存分析快照到 analysis_snapshot.json。"""
    try:
        _atomic_write_json(ANALYSIS_SNAPSHOT_FILE, analysis_data, ensure_ascii=False, indent=1)
        print("[分析] 保存分析快照 → %s" % ANALYSIS_SNAPSHOT_FILE)
    except Exception as e:
        print("[分析] 保存失败: %s" % e, file=sys.stderr)


def _save_source_quality(quality_data):
    """保存信源质量评分到 source_quality.json。"""
    try:
        _atomic_write_json(SOURCE_QUALITY_FILE, quality_data, ensure_ascii=False, indent=1)
        print("[分析] 保存信源评分 → %s (%d 个源)" % (SOURCE_QUALITY_FILE, len(quality_data)))
    except Exception as e:
        print("[分析] 信源评分保存失败: %s" % e, file=sys.stderr)


def _log_insight_tracking(analysis_data, mode="incremental"):
    """将洞察板块全量追踪数据追加写入 insight_tracking_history.jsonl。

    覆盖 RAGAS 评分、洞察文本、关键词、话题、统计指标与构建元信息，
    确保「日志所见即用户所见」。14 天滚动窗口，原子写入，异常安全。
    """
    try:
        now_bj = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        ragas = analysis_data.get("_ragas_eval", {})
        summary = analysis_data.get("summary", {})
        stats = analysis_data.get("stats", {})
        meta = analysis_data.get("meta", {})
        topic_clusters = analysis_data.get("topic_clusters", [])
        kw_global = analysis_data.get("keywords", {}).get("global", [])

        entry = {
            "ts": now_bj.isoformat(),
            "mode": mode,
            "overall": ragas.get("overall"),
            "context_coverage": ragas.get("context_coverage"),
            "faithfulness": ragas.get("faithfulness"),
            "relevance": ragas.get("relevance"),
            "feedback": ragas.get("feedback", ""),
            "iterations": ragas.get("iterations"),
            "threshold": ragas.get("threshold"),
            "passed": (ragas.get("overall", 0) >= ragas.get("threshold", 0.6))
                      if ragas.get("overall") is not None else None,
            "top_keywords": [w for w, _ in kw_global[:10]],
            "topic_labels": [c.get("label", "") for c in topic_clusters[:15]],
            "narrative": (analysis_data.get("narrative") or "")[:500],
            "core_trends": (summary.get("core_trends") or "")[:500],
            "rss_insights": (summary.get("rss_insights") or "")[:500],
            "outlook": (summary.get("outlook") or "")[:500],
            "stats": {
                "total_articles": stats.get("total_articles", 0),
                "recent_count": stats.get("recent_count", 0),
                "source_count": stats.get("source_count", 0),
            },
            "meta": {
                "llm_provider": meta.get("llm_provider", ""),
                "embed_model": meta.get("embed_model", ""),
                "elapsed_seconds": meta.get("elapsed_seconds", 0),
            },
            "stale": analysis_data.get("stale", False),
        }

        # 加载现有记录
        lines = []
        if os.path.exists(INSIGHT_TRACKING_FILE):
            try:
                with open(INSIGHT_TRACKING_FILE, "r", encoding="utf-8") as f:
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
        _atomic_write_text(INSIGHT_TRACKING_FILE, "".join(lines))
        print("[分析] 洞察追踪日志已记录 → %s (保留 %d 条)"
              % (INSIGHT_TRACKING_FILE, len(lines)))
    except Exception as e:
        print("[分析] 洞察追踪日志失败: %s" % e, file=sys.stderr)


# ── 热榜轨迹追踪 ──

def _accumulate_hot_history(hot_snapshot, now_bj):
    """追加热榜快照到历史轨迹文件。保留 7 天数据，自动清理过期条目。
    hot_snapshot 格式: [{platform, name, items: [{rank, title, url, hot}]}]"""
    # 加载已有历史
    history = {}
    if os.path.exists(HOT_HISTORY_FILE):
        try:
            with open(HOT_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {}
    ts = now_bj.isoformat()
    cutoff_7d = (now_bj - datetime.timedelta(days=7)).isoformat()
    for plat_data in hot_snapshot:
        plat = plat_data.get("platform", "")
        if not plat:
            continue
        if plat not in history:
            history[plat] = []
        # 追加本次快照条目
        for item in plat_data.get("items", []):
            title = item.get("title", "")
            if not title:
                continue
            history[plat].append({
                "title": title,
                "rank": item.get("rank", 0),
                "ts": ts,
            })
        # 清理超过 7 天的旧数据
        history[plat] = [e for e in history[plat] if e["ts"] >= cutoff_7d]
    try:
        _atomic_write_json(HOT_HISTORY_FILE, history, ensure_ascii=False, separators=(",", ":"))
        total = sum(len(v) for v in history.values())
        print("[热榜] 轨迹累积: %d 平台, %d 条记录" % (len(history), total))
    except Exception as e:
        print("[热榜] 轨迹写入失败: %s" % e, file=sys.stderr)
    return history


def _compute_hot_trends(hot_history):
    """从热榜历史中计算趋势标记。
    返回 {platform: {title: {trend, prev_rank, curr_rank, rise, duration}}}"""
    if not hot_history:
        return {}
    result = {}
    for plat, entries in hot_history.items():
        if not entries:
            continue
        # 按时间戳分组（每次快照为一个周期）
        periods = collections.OrderedDict()
        for e in entries:
            ts = e["ts"][:13]  # 截断到小时精度（同一批次的 ts 相同）
            if ts not in periods:
                periods[ts] = {}
            periods[ts][e["title"]] = e["rank"]
        if len(periods) < 1:
            continue
        period_keys = sorted(periods.keys())  # 按时间升序排列
        latest = period_keys[-1]
        prev = period_keys[-2] if len(periods) >= 2 else None
        latest_items = periods[latest]
        prev_items = periods[prev] if prev else {}
        plat_trends = {}
        for title, rank in latest_items.items():
            prev_rank = prev_items.get(title)
            if prev_rank is None:
                trend = "new"
                rise = 0
            else:
                diff = prev_rank - rank  # 正数=上升，负数=下降
                if diff >= 5:
                    trend = "rising"
                elif diff <= -5:
                    trend = "falling"
                else:
                    trend = "stable"
                rise = diff
            # 计算在榜持续时长（周期数）
            duration = 0
            for pk in reversed(period_keys):
                if title in periods[pk]:
                    duration += 1
                else:
                    break
            plat_trends[title] = {
                "trend": trend,
                "curr_rank": rank,
                "prev_rank": prev_rank,
                "rise": rise,
                "duration": duration,
            }
        # 检测 "gone" 状态：任何非最新周期中存在、但最新周期中不存在、且脱榜超 2 个周期的条目
        all_prev_titles = {}
        for pk in period_keys[:-1]:  # 除最新周期外的所有周期
            for title, rank in periods[pk].items():
                if title not in latest_items:
                    all_prev_titles[title] = rank  # 保留最后出现的排名
        for gone_title, gone_rank in all_prev_titles.items():
            # 计算脱榜周期数（从最新周期往前数，不在榜的连续周期数）
            absent_count = 0
            for pk in reversed(period_keys):
                if gone_title not in periods[pk]:
                    absent_count += 1
                else:
                    break
            if absent_count > 2:
                plat_trends[gone_title] = {
                    "trend": "gone",
                    "curr_rank": None,
                    "prev_rank": gone_rank,
                    "rise": 0,
                    "duration": 0,
                }
        result[plat] = plat_trends
    return result


def _cross_platform_topics(hot_history, hot_snapshot):
    """检测跨平台共振话题：同一话题在 2+ 平台同时上榜。
    返回 [{label, platforms: [{name, rank}]}]"""
    if not hot_snapshot or len(hot_snapshot) < 2:
        return []
    # 收集各平台最新快照的标题集合
    plat_titles = {}
    for plat_data in hot_snapshot:
        plat = plat_data.get("platform", "")
        titles = []
        for item in plat_data.get("items", []):
            t = item.get("title", "")
            if t:
                titles.append({"title": t, "rank": item.get("rank", 99)})
        if titles:
            plat_titles[plat] = titles
    if len(plat_titles) < 2:
        return []
    # 两两比较平台间的标题相似度
    plat_names = list(plat_titles.keys())
    # 为每个标题建立 token 集合
    title_tokens = {}
    for plat, titles in plat_titles.items():
        for td in titles:
            key = plat + ":" + td["title"]
            title_tokens[key] = set(_tokenize(td["title"]))
    # 跨平台匹配
    matched = []  # [(label, [(plat, rank), ...])]
    used = set()  # 已匹配的标题 key
    for i in range(len(plat_names)):
        for j in range(i + 1, len(plat_names)):
            p1, p2 = plat_names[i], plat_names[j]
            for t1 in plat_titles[p1]:
                k1 = p1 + ":" + t1["title"]
                if k1 in used:
                    continue
                for t2 in plat_titles[p2]:
                    k2 = p2 + ":" + t2["title"]
                    if k2 in used:
                        continue
                    # Jaccard 相似度
                    tok1 = title_tokens.get(k1, set())
                    tok2 = title_tokens.get(k2, set())
                    if not tok1 or not tok2:
                        continue
                    inter = len(tok1 & tok2)
                    union = len(tok1 | tok2)
                    sim = inter / union if union > 0 else 0
                    if sim >= 0.3:
                        # 匹配成功，检查是否已有该话题
                        label = t1["title"] if len(t1["title"]) <= len(t2["title"]) else t2["title"]
                        found = False
                        for m in matched:
                            if m["label"] == label:
                                # 追加平台
                                existing_plats = {p["name"] for p in m["platforms"]}
                                if p1 not in existing_plats:
                                    m["platforms"].append({"name": p1, "rank": t1["rank"]})
                                if p2 not in existing_plats:
                                    m["platforms"].append({"name": p2, "rank": t2["rank"]})
                                found = True
                                break
                        if not found:
                            matched.append({
                                "label": label,
                                "platforms": [
                                    {"name": p1, "rank": t1["rank"]},
                                    {"name": p2, "rank": t2["rank"]},
                                ],
                            })
                        used.add(k1)
                        used.add(k2)
                        break  # t1 已匹配，跳出内层
    # 按平台数降序，过滤仅 1 个平台的
    matched = [m for m in matched if len(m["platforms"]) >= 2]
    matched.sort(key=lambda m: -len(m["platforms"]))
    # 添加 platform_count 字段
    for m in matched:
        m["platform_count"] = len(m["platforms"])
    return matched[:15]


# ── RSS 内容趋势轨迹追踪 ──

def _accumulate_rss_trend_history(analysis_data, now_bj):
    """追加当前构建的关键词/话题快照到 RSS 趋势历史。保留 14 天。"""
    history = {"snapshots": []}
    if os.path.exists(RSS_TREND_HISTORY_FILE):
        try:
            with open(RSS_TREND_HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {"snapshots": []}
    # 提取当前快照的关键词排名 dict
    kw_rank = {}
    kw_data = analysis_data.get("keywords", {})
    for i, (w, _) in enumerate(kw_data.get("global", [])):
        kw_rank[w] = i + 1
    # 提取分类关键词
    by_cat = {}
    for cat, kws in kw_data.get("by_cat", {}).items():
        by_cat[cat] = [w for w, _ in kws[:20]]
    # 提取话题摘要
    topics_summary = []
    for t in analysis_data.get("topics", []):
        topics_summary.append({
            "label": t.get("label", ""),
            "count": t.get("count", 0),
            "sources": len(t.get("sources", [])),
        })
    # 追加新快照（D4：记录不可信日期源名单，供趋势/关键词降权消费）
    snapshot = {
        "ts": now_bj.isoformat(),
        "keywords": kw_rank,
        "topics": topics_summary,
        "by_cat": by_cat,
        "bad_date_sources": sorted(_LAST_UNRELIABLE_SRCS),
        # D3 轻量场：本快照的语义主题为沿用值，消费方可据此识别平台期
        "stale": bool(analysis_data.get("stale")),
    }
    history["snapshots"].append(snapshot)
    # 清理超过 14 天的旧快照
    cutoff_14d = (now_bj - datetime.timedelta(days=14)).isoformat()
    history["snapshots"] = [s for s in history["snapshots"] if s["ts"] >= cutoff_14d]
    try:
        _atomic_write_json(RSS_TREND_HISTORY_FILE, history, ensure_ascii=False, separators=(",", ":"))
        print("[分析] RSS 趋势历史累积: %d 个快照" % len(history["snapshots"]))
    except Exception as e:
        print("[分析] RSS 趋势历史写入失败: %s" % e, file=sys.stderr)
    return history


def _compute_rss_trajectories(trend_history):
    """从 RSS 趋势历史中计算关键词和话题的跨周期轨迹。
    返回 {keywords: {word: {lifecycle, first_seen, latest_rank, duration, rank_history}},
          topics: {label: {lifecycle, count_history, peak_count, duration}}}"""
    snapshots = trend_history.get("snapshots", [])
    if len(snapshots) < 2:
        return {"keywords": {}, "topics": {}}
    # ── 关键词轨迹 ──
    kw_trajectories = {}
    # 收集所有出现过的关键词
    all_keywords = set()
    for s in snapshots:
        all_keywords.update(s.get("keywords", {}).keys())
    for kw in all_keywords:
        # 构建排名序列（None = 未出现）
        rank_seq = []
        first_seen = None
        for s in snapshots:
            rank = s.get("keywords", {}).get(kw)
            rank_seq.append(rank)
            if rank is not None and first_seen is None:
                first_seen = s["ts"]
        latest_rank = rank_seq[-1]
        # 计算连续出现周期数
        duration = 0
        for r in reversed(rank_seq):
            if r is not None:
                duration += 1
            else:
                break
        # 计算消失周期数
        absent = 0
        for r in reversed(rank_seq):
            if r is None:
                absent += 1
            else:
                break
        # 生命周期标记
        if latest_rank is None and absent >= 3:
            lifecycle = "gone"
        elif duration <= 2 and first_seen == snapshots[-1].get("ts"):
            lifecycle = "emergent"
        else:
            # 检查排名趋势（仅看有值的周期）
            valid_ranks = [(i, r) for i, r in enumerate(rank_seq) if r is not None]
            if len(valid_ranks) >= 3:
                recent = valid_ranks[-3:]
                rises = sum(1 for j in range(1, len(recent)) if recent[j][1] < recent[j-1][1])
                falls = sum(1 for j in range(1, len(recent)) if recent[j][1] > recent[j-1][1])
                if rises >= 2:
                    lifecycle = "rising"
                elif falls >= 2:
                    lifecycle = "declining"
                elif latest_rank is not None and latest_rank <= 10:
                    lifecycle = "peaking"
                else:
                    lifecycle = "stable"
            elif len(valid_ranks) == 2:
                if valid_ranks[-1][1] < valid_ranks[-2][1]:
                    lifecycle = "rising"
                elif valid_ranks[-1][1] > valid_ranks[-2][1]:
                    lifecycle = "declining"
                else:
                    lifecycle = "stable"
            else:
                lifecycle = "stable"
        kw_trajectories[kw] = {
            "lifecycle": lifecycle,
            "first_seen": first_seen or "",
            "latest_rank": latest_rank,
            "duration": duration,
        }
    # ── 话题轨迹 ──
    topic_trajectories = {}
    # 用 Jaccard 相似度匹配跨快照的同一话题
    latest_topics = snapshots[-1].get("topics", [])
    if not latest_topics:
        return {"keywords": kw_trajectories, "topics": {}}
    latest_token_sets = {}
    for t in latest_topics:
        label = t.get("label", "")
        latest_token_sets[label] = set(_tokenize(label))
    for t in latest_topics:
        label = t.get("label", "")
        tok_set = latest_token_sets.get(label, set())
        count_seq = []
        matched_label = label
        for s in snapshots:
            best_count = 0
            best_match = ""
            for ht in s.get("topics", []):
                ht_label = ht.get("label", "")
                if not ht_label:
                    continue
                ht_tokens = set(_tokenize(ht_label))
                if not tok_set or not ht_tokens:
                    continue
                inter = len(tok_set & ht_tokens)
                union = len(tok_set | ht_tokens)
                sim = inter / union if union > 0 else 0
                if sim >= 0.3 and ht.get("count", 0) > best_count:
                    best_count = ht["count"]
                    best_match = ht_label
            count_seq.append(best_count if best_match else 0)
            if best_match:
                matched_label = best_match
        latest_count = count_seq[-1] if count_seq else 0
        # 话题生命周期
        valid_counts = [c for c in count_seq if c > 0]
        if not valid_counts:
            lifecycle = "cold"
        elif len(valid_counts) <= 1:
            lifecycle = "emerging"
        else:
            recent_counts = valid_counts[-3:] if len(valid_counts) >= 3 else valid_counts
            if latest_count > 10 and all(recent_counts[i] <= recent_counts[i+1] for i in range(len(recent_counts)-1)):
                lifecycle = "hot"
            elif len(recent_counts) >= 2 and all(recent_counts[i] >= recent_counts[i+1] for i in range(len(recent_counts)-1)):
                lifecycle = "cooling"
            elif latest_count < 3:
                lifecycle = "cold"
            else:
                lifecycle = "emerging"
        topic_trajectories[matched_label] = {
            "lifecycle": lifecycle,
            "latest_count": latest_count,
            "peak_count": max(valid_counts) if valid_counts else 0,
            "duration": len(valid_counts),
        }
    return {"keywords": kw_trajectories, "topics": topic_trajectories}


def _compute_cross_category_topics(kw_data):
    """检测同一关键词在多个 RSS 分类中同时热门。
    返回 [{keyword, categories: [cat, ...], category_count}]"""
    by_cat = kw_data.get("by_cat", {})
    if len(by_cat) < 2:
        return []
    # 收集每个分类的 Top 20 关键词
    cat_kw_sets = {}
    for cat, kws in by_cat.items():
        cat_kw_sets[cat] = set(w for w, _ in kws[:20])
    # 统计每个关键词出现在多少个分类中
    kw_cats = collections.defaultdict(list)
    for cat, kw_set in cat_kw_sets.items():
        for kw in kw_set:
            kw_cats[kw].append(cat)
    # 过滤仅出现在 1 个分类的，按分类数降序
    cross = []
    for kw, cats in kw_cats.items():
        if len(cats) >= 2:
            cross.append({
                "keyword": kw,
                "categories": sorted(cats),
                "category_count": len(cats),
            })
    cross.sort(key=lambda x: -x["category_count"])
    return cross[:20]


def _run_analysis(sources_with_items, now_bj, hot_snapshot=None, hot_history=None, mode="incremental"):
    """执行智能分析流水线。

    D3 轻/重分级：重分析（语义聚类/深度洞察/RAGAS）按 insight_heavy_interval_hours
    （默认 6h；full 模式无条件重跑）间隔运行，轻量场复用上次重字段并标注 stale=true；
    统计字段（关键词/趋势/质量/轨迹）每场照算。insight_engine 不可用时回退统计方法。
    """
    prev_analysis = _load_prev_analysis()
    ie_cfg = {}
    engine_on = False
    try:
        import insight_engine
        ie_cfg = insight_engine.load_config()
        engine_on = bool(ie_cfg.get("insight_engine_enabled", True))
    except Exception:
        engine_on = False
    heavy_due = True
    if engine_on:
        try:
            _interval = float(ie_cfg.get("insight_heavy_interval_hours", 6))
            if not math.isfinite(_interval) or _interval <= 0:
                _interval = 6.0  # NaN 比较恒 False 会锁死轻量模式（rev8 P2-3）
        except (TypeError, ValueError):
            _interval = 6.0
        _prev_gen = (prev_analysis or {}).get("generated_at", "")
        # P1-2 修复（对抗性审查 f168c43）：语义字段（deep_insights）缺失也触发重分析——
        # 重分析失败时统计路径会刷新 generated_at，仅看时间会把退化态锁定一整个间隔
        _semantics_missing = not (prev_analysis or {}).get("deep_insights")
        if not _prev_gen or mode == "full" or _interval <= 0 or _semantics_missing:
            heavy_due = True
        else:
            try:
                _pg = datetime.datetime.fromisoformat(_prev_gen)
                if _pg.tzinfo:
                    _pg = _pg.astimezone(datetime.timezone(datetime.timedelta(hours=8))).replace(tzinfo=None)
                heavy_due = (now_bj.replace(tzinfo=None) - _pg).total_seconds() >= _interval * 3600
            except (TypeError, ValueError):
                heavy_due = True
    if engine_on and heavy_due:
        # ── 重分析：insight_engine (LlamaIndex) ──
        try:
            import insight_engine
            prev_keywords = []
            if prev_analysis and prev_analysis.get("keywords", {}).get("global"):
                prev_keywords = [w for w, _ in prev_analysis["keywords"]["global"][:50]]
            # 读取 Trending 数据供 insight engine 使用
            trending_data = {}
            try:
                with open(TRENDING_SNAPSHOT_FILE, "r", encoding="utf-8") as _f:
                    trending_data = json.load(_f)
            except Exception:
                pass
            ie_result = insight_engine.run_analysis(
                hot_snapshot=hot_snapshot,
                rss_history=_rss_history,
                trending_data=trending_data,
                config=ie_cfg,
                prev_keywords=prev_keywords,
                hot_history=hot_history,
                bad_date_sources=set(_LAST_UNRELIABLE_SRCS),
            )
            if ie_result is not None:
                # 合并原有需要保留的字段（热榜趋势、RSS轨迹等仍由旧方法计算）
                ie_result["hot_trends"] = _compute_hot_trends(hot_history) if hot_history else {}
                trend_history = _accumulate_rss_trend_history(ie_result, now_bj)
                ie_result["rss_trajectories"] = _compute_rss_trajectories(trend_history)
                if not ie_result.get("cross_category"):
                    ie_result["cross_category"] = _compute_cross_category_topics(
                        ie_result.get("keywords", {}))
                quality = _score_sources(sources_with_items, _rss_history)
                _save_source_quality(quality)
                # 保存 RAGAS 评估数据（在 quality 被信源质量分覆盖前）
                _ragas_raw = ie_result.get("quality", {})
                if isinstance(_ragas_raw, dict) and "overall" in _ragas_raw:
                    ie_result["_ragas_eval"] = _ragas_raw
                ie_result["quality"] = {sk: v["score"] for sk, v in quality.items()}
                _save_analysis_snapshot(ie_result)
                _log_insight_tracking(ie_result, mode)
                print("[分析] insight_engine (LlamaIndex) 重分析完成")
                return ie_result
        except ImportError:
            print("[分析] insight_engine 未安装，回退统计方法")
        except Exception as e:
            print("[分析] insight_engine 失败: %s，回退统计方法" % e, file=sys.stderr)
            import traceback; traceback.print_exc()
    elif engine_on:
        print("[分析] 轻量模式：距上次重分析不足间隔，语义主题/深度洞察沿用旧值")
    # ── 统计路径（insight_engine 关闭/失败/轻量场共用）──
    t0 = time.time()
    print("[分析] 开始智能分析...")
    # 1. 信源质量评分
    quality = _score_sources(sources_with_items, _rss_history)
    _save_source_quality(quality)
    # 2. 关键词提取
    kw_data = _extract_keywords(_rss_history, now_bj, _LAST_UNRELIABLE_SRCS)
    global_kw = kw_data["global"]
    # 3. 趋势检测（与上次构建对比）
    prev_kw = prev_analysis.get("keywords", {}).get("global", []) if prev_analysis else []
    rising = []
    if prev_kw:
        prev_freq = {w: i + 1 for i, (w, _) in enumerate(prev_kw[:50])}
        curr_freq = {w: i + 1 for i, (w, _) in enumerate(global_kw[:50])}
        for w, rank in curr_freq.items():
            prev_rank = prev_freq.get(w, 999)
            if prev_rank > rank + 5:  # 排名上升超过 5 位
                rising.append({"word": w, "rise": prev_rank - rank, "current_rank": rank})
        rising.sort(key=lambda x: -x["rise"])
        rising = rising[:15]
    # 4. 话题聚类
    topics = _cluster_topics(_rss_history, now_bj)
    # 5. 统计摘要
    total_articles = len(_rss_history)
    stats = {
        'total_articles': total_articles,
        'recent_count': kw_data['recent_count'],
        'source_count': len(sources_with_items),
    }
    # 6. AI 摘要（传入升温词数据以增强分析深度）
    summary = _generate_daily_summary(global_kw, topics, stats, rising=rising)
    # 7. 热榜趋势标记
    hot_trends = _compute_hot_trends(hot_history) if hot_history else {}
    # 8. 跨平台共振检测
    cross_platform = _cross_platform_topics(hot_history, hot_snapshot) if hot_snapshot else []
    # 组装结果
    analysis = {
        'generated_at': now_bj.isoformat(),
        'keywords': {
            'global': [(w, round(s, 2)) for w, s in global_kw[:50]],
            'by_cat': {cat: [(w, round(s, 2)) for w, s in kws[:20]] for cat, kws in kw_data['by_cat'].items()},
        },
        'rising': rising,
        'topics': topics,
        'summary': summary,
        'stats': stats,
        'quality': {sk: v['score'] for sk, v in quality.items()},
        'hot_trends': hot_trends,
        'cross_platform': cross_platform,
    }
    # D3 轻量场：语义主题/深度洞察/RAGAS 等重字段沿用上次重分析，诚实标注 stale；
    # generated_at 保持上次重分析时间（前端据此展示真实新鲜度）
    # P1-2 修复：overlay 无条件复用——重分析失败（insight_engine 抛异常）时统计路径
    # 也要沿用上次的语义主题/深度洞察，并诚实标注 stale，防止退化态无痕。
    # rev8 P1-2：语义主题/簇结构/深度洞察/RAGAS 显式沿用 prev（统计聚类的 topics 键
    # 恒存在会顶掉语义主题——spec D3 承诺 light 场主题数据不回退）
    if prev_analysis:
        for _k in ("topics", "topic_clusters", "deep_insights", "ragas",
                   "narrative", "cross_platform_semantic"):
            if prev_analysis.get(_k) is not None:
                analysis[_k] = prev_analysis[_k]
        for _k, _v in prev_analysis.items():
            if _k not in analysis and _k != "bad_date_sources":
                analysis[_k] = _v
        analysis["stale"] = True
        analysis["generated_at"] = prev_analysis.get("generated_at") or analysis["generated_at"]
    _save_analysis_snapshot(analysis)
    # 9. RSS 内容趋势历史累积（在 analysis_snapshot 保存后，以便读取当前关键词数据）
    trend_history = _accumulate_rss_trend_history(analysis, now_bj)
    # 10. RSS 关键词/话题轨迹计算
    rss_trajectories = _compute_rss_trajectories(trend_history)
    analysis['rss_trajectories'] = rss_trajectories
    # 11. 跨分类话题检测
    cross_category = _compute_cross_category_topics(kw_data)
    analysis['cross_category'] = cross_category
    # 重新保存（含轨迹和跨分类数据）
    _save_analysis_snapshot(analysis)
    # 轻量模式：从旧分析中提取 RAGAS 评估数据
    if prev_analysis and "_ragas_eval" not in analysis:
        _prev_q = prev_analysis.get("quality", {})
        if isinstance(_prev_q, dict) and "overall" in _prev_q:
            analysis["_ragas_eval"] = _prev_q
    _log_insight_tracking(analysis, mode)
    elapsed = round(time.time() - t0, 1)
    n_kw_traj = len(rss_trajectories.get('keywords', {}))
    n_topic_traj = len(rss_trajectories.get('topics', {}))
    print("[分析] 完成: %d 个关键词, %d 个话题, %d 个升温词, %d 个跨平台话题, "
          "%d 个关键词轨迹, %d 个话题轨迹, %d 个跨分类话题, 耗时 %.1fs" % (
        len(global_kw), len(topics), len(rising), len(cross_platform),
        n_kw_traj, n_topic_traj, len(cross_category), elapsed))
    return analysis


# ──────────────────────────── Main ────────────────────────────

def main(mode="full"):
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
    failed_count = 0
    _source_log = []  # 每源抓取结果
    _build_start = time.time()

    # 并行抓取 RSS（短超时，失败快速跳过）
    # - 全局并发 12；同一域名并发 2（对单域行为接近串行，避免打爆 xgo.ing 等桥接服务）
    # - 域级熔断：同域名连续 3 次「上游侧失败」则本轮跳过该域剩余源；成功但 0 条不计入。
    #   注意它是尽力而为的限流、不是硬上限：熔断只在任务尚未开工时生效，已排在域名
    #   信号量上的那批（≤12）照样会发出；且任何一次成功都会把连续计数清零，所以
    #   "域内部分失败"的混合场景不会触发 —— 只有整域在失败才会。
    # - 流水线：某源抓完即在主线程串行翻译，与其余源的网络 IO 重叠；翻译不进线程池（翻译服务有限流）
    _results = [None] * len(RSS_SOURCES)  # 按 RSS_SOURCES 顺序回填，保持产物顺序稳定

    # 每场构建都抓全部源：跳过会让被跳源的内容完全依赖存档跨构建存活，而存档正是
    # 2026-09-17 故障里断掉的那一环。RSS 也没有增量协议，省下的只是请求数。
    _to_fetch = list(enumerate(RSS_SOURCES))

    # 域名信号量与熔断状态全部在提交任务前于主线程建好，规避运行期并发初始化竞争
    def _src_domain(url):
        m = re.match(r"https?://([^/]+)", url or "")
        return m.group(1) if m else ""

    _domain_sems = {}
    for src in RSS_SOURCES:
        _d = _src_domain(src.get("url", ""))
        if _d and _d not in _domain_sems:
            _domain_sems[_d] = threading.Semaphore(2)
    _domain_lock = threading.Lock()
    _domain_failstreak = collections.defaultdict(int)  # 域名 → 连续失败数
    _domain_broken = set()  # 本轮已熔断域名
    _domain_rl = set()      # 本轮命中 429 的域名（限流冷却，与熔断互不影响）

    def _worker(i, src):
        """只做网络 IO 与域名熔断判定；计数与翻译由主线程汇总"""
        dom = _src_domain(src.get("url", ""))
        with _domain_lock:
        # 限流复查只做一次，且必须放在取到域名额之后（见下面那段）：
        # 入口这次在变异体检里被证明是死分支（删掉后 17 条判据全绿）——
        # 域内名额只有 2 个、排队发生在信号量内侧，入口拦不住任何已排队的 worker，
        # 留着只会让人以为冷却有两道防线。
            if dom and dom in _domain_broken:
                return i, src, None, "domain_broken"
        crashed = False
        try:
            if dom:
                with _domain_sems[dom]:
                    # 取到名额之后再查一次限流：入口那次检查挡不住已经在排队的 worker
                    # （生产里 972 源排 12 线程、域内名额只有 2 个，
                    #  第一次 429 落地时同域后面的 worker 大多还没发出请求，
                    #  但"已过入口检查"的那几个会照发 —— 离线复现里六个源全被抓了）。
                    with _domain_lock:
                        if dom in _domain_rl:
                            return i, src, None, "rate_limited"
                    items = _fetch_rss(src, timeout=src.get("timeout"))
            else:
                items = _fetch_rss(src, timeout=src.get("timeout"))
        except Exception as _ex429:
            # 只认 429（鸭子类型取 code，避免依赖 urllib.error 是否被导入）。
            # 命中即把该域拉入本场冷却：同域剩余源不再发请求，
            # 既停止锤限流器，也不再被"连续 3 次硬失败"的熔断连坐成 domain_broken。
            if getattr(_ex429, "code", None) == 429:
                with _domain_lock:
                    if dom:
                        _domain_rl.add(dom)
                print("[RSS聚合] %s 命中 429 限流，本域 %s 剩余源本场不再请求"
                      % (src["name"], dom), file=sys.stderr)
                return i, src, None, "rate_limited"
            _ex = _ex429
            # 我方清洗/去重链路抛错：请求本身已发出并成功，不该牵连同域名兄弟源，
            # 但必须留痕 —— 静默吞成「0 条」让 2026-09-16 起的 84 个源无声消失了三天。
            print("[RSS聚合] %s 抓取异常: %s: %s"
                  % (src["name"], type(_ex).__name__, _ex), file=sys.stderr)
            items, crashed = [], True
        hard_fail = items is None
        ok = bool(items)
        with _domain_lock:
            if ok:
                _domain_failstreak[dom] = 0
            elif hard_fail and dom:
                # 只有上游侧失败（网络/HTTP/XML 不可解析）才计入域名熔断。
                # 「成功但 feed 0 条」是正常状态：计入会让 api.xgo.ing 这种一个域名挂
                # 160 个源的桥接服务，被前 3 个空 feed 熔断掉其余全部源，
                # 而提交顺序固定 ⇒ 尾部源每场都轮不到（实测 0.00 次/场 × 18 场）。
                _domain_failstreak[dom] += 1
                if _domain_failstreak[dom] >= 3:
                    _domain_broken.add(dom)
        if ok:
            _status = "ok"
        elif hard_fail or crashed:
            _status = "error"   # 没拿到数据且不是「上游本来就空」，与 empty 分开才查得动
        else:
            _status = "empty"
        return i, src, items, _status

    # 翻译线程池：有界并发（6 源并发），不拖住抓取主循环；Agnes 429/全链熔断时自动降级
    _trans_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
    _trans_futs = []
    _pool = concurrent.futures.ThreadPoolExecutor(max_workers=12)
    try:
        _futs = {_pool.submit(_worker, i, src): (i, src) for i, src in _to_fetch}
        for _fut in concurrent.futures.as_completed(_futs):
            i, src = _futs[_fut]
            try:
                _, src, items, status = _fut.result()
            except Exception as _ex:
                # 单个 worker 意外异常不拖垮整场构建：该源按失败处理，其余继续
                print("[RSS聚合] %s worker 异常: %s" % (src["name"], _ex), file=sys.stderr)
                items, status = [], "empty"
            key = src["key"]
            tier = src.get("tier", 3)
            n = len(items) if items else 0
            if status == "ok":
                ok_count += 1
                # Fix 4: T4 源连续成功后自动升级到 T3
                if tier == 4:
                    _tier4_success_streak[key] = _tier4_success_streak.get(key, 0) + 1
                    if _tier4_success_streak[key] >= _TIER_PROMOTION_THRESHOLD:
                        src["tier"] = 3
                        print("[T4升级] %s 连续 %d 次成功 → 升级到 T3" % (src["name"], _tier4_success_streak[key]))
                        del _tier4_success_streak[key]
                else:
                    _tier4_success_streak[key] = 0  # 非 T4 源重置连续计数
                # 翻译交给独立线程池有界并发；完成顺序不影响最终产物顺序
                if items:
                    _trans_futs.append(_trans_pool.submit(_translate_source_items, items))
            else:
                failed_count += 1
                if items is None:
                    items = []
            _results[i] = {
                "key": key, "name": src["name"], "cat": src["cat"],
                "color": src.get("color", "#6366f1"), "url": src.get("url", ""),
                "items": items,
                "tier": tier,
            }
            total_items += n
            print("[RSS聚合] %s: %d 条%s" % (src["name"], n, "（域熔断跳过）" if status == "domain_broken" else ""))
            _source_log.append({"key": key, "name": src["name"], "cat": src["cat"], "tier": tier, "status": status, "items": n})
    except BaseException:
        # 中断（Ctrl+C/任务取消）时丢弃排队任务立即退出，避免 shutdown(wait=True) 等完上千个待抓源
        _pool.shutdown(wait=False, cancel_futures=True)
        _trans_pool.shutdown(wait=False, cancel_futures=True)
        raise
    _pool.shutdown(wait=True)
    # 等待全部源级翻译完成（下游历史累积/渲染依赖 title_zh）
    for _tf in _trans_futs:
        try:
            _tf.result()
        except Exception as _ex:
            print("[翻译] 条目翻译任务异常: %s" % _ex, file=sys.stderr)
    _trans_pool.shutdown(wait=True)

    sources_with_items.extend(r for r in _results if r is not None)


    if total_items == 0 and mode == "full":
        print("[RSS聚合] 所有源均失败，尝试使用历史数据", file=sys.stderr)

    # 累积到 72 小时历史，用累积数据替换当次抓取
    _history_before = len(_rss_history)
    # Fix 3: 保存抓取阶段数字，避免被历史重组覆写导致日志口径不一致
    _items_fetched = total_items
    _sources_with_data_before = sum(1 for s in sources_with_items if s.get("items"))
    sources_with_items, total_items = _accumulate_history(sources_with_items)
    _history_after = len(_rss_history)

    # ── 历史回流图片补升级（历史缓存中的旧 URL 未经 _upgrade_img_url）──
    _hist_upgraded = 0
    for _src in sources_with_items:
        for _it in _src.get("items", []):
            _old_img = _it.get("image", "")
            if _old_img:
                _new_img = _upgrade_img_url(_old_img)
                if _new_img != _old_img:
                    _it["image"] = _new_img
                    # 判空（→ 空串）不算"升级"，它由下面的封面计数单独播报
                    if _new_img:
                        _hist_upgraded += 1
    if _hist_upgraded:
        print("[图片升级] 历史缓存补升级 %d 张" % _hist_upgraded)
    # 历史缓存里躺着的必挂封面同样清掉，免得每一场都重新命中一次
    _hist_purged = _purge_bad_covers_in_history(_rss_history)
    # 正文侧的同批清扫：老条目的 full_content 是按入库那一刻的规则出厂的，不重写就要等
    # 它被重新抓取（或自然过期）才换过来。理由与落点与上一行同一条（见函数 docstring 的实测口径）。
    _hist_normed = _renormalize_history_fulltext(_rss_history)
    # 对抗审查 ②：上一行只改 `_rss_history`，而 `_accumulate_history` 早在 :909 就用
    # `entry = dict(item)` 把历史命中抄成了**出厂副本** —— 三个消费者
    # （`_save_api_snapshot` / `build_html` / `write_data_chunks`）读的是副本，
    # 所以清扫必须**也在副本上再跑一遍**，否则出厂形状仍是入库那一刻的。
    # 落点与写法照本文件 :9517-9529 那条 image 补升级（同一格道理，既有做法）。
    _out_normed = _renormalize_outgoing_fulltext(sources_with_items)
    # 清扫分账必须**无条件**播报：掉到 0 有两种解释（上游换了形状 / 某把刀被摘了），
    # 两种都需要有人知道，而"只在有命中时才打印"恰好把这一信号吞掉了
    # （与 `_bad_cover_hits` 的判空播报同一条理由）。
    print("[历史清扫] 重写 %d 条（正文规范化 %d / 导航脱链 %d；摘要 HN 模板 %d / 粘连裸链 %d）"
          % (_hist_normed, _LAST_HISTORY_SWEEP["fulltext"], _LAST_HISTORY_SWEEP["body_delink"],
             _LAST_HISTORY_SWEEP["summary_hn"], _LAST_HISTORY_SWEEP["summary_glued"]))
    # 出厂那一本另播（对抗审查 ②）：三个消费者读的是这一份，历史上这一格根本没播过，
    # 所以"清扫只改了存盘那份"这件事在构建日志里是完全静默的。
    print("[出厂清扫] 重写 %d 条（正文规范化 %d / 导航脱链 %d；摘要 HN 模板 %d / 粘连裸链 %d）"
          % (_out_normed, _LAST_OUTGOING_SWEEP["fulltext"],
             _LAST_OUTGOING_SWEEP["body_delink"], _LAST_OUTGOING_SWEEP["summary_hn"],
             _LAST_OUTGOING_SWEEP["summary_glued"]))
    # 判空必须可观测：0 命中说明上游换了域名或这张表已经过期，两种都需要有人知道
    if _bad_cover_hits:
        _bc_top = sorted(_bad_cover_hits.items(), key=lambda kv: -kv[1])
        print("[封面判空] 丢弃 %d 张必挂封面（其中历史缓存清理 %d 张；同批历史正文重写 %d 条）：%s" % (
            sum(_bad_cover_hits.values()), _hist_purged, _hist_normed,
            "、".join("%s×%d" % (_h, _n) for _h, _n in _bc_top)))
    else:
        print("[封面判空] 0 张命中（表内 %d 个域名本场均未出现；同批历史正文重写 %d 条）"
              % (len(_BAD_COVER_HOSTS), _hist_normed), file=sys.stderr)

    # ── 剥离云存储签名参数（防止 GitHub Push Protection 拦截推送）──
    # 在快照/历史/数据分块写入前统一清洗，一处覆盖全部输出路径
    _oss_stripped = 0
    for _src in sources_with_items:
        for _it in _src.get("items", []):
            for _k in ("link", "summary", "summary_zh", "full_content", "image", "media_url"):
                _old_v = _it.get(_k)
                if _old_v:
                    _new_v = _strip_oss_signature(_old_v)
                    if _new_v != _old_v:
                        _it[_k] = _new_v
                        _oss_stripped += 1
    # _rss_history 在 _accumulate_history 中创建了独立 dict 副本，需单独清洗
    for _link, _hit in _rss_history.items():
        for _k in ("link", "summary", "summary_zh", "full_content", "image", "media_url"):
            _old_v = _hit.get(_k)
            if _old_v:
                _new_v = _strip_oss_signature(_old_v)
                if _new_v != _old_v:
                    _hit[_k] = _new_v
                    _oss_stripped += 1
    if _oss_stripped:
        print("[签名剥离] 已剥离 %d 处云存储签名参数" % _oss_stripped)

    # ── 图片质量审计（自动发现低分辨率缩略图）──
    try:
        _audit_image_quality(sources_with_items)
    except Exception as e:
        print("[图片审计] 审计失败，跳过: %s" % e, file=sys.stderr)

    # ── 智能分析流水线（关键词 / 话题 / AI 摘要 / 信源评分 / 热榜轨迹 / 跨平台） ──
    analysis_data = None
    hot_snapshot = None
    hot_history = {}
    if ANALYSIS_ENABLED:
        try:
            now_bj = _now_bj()
            # 抓取热榜快照（在分析前获取，以便轨迹累积和跨平台关联）
            try:
                hot_snapshot = fetch_newsnow_snapshot()
                try:
                    _atomic_write_json(HOT_SNAPSHOT_FILE, hot_snapshot, ensure_ascii=False, separators=(",", ":"))
                except Exception as e:
                    print("[热榜] 快照写入失败: %s" % e, file=sys.stderr)
            except Exception as e:
                print("[热榜] 快照抓取失败: %s" % e, file=sys.stderr)
                hot_snapshot = []
            # 累积热榜历史轨迹（始终执行，确保 hot_history.json 被创建）
            hot_history = _accumulate_hot_history(hot_snapshot or [], now_bj)
            analysis_data = _run_analysis(sources_with_items, now_bj,
                                          hot_snapshot=hot_snapshot, hot_history=hot_history,
                                          mode=mode)
        except Exception as e:
            print("[分析] 智能分析失败，跳过: %s" % e, file=sys.stderr)
            import traceback; traceback.print_exc()

    # 若分析未启用，仍需抓取热榜快照
    if not ANALYSIS_ENABLED:
        hot_snapshot = fetch_newsnow_snapshot()
        try:
            _atomic_write_json(HOT_SNAPSHOT_FILE, hot_snapshot, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            print("[热榜] 快照写入失败: %s" % e, file=sys.stderr)

    # ── 为每篇文章提取话题标签（供前端 diverse/topic 模式消费）──
    try:
        _tag_articles(sources_with_items)
        _tagged = sum(1 for s in sources_with_items for it in s.get("items", []) if it.get("tags"))
        print("[标签] 已为 %d 篇文章提取话题标签" % _tagged)
    except Exception as e:
        print("[标签] 提取失败，跳过: %s" % e, file=sys.stderr)

    # 生成 API 快照（供 /api/rss 直接返回，避免实时抓取丢失历史累积数据）
    # 必须晚于 _tag_articles()：快照构造只搬用白名单字段，早于打标则 tags 恒为空。
    # 仍早于 build_html / write_data_chunks，保证产物写出异常时快照已落地。
    _save_api_snapshot(sources_with_items)

    html_doc = build_html(sources_with_items, build_time, total_items, build_ts_ms, analysis_data=analysis_data,
                          diverse_window_minutes=DIVERSE_CFG["window_minutes"],
                          diverse_enabled=DIVERSE_CFG["enabled"])
    _atomic_write_text(OUT, html_doc)

    # 数据分块：rss-data-0.js（首屏）/ rss-data-1.js（全量，后台合并）
    try:
        write_data_chunks(sources_with_items)
    except Exception as e:
        print("[数据分块] 写出失败: %s" % e, file=sys.stderr)

    # 生成 rss_sources.json（供 /api/rss 使用）；整体回写保留 timeout 等扩展字段
    for s in RSS_SOURCES:
        s.setdefault("tier", 3)
    _atomic_write_json("rss_sources.json", RSS_SOURCES, ensure_ascii=False, separators=(",", ":"))

    _snapshot_items = sum(len(s.get("items", [])) for s in sources_with_items)
    print("[RSS聚合] 生成完成 → %s（%d 源成功，抓取 %d 篇，快照 %d 篇）" % (OUT, ok_count, _items_fetched, _snapshot_items))

    # 打印翻译统计
    print("[翻译统计] 缓存命中: %d, Agnes: %d, Zen: %d, Google: %d, MyMemory: %d, Dict: %d, 跳过: %d, 失败: %d" % (
        _TRANS_STATS["cache_hit"], _TRANS_STATS["agnes"], _TRANS_STATS["zen"], _TRANS_STATS["google"], _TRANS_STATS["mymemory"],
        _TRANS_STATS["dict"], _TRANS_STATS["skip"], _TRANS_STATS["fail"]
    ))

    # 保存缓存
    _save_caches()
    _save_history()

    # ── 写入构建日志 ──
    _build_duration = round(time.time() - _build_start, 1)
    build_logger.append({
        "type": "build",
        "mode": mode,
        "duration_s": _build_duration,
        "sources_total": len(RSS_SOURCES),
        "sources_fetched": ok_count,
        "sources_with_data": _sources_with_data_before,
        "sources_failed": failed_count,
        "items_fetched": _items_fetched,
        "items_snapshot": _snapshot_items,
        # 闸门**之后**的口径。这三个键不是锦上添花，而是 2026-09-20 对抗审查 P0-1 的对策：
        # sources_with_data / per_source 全是抓取阶段的数，留存闸门砍掉约四分之一内容时
        # 构建日志纹丝不动 —— 塌陷只在产物里可见，等于给回归装了个没有观测面的开关。
        # 取 None 而不是 0 兜底：闸门没跑到（早退/异常路径）与"闸门把内容全裁光"必须可区分，
        # 否则日志里两种故障长得一模一样。
        "sources_after_gate": _LAST_RETENTION_STATS.get("sources_after"),
        "sources_empty_after_gate": (
            len(_LAST_RETENTION_STATS["emptied"]) if _LAST_RETENTION_STATS else None),
        # 同一个统计的**名单面**：计数只能告诉我"塌了 256 个"，问"哪 256 个、各自断供多久"
        # 就必须回头手工探针或翻 stderr（stderr 只打前 10 个，且每场被覆盖）。
        # 空表 [] = 这场没有一个源被清空；None = 闸门没跑到，两者必须是两个样子（沿用上面那条规矩）。
        "sources_empty_detail": (
            _LAST_RETENTION_STATS.get("emptied_detail") if _LAST_RETENTION_STATS else None),
        "items_after_gate": _LAST_RETENTION_STATS.get("after"),
        # 出厂龄期三段也落日志：跨构建趋势不用回去 grep stdout
        "items_le_base_h": _LAST_RETENTION_STATS.get("le72"),
        "items_base_to_hard_h": _LAST_RETENTION_STATS.get("mid"),
        "items_over_hard_h": _LAST_RETENTION_STATS.get("over"),
        "history_before": _history_before,
        "history_after": _history_after,
        # 两条账互斥，且都不是首尾差：首尾差被每场新增的条目抵成负数（生产实测全天恒负），
        # "过期删了多少"这本账必须由裁剪循环自己报数。
        "history_expired": _LAST_HISTORY_ACCOUNT.get("expired"),
        # 「不许 0 兜底」这条规矩留着：字段为 None = 这一步没跑到，
        # 与"跑到了但是 0"必须是两个不同的样子（§12 那笔全天恒负的账就是这么漏的）。
        "trans_cache_hit": _TRANS_STATS.get("cache_hit", 0),
        # 轮转的观测面：条数与本轮 LRU 丢弃数。为 None = `_save_trans_cache` 没跑到或抛异常，
        # 与"跑到了但一条没丢（0）"必须是两个样子 —— 历史侧 §12 那本恒负账就是这么漏掉的。
        "trans_cache_size": _LAST_TRANS_ACCOUNT.get("size"),
        "trans_cache_trimmed": _LAST_TRANS_ACCOUNT.get("trimmed"),
        "trans_google": _TRANS_STATS.get("google", 0),
        # LLM 兜底那两个引擎以前只印在 stdout 上：日志里 google/mymemory/dict 都有，
        # 唯独 agnes/zen 缺席 —— 于是"这场到底有没有走 LLM 兜底"在构建日志里问不出答案。
        "trans_agnes": _TRANS_STATS.get("agnes", 0),
        "trans_zen": _TRANS_STATS.get("zen", 0),
        "trans_mymemory": _TRANS_STATS.get("mymemory", 0),
        "trans_dict": _TRANS_STATS.get("dict", 0),
        "trans_skip": _TRANS_STATS.get("skip", 0),
        "trans_fail": _TRANS_STATS.get("fail", 0),
        "per_source": _source_log,
    })
    print("[日志] 构建日志已写入 build_logs/")

    return True


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    ok = main(mode=mode)
    sys.exit(0 if ok else 1)
