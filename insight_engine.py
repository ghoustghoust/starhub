"""
insight_engine.py — LlamaIndex-powered semantic analysis engine for StarHub.

Replaces the statistical _run_analysis() pipeline with LLM + embedding-based
semantic analysis.  Fully optional: works (with reduced functionality) even
when llama-index / fastembed are not installed.
"""

import hashlib
import json
import math
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ────────────────── Optional LlamaIndex imports ──────────────────
LLAMA_INDEX_AVAILABLE = False
FASTEMBED_AVAILABLE = False

try:
    from llama_index.core import Document, VectorStoreIndex, Settings
    from llama_index.core.node_parser import SentenceSplitter
    LLAMA_INDEX_AVAILABLE = True
except ImportError:
    pass

try:
    from llama_index.embeddings.fastembed import FastEmbedEmbedding  # noqa: F401
    FASTEMBED_AVAILABLE = True
except ImportError:
    pass

BJT = timezone(timedelta(hours=8))

# ────────────────── Embedding model ──────────────────
# 硅基流动 BAAI/bge-m3 API（主力）：8192 token 上下文，1024 维，中英双语
# 本地 fastembed 作为无 API key 时的回退
_SF_EMBED_MODEL = "BAAI/bge-m3"
_SF_EMBED_URL = "https://api.siliconflow.cn/v1/embeddings"
_SF_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
_SF_BATCH = 32  # 每批最多处理文本数
_last_embed_model = None  # 记录最近一次成功的 embedding 模型名

# ── D1 嵌入缓存（spec: docs/superpowers/specs/2026-09-14-insight-optimization-spec.md）──
# 文本 md5 → 向量，按模型名隔离；每次 _get_embeddings 前重读文件（MB 级、每场仅数次调用，
# 可接受），写盘原子替换。仅缓存 SiliconFlow 结果（本地 fastembed 免费快速，不缓存）。
_EMBED_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emb_cache.json")
_EMBED_CACHE_MAX = 5000  # 向量条数上限，超限按插入序淘汰最旧


def _embed_cache_load(model_name):
    """读取嵌入缓存。返回 (cache_model, vectors_dict)；文件缺失/损坏/模型不匹配 → 空缓存。"""
    try:
        with open(_EMBED_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("model") != model_name:
            return model_name, {}
        vectors = data.get("vectors", {})
        if not isinstance(vectors, dict):
            return model_name, {}
        # rev10 P3①：值类型过滤——手改/损坏产生的非 list 值（str/int）不透传调用方
        clean = {k: v for k, v in vectors.items() if isinstance(v, list) and v}
        return model_name, clean
    except Exception:
        return model_name, {}


def _embed_cache_save(model_name, vectors):
    """原子写缓存（tmp + replace，尽力而为：写失败不影响主流程）。"""
    try:
        tmp = "%s.%s.tmp" % (_EMBED_CACHE_FILE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"model": model_name, "vectors": vectors}, f)
        os.replace(tmp, _EMBED_CACHE_FILE)
    except Exception as exc:
        print(f"[insight_engine] embed cache save failed: {exc}", file=sys.stderr)


# ────────────────── Hierarchical chunking (小索引大窗口) ──────────────────
_CHUNK_TOKEN_TARGET = 150    # 子块目标 token 数（约100词/句）
_RETRIEVE_TOP_K = 10         # 检索返回 top-K 子块
_CONTEXT_RADIUS = 3          # 命中块前后各取 N 个句子组（大窗口）

# 本地回退模型（中英文兼容，fastembed 0.8+ / llama-index-embeddings-fastembed 0.7 支持）
_EMBED_MODEL = "intfloat/multilingual-e5-small"          # 多语言，中英双语兼容
_EMBED_MODEL_FALLBACK = "BAAI/bge-small-zh-v1.5"           # 中文回退

# ────────────────── Task 2: Defaults / Config ──────────────────
_DEFAULTS = {
    "insight_engine_enabled": True,
    "insight_llm_provider": "agnes",
    "insight_max_documents": 500,
    "insight_top_keywords": 30,
    "insight_top_topics": 15,
    "insight_self_correct": True,
    "insight_quality_threshold": 0.6,
    "insight_max_corrections": 2,
}


