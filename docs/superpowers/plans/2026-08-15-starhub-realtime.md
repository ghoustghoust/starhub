# StarHub 实时更新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 StarHub 打开页面即显示最新 star 列表（含元数据），页面常开时每 5 分钟自动刷新，新项目出现轻提示，代理失败静默回退静态数据。

**Architecture:** 新增 Vercel Serverless 函数 `api/stars.js`（持 `GH_TOKEN` 代理 GitHub starred API，翻页全量，映射为 DATA 兼容结构）；前端在静态渲染后 fetch `/api/stars`，合并实时数据（已存在项目继承静态分类/中文描述，新项目标记「最新收藏」）后重算语言统计并重新渲染；`setInterval` 5 分钟轮询（`document.hidden` 时跳过），失败 `console.warn` 回退静态。

**Tech Stack:** Node.js（Vercel Functions，仅内置 fetch）、原生 JS（template.html 内嵌脚本）、Python 标准库（dev_render.py 本地渲染）。

**前置事实（执行者必读）：**
- GitHub API：`GET /users/Kwei168/starred?per_page=100&page=N&sort=created&direction=desc`，按 star 时间倒序；未认证 60 次/h，认证（`GH_TOKEN`）5000 次/h
- 静态 DATA 每项结构：`{id, name, owner, full_name, html_url, desc, language, stars, topics, pushed_at, updated_today, category, categoryLabel}`
- `template.html` 关键行号：`const DATA` 在 561、`const UPDATED` 在 568、语言统计初始化 612-616、init 尾部 1124
- 已有 `toast(msg)` 函数（template.html 1085 行）直接复用
- git 提交必须带 `-c user.email="408320567@qq.com" -c user.name="Kwei168"`（Vercel 校验 HEAD commit 邮箱）；远端常有 Actions bot 提交，push 前先 `pull --rebase`

---

## File Structure

| 文件 | 职责 | 操作 |
|---|---|---|
| `api/stars.js` | Vercel 函数：CORS + 翻页拉取 + 字段映射 + 错误包装 | Create |
| `.vercel-tmp/test-stars.mjs` | 本地单元测试（mock fetch，测映射/翻页/错误），测完删除 | Create（临时） |
| `template.html` | DATA/UPDATED 改 let、语言统计可重算、实时拉取模块、5 分钟轮询、toast 提示 | Modify |
| `index.html` | 渲染产物，dev_render.py 重新生成 | Modify（生成） |

---

### Task 1: api/stars.js（Vercel 实时数据代理函数）

**Files:**
- Create: `api/stars.js`
- Create: `.vercel-tmp/test-stars.mjs`（临时测试，Task 3 末尾删除）

- [ ] **Step 1: 写失败测试**

创建 `.vercel-tmp/test-stars.mjs`：

```javascript
// 本地单元测试：mock fetch 验证 api/stars.js 的映射与翻页逻辑
// 用法：node .vercel-tmp/test-stars.mjs （退出码 0=通过，1=失败）
import assert from 'node:assert';
import { mapRepo, isTodayCn, fetchAllStars } from '../api/stars.js';

const tests = [
  ['mapRepo: 完整字段映射', async () => {
    const r = mapRepo({
      full_name: 'octocat/Hello-World',
      name: 'Hello-World',
      html_url: 'https://github.com/octocat/Hello-World',
      description: '  My   first   repo  ',
      language: 'JavaScript',
      stargazers_count: 1234,
      topics: ['demo'],
      pushed_at: '2026-08-15T02:03:04Z',
    });
    assert.strictEqual(r.id, 'octocat/Hello-World');
    assert.strictEqual(r.owner, 'octocat');
    assert.strictEqual(r.desc, 'My first repo'); // 空白压缩
    assert.strictEqual(r.stars, 1234);
    assert.strictEqual(r.pushed_at, '2026-08-15');
    assert.strictEqual(r.updated_today, true);
  }],

  ['mapRepo: 空描述/缺字段容错', async () => {
    const r = mapRepo({ full_name: 'a/b' });
    assert.strictEqual(r.desc, '');
    assert.strictEqual(r.owner, 'a');
    assert.deepStrictEqual(r.topics, []);
    assert.strictEqual(r.updated_today, false);
  }],

  ['isTodayCn: 当前时刻前后 1 小时（相对时间断言，任何日期运行均通过）', async () => {
    const iso = (offsetH) => new Date(Date.now() + offsetH * 3600 * 1000).toISOString();
    assert.strictEqual(isTodayCn(iso(0)), true);    // 现在 → 今天
    assert.strictEqual(isTodayCn(iso(-25)), false); // 25 小时前 → 必为昨天
  }],

  ['fetchAllStars: 满页继续翻页，不满页停止', async () => {
    const calls = [];
    const mk = (n) => Array.from({ length: n }, (_, i) => ({ full_name: 'r/p' + i }));
    globalThis.fetch = async (url) => {
      calls.push(url);
      const page = Number(new URL(url).searchParams.get('page'));
      return { ok: true, json: async () => (page === 1 ? mk(100) : mk(50)) };
    };
    const out = await fetchAllStars('t');
    assert.strictEqual(out.length, 150);
    assert.strictEqual(calls.length, 2); // 第二页不足 100 → 停止
  }],

  ['fetchAllStars: 最多翻 10 页', async () => {
    const mk = () => Array.from({ length: 100 }, (_, i) => ({ full_name: 'r/p' + i }));
    globalThis.fetch = async () => ({ ok: true, json: async () => mk() });
    const out = await fetchAllStars('t');
    assert.strictEqual(out.length, 1000);
  }],

  ['fetchAllStars: GitHub 错误抛出', async () => {
    globalThis.fetch = async () => ({ ok: false, status: 403 });
    await assert.rejects(() => fetchAllStars('t'), /403/);
  }],
];

let failures = 0;
for (const [name, fn] of tests) {
  try { await fn(); console.log('PASS', name); }
  catch (e) { failures++; console.error('FAIL', name, '-', e.message); }
}
console.log(failures === 0 ? '\nALL PASS' : '\n' + failures + ' FAILED');
process.exit(failures === 0 ? 0 : 1);
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node .vercel-tmp/test-stars.mjs`
Expected: FAIL（`Cannot find module '../api/stars.js'` 或 `mapRepo is not a function`——文件尚不存在）

