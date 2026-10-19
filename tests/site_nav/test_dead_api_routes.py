# -*- coding: utf-8 -*-
"""死路由判据：删掉恒为 0 的 /api/build_log，并保证删除本身不留尾巴。

为什么这个端点是死的（2026-10-01 实测，HANDOFF §10.17/§10.18）：
  `api/build_log.js:14` 读 `join(process.cwd(), 'build_logs')`，目录不存在就返回空；
  而 `build_logs/` 被 **两道**排除同时挡住 —— `.vercelignore:31` 与 update.yml 的 prune 步
  （`rm -rf ... build_logs docs`，在 `vercel --prod` 之前）⇒ 线上永远读不到数据：
  `?summary=1` 实测 `{"date":"2026-09-30","builds":0,...}`，无参实测 `{"total":0,"entries":[]}`；
  全仓前端（*.html / 生成脚本）**零引用**。
所以删函数 + 删 vercel.json 声明；日志本身照写（14 天轮转在跑，人要在 GitHub 上看）。

判据不只钉这一个文件，钉两条会长期防腐的不变量 —— 因为"删干净"最容易留下的正是这两种残渣：
  ① vercel.json 声明了某个 api 函数，但文件不存在（悬空声明：部署期报错或静默无效）
  ② 页面引用了某个 `/api/<名字>`，但 `api/<名字>.js` 不存在（用户点了才发现的死链）
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERCEL = os.path.join(ROOT, "vercel.json")
API_DIR = os.path.join(ROOT, "api")

# 已判定为死、且**禁止再被加回来**的路由：文件与 vercel.json 声明都不许出现。
# 删完不要把这里清空 —— 清空后这条判据就永不检查，等于自我注销（我差点这么写）。
# 只有当某天真的让端点可用（数据能读到 + 有前端调用方）时，才把它从这里移除。
DEAD_ROUTES = ["api/build_log.js"]

# 允许"没有页面调用方"的 api 函数。要往里加必须先写清谁在调它——
# 例如只被另一个函数内部调用的话，调用方应写在那个函数里，而不是豁免。
ALLOWED_UNCALLED = {
    # health：只读探活端点（T5，2026-10-04）——调用方是**外部** uptime 监控
    # （UptimeRobot 类：30 分钟 GET，非 2xx 告警），页面 JS 永远不打它。
    # 它存在的理由就是把"静默退化"变成主动报警，豁免"无页面调用方"检查；
    # 除此之外的死重量仍然红。
    "health",
}

# 认两种写法：同源 `'/api/x'`，以及**我们自己域名**的绝对写法 `'https://starhub-refresh.vercel.app/api/x'`。
# 只认同源是错的：站点的真调用（`_API_BASE`、translate、article 那几处）全是绝对写法，
# 2026-10-01 实测这样收紧后判据看到的引用集合是**空集**，等于一条恒绿的判据。
# 仍然不收外部 API（`https://aihot.example/api/v1/...` 这类），否则会红在一堆与本题无关的路径上。
API_REF_RE = re.compile(r"""(?:['"`(]|starhub-refresh\.vercel\.app)/api/([a-z0-9_-]+)""")


def _vercel_functions():
    return json.load(io.open(VERCEL, encoding="utf-8")).get("functions", {})


def test_dead_routes_are_really_gone():
    """死端点的函数文件与路由声明必须都不在。"""
    funcs = _vercel_functions()
    offenders = []
    for p in DEAD_ROUTES:
        if os.path.isfile(os.path.join(ROOT, p.replace("/", os.sep))):
            offenders.append("%s（文件还在）" % p)
        if p in funcs:
            offenders.append("%s（vercel.json 还声明着）" % p)
    assert not offenders, (
        "这些端点实测恒为 0 且零引用，却仍留在仓库/路由表里：%s" % ", ".join(offenders))


def test_no_dangling_vercel_function_declaration():
    """① vercel.json 声明的每个 api 函数都必须真的存在。"""
    missing = [k for k in _vercel_functions() if not os.path.isfile(os.path.join(ROOT, k.replace("/", os.sep)))]
    assert not missing, (
        "vercel.json 声明了不存在的函数（悬空声明）：%s" % ", ".join(missing))


def _generated_rss_html():
    """现生成的 RSS 页（同 test_artifact_js_parses 的口径：空数据只取静态代码部分）。"""
    sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
    sys.path.insert(0, ROOT)
    from _loader import load_build
    return load_build().build_html([], "2026-10-01 12:20", 0, 0, analysis_data=None,
                                   diverse_window_minutes=120, diverse_enabled=True)


def _api_refs_in_pages():
    """页面与其生成端里出现的 /api/<名字> 引用。

    `rss-aggregator.html` 必须用**现生成**的那份，不能读库里的副本：CI 的提交清单
    （update.yml 的 `git add rss-data-0.js known_categories.json …`）里**没有这些 HTML**
    ⇒ 仓库那份是某次冻结的旧运行时。2026-10-01 实测：线上产物有 `_wallWindow`/`WALL_SUMMARY_LIMIT`
    各 2 处，库里副本 0 处 —— 拿它当输入就是让判据读一份会说谎的地面真值。
    其余 HTML 读磁盘副本，并把 ROOT 下所有 *.html 都纳入，新增页面不会被硬编码名单漏掉。
    """
    refs = set()
    sources = []
    for fn in sorted(os.listdir(ROOT)):
        if not fn.endswith(".html"):
            continue
        if fn == "rss-aggregator.html":
            sources.append((fn + "（现生成）", _generated_rss_html()))
            continue
        sources.append((fn, io.open(os.path.join(ROOT, fn), encoding="utf-8").read()))
    for name in ("build_rss_aggregator.py", "fetch_and_build.py", "build_ai_daily.py"):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            sources.append((name, io.open(p, encoding="utf-8").read()))
    for name, text in sources:
        for m in API_REF_RE.finditer(text):
            refs.add((m.group(1), name))
    return refs


def test_pages_never_reference_a_missing_api():
    """② 页面/生成端引用的每个 /api/<名字> 都必须有对应函数文件。

    这条同时兜住"我这次删端点会不会留下引用"：删之前它要是红，就说明还有人在调。
    """
    have = {f[:-3] for f in os.listdir(API_DIR) if f.endswith(".js")} if os.path.isdir(API_DIR) else set()
    dangling = sorted({(r, src) for r, src in _api_refs_in_pages()
                       if r not in have and r not in ("", )})
    assert not dangling, (
        "这些 /api/ 引用找不到对应函数（死链，用户点了才 404）：%s" % dangling[:8])


def test_no_api_function_without_a_caller():
    """③ 反方向：`api/` 里每个函数都必须有人调用，否则就是每次部署都白带的死重量。

    为什么值得钉：`/api/build_log.js` 就是这一类 —— 文件、vercel.json 声明、部署产物三处都在，
    全仓却没有一个调用方，只有拿 curl 逐个打才会发现。判据把它变成"加进去就红"，
    而不是靠人记得去数。豁免表要写清原因，空豁免是常态。
    """
    callers = {r for r, _ in _api_refs_in_pages()}
    files = {f[:-3] for f in os.listdir(API_DIR) if f.endswith(".js")} if os.path.isdir(API_DIR) else set()
    orphans = sorted(files - callers - ALLOWED_UNCALLED)
    assert not orphans, (
        "这些 api 函数没有任何调用方（死重量，会被打进每次部署）：%s" % orphans)