def load_config(build_config_path="build_config.json"):
    """Read build_config.json and fill missing keys with _DEFAULTS."""
    cfg = dict(_DEFAULTS)
    try:
        with open(build_config_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    # ensure all default keys present
    for k, v in _DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


# ────────────────── Task 1: AgnesLLM ──────────────────────────
class AgnesLLM:
    """Thin wrapper around the Agnes AI chat-completions API.
    支持多 API key 轮询，遇到 429 限流自动切换到下一个 key。
    """

    API_URL = "https://apihub.agnes-ai.com/v1/chat/completions"

    def __init__(self, api_key, model="agnes-2.5-flash", timeout=60, extra_keys=None):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_keys = [api_key]
        if extra_keys:
            self.api_keys.extend(k for k in extra_keys if k)
        self._key_idx = 0
        self.model = model
        self.timeout = timeout
        print(f"[AgnesLLM] initialized with {len(self.api_keys)} key(s)", file=sys.stderr)

    def _try_complete(self, prompt, system_prompt, temperature, max_tokens):
        """Single attempt with current key. Returns (result, is_429)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

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
            self.API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"], False
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                print(f"[AgnesLLM] 429 rate-limited on key #{self._key_idx + 1}, rotating...",
                      file=sys.stderr)
                return None, True
            print(f"[AgnesLLM] complete error: {exc}", file=sys.stderr)
            return None, False
        except Exception as exc:
            print(f"[AgnesLLM] complete error: {exc}", file=sys.stderr)
            return None, False

    # -- core completion --
    def complete(self, prompt, system_prompt=None, temperature=0.3, max_tokens=600):
        """Try each key on 429; on timeout/other errors retry same key up to 2x."""
        n_keys = len(self.api_keys)
        for key_attempt in range(n_keys):
            result, is_429 = self._try_complete(prompt, system_prompt, temperature, max_tokens)
            if result:
                return result
            if is_429:
                self._key_idx = (self._key_idx + 1) % n_keys
                continue
            # Non-429 error (timeout etc.) — retry with current key up to 2 more times
            for retry in range(2):
                result, is_429 = self._try_complete(prompt, system_prompt, temperature, max_tokens)
                if result:
                    return result
                if is_429:
                    self._key_idx = (self._key_idx + 1) % n_keys
                    break  # break inner retry, outer loop will try next key
            else:
                break  # exhausted retries on this key without 429
        return ""

    def stream_complete(self, prompt, **kwargs):
        """Yield the full result (non-streaming fallback)."""
        yield self.complete(prompt, **kwargs)

    @property
    def metadata(self):
        return {"model_name": self.model, "context_window": 8192}


# ────────────────── Task 1: MockLLM ──────────────────────────
class MockLLM:
    """Deterministic mock LLM for offline / no-key scenarios."""

    def __init__(self, model="mock"):
        self.model = model

    def complete(self, prompt, system_prompt=None, temperature=0.3, max_tokens=600):
        prompt_lower = prompt.lower()
        # keyword extraction prompt → return JSON array
        if "keyword" in prompt_lower or "关键词" in prompt_lower:
            return json.dumps(["ai", "llm", "openai", "agent", "模型", "发布"])
        # narrative / insight prompt → return structured JSON
        if "insight" in prompt_lower or "洞察" in prompt_lower or "narrative" in prompt_lower:
            return json.dumps({
                "narrative": "当前科技领域以AI和大模型为核心叙事。",
                "causal_chains": ["AI发展→算力需求增长→芯片产业升温"],
                "signals": [{"signal": "AI终端化加速", "confidence": 0.8}],
                "outlook": "短期内AI仍将是信息场主旋律。",
            })
        # default
        return "这是一个模拟响应。"

    def stream_complete(self, prompt, **kwargs):
        yield self.complete(prompt, **kwargs)

    @property
    def metadata(self):
        return {"model_name": self.model, "context_window": 8192}


# ────────────────── Task 2: configure_llm ────────────────────
def configure_llm(config):
    """Create an LLM instance based on config. Falls back to MockLLM."""
    provider = config.get("insight_llm_provider", "agnes")
    if provider == "agnes":
        # 多 key 轮询：AGNES_API_KEY（主，可逗号分隔）+ AGNES_API_KEYS（逗号分隔附加），429 自动切换
        keys = [k.strip() for k in os.environ.get("AGNES_API_KEY", "").split(",") if k.strip()]
        keys += [k.strip() for k in os.environ.get("AGNES_API_KEYS", "").split(",") if k.strip()]
        if keys:
            return AgnesLLM(api_key=keys[0], extra_keys=keys[1:] or None)
        print("[insight_engine] AGNES_API_KEY not set, using MockLLM", file=sys.stderr)
    elif provider != "mock":
        print(f"[insight_engine] Unknown provider '{provider}', using MockLLM", file=sys.stderr)
    return MockLLM()


# ────────────────── Task 3: load_documents ───────────────────
_RSS_HISTORY_HOURS = 72


def _is_entry_recent(item, hours=_RSS_HISTORY_HOURS):
    """判断 RSS 条目是否在时间窗口内。优先 pub_date，回退 first_seen。"""
    now = datetime.now(BJT)
    cutoff = now - timedelta(hours=hours)
    pub_str = item.get("pub_date") or item.get("published") or item.get("date")
    fs_str = item.get("first_seen", "")
    for ts_str in [pub_str, fs_str]:
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BJT)
            if dt >= cutoff:
                return True
        except (ValueError, TypeError):
            continue
    return False  # 两个时间都无法解析，视为过期


def load_documents(hot_snapshot, rss_history, trending_data, max_documents=500):
    """Convert raw data dicts into a list of LlamaIndex Document objects.
    使用配额制确保热榜/Trending/RSS 各类别均有代表，避免高优先级源挤占全部名额。
    RSS 条目过滤 72h 窗口外的过期数据，无 pub_date 时使用 first_seen 基准。
    """
    hot_docs, trend_docs, rss_docs = [], [], []

    # --- Hot items (highest priority) ---
    if hot_snapshot:
        for platform in hot_snapshot:
            plat = platform.get("platform", platform.get("name", "unknown"))
            for item in platform.get("items", []):
                title = item.get("title", "")
                if title:
                    text = f"[热榜/{plat}] {title}"
                    hot_docs.append(text)

    # --- Trending (second priority) ---
    if trending_data:
        items = trending_data.items() if isinstance(trending_data, dict) else []
        for repo, stars in items:
            desc = repo  # repo name as description fallback
            text = f"[Trending] {repo} (+{stars} stars): {desc}"
            trend_docs.append(text)

    # --- RSS items (lowest priority, filtered by 72h window) ---
    if rss_history:
        rss_items = rss_history.values() if isinstance(rss_history, dict) else rss_history
        for item in rss_items:
            if isinstance(item, dict):
                if not _is_entry_recent(item):
                    continue  # 跳过超 72h 的过期条目
                title = item.get("title", "")
                summary = item.get("summary", "")[:300]
                cat = item.get("cat", item.get("category", "rss"))
                text = f"[RSS/{cat}] {title} {summary}".strip()[:500]
                if text.strip():
                    rss_docs.append(text)

    # 配额制：每类分配固定名额，确保话题聚类有 RSS 数据
    # 默认 max_documents=500 时：RSS 350 + 热榜 100 + Trending 50
    # 先按配额截取，未用完的配额回退给其他类别
    rss_quota = max(int(max_documents * 0.7), 10)
    hot_quota = max(int(max_documents * 0.2), 2)
    trend_quota = max(max_documents - rss_quota - hot_quota, 0)

    hot_sel = hot_docs[:hot_quota]
    trend_sel = trend_docs[:trend_quota]
    rss_sel = rss_docs[:rss_quota]

    # 回退：未用完的配额分配给有数据的类别
    remaining = max_documents - len(hot_sel) - len(trend_sel) - len(rss_sel)
    if remaining > 0:
        # 优先补 RSS（话题聚类依赖它）
        extra_rss = rss_docs[len(rss_sel):len(rss_sel) + remaining]
        rss_sel.extend(extra_rss)
        remaining -= len(extra_rss)
    if remaining > 0:
        extra_hot = hot_docs[len(hot_sel):len(hot_sel) + remaining]
        hot_sel.extend(extra_hot)
        remaining -= len(extra_hot)
    if remaining > 0:
        extra_trend = trend_docs[len(trend_sel):len(trend_sel) + remaining]
        trend_sel.extend(extra_trend)

    selected = hot_sel + trend_sel + rss_sel

    if not LLAMA_INDEX_AVAILABLE:
        # Return lightweight stand-in objects
        return [_SimpleDoc(t) for t in selected]

    from llama_index.core import Document as LiDocument
    return [LiDocument(text=t) for t in selected]


class _SimpleDoc:
    """Minimal Document stand-in when llama-index is not installed."""
    def __init__(self, text):
        self.text = text


# ────────────────── Task 3: build_index ──────────────────────
def build_index(documents, embeddings=None, embed_model_name=None):
    """Build an in-memory VectorStoreIndex.
    优先使用预计算的 embeddings（与聚类共享同一向量空间）。
    回退到本地 fastembed。
    """
    if not LLAMA_INDEX_AVAILABLE:
        print("[insight_engine] build_index: llama-index not available", file=sys.stderr)
        return None

    # 诊断日志
    print(f"[insight_engine] build_index: LLAMA_INDEX={LLAMA_INDEX_AVAILABLE}, "
          f"FASTEMBED={FASTEMBED_AVAILABLE}, docs={len(documents)}, "
          f"embeddings={'provided' if embeddings else 'none'}", file=sys.stderr)

    try:
        # 方案 A：使用预计算的 embeddings（与聚类共享 SiliconFlow API 向量空间）
        if embeddings and len(embeddings) == len(documents):
            from llama_index.core import Document as LiDocument
            from llama_index.core.schema import TextNode
            nodes = []
            for i, doc in enumerate(documents):
                text = doc.text if hasattr(doc, 'text') else str(doc)
                node = TextNode(text=text, embedding=embeddings[i])
                nodes.append(node)
            index = VectorStoreIndex(nodes, show_progress=False)
            print(f"[insight_engine] build_index: built from {len(nodes)} pre-computed embeddings "
                  f"({embed_model_name or 'unknown'})", file=sys.stderr)
            return index

        # 方案 B：回退到本地 fastembed
        if not FASTEMBED_AVAILABLE:
            print("[insight_engine] build_index: fastembed not available, cannot build index", file=sys.stderr)
            return None
        model_name = _EMBED_MODEL
        Settings.embed_model = FastEmbedEmbedding(model_name=model_name)
        index = VectorStoreIndex.from_documents(documents, show_progress=False)
        print(f"[insight_engine] build_index: built via local fastembed {model_name}", file=sys.stderr)
        return index
    except Exception as exc:
        print(f"[insight_engine] build_index error: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None


# ────────────────── Hierarchical chunking (小索引大窗口) ──────────────────
def _group_sentences(text, token_target=150):
    """将文本按句子切分，再组合为 ~token_target 大小的语义块。
    返回句子组列表，每组是完整句子的拼接。
    """
    # 中英文句子切分
    sentences = re.split(r'(?<=[。！？.!?\n])\s*', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return []

    groups = []
    current = []
    current_len = 0
    for sent in sentences:
        est = max(len(sent) // 2, 1)  # 粗略 token 估算
        if current_len + est > token_target and current:
            groups.append(' '.join(current))
            current = [sent]
            current_len = est
        else:
            current.append(sent)
            current_len += est
    if current:
        groups.append(' '.join(current))
    return groups


def _build_hierarchical_index(documents, pre_embeddings=None):
    """构建层级向量索引：精细切片(子块) + 父文档映射。

    每个文档被切分为 ~150 token 的子块（小索引），
    子块 metadata 中保存父文档引用，检索时可扩展上下文（大窗口）。

    注意：不用 VectorStoreIndex 检索（因为无法用 SiliconFlow API 做 query embed），
    改用 _retrieve_with_context 中的手动余弦相似度。

    Args:
        documents: 文档列表
        pre_embeddings: 预计算的文档级 embedding（用于回退）

    Returns:
        (index, parent_docs_map, child_vecs) 或 (None, {}, None)
        child_vecs: 子块 embedding 向量列表（用于手动检索）
    """
    if not LLAMA_INDEX_AVAILABLE or not documents:
        return None, {}, None

    try:
        from llama_index.core.schema import TextNode

        # 1) 切分文档为子块，建立 parent→children 映射
        parent_docs = {}   # parent_id → original text
        child_nodes = []   # 子块 TextNode 列表
        child_texts = []   # 子块文本（用于 embedding）

        for doc_idx, doc in enumerate(documents):
            doc_text = doc.text if hasattr(doc, 'text') else str(doc)
            parent_id = f"doc_{doc_idx}"
            parent_docs[parent_id] = doc_text

            groups = _group_sentences(doc_text, _CHUNK_TOKEN_TARGET)
            if not groups:
                continue

            for child_idx, chunk_text in enumerate(groups):
                node = TextNode(
                    text=chunk_text,
                    metadata={"parent_doc_id": parent_id, "child_idx": child_idx},
                )
                child_nodes.append(node)
                child_texts.append(chunk_text)

        if not child_nodes:
            print("[insight_engine] hierarchical index: no child chunks generated", file=sys.stderr)
            return None, {}, None

        print(f"[insight_engine] hierarchical index: {len(documents)} docs → "
              f"{len(child_nodes)} child chunks", file=sys.stderr)

        # 2) 获取子块 embedding（精细切片的关键：每个子块独立向量）
        child_vecs = None
        if pre_embeddings and len(pre_embeddings) == len(documents):
            # 尝试为子块计算独立 embedding（提高检索精度）
            child_vecs, _ = _get_embeddings(child_texts)
            if not child_vecs or len(child_vecs) != len(child_nodes):
                # 回退：复用父文档 embedding（同父块子节点共享向量）
                print("[insight_engine] child embed failed, falling back to parent embeddings", file=sys.stderr)
                child_vecs = []
                for node in child_nodes:
                    pidx = int(node.metadata["parent_doc_id"].split("_")[1])
                    child_vecs.append(pre_embeddings[pidx])

        # 3) 赋值 embedding 并建索引（仅用于存储，不用于检索）
        if child_vecs and len(child_vecs) == len(child_nodes):
            for node, vec in zip(child_nodes, child_vecs):
                node.embedding = vec

        # 设置 Settings.embed_model（VectorStoreIndex 构造时需要，但检索用手动余弦）
        if FASTEMBED_AVAILABLE:
            for mn in (_EMBED_MODEL, _EMBED_MODEL_FALLBACK):
                try:
                    Settings.embed_model = FastEmbedEmbedding(model_name=mn)
                    break
                except Exception:
                    continue

        index = VectorStoreIndex(child_nodes, show_progress=False)
        print(f"[insight_engine] hierarchical index built: {len(child_nodes)} child nodes, "
              f"{len(parent_docs)} parents", file=sys.stderr)
        return index, parent_docs, child_vecs

    except Exception as exc:
        print(f"[insight_engine] _build_hierarchical_index error: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None, {}, None


def _retrieve_with_context(parent_docs, child_vecs, child_nodes, query_vec,
                           top_k=10, context_radius=3):
    """从层级索引检索并扩展上下文窗口（大窗口）。

    使用手动余弦相似度（而非 VectorStoreIndex.as_retriever），
    因为索引向量来自 SiliconFlow API，无法用本地模型做 query embed。

    Args:
        parent_docs: {parent_id: original_text} 映射
        child_vecs: 子块 embedding 向量列表（与 child_nodes 一一对应）
        child_nodes: 子块 TextNode 列表（含 metadata）
        query_vec: 查询文本的 embedding 向量
        top_k: 返回子块数
        context_radius: 命中块前后各取 N 个邻居

    Returns:
        扩展后的上下文文本，或 None
    """
    if not parent_docs or not child_vecs or not query_vec:
        return None

    try:
        # 手动余弦相似度检索
        scores = []
        for i, child_vec in enumerate(child_vecs):
            sim = _cosine_similarity(query_vec, child_vec)
            scores.append((i, sim))
        scores.sort(key=lambda x: -x[1])

        # 按父文档分组，扩展上下文窗口
        context_parts = []
        seen_parents = set()

        for idx, sim in scores[:top_k]:
            node = child_nodes[idx]
            parent_id = node.metadata.get("parent_doc_id")
            child_idx = node.metadata.get("child_idx", 0)

            if parent_id not in parent_docs:
                continue

            parent_text = parent_docs[parent_id]
            groups = _group_sentences(parent_text, _CHUNK_TOKEN_TARGET)

            # 构建上下文窗口：命中块 + 前后各 N 个邻居
            start = max(0, child_idx - context_radius)
            end = min(len(groups), child_idx + context_radius + 1)

            if parent_id not in seen_parents:
                context_parts.append("\n".join(groups[start:end]))
                seen_parents.add(parent_id)

        return "\n---\n".join(context_parts) if context_parts else None

    except Exception as exc:
        print(f"[insight_engine] _retrieve_with_context error: {exc}", file=sys.stderr)
        return None


def _get_embeddings(texts):
    """获取 embedding 向量：硅基流动 API → 本地 fastembed → None。
    返回 (vectors, model_name) 元组。
    """
    global _last_embed_model
    if not texts:
        return None, None

    # 1) 硅基流动 BAAI/bge-m3 API（8192 token，1024 维，中英双语）
    #    D1 缓存：命中的文本零 API，只对未命中部分保持批量调用；返回顺序=输入顺序
    if _SF_KEY:
        _cache_model, _cache_vectors = _embed_cache_load(_SF_EMBED_MODEL)
        _vecs_out = [None] * len(texts)
        _missing_pos, _missing = [], []
        for _i, _t in enumerate(texts):
            _k = hashlib.md5(_t.encode("utf-8")).hexdigest()
            _v = _cache_vectors.get(_k)
            if _v is not None:
                _vecs_out[_i] = _v
            else:
                _missing_pos.append(_i)
                _missing.append(_t)
        all_vecs = []
        ok = True
        for i in range(0, len(_missing), _SF_BATCH):
            batch = _missing[i:i + _SF_BATCH]
            payload = json.dumps({
                "model": _SF_EMBED_MODEL,
                "input": batch,
            }).encode("utf-8")
            req = urllib.request.Request(
                _SF_EMBED_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {_SF_KEY}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                batch_vecs = [d["embedding"] for d in data.get("data", [])]
                all_vecs.extend(batch_vecs)
            except Exception as exc:
                print(f"[insight_engine] SiliconFlow embed batch error: {exc}", file=sys.stderr)
                ok = False
                break
        _dims_ok = True
        if ok and len(all_vecs) == len(_missing) and all_vecs:
            # P2-2 维度护栏 v3（rev10 处方）：_cached_dims 取整个缓存文件——
            # 纯未命中调用（如 cross_platform 每场全新的热榜标题）也能检测漂移；
            # 检出漂移即失效缓存文件（自愈，防粘性降级），本轮走 fastembed 全量重算
            _dims = {len(v) for v in all_vecs}
            _cached_dims = {len(v) for v in _cache_vectors.values() if isinstance(v, list)}
            if len(_dims) > 1 or (_cached_dims and _dims != _cached_dims):
                print(f"[insight_engine] 维度漂移检测 {_dims}/缓存{_cached_dims}，失效缓存并弃用本轮嵌入", file=sys.stderr)
                try:
                    os.remove(_EMBED_CACHE_FILE)
                except OSError:
                    pass
                _dims_ok = False
        if ok and _dims_ok and len(all_vecs) == len(_missing):
            for _pos, _i in enumerate(_missing_pos):
                _vecs_out[_i] = all_vecs[_pos]
                _cache_vectors[hashlib.md5(_missing[_pos].encode("utf-8")).hexdigest()] = all_vecs[_pos]
            while len(_cache_vectors) > _EMBED_CACHE_MAX:
                _cache_vectors.pop(next(iter(_cache_vectors)))
            if _missing:  # P3-2：全命中零新数据时跳过大文件重写
                _embed_cache_save(_SF_EMBED_MODEL, _cache_vectors)
            if all(v is not None for v in _vecs_out):
                _last_embed_model = f"siliconflow/{_SF_EMBED_MODEL}"
                print(f"[insight_engine] embeddings via SiliconFlow {_SF_EMBED_MODEL} ({len(texts)} texts, {len(_missing)} new)", file=sys.stderr)
                return _vecs_out, _last_embed_model
        if not ok:
            print(f"[insight_engine] SiliconFlow API failed, falling back to local fastembed", file=sys.stderr)

    # 2) 本地 fastembed 回退（先试主模型，失败试回退模型）
    if FASTEMBED_AVAILABLE:
        for model_name in (_EMBED_MODEL, _EMBED_MODEL_FALLBACK):
            try:
                Settings.embed_model = FastEmbedEmbedding(model_name=model_name)
                vecs = Settings.embed_model.get_text_embedding_batch(texts)
                if vecs and len(vecs) == len(texts):
                    _last_embed_model = model_name
                    print(f"[insight_engine] embeddings via local {model_name} ({len(texts)} texts)", file=sys.stderr)
                    return vecs, _last_embed_model
            except Exception as exc:
                print(f"[insight_engine] local embed error ({model_name}): {exc}", file=sys.stderr)

    return None, None


# ────────────────── Task 4: Helpers ──────────────────────────
def _try_parse_json(text):
    """Try to parse JSON from text; return None on failure."""
    if not text:
        return None
    # strip markdown code fences
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _cosine_similarity(a, b):
    """Cosine similarity between two vectors (lists of floats)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _average_vector(vecs):
    """Element-wise average of a list of vectors."""
    if not vecs:
        return []
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(dim)]


