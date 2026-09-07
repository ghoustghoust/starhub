# RSS 聚合器微信分享功能 Spec

> 日期：2026-09-04
> 状态：待实施
> 目标文件：`rss-aggregator.html`

## 1. 目标

在 RSS 聚合阅读器页面实现**图片化分享**功能：用户点击分享按钮后，将文章标题、摘要、来源信息渲染为一张精美图片（含原文二维码），引导用户保存后发送至微信好友或分享到朋友圈。

## 2. 需求确认

| 维度 | 决策 |
|------|------|
| 目标页面 | `rss-aggregator.html` |
| 分享触发位置 | 卡片墙每张卡片的图标 + 阅读器工具栏图标 |
| 分享图内容 | 标题（完整） + 摘要（完整，不截断） + 来源标识 + 原文 QR 码 + 品牌水印 |
| 分享方式 | 生成图片 → 用户长按/下载保存 → 微信发送或朋友圈 |
| 技术选型 | 纯 Canvas API 绘制 + QR 库（~4KB），不使用 html2canvas |
| 依赖加载 | QR 库通过 CDN 按需懒加载，首屏零影响 |

## 3. 技术可行性

### 3.1 微信 JS-SDK 不可行

微信 JS-SDK 的"分享给朋友"API 需要已认证的公众号 + 后端签名服务 + 域名白名单配置，对纯静态站点成本过高。

### 3.2 图片化分享方案

采用 Canvas API 直接绘制分享卡片，配合轻量 QR 库生成二维码。理由：

- 仅需 ~4KB 外部依赖（qrcode-generator）
- 渲染极快（< 50ms），无 DOM 重绘开销
- 兼容微信内置浏览器、iOS Safari、Android Chrome

### 3.3 CDN 加载策略

```javascript
var _qrLoaded = false;
function loadQRLib() {
  if (_qrLoaded) return Promise.resolve();
  return new Promise(function(resolve, reject) {
    var s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js';
    s.onload = function() { _qrLoaded = true; resolve(); };
    s.onerror = function() {
      // fallback
      var s2 = document.createElement('script');
      s2.src = 'https://unpkg.com/qrcode-generator@1.4.4/qrcode.min.js';
      s2.onload = function() { _qrLoaded = true; resolve(); };
      s2.onerror = reject;
      document.head.appendChild(s2);
    };
    document.head.appendChild(s);
  });
}
```

## 4. 架构与数据流

### 4.1 模块结构

在现有 `<script>` 中新增独立的分享模块（~225 行），逻辑上分为：

- `shareArticle(article)` — 入口函数
- `loadQRLib()` — 懒加载 QR 库
- `drawShareCard(article)` — Canvas 绘制，返回 dataURL
- `showShareModal(dataURL)` — 展示图片模态框
- `closeShareModal()` — 关闭模态框

### 4.2 数据流

```
用户点击分享图标
       ↓
shareArticle(article)
       ↓
  QR 库已加载？ ──No──→ 动态 <script> 加载 CDN (~4KB, 仅首次)
       ↓ Yes
  QRCode.getMatrix(article.url) → 二维码矩阵
       ↓
  drawShareCard(article, matrix) → Canvas → canvas.toDataURL('image/png')
       ↓
  showShareModal(dataURL) → 模态框弹出
       ↓
  用户操作：长按保存 / 点击下载 / 复制图片 / ESC 关闭
```

### 4.3 扩展性预留

`shareArticle()` 签名预留平台参数：

```javascript
function shareArticle(article, platform) {
  // platform: undefined = 生成图片（当前 V1 默认）
  // 未来: 'x' | 'wechat' | 'link' 等
}
```

模态框底部 `.share-actions` 下方预留 `.share-platforms` 扩展位，V2 可追加 X、微信等直接分享按钮。

## 5. 分享卡片视觉设计

### 5.1 画布参数

| 参数 | 值 |
|------|------|
| 宽度 | 750px（逻辑宽度） |
| 高度 | **自适应**，根据标题和摘要实际行数动态计算 |
| 设备像素比 | 2x（`canvas.width = 750*2`，`ctx.scale(2,2)`） |
| 圆角 | 16px（导出图片四角） |
| 格式 | PNG（`canvas.toDataURL('image/png')`） |

