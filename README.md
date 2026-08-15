# GitHub Star 收藏台

个人 GitHub Star 项目收藏台：自动拉取 star 列表、智能分类展示，支持模糊搜索、语言/星标筛选、收藏置顶。托管在 GitHub Pages，通过 GitHub Actions 每天自动更新。

在线地址：https://ghoustghoust.github.io/starhub/

## 仓库结构

- `index.html` — 工作台页面（脚本自动生成，Pages 入口）
- `template.html` — 页面模板（数据用占位符）
- `fetch_and_build.py` — 自动更新脚本：拉取 star → 分类 → 生成 index.html
- `known_categories.json` — 已知项目分类映射（保持分类稳定，随运行自动增长）
- `.github/workflows/update.yml` — 每天定时运行的 GitHub Actions

## 自动更新

每天北京时间 09:00（UTC 01:00）自动运行，拉取最新 star 列表、智能分类、重新生成页面并提交，Pages 自动重新部署。也可在 Actions 页手动触发。

## 自定义域名

在仓库 Settings → Pages 里设置自定义域名，并在 DNS 添加一条 CNAME 记录指向 `ghoustghoust.github.io`，再把域名填入 Pages 的 Custom domain 即可。
