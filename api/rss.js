// Vercel Serverless Function：API-First 实时 RSS 聚合
// GET /api/rss → 返回 JSON（服务端缓存 5 分钟）
// 页面加载时立即调用，获取全部源的最新内容
// 失败时回退到滚动缓存（上次成功抓取的数据）

import { readFileSync } from 'fs';
import { join } from 'path';

const FETCH_TIMEOUT = 5000;     // 单源超时 5s（从8s降低以加快失败速度）
const CONCURRENCY = 20;         // 20 路并发（从10提高到20以加快速度）
const CACHE_TTL = 5 * 60 * 1000;  // 服务端缓存 5 分钟
const BATCH_SIZE = 200;          // 分批刷新每批源数量（200源 × 20并发 ≈ 10s/批）
const UA = 'starhub-rss-aggregator/1.0';

// ── 运行时留存闸门 ──
// 规则在 lib/rss_retention.js，与构建期 build_rss_aggregator._apply_retention 同一套口径
// （基线 72h、按源发布间隔放宽到最多 168h、每源保底 3 条且保底也受 168h、判不了龄一律丢）。
// 为什么运行时也要过：?batch=N 与 ?source=KEY 是本函数自己发 HTTP 抓上游，完全不吃构建产物，
// 只裁构建产物会出现"页面刷新一下，4 年前的旧文整批回来"（2026-09-20 对抗审查 P0-2）。
// 加载失败绝不打断响应（宁可放过不可 500），但必须出声：打 ::warning 并在响应头留痕，
// 静默退化是本项目付过两次代价的形态。
let RETENTION = null;
let RETENTION_FAILED = false;   // 计算期异常也算降级，见 gateSources
try {
  RETENTION = require('../lib/rss_retention.js');
} catch (e) {
  console.error('[rss] 留存闸门模块加载失败:', e && e.message);
  console.log('::warning title=RSS 运行时留存闸门不可用::' + (e && e.message));
}

// ── 实时封面抽取 ──
// 规则在 lib/rss_cover.js，与构建期 build_rss_aggregator._pick_item_image 同一优先级
// （enclosure > media:content > media:thumbnail > 正文首图 > 描述首图）。
// 缺这一段时 ?source= / ?batch= 现抓的条目从出生就没有封面（模板注释自己写明过），
// 而页面 _apiMergeTo 只能保住旧条目的图 ⇒ 新文章永远比历史条目少一张封面卡。
// 加载失败只影响封面（非致命），但必须出声：静默退化是本项目付过两次代价的形态。
let COVER = null;
try {
  COVER = require('../lib/rss_cover.js');
} catch (e) {
  console.error('[rss] 封面抽取模块加载失败:', e && e.message);
  console.log('::warning title=RSS 实时封面抽取不可用::' + (e && e.message));
}

// ── 运行时正文规范层 ──
// 规则在 lib/body_rules.js，与构建期 build_rss_aggregator 的 `_normalize_body_html` / `_cap_body`
// 同一套口径（懒加载提升 + 相对地址绝对化 + 按码点截断且抹净尾巴半个标签/残缺实体），
// 由 tests/site_nav/test_body_rules_parity.py 逐条对账钉住。
// 这里**故意不用** RETENTION/COVER 那种 try+`if (X)` 的可选闸门写法：装载失败若只影响封面
// 可以出声放过，而正文要么规范要么不出厂 —— 退回 `.slice(0, 50000)` 就是用户报的"半个标签"，
// 跳过 normalize 就是"正文丢图"。装不上就让函数 500，静默出厂坏正文是本项目付过两次代价的形态。
const BODY = require('../lib/body_rules.js');

/** 响应头口径：装载失败或计算异常都必须显式露出来，不许静默当成"已过滤"。 */
function retentionHeader() {
  return (RETENTION && !RETENTION_FAILED) ? 'on' : 'unavailable';
}

/** 从**已加载**的快照对象建 URL→日期索引（等价于 Python 侧的 first_seen）。
 *  刻意不在这里 loadSnapshot()：快照有 ~80MB，每次 batch 请求多解析一遍会在
 *  serverless 内存上限下把端点自己打挂 —— 调用方手里已经有快照，必须由它传进来。 */
function buildDateIndex(snap) {
  const map = new Map();
  try {
    if (snap && snap.sources) {
      for (const s of snap.sources) {
        for (const it of (s.items || [])) {
          if (it.u && it.d && !map.has(it.u)) map.set(it.u, it.d);
        }
      }
    }
  } catch (e) { /* 快照结构异常时按"没有可核时间"处理，闸门会丢掉判不了龄的条目 */ }
  return map;
}

/** 源声明的 pub_date 时区偏移（rss_sources.json 的 pub_date_offset_min）。 */
function snapshotOffsets(sources) {
  const off = {};
  for (const s of (sources || [])) {
    if (s && s.key && s.pub_date_offset_min) off[s.key] = s.pub_date_offset_min;
  }
  return off;
}

/** 唯一的出口闸门调用：任何失败都降级为"不过闸"，绝不打断响应。
 *  注释承诺过"宁可放过不可 500"，所以 require 之外的计算异常也必须兜住
 *  （2026-09-20 对抗审查 P1-2：只兜 require 时，lib 抛异常会把 ?source 打成 500）。
 *  降级必须留痕：X-RSS-Retention: unavailable，别让它变成静默失效。 */
function gateSources(sources, sourcesMeta, knownDates) {
  if (!RETENTION) return sources;
  try {
    const res = RETENTION.applyRetention(sources, {
      nowMs: Date.now(),
      offsets: snapshotOffsets(sourcesMeta || []),
      knownDates: knownDates || null,
    });
    console.log(`[rss] 留存闸门: 进 ${res.stats.before} → 出 ${res.stats.after}`
      + `（判不了龄 ${res.stats.undatable}）`);
    return res.sources;
  } catch (e) {
    console.error('[rss] 留存闸门执行失败，本条响应未过闸:', e && e.message);
    RETENTION_FAILED = true;
    return sources;
  }
}

// 滚动缓存：每个源保留上次成功抓取的数据
let rollingCache = new Map();  // key → { items, lastModified }
let fullCache = { t: 0, v: null };  // 完整响应缓存

// ── 标题翻译（调用同项目 /api/translate，降级链：GTX → MyMemory → Agnes → Zen） ──
let transCache = new Map();  // text → translated（进程内缓存，避免重复调用）

function isChinese(text) {
  if (!text) return true;
  let cn = 0;
  for (const c of text) { if (c >= '\u4e00' && c <= '\u9fff') cn++; }
  return cn > text.length * 0.3;
}

function getTransBaseUrl() {
  // 优先用 VERCEL_URL 环境变量，回退到已知生产域名
  if (process.env.VERCEL_URL) return `https://${process.env.VERCEL_URL}`;
  return 'https://starhub-refresh.vercel.app';
}

