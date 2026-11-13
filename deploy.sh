#!/usr/bin/env bash
# Deploy this checkout's netinventory engine to another machine (a Raspberry
# Pi, or another Mac) and restart it there, so it runs the same code and
# features as this one. Only committed code is copied (git ls-files), never
# your data files; keep the two copies' data in step with `netinv sync`.
#
# Usage:  ./deploy.sh user@host [path] [options]
#           path                  where it lives there (default: netinventory, in $HOME)
#           --copy-credentials    also copy cloud logins, the email settings, the
#                                 serve passcode and cached login tokens, over the
#                                 ssh pipe (stored in Keychain on a Mac, else in
#                                 ~/.netinv/credentials.json, owner-only)
#           --watch-args "..."    watch arguments there (default: --interval 300
#                                 --no-router; no --email, so you aren't mailed twice)
#           --no-serve            don't run the dashboard there
#           --no-restart          copy and install only
#
# Needs: key-based ssh to the host; python3 there (on a Pi also:
#        sudo apt install python3-venv nmap net-tools).
set -euo pipefail

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
[ $# -ge 1 ] || usage 1
case "$1" in -h|--help) usage 0;; esac

HOST="$1"; shift
DEST="netinventory"
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then DEST="$1"; shift; fi
COPY_CREDS=0; RESTART=1; SERVE=1; WATCH_ARGS="--interval 300 --no-router"
while [ $# -gt 0 ]; do
  case "$1" in
    --copy-credentials) COPY_CREDS=1;;
    --no-restart) RESTART=0;;
    --no-serve) SERVE=0;;
    --watch-args) WATCH_ARGS="${2:?--watch-args needs a value}"; shift;;
    -h|--help) usage 0;;
    *) echo "unknown option: $1" >&2; usage 1;;
  esac
  shift
done
case "$HOST" in -*|*[!A-Za-z0-9._@-]*) echo "bad host: $HOST" >&2; exit 1;; esac
case "$DEST" in *[!A-Za-z0-9._/~-]*|"") echo "bad path: $DEST" >&2; exit 1;; esac
case "$WATCH_ARGS" in *[!A-Za-z0-9._\ =-]*) echo "bad --watch-args: $WATCH_ARGS" >&2; exit 1;; esac

ROOT="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 --)

echo "→ checking ssh to $HOST"
"${SSH[@]}" "$HOST" true || { echo "  ssh failed: set up a key first (ssh-copy-id $HOST)" >&2; exit 1; }
# A box that already runs netinventory as a system service (e.g. homehub's
# /etc/systemd/system/netinventory.service behind Caddy) is managed elsewhere;
# a second copy here would fight it for port 8787.
if "${SSH[@]}" "$HOST" 'test -f /etc/systemd/system/netinventory.service'; then
  echo "  $HOST already runs netinventory as a system service (netinventory.service)." >&2
  echo "  Update it the way that box is managed (homehub: git pull in /opt/homehub/netinventory," >&2
  echo "  then sudo systemctl restart netinventory), not with deploy.sh." >&2
  exit 1
fi

echo "→ copying committed engine code to $HOST:$DEST"
FILES=$(cd "$ROOT" && git ls-files engine scan-jobs.sh deploy.sh | grep -v '^engine/tests/')
[ -n "$FILES" ] || { echo "  no tracked files found (run from a git checkout)" >&2; exit 1; }
(cd "$ROOT" && printf '%s\n' "$FILES" | tar -cf - -T -) \
  | "${SSH[@]}" "$HOST" "mkdir -p $DEST && tar -xf - -C $DEST"

if [ "$COPY_CREDS" = 1 ]; then
  echo "→ copying credentials over ssh (never written to disk on the way)"
  PY="$ROOT/engine/.venv/bin/python"; [ -x "$PY" ] || PY=python3
  (cd "$ROOT/engine" && "$PY" netinv.py creds-export) \
    | "${SSH[@]}" "$HOST" "cd $DEST/engine && P=.venv/bin/python; [ -x \$P ] || P=python3; \$P netinv.py creds-import"
fi

