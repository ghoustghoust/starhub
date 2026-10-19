"""RSS 源清单变更的可重放补丁：按 key 删、按 key 改址、补缺失源。

为什么是脚本而不是一份最终文件：2026-09-20 与「72h 留存」改造并行，对方以已推远端的
版本为基线也在改 rss_sources.json。整文件覆盖会把对方对清单的增删静默抹掉，
所以这里只声明「对哪些 key 做什么」，在任何版本的清单上重放一遍即可。

幂等：目标 key 不存在（已被删/已改过）时只报告，不报错。
用法：python tools/rss_source_list_patch.py [--apply]   默认只 dry-run
"""
import json
import os
import sys

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "rss_sources.json")

# 已确认彻底失效或字面重复的源（依据见 docs/superpowers/plans/2026-09-20-rss-coverage-fix-pending-push.md）
DELETE_KEYS = {
    "cs_cl_updates_on_arxiv_org_599",   # 与 arXiv NLP 同一 feed（export 旧址）
    "cs_lg_updates_on_arxiv_org_740",   # 与 arXiv 机器学习 同一 feed
    "唐巧博客_28",                        # 与「唐巧的博客」同一 atom.xml
    "卫诗婕商业漫谈_6",                    # URL 与另一条字面全同
}
DELETE_BY_NAME = {
    # name: url 必须包含的子串（None = 不校验）
    "Codrops": None,                     # HTTP 410 Gone
    "Reuters World": None,               # 404，reutersagency.com 已废弃
    "謝懿Shine": None,                   # 域名停放页
    "FreeBuf 网络安全行业门户": None,        # 阿里云 WAF 滑块，无可用替代
    "AP News": None,                     # Cloudflare 全站挑战
    "CISA": None,                        # Akamai 拒所有 .xml
    "LlamaIndex Blog": None,             # 官方 RSS 已下线
    "MongoDB Blog": None,                # 官方 feed 404，Medium 镜像非官方域
    "FireCrawl Blog": None,              # api.bestblogs.dev 恒 0 条、从未产出
    "Groq": None,                        # 同上
    "Hugging Face": "wechat2rss",        # 桥接返回 "feed not found"
    "ShowMeAI研究中心": "wechat2rss",
    "Thoughtworks洞见": "wechat2rss",
    "yikai 的摸鱼笔记": "wechat2rss",
    "开源服务指南": "wechat2rss",
    "青哥谈AI": "wechat2rss",
}

# 换址：保留 key（同一出版方，历史数据继续挂同一信源）
URL_FIX = {
    "grafana_labs_382": "https://grafana.com/blog/index.xml",
    "langchain_blog_378": "https://www.langchain.com/blog/rss.xml",
    "hf_daily_papers_803": "https://rsshub.bestblogs.dev/huggingface/daily-papers",
    "nikkei_rsshub_802": "https://rsshub.bestblogs.dev/nikkei/index",
    "arxiv_cs_14": "https://rss.arxiv.org/rss/cs",
    "cs_cv_updates_on_arxiv_org_739": "https://rss.arxiv.org/rss/cs.CV",
}
NAME_FIX = {"cs_cv_updates_on_arxiv_org_739": "arXiv 计算机视觉"}
# 按 UA 拦的源（CloudFront 对 Chrome 串 403、对 curl 串 200）
UA_FIX = {"engadget_667": "curl/8.5.0", "engadget_4": "curl/8.5.0"}
# RSSHub 实例迁移：按 url 后缀路由匹配，key 可能随清单变动
ROUTE_FIX = [("/xiaoyuzhou/podcast/5f22729f9504bbdb77253e46",
              "https://rsshub.bestblogs.dev/xiaoyuzhou/podcast/5f22729f9504bbdb77253e46")]
