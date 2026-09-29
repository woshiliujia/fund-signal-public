#!/bin/bash
# 安装/卸载 管理后台服务(开机自启+常驻)与每日定时
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.fund-signal.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/fund-signal.log"
PY="$(command -v python3 || echo /usr/bin/python3)"
PORT="${PORT:-8787}"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载后台服务"
  exit 0
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$BASE_DIR/server.py</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$BASE_DIR</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "已安装后台服务(开机自启，进程崩溃自动重启)"
echo "  管理后台: http://127.0.0.1:$PORT"
echo "  日志: $LOG"
echo "  卸载: bash $BASE_DIR/install.sh --uninstall"
echo "  (内置调度: 每个交易日 14:50 自动计算并推送买卖信号)"