def _extract_cluster_label(texts, all_doc_texts=None):
    """从簇内文本中提取语义标签。
    策略：去前缀 → 最长公共子串（保证标签是标题中的完整片段）→ n-gram 回退。
    标签必须从原始标题中完整召回，确保语义清晰。
    """
    # Step 1: 去前缀 + RSS 模板尾部 + 结构化括号 + 工具后缀
    cleaned = []
    for t in texts:
        t = t.strip()
        t = re.sub(r'^\[(?:RSS|\u70ed\u699c|Trending)/[^\]]*\]\s*', '', t)
        # 去除 【xxx】 结构化括号前缀（如【喷嚏图卦20260901】）
        t = re.sub(r'^\u3010[^\u3011]*\u3011\s*', '', t)
        # 去除工具/平台后缀（如 -notebooklm）
        t = re.sub(r'\s*-\s*[a-z]+(?:lm|ai|bot|app)$', '', t, flags=re.IGNORECASE)
        # 去除知乎/阅读模板文本
        t = re.sub(r'^[\u67e5\u770b\u70b9\u51fb]*\u77e5\u4e4e[\u539f\u6587]*\s*', '', t)
        t = re.sub(r'[\u67e5\u770b\u70b9\u51fb]?\u77e5\u4e4e[\u539f\u6587]?\u00b7?\s*$', '', t)
        t = re.sub(r'\u9605\u8bfb[\u539f\u6587]+.*$', '', t)
        t = re.sub(r'^\u67e5\u770b\u539f\u6587\s*', '', t)
        if len(t) >= 4:
            cleaned.append(t)
    if not cleaned:
        return max(texts, key=len)[:60] if texts else ''
    n = len(cleaned)

    # Step 2: 构建全局 IDF（用于过滤通用词）
    global_df = {}
    if all_doc_texts:
        for doc in all_doc_texts:
            doc_cn = re.sub(r'[^\u4e00-\u9fff]', '', doc)
            seen = set()
            for ng_len in (4, 3, 2):
                for start in range(len(doc_cn) - ng_len + 1):
                    ng = doc_cn[start:start + ng_len]
                    if ng not in seen:
                        global_df[ng] = global_df.get(ng, 0) + 1
                        seen.add(ng)
    total_docs = max(len(all_doc_texts), n) if all_doc_texts else n

    # Step 3: 停用词/字
    _STOP_CHARS = set('\u7684\u4e86\u662f\u5728\u6211\u6709\u548c\u5c31\u4e0d\u90fd\u4e00\u4e2a\u4e5f\u4e0a\u8fd9\u5230\u8bf4\u4eec\u4e3a\u5bf9\u88ab\u628a\u8ba9\u7ed9\u7528\u4ece\u5411\u5982\u53ef\u4ee5\u80fd\u4f1a\u5df2\u7ecf\u8fd8\u5f88\u592a\u90a3\u4ed6\u5979\u5b83\u4e48\u5427\u5417\u5462\u554a\u54c8\u54df\u5566\u5457\u561b\u563f\u561f\u561c\u5616\u5618\u561a\u5619\u561b\u55d2\u55d3\u55d4\u55d5\u55d6\u55d7\u55d8\u55d9\u55da\u55db\u55dc\u55dd\u55de\u55df\u55e0\u55e1\u55e2\u55e3\u55e4\u55e5\u55e6\u55e7\u55e8\u55e9\u55ea\u55eb\u55ec\u55ed\u55ee\u55ef\u55f0\u55f1\u55f2\u55f3\u55f4\u55f5\u55f6\u55f7\u55f8\u55f9\u55fa\u55fb\u55fc\u55fd\u55fe\u55ff')
    _STOP_NGRAMS = {
        '需要','应该','可能','可以','这个','那个','什么','怎么','为什么',
        '因为','所以','但是','虽然','如果','已经','还是','或者','而且',
        '不是','没有','知道','觉得','感觉','希望','想要','开始',
        '进行','通过','使用','实现','问题','情况','方面','部分','结果',
        '分钟','时间','时候','地方','东西','样子','方法','方式','系统',
        '技术','世界','国家','社会','公司','学校','企业','市场','用户',
        '数据','网络','平台','工作','生活','文化','历史','未来','现在',
        '今天','昨天','明天','今年','去年','自己','别人','人们',
        '查看','知乎','阅读','原文','点击','链接','分享','关注',
        '订阅','评论','回复','转载','编辑','推荐','更多','相关',
        '搜索','登录','注册','首页','频道','专栏','话题','标签',
        '看知','乎原','事情','音频','声音','内容','感觉','意思',
        '正式','发布','开源','团队','技术','分享','系列','博客',
        '查看','原文','任务','工作','中的','中最','中最',
    }

    # 提取 【...】 括号内的中文内容（栏目名等关键信息）
    bracket_names = []
    for t in texts:
        m = re.match(r'(?:\[(?:RSS|\u70ed\u699c|Trending)/[^\]]*\]\s*)?\u3010([^\u3011]+)\u3011', t)
        if m:
            cn_name = re.sub(r'[^\u4e00-\u9fff]', '', m.group(1))
            if len(cn_name) >= 3:
                bracket_names.append(cn_name)

    # ── Tier 0: 括号名直接匹配（栏目名是最佳标签）──
    if bracket_names:
        from collections import Counter
        bn_count = Counter(bracket_names)
        for bn, bn_cnt in bn_count.most_common():
            if bn_cnt >= max(2, n * 0.3):
                return bn

    # ── Tier 1: 最长公共子串（Longest Common Substring）──
    # 从原始标题中提取完整片段，保证语义完整可读
    cn_titles = [re.sub(r'[^\u4e00-\u9fff]', '', t) for t in cleaned]
    cn_titles = [c for c in cn_titles if len(c) >= 3]
    # LCS 候选池 = 原始标题 + 括号名（去重）
    lcs_pool = list(dict.fromkeys(cn_titles + bracket_names))
    # 覆盖率检查池（包含括号名，确保栏目名能被正确匹配）
    cov_pool = cn_titles + bracket_names

    def _has_stop_ngram(s):
        """检查子串是否包含任意停用 n-gram（2字起）。"""
        if s in _STOP_NGRAMS:
            return True
        for i in range(len(s) - 1):
            if s[i:i+2] in _STOP_NGRAMS:
                return True
        return False

    if len(cn_titles) >= 2:
        best_lcs = ''
        min_cov = max(2, int(n * 0.3))  # 至少 30% 标题包含
        # 尝试多个基准标题（不只第一个），找到最优 LCS
        bases = sorted(set(lcs_pool), key=len, reverse=True)[:5]
        for base in bases:
            for sub_len in range(min(10, len(base)), 2, -1):  # 最长10字，从长到短
                if sub_len <= len(best_lcs):
                    break  # 不可能找到更长的
                found = False
                for start in range(len(base) - sub_len + 1):
                    sub = base[start:start + sub_len]
                    # 跳过含停用字的或含停用ngram的
                    if any(c in _STOP_CHARS for c in sub):
                        continue
                    if _has_stop_ngram(sub):
                        continue
                    # 检查覆盖率（对原始标题 + 括号名计算）
                    cov = sum(1 for ct in cov_pool if sub in ct)
                    if cov >= min_cov:
                        # 检查 IDF（过滤太常见的子串）
                        df = global_df.get(sub, 0)
                        idf = math.log((total_docs + 1) / (df + 1)) + 1
                        if idf > 1.5:  # 有一定区分度
                            if len(sub) > len(best_lcs):
                                best_lcs = sub
                                found = True
                if found and len(best_lcs) >= 4:
                    break  # 找到足够长的公共子串，停止
            if len(best_lcs) >= 4:
                break  # 已找到好标签
        if len(best_lcs) >= 3:
            return best_lcs

    # ── Tier 2: 中文 n-gram（3-4 gram IDF 加权）──
    cn_items = [c for c in cn_titles if len(c) >= 2]
    if cn_items:
        best_label = ''
        best_score = 0.0
        for ng_len in (4, 3):
            for t in cn_items:
                for start in range(len(t) - ng_len + 1):
                    sub = t[start:start + ng_len]
                    if any(c in _STOP_CHARS for c in sub):
                        continue
                    if _has_stop_ngram(sub):
                        continue
                    cnt = sum(1 for ct in cov_pool if sub in ct)
                    if cnt >= max(2, n * 0.3):
                        tf = cnt / n
                        df = global_df.get(sub, 0)
                        idf = math.log((total_docs + 1) / (df + 1)) + 1
                        score = tf * idf * (1 + 0.2 * (ng_len - 2))
                        if score > best_score:
                            best_label, best_score = sub, score
        if best_label:
            return best_label

    # ── Tier 3: 英文高频词 ──
    _STOP_WORDS = {'the','a','an','is','are','was','were','be','been','being',
                   'have','has','had','do','does','did','will','would','could',
                   'should','may','might','can','shall','to','of','in','for',
                   'on','with','at','by','from','as','into','through','during',
                   'before','after','above','below','between','out','off','over',
                   'under','again','further','then','once','here','there','when',
                   'where','why','how','all','both','each','few','more','most',
                   'other','some','such','no','nor','not','only','own','same',
                   'so','than','too','very','just','because','but','and','or',
                   'if','while','about','up','it','its','i','me','my','we','our',
                   'you','your','he','him','his','she','her','they','them','this','that','these'}
    words_list = [re.findall(r'[a-zA-Z]{3,}', t) for t in cleaned]
    if words_list and any(words_list):
        word_score = {}
        for wl in words_list:
            seen = set()
            for w in wl:
                wl_lower = w.lower()
                if wl_lower in _STOP_WORDS or wl_lower in seen:
                    continue
                seen.add(wl_lower)
                word_score[wl_lower] = word_score.get(wl_lower, 0) + 1
        if word_score:
            best_word = max(word_score, key=lambda w: (word_score[w], len(w)))
            if word_score[best_word] >= max(2, n * 0.2):
                return best_word

    # ── Tier 4: 回退（最短标题截断，最多20字）──
    return max(cleaned, key=len)[:20]


