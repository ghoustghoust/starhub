// Vercel Serverless Function：管线健康探活（T5）
// GET /api/health（无需认证，只读）→
//   { ok, last_build_age_minutes, star_fast_age_minutes,
//     secrets: { agnes, gh_token, refresh_key }, pages_ok }
//
// 口径（v7 计划：最近【成功】构建年龄 + 触发链状态 + Secret 配置布尔）：
//   last_build_age_minutes = update.yml 最近一场 conclusion=success 的 run 距今分钟数（查不到 = null）
//   star_fast_age_minutes  = star-fast.yml 同口径
//   pages_ok               = Pages 首页 200 且体积 ≥ MIN_INDEX_BYTES——限流占位页 ~698B
//                            曾假绿（HANDOFF §8.20），只认 200 不够
//   emb_ok                 = 线上首页里 "emb":[ 的条数：归零 = false（语义扩展整段静默消失，
//                            前端 DATA.some(d=>d.emb) 直接不走那一支）；1..49 = null（不确定，
//                            不报坏）；≥50 = true。与 pages_ok 共用同一次正文读取，不多打一发。
//   reader_ok              = 阅读器数据面：从线上 rss-data-0.js 尾部现读块总数（开集，不许写死），
//                            再逐块验在不在。true / false / null（探不到 = 不知道）。
//                            2026-10-07 那次"信源只剩 55"就是这条面坏了而 pages_ok 与 run 颜色全绿。
//   secrets.*              = Boolean 包装，只报"配没配"，原始值绝不进响应
//   ok                     = pages_ok 且 reader_ok/emb_ok 均不为 false 且成功构建年龄 ≤ 阈值
//                            （reader_ok=null 不参与：宁可不报，也不制造假警）
//
// 不健康时 HTTP 503（body 仍是同结构 JSON）：外部探活（UptimeRobot 类）按
// 「30 分钟 GET，非 2xx 告警」配置即可同时抓住端点死亡与静默退化——
// 这是本端点存在的理由：本仓最高频的事故模式是静默退化（快照出仓、增量退化、
// Pages 被拖慢都曾安静数天才被发现）。
// 响应缓存 60s：本函数自己会打 GitHub API 与 Pages，不缓存会被轮询烧穿配额。

const ALLOWED_ORIGINS = new Set([
  'https://starhub-refresh.vercel.app',
  'https://kwei168.github.io',
]);

const REPO = 'Kwei168/starhub';
const PAGES_INDEX = 'https://kwei168.github.io/starhub/index.html';
const DATA_CHUNK_BASE = 'https://kwei168.github.io/starhub/rss-data-';
const MIN_INDEX_BYTES = 5000;   // 真首页 ≥250KB；698B 是限流占位页
const MIN_EMB_ENTRIES = 50;     // 语义向量条数的"确定还在"水位（10-08 现取线上 330/331）
const STALE_BUILD_MINUTES = 120; // 小时场连续 2 场缺席视为管线不健康
const MAX_TOTAL_CHUNKS = 25;  // = 生成端 MAX_CHUNKS(24) + chunk0 自己：build_rss_aggregator.py:7463
                              // `n_chunks=min(n_chunks,24)` 后写 `_write_chunk0(1+n_chunks)` ⇒ 上限 25。
                              // 写 24 会在数据量最大的那场恰好"不知道"，写小更糟。
const READ_TAIL_BYTES = 1200; // _total 在 0 块 JSON 的尾部，后缀 Range 只取这一点
const READ_HEAD_BYTES = 64;   // 块头部：够看清 "sources":[] 是不是空壳
const EMPTY_SOURCES = /"?sources"?\s*:\s*\[\s*\]/;  // 生成端两种写法都要认（空壳块用无引号那形）
const READER_DEADLINE_MS = 5000; // 见下：整条探活必须留在函数 maxDuration=15s 之内
const READER_TIMEOUT_MS = 2500;  // 单次请求封顶：6s×(1+N) 会顶穿 maxDuration ⇒ 函数被杀 ⇒ 监控看到非 2xx = 假警
const TTL_MS = 60 * 1000;
const GH_TIMEOUT = 8000;
// 首页现在每次都要读正文（emb 只能从正文数），1.2MB 在 6s 里偏紧 ⇒ 抬到 10s。
// 三个探针是 Promise.all 并发，所以预算看的是最慢那支而不是加和：max(10000, 8000, 2*2500+5000) < 15000。
const PAGES_TIMEOUT = 10000;

const cache = { t: 0, ok: true, body: null };

function ageMinutes(iso) {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso).getTime();
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms / 60000)) : null;
}

