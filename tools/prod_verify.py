# -*- coding: utf-8 -*-
"""推下一批之前的生产验证门槛（判据：tests/tools/test_prod_verify_verdict.py）。

存在的理由是一条真实事故：Vercel 部署连续 6 场没落地，而我照样一批批往下推 ——
把"CI 绿"当成了"改动生效"。从此远端写入前必须先跑本工具，产出时间戳；
`tools/data_api_push.py` 见不到新鲜的时间戳就拒绝推送。

用法：
  py -3.11 tools/prod_verify.py            # 现取读数 → 写 .deploy-tmp/_prod_verify.json
  py -3.11 tools/prod_verify.py --print    # 只打印，不落时间戳（不给推送用）
分三级：blocking（站点/API 现在是坏的，拒绝推）/ warn（能推，但把未闭环的话写进推送日志）/ ok。
已知未修的问题不该永久卡住进度 —— 那只会逼人绕门槛，所以它判 warn 并把证据带上。
"""
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = os.path.join(ROOT, ".deploy-tmp", "_prod_verify.json")
MAX_AGE_S = 90 * 60          # 过期戳不算验证过
PAGES_BASE = "https://kwei168.github.io/starhub/"
VERCEL_BASE = "https://starhub-refresh.vercel.app/"
PAGES = ("index.html", "ai-daily.html", "rss-aggregator.html", "daily-insight-history.html")
API = ("api/rss", "api/news")
REPO = "Kwei168/starhub"


def verdict(r):
    """读数 → (level, why)。纯函数，不发网络，判据钉的就是这张表。"""
    for name, code in sorted((r.get("pages") or {}).items()):
        if code != 200:
            return "blocking", "站点页面 %s 是 %s（站点现在是坏的，先修它再谈推送）" % (name, code)
    for name, code in sorted((r.get("api") or {}).items()):
        if code is None:
            # 403 = Vercel Bot Protection 拦本机 curl（01:49 实跑撞上：api/news 被判成"API 坏了"）。
            # 读不到既不能算坏（假警会永久卡住推送），也不能算好（那就是我一路在防的假绿）。
            continue
        if code not in (200, 400):        # api/rss 无参时按 400 也算活着
            return "blocking", "Vercel 函数 %s 是 %s（API 主机坏了）" % (name, code)
    unread = sorted(k for k, v in (r.get("api") or {}).items() if v is None)
    if not (r.get("pages") and r.get("api")):
        return "blocking", "取数失败：页面或 API 一条读数都没有 ⇒ 取不到不等于没问题"
    runs = r.get("last_runs") or []
    if not runs:
        return "blocking", "取数失败：一场都读不到 ⇒ 没有可判的依据"
    # `pages is None` = 那场**还没走到** Pages（在飞、或刚被触发），不是"没发布"。
    # 把在飞的场读成未发布会造成假警（01:44 实跑撞上：我刚触发 1593，门槛立刻报它未发布），
    # 而假警的下一站就是有人给门槛加绕过开关。
    done = [x for x in runs if x.get("pages") is not None]
    if not done:
        return "blocking", "取数失败：读到的场都还在跑（没有 Pages 结论）⇒ 没有可判的发布事实"
    inflight = [x.get("num") for x in runs if x.get("pages") is None]
    latest = done[-1]
    warns = []
    if latest.get("pages") != "success":
        warns.append("上一场已结束的构建 %s 未发布 Pages（=%s）⇒ 站点停在更早的产物" % (
            latest.get("num"), latest.get("pages")))
    finished_vercel = [x for x in done if x.get("vercel") is not None]
    stuck = [x for x in finished_vercel if x.get("vercel") != "success"]
    if len(stuck) >= 3 and stuck == finished_vercel[-len(stuck):]:
        warns.append("Vercel 连续 %d 场（%s–%s）未落地 ⇒ 依赖 Vercel 部署的改动在生产上还没生效" % (
            len(stuck), stuck[0].get("num"), stuck[-1].get("num")))
    if r.get("public_internal"):
        warns.append("Vercel 域仍公开 %d 个内部项：%s" % (
            len(r["public_internal"]), ", ".join(r["public_internal"][:6])))
    if unread:
        warns.append("Vercel 函数这条通道读不到（多半是 Bot Protection 拦本机）：%s ⇒ 算未验证，不算过"
                     % ", ".join(unread))
    note = "（最新一场 %s 仍在跑，不计入判断）" % inflight[-1] if inflight else ""
    if warns:
        return "warn", "；".join(warns) + (("；" + note) if note else "")
    return "ok", "站点与 API 都活着，上一场已发布，公开面已关" + ("；" + note if note else "")


def stamp_is_fresh(stamp, now=None):
    now = time.time() if now is None else now
    ts = stamp.get("ts") if isinstance(stamp, dict) else None
    return isinstance(ts, (int, float)) and (now - ts) < MAX_AGE_S


# ── 取数（真跑时才会用到；判据不碰这一层）──────────────────────────────────────
def _code(url):
    """状态码；**403 与取不到都返回 None（=未知），不当成"坏"也不当成"没暴露"**。

    必须用 curl 而不是 urllib：这台机器只有 curl 会读 `*_PROXY`（01:52 实踩 —— 我把它改成
    urllib 之后，17 个内部项从"探得到"集体退化成"读不到"，门槛当场失去依据）。
    403 有两种成因且状态码分不开：Vercel Bot Protection 判本机为挑战、或我们自己的
    `api/events.js` 对缺失 Origin 返回 403 ⇒ 只能老实说"读不到"。
    """
    p = subprocess.run(["curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
                        "--max-time", "25", url], capture_output=True, text=True)
    try:
        code = int((p.stdout or "").strip())
    except ValueError:
        return None
    return None if code in (0, 403) else code