ADD_SOURCES = [
    {"key": "rfi_cn_779", "name": "RFI 中文", "cat": "news", "color": "#8a6d1f",
     "url": "https://www.rfi.fr/cn/rss", "tier": 3},   # 顶替 France24 中文（其 feed 不存在）
    # ───────── 2026-10-04 批次 A：净新增播客供给（原 18 个，现存 6 个）─────────
    # 保留的硬结论（当时用 295 个缺失源直连 + Apple Podcasts + 第二套 wechat2rss 部署交叉对撞）：
    #   **镜像没有断供** —— 9 个双方都有的微信账号，两套独立部署给出完全相同的最后发布日期；
    #   09-22~09-27 那批集体静默是中秋(9-25)+国庆的假期停更，节后自行回升。
    #   所以"信源 673"是"近 7 天有货"的口径，不是配置数 968 —— 这批抬高的是底盘，不是修坏源。
    # 但本批当时的入选判据已被 10-05 复核证伪：只看"feed 里 7 天内有没有条目"+
    #   "同 host 在 CI 日志有 status=ok"，既没验"我方能否取到可用 link"，也没管单域并发。
    #   结果 18 个里 12 个永不出厂（原因见 LINK_BIAS_DELETE_KEYS）。修正后的判据见下方批次 B。
    {"key": "david_senra_901", "name": "David Senra", "cat": "podcast", "color": "#0891b2",
     "url": "https://feeds.megaphone.fm/david-senra", "tier": 3},
    {"key": "晚安咖啡_902", "name": "晚安咖啡GoodNightCoffee", "cat": "podcast", "color": "#0891b2",
     "url": "https://feed.xyzfm.space/nf4qg8uypmrv", "tier": 3},
    {"key": "seventy3_903", "name": "Seventy3", "cat": "podcast", "color": "#d97706",
     "url": "https://feed.xyzfm.space/7g77eb3rfju8", "tier": 3},
    {"key": "隔夜市场_905", "name": "隔夜市场", "cat": "podcast", "color": "#4285f4",
     "url": "https://feed.xyzfm.space/xmugmcenwnga", "tier": 3},
    {"key": "the_exchange_906", "name": "The Exchange", "cat": "podcast", "color": "#d32f2f",
     "url": "https://feeds.simplecast.com/tc4zxWgX", "tier": 3},
    {"key": "hard_fork_909", "name": "Hard Fork", "cat": "podcast", "color": "#d32f2f",
     "url": "https://feeds.simplecast.com/6HKOhNgS", "tier": 3},
    # ───────── 2026-10-05 批次 B：净新增 25 个（播客为主）─────────
    # 入选判据是批次 A 的修正版：不再只看"feed 里 7 天内有没有条目"，而是
    #   ① 可取 link 且 ≤168h 的条目 ≥3（link 口径与 _parse_rss_item 一致，含 Atom <link href>）
    #   ② 自身中位发布间隔 ≤168h  ③ 最新一条 ≤168h
    # 外加两条批次 A 没做的约束：**单域 ≤4 个**（A 把 13 个压在同一 CDN 上，一次模板缺陷全灭）
    # 与主题相关性人工收口（iTunes 机械检索出的 64 个里，体育/真人秀/德国政治/赌博引流站占大半）。
    {"key": "tbpn_921", "name": "TBPN", "cat": "podcast", "color": "#d97706",
     "url": "https://feeds.transistor.fm/technology-brother", "tier": 3},
    {"key": "machine_learning_tech_brief_by_hackernoo_922", "name": "Machine Learning Tech Brief By HackerNoon", "cat": "podcast", "color": "#e61919",
     "url": "https://feeds.transistor.fm/machine-learning-tech-brief-by-hackernoon", "tier": 3},
    {"key": "programming_tech_brief_by_hackernoon_923", "name": "Programming Tech Brief By HackerNoon", "cat": "podcast", "color": "#6366f1",
     "url": "https://feeds.transistor.fm/programming-tech-brief-by-hackernoon", "tier": 3},
    {"key": "ai_news_minute_925", "name": "AI News Minute", "cat": "podcast", "color": "#0891b2",
     "url": "https://automatedpodcasts.com/podcast_rss/019c8c37-3feb-7940-ab90-2c00fad127c4", "tier": 3},
    {"key": "the_ai_daily_brief_artificial_intelligen_926", "name": "The AI Daily Brief: Artificial Intelligence News and Analysis", "cat": "podcast", "color": "#ff6600",
     "url": "https://anchor.fm/s/f7cac464/podcast/rss", "tier": 3},
    {"key": "hacker_news_daily_927", "name": "Hacker News Daily", "cat": "podcast", "color": "#10a37f",
     "url": "https://feed.huisheng.fm/feeds/cf-0fa5c88a-hacker-news-daily-bhmh/feed.xml", "tier": 3},
    {"key": "security_spoken_928", "name": "Security, Spoken", "cat": "podcast", "color": "#6366f1",
     "url": "https://feeds.megaphone.fm/CNE5644732130", "tier": 3},
    {"key": "wsj_tech_news_briefing_929", "name": "WSJ Tech News Briefing", "cat": "podcast", "color": "#4285f4",
     "url": "https://video-api.shdsvc.dowjones.io/api/podcasts/feed/the%20wall%20street%20journal%20tech%20talk", "tier": 3},
    {"key": "daily_tech_news_show_930", "name": "Daily Tech News Show", "cat": "podcast", "color": "#4285f4",
     "url": "https://feeds.acast.com/public/shows/69874998-717b-4db3-9857-c07cf9597f55", "tier": 3},
    {"key": "code_story_startup_podcast_for_ctos_ceos_932", "name": "Code Story | Startup Podcast for CTOs, CEOs and Technical Founders", "cat": "podcast", "color": "#24292e",
     "url": "https://rss.introcast.io/1466861744/feeds.redcircle.com/ac5e79a4-0405-49a3-af2c-02c37f0b3879", "tier": 3},
    {"key": "ev_news_933", "name": "ev.news", "cat": "podcast", "color": "#24292e",
     "url": "https://audioboom.com/channels/5051980.rss", "tier": 3},
    {"key": "bestblogs_934", "name": "BestBlogs", "cat": "podcast", "color": "#6366f1",
     "url": "https://feed.xyzfm.space/gpul9qw8appt", "tier": 3},
    {"key": "聊聊Sci_935", "name": "聊聊Sci", "cat": "podcast", "color": "#4285f4",
     "url": "https://feed.xyzfm.space/ppwu97xrxj94", "tier": 3},
    {"key": "科技最前沿_936", "name": "科技最前沿 | 最新科技前沿解读，做有态度的科技课", "cat": "podcast", "color": "#7c3aed",
     "url": "http://www.ximalaya.com/album/6748227.xml", "tier": 3},
    {"key": "每日AI_937", "name": "每日AI", "cat": "podcast", "color": "#6366f1",
     "url": "https://anchor.fm/s/10f187f58/podcast/rss", "tier": 3},
    {"key": "科技報橘_938", "name": "科技報橘", "cat": "podcast", "color": "#d32f2f",
     "url": "https://feeds.soundon.fm/podcasts/ead686e9-4513-4217-beb5-5fa4d215860d.xml", "tier": 3},
    {"key": "果仁聊科技_939", "name": "果仁聊科技", "cat": "podcast", "color": "#24292e",
     "url": "https://feeds.soundon.fm/podcasts/d5f6f588-d93a-4876-9943-255c48cc16da.xml", "tier": 3},
    {"key": "全球科技金融3分钟_940", "name": "全球科技金融3分钟", "cat": "podcast", "color": "#0891b2",
     "url": "https://feed.xyzfm.space/u37qn3c4eh9a", "tier": 3},
    {"key": "睡前短资讯_941", "name": "睡前短资讯", "cat": "podcast", "color": "#d97706",
     "url": "https://feed.xyzfm.space/rkue48tfd8yk", "tier": 3},
    {"key": "the_best_one_yet_942", "name": "The Best One Yet", "cat": "podcast", "color": "#6366f1",
     "url": "https://feeds.acast.com/public/shows/69545da8cb029db7575279fc", "tier": 3},
    # ───────── 2026-10-06：本列表里原有的 5 条（_920/_924/_931/_943/_944）已摘出 ─────────
    # latent_space_ai_engineer_podcast_920 / daily_paper_cast_924 / startup_insider_931 /
    # side_hustle_school_943 / everything_everywhere_daily_history_scie_944。
    # 原因不是"源坏了"，是我方抓取层读满 12MB 就截断（见 OVERSIZE_DELETE_KEYS）。
    # 若日后聚合器改为"按最后一个完整 </item> 收边再解析"，这 5 条应从那里原样回补。
]
DROP_THEN_ADD = {"france24_zh_779": "rfi_cn_779"}

