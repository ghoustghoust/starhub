# -*- coding: utf-8 -*-
"""封面"必挂域名"判空表（修复方向 ③#1）的行为判据与接线判据。

现场证据（docs/排查记录.md 2026-10-01 逐源实测，两条通道口径）：
    ichef.bbci.co.uk        BBC 全系 305 张封面 **100% 占位**（DNS 投毒 + schannel 握手失败）
    i.guim.co.uk            卫报 92 张 401，换 UA / 加 Referer 四种组合**全挂**
    npr.brightspotcdn.com   NPR 54 张 403，同上四种组合同挂
    external-preview.redd.it  Reddit 预览图 24 张 ECONNRESET
    *.ops.xhyun.news.cn     新华社内网 OSS，公网 NXDOMAIN（任何人取不到）
⇒ 这些封面出厂就该写空：卡片退化为纯文字紧凑卡，比"渲染一张必挂的 150px 图 + 首字母占位"更便宜。

判据要挡住的四件事：
  ① 表被清空或被**无证据**扩大 —— 所以精确集合钉死，且每一项必须在排查记录里能查到原文；
  ② 一刀切误伤"只是这台机器无代理直连挂、受众用代理能看"的 CDN（pbs.twimg.com、cdn.hk01.com
     等）—— 所以未收名单同样钉死，误收即红；
  ③ 与 ③#2（referrerpolicy 按域名放行）互相抵消：caifuzhongwen 带 Referer 就 200，
     若它同时落进判空表，③#2 立刻变成死代码。代码里不设"例外分支"（两表不相交时那种分支
     永不触发），所以由判据直接红来发现；放行名单只从**现生成产物**的 JS 里读一份。
  ④ **判空函数存在但没人调**：直接单测 `_drop_unloadable_cover` 永远绿，哪怕生产路径绕开它。
     所以额外用真解析器 `_parse_rss_item` 端到端喂 BBC enclosure，并单独验 `_upgrade_img_url`
     确实把计数打进了 `_bad_cover_hits`（CI 播报的就是它）。
"""
import ast
import os
import re
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 表内容与排查记录逐条对应；要改表必须先在那里补实测。别把它清空——清空后判据①就永不检查。
EXPECTED_TABLE = {
    "ichef.bbci.co.uk",
    "i.guim.co.uk",
    "npr.brightspotcdn.com",
    "external-preview.redd.it",
    "ops.xhyun.news.cn",
}

# 明确**不收**的域名，每种不收都有理由，误收即红：
#   pbs.twimg.com / cdn.hk01.com / cdn.prod.www.spiegel.de / static01.nyt.com /
#   wechat2rss.xlab.app：只在"无代理直连"口径挂，浏览器（带系统代理）口径能出图 ⇒ 受众可见；
#   preview.redd.it：与 external-preview 同族但记录里没有它的实测，未测不收；
#   vpsbbc.com / files.seeusercontent.com：各 1 张且两条通道结论互相矛盾 ⇒ 样本不足以按域名一刀切；
#   www.appinn.com：7 张全 404 是**单张图过期**，域名本身可达 ⇒ 按域名清会误伤新文章；
#   images1.caifuzhongwen.com：带 Referer 即 200，走 ③#2 而不是判空。
NOT_IN_TABLE = [
    "pbs.twimg.com",
    "cdn.hk01.com",
    "cdn.prod.www.spiegel.de",
    "static01.nyt.com",
    "wechat2rss.xlab.app",
    "preview.redd.it",
    "vpsbbc.com",
    "files.seeusercontent.com",
    "www.appinn.com",
    "images1.caifuzhongwen.com",
]


def _load():
    sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from _loader import load_build
    return load_build()


def _module_source():
    """读**变异副本**所在的那份源码（RSS_BUILD_SRC 与本仓变异约定同源）。"""
    src = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    with open(src, encoding="utf-8") as fh:
        return fh.read()


