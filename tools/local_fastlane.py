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


def git(args: list[str], proxy: str = "") -> subprocess.CompletedProcess:
    cmd = ["git"]
    if proxy:
        cmd += ["-c", "http.proxy=" + proxy]
    cmd += args
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
                          creationflags=NO_WINDOW)


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
    按键合并；网络/代理抖动换通道重试，pull 不可解也不再提前放弃。"""
    for attempt in range(3):
        for proxy in PROXIES:
            pull = git(["pull", "--rebase", "origin", "main"], proxy)
            if pull.returncode != 0:
                if _resolve_state_conflict(pull.stdout + pull.stderr):
                    pass  # 合并成新提交后继续推
                else:
                    log("[push] pull --rebase 失败（%s）：%s"
                        % (proxy or "direct", (pull.stderr or pull.stdout).strip()[:160]))
                    continue  # 换下一个通道重试
            if _push_once(proxy):
                return True
        time.sleep(5)
    return False


def _push_once(proxy: str) -> bool:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    helper = "!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"
    env = dict(os.environ, GH_TOKEN=token, GIT_TERMINAL_PROMPT="0")
    cmd = ["git", "-c", "credential.helper=", "-c", "credential.helper=" + helper]
    if proxy:
        cmd += ["-c", "http.proxy=" + proxy]
    cmd += ["push", "origin", "main"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                          timeout=120, env=env, creationflags=NO_WINDOW)
    if proc.returncode == 0:
        return True
    log("[push] 失败（proxy=%s）：%s" % (proxy or "direct", (proc.stderr or "").strip()[:200]))
    return False


def _resolve_state_conflict(output: str) -> bool:
    """pull --rebase 撞上 CI 数据提交时，按「远端为底 + 本地缺键补齐」解冲突。"""
    if "CONFLICT" not in output:
        return False
    log("[push] pull 撞冲突，按键合并本地状态")
    try:
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True, timeout=60,
                       creationflags=NO_WINDOW)
        base = {}
        for name in STATE_FILES:
            proc = subprocess.run(["git", "show", "origin/main:" + name], cwd=ROOT,
                                  capture_output=True, text=True, encoding="utf-8", timeout=60,
                                  creationflags=NO_WINDOW)
            base[name] = json.loads(proc.stdout) if proc.returncode == 0 else {}
        for name in STATE_FILES:
            path = os.path.join(ROOT, name)
            if not os.path.exists(path):
                continue
            mine = json.load(open(path, encoding="utf-8"))
            for key, value in mine.items():
                if value and not base[name].get(key):
                    base[name][key] = value
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(base[name], fh, ensure_ascii=False, indent=1)
        subprocess.run(["git", "add", *STATE_FILES], cwd=ROOT, capture_output=True, timeout=60,
                       creationflags=NO_WINDOW)
        subprocess.run(["git", "commit", "-m", "chore(fastlane): 按键合并本地状态写回"],
                       cwd=ROOT, capture_output=True, timeout=60, creationflags=NO_WINDOW)
        return True
    except Exception as exc:  # noqa: BLE001
        log("[push] 冲突合并失败：%s" % exc)
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, capture_output=True, timeout=60,
                       creationflags=NO_WINDOW)
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