# ───────────────── 2026-09-21 批次：逐条没有发布日期的源 ─────────────────
# 单独成批、可单独重放（--scope dateless）。理由：远端 rss_sources.json 至今仍是
# **没打过任何补丁的 1005 条**（上面那些删除/改址只存在于本地提交里，工具文件本身
# 也从没进远端），把两批混推等于让一批 09-20 的旧判定跟着今天的实测一起落地。
# 旧判定确实会过期：caixin_latest_rsshub_806 当时记为"响应 0 字节的死源"，
# 今天重测是 20/20 条带 pubDate 的正常源。
DATELESS_DELETE_KEYS = {
    # 判据：修复 _RSS_DATE_RE（支持裸两位偏移 +08）之后重测，feed 里连日期元素都不存在
    # ⇒ 唯一的时钟只剩"首次抓到"，卡片上显示的日期必然是收录时间冒充发布时间。
    "nikkei_rsshub_802",      # 远端现行 rssforever 实例抓到 0 条；bestblogs 镜像 28 条、零日期
    "喷嚏网铂程斋_11",          # plink.anyfeeder 代理结构性丢日期：14 条仅 title/link/content
    "bbc英语教学_13",           # 同上：5 条
    "中国日报双语_2",            # 同上：5 条
    # 与 google_developers_blog_406 是同一家：地址只差一个结尾斜杠，逐条零日期，
    # 出口 20 条卡片连时间位都是空的。_406 已换到逐条带 pubDate 的官方地址，
    # 这个重复钥匙留着只多一份空卡片和一次白抓。
    "google_dev_76",
    # 部分条目无日期也算无日期：实测 30 条里只有 4 条带 pubDate，且那 4 条全是 23:00 批次戳，
    # 产物里 26 条卡片连时间位都是空的（判据口径见 specs §17）。
    "知乎日报anyfeeder_3",
}
# 内容质量类删除：**日期没问题**，纯粹是判定这个源的内容不值得占配额。
# 单独成块的原因：混进 DATELESS_DELETE_KEYS 等于在清单里留下一条假原因，
# 下一个人会以为超能网的 feed 缺日期，而它今天实测 30/30 带 pubDate。
QUALITY_DELETE_KEYS = {
    "超能网_31",               # 2026-09-21 用户判定"内容质量不行"
}
# 2026-09-21 现场重测（195 场生产日志 + 逐源双次探针，见
# docs/superpowers/specs/2026-09-21-rss-source-failure-analysis.md）新增的死源。
# 判据不是"日志里 empty"，而是当场拿到的状态码/字节：
DEAD_DELETE_KEYS = {
    "linuxdo_latest_59",      # latest.rss / c/:rss 双路径 + curl UA 全 HTTP 403（全站挑战，IP 级）
    "linuxdo_top_60",         # 同上
    "linuxdo_posts_61",       # 同上
    "halfrost_25",            # SSL: CERTIFICATE_VERIFY_FAILED，双次一致 ⇒ 永远抓不到
    "tianyu2fm_—_对谈未知领域_13",  # SSL UNEXPECTED_EOF_WHILE_READING，双次一致
    "拾月的博客_693",            # 200 但响应 650 字节、raw 里 0 个 item ⇒ 上游空 feed
    # 2026-09-21 二次实测：rss.cnn.com 两个 feed 的 pubDate 停在 2023-04，标题也还是
    # 特朗普被起诉/Dominion 那批 —— 上游三年前就停更，26/28 条龄期约 3 万小时，永远过不了
    # 72h 窗；唯一还能出现在页面上的反而是那 4/2 条没有 pubDate、靠 first_seen 续命的条目
    # （等于死源在刷存在感）。按用户规则：过时源删。
    "cnn_intl_781",
    "CNN_16",
}
# 上游活着、证书过期这一类：2026-09-21 CI 日志与本机复核一致 —— 默认信任库握手报
# SSL: CERTIFICATE_VERIFY_FAILED: certificate has expired，而**跳过校验后两者都 HTTP 200 且含条目**
# （轶哥博客 200000 字节、虹线 200000 字节，raw 里都有 item）。
# 单独成桶的原因：它既不是"上游死了"也不是"我方解析缺口"。放进 DEAD_DELETE_KEYS 会让下一个
# 人以为换地址也救不回来；而这里的实情是"源活着，是我们不放宽 TLS 校验"——
# 将来若上游续了证书，这一桶是唯一允许原样回补的（DEAD 桶回补等于把 403 死源捡回来）。
# 用户 2026-09-21 判定：删除（读者点进去同样撞浏览器不安全警告，留着等于留一个坏入口）。
CERT_EXPIRED_DELETE_KEYS = {
    "轶哥博客_20",              # https://www.wyr.me/rss.xml
    "虹线_697",                 # https://1q43.blog/feed
}

