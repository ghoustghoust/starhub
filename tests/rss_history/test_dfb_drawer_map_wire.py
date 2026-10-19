# -*- coding: utf-8 -*-
"""抽屉实时刷新不许把构建期字段抹掉（任务 #30 的 C1 修正，门禁 A2）。

现场（fresh-eyes 审查 C1，已逐行核对）：
`selectSrc` 拿到 `/api/rss?source=KEY` 之后，会把"这次 API 里出现过的 link"对应的
旧条目**整条丢掉**，换成 `_apiItToSrc` 现造的对象。而 `?source=` 这一路
（api/rss.js 的 fetchOne）只发 t/u/s/d(+fc/mu/mt)，**不发封面、不发话题标签、
不发坏日期标记、也不做标题翻译** ⇒ 被换出来的条目比原来那一条差：

- 卡片封面消失（buildArt 读 `image||img`，API 侧恒空）
- 话题标签消失（buildArt 读 `tags`）
- 坏日期标记消失（`bad_date` 丢了就退回相对时间显示）
- 中文标题/摘要退回英文原文（API 这一路不产 t_zh）
- 更糟：上游这次没给 pubDate 时，`d` 是抓取时刻，会把构建期已知的**真实**发布时间
  覆盖成"刚刚"（与留存契约相反，且 lib/rss_retention.js 见 age≈0 就永不淘汰）

所以映射必须是"以 API 的新字段覆盖、以旧条目的独有字段兜底"的合并，而不是替换。

判据全部**真跑**函数（从模板抠源码交给 node），不做文本子串断言。
"""
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TPL = os.path.join(ROOT, "build_rss_aggregator.py")
FN = "function _apiMergeTo(oldItems){"
CALL = "var newItems=data.items.map(_apiMergeTo(src.items));"


def _node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


