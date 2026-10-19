# -*- coding: utf-8 -*-
"""StarHub 唯一可用的推送通道：GitHub Data API + 推送前守门 + 推完自查。

为什么不用 `git push`：主仓 `.git` 已膨胀到 6.8GB 且含断链对象，`fetch/push` 直连一律失败（2026-09-19 实测）。
为什么要守门：`update.yml` 的 auto-commit 会把它检出时刻的 docs/源码按文件名写回，
  在**它那个 Commit 步走完之前**推送 = 几分钟后被回滚（2026-09-19 晚吞掉两次文档推送）。
  守门判的是那一步的状态，不是整场是否结束 —— 后者自 2026-10-02 起永远为真（Vercel 步连场挂 40+ 分钟），
  会把推送闸变成恒拒（实测 4 小时一次都没推出去）。见 will_write_main()。
为什么推完要自查：Data API 不回写本地工作树，本地与远端是两条并行历史，只有 blob 比对才知道有没有落住。

用法：
  py -3.11 tools/data_api_push.py --msg "提交信息" HANDOFF.md docs/xxx.md
  py -3.11 tools/data_api_push.py --check-only                 # 只跑守门
  py -3.11 tools/data_api_push.py --allow-running --msg ... p   # 明知有构建在跑仍要推（大概率被回滚）
"""
import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

# 只在作为脚本跑时改挂载 stdout：pytest 捕获态下重挂载会吞掉测试输出
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
API = "https://api.github.com"
REPO = "repos/Kwei168/starhub"

# 提交身份必须**显式**给，不能让 GitHub 按 token 自动填。
# 自动填出来的是 `83650072+Kwei168@users.noreply.github.com`（GitHub 的"邮箱保密"新格式），
# 而 Vercel 的 "Block deployments from unverified users" 拿部署所记 commit 的 author 邮箱去匹配
# GitHub 账号，认不出这个带用户 ID 前缀的形式 —— 报错原文：
#   The deployment was blocked because the commit email
#   83650072+Kwei168@users.noreply.github.com could not be matched to a GitHub account.
# 后果不是"日志难看"：现取 400 个提交里我经手的 137 个全是这个邮箱，
# Vercel 面板里唯二两条 Ready 的部署都恰好挂在 bot 的 `chore: auto update stars` 提交上，
# 其余一律 Blocked ⇒ 我改的 API 代码从没真正上过生产，而 CI 一路只表现为"Vercel 部署失败"。
# 判据：tests/tools/test_data_api_push_author_identity.py
COMMIT_IDENTITY = {"name": "Kwei168", "email": "Kwei168@users.noreply.github.com"}
WF = ".github/workflows/update.yml"
# 所有路径都相对仓库根解析（本文件在 <root>/tools/ 下），不依赖调用时的 CWD
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 就地变异 harness 的持锁标记：见 gate() 里的说明
MUTATION_LOCK = os.path.join(ROOT, "_scratch", ".mutation-lock")
# 生产验证时间戳：见 prod_gate()。默认落在 .deploy-tmp（gitignore 内），判据用 monkeypatch 指到临时目录
# 先把自己的目录放进 sys.path：原来那句注释说"脚本态与测试态都在 sys.path 上"是**错的**——
# 判据用 importlib.util.spec_from_file_location 加载本文件时，tools/ 并不在 sys.path 上，
# 于是 `import prod_verify` 直接 ModuleNotFoundError ⇒ tests/tools/test_push_ignores_scratch.py
# 四条判据从这行引入那天起就在红（本仓电池不进 CI，所以没人发现）。在源头补一行，
# 而不是给每个测试各写一遍 sys.path。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prod_verify as PV  # noqa: E402
PROD_STAMP = PV.STAMP
MAX_AGE_S = PV.MAX_AGE_S


def req(method, path, body=None, tries=6):
    """该端点频繁 TLS EOF；半程失败只会留下孤儿 blob 而不动 ref，所以每步都要重试。"""
    token = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
    last = None
    for k in range(tries):
        try:
            r = urllib.request.Request(API + "/" + path, method=method,
                                       headers={"Authorization": "Bearer " + token,
                                                "Accept": "application/vnd.github+json",
                                                "Content-Type": "application/json"})
            data = json.dumps(body).encode() if body is not None else None
            with urllib.request.urlopen(r, data, timeout=90) as f:
                return json.loads(f.read().decode())
        except Exception as exc:
            last = exc
            # 404 是**语义正确**的答案，不是故障：verify(gone=…) 就靠它判定"已删除"。
            # 留在重试循环里，一次 --delete 147 条要白烧 147×45s（实测单条 tries=2 用时 10.6s）。
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                raise
            print("  重试 %d/%d %s %s: %s" % (k + 1, tries, method, path.split("/")[-1], exc),
                  file=sys.stderr)
            time.sleep(3 + 3 * k)
    raise last


