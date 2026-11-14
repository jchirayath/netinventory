#!/usr/bin/env python3
"""Write a fictional demo home into a data directory, for screenshots and
trying the dashboard without a real network.

    python3 engine/tools/demo_data.py <dir>
    NETINV_HOME=<dir> NETINV_CREDENTIALS=<dir>/credentials.json \\
        python3 engine/netinv.py serve --port 8799

Everything here is made up: vendor and app names are placeholders, MACs come
from the RFC 7042 documentation block (00:00:5E:00:53:00-FF) and IPs from
RFC 5737 (192.0.2.0/24). Dates are relative to today; the output is otherwise
the same on every run (fixed random seed). Standard library only."""
import csv
import datetime as dt
import io
import json
import os
import random
import sys

SEED = 7042

DEVICE_FIELDS = ["Device", "MAC", "IP", "Vendor", "Hostname", "Link", "Status",
                 "FirstSeen", "LastSeen", "Source", "Notes", "RSSI", "AppNames"]
HOME_FIELDS = ["Device", "Room", "Type", "Name", "Description", "Native", "Expose", "Via",
               "AppNames"]
ROOM_FIELDS = ["Room", "Floor", "Aliases"]
APP_FIELDS = ["Key", "Label", "Portal", "Match"]
MONITOR_FIELDS = ["Device", "Enabled", "OfflineAfter", "BatteryBelow", "Check", "Match", "Notes"]
LOG_FIELDS = ["Id", "Date", "Device", "Event", "Battery", "Level", "Notes",
              "Source", "Updated", "Deleted"]
HISTORY_FIELDS = ["Time", "Device", "Provider", "Level", "Pct"]
EVENT_FIELDS = ["Timestamp", "Event", "MAC", "IP", "Vendor", "Hostname", "Band", "Device",
                "Source"]

ROOMS = [
    ("Living Room", "Downstairs", "Lounge"),
    ("Kitchen", "Downstairs", ""),
    ("Family Room", "Downstairs", "Den"),
    ("Office", "Downstairs", "Study"),
    ("Primary Bedroom", "Upstairs", "Main Bedroom"),
    ("Hallway", "Upstairs", "Landing"),
    ("Basement", "Basement", "Utility"),
    ("Garage", "Downstairs", ""),
    ("Front Porch", "Outside", "Entry"),
]

# Fictional apps, so the board's app columns carry no real brand.
APPS = [
    ("acme", "Acme Home", "local", r"\bacme\b"),
    ("contoso", "Contoso Connect", "local", r"contoso"),
    ("fabrikam", "Fabrikam Cam", "local", r"fabrikam"),
    ("northwind", "Northwind Climate", "local", r"northwind"),
    ("tailspin", "Tailspin Audio", "local", r"tailspin"),
    ("example", "Example Hub", "local", r"example hub"),
]