# 现场重测后**明确保留**的两个，理由写在这里防止下一个人当死源删掉：
#   安全客_664        —— raw 有 20 个 item、我方解析 0 条：这是解析缺陷，不是上游死
#   elevate_430 / ai_musings_by_mu_421 —— 本机抓得到 20 条（curl UA），CI 侧 403
#                       ⇒ substack 按出口 IP 挑战，属基础设施限制，不是源坏

# ───────── 2026-10-05 批次 B：两类"每场白抓、永不出厂"的源 ─────────
# 与既有四桶的分工：DATELESS=feed 压根没日期；DEAD=403/404/空 feed 抓不到；
# CERT_EXPIRED=上游活着证书过期；QUALITY=内容不行。这两桶都是"抓得到、有日期、
# 有内容"，但结构性过不了留存闸门，所以既不能塞进 DEAD（会说谎：换地址救不回来
# 也不是它的原因），也不能塞进 DATELESS（它日期好好的）。
LINK_BIAS_DELETE_KEYS = {
    # 上游近期剧集把 <link> 留空、URL 只放在 <guid isPermaLink="false"> 里且值是 UUID，
    # 我方按设计拒绝非 permalink 的 guid（宁可不给链接也不给点开 404 的链接）⇒
    # 去重后只剩多年前的旧剧集 ⇒ 闸门清空整源。实测：Vergecast 剩的 10 条最新 2.6 年前、
    # Super Data Science 41 条最新 1.5 年前、Compound and Friends 264 条最新 3.3 年前。
    # 同 host 的 david_senra_901 不在此列：它的 feed 逐条带真 link，正常出厂。
    "big_technology_podcast_907",               # Big Technology Pod
    "compound_and_friends_915",               # The Compound and F
    "decoder_with_nilay_patel_904",               # Decoder with Nilay
    "morning_brew_daily_912",               # Morning Brew Daily
    "pivot_podcast_914",               # Pivot
    "plain_english_derek_thompson_916",               # Plain English with
    "prof_g_markets_900",               # Prof G Markets
    "real_eisman_playbook_908",               # The Real Eisman Pl
    "riskreversal_pod_917",               # RiskReversal Pod
    "super_data_science_podcast_913",               # Super Data Science
    "the_vergecast_911",               # The Vergecast
    "waveform_mkbhd_910",               # Waveform: The MKBH
}
DORMANT_DELETE_KEYS = {
    # 逐源直连实测：feed 可正常解析、最新一条却已 >3 月（最老 1556 天）。
    # 留存闸门硬上限 168h，这类源永远不可能出厂，留着只多一份白抓和一个虚低的信源数。
    # 已先探过同域替代 feed 路径：43 个里只有 Google Security Blog 是"搬家"（见 URL_FIX），
    # 其余 42 个确无可用替代地址。
    "a_list_apart_787",
    "admin_744",
    "ai闲谈_128",
    "barsee_heybarsee_530",
    "chrome_developer_blog_795",
    "dbanotes_45",
    "deepzz_26",
    "fellou_fellouai_552",
    "flowiseai_flowiseai_507",
    "geekplux_21",
    "joshcomeau_5",
    "laike9m_s_blog_612",
    "maxos_19",
    "monica_im_hey_im_monica_573",
    "onev_s_den_616",
    "pseudoyu_617",
    "randy'sblog_12",
    "studyinglover_s_blog_751",
    "ux_magazine_394",
    "velas电波站_692",
    "vue_blog_799",
    "webdev_3",
    "weishu_s_notes_634",
    "xuanwo_23",
    "yangxuan_s_blog_752",
    "小lin说的公众号_373",
    "小胡子哥_44",
    "得意忘形_7",
    "披萨盒的赛博日志_755",
    "捕蛇者说_58",
    "晚点对话_255",
    "有赞coder_122",
    "梁永安的播客_49",
    "王登科dk_43",
    "老钱说钱_223",
    "艾逗笔_233",
    "蒋方舟·一寸_43",
    "赫赫文王_733",
    "跑步指南_263",
    "转转技术_285",
    "风叔云_185",
    "飞哥说ai_175",
}