def runs(per_page=10):
    return req("GET", "%s/actions/runs?per_page=%d" % (REPO, per_page))["workflow_runs"]


# 全场唯一写 main 的 ref 的动作 = update.yml 的这一步。判「这场还会不会回滚我们」只看它，
# 不看 run 的 status：Vercel 步连场挂 40+ 分钟 ⇒ 任何时刻都有一只未完成的 run，
# 按 status 判会让闸恒拒（2026-10-02 实测：--wait-window 四轮 30 分钟全白等，4 小时推不出去）。
REF_WRITE_STEP = "Commit & push if changed"
_OPEN_STEP = ("queued", "pending", "in_progress")


def jobs_of(run_id):
    """取一场的 jobs（含步级状态）。取不到返回 None，由调用方按「判不了」处理。"""
    try:
        return req("GET", "%s/actions/runs/%s/jobs?per_page=100" % (REPO, run_id))["jobs"]
    except Exception as exc:
        print("  [?] 取 run %s 的 jobs 失败，按「仍可能写 ref」处理：%s" % (run_id, exc), file=sys.stderr)
        return None


def will_write_main(jobs):
    """这一场的步序列之后还会不会写 main 的 ref。判不了一律 True —— 宁可多等一轮，也不推出去被回滚。

    不接 run 对象是有意的：它那些形状（queued / in_progress / completed）在这里一律由**步序列**回答 ——
    queued 的那场根本没有步（走空 steps 分支），挂着的 Vercel 场其 Commit 步已完成（走 False 分支）。
    早先版本另有一条 `status in ("queued","requested")` 短路，被变异探针证明删掉后 12 条判据全绿
    ⇒ 它是死逻辑，只让人误以为"状态参与了判断"（2026-10-02）。

    为什么 Commit 步结束之后推是安全的（读 update.yml:346 的实现，不是猜）：它用的是**普通
    `git push`（无 --force）**，被拒后走 `fetch + reset --hard origin/main + 重建 + 重新 add`
    的重试路径 ⇒ 即便抢在它 fetch 之前推过去，它也会基于新 head 重跑，谁也不吞谁。
    危险只剩该步**内部**（fetch 与 push 之间）那几秒，而那正是 in_progress 这个形状。
    """
    if jobs is None:
        return True
    steps = [s for j in jobs for s in (j.get("steps") or [])]
    if not steps:
        return True  # job 尚未开始（queued 的那场就长这样），看不到步序列
    mine = [s for s in steps if (s.get("name") or "").strip() == REF_WRITE_STEP]
    if not mine:
        return True  # 别的 workflow，或有人改了步名 ⇒ 认不出就不放行
    return any(s.get("status") in _OPEN_STEP for s in mine)


def gate():
    """返回 (ok, 说明)。判据只有一条：当前没有任何未完成的 run（未跑完的那场随时会按文件名回滚 docs）。"""
    if os.path.exists(MUTATION_LOCK):
        # 就地变异 harness 正在往真实文件里写变异体。这个循环会等构建窗口等上半小时，
        # 而 09-22 两次就是它在持锁窗口里读了盘：main 上先后出现过
        # `over_time -> return False` 和 `_article_from 的 "source_key": ""`。
        # 判在 runs() 之前：守门本身要联网，判锁不该联网。
        try:
            with io.open(MUTATION_LOCK, encoding="utf-8") as f:
                who = f.read().strip()[:120]
        except Exception:
            who = "?"
        return False, "有就地变异 harness 在持锁（%s），此刻读盘会把变异体推上线" % who
    rs = runs()
    busy = [r for r in rs if r["status"] != "completed"]
    newest = max(rs, key=lambda r: r["created_at"])
    if busy:
        writers = [r for r in busy if will_write_main(jobs_of(r["id"]))]
        if writers:
            return False, "有 %d 场的「%s」步还没走完，现在推会被它的旧检出回滚：%s" % (
                len(writers), REF_WRITE_STEP,
                ["%s created=%s status=%s" % (r["id"], r["created_at"][11:19], r["status"])
                 for r in writers])
        return True, "守门通过：%d 场仍在飞，但它们的「%s」步都已结束 ⇒ 之后不再写 main（挂着的是 Vercel/Pages 步）；最近一场 %s created=%s" % (
            len(busy), REF_WRITE_STEP, newest["id"], newest["created_at"][11:19])
    return True, "守门通过：无在跑 run；最近一场 %s created=%s 已 completed" % (
        newest["id"], newest["created_at"][11:19])


