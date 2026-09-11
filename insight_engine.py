"""
insight_engine.py — LlamaIndex-powered semantic analysis engine for StarHub.

Replaces the statistical _run_analysis() pipeline with LLM + embedding-based
semantic analysis.  Fully optional: works (with reduced functionality) even
when llama-index / fastembed are not installed.
"""

import json
import math
import os
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

# ────────────────── Task 2: Defaults / Config ──────────────────
_DEFAULTS = {
    "insight_engine_enabled": True,
    "insight_llm_provider": "agnes",
    "insight_max_documents": 200,
    "insight_top_keywords": 30,
    "insight_top_topics": 15,
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
    """Thin wrapper around the Agnes AI chat-completions API."""

    API_URL = "https://apihub.agnes-ai.com/v1/chat/completions"

    def __init__(self, api_key, model="agnes-2.5-flash", timeout=30):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    # -- core completion --
    def complete(self, prompt, system_prompt=None, temperature=0.3, max_tokens=600):
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
        req = urllib.request.Request(
            self.API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except Exception as exc:
            print(f"[AgnesLLM] complete error: {exc}", file=sys.stderr)
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
        api_key = os.environ.get("AGNES_API_KEY", "")
        if api_key:
            return AgnesLLM(api_key=api_key)
        print("[insight_engine] AGNES_API_KEY not set, using MockLLM", file=sys.stderr)
    elif provider != "mock":
        print(f"[insight_engine] Unknown provider '{provider}', using MockLLM", file=sys.stderr)
    return MockLLM()


# ────────────────── Task 3: load_documents ───────────────────
def load_documents(hot_snapshot, rss_history, trending_data, max_documents=200):
    """Convert raw data dicts into a list of LlamaIndex Document objects."""
    docs = []
    # --- Hot items (highest priority) ---
    if hot_snapshot:
        for platform in hot_snapshot:
            plat = platform.get("platform", platform.get("name", "unknown"))
            for item in platform.get("items", []):
                title = item.get("title", "")
                if title:
                    text = f"[热榜/{plat}] {title}"
                    docs.append({"text": text, "priority": 0})

    # --- Trending (second priority) ---
    if trending_data:
        items = trending_data.items() if isinstance(trending_data, dict) else []
        for repo, stars in items:
            desc = repo  # repo name as description fallback
            text = f"[Trending] {repo} (+{stars} stars): {desc}"
            docs.append({"text": text, "priority": 1})

    # --- RSS items (lowest priority) ---
    if rss_history:
        rss_items = rss_history.values() if isinstance(rss_history, dict) else rss_history
        for item in rss_items:
            if isinstance(item, dict):
                title = item.get("title", "")
                summary = item.get("summary", "")[:300]
                cat = item.get("cat", item.get("category", "rss"))
                text = f"[RSS/{cat}] {title} {summary}".strip()[:500]
                if text.strip():
                    docs.append({"text": text, "priority": 2})

    # Sort by priority (lower = higher priority), then truncate
    docs.sort(key=lambda d: d["priority"])
    docs = docs[:max_documents]

    if not LLAMA_INDEX_AVAILABLE:
        # Return lightweight stand-in objects
        return [_SimpleDoc(d["text"]) for d in docs]

    from llama_index.core import Document as LiDocument
    return [LiDocument(text=d["text"]) for d in docs]


class _SimpleDoc:
    """Minimal Document stand-in when llama-index is not installed."""
    def __init__(self, text):
        self.text = text


# ────────────────── Task 3: build_index ──────────────────────
def build_index(documents):
    """Build an in-memory VectorStoreIndex. Returns None if deps missing."""
    if not LLAMA_INDEX_AVAILABLE or not FASTEMBED_AVAILABLE:
        return None
    try:
        # Always force fastembed (avoid default OpenAI dependency)
        Settings.embed_model = FastEmbedEmbedding(model_name="BAAI/bge-small-en-v1.5")
        index = VectorStoreIndex.from_documents(documents, show_progress=False)
        return index
    except Exception as exc:
        print(f"[insight_engine] build_index error: {exc}", file=sys.stderr)
        return None


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


def _fallback_cluster(texts, max_topics=15, threshold=0.3):
    """Character-overlap based clustering when embeddings are unavailable."""
    clusters = []
    used = set()
    for i, t in enumerate(texts):
        if i in used:
            continue
        cluster = [t]
        used.add(i)
        ti = set(t.lower())
        for j in range(i + 1, len(texts)):
            if j in used:
                continue
            tj = set(texts[j].lower())
            overlap = len(ti & tj) / max(len(ti | tj), 1)
            if overlap >= threshold:
                cluster.append(texts[j])
                used.add(j)
        if len(cluster) >= 2:
            # derive label from longest text
            label = max(cluster, key=len)[:60]
            clusters.append({"label": label, "count": len(cluster), "items": cluster})
    clusters.sort(key=lambda c: -c["count"])
    return clusters[:max_topics]


# ────────────────── Task 4: extract_keywords_llm ─────────────
def extract_keywords_llm(llm, texts, top_n=30):
    """Use LLM to extract keywords from a combined text sample."""
    if not texts:
        return []
    combined = "\n".join(texts[:100])
    prompt = (
        f"请从以下文本中提取最重要的 {top_n} 个关键词，"
        "以 JSON 数组格式返回（只返回数组，不要其他文字）：\n\n"
        f"{combined[:4000]}"
    )
    result = llm.complete(prompt)
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
    return keywords[:top_n] if keywords else []


# ────────────────── Task 4: cluster_topics_embedding ─────────
def cluster_topics_embedding(articles, max_topics=15, similarity_threshold=0.7):
    """Embedding-based greedy clustering. Falls back to char-overlap."""
    if not articles:
        return []
    # Try to get embeddings
    embeddings = None
    try:
        if FASTEMBED_AVAILABLE:
            Settings.embed_model = FastEmbedEmbedding(model_name="BAAI/bge-small-en-v1.5")
            texts = [a if isinstance(a, str) else a.get("text", str(a)) for a in articles]
            embeddings = Settings.embed_model.get_text_embedding_batch(texts)
    except Exception:
        embeddings = None

    if embeddings and len(embeddings) == len(articles):
        return _cluster_with_embeddings(articles, embeddings, max_topics, similarity_threshold)
    # fallback
    texts = [a if isinstance(a, str) else a.get("text", str(a)) for a in articles]
    return _fallback_cluster(texts, max_topics)


def _cluster_with_embeddings(articles, embeddings, max_topics, threshold):
    """Greedy clustering by cosine similarity on embeddings."""
    n = len(articles)
    used = [False] * n
    clusters = []
    for i in range(n):
        if used[i]:
            continue
        cluster_indices = [i]
        used[i] = True
        vec_i = embeddings[i]
        for j in range(i + 1, n):
            if used[j]:
                continue
            sim = _cosine_similarity(vec_i, embeddings[j])
            if sim >= threshold:
                cluster_indices.append(j)
                used[j] = True
        if len(cluster_indices) >= 2:
            texts = [articles[k] if isinstance(articles[k], str) else articles[k].get("text", "") for k in cluster_indices]
            label = max(texts, key=len)[:60]
            clusters.append({"label": label, "count": len(texts), "items": texts})
    clusters.sort(key=lambda c: -c["count"])
    return clusters[:max_topics]


# ────────────────── Task 4: cross_platform_semantic ──────────
def cross_platform_semantic(hot_snapshot, similarity_threshold=0.75):
    """Embedding-based cross-platform topic matching.
    Returns [] if embeddings unavailable (caller falls back to Jaccard).
    """
    if not hot_snapshot or not LLAMA_INDEX_AVAILABLE:
        return []
    try:
        # Ensure embed_model is set (build_index should have done this already)
        if FASTEMBED_AVAILABLE:
            Settings.embed_model = FastEmbedEmbedding(model_name="BAAI/bge-small-en-v1.5")
        else:
            return []
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

        # Get all embeddings
        all_titles = []
        title_platform = []
        for plat in platforms:
            for t in platform_titles[plat]:
                all_titles.append(t)
                title_platform.append(plat)

        embed_model = Settings.embed_model
        vecs = embed_model.get_text_embedding_batch(all_titles)
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


# ────────────────── Task 5: generate_deep_insights ───────────
def generate_deep_insights(llm, context):
    """Use LLM to generate narrative insights from analysis context."""
    prompt = (
        "基于以下分析上下文，生成深度洞察。请以 JSON 格式返回，包含以下字段：\n"
        "- narrative: 一段200字以内的核心叙事分析\n"
        "- causal_chains: 因果链条数组，如 [\"A→B→C\"]\n"
        "- signals: 异动信号数组，每项含 signal 和 confidence\n"
        "- outlook: 一段100字以内的前瞻研判\n\n"
        f"分析上下文：\n{json.dumps(context, ensure_ascii=False)[:3000]}"
    )
    system_prompt = "你是科技情报分析师，擅长从多源数据中提取深层洞察。只返回 JSON，不要其他文字。"
    result = llm.complete(prompt, system_prompt=system_prompt, temperature=0.4, max_tokens=800)
    parsed = _try_parse_json(result)
    if isinstance(parsed, dict) and "narrative" in parsed:
        return parsed
    # fallback: simple keyword-based narrative
    keywords = context.get("keywords", [])
    kw_str = "、".join(keywords[:10]) if keywords else "无"
    return {
        "narrative": f"当前信息场核心关键词为：{kw_str}。",
        "causal_chains": [],
        "signals": [],
        "outlook": "建议持续关注上述领域的发展动态。",
    }


# ────────────────── Task 5: run_analysis (main entry) ────────
def run_analysis(hot_snapshot, rss_history, trending_data, config,
                 prev_keywords=None, hot_history=None):
    """Main entry point. Returns analysis dict or None if disabled."""
    if not config.get("insight_engine_enabled", True):
        return None

    t0 = datetime.now(BJT)
    llm = configure_llm(config)
    max_docs = config.get("insight_max_documents", 200)
    top_kw = config.get("insight_top_keywords", 30)
    top_topics = config.get("insight_top_topics", 15)

    # 1. Load documents
    documents = load_documents(hot_snapshot, rss_history, trending_data, max_docs)

    # 2. Build index (may be None)
    index = build_index(documents)

    # 3. Extract keywords
    doc_texts = [d.text for d in documents] if documents else []
    keywords = extract_keywords_llm(llm, doc_texts, top_n=top_kw)

    # 4. Cluster topics
    topic_clusters = cluster_topics_embedding(doc_texts, max_topics=top_topics)

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

    # 7. Deep insights
    context = {
        "keywords": keywords,
        "topic_count": len(topic_clusters),
        "doc_count": len(documents),
        "cross_platform_count": len(cross_platform),
    }
    deep_insights = generate_deep_insights(llm, context)

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
    summary = {
        "core_trends": deep_insights.get("narrative", ""),
        "signals": deep_insights.get("outlook", ""),
        "rss_insights": f"共分析 {len(doc_texts)} 条内容，提取 {len(keywords)} 个关键词。",
        "outlook": deep_insights.get("outlook", ""),
    }

    # 10. Assemble output — old format + new fields
    now_bj = datetime.now(BJT)
    analysis = {
        "generated_at": now_bj.isoformat(),
        "keywords": {
            "global": [(w, round(top_kw - i, 2)) for i, w in enumerate(keywords[:50])],
            "by_cat": {},
        },
        "rising": rising,
        "topics": [
            {"label": c["label"], "count": c["count"], "sources": [], "links": [], "cats": []}
            for c in topic_clusters
        ],
        "summary": summary,
        "stats": stats,
        "quality": {},
        "hot_trends": {},  # filled by integration layer in build_rss_aggregator.py
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
            "elapsed_seconds": round((datetime.now(BJT) - t0).total_seconds(), 2),
        },
    }
    return analysis