// 最近【成功】构建的年龄：不过滤地拉最近 30 场、客户端按 conclusion 挑最新 success。
// ⚠ 幻影读数复盘（2026-10-04 两次实锤，锚点都是 09-24T18:00Z 的 #1361）：
// 服务端 `status=success` 过滤视图会间歇性返回停在 9 天前的陈旧列表（未过滤视图
// 始终正确）⇒ 不信任服务端过滤索引，客户端裁决。30 场覆盖 ≥24h，页内必有 success。
async function lastSuccessAgeMinutes(workflow) {
  const u = new URL('https://api.github.com/repos/' + REPO + '/actions/workflows/' + workflow + '/runs');
  u.searchParams.set('per_page', '30');
  const headers = {
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'starhub-refresh',
  };
  if (process.env.GH_TOKEN) headers.Authorization = 'Bearer ' + process.env.GH_TOKEN;
  const r = await fetch(u, { headers, signal: AbortSignal.timeout(GH_TIMEOUT) });
  if (!r.ok) return null;
  const j = await r.json();
  const runs = (j.workflow_runs || []);
  let newest = null;
  for (const run of runs) {
    if (run.conclusion === 'success' && run.created_at && (!newest || run.created_at > newest)) {
      newest = run.created_at;
    }
  }
  if (!newest) {
    console.log(JSON.stringify({ ts: new Date().toISOString(), type: 'health_no_success_in_page', workflow, got: runs.length }));
  }
  return ageMinutes(newest);
}