def verify(expect, tag, ref="main", gone=()):
    """复查必须查**刚推的那个 ref**：不带 ref 的 contents 永远读默认分支，
    推分支时会拿 main 的旧 blob 来比，把一次成功的推送报成"内容不同"。

    `gone` 是本次声明要删的路径：**删除也必须复查**，否则 `sha: null` 没生效时
    工具会报"内容不同=0"，而远端那个文件其实还在（半拉子状态比不删更难发现）。
    路径一律 `quote()`：本轮删除清单里 89/147 含非 ASCII 与空格，裸拼会让复查在 ref 已移动之后
    抛异常 ——「推成功了但报栈」和「删漏了」就分不开了（判据 tests/tools/test_push_url_quoting.py）。
    """
    bad = 0
    for p, want in expect.items():
        got = req("GET", "%s/contents/%s?ref=%s" % (REPO, quote(p), ref))
        if got["sha"] != want:
            bad += 1
            print("  [NG] %s %s 远端=%s 期望=%s" % (tag, p, got["sha"][:10], want[:10]))
        else:
            print("  [OK] %s %s %s" % (tag, p, want[:10]))
    for p in gone:
        try:
            got = req("GET", "%s/contents/%s?ref=%s" % (REPO, quote(p), ref))
            bad += 1
            print("  [NG] %s 删除未生效：%s 远端仍存在 sha=%s" % (tag, p, got["sha"][:10]))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("  [OK] %s 删除 %s（远端已 404）" % (tag, p))
            else:
                bad += 1
                print("  [NG] %s 删除 %s 复查失败：HTTP %d（问不出来 ≠ 已删）" % (tag, p, e.code))
    return bad


def risky_runs(push_ts):
    """检出早于本次推送、且**还会**提交的 run —— 只有这种会用旧 index 覆盖我们刚推的内容。

    旧实现只看 `status != completed`：挂着 Vercel 步的那场永远不结束，于是推送后的等待
    会把 40×30s 全烧光再去复查（复查时它其实早已提交完，等的是无关的步骤）。
    """
    return [r for r in runs(10)
            if r["created_at"] < push_ts and will_write_main(jobs_of(r["id"]))]


