# tests/rss_composite/_probe_cwd.py
# -*- coding: utf-8 -*-
"""CWD 无关性探针（issue m4 的专用检测器）

背景：`_load_diverse_config()` / `_load_analysis_enabled()` 原先用相对路径
`open("build_config.json")` 读配置，路径解析依赖进程 CWD。实测把同名诱饵文件
放进别的目录、从那里导入模块，会**静默读到另一个文件**（不是回落默认值）。

本探针刻意**不覆盖 `mod.__file__`**（对照 `_loader.py:18` 的做法），
让模块以真实 `__file__` 被加载 —— 这样「按 `__file__` 定位」与「按 CWD 定位」
两种实现会给出不同答案，缺陷才有区分力。

用法：python _probe_cwd.py <decoy_dir>
输出：单行 JSON（便于回归套件解析）
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DECOY = sys.argv[1]

result = {"cwd": None, "module_file": None, "error": None,
          "window_minutes": None, "enabled": None, "analysis_enabled": None}

try:
    os.chdir(DECOY)
    result["cwd"] = os.getcwd()
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    # 支持 RSS_BUILD_SRC：否则变异测试指向副本时本探针仍读真源码，对变异免疫
    real = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    spec = importlib.util.spec_from_file_location("bra_cwd_probe", real)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bra_cwd_probe"] = mod
    spec.loader.exec_module(mod)
    result["module_file"] = os.path.abspath(getattr(mod, "__file__", "") or "")
    result["window_minutes"] = mod.DIVERSE_CFG.get("window_minutes")
    result["enabled"] = mod.DIVERSE_CFG.get("enabled")
    result["analysis_enabled"] = mod.ANALYSIS_ENABLED
except Exception as e:
    result["error"] = "%s: %s" % (type(e).__name__, e)

sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