def _fallback_cluster(texts, max_topics=15, threshold=0.5, all_doc_texts=None):
    """Character-overlap based clustering when embeddings are unavailable.
    阈值从 0.3 提升到 0.5，避免中文常用字导致误聚类。
    使用锚点比较（而非贪婪链接）防止链式聚类。
    """
    _MAX_CLUSTER = max(5, len(texts) // 8)  # 单簇上限，防止巨型簇
    clusters = []
    used = set()
    for i, t in enumerate(texts):
        if i in used:
            continue
        cluster = [t]
        used.add(i)
        anchor_chars = set(t.lower())  # 锚点：种子文本的字符集
        for j in range(i + 1, len(texts)):
            if j in used:
                continue
            if len(cluster) >= _MAX_CLUSTER:
                break
            tj = set(texts[j].lower())
            # 与锚点比较（而非最后一个簇成员），防止链式聚类
            overlap = len(anchor_chars & tj) / max(len(anchor_chars | tj), 1)
            if overlap >= threshold:
                cluster.append(texts[j])
                used.add(j)
        if len(cluster) >= 2:
            label = _extract_cluster_label(cluster, all_doc_texts)
            clusters.append({"label": label, "count": len(cluster), "items": cluster})
    clusters.sort(key=lambda c: -c["count"])
    return clusters[:max_topics]


# ────────────────── Task 4: extract_keywords_llm ─────────────
def _tfidf_keywords(texts, top_n=30):
    """TF-IDF fallback keyword extraction (no LLM needed).
    中文用 2-gram，英文用整词，去除停用词后按 TF 降序取 top_n。
    """
    import re
    from collections import Counter
    # 中文停用 2-gram + 英文停用词
    _STOP_2G = {'的', '了', '是', '在', '和', '与', '及', '等', '为', '也',
                '不', '就', '都', '而', '但', '从', '到', '对', '中', '上',
                '下', '有', '被', '把', '让', '向', '往', '以', '于', '其',
                '个', '后', '前', '新', '更', '最', '已', '将', '能', '可',
                # 日期/时间碎片
                '月日', '年月', '日年月', '时分', '秒分',
                # 无意义组合
                '地址', '网址', '链接', '点击', '查看', '阅读', '原文'}
    _STOP_EN = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
                'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
                'can', 'could', 'may', 'might', 'shall', 'should', 'must',
                'not', 'no', 'but', 'and', 'or', 'if', 'then', 'than',
                'for', 'to', 'of', 'in', 'on', 'at', 'by', 'with', 'from',
                'as', 'into', 'about', 'this', 'that', 'it', 'its',
                'via', 'items', 'item', 'news', 'post', 'posts'}

    def _is_valid_keyword(w):
        """过滤含空格、特殊字符或无意义片段的关键词。"""
        if not w or len(w) < 2:
            return False
        # 含空格 → 拒绝（如 "月 日"、"址  "）
        if ' ' in w or '\u3000' in w:
            return False
        # 含特殊符号 → 拒绝（如 "文 ·"、"评论:"）
        if re.search(r'[·:：.,，;；!?！？()（）\[\]【】]', w):
            return False
        # 纯数字 → 拒绝
        if w.isdigit():
            return False
        return True

    counter = Counter()
    for text in texts[:200]:
        # Chinese 2-grams（只取连续中文字符）
        cn_fragments = re.findall(r'[\u4e00-\u9fff]+', text)
        for frag in cn_fragments:
            for i in range(len(frag) - 1):
                gram = frag[i] + frag[i + 1]
                if gram not in _STOP_2G:
                    counter[gram] += 1
        # English words
        en_words = re.findall(r'[a-zA-Z]{3,}', text)
        for w in en_words:
            wl = w.lower()
            if wl not in _STOP_EN:
                counter[wl] += 1
    return [w for w, _ in counter.most_common(top_n) if _is_valid_keyword(w)]


def extract_keywords_llm(llm, texts, top_n=30, max_retries=2):
    """Use LLM to extract keywords from a combined text sample.
    失败时自动重试；全部失败后回退到 TF-IDF。
    """
    if not texts:
        return []
    combined = "\n".join(texts[:100])
    prompt = (
        f"请从以下文本中提取最重要的 {top_n} 个关键词，"
        "以 JSON 数组格式返回（只返回数组，不要其他文字）：\n\n"
        f"{combined[:4000]}"
    )
    # LLM 提取（带重试）
    for attempt in range(max_retries + 1):
        result = llm.complete(prompt)
        if not result:
            print(f"[insight_engine] keyword LLM attempt {attempt+1} returned empty, retrying...",
                  file=sys.stderr)
            continue
        parsed = _try_parse_json(result)
        if isinstance(parsed, list) and len(parsed) > 0:
            return [str(k) for k in parsed[:top_n]]
        # fallback: line-split
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        keywords = []
        for line in lines:
            clean = line.strip("-•· ").strip()
            if clean and len(clean) < 50:
                keywords.append(clean)
        if keywords:
            return keywords[:top_n]
    # 全部 LLM 尝试失败 → TF-IDF fallback
    print("[insight_engine] keyword LLM all attempts failed, using TF-IDF fallback",
          file=sys.stderr)
    return _tfidf_keywords(texts, top_n)


# ────────────────── Task 4: cluster_topics_embedding ─────────
def cluster_topics_embedding(articles, max_topics=15, similarity_threshold=0.55,
                            all_doc_texts=None, pre_embeddings=None, embed_model_name=None):
    """Embedding-based greedy clustering. Falls back to char-overlap.
    支持传入预计算的 embedding 向量，避免重复调 API。
    """
    if not articles:
        return []
    texts = [a if isinstance(a, str) else a.get("text", str(a)) for a in articles]

    # 使用预计算向量，或实时获取
    if pre_embeddings and len(pre_embeddings) == len(articles):
        embeddings = pre_embeddings
        embed_model_name = embed_model_name or "pre-computed"
    else:
        embeddings, embed_model_name = _get_embeddings(texts)

    if embeddings and len(embeddings) == len(articles):
        print(f"[insight_engine] clustering with embeddings: {embed_model_name} ({len(texts)} texts)", file=sys.stderr)
        return _cluster_with_embeddings(articles, embeddings, max_topics, similarity_threshold, all_doc_texts)
    # fallback
    print(f"[insight_engine] clustering with char-overlap fallback (embeddings unavailable)", file=sys.stderr)
    return _fallback_cluster(texts, max_topics, all_doc_texts=all_doc_texts)


