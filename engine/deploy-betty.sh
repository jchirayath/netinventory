#!/usr/bin/env bash
# =============================================================================
# deploy-betty.sh — publish a read-only copy of the inventory to net.aspl.net.
#
#   ./deploy-betty.sh              # export + push the current data
#   ./deploy-betty.sh --dry-run    # show what would change, copy nothing
#   ./deploy-betty.sh --setup      # one time: create the site on betty + Entra gate
#   ./deploy-betty.sh --check      # probe for unauthenticated access (no login needed)
#
# betty is an Azure VM, so it cannot scan the home network: the Mac's watch
# keeps the data current (and syncs it with the homehub Pi), and each deploy
# pushes a fresh snapshot. The site is static (`netinv.py export-site`): the
# dashboard, the room board, the Batteries page and the JSON they read, served
# by betty's Caddy behind the shared Entra ID gate (oauth2-proxy, any aspl.net
# account) like the other *.aspl.net sites.
#
# Authenticates with your Entra identity (`az login` is the only prerequisite);
# the SSH certificate is refreshed below on every run. Data comes from this
# checkout, or from NETINV_HOME.
#
# The push REFUSES to run unless an anonymous request to the site is redirected
# to the Entra sign-in: device names, IPs and MACs never go up ungated.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

SITE="${NETINV_SITE:-net.aspl.net}"
HOST_IP="${BETTY_IP:-57.154.11.4}"
STACK="/home/azureuser/podcast"          # betty's compose project (add-site.sh lives here)
REMOTE="$STACK/www/$SITE"                # Caddy serves it as /srv/www/$SITE
SSHCFG="${BETTY_SSH_CONFIG:-$HOME/.netinv/betty-ssh.config}"   # holds the SSH cert: keep it out of /tmp
mkdir -p "$(dirname "$SSHCFG")" && chmod 700 "$(dirname "$SSHCFG")"

c()   { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

DRY="" SETUP="" CHECK=""
for a in "$@"; do
  case "$a" in
    --dry-run) DRY="--dry-run --itemize-changes" ;;
    --setup)   SETUP=1 ;;
    --check)   CHECK=1 ;;
    -h|--help) sed -n '3,8p' "$(basename "$0")" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option '$a' (see --help)" ;;
  esac
done

# 302 to the Entra sign-in = gated. Anything else (200 above all) is not.
# Checks the data files as well as the pages, plain and with the identity
# headers oauth2-proxy would set (Caddy must strip them, not trust them), and
# a few path spellings that reach the same file.
gated() {
  local p h out
  for p in / /index.html /rooms/ /rooms/index.html /batteries/ /batteries/index.html \
           /api/board /api/devices /api/events /api/summary /api/alerts /api/monitors \
           /api/batteries //api/board /%61pi/batteries; do
    for h in "X-Probe: 1" "X-Auth-Request-Email: probe@aspl.net" "X-Forwarded-User: probe@aspl.net"; do
      out="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 25 --path-as-is \
             -H "$h" --resolve "$SITE:443:$HOST_IP" "https://$SITE$p" || echo "000")"
      [[ "$out" == 30[23]\ *"/oauth2/start"* ]] || { echo "  ungated: $p [$h] -> $out" >&2; return 1; }
    done
  done
}

if [[ -n "$CHECK" ]]; then
  gated || die "https://$SITE answers without sign-in — see above"
  ok "every probe of https://$SITE redirects to the Entra sign-in"
  exit 0
fi

command -v az >/dev/null || die "azure-cli required (brew install azure-cli)"
az account show >/dev/null 2>&1 || die "not logged in — run: az login"

c "Refreshing the Entra SSH certificate"
# Show az's own error: an expired MFA session (AADSTS50078) needs a fresh
# `az login`, not a role change, and the two look alike from the outside.
if ! AZERR="$(az ssh config --file "$SSHCFG" --resource-group rg-podcast --name podcast --overwrite 2>&1 >/dev/null)"; then
  echo "$AZERR" >&2
  die "could not generate the SSH config (expired sign-in: az login; else need 'Virtual Machine Administrator Login' on the VM)"
