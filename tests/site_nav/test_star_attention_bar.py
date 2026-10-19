# -*- coding: utf-8 -*-
"""star 提醒条判据：表里有条目而收藏里没有 ⇒ 报「旧名待人工判定」，绝不报成「收藏丢了」。

来由（2026-10-04 实测）：`known_categories.json` 有 304 个键，而 GitHub 公开 starred 全集与
线上 DATA 都是 293 条、**双向零差** ⇒ 多出的 11 个名字是旧名残留，不是内容缺失：
8 条仓库改名/转移后真身 id 仍在收藏里（`Accio-org/RealReplicaBench`→`CommerceAgentBench` 等）、
2 条旧路径已 404（`Sliverkiss/workbuddy2api`、`Vincentwei1021/video-shotcraft`）、1 条未定案。
用户一句「同名仓库不等于就是一样的仓库」把口径钉死：**判同一性只能用 repository id，名字只能当线索**。

所以这块的本领是把这件事变成页面上可见、可点开的待办，同时不越两界：
  界一 不自动定性 —— 不在 CI 里逐场探测 301/404（那要再存一份结论，等于新造状态）；
  界二 不表述成收藏丢失 —— 把「名字过期」渲染成「数据没了」是假警，比无警更糟。
另外三条是防我自己犯老毛病：容器不许在无待办时留永久痕迹；名字要转义（仓库名会原样进 HTML）；
主链与快车道共用 `build_index_html`，只给一边注入的话，有新星那版页面会静默缺这块。
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 页面上提醒块的唯一标识：实现换标签/换样式都行，但这个标记在「有待办」时必须出现、
# 「无待办」时必须完全不出现 —— 它是"空则无痕"这条判据的抓手。
ATTENTION_NODE = "data-attention"

# 禁止出现在提醒文案里的措辞：这些会把"旧名过期"说成"收藏丢失"，而收藏实际一条没少。
LOST_WORDS = ("丢失", "漏拉", "少了", "已删除", "缺失收藏")

LIVE = [{"full_name": "a/keep"}, {"full_name": "b/keep2"}]
TABLE = {"a/keep": "agent", "b/keep2": "tools", "c/gone": "coding",
         "danny-avila/LibreChat": "assistant"}


def _fab():
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import fetch_and_build as fab
    return fab


def test_attention_lists_table_names_missing_from_current_stars():
    """孤儿集合 = 表键集 − 当前收藏 full_name 集（不多不少）。"""
    items = _fab().star_attention_items(LIVE, TABLE, {}, {})
    got = {i["item"] for i in items}
    assert got == {"c/gone", "danny-avila/LibreChat"}, \
        "提醒集合必须是「表−收藏」，实得 %s" % sorted(got)


def test_attention_entry_carries_kind_so_other_sources_can_join():
    """并入口：每条必须带 kind。本期只有 star_orphan，以后 RSS 超龄等只加 kind 值、不改形状。"""
    items = _fab().star_attention_items(LIVE, TABLE, {}, {})
    assert items, "给定有孤儿却返回空集 ⇒ 判据会空转"
    assert all(i.get("kind") == "star_orphan" for i in items), \
        "每条要标 kind，否则前端无法分类展示、以后也没法并入别的来源"


def test_new_star_absent_from_table_is_never_an_attention_item():
    """反向那半：收藏里有、表里还没记 = 正常新进场，绝不能进提醒（否则每场都报新仓库）。"""
    live = [{"full_name": "a/keep"}, {"full_name": "brand/new-star"}]
    items = _fab().star_attention_items(live, {"a/keep": "agent"}, {}, {})
    assert items == [], "新进场仓库不是待人工处理项，实得 %s" % items


def test_rendered_text_never_claims_stars_are_missing():
    """文案口径：只说"旧名待判定"，不说"收藏少了/丢了"。"""
    fab = _fab()
    html = fab.render_star_attention(fab.star_attention_items(LIVE, TABLE, {}, {}))
    assert html, "有孤儿却渲染出空串 ⇒ 提醒条根本不会上线"
    for w in LOST_WORDS:
        assert w not in html, "提醒文案用了「%s」：收藏实测一条没少，这是假警" % w


def test_same_base_name_candidate_is_hint_not_verdict():
    """同名候选只能当线索（用户明确否证过"同名即同仓库"），不许写成"已改名为"的定论。"""
    fab = _fab()
    live = [{"full_name": "LibreChat-AI/LibreChat"}, {"full_name": "a/keep"}]
    table = {"a/keep": "agent", "danny-avila/LibreChat": "assistant"}
    items = fab.star_attention_items(live, table, {}, {})
    hit = [i for i in items if i["item"] == "danny-avila/LibreChat"]
    assert hit, "旧名 danny-avila/LibreChat 应进提醒，实得 %s" % items
    cand = hit[0]["hint"].get("same_base_name_live") or []
    assert cand == ["LibreChat-AI/LibreChat"], "同名候选要作为线索带出来，实得 %s" % cand
    html = fab.render_star_attention(items)
    assert ("参考" in html) or ("线索" in html), "同名候选必须标明是参考，不是判定"
    assert "已改名为" not in html, "不许把同名候选写成「已改名为」这种定论"


def test_empty_attention_renders_nothing():
    """无待办时必须返回空串——不是空 div、不是占位注释。"""
    fab = _fab()
    assert fab.render_star_attention([]) == "", "空清单要渲染成空串，实得 %r" % \
        fab.render_star_attention([])


def test_page_has_no_attention_node_when_empty():
    """空清单时整页不许留下提醒节点：留了就是永久痕迹，页面永远挂着个空壳。"""
    fab = _fab()
    html = fab.build_index_html([], fab.CATS,
                                attention_html=fab.render_star_attention([]))
    assert ATTENTION_NODE not in html, \
        "无待办却仍在页面留 %s 节点 ⇒ 要求空则无痕" % ATTENTION_NODE


def test_page_shows_attention_node_when_pending():
    """有待办时该节点必须真的出现在整页里（与上一条配对，防"只写不接"）。"""
    fab = _fab()
    block = fab.render_star_attention(fab.star_attention_items(LIVE, TABLE, {}, {}))
    html = fab.build_index_html([], fab.CATS, attention_html=block)
    assert ATTENTION_NODE in html, "有待办却没把块注进页面 ⇒ 占位符没接上"
    assert "c/gone" in html, "提醒里要看得见具体旧名"


def test_orphan_names_are_html_escaped():
    """仓库名原样进 HTML：含标签或引号时必须转义（提醒块是把数据写进页面的唯一新出口）。"""
    fab = _fab()
    live = [{"full_name": "a/keep"}]
    table = {"x/<script>alert(1)</script>": "agent", 'y/"quoted"': "tools"}
    html = fab.render_star_attention(fab.star_attention_items(live, table, {}, {}))
    assert "<script>alert(1)</script>" not in html, "名字里的标签必须被转义，不许成可执行片段"
    assert "alert(1)" in html, "转义是改写法不是删内容：文案本身还得看得见"
    assert "&quot;" in html or "&#34;" in html, "名字里的双引号必须实体化，否则能闭掉属性"


def _call_blocks(src, name):
    """取出 name( … ) 的完整调用（括号配平、跨行），跳过 def 签名。

    为什么不用 `build_index_html\\([^)]*attention_html` 这种正则：调用里一旦出现
    `sidebar.get("trending")` 这类带右括号的实参，`[^)]*` 就会提前断掉，把"传了参数"
    误报成"没传"（2026-10-04 侧栏快照改动就把它打成假红，而假红的下一站是有人去放宽判据）。
    """
    out = []
    for m in re.finditer(re.escape(name) + r"\(", src):
        i = m.start()
        if src[max(0, i - 5):i].rstrip().endswith("def"):
            continue
        depth = 0
        for j in range(m.end() - 1, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(src[i:j + 1])
                    break
    return out


def test_index_html_has_exactly_one_producer():
    """首页 HTML 的唯一产地是小时场（2026-10-07 P0 摘掉快车道的 HTML 通路）。

    两半都要钉，缺一半就是假绿：
    - 正向：主链每一处 build_index_html 调用都必须传 attention_html=，漏一处那一版页面缺提醒条；
    - 反向：fast_refresh.py 里**不许再出现** build_index_html 调用点。只钉正向的话，
      谁把 HTML 生成加回快车道都一路绿——而 Pages 是整棵制品替换，两个 HTML 版本会互相
      覆盖成"半新半旧"（本次事故的形状）。旧版这条叫 test_both_render_exits_inject_attention，
      它要求两处都存在调用点，前提已随 P0 消失。
    """
    src = io.open(os.path.join(ROOT, "fetch_and_build.py"), encoding="utf-8").read()
    fast = io.open(os.path.join(ROOT, "fast_refresh.py"), encoding="utf-8").read()
    tpl = io.open(os.path.join(ROOT, "template.html"), encoding="utf-8").read()
    assert "__ATTENTION__" in tpl, "模板里要有 __ATTENTION__ 占位符"
    main_calls = _call_blocks(src, "build_index_html")
    assert main_calls, "主链里找不到 build_index_html 的调用点"
    assert all("attention_html=" in b for b in main_calls), \
        "主链有调用没传 attention_html ⇒ 那一版页面会缺提醒条"
    # 注释里提一次不算代码：先剔掉 # 开头行再找调用点（本文件曾因这个坑写下这条口径）
    fast_code = "\n".join(l for l in fast.splitlines() if not l.strip().startswith("#"))
    assert not _call_blocks(fast_code, "build_index_html"), \
        "快车道又长出 build_index_html 调用点 ⇒ 站点出现第二个 HTML 版本，整棵替换会互相覆盖"


def test_empty_live_set_renders_nothing():
    """收藏被拉成空集时不许渲染提醒：那时「表−收藏」= 整张表，会把 300 多个名字全报成待办。
    空收藏是拉取异常的信号，不是「用户取消了所有收藏」的证据 —— 报出来就是假警。"""
    fab = _fab()
    items = fab.star_attention_items([], TABLE, {}, {})
    assert items == [], "live 为空却把整张表报成孤儿（假警风暴）：%s" % len(items)
    assert fab.render_star_attention(items) == ""


def test_attention_block_is_capped():
    """条数上限：index.html 每场重写，无上限的清单会把体积增长写进 git 历史。
    超限必须截断并如实报剩余数，不能静默丢。"""
    fab = _fab()
    live = [{"full_name": "o%d/keep" % i} for i in range(3)]
    table = {"o%d/keep" % i: "agent" for i in range(3)}
    table.update({"z/orphan%03d" % i: "tools" for i in range(60)})
    items = fab.star_attention_items(live, table, {}, {})
    assert len(items) == 60, "条目层要如实给出全部 60 条，实得 %d" % len(items)
    html = fab.render_star_attention(items)
    n = html.count('class="attn-row"')
    assert n < 60, "页面层没截断：60 条全渲染进每场重写的 index.html"
    assert n == fab.ATTENTION_MAX_ROWS, "应当正好截到实现声明的档位，实得 %d" % n
    assert ("另有" in html) and ("60" in html or "%d" % (60 - n) in html), \
        "截断必须如实报剩余，不许静默丢条目"


# ==================== 剪枝：分类表 = 当前收藏的投影 ====================
# 上面整块提醒条的本领是"把旧名交给人判定"，但 2026-10-12 用户实测它修不掉：取消关注再重新
# 关注拿回来的是**改名后的新名**（`xintaofei/codeg`→`spacering-net/codeg` 等 4 条，全部经
# GitHub 301 重定向按 repository id 确证），旧名那条没有任何代码会删 ⇒ 提示永久挂着。
# 用户裁定口径：「取消关注这个行为本身就是删除，重新关注是另一个行为（走新增分类）」。
# 所以构建期直接把表剪成收藏的投影，人不再需要判定。
# 全案唯一能删错人的路径是拉取缩水：表 344 键而分页只拉到 100 条 ⇒ 差集 244，照剪等于清空
# 分类表、下一场全量重烧 LLM。故设门槛，且门槛不许严到永不生效（那会让页面看着像修好了）。

def test_prune_drops_names_absent_from_current_stars():
    """差集必须被删掉：剪完表 == 收藏的键集。
    夹具基数刻意取 11 键剪 1 条（90.9%）而不是 3 键剪 1 条：后者落在门槛之下，会因为"根本没
    敢剪"而通过，那就成了一条空转判据。"""
    known = {"r%02d/keep" % i: "agent" for i in range(10)}
    known["old/gone"] = "coding"
    live = set(known) - {"old/gone"}
    dropped = _fab().prune_star_state(known, live)
    assert "old/gone" not in known, "旧名没被剪掉，实剩 %s" % sorted(known)
    assert len(known) == 10, "剪多了或剪少了，实剩 %d 键" % len(known)
    assert sorted(dropped) == ["old/gone"], "要如实报删了什么，实得 %s" % (dropped,)


