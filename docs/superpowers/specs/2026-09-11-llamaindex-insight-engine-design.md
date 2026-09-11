# 热点洞察引擎 LlamaIndex 集成设计

> 日期：2026-09-11
> 状态：已审批，待实施

## 1. 背景与动机

StarHub RSS 聚合器的"每日洞察"面板（`analysis_snapshot.json`）当前基于纯统计方法：

| 能力 | 当前实现 | 局限 |
|------|---------|------|
| 关键词提取 | TF-IDF / 频率统计 | 无语义理解，同义词无法合并 |
| 话题聚类 | 关键词共现分组 | 粒度粗，无法识别隐含主题 |
| 跨平台匹配 | Jaccard 字符串相似度 | 同一事件不同表述无法关联 |
| AI 摘要 | Agnes AI 单段摘要 | 用户反馈"鸡肋"，缺乏深度 |
| 趋势检测 | 7 天排名变化统计 | 无时序预测，无因果分析 |

**目标**：引入 LlamaIndex 框架，在 GitHub Actions 构建时完成语义级分析，全面提升洞察深度，同时保持前端零改动（输出仍为 `analysis_snapshot.json`）。

## 2. 方案选型

| 方案 | 描述 | 评估 |
|------|------|------|
| A. 全流水线替换 | 用 LlamaIndex 替换整个 `_run_analysis()` | **选定** — 效果提升最大 |
| B. 轻量增强 | 仅替换摘要和跨平台匹配 | 改动小但提升有限 |
| C. Agent 化 | LlamaIndex Agent 自主分析 | 太激进，成本不可控 |

**选定方案 A**：LlamaIndex 全流水线替换。

## 3. 整体架构

```
build_rss_aggregator.py
    └─→ import insight_engine
        └─→ insight_engine.run_analysis(hot_data, rss_data, trending_data)
            │
            ├─ 1. 数据加载 → LlamaIndex Document 列表
            │     ├─ hot_snapshot.json 各平台热榜条目
            │     ├─ rss_history.json 72h RSS 文章（标题+摘要）
            │     └─ trending rising 项目名称+描述
            │
            ├─ 2. 向量索引构建
            │     ├─ 嵌入模型：fastembed 本地轻量嵌入（零 API 费用）
            │     └─ VectorStoreIndex（内存模式，构建完即丢弃）
            │
            ├─ 3. 语义关键词提取
            │     └─ LLM 从全部数据中提取 top-N 关键主题词
            │
            ├─ 4. 话题聚类（嵌入向量余弦相似度）
            │     └─ 替代原有关键词共现聚类
            │
            ├─ 5. 跨平台语义匹配
            │     └─ 嵌入向量相似度替代 Jaccard 字符串匹配
            │
            ├─ 6. 深度洞察生成
            │     └─ SummaryIndex + 结构化 prompt → 多段深度分析
            │
            └─ 7. 输出 analysis_data 字典（兼容原格式 + 新增字段）
```

### 3.1 文件结构

| 文件 | 职责 |
|------|------|
| `insight_engine.py`（新建） | LlamaIndex 分析流水线封装 |
| `build_rss_aggregator.py` | 调用 `insight_engine.run_analysis()`，替代原 `_run_analysis()` |
| `build_config.json` | 新增 `insight_*` 配置项 |
| `.github/workflows/update.yml` | 新增 `pip install` 步骤 |
| `rss-aggregator.html` | 洞察面板增量渲染新字段 |

### 3.2 新增依赖

- `llama-index-core` — 核心框架
- `llama-index-embeddings-fastembed` — 本地免费嵌入模型
- `fastembed` — 嵌入推理引擎

## 4. LLM 可插拔层

```python
# insight_engine.py
from llama_index.core import Settings

def configure_llm(config: dict):
    """根据 build_config.json 配置 LLM 提供商"""
    provider = config.get("insight_llm_provider", "agnes")
    if provider == "agnes":
        Settings.llm = AgnesLLM(api_key=...)   # 复用现有 Agnes AI
    elif provider == "openai":
        from llama_index.llms.openai import OpenAI
        Settings.llm = OpenAI(model="gpt-4o")
    elif provider == "gemini":
        from llama_index.llms.gemini import Gemini
        Settings.llm = Gemini(model="gemini-2.0-flash")
    # 嵌入始终用本地免费模型
    from llama_index.embeddings.fastembed import FastEmbedEmbeddingModel
    Settings.embed_model = FastEmbedEmbeddingModel("BAAI/bge-small-en-v1.5")
```

`AgnesLLM` 为自定义包装类，实现 LlamaIndex 的 `LLM` 接口，内部调用现有 Agnes AI API。

## 5. 增强后的输出格式

在兼容原有 `analysis_snapshot.json` 结构的基础上，新增语义化字段：

