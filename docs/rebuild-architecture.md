# StarHub 重构架构方案

> 状态：设计稿，待确认后实施  
> 日期：2026-10-23  
> 范围：主站、AI 晨报、数据管线、Vercel API、GitHub Actions  
> 明确不做：RSS 聚合/订阅、原文与原站跳转、每日洞察、上游同步

## 0. 决策摘要

1. 先出架构方案，确认后再改业务代码。
2. 删除 RSS 聚合页及其全部相关能力：RSS API、RSS 数据与缓存、OPML、阅读器、原文/原站跳转、二维码分享、微信跳转清理。
3. 删除每日洞察：洞察引擎、历史页、AI 晨报中的洞察注入、相关快照与测试。
4. 不再跟进上游 `Kwei168/starhub`，本仓库独立演进。
5. 保留主站与 AI 晨报的非 RSS 功能，页面信息架构与视觉语言不变。
6. 保留 AI 晨报内部对公开 RSS/Atom 的抓取——它是晨报的数据源，不是面向用户的订阅功能。
7. 继续使用 GitHub Pages + Vercel，但 Vercel 必须是用户自己的独立项目：放在用户自己的 Vercel 团队 `kwei888`（team_lhu…）下，与 `qwis-intel` 并列但独立；不再使用上游的 `team_iovv… / starhub-refresh`。
8. StarHub 与 `D:\全网情报系统` 相互独立：同一 Vercel 团队下的不同项目；独立环境变量、数据、CORS、CI；不共享代码或运行期。
9. 其他项目的阻塞是 Turso 数据库配额；StarHub 不使用 Turso，因此 Vercel 部署不被这项配额卡住。StarHub 使用团队 `kwei888` 下的独立项目，方案仍支持“Vercel 不可用时静态降级”，核心阅读不依赖运行时 API。

### 当前事实（2026-10-23 核对）

- 线上页面最后更新停在 2026-09-11，已 42 天。
- `update.yml` 连续失败：阻断点是 RSS 的 A2 门禁，以及 Pages/Vercel 未发布判定；RSS 删除后这条阻断随之消失。
- 本仓库 `.vercel/project.json` 与工作流中的 Vercel 目标仍指向 `team_iovv… / starhub-refresh`（上游团队/项目），不是用户自己的部署。
- 用户自己的 Vercel 团队是 `kwei888`（team_lhu…，OWNER），当前只有 `qwis-intel` 一个项目；StarHub 尚未创建自己的项目。
- `D:\全网情报系统` 是同一 Vercel 团队下的另一个独立项目（qwis-intel / news.ghoustlab.com），有自己的数据与 CI；StarHub 不得依赖它。
- “占用满”的是 qwis-intel 的 Turso 数据库配额，不是 StarHub 的 Vercel 资源；StarHub 没有 Turso 依赖。Vercel 接入只需在 `kwei888` 团队下新建独立项目。

### 实施状态（2026-10-23）