# ───────── 2026-10-06：feed 体量超过抓取上限，必然解析失败 ─────────
# 机制（读远端 main 版 build_rss_aggregator.py 定死，非推测）：
#   _fetch_url 用 `r.read(MAX_FEED_BYTES)`，MAX_FEED_BYTES = 12*1024*1024（:1131、:3124）。
#   整篇剧集归档型播客 feed 一次返回 14~27MB ⇒ 读满 12MB 硬截断 ⇒ 紧接着的
#   `ET.fromstring(raw)` 拿到半篇 XML 必 ParseError ⇒ _fetch_rss 返回 None ⇒ per_source 记 error、0 条。
# 现场称量（本机直连，五个都 2~3.5 秒返回全部字节，所以**不是**超时也不是按 IP 拦）：
#   startup_insider 27.0MB / side_hustle_school 22.2MB / daily_paper_cast 14.8MB /
#   latent_space_ai_engineer_podcast 14.4MB / everything_everywhere_daily 13.9MB。
# 把 20 个非 ok 源全量称过：只有这 5 个 >12MB，其余体量正常（钛媒体 238KB 那批是 CI 侧
# 出口 IP 挑战，reddit 那 6 个是限速）⇒ 这是清单选择问题，不是一个大面积隐故障。
# 为什么单独成桶而不塞进 DEAD：上游活得好好的，是我方上限把它拒了。将来聚合器改成
# "解析失败且 raw 恰为 12MB 时按最后一个完整 </item> 收边再解析"（ITEMS_PER_SOURCE=30
# 取的就是头部最新 30 条，收边不丢出厂内容），这 5 条应原样回补 —— 只有这一桶允许。
OVERSIZE_DELETE_KEYS = {
    "latent_space_ai_engineer_podcast_920",
    "daily_paper_cast_924",
    "startup_insider_931",
    "side_hustle_school_943",
    "everything_everywhere_daily_history_scie_944",
}

