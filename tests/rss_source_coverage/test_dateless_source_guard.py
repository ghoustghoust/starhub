# tests/rss_source_coverage/test_dateless_source_guard.py
# -*- coding: utf-8 -*-
"""「逐条没有发布日期的源」这一批的落地判据。

Why（哪次故障逼出来的）：2026-09-21 统计线上产物时发现有 321 条正靠降级日期显示，
追到源头是几类 feed 里压根没有日期元素（plink.anyfeeder 代理结构性丢日期、
RSSHub 的日经路由、旧 Blogger 地址）。用户判定：真没日期的源可以去掉，
给了日期却被我们判成没给的（裸两位偏移 +08）要修算法不许删。

这批改动有两处静默失效的面，所以判据必须成对：
  1) 工具里声明了，清单文件没重放 ⇒ 线上照旧抓那 4 个源（补丁"看起来在"但没生效）；
  2) 清单改了，工具里的声明被回滚 ⇒ 下一次任何人在别的基线上重放，源又回来了。
所以除了查最终文件，还查"重放幂等"（把 dateless 批次重放到当前清单的副本上，必须零动作）。

三条判据都是离线的：不抓网络、不看时钟，可以进 blocking 门禁 A2。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

LIST_PATH = os.path.join(ROOT, "rss_sources.json")
TOOL_PATH = os.path.join(ROOT, "tools", "rss_source_list_patch.py")

# 逐条零日期、且没有任何备选地址能补上日期的源（探测记录见 specs 2026-09-20-rss-72h-retention-design §16/§17）
DATELESS_GONE = ["nikkei_rsshub_802", "喷嚏网铂程斋_11", "bbc英语教学_13", "中国日报双语_2",
                 "google_dev_76", "知乎日报anyfeeder_3"]
# 内容质量类删除：日期是好的，理由不能混进"没日期"那一堆
QUALITY_GONE = ["超能网_31"]
GONE = DATELESS_GONE + QUALITY_GONE
# 上游活着、证书过期这一类（用户 2026-09-21 判定删除）。
# 单独列而不是并进 GONE：它有自己的回补条件（上游续证即可原样回补），
# DEAD 类不行 —— 把理由混掉就会让人把 403 死源当"证书问题顺手修修"捡回来。
CERT_EXPIRED_GONE = ["轶哥博客_20", "虹线_697"]
# 曾经"没日期"其实是我们接错地址：官方镜像逐条带 pubDate，删掉就是净损失
DATED_MOVED = {
    "google_developers_blog_406": "https://blog.google/technology/developers/rss/",
    "美团技术团队_0": "https://tech.meituan.com/atom.xml",
    # 2026-10-05：不是停更是搬家。旧地址 200 但 0 条目，新地址实测 20 条全带日期+可用 link。
    "google_security_blog_785": "https://blog.google/security/rss/",
}
OLD_DATELESS_URLS = {
    "google_developers_blog_406": "https://developers.googleblog.com/feeds/posts/default",
    "美团技术团队_0": "https://tech.meituan.com/feed",
    "google_security_blog_785": "https://security.googleblog.com/atom.xml",
}
# 这批改动落地前的原始条目（取自 origin/main 的 1005 条清单），用于"声明真的能干活"的重放判据。
PRE_BATCH_DELETED = [
    {"key": "nikkei_rsshub_802", "name": "日经新闻", "cat": "news", "color": "#003366",
     "url": "https://rsshub.rssforever.com/nikkei/index", "tier": 2},
    {"key": "喷嚏网铂程斋_11", "name": "喷嚏网铂程斋", "cat": "news", "color": "#1565c0",
     "url": "https://plink.anyfeeder.com/dapenti/xilei", "tier": 2},
    {"key": "bbc英语教学_13", "name": "BBC英语教学", "cat": "news", "color": "#bb1919",
     "url": "https://plink.anyfeeder.com/bbc/learningenglish", "tier": 3},
    {"key": "中国日报双语_2", "name": "中国日报双语", "cat": "news", "color": "#cc0000",
     "url": "https://plink.anyfeeder.com/chinadaily/dual", "tier": 3},
    {"key": "google_dev_76", "name": "Google Developers", "cat": "tech", "color": "#4285f4",
     "url": "https://developers.googleblog.com/feeds/posts/default/", "tier": 3},
    {"key": "知乎日报anyfeeder_3", "name": "知乎日报anyfeeder", "cat": "news", "color": "#d32f2f",
     "url": "https://plink.anyfeeder.com/zhihu/daily", "tier": 2},
    {"key": "超能网_31", "name": "超能网", "cat": "cn_tech", "color": "#0091ea",
     "url": "https://plink.anyfeeder.com/expreview", "tier": 2, "pub_date_offset_min": -480},
]


def _tool():
    spec = importlib.util.spec_from_file_location("rss_source_list_patch", TOOL_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _replay(entries, scope="dateless"):
    """把补丁重放到给定清单上，返回重放后的清单（写盘走临时目录，不碰工作区）。"""
    tool = _tool()
    tmp = tempfile.mkdtemp(prefix="rsplist-")
    try:
        dst = os.path.join(tmp, "rss_sources.json")
        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))
        tool.PATH = dst
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):   # 工具打中文，Windows 控制台编码能炸
            tool.run(apply=True, scope=scope)
        with open(dst, encoding="utf-8") as f:
            return json.load(f), open(dst, "rb").read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _list_sources():
    with open(LIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_source_list_is_loaded_and_nontrivial():
    """防空跑：清单只有几条时后面所有"某 key 不存在"的断言都是白过的。

    这是同日对抗审查里点名的同类缺陷（隔离树 git ls-files 返回空 ⇒ 全仓扫描静默 no-op）。
    """
    src = _list_sources()
    assert len(src) > 500, "rss_sources.json 只读到 %d 条，判据视为无效" % len(src)
    keys = [s.get("key") for s in src]
    assert len(keys) == len(set(keys)), "清单有重复 key，后面的按 key 断言不可信"


def test_dateless_sources_stay_deleted():
    src = _list_sources()
    by_key = {s.get("key"): s for s in src}
    for key in GONE:
        assert key not in by_key, (
            "%s 又回到清单里了：它属于本批判定要剔除的源（无逐条日期或内容质量），"
            "重新加回请先给出对应理由" % key)


def test_no_source_keeps_the_old_dateless_blogger_url():
    """同一家出版方不能留两把钥匙，其中一把还指着逐条零日期的旧 Blogger 地址。

    google_dev_76 与 google_developers_blog_406 只差一个结尾斜杠。_406 换址之后，
    旧地址若还被某个 key 用着，那 20 条卡片就永远没有时间位（产物实测 time_str 全空）。
    """
    for s in _list_sources():
        url = (s.get("url") or "").rstrip("/")
        assert not url.endswith("developers.googleblog.com/feeds/posts/default"), (
            "%s 仍指向无日期的旧 Blogger 地址 %s" % (s.get("key"), s.get("url")))


def test_dateless_sources_not_in_loaded_module():
    """构建脚本实际加载的清单也必须与文件一致（防"改了文件但构建读的是另一份"）。"""
    mod = load_build()
    live = {s.get("key") for s in mod.RSS_SOURCES}
    assert len(live) > 500, "RSS_SOURCES 只加载到 %d 条" % len(live)
    for key in GONE:
        assert key not in live, "%s 仍在构建期生效" % key


def test_moved_sources_point_at_dated_feeds():
    by_key = {s.get("key"): s for s in _list_sources()}
    for key, want in DATED_MOVED.items():
        got = (by_key.get(key) or {}).get("url")
        assert got == want, "%s 的地址应是带逐条日期的 %s，实际 %r" % (key, want, got)
        assert got != OLD_DATELESS_URLS[key], "%s 退回了没有日期字段的旧地址" % key


def test_declaration_actually_produces_the_shipped_list():
    """把"改动前"的清单交给工具重放，必须正好得到现在这份 shipped 清单。

    这条查的是声明能不能干活，而不是文件看起来对不对。变异体 M6（工具里 `if apply and
    len(src) != n0` 的原写法）专治这种：纯改址不动条数，旧写法根本不写盘，
    于是"重放过了"是假的 —— 只查最终文件永远看不出这个分叉。
    """
    shipped = _list_sources()
    pre = [dict(s) for s in shipped]
    by_key = {s["key"]: s for s in pre}
    for key, old in OLD_DATELESS_URLS.items():
        by_key[key]["url"] = old
    pre.extend(dict(s) for s in PRE_BATCH_DELETED)

    got, _ = _replay(pre)
    keys = {s["key"] for s in got}
    for key in GONE:
        assert key not in keys, "重放声明后 %s 仍在清单 ⇒ 工具里的删除没生效" % key
    for key, want in sorted(DATED_MOVED.items()):
        got_url = next((s["url"] for s in got if s["key"] == key), None)
        assert got_url == want, "重放声明后 %s 的地址是 %r，应为 %r（旧写法会静默不落盘）" % (key, got_url, want)
    assert len(got) == len(shipped), "重放结果 %d 条，与线上清单 %d 条不等 ⇒ 批次隔离被破坏" % (
        len(got), len(shipped))


def test_url_only_replay_actually_persists():
    """只改址、不动条数的那一半也必须落盘。

    工具原写法是 `if apply and len(src) != n0`：条数没变就不写盘，于是"删 4 条"生效了、
    "换 2 个地址"静默丢失 —— 清单看起来重放过了，两个源还在抓没有日期字段的旧地址。
    上一用例里条数会变，掩盖了这个分支，所以单独一条把 count 不变的情形钉住。
    """
    shipped = _list_sources()
    pre = [dict(s) for s in shipped]
    by_key = {s["key"]: s for s in pre}
    for key, old in OLD_DATELESS_URLS.items():
        by_key[key]["url"] = old
    got, raw = _replay(pre)
    for key, want in sorted(DATED_MOVED.items()):
        got_url = next((s["url"] for s in got if s["key"] == key), None)
        assert got_url == want, (
            "%s 重放后仍是 %r：纯改址没写盘，源会继续抓到没有日期字段的地址" % (key, got_url))
    assert len(got) == len(shipped)


def test_dateless_batch_replay_is_a_noop():
    """把这一批重放到当前清单上必须零动作 —— 声明与文件长期一致的可核对形式。

    变异体 M1/M2（清单被回滚）与 M4（dateless 批次偷偷执行了全量补丁）都由本用例拦截。
    """
    before = open(LIST_PATH, "rb").read()
    _, after = _replay(json.loads(before.decode("utf-8")))
    assert after == before, (
        "dateless 批次重放后清单变了 ⇒ 文件与 tools/rss_source_list_patch.py 的声明不一致："
        "要么这批没重放完，要么有人手改了文件而没进声明")


def test_batch_keys_are_declared_in_the_tool():
    """判据与声明同源：测试里写死的 key 必须真的出现在工具声明里，
    否则工具被人删掉一批时测试会一直绿着（锁错了对象）。"""
    tool = _tool()
    # 两个理由桶分别锁死：把"内容质量"塞进"没日期"（或反之）虽然都删得掉，
    # 却会在清单里留下一条假原因 —— 超能网今天实测 30/30 带 pubDate。
    assert set(tool.DATELESS_DELETE_KEYS) == set(DATELESS_GONE), "无日期桶与判据不一致"
    assert set(tool.QUALITY_DELETE_KEYS) == set(QUALITY_GONE), "内容质量桶与判据不一致"
    assert tool.DATED_URL_FIX == DATED_MOVED


def test_every_declared_deletion_is_absent_from_the_list():
    """**所有**理由桶都要被"已删除"断言覆盖，不只是无日期/质量两桶。

    原先只有 GONE 进这条，DEAD 与证书桶没人管：把它们从工具里抹掉、或把源加回清单，
    测试会一路绿着。理由桶的分工也在这里锁：同一条 key 不得同时出现在两个桶里。
    """
    tool = _tool()
    buckets = {
        "DATELESS": set(tool.DATELESS_DELETE_KEYS),
        "QUALITY": set(tool.QUALITY_DELETE_KEYS),
        "DEAD": set(tool.DEAD_DELETE_KEYS),
        "CERT_EXPIRED": set(tool.CERT_EXPIRED_DELETE_KEYS),
        # 2026-10-05 批次 B 的两个新桶必须一起接进来：不接就等于"删 54 个源"这件事
        # 只由工具单方面声明，谁把桶清空或把源加回清单，这条判据都会一路绿着。
        "LINK_BIAS": set(tool.LINK_BIAS_DELETE_KEYS),
        "DORMANT": set(tool.DORMANT_DELETE_KEYS),
        # 12MB 上限桶同理：这 5 个在 CI 里看起来就是"源坏了"（error、0 条），
        # 而真实原因是我方 _fetch_url 读满 12MB 硬截断。桶在，理由才在。
        "OVERSIZE": set(tool.OVERSIZE_DELETE_KEYS),
    }
    by_key = {s.get("key"): s for s in _list_sources()}
    for name, keys in buckets.items():
        assert keys, "%s 桶空了：删除理由不能靠注释传，桶本身就是声明" % name
        for key in keys:
            assert key not in by_key, (
                "%s 桶里的 %s 又回到清单里了 —— 重新加回要么给出该桶的理由复核，"
                "要么从桶里移出并说明" % (name, key))
    names = list(buckets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            overlap = buckets[names[i]] & buckets[names[j]]
            assert not overlap, "%s 与 %s 两个理由桶重叠：%s —— 一条源只能有一个删除理由" % (
                names[i], names[j], sorted(overlap))


def test_cert_expired_sources_declared_in_their_own_bucket():
    """证书过期必须是独立桶，不能被折进 DEAD。

    这两种源的可救性完全不同：`www.wyr.me/rss.xml` 与 `1q43.blog/feed` 实测
    跳过校验后 HTTP 200 且含条目（上游活着、只是证书过期，续证即可原样回补）；
    DEAD 桶里那些是 403/404/空 feed，换地址或续证都救不回来。
    混进 DEAD 就等于在清单历史里留下一条假原因。
    """
    tool = _tool()
    assert set(tool.CERT_EXPIRED_DELETE_KEYS) == set(CERT_EXPIRED_GONE), \
        "证书过期桶与判据不一致"
    assert not (set(tool.CERT_EXPIRED_DELETE_KEYS) & set(tool.DEAD_DELETE_KEYS)), \
        "证书过期源被并进了死源桶 —— 理由被洗掉了"


def test_link_bias_bucket_is_pinned_and_dormant_bucket_is_not_silently_shrunk():
    """批次 B 两个新桶的"锁内容"判据。

    LINK_BIAS 整桶钉死：这 12 个的理由（上游近期条目无可取 link ⇒ 去重后只剩多年前旧剧集）
      是实测出来的、且**看起来完全能救**（feed 有内容、日期正常），所以最容易被后人
      "顺手改改解析器就加回来"。把 12 个 key 钉在判据里，加回任何一个都要先给复核理由。
    DORMANT 钉条数 + 三个最极端的 key：43 个 >3 月静默源逐条硬编码会随每次清理漂移，
      但"桶被整体掏空"必须当场红 —— 条数与最老样本双锁。
    """
    tool = _tool()
    LINK_BIAS_GONE = {
        "prof_g_markets_900", "decoder_with_nilay_patel_904", "big_technology_podcast_907",
        "real_eisman_playbook_908", "waveform_mkbhd_910", "the_vergecast_911",
        "morning_brew_daily_912", "super_data_science_podcast_913", "pivot_podcast_914",
        "compound_and_friends_915", "plain_english_derek_thompson_916", "riskreversal_pod_917",
    }
    assert set(tool.LINK_BIAS_DELETE_KEYS) == LINK_BIAS_GONE, \
        "链接偏置桶与判据不一致 —— 这 12 个是 2026-10-04 批次 A 的实测教训，加回需先复核"
    assert len(tool.DORMANT_DELETE_KEYS) >= 40, (
        "静默桶只剩 %d 个：整桶被掏空不是'这批做完了'，是判据失效" % len(tool.DORMANT_DELETE_KEYS))
    # 最老的三个（实测静默 1556 / 1074 / 764 天）：桶若被换血，这几个最先被牺牲
    for k in ("老钱说钱_223", "小胡子哥_44", "vue_blog_799"):
        assert k in tool.DORMANT_DELETE_KEYS, "静默桶里丢了 %s（>2 年无更新的最老样本）" % k
    by_key = {s.get("key"): s for s in _list_sources()}
    for k in LINK_BIAS_GONE | set(tool.DORMANT_DELETE_KEYS):
        assert k not in by_key, "%s 又回到清单里了 —— 每场白抓一次且永不出厂" % k


def test_oversize_bucket_is_pinned_and_conflict_declaration_is_refused():
    """12MB 上限桶（2026-10-06）的三条判据。

    机制已读远端 main 版代码定死：_fetch_url 的 `r.read(MAX_FEED_BYTES=12MB)` 截断
    整篇归档型播客 feed（实测 13.9~27.0MB，本机 2~3.5 秒即返回全部字节，故既非超时也非
    按 IP 拦），截断后的半篇 XML 交给 ET.fromstring 必 ParseError ⇒ per_source 记 error、0 条。
    ① 整桶钉死 5 个 key：这 5 个"看起来完全能救"（feed 活着、日期正常），最容易被顺手加回；
    ② ADD_SOURCES 里不得再出现它们 —— 否则 scope="all" 先删后加，删除被静默撤销；
    ③ 工具里那条"删/加撞车"断言必须真会红（变异自证），不能是恒绿摆设；
    ④ 批次隔离：把 oversize 批次重放到当前清单上必须零改动。
    """
    tool = _tool()
    OVERSIZE_GONE = {
        "latent_space_ai_engineer_podcast_920",   # api.substack.com       14.4MB
        "daily_paper_cast_924",                   # feeds.transistor.fm    14.8MB
        "startup_insider_931",                    # feeds.simplecast.com   27.0MB
        "side_hustle_school_943",                 # feeds.acast.com        22.2MB
        "everything_everywhere_daily_history_scie_944",  # feeds.megaphone.fm 13.9MB
    }
    assert set(tool.OVERSIZE_DELETE_KEYS) == OVERSIZE_GONE, "12MB 上限桶与判据不一致"
    add_keys = {s["key"] for s in tool.ADD_SOURCES}
    assert not (add_keys & OVERSIZE_GONE), (
        "ADD_SOURCES 里又有 %s：run() 的顺序是先删后加，这条声明会把刚摘掉的超大源"
        "原样补回来，清单看着打过补丁实则一条没少" % sorted(add_keys & OVERSIZE_GONE))
    by_key = {s.get("key"): s for s in _list_sources()}
    for k in OVERSIZE_GONE:
        assert k not in by_key, "%s 仍在清单里：每场白抓 12MB 且永不出厂" % k

    # ③ 变异自证：造一条"既声明删除又声明新增"的冲突，工具必须当场拒绝。
    # 必须打在 scope="all" 上：只有它把 adds 绑到 ADD_SOURCES，oversize/dateless 两批的
    # adds 都是 []，往 ADD_SOURCES 里塞撞车项在这两个 scope 下根本不会被读到 ——
    # 第一版就写成了 scope="oversize"，变异探针打在一处空转的分支上，看着自证其实恒绿。
    # 而"恢复清单用默认 --apply"正是将来最可能把超大源静默补回来的那条路径。
    tmp = tempfile.mkdtemp(prefix="rsplist-conflict-")
    try:
        dst = os.path.join(tmp, "rss_sources.json")
        with open(dst, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps([], ensure_ascii=False))
        tool.PATH = dst
        tool.ADD_SOURCES = list(tool.ADD_SOURCES) + [{
            "key": "daily_paper_cast_924", "name": "Daily Paper Cast", "cat": "podcast",
            "color": "#6366f1", "url": "https://feeds.transistor.fm/daily-paper-cast-ai",
            "tier": 3}]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                tool.run(apply=False, scope="all")
        except AssertionError as ex:
            assert "daily_paper_cast_924" in str(ex), (
                "断言是红了，但报的不是这个 key ⇒ 拦的是别的事，不能算拦住撞车")
        else:
            raise AssertionError(
                "删/加声明撞车时工具没拦：后面的 src.extend(added) 会静默撤销删除")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ③b 反向半：不加那条冲突项时 scope="all" 必须能正常走完。
    # 缺这一半时，"撞车断言"可能被一个无关的既有重叠常年挡住 ⇒ 上面的红看着像拦住撞车，
    # 其实拦的是别的事，而本批真正要防的静默回补反倒没人管。
    tool2 = _tool()
    tmp2 = tempfile.mkdtemp(prefix="rsplist-clean-")
    try:
        dst2 = os.path.join(tmp2, "rss_sources.json")
        with open(dst2, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps([], ensure_ascii=False))
        tool2.PATH = dst2
        with contextlib.redirect_stdout(io.StringIO()):
            tool2.run(apply=False, scope="all")   # 不该抛
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # ④ 批次隔离：已落地的清单上重放本批 = 零动作
    before = open(LIST_PATH, "rb").read()
    _, after = _replay(json.loads(before.decode("utf-8")), scope="oversize")
    assert after == before, "oversize 批次在当前清单上重放产生了改动 ⇒ 声明与文件不一致"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