- Vercel 项目 `starhub` 已在团队 `kwei888` 创建（项目 ID `prj_n3vCEbSo9H4MxRogdjARvyBxLXkU`），尚未部署 API。
- 本地解阻提交 `84f5f789`：数据源切到 `ghoustghoust`（134 stars）；移除 RSS/每日洞察生成调用；删除上游 Vercel 同步工作流；Pages 改为 Actions artifact；构建产物移出 git。
- 新 `update.yml`：每日 09:00 + 手动触发，构建 → 校验 → Pages 发布 → 数据回写；暂用 `STARHUB_FAST=1`，未接模型密钥，避免免费翻译长尾与未授权计费。
- 线上验证：主站 134 项、更新时间 2026-10-23 08:56；AI 晨报 72 条、6 个分类；`rss-aggregator.html` 返回 404。
- 已删除 RSS/每日洞察的代码、数据、API、测试与依赖；`api/_lib/translate.js` 保留给搜索复用。
- Phase 4 起步：`config/site.json`、`pipeline/starhub_pipeline`（配置/状态/CLI）、`data/schema` 已落地；构建已导出 `data/stars.json`、`data/categories.json`、`data/trending.json`、`data/build-status.json`，`validate` 已接入工作流。
- 主站 CSS 已抽到 `assets/css/main.css`，主站脚本已抽到 `assets/js/main.js`，Pages 走外部资源；本地与线上首屏截图对比一致。
- GitHub star 采集已抽到 `pipeline/starhub_pipeline/sources/github.py`，账号与 Pages 地址由 `config/site.json` 提供，不再硬编码。
- 自有 Vercel API 已上线 `https://starhub-drab.vercel.app`：search/events/news/refresh 已验证；CORS 使用 `Vary: Origin` + `Cache-Control: no-store`；前端 `API_BASE` 构建期注入；静态资源带构建版本号避免 Pages 缓存旧 JS。
- 新增 `deploy-api.yml`：API 文件变更时自动部署到 Vercel；Pages 仍由 `update.yml` 发布。
- 下一步：继续拆分搜索/筛选/榜单等功能模块，并迁移 AI 晨报。

## 1. 为什么要重构（因果链）

现状不是“代码旧”，而是四类结构性耦合：

- **同一能力多份实现**：RSS 解析、正文清洗、翻译、相对时间、分类去重分散在构建脚本、Vercel 函数与前端内联脚本中，修改一处必须同步多处。
- **功能与页面互相绑死**：每日洞察依赖 RSS 历史，AI 晨报又被洞察注入；删除 RSS 会连带发布链、健康检查、测试门禁断裂。
- **身份硬编码在代码里**：仓库 owner、站点标题、CORS 白名单、API 域名、关注用户名、导出备份里的 user 都写死为上游身份，fork 后形成隐性依赖。
- **生成物与源码混在一起**：页面 HTML、内联 CSS/JS、数据快照、构建脚本同处根目录；改一个功能需要读懂并替换一整段生成字符串。

重构要达到的效果：

- 功能按业务能力拆分，互不越界引用；
- 数据、渲染、交互、API 四层只通过契约通信；
- 三页共用一套设计令牌、布局壳和基础组件；
- 删除 RSS/洞察后，管线与发布链显著变短；
- 本仓库可以独立配置、独立部署、独立测试。

非目标：

- 不重做视觉风格，不调整主站信息架构；
- 不引入重型前端框架；
- 不保留上游 merge 兼容；
- 不为了“看起来新”而替换仍可靠的运行组件。

## 2. 范围边界

### 2.1 保留（功能对等）

**主站**

- 收藏台：总数/分类/语言/收藏统计、语言分布、分类导航、语言与星标筛选、更新时间筛选、排序、卡片/列表视图。
- 本地搜索：关键词检索、查询历史、清空、空态与筛选重置、语义扩展召回。
- 全网搜索：GitHub 跨语言搜索、查询词编辑、语言过滤、排序标签、分页/重试、已收藏标记。
- 收藏与备份：收藏置顶、取消收藏、JSON 导出/导入、跨域/换浏览器后的已收藏识别。
- 外观与交互：明暗主题、视图切换、键盘快捷键、Toast、返回顶部。
- 排行榜：每日三榜（涨星/总星/新秀）、AI 摘要、完整 Top20 弹层。
- 关注动态：近 24 小时 GitHub 关注事件。
- AI 动态流：AIHOT 条目、36氪/Redis 补充源、分类筛选、加载更多。
- 注意力提醒：star 拉取不完整时的提示块。
- 手动刷新：触发本仓库的更新工作流，并反馈结果。

**AI 晨报**

- 报纸式刊头、日期/期号、今日条目数、分类索引。
- 头条区、分类正文、快讯流、来源与原文直达链接、归属说明。
- 明暗主题、返回主站。
- 数据源：AIHOT、Hacker News、The Verge、TechCrunch、arXiv、36氪、AtlasNote。
- 翻译、去重、分类、头条评分、失败回退。

