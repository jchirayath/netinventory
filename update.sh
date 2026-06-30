#!/usr/bin/env bash
# One-shot refresh of the network inventory.
#   - live subnet scan (nmap ping-sweep + ARP + reverse DNS)
#   - SSDP/UPnP friendly-name discovery
#   - Xfinity gateway connected-devices scrape (DHCP hostnames)
#   - smart-home cloud connectors (any configured: tuya/wyze/blink/smartthings)
#   - auto-fills blank friendly names from high-quality discovered names
#   - merges into devices.csv, preserving your friendly names + notes
#   - rebuilds report.md and report-by-device.csv
#
# Router credentials come from the macOS Keychain (./netinv.py set-router-password).
# Cloud credentials too (./netinv.py set-cloud <provider>). Flags:
#   --no-router   skip the gateway        --no-cloud   skip cloud connectors
set -euo pipefail
cd "$(dirname "$0")"

[ -f "$HOME/.netinv.env" ] && set -a && . "$HOME/.netinv.env" && set +a

ROUTER_FLAG="--router"
CLOUD_FLAG="--cloud"
FP_FLAG="--fingerprint --fp-fast"   # OUI is always on; active probe is incremental
for a in "$@"; do
  [ "$a" = "--no-router" ]      && ROUTER_FLAG=""
  [ "$a" = "--no-cloud" ]       && CLOUD_FLAG=""
  [ "$a" = "--no-fingerprint" ] && FP_FLAG=""
done

# use the local venv (cloud SDKs live there) when present, else system python3
PY="python3"; [ -x ".venv/bin/python" ] && PY=".venv/bin/python"
"$PY" netinv.py update --scan $ROUTER_FLAG $CLOUD_FLAG $FP_FLAG --auto-name --notify
echo
echo "Report: $(pwd)/report.md"
