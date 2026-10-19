# -*- coding: utf-8 -*-
"""header 行在窄一些时换行，而不是把工具条压住（blocking 门禁 A2，只断生成端）。

来历（2026-09-26 实测，既有缺陷，非 §10.74 引入）：基础 `.hd` 是单行 flex，
而 `.nav-links` 带 `min-width:0` 允许被压缩 —— 于是 641~1100px 这段导航不是换行而是**溢出自己的盒子**，
画到 `.acts` 上面。实测当前版在 700/820/1000px 时"导航按钮 × 工具条按钮"矩形相交对数为
6/4/1，且 `.acts` 第一个按钮（切换视图）中心取点命中的是导航按钮 —— 也就是**点不到**。
给 `.hd` 加 `flex-wrap:wrap` 后各宽度相交对数归零、工具条可点、header 在 641~1000 变成 103~138px 两行。

侧栏 `.side{position:sticky;top:76px}` 是按 header 单行写死的，但 `@media (max-width:1100px)`
已把它改成 `position:static`，与"header 会换行"的区间正好不交叠；实测 1120/1400px 改动前后
header 底边 61、侧栏顶 76 完全一致。
"""
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _base_rule(text, selector):
    m = re.search(r"(?m)^" + re.escape(selector) + r"\{([^}]*)\}", text)
    assert m, "%s 基础规则找不到了，判据锚点已失效" % selector
    return m.group(1)


def test_header_row_is_allowed_to_wrap():
    path = os.path.join(REPO_ROOT, "template.html")
    assert os.path.isfile(path), "template.html 不在了，这条判据就成了恒真"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    hd = _base_rule(text, ".hd").replace(" ", "")
    assert "flex-wrap:wrap" in hd, (
        ".hd 基础规则丢了 flex-wrap:wrap：窄一些时导航会溢出自己的盒子压住 .acts，"
        "实测 700~1000px 段工具条第一个按钮点不到")
    assert "row-gap:" in hd, "换行后两行之间要有 row-gap，否则导航与 logo 贴死"
