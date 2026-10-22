#!/usr/bin/env bash
# Start / stop the background network watch: a LaunchAgent that runs
# `netinv.py watch --email`, so new devices, changed ones and ones back after
# 30+ days away are emailed (other leaves and comebacks are only logged).
# It starts at login and launchd restarts it if it dies.
#
# Usage:  ./scan-jobs.sh start [watch args]   (default: --interval 120 --no-router --email)
#         ./scan-jobs.sh stop                 (also stops a watch started by hand)
#         ./scan-jobs.sh restart [watch args]
#         ./scan-jobs.sh status
#         ./scan-jobs.sh logs                 (follow the log; Ctrl-C to quit)
set -euo pipefail

ENGINE="$(cd "$(dirname "$0")/engine" && pwd)"
LABEL="com.netinventory.watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$ENGINE/netinv-watch.log"
DOMAIN="gui/$(id -u)"
DEFAULT_ARGS=(--interval 120 --no-router --email)

python_bin() {
  for p in "$ENGINE/.venv/bin/python" "$ENGINE/../.venv/bin/python"; do
    [ -x "$p" ] && { echo "$p"; return; }
  done
  command -v python3
}

# PIDs of `netinv.py watch` running out of this engine dir (LaunchAgent or by hand)
watch_pids() {
  local pid
  # anchored on the interpreter, so a shell that merely launched it is left alone
  for pid in $(pgrep -f "^[^ ]*[Pp]ython[^ ]* netinv.py watch" || true); do
    if lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep -qx "n$ENGINE"; then
      echo "$pid"
    fi
  done
}

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

start() {
  local args=("$@")
  [ ${#args[@]} -eq 0 ] && args=("${DEFAULT_ARGS[@]}")
  stop quiet
  local py; py="$(python_bin)"
  local argxml="" a
  for a in "$py" netinv.py watch "${args[@]}"; do
    argxml+="    <string>$a</string>"$'\n'
  done
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$argxml  </array>
  <key>WorkingDirectory</key><string>$ENGINE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "Started $LABEL: netinv.py watch ${args[*]}"
  echo "Log: $LOG"
}

stop() {
  local was=""
  if loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    was=1
  fi
  local pids; pids="$(watch_pids)"
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    was=1
  fi
  [ "${1:-}" = quiet ] && return 0
  if [ -n "$was" ]; then echo "Stopped the network watch."; else echo "No network watch was running."; fi
}

status() {
  if loaded; then
    echo "LaunchAgent $LABEL: loaded"
  else
    echo "LaunchAgent $LABEL: not loaded"
  fi
  local pids; pids="$(watch_pids)"
  if [ -n "$pids" ]; then
    local pid
    for pid in $pids; do ps -o pid=,etime=,command= -p "$pid"; done
  else
    echo "No netinv.py watch is running from $ENGINE"
  fi
  if [ -f "$LOG" ]; then
    echo "--- last lines of $LOG"
    tail -n 5 "$LOG"
  fi
}

case "${1:-}" in
  start)   shift; start "$@" ;;
  stop)    stop ;;
  restart) shift; start "$@" ;;
  status)  status ;;
  logs)    touch "$LOG"; tail -f "$LOG" ;;
  *)       sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