def test_table_is_exactly_the_measured_hosts():
    B = _load()
    assert set(B._BAD_COVER_HOSTS) == EXPECTED_TABLE, (
        "必挂封面域名表与排查记录的实测不一致。新增要写证据（并在 docs/排查记录.md 留逐源读数），"
        "删除要说明该域名为何已恢复；两边都不许静默改。")


def _table_source_block():
    """取 `_BAD_COVER_HOSTS = frozenset({...})` 那一段源码（域名与它的实测理由必须同行出现）。"""
    src = _module_source()
    i = src.index("_BAD_COVER_HOSTS = frozenset({")
    j = src.index("})", i)
    return src[i:j]


def test_every_entry_carries_its_measurement_comment():
    """每个域名后面必须跟一句实测理由。

    原来这条是去读 `docs/排查记录.md` 的，CI 里直接 FileNotFoundError —— 那份记录**没有进 git**，
    于是判据在本地绿、在 ubuntu 红（09:00 场 A2 blocking 就是被它冻住的）。
    证据要么跟着代码走，要么就别声称"有证据链"：这里改成要求行尾注释。
    """
    block = _table_source_block()
    no_reason = []
    for host in sorted(EXPECTED_TABLE):
        lines = [l for l in block.splitlines() if '"%s"' % host in l]
        assert len(lines) == 1, "表里 %s 出现 %d 次，判据的行定位失效" % (host, len(lines))
        reason = lines[0].split("#", 1)[1].strip() if "#" in lines[0] else ""
        if len(reason) < 8:
            no_reason.append(host)
    assert not no_reason, (
        "这些域名没有行尾实测理由（写清几条封面、什么错误码），不许凭印象进表：%s" % ", ".join(no_reason))


@pytest.mark.parametrize("host", sorted(EXPECTED_TABLE))
def test_bad_host_cover_is_nulled(host):
    B = _load()
    assert B._drop_unloadable_cover("https://%s/240x134.jpg" % host) == ""


@pytest.mark.parametrize("host", NOT_IN_TABLE)
def test_healthy_or_unmeasured_host_is_untouched(host):
    B = _load()
    url = "https://%s/a.jpg" % host
    assert B._drop_unloadable_cover(url) == url, "%s 不在实测必挂表里，不许被清掉" % host


def _generated_html():
    return _load().build_html([], "2026-10-01 12:20", 0, 0, analysis_data=None,
                              diverse_window_minutes=120, diverse_enabled=True)


def _js_ref_hosts():
    """页面侧 `referrerpolicy` 放行名单（③#2），从**现生成产物**里取。

    Python 侧故意不再存一份副本：两处各写一套名单就会分叉（今天刚为翻译判据的 Py/JS
    分叉补过判据），而产物里那份才是浏览器真正在用的事实来源。
    """
    html = _generated_html()
    marker = "var _REF_HOSTS = "
    i = html.index(marker)
    lit = html[i + len(marker):html.index(";", i + len(marker))].strip()
    body = lit[lit.index("[") + 1:lit.rindex("]")]
    return {x.strip().strip("'\"") for x in body.split(",") if x.strip()}


def test_referer_hosts_are_the_measured_ones():
    """放行名单只许是"带 Referer 即 200"实测过的那个域名（证据见本文件 docstring §防盗链反向）。"""
    hosts = _js_ref_hosts()
    assert hosts == {"caifuzhongwen.com"}, (
        "referrerpolicy 放行名单变了：%s（新增要实测证据，删除要确认没人再依赖它）" % sorted(hosts))


def test_referer_hosts_survive_the_bad_table():
    """③#2 放行与 ③#1 判空不许互相抵消：放行域名的封面必须照旧出厂。

    代码里**没有**运行时例外分支（两张表不相交时那种分支永不触发 = 死代码），
    所以两表相撞只能由这条判据变红来发现。
    """
    B = _load()
    for h in sorted(_js_ref_hosts()):
        for url in ("https://%s/a.jpg" % h, "https://images1.%s/x.jpg" % h):
            assert B._drop_unloadable_cover(url) == url, (
                "%s 的封面被判空表清掉了 ⇒ 页面侧的 referrerpolicy 放行成了死代码" % url)