- [ ] **Step 3: 实现 api/stars.js**

创建 `api/stars.js`：

```javascript
// Vercel Serverless Function：代理拉取 Kwei168 的 starred 仓库（实时数据源）
// 用法：GET /api/stars → { stars: [...DATA 兼容项], total, fetched_at }
// 认证令牌通过 Vercel 环境变量 GH_TOKEN 注入（fine-grained PAT，public_repo 读权限）
// 注：export 的 mapRepo/isTodayCn/fetchAllStars 供本地单元测试复用
const USER = 'Kwei168';
const PAGE_SIZE = 100;
const MAX_PAGE = 10;

// 北京时间（UTC+8）的"今天"判断，与 fetch_and_build.py 的 _today_cn() 语义一致
export function isTodayCn(iso) {
  if (!iso) return false;
  const cn = new Date(new Date(iso).getTime() + 8 * 3600 * 1000);
  const now = new Date(Date.now() + 8 * 3600 * 1000);
  return cn.getUTCFullYear() === now.getUTCFullYear() &&
         cn.getUTCMonth() === now.getUTCMonth() &&
         cn.getUTCDate() === now.getUTCDate();
}

// GitHub starred 仓库项 → 静态 DATA 兼容结构（desc 压缩空白，与 _pick() 一致）
export function mapRepo(r) {
  const fn = r.full_name || '';
  return {
    id: fn,
    name: r.name,
    owner: fn.split('/')[0] || '',
    full_name: fn,
    html_url: r.html_url,
    desc: (r.description || '').replace(/\s+/g, ' ').trim(),
    language: r.language,
    stars: r.stargazers_count,
    topics: r.topics || [],
    pushed_at: (r.pushed_at || '').slice(0, 10),
    updated_today: isTodayCn(r.pushed_at),
  };
}

// 翻页拉取全量 starred（按 star 时间倒序），页尾不满即停，最多 MAX_PAGE 页
export async function fetchAllStars(token) {
  const out = [];
  for (let page = 1; page <= MAX_PAGE; page++) {
    const r = await fetch(
      `https://api.github.com/users/${USER}/starred?per_page=${PAGE_SIZE}&page=${page}&sort=created&direction=desc`,
      {
        headers: {
          'Authorization': 'Bearer ' + token,
          'Accept': 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'User-Agent': 'starhub-refresh',
        },
      }
    );
    if (!r.ok) throw new Error('GitHub API ' + r.status);
    const items = await r.json();
    if (!Array.isArray(items) || items.length === 0) break;
    out.push(...items.map(mapRepo));
    if (items.length < PAGE_SIZE) break;
  }
  return out;
}

function cnNow() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 19).replace('T', ' ');
}

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') { res.status(204).end(); return; }
  if (req.method !== 'GET') { res.status(405).json({ error: 'Method Not Allowed' }); return; }

  const token = process.env.GH_TOKEN;
  if (!token) {
    res.status(500).json({ error: 'GH_TOKEN 未配置（Vercel 环境变量）' });
    return;
  }

  try {
    const stars = await fetchAllStars(token);
    res.status(200).json({ stars, total: stars.length, fetched_at: cnNow() });
  } catch (e) {
    res.status(502).json({ error: 'GitHub API 拉取失败: ' + String(e) });
  }
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `node .vercel-tmp/test-stars.mjs`
Expected: `ALL PASS`（6 项），退出码 0

