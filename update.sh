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
for a in "$@"; do
  [ "$a" = "--no-router" ] && ROUTER_FLAG=""
  [ "$a" = "--no-cloud" ]  && CLOUD_FLAG=""
done

./netinv.py update --scan $ROUTER_FLAG $CLOUD_FLAG --auto-name
echo
echo "Report: $(pwd)/report.md"
