# -*- coding: utf-8 -*-
"""内联 JSON 注入面：`ANALYSIS_DATA` 必须与数据分块同口径转义（任务 #37）。

现场（现网产物实测，2026-09-21 18:29）：
`rss-aggregator.html` 里 `ANALYSIS_DATA = {...}` 段长 2,363,364 字节，含裸 `<` 28 个、
`>` 52 个，其中 `topic_clusters[6].items[0]/[3]` 的值直接来自**上游文章标题**。
`</script>` 目前计数为 0 所以还没炸，但通路是开的：任一格子里出现
`</script><img src=x onerror=...>`，下一场构建就会让它对每个访客执行。

同一文件里数据分块落盘那条通道（`_dump`）已经把 `<` 转成 `\\u003c` —— 本仓**已有**
"内联 JSON 转义尖括号"的约定，只有 `build_rss_aggregator.py:3186` 那句
"三引号 + analysis_json + 三引号" 的原样拼接漏了。这条判据就是把约定钉回这个注入点。
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _loader import load_build  # noqa: E402

M = load_build()

# 故意拆成两半写，免得这个测试文件本身在仓库里看起来像个注入串
HOSTILE = '</scr' + 'ipt><img id=pwned onerror=alert(1)>'
HOSTILE_JSON = json.dumps({"quality": {"x": HOSTILE},
                           "topic_clusters": [{"label": "L", "items": [HOSTILE]}]},
                          ensure_ascii=False)


def _injected_blob():
    js = M._build_js([{"key": "k1", "name": "N", "cat": "c", "color": "#f00", "items": []}],
                     0, HOSTILE_JSON)
    marker = "ANALYSIS_DATA = "
    i = js.index(marker) + len(marker)
    obj, end = json.JSONDecoder().raw_decode(js[i:])
    return js[i:i + end], obj


def test_no_bare_script_terminator_in_inlined_json():
    blob, _obj = _injected_blob()
    assert '</scr' + 'ipt' not in blob.lower(), (
        "内联 ANALYSIS_DATA 里出现裸 </script> 前缀 ⇒ 上游标题可注入脚本；"
        "必须与数据分块同口径转义（< → \\u003c）")


def test_escaping_keeps_the_payload_lossless():
    """转义之后必须还能解回原值：否则这条判据会把功能一起打死。"""
    _blob, obj = _injected_blob()
    assert obj["quality"]["x"] == HOSTILE, "转义把数据改坏了：%r" % (obj["quality"]["x"],)
    assert obj["topic_clusters"][0]["items"][0] == HOSTILE


def test_no_raw_lt_inside_the_inlined_json():
    """与 chunk 的 `_dump` 同口径：注入段内不许出现裸 `<`。

    只约束 `<` 是有意的 —— `</script>` 的触发字符就是它，裸 `>` 开不了标签边界，
    而数据分块通道也只转这一个。把 `>` 一起转会让两条通道口径分叉，白多一处差异。
    """
    blob, _obj = _injected_blob()
    assert "<" not in blob, (
        "仍有裸 `<`（%d 个）—— 只挡 </script> 是不够的，`<` 本身就是标签边界起点"
        % blob.count("<"))


def _node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_js_reads_the_escaped_payload_back_verbatim(tmp_path):
    """转义不许改坏数据：用真的 JS 引擎把那一行 eval 回来比对。

    Python 侧 json.loads 通过不代表 JS 也认 `\u003c`（其实认，但这条判据要的是"由 JS 证明"，
    因为出厂后读它的是浏览器）。
    """
    js = M._build_js([{"key": "k1", "name": "N", "cat": "c", "color": "#f00", "items": []}],
                     0, HOSTILE_JSON)
    marker = "var ANALYSIS_DATA = "
    i = js.index(marker) + len(marker)
    _obj, end = json.JSONDecoder().raw_decode(js[i:])
    line = js[i:i + end]
    drv = tmp_path / "readback.cjs"
    drv.write_text("var ANALYSIS_DATA = %s;\nprocess.stdout.write(JSON.stringify("
                   "[ANALYSIS_DATA.quality.x, ANALYSIS_DATA.topic_clusters[0].items[0]]));\n"
                   % line, encoding="utf-8")
    r = subprocess.run(["node", str(drv)], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, "node 解析这行就挂了：%s" % (r.stderr or r.stdout)[-300:]
    got = json.loads(r.stdout.strip())
    assert got == [HOSTILE, HOSTILE], "JS 读回的值与出厂值不一致：%s" % got


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
