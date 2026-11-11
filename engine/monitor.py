"""Device monitors: alert when a chosen device stays offline too long or its
battery runs low.

monitors.csv (semicolon CSV next to devices.csv) lists the watched devices,
one row each; edit it by hand or with `netinv monitor on|off`:

    Device        the name in devices.csv / home.csv, or a cloud app's name
    Enabled       yes / no: the on-off switch
    OfflineAfter  30m, 2h, 1d...: alert once it has been gone this long
                  (blank = never alert on offline)
    BatteryBelow  percent: alert once a cloud reports this or less
                  (blank = never alert on battery)
    Check         how presence is verified, cheapest first:
                    ping      one ICMP echo to its last IP (default with a MAC)
                    tcp:PORT  one TCP connect, so the service is up, not just the box
                    cloud     the vendor cloud's online flag (sleepy battery
                              devices that drop off Wi-Fi between check-ins;
                              the default with no MAC)
                    ble       heard in this Mac's passive Bluetooth scan
                              (Govee thermometers, tags: no network at all)
                    none      battery only
    Match         optional 'provider=name' for a cloud reading whose name
                  differs from Device ('flume=Flume Sensor'); auto-matched
                  by name, hostname, AppNames or MAC otherwise
    Notes         free text

State (when each device was last confirmed, what was alerted) lives in
monitor_state.json. Each alert fires once when a threshold is crossed and
once more on recovery, never every poll. Standard library only."""
import csv
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys

FIELDS = ["Device", "Enabled", "OfflineAfter", "BatteryBelow", "Check", "Match", "Notes"]
CHECKS = ("ping", "cloud", "ble", "none")  # plus tcp:PORT
# Battery labels some clouds report instead of a percent.
LEVELS = {"empty": 0, "critical": 5, "low": 10, "middle": 50, "medium": 50,
          "mid": 50, "ok": 100, "normal": 100, "high": 100, "full": 100}
RECOVER_MARGIN = 10      # battery must rise this far above the threshold to re-arm
ON = {"yes", "y", "on", "true", "1"}


# --------------------------------------------------------------------------- #
# monitors.csv
# --------------------------------------------------------------------------- #
def load(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "").strip() for k in FIELDS}
                for r in csv.DictReader(f, delimiter=";") if (r.get("Device") or "").strip()]


def save(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";")
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in FIELDS} for r in rows)


def enabled(m):
    return m["Enabled"].lower() in ON


