# RSS 聚合页面实时刷新方案

## 1. 背景与目标

### 当前问题
- RSS 聚合页面（rss-aggregator.html）依赖 GitHub Actions 构建时抓取 36 个 RSS 源
- 构建耗时 4+ 分钟，经常超时失败
- 用户看到的数据最多滞后 1 小时（GitHub Actions 每小时构建一次）
- GitHub Actions schedule 在免费账户上不可靠（经常被延迟或跳过）

### 目标
- 用户打开页面时能获取**近实时**的 RSS 内容（延迟 ≤ 5 分钟）
- 首屏加载速度不受影响（< 100ms）
- 完全在 GitHub + Vercel 免费额度内运行
- 利用已有的 cron-job.org 基础设施

---

## 2. 架构设计

### 2.1 三层数据架构

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: 静态数据（GitHub Actions 每小时构建）                │
│  - 36 个 RSS 源，完整数据                                      │
│  - 作为 100% 可靠的 fallback                                  │
│  - 构建产物：rss-aggregator.html（内嵌 JSON）                  │
────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  Layer 2: 实时 API（Vercel Serverless Function）              │
│  - Top 10 高频源，增量更新                                     │
│  - CDN 缓存 5 分钟                                             │
│  - 接口：GET /api/rss → JSON                                  │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  Layer 3: 缓存预热（cron-job.org 每 5 分钟）                   │
│  - 定时调用 /api/rss，保持 CDN 缓存始终新鲜                    │
│  - 确保用户访问时命中缓存（< 100ms）                           │
─────────────────────────────────────────────────────────────┘
```

### 2.2 请求流程

```
用户访问 rss-aggregator.html
    │
    ├── 立即渲染静态数据（0ms，36 源）
    │
    └── 后台 fetch /api/rss
            │
            ├── CDN 命中（cron-job.org 已预热）→ < 100ms
            │       └── 用 API 数据更新 Top 10 源
            │
            └── CDN 未命中 → Vercel 执行函数（3-8s）
                    └── 抓取 Top 10 源 → 返回 JSON → 更新页面
```

---

## 3. 详细设计

### 3.1 Vercel Serverless Function: `api/rss.js`

**职责**：抓取 Top 10 RSS 源，返回 JSON

**Top 10 源选择标准**：
- 更新频率高（每日多次更新）
- 内容质量高（技术/AI 相关）
- RSS 源稳定（历史可用性 > 90%）
- 响应速度快（平均 < 3s）

**推荐 Top 10 源**：
| 序号 | 源名称 | URL | 分类 | 预期响应时间 |
|------|--------|-----|------|-------------|
| 1 | Hacker News | https://hnrss.org/frontpage | tech | 1-2s |
| 2 | TechCrunch AI | https://techcrunch.com/category/artificial-intelligence/feed/ | tech | 2-3s |
| 3 | The Verge AI | https://www.theverge.com/rss/ai-artificial-intelligence/index.xml | tech | 2-3s |
| 4 | arXiv CS.AI | https://rss.arxiv.org/rss/cs.AI | tech | 2-4s |
| 5 | 36氪 | https://rsshub.ktachibana.party/36kr/information/AI | cn_tech | 2-3s |
| 6 | IT之家 | https://www.ithome.com/rss/ | cn_tech | 1-2s |
| 7 | 少数派 | https://sspai.com/feed | cn_tech | 1-2s |
| 8 | Reddit r/tech | https://www.reddit.com/r/technology/.rss | tech | 2-4s |
| 9 | BBC 中文 | https://plink.anyfeeder.com/bbc/cn | news | 2-3s |
| 10 | Redis Blog | https://redis.io/feed/ | dev | 1-2s |

**技术规格**：
- 并行抓取：4 线程（Promise.allSettled）
- 单源超时：6s
- 总超时：12s（含解析）
- 每源条目数：10 条
- 响应格式：JSON
- 缓存策略：`Cache-Control: s-maxage=300, stale-while-revalidate=600`

**响应格式**：
```json
{
  "updated_at": "2026-08-31T12:30:00+08:00",
  "sources": [
    {
      "key": "hn",
      "name": "Hacker News",
      "color": "#ff6600",
      "cat": "tech",
      "items": [
        {
          "title": "文章标题",
          "link": "https://...",
          "summary": "摘要内容...",
          "pub_date": "2026-08-31T12:00:00Z",
          "time_str": "30分钟前"
        }
      ]
    }
  ]
}
```

### 3.2 cron-job.org 配置

**任务名称**：StarHub RSS Cache Warm

**配置参数**：
- URL: `https://starhub-refresh.vercel.app/api/rss`
- 执行频率：每 5 分钟
- 请求方法：GET
- 超时：15s
- 失败重试：3 次

**目的**：
- 预热 Vercel CDN 缓存
- 确保用户访问时命中缓存（< 100ms）
- 监控 API 可用性（失败时发送通知）

