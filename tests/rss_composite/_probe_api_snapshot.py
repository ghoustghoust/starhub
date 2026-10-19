# tests/rss_composite/_probe_api_snapshot.py
# -*- coding: utf-8 -*-
"""_save_api_snapshot() 探针（在临时 CWD 下运行，避免覆盖仓库真快照）。

_save_api_snapshot 用相对路径写 "rss_api_snapshot.json"，因此必须在临时目录下调用。
本探针把结果打印成一行 JSON，供测试断言。

用法：python _probe_api_snapshot.py <temp_dir>
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

tmp = sys.argv[1] if len(sys.argv) > 1 else None
if not tmp:
    print(json.dumps({"error": "missing temp dir"}))
    sys.exit(0)
os.makedirs(tmp, exist_ok=True)
os.chdir(tmp)

from _loader import load_build  # noqa: E402

B = load_build()

fixture = [{
    "key": "S1", "name": "测试源", "cat": "ai", "color": "#111", "tier": 2,
    "items": [
        {"title": "OpenAI 发布新模型", "title_zh": "OpenAI 发布新模型", "summary_zh": "",
         "link": "https://x/1", "pub_date": "2026-09-14T10:00:00+08:00",
         "tags": ["openai", "大模型"]},
        {"title": "无标签文章", "title_zh": "无标签文章", "summary_zh": "",
         "link": "https://x/2", "pub_date": "2026-09-14T09:00:00+08:00"},
        {"title": "空标签列表", "title_zh": "空标签列表", "summary_zh": "",
         "link": "https://x/3", "pub_date": "2026-09-14T08:00:00+08:00", "tags": []},
    ],
}]

out = {"error": None}
try:
    B._save_api_snapshot(fixture)
    path = os.path.join(tmp, "rss_api_snapshot.json")
    with io.open(path, "r", encoding="utf-8") as f:
        snap = json.load(f)
    items = snap["sources"][0]["items"]
    out["n"] = len(items)
    out["tags"] = [it.get("tags", "<absent>") for it in items]
    out["keys0"] = sorted(items[0].keys())
    out["keys1"] = sorted(items[1].keys())
except Exception as e:
    out["error"] = "%s: %s" % (type(e).__name__, e)

print(json.dumps(out, ensure_ascii=False))
