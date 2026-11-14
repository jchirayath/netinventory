#!/usr/bin/env bash
# =============================================================================
# deploy-betty.sh — publish the project website at https://netinventory.aspl.net
#
#   site/deploy-betty.sh             # stage, sync to betty, verify
#   site/deploy-betty.sh --dry-run   # show what would change
#
# A static page: site/index.html plus the README's dashboard screenshots and
# the logo, staged into one folder and synced to betty's
# /home/azureuser/podcast/www/netinventory, which Caddy serves with no
# sign-in. The Caddy site block is kept in betty's own repo
# (~/code/azure/PodcastHost, stack/sites/netinventory.aspl.net.caddy).
# The live dashboard is a different thing: net.aspl.net, engine/deploy-betty.sh.
#
# Authenticates with your Entra identity (`az login`).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

SITE="netinventory.aspl.net"
HOST_IP="${BETTY_IP:-57.154.11.4}"
REMOTE="/home/azureuser/podcast/www/netinventory"
SSHCFG="${BETTY_SSH_CONFIG:-$HOME/.netinv/betty-ssh.config}"   # holds the SSH cert: keep it out of /tmp
DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="--dry-run"

c()   { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v az >/dev/null || die "azure-cli required"
az account show >/dev/null 2>&1 || die "not logged in — run: az login"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/img"
cp site/index.html "$STAGE/"
cp assets/screenshots/web-devices.png assets/screenshots/web-rooms.png \
   assets/screenshots/web-batteries.png assets/screenshots/web-battery-history.png \
   assets/brand/netinventory-logo.svg "$STAGE/img/"
# every image the page names must be staged
for f in $(grep -oE 'img/[A-Za-z0-9._-]+' site/index.html | sort -u); do
  [[ -f "$STAGE/$f" ]] || die "index.html uses $f, which isn't staged"
done
ok "staged $(find "$STAGE" -type f | wc -l | tr -d ' ') files"

mkdir -p "$(dirname "$SSHCFG")" && chmod 700 "$(dirname "$SSHCFG")"
c "Refreshing the Entra SSH certificate"
az ssh config --file "$SSHCFG" --resource-group rg-podcast --name podcast --overwrite >/dev/null 2>&1 \
  || die "could not generate the SSH config"
SSH=(ssh -F "$SSHCFG" -o BatchMode=yes -o ConnectTimeout=20)
"${SSH[@]}" "$HOST_IP" true 2>/dev/null || die "cannot reach betty at $HOST_IP"

c "Syncing to $REMOTE${DRY:+ (dry run)}"
"${SSH[@]}" "$HOST_IP" "sudo mkdir -p $REMOTE"
rsync -rltz --delete --itemize-changes $DRY --rsync-path="sudo rsync" \
  -e "ssh -F $SSHCFG -o BatchMode=yes" "$STAGE/" "$HOST_IP:$REMOTE/"
[[ -n "$DRY" ]] && { echo "  dry run: nothing copied"; exit 0; }
"${SSH[@]}" "$HOST_IP" "sudo chown -R azureuser:azureuser $REMOTE && sudo chmod -R a+rX,go-w $REMOTE"
ok "synced"

c "Verifying"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 --resolve "$SITE:443:$HOST_IP" "https://$SITE/" || echo 000)
[[ "$code" == "200" ]] || die "https://$SITE/ answered $code, not 200"
img=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 --resolve "$SITE:443:$HOST_IP" "https://$SITE/img/web-rooms.png" || echo 000)
[[ "$img" == "200" ]] || die "https://$SITE/img/web-rooms.png answered $img"
ok "https://$SITE/ is up, with no sign-in"
