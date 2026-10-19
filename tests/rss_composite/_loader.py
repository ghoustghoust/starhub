# tests/rss_composite/_loader.py
# -*- coding: utf-8 -*-
"""被测模块加载器（复用 tests/rss_date/_loader.py 模式）。"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_build():
    # 构建脚本 import build_logger 等同目录模块，需把仓库根加入模块搜索路径
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    src = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    spec = importlib.util.spec_from_file_location("build_rss_aggregator", src)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = os.path.join(ROOT, "build_rss_aggregator.py")
    sys.modules["build_rss_aggregator"] = mod
    spec.loader.exec_module(mod)
    return mod
