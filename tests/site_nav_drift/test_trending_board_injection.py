# -*- coding: utf-8 -*-
"""排行榜注入化判据：让 TRENDING 有一个自己的数据出口，快车道重建整页就再也伤不到它。

为什么只做排行榜、不做关注动态（2026-10-04 复核，修正了我上一轮的诊断范围）：
`FEED` 早就有运行时路径——`refreshFeed()` 拉 `/api/events`，成功替换、失败静默保留内联，
所以内联那份被覆盖成 [] 并不会真的空；而 `TRENDING` 全文只有"内联 + 三处渲染读取"，
**没有任何运行时路径** ⇒ 它才是唯一被快车道覆盖坏掉的一栏（外加 AI 摘要那句也是内联）。

四条判据，第二条和第三条是一对（既要能刷新，又不许把首屏兜底删了）：
① 小时场把**已经算好的**榜单与摘要写成站点件，不重算 ⇒ 零 GitHub 配额；
② 页面有 fetch 该件的注入路径，且失败时退回内联值；
③ 反向：`__TRENDING__` 内联占位符必须还在——删了它首屏就变空，观感退步，
   这类"为了架构干净牺牲首屏"的取舍没经过用户同意；
④ 该件必须同时出现在 update.yml 的 save path 清单与 star-fast.yml 的 restore path 清单，
   只有一边就等于跨场传不到（这个不对称本仓犯过一次，判据是事后补的）。
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOARD = "trending_board.json"
TPL = os.path.join(ROOT, "template.html")
FAB = os.path.join(ROOT, "fetch_and_build.py")
WF_UPDATE = os.path.join(ROOT, ".github", "workflows", "update.yml")
WF_FAST = os.path.join(ROOT, ".github", "workflows", "star-fast.yml")


def _t(p):
    return io.open(p, encoding="utf-8").read()


def test_hourly_build_emits_trending_board():
    """小时场要把榜单 + AI 摘要落成一个站点件（数据是 build_trending 现算好的，不重算）。"""
    src = _t(FAB)
    assert BOARD in src, "主链没写 %s ⇒ 前端没东西可注入" % BOARD
    fn = _t(os.path.join(ROOT, "fetch_and_build.py"))
    i = fn.find("def trending_board_payload")
    assert i >= 0, "缺 trending_board_payload()：出口的形状要有单一出处，不能两处各拼一份"


def test_page_fetches_board_and_falls_back_to_inline():
    """注入路径 + 失败退回内联（与 refreshFeed 同构）。"""
    tpl = _t(TPL)
    assert BOARD in tpl, "页面里没有 %s 的 fetch ⇒ 排行榜仍只靠内联，覆盖问题没根治" % BOARD
    i = tpl.find(BOARD)
    window = tpl[max(0, i - 700):i + 900]
    assert "catch" in window or "if(!" in window or "r.ok" in window, \
        "取不到数据时必须退回内联兜底，不许把榜单清空"


def test_inline_trending_fallback_is_not_removed():
    """反向那半：内联占位符不许被删。删了它，首屏在 JSON 回来之前是空的。"""
    tpl = _t(TPL)
    assert "__TRENDING__" in tpl, \
        "__TRENDING__ 内联兜底被删了：注入是加一层，不是换掉首屏（这个取舍没经用户同意）"
    # 只钉"内联赋值还在"，不钉 const/let——注入要能替换它就得是 let（FEED 同理）。
    assert "TRENDING = __TRENDING__" in tpl, "内联赋值语句本身要在"


def _publish_list_of(txt, step_name):
    """取某个 Stage 步里那份 `for f in ... do` 清单文本。

    为什么不裸搜文件名：`trending_board.json` 在 star-fast.yml 里**早就出现过**（curl 那一行），
    但它没进发布清单 ⇒ 裸搜判据一路绿灯，而 Pages 是整棵制品替换，快车道每真发布一次就把
    出口件从线上抹掉（2026-10-05 01:45 真发布后现网 404，实测）。
    """
    i = txt.find(step_name)
    assert i >= 0, "找不到步骤 %r：判据的锚点先失效了" % step_name
    m = re.search(r"for f in (.*?)\bdo\b", txt[i:], re.S)
    return m.group(1) if m else ""


def test_board_is_published_not_just_cached():
    """它是**站点件**：浏览器同源 fetch ⇒ 必须进**每一条**会写 Pages 制品的清单。

    Pages 部署是整棵替换而不是增量：哪条清单漏了它，那场发布就把这个文件从线上抹掉。
    小时场和快车道各有一份自己的清单，只钉一边就会漏另一边（我上一轮就只钉了小时场侧）。
    """
    # 只剩一条清单要钉了：2026-10-07 P0 摘掉快车道的 Pages 发布权，它那份"带回清单"随
    # Stage 步一起消失。上一版这里遍历两条——因为 10-05 事故就是"只钉了小时场侧、漏了
    # 快车道侧"复发的。现在站点唯一写者是 update.yml ⇒ 钉一条就是钉全部；快车道侧由
    # test_star_fast_wiring.py::test_fast_lane_has_no_publish_authority 反向钉住。
    for name, path, step in (("update.yml 的 Stage 清单", WF_UPDATE, "Stage Pages site"),):
        lst = _publish_list_of(_t(path), step)
        assert lst, "%s 里没抓到 for-f-in 清单 ⇒ 这条判据在空转" % name
        assert BOARD in lst, \
            "%s 不含 %s ⇒ 那场真发布会把出口件从 Pages 上抹掉（浏览器 fetch 404）" % (name, BOARD)
    for name, path in (("A2 STAGE_ALLOWLIST", os.path.join(ROOT, "tests", "rss_history", "test_pages_deploy_wiring.py")),
                       ("A3 PUBLISH", os.path.join(ROOT, "tests", "site_nav_drift", "test_pages_artifact_scope.py"))):
        assert BOARD in _t(path), "%s 里没有 %s ⇒ 前端 fetch 会 404" % (name, BOARD)


def test_fast_lane_touches_no_page_or_board_at_all():
    """反向：快车道既不产页面，也不再需要把排行榜取回来。

    旧版这条叫 test_fast_lane_reuses_the_same_board_file_as_inline_value，钉的是"快车道的
    内联兜底必须与浏览器读同一份出口件"——它的前提（快车道产 HTML）已随 2026-10-07 P0 消失。
    为什么翻成反向而不是直接删：回填这件事本身就是单点（10-04 拆 starhub-sidebar 缓存族时
    写过的同一条理由——"取不到 ⇒ 整页不发布"），只有在本车道根本不产页面之后它才无意义。
    留着这条反向判据，是为了让"顺手把 curl 加回去"当场红，而不是等下一轮再靠人想起来。
    """
    fr = _code_only(_t(os.path.join(ROOT, "fast_refresh.py")))
    fast = _code_only(_t(WF_FAST))
    assert BOARD not in fr and "TRENDING_BOARD" not in fr, \
        "快车道仍在读 %s ⇒ 它又被当成 HTML 的生产者了" % BOARD
    assert BOARD not in fast, "star-fast.yml 里还留着 %s 的 curl 带回" % BOARD
    assert "build_index_html" not in fr, "fast_refresh.py 里还有 HTML 生成调用"


def _code_only(txt):
    """剔掉注释行——判据要读行为，不能读关键词。

    本仓第三次踩这个坑了：`_needs_rsync` 被注释里那句"换成 cp 而不是继续 rsync"骗过，
    这次又被我自己写的"这里原本是 starhub-sidebar 族"骗成"没拆干净"。
    """
    return "\n".join(l for l in txt.splitlines() if not l.strip().startswith("#"))


def test_sidebar_snapshot_transition_is_gone():
    """防回潮：过渡态整条链必须消失（只看代码行，注释里讲历史不算残留）。"""
    for name, path in (("star-fast.yml", WF_FAST), ("update.yml", WF_UPDATE),
                       ("fetch_and_build.py", FAB),
                       ("fast_refresh.py", os.path.join(ROOT, "fast_refresh.py"))):
        txt = _code_only(_t(path))
        assert "sidebar_snapshot" not in txt, "%s 里还留着 sidebar_snapshot（过渡态没拆干净）" % name
        assert "starhub-sidebar" not in txt, "%s 里还留着 starhub-sidebar 缓存族" % name
        # 中文名也要钉：前两条只搜英文字面，结果 fast_refresh 拆掉之后 star-fast.yml 的
        # ::notice:: 还在写"或有新星但缺侧栏快照"，判据全绿而日志在讲一个已经不存在的分支
        # ⇒ 读日志的人（包括下一轮的 agent）会以为"有新星也可能不发页面"。
        assert "侧栏快照" not in txt, "%s 的可见内容还写着已删掉的『侧栏快照』分支" % name
    assert not os.path.exists(os.path.join(ROOT, "tests", "site_nav_drift",
                                           "test_sidebar_snapshot_wiring.py")), \
        "旧过渡态判据还在 A3 里，会跟着已删的机制一起变成噪音"
