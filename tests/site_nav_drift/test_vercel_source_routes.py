# -*- coding: utf-8 -*-
"""A3：Vercel 域不许把函数与 lib 的**源码**当静态资源发出去（routes 必须精确 404 源码路径）。

现取读数（2026-10-04 08:0x BJT，直接 curl 生产域）：
    /api/rss.js            200  132,011 B   ← 函数源码明文，连中文注释都在
    /lib/rel_time.js       200    4,271 B
    /lib/rss_cover.js      200    4,576 B
    /lib/rss_retention.js  200    5,313 B
    /package.json          404             ← #33 那批关掉的
    Pages 域 /lib/…        404             ← 白名单关掉的
⇒ 站点侧（Pages）与 package.json 都收口了，**只剩 Vercel 这一侧的函数/lib 源码还在公开**。
`.vercelignore` 关不掉它们：`api/*.js` 是函数本体、`lib/*.js` 被 `api/rss.js` 运行时 require，
排掉就是打挂 API ⇒ 唯一出路是 `vercel.json` 的 routes 把这些**路径**判 404。

为什么是 routes 而不是别的（官方文档要点，2026-08-14 版 vercel-json 页）：
- `routes` 数组**按定义顺序**处理；每条的 `src` 是 PCRE 正则，匹配的是不含 query 的 pathname；
- `status` 可以不带 `dest` 直接回码（官方示例里就有 `{ "src": "/legacy", "status": 404 }`）；
- `continue` 缺省为 false ⇒ 命中即终止路由，正是"这条路径不给看"的语义；
- 示例中 routes 与函数共存（`{ "src": "/api", "dest": "/my-api.js" }`），**未匹配的路径继续走默认流水线**
  ⇒ 所以只要不写 catch-all，函数调用与页面都不受影响。

⇒ 因此本文件的两半同等重要：正向钉"源码路径必须被 404"，**反向钉"函数调用与页面路径一条都不许被吞"**。
后者才是真风险：写一条 `{src:"/.*"}` 就能让所有路径 404，判据必须把它挡下来，
因为那会直接打挂生产 API（用户可见），不是"验不过再回退"那么轻。
"""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG = os.environ.get("STARHUB_VERCEL_JSON") or os.path.join(ROOT, "vercel.json")

# 已知必须存在的函数（远端 api/ 实际文件；本地可能多一个正在开发的 health.js ⇒ 只要求 ⊇）
KNOWN_FUNCTIONS = ("api/refresh.js", "api/search.js", "api/events.js", "api/news.js",
                   "api/rss.js", "api/article.js", "api/agihunt.js", "api/translate.js")

# 这些路径**必须**被 404（源码直取）
MUST_BLOCK = ("/lib/rel_time.js", "/lib/rss_cover.js", "/lib/rss_retention.js",
              "/api/rss.js", "/api/translate.js", "/api/search.js")

# 这些路径**绝不允许**被任何一条 route 命中（函数调用与页面，用户可见）
MUST_NOT_BLOCK = ("/api/rss", "/api/news", "/api/search", "/api/translate", "/api/refresh",
                  "/index.html", "/ai-daily.html", "/rss-aggregator.html",
                  "/daily-insight-history.html", "/rss-data-0.js", "/hot_snapshot.json",
                  "/rss_sources.json")


def _cfg():
    return json.load(open(CFG, encoding="utf-8"))


def _routes():
    r = _cfg().get("routes")
    assert isinstance(r, list) and r, (
        "vercel.json 里没有非空 routes ⇒ `/api/*.js` 与 `/lib/*.js` 的源码仍能被 HTTP 直取"
        "（现取 /api/rss.js = 200 / 132,011 B）")
    return r


def _hits(route, path):
    """按 Vercel 语义判断一条 route 是否命中该 pathname：src 是全路径 PCRE 匹配。"""
    src = route.get("src") or route.get("source")
    if not src:
        return False
    if re.fullmatch(src, path) is None and re.match(src, path) is None:
        return False
    # continue: true 表示命中后仍继续路由 ⇒ 单独一条 status 404 不生效
    return not route.get("continue", False)


def _blocker(path):
    for r in _routes():
        if _hits(r, path) and r.get("status") == 404:
            return r
    return None


def test_source_paths_are_blocked_with_404():
    for p in MUST_BLOCK:
        assert _blocker(p), (
            "%s 没有被任何一条 `status:404` 的 route 命中 ⇒ 源码仍可公开直取。"
            "（`.vercelignore` 排不掉它们：api/*.js 是函数本体、lib/*.js 被 api/rss.js 运行时 require）"
            % p)


def test_no_route_may_swallow_functions_or_pages():
    """反向（与正向同权重）：任何一条 route 都不许命中函数调用或页面路径。

    这是最危险的一种"看起来修好了"：加一条 `{src:"/.*"}` 能让上面那条判据全绿，
    同时把 /api/rss 与四个页面一起打成 404。
    """
    rs = _routes()
    for p in MUST_NOT_BLOCK:
        for r in rs:
            assert not _hits(r, p), (
                    "route %r 命中了 %s ⇒ 会把用户可见的接口/页面一起 404 掉。"
                    "只允许精确匹配源码路径（/lib/… 与 /api/….js），不许写 catch-all" % (r, p))


def test_routes_do_not_carry_catch_all():
    """直接挡掉 catch-all 与 handle:filesystem 这类越界写法。"""
    for r in _routes():
        src = (r.get("src") or r.get("source") or "").strip()
        assert src not in ("/.*", "/(.*)", ".*", "/"), "出现 catch-all route %r ⇒ 会吞掉全站" % src
        assert "handle" not in r, "用了已废弃的 handle（%r）⇒ 会改写整个路由阶段" % r


def test_functions_block_survives_the_routes_edit():
    """改 routes 不许顺手把 functions 段弄丢——8 个 maxDuration 是各函数超时上限的依据。"""
    fn = _cfg().get("functions") or {}
    for k in KNOWN_FUNCTIONS:
        assert k in fn, "functions 段少了 %s（实得 %s）⇒ 那个函数会退回默认超时" % (k, sorted(fn))
    assert "name" not in _cfg() or isinstance(_cfg()["name"], str), "name 字段被改成非字符串"


def test_json_stays_valid_and_routes_come_last_in_the_file():
    """routes 放在数组末尾之外还要保证 JSON 能解析（CI 里 Vercel 读不到就直接部署失败）。"""
    raw = open(CFG, encoding="utf-8").read()
    doc = json.loads(raw)                      # 解析失败 = 生产部署当场挂
    assert "routes" in doc
    assert list(doc)[-1] == "routes", (
        "routes 建议放在末键（实得键序 %s）——纯可读性：functions 段在前，改超时的人先看到它，"
        "不至于在数组中间找路由规则" % list(doc))
