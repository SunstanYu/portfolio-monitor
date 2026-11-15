#!/usr/bin/env bash
# 更新到远端最新代码并重启网页（定时任务下次运行时自动用新代码）。
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
exec deploy/install.sh
