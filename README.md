# netinventory

A small, dependency-light tool for keeping a **friendly, durable inventory of every
device on your home network** — even when DHCP keeps changing their IPs and the
discovered names look nothing like the real devices.

It identifies devices by **MAC address** (the stable identity — a device can have
several, e.g. wired + Wi-Fi), re-discovers their **IP** on every run, and lets you
keep a curated **friendly name** on top that survives every refresh. It pulls names
from your router, from UPnP/SSDP, and optionally from smart-home clouds, and it can
even classify a Wi-Fi survey into *your* devices vs the *neighbors'*.

> Built for a Comcast/Xfinity (Technicolor) gateway, but the local-scan, SSDP, and
> cloud features work on any network. The router scraper is gateway-specific.

```
$ ./update.sh
  router reported 121 devices (55 online)
  local scan found 104 hosts on 10.0.0.0/24
  SSDP discovered 7 named devices
  auto-named 5 device(s) from discovery
  135 MACs in devices.csv · 56 online this run
```

## Why

Home networks accumulate dozens of devices. The router shows cryptic names
(`wlan0-53`, `ESP_921C68`, a bare MAC), IPs churn with DHCP, and a device with both
a wired and a Wi-Fi NIC shows up as two unrelated entries. netinventory gives every
device one friendly name tied to all of its MACs, tracks whether it's wired or
wireless, and flags devices that have gone defunct.

## Quick start

```bash
git clone https://github.com/jchirayath/netinventory.git
cd netinventory

./netinv.py update --scan          # local subnet scan only (no router/cloud needed)
open inventory.xlsx                 # or open devices.csv in a spreadsheet
```

Name things (rows sharing a name are treated as one device):

```bash
./netinv.py name "Living Room TV" AA:BB:CC:00:00:01 AA:BB:CC:00:00:02
```

…or just type names into the `Device` column of `devices.csv` — they're never
overwritten by a refresh.

## What it produces

| File | Contents |
|---|---|
| `devices.csv` | Master registry, one row per MAC, **sorted by IP**. You edit `Device` + `Notes`; everything else auto-refreshes. |
| `inventory.xlsx` | Workbook with three tabs: **Devices** (detail, filterable), **By Device** (multi-MAC devices collapsed to one row), **Summary** (rollups by category, connection, status). |
| `report.md` / `report-by-device.csv` | Plain-text/Markdown reports. |
| `wifi_scan.csv` | Output of the Wi-Fi neighbor classifier. |

> All generated files contain real MACs/IPs and are **git-ignored**. See
> `devices.example.csv` for the format.

## Discovery sources (merged by MAC on every run)

1. **Local scan** — `nmap` ping-sweep → system ARP table → reverse DNS.
2. **SSDP / UPnP** — reads each device's `<friendlyName>` (often the name *you* gave
   it, e.g. "Family Room TV").
3. **Router** — scrapes the gateway's connected-devices page for DHCP hostnames,
   wired/wireless link type, and authoritative online/offline status.
4. **Smart-home clouds** (optional) — the names you set in each app.

`--auto-name` fills a blank `Device` from the best discovered name; it never
overwrites a name you set.

## Commands

```
./netinv.py update [--scan] [--router] [--cloud] [--auto-name] [--notify]   # gather + merge + report
./netinv.py watch [--interval 300] [--notify] [--once]           # poll & alert on new devices
./netinv.py name "Friendly Name" <mac> [<mac> ...]               # group MAC(s) under a name
./netinv.py show [filter]                                        # print inventory
./netinv.py report                                               # rebuild reports from devices.csv
./netinv.py pairs [--by-mac] [--apply]                           # find likely wired+wifi duplicates
./netinv.py wifi-scan [file] [--add-mine]                        # classify a Wi-Fi survey
./netinv.py set-router-password                                  # store router login in Keychain
./netinv.py set-cloud <provider>                                 # store a cloud connector's creds
./update.sh                                                      # one-shot: everything above
```

## Wired vs wireless, and dual-NIC devices

The router reports each device's connection medium **and Wi-Fi band**, stored in the
`Link` column (`Ethernet`, `Wi-Fi 2.4 GHz`, `Wi-Fi 5 GHz`, `Wi-Fi 6 GHz`). The
**Devices** tab shows both a coarse `Link` (Wired/Wireless) and the exact `Band`; the
**Summary** tab includes a *Devices by Band* rollup. Filter from the CLI:

```bash
./netinv.py show "5 ghz"      # everything on the 5 GHz band
./netinv.py show wired        # everything on Ethernet
```

A device with both a wired and a Wi-Fi interface is two MACs sharing one `Device`
name; the **By Device** tab shows its combined connections (e.g. `Ethernet + 5 GHz`).

