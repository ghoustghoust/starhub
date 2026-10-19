# -*- coding: utf-8 -*-
"""T3 快车道判据（A3 advisory）：
- 无新星 → 零文件写（三个状态文件都不动）；
- 有新星 → 工作树产物集合恰好是那三个状态文件，且不出现 index.html
  （2026-10-07 P0 摘掉快车道的 HTML 通路，站点唯一写者是 update.yml）；
- 拉取失败 → ::warning:: 退出 0，不炸 CI；
- 红线：不 import fetch_and_build 的 main（RSS/洞察/Vercel 不许被触发）。
网络全打桩；断言不写死日期。"""
import importlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fetch_and_build as fab  # noqa: E402
import fast_refresh as fr  # noqa: E402


def _repo(fn, desc="demo project"):
    owner, name = fn.split("/")
    return {"full_name": fn, "name": name, "owner": owner, "description": desc,
            "language": "Python", "stargazers_count": 42, "topics": ["ai"],
            "html_url": "https://github.com/%s" % fn,
            "pushed_at": "2026-09-01T00:00:00Z", "starred_at": "2026-10-01T00:00:00Z"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把快车道的读写都引到 tmp_path（ monkeypatch 模块常量）。"""
    monkeypatch.setattr(fr, "ROOT", str(tmp_path))
    monkeypatch.setattr(fr, "KC_PATH", str(tmp_path / "known_categories.json"))
    monkeypatch.setattr(fr, "NOTES_PATH", str(tmp_path / "known_notes.json"))
    monkeypatch.setattr(fr, "DESC_PATH", str(tmp_path / "descriptions_zh.json"))
    monkeypatch.setattr(fr, "_atomic_write_json", fr._atomic_write_json)
    # chdir 必须留着，且不是历史包袱：产物集合相等只看得见 tmp_path 里的东西，而本仓写盘
    # 的主流写法是相对路径（fetch_and_build.py 里 open("xxx.json","w") 一片）。哪天快车道
    # 新增一个用相对路径的状态文件，它会落在 pytest 的 cwd 里、既不进 env 也不被断言看见
    # ＝静默产物。chdir 到 env 就是把这类漏网文件强制收进被检查的那个目录。
    monkeypatch.chdir(tmp_path)
    known = {"old/one": "coding", "old/two": "video"}
    (tmp_path / "known_categories.json").write_text(json.dumps(known), encoding="utf-8")
    (tmp_path / "descriptions_zh.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_no_new_star_means_zero_writes(env, monkeypatch):
    monkeypatch.setattr(fab, "fetch_stars", lambda token: [_repo("old/one"), _repo("old/two")])
    before = sorted(os.listdir(env))
    rc = fr.main()
    assert rc == 0
    assert sorted(os.listdir(env)) == before          # 零新文件（index.html/三缓存都不动）


def test_no_new_star_logs_skip(env, monkeypatch, capsys):
    monkeypatch.setattr(fab, "fetch_stars", lambda token: [_repo("old/one")])
    assert fr.main() == 0
    assert "no change, skip write" in capsys.readouterr().out


def test_new_star_writes_exactly_three_state_files(env, monkeypatch, capsys):
    """有新星 ⇒ 工作树里**恰好**多出三个状态文件，绝不出现 index.html。

    产物集合用 sorted(os.listdir()) 相等来钉（封闭断言，不是"包含"）：加回任何产物——
    包括 2026-10-07 P0 摘掉的 index.html——都会立刻不等。这条是本文件唯一的正向锚，
    必须挂在"有新星"这条路径上：零写入那条走 fast_refresh 的早退分支，永远看不到产物，
    拿它当锚等于恒真。
    """
    monkeypatch.setattr(fab, "fetch_stars",
                        lambda token: [_repo("old/one"), _repo("brand/new", "a brand new thing")])
    monkeypatch.setattr(fab, "classify_llm",
                        lambda *a, **k: {"category": "tools", "note": "小工具一枚"})
    rc = fr.main()
    assert rc == 0
    assert sorted(os.listdir(env)) == [
        "descriptions_zh.json", "known_categories.json", "known_notes.json"], \
        "快车道的产物集合变了：多出来的每一项都意味着它又开始生产不该由它生产的东西"
    known = json.loads((env / "known_categories.json").read_text(encoding="utf-8"))
    assert known["brand/new"] == "tools"
    notes = json.loads((env / "known_notes.json").read_text(encoding="utf-8"))
    assert notes.get("brand/new") == "小工具一枚"
    desc = json.loads((env / "descriptions_zh.json").read_text(encoding="utf-8"))
    assert isinstance(desc, dict)
    assert "wrote 1" in capsys.readouterr().out


def test_fetch_failure_warns_and_exits_zero(env, monkeypatch, capsys):
    monkeypatch.setattr(fab, "fetch_stars", lambda token: None)
    assert fr.main() == 0
    out = capsys.readouterr().out
    assert "::warning::" in out and "拉取 star 失败" in out


def test_fast_refresh_never_imports_main_pipeline():
    """红线：快车道不许触发 RSS/洞察/Vercel——import fast_refresh 不产生任何产物文件。"""
    mod = importlib.reload(fr)
    assert hasattr(mod, "main")
    # fetch_and_build 的 main 不在 fast_refresh 的调用面里（源码静态断言）
    src = open(mod.__file__, encoding="utf-8").read()
    assert ".main(" not in src
