# -*- coding: utf-8 -*-
"""被测模块加载器：默认加载仓库里的 build_rss_aggregator.py；
RSS_BUILD_SRC 指向副本时加载副本 —— 供变异测试在不改动真实源码的前提下验证守卫。

（与 tests/rss_sort 的抽取式 harness 同一思路：测试必须能对着"被改坏的源码"跑。）
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_build():
    src = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    spec = importlib.util.spec_from_file_location("build_rss_aggregator", src)
    mod = importlib.util.module_from_spec(spec)
    # 该脚本用 dirname(__file__) 定位 rss_sources.json / vendor_qrcode.min.js，
    # 指向副本会让它找不到资源 → 保持与原地运行等价
    mod.__file__ = os.path.join(ROOT, "build_rss_aggregator.py")
    sys.modules["build_rss_aggregator"] = mod
    spec.loader.exec_module(mod)
    return mod
