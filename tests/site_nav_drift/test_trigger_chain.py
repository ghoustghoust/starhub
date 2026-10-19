# -*- coding: utf-8 -*-
"""T5 触发链与健康探活判据（A3 advisory）。

钉住四件事（v7 计划 Task 5）：
- refresh.js 的 mode 白名单分流：star → star-fast.yml、缺省 → update.yml，
  且 dispatch 必须发生在 X-Refresh-Key 校验之后（无头扫描器拿不到 key 就打不到 GH 配额）；
- api/health.js 只读探活：响应结构完整；「最近【成功】构建年龄」口径（status=success，
  不是最近 run——失败场不算活）；secrets 三项一律 Boolean 包装，payload 组装区
  不许出现 env 字符串拼接（泄密红线）；不健康返回 503（外部探活"非 2xx 告警"才抓得住静默退化）；
- update.yml 的 `Fetch stars & build` 步注入 GITHUB_TOKEN
  （fetch_and_build.py:1217 消费；缺失 = 小时场 stars 拉取全程匿名 60 req/h 配额）；
- vercel.json 为 health.js 声明 maxDuration（函数超时是部署契约的一部分）。
文本断言（不引 YAML/JS 解析依赖），离线自洽。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REFRESH = os.path.join(ROOT, "api", "refresh.js")
# 变异电池（tools/mut_health_reader.py）在临时副本上打坏改动，靠这个注入点指过去；
# 不设就读真实文件——判据平时读的也是它。（与 test_star_fast_wiring.py 的 STAR_FAST_YML 同形）
HEALTH = os.environ.get("HEALTH_JS") or os.path.join(ROOT, "api", "health.js")
UPDATE_YML = os.path.join(ROOT, ".github", "workflows", "update.yml")
VERCEL_JSON = os.path.join(ROOT, "vercel.json")
# 探活上界要和生成端的分块上限对账（跨文件一致性，本仓惯用手法）
AGG = os.path.join(ROOT, "build_rss_aggregator.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------- refresh.js 分流（已实现，这里钉住行为） ----------

def test_refresh_mode_whitelist_maps_star_to_star_fast():
    src = _read(REFRESH)
    m = re.search(r"WORKFLOW_BY_MODE\s*=\s*\{([^}]*)\}", src)
    assert m, "mode 白名单表必须存在（防任意 workflow 名注入）"
    table = m.group(1)
    assert re.search(r"star:\s*'star-fast\.yml'", table), "mode=star 必须分流到 star-fast.yml"
    assert re.search(r"''\s*:\s*'update\.yml'", table), "缺省必须分流到 update.yml"


def test_refresh_dispatch_after_key_check():
    src = _read(REFRESH)
    key_pos = src.find("keyValid")
    dispatch_pos = src.find("/dispatches")
    assert key_pos != -1 and dispatch_pos != -1
    assert key_pos < dispatch_pos, "dispatch 必须在 X-Refresh-Key 校验之后"


def test_refresh_dispatch_body_pins_ref_main():
    src = _read(REFRESH)
    assert re.search(r"JSON\.stringify\(\{\s*ref:\s*'main'\s*\}\)", src), "dispatch body 必须钉 ref=main"


def test_refresh_unknown_mode_is_400():
    src = _read(REFRESH)
    assert "未知 mode" in src and "400" in src, "白名单外的 mode 必须 400"


# ---------- api/health.js ----------

def test_health_exists_with_full_response_shape():
    src = _read(HEALTH)
    for key in ("ok", "last_build_age_minutes", "star_fast_age_minutes", "pages_ok"):
        assert key in src, "health 响应缺字段: %s" % key
    m = re.search(r"secrets\s*:\s*\{([^}]*)\}", src)
    assert m, "secrets 子对象必须存在"
    for k in ("agnes", "gh_token", "refresh_key"):
        assert k in m.group(1), "secrets 缺布尔项: %s" % k


def test_health_secrets_only_as_booleans():
    src = _read(HEALTH)
    # 取【真对象】而不是文件头注释里的示意形：secrets 块必须含 process.env 引用
    blocks = re.findall(r"secrets\s*:\s*\{([^}]*)\}", src)
    blocks = [b for b in blocks if "process.env" in b]
    assert blocks, "secrets 子对象（含 Boolean 探测）必须存在"
    block = blocks[0]
    for k in ("agnes", "gh_token", "refresh_key"):
        assert re.search(r"%s\s*:\s*Boolean\(" % k, block), "secrets.%s 必须 Boolean 包装" % k
    # 泄密红线：payload 组装区不许把 env 值拼进字符串（Boolean( 探测之外的原始引用）
    payload_zone = src[src.find("const payload"):] if "const payload" in src else src
    assert not re.search(r"[\"'`]\s*\+\s*process\.env\.|process\.env\.\w+\s*\+", payload_zone), \
        "payload 区出现 env 拼接——原始 secret 值可能进入响应"


def test_health_ages_are_last_success_not_last_run():
    src = _read(HEALTH)
    # workflow 名是参数（同一查询函数复用），钉"两个 workflow 都被查"+"成功口径"
    assert "/actions/workflows/" in src, "必须走 workflow runs API"
    assert re.search(r"lastSuccessAgeMinutes\(\s*'update\.yml'\s*\)", src), "必须查小时场 run"
    assert re.search(r"lastSuccessAgeMinutes\(\s*'star-fast\.yml'\s*\)", src), "必须查快车道 run"
    # 幻影读数复盘（2026-10-04 两次实锤，锚点都是 09-24T18:00Z=#1361）：
    # 服务端 status=success 过滤视图会间歇性停在 9 天前 ⇒ 必须【不过滤】拉最近
    # 30 场、客户端按 conclusion 挑最新 success——不信任服务端过滤索引。
    assert not re.search(r"set\(\s*'status'\s*,\s*'success'\s*\)", src), \
        "禁止服务端 status=success 过滤（幻影视图源头，两次实锤）"
    assert re.search(r"per_page'\s*,\s*'30'", src), "必须拉最近 30 场（覆盖 ≥24h，保证页内有 success）"
    assert re.search(r"conclusion\s*===?\s*'success'", src), "必须客户端按 conclusion 挑最新 success"
    assert "Authorization" in src, "runs 查询要带 token（匿名 60/h 会被探活自己烧穿）"


def test_health_pages_probe_rejects_placeholder_body():
    src = _read(HEALTH)
    assert "kwei168.github.io/starhub/index.html" in src, "pages 探活必须打真实首页"
    # 占位体 ~698B 假绿教训（HANDOFF §8.20）：只认 200 不够，必须卡最小体积
    assert "MIN_INDEX_BYTES" in src, "pages 判定必须含最小体积信号（防限流占位页假绿）"


def test_health_reader_probe_reads_chunk_count_from_the_shipped_file():
    """阅读器数据面探活必须是「内容级 + 开集」：块总数从线上 rss-data-0.js 尾部现读。

    为什么单独立一条：本仓为「闭集表追不上开集」付过一次大代价（10-07 那份带回清单写死 7 个
    名字，每发布一次就把阅读器后台抹掉一次）。探活如果也写死块数（`i < 8`），块数一浮动就永远
    只验前几块 ⇒ 后面的块 404 无人知晓——把事故的认知根源换个地方重演一遍。
    """
    src = _read(HEALTH)
    assert "rss-data-" in src, "reader 探活必须打真实的分块文件"
    assert 'match(/"_total"' in src, \
        "块总数必须从 0 块正文里解析出来（写死数字就是闭集表达开集）"
    m = re.search(r"for\s*\(let i = 1;\s*i < (\w+);\s*i\+\+\)", src)
    assert m, "逐块校验的循环找不到 ⇒ 这条探活可能压根没跑"
    assert m.group(1) == "total", \
        "循环上界必须是现读到的 total（现在是 %s）" % m.group(1)
    # 上界必须跟生成端对齐：build_rss_aggregator 里 n_chunks=min(…,MAX_CHUNKS) 之后写
    # _write_chunk0(1 + n_chunks) ⇒ 实际可达上限是 MAX_CHUNKS + 1。写小会在数据量最大的那场
    # 恰好退化成"不知道"——正是最需要它说话的时候闭嘴（2026-10-08 对抗审查命中）。
    gen_max = int(re.search(r"MAX_CHUNKS = (\d+)", _read(AGG)).group(1))
    probe_max = re.search(r"const MAX_TOTAL_CHUNKS = (\d+)", src)
    assert probe_max, "现读的总数必须设上界（它同时是假警闸门与请求数闸门）"
    assert int(probe_max.group(1)) >= gen_max + 1, \
        "探活上界 %s < 生成端可达 %d 块 ⇒ 数据量最大那场会正好变成『不知道』" % (
            probe_max.group(1), gen_max + 1)
    # Range 切片遇上中间层压缩会静默失效：Content-Range 是按压缩体切的，切下来的半截流解压就抛
    # ⇒ 探针永远返回 null，这条面"装了但没人守"。显式要 identity 把静默失效变成不可能。
    # （10-08 实测：本机两种请求都回 206 无压缩，但 POP 不同 ⇒ 这是廉价保险，不是臆测。）
    # 钉法用"共用一个头构造器 + 两处都用它"，不数 identity 字面量出现次数：实现把它抽成
    # headers(range) 后字面量只有一处，按次数断言会把更好的写法判红（判据不许偏爱啰嗦）。
    assert "'Accept-Encoding': 'identity'" in src, "Range 请求必须显式要 identity"
    assert src.count("headers: headers(") == 2, \
        "取总数与逐块校验两处 Range 请求必须共用同一个头构造器（现在 %d 处用它）" % src.count("headers: headers(")


def test_health_reader_probe_is_tri_state_not_binary():
    """探活必须能表达第三种状态「读不到」，且报坏只能来自确定性 HTTP 信号。

    两个方向都会咬人：把合成写成 `readerOk`（真值判断）⇒「不知道」变 503 ⇒ 监控天天报警
    ⇒ 下一步一定有人加绕过开关，防线自毁；反过来把网络抖动/5xx 写成 return false ⇒ 一次
    Pages 抖动就是一场假警。所以钉的是「报坏的出处必须可数，且每一处都对应一个确定信号」。
    """
    src = _read(HEALTH)
    assert re.search(r"readerOk\s*!==\s*false", src), \
        "ok 的合成必须是『不为 false 才拉黑』；写成 readerOk 真值判断会把『不知道』报成坏"
    fn = src[src.index("async function readerChunksOk"):src.index("export default")]
    # 报坏恰好三处：0 块没了、某块 404/410、空壳块。多一处=把不确定报成坏，少一处=某种确定损坏不响。
    lines = [l.strip() for l in fn.splitlines() if "return false" in l]
    assert len(lines) == 3, \
        "readerChunksOk 报坏应当是 3 处（gone×2 + 空壳×1），实际 %d 处：%s" % (len(lines), lines)
    assert sum(1 for l in fn.splitlines() if re.search(r"gone\(r\.status\)\) return false", l)) == 2, \
        "0 块与逐块的『404/410 才算坏』必须都在——0 块没了等于整页空白，不该报『不知道』"
    assert sum(1 for l in fn.splitlines() if "EMPTY_SOURCES" in l and "return false" in l) == 1, \
        "空壳块（\"sources\":[]）必须算坏：生成端『旧块清空不删除』会留下这种 200 空文件"
    assert "if (!(total >= 1 && total <= MAX_TOTAL_CHUNKS)) return null" in src, \
        "解析出荒谬总数必须退化成不知道"
    assert "Date.now() > deadline" in fn, "没有时间截止 ⇒ 整条探活可能顶穿 maxDuration 被杀成假警"


def test_health_reader_probe_fits_inside_max_duration():
    """整条 reader 探活的时间预算必须留在函数 maxDuration 之内。

    不是洁癖：顶穿 maxDuration ⇒ 函数被杀 ⇒ 监控按「非 2xx 告警」报的是「探活死了」
    这种最难查的形状。算式：首块读取 + 循环截止 + 末次请求 ≤ maxDuration。
    """
    src = _read(HEALTH)
    mt = re.search(r"const READER_TIMEOUT_MS = (\d+)", src)
    md = re.search(r"const READER_DEADLINE_MS = (\d+)", src)
    assert mt and md, "reader 探活必须有单次超时与整体截止（否则时间预算无从核算）"
    m = re.search(r'"api/health\.js"\s*:\s*\{\s*"maxDuration"\s*:\s*(\d+)\s*\}', _read(VERCEL_JSON))
    budget = int(m.group(1)) * 1000
    worst = 2 * int(mt.group(1)) + int(md.group(1))
    # 首页探针从"只看头"改成"读正文"之后它成了最慢那一支（10s）；三个探针是 Promise.all 并发，
    # 所以预算取**最慢支**而不是加和。漏掉这条的话，改 PAGES_TIMEOUT 不会有任何判据响应。
    pto = re.search(r"const PAGES_TIMEOUT = (\d+)", src)
    gho = re.search(r"const GH_TIMEOUT = (\d+)", src)
    worst_overall = max(worst, int(pto.group(1)), int(gho.group(1)))
    assert worst_overall < budget, \
        "最慢探针 %dms 逼近/超过 maxDuration %ds ⇒ 函数被杀，监控报的是『探活死了』" % (
            worst_overall, budget // 1000)
    assert worst < budget, \
        "reader 探活最坏耗时 %dms 逼近/超过 maxDuration %ds ⇒ 会被杀成假警" % (worst, budget // 1000)


def test_health_emb_probe_counts_from_the_fetched_body():
    """语义向量富度必须从**线上正文**数出来，且归零才算坏（P2）。

    `template.html` 的语义扩展分支写作 `DATA.some(d=>d.emb)`：SILICONFLOW 配额耗尽时
    `embed_star_entries` 保持原数组不变（不写 emb 键）⇒ 整段语义召回静默消失，页面照常 200、
    体积照常 1.2MB、构建照样绿。所以这一面只能数产物里的 `"emb":[` 条数，别的路子都看不见它。
    """
    src = _read(HEALTH)
    assert r'"emb":\[' in src, "emb 条数必须从正文正则数出来"
    assert "MIN_EMB_ENTRIES" in src, "必须有一条『确定还在』的水位线，否则 1 条也算健康"
    # 三态：归零 = false；0 < n < 水位 = null（早期规模本可能小，不许据此报坏）；≥ 水位 = true
    assert re.search(r"if \(n === 0\) emb = false", src), "emb 归零必须报坏——那正是静默降级的形状"
    assert re.search(r"n >= MIN_EMB_ENTRIES\) emb = true", src), "过水位才算好"
    assert "let emb = null;" in src, "中间地带必须是『不知道』，不能默认成好或坏"
    # 不许退回"只看 content-length 就返回"的快路径：那条路对体积正常但向量归零的首页是瞎的。
    # 只看 pagesProbe 的**代码行**——本文件注释里正合法地叙述着这段历史，
    # 全文搜字面量会把注释当成行为（本仓为这个坑写过 _code_only，这里是又一次用到）。
    probe = src[src.index("async function pagesProbe()"):src.index("async function readerChunksOk")]
    probe_code = "\n".join(l for l in probe.splitlines() if not l.strip().startswith("//"))
    assert "content-length" not in probe_code, \
        "首页探针必须真读正文；靠 content-length 短路就等于不数 emb（这一版最初就是这样）"


def test_health_probes_treat_network_failure_as_unknown_not_broken():
    """三个探针遇到『取不到』一律 null，遇到服务端明确的非 2xx 才算坏。

    形状来自 10-08 的实测：把首页改成"每次读正文"之后，本机一次慢网络直接跑出
    `http=503 / pages_ok=false / reader_ok=null / emb_ok=null`——那是我的通道慢，不是站点坏。
    假警比无警更糟：它下一步一定催生绕过开关。真停更不会因此漏掉：发布停下 ⇒ buildAge 越过 120 ⇒ 仍 503。
    """
    src = _read(HEALTH)
    assert re.search(r"catch \(e\) \{\n[^}]*?return \{ pages: null, emb: null \};", src), \
        "首页探针的 catch 必须给『不知道』，不能给 false"
    assert re.search(r"const ok = pagesOk !== false && readerOk !== false && embOk !== false", src), \
        "合成必须对三个信号都用『不为 false 才拉黑』，写成真值判断会把 null 报成坏"
    assert "if (!r.ok) return { pages: false, emb: null }" in src, \
        "服务端明确回了非 2xx 仍算坏（Pages 真挂了要响），这一半不许被三态化"


def test_health_unhealthy_is_503_for_uptime_monitors():
    src = _read(HEALTH)
    assert "STALE_BUILD_MINUTES" in src, "ok 必须有新鲜度阈值（连续缺席才算不健康）"
    # 「非 2xx 告警」要抓住静默退化，前提是 ok 决定 200/503——且两条返回路径
    # （缓存重放 + 新计算）都必须走同一三元式，只改一处另一处会漏
    ternaries = re.findall(r"(?:cache\.ok|ok)\s*\?\s*200\s*:\s*503", src)
    assert len(ternaries) >= 2, \
        "缓存重放与新计算两条路径都必须 ok?200:503（恒 200 会让探活抓不住静默退化）"


def test_health_has_response_cache():
    src = _read(HEALTH)
    assert re.search(r"TTL", src), "探活结果必须有短缓存（防监控轮询烧 GH 配额）"


def test_health_get_only():
    src = _read(HEALTH)
    assert re.search(r"method\s*!==?\s*'GET'|'OPTIONS'", src), "health 只读：GET/OPTIONS 之外必须拒绝"


# ---------- update.yml build 步 token ----------

def _build_step_text():
    yml = _read(UPDATE_YML)
    i = yml.find("Fetch stars & build")
    assert i != -1, "update.yml 必须有 Fetch stars & build 步"
    nxt = yml.find("- name:", i + 1)
    return yml[i:nxt if nxt != -1 else len(yml)]


def test_update_build_step_injects_github_token():
    step = _build_step_text()
    assert re.search(r"GITHUB_TOKEN:\s*\$\{\{\s*secrets\.GITHUB_TOKEN\s*\}\}", step), \
        "Fetch stars & build 必须注入 GITHUB_TOKEN（缺失 = 匿名 60 req/h）"


def test_update_build_step_keeps_id_and_run():
    step = _build_step_text()
    assert "id: build" in step, "build 步 id 不能动（后续步骤按 id 引用）"
    assert "python fetch_and_build.py $MODE" in step, "构建命令不能动"


# ---------- vercel.json ----------

def test_vercel_declares_health_max_duration():
    src = _read(VERCEL_JSON)
    m = re.search(r'"api/health\.js"\s*:\s*\{\s*"maxDuration"\s*:\s*(\d+)\s*\}', src)
    assert m, "health.js 必须在 vercel.json 声明 maxDuration"
    assert 10 <= int(m.group(1)) <= 60


# ---------- 翻译方向（T6 修正3）----------
# 线上实测（2026-10-04 00:2x）：search 拿 →中文 的链做 中→英，中文输入时 MyMemory
# langpair=zh-CN|zh-CN 返回错误文案 "PLEASE SELECT TWO DISTINCT LANGUAGES" 被当成功
# 译文缓存并组进 GitHub 查询串 ⇒ 全是无关仓库。链必须带方向，en 方向跳过硬编码
# "译成中文" 的 LLM 腿，MyMemory 必须拒收错误横幅。

TRANSLATE = os.path.join(ROOT, "api", "translate.js")


def test_translate_chain_takes_direction_param():
    src = _read(TRANSLATE)
    assert re.search(r"translateWithFallback\(\s*text\s*,\s*\{\s*to\s*=\s*'zh-CN'", src), \
        "统一链必须带 to 方向参数（默认 zh-CN 向后兼容）"
    assert "tl=zh-CN" not in src, "GTX 端点不得硬编码 tl=zh-CN（方向必须进 URL）"
    assert re.search(r"tl=\$\{to\}|tl=' \+ to", src), "GTX 的 target 必须用 to"
    assert "|${to}" in src or "|' + to" in src, "MyMemory langpair 的目标语言必须用 to"
    assert re.search(r"langpair=\$\{src\}\|\$\{to\}|langpair=\$\{src\}\|' \+ to", src)


def test_translate_cache_key_separates_direction():
    src = _read(TRANSLATE)
    assert re.search(r"\+\s*'\|'\s*\+\s*to|\`\|\$\{to\}", src), \
        "链内缓存键必须带方向维度——否则 en 译文会被 zh 请求命中（反向同理）"
    assert "zh-CN" in src, "默认方向必须是 zh-CN（rss/translate 既有行为零变化）"


def test_llm_legs_skipped_for_non_chinese_direction():
    src = _read(TRANSLATE)
    chain_start = src.find("async function translateWithFallback")
    assert chain_start != -1
    # 从链定义处起找【调用点】——find 全文会先命中文件前部的函数定义（锚错位的教训）
    agnes_leg = src.find("translateOne(text", chain_start)
    zen_leg = src.find("translateZen(text", chain_start)
    assert agnes_leg != -1 and zen_leg != -1, "链内必须有 Agnes/Zen 调用点"
    gate = "if (to === 'zh-CN')"
    assert src.rfind(gate, chain_start, agnes_leg) != -1, "Agnes 腿必须在 zh-CN 门内（prompt 硬编码译成中文）"
    assert src.rfind(gate, chain_start, zen_leg) != -1, "Zen 腿必须与 Agnes 同门"


def test_mymemory_rejects_error_banners():
    src = _read(TRANSLATE)
    # §8.17 教训：注释会喂假读数——先剥 // 注释行，横幅黑名单必须在【可执行代码】里
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
    assert "responseStatus" in code, "MyMemory 必须查 responseStatus（200 才算成功）"
    assert re.search(r"PLEASE SELECT", code), \
        "错误横幅黑名单必须含 PLEASE SELECT（zh|zh 同语言实测返回）"


def test_search_requests_english_direction():
    src = _read(os.path.join(ROOT, "api", "search.js"))
    assert re.search(r"translateWithFallback\(\s*term\s*,\s*\{\s*to:\s*'en'\s*\}\)", src), \
        "search 必须显式请求 en 方向（默认链是 →中文 的）"