async function translateBatchViaApi(texts) {
  // 过滤已翻译和中文
  const pending = [];
  const results = new Array(texts.length);
  for (let i = 0; i < texts.length; i++) {
    const t = texts[i];
    if (!t || isChinese(t)) { results[i] = t; continue; }
    if (transCache.has(t)) { results[i] = transCache.get(t); continue; }
    pending.push(i);
  }
  if (pending.length === 0) return results;

  // 分批调用 /api/translate（MAX_TEXTS=20）
  const baseUrl = getTransBaseUrl();
  const MAX_BATCH = 20;
  for (let i = 0; i < pending.length; i += MAX_BATCH) {
    const chunk = pending.slice(i, i + MAX_BATCH);
    const chunkTexts = chunk.map(idx => texts[idx]);
    try {
      const r = await fetch(`${baseUrl}/api/translate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Origin': 'https://starhub-refresh.vercel.app' },
        body: JSON.stringify({ texts: chunkTexts, mode: 'bulk' }),
        signal: AbortSignal.timeout(30000),
      });
      if (r.ok) {
        const j = await r.json();
        if (j.translations) {
          for (let k = 0; k < chunk.length; k++) {
            const zh = j.translations[k] || texts[chunk[k]];
            results[chunk[k]] = zh;
            transCache.set(texts[chunk[k]], zh);
          }
          continue;
        }
      }
    } catch (e) {
      console.error('[rss] translate API call failed:', e.message);
    }
    // 翻译失败，保留原文
    for (const idx of chunk) { results[idx] = texts[idx]; }
  }
  return results;
}

async function translateTitlesForSources(formattedSources, snapshotTrans) {
  // 收集所有英文标题（去重），跳过快照中已有翻译的条目
  const titleSet = new Map();  // title → [items]
  let skipped = 0;
  for (const src of formattedSources) {
    for (const it of src.items) {
      const t = it.t || '';
      if (!t || isChinese(t)) continue;
      // 查快照翻译：同 URL 的条目在构建时已翻译 → 直接复用
      if (snapshotTrans) {
        const cached = snapshotTrans.get(it.u);
        if (cached && cached !== t) {
          it.t_zh = cached;
          skipped++;
          continue;
        }
      }
      if (!titleSet.has(t)) titleSet.set(t, []);
      titleSet.get(t).push(it);
    }
  }
  if (skipped > 0) console.log(`[rss] Snapshot reuse: ${skipped} titles already translated`);
  const uniqueTitles = [...titleSet.keys()];
  if (uniqueTitles.length === 0) return;
  console.log(`[rss] Translating ${uniqueTitles.length} new English titles via /api/translate`);

  const translated = await translateBatchViaApi(uniqueTitles);
  let count = 0;
  for (let i = 0; i < uniqueTitles.length; i++) {
    const zh = translated[i];
    if (zh && zh !== uniqueTitles[i]) {
      for (const it of titleSet.get(uniqueTitles[i])) {
        it.t_zh = zh;
      }
      count++;
    }
  }
  console.log(`[rss] Translation done: ${count}/${uniqueTitles.length} new titles translated`);
}

// ── 加载 API 快照（构建时生成的 72h 累积数据） ──

function loadSnapshot() {
  try {
    const p = join(process.cwd(), 'rss_api_snapshot.json');
    const snap = JSON.parse(readFileSync(p, 'utf-8'));
    const totalChunks = snap._total_chunks || 1;

    // 合并分块快照（rss_api_snapshot.json + rss_api_snapshot_1.json ...）
    if (totalChunks > 1) {
      for (let i = 1; i < totalChunks; i++) {
        try {
          const cp = join(process.cwd(), `rss_api_snapshot_${i}.json`);
          const cs = JSON.parse(readFileSync(cp, 'utf-8'));
          if (cs.sources && cs.sources.length) {
            snap.sources = snap.sources.concat(cs.sources);
          }
        } catch (e) {
          console.warn(`[rss] Failed to load snapshot chunk ${i}:`, e.message);
        }
      }
    }

    const total = (snap.sources || []).reduce((n, s) => n + (s.items || []).length, 0);
    console.log(`[rss] Loaded snapshot: ${(snap.sources || []).length} sources, ${total} items (${totalChunks} chunks)`);
    return snap;
  } catch (err) {
    console.log('[rss] Snapshot not found, falling back to live fetch');
    return null;
  }
}

// ── 加载源列表 ──

function loadSources() {
  const p = join(process.cwd(), 'rss_sources.json');
  return JSON.parse(readFileSync(p, 'utf-8'));
}

// ── 简易 XML 文本提取 ──

function extractTag(xml, tag) {
  const m = xml.match(new RegExp('<' + tag + '[^>]*>([\\s\\S]*?)</' + tag + '>', 'i'));
  return m ? m[1].trim() : '';
}

function extractAttr(xml, tag, attr) {
  const m = xml.match(new RegExp('<' + tag + '[^>]*\\s' + attr + '="([^"]*)"', 'i'));
  return m ? m[1] : '';
}

/** RSS <link> 缺失时认 <guid>；Atom 没有 guid，用 <id>。
 *  与 Python 侧 _parse_rss_item 同口径（台账 §20）：只接受 http(s) 且未显式
 *  isPermaLink="false" 的值。否则宁可为空 —— 一个看起来能点、点开 404 的链接
 *  比空链接更坏。 */
/** link 值归一：剥 CDATA 包装 + 解 HTML 实体 + 去首尾空白。
 *  Python 侧 `_parse_rss_item` 一直做这三步（html.unescape），JS 侧不做的后果是
 *  同一条链接在两条通道长得不一样 —— 线上实测 bbc_top_stories_592 / times_of_india_world_769 /
 *  der_spiegel_783 三个源 82 条 link 100% 畸形，而构建期产物 9,067 条零畸形。
 *  链接既决定卡片跳哪儿，也决定抽屉能不能认出"同一篇文章"。
 *  判据：tests/rss_history/test_parse_parity_js.py 的双跑对照。 */
function cleanLink(raw) {
  let s = String(raw == null ? '' : raw).trim();
  const cdata = s.match(/^<!\[CDATA\[([\s\S]*?)\]\]>$/);
  if (cdata) s = cdata[1].trim();
  // &amp; 放最后解，否则 `&amp;lt;` 会被二次解成 `<`（与 html.unescape 的单趟语义一致）
  // 超范围码点必须原样留着：String.fromCodePoint(0x110000) 抛 RangeError，而 cleanLink
  // 跑在 parseFeed 里、外面套的是 fetchOne 的 try ⇒ 一条脏链接就让整源变成
  // "HTTP 200 + items=[]"，抽屉表现为刷新没反应。Python html.unescape 对解不开的
  // 码点也是原样返回（chr() 抛 ValueError 即跳过），所以钳上界同时保住了两侧一致。
  const _dec = (radix) => (whole, digits) => {
    const c = parseInt(digits, radix);
    return Number.isFinite(c) && c >= 0 && c <= 0x10FFFF ? String.fromCodePoint(c) : whole;
  };
  s = s.replace(/&#x([0-9a-f]+);/gi, _dec(16))
       .replace(/&#(\d+);/g, _dec(10))
       .replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&#39;/g, "'")
       .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
       .replace(/&amp;/g, '&');
  return s.trim();
}

function linkOrPermaId(block, linkTag, idTag) {
  const direct = cleanLink(extractTag(block, linkTag));
  if (direct && /^https?:\/\//i.test(direct)) return direct;
  const idTag2 = idTag || 'guid';
  const open = block.match(new RegExp('<' + idTag2 + '([^>]*)>', 'i'));
  if (!open) return direct || '';
  if (/\bisPermaLink\s*=\s*"false"/i.test(open[1])) return direct || '';
  const v = cleanLink(extractTag(block, idTag2));
  return /^https?:\/\//i.test(v) ? v : (direct || '');
}

/** 上游没给发布日期时的降级值 = 本次抓取时刻，并打 date_fallback 标记。
 *  前端据此显示「收录 …」，不冒充发布时间；缺了这个标记，卡片上的时间就是假的。 */
function datedOrCapture(pubDate) {
  if (pubDate && String(pubDate).trim()) return { pub_date: String(pubDate).trim(), date_fallback: false };
  return { pub_date: new Date().toISOString(), date_fallback: true };
}

// ── 摘要/标题的"解实体 + 去标签"两层（R43 拆的层，审查 ③ 换了实体那一格）──────
// 拆成两层是给 R43 用的：`_strip_html`（构建期）只解实体+去标签、**保留换行**，
// 而 HN 模板那四行是按行锚定（^…$）的 —— 实时通道要是先压掉换行再交给
// rewriteHnSummary，那把刀在运行时永远开不了火（正是 R43 要收的断链）。
function unwrapCdata(text) {
  return text
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<!\[CDATA\[/g, '')
    .replace(/\]\]>/g, '');
}

function stripTagsKeepLines(text) {
  return text
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')
    .replace(/<[^>]+>/g, '')
    .replace(/<[^>]*$/, '');   // 移除末尾未闭合的标签片段（如 <video src="..." controls="controls" webkit-playsin…）
}

/** 审查 ③（裁定 R50 收窄后）：摘要出口的实体这一格走 `BODY.decodeEntities` —— **单趟**、
 *  只认表内那 **10 个名字**（legacy 四个 `&amp; &lt; &gt; &quot;` 含可省分号与大写形态，
 *  加 2026-10-05 按原始 feed 普查补的 nbsp/apos/rsquo/rarr/ldquo/rdquo；键一共 14 个）
 *  **+ 数字/十六进制引用**，其余一律**整枚原样留着**。"只认预定义 5 个"是普查前的旧口径、已作废。
 *  修前这里是**顺序级联**（`&amp;` 先解
 *  ⇒ `&amp;lt;` 被二次解成真 `<`）+ `&[a-z]+;`→空串 + `&#\d+;`→空串，四条实测形状：
 *    `价格 A &amp;amp; B 折扣`          现 `价格 A &amp; B 折扣`（= py）  / 旧 js `价格 A  B 折扣`
 *    `&copy; 2026`                      现 `&copy; 2026`（字面量）        / 旧 js ` 2026`
 *    `&amp;lt;script&amp;gt;alert(1)`   现 `&lt;script&gt;alert(1)`（= py）/ 旧 js 整条摘要没了
 *    `&#20998;&#25968;`                 现 `分数`（= py）                 / 旧 js 空（汉字被整枚吃掉）
 *  与 Python `html.unescape` 唯一分叉那一格：表外的命名实体（`&copy;` → py `©`）这里留字面量，
 *  两侧**都**不再把实体名吃成空串 —— 那才是本批要修的缺陷；分叉面按**原始 feed**普查（89 个上游源、
 *  抽样 89/957 源 = 9.3%，`&名字;` 共 10 种；既往用"已解码语料"测零暴露是在空集上取读数，永远绿）；
 *  除 amp/lt/gt/quot 外真出现的 nbsp(241)/apos(31)/rsquo(21)/rarr(6)/ldquo(2)/rdquo(2)共 303 处已进
 *  BODY 表，表外（`&copy;` 与"只认特定大小写形态"的 `&Rarr;/&rArr;/&Larr;/&hArr;`，今天零出现）
 *  才留字面量 = **已声明分叉**，钉在 CI 每场必跑的两条判据上：
 *  tests/site_nav/test_body_rules_parity.py::test_entity_name_matrix_matches_python_per_form（名字 ×
 *  形态矩阵，含 title-case 那一档）与 ::test_runtime_entity_exit_gate_runs_without_any_corpus
 *  （真出口 + `_SYN_OFFTABLE` 样本）；::test_corpus_named_entity_sentinel 只是**本地可选度量**
 *  （没有大语料就 skip ⇒ 从不作为 CI 上的闸），不许拿它当"零暴露"的出处。
 *  反例同样钉住：正文里合法存在的 `&amp;lt;img&amp;gt;`（要展示的代码字面量）解一层之后
 *  仍然是字面量，不会被激活成真标签。判据：
 *  tests/site_nav/test_body_rules_parity.py §审查③（同面对账 + 分叉声明 + 语料哨兵 + 真出口对照）。 */
function stripHtmlKeepLines(text) {
  if (!text) return '';
  return stripTagsKeepLines(BODY.decodeEntities(unwrapCdata(text)));
}

/** ⚠ 标题与去重键那一格**仍是修前那段顺序级联**，与构建期不同源 —— 已知挂起，
 *  别把这段当成"两侧同一套实体口径"。为什么本批不动它：三条只注入 `COVER`、不注入
 *  `BODY` 的 node 切片判据（`tests/rss_history/test_cleanlink_and_dedup_safety.py`、
 *  `tests/rss_history/test_parse_parity_js.py`，他人线本批不许改）跑的是真 `parseFeed`，
 *  标题链一旦吃 `BODY` 就红在 ReferenceError 上；而把 require 挪进切片能覆盖到的位置，
 *  运行时就要按 cwd 动态解析路径（Vercel 打包靠静态跟踪 require）⇒ 代价比收益贵。
 *  暴露面也量过（2026-10-05，真语料 18,037 条，逐条比"旧级联"与"新最小集合"的输出）：
 *  title/title_zh 差异 **0** 条、summary/summary_zh 差异 **3** 条 ⇒ 这一格今天没有用户可见的
 *  暴露，留着的是"没测出暴露"而不是"已知的缺陷"。摘要那一格已经换了口径，别把两边搞混。 */
function legacyEntityCascade(text) {
  return text
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
    .replace(/&#\d+;/g, '')
    .replace(/&[a-z]+;/gi, '');
}

function stripHtmlKeepLinesLegacy(text) {
  if (!text) return '';
  // 顺序与修前逐字相同：CDATA → 实体 → 去标签（换回来会在"转义过的 CDATA 记号"上给
  // 出不同答案，那条不是本批要动的行为）
  return stripTagsKeepLines(legacyEntityCascade(unwrapCdata(text)));
}

// 出口形状不变：压空白 + trim 与改动前逐字同形（标题、去重键等调用点不受影响）。
function collapseRuns(text) {
  return text.replace(/\s+/g, ' ').trim();
}

function stripHtml(text) {
  return collapseRuns(stripHtmlKeepLinesLegacy(text));
}

const SAFE_TAGS = new Set(['p','br','img','a','b','i','em','strong','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','figure','figcaption','table','tr','td','th','thead','tbody','span','div','hr','sup','sub','dl','dt','dd','audio','video','source','iframe']);

// 允许的 iframe 域名（YouTube / Vimeo embed）
const SAFE_IFRAME_HOSTS = /youtube\.com|youtu\.be|vimeo\.com/i;

function sanitizeHtml(text) {
  if (!text) return '';
  // ⚠ 这里仍是**顺序级联**（`&amp;` 先解，`&amp;lt;` 会被解成真标签），与构建期
  // `_sanitize_html` 的第一句 `html.unescape` 不同源 —— 对抗审查 ③ 只派工了摘要那一格
  // （`stripHtmlKeepLines`，已改走 `BODY.decodeEntities`）。正文这一格的同批对齐登记为
  // 已知挂起，别把这段当成"和构建期同一套实体口径"。
  text = text
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<!\[CDATA\[/g, '')
    .replace(/\]\]>/g, '')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
  text = text.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '');
  text = text.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
  // 提取安全 iframe（YouTube / Vimeo），替换为占位符，清洗后还原
  const safeIframes = [];
  const safeIframeRe = /<iframe\s[^>]*src\s*=\s*"([^"]*(?:youtube\.com|youtu\.be|vimeo\.com)[^"]*)"[^>]*>\s*<\/iframe>/gi;
  text = text.replace(safeIframeRe, (match) => {
    safeIframes.push(match);
    return '\x00IFRAME' + (safeIframes.length - 1) + '\x00';
  });
  // 移除剩余非安全 iframe
  text = text.replace(/<iframe[^>]*>[\s\S]*?<\/iframe>/gi, '');
  text = text.replace(/<form[^>]*>[\s\S]*?<\/form>/gi, '');
  text = text.replace(/<[^>]+>/g, (match) => {
    const m = match.match(/^<\/?(\w[\w-]*)/);
    if (!m) return '';
    const tag = m[1].toLowerCase();
    if (!SAFE_TAGS.has(tag)) return '';
    // iframe 二次校验：仅放行 YouTube / Vimeo
    if (tag === 'iframe') {
      const srcMatch = match.match(/src\s*=\s*"([^"]*)"/i);
      if (!srcMatch || !SAFE_IFRAME_HOSTS.test(srcMatch[1])) return '';
    }
    const ALLOWED_ATTRS = {
      img: new Set(['src', 'alt']),
      a: new Set(['href']),
      iframe: new Set(['src', 'width', 'height', 'frameborder', 'allowfullscreen']),
      audio: new Set(['src', 'controls', 'preload']),
      video: new Set(['src', 'controls', 'preload', 'poster', 'width', 'height']),
      source: new Set(['src', 'type']),
    };
    const allowed = ALLOWED_ATTRS[tag] || null;
    const attrs = [];
    const attrRe = /([\w-]+)\s*=\s*"([^"]*)"/g;
    let am;
    while ((am = attrRe.exec(match)) !== null) {
      if (/^on/i.test(am[1])) continue;
      if (am[1].toLowerCase() === 'href' && am[2].trim().toLowerCase().startsWith('javascript:')) continue;
      if (allowed && !allowed.has(am[1].toLowerCase())) continue;
      attrs.push(am[1] + '="' + am[2] + '"');
    }
    // 布尔属性（如 controls）无 ="value"，单独检测
    if (tag === 'audio' || tag === 'video') {
      if (/\bcontrols(?:\s|>|\/)/i.test(match) && (!allowed || allowed.has('controls'))) {
        attrs.push('controls');
      }
    }
    const isClose = match.startsWith('</');
    if (attrs.length) return '<' + (isClose ? '/' : '') + tag + ' ' + attrs.join(' ') + '>';
    return isClose ? '</' + tag + '>' : '<' + tag + '>';
  });
  // 还原安全 iframe
  for (let i = 0; i < safeIframes.length; i++) {
    text = text.replace('\x00IFRAME' + i + '\x00', safeIframes[i]);
  }
  return text.trim();
}

