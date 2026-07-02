#!/usr/bin/env bash
# Install a macOS LaunchAgent that runs ./update.sh on a schedule.
# Usage:  ./contrib/install-launchagent.sh [interval_seconds]   (default 1800 = 30 min)
#         ./contrib/install-launchagent.sh uninstall
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.netinventory.update"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled $LABEL"
  exit 0
fi

INTERVAL="${1:-1800}"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-l</string>
    <string>$DIR/update.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DIR/netinv-cron.log</string>
  <key>StandardErrorPath</key><string>$DIR/netinv-cron.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Loaded $LABEL — runs ./update.sh every ${INTERVAL}s."
echo "Logs: $DIR/netinv-cron.log   ·   Remove with: $0 uninstall"