- [ ] **Step 5: 语法检查**

Run: `node --check api/stars.js`
Expected: 无输出，退出码 0

- [ ] **Step 6: Commit**

```bash
git add api/stars.js .vercel-tmp/test-stars.mjs
git -c user.email="408320567@qq.com" -c user.name="Kwei168" commit -m "feat: 新增 /api/stars 实时数据代理函数"
```

---

### Task 2: template.html 前端实时拉取

**Files:**
- Modify: `template.html:561,568`（const → let）
- Modify: `template.html:612-616`（语言统计封装为可重算）
- Modify: `template.html`（新增实时模块 + init 启动）

- [ ] **Step 1: DATA/UPDATED 改为 let**

`template.html` 561 行与 568 行：

```javascript
let DATA = __DATA__;
```

```javascript
let UPDATED = '__UPDATED__';
```

- [ ] **Step 2: 语言统计封装为可重算**

将 `template.html` 612-616 行：

```javascript
const langCount = {};
DATA.forEach(d => { const l = d.language || 'Other'; langCount[l] = (langCount[l]||0)+1; });
const langSorted = Object.entries(langCount).sort((a,b)=>b[1]-a[1]);
const topLangs = langSorted.slice(0, 8);
const topSum = topLangs.reduce((s,x)=>s+x[1],0);
```

替换为：

```javascript
let langCount = {}, langSorted = [], topLangs = [], topSum = 0;
// 语言统计可重算：实时数据刷新后语言分布会变化
function rebuildLangStats(){
  langCount = {};
  DATA.forEach(d => { const l = d.language || 'Other'; langCount[l] = (langCount[l]||0)+1; });
  langSorted = Object.entries(langCount).sort((a,b)=>b[1]-a[1]);
  topLangs = langSorted.slice(0, 8);
  topSum = topLangs.reduce((s,x)=>s+x[1],0);
}
rebuildLangStats();
```

- [ ] **Step 3: 新增实时更新模块**

在 `template.html` 中 `function toast(msg){` 定义的 `toast` 函数之后（原 1089 行附近）插入：

```javascript
// ---- 实时更新：打开页面即最新，常开时每 5 分钟轮询 ----
// 数据源：同源 /api/stars（Vercel 函数持 GH_TOKEN 代理 GitHub API）
// 失败时静默回退静态数据（console.warn），不影响页面可用性
const LIVE_INTERVAL = 5 * 60 * 1000;
let liveTimer = null;

// 实时列表与静态 DATA 合并：
// 已存在项目 → 保留静态分类/中文描述，只更新 stars/pushed_at/updated_today
// 新项目 → 临时分类 category=''（仅在"全部"可见），标签"最新收藏"，次日构建转正
function mergeLiveData(live){
  const oldMap = new Map(DATA.map(d => [d.full_name, d]));
  let fresh = 0;
  DATA = live.map(r => {
    const old = oldMap.get(r.full_name);
    if(old){
      old.stars = r.stars;
      old.pushed_at = r.pushed_at;
      old.updated_today = r.updated_today;
      return old;
    }
    fresh++;
    return Object.assign(r, { category: '', categoryLabel: '最新收藏' });
  });
  rebuildLangStats();
  renderHeader(); renderCats(); renderLangSel(); renderLangBar(); renderList();
  return fresh;
}

async function fetchLiveStars(){
  if(document.hidden) return; // 后台标签页不轮询
  try{
    const r = await fetch('/api/stars');
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    if(!j || !Array.isArray(j.stars)) throw new Error('payload 格式错误');
    UPDATED = j.fetched_at || UPDATED;
    const fresh = mergeLiveData(j.stars);
    if(fresh > 0) toast('发现 ' + fresh + ' 个新收藏');
  }catch(e){
    console.warn('[live] 实时数据拉取失败，回退静态数据：', e);
  }
}
```

- [ ] **Step 4: init 启动实时轮询**

在 `template.html` init 末尾（1124 行 `bindEvents();` 之后）追加：

```javascript
fetchLiveStars();
liveTimer = setInterval(fetchLiveStars, LIVE_INTERVAL);
document.addEventListener('visibilitychange', () => {
  if(!document.hidden) fetchLiveStars(); // 切回前台立即刷新一次
});
```

- [ ] **Step 5: JS 语法检查**

提取 `<script>` 内容后检查（沿用既有方法）：

```bash
python -c "import re,io; s=open('template.html',encoding='utf-8').read(); m=re.search(r'<script>(.*?)</script>', s, re.S); open('.vercel-tmp/page.js','w',encoding='utf-8').write(m.group(1))"
node --check .vercel-tmp/page.js
```