echo "→ installing on $HOST"
"${SSH[@]}" "$HOST" "DEST=$DEST RESTART=$RESTART SERVE=$SERVE WATCH_ARGS='$WATCH_ARGS' bash -s" <<'REMOTE'
set -euo pipefail
case "$DEST" in /*) ABS="$DEST";; ~*) ABS="${DEST/#\~/$HOME}";; *) ABS="$HOME/$DEST";; esac
cd "$ABS/engine"
command -v python3 >/dev/null || { echo "  ! python3 missing (sudo apt install python3)"; exit 1; }
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || { echo "  ! venv failed: sudo apt install python3-venv"; exit 1; }
fi
.venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1 || true
.venv/bin/python -m pip install -q -e '.[tuya,blink,ble]' \
  || echo "  ! some optional libraries failed to install; the core still runs"
for t in nmap arp; do
  command -v "$t" >/dev/null || [ -x "/usr/sbin/$t" ] \
    || echo "  ! $t missing: scans find nothing without it (sudo apt install nmap net-tools)"
done

if [ "$RESTART" != 1 ]; then echo "  installed (not restarted)"; exit 0; fi
if [ "$(uname)" = Darwin ]; then
  ../scan-jobs.sh restart $WATCH_ARGS
  echo "  (a Mac: start the dashboard there with: netinv serve --lan --allow-write)"
  exit 0
fi
command -v systemctl >/dev/null || { echo "  ! no systemd: start it with ./.venv/bin/python netinv.py watch $WATCH_ARGS"; exit 0; }
UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
unit() {  # name, description, args
  cat > "$UNIT_DIR/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target

[Service]
WorkingDirectory=$ABS/engine
Environment=PYTHONUNBUFFERED=1
ExecStart=$ABS/engine/.venv/bin/python netinv.py $3
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
EOF
}
unit netinv-watch "netinventory watch" "watch $WATCH_ARGS"
SERVE_ARGS="serve --lan"
.venv/bin/python -c 'import connectors,sys; sys.exit(0 if connectors.kc_get("serve","passcode") else 1)' \
  && SERVE_ARGS="serve --lan --allow-write"
unit netinv-serve "netinventory dashboard" "$SERVE_ARGS"
systemctl --user daemon-reload
systemctl --user enable netinv-watch >/dev/null 2>&1
systemctl --user restart netinv-watch
if [ "$SERVE" = 1 ]; then
  systemctl --user enable netinv-serve >/dev/null 2>&1
  systemctl --user restart netinv-serve
else
  systemctl --user disable --now netinv-serve >/dev/null 2>&1 || true
fi
systemctl --user --no-pager --lines=0 status netinv-watch netinv-serve 2>/dev/null | grep -E "●|Active:" || true
# `restart` returns as soon as the process starts; prove it stayed up (and
# that the dashboard answers) before anyone reads "deployed".
sleep 5
WANT="netinv-watch"; [ "$SERVE" = 1 ] && WANT="$WANT netinv-serve"
for u in $WANT; do
  if [ "$(systemctl --user is-active "$u")" != active ]; then
    journalctl --user -u "$u" -n 20 --no-pager || true
    echo "  ! $u is not running (log above)"; exit 1
  fi
done
if [ "$SERVE" = 1 ]; then
  ok=""
  for _ in 1 2 3 4 5; do
    python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8787/api/summary", timeout=5)' \
      2>/dev/null && { ok=1; break; }
    sleep 2
  done
  [ -n "$ok" ] || { journalctl --user -u netinv-serve -n 20 --no-pager || true
                    echo "  ! the dashboard doesn't answer on :8787 (log above)"; exit 1; }
fi
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
  echo "  ! to keep running after you log out and across reboots: sudo loginctl enable-linger $USER"
fi
[ "$SERVE_ARGS" = "serve --lan" ] && echo "  (dashboard is read-only there: no passcode stored; deploy with --copy-credentials, or run netinv set-passcode there)"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "  dashboard: http://${IP:-<pi-ip>}:8787/batteries"
REMOTE
echo "✓ deployed to $HOST:$DEST"
echo "  keep data in step from this Mac: netinv sync $HOST:$DEST   (or sync_target in ~/.netinv/config)"