**数据与运维**

- 星标分类映射、中文描述、人工备注、星数增长基线、AI 摘要、晨报快照、构建状态。
- 搜索、关注动态、AI 新闻、刷新、健康检查五类运行时 API。
- GitHub Actions 定时更新与手动触发。
- 现有 localStorage 键与备份文件格式。

### 2.2 删除

- `rss-aggregator.html` 及全部 RSS 聚合页面能力。
- RSS 相关 Vercel 函数：聚合、全文抓取、AGI Hunt 代理。
- RSS 数据与缓存：信源表、历史正文、API 快照、分块 JS、封面/留存规则、RSS 翻译缓存、热榜与趋势快照。
- OPML 导出、阅读器抽屉、原站/原文按钮、二维码分享、微信跳转清理。
- 每日洞察：洞察引擎、历史页、AI 晨报中的洞察区块、嵌入缓存与相关测试。
- 上游同步流程、上游身份硬编码、上游兼容门禁。
- 只为上述功能存在的测试、工具脚本、Actions 缓存与发布步骤。

### 2.3 解释性边界

- **AI 晨报内部抓取公开 feed 保留。** 删除的是用户可见的“RSS 订阅/聚合/阅读器”，不是晨报获取内容的技术手段。
- **顶部“学习资源 / 资讯平台 / AI 工具”外链保留。** 它们不属于 RSS 或中转跳转；如也要移除，需要另行确认。
- **外链一律直达。** 不设中转路由、不包装跳转页、不经过阅读器；用标准外链属性保证安全。
- **`/api/news` 保留。** 它是主站 AI 动态的跨域数据代理，返回 JSON，不承担跳转职责。

### 2.4 禁止事项

- 任何页面、API、数据结构、构建步骤中不得重新引入 RSS 聚合或每日洞察。
- 任何功能模块不得直接引用另一个功能模块的内部状态或 DOM。
- 数据契约中不得出现上游原始响应、未清洗 HTML 或临时字段。
- 前端不得把数据/API 返回内容直接拼进 `innerHTML`；必须走转义或受控渲染。
- 不得在客户端存放 GitHub token 或模型密钥。
- 不得硬编码仓库 owner、关注用户名、站点域名、API 域名、CORS 白名单。
- 不得改动现有 localStorage 键；备份导入必须兼容旧格式。
- 抓取失败时不得用空数据覆盖上一份可用快照。
- 生成页面不得再提交进 git；页面作为发布产物。

## 3. 目标架构

```text
数据源
  GitHub / AIHOT / HN / 公开 feed
        │
        ▼
pipeline（采集 → 归一化 → 加工 → 生成契约）
        │
        ▼
data/*.json（唯一跨层接口）
        │
        ├──────────────► renderer（Jinja2 布局与页面壳）──► public/（Pages 产物）
        │
        └──────────────► Vercel API（运行时查询/代理/刷新/健康）
                                  │
                                  ▼
                          前端功能模块（ES Modules）
```

### 3.1 目录结构

```text
starhub/
├─ pipeline/                  # Python 数据管线（唯一构建入口）
│  ├─ pyproject.toml
│  ├─ starhub_pipeline/
│  │  ├─ cli.py               # update / render / validate
│  │  ├─ config.py            # 站点配置与环境变量
│  │  ├─ state.py             # 原子读写、快照回退、契约校验
│  │  ├─ sources/             # github / aihot / hackernews / feeds / arxiv
│  │  ├─ enrich/              # classify / translate / summarize / embed / health
│  │  ├─ build/               # stars / trending / ai_daily
│  │  └─ render/              # Jinja2 环境与过滤器
│  └─ tests/
├─ site/                      # 前端源码
│  ├─ templates/              # base + index + ai-daily + partials
│  ├─ assets/css/             # tokens / base / components / pages
│  └─ assets/js/
│     ├─ shared/              # http / store / dom / time / format / toast
│     ├─ features/            # 按业务能力分包
│     └─ entries/             # main / ai-daily 两个入口
├─ api/                       # Vercel Functions
│  ├─ _lib/                   # github / http / respond / auth / translate
│  ├─ search.js
│  ├─ events.js
│  ├─ news.js
│  ├─ refresh.js
│  └─ health.js
├─ data/                      # 提交入库的状态与快照
│  ├─ schema/                 # JSON Schema
│  ├─ stars.json
│  ├─ categories.json
│  ├─ descriptions.json
│  ├─ notes.json
│  ├─ trending.json
│  ├─ events.json
│  ├─ ai-feed.json
│  ├─ ai-daily.json
│  └─ build-status.json
├─ public/                    # 生成产物，不入库，由 Actions 发布
├─ tests/                     # 契约 / API / 前端逻辑 / 端到端
├─ config/
│  └─ site.json               # 站点身份、导航、API 基址（无密钥）
├─ .github/workflows/update.yml
└─ docs/
```