// ── 深度清洗：移除 RSS 正文中的广告、推广、引导关注等噪音 ──

function deepCleanHtml(text) {
  if (!text) return '';
  // 1. 【已删除】按 class/id 整枚删容器的规则 —— 与构建期 `_deep_clean_html` 同一笔账（R42）：
  //    本函数的唯一调用点是 `deepCleanHtml(sanitizeHtml(...))`，而上面那个 sanitizeHtml 的
  //    ALLOWED_ATTRS 只留 src/href/alt/…，class/id 到不了这里 ⇒ 那条刀命中恒为 0，
  //    留着就是"看着有防线、实际为 0"的假绿。闸：tests/rss_history/test_no_inert_deep_clean_rules.py
  //    （构建期侧）+ tests/site_nav/test_body_rules_parity.py 的 deepCleanHtml 无 class 判据（这一侧）。
  // 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）
  text = text.replace(/<a[^>]*href="[^"]*(?:link-proxy|wechat2rss|mp\.weixin\.qq\.com)[^"]*"[^>]*>[^<]*<\/a>/gi, '');
  text = text.replace(/<a[^>]*>[^<]*\u8df3\u8f6c\u5fae\u4fe1[^<]*<\/a>/gi, '');
  // 2. 逐块检测：剥离内联标签后匹配推广模式
  const promoRe = /\u4ee3\u5f00\u5173\u6ce8|\u957f\u6309\u4e8c\u7ef4\u7801|\u626b\u7801\u5173\u6ce8|\u626b\u4e00\u626b\u5173\u6ce8|\u5fae\u4fe1\u641c\u7d22.*\u5173\u6ce8|\u5173\u6ce8\u516c\u4f17\u53f7|\u5173\u6ce8\u6211\u4eec|\u7acb\u5373\u8d2d\u4e70|\u70b9\u51fb\u9886\u53d6|\u70b9\u51fb\u6ce8\u518c|\u9650\u65f6\u4f18\u60e0|\u79d2\u6740\u6d3b\u52a8|\u52a0\u5165\u793e\u7fa4|\u52a0\u5165\u6211\u4eec|\u52fe\u9009\u5173\u6ce8|\u957f\u6309\u5173\u6ce8|\u8bc6\u522b\u4e8c\u7ef4\u7801|\u4e8c\u7ef4\u7801|\u957f\u6309\u8bc6\u522b|\u5173\u6ce8.*\u516c\u4f17\u53f7|\u5173\u6ce8.*\u5fae\u4fe1|\u70b9\u51fb.*\u8ba2\u9605|\u8ba2\u9605.*\u9891\u9053|\u8ba2\u9605.*\u90ae\u4ef6|\u52a0\u5165.*\u90ae\u4ef6\u5217\u8868|\u5fae\u535a.*\u5173\u6ce8|\u5173\u6ce8.*\u5fae\u535a|\u5206\u4eab.*\u597d\u53cb|\u8f6c\u53d1.*\u670b\u53cb|\u8f6c\u53d1.*\u5173\u6ce8|\u5173\u6ce8.*\u8f6c\u53d1|\u70b9\u8d5e.*\u5173\u6ce8|\u5173\u6ce8.*\u70b9\u8d5e|\u70b9\u8d5e.*\u5728\u770b|\u559c\u6b22.*\u5173\u6ce8|\u559c\u6b22.*\u70b9\u8d5e|\u89c9\u5f97.*\u5173\u6ce8|\u89c9\u5f97.*\u6709\u7528|\u7cbe\u5f69.*\u4e0d\u9519\u8fc7|\u8bf7\u957f\u6309|\u8bf7\u626b\u7801|\u70b9\u51fb\u539f\u6587|\u70b9\u51fb.*\u539f\u6587|\u70b9\u51fb.*\u67e5\u770b\u539f\u6587|buy now|subscribe\s+(?:now|today)|limited.?time|click here to|sign up (?:now|today)|special offer|discount code|use code|free trial|donate (?:now|today)|support us|follow us (?:on|for)|join our|share this (?:article|post)/i;
  text = text.replace(/<(p|div)\b[^>]*>[\s\S]*?<\/\1>/gi, (block) => {
    const plain = block.replace(/<[^>]+>/g, '');
    return promoRe.test(plain) ? '' : block;
  });
  // 2.5 站内导航链接：**去链接留文字**（R43 接上运行时出口；与构建期同一把刀，
  //     端口在 lib/body_rules.js 的 delinkNavLinks，逐条对账判据在
  //     tests/site_nav/test_body_rules_parity.py）。位置照构建期的规则 2.5：
  //     在逐块推广检测之后、收尾空块之前 —— 挪到 sanitize 之前会看不见绝对化后的 href，
  //     挪到最后会让规则 3 收尾不了被脱空的壳。
  //     外部正文链接一条不许被动（`https://about.fb.com/news/…` 的 about 在 host 上、
  //     `…/archive/html/…` 的 archive 在中段，都不算站内导航）。
  text = BODY.delinkNavLinks(text);
  // 2.6 空锚点 / 站点根链接（"阅读更多""返回首页"那类壳）**整枚**去掉（审查 ④）。
  //     构建期 `_deep_clean_html` 的这一格以前只有 Python 一份，实时出口的 fc 没有它 ⇒
  //     同一份 content:encoded 两侧产物不同。端口在 lib/body_rules.js 的
  //     dropEmptyAnchorLinks，只吃 `href="#"` 与 `href="/"` 两种精确形状
  //     （`#sec-2` 那种有内容的页内锚点必须活着）。位置照构建期的 2.6：2.5 之后、
  //     规则 3 之前 —— 这两把刀删空的 `<p><a href="#">…</a></p>` 壳正需要规则 3 收尾。
  text = BODY.dropEmptyAnchorLinks(text);
  // 3. 移除清洗后残留的空块元素
  text = text.replace(/<(?:p|div|span)\b[^>]*>\s*(?:<br\s*\/?>\s*)*<\/(?:p|div|span)>/gi, '');
  // 4. 压缩连续空行（保留段落间距）
  text = text.replace(/(?:\s*\n){3,}/g, '\n\n');
  return text.trim();
}