def test_prune_leaves_live_entries_untouched():
    """只删键，绝不改活着的键的值：分类是每场查表复用的，动值会让别的仓库串台。
    同样必须让剪枝真的发生（11→10），否则这条只是在验"门槛拦住了"。"""
    known = {"r%02d/keep" % i: "cat%02d" % i for i in range(10)}
    known["old/gone"] = "coding"
    before = dict(known)
    dropped = _fab().prune_star_state(known, set(before) - {"old/gone"})
    assert len(dropped) == 1, "夹具没落在门槛之上，本条会空转：%s" % (dropped,)
    for fn, cat in before.items():
        if fn in known:
            assert known[fn] == cat, "live 里的键 %s 的值被动过：%r -> %r" % (fn, cat, known[fn])


def test_prune_skips_when_live_is_empty():
    """收藏被拉成空集 ⇒ 一个都不许删。与提醒条同源的那条免疫，在删除上致命得多。"""
    known = {"a/keep": "agent", "b/keep2": "tools"}
    dropped = _fab().prune_star_state(known, set())
    assert known == {"a/keep": "agent", "b/keep2": "tools"}, "空收藏却把整张表剪了"
    assert list(dropped) == [], "空集本该整体跳过，却报删了 %d 条" % len(list(dropped))


def test_prune_skips_when_live_shrinks_below_floor():
    """拉取缩水到门槛之下 ⇒ 一律不剪。形状照抄真实事故：表 100 键只拉到 20 条。"""
    known = {"r%03d/repo" % i: "agent" for i in range(100)}
    dropped = _fab().prune_star_state(known, {"r%03d/repo" % i for i in range(20)})
    assert len(known) == 100, "缩水到 20% 却照剪，实剩 %d 键" % len(known)
    assert list(dropped) == [], "门槛没拦住，实删 %d 条" % len(list(dropped))