def _cluster_with_embeddings(articles, embeddings, max_topics, threshold, all_doc_texts=None):
    """Greedy clustering by cosine similarity on embeddings.
    使用锚点比较 + 单簇上限防止巨型簇。
    """
    n = len(articles)
    used = [False] * n
    _MAX_CLUSTER = max(5, n // 8)  # 单簇上限
    clusters = []
    for i in range(n):
        if used[i]:
            continue
        cluster_indices = [i]
        used[i] = True
        vec_i = embeddings[i]  # 锚点向量
        for j in range(i + 1, n):
            if used[j]:
                continue
            if len(cluster_indices) >= _MAX_CLUSTER:
                break
            sim = _cosine_similarity(vec_i, embeddings[j])
            if sim >= threshold:
                cluster_indices.append(j)
                used[j] = True
        if len(cluster_indices) >= 2:
            texts = [articles[k] if isinstance(articles[k], str) else articles[k].get("text", "") for k in cluster_indices]
            label = _extract_cluster_label(texts, all_doc_texts)
            clusters.append({"label": label, "count": len(texts), "items": texts})
    clusters.sort(key=lambda c: -c["count"])
    # 标签去重：合并相同标签的簇
    merged = {}
    for c in clusters:
        lbl = c["label"]
        if lbl in merged:
            merged[lbl]["items"].extend(c["items"])
            merged[lbl]["count"] = len(merged[lbl]["items"])
        else:
            merged[lbl] = {"label": lbl, "count": c["count"], "items": list(c["items"])}
    clusters = sorted(merged.values(), key=lambda c: -c["count"])
    return clusters[:max_topics]


# ────────────────── Task 4: cross_platform_semantic ──────────
def cross_platform_semantic(hot_snapshot, similarity_threshold=0.75):
    """Embedding-based cross-platform topic matching.
    Returns [] if embeddings unavailable (caller falls back to Jaccard).
    """
    if not hot_snapshot:
        return []
    try:
        # Collect titles per platform
        platform_titles = {}
        for platform in hot_snapshot:
            plat = platform.get("platform", platform.get("name", "unknown"))
            titles = [item.get("title", "") for item in platform.get("items", []) if item.get("title")]
            if titles:
                platform_titles[plat] = titles

        platforms = list(platform_titles.keys())
        if len(platforms) < 2:
            return []

        # Get all embeddings via unified helper
        all_titles = []
        title_platform = []
        for plat in platforms:
            for t in platform_titles[plat]:
                all_titles.append(t)
                title_platform.append(plat)

        vecs, _ = _get_embeddings(all_titles)
        if not vecs or len(vecs) != len(all_titles):
            return []

        # Find cross-platform matches
        matches = []
        seen = set()
        for i in range(len(all_titles)):
            for j in range(i + 1, len(all_titles)):
                if title_platform[i] == title_platform[j]:
                    continue
                sim = _cosine_similarity(vecs[i], vecs[j])
                if sim >= similarity_threshold:
                    key = (all_titles[i][:30], all_titles[j][:30])
                    if key not in seen:
                        seen.add(key)
                        matches.append({
                            "title_a": all_titles[i],
                            "title_b": all_titles[j],
                            "platform_a": title_platform[i],
                            "platform_b": title_platform[j],
                            "similarity": round(sim, 3),
                        })
        return matches[:20]
    except Exception as exc:
        print(f"[insight_engine] cross_platform_semantic error: {exc}", file=sys.stderr)
        return []


# ────────────────── Task 5: _is_recent ───────────────────────
def _is_recent(item, hours=24):
    """Check if an item is recent based on pub_date or timestamp."""
    now = datetime.now(BJT)
    for key in ("pub_date", "published", "date", "timestamp"):
        val = item.get(key) if isinstance(item, dict) else None
        if not val:
            continue
        try:
            if isinstance(val, str):
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            elif isinstance(val, (int, float)):
                dt = datetime.fromtimestamp(val, tz=BJT)
            else:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BJT)
            return (now - dt).total_seconds() < hours * 3600
        except (ValueError, TypeError, OSError):
            continue
    return True  # assume recent if no date field


# ────────────────── Task 4.5: LLM label refinement ───────────
def _refine_labels_with_index(topic_clusters, llm, child_vecs=None,
                               child_nodes=None, parent_docs=None):
    """用 index 检索完整文章上下文 + LLM 生成语义标签。

    复用"小索引大窗口"机制：对每个话题簇，检索相关全文后让 LLM
    生成简洁中文标签，替代纯统计提取（避免英文单词/截断碎片）。
    无 index/LLM 时保持原统计标签不变。
    """
    if not topic_clusters:
        return topic_clusters
    if isinstance(llm, MockLLM):
        return topic_clusters
    if not (child_vecs and child_nodes and parent_docs):
        return topic_clusters

    # 只对"弱标签"做 LLM 精炼：纯英文 / 截断过短 / 无中文
    def _needs_refine(label):
        has_cn = bool(re.search(r'[\u4e00-\u9fff]', label))
        if has_cn and len(label) > 6:
            return False  # 中文标签且够长，跳过
        return True

    targets = [(i, c) for i, c in enumerate(topic_clusters)
               if _needs_refine(c.get('label', ''))]
    if not targets:
        return topic_clusters

    # 批量嵌入查询文本（与 generate_deep_insights 同模式）
    _q_texts = []
    for idx, cluster in targets:
        items = cluster.get('items', [])
        label = cluster.get('label', '')
        query_parts = [label] if label else []
        query_parts.extend(items[:3])
        _q_texts.append(" ".join(query_parts))

    try:
        q_vec_list, _ = _get_embeddings(_q_texts)
    except Exception:
        q_vec_list = None
    if not q_vec_list or len(q_vec_list) != len(_q_texts):
        return topic_clusters

    refined_count = 0
    for (cluster_idx, cluster), qvec in zip(targets, q_vec_list):
        if not qvec:
            continue
        items = cluster.get('items', [])
        orig_label = cluster.get('label', '')
        ctx = _retrieve_with_context(parent_docs, child_vecs, child_nodes,
                                     qvec, top_k=5, context_radius=2)
        if not ctx and items:
            ctx = "\n".join(items[:5])
        if not ctx:
            continue

        prompt = (
            "从以下上下文提取一个简洁的中文话题标签（4-15字）。\n"
            "要求：概括核心话题，使用中文，不要引号或JSON格式，直接输出标签文本。\n"
            "禁止：英文、解释性文字、超过15字。\n\n"
            f"上下文：\n{ctx[:2000]}"
        )
        try:
            result = llm.complete(prompt, temperature=0.2, max_tokens=30)
            if result and isinstance(result, str):
                new_label = result.strip().strip('"\'「」『』').strip()
                # 验证：3-20 字，必须含中文（防止 LLM 返回英文）
                has_cn = bool(re.search(r'[\u4e00-\u9fff]', new_label))
                if 3 <= len(new_label) <= 20 and has_cn:
                    topic_clusters[cluster_idx]['label'] = new_label
                    print(f"[insight_engine] label refined: "
                          f"{orig_label!r} → {new_label!r}", file=sys.stderr)
                    refined_count += 1
        except Exception as exc:
            print(f"[insight_engine] label refinement error: {exc}",
                  file=sys.stderr)

    print(f"[insight_engine] label refinement: {refined_count}/{len(targets)} "
          f"labels refined via index+LLM", file=sys.stderr)
    return topic_clusters


def _clean_llm_hedging(text):
    """移除 LLM 输出中的 hedging 语言和推脱性文字。

    例如：
    - "OpenAI GPT-6 Astra、DeepSeek 等实体不存在" → 移除
    - "以上为上下文中可验证的信息，其他厂商动态因上下文未覆盖无法确认" → 移除
    - "根据检索上下文" → 保留（有实际内容）
    """
    if not text:
        return text
    # 移除"实体不存在"类型的无意义声明
    text = re.sub(r'[^。]*实体不存在[^。]*。?', '', text)
    # 移除"无法确认"类型的推脱语句
    text = re.sub(r'[^。]*(?:无法确认|无法验证|未覆盖|未包含)[^。]*。?', '', text)
    # 移除"以上为"类型的总结性推脱
    text = re.sub(r'以上为[^。]*。?', '', text)
    # 移除"因上下文"类型的推脱
    text = re.sub(r'因上下文[^。]*。?', '', text)
    # 清理多余标点
    text = re.sub(r'。{2,}', '。', text)
    text = text.strip()
    return text


# ────────────────── 统一 System Prompt（借鉴 TrendRadar 最佳实践） ───────────
_SYS_ANALYST_PROMPT = (
    "你是一名高级科技情报分析师。你的核心能力是从海量碎片化信息中提炼核心逻辑，"
    "识别被大众忽略的弱信号。\n\n"
    "## 核心思维模型\n"
    "1. 见微知著：从散点关键词和话题中寻找底层共性叙事（如多条新闻共同指向'AI应用落地'）。\n"
    "2. 交叉验证：RSS 专业视角与大众热榜的差异往往隐藏认知套利机会。\n"
    "3. 反直觉思考：当全网叫好时寻找风险，当全网恐慌时寻找机会。拒绝平庸共识。\n\n"
    "## 核心原则\n"
    "1. 直击要害：拒绝'综上所述'、'众所周知'等废话，直接输出结论。\n"
    "2. 逻辑闭环：不仅描述'发生了什么'，必须解释'为什么发生'以及'未来会怎样'。\n"
    "3. 去情绪化：可以分析舆论情绪，但你的分析必须冷静、客观。\n\n"
    "## 防幻觉约束\n"
    "- 只分析检索上下文中实际存在的内容，直接输出结论。\n"
    "- 正确示例：'Google 推出 Agent Platform 企业级评估服务，标志着 Agent 工程化进入系统化阶段。'\n"
    "- 错误示例：'根据检索上下文，未能找到关于 Agent 评估的充分信息。'\n"
    "- 禁止编造上下文中未出现的事实。"
)

# ────────────────── 平台调性映射（Platform DNA） ───────────
_PLATFORM_DNA = {
    'tech': '技术社区(英文/前沿)',
    'cn_tech': '中文技术媒体(产业/应用)',
    'v2ex': '开发者社区(技术/吐槽)',
    'hackernews': '技术社区(英文/前沿)',
    'zhihu': '知识社区(深度/批判)',
    'weixin': '自媒体(垂直/深度)',
    'arxiv': '学术论文(理论/前沿)',
    'papers': '学术论文(理论/前沿)',
    '36kr': '科技媒体(商业/创业)',
    'startup': '创业媒体(融资/产品)',
    'weibo': '社交媒体(情绪/传播)',
    'douyin': '短视频(视觉/传播快)',
    'rss': 'RSS订阅(专业/垂直)',
}


def _enrich_platform_tag(cat):
    """将简短分类标签扩展为带调性描述的标签，供 LLM 感知数据源差异。"""
    return _PLATFORM_DNA.get(cat, cat)