# (Device, Room, Type, Vendor, Hostname, Link, Status, Native, Description)
# Link '' + Status '' means no network presence of its own (via a hub).
DEVICES = [
    ("Home Router", "Basement", "network", "Example Networks", "gateway", "Ethernet",
     "online", "", "Example Networks router, fiber uplink"),
    ("Upstairs Access Point", "Hallway", "network", "Example Networks", "ap-upstairs",
     "Ethernet", "online", "", "Example Networks mesh access point"),
    ("Backyard Access Point", "Garage", "network", "Example Networks", "ap-backyard", "Ethernet",
     "online", "", "Example Networks outdoor access point"),
    ("Living Room TV", "Living Room", "tv", "Contoso Electronics", "living-room-tv",
     "Ethernet", "online", "contoso", "Contoso 65-inch TV"),
    ("Living Room Speaker", "Living Room", "speaker", "Tailspin Audio", "tailspin-lr",
     "Wi-Fi 5 GHz", "online", "tailspin", "Tailspin soundbar"),
    ("Family Room TV", "Family Room", "tv", "Contoso Electronics", "family-tv", "Wi-Fi 5 GHz",
     "online", "contoso", "Contoso 50-inch TV"),
    ("Family Room Lamp", "Family Room", "plug", "Acme Devices", "acme-plug-1",
     "Wi-Fi 2.4 GHz", "online", "acme", "Acme smart plug, floor lamp"),
    ("Kitchen Ceiling Light", "Kitchen", "switch", "Acme Devices", "acme-switch-1",
     "Wi-Fi 2.4 GHz", "online", "acme", "Acme wall switch, 4 recessed cans"),
    ("Kitchen Speaker", "Kitchen", "speaker", "Tailspin Audio", "tailspin-kitchen",
     "Wi-Fi 5 GHz", "online", "tailspin", "Tailspin mini speaker"),
    ("Coffee Maker Plug", "Kitchen", "plug", "Acme Devices", "acme-plug-2", "Wi-Fi 2.4 GHz",
     "offline", "acme", "Acme smart plug"),
    ("Office Desktop", "Office", "", "Generic PC Co", "office-desktop", "Ethernet", "online",
     "", "Desktop computer"),
    ("Office Printer", "Office", "", "Example Print", "PRINTER01", "Wi-Fi 2.4 GHz", "online",
     "", "Laser printer"),
    ("Office Desk Lamp", "Office", "plug", "Acme Devices", "acme-plug-3", "Wi-Fi 2.4 GHz",
     "online", "acme", "Acme smart plug"),
    ("Work Laptop", "Office", "", "Generic PC Co", "laptop-alex", "Wi-Fi 5 GHz", "online",
     "", "Laptop"),
    ("Alex Phone", "", "", "Generic Handsets", "phone-alex", "Wi-Fi 5 GHz", "online", "",
     "Phone"),
    ("Sam Phone", "", "", "Generic Handsets", "phone-sam", "Wi-Fi 5 GHz", "offline", "", "Phone"),
    ("Family Tablet", "Family Room", "", "Generic Handsets", "tablet", "Wi-Fi 5 GHz", "online",
     "", "Tablet"),
    ("Bedroom TV", "Primary Bedroom", "tv", "Contoso Electronics", "bedroom-tv",
     "Wi-Fi 5 GHz", "online", "contoso", "Contoso 43-inch TV"),
    ("Bedroom Fan", "Primary Bedroom", "fan", "Acme Devices", "acme-fan-1", "Wi-Fi 2.4 GHz",
     "online", "acme", "Acme ceiling fan controller"),
    ("Bedside Lamp", "Primary Bedroom", "plug", "Acme Devices", "acme-plug-4",
     "Wi-Fi 2.4 GHz", "online", "acme", "Acme smart plug"),
    ("Hallway Night Light", "Hallway", "light", "Acme Devices", "acme-bulb-1",
     "Wi-Fi 2.4 GHz", "online", "acme", "Acme color bulb"),
    ("Bedroom Speaker", "Primary Bedroom", "speaker", "Tailspin Audio", "tailspin-bedroom",
     "Wi-Fi 2.4 GHz", "online", "tailspin", "Tailspin mini speaker"),
    ("Hallway Thermostat", "Hallway", "thermostat", "Northwind Climate", "northwind-tstat",
     "Wi-Fi 2.4 GHz", "online", "northwind", "Northwind smart thermostat, upstairs zone"),
    ("Zigbee Hub", "Basement", "hub", "Example Hub Co", "example-hub", "Ethernet", "online",
     "example", "Example Zigbee and Z-Wave hub"),
    ("Basement Dehumidifier", "Basement", "appliance", "Acme Devices", "acme-dehumid",
     "Wi-Fi 2.4 GHz", "online", "acme", "Acme dehumidifier"),
    ("Garage Door", "Garage", "garage_door", "Acme Devices", "acme-garage", "Wi-Fi 2.4 GHz",
     "online", "acme", "Acme garage door opener"),
    ("Garage Camera", "Garage", "camera", "Fabrikam Security", "fabrikam-garage",
     "Wi-Fi 2.4 GHz", "online", "fabrikam", "Fabrikam battery camera"),
    ("Front Porch Camera", "Front Porch", "camera", "Fabrikam Security", "fabrikam-porch",
     "Wi-Fi 2.4 GHz", "online", "fabrikam", "Fabrikam rechargeable camera"),
    ("Doorbell", "Front Porch", "doorbell", "Fabrikam Security", "fabrikam-doorbell",
     "Wi-Fi 2.4 GHz", "offline", "fabrikam", "Fabrikam video doorbell"),
    ("Driveway Light", "Front Porch", "switch", "Acme Devices", "acme-switch-2",
     "Wi-Fi 2.4 GHz", "online", "acme", "Acme outdoor switch"),
    # Hub-paired, no network presence of their own
    ("Front Door Lock", "Front Porch", "lock", "", "", "", "", "example",
     "Example deadbolt, 4x AA"),
    ("Back Door Lock", "Kitchen", "lock", "", "", "", "", "example", "Example deadbolt, 4x AA"),
    ("Basement Leak Sensor", "Basement", "sensor", "", "", "", "", "example",
     "Example water leak sensor, by the water heater"),
    ("Laundry Leak Sensor", "Basement", "sensor", "", "", "", "", "example",
     "Example water leak sensor, behind the washer"),
    ("Hallway Motion Sensor", "Hallway", "sensor", "", "", "", "", "example",
     "Example motion sensor"),
]