### 5.2 布局结构

```
750px 宽
┌──────────────────────────────────────────────┐
│  padding-top: 40px                              │
│                                                  │
│  ● [分类名]                    StarHub RSS 聚合 │  顶栏 12px
│                                                  │  gap: 28px
│  ┌─────────────────────────────────────────┐ │
│  │ 文章标题（完整，不限行数，通常 1-3 行）  │ │  26px, 行高 1.45
│  └─────────────────────────────────────────┘ │  gap: 16px
│  ┌─────────────────────────────────────────┐ │
│  │ 文章摘要（完整，不限行数，不截断）        │ │  15px, 行高 1.7
│  │ 可能 1-10+ 行                            │ │
│  └─────────────────────────────────────────┘ │  gap: 24px
│  ─────────── 分隔线 ───────────               │  gap: 24px
│                                                  │
│  ● 来源名 · 时间            ┌──────────┐      │
│                             │ QR 90×90 │      │
│  长按识别 · 阅读原文        └──────────┘      │
│                                                  │
│  padding-bottom: 36px                            │
└──────────────────────────────────────────────┘
```

### 5.3 文本换行算法

```javascript
function wrapText(ctx, text, maxWidth) {
  // 不限制行数，完整换行，不截断
  var lines = [], line = '';
  for (var i = 0; i < text.length; i++) {
    var test = line + text[i];
    if (ctx.measureText(test).width > maxWidth && line) {
      lines.push(line);
      line = text[i];
    } else {
      line = test;
    }
  }
  if (line) lines.push(line);
  return lines;
}
```

### 5.4 动态高度计算

```
totalHeight = 40 (padding-top)
  + 50 (顶栏)
  + 28 (gap)
  + titleLines.length * (26 * 1.45) (标题区)
  + 16 (gap)
  + summaryLines.length * (15 * 1.7) (摘要区)
  + 24 (gap)
  + 1 (分隔线)
  + 24 (gap)
  + 110 (底部区: 来源+QR+提示文字)
  + 36 (padding-bottom)
```

### 5.5 配色方案

| 元素 | 明色 | 暗色 |
|------|------|------|
| 画布背景 | `#faf9f7` | `#161412` |
| 标题文字 | `#1c1917` | `#ece7df` |
| 摘要文字 | `#5f594c` | `#a59d90` |
| 分隔线 | `#ddd6c9` | `#37312a` |
| 分类标签色 | 跟随 `--cat-*` | 自动适配 |
| QR 前景 | `#1c1917` | `#ece7df` |
| QR 背景 | `#fffdf9` | `#1d1a17` |
| 品牌水印 | `#857e74` | `#98907f` |

配色在点击分享时读取当前 CSS 变量，自动适配明/暗主题。

## 6. UI 组件

### 6.1 卡片墙分享图标

在每张卡片 `card-top` 行中，外链按钮（`.ext-btn`）后面追加：

```html
<button class="share-btn" data-k="{articleKey}" title="分享文章"
        onclick="event.stopPropagation()">
  <svg><!-- 分享图标 --></svg>
</button>
```

样式复用 `.ext-btn` 规格（22×22，圆角 6px），hover 时品牌色高亮。

### 6.2 阅读器工具栏分享图标

在 `.r2-acts` 区域"原站 ↗"按钮左侧追加：

```html
<button class="r2-icon-btn" id="r2Share" title="分享文章"
        onclick="shareArticle(curArt)">
  <svg><!-- 分享图标 --></svg>
</button>
```

样式使用 `.r2-icon-btn`（28×28，圆角 8px），与工具栏其他图标风格一致。

### 6.3 分享图片模态框

