# -*- coding: utf-8 -*-
"""推送器必须把文本的 CRLF 归一成 LF，但**不许**动二进制。

为什么这条要有判据（而不是"代码里写了就算"）：
本仓所有远端写入都走 `tools/data_api_push.py`（本机 `git push` 不可用），而它是 `open(p,"rb")` **按字节**发 blob。
Windows 工作树里 Edit/IDE 落下的文件常是 CRLF ⇒ 一次"只改三行"的推送会变成**整个文件的假改动**，
远端 blob 从此与同仓其它文件行尾不一致。实测这种痕已经在库里：
`api/search.js` 的远端 blob 含 176 个 CRLF，而我逐个核对过的其余 21 个文件全是 LF。

两个方向都必须钉：
  · 文本要归一（不归一 = 静默翻转整个文件的历史行尾）；
  · 二进制不许动（无条件 `replace(b"\\r\\n", b"\\n")` 会毁掉 .png —— 仓库里真有 4 个）。
`req` 与 `gate` 全部打桩，本判据不发任何真实网络请求。
"""
import base64
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.environ.get("STARHUB_TOOLS_DIR") or os.path.join(ROOT, "tools"))
import data_api_push as D  # noqa: E402

TEXT = "template.html"          # 工作树里确实是 CRLF（见下方 assert，不是假设）
BINARY = "actions-step5-expanded.png"


class Api:
    """记录每个路径实际发出去的字节；contents 查询回同一 sha，让工具自己的复查判"内容一致"。"""

    def __init__(self):
        self.blobs = {}
        self.sha_of = {}
        self.current = None
        self.n = 0

    def __call__(self, method, path, body=None):
        if path.endswith("/git/blobs"):
            self.n += 1
            raw = base64.b64decode(body["content"])
            sha = "blob%02d" % self.n
            self.blobs[self.current] = raw
            self.sha_of[self.current] = sha
            return {"sha": sha}
        if path.endswith("/git/trees"):
            return {"sha": "tree01"}
        if path.endswith("/git/commits"):
            return {"sha": "commit01"}
        if method == "PATCH":
            return {"object": {"sha": "commit01"}}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "mainhead1"}}
        if "/contents/" in path:
            p = path.split("/contents/")[1].split("?")[0]
            if p not in (TEXT, BINARY):
                raise AssertionError("未预期的 contents 查询：%s" % p)
            # 读回远端 sha：与刚发的 blob sha 相同，工具的复查才会判"内容一致"
            return {"sha": self.sha_of.get(p, "blob01"), "content": ""}
        raise AssertionError("未预期的请求：%s %s" % (method, path))


def _drive(paths, api, monkeypatch, tmp_path):
    monkeypatch.setattr(D, "req", api)
    monkeypatch.setattr(D, "gate", lambda: (True, "测试守门通过"))
    monkeypatch.setattr(D, "prod_gate", lambda: (True, "测试：生产验证通过"))
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.setattr(D, "risky_runs", lambda ts: [])
    msg = tmp_path / "msg.txt"
    msg.write_text("测试提交信息\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["data_api_push.py"] + list(paths) + ["--msg-file", str(msg)])
    return D.main()


def test_crlf_text_is_normalized_before_it_reaches_the_blob(monkeypatch, tmp_path):
    # 原来这里直接读工作树的 template.html 并 assert 它是 CRLF——那等于把判据的成立与否
    # 交给"上一个碰这个文件的人用了什么行尾"（10-08 实测：Edit/python 以 LF 落盘之后，
    # 这条判据就只剩"拒绝执行"，而它拒绝的那个条件恰恰是它要测的形状）。
    # 现在**自己造** CRLF 输入：取真实文件的字节，强制转成 CRLF，再把工具的读文件入口指过去。
    # 判据的语义不变（工具必须把它归一回 LF），但不再依赖本地工作树状态，也不会空跑：
    # 归一逻辑一旦被删，发出去的字节就带 \r\n ⇒ 下面第一条断言立刻红。
    # 先归一再翻成 CRLF：本机 core.autocrlf=true，工作树那份可能本来就是 CRLF，
    # 直接 replace(b"\n", b"\r\n") 会造出 \r\r\n —— 归一一步之后仍残留 \r\n ⇒ 假红（对抗审查抓到）。
    disk = open(os.path.join(ROOT, TEXT), "rb").read().replace(b"\r\n", b"\n")
    crlf_path = tmp_path / TEXT
    crlf_path.write_bytes(disk.replace(b"\n", b"\r\n"))
    fixture = crlf_path.read_bytes()
    assert b"\r\n" in fixture and b"\r\r" not in fixture, \
        "造的 CRLF 夹具无效（出现 \\r\\r 说明源里本来就带 CR）⇒ 这条会退化成假红或空跑"
    monkeypatch.setattr(D, "_abs", lambda p: str(crlf_path))
    api = Api()
    api.current = TEXT
    rc = _drive([TEXT], api, monkeypatch, tmp_path)
    assert rc == 0, "推送流程自身没跑通（rc=%s）⇒ 下面的字节断言不作数" % rc
    sent = api.blobs[TEXT]
    assert b"\r\n" not in sent, (
        "发出去的 blob 仍含 CRLF ⇒ 远端会整文件翻转行尾（`api/search.js` 就是这种痕）")
    assert sent == disk.replace(b"\r\n", b"\n"), "归一后的字节应等于「CRLF 全部换成 LF」的结果，实得不同"


def test_binary_bytes_are_sent_untouched(monkeypatch, tmp_path):
    disk = open(os.path.join(ROOT, BINARY), "rb").read()
    assert D.is_binary_bytes(disk), (
        "%s 不再被识别为二进制 ⇒ 归一逻辑会开始动它，先修分类再谈这条判据" % BINARY)
    api = Api()
    api.current = BINARY
    rc = _drive([BINARY], api, monkeypatch, tmp_path)
    assert rc == 0
    assert api.blobs[BINARY] == disk, (
        "二进制被改写了（无条件 CRLF→LF 会毁掉 .png，仓库里真有 4 个图片类文件）")
