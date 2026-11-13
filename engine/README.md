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
cd netinventory/engine

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
3. **mDNS / DNS-SD** — multicast query for common service types (AirPlay, Chromecast,
   HomeKit, printers, …); reads DNS-SD *instance* names (`fn=`) and models (`md=`).
   Because it keys on advertised names, not the OUI, it identifies devices that use a
   **randomized MAC** (modern iOS/Android/macOS), which OUI lookup can't. A discovered
   model fills an *empty* Vendor only — it never overrides a real OUI manufacturer.
   Spotify Connect speakers are also asked for their zeroconf `getInfo`: receivers and
   TVs report their own name, and Echos (which hide their Alexa name) report a model,
   so `SpotifyConnect #4` becomes `Amazon Echo Dot`. `--auto-name` numbers look-alikes
   (`Amazon Echo Dot 2`). Skip with `--no-mdns`.
4. **Router** — scrapes the gateway's connected-devices page for DHCP hostnames,
   wired/wireless link type, and authoritative online/offline status. The
   authenticated session cookie is cached (`router_cookies.txt`) and reused, so
   it only logs in when the session has expired — repeated logins trip the
   gateway's brute-force lockout. If a lockout is detected it backs off for
   `NETINV_ROUTER_COOLDOWN_MIN` minutes (default 30) instead of hammering it.
5. **Smart-home clouds** (optional) — the names you set in each app.

`--auto-name` fills a blank `Device` from the best discovered name; it never
overwrites a name you set.

## Commands

```
# Inventory
./netinv.py update [--scan] [--router] [--cloud] [--auto-name] [--notify]   # gather + merge + report
./netinv.py watch [--interval 300] [--notify] [--email] [--once] [--sync T]  # poll & alert on new devices
./netinv.py name "Friendly Name" <mac> [<mac> ...]               # group MAC(s) under a name
./netinv.py show [filter]                                        # print inventory
./netinv.py history "<name|mac>"                                 # join history + record
./netinv.py alerts [--rule id] [--notify]                        # evaluate alert rules
./netinv.py report                                               # rebuild reports from devices.csv
./update.sh                                                      # one-shot: scan, router, cloud, report

# Identifying devices
./netinv.py pairs [--by-mac] [--apply]                           # find likely wired+wifi duplicates
./netinv.py fingerprint [ip|mac] [--fast] [--apply]              # identify a device by ports/banners
./netinv.py wifi-scan [file] [--add-mine]                        # classify a Wi-Fi survey
./netinv.py cloud [provider ...]                                 # pull names from smart-home clouds
./netinv.py blink-locate [camera ...] [--apply] [--via user@pi]  # wake Blink cameras to learn their MACs

# Rooms, types and names (docs/home-standard.md)
./netinv.py home rooms|draft|check|push [--from tuya|kasa|ha|smartthings]

# Monitors and batteries
./netinv.py monitor [list|on|off|check|suggest|batteries]        # offline / low-battery alerts
./netinv.py battery [list|log|history|edit|delete]               # battery changes + levels

# Dashboard, sync and deploy
./netinv.py serve [--host H] [--port 8787] [--lan] [--allow-write]   # JSON API + web dashboard
./netinv.py sync user@host[:path] [--dry-run] [--scanner]        # merge edits with another copy
./netinv.py agent [https://net.aspl.net] [--once]                 # keep a web copy in step, outbound only
./netinv.py agent-token [--generate]                             # the agent's shared token
../deploy.sh user@host [--copy-credentials]                      # install/update on a Pi

# Credentials and settings
./netinv.py set-router-password                                  # router login (Keychain)
./netinv.py set-cloud <provider>                                 # a cloud connector's creds
./netinv.py blink-login | govee-login                            # interactive 2FA / emailed-code login
./netinv.py set-email                                            # SMTP settings for watch --email
./netinv.py set-passcode                                         # passcode for edits from other devices
./netinv.py creds-export [--only agent,serve] | creds-import     # pipe credentials to another copy
```

