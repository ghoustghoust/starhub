import json

d = json.load(open("daily-insight.json", "r", encoding="utf-8"))
events = d["events"]

print("=== 事件分析 ===")
for e in events:
    sig = e["signal"]
    print("  %s: score_a=%.1f, score_b=%.1f, heat=%.1f, resonance=%-10s sources=%s" % (
        e["id"], sig["score_a"], sig["score_b"], sig["heat"], e["resonance"], e["sources"]))

print("\n=== 统计 ===")
print("总事件: %d" % len(events))
sa = [e["signal"]["score_a"] for e in events]
print("score_a 范围: %.1f - %.1f, 非零: %d/%d" % (min(sa), max(sa), sum(1 for x in sa if x > 0), len(sa)))
print("context_coverage: %.2f" % d["quality"]["context_coverage"])
print("faithfulness: %.2f" % d["quality"]["faithfulness"])
print("overall: %.2f" % d["quality"]["overall"])

ids = [e["id"] for e in events]
dups = [x for x in set(ids) if ids.count(x) > 1]
print("重复ID: %s" % (dups if dups else "无"))

# Source type coverage
all_sources = set()
for e in events:
    all_sources.update(e["sources"])
print("事件覆盖源: %s" % sorted(all_sources))

# Articles by type
from collections import Counter
art_types = Counter()
for e in events:
    for a in e.get("articles", []):
        art_types[a["type"]] += 1
print("文章源分布: %s" % dict(art_types))
