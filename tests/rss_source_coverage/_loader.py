# tests/rss_source_coverage/_loader.py
# -*- coding: utf-8 -*-
"""被测模块加载器（沿用 tests/rss_history/_loader.py 模式）。

同一进程内二次 exec 会让解释器在回收 build_rss_aggregator 重挂的 sys.stdout 时
关掉共用 fd，pytest 直接 exit 127，所以必须缓存。
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
