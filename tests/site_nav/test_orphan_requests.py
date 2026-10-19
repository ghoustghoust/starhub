# -*- coding: utf-8 -*-
"""两处孤儿：永远被绕过的 preload，和挂了不隐藏的封面图。

现场证据（2026-10-01 08:0x，Pages 版 rss-aggregator.html 控制台）：
  1. `The resource hot_snapshot.json was preloaded using link preload but not used within a
     few seconds` —— 每次加载都刷。根因不是"用户没点热榜"，而是消费方写死了
     `fetch('hot_snapshot.json',{cache:'no-cache'})`：**no-cache 明确绕过预载那份**，
     所以 <link rel=preload> 无论用户点不点热榜都不会被复用，63KB 白下载一次。
     （preload 还带 crossorigin，而 fetch 不带 credentials，两边连键都对不上。）
  2. 封面图请求失败（新华网 OSS 实测 http=502、https TLS 直接失败；BBC 图 ERR_CONNECTION_CLOSED）
     时没有任何降级 —— `.cover-img` 就这么挂在卡片上，用户看到一个破图占位。

两条判据都写成"读真实产出形状"而不是"读我的意图"：preload 那条同时钉住消费方的
`cache:'no-cache'`，防止有人反过来把 preload 留下、把 no-cache 当"数据新鲜度"保下来。
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")


def _src():
    with io.open(BUILD, encoding="utf-8") as fh:
        text = fh.read()
    # 这段 JS 活在 Python 三引号串里，串内的单引号写成 \'；先把这层转义抹平，
    # 否则正则读到的永远是源码转义形（我第一版就因此匹配不到消费方）。
    return text.replace("\\'", "'")


def test_no_preload_for_the_lazily_consumed_snapshot():
    """preload 与 cache:'no-cache' 同时存在 = 每次加载白下 63KB，必须只留一个。"""
    text = _src()
    has_preload = re.search(r'rel="preload"[^>]*hot_snapshot\.json', text)
    consumer = re.search(r"fetch\(['\"]hot_snapshot\.json['\"][^)]*cache:\s*'no-cache'", text)
    assert consumer, (
        "热榜消费方不再是 cache:'no-cache' 了：本判据的前提变了，请重读 docstring 再决定留哪边")
    assert not has_preload, (
        "既然 fetch 用的是 cache:'no-cache'，preload 那份永远不会被复用 —— 留着就是每次加载 "
        "63KB 的孤儿请求：%s" % has_preload.group(0))


def test_hot_tab_still_fetches_its_snapshot():
    """拆 preload 不许把热榜本身拆掉：懒加载那条 fetch 必须还在。"""
    assert "function loadHotSnapshot(" in _src(), "热榜懒加载入口没了，页面会点不出内容"


def test_cover_img_degrades_when_the_request_fails():
    """封面图取不到（源站 502 / TLS 失败 / 大陆连不通）时必须隐身，而不是留个破图。"""
    text = _src()
    img = re.search(r"h\+='<img class=\"cover-img\"[^;]*?>';", text)
    assert img, "封面图的产出形状变了，本判据读不到那行：%s" % (img and img.group(0))
    assert "onerror" in img.group(0), (
        "封面图没有 onerror：外部图源挂掉时卡片上会留一个破图占位（线上实测新华网 502、BBC 连接失败）")


# ── 封面 referrer 策略（#2：修"全局 no-referrer 一票否决部分图床"） ─────────
COVER_SAMPLES = [
    ("白名单主机", "http://images1.caifuzhongwen.com/a/b.jpg", ""),
    ("白名单子域", "https://foo.caifuzhongwen.com/x.jpg", ""),
    ("普通图床", "https://ichef.bbci.co.uk/1.jpg", "no-referrer"),
    ("查询串里冒充白名单", "http://evil.com/?x=caifuzhongwen.com", "no-referrer"),
    ("后缀里冒充白名单", "https://caifuzhongwen.com.evil.net/a.jpg", "no-referrer"),
    ("相对路径", "/local/img.png", "no-referrer"),
]


def _cover_policy_js():
    import os as _os
    import sys as _sys
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    _sys.path.insert(0, _os.path.join(root, "tests", "rss_composite"))
    _sys.path.insert(0, root)
    from _loader import load_build
    html = load_build().build_html([], "2026-10-01 12:20", 0, 0, analysis_data=None,
                                   diverse_window_minutes=120, diverse_enabled=True)

    def cut(a, b):
        i = html.find(a)
        assert i >= 0, "产物里找不到 %r：#2 的实现被删了或改名了" % a
        j = html.find(b, i)
        assert j > i, "%r 在产物里没有结束边界" % a
        return html[i:j + len(b)]

    def cut_block(a, b):
        i = html.find(a)
        assert i >= 0, "产物里找不到 %r：#2 的实现被删了或改名了" % a
        j = html.find(b, i)
        assert j > i, "%r 在产物里没有结束边界" % a
        return html[i:j]

    # `_coverRefPolicy` 现在与 `_coverHost`/`_hostInTable`/两张表连成一段，
    # 只抠函数体会在 node 里 ReferenceError —— 那是判据读错了范围，不是实现的错。
    # 所以白名单的静态检查读**声明行**，执行时喂**整段**。
    return cut("var _REF_HOSTS = ", ";"), cut_block("var _REF_HOSTS = ", "function renderWall")


def test_cover_referrer_policy_is_per_host(tmp_path):
    """按主机名判：白名单不许空，冒充样本不许命中，普通图床不许被放开。"""
    import json
    import re
    import shutil
    import subprocess

    decl, block = _cover_policy_js()
    hosts = re.findall(r"['\"]([^'\"]+)['\"]", decl)
    assert hosts, "白名单空了：等于退回全局 no-referrer 的一刀切（财富中文网那类又取不到图）"
    bad = [h for h in hosts if ("/" in h or ":" in h or " " in h)]
    assert not bad, "白名单里必须是裸主机名（带协议/路径就是写错了）：%s" % bad

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("本机没有 node，跑不了产物级 JS")
    script = (block + "\nvar S=" + json.dumps([s for _, s, _ in COVER_SAMPLES], ensure_ascii=False)
              + ";\nconsole.log(JSON.stringify(S.map(_coverRefPolicy)));\n")
    p = tmp_path / "_cover_ref.js"
    p.write_text(script, encoding="utf-8")
    r = subprocess.run([node, str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, "产物 JS 跑崩：\n%s" % (r.stderr or "")[-600:]
    got = json.loads(r.stdout.strip())
    assert len(got) == len(COVER_SAMPLES), "返回条数与样本不等，判据没逐条对齐"
    for (label, _url, want), have in zip(COVER_SAMPLES, got):
        assert want == have, "%s 的 referrer 策略应为 %r，实为 %r" % (label, want, have)
