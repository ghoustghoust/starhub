# -*- coding: utf-8 -*-
"""产物级判据：生成出来的内联脚本必须还能被 node 解析。

为什么需要这一条而不是只测源码片段：`build_rss_aggregator.py` 里的 JS 活在**非 raw** 的
Python 三引号串中，源码写的 `\\b` / `\\n` / `\\u4e00` 要经 Python 解一道才成为 JS。
2026-10-01 改运行时翻译守卫时我就踩过：JS 正则里的 `\\b` 单写会被 Python 变成**退格符**，
词边界整个失效 —— 源码看着完全正常，只有产物才暴露。所以这里直接跑 `node --check` 验产物。
"""
import os
import re
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))

from _loader import load_build  # noqa: E402

mod = load_build()


def _node():
    import shutil
    return shutil.which("node")


SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _artifact():
    return mod.build_html([], "2026-10-01 08:20", 0, 0, analysis_data=None,
                          diverse_window_minutes=120, diverse_enabled=True)


def _inline_scripts(html):
    """按浏览器语义逐个取内联脚本：跳过带 src 的标签，正文取到下一个 </script>。

    不用一条正则通吃：页面里有 `s.onerror=...'createElement(\'script\')'` 之类往字符串里
    塞 `<script>` 的写法，扁平正则会把一段拆成两段（我第一版就只捞到 1 段，判据静默变空）。
    """
    out, i = [], 0
    while True:
        s = html.find("<script", i)
        if s < 0:
            break
        gt = html.find(">", s)
        end = html.find("</script>", gt)
        if gt < 0 or end < 0:
            break
        if "src=" not in html[s:gt]:
            out.append(html[gt + 1:end])
        i = end + 1
    return out


def test_app_script_parses_as_javascript():
    """产物里那段应用脚本（含运行时翻译守卫）必须过 `node --check`。

    Python 吞转义造成的语法雷只在产物层现形，源码 grep 看不出来。
    """
    node = _node()
    if not node:
        pytest.skip("本机没有 node，产物语法判据测不了")
    html = _artifact()
    bodies = _inline_scripts(html)
    app = [b for b in bodies if "_needsTranslation" in b]
    assert app, "产物里找不到含 _needsTranslation 的应用脚本（内联脚本共 %d 段）" % len(bodies)
    for k, body in enumerate(app):
        p = os.path.join(tempfile.gettempdir(), "_artifact_app_%d.js" % k)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        assert r.returncode == 0, "产物脚本语法不过：\n%s" % (r.stderr or "")[-1500:]


def test_artifact_has_no_unused_preload():
    """产物 head 里不许再有那个永远不会被复用的 hot_snapshot 预载。"""
    html = _artifact()
    assert 'href="hot_snapshot.json"' not in html, (
        "预载又回到产物里：消费方是 cache:'no-cache'，每次加载都会白下 63KB 并被控制台报警")


def test_translated_stub_rule_arrives_as_a_word_boundary():
    """\\b 必须原样落到产物里（而不是被 Python 变成退格符），否则元数据样板判据静默失效。"""
    html = _artifact()
    assert "_META_STUB" in html, "运行时翻译守卫没进产物，页面行为会退回旧版"
    i = html.find("var _META_STUB")
    line = html[i:html.find("\n", i)]
    assert "\\b" in line, "产物里词边界不是 \\b（Python 把它吃成退格符了）：%r" % line
    assert chr(8) not in line, "产物正则里混进了退格符：词边界整个失效：%r" % line[:120]


# 2026-10-01 运行时窗口队列引入的符号。逐条钉"在产物里被真调用"，因为这一类回归是静默的：
# 删掉一个调用点，页面只是少翻几屏，控制台不会报错，判据也不红。
# 计数只看代码行（跳过注释行），否则"名字出现在注释里"会冒充成"有人调用"。
WALL_QUEUE_SYMBOLS = [
    "_wallWindow", "_wallFingerprint", "_abortWallInflight", "_applyWallTr",
    "_translateWallItems", "_scheduleWallTranslate",
    "_browserGtx", "_gtxUnreachable", "_markGtxDead", "_cutW",
    "GTX_ABORT_MS", "WALL_SUMMARY_LIMIT",
]


def _code_only(body):
    keep = []
    for line in body.split("\n"):
        t = line.strip()
        if t.startswith("//") or t.startswith("/*") or t.startswith("*"):
            continue
        keep.append(line)
    return "\n".join(keep)


def test_wall_queue_symbols_are_actually_called_in_artifact():
    assert WALL_QUEUE_SYMBOLS, "名单空了：这条判据就成了恒绿的装饰"
    body = _code_only("".join(b for b in _inline_scripts(_artifact()) if "_needsTranslation" in b))
    dead = [n for n in WALL_QUEUE_SYMBOLS if len(re.findall(r"\b" + n + r"\b", body)) < 2]
    assert not dead, (
        "这些符号在产物里只有定义、没有调用点（= 窗口队列被改成了写了不跑的孤儿）：%s" % ", ".join(dead))
