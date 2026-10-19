# -*- coding: utf-8 -*-
"""D1 嵌入缓存 TDD 测试（spec: docs/superpowers/specs/2026-09-14-insight-optimization-spec.md §5-D1）。

全部 mock SiliconFlow API（零网络）。运行：python test_insight_embeddings.py，退出码 0 = 全过。
"""
import hashlib
import io
import json
import os
import sys
import tempfile

import build_rss_aggregator as _m  # noqa: F401  (确保模块可导入)
import insight_engine as ie

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if (detail and not cond) else ""))


class Env:
    """隔离沙箱：临时缓存文件 + API 调用计数 + 确定性假向量。"""

    def __init__(self, texts_meta=None):
        self.tmp = tempfile.mkdtemp(prefix="_t_emb_")
        self.cache_path = os.path.join(self.tmp, "emb_cache.json")
        self.api_calls = []          # 每次批量调用的 input 文本列表
        self._m = ie

    def vec_for(self, text, dim=8):
        h = hashlib.md5(text.encode("utf-8")).digest()
        return [b / 255.0 for b in h[:dim]]

    def mock_urlopen(self, req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        inputs = payload["input"]
        self.api_calls.append(list(inputs))
        data = [{"embedding": self.vec_for(t), "index": i} for i, t in enumerate(inputs)]
        resp = json.dumps({"data": data}).encode("utf-8")

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return resp

        return R()

    def install(self):
        self._m._SF_KEY = "test-key"
        self._m._SF_EMBED_URL = "https://mock/siliconflow/embeddings"
        self._m._SF_EMBED_MODEL = "BAAI/bge-m3"
        self._m._EMBED_CACHE_FILE = self.cache_path
        self._m._EMBED_CACHE.clear() if hasattr(self._m, "_EMBED_CACHE") else None
        self._m.urllib.request.urlopen = self.mock_urlopen
        self._m.FASTEMBED_AVAILABLE = False  # 隔离本地回退，专注 SF 路径

    def reset(self):
        self.api_calls = []


def main():
    env = Env()
    env.install()
    m = env._m

    # ── T-D1-1 同文本两次调用 → API 恰 1 次批量、向量一致 ──
    texts = ["Alpha 关于智能体的分析", "Beta 模型评测报告"]
    v1, model1 = m._get_embeddings(list(texts))
    calls_after_first = len(env.api_calls)
    v2, _ = m._get_embeddings(list(texts))
    ok = (calls_after_first == 1 and len(env.api_calls) == 1
          and v1 == v2 and v1 is not None)
    check("T-D1-1 同文本两次调用 API 恰 1 次、向量一致", ok,
          {"calls": env.api_calls})

    # ── T-D1-2 混合批：50% 命中只调未命中部分，顺序保持 ──
    env.reset()
    mixed = ["Alpha 关于智能体的分析", "NEW-Gamma 推理成本暴跌", "Beta 模型评测报告", "NEW-Delta 开源权重"]
    vecs, _ = m._get_embeddings(list(mixed))
    sent = env.api_calls[-1] if env.api_calls else []
    ok = (sent == ["NEW-Gamma 推理成本暴跌", "NEW-Delta 开源权重"]
          and len(vecs) == 4
          and vecs[0] == env.vec_for(mixed[0]) and vecs[2] == env.vec_for(mixed[2])
          and vecs[1] == env.vec_for(mixed[1]) and vecs[3] == env.vec_for(mixed[3]))
    check("T-D1-2 混合批只调未命中、顺序严格一致", ok,
          {"sent": sent, "n_vecs": len(vecs or [])})

    # ── T-D1-3 缓存持久化 → 模拟新进程（重建模块级缓存状态）零调用命中 ──
    #  实现：缓存写入文件 + 模块加载时读取。此处通过重新 install（清内存态）模拟新进程，
    #  但保留同一 cache_path 文件。
    env.reset()
    env._m._EMBED_CACHE.clear() if hasattr(env._m, "_EMBED_CACHE") else None
    env._m._EMBED_CACHE_FILE = env.cache_path
    m2_texts = ["Alpha 关于智能体的分析"]
    vecs, _ = m._get_embeddings(list(m2_texts))
    ok = len(env.api_calls) == 0 and vecs is not None and vecs[0] == env.vec_for(m2_texts[0])
    check("T-D1-3 缓存持久化后新进程零调用命中", ok,
          {"calls": env.api_calls})

    # ── T-D1-4 缓存文件损坏 → 忽略缓存全量重算 ──
    with open(env.cache_path, "w", encoding="utf-8") as f:
        f.write("{corrupted!!!")
    env.reset()
    vecs, _ = m._get_embeddings(["新文本-损坏缓存场景"])
    ok = vecs is not None and len(env.api_calls) >= 1
    check("T-D1-4 损坏缓存降级重算不炸", ok)

    # ── T-D1-5 模型名变更 → 旧缓存失效 ──
    env.reset()
    m._SF_EMBED_MODEL = "BAAI/other-model"
    m._EMBED_CACHE_FILE = env.cache_path
    calls_before = len(env.api_calls)
    vecs, _ = m._get_embeddings(["Alpha 关于智能体的分析"])
    ok = vecs is not None and len(env.api_calls) > calls_before
    check("T-D1-5 模型变更使旧缓存失效重算", ok)

    # ── T-D1-6 超容量 LRU 淘汰 ──
    env.reset()
    m._EMBED_CACHE_FILE = env.cache_path
    m._EMBED_CACHE_MAX = 5
    m._get_embeddings([f"bulk-{i}" for i in range(8)])
    size_ok = True
    cache = json.load(open(env.cache_path, encoding="utf-8"))
    size_ok = len(cache.get("vectors", {})) <= 5
    ok = size_ok
    check("T-D1-6 缓存容量 LRU 封顶", ok, {"size": len(cache.get("vectors", {}))})

    print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("FAILED:", name, detail)
        sys.exit(1)


if __name__ == "__main__":
    main()
