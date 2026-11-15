#!/usr/bin/env bash
# 在服务器上安装或重装 portfolio-monitor 的 systemd 用户服务（可重复运行）。
#
# 前提：代码在 ~/portfolio-monitor；local/config.toml 和 .env 已就位（不进 git）；
#       ~/.local/bin/uv 存在；用户已开启 linger（loginctl enable-linger），服务才会开机自启。
# 隧道服务只在 ~/.cloudflared/portfolio.yml 存在时启用。
set -euo pipefail
cd "$(dirname "$0")/.."
UV="${UV:-$HOME/.local/bin/uv}"
UNITS="$HOME/.config/systemd/user"

[ -f local/config.toml ] || { echo "缺少 local/config.toml（真实配置，不进 git）"; exit 1; }
[ -f .env ] || { echo "缺少 .env（凭据，不进 git）"; exit 1; }
chmod 600 .env local/config.toml
[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = "yes" ] \
  || echo "⚠ 用户没有开启 linger，服务不会开机自启：sudo loginctl enable-linger $USER"

"$UV" sync --frozen
"$UV" run --frozen --env-file .env portfolio-monitor migrate

mkdir -p "$UNITS"
install -m 644 deploy/systemd/*.service deploy/systemd/*.timer "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now portfolio-monitor-web.service \
  portfolio-monitor-sync.timer portfolio-monitor-ibkr.timer portfolio-monitor-backup.timer
systemctl --user restart portfolio-monitor-web.service
# Hermes 的日报技能（Hermes 装在同一台机器时）；定时任务只需建一次，见 README
if [ -d "$HOME/.hermes/skills" ]; then
  install -D -m 644 deploy/hermes/portfolio-daily-report/SKILL.md \
    "$HOME/.hermes/skills/portfolio-daily-report/SKILL.md"
fi
if [ -f "$HOME/.cloudflared/portfolio.yml" ]; then
  systemctl --user enable --now portfolio-monitor-tunnel.service
else
  echo "（还没有 ~/.cloudflared/portfolio.yml，跳过隧道服务）"
fi

systemctl --user --no-pager list-timers 'portfolio-monitor-*'
systemctl --user --no-pager --lines=0 status portfolio-monitor-web.service || true