function truncate(text, maxLen) {
  if (!text) return '';
  text = text.trim();
  if (text.length <= maxLen) return text;
  const cut = text.slice(0, maxLen).lastIndexOf('。');
  return (cut > maxLen * 0.5 ? text.slice(0, cut + 1) : text.slice(0, maxLen)) + '…';
}

// ── Feed 解析 ──

function extractMediaFromEntry(entry) {
  // enclosure
  const encMatch = entry.match(/<enclosure[^>]*>/i);
  if (encMatch) {
    const typeM = encMatch[0].match(/type\s*=\s*"([^"]*)"/i);
    const urlM = encMatch[0].match(/url\s*=\s*"([^"]*)"/i);
    if (urlM && typeM) {
      const type = typeM[1].toLowerCase();
      if (type.startsWith('audio') || type.startsWith('video')) {
        return { media_url: cleanLink(urlM[1]), media_type: type };
      }
    }
  }
  // media:content
  const mcMatch = entry.match(/<media:content[^>]*>/i);
  if (mcMatch) {
    const urlM = mcMatch[0].match(/url\s*=\s*"([^"]*)"/i);
    const medM = mcMatch[0].match(/medium\s*=\s*"([^"]*)"/i);
    if (urlM && medM && (medM[1] === 'audio' || medM[1] === 'video')) {
      return { media_url: cleanLink(urlM[1]), media_type: medM[1] };
    }
  }
  return {};
}