def parse_duration(s):
    """'90s' / '30m' / '2h' / '1d' / '1w' (a bare number is minutes) -> seconds;
    blank or unreadable -> None."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\w*\s*", (s or "").lower())
    if not m:
        return None
    mult = {"s": 1, "": 60, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return int(float(m.group(1)) * mult)


def fmt_duration(secs):
    secs = int(secs)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}" + (f" {secs % n // 3600}h" if unit == "d" and secs % n >= 3600 else "")
    return f"{secs}s"


def battery_below(m):
    v = m["BatteryBelow"].rstrip("%").strip()
    return int(v) if v.isdigit() else None


def validate(m):
    """Problems with one monitors.csv row, as strings."""
    out = []
    if m["OfflineAfter"] and parse_duration(m["OfflineAfter"]) is None:
        out.append(f"OfflineAfter '{m['OfflineAfter']}' is not a duration (30m, 2h, 1d)")
    if m["BatteryBelow"] and battery_below(m) is None:
        out.append(f"BatteryBelow '{m['BatteryBelow']}' is not a percent")
    c = m["Check"].lower()
    if c and c not in CHECKS and not re.fullmatch(r"tcp:\d{1,5}", c):
        out.append(f"Check '{m['Check']}' is not ping, tcp:PORT, cloud, ble or none")
    if m["Match"] and "=" not in m["Match"]:
        out.append(f"Match '{m['Match']}' should be provider=name")
    return out


# --------------------------------------------------------------------------- #
# matching a monitor to inventory rows and cloud readings
# --------------------------------------------------------------------------- #
def norm(s):
    """Case- and quote-insensitive key: "Jacob’s Mac" == "jacob's mac"."""
    return " ".join(re.sub(r"[’‘`´]", "'", s or "").lower().split())


def _app_names(s):
    out = {}
    for part in (s or "").split(";"):
        p, _, n = part.partition("=")
        if p.strip() and n.strip():
            out[p.strip()] = n.strip()
    return out


def rows_for(m, master):
    """Inventory rows (one per MAC) that belong to monitor m: by Device name,
    else by hostname, else by a MAC typed as the Device."""
    key = norm(m["Device"])
    rows = [r for r in master.values() if norm(r["Device"]) == key]
    if not rows:
        rows = [r for r in master.values() if norm(r["Hostname"]) == key]
    if not rows and key.upper() in master:
        rows = [master[key.upper()]]
    return rows


def names_for(m, rows, home_rows=()):
    """{provider or '': {normalized names}} this monitor answers to in clouds."""
    names = {"": {norm(m["Device"])}}
    for r in rows:
        if r.get("Hostname"):
            names[""].add(norm(r["Hostname"]))
        for p, n in _app_names(r.get("AppNames", "")).items():
            names.setdefault(p, set()).add(norm(n))
    for h in home_rows:
        if norm(h.get("Device")) == norm(m["Device"]):
            for p, n in _app_names(h.get("AppNames", "")).items():
                names.setdefault(p, set()).add(norm(n))
    return names


def match_reading(m, rows, readings, home_rows=()):
    """The cloud reading for monitor m, or None. An explicit Match wins; then
    a MAC; then the provider's own name for the device; then any name."""
    if m["Match"]:
        p, _, n = m["Match"].partition("=")
        for rd in readings:
            if rd["provider"] == p.strip() and (n.strip() == "*" or norm(rd["name"]) == norm(n)):
                return rd
        return None
    macs = {r["MAC"] for r in rows}
    for rd in readings:
        if rd.get("mac") and rd["mac"] in macs:
            return rd
    names = names_for(m, rows, home_rows)
    for rd in readings:
        # Govee devices advertise over Bluetooth under the Govee app's name.
        own = names.get(rd["provider"], set()) | (names.get("govee", set())
                                                  if rd["provider"] == "ble" else set())
        if norm(rd["name"]) in own:
            return rd
    for rd in readings:
        # Never by a bare name for Bluetooth: it would trust any broadcaster.
        if rd["provider"] != "ble" and norm(rd["name"]) in names[""]:
            return rd
    return None


def check_of(m, rows):
    """Effective Check: the column, else ping with a MAC and cloud without."""
    c = m["Check"].lower()
    return c or ("ping" if rows else "cloud")


# --------------------------------------------------------------------------- #
# low-bandwidth probes
# --------------------------------------------------------------------------- #
def ping(ip, wait=2):
    """One ICMP echo: True if it answers."""
    flag = "-t" if sys.platform == "darwin" else "-W"
    try:
        return subprocess.run(["ping", "-c", "1", flag, str(wait), ip],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=wait + 3).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def tcp(ip, port, wait=2):
    """One TCP connect: True only if the port accepts (the service is up)."""
    try:
        with socket.create_connection((ip, port), timeout=wait):
            return True
    except OSError:
        return False


# Tried one at a time when a ping goes unanswered: SMB (Macs, NASes), SSH,
# HTTP(S), and Apple's lockdown port (iPhones, iPads).
FALLBACK_PORTS = (445, 22, 80, 443, 62078)


def answers_tcp(ip, ports=FALLBACK_PORTS, wait=1):
    """True if the host accepts or actively refuses a connect on any port:
    a refusal (TCP reset) proves the host is up as well as an accept does.
    Hosts that drop ICMP (macOS stealth mode, some IoT) still do this."""
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=wait):
                return True
        except ConnectionRefusedError:
            return True
        except OSError:
            continue
    return False


