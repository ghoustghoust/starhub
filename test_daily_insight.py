# -*- coding: utf-8 -*-
"""本地测试 build_daily_insight RAG 管线核心逻辑（跳过网络请求）。"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import build_daily_insight as B

# 初始化时间
B.NOW_BJ = B._now_bj()
print("=== 测试时间: %s ===" % B.NOW_BJ.strftime("%Y-%m-%d %H:%M"))
print("FAISS available: %s" % B.FAISS_AVAILABLE)
print("BM25 available: %s" % B.BM25_AVAILABLE)

# ── 测试 1: 硬过滤 ──
print("\n=== 测试 1: 硬过滤 ===")
test_items = [
    {"title": "OpenAI 发布 GPT-5 模型", "link": "https://a.com/1"},
    {"title": "震惊！这个 AI 工具太疯了必看", "link": "https://a.com/2"},
    {"title": "限时折扣！优惠券免费领取", "link": "https://a.com/3"},
    {"title": "Anthropic Claude 4 发布", "link": "https://a.com/4"},
    {"title": "ab", "link": "https://a.com/5"},  # 太短
    {"title": "Google Gemini 3 多模态能力评测", "link": "https://a.com/6"},
]
clean = B._filter_noise(test_items)
print("输入 %d 条, 过滤后 %d 条" % (len(test_items), len(clean)))
assert len(clean) == 3, "期望 3 条干净条目，实际 %d" % len(clean)
print("[PASS] 硬过滤正确剔除 3 条噪声")

# ── 测试 2: Jaccard 相似度 ──
print("\n=== 测试 2: Jaccard 相似度 ===")
t1 = B._tokenize_title("OpenAI 发布 GPT-5 模型")
t2 = B._tokenize_title("GPT-5 正式发布 OpenAI 新模型")
t3 = B._tokenize_title("苹果发布新款 iPhone")
sim_same = B._jaccard(t1, t2)
sim_diff = B._jaccard(t1, t3)
print("相似标题 Jaccard: %.3f" % sim_same)
print("不同标题 Jaccard: %.3f" % sim_diff)
assert sim_same > sim_diff, "相似标题应比不同标题 Jaccard 更高"
print("[PASS] Jaccard 相似度符合预期")

# ── 测试 3: 数据加载 ──
print("\n=== 测试 3: 数据加载 ===")
B._load_rss_tiers()
print("RSS Tier 映射: %d 个源" % len(B._RSS_TIER_MAP))

rss_history = B._load_rss_history()
print("RSS 历史: %d 篇" % len(rss_history))

hot_snapshot = B._load_hot_snapshot()
print("热榜快照: %d 个平台" % len(hot_snapshot))

# ── 测试 4: RSS 条目展平 ──
print("\n=== 测试 4: RSS 条目展平 ===")
rss_items = []
for link, item in list(rss_history.items())[:500]:
    if not isinstance(item, dict):
        continue
    title = item.get("title", "") or item.get("title_zh", "")
    if not title:
        continue
    pub_dt = B._parse_iso(item.get("pub_date", ""))
    if B._hours_ago(pub_dt) > 72:
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
print("72h 内 RSS 条目: %d 篇" % len(rss_items))

# 热榜展平
hot_items = []
for platform in hot_snapshot:
    plat = platform.get("platform", "")
    for item in platform.get("items", []):
        title = item.get("title", "")
        if not title:
            continue
        hot_items.append({
            "title": title, "url": item.get("url", ""),
            "rank": item.get("rank", 50), "hot": item.get("hot", ""),
            "platform": plat, "_src": "hot",
        })
print("热榜条目: %d 条" % len(hot_items))

# ── 就绪守卫 ──
# 这道闸（Quality gate B）跑在 `Fetch stars & build` 之前，那时 RSS 历史与热榜快照都还不存在
# （两者是 .gitignore 排除的跨场态，不进 git）⇒ 0 chunks 是必然。2026-10-02 实测 07:00~11:00
# 四场全部在本文件 chunk 断言处以 exit 1 收场，而被 `continue-on-error: true` 遮成 success：
# 恒红的闸等于没有闸，真回归混在里面看不见。
# 处置（用户批准方案 a）：数据未就绪 ⇒ 响亮跳过并退 0。今天 106 行之后本来就一节都没跑过，
# 所以早退不减少任何现有覆盖，只是把"必然红"换成"必然播报"。
# 不选"塞合成样本继续跑"：合成条目要穿过 _filter_noise/_chunk_documents 的真实阈值，
# 样本被判空时报出来的红就又变回噪声。真实数据那一节由构建后的产物判据接管。
# 判据：tests/daily_insight/test_gate_b_readiness.py（守卫必须在 chunk 断言之前、必须退 0、必须留 [SKIP]）
if not (rss_items or hot_items):
    print("[SKIP] 构建前无当日历史数据 ⇒ 测试 5 起的机制段本轮不跑，真实数据那节由构建后的产物判据接管")
    print("[SKIP] 本轮取数 RSS %d 篇 / 热榜 %d 条（都在 .gitignore 排除的跨场态里）"
          % (len(rss_items), len(hot_items)))
    sys.exit(0)

# ── 测试 5: 文档切片 ──
print("\n=== 测试 5: 文档切片 ===")
rss_clean = B._filter_noise(rss_items)
hot_clean = hot_items
aihot_clean = []  # 跳过网络
agihunt_clean = []  # 跳过网络

chunks = B._chunk_documents(rss_clean[:100], hot_clean[:50], aihot_clean, agihunt_clean)
print("Chunks: %d 个" % len(chunks))
assert len(chunks) > 0, "有数据却切不出 chunk ⇒ 切片机制真坏了（走到这里说明上面两族非空）"
# 验证 chunk 格式
c0 = chunks[0]
assert "chunk_id" in c0, "chunk 应有 chunk_id"
assert "source_type" in c0, "chunk 应有 source_type"
assert "text" in c0, "chunk 应有 text"
print("首个 chunk: type=%s, title=%s" % (c0["source_type"], c0.get("title", "")[:40]))
print("[PASS] 文档切片产出 %d 个 chunks" % len(chunks))

# ── 测试 6: FAISS 索引构建 ──
print("\n=== 测试 6: FAISS 索引构建 ===")
if B.FAISS_AVAILABLE:
    # 用简单假向量测试索引构建
    import numpy as np
    test_vecs = np.random.randn(20, 128).astype(np.float32)
    test_vecs /= np.linalg.norm(test_vecs, axis=1, keepdims=True)
    idx = B._build_faiss_index(test_vecs.tolist())
    assert idx is not None, "FAISS 索引应成功构建"
    assert idx.ntotal == 20, "索引应有 20 个向量"
    print("FAISS 索引: %d 向量, %d 维" % (idx.ntotal, idx.d))
    print("[PASS] FAISS 索引构建成功")
else:
    print("[SKIP] FAISS 不可用")

# ── 测试 7: 事件组装 ──
print("\n=== 测试 7: 事件组装 ===")
# 构造模拟检索结果
mock_retrieved = []
for i in range(15):
    mock_retrieved.append({
        "chunk_id": "chunk_%05d" % i,
        "source_type": "hot" if i < 5 else "rss",
        "source": "weibo" if i < 3 else "test_source",
        "title": "OpenAI GPT-5 发布" if i % 3 == 0 else "不同事件 %d" % i,
        "url": "https://example.com/%d" % i,
        "text": "测试文本 %d" % i,
        "pub_date": B.NOW_BJ.isoformat(),
        "retrieval_score": 1.0 / (i + 1),
        "final_score": 0,
        "vec_similarity": 0.8 - i * 0.02,
    })
events = B._assemble_events(mock_retrieved)
print("事件: %d 个" % len(events))
assert len(events) > 0, "应至少有 1 个事件"
for i, e in enumerate(events[:5]):
    print("  事件 %d: %s (%d chunks, 源: %s)" % (
        i + 1, e["label"][:40], len(e["items"]), ", ".join(e["source_types"])))
print("[PASS] 事件组装产出 %d 个事件" % len(events))

# ── 测试 8: 打分排序 ──
print("\n=== 测试 8: 综合打分 ===")
events = B._score_events(events, hot_snapshot)
print("排序后事件:")
for i, e in enumerate(events[:5]):
    print("  %d. [%.1f分] %s (共振: %s, 源: %s)" % (
        i + 1, e["score"], e["label"][:40],
        e.get("resonance", ""), ", ".join(e["source_types"])))
assert all(events[i]["score"] >= events[i+1]["score"]
           for i in range(len(events)-1)), "应按分数降序排列"
print("[PASS] 打分排序正确")

# ── 测试 9: JSON 输出 ──
print("\n=== 测试 9: JSON 输出 ===")
B._write_insight_json(events, "测试主题导语", False)
assert os.path.exists(B.INSIGHT_FILE), "daily-insight.json 应已生成"
with open(B.INSIGHT_FILE, "r", encoding="utf-8") as f:
    insight = json.load(f)
print("日期: %s" % insight.get("date"))
print("主题: %s" % insight.get("theme"))
print("事件数: %d" % len(insight.get("events", [])))
assert insight.get("date") == B.NOW_BJ.strftime("%Y-%m-%d")
assert insight.get("theme") == "测试主题导语"
assert len(insight.get("events", [])) > 0
# 验证 articles 字段
evt0 = insight["events"][0]
assert "articles" in evt0, "事件应包含 articles 字段"
print("articles 字段: %d 条" % len(evt0.get("articles", [])))
print("[PASS] JSON 输出格式正确")

# ── 测试 10: 历史留存 + 页面 ──
print("\n=== 测试 10: 历史留存 + 页面 ===")
B._update_history(events, "测试主题")
assert os.path.exists(B.HISTORY_FILE), "daily_insight_history.json 应已生成"
with open(B.HISTORY_FILE, "r", encoding="utf-8") as f:
    history = json.load(f)
print("历史天数: %d" % len(history.get("days", [])))
assert len(history.get("days", [])) >= 1

B._build_history_html()
assert os.path.exists(B.HISTORY_HTML), "daily-insight-history.html 应已生成"
size = os.path.getsize(B.HISTORY_HTML)
print("历史页面大小: %d bytes" % size)
assert size > 100
print("[PASS] 历史留存 + 页面正确")

# ── 测试 11: AI 日报注入 ──
print("\n=== 测试 11: AI 日报注入 ===")
# 2026-09-30 起 ai-daily.html 不再进 git（纯产物，由 Pages artifact 发布），
# 所以 CI 里 gate B 跑这场测试时磁盘上可能还没有它。旧写法是直接 [SKIP]，
# 那就等于这条注入断言从此不再执行却没人察觉 —— 宁可现造夹具，也不许静默空转。
_ai_daily_seed = None
if not os.path.exists("ai-daily.html"):
    _ai_daily_seed = "ai-daily.html"
    with open("ai-daily.html", "w", encoding="utf-8") as f:
        f.write("<!DOCTYPE html><html><body><h1>fixture</h1>"
                "<footer>footer</footer></body></html>")
    print("[SEED] ai-daily.html 不在磁盘上，已造最小夹具（含 <footer>）以保证注入断言真的跑")
if os.path.exists("ai-daily.html"):
    B._inject_into_ai_daily(events, "测试主题")
    with open("ai-daily.html", "r", encoding="utf-8") as f:
        html = f.read()
    assert "daily-insight-start" in html, "应包含注入标记"
    assert "每日深度洞察" in html, "应包含板块标题"
    print("[PASS] AI 日报注入成功")
    if _ai_daily_seed:
        os.remove(_ai_daily_seed)
else:
    print("[SKIP] ai-daily.html 不存在")

# ── 测试 12: RAGAS 质量评估 ──
print("\n=== 测试 12: RAGAS 质量评估 ===")
# 测试 _build_ragas_context
mock_retrieved_ragas = []
for i in range(10):
    mock_retrieved_ragas.append({
        "chunk_id": "chunk_%05d" % i,
        "source_type": "rss" if i < 5 else "hot",
        "source": "test_source",
        "title": "测试标题 %d" % i,
        "text": "这是测试文本 %d，用于验证 RAGAS 上下文构建。" % i,
        "retrieval_score": 1.0 / (i + 1),
    })
context = B._build_ragas_context(events, mock_retrieved_ragas)
assert len(context) > 0, "RAGAS 上下文应非空"
assert "测试标题" in context, "RAGAS 上下文应包含 chunk 标题"
print("RAGAS 上下文: %d 字符" % len(context))

# 测试 _evaluate_report_quality 无 LLM 时的降级
result_no_llm = B._evaluate_report_quality(None, events, "测试", context)
assert result_no_llm["overall"] == 0.5, "无 LLM 时应返回默认分"
print("无 LLM 降级评分: overall=%.2f" % result_no_llm["overall"])

# 测试 _ragas_evaluate_and_correct 无 LLM 时跳过
clusters_ragas, eval_ragas = B._ragas_evaluate_and_correct(
    None, events, "测试", mock_retrieved_ragas, {})
assert eval_ragas == {}, "无 LLM 时 eval_result 应为空"
assert clusters_ragas == events, "无 LLM 时 clusters 不应改变"
print("无 LLM 降级: 跳过 RAGAS 评估")

# 测试 _write_insight_json 带 RAGAS 评分
B._write_insight_json(events, "测试主题", False, {
    "overall": 0.82, "context_coverage": 0.78,
    "faithfulness": 0.85, "relevance": 0.83,
    "feedback": "测试反馈",
})
with open(B.INSIGHT_FILE, "r", encoding="utf-8") as f:
    insight_ragas = json.load(f)
assert "quality" in insight_ragas, "输出 JSON 应包含 quality 字段"
assert insight_ragas["quality"]["overall"] == 0.82, "quality.overall 应为 0.82"
print("RAGAS 评分写入 JSON: overall=%.2f" % insight_ragas["quality"]["overall"])
print("[PASS] RAGAS 质量评估功能正确")

# ── 测试 7: 增量向量缓存 ──
print("\n=== 测试 7: 增量向量缓存 ===")

# 7a) content_hash 稳定性：相同输入产生相同 hash
B.NOW_BJ = B._now_bj()
test_chunks_fn = B._chunk_documents
rss_item = [{"title": "测试标题", "link": "https://a.com/1", "source": "测试源",
             "source_key": "test_src", "summary": "这是测试内容。", "full_content": "",
             "pub_date": "2026-09-17T10:00:00+08:00", "cat": "tech"}]
chunks_a = B._chunk_documents(rss_item, [], [], [])
chunks_b = B._chunk_documents(rss_item, [], [], [])
assert len(chunks_a) > 0, "应产生至少一个 chunk"
assert chunks_a[0]["content_hash"] == chunks_b[0]["content_hash"], \
    "相同输入应产生相同 content_hash"
print("content_hash 稳定性: %s" % chunks_a[0]["content_hash"])

# 7b) content_hash 区分性：不同输入产生不同 hash
rss_item_diff = [{"title": "完全不同的标题", "link": "https://b.com/2",
                  "source": "其他源", "source_key": "other",
                  "summary": "这是不同的内容。", "full_content": "",
                  "pub_date": "2026-09-17T11:00:00+08:00", "cat": "tech"}]
chunks_diff = B._chunk_documents(rss_item_diff, [], [], [])
assert chunks_a[0]["content_hash"] != chunks_diff[0]["content_hash"], \
    "不同输入应产生不同 content_hash"
print("content_hash 区分性: %s vs %s" % (chunks_a[0]["content_hash"], chunks_diff[0]["content_hash"]))
print("[PASS] content_hash 计算正确")

# 7c) 向量缓存 round-trip（save → load 数据一致）
if B.FAISS_AVAILABLE:
    import numpy as np
    import tempfile
    import shutil
    # 创建临时目录测试
    orig_meta = B.FAISS_META_FILE
    orig_vec = B.VECTOR_CACHE_FILE
    tmp_dir = tempfile.mkdtemp()
    try:
        B.FAISS_META_FILE = os.path.join(tmp_dir, "test_chunks.json")
        B.VECTOR_CACHE_FILE = os.path.join(tmp_dir, "test_vectors.npy")
        # 构造测试数据
        test_chunks = [
            {"chunk_id": "c0", "source_type": "rss", "source": "s",
             "title": "t1", "url": "", "text": "hello world",
             "pub_date": "2026-09-17T10:00:00+08:00",
             "content_hash": "abc123def456"},
            {"chunk_id": "c1", "source_type": "hot", "source": "s2",
             "title": "t2", "url": "", "text": "test data",
             "pub_date": "", "content_hash": "xyz789abc012"},
        ]
        test_vecs = np.random.rand(2, B.EMBED_DIM).astype(np.float32)
        test_model = "test/model"
        # 保存
        B._save_vector_cache(test_chunks, test_vecs, test_model)
        # 加载
        loaded_chunks, loaded_vecs, loaded_model = B._load_vector_cache()
        assert len(loaded_chunks) == 2, "加载 chunks 数量应为 2"
        assert loaded_vecs.shape == (2, B.EMBED_DIM), "加载向量 shape 应为 (2, %d)" % B.EMBED_DIM
        assert loaded_model == test_model, "加载模型名应为 %s" % test_model
        assert loaded_chunks[0]["content_hash"] == "abc123def456", "content_hash 应保留"
        np.testing.assert_array_almost_equal(loaded_vecs, test_vecs, decimal=5)
        print("向量缓存 round-trip: chunks=%d, shape=%s, model=%s" % (
            len(loaded_chunks), loaded_vecs.shape, loaded_model))
        print("[PASS] 向量缓存 round-trip 正确")
    finally:
        B.FAISS_META_FILE = orig_meta
        B.VECTOR_CACHE_FILE = orig_vec
        shutil.rmtree(tmp_dir, ignore_errors=True)
else:
    print("[SKIP] FAISS 不可用，跳过向量缓存 round-trip 测试")

# 7d) 常量验证
assert B.INSIGHT_RSS_HOURS == 168, "INSIGHT_RSS_HOURS 应为 168 (7天)"
assert B.MAX_EMBED_CHUNKS == 30000, "MAX_EMBED_CHUNKS 应为 30000"
assert B.BM25_WINDOW == 10000, "BM25_WINDOW 应为 10000"
print("常量检查: INSIGHT_RSS_HOURS=%d, MAX_EMBED_CHUNKS=%d, BM25_WINDOW=%d" % (
    B.INSIGHT_RSS_HOURS, B.MAX_EMBED_CHUNKS, B.BM25_WINDOW))
print("[PASS] 常量配置正确")

# ── 测试 12: BM25 窗口化 ──
print("\n=== 测试 12: BM25 窗口化 ===")
if B.FAISS_AVAILABLE and B.BM25_AVAILABLE:
    import numpy as np

    mock_chunks = []
    mock_vecs = []
    # 关键词只放进少数 hot 文档：rank_bm25 用无平滑 Okapi IDF = log(N-df+0.5)-log(df+0.5)，
    # df 恰为窗口一半（旧写法 5000/10000）时 IDF=0，全部得分归零，本测试会假性失败。
    for i in range(5000):
        mock_chunks.append({
            "chunk_id": "hot_%05d" % i,
            "source_type": "hot",
            "source": "weibo",
            "title": "热榜条目 %d" % i,
            "url": "", "text": "热榜测试文本 %s %d" % ("关键词Alpha" if i < 50 else "普通词条", i),
            "pub_date": "",
            "content_hash": "h_%016d" % i,
        })
        mock_vecs.append(np.random.rand(B.EMBED_DIM).astype(np.float32))
    for i in range(10000):
        mock_chunks.append({
            "chunk_id": "rss_%05d" % i,
            "source_type": "rss",
            "source": "test_feed",
            "title": "RSS条目 %d" % i,
            "url": "", "text": "RSS测试文本 关键词Beta %d" % i,
            "pub_date": "2026-09-%02dT10:00:00+08:00" % (1 + i % 7),
            "content_hash": "r_%016d" % i,
        })
        mock_vecs.append(np.random.rand(B.EMBED_DIM).astype(np.float32))

    all_vecs = np.vstack(mock_vecs)
    norms = np.linalg.norm(all_vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    all_vecs = all_vecs / norms
    index = B._build_faiss_index(all_vecs)

    queries = ["关键词Alpha 热榜搜索"]
    results = B._hybrid_retrieve(index, mock_chunks, queries, top_k=80)

    hot_hits = [r for r in results if r["source_type"] == "hot"]
    print("总检索结果: %d, 其中 hot: %d, rss: %d" % (
        len(results), len(hot_hits), len(results) - len(hot_hits)))
    assert len(results) > 0, "应有检索结果"
    assert len(hot_hits) > 0, "BM25 应能匹配 hot chunks 的关键词Alpha"
    print("BM25_WINDOW=%d, 总 chunks=%d" % (B.BM25_WINDOW, len(mock_chunks)))
    print("[PASS] BM25 窗口化检索正确")
else:
    print("[SKIP] FAISS/BM25 不可用，跳过窗口化测试")

# ── 测试 9: TrustJudge 优化（Judge LLM + CoT prompt + 交叉验证 + 元数据） ──
print("\n=== 测试 9: TrustJudge 优化 ===")

# 9a) _MimoLLM 初始化
# "public" 只是两个 Zen key **都未设置**时的兜底；CI 里这些变量有值，必须自行隔离环境再断言
_saved_zen = {k: os.environ.pop(k, None) for k in ("ZEN_API_KEY", "OPENCODE_KEY")}
mimo = B._MimoLLM()
assert mimo.model == "mimo-v2.5-free", "默认模型应为 mimo-v2.5-free"
assert mimo.timeout == 30, "类默认超时应为 30s（b21ee89 快速降级；运行时由 daily_insight_judge_timeout 覆盖为 60）"
assert mimo.api_key == "public", "两个 Zen key 均缺失时应兜底 public"
# 空串等同未配置，且 ZEN 缺失时回退 OPENCODE_KEY（与 build_rss_aggregator._ZEN_KEY 同语义）
os.environ["ZEN_API_KEY"] = ""
os.environ["OPENCODE_KEY"] = "oc-key"
assert B._MimoLLM().api_key == "oc-key", "ZEN_API_KEY 为空应回退 OPENCODE_KEY"
os.environ["ZEN_API_KEY"] = "zen-key"
assert B._MimoLLM().api_key == "zen-key", "ZEN_API_KEY 优先于 OPENCODE_KEY"
for _k, _v in _saved_zen.items():
    if _v is not None:
        os.environ[_k] = _v
    else:
        os.environ.pop(_k, None)
print("_MimoLLM 初始化: model=%s, timeout=%d" % (mimo.model, mimo.timeout))

# 9b) _MimoLLM 自定义参数
mimo_custom = B._MimoLLM(model="custom-model", timeout=120, api_key="test-key")
assert mimo_custom.model == "custom-model"
assert mimo_custom.timeout == 120
assert mimo_custom.api_key == "test-key"
print("_MimoLLM 自定义参数正确")

# 9c) _FallbackJudgeLLM 首选模型成功
class _FakeLLM:
    def __init__(self, result, model="fake"):
        self._result = result
        self.model = model
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        return self._result

primary = _FakeLLM('{"scores": {"context_coverage": 0.8}}', "mimo-v2.5-free")
fallback = _FakeLLM('{"scores": {"context_coverage": 0.6}}', "agnes-2.5-flash")
fj = B._FallbackJudgeLLM(primary, fallback)
result = fj.complete([{"role": "user", "content": "test"}])
assert result == '{"scores": {"context_coverage": 0.8}}', "应返回首选模型结果"
assert len(fj._call_log) == 1, "应有 1 条调用日志"
assert fj._call_log[0]["model"] == "mimo-v2.5-free"
assert fj._call_log[0]["fallback"] is False
assert fj._call_log[0]["elapsed_s"] >= 0
print("_FallbackJudgeLLM 首选成功: model=%s, fallback=%s" % (
    fj._call_log[0]["model"], fj._call_log[0]["fallback"]))

# 9d) _FallbackJudgeLLM 降级逻辑
class _FailLLM:
    def __init__(self, model="fail"):
        self.model = model
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        return ""

fail_primary = _FailLLM("fail-model")
fallback_ok = _FakeLLM('{"fallback": true}', "agnes-2.5-flash")
fj2 = B._FallbackJudgeLLM(fail_primary, fallback_ok)
result2 = fj2.complete([{"role": "user", "content": "test"}])
assert result2 == '{"fallback": true}', "首选失败时应降级到备用模型"
assert len(fj2._call_log) == 1
assert fj2._call_log[0]["fallback"] is True
assert fj2._call_log[0]["model"] == "agnes-2.5-flash"
print("_FallbackJudgeLLM 降级成功: fallback=%s, model=%s" % (
    fj2._call_log[0]["fallback"], fj2._call_log[0]["model"]))

# 9e) _weighted_avg_scores 加权平均
v1 = {
    "context_coverage": 0.8, "faithfulness": 0.9, "relevance": 0.7,
    "overall": 0.8,
    "feedback": "v1反馈",
    "weak_events": [1, 3],
    "confidences": {"coverage": "high", "faithfulness": "high", "relevance": "high"},
}
v2 = {
    "context_coverage": 0.6, "faithfulness": 0.7, "relevance": 0.5,
    "overall": 0.6,
    "feedback": "v2反馈",
    "weak_events": [2, 3],
    "confidences": {"coverage": "low", "faithfulness": "low", "relevance": "low"},
}
merged = B._weighted_avg_scores(v1, v2)
# v1 置信度高 (weight=1.5*3=4.5)，v2 置信度低 (weight=0.5*3=1.5)
# cov = (0.8*4.5 + 0.6*1.5) / (4.5+1.5) = (3.6+0.9)/6.0 = 0.75
assert abs(merged["context_coverage"] - 0.75) < 0.01, "加权平均 coverage 应接近 0.75, got %.3f" % merged["context_coverage"]
assert "v1反馈" in merged["feedback"] and "v2反馈" in merged["feedback"], "反馈应合并"
assert set(merged["weak_events"]) == {1, 2, 3}, "weak_events 应去重合并"
print("_weighted_avg_scores: cov=%.2f (期望0.75), feedback合并正确, weak_events去重正确" % merged["context_coverage"])

# 9f) _evaluate_report_quality 新 prompt 格式解析（含 scores 子对象 + confidences）
mock_json = json.dumps({
    "reasoning": {
        "coverage": {"evidence_points": ["a", "b"], "covered": ["a"], "missed": ["b"], "confidence": "high"},
        "faithfulness": {"claims": [{"claim": "x", "status": "supported"}], "confidence": "medium"},
        "relevance": {"key_topics": ["t1"], "matched": ["t1"], "unmatched": [], "confidence": "low"},
    },
    "scores": {"context_coverage": 0.85, "faithfulness": 0.92, "relevance": 0.73},
    "feedback": "测试改进建议",
    "weak_events": [2],
})
class _MockJudgeLLM:
    def __init__(self, result):
        self._result = result
        self.model = "mock-judge"
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        return self._result

mock_llm = _MockJudgeLLM(mock_json)
eval_result = B._evaluate_report_quality(mock_llm, events, "测试", "mock context")
assert eval_result is not None, "应成功解析新 prompt 格式"
assert eval_result["context_coverage"] == 0.85, "应从 scores 子对象读取 coverage"
assert eval_result["faithfulness"] == 0.92, "应从 scores 子对象读取 faithfulness"
assert eval_result["relevance"] == 0.73, "应从 scores 子对象读取 relevance"
assert eval_result["confidences"]["coverage"] == "high", "应提取 coverage 置信度"
assert eval_result["confidences"]["faithfulness"] == "medium", "应提取 faithfulness 置信度"
assert eval_result["confidences"]["relevance"] == "low", "应提取 relevance 置信度"
assert eval_result["weak_events"] == [2], "应解析 weak_events"
assert eval_result["feedback"] == "测试改进建议", "应解析 feedback"
print("新 prompt 格式解析: scores子对象OK, confidences OK, reasoning OK")

# 9g) 旧格式兼容性（无 scores 子对象）
old_json = json.dumps({"context_coverage": 0.7, "faithfulness": 0.8, "relevance": 0.6, "feedback": "old"})
mock_llm_old = _MockJudgeLLM(old_json)
eval_old = B._evaluate_report_quality(mock_llm_old, events, "测试", "mock context")
assert eval_old is not None, "旧格式应兼容"
assert eval_old["context_coverage"] == 0.7, "旧格式应从顶层读取"
assert eval_old["confidences"]["coverage"] == "medium", "旧格式无置信度应默认 medium"
print("旧格式兼容: 顶层字段OK, 默认置信度OK")

# 9h) prompt 变体选择
assert "RAG 质量评估专家" in B._RAGAS_JUDGE_PROMPT, "V1 prompt 应存在"
assert "RAG 质量审计专家" in B._RAGAS_JUDGE_PROMPT_V2, "V2 prompt 应存在"
assert "推理步骤" in B._RAGAS_JUDGE_PROMPT, "V1 应包含推理步骤"
assert "审计" in B._RAGAS_JUDGE_PROMPT_V2, "V2 应为反向审计视角"
print("Prompt 变体: V1(正向)OK, V2(反向)OK")

# 9i) 评估元数据写入
mock_llm_meta = _MockJudgeLLM(mock_json)
config_with_cv = {"daily_insight_cross_validation": False}
eval_meta_result = B._ragas_evaluate_and_correct(
    mock_llm_meta, events, "测试", mock_retrieved_ragas, config_with_cv)
assert "meta" in eval_meta_result[1], "eval_result 应包含 meta 字段"
meta = eval_meta_result[1]["meta"]
assert meta["judge_model"] == "mock-judge", "meta 应记录 judge_model"
assert meta["prompt_variants"] == 1, "未启用交叉验证时 prompt_variants=1"
assert isinstance(meta["call_log"], list), "call_log 应为列表"
assert meta["temperature"] == 0.2, "temperature 应为 0.2"
print("评估元数据: judge_model=%s, variants=%d, temp=%.1f" % (
    meta["judge_model"], meta["prompt_variants"], meta["temperature"]))

# 9i-2) 交叉验证流程：两个 prompt 变体都被调用
class _CountingMockLLM:
    def __init__(self, result_template, model="counting-mock"):
        self._template = result_template
        self.model = model
        self._call_log = []
        self.call_count = 0
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        self.call_count += 1
        return self._template

counting_llm = _CountingMockLLM(mock_json)
config_cv_on = {"daily_insight_cross_validation": True}
cv_clusters, cv_eval = B._ragas_evaluate_and_correct(
    counting_llm, events, "测试", mock_retrieved_ragas, config_cv_on)
assert "cross_validation" in cv_eval, "启用交叉验证时应有 cross_validation 字段"
assert "v1_overall" in cv_eval["cross_validation"], "应有 v1_overall"
assert "v2_overall" in cv_eval["cross_validation"], "应有 v2_overall"
assert cv_eval["meta"]["prompt_variants"] == 2, "交叉验证时 prompt_variants=2"
assert counting_llm.call_count >= 2, "交叉验证应至少调用 LLM 2 次 (v1+v2), got %d" % counting_llm.call_count
print("交叉验证流程: call_count=%d, v1=%.2f, v2=%.2f, avg=%.2f" % (
    counting_llm.call_count,
    cv_eval["cross_validation"]["v1_overall"],
    cv_eval["cross_validation"]["v2_overall"],
    cv_eval["overall"]))

# 9j) _write_insight_json 带元数据
B._write_insight_json(events, "测试主题", False, {
    "overall": 0.82, "context_coverage": 0.78,
    "faithfulness": 0.85, "relevance": 0.83,
    "feedback": "测试反馈",
    "meta": {"judge_model": "mimo-v2.5-free", "prompt_variants": 2,
             "call_log": [{"model": "mimo", "elapsed_s": 1.5, "fallback": False}],
             "temperature": 0.2},
})
with open(B.INSIGHT_FILE, "r", encoding="utf-8") as f:
    insight_meta = json.load(f)
assert "meta" in insight_meta["quality"], "quality 应包含 meta 子字段"
assert insight_meta["quality"]["meta"]["judge_model"] == "mimo-v2.5-free"
assert insight_meta["quality"]["meta"]["prompt_variants"] == 2
print("JSON 输出元数据: meta.judge_model OK, meta.prompt_variants OK")

# 9k) 对抗性: _init_judge_llm 非 mimo provider 返回纯 agnes (无 FallbackJudgeLLM 包装)
old_provider = os.environ.get("AGNES_API_KEY", "")
os.environ["AGNES_API_KEY"] = "test-key-for-init"
plain_result = B._init_judge_llm({"daily_insight_judge_provider": "agnes"})
assert plain_result is not None, "agnes provider 应返回非 None"
assert not isinstance(plain_result, B._FallbackJudgeLLM), "非 mimo provider 不应使用 FallbackJudgeLLM 包装"
assert isinstance(plain_result, B._LLM), "应返回 _LLM 实例"
print("_init_judge_llm(agnes): 返回纯 _LLM, 无冗余包装")

# 9l) 对抗性: CV 中一个变体失败时 prompt_variants 准确反映实际运行数
class _FailOnceLLM:
    def __init__(self, model="fail-once"):
        self.model = model
        self._call_log = []
        self._n = 0
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        self._n += 1
        # 只能按 v1 用户 prompt 独有语句标记失败：system 里也含"RAG 质量评估专家"，
        # 用它当标记会让 v2 一起挂掉，测的其实是"全挂"分支
        if "请按以下步骤评估每日 AI 洞察报告的质量" in json.dumps(messages, ensure_ascii=False):
            return ""  # v1 失败
        return mock_json  # v2 成功

fail_once = _FailOnceLLM()
cv_partial_cfg = {"daily_insight_cross_validation": True}
_, cv_partial = B._ragas_evaluate_and_correct(
    fail_once, events, "测试", mock_retrieved_ragas, cv_partial_cfg)
assert cv_partial["meta"]["prompt_variants"] == 1, "一个变体失败时 prompt_variants 应为 1, got %d" % cv_partial["meta"]["prompt_variants"]
print("对抗性 CV 部分失败: prompt_variants=%d (准确)" % cv_partial["meta"]["prompt_variants"])

# 9m) 对抗性: CV 中所有变体都失败时不得谎报变体数，必须标 degraded
class _AlwaysFailLLM:
    def __init__(self, model="always-fail"):
        self.model = model
        self._call_log = []
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        return ""

always_fail = _AlwaysFailLLM()
_, cv_fail = B._ragas_evaluate_and_correct(
    always_fail, events, "测试", mock_retrieved_ragas, {"daily_insight_cross_validation": True})
assert cv_fail["meta"]["prompt_variants"] == 0, "两路均失败时不得谎报变体数, got %d" % cv_fail["meta"]["prompt_variants"]
assert cv_fail["meta"]["eval_samples"] == 0, "全挂时样本数必须为 0"
assert cv_fail["meta"]["degraded"] is True, "合成 0.5 必须标 degraded，否则与真实评分无法区分"
print("对抗性 CV 全部失败: prompt_variants=%d eval_samples=%d degraded=%s (准确)" % (
    cv_fail["meta"]["prompt_variants"], cv_fail["meta"]["eval_samples"], cv_fail["meta"]["degraded"]))

if old_provider:
    os.environ["AGNES_API_KEY"] = old_provider
else:
    os.environ.pop("AGNES_API_KEY", None)

print("[PASS] TrustJudge 优化功能全部正确 (含对抗性审查修复)")

# ── 测试 9n: _OpenRouterLLM ──
print("\n=== 测试 9n: _OpenRouterLLM ===")

# 9n-1) 初始化
or_llm = B._OpenRouterLLM(api_key="test-key")
assert or_llm.model == "qwen/qwen3.8-27b:free", "默认模型应为 qwen"
assert len(or_llm.models) == 2, "应有 2 个默认模型"
print("_OpenRouterLLM 初始化: models=%s" % or_llm.models)

# 9n-2) 自定义模型列表
or_custom = B._OpenRouterLLM(api_key="test-key", models=["model-a", "model-b", "model-c"])
assert len(or_custom.models) == 3, "应支持自定义模型列表"
assert or_custom.model == "model-a", "对外模型名应为第一个"
print("_OpenRouterLLM 自定义模型: OK")

# 9n-3) 即使残留 OPENROUTER_API_KEY，降级链也必须是两级（OpenRouter 已于 ff137b0 移除）
os.environ["OPENROUTER_API_KEY"] = "test-or-key"
old_key = os.environ.get("AGNES_API_KEY", "")
os.environ["AGNES_API_KEY"] = "test-agnes-key"
chain = B._init_judge_llm({"daily_insight_judge_provider": "mimo"})
assert isinstance(chain, B._FallbackJudgeLLM), "应返回 FallbackJudgeLLM"
assert not isinstance(chain.fallback, B._FallbackJudgeLLM), "不应再嵌套 OpenRouter 中间层"
assert isinstance(chain.fallback, B._LLM), "唯一 fallback 应为 agnes"
print("两级降级链（残留 OpenRouter key 也不启用）: mimo -> agnes OK")

# 9n-4) 无 OPENROUTER_API_KEY 时回退两级链
os.environ.pop("OPENROUTER_API_KEY", None)
chain2 = B._init_judge_llm({"daily_insight_judge_provider": "mimo"})
assert isinstance(chain2, B._FallbackJudgeLLM), "应返回 FallbackJudgeLLM"
assert not isinstance(chain2.fallback, B._FallbackJudgeLLM), "无 OpenRouter 时不应嵌套"
print("无 OpenRouter key: mimo -> agnes 两级链 OK")

# 9n-4b) judge_model 必须反映实际打分的模型（历史缺陷：降级后 .model 仍是主模型名，
#        导致 quality.meta.judge_model 一直谎报 mimo，而 call_log 全是被降级的 agnes）
_stub_primary = B._MimoLLM(api_key="p", model="mimo-v2.5-free")
_stub_fallback = B._LLM("k", extra_keys=None)
_stub_fallback.model = "agnes-2.5-flash"
_j = B._FallbackJudgeLLM(_stub_primary, _stub_fallback)
assert B._effective_judge_model(_j) == "mimo-v2.5-free", "无调用记录时回退配置值"
_j._call_log = [{"model": "agnes-2.5-flash", "fallback": True},
                {"model": "agnes-2.5-flash", "fallback": True}]
assert B._effective_judge_model(_j) == "agnes-2.5-flash", "全部降级时应报实际模型"
_j._call_log = [{"model": "mimo-v2.5-free"}, {"model": "agnes-2.5-flash", "fallback": True}]
assert B._effective_judge_model(_j) == "mixed(mimo-v2.5-free+agnes-2.5-flash)", "混合时应标注 mixed"
assert B._effective_judge_model(object()) == "unknown", "无 model 属性时应返回 unknown"
print("judge_model 取实际调用模型 OK（含 mixed / unknown）")

# 9n-5) 三级链降级行为
class _SuccessLLM:
    def __init__(self, name, result):
        self.model = name
        self._result = result
        self._call_log = []
    def complete(self, messages, temperature=0.3, max_tokens=2000):
        self._call_log.append({"model": self.model})
        return self._result

primary_fail = _SuccessLLM("mimo", "")
mid_fail = _SuccessLLM("openrouter", "")
final_ok = _SuccessLLM("agnes", '{"scores": {"context_coverage": 0.7}}')
chain3 = B._FallbackJudgeLLM(primary_fail, B._FallbackJudgeLLM(mid_fail, final_ok))
result = chain3.complete([{"role": "user", "content": "test"}])
assert result == '{"scores": {"context_coverage": 0.7}}', "三级链应降级到最终成功"
assert len(chain3._call_log) == 1, "应记录一次调用"
assert chain3._call_log[0]["fallback"] is True, "应标记为 fallback"
assert chain3._call_log[0]["model"] == "agnes", "实际模型应为 agnes"
print("三级链降级: mimo(X) -> openrouter(X) -> agnes(OK) OK")

if old_key:
    os.environ["AGNES_API_KEY"] = old_key
else:
    os.environ.pop("AGNES_API_KEY", None)
os.environ.pop("OPENROUTER_API_KEY", None)

print("[PASS] _OpenRouterLLM + 三级降级链正确")

print("\n" + "=" * 50)
print("全部测试通过！(RAG 管线 + RAGAS + 增量向量缓存 + TrustJudge 优化)")