`netinv.py pairs` suggests MACs that are probably the same physical device:
- by **shared hostname** (default), or
- `--by-mac`: same vendor OUI with near-sequential MACs (looser, opt-in).

## Wi-Fi neighbor scan (mine vs neighbor)

Paste your gateway's "neighboring networks" survey and find out which APs are yours:

```bash
pbpaste | ./netinv.py wifi-scan             # paste the survey table on stdin
./netinv.py wifi-scan scan.txt              # or from a file
./netinv.py wifi-scan scan.txt --add-mine   # fold the "mine" BSSIDs into devices.csv
```

A BSSID is **mine** if it broadcasts one of your home SSIDs (`home_ssids.txt`, or the
`NETINV_HOME_SSIDS` env var) or its radio matches an inventory MAC — i.e. the same
last 5 octets / locally-administered-bit flip, or the same OUI with a small last-octet
offset, which is how a device's AP/Direct radio relates to its client MAC. Everything
else is a **neighbor**; signal strength is shown to break ties.

## Detecting new devices

Every MAC gets a `FirstSeen` timestamp the first time it's observed. When a
refresh sees a MAC it has never recorded, it prints it, appends a row to
`events.csv` (an append-only join history), and — with `--notify` — pops a macOS
notification.

```bash
./netinv.py update --scan --router --notify   # one-off, alert on any new device
./netinv.py watch --interval 300 --notify     # poll every 5 min, alert on joins
./netinv.py watch --once --no-router           # single local-only sweep
```

`watch` keeps polling (gateway + local scan + SSDP), merging, and alerting until
you stop it — leave it running in a terminal, or wrap it in a `launchd`/cron job
for unattended monitoring. Each join is captured with its time, IP, MAC, vendor,
hostname, and band in `events.csv`.

## Status / staleness

A device confirmed by the gateway (or seen on the wire when the gateway isn't used)
is `online`; one the gateway lists but isn't currently connected is `offline`; one
only ever seen in an imported export and never confirmed live ages to `defunct`.

## Credentials (macOS Keychain)

Secrets are **never written to disk** — they live in the macOS Keychain.

```bash
./netinv.py set-router-password             # internet-password for your gateway

# Cloud SDKs go in a local venv (Homebrew Python blocks system pip);
# update.sh uses it automatically when present.
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-cloud.txt   # only what you use
./.venv/bin/python netinv.py set-cloud tuya                   # then wyze / blink / ...
./.venv/bin/python netinv.py cloud                            # pull names from clouds
```

For **Tuya/SmartLife** the cloud returns the app name but no LAN MAC (only a
public IP). The connector runs a local Tuya broadcast scan to map each cloud
device → its LAN IP → MAC (via ARP), falling back to the MAC embedded in
all-hex WiFi device ids. Zigbee/BLE sub-devices behind a hub don't broadcast on
WiFi, so they land in `cloud_devices.csv` as a name reference.

### Smart-home cloud connectors

| Provider | Credentials | Yields | Match |
|---|---|---|---|
| **tuya** / SmartLife | Tuya IoT Cloud project → Access ID/Secret + region | name + MAC + IP | direct |
| **wyze** | Wyze API Key ID + API Key (+ login) | nickname + MAC | direct |
| **blink** | account email/password (+ 2FA) | camera names | by IP / reference |
| **smartthings** | Personal Access Token | device labels | reference |
| **apple** | Apple ID + app-specific password | device roster (name + model) | reference¹ |

¹ Apple exposes no MAC/IP and Wi-Fi MACs are randomized, so the roster is a reference
only. Map each once by reading the device's Wi-Fi Address (iOS: Settings → Wi-Fi → ⓘ);
the private MAC is stable per network, so the label sticks.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `NETINV_ROUTER` | `192.168.1.1` | Gateway host |
| `NETINV_SUBNET` | `192.168.1.0/24` | Subnet to scan |
| `NETINV_STALE_DAYS` | `30` | Days unseen before a device is `defunct` |
| `NETINV_HOME_SSIDS` | — | Comma-separated home SSIDs for `wifi-scan` |

## Requirements

- Python 3.8+ (standard library only for the core)
- `nmap` for the ping-sweep (`brew install nmap`)
- macOS for the Keychain credential storage (`security`)
- Optional: `xlsxwriter` for the workbook, and the per-connector SDKs in
  `requirements-cloud.txt`

## Privacy

`devices.csv`, the reports, `inventory.xlsx`, `wifi_scan.csv`, and `home_ssids.txt`
all contain real network details and are git-ignored. Credentials are stored only in
the macOS Keychain. Nothing identifying your network is committed to the repo.

## License

MIT