`sync-export` / `sync-import` also exist; `sync` runs them on the other copy
over ssh, so you don't call them yourself.

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
you stop it. Each poll also logs a `leave` event when a device that was online
stops answering and a `back` event when an offline one returns (it pings every
host, so the time is the poll's, not when the ARP cache lets go ~20 min later).
`./netinv.py set-email` stores SMTP settings in the Keychain (Gmail: an app
password) and sends a test; `watch --email` then mails each poll's new devices
and known devices whose IP or hostname changed (an `update` event), with
name, room, MAC, IP, vendor and time. A device coming back after 30+ days
unseen (`stale_days`) is mailed too, with when it was last seen; other leaves
and comebacks are logged but not mailed, since battery devices that sleep
between check-ins flap. Leave the
password blank and mail goes out through Mail.app's own account instead (it is
also the fallback when SMTP fails); macOS asks once to let the job control Mail.
Each join is captured with its time, IP, MAC, vendor, hostname, and
band in `events.csv`. To keep `watch --email` running in the background (starts
at login, restarted if it dies), use `scan-jobs.sh` at the repo root:

```bash
../scan-jobs.sh start        # default: --interval 120 --no-router --email
../scan-jobs.sh status       # loaded? running? last log lines
../scan-jobs.sh logs         # follow engine/netinv-watch.log
../scan-jobs.sh stop         # also stops a watch started by hand
```

A poll in which the scan hears nobody at all (Wi-Fi down, the Mac away from
home) changes nothing: no device goes offline, no `leave` events, no monitor
alerts, and no heartbeat. An email that fails to send is not lost: monitor
alerts wait and go with the next poll's email. Log lines carry a timestamp
and a level (`INFO`, `WARN`, `ERROR`), so `grep -E 'WARN|ERROR'
netinv-watch.log` lists every problem and when it started.

## Device monitors (offline too long, battery low)

Pick the devices you care about (the NAS, a desktop Mac, door locks, battery
cameras, the water meter) and get one email when one stays offline past its
limit or its battery drops to a threshold, and one more when it recovers:

```bash
./netinv.py monitor suggest             # propose monitors from your inventory + clouds
./netinv.py monitor suggest --apply     # add them to monitors.csv
./netinv.py monitor                     # list: on/off, limits, live status
./netinv.py monitor on "QNAP NAS" --offline 30m --check tcp:8080
./netinv.py monitor on "Garage door" --battery 25
./netinv.py monitor off "Foscam Camera"           # keeps its settings
./netinv.py monitor batteries           # every battery level the clouds report
./netinv.py monitor check --email       # one pass on its own, no network sweep
```

`monitors.csv` is one row per device, editable by hand:

| Column | Meaning |
|---|---|
| `Device` | name in `devices.csv` (or its hostname), or a cloud's name for a device with no MAC |
| `Enabled` | `yes` / `no`: the toggle |
| `OfflineAfter` | `30m`, `2h`, `1d`: alert once it has been gone this long (blank = never) |
| `BatteryBelow` | percent: alert at or below it (blank = never) |
| `Check` | `ping` (default with a MAC), `tcp:PORT` (the service is up, not just the box), `cloud` (the vendor's online flag, for battery devices that sleep off Wi-Fi; default with no MAC), `ble` (heard in this Mac's Bluetooth scan), `none` |
| `Match` | optional `provider=name` when the cloud calls it something else (`flume=Flume Sensor`) |

Battery levels come from **Home Assistant** (every `battery` entity, so
SmartThings, Zigbee and BLE sensors too), **Tuya / Smart Life** (locks,
door and flood sensors), **Blink** (`ok` / `low` per battery camera and
doorbell) and **Flume** (`set-cloud flume` with an API client id and secret
from portal.flumewater.com). Labels map to percents: `low` = 10,
`middle`/`medium` = 50, `ok`/`high` = 100.

**Bluetooth sensors** (Govee thermometers and hygrometers) are heard
directly by this Mac in a 30-second passive scan each hour (`pip install
bleak`; macOS asks once to allow Bluetooth). It only listens, so it uses no
network. A sensor counts as online when the scan hears it, and Govee
H5074 / H507x / H510x / H517x broadcasts carry the battery. One scan can
miss a sensor, so give `ble` monitors a limit of hours, not minutes.
macOS hides Bluetooth MACs, so a monitor is named after what the sensor
broadcasts (`H5101_4670`). Rename the `Device` and keep
`Match ble=H5101_4670`.