### 3.2 依赖规则

- `site/features/*` 只能依赖 `site/shared/*` 和页面注入的上下文，功能之间通过页面组合，不互相 import。
- `api/*` 只能依赖 `api/_lib/*`；不得 import 前端代码或管线代码。
- `pipeline` 内部单向依赖：`sources → enrich → build → render`，禁止反向。
- 跨语言、跨运行时的唯一接口是 `data/schema/*` 与 API 响应契约。
- 页面入口负责组装功能；功能模块不得自行决定页面布局。
- 共享能力只允许一份：时间、格式化、HTTP、主题、Toast、转义。

## 4. 功能解耦矩阵

| 功能 | 页面 | 数据来源 | 目标模块 | 形态 |
|---|---|---|---|---|
| 收藏台与筛选 | 主站 | `data/stars.json` | `features/collection` | 运行时 |
| 收藏置顶/导入导出 | 主站 | localStorage | `features/favorites` | 运行时 |
| 本地搜索/语义扩展 | 主站 | stars 数据 | `features/local-search` | 运行时 |
| 全网搜索 | 主站 | GitHub API | `features/global-search` + `api/search` | 运行时 |
| 排行榜与 AI 摘要 | 主站 | `data/trending.json` | `features/trending` | 构建 + 运行时 |
| 关注动态 | 主站 | `data/events.json` + GitHub API | `features/events` + 可选 `api/events` | 构建快照 + 可选实时 |
| AI 动态流 | 主站 | `data/ai-feed.json` + AIHOT/`api/news` | `features/ai-feed` + 可选 `api/news` | 构建快照 + 可选实时 |
| 主题/视图/快捷键 | 两页 | localStorage | `features/preferences` | 运行时 |
| 手动刷新 | 主站 | Actions API | `features/refresh` + `api/refresh` | 运行时 |
| AI 晨报 | 晨报页 | `data/ai-daily.json` | `features/report` | 构建 + 少量运行时 |
| 健康状态 | 无独立页面 | `data/build-status.json` + Actions | `api/health` | 构建 + 运维 |
| RSS/洞察/跳转 | — | — | 删除 | — |

## 5. 数据契约

### 5.1 原则

- 契约带版本号，字段统一 `snake_case`。
- 时间统一 ISO-8601 UTC；时区转换只发生在前端展示层。
- 核心字段稳定；可选增强字段允许缺省，前端必须容忍缺失。
- 所有 URL 必须是绝对 HTTPS 地址，直接指向内容源。
- 契约里不出现原始上游响应、HTML 字符串、密钥、调试字段。
- 管线写文件必须原子化；校验失败保留上一份可用快照。
- 分类、描述、备注等用户数据只允许增量补充，不允许静默清空。

### 5.2 示例

`data/stars.json`

