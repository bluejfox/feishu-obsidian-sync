#!/bin/bash
# 飞书同步定时器开关。用法:
#   bash scripts/timer.sh enable    # 开启每日定时
#   bash scripts/timer.sh disable   # 关闭定时(文件保留)
#   bash scripts/timer.sh status    # 查看是否在运行
#   bash scripts/timer.sh run-now   # 立刻手动触发一次
#   bash scripts/timer.sh logs      # 看最近日志
set -euo pipefail

LABEL="com.ray.feishu-sync"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PLIST_SRC="$HERE/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  enable)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true   # 先卸旧的(若有)
    launchctl bootstrap "$DOMAIN" "$PLIST_DST"
    echo "✅ 已开启:每 1 小时自动 sync 一次。状态见 'timer.sh status'。"
    ;;
  disable)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "🛑 已关闭定时(plist 文件保留,随时可再 enable)。"
    ;;
  status)
    if launchctl list | grep -q "$LABEL"; then
      echo "🟢 运行中(已加载):"
      launchctl list | grep "$LABEL"
    else
      echo "⚪️ 未加载(已关闭)。"
    fi
    ;;
  run-now)
    launchctl kickstart -p "$DOMAIN/$LABEL" 2>/dev/null \
      || { echo "未加载,直接跑一次:"; "$ROOT/.venv/bin/python" -m feishu_sync.cli sync; exit 0; }
    echo "已触发一次,跟踪日志: tail -f $ROOT/logs/sync.out.log"
    ;;
  logs)
    echo "=== feishu-sync.log(末40行,有界滚动)==="; tail -n 40 "$ROOT/logs/feishu-sync.log" 2>/dev/null || echo "(空)"
    echo "=== launchd.err.log(崩溃兜底,末20行)==="; tail -n 20 "$ROOT/logs/launchd.err.log" 2>/dev/null || echo "(空)"
    ;;
  *)
    echo "用法: bash scripts/timer.sh {enable|disable|status|run-now|logs}"
    exit 1
    ;;
esac