// 摘要出口的三把刀（R43）：实时通道 `?source=`/`?batch=` 自己抓上游，
// 之前只走 truncate(stripHtml(...)) ⇒ 构建期的两把摘要刀在这里是断链。
// 顺序**是契约**，与构建期逐字一致：先 HN 模板重写、再粘连裸链接剥离
// （倒过来会先改掉 HN 重写要看的行首形状，两条互相吞；构建期行为证据见
//  tests/rss_history/test_body_noise_strip.py::test_order_matters_when_meta_lines_come_before_the_glued_line）。
// `stripHtmlKeepLines` 而不是 `stripHtml`：换行必须先留着，否则 HN 那四行按行锚定的
// 正则一条都看不见（压成空格=另一把开不了火的刀）。压空白放回**最后**一步做，
// 出厂形状与改动前一致。
// 输入用 `it.summaryRaw`（未剥标签的原始 description）而不是 `it.summary`：后者在 parseFeed 里
// 已经过了 `stripHtml` 那一格（**旧顺序级联**，见上方 `legacyEntityCascade` 的"已知挂起"），
// 再解一遍就是**双重解码**（`&amp;amp;` → `&amp;` → `&`），与构建期"实体只走一趟"的口径分叉。
// 摘要这一格现在走 `BODY.decodeEntities`：**单趟**、认不出的一律整枚原样留着，与构建期
// `_strip_html` 的 `html.unescape` 在"表内 10 个名字 + 数字/十六进制"这一圈上逐字节同形
// （裁定 R50：只对齐这一圈，不搬整张 html5 表；"预定义 5 个"是普查前的旧口径、已作废）。
// 唯一分叉：表外的命名实体（`&copy;` → py `©`）这里留字面量 —— 两侧都不再把实体名吃成空串，
// 那正是本批修的缺陷。
// 这一格的账（2026-10-05 复评改口，别再写成"全体零暴露"）：分叉面是在 **89 个上游源的原始
// feed** 上普查的（抽样 89/957 源 = 9.3%，957 取 `rss_sources.json`），`&名字;` 共 **10 种**、
// 全部已进表；表外**仍有** Python 会解的名字 —— `&copy;` 与"只认特定大小写形态"的
// `&Rarr;`/`&rArr;`/`&Larr;`/`&hArr;`（带分号才解，分别给 U+21A0/U+21D2/U+219E/U+21D4）——
// 它们在这 89 个源的普查里今天零出现 ⇒ 那是**已声明分叉**（按 R50 不扩表），由
// tests/site_nav/test_body_rules_parity.py::test_entity_name_matrix_matches_python_per_form
// （名字 × 形态矩阵，含 title-case 那一档）与同一文件的 `_SYN_OFFTABLE` 样本
// （真出口 + CI 每场必跑的合成闸）钉住。
// 旧版这里写的是"18,037 条真语料 / 41,763 个 token 零暴露，并由哨兵判据钉住"：那批字段
// **入库前已经过 `html.unescape`**，在它上面数命名 token 正是本批点名的**空集读数**（恒绿），
// 而 ::test_corpus_named_entity_sentinel 没有大语料就 skip ⇒ 它从来不是 CI 上的闸。
// （同面的逐字节对账与 `double_encoded_amp`、`&amp;lt;img&amp;gt;` 那批代码字面量样本在同文件）。
function cleanSummary(raw, link) {
  return collapseRuns(BODY.stripGluedUrl(BODY.rewriteHnSummary(stripHtmlKeepLines(raw), link)));
}

// 出厂卡片（与构建期快照的 t/u/s/d 同一形状）。**为什么住在顶层而不是 fetchOne 里**：
// 这样它和 parseFeed 一起落在 `extractTag..fetchOne` 那段可离线 eval 的区段里，判据能拿
// "真 parseFeed 的产物 + 真出口函数"给出接线证据；fetchOne 是 async + 真网络，
// 接在里面就只能退回静态 grep（静态 grep 在子匹配变异下不红，Task 8/9 §4.2 栽过）。
function toCardItem(it) {
  const obj = {
    t: it.title,
    u: it.link,
    s: truncate(cleanSummary(it.summaryRaw || '', it.link), SUMMARY_CAP),
    d: it.pub_date,
  };
  // 无 pubDate 时 d 是抓取时刻（datedOrCapture 给的），必须带出降级标记，
  // 否则实时路径的卡片会把抓取时间当发布时间显示。键名沿用构建期快照的长名
  // date_fallback（前端 buildArt/_mergeRemoteSources 读的就是它），且只在真值时写。
  if (it.date_fallback) obj.date_fallback = 1;
  if (it.fullContent) obj.fc = it.fullContent;
  if (it.media_url) { obj.mu = it.media_url; obj.mt = it.media_type; }
  // 封面同样只在真值时写（与 mu/mt 一致）；出口那行 `img: it.img || ''` 靠它才有内容
  if (it.img) obj.img = it.img;
  return obj;
}
// 出厂摘要的**唯一一份**收尾（复核 P0-2）：`s` 到这里已经过**单趟**清洗
// （实时条目走 toCardItem→cleanSummary→BODY.decodeEntities；快照条目的 s 是构建期
// `_strip_html`/`html.unescape` 的产物）。出口**再**过一遍 `stripHtml` 就是第二趟，而那一格
// 走的是旧顺序级联 `legacyEntityCascade` —— 它有一支 `&[a-z]+;`→空串，于是
// `&amp;copy; 2026 版权所有` 单趟得 `&copy; 2026 版权所有`（= 构建期答案），再解一趟就被吃成
// ` 2026 版权所有`。实测四条出口（?batch= / 快照服务 / refresh 合并 T1 / 快照 T2T3）各抄了一份
// 这个第二遍 ⇒ 同一篇文章在两条通道显示不同。长度口径已按裁定统一到构建期那一侧（见 SUMMARY_CAP），
// 只把第二趟解码删掉；标题那一格 `t: stripHtml(...)` 是同一族的另一处，见上方
// `legacyEntityCascade` 的"已知挂起"，本批不动它（改了要连带动他人线 harness 的 parseFeed 口径）。
/* 摘要长度口径：与构建期**同一个数**，不再各截一刀。
 * 参照物 = build_rss_aggregator.py:2274 `def _truncate(s, maxlen=500)`，两条解析出口(:3198 Atom / :3304 RSS)都用这个默认值；
 * 旧写法在这里写死 200 ⇒ 同一条摘要两条通道两个长度（现取 659 条里 209 条长度不同，页面上是"抽屉里完整、列表刷新后剩半句"）。
 * 代价现取读数（7,184 条真条目）：摘要 >200 字 2,596 条、>500 字 524 条，出口 200⇒500 让运行时摘要文本量 +69.2%（750,577⇒1,270,102 字符）；
 * 卡片可见布局不变：`.card-summary` 本来就 `-webkit-line-clamp:4` + overflow:hidden（产物那 500 字一直进的是同一批卡片）。
 * 判据不许把 500 再抄一遍：test_summary_exit_cap_matches_buildtime_default 直接读 `_truncate` 的默认值当参照物。
 */
const SUMMARY_CAP = 500;

function shipSummary(x) {
  // 剥标签这半边**必须留着**：被替换掉的旧行 `truncate(stripHtml(x),200)` 里的 `stripHtml`
  // 干两件事，剥标签那件是原注释写明的"对快照数据做 HTML 清理（防御性）"（:1050），
  // 只有解实体那件是缺陷。两半的切法照 Python `_strip_html`(build_rss_aggregator.py:1145) 的后两步：
  // `<[^>]+>`→空、`<[^>]*$`→空、再 trim；**不许**改用 stripTagsKeepLines —— 它还整块删
  // `<script>` 内容，而 Python 不删，那会把已登记的 P1-5 分叉搬进入口。
  // 剥标签 + 压空白两样都保持改动前的形状，只删掉"第二遍解实体"那一件：
  // 现取本地真快照 9,308 条 `s` 实测，若连 collapseRuns 一起删 ⇒ 新旧出厂不同 2,064 条(22%)、
  // 其中 21 条**变短**：未折叠的连续换行与连续空格会吃掉 200 字预算，尾巴少一句
  // 第二遍解实体那件是缺陷，但 **unwrapCdata 不是**：改动前那条出口的**第一步**就是剥 CDATA 包装，
  // 少了它，下面 `<[^>]+>` 会把 `<![CDATA[正文]]>` 整段当一个标签吃掉 ⇒ 正文直接消失（复评第 4 轮
  // 2026-10-05 拿合成样本量到：旧留「正文」、新出厂空串）。
  const t = unwrapCdata(String(x || '')).replace(/<[^>]+>/g, '').replace(/<[^>]*$/, '');
  return truncate(collapseRuns(t), SUMMARY_CAP);
}


// 全文出口的唯一一份清洗链（RSS 与 Atom 两条解析分支共用）。
// **为什么必须是一个函数而不是两处各写一遍**：对抗审查 ① 实测的就是"Atom 分支自己那段
// 只造 {t,u,s,d}"——四把刀（normalize → sanitize → deepClean → cap）在 Atom 出口集体缺席，
// 同一份 Atom 文档 Python 侧有 full_content、JS 侧没有 fc。两处各写一份就是下一个分叉源。
// 顺序与构建期一致：normalize → sanitize → deepClean → cap（cap 走码点安全的 capBody，
// 不再用 `.slice(0, 50000)`——那会把半个标签截进阅读器，innerHTML 一插就整段崩）。
// base 用条目自己的 `link`（`result.link`/`item.link` 已被写成 `link || '#'`，'#' 不是 base）。
function buildFullContent(fullContent, link) {
  return BODY.capBody(deepCleanHtml(sanitizeHtml(BODY.normalizeBodyHtml(fullContent, link))));
}

