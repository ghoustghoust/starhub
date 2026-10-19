# tests/rss_history/test_stale_chunk_guard.py
# -*- coding: utf-8 -*-
"""僵尸分块守卫：上一场多出来的旧 rss-data-N.js 必须被**清空**，不能被删除。

真实事故（2026-09-21 定位）：`write_data_chunks` 用 `os.remove` 删掉多余的旧块，而 CI 的
提交步骤是 `git add ... rss-data-*.js`（按磁盘 glob 展开）。文件被删掉就匹配不到 ⇒
删除动作永远进不了提交 ⇒ 旧块永久留在仓库里、被 Pages/Vercel 继续分发、页面照样合并进来。
实测后果：闸门当场上报"出厂 9,045 条、>168h 0 条"，而产物里还挂着 11,486 条旧数据
（5,060 条 >168h、12 条 pub_date=0001-01-01）—— 用户看到的"超过 72 小时没出去"就是它。

留着一个空文件才是可提交的"变更"，glob 会把它带上；内容空了，页面合并进来也没有旧条目。

**2026-09-30 之后，"为什么仍然要清空、不能删"换了理由**：Pages 改由本 workflow 的 artifact 发布
（update.yml 的 Stage Pages site → upload-pages-artifact），大分块不再进 git，上面那条 glob 陷阱
在提交侧已经不存在。但行为必须保持清空：页面按索引逐个取 `rss-data-N.js`，删文件就是 404；
而 staging 取的是 `git ls-files` 清单，旧块此刻仍被跟踪，照删等于把它从站点里抹掉。
别再把"git add glob"当成依据改回 os.remove —— 那是本守卫要防的第一次事故，也是第二次。
"""
import datetime
import io
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()


def _payload(path):
    text = io.open(path, encoding="utf-8").read()
    body = text[text.index("{", text.index("=")):].rstrip()
    if body.endswith(";"):
        body = body[:-1]
    # 写出器对"空块"用的是 JS 对象字面量 {sources:[]}（键不带引号），
    # 浏览器能吃，严格 JSON 不能吃 —— 这里按 JS 的写法容忍，不去改产物格式。
    try:
        return json.loads(body)
    except ValueError:
        return json.loads(body.replace("{sources:", chr(123) + chr(34) + "sources" + chr(34) + ":"))


def _items(path):
    d = _payload(path)
    return [it for s in d.get("sources", []) for it in s.get("items", [])]


def _stale_chunk(path, idx):
    """伪造上一场的旧块：条目是几年前的，pub_date 明显超窗。"""
    old = (datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))) - datetime.timedelta(days=400))
    body = {"sources": [{"key": "stale", "name": "stale", "items": [
        {"title": "old %d" % j, "link": "http://stale/%d-%d" % (idx, j),
         "pub_date": old.isoformat()} for j in range(4)]}]}
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("/* stale */\n")
        f.write("(window.__CHUNKS=window.__CHUNKS||[])[%d]=%s;\n"
                % (idx, json.dumps(body, ensure_ascii=False)))


@pytest.fixture
def tmp_out(monkeypatch, tmp_path):
    # write_data_chunks 的落点 = dirname(abspath(OUT))，OUT 是相对路径 ⇒ chdir 就能改落点
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "RSS_SOURCES", [])
    return tmp_path


def _fresh_sources(n=5):
    now = mod._now_bj()
    items = [{"title": "new %d" % i, "link": "http://fresh/%d" % i,
              "pub_date": (now - datetime.timedelta(hours=1 + i)).isoformat(),
              "summary": "", "image": ""} for i in range(n)]
    return [{"key": "a", "name": "A", "cat": "ai", "color": "#fff", "tier": 1,
             "items": items}]


def test_stale_chunks_are_emptied_not_deleted(tmp_out):
    """旧块不许消失：它必须还在磁盘上（否则 git add 的 glob 看不见），且内容为空。"""
    _stale_chunk(str(tmp_out / "rss-data-2.js"), 2)
    _stale_chunk(str(tmp_out / "rss-data-7.js"), 7)
    assert len(_items(str(tmp_out / "rss-data-2.js"))) == 4

    mod.write_data_chunks(_fresh_sources())

    for name in ("rss-data-2.js", "rss-data-7.js"):
        p = str(tmp_out / name)
        assert os.path.exists(p), (
            "%s 被删掉了：CI 的 `git add rss-data-*.js` 匹配不到删除，旧数据会永久留在仓库继续分发" % name)
        assert _items(p) == [], "%s 还带着旧条目 ⇒ 页面仍会合并出超窗内容" % name


def test_current_chunks_still_carry_this_builds_items(tmp_out):
    """清空旧块不能把本次数据也弄丢：0/1 号块的条目并集必须正好是本次出厂集合。"""
    _stale_chunk(str(tmp_out / "rss-data-3.js"), 3)
    src = _fresh_sources(7)
    want = {i["link"] for i in src[0]["items"]}
    mod.write_data_chunks(src)
    got = set()
    for name in sorted(os.listdir(str(tmp_out))):
        if name.startswith("rss-data-") and name.endswith(".js"):
            got |= {i["link"] for i in _items(str(tmp_out / name))}
    assert got == want, "产物与本次出厂集合不一致：多 %s 少 %s" % (
        sorted(got - want)[:3], sorted(want - got)[:3])


def test_cleanup_no_longer_calls_os_remove(tmp_out):
    """源码层锁：清理旧块那段不许再用 os.remove（换成清空是这次修复的本体）。"""
    import inspect
    text = inspect.getsource(mod.write_data_chunks)
    assert "os.remove" not in text, (
        "又用 os.remove 删旧块了：删除进不了 CI 的 glob 提交，等于把僵尸分块放回去")