**Govee sensors through the Govee app's account** (`govee-app`): the
developer API key reports every Bluetooth and hub sensor as offline, but
the account API the Govee app itself uses has the real battery and online
state of sensors behind a Govee hub: the H5054/H5058 leak sensors, which
talk only to their hub and can't be heard over Bluetooth. A sensor counts
as online only while its hub is too, as in the app. Bluetooth thermometers
are left out: they sync to the cloud only when a phone's Govee app is
near, so their status there can be years old.

```bash
./netinv.py set-cloud govee-app     # Govee email + password (Keychain)
./netinv.py govee-login             # once, in a terminal: enters the emailed code if asked
```

The token is cached in `~/.netinv_govee.json` (owner-only), so the hourly
check makes one call and logs in again only when the token expires. If
Govee wants a new code, the monitor log says so; run `govee-login`
again. This API is undocumented (the calls follow homebridge-govee) and
can change. If it breaks, its readings stop and nothing is marked offline.

It costs very little bandwidth. `watch` decides presence from the sweep it
already does and sends nothing extra, except one TCP connect for a
`tcp:PORT` check. `monitor check` sends one ping per device, then up to five
single TCP connects if the ping goes unanswered (a reset proves a host is up
when it drops pings). The clouds are asked once an hour
(`monitor_cloud_every`), at one call each for Tuya and Flume and a local
call for Home Assistant. Alerts go to email (`watch --email`), macOS
notifications (`--notify`) and `events.csv` (`offline`, `online`,
`battery-low`, `battery-ok`). `GET /api/monitors` serves the list with live
status.

The Mac running the watch can't report its own outage. Set `heartbeat_url`
to a dead-man check (e.g. a free healthchecks.io URL). Each poll pings it,
and the service emails you when the pings stop.

For a periodic full refresh instead, install the update LaunchAgent:

```bash
./contrib/install-launchagent.sh 1800     # run ./update.sh every 30 min
./contrib/install-launchagent.sh uninstall
```

## Battery changes and levels

`battery_log.csv` records when batteries were changed, with the battery
type and your notes. `battery_history.csv` records each monitored device's
level whenever the hourly check sees it change. When a level jumps from 50%
or less to 40+ points higher, as reported by the same provider, the change is
logged for you as a detected replacement. Providers that report words instead
of numbers (Flume high/medium/low, Blink ok/low) count only when the level
climbs from the bottom one: those words wobble across a boundary, and
medium → high is not new batteries.

```bash
./netinv.py battery                                   # every battery device, worst first
./netinv.py battery log "Front Door Lock" --type "4x AA" --note "Energizer lithium"
./netinv.py battery log "Flume Sensor" --event checked --level 60 --date 2026-11-01
./netinv.py battery history "Front Door Lock"         # its log, with entry ids, and levels
./netinv.py battery edit --id 538f40cacb64 --note "..."
./netinv.py battery delete --id 538f40cacb64
```

Events are `replaced`, `charged`, `checked` and `note`. With two or more
changes logged, the list shows how long a set lasts and when the next is
due.

