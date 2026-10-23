# GitHub Star 收藏台

个人 GitHub Star 项目收藏台：自动拉取 star 列表、智能分类展示，支持模糊搜索、语言/星标筛选、收藏置顶。托管在 GitHub Pages，通过 GitHub Actions 每天自动更新。

在线地址：https://ghoustghoust.github.io/starhub/

## 仓库结构

- `template.html` — 主站模板（数据用占位符）
- `assets/` — 主站共享 CSS 与脚本
- `config/site.json` — 站点身份、仓库与 Pages 配置
- `pipeline/` — 新数据管线（配置、契约、CLI、来源适配与测试）
- `fetch_and_build.py` — 构建入口：拉取 star → 分类 → 生成主站与 AI 晨报
- `build_ai_daily.py` — AI 晨报生成器
- `known_categories.json` — 已知项目分类映射（保持分类稳定，随运行自动增长）
- `docs/rebuild-architecture.md` — 重构架构与迁移计划
- `.github/workflows/update.yml` — 每日构建、Pages 发布与数据回写

`index.html` 与 `ai-daily.html` 是构建产物，由 Actions artifact 发布，不提交进 git。

## 自动更新

每天北京时间 09:00（UTC 01:00）自动运行，拉取最新 star 列表、生成主站与 AI 晨报，通过 Actions artifact 发布到 GitHub Pages，并把分类/描述/榜单/晨报数据提交回仓库。也可在 Actions 页手动触发。

## 本地构建

```bash
cp .env.example .env   # 填入 GH_TOKEN / GITHUB_TOKEN
python fetch_and_build.py
```

构建产物为 `index.html` 与 `ai-daily.html`。CI 当前使用 `STARHUB_FAST=1` 跳过免费翻译端点的长重试；带缓存的增强管线在重构中恢复。

## 自定义域名

在仓库 Settings → Pages 里设置自定义域名，并在 DNS 添加一条 CNAME 记录指向 `ghoustghoust.github.io`，再把域名填入 Pages 的 Custom domain 即可。
