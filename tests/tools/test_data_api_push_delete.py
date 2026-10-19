# -*- coding: utf-8 -*-
"""`--delete` 判据：删除必须进 tree、必须复查，且不允许"又推又删"。

为什么要有这个能力：本机 `git push` 不可用（见 env-git-identity-and-remote-commits），
所有远端写入都走 `tools/data_api_push.py`，而它原先只会新增/覆盖，**不会删**。
于是要删一个文件时，唯一"看得见"的半边会被推上去（比如 vercel.json 去掉了声明），
函数文件却还躺在远端 —— 半拉子状态比不删更难发现。

判据三向都钉：
  · tree 里必须出现 `sha: null` 的那一项（否则远端根本没删）
  · 复查必须真的去问 contents：200 = 没删掉 = 判红；404 = 已删 = 绿
  · 同一文件既推又删 = 未定义行为，直接拒
"""
import io
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 变异体要指向副本目录（同 RSS_BUILD_SRC / STARHUB_UPDATE_YML 的约定）：
# 不加这个覆盖，变异脚本会"跑了但改的东西根本没生效"，然后把存活误报成判据无效。
sys.path.insert(0, os.environ.get("STARHUB_TOOLS_DIR") or os.path.join(ROOT, "tools"))
import data_api_push as D  # noqa: E402

KEEP = "vercel.json"
GONE = "api/build_log.js"


class Api:
    """记录 tree 载荷；contents 查询按 gone_paths 决定 404 还是 200。"""

    def __init__(self, deleted_ok=True):
        self.tree_items = None
        self.deleted_ok = deleted_ok
        self.n = 0

    def __call__(self, method, path, body=None):
        if path.endswith("/git/blobs"):
            self.n += 1
            return {"sha": "blob%02d" % self.n}
        if path.endswith("/git/trees"):
            self.tree_items = body["tree"]
            return {"sha": "tree01"}
        if path.endswith("/git/commits"):
            return {"sha": "commit01"}
        if method == "PATCH":
            return {"object": {"sha": "commit01"}}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "mainhead1"}}
        if "/contents/" in path:
            p = path.split("/contents/")[1].split("?")[0]
            if p == GONE and self.deleted_ok:
                raise urllib.error.HTTPError(path, 404, "not found", None, None)
            return {"sha": "blob01", "content": ""}
        raise AssertionError("未预期的请求：%s %s" % (method, path))


def _drive(argv, api, monkeypatch, tmp_path):
    monkeypatch.setattr(D, "req", api)
    monkeypatch.setattr(D, "gate", lambda: (True, "测试守门通过"))
    monkeypatch.setattr(D, "prod_gate", lambda: (True, "测试：生产验证通过"))
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    monkeypatch.setattr(D, "risky_runs", lambda ts: [])
    msg = tmp_path / "msg.txt"
    msg.write_text(u"测试：删除死端点", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["data_api_push.py"] + argv + ["--msg-file", str(msg)])
    return D.main()


def test_delete_put_a_null_sha_into_the_tree(monkeypatch, tmp_path):
    api = Api(deleted_ok=True)
    rc = _drive([KEEP, "--delete", GONE], api, monkeypatch, tmp_path)
    assert rc == 0, rc
    nulls = [it for it in api.tree_items if it.get("sha") is None]
    assert [it["path"] for it in nulls] == [GONE], (
        "tree 里没有 sha:null 的删除项，远端不会被删：%s" % api.tree_items)
    kept = [it for it in api.tree_items if it.get("sha") not in (None,)]
    assert kept and all(it["path"] == KEEP for it in kept), "正常路径的项被带坏了：%s" % kept


def test_delete_is_reverified_and_reports_a_surviving_file(monkeypatch, tmp_path):
    """删除没生效时工具必须报红 —— 否则它一边报"内容不同=0"一边留着那个文件。"""
    api = Api(deleted_ok=False)
    out = io.StringIO()
    real = sys.stdout
    sys.stdout = out
    try:
        rc = _drive([KEEP, "--delete", GONE], api, monkeypatch, tmp_path)
    finally:
        sys.stdout = real
    assert rc == 1, "远端文件还在，工具却判成功：rc=%d\n%s" % (rc, out.getvalue()[-800:])
    assert "删除未生效" in out.getvalue(), out.getvalue()[-800:]


def test_same_path_cannot_be_pushed_and_deleted(monkeypatch, tmp_path):
    api = Api()
    rc = _drive([GONE, "--delete", GONE], api, monkeypatch, tmp_path)
    assert rc == 1, "同一个路径既推又删必须有定义行为，不能默默选一个"
    assert api.tree_items is None, "被拒之后不许已经构造 tree：%s" % api.tree_items
