# GitHub API 速率限制与最佳实践

> 基于 2025-09 调研结果，适用于 StarHub 与全网情报系统

## 速率限制概览

| 类别 | 限制 | 计算维度 | 说明 |
|------|------|----------|------|
| **匿名请求** | 60 次/小时 | 源 IP | 2025-05 后进一步收紧 |
| **认证请求（PAT/OAuth）** | 5,000 次/小时 | Token | Fine-grained PAT 同理 |
| **GitHub App** | 5,000~15,000 次/小时 | 安装实例 | 取决于安装范围 |
| **次级限流（Abuse Detection）** | 并发请求限制 | 时间窗口 | 短时间大量并发触发 429 |

## 响应头关键字段

```http
X-RateLimit-Limit: 5000          # 总额度
X-RateLimit-Remaining: 4985      # 剩余额度
X-RateLimit-Reset: 1693012800    # 重置时间（Unix 秒）
Retry-After: 60                  # 次级限流时等待秒数（仅 429 响应）
```

## 错误码含义

| 状态码 | 含义 | 处理方式 |
|--------|------|----------|
| **403** + `remaining=0` | 主限流触发 | 等待 `X-RateLimit-Reset` 时间 |
| **429** | 次级限流（滥用检测） | 按 `Retry-After` 头等待 |
| **404** | 资源不存在 | 检查 URL/权限，不要重试 |

## StarHub 项目分析

### 当前消耗量

`api/events.js` 单次调用：
- 获取关注列表：1~5 页（~5 请求）
- 每用户事件：100 用户 × 2 页 = ~200 请求
- **单次总计：~205 请求**

缓存策略：
- 服务端：10 分钟 TTL
- 前端：30 分钟轮询（自适应调整）
- **每小时最多 2 次实际调用 → ~410 请求/小时**

额度使用率：**410 / 5000 = 8.2%** ✅ 完全安全

### 已实现的防护措施

1. **速率追踪**：每次 API 响应解析 `X-RateLimit-*` 头
2. **自适应降频**：
   - 额度 > 50%：30 分钟轮询
   - 额度 20%~50%：45 分钟轮询
   - 额度 < 20%：60 分钟轮询
3. **429 响应**：返回 `Retry-After` 头，前端据此延长下次轮询
4. **缓存命中零开销**：命中缓存时不调用 GitHub API

### 推荐配置

| 场景 | 认证状态 | 推荐轮询间隔 | 说明 |
|------|----------|--------------|------|
| 正常使用 | 认证（GH_TOKEN） | 30 分钟 | 当前配置，额度充足 |
| 高频需求 | 认证 | 可降至 10~15 分钟 | 额度仍够用（~15% 使用率） |
| 匿名访问 | 未认证 | ≥ 30 分钟 | 60 次/小时限制，必须保守 |
| 额度紧张 | 认证（< 20%） | 自动 60 分钟 | 代码已实现自适应 |

## 全网情报系统分析

### 关键结论

**全网情报系统不直接调用 GitHub API。**

- RSS 源抓取走标准 RSS/Atom 协议（`server/services/collectors/rss/index.js`）
- 即使某些 RSS 链接来自 GitHub（如 releases feed），也是 RSS 解析，不计入 GitHub API 限额
- **无 GitHub 风控风险**

### 当前配置

- RSS 源默认间隔：**30 分钟**（`settings.intervals.rss = 0.5` 小时）
- 调度器每 60s 扫描到期源
- 队列并发 5 消费

### 推荐

- 30 分钟间隔对 RSS 源合理，无需调整
- 如需更高频，可降至 15 分钟（`intervals.rss = 0.25`）

## 部署验证清单

### Vercel 部署后检查

1. **确认环境变量**：
   ```bash
   vercel env ls | grep GH_TOKEN
   ```

2. **检查 API 响应**：
   ```bash
   curl -I https://starhub-refresh.vercel.app/api/events
   # 应返回 200，检查 rate_limit 字段
   ```

3. **监控日志**：
   ```bash
   vercel logs --follow starhub-refresh
   # 关注 rate_limited / abuse_limited 错误
   ```

4. **验证自适应**：
   - 查看响应 JSON 中的 `rate_limit.remaining`
   - 确认前端轮询间隔随额度变化

### 告警机制

当前实现：
- 429 响应自动延长前端轮询间隔
- 额度 < 20% 自动延长服务端缓存有效期

建议增强（未来）：
- Vercel Cron 每小时检查 `rate_limit.remaining`
- 低于阈值时发送告警（邮件/Slack/飞书）

## 常见陷阱

### ❌ 错误做法

1. **匿名请求高频轮询**：60 次/小时，5 分钟一次就耗尽
2. **忽略 429 响应**：继续请求会加剧限流
3. **并发请求过多**：同时发起大量请求触发滥用检测
4. **不检查响应头**：错过限流预警信号

### ✅ 正确做法

1. **始终使用认证**：配置 `GH_TOKEN` 环境变量
2. **解析速率头**：每次响应后更新额度信息
3. **实现退避策略**：429 时按 `Retry-After` 等待
4. **缓存优先**：命中缓存时不调用 API
5. **监控日志**：定期检查是否触发限流

## 参考链接

- [GitHub REST API 速率限制](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
- [2025-05 匿名限流更新](https://github.blog/changelog/2025-05-08-updated-rate-limits-for-unauthenticated-requests/)
- [条件请求优化](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests)

---

*最后更新：2025-09-09*
