# -*- coding: utf-8 -*-
"""本地快车道：新星分类/点评/翻译 → 提交状态 → 推送 → 触发 CI 发布。

背景：原 star-fast.yml（15 分钟场）已删除，而 CI 的数据中心 IP 被所有免费
翻译端点封锁（Agnes 429 / MyMemory 配额耗尽 / gtx 封锁）——新星翻译只有
本机（住宅 IP + Agnes）稳定可用。本脚本由 Windows 计划任务周期调用：

  pythonw tools/local_fastlane.py

pythonw 无控制台，stdout/stderr 全部重定向到 local_fastlane.log（gitignore）。
只做状态写回与发布触发，不产出 HTML（站点唯一通路仍是 update.yml）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(ROOT, "local_fastlane.log")
ENV_PATH = os.path.join(ROOT, ".env")

# 状态文件：快车道的全部产物，也是 push 冲突时要按键合并的对象
STATE_FILES = ("known_categories.json", "known_notes.json", "descriptions_zh.json")
REPO = "ghoustghoust/starhub"

# 出网代理候选：本机直连 GitHub 时通时断，依次回退（空 = 直连）
PROXIES = ["", "http://127.0.0.1:12001", "http://127.0.0.1:12000"]

# pythonw 无控制台，子进程默认会各开一个可见终端窗口，必须显式隐藏
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write("[%s] %s\n" % (stamp, msg))


def load_env() -> None:
    """计划任务环境里没有 .env 变量，手工并入（不覆盖已有）。"""
    try:
        with open(ENV_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        log("[env] .env 不存在，跳过注入")


def _token() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def git(args: list[str], proxy: str = "") -> subprocess.CompletedProcess:
    # 统一用 .env token 静默认证：清空默认 helper（绕开 manager/GCM 登录弹窗），
    # 禁终端与 GUI 交互；core.editor=true 让 rebase --continue 不弹编辑器。
    helper = "!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"
    cmd = ["git", "-c", "credential.helper=", "-c", "credential.helper=" + helper,
           "-c", "credential.interactive=false", "-c", "core.editor=true"]
    if proxy:
        cmd += ["-c", "http.proxy=" + proxy]
    cmd += args
    env = dict(os.environ, GH_TOKEN=_token(), GIT_TERMINAL_PROMPT="0",
               GCM_INTERACTIVE="never", GIT_EDITOR="true")
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env=env, creationflags=NO_WINDOW)


def fetch_first_ok() -> bool:
    """拉取远端 refs：直连优先，逐个代理回退；全挂只告警（推/拉会在各自通道里再试）。"""
    for proxy in PROXIES:
        proc = git(["fetch", "origin"], proxy)
        if proc.returncode == 0:
            return True
        log("[fetch] 失败（%s）：%s" % (proxy or "direct", (proc.stderr or "").strip()[:160]))
    return False


def push_with_retry() -> bool:
    """推送（本场新提交 + 历史积压都在 HEAD 上）。pull --rebase 撞 CI 数据提交
    时在 rebase 内就地按键合并，使 HEAD 落到 origin/main 之上，push 才能快进。"""
    for attempt in range(3):
        for proxy in PROXIES:
            pull = git(["pull", "--rebase", "origin", "main"], proxy)
            if pull.returncode != 0:
                if _resolve_state_conflict(pull.stdout + pull.stderr):
                    pass  # rebase 已完成，HEAD 在 origin/main 之上，继续推
                else:
                    log("[push] pull --rebase 失败（%s）：%s"
                        % (proxy or "direct", (pull.stderr or pull.stdout).strip()[:160]))
                    continue  # 换下一个通道重试
            if _push_once(proxy):
                return True
        time.sleep(5)
    return False


def _push_once(proxy: str) -> bool:
    proc = git(["push", "origin", "main"], proxy)
    if proc.returncode == 0:
        return True
    log("[push] 失败（proxy=%s）：%s" % (proxy or "direct", (proc.stderr or "").strip()[:200]))
    return False


def _rebase_in_progress() -> bool:
    return os.path.isdir(os.path.join(ROOT, ".git", "rebase-merge")) or \
        os.path.isdir(os.path.join(ROOT, ".git", "rebase-apply"))


def _resolve_state_conflict(output: str) -> bool:
    """rebase 撞 CI 数据提交时，逐个提交就地按「远端为底 + 本地缺键补齐」解冲突，
    让 rebase 真正走完（HEAD 落到 origin/main 之上）。绝不 abort 后在分叉历史上另打
    提交——那会让 push 永远 non-fast-forward。非状态文件冲突则放弃本场。"""
    if "CONFLICT" not in output:
        return False
    log("[push] pull 撞冲突，rebase 内按键合并状态文件")
    try:
        for _ in range(300):
            if not _rebase_in_progress():
                return True
            unmerged = git(["diff", "--name-only", "--diff-filter=U"]).stdout.split()
            for name in unmerged:
                if name not in STATE_FILES:
                    log("[push] 非状态文件冲突（%s），放弃本场" % name)
                    git(["rebase", "--abort"])
                    return False
                ours = git(["show", ":2:" + name])    # rebase 中 :2 = 远端底
                theirs = git(["show", ":3:" + name])  # :3 = 正在重放的本地提交
                merged = json.loads(ours.stdout) if ours.returncode == 0 and ours.stdout.strip() else {}
                add = json.loads(theirs.stdout) if theirs.returncode == 0 and theirs.stdout.strip() else {}
                for key, value in add.items():
                    if value and not merged.get(key):
                        merged[key] = value
                with open(os.path.join(ROOT, name), "w", encoding="utf-8") as fh:
                    json.dump(merged, fh, ensure_ascii=False, indent=1)
                git(["add", name])
            cont = git(["rebase", "--continue"])
            err = cont.stdout + cont.stderr
            if cont.returncode == 0 and not _rebase_in_progress():
                return True
            if "No changes" in err or "now empty" in err or "Nothing to do" in err:
                git(["rebase", "--skip"])  # 该提交合并后变空，跳过
                continue
            if "CONFLICT" in err or _rebase_in_progress():
                continue  # 下一个提交又撞冲突，继续循环
            log("[push] rebase --continue 异常：%s" % err.strip()[:160])
            git(["rebase", "--abort"])
            return False
        log("[push] 冲突合并超过 300 轮，放弃本场")
        git(["rebase", "--abort"])
        return False
    except Exception as exc:  # noqa: BLE001
        log("[push] 冲突合并失败：%s" % exc)
        git(["rebase", "--abort"])
        return False


def dispatch_site_build() -> bool:
    """触发 update.yml：站点唯一发布通路。"""
    sys.path.insert(0, os.path.join(ROOT, "pipeline"))
    from starhub_pipeline.sources import netguard

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    opener = netguard.build_safe_opener({"api.github.com"})
    request = urllib.request.Request(
        "https://api.github.com/repos/%s/actions/workflows/update.yml/dispatches" % REPO,
        data=json.dumps({"ref": "main"}).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(request, timeout=30) as response:
            ok = response.status == 204
    except Exception as exc:  # noqa: BLE001
        log("[dispatch] 失败：%s" % exc)
        return False
    log("[dispatch] update.yml 触发 %s" % "成功" if ok else "异常")
    return ok


def main() -> int:
    os.chdir(ROOT)
    load_env()
    fetch_first_ok()  # 刷新 origin/main，让下面的积压判断有据可依

    sys.path.insert(0, ROOT)
    import fast_refresh  # noqa: E402  import 时即接管出网护栏与 .env 后的 key 池

    rc = fast_refresh.main()
    if rc != 0:
        log("[fast] 退出码 %d，本场结束" % rc)
        return rc

    changed = git(["status", "--porcelain", "--"] + list(STATE_FILES))
    ahead = git(["rev-list", "--count", "origin/main..HEAD"]).stdout.strip()
    has_backlog = ahead not in ("", "0")  # 上一场推送失败的积压提交，本场必须补推

    if changed.stdout.strip():
        commit = git(["add", *STATE_FILES])
        commit = git(["commit", "-m", "chore(fastlane): 新星状态写回（分类/点评/翻译）"])
        if commit.returncode != 0:
            log("[fastlane] commit 失败：%s" % (commit.stderr or "").strip()[:200])
            return 1
    elif not has_backlog:
        log("[fastlane] 状态无变化且无积压，无需推送")
        return 0

    if not push_with_retry():
        log("[fastlane] 推送未成功，状态保留在本地，下一场重试")
        return 1
    log("[fastlane] 状态已推送（%s）" % ("含本场新写回" if changed.stdout.strip() else "积压补推"))
    dispatch_site_build()
    return 0


if __name__ == "__main__":
    # pythonw 下 sys.stdout/stderr 为 None，fast_refresh 的大量 print 会炸；全部进日志
    _log_fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log_fh
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(main())