# Second NIC: the TV is on Ethernet and Wi-Fi.
EXTRA_NIC = {"Living Room TV": "Wi-Fi 5 GHz"}
UNNAMED = [("Generic IoT", "wlan0-3f2a", "Wi-Fi 2.4 GHz")]

# Battery devices: (name, provider, battery type, start pct, daily drain, replaced day ago)
BATTERIES = [
    ("Garage Camera", "fabrikam", "2x AA lithium", 38, 0.8, 22),
    ("Front Porch Camera", "fabrikam", "built-in Li-ion", 70, 0.9, None),
    ("Front Door Lock", "example", "4x AA", 74, 0.35, None),
    ("Back Door Lock", "example", "4x AA", 58, 0.3, None),
    ("Basement Leak Sensor", "example", "CR2032", 91, 0.12, None),
    ("Laundry Leak Sensor", "example", "CR2032", 19, 0.12, None),
]


def mac(i):
    return f"00:00:5E:00:53:{i:02X}"


def write_csv(path, fields, rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, delimiter=";", lineterminator="\r\n")
    w.writeheader()
    w.writerows({k: r.get(k, "") for k in fields} for r in rows)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(out.getvalue())


def iso(t):
    return t.replace(microsecond=0).isoformat()


def build(out):
    rnd = random.Random(SEED)
    now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)  # same output all hour
    today = now.date()
    day = lambda n: (today - dt.timedelta(days=n)).isoformat()  # noqa: E731
    os.makedirs(out, exist_ok=True)

    # devices.csv + home.csv
    devices, home, ips, n = [], [], {}, 0
    for name, room, typ, vendor, host, link, status, native, desc in DEVICES:
        if link:
            links = [link] + ([EXTRA_NIC[name]] if name in EXTRA_NIC else [])
            for lk in links:
                n += 1
                ip = "192.0.2.1" if name == "Home Router" else f"192.0.2.{10 + n * 3}"
                ips.setdefault(name, ip)
                wifi = "GHz" in lk
                seen = day(0) if status == "online" else day(rnd.randint(2, 9))
                devices.append({
                    "Device": name, "MAC": mac(n), "IP": ip, "Vendor": vendor,
                    "Hostname": host, "Link": lk, "Status": status,
                    "FirstSeen": day(rnd.randint(60, 400)), "LastSeen": seen,
                    "Source": "router+scan" if status == "online" else "router",
                    "Notes": "", "RSSI": str(rnd.randint(-78, -45)) if wifi else "",
                    "AppNames": f"{native}={name}" if native else ""})
        if room or typ:
            home.append({"Device": name if link else "", "Room": room, "Type": typ,
                         "Name": name, "Description": desc, "Native": native,
                         "Via": "" if link else "Zigbee Hub",
                         "AppNames": "" if link else f"{native}={name}"})
    for vendor, host, lk in UNNAMED:
        n += 1
        devices.append({"Device": "", "MAC": mac(n), "IP": f"192.0.2.{10 + n * 3}",
                        "Vendor": vendor, "Hostname": host, "Link": lk, "Status": "online",
                        "FirstSeen": day(1), "LastSeen": day(0), "Source": "router+scan",
                        "RSSI": "-71", "Notes": "unnamed: fill in the Device column"})
    write_csv(os.path.join(out, "devices.csv"), DEVICE_FIELDS, devices)
    write_csv(os.path.join(out, "home.csv"), HOME_FIELDS, home)
    write_csv(os.path.join(out, "rooms.csv"), ROOM_FIELDS,
              [dict(zip(ROOM_FIELDS, r)) for r in ROOMS])
    write_csv(os.path.join(out, "apps.csv"), APP_FIELDS,
              [dict(zip(APP_FIELDS, a)) for a in APPS])

    # monitors.csv
    monitors = [
        {"Device": "Home Router", "Enabled": "yes", "OfflineAfter": "10m", "Check": "ping"},
        {"Device": "Zigbee Hub", "Enabled": "yes", "OfflineAfter": "30m", "Check": "ping"},
        {"Device": "Living Room TV", "Enabled": "no", "OfflineAfter": "1d", "Check": "ping"},
        {"Device": "Doorbell", "Enabled": "yes", "OfflineAfter": "2h", "Check": "cloud"},
    ]
    for name, prov, *_ in BATTERIES:
        lan = name in ips
        monitors.append({"Device": name, "Enabled": "yes", "OfflineAfter": "6h" if lan else "",
                         "BatteryBelow": "20", "Check": "cloud" if lan else "none",
                         "Match": "" if lan else f"{prov}={name}"})
    write_csv(os.path.join(out, "monitors.csv"), MONITOR_FIELDS, monitors)

    # battery_history.csv: a row whenever the level changes, ~60 days
    history, log, state, at = [], [], {}, {}
    for name, prov, kind, start, drain, replaced in BATTERIES:
        level, last = float(start), None
        for d in range(60, -1, -1):
            t = now - dt.timedelta(days=d, hours=rnd.randint(0, 5), minutes=rnd.randint(0, 59))
            if replaced is not None and d == replaced:
                at[name, "jump"] = last
                level = 100.0
            elif name == "Front Porch Camera" and level < 45:
                level = 100.0           # recharged
                log.append(_entry(rnd, day(d), name, "charged", kind, "100",
                                  "charged overnight with the USB cable", "manual", t))
            else:
                level = max(1.0, level - drain * rnd.uniform(0.6, 1.4))
            pct = int(round(level))
            at[name, d] = pct
            if pct != last:
                history.append({"Time": iso(t), "Device": name, "Provider": prov,
                                "Level": str(pct), "Pct": str(pct)})
                last = pct
        state[name] = {"battery": {"pct": last, "label": str(last), "provider": prov,
                                   "model": "", "at": iso(now - dt.timedelta(minutes=12))},
                       "up": True if name in ips else None,
                       "last_ok": iso(now - dt.timedelta(minutes=12))}
        if last <= 20:
            state[name]["battery_alerted"] = True
    history.sort(key=lambda h: h["Time"])

    # battery_log.csv: the jump, plus some history before the readings began
    g = BATTERIES[0]
    log += [
        _entry(rnd, day(g[5]), g[0], "replaced", g[2], "100",
               f"detected: {g[1]} level rose from {at[g[0], 'jump']}% to 100%", "auto", now),
        _entry(rnd, day(g[5]), g[0], "note", "", "", "swapped in lithium cells, the "
               "alkalines lasted under four months in the cold", "manual", now),
        _entry(rnd, day(150), g[0], "replaced", "2x AA alkaline", "", "", "manual", now),
        _entry(rnd, day(268), g[0], "replaced", "2x AA alkaline", "", "", "manual", now),
        _entry(rnd, day(190), "Front Door Lock", "replaced", "4x AA", "", "", "manual", now),
        _entry(rnd, day(9), "Front Door Lock", "checked", "", str(at["Front Door Lock", 9]), "keypad still bright",
               "manual", now),
        _entry(rnd, day(240), "Back Door Lock", "replaced", "4x AA", "", "", "manual", now),
        _entry(rnd, day(400), "Laundry Leak Sensor", "replaced", "CR2032", "", "", "manual", now),
        _entry(rnd, day(3), "Laundry Leak Sensor", "note", "", str(at["Laundry Leak Sensor", 3]),
               "spare CR2032 in the junk drawer", "manual", now),
    ]
    log.sort(key=lambda r: (r["Date"], r["Updated"]))
    write_csv(os.path.join(out, "battery_history.csv"), HISTORY_FIELDS, history)
    write_csv(os.path.join(out, "battery_log.csv"), LOG_FIELDS, log)

    # monitor_state.json: the network monitors too
    for m in monitors[:4]:
        up = m["Device"] != "Doorbell"
        st = {"up": up, "last_ok": iso(now - dt.timedelta(minutes=2 if up else 26 * 60))}
        if not up:
            st["offline_alerted"] = True
        state[m["Device"]] = st
    with open(os.path.join(out, "monitor_state.json"), "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)

    # events.csv: joins, leaves and comebacks over the last few weeks
    by_name = {r["Device"] or r["Hostname"]: r for r in devices}
    events = []
    for days_ago, kind, who in [
            (41, "join", "Hallway Night Light"), (33, "join", "Basement Dehumidifier"),
            (21, "leave", "Coffee Maker Plug"), (14, "join", "Family Tablet"),
            (9, "leave", "Sam Phone"), (6, "back", "Coffee Maker Plug"),
            (4, "leave", "Coffee Maker Plug"), (2, "update", "Bedroom TV"),
            (1, "leave", "Doorbell"), (1, "join", "wlan0-3f2a")]:
        r = by_name[who]
        t = now - dt.timedelta(days=days_ago, hours=rnd.randint(0, 12))
        band = "Ethernet" if r["Link"] == "Ethernet" else r["Link"].replace("Wi-Fi ", "")
        events.append({"Timestamp": iso(t), "Event": kind, "MAC": r["MAC"], "IP": r["IP"],
                       "Vendor": r["Vendor"], "Hostname": r["Hostname"], "Band": band,
                       "Device": r["Device"], "Source": r["Source"]})
    events.sort(key=lambda e: e["Timestamp"])
    write_csv(os.path.join(out, "events.csv"), EVENT_FIELDS, events)

    # an empty credential store, so nothing falls back to a real one
    with open(os.path.join(out, "credentials.json"), "w") as f:
        f.write("{}\n")
    os.chmod(os.path.join(out, "credentials.json"), 0o600)
    return len(devices), len(home), len(history)


def _entry(rnd, date, device, event, battery, level, notes, source, updated):
    return {"Id": "%012x" % rnd.getrandbits(48), "Date": date, "Device": device,
            "Event": event, "Battery": battery, "Level": level, "Notes": notes,
            "Source": source, "Deleted": "",
            # written the day it happened, not when the demo was generated
            "Updated": min(iso(updated), f"{date}T{rnd.randint(8, 21):02d}:{rnd.randint(0, 59):02d}:00")}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__.split("\n\n")[1])
    target = os.path.abspath(sys.argv[1])
    if os.path.exists(os.path.join(target, "devices.csv")) and \
            not os.path.exists(os.path.join(target, ".netinv-demo")):
        sys.exit(f"  ! {target} already holds a devices.csv that isn't demo data; "
                 f"pick an empty directory")
    nd, nh, nb = build(target)
    open(os.path.join(target, ".netinv-demo"), "w").close()
    print(f"  wrote a demo home to {target}: {nd} network rows, {nh} home devices, "
          f"{nb} battery readings")