Each battery device is shown as what it is: its `home.csv` name, room,
type and app, plus the model ("Garage Door Lock · Garage · lock · Smart Life
DY QSLOCK"). It's found by the monitor's name, then by the cloud name it is
matched by (`tuya=Garage door`), then by its network row; without a
`home.csv` row, the room comes from the vendor app and the type is guessed
from the name. The page and the app can group the list by room, type or
app. Monitored locks are listed even with no battery reading (a U-Bolt
reports none), so their changes can be logged by hand.

Devices nothing describes (the Flume sensor, a Govee Bluetooth thermometer
known only as `H5101_4670`) get a name, room and type from the pencil next to
their name on the page, or **Edit details** in the app (`POST
/api/battery-device`). That writes their `home.csv` row. Giving one the name
of a device `home.csv` already has links it to that row instead of adding a
second one, which is how you tell netinventory which Bluetooth sensor is the
Govee app's "Display-Humidor". Unticking **Track its battery** removes a
plugged-in device (a Govee hub) from the list. The dashboard's **Batteries** page (`/batteries`) and the app's
**Batteries** tab show the same data, with a form to log changes.
Click (or tap) a device for its history: a chart of its level over time
with battery changes marked, every level reading, and every logged change
(edit or delete them there);
`GET /api/batteries` and `GET /api/battery?device=` serve it.

## Dashboard and API

`./netinv.py serve` serves three pages on `http://127.0.0.1:8787`:

| Page | What it shows |
|---|---|
| `/` | every device: search, group, sort, rename (with `--allow-write`) |
| `/rooms` | a board of devices by room, type or app. Drag a device to move it, filter by room/type/app, and add, rename or delete rooms, types and apps. Writes `home.csv`, `rooms.csv`, `types.csv`, `apps.csv` |
| `/batteries` | battery levels, change log and details (see above) |

| Read (GET) | Write (POST, needs `--allow-write`) |
|---|---|
| `/api/devices`, `/api/summary`, `/api/alerts`, `/api/events` | `/api/name`, `/api/rename`, `/api/delete` |
| `/api/monitors` (live status) | `/api/room`, `/api/room-add`, `/api/room-edit`, `/api/room-rename`, `/api/room-delete` |
| `/api/board` (the rooms board) | `/api/type`, `/api/type-add`, `/api/type-edit`, `/api/type-delete` (and the same for `app`) |
| `/api/batteries`, `/api/battery?device=` | `/api/battery-log`, `/api/battery-edit`, `/api/battery-delete`, `/api/battery-device` |

Writes take JSON (`Content-Type: application/json`), and a browser's `Origin`
must match the host. A request from another device must carry the passcode
in `X-Netinv-Passcode`. No response sends a CORS header. Every response
carries a Content-Security-Policy and `X-Frame-Options: DENY`, so no other
site can frame the pages.

Every write, and every refused one, adds a line to `audit.csv` next to the
data: time, who (this machine, the client's address, or the agent), the
action, its target and the answer. It stays on its own copy and is never
synced; past 1 MB it moves to `audit.csv.1`. Serve logs refused and failed
requests (4xx/5xx) with the client's address, and nothing for successful ones.

## Using the dashboard from your phone or another computer

```bash
./netinv.py set-passcode                    # 8+ characters, stored as a salted hash
./netinv.py serve --lan --allow-write       # prints the address to open
../scan-jobs.sh serve-start                 # or keep it running: starts at login
```

`--lan` serves the dashboard to your whole network. With `--allow-write`,
edits from any device other than this Mac need the passcode. Browsers ask
for it once, and the app takes it in its host dialog. `serve` refuses LAN
writes until a passcode is set. It answers only requests addressed to this
machine (its LAN IP, its name, `localhost`, plus any names in
`serve_hosts`), which blocks DNS-rebinding pages. Ten wrong passcodes in
ten minutes lock that device out of editing for ten minutes. Reading the
inventory needs no passcode on your LAN, so don't use `--lan` on a network
you don't trust. Over plain `http://` the passcode crosses your Wi-Fi in clear
text, so a TLS front (Caddy) is the safer way to share edits.

## Keeping two copies in step (a Raspberry Pi)

**Deploy** this checkout to another machine over ssh (key login):

```bash
../deploy.sh pi@192.168.1.50                       # code + venv + services
../deploy.sh pi@192.168.1.50 --copy-credentials    # also logins, email, passcode, tokens
../deploy.sh pi@192.168.1.50 apps/netinventory --watch-args "--interval 300 --no-router"
```

It copies only committed code (never your data), creates a venv with the
cloud and Bluetooth libraries, and runs `watch` and `serve --lan` as
systemd user services (on a Mac it uses `scan-jobs.sh`). The watch there
doesn't email by default, so alerts don't arrive twice. Run `deploy.sh`
again after pulling new code. On the Pi, first run
`sudo apt install python3-venv nmap net-tools` and
`sudo loginctl enable-linger $USER` (so it keeps running after reboots).
Credentials there live in `~/.netinv/credentials.json` (owner-only).
Home Assistant OS can't run this, so use a Pi with Raspberry Pi OS.

**Sync** the files you edit, either way:

```bash
./netinv.py sync pi@192.168.1.50            # or a path: pi@host:apps/netinventory
./netinv.py sync pi@192.168.1.50 --dry-run
```

| File | How both sides' edits combine |
|---|---|
| `battery_log.csv` | per entry, the newer edit wins; deletions sync; nothing is lost |
| `battery_history.csv` | union |
| `monitors.csv`, `home.csv`, `rooms.csv`, `types.csv`, `apps.csv` | three-way against the last sync: a field edited on one side carries over; edited on both, the side whose file was written last wins |
| `devices.csv` | names and notes only; each copy keeps its own scan data; a sync never deletes a device |

Set `sync_target = pi@192.168.1.50` in `~/.netinv/config` and the watch
syncs every `sync_every` (default 15 minutes). Each copy keeps its own
live levels and monitor state.

**A Pi that serves and a Mac that scans** (scanner mode). The Mac runs the
watch and the clouds; the Pi only runs `serve`, the dashboard you use. With
`--scanner` (or `sync_scanner = yes`), the Mac's scan results, monitor state
and events replace the Pi's, so the Pi shows live data without scanning (or
writing much to its SD card), and edits made on the Pi still merge back:

```ini
# ~/.netinv/config on the Mac
sync_target  = jacobc@192.168.1.204:/opt/homehub/netinventory
sync_key     = ~/.ssh/homehub_ed25519
sync_scanner = yes
```

| Also synced in scanner mode | From the Mac to the Pi |
|---|---|
| `devices.csv` scan columns (IP, Status, LastSeen...) | replace the Pi's; names and notes still merge both ways |
| `monitor_state.json` | battery levels and offline state for the Batteries page |
| `events.csv` | join/leave history |

**Behind a reverse proxy** (the homehub Pi serves it through Caddy at
`/netinv/`): the pages use relative links, so the prefix works. Every
proxied request arrives from loopback, so a request carrying
`X-Forwarded-For` is never treated as this machine's own: edits need the
passcode, are refused outright if none is set, and wrong guesses are counted
per forwarded client. Add the names the proxy answers to (`netinv.local`) to
`serve_hosts`. `deploy.sh` refuses a box that already runs
`netinventory.service` as a system service; homehub updates its copy with
its own `netinventory/update.sh`.

## An editable copy on the web (net.aspl.net), kept in step by the Pi

```
  Mac (scans) ── sync every 15 min ──▶ Pi (serves at home) ── netinv agent ──▶ net.aspl.net
                ◀── edits ────────────                     ◀── edits ───────   (betty, Entra)
```

The whole picture (diagrams, every job on every machine, logs, and where to
look when something is off) is in [../docs/architecture.md](../docs/architecture.md).

net.aspl.net runs `netinv serve --allow-write` on a cloud VM, behind the
Entra ID sign-in, with edits also needing the passcode. It can't reach your
network, and nothing on the internet can reach into it: the Pi's `netinv
agent` makes **one outbound HTTPS connection** and keeps it open (the
pattern poolhound uses for its pool controller).

- The agent holds `GET /api/agent/stream` open. An edit on net.aspl.net
  sends `sync` down it, so the change reaches the Pi within seconds, and
  the Mac at its next sync.
- It also syncs every `agent_every` (5 minutes): the Pi's scan data, levels
  and events go up (scanner mode), and edits merge both ways under the same
  rules as `netinv sync`.
- An idle sync costs one small request. The agent sends fingerprints of what
  it last synced, and the server sends back only files that differ. Bodies
  are gzipped, and nothing is written to the Pi's SD card when nothing
  changed.
- An import is refused (409) if the server's file changed after the export,
  so an edit made on the web mid-sync is never overwritten. The agent just
  syncs again.
- `/api/agent/*` skips Entra and needs a bearer token (`agent-token`),
  compared in constant time, with its own lockout for wrong tokens, counted
  per visitor. Behind a container proxy that needs `NETINV_TRUSTED_PROXIES`
  (`deploy-betty.sh` checks it). Without a token set, those paths don't exist.
- The dashboard on net.aspl.net shows when the Pi last synced, next to the
  device count, so stale data shows.

```bash
# on the server copy, once; then copy only the token across:
#   netinv creds-export --only agent | ssh <server> netinv creds-import
netinv agent-token --generate
# on the Pi: agent_url = https://net.aspl.net in ~/.netinv/config, the same token, then
netinv agent --once            # one sync, to test
netinv agent                   # run it (homehub runs it as netinventory-agent.service)
```

Run `./deploy-betty.sh` to ship this checkout's committed engine to
net.aspl.net, and `./deploy-betty.sh --check` to confirm that every page
and data path redirects to the sign-in while the agent path wants its
token. The script needs `az login` and refuses to ship if the gates aren't
in place. `netinv.py export-site DIR` still writes a static read-only copy
(the three pages and every GET answer, each page marked as a snapshot) for a
plain web server. It holds every MAC, IP and the join/leave history, so publish
it only behind a login.

## Identifying unknown devices (fingerprinting)

Two layers, both built in:

**OUI vendor lookup (passive, automatic).** Every run fills a blank `Vendor`
from the MAC's manufacturer prefix using nmap's offline OUI database — no
scanning, instant. Randomized/private MACs (phones) have no real OUI and stay
blank by design.

**Active fingerprinting (port + service + banner probe).** Identifies device
*type* by scanning a curated set of IoT ports, reading HTTP/TLS banners, and
matching service signatures (e.g. `8060`→Roku, `9999`→Kasa, `62078`→iPhone,
RTSP→camera, IPP→printer):

```bash
./netinv.py fingerprint 192.168.1.50        # one device
./netinv.py fingerprint --apply             # unnamed online devices -> hints in Notes
./netinv.py update --scan --fingerprint     # fold it into a refresh (incremental)
```

`update --fingerprint` probes new joins + still-unnamed online devices (up to
`--fp-limit`, default 10), skipping any already fingerprinted, and writes a
`fp: …` hint to each device's `Notes`. `update.sh` runs it in fast mode by
default (`--no-fingerprint` to skip). Active scans only touch your own LAN.

**Blink cameras** sleep off Wi-Fi, and the Blink cloud gives no MAC.
`blink-locate` wakes each camera in turn, watches which MAC/IP joins, waits for
it to sleep again, then moves on to the next. `--apply` writes the Blink names
into `devices.csv` by MAC. `--via user@pi` runs the ARP sweep on another box
(needs nmap and passwordless sudo there) when this Mac can't see the joins.

## Rooms, types and one name per device

[`docs/home-standard.md`](../docs/home-standard.md) is the standard:
`rooms.csv` lists the rooms, and `home.csv` gives every device a room, a type
and the name it should have in each smart-home app.

```bash
./netinv.py home rooms --from tuya     # import an app's rooms into rooms.csv
./netinv.py home draft                 # add unclassified devices to home.csv
./netinv.py home check                 # validate rooms.csv + home.csv
./netinv.py home push --to tuya        # rename devices in the app to match
```

The dashboard's `/rooms` board edits the same files.

## Status / staleness

A device confirmed by the gateway (or seen on the wire when the gateway isn't used)
is `online`; one the gateway lists but isn't currently connected is `offline`; one
only ever seen in an imported export and never confirmed live ages to `defunct`.

## Credentials (macOS Keychain)

On a Mac, secrets live in the macOS Keychain and are never written to a
file. On Linux (a Pi), they go to `~/.netinv/credentials.json`, owner-only (or
`$NETINV_CREDENTIALS`). `deploy.sh --copy-credentials` moves them through
`creds-export | creds-import` on a pipe, never through a file. For a copy
that scans nothing (the web copy), send only what it uses:
`creds-export --only agent,serve`. The edit passcode is stored as a salted
scrypt hash; a plain one from an older version is converted when serve starts.

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
| **govee** | API key (Govee Home app → Profile → Settings → Apply for API Key) | app name + model; MAC + IP with LAN Control on | direct² / reference |
| **kasa** | none: local protocol on port 9999, always on | app name + MAC + IP | direct |
| **flume** | API client id + secret (portal.flumewater.com) + login | bridge/sensor + sensor battery | reference |
| **govee-app** | Govee account email + password (`govee-login` for the emailed code) | sensor battery + online (monitors only) | — |

² Govee devices answer a LAN scan (UDP 4001/4002) only with **LAN Control** switched
on per device in the Govee app; others are matched by a MAC guessed from the device id,
or listed as a reference. Bluetooth-only models are reference only.

¹ Apple exposes no MAC/IP and Wi-Fi MACs are randomized, so the roster is a reference
only. Map each once by reading the device's Wi-Fi Address (iOS: Settings → Wi-Fi → ⓘ);
the private MAC is stable per network, so the label sticks.

## Configuration

Settings resolve **env var → config file → default**. The config file is an INI
at `~/.netinv/config` (or `$NETINV_CONFIG`) with a `[netinv]` section — see
[`config.example`](config.example). The LAN filter is derived from `subnet`, so
non-`192.168.1.x` networks work by setting it.

| Variable | Config key | Default | Purpose |
|---|---|---|---|
| `NETINV_ROUTER` | `router_host` | `192.168.1.1` | Gateway host |
| `NETINV_SUBNET` | `subnet` | `192.168.1.0/24` | Subnet to scan (drives LAN filter) |
| `NETINV_STALE_DAYS` | `stale_days` | `30` | Days unseen before a device is `defunct` |
| `NETINV_ROUTER_COOLDOWN_MIN` | `router_cooldown_min` | `30` | Lockout back-off minutes |
| `NETINV_HOME_SSIDS` | `home_ssids` | — | Comma-separated home SSIDs for `wifi-scan` |
| `NETINV_MONITOR_CLOUD_EVERY` | `monitor_cloud_every` | `1h` | How often monitors ask the clouds for batteries |
| `NETINV_HEARTBEAT_URL` | `heartbeat_url` | — | Dead-man URL fetched after each watch poll / monitor check |
| `NETINV_SYNC_TARGET` | `sync_target` | — | Copy the watch syncs with (`user@host[:path]` or a directory) |
| `NETINV_SYNC_EVERY` | `sync_every` | `15m` | How often the watch syncs |
| `NETINV_SYNC_KEY` | `sync_key` | — | ssh identity file for the sync target |
| `NETINV_SYNC_SCANNER` | `sync_scanner` | `no` | This copy scans: its scan data, monitor state and events replace the target's |
| `NETINV_AGENT_URL` | `agent_url` | — | Server copy `netinv agent` keeps in step (outbound HTTPS) |
| `NETINV_AGENT_EVERY` | `agent_every` | `5m` | How often the agent syncs regardless of nudges |
| `NETINV_SERVE_HOSTS` | `serve_hosts` | — | Extra names `serve` answers to (e.g. `mac-studio.lan`), comma-separated |
| `NETINV_CREDENTIALS` | — | `~/.netinv/credentials.json` | Credential file where there's no Keychain (Linux) |

## Tests

```bash
python -m unittest discover -s tests   # ~280 tests, no network
```

Use the venv's Python (`.venv/bin/python`) so the connector tests can import
their SDKs.

## Requirements

- Python 3.8+ (standard library only for the core)
- `nmap` for the ping-sweep (`brew install nmap`, `apt install nmap`)
- macOS (Keychain, notifications, Mail.app) or Linux (a Raspberry Pi, with the
  owner-only credentials file)
- Optional: `xlsxwriter` for the workbook, `bleak` for Bluetooth sensors, and
  the per-connector SDKs in `requirements-cloud.txt` (`pip install -e '.[all]'`
  installs everything)

## Privacy

`devices.csv`, the reports, `inventory.xlsx`, `wifi_scan.csv`, `home_ssids.txt`,
`events.csv`, `home.csv`, `rooms.csv`, `app_rooms.csv`, `monitors.csv`,
`monitor_state.json`, `battery_log.csv`, `battery_history.csv`, `audit.csv`
and the `.sync/` bases all contain real network details and are git-ignored. Credentials are
stored in the macOS Keychain or the owner-only credentials file. Nothing
identifying your network is committed to the repo.

## License

MIT