def test_prune_acts_on_bulk_unstar_within_floor():
    """反向那半：门槛不许严到永不生效。一次取关 10% 必须照剪，
    否则这条判据会退化成"永远不剪"，而页面上看着像已经修好了。"""
    known = {"r%03d/repo" % i: "agent" for i in range(100)}
    dropped = _fab().prune_star_state(known, {"r%03d/repo" % i for i in range(90)})
    assert len(known) == 90, "10% 的批量取关被门槛挡住，实剩 %d" % len(known)
    assert len(list(dropped)) == 10, "剪了却没如实报数，实得 %d" % len(list(dropped))


def test_prune_makes_attention_empty():
    """端到端：剪完再算提醒必须归零——这才是"用户再也看不到那个提示"的唯一真出口。
    只钉剪枝函数本身不够，两条链各自对也可能接不上。"""
    fab = _fab()
    known = {"a/keep": "agent", "old/renamed": "coding"}
    live_repos = [{"full_name": "a/keep"}, {"full_name": "new/renamed"}]
    fab.prune_star_state(known, {r["full_name"] for r in live_repos})
    items = fab.star_attention_items(live_repos, known, {}, {})
    assert items == [], "剪枝后仍报孤儿，提示条不会消失：%s" % [i["item"] for i in items]


def test_prune_floor_ratio_is_in_safe_band():
    """门槛常量必须真的参与判定，且落在安全区间：>0.5 才不至于"删掉一半以上也算正常"。
    上面两条用 20% 与 90% 各钉一端，任何 0.2<x<=0.9 的取值都能过，所以这里补下界。"""
    assert 0.5 < _fab().PRUNE_LIVE_MIN_RATIO <= 1, \
        "门槛 %r 不在安全区间" % (_fab().PRUNE_LIVE_MIN_RATIO,)