function parseFeed(xml, sourceKey, maxItems) {
  const items = [];
  // Atom
  const atomEntries = xml.match(/<entry[^>]*>[\s\S]*?<\/entry>/gi) || [];
  if (atomEntries.length > 0) {
    for (const entry of atomEntries.slice(0, maxItems)) {
      const title = extractTag(entry, 'title');
      const link = cleanLink(extractAttr(entry, 'link', 'href')) || linkOrPermaId(entry, 'link', 'id');
      const summaryTag = extractTag(entry, 'summary');
      // 复评 B1：摘要**只取 `<summary>`**，不许回退到 `<content>`。
      // 参照物 Python Atom 分支就一句 `desc = _strip_html(summary_raw)`（`summary_raw =
      // e.findtext(ns+"summary") or ""`），没有回退。上一波给同一条又产了 `fc` 之后，
      // 回退让 `<summary>` 缺失的条目把同一份正文显示两遍（先 `.r2-summary` 前 200 字、
      // 再 `_insertFulltext(fc)` 全文），实测 py `s=''` / js `s='只有正文'`。
      const summary = summaryTag;
      const contentEncoded = extractTag(entry, 'content') || '';
      // 清洗链只跑一次：门槛要用**清洗后的正文**当左边，右边才与 Python 同基准。
      const cleanedBody = contentEncoded ? buildFullContent(contentEncoded, link) : '';
      // 复评 B2：门槛基准 = 清洗后的正文长度 vs **剥标签后的摘要长度**，与参照物同判
      // （Python `full_content = atom_content if len(atom_content) > len(desc)`，
      // `desc = _strip_html(summary_raw)`）。旧写法比的是两个**原始标签**的长度，两处不同源：
      //   · CDATA 包装白送 12 字符 ⇒ `<summary>短</summary>` + `<content><![CDATA[短]]></content>`
      //     这种"正文==摘要"的形状 JS 出 `fc`、Python 不出（JS 多产）；
      //   · `<summary>` 与 `<content>` 逐字相同时原始长度相等 ⇒ JS 不出 `fc`、
      //     Python 出（清洗后正文带着标签、剥标签后的摘要只剩文字，14 > 7）。
      // 剥标签用现成的 `stripHtmlKeepLines`（解实体走 BODY 那一格端口 + 去标签 + CDATA），
      // 不新写第三套；**不加 `collapseRuns`** —— Python `_strip_html` 只 `.strip()` 首尾、
      // 不压内部空白，压了基准就短一截、方向又错回去。
      const summaryPlain = stripHtmlKeepLines(summaryTag).trim();
      const fullContent = cleanedBody.length > summaryPlain.length ? cleanedBody : '';
      const pubDate = extractTag(entry, 'published') || extractTag(entry, 'updated');
      if (title) {
        const _dt = datedOrCapture(pubDate);
        const item = {
          title: stripHtml(title),
          link: link || '#',
          summaryRaw: summary || '',
          summary: truncate(stripHtml(summary), SUMMARY_CAP),
          pub_date: _dt.pub_date,
        };
        if (_dt.date_fallback) item.date_fallback = true;
        // 审查 ①：这条分支以前只造 {t,u,s,d}，四把刀在这里全是断链。
        // `fullContent` 已经是清洗链的产物（上面算门槛时清洗过一次，不再二次清洗：
        // normalize 不幂等的那一格见 `test_article_snapshot_channel_is_capped_but_not_normalized_again`）。
        if (fullContent) item.fullContent = fullContent;
        const media = extractMediaFromEntry(entry);
        if (media.media_url) { item.media_url = media.media_url; item.media_type = media.media_type; }
        if (COVER) {
          const _cv = COVER.pickItemImage(entry, extractTag(entry, 'content'), extractTag(entry, 'summary'));
          if (_cv) item.img = _cv;
        }
        items.push(item);
      }
    }
    return items;
  }
  // RSS
  const rssItems = xml.match(/<item[^>]*>[\s\S]*?<\/item>/gi) || [];
  for (const item of rssItems.slice(0, maxItems)) {
    const title = extractTag(item, 'title');
    const link = linkOrPermaId(item, 'link', 'guid');
    const desc = extractTag(item, 'description') || '';
    const contentEncoded = extractTag(item, 'content:encoded') || '';
    // ── 出厂两道门槛（B1 摘要回退、B2 全文长度基准）现在都与 Python 同判，理由各见下面两段 ─────
    // B1（摘要回退正文）**本批已收**：参照物 Python 的 RSS 分支就一句
    // `desc = _strip_html(desc_raw)`（`desc_raw = _rss_text(it, "description")`），**没有回退** ⇒
    // `<description>` 缺失时出厂 `summary=''`。这里曾写 `desc || contentEncoded`，于是同一条
    // 既出厂 `s=正文前 200 字`（`.r2-summary`）又出厂 `fc=同一份正文`（`_insertFulltext`）
    // ⇒ 阅读器里同一份正文两遍。判据 = tests/site_nav/test_article_contract.py 的 RSS 语料
    // 两条（行为对账 + 反重复），坏改动登记在 tools/mut_reader.py 的靶 B04。
    // 复评 B2（这一批收口）：门槛基准 = **清洗后的正文长度** vs **剥标签后的摘要长度**，
    // 与参照物 Python 的 RSS 分支同判（`build_rss_aggregator.py:3297`
    // `full_content = content_encoded if len(content_encoded) > len(desc)`，两边都是**清洗后**的值，
    // 右边 `desc = _strip_html(desc_raw)`）。旧写法比两个**原始标签**的长度，两处不同源：
    //   · CDATA 包装白送 12 字符 ⇒ "正文==摘要"那种形状 JS 多出 `fc`、Python 不出；
    //   · 正文里的标签让左边虚高 ⇒ 同一 feed 里 JS 判"有全文"、Python 判"没有"。
    // 真样本：woshipm/ai/6473625.html 原始 12,065 vs 12,157（JS 判出全文），
    // 清洗后 7,906 vs 7,907（Python 判不出）⇒ 该不出，旧写法出了。
    // 清洗链**只跑一次**（与 Atom 分支同一手法）：定门槛用的与出厂的必须是同一份产物，
    // normalize 不幂等那一格见 `test_article_snapshot_channel_is_capped_but_not_normalized_again`。
    const cleanedBody = contentEncoded ? buildFullContent(contentEncoded, link) : '';
    const descPlain = stripHtmlKeepLines(desc).trim();
    const fullContent = cleanedBody.length > descPlain.length ? cleanedBody : '';
    const pubDate = extractTag(item, 'pubDate') || extractTag(item, 'dc:date');
    if (title) {
      const _dt = datedOrCapture(pubDate);
      const result = {
        title: stripHtml(title),
        link: link || '#',
        // B1（与 Atom 分支同一格，理由见上面那段登记）：**只取 `<description>`**，
        // 缺失就是缺失 —— 拿正文顶上等于让同一份正文在阅读器里出现两遍。
        summaryRaw: desc,
        summary: truncate(stripHtml(desc), SUMMARY_CAP),
        pub_date: _dt.pub_date,
      };
      if (_dt.date_fallback) result.date_fallback = true;
      if (fullContent) {
        // 定门槛时已经过一次清洗链（normalize → sanitize → deepClean → cap），这里**不许再过一遍**：
        // normalize 不幂等，二次清洗会改掉出厂形状（Atom 分支同一格同理）。
        result.fullContent = fullContent;
      }
      // 提取 enclosure / media:content 中的音频视频
      const encMatch = item.match(/<enclosure[^>]*>/i);
      if (encMatch) {
        const typeM = encMatch[0].match(/type\s*=\s*"([^"]*)"/i);
        const urlM = encMatch[0].match(/url\s*=\s*"([^"]*)"/i);
        if (urlM && typeM) {
          const t = typeM[1].toLowerCase();
          if (t.startsWith('audio') || t.startsWith('video')) {
            result.media_url = cleanLink(urlM[1]); result.media_type = t;
          }
        }
      }
      if (COVER) {
        const _cv = COVER.pickItemImage(item, contentEncoded, desc);
        if (_cv) result.img = _cv;
      }
      items.push(result);
    }
  }
  return items;
}

// ── 单源内去重 ──