def _brace_slice(text, i):
    depth = 0
    for j in range(i + len(FN) - 1, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError("_apiMergeTo 大括号没配平，模板被改坏了")


def _extract(path=TPL):
    text = open(path, encoding="utf-8").read()
    return _brace_slice(text, text.index(FN))


def _run_js(body, tmp_path):
    drv = tmp_path / "drawer_merge.cjs"
    drv.write_text(body + "\n", encoding="utf-8")
    r = subprocess.run(["node", str(drv)], capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, "node 跑挂: %s %s" % (r.stdout[-400:], r.stderr[-400:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def _merge(old_items, api_item, tmp_path):
    return _run_js(
        _extract() + "\nprocess.stdout.write(JSON.stringify("
        "_apiMergeTo(%s)(%s)));" % (json.dumps(old_items, ensure_ascii=False),
                                    json.dumps(api_item, ensure_ascii=False)), tmp_path)


# 一条"构建期该有的都有"的旧条目（chunk 通道的出厂形状，键名照线上产物抄）
OLD_FULL = {
    "title": "English Title", "title_zh": "中文标题", "summary": "en summary",
    "summary_zh": "中文摘要", "link": "http://e/1", "pub_date": "2026-09-20T01:00:00+08:00",
    "image": "http://cdn/cover.jpg",
    "media_url": "https://cdn/audio.mp3", "media_type": "audio/mpeg",
    "tags": ["大模型", "开源"], "bad_date": True, "full_content": "长文正文",
}
# ?source= 这一路**实际**会发的字段：线上实测三个源的条目键只有 t/u/s/d
# （fc/mu/mt 只在有值时才发，且 `fetchOne` 压根不发 img、tags、bad_date、翻译）。
# 故意不给 fc：上一版 fixture 塞了 fc，等于替实现承认"API 会发正文"，
# 于是"刷新后正文被清空"这一条真实缺陷被测试钉成了正确（2026-09-21 第二轮审查 C2）。
API_LIVE = {"t": "English Title", "u": "http://e/1", "s": "en summary",
            "d": "2026-09-20T01:00:00+08:00"}


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_live_refresh_keeps_build_time_fields(tmp_path):
    """刷新之后封面/标签/坏日期标记/中文标题/全文都还在（C1 + C2 主判据）。

    契约变更（2026-09-30）：`time_str` 不再进产物（ITEM_DERIVED_FIELDS），所以这里
    不再断言"它被搬过来了"——改成两条更强的：① `pub_date` 必须原样保住（标签的唯一
    输入，丢了它等于标签算错）；② 合并结果里**不许出现 time_str**（防止从后门复活，
    把每场全量重写请回来）。读者实际看到的文案由 goldens/rel_time_labels.json 与
    test_time_label_parity.py 钉住，且用的是同一个 pub_date，两条判据接起来
    覆盖的比原来那一句"字段搬对了"更完整。
    """
    out = _merge([OLD_FULL], API_LIVE, tmp_path)
    assert out.get("link") == "http://e/1", out
    for k, want in (("image", "http://cdn/cover.jpg"), ("bad_date", True),
                    ("title_zh", "中文标题"), ("summary_zh", "中文摘要"),
                    ("fc", "长文正文"), ("mu", "https://cdn/audio.mp3")):
        assert out.get(k) == want, "刷新把 %s 抹掉了（%r -> %r）" % (k, want, out.get(k))
    assert out.get("tags") == ["大模型", "开源"], "话题标签被抹掉：%s" % out.get("tags")
    assert out.get("pub_date") == API_LIVE["d"], "发布时间被改写，时间标签会跟着错：%s" % out.get("pub_date")
    assert "time_str" not in out, "time_str 复活了（派生字段一回来，每场就又全量重写）：%s" % out


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_edited_original_title_drops_the_stale_translation(tmp_path):
    """上游改了原标题时，不许继续显示旧译文（否则标题永远是旧的那一句）。"""
    live = dict(API_LIVE)
    live["t"] = "English Title (revised)"
    out = _merge([OLD_FULL], live, tmp_path)
    assert out.get("title_zh") == "English Title (revised)", (
        "原标题变了却还沿用旧译文：%s" % out.get("title_zh"))


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_fresh_image_from_api_is_not_overwritten_by_old_one(tmp_path):
    """两侧都有封面时以新的为准；只有旧的有时才兜底（否则等价改写能绕过判据）。"""
    live = dict(API_LIVE)
    live["img"] = "http://cdn/new.jpg"
    out = _merge([OLD_FULL], live, tmp_path)
    assert out.get("image") == "http://cdn/new.jpg", out
    out2 = _merge([OLD_FULL], API_LIVE, tmp_path)
    assert out2.get("image") == "http://cdn/cover.jpg", out2


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_capture_time_does_not_clobber_a_known_real_date(tmp_path):
    """上游这次没给 pubDate 时，不许把构建期已知的真实发布时间盖成抓取时刻。"""
    live = dict(API_LIVE)
    live["d"] = "2026-09-21T16:00:00.000Z"     # datedOrCapture 给的抓取时刻
    live["date_fallback"] = 1
    out = _merge([OLD_FULL], live, tmp_path)
    assert out.get("pub_date") == OLD_FULL["pub_date"], (
        "真实发布时间被'刚抓取'盖掉了：%s" % out.get("pub_date"))
    assert not out.get("date_fallback"), "已知真实时间的条目被误标成收录"


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_genuine_new_date_wins_over_stale_old_one(tmp_path):
    """反方向也要动：上游这次给了真日期，就用新的，别抱着旧条目不放。"""
    old = dict(OLD_FULL)
    old["pub_date"] = "2026-09-10T00:00:00+08:00"
    live = dict(API_LIVE)
    live["d"] = "2026-09-21T08:00:00+08:00"
    out = _merge([old], live, tmp_path)
    assert out.get("pub_date") == "2026-09-21T08:00:00+08:00", out
    assert not out.get("date_fallback"), out


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_brand_new_item_gets_the_capture_marker(tmp_path):
    """没在构建期出现过的条目：收录标记照样带出来（原 #30 判据搬到这里）。"""
    out = _merge([], {"t": "T", "u": "http://e/new", "d": "2026-09-21T10:00:00Z",
                      "date_fallback": 1}, tmp_path)
    assert out.get("date_fallback") is True, (
        "新条目的 date_fallback 没带出来 ⇒ 抽屉里把抓取时刻当发布时间显示：%s" % out)
    out2 = _merge([], {"t": "T", "u": "http://e/ok", "d": "2026-09-21T09:00:00Z"}, tmp_path)
    assert not out2.get("date_fallback"), "带真实 pubDate 的条目被误标成收录"


def test_call_site_uses_the_shared_merge():
    """接线：调用点必须走这个合并函数，且不许退回内联对象字面量。"""
    text = open(TPL, encoding="utf-8").read()
    assert CALL in text, (
        "抽屉调用点没走 _apiMergeTo(src.items) ⇒ 映射可能有第二份实现，字段又会被抹掉")
    assert not re.search(r"data\.items\.map\(\s*function\s*\(it\)\s*\{\s*return \{", text), (
        "模板里又出现了内联的 data.items.map(function(it){return {...}})，"
        "它会绕过 _apiMergeTo 并再次丢字段")


def _generated_js():
    sys.path.insert(0, ROOT)
    import build_rss_aggregator as M
    srcs = [{"key": "k1", "name": "N", "cat": "科技", "color": "#f00", "items": []}]
    return M._build_js(srcs)


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_generated_page_carries_the_merge(tmp_path):
    """判据必须打在**生成物**上：模板对了不代表出厂 JS 对了。"""
    js = _generated_js()
    joined = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", js, re.S))
    assert FN in joined, "生成物里没有这个函数（模板没被带上）"
    assert CALL in joined
    i = joined.index(FN)
    out = _run_js(
        _brace_slice(joined, i) + "\nprocess.stdout.write(JSON.stringify("
        "_apiMergeTo([%s])(%s)));" % (json.dumps(OLD_FULL, ensure_ascii=False),
                                      json.dumps(API_LIVE, ensure_ascii=False)), tmp_path)
    assert out.get("image") == "http://cdn/cover.jpg", (
        "出厂 JS 里的合并丢了封面（模板修对了但转义把它改了）：%s" % out)


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_generated_page_script_parses(tmp_path):
    """出厂脚本必须能被 JS 解析器接受：转义出错时这段会先炸，而不是等到浏览器。"""
    js = _generated_js()
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", js, re.S)
    assert blocks, "生成物里找不到 <script> 段，探针失效"
    for n, b in enumerate(blocks):
        f = tmp_path / ("blk%d.js" % n)
        f.write_text(b, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == 0, "第 %d 段脚本语法坏了：%s" % (
            n, (r.stderr or r.stdout)[-400:])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