def test_host_parsing_edges():
    B = _load()
    # 子域、大写、带端口与 userinfo 都要命中（BBC 的 URL 形态不止一种）
    for url in ("https://live.bbc.example.i.guim.co.uk/a.jpg",
                "HTTPS://ICHEF.BBCI.CO.UK/240x134.jpg",
                "https://cdn.npr.brightspotcdn.com:443/a.jpg",
                "https://user:pass@external-preview.redd.it/a.jpg",
                "http://bucket-cb-yunqiao.oss-cn-beijing-xhyun-d01-a.ops.xhyun.news.cn/a.jpg"):
        assert B._drop_unloadable_cover(url) == "", "漏判：%s" % url
    # 前缀相似但并非子域 ⇒ 不许误伤
    for url in ("https://i.guim.co.uk.attacker.example/a.jpg",
                ("https://notnpr.brightspotcdn.com.attacker.example/a.jpg")):
        assert B._drop_unloadable_cover(url) == url, "误判：%s" % url
    # 非 http(s)（相对路径 / data URI）不参与判空
    for url in ("/local/a.jpg", "data:image/png;base64,AAAA", ""):
        assert B._drop_unloadable_cover(url) == url


def _item_with_enclosure(url):
    return ET.fromstring(
        "<item><title>Some headline</title>"
        "<link>https://example.com/a</link>"
        "<description>English description</description>"
        "<enclosure url=\"%s\" type=\"image/jpeg\" length=\"123\"/>"
        "<pubDate>Wed, 01 Oct 2026 08:00:00 GMT</pubDate></item>" % url)


def test_parse_rss_item_drops_bad_cover_end_to_end():
    """真解析器路径：BBC 的 enclosure 必须落到产物里为空。

    这条是"判空函数被绕过"的解药 —— 只单测 `_drop_unloadable_cover` 的话，
    把 `_upgrade_img_url` 换回裸 `_pick_item_image` 也不会有任何判据变红。
    """
    B = _load()
    out = []
    B._parse_rss_item(_item_with_enclosure("https://ichef.bbci.co.uk/1/640/c000.jpg"),
                      "BBC Top Stories", "bbc_top", "world", out)
    assert len(out) == 1, "合成 item 没被解析，判据在空输入上跑"
    assert out[0]["image"] == "", "BBC 封面没被判空：%r" % out[0]["image"]

    ok = []
    B._parse_rss_item(_item_with_enclosure("https://imgpai.thepaper.cn/1/640/c000.jpg"),
                      "澎湃", "thepaper", "world", ok)
    assert ok[0]["image"] == "https://imgpai.thepaper.cn/1/640/c000.jpg", "健康封面被误清"


def test_upgrade_img_url_bumps_the_drop_counter():
    """CI 播报读的是 `_bad_cover_hits`；`_upgrade_img_url` 不接判空 ⇒ 计数恒 0、播报恒 0。"""
    B = _load()
    B._bad_cover_hits.clear()
    assert B._upgrade_img_url("https://ichef.bbci.co.uk/1/240x134/c000.jpg") == ""
    assert B._bad_cover_hits.get("ichef.bbci.co.uk") == 1
    assert B._upgrade_img_url("https://scx1.b-cdn.net/tmb/a.jpg") == \
        "https://scx1.b-cdn.net/800/a.jpg", "健康域名的升级规则必须照旧生效"


def test_history_purge_only_touches_bad_hosts_and_reports_count():
    """历史缓存清理必须①真的清、②不误伤健康条目、③给得出条数（播报里那个"历史清理 N 张"）。"""
    B = _load()
    history = {
        "https://x/1": {"image": "https://ichef.bbci.co.uk/a.jpg", "link": "https://x/1"},
        "https://x/2": {"image": "https://imgpai.thepaper.cn/a.jpg", "link": "https://x/2"},
        "https://x/3": {"link": "https://x/3"},  # 没有 image 键：不许被凭空加一个
    }
    n = B._purge_bad_covers_in_history(history)
    assert n == 1
    assert history["https://x/1"]["image"] == ""
    assert history["https://x/2"]["image"] == "https://imgpai.thepaper.cn/a.jpg"
    assert "image" not in history["https://x/3"], "空条目被写进了 image 键，历史结构被改坏"
    assert B._purge_bad_covers_in_history(history) == 0, "第二次必须无变化（幂等），否则每场都在重复计数"


