# -*- coding: utf-8 -*-
"""手机端导航下拉的可点性判据（blocking 门禁 A2，只断生成端）。

来历（2026-09-26 实测）：`@media (max-width:640px)` 给 `.nav-links` 加了 `overflow-x:auto`
让窄屏能横向滚动导航条。CSS 规范规定：一个轴不是 visible 时，另一轴的**使用值**会被强制成
auto —— 作者从没写过 `overflow-y`，它照样变成 auto。于是 `.nav-links` 成了纵向裁剪容器
（实测 clientHeight 31 / scrollHeight 278），绝对定位的下拉面板虽然 `opacity:1 visibility:visible`，
却被裁到只剩 12px，面板里的链接 `elementFromPoint` 根本打不到 —— 用户看到的就是"点了没反应"。

修法是在窄屏分支把面板改成 `position:fixed`（脱离该滚动容器），并用 `top:auto` 取静态位置，
天然贴在按钮下方 6px（`margin-top`），不需要 JS 写 inline top —— 实测跨断点缩放不会留下残值。
"""
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MEDIA_OPEN = "@media (max-width:640px){"


def _mobile_block():
    path = os.path.join(REPO_ROOT, "template.html")
    assert os.path.isfile(path), "template.html 不在了，这条判据就成了恒真"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.find(MEDIA_OPEN)
    assert start >= 0, "窄屏媒体查询不在了，这条判据已失效"
    depth, i = 0, start + len(MEDIA_OPEN) - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    raise AssertionError("窄屏媒体查询的括号没闭合")


def _decl_for(block, selector):
    m = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", block)
    return m.group(1) if m else ""


def test_mobile_dropdown_panel_escapes_the_scroll_container():
    block = _mobile_block()
    nav = _decl_for(block, ".nav-links")
    panel = _decl_for(block, ".nav-drop-panel")
    assert panel, "窄屏分支里没有 .nav-drop-panel 规则，判据锚点已失效"
    # 条仼式不变量：只有在容器确实会裁剪时才要求面板脱离。
    # 直接断言 overflow-x:auto 必须存在，等于把"窄屏横向滚动"这个设计也钉死：
    # 改成换行会让面板不再被裁，却把 blocking 门禁判红、部署冻住（与 §10.75 同一类错）。
    clipped = "overflow-x:auto" in nav.replace(" ", "") or "overflow:auto" in nav.replace(" ", "")
    assert not clipped or "position:fixed" in panel.replace(" ", ""), (
        "窄屏下拉面板必须脱离滚容器：.nav-links 带 overflow-x:auto 时，"
        "规范会把它 overflow-y 的使用值也变成 auto，绝对定位的面板虽 opacity:1 却被裁到点不到")


def test_mobile_panel_still_anchored_by_static_position():
    """top:auto + margin-top 撑起 6px 间距；写死 top 会在 header 换行时错位。"""
    panel = _decl_for(_mobile_block(), ".nav-drop-panel").replace(" ", "")
    if "position:fixed" not in panel:
        # 面板已脱离 fixed 方案（例如容器不再裁剪、改回常规定位）：
        # 锚定细节随之失去意义，不应把它钉死（与上条同理，去掉无条件断言）。
        return
    assert "top:auto" in panel, "面板用了 fixed 就得靠 top:auto 取静态位置，否则会退回硬写坐标"
    assert "margin-top:6px" in panel, "窄屏面板与按钮之间要有 6px 间距，不能贴死"