# ────────────────── Task 5: generate_deep_insights ───────────
def generate_deep_insights(llm, topic_clusters, child_vecs=None, child_nodes=None,
                           parent_docs=None, keywords=None):
    """Use LLM to generate narrative insights from analysis context.
    小索引大窗口：通过手动余弦相似度检索相关子块，扩展上下文后提供给 LLM。
    """
    # 为每个话题检索相关上下文（大窗口）
    topic_contexts = []
    # 提取 top 关键词用于混合检索 query，确保召回与关键词匹配的文档
    top_kw_for_retrieval = keywords[:5] if keywords else []
    if topic_clusters:
        # D2 查询批化：先收集全部簇查询文本，一次批量嵌入（消灭 ≤5 次单条调用的 N+1）
        _q_labels, _q_items, _q_texts = [], [], []
        for cluster in topic_clusters[:5]:  # 最多 5 个话题
            label = cluster.get("label", "")
            items = cluster.get("items", [])
            if not (child_vecs and child_nodes and parent_docs and (items or top_kw_for_retrieval)):
                continue
            # 混合查询：簇标签 + 簇内条目 + RSS 关键词
            # 纯用簇内条目会导致检索偏移（如全部偏向某单一话题），
            # 混入关键词可确保召回与关键词匹配的文档
            query_parts = [label] if label else []
            query_parts.extend(items[:2])
            query_parts.extend(top_kw_for_retrieval)
            _q_texts.append(" ".join(query_parts))
            _q_labels.append(label)
            _q_items.append(items)
        _q_vec_list = []
        if _q_texts:
            _q_vec_list, _ = _get_embeddings(_q_texts)
        for label, items, qvec in zip(_q_labels, _q_items, _q_vec_list or [None] * len(_q_texts)):
            ctx = None
            if qvec:
                ctx = _retrieve_with_context(parent_docs, child_vecs, child_nodes,
                                             qvec,
                                             top_k=_RETRIEVE_TOP_K,
                                             context_radius=_CONTEXT_RADIUS)

            # 回退：直接使用簇内条目
            if not ctx and items:
                ctx = "\n".join(items[:5])

            if ctx:
                topic_contexts.append(f"【{label}】\n{ctx[:1500]}")

    combined_context = "\n\n".join(topic_contexts) if topic_contexts else "无检索上下文"

    # 分视角生成：分别调用 LLM 确保 core_trends / rss_insights / narrative 内容不同
    # 1) 结构化字段（因果链、信号、前瞻）—— 一次调用
    prompt_struct = (
        "基于以下检索上下文，以 JSON 格式返回：\n"
        "- causal_chains: 因果链条数组，如 [\"A→B→C\"]\n"
        "- signals: 异动信号数组，每项含 signal 和 confidence\n"
        "- outlook: 一段100字以内的前瞻研判\n\n"
        "## 输出要求\n"
        "- 每个因果链必须是 A→B→C 格式，描述事件间的因果关系\n"
        "- 每个信号必须包含具体的升温话题或异常波动\n"
        "- outlook 必须给出具体建议，禁止'建议持续关注'等空话\n\n"
        "## 正确示例\n"
        "causal_chains: [\"OpenAI发布Agent Platform→企业级评估需求爆发→Agent工程化进入系统化阶段\"]\n"
        "signals: [{\"signal\": \"Agent评估服务升温(+15位)\", \"confidence\": 0.85}]\n"
        "outlook: \"Agent基础设施竞争加剧，关注评估标准统一化进程\"\n\n"
        f"关键词（仅供参考）：{', '.join(keywords[:15]) if keywords else '无'}\n\n"
        f"检索上下文：\n{combined_context[:4000]}"
    )
    sys_struct = _SYS_ANALYST_PROMPT + "\n\n只返回 JSON，不要解释。"
    struct_result = None
    for _a in range(3):
        struct_result = llm.complete(prompt_struct, system_prompt=sys_struct, temperature=0.3, max_tokens=600)
        if struct_result:
            break
    struct_parsed = _try_parse_json(struct_result) if struct_result else None
    causal_chains = struct_parsed.get("causal_chains", []) if isinstance(struct_parsed, dict) else []
    signals = struct_parsed.get("signals", []) if isinstance(struct_parsed, dict) else []
    outlook = struct_parsed.get("outlook", "") if isinstance(struct_parsed, dict) else ""

    # 2) core_trends —— 整体市场/技术格局概览
    _kw_core = f"关键词（必须紧扣）：{', '.join(keywords[:10]) if keywords else '无'}\n\n" if keywords else ""
    prompt_core = (
        "基于以下检索上下文，用200字以内中文分析核心态势。\n\n"
        "## 写法要求\n"
        "1. 第一句话定性（使用'主导'/'分化'/'拐点'/'加速'等判断词）\n"
        "2. 用【宏观主线】+【微观佐证】结构串联\n"
        "3. 每个论点必须引用上下文中的具体事实\n"
        "4. 禁止'综上所述'、'值得关注'等空话\n\n"
        "## 正确示例\n"
        "'Agent工程化进入系统化阶段。【宏观主线】OpenAI、Google、Anthropic 同步推出企业级 Agent 评估服务，"
        "标志从'能用'到'可观测'的拐点。【微观佐证】1.LangSmith 发布 Trace Analytics 2.CrewAI 集成 OpenTelemetry'\n\n"
        "只返回纯文本，不要 JSON，不要字段名。\n\n"
        f"{_kw_core}"
        f"检索上下文：\n{combined_context[:4000]}"
    )
    sys_core = _SYS_ANALYST_PROMPT + "\n\n直接输出分析结论，不要解释。"
    core_trends = ""
    for _a in range(3):
        core_trends = llm.complete(prompt_core, system_prompt=sys_core, temperature=0.4, max_tokens=400)
        if core_trends and len(core_trends.strip()) > 10 and re.search(r'[\u4e00-\u9fff]', core_trends):
            break
    core_trends = core_trends.strip() if core_trends and re.search(r'[\u4e00-\u9fff]', core_trends) else ""

    # 3) rss_insights —— 侧重技术趋势、论文、开源动态
    _kw_rss = f"关键词（必须紧扣）：{', '.join(keywords[:10]) if keywords else '无'}\n\n" if keywords else ""
    prompt_rss = (
        "基于以下检索上下文，用200字以内中文分析技术趋势与开源动态。\n\n"
        "## 写法要求\n"
        "1. 聚焦硬核增量：新技术路线、开源项目、论文、架构创新\n"
        "2. 用【认知纠偏】+【硬核增量】结构\n"
        "3. 必须指出专业视角如何修正大众热搜的误区\n"
        "4. 禁止平铺直叙罗列事件\n\n"
        "## 正确示例\n"
        "'【认知纠偏】大众关注 Agent 功能演示，但 arXiv 论文显示评估方法论才是真正瓶颈。"
        "【硬核增量】1.AgentBench 提出多维度评估框架 2.OpenTelemetry 发布 Agent 扩展规范'\n\n"
        "只返回纯文本，不要 JSON，不要字段名。\n\n"
        f"{_kw_rss}"
        f"检索上下文：\n{combined_context[:4000]}"
    )
    sys_rss = _SYS_ANALYST_PROMPT + "\n\n直接输出分析结论，不要解释。"
    rss_insights = ""
    for _a in range(3):
        rss_insights = llm.complete(prompt_rss, system_prompt=sys_rss, temperature=0.4, max_tokens=400)
        if rss_insights and len(rss_insights.strip()) > 10 and re.search(r'[\u4e00-\u9fff]', rss_insights):
            break
    rss_insights = rss_insights.strip() if rss_insights and re.search(r'[\u4e00-\u9fff]', rss_insights) else ""

    # 4) narrative —— 串联核心事件的故事线
    _kw_narr = f"关键词（必须紧扣）：{', '.join(keywords[:10]) if keywords else '无'}\n\n" if keywords else ""
    prompt_narr = (
        "基于以下检索上下文，用200字以内中文串联核心事件的故事线。\n\n"
        "## 写法要求\n"
        "1. 必须呈现事件之间的因果/时间关系\n"
        "2. 用'起因→发展→转折→结果'或类似叙事结构\n"
        "3. 每个节点必须引用上下文中的具体事件\n"
        "4. 禁止平铺罗列，必须有逻辑串联\n\n"
        "## 正确示例\n"
        "'Agent 工程化浪潮始于 OpenAI 发布 Assistants API，但企业落地遭遇'黑箱'困境。"
        "转折点是 Google 推出 Agent Space 企业版，首次引入可观测性。随后 Anthropic 跟进发布 Claude Agent SDK，"
        "竞争焦点从'功能演示'转向'可评估、可审计'。'\n\n"
        "只返回纯文本，不要 JSON，不要字段名。\n\n"
        f"{_kw_narr}"
        f"检索上下文：\n{combined_context[:4000]}"
    )
    sys_narr = _SYS_ANALYST_PROMPT + "\n\n直接输出分析结论，不要解释。"
    narrative = ""
    for _a in range(3):
        narrative = llm.complete(prompt_narr, system_prompt=sys_narr, temperature=0.4, max_tokens=400)
        if narrative and len(narrative.strip()) > 10 and re.search(r'[\u4e00-\u9fff]', narrative):
            break
    narrative = narrative.strip() if narrative and re.search(r'[\u4e00-\u9fff]', narrative) else ""

    # 组装结果（不再 fallback 复制，各字段独立生成）
    # 后处理：移除 hedging 语言
    core_trends = _clean_llm_hedging(core_trends)
    rss_insights = _clean_llm_hedging(rss_insights)
    narrative = _clean_llm_hedging(narrative)

    if narrative or core_trends or rss_insights:
        return _normalize_deep_insights({
            "narrative": narrative,
            "core_trends": core_trends,
            "rss_insights": rss_insights,
            "causal_chains": causal_chains,
            "signals": signals,
            "outlook": outlook,
        })
    # fallback: simple keyword-based narrative
    kw_str = "、".join(keywords[:10]) if keywords else "无"
    return {
        "narrative": f"当前信息场核心关键词为：{kw_str}。",
        "causal_chains": [],
        "signals": [],
        "outlook": "建议持续关注上述领域的发展动态。",
    }


def _normalize_deep_insights(parsed):
    """规范化 deep_insights 输出：确保 signals 为 dict 数组，杜绝 dict-repr 字符串入库。"""
    raw_signals = parsed.get("signals", [])
    normalized = []
    for item in raw_signals:
        if isinstance(item, str):
            # 字符串项 → 包装为 dict
            normalized.append({"signal": item, "confidence": None})
        elif isinstance(item, dict):
            # dict 项：统一取 signal/label/name 字段
            sig_text = item.get("signal") or item.get("label") or item.get("name") or ""
            normalized.append({
                "signal": str(sig_text),
                "confidence": item.get("confidence"),
            })
        # 其他类型跳过
    parsed["signals"] = normalized
    return parsed