def read_public_internal():
    """改前基线里"本来 200、且不该公开"的名字，现在还有几个仍开着。"""
    base = os.path.join(ROOT, ".deploy-tmp", "_vercel_pre_baseline.json")
    if not os.path.exists(base):
        # 不能返回空：空 = verdict 里的"公开面已关" ⇒ 基线一丢门槛就假报 OK（这正是要防的空集形状）
        return ["(改前基线缺失：无法核对公开面，先重跑 _verify_build 的枚举再谈收口)"]
    names = internal_names(json.load(io.open(base, encoding="utf-8")))
    probed = [(n, _code(VERCEL_BASE + n)) for n in names]
    out = [n for n, c in probed if c == 200]
    unk = [n for n, c in probed if c is None]
    if unk:
        # 读不到 ≠ 已关。这一条若沉默，门槛会把"我探测不到"报成"公开面已收口"（假绿）。
        out.append("(%d 条读不到、未验证：%s)" % (len(unk), ", ".join(unk[:4])))
    return out


# 这些名字公开是**应该**的：四页与 rss_sources.json 是壳页/数据源，api/ 与 lib/ 是函数本体和被 require 的共享码。
# 把它们算进"暴露清单"会让门槛天天报假警，而假警的下一站就是有人把门槛关掉。
RUNTIME_KEEP = PAGES + API + ("rss_sources.json",)


def internal_names(baseline):
    """纯函数：从"改前状态存档"里挑出本来 200、且属于不该公开的名字。

    抽出来是因为筛法一旦写错（漏项或把运行时当暴露），网络层没法测：
    判据 = tests/tools/test_prod_verify_verdict.py::test_internal_selector_keeps_only_real_exposure。
    改前就已经 404 的一律不算 —— 那是 Vercel 平台自带的排除，不能冒充我们的功劳。
    """
    return [k for k, v in baseline.items()
            if v == "200" and not k.startswith(RUNTIME_KEEP) and not k.startswith(("api/", "lib/"))]


PUBLISH_WORKFLOW = "Update Star Hub"   # 唯一会写 Pages 的车道


def select_publish_runs(runs, n=8, workflow=PUBLISH_WORKFLOW):
    """只保留"会产出发布事实"的那条车道，取最近 n 场（新→旧的原顺序进、按旧→新出）。

    为什么必须有这一层（2026-10-08 22:19 实测）：取样曾经不分 workflow 直接取最近 8 场，
    而 star-fast 每 15 分钟一场 ⇒ 近 12 场里 10 场是 star-fast。那些场**根本没有
    `Deploy to GitHub Pages` 步**，读出来 `pages=None`；`verdict()` 把 None 读成"还没走到 Pages"
    （那是对在飞场的正确解释），于是 `done` 为空 ⇒ 门槛判 BLOCKING「读到的场都还在跑」。
    后果不是误报那么简单：小时场在飞的 20~40 分钟里门槛**每次**都挡，正好挡在我需要推送的缝上。
    改名/删掉这条车道 ⇒ 这里返回空 ⇒ verdict 报"一场都读不到"（响亮地坏，不是安静地绿）。
    """
    picked = [r for r in runs if (r.get("name") or "") == workflow][:n]
    return list(reversed(picked))


def read_runs(n=8):
    import subprocess
    # 30 是为了过滤后仍能凑够 n 场 update（star-fast 占比约 10/12，取 8 会一场 update 都不剩）
    p = subprocess.run(["gh", "api", "repos/%s/actions/runs?per_page=30" % REPO],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode:
        return []
    out = []
    for r in select_publish_runs(json.loads(p.stdout).get("workflow_runs", []), n=n):

        j = subprocess.run(["gh", "api", "repos/%s/actions/runs/%s/jobs" % (REPO, r["id"])],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        con = {}
        if not j.returncode:
            for s in (json.loads(j.stdout).get("jobs") or [{}])[0].get("steps", []):
                con[s["name"]] = s["conclusion"]
        out.append({"num": r["run_number"],
                    "pages": next((v for k, v in con.items() if k.startswith("Deploy to GitHub Pages")), None),
                    "vercel": next((v for k, v in con.items() if k.startswith("Deploy to Vercel")), None)})
    return out


def main(argv=None):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    readings = {
        "pages": {p: _code(PAGES_BASE + p) for p in PAGES},
        "api": {a: _code(VERCEL_BASE + a) for a in API},
        "last_runs": read_runs(),
        "public_internal": read_public_internal(),
        "fingerprint_gap_hours": None,
    }
    level, why = verdict(readings)
    stamp = {"ts": time.time(), "level": level, "why": why, "readings": readings}
    print("[prod-verify] %s — %s" % (level.upper(), why))
    if "--print" in argv:
        return 0 if level != "blocking" else 1
    os.makedirs(os.path.dirname(STAMP), exist_ok=True)
    json.dump(stamp, io.open(STAMP, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("时间戳已写入 %s（有效期 %d 分钟）" % (STAMP, MAX_AGE_S // 60))
    return 0 if level != "blocking" else 1


if __name__ == "__main__":
    sys.exit(main())