def test_prune_is_wired_before_attention():
    """剪枝必须真的接在主链里，且排在算提醒条**之前**。

    上面那些判据全都是直接调函数，一条都不碰 main()：函数写对而忘了接线的话它们全绿，
    而页面上那个提示一条都不会少。形状照抄 test_index_html_has_exactly_one_producer
    （同一份"只钉实现不钉接线会假绿"的教训）。
    """
    src = io.open(os.path.join(ROOT, "fetch_and_build.py"), encoding="utf-8").read()
    assert _call_blocks(src, "prune_star_state"), \
        "主链里找不到 prune_star_state 的调用点 ⇒ 剪枝根本没生效"
    i_prune = src.index("= prune_star_state(")
    i_attn = src.index("star_attention_items(out")
    assert i_prune < i_attn, "剪枝排在算提醒条之后，提示条仍会报旧名"


# 提示块里不许再出现"请你去判定是改名还是消失"的措辞。这条判定已被用户裁定为不需要人做
# （「取消关注这个行为本身就是删除」），而剪枝生效后这块唯一的到达路径就是门槛拦下那场——
# 标题写着"本场未自动清理"、正文却教人逐个核对改名，是同一块里的两头话（2026-10-12 对抗审查 P2-3）。
ADJUDICATION_WORDS = ("点开确认", "一眼可辨", "待人工判定", "点开后确认")


def test_attention_block_does_not_ask_user_to_adjudicate_renames():
    fab = _fab()
    html = fab.render_star_attention(fab.star_attention_items(LIVE, TABLE, {}, {}))
    assert html, "给定有孤儿却渲染出空串 ⇒ 本条会空转，禁词查的是空气"
    for w in ADJUDICATION_WORDS:
        assert w not in html, "提示块仍写着「%s」：与标题口径相反，且这判定已不需要人做" % w