function dedupSourceItems(items, sourceKey) {
  if (!items || items.length < 2) return items;

  // Pass 1: URL 归一化
  for (const it of items) {
    let link = it.link || '';
    // V2EX: 剥离 #replyN
    if (sourceKey && sourceKey.includes('v2ex')) {
      link = link.replace(/#reply\d+$/, '');
    }
    // 通用: 剥离 tracking 参数
    link = link.replace(/[?&](utm_source|utm_medium|utm_campaign|utm_content|at_medium|at_campaign)=[^&]*/g, '');
    // 通用: 剥尾部 fragment（Python 侧同一条规则，负向前瞻保住 v2ex 的 #replyN 锚点）。
    // 漏它的线上代价：der_spiegel_783 的 30/30 条带 `#ref=rss`，与构建期产物里同一篇
    // 文章的链接不相等 ⇒ 抽屉按 link 认不出旧条目，"无损合并"退化成整条替换。
    link = link.replace(/#(?!reply\d+$)[^#]*$/, '');
    link = link.replace(/[?&]$/, '');
    it.link = link;
  }

  // Pass 2: 相同 URL 去重
  const seenUrls = new Set();
  items = items.filter(it => {
    const link = it.link || '';
    /* 没有落点的条目整条删 —— 这是出厂口径，不是中游去重口径：
       `_parse_rss_item` 的 `if not title or not link: return` 与批量出口的
       `!it.u || it.u === '#'` 都不让无链接卡片出厂（无链接 = 死链）。
       这里一度改成"空链接保留，以对齐 Python `if link and link in seen_urls`"——
       那次对齐选错了层：`_dedup_source_items` 确不删空链接，但产物里 360/360 条
       都带真链接，因为上游更早就把它们丢了。按错层的判据改，反而把死链卡片放了进来。 */
    if (!link || seenUrls.has(link)) return false;
    seenUrls.add(link);
    return true;
  });

  // Pass 3: 内容去重
  const isWechat = items.slice(0, 5).some(it => (it.link || '').includes('mp.weixin.qq.com'));
  const normText = (t) => (t || '').replace(/\s+/g, '').toLowerCase().replace(/[^\w\u4e00-\u9fff]/g, '').slice(0, 80);
  const wechatBiz = (link) => {
    const m = (link || '').match(/__biz=([A-Za-z0-9=]+)/);
    return m ? m[1] : '';
  };

  if (isWechat) {
    // 微信策略：标题+摘要都相同才视为重复
    const seenContent = new Set();
    return items.filter(it => {
      const title = (it.title || '').trim();
      if (!title) return true;
      const summary = (it.summary || '').trim();
      const link = it.link || '';
      const dedupKey = wechatBiz(link) + '|' + normText(title) + '|' + normText(summary);
      if (seenContent.has(dedupKey)) return false;
      seenContent.add(dedupKey);
      return true;
    });
  } else {
    // 其他源：标题归一化去重（48h 窗口保护）
    const normTitle = (t) => (t || '').replace(/\s+/g, '').toLowerCase().replace(/[^\w\u4e00-\u9fff]/g, '').slice(0, 50);
    const seenTitles = new Map(); // key → pub_date
    return items.filter(it => {
      const title = (it.title || '').trim();
      if (!title) return true;
      const link = it.link || '';
      const pubDate = it.pub_date || '';
      const dedupKey = normTitle(title);

      if (seenTitles.has(dedupKey)) {
        const prevDate = seenTitles.get(dedupKey);
        if (pubDate && prevDate) {
          const pdCur = new Date(pubDate).getTime();
          const pdPrev = new Date(prevDate).getTime();
          if (!isNaN(pdCur) && !isNaN(pdPrev) && Math.abs(pdCur - pdPrev) > 48 * 3600 * 1000) {
            seenTitles.set(dedupKey, pubDate);
            return true;
          }
        }
        return false;
      }
      seenTitles.set(dedupKey, pubDate);
      return true;
    });
  }
}

// ── 单源抓取 ──

async function fetchOne(source) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT);
  
  try {
    const res = await fetch(source.url, {
      signal: ctrl.signal,
      headers: { 'User-Agent': UA, 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml' },
    });
    clearTimeout(timer);
    
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    
    const xml = await res.text();
    let items = parseFeed(xml, source.key, 30);

    // 单源内去重：URL 归一化 + 标题重复检测
    items = dedupSourceItems(items, source.key);
    
    // 成功：更新滚动缓存（映射住在顶层的 toCardItem，判据才能离线跑真出口）
    const cached = {
      items: items.map(toCardItem),
      lastModified: new Date().toUTCString(),
    };
    rollingCache.set(source.key, cached);
    
    return {
      key: source.key,
      name: source.name,
      cat: source.cat,
      color: source.color,
      url: source.url,
      ...cached,
    };
  } catch (err) {
    clearTimeout(timer);
    console.error(`[rss] ${source.key} failed:`, err.message);
    
    // 失败：返回滚动缓存中的旧数据
    const cached = rollingCache.get(source.key);
    if (cached) {
      return {
        key: source.key,
        name: source.name,
        cat: source.cat,
        color: source.color,
        url: source.url,
        ...cached,
        _stale: true,  // 标记为旧数据
      };
    }
    
    // 无缓存：返回空
    return {
      key: source.key,
      name: source.name,
      cat: source.cat,
      color: source.color,
      url: source.url,
      items: [],
      lastModified: new Date().toUTCString(),
      _error: err.message,
    };
  }
}

// ── 并发控制 ──

async function fetchAllBatched(sources) {
  const results = [];
  for (let i = 0; i < sources.length; i += CONCURRENCY) {
    const batch = sources.slice(i, i + CONCURRENCY);
    const batchResults = await Promise.all(batch.map(fetchOne));
    results.push(...batchResults);
  }
  return results;
}

// ── Handler ──

export default async function handler(req, res) {
  // CORS 头：允许 GitHub Pages 跨域访问
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  
  // 处理 OPTIONS 预检请求
  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  // 轻量探测端点：仅返回快照总条数，供前端轮询检测新构建（避免整包拉取 15MB）
  if (req.query && req.query.meta === '1') {
    let metaTotal = 0;
    try {
      const snapMeta = loadSnapshot();
      if (snapMeta && snapMeta.sources) {
        metaTotal = snapMeta.sources.reduce((n, s) => n + (s.items || []).length, 0);
      }
    } catch (e) { /* 快照不可用时返回 0，前端不会触发合并 */ }
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    return res.status(200).json({ total: metaTotal });
  }

  // ── 分批实时抓取端点：前端 refresh 时逐批拉取 T2/T3/T4 源 ──
  // GET /api/rss?batch=0  → 第 0 批（源 0-199）
  // GET /api/rss?batch=1  → 第 1 批（源 200-399）
  // GET /api/rss?batch_info=1 → 返回批次元信息（总批次数、源总数）
  if (req.query && req.query.batch_info === '1') {
    try {
      const sources = loadSources();
      const nonT1 = sources.filter(s => s.tier !== 1);
      const totalBatches = Math.ceil(nonT1.length / BATCH_SIZE);
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      res.setHeader('Cache-Control', 'no-store');
      return res.status(200).json({
        totalSources: nonT1.length,
        batchSize: BATCH_SIZE,
        totalBatches: totalBatches,
      });
    } catch (err) {
      console.error('[rss] batch_info error:', err);
      return res.status(500).json({ error: 'Internal server error' });
    }
  }

  if (req.query && req.query.batch !== undefined) {
    try {
      const batchIdx = parseInt(req.query.batch, 10);
      if (isNaN(batchIdx) || batchIdx < 0) {
        return res.status(400).json({ error: 'invalid batch index' });
      }
      const sources = loadSources();
      const nonT1 = sources.filter(s => s.tier !== 1);
      const start = batchIdx * BATCH_SIZE;
      const end = Math.min(start + BATCH_SIZE, nonT1.length);
      if (start >= nonT1.length) {
        return res.status(404).json({ error: 'batch out of range' });
      }
      const batchSources = nonT1.slice(start, end);
      const totalBatches = Math.ceil(nonT1.length / BATCH_SIZE);
      console.log(`[rss] Batch ${batchIdx}/${totalBatches}: fetching ${batchSources.length} sources (${start}-${end - 1})`);

      const results = await fetchAllBatched(batchSources);

      // 快照只加载一次：既给闸门当"我方已见过该 URL"的时间索引（等价 Python 侧的 first_seen），
      // 也给标题翻译复用已译结果。loadSnapshot() 解析的是几十 MB 的分块快照，
      // 在同一次请求里加载两遍会在 serverless 内存上限下把端点自己打挂。
      let snapObj = null;
      try { snapObj = loadSnapshot(); } catch (e) { /* 快照不可用：闸门按"没有可核时间"处理 */ }
      const knownDates = buildDateIndex(snapObj);

      // 出口闸门：本端点完全不吃构建产物，不过闸就等于把 72h 契约绕过去了
      const gated = gateSources(results, batchSources, knownDates);

      // 快照翻译索引（URL → 已翻译标题），避免重复翻译构建时已翻译的条目
      let snapshotTrans = null;
      if (snapObj && snapObj.sources) {
        snapshotTrans = new Map();
        for (const src of snapObj.sources) {
          for (const it of (src.items || [])) {
            if (it.u && it.t) snapshotTrans.set(it.u, it.t);
          }
        }
      }

      // 格式化返回（与快照格式对齐）
      const formattedSources = gated.map(src => ({
        key: src.key,
        name: src.name,
        cat: src.cat,
        color: src.color,
        tier: nonT1.find(s => s.key === src.key)?.tier || 3,
        items: (src.items || []).map(it => ({
          t: stripHtml(it.t || ''),
          u: it.u || '#',
          s: shipSummary(it.s),
          d: it.d || '',
          // 与构建期快照同一键名、同一只在真值时写的形状；每源每条目都固定写 0 会白涨 payload。
          ...(it.date_fallback ? { date_fallback: 1 } : {}),
          fc: it.fc || '',
          img: it.img || '',
          mu: it.mu || '',
          mt: it.mt || '',
        })),
      }));

      const totalItems = formattedSources.reduce((n, s) => n + s.items.length, 0);
      console.log(`[rss] Batch ${batchIdx} done: ${formattedSources.length} sources, ${totalItems} items`);

      // 标题翻译：复用快照已有翻译，仅翻译构建后新发布的条目
      try {
        await translateTitlesForSources(formattedSources, snapshotTrans);
      } catch (e) {
        console.error('[rss] Title translation failed (non-fatal):', e.message);
      }

      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      res.setHeader('Cache-Control', 'no-store');
      res.setHeader('X-RSS-Batch', `${batchIdx}/${totalBatches}`);
      res.setHeader('X-RSS-Retention', retentionHeader());
      return res.status(200).json({
        batch: batchIdx,
        totalBatches: totalBatches,
        sources: formattedSources,
      });
    } catch (err) {
      console.error('[rss] Batch fetch error:', err);
      return res.status(500).json({ error: 'Internal server error' });
    }
  }

  // 单源实时抓取端点：前端点开某个信源时调用，返回该源最新内容
  if (req.query && req.query.source) {
    try {
      const sources = loadSources();
      const src = sources.find(s => s.key === req.query.source);
      if (!src) {
        return res.status(404).json({ error: 'source not found' });
      }
      const result = await fetchOne(src);
      res.setHeader('Content-Type', 'application/json; charset=utf-8');
      res.setHeader('Cache-Control', 'no-store');
      // 源键必须转义后再进响应头：Node 的 ServerResponse 只收 latin1，中文源键会抛
      // ERR_INVALID_CHAR，被外层 catch 变成 500 —— 生产实测 542/968 个源（键含中文）
      // 的抽屉实时刷新全部失效，而页面 fetch 的 .catch 把它静默吞成"刷新没反应"。
      res.setHeader('X-RSS-Single', encodeURIComponent(src.key));
      // 单源抽屉也是实时抓取出口：不过闸的话，点开一个停更源就能看到几年前的旧文。
      // knownDates 传 null：这里没有已加载的快照，为一次点击去解析几十 MB 不值得，
      // 代价是"上游没给日期且快照没见过"的条目在单源视图里会被判不了龄丢掉（墙侧仍按快照续命）。
      const [gatedOne] = result && result.items ? gateSources([result], [src], null) : [result];
      res.setHeader('X-RSS-Retention', retentionHeader());
      return res.status(200).json(gatedOne || result);
    } catch (err) {
      console.error('[rss] Single source error:', err);
      return res.status(500).json({ error: 'Internal server error' });
    }
  }

  const now = Date.now();
  const isRefresh = req.query && req.query.refresh === '1';
  
  // 检查完整响应缓存（refresh 时跳过缓存）
  if (fullCache.v && now - fullCache.t < CACHE_TTL && !isRefresh) {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'public, max-age=300');
    res.setHeader('X-RSS-Cache', 'hit');
    return res.status(200).json(fullCache.v);
  }
  
  try {
    // 优先返回构建时生成的 API 快照（包含 72h 累积历史数据）
    if (!isRefresh) {
      const snapshot = loadSnapshot();
      if (snapshot && snapshot.sources && snapshot.sources.length > 0) {
        // 对快照数据做 HTML 清理（防御性）
        snapshot.sources = snapshot.sources.map(src => ({
          ...src,
          items: (src.items || []).map(item => ({
            ...item,
            t: stripHtml(item.t || ''),
            s: shipSummary(item.s),
          })),
        }));
        // 兜底翻译：构建时翻译失败（熔断/限流/超时）的英文标题，在快照服务时补翻
        // 已翻译的标题 t 已是中文 → isChinese 判定跳过；仅英文标题走 /api/translate
        try {
          await translateTitlesForSources(snapshot.sources, null);
        } catch (e) {
          console.error('[rss] Snapshot title translation failed (non-fatal):', e.message);
        }
        const total = snapshot.sources.reduce((n, s) => n + s.items.length, 0);
        console.log(`[rss] Serving snapshot: ${snapshot.sources.length} sources, ${total} items`);
        res.setHeader('Content-Type', 'application/json; charset=utf-8');
        res.setHeader('Cache-Control', 'public, max-age=300');
        res.setHeader('X-RSS-Source', 'snapshot');
        return res.status(200).json(snapshot);
      }
    }
    
    // Fallback: 实时抓取 RSS（仅当快照不存在或 refresh=1 时）
    const sources = loadSources();
    const snapshot = loadSnapshot();
    const snapshotMap = {};
    if (snapshot && snapshot.sources) {
      for (const s of snapshot.sources) {
        snapshotMap[s.key] = s;
      }
    }

    // 分层：T1 实时抓取，T2/T3 从快照读取
    const t1Sources = sources.filter(s => s.tier === 1);
    const otherSources = sources.filter(s => s.tier !== 1);
    console.log(`[rss] T1 live fetch: ${t1Sources.length} sources, T2/T3 from snapshot: ${otherSources.length} sources`);

    const t1Results = await fetchAllBatched(t1Sources);

    // T1 英文源标题翻译（复用 batch 同一逻辑：/api/translate 降级链，只翻译标题）
    try {
      await translateTitlesForSources(t1Results);
    } catch (e) {
      console.error('[rss] T1 title translation failed (non-fatal):', e.message);
    }

    // 合并 T1 实时 + T2/T3 快照
    // （原先这里有个 filterItems 只做"72h + 无日期一律保留"，两道口子都补在闸门里：
    //   无日期不再无条件保留，超龄不再靠源自动放宽到永久）
    const mergedSources = t1Results.map(src => ({
      key: src.key, name: src.name, cat: src.cat, color: src.color,
      tier: 1,
      items: (src.items || []).map(item => ({
        ...item,
        t: stripHtml(item.t || ''),
        t_zh: item.t_zh || '',
        s: shipSummary(item.s),
      })),
    })).concat(otherSources.map(src => {
      const snap = snapshotMap[src.key];
      return {
        key: src.key, name: src.name, cat: src.cat, color: src.color,
        tier: src.tier || 3,
        items: (snap ? snap.items : []).map(item => ({
          ...item,
          t: stripHtml(item.t || ''),
          s: shipSummary(item.s),
        })),
      };
    }));

    // 出口闸门：refresh 走的是"实时 T1 + 快照 T2/T3"合并结果，同样必须过同一套规则
    const gatedMerged = gateSources(mergedSources, sources, buildDateIndex(snapshot));
    res.setHeader('X-RSS-Retention', retentionHeader());

    const response = {
      t: new Date().toISOString(),
      sources: gatedMerged,
    };

    // 更新完整响应缓存
    fullCache = { t: now, v: response };

    const liveItemCount = t1Results.reduce((n, s) => n + (s.items || []).length, 0);
    const snapItemCount = gatedMerged.reduce((n, s) => n + (s.items || []).length, 0);
    console.log(`[rss] Refresh merge: T1 live=${liveItemCount} items from ${t1Sources.length} sources, total merged=${snapItemCount} items`);

    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    if(isRefresh) {
      res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate');
      res.setHeader('X-RSS-Refresh', '1');
    } else {
      res.setHeader('Cache-Control', 'public, max-age=300');
    }
    res.setHeader('X-RSS-Cache', 'miss');
    return res.status(200).json(response);
  } catch (err) {
    console.error('[rss] Handler error:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
