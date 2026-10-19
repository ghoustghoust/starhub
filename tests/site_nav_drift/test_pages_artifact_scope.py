# -*- coding: utf-8 -*-
"""A3（advisory）：Pages 制品里不许有非站点文件——在合成树上**真跑** Stage 正文。

为什么要真跑而不是 grep 文本：本仓反复栽在"读配置当读行为"（`git check-ignore` 不带 --no-index、
`ls-files` 不带 -z、门禁 B 恒红却报绿……都是口径与行为不一致）。制品面这条同理：
只有把 update.yml 里那段 shell 拿出来在合成目录跑一遍，数出来的"制品里有什么"才是它真正的行为。

现状（2026-10-02 实测）：Stage 步用 `git ls-files -z | grep -zv -E '(^|/)[._]'` 全量 rsync，
除了 `.`/`_` 前缀之外不设限 ⇒ tests/ 144 个、tools/ 26 个、根目录 .py、build_logs/ 14.3 MiB
都随站点公开（`curl` 现取：`build_logs/2026-10-02.jsonl` 线上 200 / 237 KB、`fetch_and_build.py` 200）。
用户批准的收窄范围＝tests/tools/根目录 .py/build_logs/**api/**（`api/` 于 2026-10-02 13:16 点单"关出去"）。
关 `api/` 不伤站点：页面里的调用全是绝对域名 `https://….vercel.app/api/x`（同源 `/api/x` 普查为 0），
而 Vercel 是从工作目录部署、不读 `_pages` —— 见 memory: vercel-pages-hosting-split（入口就是 Pages、Vercel 只当 API 主机）。

判据是双向的：既要求非站点件不在制品里，也要求站点自身的东西一个都不能少 ——
只关不留会把"收窄"做成"砍站点"。
"""
import fnmatch
import os
import re
import shutil
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")

# SITE = 站点运行时集合 = 白名单本身（两边不许各写一份，见 PUBLISH）。
# 18:12 现取核过：`known_categories.json` 从这份名单里删掉了 —— 它只被 build_daily_insight.py
# 在构建期读，4 个入口的正文里 0 引用；留着它会逼 Stage 把一个非运行时文件发上线。
SITE = ["index.html", "ai-daily.html", "rss-aggregator.html", "daily-insight-history.html",
        "rss-data-0.js", "rss-data-1.js", "hot_snapshot.json", "rss_sources.json",
        "trending_board.json"]
NON_SITE = ["build_rss_aggregator.py", "fetch_and_build.py", "insight_engine.py",
            "tests/rss_history/test_pages_deploy_wiring.py",
            "tools/data_api_push.py",
            "build_logs/2026-10-02.jsonl",
            "docs/insight-pipeline-flow.md",
            # 批 10 的名单比现实窄了一处：它挡了 docs/ 却没挡根级 .md。
            # 2026-10-02 15:26 现取：线上 HANDOFF.md 仍 200 / 84,323 B（其余内部件已 404）。
            "HANDOFF.md", "README.md", "CLAUDE.md", "REFRESH_VERIFICATION_REPORT.md",
            # 15:58 用自测过的解析器在旧场清单里抓出的同类漏项（三个文件里 rel_time.js 是构建期
            # 内联，另两个由 api/rss.js 在 Vercel 侧 require ⇒ 浏览器从不请求 lib/）。
            "lib/rel_time.js", "lib/rss_cover.js", "lib/rss_retention.js",
            # ↓ 18:12 **全量枚举**（远端 250 个跟踪 blob 按现过滤跑一遍）才看清的东西：
            # 排除式名单会漏掉一切"没想到的类型"，而这些恰恰都真在仓库根里。
            "vercel.json", "package.json", "LICENSE", "build_config.json",
            "known_categories.json", "ai_daily.json", "predictions.jsonl",
            "vendor_qrcode.min.js", "failed-run-364-final.png",
            "daily-deep-2026-09-25.json"]

# 站点运行时真正需要的全集（18:12 现取：4 个入口的同源请求只有这些；
# qrcode 走 jsDelivr/unpkg 绝对 URL，starhub-share.png 是 a.download 的文件名不是请求）。
PUBLISH = {"index.html", "ai-daily.html", "rss-aggregator.html", "daily-insight-history.html",
           "hot_snapshot.json", "rss_sources.json", "trending_board.json"}

