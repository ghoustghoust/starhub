# StarHub 全网仓库搜索功能设计

日期：2026-08-18
状态：已获用户确认（线框原型审阅通过）

## 1. 目标

在 StarHub 收藏台新增「全网 GitHub 仓库搜索」：用户输入模糊词组（中文为主），可跨语言搜索整个 GitHub 仓库（中文输入也能命中英文仓库），搜索结果不局限于已收藏项目。

## 2. 已确认需求决策

| 决策点 | 结论 |
|---|---|
| 搜索入口 | 复用现有 `#q` 搜索框，升级为独立板块，不再与筛选控件同行 |
| 跨语言 | 中文输入 → 翻译成英文 → `中文 OR 英文` 组合查询；英文输入直接搜 |
| 调用通道 | Vercel Serverless 代理（`api/search.js`），带 token 调 GitHub Search API |
| 结果排序 | 弹窗内可切换：相关度（best-match）/ 最多星标（stars）/ 最近更新（updated） |
| 结果展示 | 弹窗展示（复用现有 modal 体系），板块内只保留输入框 + 搜全网按钮 + 提示行 |

## 3. 架构

```
用户输入（收藏池本地实时过滤，现有行为不变）
   └─ 输入 ≥2 字符 → 搜索板块显示「搜全网」按钮（含当前关键词，不自动请求）
        └─ 点击 → 打开 #searchModal → POST /api/search {q, sort}
             └─ Vercel 代理 api/search.js：
                  ① 中文检测 /[\u4e00-\u9fff]/ → 翻译英文（Google 非官方端点 → MyMemory 降级 → 失败原词）
                  ② 构造 q = "中文 OR 英文"（英文输入直接搜）
                  ③ 调 GitHub Search API /search/repositories（带 GH_TOKEN）
                  ④ 10 分钟内存缓存（key = q|sort）
             └─ 弹窗展示：排序切换 tabs + 仓库卡片列表（20 条）+ 加载/空/错误态
```

## 4. UI 设计（已确认线框）

### 4.1 页面层级（三段式）

```
Header（现有，不改动）
├── 全网仓库搜索板块（新，全宽独立卡片）
│   ├── 搜索输入框 #q（从 toolbar 移入）+ 清空按钮 #btnClear
│   ├── 「搜全网」按钮 #btnSearchAll（输入 ≥2 字符淡入）
│   └── 提示行 #searchHint（收藏池匹配 N 个 · 搜全网：<词>）
├── 统计条（现有）
├── 筛选工具栏（现有，移除 .search）
├── 分类 Tabs（现有）
├── 收藏池列表（现有，本地过滤行为不变）
└── 侧栏：排行榜/关注动态（现有）
```

### 4.2 交互三态

- **默认态**：空输入；按钮隐藏；提示行隐藏
- **输入态**（≥2 字符）：本地列表实时过滤（现有 `state.q` + `renderList()`）；按钮淡入（文案「搜全网：<词>」）；提示行显示「收藏池匹配 N 个 · 输入 2 个以上字符可搜全网」；`[×]` 清空恢复默认态
- **弹窗态**：点击按钮 → `#searchModal` 打开（复用 modal/panel 体系：m-hd 标题「全网仓库搜索 · "<词>"」+ 关闭 ×、m-tabs 排序切换、m-list 结果列表）；Esc/遮罩关闭

### 4.3 既有行为衔接（不改动）

- `#q` 的输入过滤、`/` 快捷键聚焦、Esc 清空逻辑原样保留
- 搜索时自动清空分类/语言/星标筛选的逻辑不变
- 统计条、语言分布、分类 Tabs、侧栏、置顶收藏均不动
- 移动端（≤640px）：搜索板块全宽；按钮位于输入框右侧，必要时换行

## 5. api/search.js 设计

### 5.1 接口

```
POST /api/search
Body: { "q": "视频创作", "sort": "best-match" | "stars" | "updated" }
响应 200: {
  "query": "视频创作 OR \"video creation\"",   // 实际发给 GitHub 的 q
  "translated": true,                           // 是否发生了翻译
  "total": 1280,
  "items": [{ "full_name", "desc", "language", "stars", "updated_at", "html_url" }]
}
错误 403（Origin 不在白名单/缺少或错误 X-Search-Key）/ 405（非 POST）/ 502（上游不可用）/
503（GitHub 限流）→ { "error": "..." }（不向上游回显 GitHub 错误细节，由 Vercel 日志记录）
```

