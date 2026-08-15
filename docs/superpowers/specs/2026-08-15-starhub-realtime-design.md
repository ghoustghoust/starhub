# StarHub 实时更新设计文档

> **日期：** 2026-08-15
> **状态：** 已确认（用户逐项确认需求）

## 1. 需求

将 StarHub 从「每日定时更新」升级为「打开页面即最新 + 常开时 5 分钟自动刷新」：

- 打开页面：先渲染静态数据（秒开），随后静默拉取最新 star 列表（含元数据）并替换渲染
- 页面常开：每 5 分钟轮询一次（页面不可见时暂停）
- 页面内收藏（localStorage favs）：元数据随实时数据自动刷新（零改造）
- 无需主动通知；发现新 star 时显示轻提示 toast「发现 N 个新收藏」
- 个人使用，公开访问亦可（数据本身公开）

## 2. 技术约束（已确认）

- GitHub **无用户级 star webhook** → 事件驱动降级为 5 分钟轮询（用户已确认接受）
- GitHub API 匿名限额 60 次/h，全量拉取不够 → **必须经 Vercel 代理**（复用现有 `GH_TOKEN` 环境变量，5000 次/h）
- 代理函数沿用现有 `api/refresh.js` 的 CORS / 部署 / 环境变量模式

## 3. 架构

```
浏览器 (index.html)
  │ 1. 渲染静态 DATA（秒开，失败兜底）
  │ 2. fetch GET /api/stars ← 新增 Vercel Serverless 函数
  │ 3. 返回最新 star 列表（full_name/description/html_url/stargazers_count/language）
  │ 4. 前端替换 DATA 重新渲染（分类/搜索/收藏逻辑复用）
  │ 5. setInterval 5min 重复 2-4（document.hidden 暂停）
  ▼
Vercel api/stars.js
  │ GH_TOKEN → GET api.github.com/users/Kwei168/starred?per_page=100&sort=created
  │ 翻页全量（≤10 页），映射为 DATA 兼容结构，返回 JSON
  ▼
GitHub REST API v3
```

## 4. 数据与降级策略

| 数据 | 实时层 | 每日构建层（保持不变） |
|---|---|---|
| star 列表 + 元数据 | ✅ 页面实时拉取 | 持久化到 index.html 静态基线 |
| 新项目分类 | 临时：未分类/「最新」组，英文描述 | 次日 07:35 正式分类 + 中文翻译 |
| trending / feed | ❌ 保持每日更新 | ✅ 维持现状 |
| 页面收藏 favs | ✅ 元数据随列表自动刷新 | — |

降级链路：代理失败 / GitHub API 异常 → 静默回退静态数据，不影响页面可用性。

## 5. 验收标准

1. 打开页面：静态数据立即显示，随后（≤3s）替换为实时数据，无白屏闪烁
2. star 一个新仓库后 5 分钟内，页面（打开或常开）自动出现该项目
3. 新项目显示英文描述、无正式分类；次日构建后分类与翻译转正
4. 页面内收藏项目的 star 数/描述随实时刷新变化
5. 代理 500 错误时页面保持静态数据正常显示（控制台有 warning 即可）
6. 现有功能（分类筛选、搜索、收藏、主题、trending、feed、刷新按钮）全部不回归

## 6. 范围外（明确不做）

- 主动通知（邮件/浏览器推送）——用户已选「看到即可」
- trending / feed 实时化
- localStorage 收藏跨设备同步
- 部署自动化（Vercel Git 集成仍受 commit-author block 限制，维持 CLI 部署）

## 7. 工作量预估

- 新增 `api/stars.js`（约 60 行）：Vercel 函数 + 翻页 + 字段映射
- `template.html` JS 改造（约 60 行）：fetch 实时数据、合并渲染、5 分钟定时器、toast 提示、失败回退
- 测试：本地模拟代理响应、页面端到端验证、部署后真实拉取验证
