# -*- coding: utf-8 -*-
"""维度护栏 v3 测试（rev10 处方验证）。

V3-1: 纯未命中调用 + 异维 API → 漂移检出 → 缓存文件失效（不再污染写入）
V3-2: 已污染缓存（混维）+ 混合调用 → 漂移检出 → 缓存失效自愈
V3-3: 健康缓存 → 纯命中/部分命中/混合全部正常（无误杀）
"""
import hashlib
import io
import json
import os
import sys
import tempfile

import insight_engine as ie

# Windows GBK 控制台无法打印 ✓/✗，会让全部通过的用例以 exit=1 收场
try:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass


def md5k(t):
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def setup(cache_vectors):
    ie._SF_KEY = "k"
    ie._SF_EMBED_MODEL = "BAAI/bge-m3"
    # _get_embeddings 的 fastembed 回退分支（insight_engine.py:664）不受 _SF_KEY 门控，
    # 而 CI 会 pip install fastembed；不关掉它，V3-1/V3-2 的 assert vecs is None 在 CI 必红。
    ie.FASTEMBED_AVAILABLE = False
    cache_file = os.path.join(tempfile.mkdtemp(), "emb_cache.json")
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump({"model": "BAAI/bge-m3", "vectors": cache_vectors}, f)
    ie._EMBED_CACHE_FILE = cache_file
    return cache_file


def mock_dim(dim, mix=None):
    """返回 urlopen mock：所有新文本 embedding 均为 dim 维；mix 提供按序覆盖。"""
    state = {"i": 0}

    def handler(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        data = []
        for i, t in enumerate(payload["input"]):
            d = dim
            if mix and state["i"] < len(mix):
                d = mix[state["i"]]
            state["i"] += 1
            data.append({"embedding": [0.5] * d, "index": i})
        resp = json.dumps({"data": data}).encode()

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return resp

        return R()

    return handler


def main():
    orig_urlopen = ie.urllib.request.urlopen
    orig_file = ie._EMBED_CACHE_FILE

    # ── V3-1: 纯未命中 + 异维 API → 检出并失效缓存 ──
    cache_file = setup({md5k("旧"): [0.1] * 8})
    ie.urllib.request.urlopen = mock_dim(16)
    vecs, _ = ie._get_embeddings(["全新的热榜标题甲", "全新的热榜标题乙"])
    assert vecs is None, "V3-1 FAIL: 漂移应弃用本轮（走 fastembed 回退）"
    assert not os.path.exists(cache_file), "V3-1 FAIL: 缓存文件未失效"
    print("PASS V3-1 纯未命中+异维: 漂移检出、缓存失效、不再污染")

    # ── V3-2: 已污染缓存（混维）+ 混合调用 → 自愈 ──
    cache_file = setup({md5k("污染甲"): [0.1] * 8, md5k("污染乙"): [0.2] * 16})
    ie.urllib.request.urlopen = mock_dim(8, mix=[16])  # 混合调用触发漂移检出
    vecs, _ = ie._get_embeddings(["污染甲 新查询", "新查询乙"])
    assert vecs is None, "V3-2 FAIL: 漂移应弃用"
    assert not os.path.exists(cache_file), "V3-2 FAIL: 污染缓存未自愈"
    print("PASS V3-2 已污染缓存: 漂移检出并失效自愈 ✓")

    # ── V3-3: 健康缓存无误杀（纯命中/部分命中）──
    cache_file = setup({md5k("健康甲"): [0.1] * 8, md5k("健康乙"): [0.2] * 8})
    ie.urllib.request.urlopen = mock_dim(8)
    vecs, _ = ie._get_embeddings(["健康甲"])  # 纯命中
    assert vecs is not None and vecs[0] == [0.1] * 8, "V3-3 FAIL: 纯命中被误杀"
    vecs, _ = ie._get_embeddings(["健康甲", "新条目"])  # 部分命中（批量补齐）
    assert vecs is not None and len(vecs) == 2, "V3-3 FAIL: 部分命中被误杀"
    assert os.path.exists(cache_file), "V3-3 FAIL: 健康缓存不应失效"
    print("PASS V3-3 健康缓存: 纯命中/部分命中均正常 ✓")

    ie.urllib.request.urlopen = orig_urlopen
    ie._EMBED_CACHE_FILE = orig_file
    print("维度护栏 v3 全部通过")


if __name__ == "__main__":
    main()
