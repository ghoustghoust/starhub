# -*- coding: utf-8 -*-
"""源级 pub_date 时区误标校正 + 未来日期处置 · 单元测试

背景（2026-09-14 实测）：
    超能网_31 的上游把**北京时间标成 +00:00**，解析后 pub_date 比真实发布时间晚 8 小时。
    37 条中 36 条「日期倒挂」（pub_date > first_seen），中位 -6.25h；
    按 -8h 校正后残留抓取延迟 [+0.59h, +11.11h] 全部为正。
    「抓取延迟必须 ≥ 0」是物理约束（我们不可能在文章发布之前抓到它）。

    旧的 bad_date 审计只标记不校正，错误日期原样进输出；前端钳制又把**每一条**未来日期
    改写成 `now`，N 条因此获得完全相同的时刻 → 降序排序下同刻并列 → 整块钉在首屏顶部。

    为什么校正量是**声明式**（rss_sources.json 的 pub_date_offset_min）而不是自动推断：
    物理约束只给出下界 need = max(first_seen - pub_date)；实测超能网 need = 444.7min 时，
    480 / 525 / 570min 全都是物理可行的候选，统计上无法唯一确定。
    「取最小候选」的启发式只是碰巧对（因为 7.5h 不是真实时区），一般情况会出错，
    且会静默改写日期。故自动检测降级为**发现器**（只报告），校正由配置显式声明。

本文件覆盖 L1（声明式校正）与 L2（未来日期判伪 + 保序回拉）。
D 段专门守住 L2 的「不得折叠」：first_seen 是**批次戳**（全量 8411 条仅 122 个取值），
判伪后若统统取同一个 first_seen，同批次会塌成一个时刻 → 重演「同刻扎堆」。
L3（前端钳制不再制造并列）在 tests/rss_sort/cases.js 的 T13/T14。

用法：python tests/rss_date/test_tz_correction.py    退出码 0=全绿 1=有失败
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from _loader import load_build  # noqa: E402

B = load_build()

FAIL = 0
PASS = 0
failures = []
BJ = datetime.timezone(datetime.timedelta(hours=8))


def eq(actual, expected, label):
    global FAIL, PASS
    if actual == expected:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        failures.append(label)
        print("  [FAIL] %s\n         期望 %r\n         实得 %r" % (label, expected, actual))


def ok(cond, label):
    eq(bool(cond), True, label)


def near(actual, expected, tol, label):
    ok(abs(actual - expected) <= tol, "%s（实得 %.1f，期望 %.1f±%g）" % (label, actual, expected, tol))


def h(n):
    """n 小时前（北京时间裸值，与历史文件口径一致）"""
    return datetime.datetime.now(BJ).replace(tzinfo=None) - datetime.timedelta(hours=n)


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def mk_item(link, sk, pub_date, first_seen, title="t"):
    return {"link": link, "source": "源", "source_key": sk, "cat": "news", "color": "#000",
            "title": title, "title_zh": "", "summary": "", "summary_zh": "",
            "full_content": "", "image": "", "media_url": "", "media_type": "",
            "pub_date": pub_date, "first_seen": first_seen}


def mk_new(link, pub_date, title="t"):
    return {"title": title, "link": link, "summary": "", "full_content": "",
            "image": "", "media_url": "", "media_type": "", "pub_date": pub_date}


def mksrc(sk, items, name="源"):
    return [{"key": sk, "name": name, "cat": "news", "color": "#000", "tier": 2,
             "items": [dict(it, source=name, source_key=sk) for it in items]}]


LAGS = [0.5, 0.9, 1.2, 1.4, 1.6, 1.75, 1.9, 2.1, 2.3, 1.1, 0.7, 2.0]


def tz_mislabel_history(sk="S_CN", n=12, offset_h=8, lags=None):
    """构造「本地时间被标成 UTC」的源：真实发布 T，pub_date = T + offset，first_seen = T + lag"""
    lags = lags or LAGS
    hist = {}
    for i in range(n):
        t = h(4 + i * 0.7)
        lag = lags[i % len(lags)]
        pd = t + datetime.timedelta(hours=offset_h)
        fs = t + datetime.timedelta(hours=lag)
        link = "https://cn.example.com/%d" % i
        hist[link] = mk_item(link, sk, iso(pd), iso(fs), "文%d" % i)
    return hist


print("=" * 74)
print("A. L1 声明式校正 + 系统性倒挂发现器")
print("=" * 74)

# A1：真实配置必须已声明超能网的偏移（这是「修好了」的实测门）
declared = B._load_pub_date_offsets(B.RSS_SOURCES)
eq(declared.get("超能网_31"), -480, "A1 rss_sources.json 已为 超能网_31 声明 -480min")

# A2：无该字段的源不产生校正；非法值不得让构建崩溃
edge = B._load_pub_date_offsets([
    {"key": "A"}, {"key": "B", "pub_date_offset_min": 0},
    {"key": "C", "pub_date_offset_min": "abc"}, {"key": "D", "pub_date_offset_min": -60},
    "不是字典",
])
eq(edge, {"D": -60}, "A2 只取合法非零声明，非法值静默忽略")

# A3：平移语义 —— feed 把「北京时间 10:17」标成 10:17 UTC（真实样本），
#     解析后得到 18:17 北京时间；前移 480min 必须还原成 10:17 北京时间
got = B._parse_hist_dt(B._shift_iso_minutes("2026-09-11T10:17:00+00:00", -480))
want = B._parse_hist_dt("2026-09-11T10:17:00+08:00")
eq(got, want, "A3 -480min 把被标成 UTC 的北京时间还原为真实发布时间")
eq(B._shift_iso_minutes("", -480), "", "A3b 空串原样返回（不制造数据）")
eq(B._shift_iso_minutes("不是时间", -480), "不是时间", "A3c 不可解析原样返回")

# A4：发现器必须报出超能网形态，并给出物理可行域下界
an = B._detect_pub_date_anomalies(tz_mislabel_history(), {})
eq(len(an), 1, "A4a 报出 1 个疑似源")
if an:
    eq(an[0]["key"], "S_CN", "A4b 命中的是 S_CN")
    near(an[0]["inverted_ratio"], 1.0, 0.001, "A4c 倒挂率 100%")
    near(an[0]["need_min"], 450.0, 1.0, "A4d 需要至少前移 450min（=7.5h）")
    eq(an[0]["kind"], "tz", "A4e 判为时区误标（可行域落在真实时区范围内）")
    eq(an[0]["suggest_min"], -480, "A4f 建议值 -480min（真实时区里最小可行候选）")

# A4g：真实数据下的 need 与建议值（超能网 实测最小抓取延迟 0.59h → need≈445min）
_real = tz_mislabel_history("S_R", n=37, offset_h=8, lags=[0.59, 1.2, 1.8, 2.2])
_an = B._detect_pub_date_anomalies(_real, {})
if _an:
    near(_an[0]["need_min"], 444.6, 2.0, "A4g 真实 n=37 形态 need≈445min")
    eq(_an[0]["suggest_min"], -480, "A4h 建议 -480min（与已声明值一致）")

# A10：需要前移几十小时 ⇒ 不是时区问题，不得给偏移建议
far = {}
for i in range(12):
    t = h(6 + i)
    link = "https://far.example.com/%d" % i
    far[link] = mk_item(link, "S_FAR", iso(t + datetime.timedelta(hours=30)),
                        iso(t + datetime.timedelta(minutes=30)))
an10 = B._detect_pub_date_anomalies(far, {})
eq(len(an10), 1, "A10a 仍报出该源（日期确实异常）")
if an10:
    eq(an10[0]["kind"], "other", "A10b 判为「非时区」而非时区误标")
    eq(an10[0]["suggest_min"], None, "A10c 不给偏移建议（避免 -4103min 这类荒谬建议）")

# A5/A6：不得误报
wide = {}
for i in range(12):
    t = h(6 + i)
    pd = t + datetime.timedelta(hours=(1.5 if i % 2 == 0 else -12))
    fs = t + datetime.timedelta(hours=0.5)
    link = "https://wide.example.com/%d" % i
    wide[link] = mk_item(link, "S_WIDE", iso(pd), iso(fs))
eq(B._detect_pub_date_anomalies(wide, {}), [], "A5 宽阔带（倒挂率 50%，arxiv/播客形态）→ 不报")

eq(B._detect_pub_date_anomalies(tz_mislabel_history("S_TINY", n=5), {}), [],
   "A6 小样本（n=5）→ 不报")

ok_dict = {}
for i in range(12):
    t = h(5 + i)
    link = "https://ok.example.com/%d" % i
    ok_dict[link] = mk_item(link, "S_OK", iso(t), iso(t + datetime.timedelta(minutes=30)))
eq(B._detect_pub_date_anomalies(ok_dict, {}), [], "A7 正常源（延迟 +30min）→ 不报")

# A8：已声明偏移的源不再重复告警（避免每次构建刷同样的日志）
eq(B._detect_pub_date_anomalies(tz_mislabel_history(), {"S_CN": -480}), [],
   "A8 已声明的源不再重复告警")

# A9：发现器不得修改数据
snap = tz_mislabel_history()
before = {k: v["pub_date"] for k, v in snap.items()}
B._detect_pub_date_anomalies(snap, {})
eq({k: v["pub_date"] for k, v in snap.items()}, before, "A9 发现器只读，不改数据")

print()
print("=" * 74)
print("B. L2 未来日期判伪（我们不可能在发布前抓到）")
print("=" * 74)

e_ok = mk_item("l1", "S", iso(h(2)), iso(h(1)))
eq(B._pub_date_falsified(e_ok), False, "B1 pub_date 早于 first_seen → 未证伪")
e_bound = mk_item("l2", "S", iso(h(2) + datetime.timedelta(minutes=60)), iso(h(2)))
eq(B._pub_date_falsified(e_bound), False, "B2 恰好超出 60min → 未证伪（边界取严格大于）")
e_fut = mk_item("l3", "S", iso(h(2) + datetime.timedelta(minutes=61)), iso(h(2)))
eq(B._pub_date_falsified(e_fut), True, "B3 超出 61min → 已证伪")
eq(B._pub_date_falsified({"pub_date": "", "first_seen": iso(h(1))}), False,
   "B4 无 pub_date → 交给既有降级路径，不重复判伪")

print()
print("=" * 74)
print("C. 端到端：校正后条目不再「出生于未来」，且不误伤正常源")
print("=" * 74)

_SAVED = B.RSS_SOURCES
B.RSS_SOURCES = [{"key": "S_CN", "name": "超能网", "url": "https://x", "pub_date_offset_min": -480}]

hist = tz_mislabel_history()
items = [{"title": it["title"], "link": it["link"], "summary": "", "full_content": "",
          "image": "", "media_url": "", "media_type": "", "pub_date": it["pub_date"]}
         for it in hist.values()]
B._rss_history = {k: dict(v) for k, v in hist.items()}
orig_raw = {k: v["pub_date"] for k, v in hist.items()}
res, total = B._accumulate_history(mksrc("S_CN", items, "超能网"))
out = [it for s in res for it in s.get("items", [])]
eq(len(out), 12, "C1 12 条全部保留")
eq(sum(1 for it in out if it.get("date_fallback")), 0, "C2 校正后的条目不走降级键")
eq(sum(1 for it in out if it.get("bad_date")), 0, "C3 校正后不再被标不可信（审计跑在校正值上）")
bad = 0
for it in out:
    pd = B._parse_hist_dt(it["pub_date"])
    fs = B._parse_hist_dt(it["first_seen"])
    if pd > fs:
        bad += 1
eq(bad, 0, "C4 校正后没有任何条目晚于 first_seen（物理可行）")
eq(all(B._rss_history[k]["pub_date"] == orig_raw[k] for k in orig_raw), True,
   "C5 历史文件的 pub_date 保持原始（校正只作用于输出副本）")
# 校正后必须真的回到真实发布时间（不能只是「不再未来」）
got0 = min(B._parse_hist_dt(it["pub_date"]) for it in out)
want0 = min(B._parse_hist_dt(v["pub_date"]) for v in hist.values()) - datetime.timedelta(minutes=480)
eq(got0, want0, "C6 输出时间 == 原始时间前移 480min（校正值精确）")

# 残留未来日期（源未声明偏移，样本不足以让发现器建议）→ 降级键
B.RSS_SOURCES = [{"key": "S_POD", "name": "播客", "url": "https://y"}]
LINK = "https://future.example.com/podcast"
fs0 = h(3)
pd0 = fs0 + datetime.timedelta(hours=5)
B._rss_history = {LINK: mk_item(LINK, "S_POD", iso(pd0), iso(fs0), "预约发布")}
res2, _t = B._accumulate_history(mksrc("S_POD", [mk_new(LINK, iso(pd0), "预约发布")], "播客"))
it2 = [it for s in res2 for it in s.get("items", []) if it["link"] == LINK]
eq(len(it2), 1, "C7 预约发布条目保留（不被丢弃）")
if it2:
    eq(bool(it2[0].get("date_fallback")), True, "C8 已被证伪的日期 → 标记降级")
    eq(B._parse_hist_dt(it2[0]["pub_date"]).replace(tzinfo=None, microsecond=0),
       fs0.replace(microsecond=0),
       "C9 降级键 == first_seen（我方首次收录时刻，真实可排序）")

# 对照组：正常源不被动
B.RSS_SOURCES = [{"key": "S_OK", "name": "正常源", "url": "https://z"}]
LINK2 = "https://ok.example.com/fresh"
B._rss_history = {}
res3, _t3 = B._accumulate_history(mksrc("S_OK", [mk_new(LINK2, iso(h(1)), "正常")], "正常源"))
it3 = [it for s in res3 for it in s.get("items", []) if it["link"] == LINK2]
eq(len(it3), 1, "C10 正常条目保留")
if it3:
    eq(bool(it3[0].get("date_fallback")), False, "C11 正常条目不得被降级")
    eq(bool(it3[0].get("bad_date")), False, "C12 正常条目不得被标不可信")
    eq(it3[0]["pub_date"], iso(h(1)), "C13 正常条目时间原样")

B.RSS_SOURCES = _SAVED

print()
print("=" * 74)
print("D. L2 判伪必须保序回拉，不得把同一抓取批次折叠成同一时刻")
print("=" * 74)

# 背景：first_seen 是按抓取批次打的时间戳（全量 8411 条仅 122 个取值，最大批次 1857 条）。
# 若判伪条目统统写成同一个 first_seen，同批次的一整组会塌成一个时刻 →
# 前端降序排完就是一大块同源同刻卡片，与「超能网 6 条占前 7 位」是同一形态。
# 实测受影响：101 条判伪里 68 条落在同批次多条上，最大单组 27 条（arxiv_ai_4）、
# 次大 24 条（product_hunt_797）。

_SAVED2 = B.RSS_SOURCES
B.RSS_SOURCES = [{"key": "S_BATCH", "name": "批次源", "url": "https://b"}]

_batch_fs = h(3)
# 注意：60min 是本层容忍阈值，adv 恰好 = 1.0h 的条**不**判伪（保留其 ≤60min 的未来日期，
# 由前端 L3 回拉兜住）。故样本刻意避开边界值。
_ad = [2.0, 3.0, 5.0, 7.0, 9.0]
_hist = {}
_items = []
for _i, _a in enumerate(_ad):
    _link = "https://batch.example.com/%d" % _i
    _pd = _batch_fs + datetime.timedelta(hours=_a)
    _hist[_link] = mk_item(_link, "S_BATCH", iso(_pd), iso(_batch_fs), "批次%d" % _i)
    _items.append(mk_new(_link, iso(_pd), "批次%d" % _i))
B._rss_history = {k: dict(v) for k, v in _hist.items()}
res_d, _td = B._accumulate_history(mksrc("S_BATCH", _items, "批次源"))
out_d = [it for s in res_d for it in s.get("items", [])]
_d_ts = [B._parse_hist_dt(it["pub_date"]) for it in out_d]

eq(len(out_d), 5, "D1 5 条全部保留")
eq(sum(1 for it in out_d if it.get("date_fallback")), 5, "D2 全部被判伪（声称时间均晚于 first_seen）")
eq(len(set(_d_ts)), 5, "D3 回拉后 5 条时刻互不相同（不折叠为同一时刻）")
eq(max(_d_ts).replace(tzinfo=None, microsecond=0), _batch_fs.replace(microsecond=0),
   "D4 组内最新的一条恰好锚在 first_seen")
eq(min(_d_ts).replace(tzinfo=None, microsecond=0),
   (_batch_fs - datetime.timedelta(hours=7)).replace(microsecond=0),
   "D5 组内最早的一条 = 最新锚点 - 组内最大间隔（7h）")
eq([it["title"] for it in sorted(out_d, key=lambda x: B._parse_hist_dt(x["pub_date"]), reverse=True)],
   ["批次4", "批次3", "批次2", "批次1", "批次0"],
   "D6 组内相对先后被保留（与 feed 声称的时间顺序一致）")
eq(all(t <= B._parse_hist_dt(it["first_seen"]) + datetime.timedelta(
        minutes=B.FUTURE_DATE_TOLERANCE_MIN) for t, it in zip(_d_ts, out_d)), True,
   "D7 回拉后没有任何一条晚于 first_seen + 容忍阈值")
eq(all(t >= datetime.datetime.now(BJ) - datetime.timedelta(hours=72) for t in _d_ts), True,
   "D8 回拉后没有任何一条早于 72h 窗口下界")

# 两个批次：各自锚到自己的 first_seen（后一批整体更新）
_fs_b = h(1)
_link_b = "https://batch.example.com/9"
_pd_b = _fs_b + datetime.timedelta(hours=2)
B._rss_history[_link_b] = mk_item(_link_b, "S_BATCH", iso(_pd_b), iso(_fs_b), "批次B")
_items_b = _items + [mk_new(_link_b, iso(_pd_b), "批次B")]
res_d2, _td2 = B._accumulate_history(mksrc("S_BATCH", _items_b, "批次源"))
out_d2 = [it for s in res_d2 for it in s.get("items", [])]
_ts2 = {it["title"]: B._parse_hist_dt(it["pub_date"]) for it in out_d2}
eq(max(_ts2.values()).replace(tzinfo=None, microsecond=0), _fs_b.replace(microsecond=0),
   "D9 后一批次的最新一条锚在它自己的 first_seen")
eq(_ts2["批次4"].replace(tzinfo=None, microsecond=0), _batch_fs.replace(microsecond=0),
   "D10 前一批次仍锚在它自己的 first_seen（批次互不影响）")

# 无 pub_date 的条目没有相对信息 → 保持原行为（取 first_seen，允许同刻）
B.RSS_SOURCES = [{"key": "S_NODATE", "name": "无日期源", "url": "https://n"}]
_fs_n = h(2)
_hist_n, _items_n = {}, []
for _i in range(3):
    _ln = "https://nodate.example.com/%d" % _i
    _hist_n[_ln] = mk_item(_ln, "S_NODATE", "", iso(_fs_n), "无日期%d" % _i)
    _items_n.append(mk_new(_ln, "", "无日期%d" % _i))
B._rss_history = {k: dict(v) for k, v in _hist_n.items()}
res_d3, _td3 = B._accumulate_history(mksrc("S_NODATE", _items_n, "无日期源"))
out_d3 = [it for s in res_d3 for it in s.get("items", [])]
eq(len(out_d3), 3, "D11 无日期条目全部保留")
eq(len({B._parse_hist_dt(it["pub_date"]) for it in out_d3}), 1,
   "D12 无日期条目仍统一取 first_seen（无相对信息，行为不变）")

# 窗口保护：回拉不得把条目推出 72h 窗口
B.RSS_SOURCES = [{"key": "S_EDGE", "name": "窗口源", "url": "https://e"}]
_fs_e = h(71)
_hist_e, _items_e = {}, []
for _i, _a in enumerate([1.5, 26.0]):
    _le = "https://edge.example.com/%d" % _i
    _pde = _fs_e + datetime.timedelta(hours=_a)
    _hist_e[_le] = mk_item(_le, "S_EDGE", iso(_pde), iso(_fs_e), "窗口%d" % _i)
    _items_e.append(mk_new(_le, iso(_pde), "窗口%d" % _i))
B._rss_history = {k: dict(v) for k, v in _hist_e.items()}
res_d4, _td4 = B._accumulate_history(mksrc("S_EDGE", _items_e, "窗口源"))
out_d4 = [it for s in res_d4 for it in s.get("items", [])]
_ts4 = [B._parse_hist_dt(it["pub_date"]) for it in out_d4]
eq(all(t >= datetime.datetime.now(BJ) - datetime.timedelta(hours=72) - datetime.timedelta(minutes=1)
       for t in _ts4), True,
   "D13 回拉下限被钳在 72h 窗口上（不会把条目推出窗口）")

B.RSS_SOURCES = _SAVED2

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    print("失败用例：")
    for f in failures:
        print("  - %s" % f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
