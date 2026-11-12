#!/usr/bin/env bash
# =============================================================================
# deploy-betty.sh — update the live netinventory copy at net.aspl.net.
#
#   ./deploy-betty.sh              # ship this checkout's committed engine, restart
#   ./deploy-betty.sh --check      # probe the gates (no login needed)
#
# net.aspl.net runs `netinv serve --allow-write` in a container on betty (the
# `netinventory` service in betty's compose.yml, code mounted read-only from
# $STACK/netinventory, data in $STACK/netinventory-data). betty can't scan the
# home network, so it holds no data of its own: the homehub Pi's `netinv agent`
# connects OUT to https://net.aspl.net/api/agent/* with a bearer token, pushes
# the scan data, levels and events, and merges edits both ways. Nothing on the
# internet can open a connection toward the house.
#
# Two gates, both enforced by betty's Caddy (sites/net.aspl.net.caddy):
#   people   every path but /api/agent/* is behind the shared Entra ID sign-in,
#            and edits also need the passcode (serve sees them as proxied)
#   the Pi   /api/agent/* skips Entra and needs the agent token instead
#
# Authenticates with your Entra identity (`az login`); the SSH certificate is
# refreshed on every run. Refuses to ship if the gates are not as above.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

SITE="${NETINV_SITE:-net.aspl.net}"
HOST_IP="${BETTY_IP:-57.154.11.4}"
STACK="/home/azureuser/podcast"          # betty's compose project
CODE="$STACK/netinventory"               # mounted read-only at /app
SSHCFG="${BETTY_SSH_CONFIG:-$HOME/.netinv/betty-ssh.config}"   # holds the SSH cert: keep it out of /tmp
mkdir -p "$(dirname "$SSHCFG")" && chmod 700 "$(dirname "$SSHCFG")"

c()   { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CHECK=""
for a in "$@"; do
  case "$a" in
    --check)   CHECK=1 ;;
    -h|--help) sed -n '3,6p' "$(basename "$0")" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option '$a' (see --help)" ;;
  esac
done

probe() {  # path [header] [POST body] -> "code redirect"
  curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 25 --path-as-is \
       ${2:+-H "$2"} ${3:+-H "Content-Type: application/json" --data "$3"} \
       --resolve "$SITE:443:$HOST_IP" "https://$SITE$1" || echo "000"
}

# People: 302 to the Entra sign-in on every page and data path, plain and with
# the identity headers oauth2-proxy would set (Caddy must strip, not trust, them),
# and with path spellings that reach the same handler.
# The Pi: /api/agent/* answers 401 without the token, never the sign-in and
# never data.
gated() {
  local p h out bad=0
  for p in / /index.html /rooms /batteries /api/board /api/devices /api/events \
           /api/summary /api/alerts /api/monitors /api/batteries "/api/battery?device=x" \
           //api/batteries /%61pi/batteries /api/agent/../batteries; do
    for h in "X-Probe: 1" "X-Auth-Request-Email: probe@aspl.net" "X-Forwarded-User: probe@aspl.net"; do
      out="$(probe "$p" "$h")"
      [[ "$out" == 30[23]\ *"/oauth2/start"* ]] || { echo "  ungated: $p [$h] -> $out" >&2; bad=1; }
    done
  done
  # One wrong token only: each counts toward the per-client lockout, and from
  # home this machine shares the Pi's public IP.
  for pr in "/api/agent/stream|X-Probe: 1|" "/api/agent/sync|X-Probe: 1|{\"op\":\"export\"}" \
            "/api/agent/stream|Authorization: Bearer wrong|"; do
    IFS='|' read -r p h body <<<"$pr"
    out="$(probe "$p" "$h" "$body")"
    [[ "$out" == 401\ * ]] || { echo "  agent path: $p [$h] -> $out (want 401)" >&2; bad=1; }
  done
  return $bad
}

if [[ -n "$CHECK" ]]; then
  gated || die "https://$SITE is not gated as it should be — see above"
  ok "pages and data redirect to the Entra sign-in; the agent path wants its token"
  exit 0
fi

[[ -z "$(git status --porcelain -- . 2>/dev/null)" ]] || echo "  (uncommitted engine changes are NOT shipped: only HEAD is)"

command -v az >/dev/null || die "azure-cli required (brew install azure-cli)"
az account show >/dev/null 2>&1 || die "not logged in — run: az login"
c "Refreshing the Entra SSH certificate"
if ! AZERR="$(az ssh config --file "$SSHCFG" --resource-group rg-podcast --name podcast --overwrite 2>&1 >/dev/null)"; then
  echo "$AZERR" >&2
  die "could not generate the SSH config (expired sign-in: az login; else need 'Virtual Machine Administrator Login' on the VM)"
fi
SSH=(ssh -F "$SSHCFG" -o BatchMode=yes -o ConnectTimeout=20)
"${SSH[@]}" "$HOST_IP" true 2>/dev/null || die "cannot reach betty at $HOST_IP"
ok "authenticated as $("${SSH[@]}" "$HOST_IP" 'whoami')"

gated || die "https://$SITE is not gated as it should be — fix that before shipping code"
ok "$SITE gates are in place"

REV="$(git rev-parse --short HEAD)"
c "Shipping engine $REV"
# Committed code only (git archive), into a fresh directory swapped in whole.
git archive --format=tar HEAD -- . ':!tests' | "${SSH[@]}" "$HOST_IP" "sudo bash -c '
  set -e; rm -rf $CODE.new; mkdir -p $CODE.new; tar -x -C $CODE.new
  echo $REV > $CODE.new/REVISION
  rm -rf $CODE.old; [ -d $CODE ] && mv $CODE $CODE.old; mv $CODE.new $CODE
  chmod -R a+rX $CODE
  cd $STACK && docker compose up -d --force-recreate --no-deps netinventory >/dev/null 2>&1'"
ok "code at $REV, container restarted"

c "Verifying"
for _ in $(seq 1 20); do
  st="$("${SSH[@]}" "$HOST_IP" "sudo docker inspect -f '{{.State.Status}}' podcast-netinventory-1" 2>/dev/null || true)"
  [[ "$st" == running ]] && break; sleep 1
done
[[ "$st" == running ]] || { "${SSH[@]}" "$HOST_IP" "sudo docker logs --tail 20 podcast-netinventory-1" >&2 || true; die "container is $st"; }
sleep 2
gated || die "$SITE is not gated as it should be after the restart — check sites/$SITE.caddy now"
ok "running; gates still in place"
echo "  https://$SITE/batteries"