Expected: `node --check` 无输出，退出码 0

- [ ] **Step 6: Commit**

```bash
git add template.html
git -c user.email="408320567@qq.com" -c user.name="Kwei168" commit -m "feat: 前端实时拉取最新 star 列表（5 分钟轮询 + 新收藏提示 + 失败回退）"
```

---

### Task 3: 渲染、部署与端到端验证

**Files:**
- Modify: `index.html`（dev_render.py 重新生成）
- Delete: `.vercel-tmp/test-stars.mjs`（测试完成清理）

- [ ] **Step 1: 重新渲染 index.html**

Run: `python dev_render.py`
Expected: 输出渲染成功提示；验证 index.html 含新代码：

```bash
findstr /C:"LIVE_INTERVAL" /C:"fetchLiveStars" /C:"/api/stars" index.html
```

Expected: 三处均命中（新实时模块已进入产物）

- [ ] **Step 2: 产物 JS 语法检查**

```bash
python -c "import re; s=open('index.html',encoding='utf-8').read(); m=re.search(r'<script>(.*?)</script>', s, re.S); open('.vercel-tmp/page.js','w',encoding='utf-8').write(m.group(1))"
node --check .vercel-tmp/page.js
```

Expected: 无输出，退出码 0

- [ ] **Step 3: 本地单元测试再跑一遍（确认无回归）**

Run: `node .vercel-tmp/test-stars.mjs`
Expected: `ALL PASS`

- [ ] **Step 4: 删除临时测试文件**

```bash
Remove-Item .vercel-tmp/test-stars.mjs
```

- [ ] **Step 5: Commit & Push**

```bash
git add index.html
git -c user.email="408320567@qq.com" -c user.name="Kwei168" commit -m "build: 用最新模板渲染 index.html（实时更新模块）"
git -c user.email="408320567@qq.com" -c user.name="Kwei168" pull --rebase origin main
git -c user.email="408320567@qq.com" -c user.name="Kwei168" push origin main
```

Expected: push 成功（若被拒先 rebase 再推）

- [ ] **Step 6: 部署 Vercel**

Run: `node .vercel-tmp/vercel-deploy.cjs --yes --prod`
Expected: 部署成功（Ready）

- [ ] **Step 7: 线上验证 /api/stars**

Run: `curl -s https://starhub-refresh.vercel.app/api/stars | python -c "import json,sys; d=json.load(sys.stdin); print('total:', d.get('total')); s=d.get('stars') or []; print('first:', s[0]['full_name'], s[0]['stars'], s[0]['pushed_at']) if s else print('EMPTY')"`
Expected: total > 0，first 为最新 star 的仓库（按 star 时间倒序）

- [ ] **Step 8: 线上页面验证**

Run: `curl -s https://starhub-refresh.vercel.app/ | findstr /C:"api/stars" /C:"LIVE_INTERVAL"`
Expected: 命中 `/api/stars` 与 `LIVE_INTERVAL`（线上 index.html 已含实时模块）

- [ ] **Step 9: 请求用户浏览器验收**

请用户 **Ctrl+F5 强制刷新** 页面，确认：
1. 页面先显示静态数据，随后（≤3s）数据不闪烁地刷新为最新
2. 控制台无 `[live]` 错误（Network 里 /api/stars 返回 200）
3. 若期间 star 了新仓库：5 分钟内出现 + toast「发现 N 个新收藏」；新项目显示英文描述与「最新收藏」标签

---

## Self-Review

**Spec 覆盖：**
- 打开页面即最新（Task 2 Step 3-4 + Task 3 部署）✓
- 5 分钟轮询（Task 2 Step 3 `LIVE_INTERVAL`）✓
- 新项目降级：英文描述 + 最新收藏标签 + 次日转正（mergeLiveData 逻辑）✓
- favs 元数据刷新（buildCard 读 `d.stars`，DATA 更新后 renderList 自动生效，零改造）✓
- toast 提示（复用现有 toast，fresh 计数）✓
- 失败回退（catch + console.warn，静态数据保持）✓
- 无回归（渲染函数复用、state 不变；单元测试 + 线上 curl 双验证）✓

**类型一致性：** `mergeLiveData`/`fetchLiveStars`/`rebuildLangStats` 名称在 Task 2 各步骤一致；`api/stars.js` 的 `fetchAllStars`/`mapRepo`/`isTodayCn` 与测试文件引用一致；响应字段 `stars/total/fetched_at` 前后端一致。

**注意（执行者）：** `isTodayCn` 测试中「当前时刻前后 1 小时」用例是相对时间断言，任何日期运行均通过；若本地测试在 00:00-01:00（北京）边缘运行，`iso(0)` 可能为昨天——测试设计已避免该边界（相对当前时刻）。
