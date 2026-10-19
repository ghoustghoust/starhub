# -*- coding: utf-8 -*-
"""T-D3 轻/重分析分级测试（spec D3）。

T-D3-1 light 场: prev 新鲜 → 重分析零调用、stale=True、重字段沿用、统计字段新算
T-D3-2 full 场: 无条件重分析（spy 调用、stale 缺席）
T-D3-3 prev 距今 ≥ interval → 重分析
T-D3-4 light 输出含 generated_at（保持上次重分析时间）+ stale 标记

零网络：insight_engine 用桩模块替换并记录 run_analysis 调用；文件读写进临时目录；
_generate_daily_summary 打桩（避免 Agnes 真调用）。
"""
import datetime
import json
import os
import sys
import tempfile
import types

import build_rss_aggregator as m

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime(2026, 9, 14, 12, 0, 0, tzinfo=TZ8)


class SpyIE(types.ModuleType):
    """insight_engine 桩：记录 run_analysis 调用。"""
    calls = []
    interval = 6

    def load_config(self):
        return {"insight_engine_enabled": True,
                "insight_heavy_interval_hours": type(self).interval}

    fail = False

    def run_analysis(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("模拟重分析失败")
        return {
            "generated_at": datetime.datetime.now(TZ8).isoformat(),
            "topics": [{"label": "语义主题A", "items": ["a1", "a2"]}],
            "deep_insights": {"narrative": "新深度洞察"},
            "ragas": {"overall": 0.8},
            "keywords": {"global": [["语义关键词", 30]]},
            "stats": {"total_articles": 100, "recent_count": 50, "source_count": 10},
            "summary": {"core_trends": "新趋势"},
        }


def iso_ago(hours):
    return (NOW - datetime.timedelta(hours=hours)).isoformat()


def make_history():
    iso = (NOW - datetime.timedelta(hours=2)).isoformat()
    return {
        "link1": {"link": "link1", "title": "AI 芯片发布新架构", "source": "源1",
                  "source_key": "s1", "cat": "ai", "color": "#111",
                  "summary": "摘要1", "pub_date": iso, "first_seen": iso},
        "link2": {"link": "link2", "title": "开发框架开源", "source": "源2",
                  "source_key": "s2", "cat": "dev", "color": "#222",
                  "summary": "摘要2", "pub_date": iso, "first_seen": iso},
    }


def make_sources():
    iso = (NOW - datetime.timedelta(hours=2)).isoformat()
    return [
        {"key": "s1", "name": "源1", "cat": "ai", "color": "#111", "tier": 3,
         "url": "https://s1.example/feed", "items": [
             {"link": "link1", "title": "AI 芯片发布新架构", "title_zh": "",
              "summary": "摘要1", "pub_date": iso, "image": "", "full_content": ""}]},
        {"key": "s2", "name": "源2", "cat": "dev", "color": "#222", "tier": 3,
         "url": "https://s2.example/feed", "items": [
             {"link": "link2", "title": "开发框架开源", "title_zh": "",
              "summary": "摘要2", "pub_date": iso, "image": "", "full_content": ""}]},
    ]


def main():
    tmp = tempfile.mkdtemp(prefix="_td3_")
    # 沙箱：文件常量指向临时目录
    m.ANALYSIS_SNAPSHOT_FILE = os.path.join(tmp, "analysis_snapshot.json")
    m.SOURCE_QUALITY_FILE = os.path.join(tmp, "source_quality.json")
    m.RSS_TREND_HISTORY_FILE = os.path.join(tmp, "rss_trend_history.json")
    m.TRENDING_SNAPSHOT_FILE = os.path.join(tmp, "trending_snapshot.json")
    m._rss_history = make_history()
    sources = make_sources()
    m._generate_daily_summary = lambda *a, **k: "统计摘要"

    # 桩 insight_engine 模块
    spy = SpyIE("insight_engine")
    sys.modules["insight_engine"] = spy

    try:
        # ── T-D3-1: light 场 ──
        spy.calls.clear()
        spy.interval = 6
        with open(m.ANALYSIS_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated_at": iso_ago(1),
                       "topics": [{"label": "旧语义主题", "items": ["x"]}],
                       "deep_insights": {"narrative": "旧深度洞察"},
                       "ragas": {"overall": 0.7}}, f)
        r = m._run_analysis(sources, NOW, mode="incremental")
        assert len(spy.calls) == 0, "T-D3-1 FAIL: light 场仍调用了重分析 %d 次" % len(spy.calls)
        assert r.get("stale") is True, "T-D3-1 FAIL: 缺 stale 标记"
        assert r.get("deep_insights", {}).get("narrative") == "旧深度洞察", "T-D3-1 FAIL: 重字段未沿用"
        assert r.get("keywords", {}).get("global"), "T-D3-1 FAIL: 统计关键词缺失"
        assert r.get("rss_trajectories") is not None, "T-D3-1 FAIL: 轨迹缺失"
        print("PASS T-D3-1 light 场: 重分析零调用、stale=True、重字段沿用、统计字段新算")

        # ── T-D3-4: generated_at + stale 键存在（light 保持旧时间，诚实新鲜度）──
        assert "generated_at" in r and "stale" in r
        assert r["generated_at"] == iso_ago(1), "T-D3-4 FAIL: light 场 generated_at 应保持上次重分析时间"
        print("PASS T-D3-4 light 输出含 generated_at（保持旧值）+ stale")

        # ── T-D3-3: prev 过期 → 重分析 ──
        spy.calls.clear()
        with open(m.ANALYSIS_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated_at": iso_ago(7),
                       "topics": [{"label": "过期主题"}], "deep_insights": {"narrative": "旧"}}, f)
        r = m._run_analysis(sources, NOW, mode="incremental")
        assert len(spy.calls) == 1, "T-D3-3 FAIL: prev 过期应触发重分析"
        assert r.get("deep_insights", {}).get("narrative") == "新深度洞察", "T-D3-3 FAIL: 未采用新结果"
        assert not r.get("stale"), "T-D3-3 FAIL: 新结果不应带 stale"
        print("PASS T-D3-3 prev 过期: 触发重分析并采用新结果")

        # ── T-D3-2: full 模式无条件重分析 ──
        spy.calls.clear()
        with open(m.ANALYSIS_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated_at": iso_ago(0.1),
                       "topics": [{"label": "新鲜主题"}], "deep_insights": {"narrative": "新"}}, f)
        r = m._run_analysis(sources, NOW, mode="full")
        assert len(spy.calls) == 1, "T-D3-2 FAIL: full 模式应重分析"
        print("PASS T-D3-2 full 模式: 无条件重分析 ✓")

        # ── T-D3-5: 重分析失败 → overlay 沿用旧语义字段 + stale（P1-2 修复）──
        spy.calls.clear()
        spy.fail = True
        with open(m.ANALYSIS_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated_at": iso_ago(7),
                       "topics": [{"label": "旧语义主题"}],
                       "deep_insights": {"narrative": "旧深度洞察"}}, f)
        r = m._run_analysis(sources, NOW, mode="incremental")
        spy.fail = False
        assert r.get("deep_insights", {}).get("narrative") == "旧深度洞察",             "T-D3-5 FAIL: 重分析失败后旧语义字段未保留 %r" % r.get("deep_insights")
        assert r.get("stale") is True, "T-D3-5 FAIL: 失败路径缺 stale 标注"
        print("PASS T-D3-5 重分析失败: overlay 沿用旧语义字段 + stale 诚实标注")

        # ── T-D3-6: 语义字段缺失（模拟失败后快照）→ 到期前也触发重分析（P1-2 条件）──
        spy.calls.clear()
        with open(m.ANALYSIS_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"generated_at": iso_ago(0.5),
                       "keywords": {"global": [["统计词", 3]]}}, f)  # 无 deep_insights
        r = m._run_analysis(sources, NOW, mode="incremental")
        assert len(spy.calls) == 1, "T-D3-6 FAIL: 语义字段缺失应触发重分析 %d" % len(spy.calls)
        print("PASS T-D3-6 语义字段缺失: 即使 prev 新鲜也触发重分析 ✓")
    finally:
        sys.modules.pop("insight_engine", None)

    print("T-D3 全部通过")


if __name__ == "__main__":
    main()
