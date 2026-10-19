# -*- coding: utf-8 -*-
"""template.html 内联 JS 语法门禁（A2 blocking）。

2026-10-04 白屏事故：质心维度守卫的 Edit 加了第三层 if 嵌套却少写一个闭括号，
整段内联 JS 解析失败 ⇒ 线上 index.html 白屏约 1 小时。既有门禁只对 api/*.js 跑
node --check（gate A），template 的内联 JS 没有任何语法检查；div 配平测试
（test_header_structure）不管 JS 括号。本判据把 template 的每个 <script> 块
喂给 node --check，坏语法在 A2 就死，上不了线。
"""
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TPL = os.path.join(ROOT, "template.html")


def _script_blocks():
    html = open(TPL, encoding="utf-8").read()
    blocks = re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
    n_open = len(re.findall(r"<script[ >]", html))
    n_close = html.count("</script>")
    assert n_open == n_close == len(blocks), (
        "script 开闭标签数不一致（open=%d close=%d blocks=%d）——"
        "可能 JS 字符串里有裸 </script>（须转义为 <\\/script>）" % (n_open, n_close, len(blocks)))
    return blocks


def test_template_inline_js_parses():
    blocks = _script_blocks()
    assert blocks, "template 必须有内联 JS"
    for i, block in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(block)
            path = f.name
        try:
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            assert r.returncode == 0, (
                "template.html 第 %d 个 <script> 块语法错误（白屏级故障）：\n%s%s"
                % (i, r.stderr[-600:], r.stdout[-200:]))
        finally:
            os.unlink(path)