def probe(check, ips):
    """Verify presence with the fewest packets: stop at the first IP that
    answers. Returns True/False, or None when the check sends nothing."""
    if check in ("cloud", "none") or not ips:
        return None
    for ip in ips:
        if check.startswith("tcp:"):
            if tcp(ip, int(check[4:])):
                return True
        elif ping(ip) or answers_tcp(ip):
            return True
    return False


def candidate_ips(rows, arp=None):
    """IPs to try for a device: what the ARP cache maps its MACs to now (it
    may have moved under DHCP), then its last known IPs."""
    arp = arp or {}
    macs = {r["MAC"] for r in rows}
    ips = [ip for ip, mac in arp.items() if mac in macs]
    ips += [r["IP"] for r in rows if r.get("IP")]
    return list(dict.fromkeys(ips))


# --------------------------------------------------------------------------- #
# state + evaluation  (pure: no I/O)
# --------------------------------------------------------------------------- #
def load_state(path):
    try:
        with open(path) as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _iso(t):
    return t.isoformat(timespec="seconds")


def _parse(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def battery_pct(value):
    """A battery reading -> percent 0..100, or None. Numbers pass through
    (clamped); labels like 'low' / 'middle' / 'high' map via LEVELS."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(value))))
    s = str(value).strip().lower().rstrip("%")
    try:
        return battery_pct(float(s))
    except ValueError:
        return LEVELS.get(s)


def evaluate(m, st, present, reading, now):
    """Advance one monitor's state; return alerts as [(kind, message)].
    present: True / False / None (None = not checked this run, no change).
    reading: this run's cloud reading for it, or None.
    kind: offline | online | battery-low | battery-ok."""
    alerts = []
    limit = parse_duration(m["OfflineAfter"])
    if present is not None:
        st["up"] = present
    if present is True:
        if st.get("offline_alerted"):
            since = _parse(st.get("last_ok"))
            gone = f" after {fmt_duration((now - since).total_seconds())}" if since else ""
            alerts.append(("online", f"back online{gone}"))
        st["last_ok"] = _iso(now)
        st.pop("missing_since", None)
        st["offline_alerted"] = False
    elif present is False:
        # Never confirmed yet (first run, or it was already gone): count from now.
        since = _parse(st.get("last_ok")) or _parse(st.get("missing_since"))
        if since is None:
            st["missing_since"] = _iso(now)
            since = now
        gone = (now - since).total_seconds()
        if limit is not None and gone >= limit and not st.get("offline_alerted"):
            seen = f"last seen {st['last_ok'].replace('T', ' ')}" if st.get("last_ok") \
                else "not seen since monitoring began"
            alerts.append(("offline", f"offline for {fmt_duration(gone)} ({seen})"))
            st["offline_alerted"] = True

    pct = reading.get("pct") if reading else None
    if pct is not None:
        st["battery"] = {"pct": pct, "label": str(reading.get("level", "")),
                         "provider": reading["provider"], "at": _iso(now)}
        below = battery_below(m)
        if below is not None:
            src = f"{reading['provider']}: {reading.get('level')}" \
                if str(reading.get("level")) != str(pct) else reading["provider"]
            if pct <= below and not st.get("battery_alerted"):
                alerts.append(("battery-low", f"battery {pct}% ({src}), at or below {below}%"))
                st["battery_alerted"] = True
            elif pct > below + RECOVER_MARGIN and st.get("battery_alerted"):
                alerts.append(("battery-ok", f"battery back to {pct}% (replaced or charged)"))
                st["battery_alerted"] = False
    return alerts


def status_line(st, now):
    """Short human state for `monitor list` / the API."""
    bits = []
    if st.get("up") is False:
        since = _parse(st.get("last_ok")) or _parse(st.get("missing_since"))
        bits.append(f"offline {fmt_duration((now - since).total_seconds())}" if since else "offline")
    elif st.get("up"):
        bits.append("online")
    b = st.get("battery")
    if b:
        bits.append(f"battery {b['pct']}%" + (f" ({b['label']})" if b.get("label")
                                              and b["label"] != str(b["pct"]) else ""))
    return ", ".join(bits) or "not checked yet"


# --------------------------------------------------------------------------- #
# one pass
# --------------------------------------------------------------------------- #
def run(monitors, master, state, readings=None, *, home_rows=(), now=None,
        seen_online=None, arp=None, probe_fn=probe):
    """Check every enabled monitor once; returns [(device, kind, message)].

    seen_online: MACs a sweep (the watch) just saw. A device with one counts
    as present without sending anything; one without is probed only when its
    Check is tcp:PORT (a sweep proves the box, not the service), since the
    sweep already pinged it. Without a sweep (`monitor check`), ping/tcp
    devices get one probe each. readings: cloud battery/online readings, or
    None when the clouds were not asked this run."""
    from concurrent.futures import ThreadPoolExecutor
    now = now or dt.datetime.now().replace(microsecond=0)
    plan = []                      # (monitor, reading, present or a probe future)
    with ThreadPoolExecutor(max_workers=16) as pool:
        for m in monitors:
            if not enabled(m):
                continue
            rows = rows_for(m, master)
            check = check_of(m, rows)
            # Bluetooth names are unauthenticated (anything nearby can broadcast
            # 'Front Door'), so ble readings serve ble monitors only, and only
            # by an explicit Match or the Govee app's name (see match_reading).
            # A ble monitor ignores the clouds: HA's copy may be stale.
            pool_rd = [rd for rd in readings or [] if (rd["provider"] == "ble") == (check == "ble")]
            reading = match_reading(m, rows, pool_rd, home_rows) if pool_rd else None
            present = None
            if check == "ble":
                # Readings exist only when the scan ran and heard something,
                # so silence means gone; no readings means it wasn't asked.
                present = (reading is not None) if pool_rd else None
            elif check == "cloud":
                if reading is not None and reading.get("online") is not None:
                    present = bool(reading["online"])
            elif check != "none":
                if seen_online is not None and not check.startswith("tcp:"):
                    present = bool({r["MAC"] for r in rows} & seen_online)
                else:              # probes run side by side: same packets, less waiting
                    present = pool.submit(probe_fn, check, candidate_ips(rows, arp))
            plan.append((m, reading, present))
    out = []
    for m, reading, present in plan:
        if hasattr(present, "result"):
            present = present.result()
        st = state.setdefault(m["Device"], {})
        st["checked"] = _iso(now)
        out += [(m["Device"], k, msg) for k, msg in evaluate(m, st, present, reading, now)]
    return out


# --------------------------------------------------------------------------- #
# suggestions
# --------------------------------------------------------------------------- #
# Always-on boxes worth an offline alert, by name/vendor/hostname -> threshold.
ALWAYS_ON = [
    (re.compile(r"\b(qnap|synology|nas|truenas|unraid)\b", re.I), "30m", "NAS"),
    (re.compile(r"mac ?(studio|mini|pro)\b|imac", re.I), "2h", "desktop Mac"),
    (re.compile(r"\block\b|u-bolt|ultraloq|yale|schlage|august|kwikset", re.I), "2h", "lock"),
    (re.compile(r"sync.module|\bhub\b|blink-sync", re.I), "1h", "hub"),
    (re.compile(r"flume", re.I), "2h", "water monitor"),
    (re.compile(r"raspberry|home ?assistant|\bpi\b|router|gateway|access point|mesh", re.I),
     "30m", "infrastructure"),
    # Plugged-in cameras stay on Wi-Fi (battery ones are caught by their cloud first).
    (re.compile(r"camera|\bcam\b|blink.mini|doorbell|\bdvr\b|\bnvr\b", re.I), "2h", "camera"),
]
DEFAULT_BATTERY = 20


def suggest(master, readings, existing, home_rows=(), self_macs=()):
    """New monitors.csv rows: always-on devices (NAS, desktop Macs, locks,
    hubs) for offline alerts, and every device a cloud reports a battery for.
    Never repeats a Device already in `existing`; skips this machine (it
    cannot report itself offline). Returns [(row, reason)]."""
    have = {norm(m["Device"]) for m in existing}
    out = []
    devices = {}
    for r in master.values():
        if r["Device"]:
            devices.setdefault(norm(r["Device"]), []).append(r)
    readings = readings or []
    # Readings an existing monitor already answers to are taken, so a re-run
    # doesn't add 'Garbage Door' beside 'Outside Garbage Door Camera'.
    used = {id(rd) for rd in (match_reading(m, rows_for(m, master), readings, home_rows)
                              for m in existing) if rd is not None}

    def add(row, why):
        have.add(norm(row["Device"]))
        out.append((row, why))

    for key, rows in sorted(devices.items()):
        if key in have or any(r["MAC"] in self_macs for r in rows):
            continue
        name = rows[0]["Device"]
        hay = " ".join([name] + [r["Hostname"] + " " + r["Vendor"] for r in rows])
        if re.search(r"macbook|\btv\b|television", hay, re.I):
            continue                      # laptops roam, TVs get switched off: not "offline"
        m = {"Device": name, "Enabled": "yes", "OfflineAfter": "", "BatteryBelow": "",
             "Check": "", "Match": "", "Notes": ""}
        rd = match_reading(m, rows, readings, home_rows)
        if rd and rd.get("pct") is not None:
            # A battery device sleeps off Wi-Fi between check-ins (so it can
            # look defunct here); only its cloud knows whether it is gone.
            used.add(id(rd))
            m.update(BatteryBelow=str(DEFAULT_BATTERY), Check="cloud", OfflineAfter="1d")
            add(m, f"{rd['provider']} battery {rd['pct']}%")
            continue
        if all(r["Status"] == "defunct" for r in rows):
            continue
        hit = next(((after, lb) for pat, after, lb in ALWAYS_ON if pat.search(hay)), None)
        if not hit:
            continue
        m["OfflineAfter"], label = hit
        if not any(r["Status"] == "online" for r in rows):
            # Already gone: an alert on day one would be old news. Off until
            # you've looked at it.
            m["Enabled"], m["Notes"] = "no", "offline when suggested"
        add(m, label)
    # Battery devices with no network row (Zigbee/BLE behind a hub, the
    # Flume sensor): monitored by their cloud name alone. Bluetooth ones by
    # being heard: Govee sensors (named like 'H5074_0447') even without a
    # battery in their advertisement; never tags, phones or cars.
    for rd in readings:
        ble = rd["provider"] == "ble"
        if id(rd) in used or norm(rd["name"]) in have:
            continue
        if rd.get("pct") is None and not (ble and re.match(r"H\d{4}", rd["name"])):
            continue
        has_batt = rd.get("pct") is not None
        # Not every Govee broadcast carries the battery, so a scan may miss it;
        # set the threshold anyway (a model that never reports just never fires).
        add({"Device": rd["name"], "Enabled": "yes", "OfflineAfter": "1d",
             "BatteryBelow": str(DEFAULT_BATTERY) if has_batt or ble else "",
             "Check": "ble" if ble else "cloud",
             "Match": f"{rd['provider']}={rd['name']}", "Notes": rd.get("model", "")},
            (f"{rd['provider']} battery {rd['pct']}%" if has_batt else "heard over bluetooth")
            + (" (bluetooth)" if ble and has_batt else "" if ble else " (cloud only)"))
    return out
