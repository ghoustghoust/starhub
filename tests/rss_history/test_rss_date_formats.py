# tests/rss_history/test_rss_date_formats.py
# -*- coding: utf-8 -*-
"""feed 日期写法兼容：真实世界存在的写法必须能解析出来，不能静默变成"无日期"。

起因（2026-09-21 实测）：超能网_31 在历史里 68/68 条 pub_date 为空，看着像"上游不给日期的源"、
差点被删掉。实际是它的 feed 有 50 条 <pubDate>，写成 `Sun, 20 Sep 2026 17:42:00 +08`
—— 裸两位数偏移。旧正则的时区分支只接受 `[+-]\d{2}:?\d{2}`（四位）或字母时区，
`+08` 两支都不匹配，而日期主体要求匹配到结尾 ⇒ 整串解析失败 ⇒ 降级成"靠收录时刻撑龄期"，
于是有了"每场被重新抓到就清零"的永生通道。
样本一律用线上真实字符串，不用我编的格式。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from _loader import load_build  # noqa: E402

mod = load_build()

# (真实样本, 期望的北京时间小时偏移)
CASES = [
    ("Sun, 20 Sep 2026 17:42:00 +08", 8),          # plink.anyfeeder（超能网）
    ("Sun, 20 Sep 2026 17:42:00 +0800", 8),        # 同族四位写法，本来就能过
    ("Mon, 14 Sep 2026 01:18:00 GMT", 0),          # 标准 RFC 822
    ("Tue, 18 Apr 2023 21:25:59 GMT", 0),           # CNN
    ("Fri Oct 27 2023 15:13:00 GMT+0800 (China Standard Time)", 8),   # JS Date.toString()
    ("2026-09-20T17:42:00+08:00", 8),               # ISO（Atom）
    ("Sat, 05 Sep 2026 09:00:00 -0500", -5),       # 负偏移四位
    ("Sat, 05 Sep 2026 09:00:00 -05", -5),         # 负偏移裸两位
]


def test_real_feed_date_writings_all_parse():
    bad = []
    for s, _want in CASES:
        d = mod._parse_rss_date(s)
        if d is None:
            bad.append(s)
    assert not bad, "%d 种真实 feed 日期写法解析成 None：%s" % (len(bad), bad[:3])


def test_bare_two_digit_offset_is_not_dropped():
    """根因用例：`+08` 必须解析成功且偏移按 +8 小时解释（不是 8 分钟、不是 UTC）。"""
    d = mod._parse_rss_date("Sun, 20 Sep 2026 17:42:00 +08")
    assert d is not None, "裸两位数偏移又被判成无日期了：这正是删错源的起点"
    assert d.utcoffset() == mod.datetime.timedelta(hours=8), \
        "偏移解释错了：%r" % (d.utcoffset(),)
    assert d.hour == 17 and d.day == 20


def test_negative_bare_offset_same_rule():
    d = mod._parse_rss_date("Sat, 05 Sep 2026 09:00:00 -05")
    assert d is not None and d.utcoffset() == mod.datetime.timedelta(hours=-5), \
        "负向裸偏移没走同一条规则：%r" % (d,)


def test_garbage_still_returns_none():
    """放宽不能变成"什么都能过"：垃圾串仍然必须返回 None，由降级路径处理。"""
    for s in ("", "   ", "昨天下午", "Sun, 99 Zzz 2026", "not-a-date"):
        assert mod._parse_rss_date(s) is None, "假日期被接受了：%r" % s
