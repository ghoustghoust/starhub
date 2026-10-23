#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地开发工具：从现有 index.html 提取已注入的数据，重新渲染 template.html。

只用于本地预览模板/样式改动；不参与 GitHub Actions 构建。
"""
import json
import re
import sys

SRC = "index.html"
TPL = "template.html"

CONSTANTS = [
    ("DATA", "__DATA__"),
    ("CATS", "__CATS__"),
    ("LANG_COLORS", "__LANGS__"),
    ("DEFAULT_FAVS", "__FAVS__"),
    ("UPDATED", "__UPDATED__"),
    ("TRENDING", "__TRENDING__"),
    ("FEED", "__FEED__"),
]


def extract_value(source, name):
    match = re.search(r"(?:const|let)\s+%s\s*=\s*" % re.escape(name), source)
    if not match:
        raise SystemExit("index.html 中未找到常量 %s" % name)

    start = index = match.end()
    depth = 0
    quote = None
    escaped = False
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
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == ";" and depth == 0:
                return source[start:index]
        index += 1
    raise SystemExit("常量 %s 没有找到结束分号" % name)


def main():
    source = open(SRC, encoding="utf-8").read()
    template = open(TPL, encoding="utf-8").read()

    for name, placeholder in CONSTANTS:
        raw = extract_value(source, name).strip()
        if name == "UPDATED":
            raw = raw[1:-1]
        else:
            json.loads(raw)
        if placeholder not in template:
            raise SystemExit("template.html 中缺少占位符 %s" % placeholder)
        template = template.replace(placeholder, raw)

    open(SRC, "w", encoding="utf-8").write(template)
    print("渲染完成：index.html 已由 template.html 重新生成")


if __name__ == "__main__":
    main()