### 5.2 逻辑

1. **CORS**：复用 refresh.js 白名单模式（`starhub-refresh.vercel.app` + `Kwei168.github.io`），允许 GET/POST/OPTIONS
2. **鉴权**：`X-Search-Key` 头 == Vercel 环境变量 `SEARCH_KEY`（弱防护，与 refresh.js 同思路）
3. **翻译**：含中文 → 先 Google 非官方端点（`translate.googleapis.com/translate_a/single`），失败降级 MyMemory（`api.mymemory.translated.net/get`），再失败原词（响应 `translated:false`）
4. **q 构造**：中文 `zh` + 翻译 `en` → `zh OR "en"`（en 含空格加引号，不含则裸词）；纯英文 → 原词；翻译后与原文相同 → 仅原词
5. **GitHub 请求**：`GET /search/repositories?q=...&per_page=20`；sort 参数映射：`best-match` 不传 sort（GitHub 默认相关度）、`stars` 传 `sort=stars&order=desc`、`updated` 传 `sort=updated&order=desc`；头 `Authorization: Bearer GH_TOKEN`、`Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-11-28`
6. **缓存**：内存 Map，key = `q|sort`，TTL 10 分钟；命中直接返回
7. **超时**：翻译端点各 8s，GitHub 请求 15s；函数 maxDuration 30s

### 5.3 vercel.json

```json
"functions": {
  "api/refresh.js": { "maxDuration": 10 },
  "api/search.js": { "maxDuration": 30 }
}
```

## 6. 前端改动（template.html）

- **CSS**：`.searchbar`（新板块容器：card 底、圆角、边框、内边距）；`#btnSearchAll`（accent 主按钮样式 + 淡入过渡）；`#searchHint`（faint 小字）；弹窗内排序 tabs 复用 `.m-tabs .trend-tab`；结果行复用 `.feed-item` 骨架或新增 `.s-item`
- **HTML**：搜索板块（含 `#q`/`#btnClear`/`#btnSearchAll`/`#searchHint`）插入 `.main` 顶部（main-title 之后、stats 之前）；`#q` 从 toolbar 移除；toolbar 保留三个筛选控件
- **JS**：
  - `renderSearchHint()`：输入事件内调用，更新提示行 + 按钮显隐（`state.q.trim().length >= 2`）
  - `openSearchModal()` / `runSearch()`：fetch POST `/api/search`，请求中按钮与 tabs 禁用、显示 loading；AbortController 防止并发覆盖；`state.searchSort` 记忆当前排序
  - `renderSearchResults()`：渲染卡片（owner/name、desc、language 色点、stars 格式化、updated 相对时间、链接新窗口打开）；空态「未找到相关仓库」；错误态文案 + 重试按钮
  - 弹窗开关与 trendModal 共用 Esc/遮罩绑定逻辑
  - `translated:false` 时结果区顶部提示「翻译服务不可用，结果可能缺少英文仓库」

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| 翻译全失败 | 原词搜索，`translated:false`，前端提示 |
| GitHub 403（限流） | 代理回 503 `{error:'限流'}`，前端提示「搜索太频繁，请稍后再试」 |
| GitHub 422（查询语法） | 对 q 做 trim/去引号清洗后重试一次，仍失败回 502 |
| 网络/上游超时 | 502，前端显示重试按钮 |
| 空结果 | 空态提示，不改动其他 UI |

## 8. 测试计划

1. **代理逻辑（本地 node 脚本）**：OR 查询构造（中/英/中英混合/翻译失败）、翻译降级链、缓存命中与过期、CORS/鉴权拦截
2. **前端（本地）**：`dev_render.py` 渲染 + 本地起服务浏览器验证：三态切换、按钮显隐、弹窗开关、排序切换、错误态
3. **线上**：部署 Vercel 后真实搜索「视频创作」（期望中英文仓库混合结果）、英文词直搜、排序切换、快速重复搜索（限流提示）

## 9. 边界（YAGNI）

- 点击按钮才请求（不自动搜），天然防抖，限流可控
- 结果不落收藏池、不持久化、不做分页（Top20 足够）
- 代理缓存为单实例内存（Vercel 冷启动会丢，可接受）
- 不新增收藏操作（收藏仅针对已收藏池）