def prod_gate():
    """推下一批之前必须先做生产验证：见不到新鲜的时间戳就拒绝。

    为什么是硬门槛而不是"我会记得跑"（2026-10-03 的定性批评）：Vercel 部署连续 6 场没落地
    （现取步级 conclusion：1584 success，1585–1589 cancelled，1590 skipped），
    我却照样一批批往下推 —— 把"CI 绿"当成了"改动生效"。线上读数一直拿得到，
    我没把它变成阻塞条件，所以事故是被你发现的、不是被我发现的。

    `warn` 不挡路：已知未修的问题（#27）若永久卡住推送，结果只会是有人加 --skip，
    门槛反而失效；但 warn 的原文会打进推送日志，生产状态在提交历史里留痕。
    """
    try:
        with io.open(PROD_STAMP, encoding="utf-8") as f:
            stamp = json.load(f)
    except Exception:
        return False, ("没做过生产验证 ⇒ 生产状态未知，拒绝推送。先跑 "
                       "`py -3.11 tools/prod_verify.py`（明知生产状态仍要推，用 --allow-running）")
    if not PV.stamp_is_fresh(stamp):
        return False, ("生产验证时间戳已过期（>%d 分钟）⇒ 重跑 tools/prod_verify.py" % (MAX_AGE_S // 60))
    if stamp.get("level") == "blocking":
        return False, "生产验证判为 blocking：%s" % stamp.get("why", "（没时间戳里的理由）")
    note = "；提醒：%s" % stamp.get("why") if stamp.get("level") == "warn" else ""
    return True, "生产验证通过（level=%s，%d 分钟内）%s" % (
        stamp.get("level"), MAX_AGE_S // 60, note)


def _abs(p):
    """一律相对仓库根解析 —— 原实现用 CWD 相对路径，从别的目录调用会误判"本地不存在"。"""
    return p if os.path.isabs(p) else os.path.join(ROOT, p.replace("/", os.sep))


def norm_path(p):
    return re.sub(r"^\./", "", p.replace("\\", "/"))


def is_binary_bytes(b):
    return b"\x00" in b[:8000]


def _rel(full):
    """ROOT 相对化；跨盘符（Windows 临时目录在 C:）时 relpath 会抛 ValueError，退回绝对路径。"""
    try:
        return os.path.relpath(full, ROOT).replace(os.sep, "/")
    except ValueError:
        return full.replace(os.sep, "/")


def _ignored(rels):
    """返回其中被 .gitignore 规则排除的路径。

    必须带 `--no-index`：`check-ignore` 默认先看索引，**已跟踪的文件一律答"不忽略"**，
    那正好漏掉我们要拦的那一类（"被忽略却已跟踪"）。
    """
    if not rels:
        return []
    p = subprocess.run(["git", "check-ignore", "--stdin", "--no-index", "-z"],
                       input="\0".join(rels).encode("utf-8"),
                       cwd=ROOT, capture_output=True)
    return [x.decode("utf-8", "replace") for x in (p.stdout or b"").split(b"\0") if x]


def refused_ignored(paths):
    """显式点名、但被 .gitignore 排除的路径 —— 单独成口，是为了让调用方能审计而不是静默吞掉。"""
    hit = set(_ignored([norm_path(p) for p in paths]))
    return [p for p in paths if norm_path(p) in hit]


def list_files(rel, allow_ignored=False):
    """列出一个路径（文件或目录）下的文件；排除 __pycache__ 与 .pyc，以及被 .gitignore 排除的路径。

    为什么这条必须在工具里：目录展开历史上只看 `__pycache__`/`.pyc`，
    于是"按目录推"会把 gitignored 的 Scratch 件永久写进 git 历史（实测远端树里那个
    411,404 B 的 `_check_js_temp.js` 就是这么进去的），而历史不可回收。
    """
    ap = _abs(rel)
    if os.path.isfile(ap):
        out = [rel]
    else:
        out = []
        for root, dirs, files in os.walk(ap):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in sorted(files):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                out.append(_rel(os.path.join(root, fn)))
    if allow_ignored:
        return out
    drop = set(_ignored(out))
    if drop:
        print("  [忽略] 目录展开跳过 %d 个被 .gitignore 排除的路径：%s"
              % (len(drop), ", ".join(sorted(drop)[:6])))
    return [p for p in out if p not in drop]


def expand_paths(paths, allow_ignored=False):
    """目录参数展开成其中的文件（原子推送要靠一次列出全部路径，逐个数文件正是漏项的来源）。"""
    out = []
    for p in paths:
        p = norm_path(p)
        if os.path.isdir(_abs(p)):
            out.extend(list_files(p, allow_ignored))
        else:
            out.append(p)
    return out


def _git_blob_sha(rel):
    """用 git hash-object 取本地 blob sha：会套用与提交时相同的转换（含 CRLF 归一），可直接与远端比。"""
    try:
        return subprocess.check_output(
            ["git", "hash-object", "--", rel], cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


def _head_blob_sha(rel):
    """HEAD 里该路径的 blob（拿不到=未跟踪，视为脏）。"""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD:%s" % rel],
                                       cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""


def local_is_clean(rel):
    """工作树该文件与 HEAD 一致 ⇒ 本地没动过它：与远端的差异属于"别人推得更新"，不是本次的事。"""
    h, w = _head_blob_sha(rel), _git_blob_sha(rel)
    return bool(h) and h == w


def remote_matches_local(rel):
    """远端该路径是否存在（目录要看能不能列出条目）。

    只判"存在与否"才是门禁的硬条件：`pytest tests/x/` 目录不存在才退出码 4；
    目录在、但缺几个**只在本地**的文件（另一会话的在途用例），门禁照样能跑完 ——
    那种情况降级为提示，不该把别人的未推文件裹进这次推送，也不该因此挡住这次发布。
    返回 (ok, 提示)。
    """
    files = list_files(rel)
    if not files:
        return False, "%s —— 本地拿不到可枚举的文件" % rel
    missing_local_only, notes = [], []
    for f in files:
        try:
            # quote 不能省：非 ASCII/含空格的路径裸拼会抛，而这里的 except 会把它
            # 误分类成「远端不存在」⇒ 判据读数为假阴性。
            got = req("GET", "%s/contents/%s" % (REPO, quote(f)), tries=2)
        except Exception:
            missing_local_only.append(f)
            continue
        want = _git_blob_sha(f)
        if want and got.get("sha") != want:
            if local_is_clean(f):
                # 本仓常态：Data API 不回写 ⇒ 本地干净却落后远端。这时远端那份是更新的内容，
                # base_tree 会原样保留它，把它判成阻塞就等于每次发布都被别人的进度卡住。
                notes.append("%s —— 本地干净但落后远端（远端是更新的一版，本次不推它）" % f)
                continue
            return False, "%s —— 本地有未提交改动且与远端不一致（这次会把旧内容推回去）：%s" % (rel, f)
    if len(missing_local_only) == len(files):
        return False, "%s —— 远端不存在（门禁会退出码 4，连 Commit 带 Vercel 部署一起停摆）" % rel
    if notes:
        return True, "；".join(notes)
    if missing_local_only:
        return True, "%s —— 远端缺 %d 个仅本地文件 %s：门禁仍能跑，但那些用例不会在 CI 里执行" % (
            rel, len(missing_local_only), ", ".join(os.path.basename(x) for x in missing_local_only[:3]))
    return True, ""


# 这类写法不是字面路径，交给解析器判红会误报；单列出来要人看一眼
UNPARSED_HINTS = ("*", "$", "{", "<", ">")


def ci_atomic_deps(push_paths, wf):
    """推 workflow 时的原子性检查：它引用的每条测试路径必须"同批推送"或"远端已有且与本地一致"。

    实测依据（09-20 07:1x 核实）：门禁 A2 `python -m pytest tests/rss_history/ tests/rss_source_coverage/ -q -s`
    没有 `continue-on-error`（是 blocking），而 `Deploy to Vercel` 也没有 `if:` → 默认 success()。
    pytest 找不到目录会退出码 4，于是**提交步骤与部署一起被跳过**，站点整小时冻住。
    """
    try:
        with open(_abs(wf), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return [wf + " 本地读不到"], []
    refs, odd, advisory = set(), set(), set()
    # 按步骤分块解析：advisory（带 continue-on-error，如门禁 B）引用的路径缺了不会红掉作业，
    # 拿它去阻塞发布是误伤（审查 P1-4）。
    blocks = re.split("\n      - name:", text)
    for block in blocks:
        is_adv = "continue-on-error: true" in block
        for line in block.splitlines():
            for m in re.finditer(r"(?:python3?|bash|sh)\s+((?:[\w./-]+/)?[\w.-]+\.(?:py|sh))", line):
                tok = norm_path(m.group(1))
                if any(h in tok for h in UNPARSED_HINTS):
                    odd.add("%s —— 通配/matrix 模板写法，无法机器判定，请人工确认已同批推送" % tok)
                elif is_adv:
                    advisory.add(tok)
                else:
                    refs.add(tok)
            if "pytest" not in line and not re.search(r"python[^\n]*test_[\w/]+\.py", line):
                continue
            line = re.sub(r"--[\w-]+=\S+", " ", line)
            for tok in re.findall(r"[\w./$*{}<>-]*(?:tests/[\w./$*{}<>-]+|test_[\w.$*{}<>-]+\.py)", line):
                tok = tok.rstrip("/")
                if tok.startswith("-"):
                    continue
                if any(h in tok for h in UNPARSED_HINTS):
                    odd.add("%s —— 通配/matrix 模板写法，无法机器判定，请人工确认已同批推送" % tok)
                elif is_adv:
                    advisory.add(norm_path(tok))
                else:
                    refs.add(norm_path(tok))
    refs -= advisory          # 只要有一个 blocking 步引用它，才需要强制
    problems = []
    for p in sorted(refs):
        if not os.path.exists(_abs(p)):
            problems.append("%s —— 本地不存在，推上去该门禁必红" % p)
            continue
        if p in push_paths or any(x.startswith(p + "/") for x in push_paths):
            continue
        ok, note = remote_matches_local(p)
        if not ok:
            problems.append(note)
        elif note:
            odd.add(note)
    for p in sorted(advisory - refs):
        if not os.path.exists(_abs(p)):
            odd.add("%s —— 仅 advisory 步引用且本地不存在（不阻塞发布，但该步会红）" % p)
    return problems, sorted(odd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--msg", default="")
    ap.add_argument("--msg-file", default="", help="从 UTF-8 文件读提交信息（CJK 走 argv 容易被 shell 吃掉）")
    ap.add_argument("--wait-window", action="store_true", help="守门不过就每 60s 重试，最多 30 分钟")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只做守门+原子性检查并列出将要推的路径，不写远端")
    ap.add_argument("--allow-running", action="store_true",
                    help="同时绕过『构建窗口』与『生产验证』两道闸（明知仍要推才用）")
    ap.add_argument("--delete", action="append", default=[],
                    help="从远端删除这些路径（tree 项 sha:null，走 Git Data API 的唯一删除方式）")
    ap.add_argument("--ref", default="",
                    help="目标分支，默认 main。夜场验证走分支：不占白天构建窗口、不碰线上站点")
    ap.add_argument("--allow-ignored", action="store_true",
                    help="放行被 .gitignore 排除的路径（默认拒绝：这类文件一旦进树就是永久历史，"
                         "远端那个 411KB 的 _check_js_temp.js 就是这么进去的）")
    a = ap.parse_args()
    if a.msg_file:
        a.msg = open(a.msg_file, encoding="utf-8").read().strip()

    # 点名路径先判 ignore（目录展开那条路是"跳过 + 播报"，点名的路必须让人明确决定）；
    # 只查点名文件不查 --delete：删除被忽略却仍在树里的路径，正是清理要做的事。
    named = [norm_path(p) for p in (a.paths or [])]
    blocked = [] if a.allow_ignored else refused_ignored(
        [p for p in named if not os.path.isdir(_abs(p))])
    if blocked:
        print("::error::拒绝推送被 .gitignore 排除的路径：%s（确实要推就加 --allow-ignored）"
              % ", ".join(blocked))
        return 1
    a.paths = expand_paths(named, a.allow_ignored)
    # 任一 workflow 被推都要过原子性检查（原实现只认 update.yml 这一个字符串）
    wfs = [p for p in a.paths if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))]
    for wf in wfs:
        problems, odd = ci_atomic_deps(a.paths, wf)
        for o in odd:
            print("  [?] %s：%s" % (wf, o))
        if problems:
            print("原子性检查未通过（%s）：" % wf)
            for b in problems:
                print("  [NG] " + b)
            return 1
        print("原子性检查通过：%s 引用的测试路径都已随本次推送，或远端已有且与本地逐文件一致" % wf)

    ref_name = (a.ref or "main").strip("/")
    on_main = ref_name == "main"
    # 构建窗口那道闸只为 main 存在：update.yml 的 Commit 步骤会 `reset --hard origin/main`
    # 再按清单 add，中途推 main 会被它回滚。分支不在它的管辖里 —— 夜场验证因此不必再等缝。
    if on_main:
        ok, why = gate()
        pok, pwhy = prod_gate()
        why = why + "\n" + pwhy
        ok = ok and pok
    else:
        ok, why = True, ("推送到分支 %s：不守构建窗口（白天场只回滚 main），"
                         "夜场验证与站点部署因此完全解耦" % ref_name)
    print(why)
    if a.wait_window:
        for _ in range(30):
            if ok:
                break
            time.sleep(60)
            ok, why = gate()
            print(why)
    if a.check_only:
        return 0 if ok else 1
    if a.dry_run:
        print("dry-run：将推 %d 个路径：%s" % (len(a.paths), ", ".join(a.paths)))
        if a.delete:
            print("dry-run：将删 %d 个路径：%s" % (len(a.delete), ", ".join(a.delete)))
        return 0
    if not ok and not a.allow_running:
        print("拒绝推送：上面两道闸（构建窗口 / 生产验证）至少一道没过。"
              "加 --allow-running 可强行推 —— 但它同时绕过两道，用之前请把上面两行读完")
        return 1
    both = sorted(set(a.paths) & set(a.delete))
    if both:
        print("同一个路径既要推又要删（未定义行为），拒绝：%s" % ", ".join(both))
        return 1
    if not (a.paths or a.delete) or not a.msg:
        print("缺参数：需要 --msg 与至少一个文件路径")
        return 1

    push_t = time.time()

    def head_of(ref):
        """分支可能还不存在：那就以 main 为父建 ref，第一次推等于开分支。"""
        try:
            return req("GET", "%s/git/ref/heads/%s" % (REPO, ref))["object"]["sha"], False
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            return req("GET", REPO + "/git/ref/heads/main")["object"]["sha"], True

    head, create_ref = head_of(ref_name)
    items, expect, gone = [], {}, []
    for p in a.paths:
        with open(_abs(p), "rb") as fh:
            raw = fh.read()
        # 无条件 CRLF→LF 会毁掉被跟踪的二进制（仓库里有 4 个 .png）；只对文本做归一
        if not is_binary_bytes(raw):
            raw = raw.replace(b"\r\n", b"\n")
        blob = req("POST", REPO + "/git/blobs",
                   {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        expect[p] = blob["sha"]
        print("blob  %-56s %7d -> %s" % (p, len(raw), blob["sha"][:10]))
        items.append({"path": p, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    # 删除：Git Data API 的表示法是同一个 tree 项里 `sha: null`。
    # 为什么要工具支持删除而不是"本地删了就算了"：本地 git rm 后远端文件仍在，
    # 只推配置文件就会留下"声明已删、文件还在线"的半拉子状态（比不删更难查）。
    for p in a.delete:
        gone.append(p)
        print("del   %-56s （远端将不存在）" % p)
        items.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
    tree_sha = None
    for attempt in range(1, 5):
        # 422 的意思是"父提交已落后"：白天那场随时可能把 main 往前推。
        # req() 里那 6 次重试是同一份请求重发，对 422 完全无效 —— 必须重读 head、
        # 用新 base_tree 重建 tree+commit 再 PATCH，否则一定 6 次同样失败（今天实撞过）。
        if attempt > 1:
            head, create_ref = head_of(ref_name)
        try:
            tree = req("POST", REPO + "/git/trees", {"base_tree": head, "tree": items})
            commit = req("POST", REPO + "/git/commits",
                         {"message": a.msg, "tree": tree["sha"], "parents": [head],
                          "author": COMMIT_IDENTITY, "committer": COMMIT_IDENTITY})
            if create_ref:
                ref = req("POST", REPO + "/git/refs",
                          {"ref": "refs/heads/" + ref_name, "sha": commit["sha"]})
            else:
                ref = req("PATCH", "%s/git/refs/heads/%s" % (REPO, ref_name),
                          {"sha": commit["sha"], "force": False})
            break
        except urllib.error.HTTPError as e:
            if e.code != 422 or attempt == 4:
                raise
            print("  ref 被推进过（422），重读 head 重建提交：%d/4" % attempt)
            time.sleep(2 * attempt)
    print("parent %s -> commit %s -> ref %s" % (head[:10], commit["sha"][:10], ref["object"]["sha"][:10]))

    bad = verify(expect, "即时复查", ref_name, gone)
    if on_main:
        # Actions 的 run 列表有 10~20s 延迟：守门通过≠真的没有在跑的场，推后必须再看一次
        # 减 120s 是为了抵消本机与 GitHub 时钟的偏差（判据方向是"更保守"，不会漏判）
        push_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(push_t - 120))
        print("等待 90s 让 run 列表落定，再确认没有『检出早于本次推送』的构建…")
        time.sleep(90)
        risky = risky_runs(push_ts)
        if risky:
            print("  [风险] 有 %d 场在本推送之前检出、尚未提交：%s" % (
                len(risky), ["%s created=%s %s" % (r["id"], r["created_at"][11:19], r["status"]) for r in risky]))
            for _ in range(40):
                if not risky_runs(push_ts):
                    break
                time.sleep(30)
            print("那批构建已结束，复查是否被回滚：")
            bad = verify(expect, "构建后复查", ref_name, gone)
        else:
            print("  [OK] 无早于本次推送且未提交的 run")
    else:
        print("分支推送：跳过『早于本次推送的构建』复查（那条查的是 main 的回滚风险）")
    print("最终：内容不同=%d" % bad)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