```json
{
  "keywords": [...],
  "rising": [...],
  "topics": [...],
  "summary": {...},
  "stats": {...},
  "hot_trends": {...},
  "cross_platform": [...],
  "cross_category": [...],

  "deep_insights": {
    "narrative": "今日热点的整体叙事脉络（3-5句话）",
    "causal_chains": [
      {
        "trigger": "触发事件",
        "effects": ["影响1", "影响2"],
        "confidence": 0.85
      }
    ],
    "signals": [
      {
        "description": "值得关注的信号",
        "importance": "high|medium|low",
        "evidence": ["支撑证据来源1", "来源2"]
      }
    ],
    "outlook": "未来 24-72 小时趋势展望"
  },
  "topic_clusters": [
    {
      "name": "话题名称",
      "keywords": ["关键词1", "关键词2"],
      "articles": ["相关文章标题..."],
      "platforms": ["微博", "知乎"],
      "heat_score": 0.92,
      "trajectory": "emerging|hot|cooling|cold"
    }
  ],
  "meta": {
    "llm_provider": "agnes",
    "embedding_model": "BAAI/bge-small-en-v1.5",
    "total_documents": 156,
    "build_time_seconds": 45
  }
}
```

### 5.1 关键增强点

1. **`deep_insights.narrative`**：连贯的叙事分析，替代干巴巴的要点罗列
2. **`causal_chains`**：LLM 识别热点之间的因果关系链
3. **`signals`**：带置信度和证据来源的洞察信号
4. **`topic_clusters`**：基于嵌入向量的话题簇，附带热度分数和轨迹

## 6. 降级策略

核心原则：**AI 可降级，主流程永不断**。

```
LlamaIndex 分析流水线
    │
    ├─ 成功 → 写入完整 analysis_snapshot.json（含 deep_insights）
    │
    └─ 失败（任一步骤异常）
        │
        ├─ LLM API 不可用 → 跳过 LLM 步骤，仅用嵌入向量做聚类+匹配
        │                     （关键词提取回退到 TF-IDF，摘要留空）
        │
        ├─ 嵌入模型加载失败 → 完全回退到原有统计方法
        │                     （等同于当前 _run_analysis() 行为）
        │
        └─ 整个 insight_engine 导入失败 → build_rss_aggregator.py
                                          使用原有 _run_analysis()
                                          （零回归）
```

## 7. 构建配置

`build_config.json` 新增字段：

| 配置项 | 说明 | 默认值 |
|-------|------|--------|
| `insight_engine_enabled` | 总开关，false 则完全使用旧方法 | `true` |
| `insight_llm_provider` | LLM 提供商：`agnes` / `openai` / `gemini` | `"agnes"` |
| `insight_max_documents` | 送入 LlamaIndex 的最大文档数（控制成本） | `200` |
| `insight_top_keywords` | LLM 提取的关键词数量 | `30` |
| `insight_top_topics` | 话题聚类数量上限 | `15` |

## 8. GitHub Actions 变更

```yaml
# .github/workflows/update.yml 新增步骤
- name: Install insight engine dependencies
  run: pip install llama-index-core llama-index-embeddings-fastembed fastembed
```

**预计构建时间影响**：
- 首次 pip install：约 30s（有缓存）
- 嵌入模型下载：约 20s
- 分析流水线：约 1-3 分钟（取决于文档数和 LLM 响应速度）
- 总计增加：约 2-5 分钟

## 9. 前端洞察面板适配

`rss-aggregator.html` 的 `renderInsight()` 函数增量渲染新字段：

```
洞察面板
├── 统计摘要（保持原有）
├── [新增] 深度叙事（deep_insights.narrative）—— 卡片式展示
├── [新增] 因果链（deep_insights.causal_chains）—— 箭头流程图样式
├── [新增] 信号看板（deep_insights.signals）—— 带重要性标签的列表
├── [新增] 趋势展望（deep_insights.outlook）—— 引用块样式
├── AI 情报分析（summary，保持原有）
├── 热门关键词（keywords，保持原有 + 语义提取标记）
├── [新增] 话题簇（topic_clusters）—— 可展开卡片，含文章列表和热度条
├── 升温词（rising，保持原有）
├── 跨平台共振（cross_platform，保持原有 + 语义匹配标记）
└── 跨分类热点（cross_category，保持原有）
```

**降级兼容**：如果 `deep_insights` 或 `topic_clusters` 不存在（旧格式数据或 LLM 降级），对应区域不渲染，不影响其他内容。

## 10. 测试策略

| 测试类型 | 方法 |
|---------|------|
| 单元 | `insight_engine.py` 每个函数可独立测试，mock LLM 响应 |
| 集成 | 本地运行 `python -c "import insight_engine; ..."` 验证完整流水线 |
| 降级 | 模拟 LLM 不可用 → 验证回退到统计方法 |
| 前端 | 构建后本地打开 `rss-aggregator.html`，检查洞察面板渲染 |
| CI | GitHub Actions 构建日志确认 insight_engine 成功/降级状态 |

## 11. 约束与风险

| 风险 | 缓解措施 |
|------|---------|
| Agnes AI 上下文窗口不够大 | 限制 `insight_max_documents=200`，超长文本截断 |
| LLM API 费用增加 | 默认限制每次构建最多 200 文档，可配置 |
| GitHub Actions 超时 | 分析流水线设置 5 分钟超时，超时即降级 |
| 嵌入模型首次下载慢 | fastembed 有缓存，后续构建复用 |
| 前端渲染性能 | 新字段为增量渲染，不影响现有内容 |

## 12. 后续演进路径

1. **Phase 1**（本次）：Agnes AI + fastembed，跑通全流水线
2. **Phase 2**：切换到 GPT-4o / Gemini 获得更强分析能力
3. **Phase 3**（可选）：引入向量持久化存储（避免每次重建索引）
4. **Phase 4**（可选）：LlamaIndex Agent 自主分析模式