# ────────────────── RAGAS-inspired Evaluation & Self-Correction ──────────────────
def _evaluate_insight_quality(llm, deep_insights, context_text, keywords=None):
    """RAGAS-inspired 洞察质量评估（无需 ground truth）。

    评估维度：
    - context_coverage: 洞察对检索上下文的覆盖度（0-1）
    - faithfulness: 洞察内容是否有上下文支撑（0-1）
    - relevance: 洞察与关键词/话题的相关性（0-1）

    Returns:
        {"context_coverage": float, "faithfulness": float, "relevance": float,
         "overall": float, "feedback": str}
    """
    if not context_text or not deep_insights:
        return {"context_coverage": 0.5, "faithfulness": 0.5,
                "relevance": 0.5, "overall": 0.5, "feedback": "无上下文可评估"}

    narrative = deep_insights.get("narrative", "")
    chains = deep_insights.get("causal_chains", [])
    outlook = deep_insights.get("outlook", "")
    insight_text = f"{narrative}\n因果链: {chains}\n前瞻: {outlook}"

    prompt = (
        "你是 RAG 质量评估专家。请评估以下洞察报告的质量。\n\n"
        "【检索上下文】（来自小索引大窗口检索）：\n"
        f"{context_text[:2500]}\n\n"
        "【生成的洞察报告】：\n"
        f"{insight_text[:1500]}\n\n"
        f"【关键词】：{', '.join(keywords[:10]) if keywords else '无'}\n\n"
        "请从三个维度评分（0.0-1.0）并给出改进建议：\n"
        "1. context_coverage: 洞察是否充分利用了检索上下文中的关键信息？\n"
        "2. faithfulness: 洞察中的事实/判断是否都有上下文支撑（无编造）？\n"
        "3. relevance: 洞察是否紧扣关键词和核心话题？\n\n"
        "以 JSON 返回：{\"context_coverage\": 0.8, \"faithfulness\": 0.9, "
        "\"relevance\": 0.7, \"feedback\": \"具体改进建议\"}"
    )
    result = llm.complete(prompt, temperature=0.2, max_tokens=400)
    parsed = _try_parse_json(result)

    def _clamp(v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    if isinstance(parsed, dict):
        cov = _clamp(parsed.get("context_coverage", 0.5))
        faith = _clamp(parsed.get("faithfulness", 0.5))
        rel = _clamp(parsed.get("relevance", 0.5))
        return {
            "context_coverage": round(cov, 2),
            "faithfulness": round(faith, 2),
            "relevance": round(rel, 2),
            "overall": round((cov + faith + rel) / 3, 2),
            "feedback": str(parsed.get("feedback", "")),
        }

    return {"context_coverage": 0.5, "faithfulness": 0.5,
            "relevance": 0.5, "overall": 0.5, "feedback": "评估解析失败"}


def _self_correct_insights(llm, deep_insights, context_text, evaluation, keywords=None):
    """基于评估反馈的自我修正：让 LLM 针对薄弱维度重新生成。
    分视角独立调用，确保 core_trends / rss_insights / narrative 内容不同。
    """
    feedback = evaluation.get("feedback", "")
    low_dims = []
    if evaluation.get("context_coverage", 1) < 0.6:
        low_dims.append("context_coverage（需更多利用检索上下文中的信息）")
    if evaluation.get("faithfulness", 1) < 0.6:
        low_dims.append("faithfulness（需确保每个判断都有上下文依据，不要编造）")
    if evaluation.get("relevance", 1) < 0.6:
        low_dims.append("relevance（需更紧扣关键词和核心话题）")

    if not low_dims:
        return deep_insights  # 无需修正

    ctx = context_text[:2500]
    kw_str = ", ".join(keywords[:10]) if keywords else "无"
    forbid = (
        "绝对禁止说'检索上下文未包含'、'无法生成'、'建议提供'等解释性文字。\n"
        "只返回纯文本，不要 JSON，不要字段名。"
    )
    sys_prompt = "你是科技情报分析师。直接输出分析结论，不要解释。"

    # 1) 结构化字段修正
    prompt_struct = (
        "之前的洞察报告质量评估不达标，请根据反馈重新生成。\n\n"
        f"【薄弱维度】：{'; '.join(low_dims)}\n"
        f"【评估反馈】：{feedback}\n\n"
        "绝对禁止：\n"
        "- 禁止说'检索上下文未包含'、'无法生成'、'建议提供'等解释性文字\n"
        "- 禁止提及任何关键词在上下文中缺失\n\n"
        f"【检索上下文】：\n{ctx}\n\n"
        f"【关键词（仅供参考）】：{kw_str}\n\n"
        "请重新生成，以 JSON 格式返回：\n"
        "{\"causal_chains\": [\"A→B→C\"], \"signals\": [{\"signal\": \"...\", \"confidence\": 0.8}], \"outlook\": \"100字以内前瞻\"}"
    )
    sys_struct = "你是科技情报分析师。只返回 JSON。"
    struct_result = None
    for _a in range(3):
        struct_result = llm.complete(prompt_struct, system_prompt=sys_struct, temperature=0.3, max_tokens=600)
        if struct_result:
            break
    struct_parsed = _try_parse_json(struct_result) if struct_result else None
    causal_chains = struct_parsed.get("causal_chains", deep_insights.get("causal_chains", [])) if isinstance(struct_parsed, dict) else deep_insights.get("causal_chains", [])
    signals = struct_parsed.get("signals", deep_insights.get("signals", [])) if isinstance(struct_parsed, dict) else deep_insights.get("signals", [])
    outlook = struct_parsed.get("outlook", deep_insights.get("outlook", "")) if isinstance(struct_parsed, dict) else deep_insights.get("outlook", "")

    # 2) core_trends 修正
    _kw_core_c = f"\n【关键词（必须紧扣）】：{kw_str}" if kw_str != "无" else ""
    prompt_core = (
        "之前的洞察质量不达标，请根据反馈重新分析整体市场与技术格局的核心态势。\n"
        f"【薄弱维度】：{'; '.join(low_dims)}\n"
        f"【反馈】：{feedback}\n"
        "聚焦：哪些技术/产品/公司正在主导方向？竞争格局如何？200字以内。\n"
        f"{forbid}{_kw_core_c}\n\n检索上下文：\n{ctx}"
    )
    core_trends = ""
    for _a in range(3):
        core_trends = llm.complete(prompt_core, system_prompt=sys_prompt, temperature=0.4, max_tokens=400)
        if core_trends and len(core_trends.strip()) > 10 and re.search(r'[\u4e00-\u9fff]', core_trends):
            break
    core_trends = core_trends.strip() if core_trends and re.search(r'[\u4e00-\u9fff]', core_trends) else deep_insights.get("core_trends", "")

    # 3) rss_insights 修正
    _kw_rss_c = f"\n【关键词（必须紧扣）】：{kw_str}" if kw_str != "无" else ""
    prompt_rss = (
        "之前的洞察质量不达标，请根据反馈重新分析技术趋势与开源动态。\n"
        f"【薄弱维度】：{'; '.join(low_dims)}\n"
        f"【反馈】：{feedback}\n"
        "聚焦：有哪些新的技术路线、开源项目、论文或架构创新？200字以内。\n"
        f"{forbid}{_kw_rss_c}\n\n检索上下文：\n{ctx}"
    )
    rss_insights = ""
    for _a in range(3):
        rss_insights = llm.complete(prompt_rss, system_prompt=sys_prompt, temperature=0.4, max_tokens=400)
        if rss_insights and len(rss_insights.strip()) > 10 and re.search(r'[\u4e00-\u9fff]', rss_insights):
            break
    rss_insights = rss_insights.strip() if rss_insights and re.search(r'[\u4e00-\u9fff]', rss_insights) else deep_insights.get("rss_insights", "")

    # 4) narrative 修正
    _kw_narr_c = f"\n【关键词（必须紧扣）】：{kw_str}" if kw_str != "无" else ""
    prompt_narr = (
        "之前的洞察质量不达标，请根据反馈重新串联核心事件的故事线。\n"
        f"【薄弱维度】：{'; '.join(low_dims)}\n"
        f"【反馈】：{feedback}\n"
        "聚焦：事件之间的因果/时间关系是什么？200字以内。\n"
        f"{forbid}{_kw_narr_c}\n\n检索上下文：\n{ctx}"
    )
    narrative = ""
    for _a in range(3):
        narrative = llm.complete(prompt_narr, system_prompt=sys_prompt, temperature=0.4, max_tokens=400)
        if narrative and len(narrative.strip()) > 10 and re.search(r'[\u4e00-\u9fff]', narrative):
            break
    narrative = narrative.strip() if narrative and re.search(r'[\u4e00-\u9fff]', narrative) else deep_insights.get("narrative", "")

    return _normalize_deep_insights({
        "narrative": narrative,
        "core_trends": core_trends,
        "rss_insights": rss_insights,
        "causal_chains": causal_chains,
        "signals": signals,
        "outlook": outlook,
    })


def _evaluate_and_correct(llm, deep_insights, topic_clusters,
                          child_vecs, child_nodes, parent_docs, keywords, config):
    """评估-修正闭环：RAGAS 评分 → 不达标则自我修正。"""
    if not config.get("insight_self_correct", False):
        return deep_insights, {}

    threshold = config.get("insight_quality_threshold", 0.6)
    max_iterations = config.get("insight_max_corrections", 1)

    # 构建评估用上下文（同样使用混合查询确保召回关键词相关文档）
    context_parts = []
    top_kw_eval = keywords[:5] if keywords else []
    # D2 查询批化：先收集查询文本，一次批量嵌入（消灭 ≤5 次单条调用）
    _q_meta, _q_texts = [], []
    for cluster in topic_clusters[:5]:
        items = cluster.get("items", [])
        label = cluster.get("label", "")
        if not (child_vecs and child_nodes and parent_docs and (items or top_kw_eval)):
            continue
        query_parts = [label] if label else []
        query_parts.extend(items[:1])
        query_parts.extend(top_kw_eval)
        _q_texts.append(" ".join(query_parts))
        _q_meta.append((label, items))
    _q_vec_list = []
    if _q_texts:
        _q_vec_list, _ = _get_embeddings(_q_texts)
    for (label, items), qvec in zip(_q_meta, _q_vec_list or [None] * len(_q_texts)):
        if qvec:
            ctx = _retrieve_with_context(parent_docs, child_vecs, child_nodes,
                                         qvec,
                                         top_k=_RETRIEVE_TOP_K,
                                         context_radius=_CONTEXT_RADIUS)
            if ctx:
                context_parts.append(ctx)
        elif items:
            context_parts.append("\n".join(items[:3]))
    context_text = "\n---\n".join(context_parts) if context_parts else ""

    if not context_text:
        return deep_insights, {}

    # 评估-修正循环
    current = deep_insights
    eval_result = {}
    for iteration in range(max_iterations + 1):
        eval_result = _evaluate_insight_quality(llm, current, context_text, keywords)
        print(f"[insight_engine] RAGAS eval iter={iteration}: "
              f"overall={eval_result['overall']}, "
              f"cov={eval_result['context_coverage']}, "
              f"faith={eval_result['faithfulness']}, "
              f"rel={eval_result['relevance']}", file=sys.stderr)

        if eval_result["overall"] >= threshold:
            break
        if iteration < max_iterations:
            print(f"[insight_engine] quality {eval_result['overall']:.2f} < {threshold}, "
                  f"self-correcting...", file=sys.stderr)
            current = _self_correct_insights(llm, current, context_text, eval_result, keywords)

    return current, eval_result


# ────────────────── Topic metadata builder ──────────────────
def _build_topics_with_meta(topic_clusters, rss_history):
    """从 topic_clusters 构建带 sources/links/cats 的话题列表。
    通过匹配 rss_history 中的文章元数据填充话题属性。
    """
    if not rss_history or not isinstance(rss_history, dict):
        return [{"label": c["label"], "count": c["count"],
                 "sources": [], "links": [], "cats": []}
                for c in topic_clusters]
    # 建立标题→元数据索引（多种key策略提高匹配率）
    title_meta = {}
    for link, item in rss_history.items():
        title = item.get("title", "") or item.get("title_zh", "")
        if title:
            meta = {
                "link": link,
                "source": item.get("source", ""),
                "cat": item.get("cat", item.get("category", "rss")),
            }
            # 多种 key 策略
            title_meta[title[:30]] = meta
            # 纯中文 key（去标点英文）
            cn_key = re.sub(r'[^\u4e00-\u9fff]', '', title)[:20]
            if len(cn_key) >= 4:
                title_meta[cn_key] = meta
    topics = []
    for c in topic_clusters:
        sources_set = set()
        links_list = []
        cats_set = set()
        items = c.get("items", [])
        for item_text in items:
            # 去前缀后多种策略匹配
            clean = re.sub(r'^\[(?:RSS|\u70ed\u699c|Trending)/[^\]]*\]\s*', '', item_text)
            meta = title_meta.get(clean[:30])
            # 回退：纯中文匹配
            if not meta:
                cn_key = re.sub(r'[^\u4e00-\u9fff]', '', clean)[:20]
                if len(cn_key) >= 4:
                    meta = title_meta.get(cn_key)
            if meta:
                if meta["source"]:
                    sources_set.add(meta["source"])
                links_list.append(meta["link"])
                if meta["cat"]:
                    cats_set.add(meta["cat"])
        topics.append({
            "label": c["label"],
            "count": c["count"],
            "sources": sorted(sources_set)[:5],
            "links": links_list[:10],
            "cats": sorted(cats_set),
        })
    return topics


# ────────────────── Task 5: run_analysis (main entry) ────────
def run_analysis(hot_snapshot, rss_history, trending_data, config,
                 prev_keywords=None, hot_history=None, bad_date_sources=None):
    """Main entry point. Returns analysis dict or None if disabled."""
    if not config.get("insight_engine_enabled", True):
        return None

    t0 = datetime.now(BJT)
    llm = configure_llm(config)
    max_docs = config.get("insight_max_documents", 500)
    top_kw = config.get("insight_top_keywords", 30)
    top_topics = config.get("insight_top_topics", 15)

    # 1. Load documents
    documents = load_documents(hot_snapshot, rss_history, trending_data, max_docs)

    # 2. Split texts by source & extract keywords per source
    doc_texts = [d.text for d in documents] if documents else []
    rss_texts = [t for t in doc_texts if t.startswith('[RSS/')]
    hot_texts = [t for t in doc_texts if t.startswith('[热榜/')]
    rss_keywords = extract_keywords_llm(llm, rss_texts, top_n=top_kw)
    hot_keywords = extract_keywords_llm(llm, hot_texts, top_n=top_kw)
    # global = merge (rss first, then hot, deduplicated)
    _seen = set()
    keywords = []
    for w in rss_keywords + hot_keywords:
        if w not in _seen:
            _seen.add(w)
            keywords.append(w)
    keywords = keywords[:top_kw]
    print(f"[insight_engine] keywords: rss={len(rss_keywords)}, hot={len(hot_keywords)}, global={len(keywords)}", file=sys.stderr)

    # 3. 统一 embedding：一次计算，index + 聚类共享
    documents_rss = [d for d in documents if d.text.startswith('[RSS/')]
    rss_embeddings, rss_embed_model = _get_embeddings(rss_texts) if rss_texts else (None, None)

    # 4. Build hierarchical index — 小索引大窗口（精细切片 + 上下文扩展）
    index = None
    parent_docs = {}
    child_vecs = None
    child_nodes = None
    if rss_texts and LLAMA_INDEX_AVAILABLE:
        index, parent_docs, child_vecs = _build_hierarchical_index(
            documents_rss,  # 用 Document 对象列表
            pre_embeddings=rss_embeddings
        )
        if index:
            # 从 index 中提取 child_nodes（用于手动余弦检索）
            child_nodes = list(index.docstore.docs.values()) if hasattr(index, 'docstore') else None
    elif not LLAMA_INDEX_AVAILABLE:
        print("[insight_engine] index skipped: llama-index not available", file=sys.stderr)

    # 5. Cluster topics — 按类别分组聚类，排除噪声源（twitter/podcast/youtube 不参与聚类，
    #    但仍保留在 doc_texts 中供关键词提取和深度洞察使用）
    _CLUSTER_NOISE_CATS = {'twitter', 'podcast', 'youtube'}
    cluster_texts = []
    cluster_embeds = []
    cluster_cats = []
    for i, t in enumerate(rss_texts):
        m = re.match(r'^\[RSS/([^\]]+)\]', t)
        cat = m.group(1) if m else 'unknown'
        if cat in _CLUSTER_NOISE_CATS:
            continue
        cluster_texts.append(t)
        if rss_embeddings and i < len(rss_embeddings):
            cluster_embeds.append(rss_embeddings[i])
        cluster_cats.append(cat)
    # 按类别分组
    _cat_groups = {}
    for i, cat in enumerate(cluster_cats):
        _cat_groups.setdefault(cat, []).append(i)
    topic_clusters = []
    _per_cat_max = max(3, top_topics // max(len(_cat_groups), 1))
    for cat, indices in _cat_groups.items():
        cat_texts = [cluster_texts[i] for i in indices]
        cat_embeds = [cluster_embeds[i] for i in indices] if cluster_embeds else None
        if len(cat_texts) < 2:
            continue
        cat_clusters = cluster_topics_embedding(
            cat_texts, max_topics=_per_cat_max, all_doc_texts=doc_texts,
            pre_embeddings=cat_embeds, embed_model_name=rss_embed_model
        )
        topic_clusters.extend(cat_clusters)
    # 跨类别合并去重（按标签）+ 按 count 降序取 Top N
    _merged = {}
    for c in topic_clusters:
        lbl = c["label"]
        if lbl in _merged:
            _merged[lbl]["items"].extend(c["items"])
            _merged[lbl]["count"] = len(_merged[lbl]["items"])
        else:
            _merged[lbl] = {"label": lbl, "count": c["count"], "items": list(c["items"])}
    topic_clusters = sorted(_merged.values(), key=lambda c: -c["count"])[:top_topics]
    print(f"[insight_engine] topic clustering: {len(_cat_groups)} categories, "
          f"{len(cluster_texts)} texts (excluded twitter/podcast/youtube), "
          f"{len(topic_clusters)} clusters", file=sys.stderr)

    # 5. Cross-platform semantic
    cross_platform = cross_platform_semantic(hot_snapshot)

    # 6. Rising detection
    rising = []
    if prev_keywords:
        prev_freq = {w: i + 1 for i, w in enumerate(prev_keywords[:50])}
        curr_freq = {w: i + 1 for i, w in enumerate(keywords[:50])}
        for w, rank in curr_freq.items():
            prev_rank = prev_freq.get(w, 999)
            if prev_rank > rank + 5:
                rising.append({"word": w, "rise": prev_rank - rank, "current_rank": rank})
        rising.sort(key=lambda x: -x["rise"])
        rising = rising[:15]

    # 6. LLM 标签精炼 — 用 index 检索全文 + LLM 生成语义标签
    topic_clusters = _refine_labels_with_index(
        topic_clusters, llm,
        child_vecs=child_vecs, child_nodes=child_nodes,
        parent_docs=parent_docs
    )

    # 7. Deep insights — 使用层级索引检索 + 大窗口上下文
    deep_insights = generate_deep_insights(
        llm, topic_clusters,
        child_vecs=child_vecs, child_nodes=child_nodes,
        parent_docs=parent_docs,
        keywords=rss_keywords
    )

    # 7b. RAGAS 评估 + 自我修正
    deep_insights, eval_result = _evaluate_and_correct(
        llm, deep_insights, topic_clusters,
        child_vecs, child_nodes, parent_docs, rss_keywords, config
    )

    # 8. Stats
    rss_items = list((rss_history or {}).values()) if isinstance(rss_history, dict) else []
    recent_count = sum(1 for item in rss_items if _is_recent(item))
    source_count = len(set(item.get("source_key", "") for item in rss_items if item.get("source_key")))
    stats = {
        "total_articles": len(documents) if documents else 0,
        "recent_count": recent_count if rss_items else len(doc_texts),
        "source_count": source_count if source_count else (len(hot_snapshot) if hot_snapshot else 0),
    }

    # 9. Build old-format summary for frontend compatibility
    # WS-C: signals/outlook 不再互相复制，deep_insights 为唯一事实源
    summary = {
        "core_trends": deep_insights.get("core_trends", ""),
        "signals": "",                                        # 不再用 outlook 冒充
        "rss_insights": deep_insights.get("rss_insights", "") or f"共分析 {len(doc_texts)} 条内容，提取 {len(keywords)} 个关键词。",
        "outlook": deep_insights.get("outlook", ""),          # 唯一来源
    }

    # 10. Assemble output — old format + new fields
    now_bj = datetime.now(BJT)
    analysis = {
        "generated_at": now_bj.isoformat(),
        "keywords": {
            "global": [(w, round(top_kw - i, 2)) for i, w in enumerate(keywords[:50])],
            "rss": [(w, round(top_kw - i, 2)) for i, w in enumerate(rss_keywords[:50])],
            "hot": [(w, round(top_kw - i, 2)) for i, w in enumerate(hot_keywords[:50])],
            "by_cat": {},
        },
        "rising": rising,
        "topics": _build_topics_with_meta(topic_clusters, rss_history),
        "summary": summary,
        "stats": stats,
        "quality": eval_result if eval_result else {},
        "hot_trends": {},  # filled by integration layer in build_rss_aggregator.py
        "bad_date_sources": sorted(bad_date_sources) if bad_date_sources else [],
        "cross_platform": cross_platform if cross_platform else [],
        "cross_category": [],  # filled by integration layer in build_rss_aggregator.py
        # New fields
        "deep_insights": deep_insights,
        "topic_clusters": topic_clusters,
        "meta": {
            "engine": "insight_engine",
            "llm_provider": config.get("insight_llm_provider", "agnes"),
            "llm_available": not isinstance(llm, MockLLM),
            "index_built": index is not None,
            "embed_model": _last_embed_model or ("siliconflow/" + _SF_EMBED_MODEL if _SF_KEY else _EMBED_MODEL),
            "elapsed_seconds": round((datetime.now(BJT) - t0).total_seconds(), 2),
        },
    }
    return analysis