API_SOURCES = ["api/agihunt.js", "api/article.js", "api/events.js", "api/news.js",
               "api/refresh.js", "api/rss.js", "api/search.js", "api/translate.js"]


def _stage_body():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            name = step.get("name", "")
            if name.startswith("Stage Pages site") and step.get("run"):
                return step["run"]
    raise AssertionError("找不到 Stage Pages site 步骤（workflow 改名了？判据要跟着改）")


def _tree(tmp):
    """造一个能跑的合成仓：站点件 + 非站点件 + 生成端会补的新鲜产物。"""
    subprocess.run(["git", "init", "-q", "."], cwd=tmp, check=True)
    for k, v in (("user.name", "gate"), ("user.email", "gate@example.invalid"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=tmp, check=True)
    for rel in SITE + NON_SITE + API_SOURCES:
        p = os.path.join(tmp, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x" * 64)
    subprocess.run(["git", "add", "-f", "--", "."], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=tmp, check=True)
    return tmp


def _needs_rsync(body):
    """正文是否**真的调用** rsync（只看非注释行）。

    第一版直接 "rsync" in body —— 结果被自己新写的注释（"换成 cp 而不是继续 rsync"）骗过去，
    两条主判据在收窄已经落地之后仍然报 skip。判据读的是行为，不能读关键词。
    """
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if re.search(r"\brsync\b", s):
            return True
    return False


def _run_stage(tmp):
    body = _stage_body()
    if _needs_rsync(body) and shutil.which("rsync") is None:
        pytest.skip("正文还在调用 rsync 而这台机器没有 rsync ⇒ 这条只能到 CI 验；"
                    "不能把'工具缺失'读成'没有泄漏'")
    r = subprocess.run(["bash", "-e", "-c", body], cwd=tmp, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 0, "Stage 正文在合成树上就没跑通：%s\n%s" % (
        r.stdout[-400:], r.stderr[-400:])
    out = []
    for base, _dirs, files in os.walk(os.path.join(tmp, "_pages")):
        for f in files:
            out.append(os.path.relpath(os.path.join(base, f), os.path.join(tmp, "_pages"))
                       .replace(os.sep, "/"))
    return set(out)


@pytest.fixture()
def staged(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("没有 git，无法造合成仓")
    return _run_stage(_tree(str(tmp_path)))


def test_published_set_is_exactly_the_allowlist(staged):
    """白名单式收口：制品里的东西必须**正好**等于站点需要的集合，多一项少一项都算红。

    为什么翻成白名单（18:12 全量枚举的证据）：排除式名单跑出来的公开集是 15 项／0.48 MiB，
    里面是 4 张调试截图、两个过期 daily-deep-*.json、build_config.json、predictions.jsonl、
    vercel.json/package.json/LICENSE/known_categories.json/vendor_qrcode.min.js ——
    每一类都要我"先想到才能挡"。集合相等这条把默认方向反过来：**新垃圾默认不公开**，
    要公开就得改白名单（改的时候这条判据会逼你看清自己在加什么）。
    """
    chunks = {p for p in staged if re.fullmatch(r"rss-data-\d+\.js", p)}
    extra = sorted(staged - PUBLISH - chunks)
    missing = sorted(PUBLISH - staged)
    assert not extra, "这些不是站点运行时需要的东西，却进了 Pages 制品：%s" % extra[:10]
    assert not missing, "白名单里的站点件没进制品 ⇒ 首页或某个入口会空：%s" % missing
    assert chunks, "rss-data-*.js 一块都没进制品 ⇒ 卡片墙没数据，白名单实现走偏了"


def test_non_site_files_are_not_published(staged):
    leaks = sorted(p for p in staged
                   if p.endswith((".py", ".md"))
                   or p.startswith(("tests/", "tools/", "build_logs/", "docs/", "api/", "lib/")))
    assert not leaks, "这些非站点文件随制品公开了：%s" % leaks[:8]


def test_site_files_are_still_published(staged):
    missing = [p for p in SITE if p not in staged]
    assert not missing, "收窄把站点自己的文件也砍掉了：%s" % missing
    assert any(p.startswith("rss-data-") for p in staged), "分块没进制品，首页会没数据"


def test_needs_rsync_reads_invocations_not_comments():
    """上面那个启发式自己的判据：注释里提 rsync 不许触发 skip（我第一版就是这么被骗的）。"""
    assert _needs_rsync("git ls-files -z | rsync -a --files-from=- ./ _pages/\n") is True
    assert _needs_rsync("# 换成 cp 而不是继续 rsync\ncp -f x _pages/\n") is False
    assert _needs_rsync("echo rsyncless\n") is False


def test_the_probe_bites_on_a_blanket_copy(tmp_path):
    """反空转（也是实现前的红）：把正文换成"跟踪文件整拷"，同一套断言必须判红。

    少了这条，`test_non_site_files_are_not_published` 有可能只是因为合成树里根本没建那些文件
    而"绿"——那是空集自证。这里用 `cp --parents` 而不是 rsync，好让没有 rsync 的机器也能验。
    """
    d = _tree(str(tmp_path))
    body = ("mkdir -p _pages\n"
            "git ls-files -z | grep -zv -E '(^|/)[._]' | xargs -0 -r cp --parents -t _pages/\n")
    r = subprocess.run(["bash", "-e", "-c", body], cwd=d, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 0, "对照组正文自己就没跑通：%s" % r.stderr[-200:]
    got = set()
    for base, _dirs, files in os.walk(os.path.join(d, "_pages")):
        for f in files:
            got.add(os.path.relpath(os.path.join(base, f), os.path.join(d, "_pages"))
                    .replace(os.sep, "/"))
    leaks = sorted(p for p in got
                   if p.endswith((".py", ".md"))
                   or p.startswith(("tests/", "tools/", "build_logs/", "api/", "lib/")))
    assert leaks, "对照组居然没泄漏 ⇒ 合成树没建非站点文件，主判据是在空集上跑"
    assert {"build_rss_aggregator.py", "tools/data_api_push.py",
            "build_logs/2026-10-02.jsonl", "api/rss.js", "HANDOFF.md",
            "lib/rss_cover.js"} <= set(leaks), leaks


# ---- 批 11 的前提：`api/` 已关出 Pages 制品 ⇒ 站点从此不许用同源写法调它 ----
# 页面里那些调用今天全是绝对域名（2026-10-02 现取：4 个入口共 13 处引用，同源 0 处）。
# 这条把"前提"钉成判据：将来谁改成 `fetch('/api/rss')`，Pages 域上就是 404，
# 而站点功能会静默坏掉 —— 制品面那条判据本身看不见这件事。
CLIENT_SOURCES = ["template.html", "build_rss_aggregator.py", "build_ai_daily.py", "build_daily_insight.py"]
# 四个已发布页面各自的生成端。名单要覆盖全部四个而不是只抽两个：白名单化（cee84b1666）之后
# Pages 只发那 6 个名字 + 分块，任何一个入口写出白名单外的请求都是同一类坏。
# 现取（本次改动前）：build_ai_daily.py / build_daily_insight.py 里的 `/api/v1`、`/api/query`
# 全是 aihot / algolia / arxiv / openrouter 的绝对 URL ⇒ 分类器两类都不计，扩名单不判红。
PUBLISHED_PAGES = {"template.html": "index.html", "build_rss_aggregator.py": "rss-aggregator.html",
                   "build_ai_daily.py": "ai-daily.html", "build_daily_insight.py": "daily-insight-history.html"}


def _client_sources():
    """被测输入必须真在检出集合里取到（这四份都被跟踪）。变异电池用 env 换成合成副本。"""
    override = os.environ.get("STARHUB_CLIENT_SOURCES")
    if override:
        return override.split(os.pathsep)
    missing = [p for p in CLIENT_SOURCES if not os.path.exists(os.path.join(ROOT, p))]
    assert not missing, "生成端清单里有取不到的文件：%s ⇒ 判据会在缺件上空跑" % missing
    return [os.path.join(ROOT, p) for p in CLIENT_SOURCES]


def _api_refs(text):
    """把 `/api/<name>` 引用分成三类：绝对（返回名列表）、同源（返回名列表）、其余忽略。

    只认确实是调用的形状：紧贴 `/api/` 之前是**我们自己 API 域名**的尾巴 ⇒ 绝对；是引号/反引号/左括号 ⇒ 同源。
    外部域名（newsnow / aihot 那类）与制品无关，一律不计；注释与散文里的"供 /api/rss 直接返回"前面是空格 ⇒
    两类都不算（判据按关键词读正文会被注释喂假读数，本仓 2026-10-02 栽过一次）。
    模板插值 `${host}/api/x` 静态读不出宿主，故也不在两列之内。
    """
    absolute, relative = [], []
    for line in text.splitlines():
        for m in re.finditer(r"/api/([A-Za-z0-9_-]+)", line):
            tail = line[:m.start()]
            if re.search(r"https?://starhub-refresh\.vercel\.app(?:/[A-Za-z0-9._\-]*)*$", tail):
                absolute.append(m.group(1))
            elif tail and tail[-1] in "\"'`(":
                relative.append(m.group(1))
    return absolute, relative


def test_api_ref_classifier_sees_both_forms():
    """分类器自己的双向控制：漏认同源就是漏掉真回归，误伤注释就是假红。"""
    assert _api_refs("var x='https://starhub-refresh.vercel.app/api/rss';") == (["rss"], [])
    assert _api_refs("const y = '/api/translate';") == ([], ["translate"])
    assert _api_refs('fetch(`"/api/news"`)') == ([], ["news"])
    assert _api_refs("('/api/search')") == ([], ["search"])
    assert _api_refs("# 供 /api/rss 直接返回，避免实时抓取") == ([], [])
    assert _api_refs('NEWSNOW = "https://newsnow.busiyi.world/api/s?id=%s"') == ([], [])


def test_client_sources_make_no_same_origin_api_calls():
    """`api/` 关出制品之后，页面里出现同源 `/api/x` 调用就是 Pages 域 404。"""
    srcs = _client_sources()
    abs_names, offenders = [], []
    for p in srcs:
        a, r = _api_refs(open(p, encoding="utf-8", errors="replace").read())
        abs_names += a
        offenders += ["%s: /api/%s" % (os.path.basename(p), n) for n in r]
    assert not offenders, (
        "api/ 已不在 Pages 制品里（批 11），同源调用只会在 Pages 域 404：%s" % offenders[:6])
    assert len(set(abs_names)) >= 4, (
        "只认出 %s 个绝对引用 ⇒ 分类器没读到真调用，上面那条「不报错」是空集自证" % sorted(set(abs_names)))


# ---- 浏览器请求的每个名字都必须发得出去（白名单化之后新开的接缝，cee84b1666）----
# `test_published_set_is_exactly_the_allowlist` 比的是"两份手抄名单相等"：页面真去请求一个
# 名单外的名字时，那两项会**一起缺**，相等照样成立 ⇒ 它天生看不见这类坏。
# 这里的对账对象是**跑出来的制品**（合成树上真跑 Stage 正文的产物清单），不是又一份手抄。
_NAME = r"[A-Za-z0-9_.\-/]*[A-Za-z0-9_.\-]+\.(?:html|js|json|css|png|jpg|jpeg|svg|ico|webp|woff2?|map)"
# 语境要按"浏览器会发请求的那几种写法"来限定，不是把所有引号里的文件名都算上：
# 生成端有大量 `open("x.json","w")` 这类构建期写法，全算就是把 批 3 刚清掉的东西又请回制品。
# 尚**未**覆盖的形状（现取四份生成端零命中，将来出现要在这里加语境）：`import('x.js')`、
# `new Worker('x.js')`、`xhr.open('GET','x.json')`、`navigator.sendBeacon('x')`。
_REF_RULES = [
    # <a href="x.html"> / <link rel="preload" href="x.json"> / sc.src='x.js'
    # `\\?` 是给生成端留的：JS 写在 Python 字符串里时引号常被转义（A2 的第一版没吃反斜杠，
    # hot_snapshot.json 就这么漏过去一次）。
    (re.compile(r"""(?:src|href)\s*=\s*\\?["'](%s)\\?["']""" % _NAME), lambda m: m.group(1)),
    # fetch('x.json') / fetch("sub/x.json") ⇒ 保留相对路径去和制品对账。
    # 绝对 URL 含 `:`，落不进 _NAME ⇒ aihot / algolia / arxiv / newsnow 这些外部域名不会被误当同源。
    (re.compile(r"""fetch\(\s*\\?["'](%s)\\?["']""" % _NAME), lambda m: m.group(1)),
    # CSS `url(x.css)` / `url("assets/app.js")`：样式与字体也走同一只手套（白名单外一样 404）。
    (re.compile(r"""url\(\s*['"]?(%s)['"]?""" % _NAME), lambda m: m.group(1)),
    # 'rss-data-' + i + '.js?v=' ⇒ 块号是运行期定的，静态抽不出具体文件名，
    # 归一成 rss-data-*.js 去和制品对账（fnmatch 的**方向**是"制品名 匹配 引用模式"）。
    (re.compile(r"""["']([A-Za-z0-9_.\-]*)["']\s*\+\s*[A-Za-z0-9_.\[\]()]+?\s*\+\s*"""
                r"""["']\.(js|json)(\?[^"']*)?["']"""),
     lambda m: m.group(1) + "*." + m.group(2)),
]


def _strip_comments(text):
    """剥掉注释再抽 —— 判据按关键词读正文会被注释喂假读数（本仓在 `_needs_rsync` 上栽过一次，
    见 test_needs_rsync_reads_invocations_not_comments）。今天四个生成端的注释里没有任何
    `href/src/fetch` 形状（实测含注释与去注释抽出的是同一批 6 个名字），所以这条不改判读数，
    只是把"不改"从巧合变成性质：将来谁在文档注释里写个 `<a href="demo.json">` 举例，
    不该让 A3 判红。
    """
    t = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    keep = []
    for line in t.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("#") or s.startswith("*"):
            continue
        keep.append(line)
    return "\n".join(keep)


def _browser_refs(text):
    got = set()
    for rx, to_name in _REF_RULES:
        for m in rx.finditer(_strip_comments(text)):
            got.add(to_name(m))
    return got


def test_browser_ref_extractor_sees_every_request_shape():
    """抽取器自己的双向控制：五种真形状都要认，三类假形状都不许认。

    少了反向那半，"页面没请求任何名单外文件"这句绿话可能只是抽取器失明的另一种说法
    （本仓在 _needs_rsync 上被注释骗过一次，判据 test_needs_rsync_reads_invocations_not_comments 就是那次的账）。
    """
    assert _browser_refs('<a href="rss-aggregator.html">AI</a>') == {"rss-aggregator.html"}
    assert _browser_refs('<link rel="preload" href="hot_snapshot.json" as="fetch">') == {"hot_snapshot.json"}
    assert _browser_refs("const r = await fetch('daily-deep-2026-10-02.json')") == {"daily-deep-2026-10-02.json"}
    assert _browser_refs("sc.src='rss-data-'+i+'.js?v='+BUILD_TS;") == {"rss-data-*.js"}
    assert _browser_refs("background:url(lib/cover.css)") == {"lib/cover.css"}
    # 生成端把 JS 塞进 Python 字符串时引号是转义的，这条必须照样认
    assert _browser_refs(r"""fetch(\'hot_snapshot.json\')""") == {"hot_snapshot.json"}
    # 反向：外部域名 / data URI / 构建期文件名 / 注释里的例子都不是浏览器请求
    assert _browser_refs('fetch("https://aihot.virxact.com/api/v1/items")') == set()
    assert _browser_refs('background:url("data:image/png;base64,iVBOR")') == set()
    assert _browser_refs('json.dump(d, open("known_categories.json", "w"))') == set()
    assert _browser_refs('OUT = "ai-daily.html"') == set()
    # 注释剥掉的**那半**也要有靶：同一行内容，带注释前缀不算、不带就算
    assert _browser_refs('// 举例：<a href="demo.json">demo</a>') == set()
    assert _browser_refs('<a href="demo.json">demo</a>') == {"demo.json"}
    assert _browser_refs('/* <link href="demo.css"> */') == set()
    assert _browser_refs('# 形如 href="demo.html" 的写法') == set()



def test_every_browser_referenced_name_is_published(staged):
    refs = set()
    for p in _client_sources():
        refs |= _browser_refs(open(p, encoding="utf-8", errors="replace").read())
    assert refs, "四个生成端一个浏览器引用都没抽到 ⇒ 抽取器失明，这条判据在空集合上跑"
    known = {"ai-daily.html", "rss-aggregator.html", "hot_snapshot.json", "rss-data-*.js"}
    assert known <= refs, (
        "抽取结果 %s 少了 %s —— 模式退化（这四个是实测在页面里请求的名字，"
        "rss-data 那份是 'rss-data-'+i+'.js' 的拼接形状，最容易整条丢掉）"
        % (sorted(refs), sorted(known - refs)))
    naked = sorted(n for n in refs if not any(fnmatch.fnmatch(f, n) for f in staged))
    assert not naked, (
        "浏览器会请求、制品里却没有：%s ⇒ Pages 域上就是 404（白名单外不发）" % naked)