### 3.3 前端修改：`rss-aggregator.html`

**修改范围**：仅修改 `<script>` 部分，HTML/CSS 不变

**新增逻辑**：
```javascript
// 页面加载后 500ms 执行
setTimeout(async () => {
  try {
    const res = await fetch('/api/rss');
    if (!res.ok) return;
    const data = await res.json();
    
    // 更新 Top 10 源的内容
    data.sources.forEach(liveSource => {
      const source = SOURCES.find(s => s.key === liveSource.key);
      if (source && liveSource.items.length > 0) {
        source.items = liveSource.items;
        source.is_live = true; // 标记为实时数据
      }
    });
    
    // 重新渲染
    renderSidebar();
    renderArticleList();
    renderReader();
    
    // 显示"已更新"提示
    showUpdateToast(data.updated_at);
  } catch (e) {
    // 静默失败，保持静态数据
    console.warn('RSS API 更新失败:', e);
  }
}, 500);
```

**UI 增强**：
- 实时更新的源显示绿色圆点（🟢）
- 显示"最后更新时间"
- 可选：手动刷新按钮

### 3.4 `vercel.json` 更新

```json
{
  "functions": {
    "api/rss.js": { "maxDuration": 15 }
  }
}
```

---

## 4. 成本评估

### 4.1 Vercel Hobby（免费）

| 资源 | 消耗估算 | 免费额度 | 余量 |
|------|----------|----------|------|
| Serverless 调用 | 288 次/天（cron）+ 用户访问 | 无明确限制 | ✅ |
| 计算时间 | 288 × 0.01s = 2.88s/天 ≈ 0.08GB·h/月 | 100GB·h/月 | ✅ 1250 倍 |
| 带宽 | 288 × 50KB × 30 = 432MB/月 | 100GB/月 | ✅ 231 倍 |

### 4.2 GitHub Actions（公开仓库）

| 资源 | 消耗估算 | 免费额度 | 余量 |
|------|----------|----------|------|
| 构建次数 | 24 次/天（每小时） | 公开仓库无限 | ✅ |
| 构建时间 | 24 × 2 分钟 = 48 分钟/天 | 公开仓库无限 | ✅ |

### 4.3 cron-job.org（免费）

| 资源 | 消耗估算 | 免费额度 | 余量 |
|------|----------|----------|------|
| 任务数 | 1 个（RSS 预热）+ 1 个（已有 refresh） | 5 个 | ✅ |
| 执行次数 | 288 次/天 | 无明确限制 | ✅ |

**结论**：所有服务均在免费额度内，余量充足。

---

## 5. 实施步骤

### Step 1: 创建 `api/rss.js`
- 实现 Top 10 源并行抓取
- XML 解析（RSS 2.0 + Atom）
- 错误处理与降级
- CDN 缓存头设置

### Step 2: 更新 `vercel.json`
- 添加 `/api/rss` 函数配置（maxDuration: 15s）

### Step 3: 修改 `rss-aggregator.html` 前端 JS
- 添加后台 fetch 逻辑
- 数据合并与渲染更新
- UI 增强（实时标记、更新时间）

### Step 4: 配置 cron-job.org
- 添加新任务：每 5 分钟调用 `/api/rss`
- 设置失败通知（可选）

### Step 5: 测试验证
- 本地测试 `/api/rss` 响应
- 验证 CDN 缓存生效
- 验证前端更新逻辑
- 验证 cron-job.org 调用

### Step 6: 部署上线
- 提交代码
- 触发 Vercel 部署
- 验证线上效果

---

## 6. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| Vercel Serverless 超时 | API 不可用 | 前端静默降级，保持静态数据 |
| RSS 源限流/反爬 | 部分源失败 | 并行抓取 + 单源失败不影响整体 |
| cron-job.org 故障 | CDN 缓存过期 | 用户访问时触发冷启动（3-8s） |
| Vercel 免费额度耗尽 | 服务中断 | 当前余量 1000+ 倍，风险极低 |

---

## 7. 预期效果

| 指标 | 当前 | 优化后 |
|------|------|--------|
| 首屏加载 | 0ms（静态） | 0ms（静态） |
| 数据新鲜度（Top 10） | 最多 1 小时 | 最多 5 分钟 |
| 数据新鲜度（其余 26 源） | 最多 1 小时 | 最多 1 小时（不变） |
| API 响应时间（缓存命中） | N/A | < 100ms |
| API 响应时间（冷启动） | N/A | 3-8s |
| GitHub Actions 构建时间 | 4+ 分钟 | 2 分钟（可减少源数） |

---

## 8. 后续优化（可选）

- [ ] 添加手动刷新按钮
- [ ] 添加"最后更新时间"显示
- [ ] 实现增量更新动画（新条目高亮）
- [ ] 添加更多源到 Top 10（根据可用性调整）
- [ ] 实现 Service Worker 离线缓存