```json
{
  "schema_version": 2,
  "generated_at": "2026-10-23T01:00:00Z",
  "items": [
    {
      "id": "owner/repo",
      "full_name": "owner/repo",
      "html_url": "https://github.com/owner/repo",
      "description": "一句话中文描述",
      "language": "Python",
      "stars": 1234,
      "topics": ["agent", "llm"],
      "pushed_at": "2026-10-20T12:00:00Z",
      "starred_at": "2026-10-21T12:00:00Z",
      "category": "agent",
      "category_label": "AI Agent & Skills",
      "note": "",
      "health": { "level": "ok", "reason": "" },
      "embedding": [0.01, -0.02]
    }
  ]
}
```

`data/categories.json`

```json
{
  "schema_version": 2,
  "updated_at": "2026-10-23T01:00:00Z",
  "map": { "owner/repo": "agent" },
  "labels": { "agent": "AI Agent & Skills" }
}
```

`data/trending.json`

```json
{
  "schema_version": 2,
  "generated_at": "2026-10-23T01:00:00Z",
  "summary": { "headline": "今日涨星主线", "bullets": ["要点一", "要点二"] },
  "boards": {
    "rising": [{ "full_name": "owner/repo", "stars": 1234, "delta": 321 }],
    "total": [{ "full_name": "owner/repo", "stars": 1234 }],
    "new": [{ "full_name": "owner/repo", "stars": 1234 }]
  }
}
```

`data/ai-daily.json`

```json
{
  "schema_version": 2,
  "report": {
    "date": "2026-10-23",
    "generated_at": "2026-10-23T00:00:00Z",
    "window_start": "2026-10-22T00:00:00Z",
    "window_end": "2026-10-23T00:00:00Z",
    "sections": [
      {
        "key": "ai-models",
        "label": "AI 模型",
        "items": [
          {
            "title": "标题",
            "summary": "纯文本摘要",
            "source": "来源名",
            "url": "https://example.com/article",
            "published_at": "2026-10-22T08:00:00Z"
          }
        ]
      }
    ],
    "flashes": []
  }
}
```

`data/events.json`

```json
{
  "schema_version": 2,
  "generated_at": "2026-10-23T01:00:00Z",
  "items": [
    {
      "kind": "release",
      "actor": "octocat",
      "repo": "owner/repo",
      "title": "v1.2.0",
      "url": "https://github.com/owner/repo/releases/tag/v1.2.0",
      "occurred_at": "2026-10-22T08:00:00Z"
    }
  ]
}
```

`data/ai-feed.json`

```json
{
  "schema_version": 2,
  "generated_at": "2026-10-23T01:00:00Z",
  "items": [
    {
      "title": "标题",
      "summary": "纯文本摘要",
      "source": "36氪",
      "category": "AI 产品",
      "url": "https://example.com/article",
      "published_at": "2026-10-22T08:00:00Z"
    }
  ]
}
```

`data/build-status.json`

```json
{
  "schema_version": 2,
  "last_success_at": "2026-10-23T01:00:00Z",
  "sources": {
    "github": { "status": "ok", "item_count": 424 },
    "ai_daily": { "status": "degraded", "reason": "fallback snapshot" }
  }
}
```

### 5.3 契约禁令

- 不得把 `emb`、`raw`、`debug`、请求头、token、临时路径写进对外契约。
- 不得在 `description`、`summary` 中夹带可执行 HTML。
- 不得让不同数据文件各自维护一份分类标签；标签只在分类契约里。
- 不得让页面直接读上游响应；只能读契约或 API 响应。

## 6. 前端重构

### 6.1 统一布局与设计系统

- 一份设计令牌：颜色、字体、字号、间距、圆角、阴影、明暗主题。
- 一份基础样式：重置、排版、焦点态、链接、可访问性。
- 一份组件样式：页头、导航、按钮、卡片、标签、弹层、Toast、空态、骨架。
- 页面级 CSS 只保留布局差异，不再复制组件样式。
- `base` 模板统一页头、导航、主题切换、页脚与返回顶部。
- 主站保留“搜索区 + 主内容 + 侧栏”的两栏结构；晨报保留报纸式阅读结构；两者共用页头、令牌和响应式规则。

### 6.2 交互模块