// 一次取回首页正文，同时得出两个信号：pages（在不在、够不够大）与 emb（语义向量还剩几条）。
// 为什么合成一个探针而不是再加一个请求：正文就是 1.2MB 那一份，分两次取等于让探活自己翻倍花销；
// 而 emb 只有从**线上产物**数才说明问题——库里读不到（产物 10-06 起已退出 git），构建日志又只有
// 当时的打印，跨不过"上一场好、这一场坏"这件事。
async function pagesProbe() {
  try {
    const r = await fetch(PAGES_INDEX, {
      headers: { 'User-Agent': 'starhub-refresh' },
      signal: AbortSignal.timeout(PAGES_TIMEOUT),
    });
    if (!r.ok) return { pages: false, emb: null };
    // 必须真读正文：以前只看 content-length 就返回，那条路对"体积够但语义向量归零"是瞎的
    const body = await r.text();
    const pages = body.length >= MIN_INDEX_BYTES;   // 占位体 ~698B 假绿教训（§8.20）
    if (!pages) return { pages: false, emb: null };
    const n = (body.match(/"emb":\[/g) || []).length;
    // 三态：归零 = 确定坏（语义扩展整段消失）；1..MIN-1 = 不确定（早期规模本就可能小，不许据此报坏）；
    // ≥MIN = 好。"不确定"仍然是 null，不参与 ok——同 readerChunksOk 的纪律。
    let emb = null;
    if (n === 0) emb = false;
    else if (n >= MIN_EMB_ENTRIES) emb = true;
    return { pages: true, emb };
  } catch (e) {
    // 网络层取不到（DNS/超时/连接断）与"服务端明确回了非 2xx"不是一回事：
    // 前者算不知道，后者才算坏。把抖动报成坏会催生绕过开关——这一版最初就是这么写的，
    // 本机实测一次慢网络直接 503（pages_ok=false / reader_ok=null / emb_ok=null），据此改。
    // 真停更仍会由 buildAge 抓到：发布停下来 ⇒ 成功构建年龄越过 120 分钟 ⇒ 503。
    return { pages: null, emb: null };
  }
}

async function readerChunksOk() {
  // 阅读器数据面的完整性。块数是**开集**（随数据量浮动，6MB 一块），所以总数不能写死在探测里——
  // 从线上 0 块 JSON 尾部的 `_total` 现读，再逐块用与浏览器同色的 GET 验在不在、是不是空壳。
  // 这一条补的是 2026-10-07 的盲区：那次"信源只剩 55"是 chunk1 被抹成 404，
  // 而 pages_ok 只看首页 200+体积、buildAge 只看 run 成功 ⇒ 唯一用户可见的损坏安静了两个小时。
  // 三态返回：true 完整 / false 确定性地坏 / null 探不到或不确定。
  // null 不参与 ok——假警比无警更糟（它下一步一定催生绕过开关，防线自毁）。
  //
  // 「确定性坏」只有两种，都是浏览器取同一 URL 也会得到同一结果的情形：
  //   ① 404/410（块不在这个 revision 上，含 chunk0 自己没了＝整页空白）；
  //   ② 块在但 `"sources":[]` 空壳（生成端"旧块清空不删除"会留下这种文件，
  //      浏览器拿到 onload 却取不到源——10-07 的症状换个写法就重现了）。
  // 5xx、超时、Range 被忽略、解析不出总数，一律 null：那些在浏览器侧是重试可恢复的，
  // 报成坏就是假警。
  const gone = (st) => st === 404 || st === 410;
  const headers = (range) => ({
    'Range': range,
    // 必须显式要 identity：探针靠"切一段"取 _total 与块头，若中间层按 gzip 编码返回，
    // Content-Range 是对压缩体切的，切下来的半截流解压会抛 ⇒ 这条面静默变成永远 null。
    // 本机实测两种请求都没被压缩（206 + 无 Content-Encoding），但 POP 不同 ⇒ 廉价保险。
    'Accept-Encoding': 'identity',
    'User-Agent': 'starhub-refresh',
  });
  const deadline = Date.now() + READER_DEADLINE_MS;
  let total = null;
  try {
    const r = await fetch(DATA_CHUNK_BASE + '0.js', {
      headers: headers('bytes=-' + READ_TAIL_BYTES),
      signal: AbortSignal.timeout(READER_TIMEOUT_MS),
    });
    if (gone(r.status)) return false;   // 0 块没了：_total 与首屏一起消失，整页空白
    if (!r.ok) return null;             // 5xx 等：不确定
    const m = (await r.text()).match(/"_total"\s*:\s*(\d+)/);
    if (!m) return null;                // 读不到声明（旧版本产物/被改写）⇒ 没有判断依据
    total = Number(m[1]);
  } catch (e) {
    return null;   // 连 0 块都取不到 ⇒ 我们没有判断依据，不许据此说站点坏了
  }
  // 上界必须 ≥ 生成端的 MAX_CHUNKS + chunk0 自己（见常量注释）：写小了会在数据量最大的
  // 那场恰好退化成"不知道"——正是最需要它说话的时候闭嘴。
  if (!(total >= 1 && total <= MAX_TOTAL_CHUNKS)) return null;
  for (let i = 1; i < total; i++) {
    if (Date.now() > deadline) return null;               // 时间不够 ⇒ 报不知道，不报好也不报坏
    try {
      const r = await fetch(DATA_CHUNK_BASE + i + '.js', {
        headers: headers('bytes=0-' + READ_HEAD_BYTES),
        signal: AbortSignal.timeout(READER_TIMEOUT_MS),
      });
      if (gone(r.status)) return false;                   // 这块不在 = 浏览器那边就是"内容数据异常"
      if (r.status === 206 && EMPTY_SOURCES.test(await r.text())) return false;  // 空壳块
      // 200（Range 被忽略）时不为了这项检查去拉整块正文；只认状态码在不在
    } catch (e) { /* 抖动不算坏 */ }
  }
  return true;
}

export default async function handler(req, res) {
  const origin = (req.headers['origin'] || '').toLowerCase();
  const allowed = ALLOWED_ORIGINS.has(origin);
  if (allowed) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    res.setHeader('Vary', 'Origin');
  }
  if (req.method === 'OPTIONS') { res.status(204).end(); return; }
  if (req.method !== 'GET') { res.status(405).json({ error: 'Method Not Allowed' }); return; }

  if (cache.body && Date.now() - cache.t < TTL_MS) {
    res.status(cache.ok ? 200 : 503).json(cache.body);
    return;
  }

  try {
    const [buildAge, starAge, pageSig, readerOk] = await Promise.all([
      lastSuccessAgeMinutes('update.yml'),
      lastSuccessAgeMinutes('star-fast.yml'),
      pagesProbe(),
      readerChunksOk(),
    ]);
    // null = 取不到（不参与 ok）；false = 服务端明确回了非 2xx 或正文过小
    const pagesOk = pageSig.pages;
    const embOk = pageSig.emb;
    // readerOk === false 才拉黑；null 是"不知道"，不参与判断（假警会催生绕过开关）
    const ok = pagesOk !== false && readerOk !== false && embOk !== false
           && buildAge !== null && buildAge <= STALE_BUILD_MINUTES;
    const payload = {
      ok,
      last_build_age_minutes: buildAge,
      star_fast_age_minutes: starAge,
      secrets: {
        agnes: Boolean(process.env.AGNES_API_KEY || process.env.AGNES_API_KEYS),
        gh_token: Boolean(process.env.GH_TOKEN),
        refresh_key: Boolean(process.env.REFRESH_KEY),
      },
      pages_ok: pagesOk,
      reader_ok: readerOk,
      emb_ok: embOk,
    };
    cache.t = Date.now();
    cache.ok = ok;
    cache.body = payload;
    res.status(ok ? 200 : 503).json(payload);
  } catch (e) {
    // 探活自身故障也不能报健康；但不写缓存（下一请求重试）
    res.status(503).json({ ok: false, error: 'health probe failed' });
  }
}