def test_history_purge_is_called_by_the_pipeline():
    """函数存在但没人调 = 死代码；调用点在源码里必须真的出现。"""
    tree = ast.parse(_module_source())
    called = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_purge_bad_covers_in_history"]
    assert len(called) == 1, (
        "历史清理调用点应为 1 处，实际 %d 处（0 = 定义后没人用，>1 = 同一场里计数会翻倍）" % len(called))



def test_no_upgrade_rule_targets_a_nulled_host():
    """判空先跑 ⇒ 规则里再留 BBC/卫报条目就是永不执行的死配置。"""
    B = _load()
    dead = []
    for entry in list(B._IMG_UPGRADE_RULES) + list(B._IMG_UPGRADE_SPECIAL):
        domain_match = entry[0]
        if B._drop_unloadable_cover("https://%s/a.jpg" % domain_match) == "":
            dead.append(domain_match)
    assert not dead, "这些升级规则的域名已被封面判空表清掉，规则永不触发：%s" % sorted(set(dead))


def test_dropped_count_is_broadcast_to_ci():
    """判空必须可观测：源码里必须有一处 print 播报 `[封面判空]`（命中数或 0 命中）。"""
    tree = ast.parse(_module_source())
    hits = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                        and sub.value.startswith("[封面判空]"):
                    hits += 1
                    break
    assert hits >= 2, (
        "期望命中数与 0 命中两条各一处 print（0 命中也要出声，否则表过期无人知），实际 %d 处" % hits)


def test_render_site_uses_the_bad_cover_filter():
    """渲染层必须真的过一遍 `_dropBadCover`，函数存在但没人调 = 表只对构建产物生效。

    为什么单独钉这条：卡片模板里 `<img class="cover-img" src="…">` 用的必须是**过滤后**的那个值。
    把它接回 `esc(a.img)`（实时链路 `?source=` 的封面就是这样绕过来的）时，
    本文件其余判据全都照绿 —— 因为它们只检查函数与名单在不在，不检查渲染点用没用。
    """
    html = _generated_html()
    i = html.index("function renderWall(")
    j = html.index('<img class="cover-img"', i)
    seg = html[i:j + 220]
    assert "_dropBadCover(a.img)" in seg, (
        "卡片渲染点不再调用 _dropBadCover ⇒ 必挂域名表对实时链路条目失效（实时封面会绕过判空）")
    assert "var hasImg=!!_cv;" in seg, "hasImg 不再由过滤后的值决定，空封面会画出一张空图卡"

    m = re.search(r"""<img class="cover-img" src="'\+esc\((\w+)\)""", seg)
    assert m, "找不到 cover-img 的 src 拼接式，判据要看的新形态是：src=\"'+esc(_cv)+'\""
    assert m.group(1) == "_cv", "cover-img 的 src 用的是 %s（未过滤的那个值）" % m.group(1)
    p = re.search(r"""referrerpolicy="'\+esc\(_coverRefPolicy\((\w+)\)\)""", seg)
    assert p, "cover-img 上找不到按域名的 referrerpolicy（③#2 被删了）"
    assert p.group(1) == "_cv", "referrerpolicy 读的是 %s 而不是过滤后的 _cv：同一个 URL 两个副本会分叉"
    assert "esc(a.img)" not in seg, "渲染点仍在直接用 a.img（未过滤），上面的等式就被绕过了"


def test_bad_cover_table_is_referenced_by_the_drop_function():
    """表若不再被判空函数使用，就等于把防线静默注销 —— 反向确认引用点存在。"""
    tree = ast.parse(_module_source())
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "_BAD_COVER_HOSTS"}
    assert used == {"_BAD_COVER_HOSTS"}, "必挂表在源码里没有读取点（只定义不使用 = 死配置）"