- 原生 ES Modules，不引入前端框架运行时。
- 每个功能模块负责自己的数据获取、状态与渲染，挂载在独立根节点上。
- 页面级 store 只保存跨模块共享的最小状态；筛选、搜索、收藏各自管理内部状态。
- 所有请求统一走共享 HTTP 模块：超时、取消、错误归一化、缓存头处理。
- 搜索、侧栏动态等非首屏内容按需加载；首屏只加载当前页面需要的模块。
- 事件、AI 动态、排行榜各自独立失败，不得互相拖垮页面。

### 6.3 兼容性红线

- 保留现有 localStorage 键：收藏、主题、视图、搜索历史。
- 备份导出继续包含标识、导出时间、收藏列表；导入同时接受 `favorites` 与旧 `favs` 字段。
- 导入时只接受当前数据集中存在的仓库，行为与现状一致。
- 站点换域后，已收藏识别继续以“当前 star 数据 + 本地收藏”合并判断。

### 6.4 安全与可访问性

- 外链直达，统一 `noopener`；不使用中转或跳转页。
- 数据渲染默认转义；只有构建期已清洗的受限富文本可以受控插入。
- 键盘快捷键、焦点管理、弹层 Esc 关闭、`aria` 标签保持现有体验。
- 尊重 `prefers-reduced-motion`；主题切换不闪烁。

## 7. 数据管线

- 保留 Python：删除 RSS/洞察后，剩余管线是经过验证的领域逻辑，重写收益低于风险。
- 统一入口：一次 `update` 完成星标、榜单、关注动态快照、AI 动态快照、AI 摘要、AI 晨报；`render` 只负责生成页面；`validate` 独立可跑。
- 关注动态与 AI 动态默认落构建期快照，保证没有 Vercel 时仍可阅读；运行时 API 只负责“更新到此刻”，不是首屏依赖。
- 管线只产出契约，不拼页面字符串；页面模板由渲染层负责。
- 采集与加工分离：每个数据源适配器只负责取数与归一化，分类/翻译/摘要各自独立。
- 所有外部调用有超时、重试上限与降级路径；单源失败不阻断整场构建。
- 星标拉取失败时保留上一份 star 契约；晨报失败时保留上一份晨报；只更新状态文件并标记 degraded。
- 配置只从 `config/site.json` 与环境变量读取；owner、仓库、关注用户、站点域名、API 基址不得写死。
- 关注动态优先使用当前 token 对应的账号，不再写死某个用户名。

## 8. Vercel API

保留：

- 搜索：查询翻译、GitHub 搜索、分页、限流、缓存。
- 关注动态：当前账号关注列表的近期公开事件。
- AI 新闻：36氪/Redis 等源的跨域代理与摘要清洗。
- 刷新：触发本仓库更新工作流；并发保护；响应只返回结果，不暴露 token。
- 健康：最近成功构建、页面可达性、核心数据是否退化；删除 RSS 阅读器探针。

删除：

- RSS 聚合、全文抓取、AGI Hunt 代理。
- 独立翻译端点；翻译函数下沉为共享库，仅供搜索复用。

统一约定：

- 一致的错误结构：错误码 + 可读信息，不返回堆栈。
- CORS 来源来自环境配置，不再写死上游域名。
- 只读接口允许短时缓存；写接口不缓存。
- 客户端持有的刷新口令只作为滥用威慑，不作为授权凭证；服务端必须有限流与并发锁。

## 9. CI/CD 与部署

### 9.1 部署身份与独立性

