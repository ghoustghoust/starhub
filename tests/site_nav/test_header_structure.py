# -*- coding: utf-8 -*-
"""首页 header 结构判据（blocking 门禁 A2，跑在构建之前）。

来历（台账 §10.74）：2026-09-25 终止深夜洞察时，`template.html` 摘除"深度洞察"入口行
连带删掉了紧邻其下的 `<div class="nav-drop">` 开标签。多出来的那个 `</div>` 在浏览器里
提前闭合了 `div.hd`（header 的 flex 行容器），于是"资讯平台""AI 工具""导出/导入/刷新"
降级成 header 的块级兄弟，纵向堆叠成错位。当晚 index.html 只删了入口行所以没露馅，
下一场白天构建从坏模板重写产物后才在线上看见。

**本文件只断生成端 `template.html`**：产物 `index.html` 每场构建都由模板重写，拿"产物与模板
不一致"去挡构建会自锁 —— 能修好产物的那场构建恰好被这条判据挡掉（2026-09-26 实测：模板已修、
产物仍旧时本文件前两条全绿、第三条红，A2 一红即不部署）。产物漂移的观测放在
`tests/site_nav_drift/`，那条是 advisory。
"""
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

DROP_OPEN = '<div class="nav-drop">'
DROP_BTN = 'class="nav-drop-btn"'


def _read(name):
    path = os.path.join(REPO_ROOT, name)
    assert os.path.isfile(path), "%s 不在了，这条判据就成了恒真" % name
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _header(text):
    start = text.find("<header>")
    assert start >= 0, "找不到 <header>，判据锚点已失效"
    end = text.find("</header>", start)
    assert end > start, "<header> 没有闭合"
    return text[start:end + len("</header>")]


def _div_walk(header_html):
    """返回 (收尾深度, 过程中出现过的最浅深度)。svg 内部无 div，整段剔掉免干扰。"""
    depth = 0
    shallowest = 0
    for line in re.sub(r"<svg\b.*?</svg>", "", header_html, flags=re.S).splitlines():
        for slash in re.findall(r"<(/?)div\b", line):
            depth += 1 if slash == "" else -1
            shallowest = min(shallowest, depth)
    return depth, shallowest


def test_template_header_div_balance():
    """开闭配平要双向：少闭会吞掉后续兄弟，多闭会提前关掉外层 flex 容器。"""
    depth, shallowest = _div_walk(_header(_read("template.html")))
    assert depth == 0, "template.html 的 header 里 div 少闭合 %d 个" % depth
    assert shallowest == 0, (
        "template.html 的 header 里有 %d 个无主 </div>，浏览器会拿它去闭合外层容器"
        % -shallowest)


def test_template_dropdown_buttons_are_wrapped():
    """每个下拉按钮都要有 nav-drop 包裹：面板定位靠容器的 position:relative。"""
    header = _header(_read("template.html"))
    wrappers = header.count(DROP_OPEN)
    buttons = header.count(DROP_BTN)
    assert buttons >= 1, "header 里已经没有下拉按钮了，这条判据失去意义"
    assert wrappers == buttons, (
        "下拉按钮 %d 个但容器只有 %d 个：有按钮裸在 nav 下，flex 布局会散"
        % (buttons, wrappers))
