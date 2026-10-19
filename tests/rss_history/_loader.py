# tests/rss_fetch_state/_loader.py
# -*- coding: utf-8 -*-
"""被测模块加载器（沿用 tests/rss_composite/_loader.py 模式，加了一层缓存）。

缓存不是优化而是必需：build_rss_aggregator 顶层会把 sys.stdout 用
open(sys.stdout.fileno(), ...) 重挂一次，同一进程内二次 exec 会让解释程序在
关闭旧流时硬崩（pytest 收集到本目录两个测试文件即触发，exit 127、无报告）。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NAME = "build_rss_aggregator"


def load_build():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    src = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    cached = sys.modules.get(NAME)
    if cached is not None and getattr(cached, "_loaded_from", None) == src:
        return cached
    spec = importlib.util.spec_from_file_location(NAME, src)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = os.path.join(ROOT, "build_rss_aggregator.py")
    mod._loaded_from = src
    sys.modules[NAME] = mod
    spec.loader.exec_module(mod)
    return mod
