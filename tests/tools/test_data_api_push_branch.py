# -*- coding: utf-8 -*-
"""`--ref` 分支推送：夜场验证与白天构建必须解耦。

依据不是"想要快"，而是用户一路坚持的"独立"：验证一场新代码，凭什么要等白天那场
35~45 分钟的构建腾出缝？构建窗口那道闸只为 main 存在（update.yml 的 Commit 步骤会
`reset --hard origin/main` 回滚中途的 main 推送），分支根本不在它的管辖里。
"""
import base64
import io
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import data_api_push as D  # noqa: E402

FILE = "tests/tools/test_data_api_push_branch.py"


class FakeApi:
    """按调用序列记账；分支的 ref 读取一律 404，逼出"建分支"那条路。"""

    def __init__(self):
        self.calls = []
        self.n = 0

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if path.endswith("/git/blobs"):
            self.n += 1
            return {"sha": "blob%02d" % self.n}
        if path.endswith("/git/trees"):
            return {"sha": "tree01"}
        if path.endswith("/git/commits"):
            return {"sha": "commit01"}
        if method == "POST" and path.endswith("/git/refs"):
            return {"object": {"sha": "commit01"}}
        if method == "PATCH":
            return {"object": {"sha": "commit01"}}
        if "/git/ref/heads/main" in path:
            return {"object": {"sha": "mainhead1"}}
        if "/git/ref/heads/" in path:
            raise urllib.error.HTTPError(path, 404, "not found", None, None)
        if "/contents/" in path:
            raw = io.open(os.path.join(ROOT, FILE), "rb").read().replace(b"\r\n", b"\n")
            return {"content": base64.b64encode(b"x" * 8).decode(), "sha": "blob01"}
        raise AssertionError("未预期的请求：%s %s" % (method, path))


def _drive(argv, monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(D, "req", api)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.setattr(D, "risky_runs", lambda ts: [])
    slept = []
    monkeypatch.setattr(D.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(sys, "argv", ["data_api_push.py"] + argv)
    code = D.main()
    return code, api, slept


def test_branch_push_never_asks_the_build_window_gate(monkeypatch):
    """推分支不许过 gate()，也不许睡那 90 秒 —— 那两样都是 main 专属的回滚防护。"""
    def boom():
        raise AssertionError("分支推送不该查构建窗口：夜场验证又被白天场绑住了")
    monkeypatch.setattr(D, "gate", boom)
    code, api, slept = _drive(["--ref", "insight-verify", "--msg", "m", FILE], monkeypatch)
    assert code == 0, code
    assert ("POST", D.REPO + "/git/refs") in api.calls, \
        "分支不存在时应以 main 为父建 ref，实为 %s" % api.calls
    assert [c for c in api.calls if c[0] == "PATCH"] == [], "不许 PATCH main 的 ref"
    assert 90 not in slept, "分支推送不该等 run 列表落定"
    # 复查必须跟着 ref 走：不带 ?ref 的 contents 永远读默认分支，
    # 会把一次成功的分支推送报成"内容不同"（这个坑是加 --ref 当天实撞的）。
    reads = [p for m, p in api.calls if m == "GET" and "/contents/" in p]
    assert reads and all("ref=insight-verify" in p for p in reads), reads


def test_main_push_still_asks_the_gate(monkeypatch):
    """反向对照：闸不能因为加了 --ref 就形同不存在 —— 推 main 仍必须过闸。"""
    asked = []
    monkeypatch.setattr(D, "gate", lambda: (asked.append(1) or (False, "有构建在跑")))
    code, api, _slept = _drive(["--msg", "m", FILE], monkeypatch)
    assert asked, "推 main 却没查构建窗口"
    assert code == 1, "守门不过必须拒绝，实为放行"