```html
<div class="share-modal" id="shareModal">
  <div class="share-backdrop" onclick="closeShareModal()"></div>
  <div class="share-panel">
    <div class="share-hd">
      <h3>分享图片已生成</h3>
      <button class="share-close" onclick="closeShareModal()">×</button>
    </div>
    <div class="share-img-wrap">
      <img id="shareImg" alt="分享图片" />
    </div>
    <p class="share-hint">长按图片保存，发送至微信好友或朋友圈</p>
    <div class="share-actions">
      <button class="btn-share-save" id="btnShareSave">保存图片</button>
      <button class="btn-share-copy" id="btnShareCopy">复制图片</button>
    </div>
    <!-- V2 扩展位: <div class="share-platforms"></div> -->
  </div>
</div>
```

CSS 样式要点：
- `.share-modal`: `position:fixed; inset:0; z-index:100; display:none`
- `.share-modal.open`: `display:flex; align-items:center; justify-content:center`
- `.share-panel`: `width:min(400px, 90vw); border-radius:16px; padding:20px`
- `.share-img-wrap img`: `width:100%; display:block`（不限高度，完整展示）
- 复用现有 CSS 变量适配明暗主题

## 7. 交互流程

### 7.1 分享操作流

```
点击分享图标
  ↓
按钮进入 loading 态（半透明 + pointer-events:none）
  ↓
loadQRLib() + drawShareCard() [异步，通常 < 300ms]
  ↓
showShareModal(dataURL)
  ├─ 模态框淡入
  └─ img.src = dataURL
  ↓
用户操作：
  ├─ 长按图片 → 系统"保存图片"（移动端微信主路径）
  ├─ 点击"保存图片" → <a download> 触发下载（桌面端）
  ├─ 点击"复制图片" → Clipboard API → toast 提示
  └─ ESC / 点击遮罩 / × → 关闭
```

### 7.2 保存图片

- 桌面浏览器：创建 `<a download="starhub-share.png">` 触发下载
- 移动端微信：用户长按图片保存（主路径），下载按钮作为补充
- iOS Safari：`<a download>` 可能在新标签页打开图片，提示用户长按保存

### 7.3 复制图片

```javascript
async function copyImageToClipboard(dataURL) {
  try {
    var blob = await (await fetch(dataURL)).blob();
    await navigator.clipboard.write([
      new ClipboardItem({ 'image/png': blob })
    ]);
    toast('图片已复制到剪贴板');
  } catch(e) {
    toast('复制失败，请长按图片手动保存');
  }
}
```

### 7.4 键盘支持

- ESC 关闭分享模态框
- 模态框打开时焦点锁定在关闭按钮

## 8. 错误处理

### 8.1 QR 库加载失败

```
加载 jsdelivr CDN → 失败 → fallback unpkg CDN → 失败
  → QR 区域显示"二维码暂不可用"文字
  → 分享图其余部分正常生成
  → toast 提示
```

### 8.2 Canvas 不可用

极端情况（老旧浏览器）：模态框中展示纯文本信息卡 + "点击复制链接"按钮。

### 8.3 文章内容边界

| 情况 | 处理 |
|------|------|
| 标题为空 | 显示"无标题文章" |
| 摘要为空 | 跳过摘要区域，高度相应缩减 |
| URL 为空或 `#` | QR 码使用当前页面 URL |
| 特殊字符 `<>&` | Canvas fillText 不解析 HTML，直接渲染原文 |
| 超长连续字符 | wrapText 逐字符测量，任意位置换行 |

### 8.4 主题切换

分享图配色在点击分享那一刻读取当前主题，无需监听主题变化事件。再次分享时自动使用最新主题。

## 9. 代码量估算

| 模块 | 行数 |
|------|------|
| CSS（模态框 + 分享图标） | ~40 行 |
| `drawShareCard()` | ~100 行 |
| `showShareModal()` | ~40 行 |
| `shareArticle()` + `loadQRLib()` | ~30 行 |
| 卡片墙/阅读器按钮注入 | ~15 行 |
| **合计** | **~225 行** |

## 10. 不在范围内

- 微信 JS-SDK 集成（需后端签名服务）
- 分享到 X / Twitter / 其他平台（V2 扩展）
- 服务端渲染分享图
- 分享统计/追踪
