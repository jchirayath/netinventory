#!/usr/bin/env bash
# Start / stop the background network watch: a LaunchAgent that runs
# `netinv.py watch --email`, so new devices, changed ones and ones back after
# 30+ days away are emailed (other leaves and comebacks are only logged), as
# are the device monitors in engine/monitors.csv (offline too long, battery low).
# It starts at login and launchd restarts it if it dies.
#
# Usage:  ./scan-jobs.sh start [watch args]   (default: --interval 120 --no-router --email)
#         ./scan-jobs.sh stop                 (also stops a watch started by hand)
#         ./scan-jobs.sh restart [watch args]
#         ./scan-jobs.sh status
#         ./scan-jobs.sh logs                 (follow the log; Ctrl-C to quit)
#         ./scan-jobs.sh serve-start [args]   the dashboard as a LaunchAgent too
#                                             (default: --lan --allow-write once a
#                                             passcode is set, else --allow-write)
#         ./scan-jobs.sh serve-stop | serve-status
set -euo pipefail

ENGINE="$(cd "$(dirname "$0")/engine" && pwd)"
LABEL="com.netinventory.watch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$ENGINE/netinv-watch.log"
SERVE_LABEL="com.netinventory.serve"
SERVE_PLIST="$HOME/Library/LaunchAgents/$SERVE_LABEL.plist"
SERVE_LOG="$ENGINE/netinv-serve.log"
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
  write_agent "$LABEL" "$PLIST" "$LOG" "$py" netinv.py watch "${args[@]}"
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "Started $LABEL: netinv.py watch ${args[*]}"
  echo "Log: $LOG"
}

# write_agent LABEL PLIST LOG PROGRAM ARGS...: a LaunchAgent that starts at
# login and is restarted if it dies.
write_agent() {
  local label="$1" plist="$2" log="$3"; shift 3
  local argxml="" a
  for a in "$@"; do
    argxml+="    <string>$a</string>"$'\n'
  done
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
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
  <key>StandardOutPath</key><string>$log</string>
  <key>StandardErrorPath</key><string>$log</string>
</dict>
</plist>
EOF
}

serve_loaded() { launchctl print "$DOMAIN/$SERVE_LABEL" >/dev/null 2>&1; }

serve_start() {
  local args=("$@") py; py="$(python_bin)"
  if [ ${#args[@]} -eq 0 ]; then
    # LAN writes need a passcode (serve refuses them without one)
    if (cd "$ENGINE" && "$py" -c 'import netinv,sys; sys.exit(0 if netinv.serve_passcode() else 1)'); then
      args=(--lan --allow-write)
    else
      args=(--allow-write)
      echo "No passcode set, so the dashboard is this Mac only. For your phone: netinv set-passcode, then serve-start again."
    fi
  fi
  serve_stop quiet
  write_agent "$SERVE_LABEL" "$SERVE_PLIST" "$SERVE_LOG" "$py" netinv.py serve "${args[@]}"
  launchctl bootstrap "$DOMAIN" "$SERVE_PLIST"
  echo "Started $SERVE_LABEL: netinv.py serve ${args[*]}"
  echo "Log: $SERVE_LOG"
}

serve_stop() {
  if serve_loaded; then
    launchctl bootout "$DOMAIN/$SERVE_LABEL" 2>/dev/null || true
    [ "${1:-}" = quiet ] || echo "Stopped the dashboard."
  elif [ "${1:-}" != quiet ]; then
    echo "The dashboard LaunchAgent wasn't running."
  fi
}

serve_status() {
  if serve_loaded; then echo "LaunchAgent $SERVE_LABEL: loaded"; else echo "LaunchAgent $SERVE_LABEL: not loaded"; fi
  [ -f "$SERVE_LOG" ] && { echo "--- last lines of $SERVE_LOG"; tail -n 6 "$SERVE_LOG"; }
  return 0
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
  serve-start|serve-restart) shift; serve_start "$@" ;;
  serve-stop)   serve_stop ;;
  serve-status) serve_status ;;
  *)       sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
