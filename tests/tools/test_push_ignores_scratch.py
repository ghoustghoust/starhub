# -*- coding: utf-8 -*-
"""推送工具不许把 .gitignore 排除的 Scratch 件带进远端树（判据先红）。

起因是实测：远端树里躺着一个 411,404 B 的 `_check_js_temp.js`，`.gitignore` 明明排除它
（生效规则是 `_check_*`），它却成了永久历史 —— 而历史不可回收（用户已明确不再第三次重写）。
根因在 `tools/data_api_push.py`：`list_files()` 用 `os.walk` 展开目录，只排除
`__pycache__`/`.pyc`，**根本不查 ignore 规则**。所以"按目录推"这个用法本身就是漏口。

这里打在行为层：构造一个临时仓（含被忽略的 scratch 与正常文件），直接调用工具里的展开函数，
要求 ① 目录展开不含被忽略项、② 显式点名被忽略项要**报错而不是静默上传**、③ 有显式开关才能放行。
"""
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")


def _load():
    spec = importlib.util.spec_from_file_location("dap", os.path.join(TOOLS, "data_api_push.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["dap"] = m
    spec.loader.exec_module(m)
    return m


def _sandbox(tmp_path):
    """造一个真 git 仓：一个被忽略的 scratch、一个正常文件、一个子目录里的被忽略项。"""
    d = str(tmp_path)
    subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
    for k, v in (("user.name", "gate"), ("user.email", "gate@example.invalid"),
                 ("commit.ggpsign", "false"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=d, check=True)
    open(os.path.join(d, ".gitignore"), "w", encoding="utf-8").write("_scratch_*\nsub/_ignored.js\n")
    open(os.path.join(d, "_scratch_a.js"), "w").write("junk" * 40)
    open(os.path.join(d, "keep.py"), "w").write("print(1)\n")
    os.makedirs(os.path.join(d, "sub"), exist_ok=True)
    open(os.path.join(d, "sub", "_ignored.js"), "w").write("junk" * 40)
    open(os.path.join(d, "sub", "real.py"), "w").write("print(2)\n")
    return d


def test_directory_expansion_drops_ignored_paths(tmp_path, monkeypatch):
    m = _load()
    d = _sandbox(tmp_path)
    monkeypatch.setattr(m, "ROOT", d)
    got = sorted(m.expand_paths(["."]))
    assert "keep.py" in got and "sub/real.py" in got, got
    leaked = [p for p in got if "_scratch_a.js" in p or "_ignored.js" in p]
    assert not leaked, "按目录展开把被 .gitignore 排除的 Scratch 件也列进来了：%s" % (leaked,)


def test_explicit_ignored_path_is_refused_not_uploaded(tmp_path, monkeypatch):
    m = _load()
    d = _sandbox(tmp_path)
    monkeypatch.setattr(m, "ROOT", d)
    refused = getattr(m, "refused_ignored", None)
    assert callable(refused), "工具没有「哪些点名路径被 ignore 拦下」的出口 ⇒ 只能静默上传或静默跳过，两者都不可审计"
    bad = refused(["_scratch_a.js", "keep.py"])
    assert bad == ["_scratch_a.js"], bad
    assert refused(["keep.py"]) == [], "正常文件不该被拦"
    assert refused(["sub/_ignored.js"]) == ["sub/_ignored.js"], "子目录里的被忽略项也要拦住"


def test_cli_refuses_an_ignored_path_and_honours_the_flag(tmp_path):
    """调用点判据：只看函数层会漏 —— 开关必须在 `main()` 的出口真被用到，否则照样静默上传。

    工具的 git 调用锚在它自己所在的仓（这是设计，不为测试而改），
    所以端到端只能在**真仓**里跑：造一个命中既有 ignore 规则（`_check_*`）的探针文件，
    走 `--dry-run`（不写远端），要求非零退出并点名；随后 `--allow-ignored` 必须放行。
    探针文件在 finally 里删掉。
    """
    probe = "_check_probe_%d.js" % os.getpid()
    p_path = os.path.join(ROOT, probe)
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", probe],
                          cwd=ROOT).returncode == 0, "探针没命中 ignore 规则 ⇒ 这条测试无效"
    open(p_path, "w", encoding="utf-8").write("探针" * 20)
    tool = os.path.join(TOOLS, "data_api_push.py")
    try:
        p = subprocess.run([sys.executable, tool, probe, "--dry-run"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        assert p.returncode != 0, "CLI 把被忽略的点名文件当正常路径放行了：" + out[-200:]
        assert "拒绝推送被 .gitignore 排除的路径" in out, out[-240:]
        assert probe in out, "没点名是哪个路径被拦"

        q = subprocess.run([sys.executable, tool, probe, "--dry-run", "--allow-ignored"],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        qout = (q.stdout or "") + (q.stderr or "")
        assert "拒绝推送被 .gitignore 排除的路径" not in qout, "开关没生效：" + qout[-240:]
    finally:
        os.remove(p_path)
        assert not os.path.exists(p_path)


def test_already_tracked_but_ignored_is_still_named(tmp_path, monkeypatch):
    """"被忽略却已跟踪"这一类必须照样拦 —— 它正是 `_check_js_temp.js` 的形态。

    也是 `--no-index` 的承重证明：`git check-ignore` 默认先看索引，已跟踪的文件一律答"不忽略"，
    少了这个开关，这道闸就正好放过我们要拦的那一类。
    """
    m = _load()
    d = _sandbox(tmp_path)
    monkeypatch.setattr(m, "ROOT", d)
    subprocess.run(["git", "add", "-f", "_scratch_a.js"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "s"], cwd=d, check=True)
    got = m.refused_ignored(["_scratch_a.js", "keep.py"])
    assert got == ["_scratch_a.js"], "已跟踪的被忽略项没被拦（多半是 --no-index 丢了）：%s" % (got,)


def test_opt_in_flag_is_the_only_way_through(tmp_path, monkeypatch):
    """必须存在显式开关（否则真需要推某个被忽略项时就只能改工具），且默认关闭。"""
    src = open(os.path.join(TOOLS, "data_api_push.py"), encoding="utf-8").read()
    assert "allow-ignored" in src or "allow_ignored" in src, "没有 --allow-ignored 这类显式开关"
    m = _load()
    d = _sandbox(tmp_path)
    monkeypatch.setattr(m, "ROOT", d)
    got = sorted(m.expand_paths(["."], allow_ignored=True))
    assert any("_scratch_a.js" in p for p in got), "开关没生效：%s" % (got,)