fi
SSH=(ssh -F "$SSHCFG" -o BatchMode=yes -o ConnectTimeout=20)
"${SSH[@]}" "$HOST_IP" true 2>/dev/null || die "cannot reach betty at $HOST_IP"
ok "authenticated as $("${SSH[@]}" "$HOST_IP" 'whoami')"

if [[ -n "$SETUP" ]]; then
  # Caddy needs DNS before it can get a certificate, and add-entra-auth.sh
  # proves the gate over HTTPS, so DNS comes first.
  RESOLVED="$(dig +short "$SITE" A | tail -1)"
  [[ "$RESOLVED" == "$HOST_IP" ]] || die "$SITE resolves to '${RESOLVED:-nothing}', not $HOST_IP — add the DNS record first"
  ok "$SITE resolves to $HOST_IP"
  c "Creating the static site and the Entra gate on betty"
  # add-site.sh validates + reloads Caddy and removes the site if the config is
  # bad; add-entra-auth.sh rolls back on a bad config and fails if the site
  # still answers 200 unauthenticated. Until the gate is on, the site holds
  # only add-site.sh's placeholder page: no data is pushed before then.
  "${SSH[@]}" "$HOST_IP" "sudo bash -c 'cd $STACK && { [ -f sites/$SITE.caddy ] || ./add-site.sh $SITE; } && ./add-entra-auth.sh $SITE'"
  gated || die "$SITE is not redirecting to the Entra sign-in — not pushing anything"
  ok "$SITE is gated; run ./deploy-betty.sh to publish the data"
  exit 0
fi

# A checkout without the data (a fresh clone or worktree) would export an empty
# inventory, and --delete would publish that.
DATA="${NETINV_HOME:-.}"
[[ -s "$DATA/devices.csv" ]] || die "no devices.csv in $DATA — run from the checkout with your data, or set NETINV_HOME"
"${SSH[@]}" "$HOST_IP" "sudo test -d $REMOTE" || die "$REMOTE does not exist on betty — run ./deploy-betty.sh --setup first"
gated || die "https://$SITE does not redirect anonymous visitors to the Entra sign-in.
  Refusing to publish the inventory ungated. Check with: ssh betty, cd $STACK, ./add-entra-auth.sh --list"
ok "$SITE is behind Entra"

c "Exporting the snapshot"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/netinv-site.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT
PY="python3"; [ -x ".venv/bin/python" ] && PY=".venv/bin/python"
NETINV_HOME="$DATA" "$PY" netinv.py export-site "$OUT" >/dev/null
ok "$(find "$OUT" -type f | wc -l | tr -d ' ') files"

# Prove the gate again right before the copy.
gated || die "https://$SITE stopped redirecting to the Entra sign-in — not pushing"

c "Syncing to betty${DRY:+ (dry run)}"
# --delete so a page or API file dropped from the export disappears too.
rsync -rlz --delete $DRY --rsync-path="sudo rsync" -e "ssh -F $SSHCFG -o BatchMode=yes" \
  "$OUT/" "$HOST_IP:$REMOTE/"
[[ -n "$DRY" ]] && { echo; echo "  DRY RUN — nothing copied."; exit 0; }
ok "synced"

# Caddy runs with cap_drop: ALL (no DAC_OVERRIDE), so it can only serve
# world-readable files; without this, freshly synced files 403.
"${SSH[@]}" "$HOST_IP" "sudo chown -R azureuser:azureuser $REMOTE && sudo chmod -R a+rX $REMOTE"
ok "permissions set"

c "Verifying"
if ! gated; then
  # Fail closed: take the data back down rather than leave it up ungated.
  "${SSH[@]}" "$HOST_IP" "sudo find $REMOTE -mindepth 1 -delete" \
    && die "$SITE stopped redirecting to the Entra sign-in after the push — the data was removed; check the gate" \
    || die "$SITE is UNGATED and the data could not be removed — remove $REMOTE/* on betty now"
fi
ok "anonymous requests to https://$SITE still redirect to the Entra sign-in"
echo
echo "  https://$SITE/            devices"
echo "  https://$SITE/rooms/      room board"
echo "  https://$SITE/batteries/  batteries"