DATED_URL_FIX = {
    # 这两个不是"上游不给日期"，是我们接的地址不给 —— 换址就保住内容，删掉是净损失。
    # 备选地址都过真实解析器复测：逐条 pub_date 命中 100%，且不是同一分钟的批次时间。
    "google_developers_blog_406": "https://blog.google/technology/developers/rss/",
    "美团技术团队_0": "https://tech.meituan.com/atom.xml",
    # 不是停更，是搬家：security.googleblog.com/atom.xml 现在返回 200 但 0 条目，
    # 新地址实测 20 条全部带日期+可用 link（最新 2026-10-01）。保留 key ⇒ 历史数据继续挂同一信源。
    "google_security_blog_785": "https://blog.google/security/rss/",
}


def run(apply=False, scope="all"):
    src = json.load(open(PATH, encoding="utf-8"))
    n0 = len(src)
    log = []
    by_key = {s["key"]: s for s in src}

    # scope="dateless" 只重放 2026-09-21 那一批；其余改动留给各自的批次落地。
    # 注意它**不含 CERT_EXPIRED_DELETE_KEYS**：证书桶是同日稍后的另一批，故意隔开，
    # 这样"上游续证 → 从这一桶移出即可回补"不会顺手把 09-21 的无日期批次也重放了。
    # ⇒ 恢复清单时用默认 `--apply`（scope=all）；用 scope="dateless" 会得到 970 而不是 968。
    if scope == "all":
        del_keys = (DELETE_KEYS | DATELESS_DELETE_KEYS | QUALITY_DELETE_KEYS
                    | DEAD_DELETE_KEYS | CERT_EXPIRED_DELETE_KEYS
                    | LINK_BIAS_DELETE_KEYS | DORMANT_DELETE_KEYS | OVERSIZE_DELETE_KEYS)
        del_names, drop_then_add = DELETE_BY_NAME, DROP_THEN_ADD
        url_fix = dict(URL_FIX)
        url_fix.update(DATED_URL_FIX)
        name_fix, ua_fix, route_fix, adds = NAME_FIX, UA_FIX, ROUTE_FIX, ADD_SOURCES
    elif scope == "dateless":
        del_keys = DATELESS_DELETE_KEYS | QUALITY_DELETE_KEYS | DEAD_DELETE_KEYS
        del_names, drop_then_add = {}, {}
        url_fix, name_fix, ua_fix = dict(DATED_URL_FIX), {}, {}
        route_fix, adds = [], []
    elif scope == "oversize":
        # 只摘这 5 条。批次 B 的 LINK_BIAS/DORMANT 两桶已随 2d34ae11 落地，混着重放
        # 等于让上一批的判定再动一次清单，改动面就说不清了。
        del_keys = OVERSIZE_DELETE_KEYS
        del_names, drop_then_add = {}, {}
        url_fix, name_fix, ua_fix, route_fix, adds = {}, {}, {}, [], []
    else:
        raise SystemExit("未知 scope: %s" % scope)

    # 删除桶与 ADD_SOURCES 不能同时声明同一个 key：下面的顺序是"先删后补"，
    # 撞车时删除会被静默撤销，清单看上去打过补丁、实际一条没少。
    conflict = del_keys & {s["key"] for s in adds}
    assert not conflict, "同一 key 既声明删除又声明新增: %s" % sorted(conflict)

    gone = [k for k in del_keys if k in by_key]
    for nm, frag in del_names.items():
        for s in src:
            if s["name"] == nm and (frag is None or frag in (s.get("url") or "")):
                gone.append(s["key"])
    for k in drop_then_add:
        if k in by_key:
            gone.append(k)
    gone = set(gone)
    declared = len(del_keys | set(del_names) | set(drop_then_add))
    src = [s for s in src if s["key"] not in gone]
    log.append("删除 %d 条（清单中已不存在的目标：%d）" % (len(gone), declared - len(gone)))

    changed = 0
    for s in src:
        k = s["key"]
        if k in url_fix and s.get("url") != url_fix[k]:
            s["url"] = url_fix[k]; changed += 1
        if k in name_fix and s.get("name") != name_fix[k]:
            s["name"] = name_fix[k]; changed += 1
        if k in ua_fix and s.get("ua") != ua_fix[k]:
            s["ua"] = ua_fix[k]; changed += 1
        for suffix, newurl in route_fix:
            if (s.get("url") or "").endswith(suffix) and s.get("url") != newurl:
                s["url"] = newurl; changed += 1
    log.append("改址/加字段 %d 处" % changed)

    have = {s["key"] for s in src}
    added = [s for s in adds if s["key"] not in have]
    src.extend(added)
    log.append("新增 %d 条" % len(added))

    keys = [s["key"] for s in src]
    assert len(keys) == len(set(keys)), "出现重复 key"
    for s in src:
        assert s.get("key") and s.get("name") and (s.get("url") or "").startswith("http"), s

    print(" | ".join(log))
    print("源数 %d -> %d%s" % (n0, len(src), "  （dry-run，未写盘）" if not apply else ""))
    # 只改址不改条数时也必须写盘：旧条件 len(src) != n0 会让纯 URL_FIX 的重放静默不落盘，
    # 于是"补丁已重放"成了假话 —— 清单与声明长期不一致正是这个工具要防的事。
    if apply and (len(src) != n0 or changed or added):
        with open(PATH, "w", encoding="utf-8", newline="") as f:
            f.write(json.dumps(src, ensure_ascii=False, separators=(",", ":")))
        print("已写回 rss_sources.json")
    elif apply:
        print("无改动，不写盘")
    return len(src)


if __name__ == "__main__":
    # 除 --apply 外的第一个参数即 scope（缺省 all），所以调用形如
    # `python tools/rss_source_list_patch.py oversize --apply`。写成通用取值而不是再叠一个
    # 三元判断，是为了让新批次不需要回来改这里。
    run(apply="--apply" in sys.argv,
        scope=next((a for a in sys.argv[1:] if a != "--apply"), "all"))