- StarHub 使用用户自己的 Vercel 团队 `kwei888`（team_lhu…）下的独立项目，项目名暂定 `starhub`，与 `qwis-intel` 并列但不共享运行期。
- 不得继续使用上游 `team_iovv… / starhub-refresh`；也不得把 `D:\全网情报系统` 当共享后端。
- StarHub 与 `D:\全网情报系统` 同一团队但不同项目：独立环境变量、独立数据、独立 CORS、独立工作流、独立域名。
- StarHub 不使用 Turso 或任何共享数据库；所有状态以 `data/*.json` 与 Actions artifact 为准。
- 允许复用同一模型供应商的密钥值，但必须在各自项目里分别配置；禁止跨项目调用 API、共享数据库/缓存或互相 import 代码。
- 站点身份全部配置化：仓库 owner/repo、站点标题、关注用户、Pages 域名、API 基址、CORS 来源、备份导出 user。
- `.vercel/project.json` 只保留本仓库自己的绑定；工作流不得内嵌上游 project/org ID。
- 前端不得写死 `starhub-refresh.vercel.app` 一类地址；API 基址在构建时注入。
- `refresh`、`events`、`health` 也必须指向本仓库与本项目，而不是上游。

### 9.2 发布顺序与受限状态

其他项目受阻于 Turso 配额；StarHub 不使用 Turso，Vercel 接入不被这项配额卡住。发布按两步走：

1. **先发布静态站**：管线在本仓库 Actions 中运行，生成最新数据与页面，直接发布 GitHub Pages。此阶段不需要 Vercel，先让 42 天的数据恢复更新。
2. **在 `kwei888` 团队新建 `starhub` 项目后接运行时 API**：部署搜索/动态/新闻/刷新/健康接口，切换 `API_BASE` 与 CORS，验证后启用完整功能。项目与 qwis-intel 并列，不共享任何运行期资源。

静态降级已确认，Vercel 不可用期间行为如下：

- 收藏台、筛选、本地搜索、收藏、排行榜、AI 摘要、关注动态快照、AI 动态快照、AI 晨报、主题与导入导出继续可用。
- 全网搜索、实时动态刷新、手动刷新等依赖运行时 API 的能力显示明确的“暂不可用”状态，不阻塞核心阅读。
- 降级状态必须可见，不能静默假装在线。

### 9.3 工作流

- 一个更新工作流：检出 → 安装依赖 → 恢复必要缓存 → 跑管线 → 校验契约 → 跑测试 → 渲染站点 → 发布 Pages → 按需部署自己的 Vercel → 提交数据变更。
- 删除 RSS/洞察缓存、阅读器数据 Stage、上游同步、历史守卫与大体积产物裁剪步骤。
- 生成页面不再提交；数据快照与用户数据提交。
- 页面发布用 Actions artifact；Vercel 只负责 API。
- 定时频率建议收敛为“每日一次全量 + 手动触发”；如果仍需盘中刷新，再加一个只更新星标的轻量任务，而不是恢复五场全量构建。
- 密钥只保留实际使用的项：GitHub token、模型密钥、自己的 Vercel token、刷新口令。删除仅供 RSS/洞察使用的密钥。
- 工作流失败时不得发布空页面；保留上一版 Pages 产物。
- 部署失败不得回退到上游项目，也不得把失败静默成绿色。

## 10. 迁移阶段与验收

### Phase 0：冻结与基线

- 建重构分支与回退标签，记录当前提交。
- 截取桌面/移动、明暗主题的主站与晨报基线。
- 备份并核对分类、描述、备注、收藏导出文件的数量。
- 标记 RSS 数据归档点；不直接删除。

验收：基线可复现，数据有备份，回退路径明确。

### Phase 1：骨架与设计系统

- 建 `pipeline/`、`site/`、`api/`、`data/`、`tests/`。
- 落共享令牌、基础样式、组件样式、base 模板与页面壳。
- 建配置入口、契约 Schema、原子写入与校验。

验收：新壳与旧站视觉一致；主题、导航、响应式无回归；旧站仍可用。

### Phase 2：星标管线

- 拆分采集、分类、翻译、摘要、嵌入、增长基线。
- 产出新契约；与旧数据做字段级对比。
- 补离线夹具与单元测试。

验收：核心字段一致；分类/描述/备注数量不减少；失败时保留旧快照。

### Phase 3：主站功能迁移

