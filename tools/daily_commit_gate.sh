#!/usr/bin/env bash
# 构建日志"每天只提交一次"的闸门。
#
# 用法: daily_commit_gate.sh <marker_file> <today:YYYY-MM-DD>   →  stdout 打印 open 或 closed
#
# 为什么读一个自己入库的标记文件，而不是 `git log -1 --format=%cd -- build_logs/…`：
# 主构建的 checkout 现在是 `fetch-depth: 0`（update.yml:45），但那是偶然条件 ——
# 谁以后改成 1，`git log` 查不到历史就会被读成"到期了"，于是**静默退回每场提交**，
# 而这正是本次要消掉的 2.0 MiB/场。标记文件随摘要一起入库，与历史深度无关。
#
# 任何异常一律放行（open）并 exit 0：这个闸门站在 Commit 步里，它红一次就是把整场构建
# 与两个部署一起冻住（本仓 2026-10-01 刚为一次 A2 红付过整场冻结的学费）。
# 宁可某天多提交一次日志，也不能因为它自己出问题而停部署。
set -u

marker="${1:-}"
today="${2:-}"

if [ -z "$marker" ] || [ -z "$today" ]; then
  echo open
  exit 0
fi

if [ -f "$marker" ]; then
  last=$(head -c 40 "$marker" 2>/dev/null | tr -d ' \t\r\n' 2>/dev/null || true)
  if [ "$last" = "$today" ]; then
    echo closed
    exit 0
  fi
fi

echo open
exit 0
