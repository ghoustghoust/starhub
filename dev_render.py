#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地预览：复用现有 index.html 的数据，重新渲染 template.html。

数据从 window.__STARHUB_BOOT__ 提取，不再依赖旧的 const 数据段。
只用于本地预览；不参与 GitHub Actions 构建。
"""
import datetime
import json
import re
import sys
from pathlib import Path

SRC = "index.html"
TPL = "template.html"
BOOT_NAME = "window.__STARHUB_BOOT__"


def object_end(source: str, brace_start: int) -> int:
    depth = 0
    quote = None
    escaped = False
    index = brace_start
    while index < len(source):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        else:
            if char in "'\"`":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    raise SystemExit("boot object has no closing brace")


def boot_span(source: str) -> tuple[int, int]:
    marker = source.find(BOOT_NAME)
    if marker < 0:
        raise SystemExit("window.__STARHUB_BOOT__ not found")
    brace = source.find("{", marker)
    if brace < 0:
        raise SystemExit("boot object has no opening brace")
    end = object_end(source, brace)
    semicolon = source.find(";", end)
    return marker, (semicolon + 1 if semicolon != -1 else end)


def main() -> None:
    source = Path(SRC).read_text(encoding="utf-8")
    template = Path(TPL).read_text(encoding="utf-8")
    source_start, source_end = boot_span(source)
    boot = source[source_start:source_end]
    template_start, template_end = boot_span(template)
    template = template[:template_start] + boot + template[template_end:]
    template = template.replace(
        "__ASSET_VERSION__",
        datetime.datetime.now().strftime("dev%Y%m%d%H%M%S"),
    )
    template = template.replace("__AI_SUMMARY__", "").replace("__ATTENTION__", "")
    Path(SRC).write_text(template, encoding="utf-8")
    print("渲染完成：index.html 已由 template.html 重新生成")


if __name__ == "__main__":
    main()