- 按收藏台与筛选 → 搜索 → 排行榜 → 动态流 → 刷新/备份的顺序迁移。
- 每个功能独立挂载、独立测试、独立截图对比。
- 保留 localStorage 与备份格式兼容。

验收：功能对等清单全部通过；键盘、空态、错误态、移动端无回归。

### Phase 4：AI 晨报迁移

- 迁移采集、翻译、去重、分类、评分与页面。
- 删除每日洞察注入；保留晨报自身的降级回退。

验收：栏目、条目数、来源链接、主题与旧版一致；失败回退可用。

### Phase 5：删除与收口

- 删除 RSS/洞察代码、数据、测试、依赖、工作流步骤。
- 删除上游身份与同步流程；配置化 owner、域名、API 基址。
- 精简 Vercel 配置与 `package.json`。

验收：仓库中不再有 RSS/洞察引用；构建产物无 RSS 路由与数据；API 正常。

### Phase 6：硬化与上线

- 可访问性、性能、安全自查；真实浏览器走查。
- 生产部署演练；失败回滚演练。
- 更新 README 与运维说明。

验收：CI 全绿；Pages/Vercel 发布成功；无控制台错误；旧数据与收藏无丢失。

### 最终验收清单

- 主站与晨报所有保留功能可用。
- 页面布局、视觉语言、信息架构与旧版一致。
- 无 RSS 页面/API/数据/网络请求；无原文跳转包装；无每日洞察。
- 分类、描述、备注、收藏、主题、视图、搜索历史完整保留。
- 搜索、动态、刷新、健康接口正常，CORS 与限流生效。
- 构建失败不会覆盖可用数据或发布空页面。
- 桌面/移动、明暗主题、键盘操作通过走查。
- 无客户端密钥泄露；无未转义数据注入。

## 11. 风险与回滚

- **数据丢失**：迁移前备份并做数量校验；契约校验不通过即中止。
- **GitHub 限流**：保留缓存与降级；搜索与动态接口限流。
- **模型/翻译服务不可用**：保留旧摘要/晨报，状态标记 degraded。
- **部署权限**：Pages/Vercel 发布步骤失败不抹掉上一版产物。
- **刷新接口滥用**：并发锁 + 服务端限流；客户端口令可轮换。
- **视觉回归**：每阶段截图对比；布局改动需单独确认。
- **回滚**：Phase 0 标签 + 每阶段独立提交；删除 RSS 前保留归档标签。

## 12. 默认假设与待确认项

默认假设：

1. 继续使用 GitHub Pages + 用户自己的 Vercel 团队 `kwei888`（team_lhu…）下的独立项目，不迁移到单一平台。
2. 顶部“学习资源 / 资讯平台 / AI 工具”外链保留。
3. AI 晨报内部的公开 feed 抓取保留。
4. 主站与晨报保持现有信息架构，不做视觉重设计。
5. 前端使用原生 ES Modules + Jinja2 模板，不引入 React/Vue/Next。
6. 更新频率收敛为每日一次全量 + 手动触发；如需盘中刷新，另设星标轻量任务。
7. StarHub 与 `D:\全网情报系统` 同一 Vercel 团队但不同项目；不共享项目、环境、数据与运行期，只允许各自独立配置同一供应商的密钥值。
8. 构建期快照优先：关注动态、AI 动态、AI 晨报、榜单都能在没有 Vercel 时阅读；运行时 API 只做实时增强。

已确认：

- StarHub 使用用户自己的 Vercel 团队 `kwei888` 下的独立项目，与 qwis-intel 并列但完全独立。
- 接受 Vercel 不可用期间的静态降级。
- 占用满的是 qwis-intel 的 Turso 数据库配额，StarHub 没有 Turso 依赖。

需要你操作/确认：

- 确认项目名 `starhub`，并允许创建项目、配置部署令牌。令牌只进 GitHub Actions secrets，不入库；已在聊天中暴露的令牌请在配置完成后轮换。
- 是否现在开始实施 Phase 0。

确认后从 Phase 0 开始，按阶段提交与验收。
