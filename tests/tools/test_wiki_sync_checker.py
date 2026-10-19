# -*- coding: utf-8 -*-
"""`check_wiki_sync.py` 的判据必须能红，且期望值必须来自仓库现读。

为什么这批测试值得存在：旧版把 "711 源 / T1=23 / 恰好 7 个函数 / 禁止出现 710" 写死成
事实，源一删就把新架构判成错（201 条红大多是这种"拿错尺子"）。新版改成"现读 + 写了才比对"，
但**改成现读很容易顺手改成恒真**，所以每条判据都要有"变异体必红"与"反向必绿"两对。
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "check_wiki_sync.py"

CRON = "0 21 * * *"
SOURCES = [
    {"key": "a", "name": "A", "tier": 1},
    {"key": "b", "name": "B", "tier": 1},
    {"key": "c", "name": "C", "tier": 2},
    {"key": "d", "name": "D", "tier": 3},
    {"key": "e", "name": "E", "tier": 3},
]


def _load_checker():
    spec = importlib.util.spec_from_file_location("wiki_sync_under_test", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _correct_page():
    """一份把现存东西都提到、数字全对的页——对照组，用来证明判据不是恒红。"""
    return ("端点 rss.js 与 search.js；构建脚本 fetch_and_build.py；workflow update.yml；"
            'schedule cron "%s"；cancel-in-progress: true。\n'
            "当前 5 个源，T1=2 / T2=1 / T3=2，共 2 个 Vercel 函数。\n" % CRON)


class Harness:
    def __init__(self, root, module, page_path):
        self.root = root
        self.module = module
        self.page_path = page_path

    def write(self, text):
        self.page_path.write_text(text, encoding="utf-8")

    def run(self):
        self.module.ERRORS.clear()
        code = self.module.main()
        return code, list(self.module.ERRORS)

    def correct(self):
        self.write(_correct_page())


@pytest.fixture()
def wiki(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "rss.js").write_text("// handler\n", encoding="utf-8")
    (tmp_path / "api" / "search.js").write_text("// handler\n", encoding="utf-8")
    (tmp_path / "fetch_and_build.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (tmp_path / "vercel.json").write_text(json.dumps({"functions": {
        "api/rss.js": {"maxDuration": 60}, "api/search.js": {"maxDuration": 30}}}),
        encoding="utf-8")
    (tmp_path / "rss_sources.json").write_text(json.dumps(SOURCES), encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "update.yml").write_text(
        'on:\n  schedule:\n    - cron: "%s"\n'
        "concurrency:\n  group: starhub-update\n  cancel-in-progress: true\n" % CRON,
        encoding="utf-8")

    module = _load_checker()
    module.ROOT = tmp_path
    module.WIKI = tmp_path / ".qoder" / "repowiki"
    page = module.WIKI / "knowledge" / "zh"
    page.mkdir(parents=True)
    return Harness(tmp_path, module, page / "架构设计.md")


def test_control_group_a_correct_wiki_is_green(wiki):
    """对照组：判据必须能为绿而绿，否则后面的"红"没有信息量。"""
    wiki.correct()
    code, errors = wiki.run()
    assert code == 0, f"全对的 Wiki 被判红：{errors}"


def test_wrong_source_count_is_red(wiki):
    """变异体：数字写错必红，且红因是"与现测不符"而不是"不等于 711"。"""
    wiki.write(_correct_page().replace("当前 5 个源", "当前 711 个源"))
    code, errors = wiki.run()
    assert code == 1, errors
    assert any("现测 5 个源" in e for e in errors), errors


def test_expectation_follows_the_repo(wiki):
    """把仓库改成 6 个源、页面对应改成 6，必须仍然绿：期望值跟着仓库走。"""
    (wiki.root / "rss_sources.json").write_text(
        json.dumps(SOURCES + [{"key": "f", "name": "F", "tier": 3}]), encoding="utf-8")
    wiki.write(_correct_page().replace("当前 5 个源", "当前 6 个源").replace("T3=2", "T3=3"))
    code, errors = wiki.run()
    assert code == 0, f"期望值没跟着仓库走：{errors}"


def test_tier_count_stated_wrong_is_red(wiki):
    """tier 分项也要对得上：总数对、分项错同样判红。"""
    wiki.write(_correct_page().replace("T2=1", "T2=9"))
    code, errors = wiki.run()
    assert code == 1 and any("T2=1" in e for e in errors), errors


def test_loose_tier_mention_is_not_counted(wiki):
    """反向：`T3 每批 200 条` 不是计数声明，不许被当成 T3=200 判红。"""
    wiki.correct()
    wiki.page_path.write_text(
        wiki.page_path.read_text(encoding="utf-8") + "\nT3 分批 200 条刷新。\n", encoding="utf-8")
    code, errors = wiki.run()
    assert code == 0, f"松写法被判成计数：{errors}"


def test_omitting_numbers_is_not_red(wiki):
    """反向：不写数字不算缺陷（宁缺勿错），这是"不钉具体数字"的另一半。"""
    wiki.write(_correct_page().split("当前")[0])
    code, errors = wiki.run()
    assert code == 0, f"漏写数字被判红了：{errors}"


def test_unmentioned_live_endpoint_is_red(wiki):
    """覆盖类：仓库新增端点而 Wiki 没跟上，必须红。"""
    wiki.correct()
    (wiki.root / "api" / "extra.js").write_text("// handler\n", encoding="utf-8")
    code, errors = wiki.run()
    assert code == 1 and any("api/extra.js" in e for e in errors), errors


def test_wrong_cron_is_red(wiki):
    """覆盖类反向：Wiki 抄了一条仓库里已不存在的 cron，必须红。"""
    wiki.write(_correct_page().replace(CRON, "30 7 * * *"))
    code, errors = wiki.run()
    assert code == 1, errors
    assert any("30 7 * * *" in e for e in errors), errors


def test_dead_path_is_red_but_tombstone_line_is_ignored(wiki):
    """失效引用：以现状口吻提到不存在的路径必红；同一行带墓碑词则放过。"""
    wiki.write(_correct_page() + "\n端点见 api/gone.js。\n")
    code, errors = wiki.run()
    assert code == 1 and any("api/gone.js" in e for e in errors), errors

    wiki.write(_correct_page() + "\n已删端点：api/gone.js。\n")
    code, errors = wiki.run()
    assert code == 0, f"历史交代理应放过，却判红：{errors}"


def test_bare_basename_resolves_anywhere_in_repo(wiki):
    """裸文件名写法（`test_x.py`）不该因为"根目录没有这个文件"而误红。"""
    tests = wiki.root / "tests" / "site_nav"
    tests.mkdir(parents=True)
    (tests / "test_pages_order.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    wiki.write(_correct_page() + "\n判据见 test_pages_order.py。\n")
    code, errors = wiki.run()
    assert code == 0, f"裸 basename 被判成不存在：{errors}"


def test_secret_token_in_wiki_is_red(wiki):
    """令牌扫描要真会红：把 Vercel token 形状写进 Wiki 必须被抓。"""
    wiki.correct()
    wiki.page_path.write_text(
        wiki.page_path.read_text(encoding="utf-8") + "\ntoken vcp_" + "a" * 24 + "\n",
        encoding="utf-8")
    code, errors = wiki.run()
    assert code == 1 and any("令牌" in e for e in errors), errors


def test_checker_reads_the_real_repo_when_unpatched():
    """不打桩时它跑的是真仓库：至少不能崩，且结论要能带上现测数字。"""
    module = _load_checker()
    module.ERRORS.clear()
    code = module.main()
    assert code in (0, 1)
