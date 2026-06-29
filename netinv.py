#!/usr/bin/env python3
"""
netinv - home network device inventory.

The stable identity of a device is its MAC address (a device may have several -
e.g. wired + Wi-Fi). IP is transient (DHCP) and re-discovered on every run. The
friendly "Device" name is a curated layer you maintain on top.

Master store: devices.csv  (one row per MAC; rows sharing a Device name belong
to the same physical device). You edit the Device and Notes columns; everything
else is refreshed automatically.

Data sources merged on each run, keyed by MAC:
  - the existing export (Documents/DevicesMacs.csv)  [--import-csv]
  - a local scan of this machine's subnet (nmap ping-sweep + ARP + reverse DNS)
  - the Xfinity/Technicolor gateway's connected-devices page  [--router]

Usage:
  netinv.py update [--scan] [--router] [--import-csv PATH]   # gather + merge + report
  netinv.py name "Living Room TV" <mac> [<mac> ...]          # assign MACs to a device
  netinv.py report                                           # rebuild reports from devices.csv
  netinv.py show [name-substring]                            # print current inventory
"""

import argparse
import csv
import datetime as dt
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "devices.csv")
REPORT_MD = os.path.join(HERE, "report.md")
REPORT_BY_DEVICE = os.path.join(HERE, "report-by-device.csv")
REPORT_XLSX = os.path.join(HERE, "inventory.xlsx")
HOME_SSIDS_FILE = os.path.join(HERE, "home_ssids.txt")
WIFI_SCAN_OUT = os.path.join(HERE, "wifi_scan.csv")
DEFAULT_IMPORT = os.path.expanduser("~/Documents/DevicesMacs.csv")

ROUTER_HOST = os.environ.get("NETINV_ROUTER", "192.168.1.1")
SUBNET = os.environ.get("NETINV_SUBNET", "192.168.1.0/24")
# A device not seen for this many days is reported as "defunct".
STALE_DAYS = int(os.environ.get("NETINV_STALE_DAYS", "30"))

FIELDS = ["Device", "MAC", "IP", "Vendor", "Hostname", "Link", "Status",
          "LastSeen", "Source", "Notes"]
TODAY = dt.date.today().isoformat()

MAC_RE = re.compile(r"\b([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})\b")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def norm_mac(mac):
    """Normalize to upper-case, zero-padded, colon form. macOS `arp` drops
    leading zeros in octets (e.g. 4c:e5:c -> 4C:E5:0C), so pad each octet."""
    if not mac:
        return ""
    parts = mac.strip().replace("-", ":").split(":")
    if len(parts) != 6:
        return mac.strip().upper()
    try:
        return ":".join(f"{int(p, 16):02X}" for p in parts)
    except ValueError:
        return mac.strip().upper()


def ip_key(ip):
    try:
        return tuple(int(o) for o in ip.split("."))
    except (ValueError, AttributeError):
        return (999, 999, 999, 999)


def is_lan_ip(ip):
    return bool(ip) and ip.startswith("192.168.1.") and not ip.endswith(".255")


def better(new, old, bad=("", "N/A", "n/a", "?", "unknown")):
    """Prefer a meaningful new value, else keep the old one."""
    if new and new not in bad:
        return new
    return old or ""


# --------------------------------------------------------------------------- #
# master CSV load / save
# --------------------------------------------------------------------------- #
def load_master():
    rows = OrderedDict()  # MAC -> dict
    if not os.path.exists(MASTER):
        return rows
    with open(MASTER, newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            mac = norm_mac(r.get("MAC", ""))
            if not mac:
                continue
            rows[mac] = {k: (r.get(k) or "").strip() for k in FIELDS}
            rows[mac]["MAC"] = mac
    return rows


def save_master(rows):
    # sorted by IP address (devices with no current IP sort last)
    ordered = sorted(rows.values(), key=lambda r: ip_key(r["IP"]))
    with open(MASTER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, delimiter=";")
        w.writeheader()
        w.writerows(ordered)


# --------------------------------------------------------------------------- #
# data sources -> list of sightings  {mac, ip, vendor, hostname, source, online}
# --------------------------------------------------------------------------- #
def import_csv(path):
    out = []
    if not os.path.exists(path):
        print(f"  ! import csv not found: {path}")
        return out
    with open(path, newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            mac = norm_mac(r.get("Mac") or r.get("MAC") or "")
            if not mac:
                continue
            out.append({
                "mac": mac,
                "ip": (r.get("IP") or "").strip(),
                "vendor": (r.get("Vendor") or "").strip(),
                "hostname": (r.get("Host") or "").strip(),
                "source": "csv",
                "online": False,  # an old export is not "seen now"
            })
    print(f"  imported {len(out)} rows from {path}")
    return out


def local_scan(do_sweep=True):
    """nmap ping-sweep (populates ARP) then read the system ARP table."""
    if do_sweep:
        print(f"  ping-sweeping {SUBNET} (nmap -sn)...")
        try:
            subprocess.run(["nmap", "-sn", "-n", SUBNET],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=180)
        except FileNotFoundError:
            print("  ! nmap not found - relying on existing ARP cache")
        except subprocess.TimeoutExpired:
            print("  ! nmap sweep timed out - using whatever ARP learned so far")

    out = [{"mac": mac, "ip": ip, "vendor": "", "hostname": reverse_name(ip),
            "source": "scan", "online": True}
           for ip, mac in arp_table().items()]
    print(f"  local scan found {len(out)} hosts on {SUBNET}")
    return out


def arp_table():
    """Return {ip: MAC} for LAN hosts in the system ARP cache."""
    table = {}
    try:
        arp = subprocess.run(["arp", "-an"], capture_output=True, text=True, timeout=15).stdout
    except Exception as e:  # noqa: BLE001
        print(f"  ! arp failed: {e}")
        return table
    for line in arp.splitlines():
        # ? (10.0.0.42) at aa:bb:cc:00:11:22 on en0 ifscope [ethernet]
        m_ip = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
        m_mac = re.search(r"at ([0-9a-fA-F:]+) on", line)
        if not (m_ip and m_mac):
            continue
        ip = m_ip.group(1)
        if not is_lan_ip(ip):
            continue
        mac = norm_mac(m_mac.group(1))
        if mac == "FF:FF:FF:FF:FF:FF" or mac.startswith(("01:00:5E", "33:33")):
            continue
        table[ip] = mac
    return table


def reverse_name(ip):
    try:
        socket.setdefaulttimeout(1.0)
        name = socket.gethostbyaddr(ip)[0]
        # strip trailing .local / .lan / domain for readability
        return name.split(".")[0] if name else ""
    except (socket.herror, socket.gaierror, OSError):
        return ""
    finally:
        socket.setdefaulttimeout(None)


def ssdp_discover(timeout=3):
    """SSDP/UPnP discovery: M-SEARCH, then fetch each device-description XML and
    read its <friendlyName>/<modelName>/<manufacturer>. friendlyName is usually
    the *user-assigned* name (e.g. 'Family Room TV'), so it makes an excellent
    discovered name. IP is mapped to a MAC via the ARP table."""
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           "HOST:239.255.255.250:1900\r\n"
           'MAN:"ssdp:discover"\r\n'
           "MX:2\r\nST:ssdp:all\r\n\r\n").encode()
    locs = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(timeout)
        s.sendto(msg, ("239.255.255.250", 1900))
        while True:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                break
            m = re.search(rb"LOCATION:\s*(\S+)", data, re.I)
            if m and is_lan_ip(addr[0]):
                locs.setdefault(addr[0], m.group(1).decode("ascii", "replace"))
    except OSError as e:
        print(f"  ! SSDP failed: {e}")
        return []
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass

    ip2mac = arp_table()
    out = []
    for ip, url in locs.items():
        name = vendor = ""
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                xml = r.read().decode("utf-8", "replace")
            fn = re.search(r"<friendlyName>([^<]+)</friendlyName>", xml, re.I)
            mn = re.search(r"<modelName>([^<]+)</modelName>", xml, re.I)
            mf = re.search(r"<manufacturer>([^<]+)</manufacturer>", xml, re.I)
            name = (fn.group(1).strip() if fn else "") or (mn.group(1).strip() if mn else "")
            vendor = mf.group(1).strip() if mf else ""
        except Exception:  # noqa: BLE001
            continue
        mac = ip2mac.get(ip)
        if mac and name:
            out.append({"mac": mac, "ip": ip, "vendor": vendor, "hostname": name,
                        "source": "ssdp", "online": True})
    print(f"  SSDP discovered {len(out)} named devices")
    return out


def router_scan(user, password):
    """Log into the Xfinity/Technicolor gateway and scrape connected devices.

    Login flow (discovered from the gateway): POST username/password/locale to
    /check.jst with a cookie jar, then GET connected_devices_computers.jst.
    """
    base = f"http://{ROUTER_HOST}"
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 netinv")]

    try:
        opener.open(f"{base}/index.jst", timeout=10).read()  # seed cookie
        data = urllib.parse.urlencode(
            {"username": user, "password": password, "locale": "false"}
        ).encode()
        opener.open(f"{base}/check.jst", data=data, timeout=10).read()
        html = opener.open(f"{base}/connected_devices_computers.jst",
                           timeout=15).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"  ! router login/scrape failed: {e}")
        return []

    # Logged-in pages still contain a "Logout"->home_loggedout link and i18n
    # strings, so don't test for those. The real signal is the device arrays.
    if "onlineHostMAC" not in html and "offlineHostMAC" not in html:
        print("  ! router login failed (no device data returned - check password)")
        return []

    # cache raw HTML so the parser can be refined against the real structure
    with open(os.path.join(HERE, "router_devices_raw.html"), "w") as f:
        f.write(html)

    sightings = parse_router_html(html)
    print(f"  router reported {len(sightings)} devices "
          f"({sum(s['online'] for s in sightings)} online)")
    return sightings


def _js_array(html, var):
    """Extract a JavaScript string array `var <var> = ["a","b",...];`."""
    m = re.search(r"var\s+" + re.escape(var) + r"\s*=\s*\[(.*?)\]", html, re.S)
    if not m:
        return []
    return re.findall(r'"([^"]*)"', m.group(1))


def parse_router_html(html):
    """Parse the Xfinity/Technicolor connected-devices page.

    Hostname + MAC + online/offline come from parallel JS arrays
    (onlineHostNameArr/onlineHostMAC and the offline equivalents). The current
    IP per device is rendered in the HTML as
        IPv4 Address</b><br/></dd><IP><dd>...MAC Address</b><br/></dd><MAC>
    """
    # MAC -> IP from the rendered HTML blocks
    ip_by_mac = {}
    for ip, mac, _ in re.findall(
            r"IPv4 Address</b><br/?></dd>\s*([\d.]+)\s*<dd>\s*<b[^>]*>MAC Address"
            r"</b><br/?></dd>\s*(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", html):
        ip_by_mac[norm_mac(mac)] = ip

    # MAC -> connection medium (Ethernet / Wi-Fi 2.4|5|6 GHz) from the per-device
    # connection-type cell that follows each MAC.
    link_by_mac = {}
    for mac, _, ct in re.findall(
            r"MAC Address</b><br/?></dd>\s*(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"
            r".*?headers='connection-type'[^>]*>\s*<span[^>]*>\s*([^<]+?)\s*</span>",
            html, re.S):
        link_by_mac[norm_mac(mac)] = re.sub(r"\s+", " ", ct).strip()

    out = []
    for prefix, online in (("online", True), ("offline", False)):
        macs = _js_array(html, f"{prefix}HostMAC")
        names = _js_array(html, f"{prefix}HostNameArr")
        for i, raw_mac in enumerate(macs):
            mac = norm_mac(raw_mac)
            if mac.count(":") != 5:
                continue
            name = names[i].strip() if i < len(names) else ""
            out.append({
                "mac": mac,
                "ip": ip_by_mac.get(mac, ""),
                "vendor": "",
                "hostname": name,
                "link": link_by_mac.get(mac, ""),
                "source": "router",
                "online": online,
            })

    # de-dup by MAC; prefer an online entry, then one carrying an IP
    dedup = {}
    for s in out:
        cur = dedup.get(s["mac"])
        if cur is None or (s["online"] and not cur["online"]) \
                or (s["ip"] and not cur["ip"]):
            dedup[s["mac"]] = s
    return list(dedup.values())


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def name_quality(name):
    """Score how 'friendly' a discovered name is (higher = better as a label).
      3 = human name ('Family Room TV', 'Roku Premiere')
      2 = model/hostname token ('iPhone-68', 'ESP_921C68', 'HS200')
      1 = junk (raw MAC, 'wlan0-53', 'lwip', bare 12-hex)
      0 = empty
    """
    n = (name or "").strip()
    if not n:
        return 0
    low = n.lower()
    if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", low) \
            or re.fullmatch(r"[0-9a-f]{12}", low) \
            or low.startswith(("wlan0", "lwip", "espressif")):
        return 1
    if " " in n or "-" in n and re.search(r"[A-Za-z]{3,}\s|\s[A-Za-z]{3,}", n):
        return 3
    if re.search(r"[A-Za-z]{2,}.*[A-Za-z]{2,}", n) and not re.search(r"\d{4,}", n):
        return 3 if " " in n else 2
    return 2


def merge(master, sightings):
    # The gateway is authoritative for online/offline when it ran: the local
    # ARP cache keeps stale entries, so a device the router lists as offline is
    # offline even if it lingers in ARP.
    router_ran = any(s["source"] == "router" for s in sightings)
    router_online, router_offline, scan_seen = set(), set(), set()

    for s in sightings:
        mac = s["mac"]
        row = master.get(mac)
        if row is None:
            row = {k: "" for k in FIELDS}
            row["MAC"] = mac
            master[mac] = row
        row["IP"] = better(s["ip"], row["IP"])
        row["Vendor"] = better(s["vendor"], row["Vendor"])
        row["Link"] = better(s.get("link", ""), row["Link"])
        # keep the highest-quality discovered name (SSDP friendly > router > raw)
        if name_quality(s["hostname"]) > name_quality(row["Hostname"]):
            row["Hostname"] = s["hostname"].strip()
        # accumulate distinct sources
        srcs = set(filter(None, row["Source"].split("+"))) | {s["source"]}
        row["Source"] = "+".join(sorted(srcs))

        if s["source"] == "router":
            (router_online if s["online"] else router_offline).add(mac)
        elif s["online"]:
            scan_seen.add(mac)

    seen_now = set()
    for mac, row in master.items():
        if mac in router_online:
            online = True
        elif mac in router_offline:
            online = False          # router says offline -> trust it over stale ARP
        elif mac in scan_seen and not (router_ran and mac in router_offline):
            online = True           # on the wire but unknown to the gateway
        else:
            online = False
        if online:
            row["Status"] = "online"
            row["LastSeen"] = TODAY
            seen_now.add(mac)
        else:
            row["Status"] = stale_status(row["LastSeen"])
    return seen_now


def stale_status(last_seen):
    # No LastSeen means the device has never been confirmed live (only imported
    # from the old export) -> treat as retired/defunct.
    if not last_seen:
        return "defunct"
    try:
        d = dt.date.fromisoformat(last_seen)
    except ValueError:
        return "defunct"
    age = (dt.date.today() - d).days
    return "offline" if age <= STALE_DAYS else "defunct"


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #
def link_class(link):
    """Collapse the router's connection medium into Wired / Wireless."""
    l = (link or "").lower()
    if "ethernet" in l or "eth" in l:
        return "Wired"
    if "wi-fi" in l or "wifi" in l or "wlan" in l or "ghz" in l:
        return "Wireless"
    return ""


# (keyword, category) ordered most- to least-specific; first hit wins.
_CATEGORY_RULES = [
    ("camera|cam |blink|wyze|hualai|raysharp|dvr|nvr|doorbell", "Camera / Security"),
    ("thermostat|ecobee|nest", "Thermostat"),
    ("garage|myq", "Garage"),
    ("lock|u-bolt|august|schlage|deadbolt", "Lock"),
    ("sprinkler|orbit|b-hyve|rachio|flume|waterguru|water", "Water / Irrigation"),
    ("solaredge|inverter|sense|energy|emporia", "Energy / Solar"),
    ("printer|epson|brother|hp |laserjet", "Printer"),
    ("obi|polycom|voip", "VoIP Phone"),
    ("roku|chromecast|apple ?tv|fire ?tv|blu-ray|onkyo|receiver|harmony| tv|tv$|television", "TV / Media"),
    ("echo|alexa|home mini|google home|homepod|sonos|speaker|nest audio", "Speaker / Voice"),
    ("hub|bridge|smartthings|hue|zigbee|z-wave|home assistant|homeassistant", "Hub / Bridge"),
    ("plug|switch|wemo|kasa|hs1|hs2|kp4|dimmer|outlet", "Smart Plug / Switch"),
    ("bulb|light|lifx|lamp", "Smart Light"),
    ("iphone|ipad|galaxy|pixel|phone|tablet", "Phone / Tablet"),
    ("macbook|mac studio|imac|mac mini|laptop|desktop|pc$|nas|qnap|synology|server", "Computer / NAS"),
    ("gateway|router|extender|repeater|access point|ap |asus|eero|orbi|xfinity", "Network"),
    ("tesla|car|vehicle|ev ", "Vehicle"),
    ("wii|xbox|playstation|ps[45]|nintendo|switch ", "Game Console"),
]


def categorize(row):
    """Best-effort device category from name + hostname + vendor keywords."""
    hay = " ".join([row.get("Device", ""), row.get("Hostname", ""),
                    row.get("Vendor", "")]).lower()
    for pattern, cat in _CATEGORY_RULES:
        if re.search(pattern, hay):
            return cat
    return "Other / Unknown"


_GENERIC_HOST = re.compile(
    r"^(mac|imac|iphone|ipad|ipod|android|localhost|espressif|amazon|samsung|"
    r"sony|guest|unknown|wlan0|lwip)\b", re.I)


def suggest_pairs(master):
    """Suggest MACs that are probably the same physical device (e.g. a box with
    both wired and Wi-Fi NICs). Signal: two+ MACs reporting the *same* distinctive
    hostname. Generic/rotating names (Mac, iPhone, ...) are excluded. Returns a
    list of row-groups not already unified under one friendly name."""
    by_host = {}
    for r in master.values():
        h = r["Hostname"].strip()
        if name_quality(h) < 2 or _GENERIC_HOST.match(h):
            continue
        by_host.setdefault(h.lower(), []).append(r)

    out = []
    for rows in by_host.values():
        macs = {r["MAC"] for r in rows}
        devs = {r["Device"] for r in rows}
        already = len(devs) == 1 and "" not in devs   # all share one real name
        if len(macs) >= 2 and not already:
            out.append(sorted(rows, key=lambda r: ip_key(r["IP"])))
    return out


# Vendors that ship many single-radio units, where adjacent MACs are different
# physical devices (not a dual-NIC box) -> excluded from the MAC-sequence guess.
_BULK_VENDORS = re.compile(
    r"espressif|beken|tuya|amazon|sichuan|mega well|ampak|murata|tp-?link|belkin|"
    r"obihai|ecobee|orbit|wyze|roku|immedia|raysharp|seiko|chamberlain|tianjin|"
    r"philips|physical graph|onkyo|solaredge", re.I)


def _mac_suffix(mac):
    return int(mac.replace(":", "")[6:], 16)   # lower 24 bits


def suggest_pairs_by_mac(master):
    """Opt-in, looser heuristic: a device's wired + Wi-Fi NICs often get adjacent
    MACs from the same OUI. Group same-OUI MACs whose suffixes differ by <=3,
    excluding bulk single-radio IoT vendors. Higher recall, lower precision -
    review before applying. Pairs with differing Link types rank as stronger."""
    by_oui = {}
    for r in master.values():
        if len(r["MAC"]) != 17 or _BULK_VENDORS.search(r["Vendor"]):
            continue
        by_oui.setdefault(r["MAC"][:8], []).append(r)

    out = []
    for rows in by_oui.values():
        rows = sorted(rows, key=lambda r: _mac_suffix(r["MAC"]))
        for a, b in zip(rows, rows[1:]):
            if a["Device"] and a["Device"] == b["Device"]:
                continue                       # already grouped
            if 1 <= _mac_suffix(b["MAC"]) - _mac_suffix(a["MAC"]) <= 3:
                out.append([a, b])
    return out


# --------------------------------------------------------------------------- #
# Wi-Fi neighbor-scan classifier (mine vs neighbor)
# --------------------------------------------------------------------------- #
def home_ssids():
    """Home SSID(s): NETINV_HOME_SSIDS env (comma-sep), else home_ssids.txt."""
    env = os.environ.get("NETINV_HOME_SSIDS", "")
    out = {s.strip().lower() for s in env.split(",") if s.strip()}
    if os.path.exists(HOME_SSIDS_FILE):
        with open(HOME_SSIDS_FILE) as f:
            out |= {ln.strip().lower() for ln in f if ln.strip()}
    return out


def parse_wifi_scan(text):
    """Parse a pasted/exported Wi-Fi survey. Tolerant: per line it pulls a BSSID,
    the RSSI ('-NN dBm'), the SSID (text between them, may be blank/hidden), and
    carries forward the band ('2.4/5/6 GHz') and channel when present."""
    mac_re = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
    rssi_re = re.compile(r"(-?\d{1,3})\s*dBm")
    band_re = re.compile(r"\b(2\.4|5|6)\s?GHz\b", re.I)
    band, chan, out = "", "", []
    for line in text.splitlines():
        if not line.strip():
            continue
        bm = band_re.search(line)
        if bm:
            band = bm.group(1) + " GHz"
        mm = mac_re.search(line)
        if not mm:
            continue
        bssid = norm_mac(mm.group(1))
        rm = rssi_re.search(line)
        rssi = int(rm.group(1)) if rm else None
        # leading channel number (token before the MAC, after any band)
        head = line[:mm.start()]
        cm = re.search(r"\b(\d{1,3})\b\s*$", head.replace(band, ""))
        if cm:
            chan = cm.group(1)
        # SSID = text between MAC and RSSI, cleaned of tabs
        mid = line[mm.end():(rm.start() if rm else len(line))]
        ssid = re.sub(r"\s+", " ", mid).strip()
        out.append({"band": band, "chan": chan, "bssid": bssid,
                    "ssid": ssid, "rssi": rssi})
    return out


def radio_match(bssid, inv_macs):
    """Return the inventory MAC that is the same physical device as this BSSID,
    or None. A device's AP/Direct radio differs from its client MAC only in the
    locally-administered bit (same last 5 octets) or by a small last-octet offset
    (same first 5 octets)."""
    b = bssid.upper()
    if b in inv_macs:
        return b
    for m in inv_macs:
        if m[3:] == b[3:]:                       # same last 5 octets (LA-bit flip)
            return m
    try:
        bp, blast = b[:14], int(b[15:], 16)
        for m in inv_macs:
            if m[:14] == bp and abs(int(m[15:], 16) - blast) <= 4:
                return m
    except ValueError:
        pass
    return None


def classify_scan(items, master):
    ssids = home_ssids()
    inv_macs = set(master)
    for it in items:
        match = radio_match(it["bssid"], inv_macs)
        if match:
            dev = master[match]["Device"] or master[match]["Hostname"] or "(unnamed)"
            it["verdict"], it["why"], it["match"] = "MINE", f"device: {dev}", match
        elif it["ssid"] and it["ssid"].lower() in ssids:
            it["verdict"], it["why"], it["match"] = "MINE", f"your SSID '{it['ssid']}'", ""
        else:
            it["verdict"], it["why"], it["match"] = "neighbor", "", ""
    return items


def write_xlsx(master):
    """Multi-tab workbook: Devices (by IP), By Device (grouped), Summary."""
    try:
        import xlsxwriter
    except ImportError:
        print("  ! xlsx skipped (`pip install xlsxwriter`)")
        return

    rows = sorted(master.values(), key=lambda r: ip_key(r["IP"]))
    wb = xlsxwriter.Workbook(REPORT_XLSX)
    hdr = wb.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "white",
                         "border": 1})
    cell = wb.add_format({"border": 1})
    title = wb.add_format({"bold": True, "font_size": 14})

    # --- Sheet 1: Devices (full detail, sorted by IP) ---
    ws = wb.add_worksheet("Devices")
    cols = ["IP", "Device", "Category", "Link", "Status", "MAC", "Vendor",
            "Hostname", "LastSeen", "Source", "Notes"]
    widths = [14, 28, 20, 14, 9, 19, 26, 24, 12, 14, 24]
    for c, (name, wdt) in enumerate(zip(cols, widths)):
        ws.set_column(c, c, wdt)
        ws.write(0, c, name, hdr)
    for i, r in enumerate(rows, start=1):
        vals = [r["IP"], r["Device"], categorize(r), link_class(r["Link"]),
                r["Status"], r["MAC"], r["Vendor"], r["Hostname"],
                r["LastSeen"], r["Source"], r["Notes"]]
        for c, v in enumerate(vals):
            ws.write(i, c, v, cell)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, len(rows), len(cols) - 1)

    # --- Sheet 2: By Device (grouped; multi-MAC devices on one row) ---
    groups = OrderedDict()
    for r in rows:
        key = r["Device"] or f"(unnamed) {r['MAC']}"
        groups.setdefault(key, []).append(r)
    ws2 = wb.add_worksheet("By Device")
    cols2 = ["Device", "Category", "Status", "Link", "#MACs", "IP(s)", "MAC(s)",
             "Vendor", "LastSeen", "Notes"]
    widths2 = [28, 20, 12, 18, 7, 30, 40, 26, 12, 24]
    for c, (name, wdt) in enumerate(zip(cols2, widths2)):
        ws2.set_column(c, c, wdt)
        ws2.write(0, c, name, hdr)
    grp_sorted = sorted(groups.items(),
                        key=lambda kv: ip_key(min((x["IP"] for x in kv[1] if x["IP"]),
                                                  default="")))
    for i, (name, grp) in enumerate(grp_sorted, start=1):
        disp = "" if name.startswith("(unnamed)") else name
        links = sorted({link_class(x["Link"]) for x in grp if link_class(x["Link"])})
        vals = [disp, categorize(grp[0]),
                "/".join(sorted({x["Status"] for x in grp})),
                " + ".join(links),
                len(grp),
                ", ".join(sorted({x["IP"] for x in grp if x["IP"]}, key=ip_key)),
                ", ".join(x["MAC"] for x in grp),
                ", ".join(sorted({x["Vendor"] for x in grp if x["Vendor"]})),
                max((x["LastSeen"] for x in grp), default=""),
                " | ".join(sorted({x["Notes"] for x in grp if x["Notes"]}))]
        for c, v in enumerate(vals):
            ws2.write(i, c, v, cell)
    ws2.freeze_panes(1, 0)
    ws2.autofilter(0, 0, len(grp_sorted), len(cols2) - 1)

    # --- Sheet 3: Summary (rollups) ---
    ws3 = wb.add_worksheet("Summary")
    ws3.set_column(0, 0, 22)
    ws3.set_column(1, 3, 12)

    def table(start_row, header, counter):
        ws3.write(start_row, 0, header, title)
        ws3.write(start_row + 1, 0, header.split(" by ")[-1], hdr)
        ws3.write(start_row + 1, 1, "Total", hdr)
        ws3.write(start_row + 1, 2, "Online", hdr)
        r = start_row + 2
        for k in sorted(counter, key=lambda x: -counter[x]["total"]):
            ws3.write(r, 0, k, cell)
            ws3.write(r, 1, counter[k]["total"], cell)
            ws3.write(r, 2, counter[k]["online"], cell)
            r += 1
        return r + 1

    def tally(keyfn):
        out = {}
        for r in rows:
            k = keyfn(r) or "(unknown)"
            out.setdefault(k, {"total": 0, "online": 0})
            out[k]["total"] += 1
            out[k]["online"] += (r["Status"] == "online")
        return out

    nxt = table(0, "Devices by Category", tally(categorize))
    nxt = table(nxt, "Devices by Connection", tally(lambda r: link_class(r["Link"]) or "Unknown"))
    table(nxt, "Devices by Status", tally(lambda r: r["Status"]))

    # --- Sheet 4: Suggested Pairs (likely same physical device) ---
    pairs = suggest_pairs(master)
    if pairs:
        ws4 = wb.add_worksheet("Suggested Pairs")
        cols4 = ["Shared hostname", "Links", "MACs", "IPs", "Current name(s)",
                 "Suggested command"]
        for c, (name, wdt) in enumerate(zip(cols4, [24, 20, 40, 28, 24, 50])):
            ws4.set_column(c, c, wdt)
            ws4.write(0, c, name, hdr)
        for i, grp in enumerate(pairs, start=1):
            host = grp[0]["Hostname"]
            macs = [r["MAC"] for r in grp]
            links = " + ".join(sorted({link_class(r["Link"]) for r in grp if link_class(r["Link"])}))
            names = sorted({r["Device"] for r in grp if r["Device"]})
            suggested = names[0] if names else host
            cmd = f'./netinv.py name "{suggested}" ' + " ".join(macs)
            for c, v in enumerate([host, links, ", ".join(macs),
                                   ", ".join(r["IP"] for r in grp),
                                   ", ".join(names), cmd]):
                ws4.write(i, c, v, cell)
        ws4.freeze_panes(1, 0)

    wb.close()
    print(f"  wrote {REPORT_XLSX}")


def write_reports(master):
    # group by device (unnamed -> each MAC is its own group)
    groups = OrderedDict()
    for row in sorted(master.values(),
                      key=lambda r: (r["Device"] == "", r["Device"].lower(), ip_key(r["IP"]))):
        key = row["Device"] or f"(unnamed) {row['MAC']}"
        groups.setdefault(key, []).append(row)

    # by-device CSV
    with open(REPORT_BY_DEVICE, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Device", "Status", "IPs", "MACs", "Vendors", "Hostnames", "LastSeen", "Notes"])
        for name, rows in groups.items():
            disp = name if not name.startswith("(unnamed)") else ""
            w.writerow([
                disp,
                "/".join(sorted({r["Status"] for r in rows})),
                ", ".join(sorted({r["IP"] for r in rows if r["IP"]}, key=ip_key)),
                ", ".join(r["MAC"] for r in rows),
                ", ".join(sorted({r["Vendor"] for r in rows if r["Vendor"]})),
                ", ".join(sorted({r["Hostname"] for r in rows if r["Hostname"]})),
                max((r["LastSeen"] for r in rows), default=""),
                " | ".join(sorted({r["Notes"] for r in rows if r["Notes"]})),
            ])

    # markdown
    named = [(n, r) for n, r in groups.items() if not n.startswith("(unnamed)")]
    unnamed = [(n, r) for n, r in groups.items() if n.startswith("(unnamed)")]
    total = len(master)
    online = sum(1 for r in master.values() if r["Status"] == "online")
    with open(REPORT_MD, "w") as f:
        f.write(f"# Network inventory — {TODAY}\n\n")
        f.write(f"- Total MACs tracked: **{total}**  ·  online now: **{online}**  ·  "
                f"named devices: **{len(named)}**  ·  unnamed MACs: **{len(unnamed)}**\n\n")
        f.write("## Named devices\n\n")
        f.write("| Device | Status | IP(s) | MAC(s) | Vendor | Last seen | Notes |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for name, rows in named:
            f.write("| {} | {} | {} | {} | {} | {} | {} |\n".format(
                name,
                "/".join(sorted({r["Status"] for r in rows})),
                "<br>".join(sorted({r["IP"] for r in rows if r["IP"]}, key=ip_key)),
                "<br>".join(r["MAC"] for r in rows),
                "<br>".join(sorted({r["Vendor"] for r in rows if r["Vendor"]})),
                max((r["LastSeen"] for r in rows), default=""),
                " ".join(sorted({r["Notes"] for r in rows if r["Notes"]})),
            ))
        f.write(f"\n## Unnamed MACs ({len(unnamed)}) — assign a friendly name\n\n")
        f.write("Use: `./netinv.py name \"Friendly Name\" <mac> [<mac> ...]`\n\n")
        f.write("| Status | IP | MAC | Vendor | Hostname |\n|---|---|---|---|---|\n")
        for _, rows in unnamed:
            r = rows[0]
            f.write(f"| {r['Status']} | {r['IP']} | {r['MAC']} | {r['Vendor']} | {r['Hostname']} |\n")
    print(f"  wrote {REPORT_MD}\n  wrote {REPORT_BY_DEVICE}")
    write_xlsx(master)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def keychain_creds():
    """Read the router login from the macOS Keychain (Passwords app /
    `security`). The entry is an internet-password whose 'server' is the router
    host; its account is the username and its secret is the password.

    Store it once with:  ./netinv.py set-router-password
    or manually:
      security add-internet-password -s 192.168.1.1 -a admin -w 'PASS' -U
    """
    try:
        pw = subprocess.run(
            ["security", "find-internet-password", "-s", ROUTER_HOST, "-w"],
            capture_output=True, text=True, timeout=15)
        if pw.returncode != 0:
            return None, None
        password = pw.stdout.strip()
        # account (username) lives in the attribute dump
        attrs = subprocess.run(
            ["security", "find-internet-password", "-s", ROUTER_HOST],
            capture_output=True, text=True, timeout=15).stdout
        m = re.search(r'"acct"<blob>="([^"]*)"', attrs)
        user = (m.group(1) if m else "") or "admin"
        return user, password
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None


def get_router_creds():
    # 1) explicit env wins (useful for headless/cron)
    user = os.environ.get("NETINV_ROUTER_USER")
    pw = os.environ.get("NETINV_ROUTER_PASS")
    if user and pw:
        return user, pw
    # 2) macOS Keychain
    ku, kpw = keychain_creds()
    if kpw:
        print(f"  using router credentials from Keychain (account '{ku}')")
        return ku, kpw
    # 3) interactive fallback
    import getpass
    print(f"  no Keychain entry for {ROUTER_HOST}; prompting "
          f"(store it with: ./netinv.py set-router-password)")
    user = user or input("Router username [admin]: ").strip() or "admin"
    pw = pw or getpass.getpass("Router password: ")
    return user, pw


def auto_name(master):
    """Fill blank Device names from a high-quality discovered Hostname. Never
    overwrites a name you've set. Returns the number of devices auto-named."""
    n = 0
    for row in master.values():
        if row["Device"]:
            continue
        if name_quality(row["Hostname"]) >= 3:
            row["Device"] = row["Hostname"]
            n += 1
    return n


def cmd_update(args):
    master = load_master()
    sightings = []
    do_local = args.scan or not (args.router or args.import_csv is not None)
    if args.import_csv is not None:
        sightings += import_csv(args.import_csv or DEFAULT_IMPORT)
    if args.router:
        u, p = get_router_creds()
        sightings += router_scan(u, p)
    if do_local:
        sightings += local_scan(do_sweep=not args.no_sweep)
        if not args.no_ssdp:
            sightings += ssdp_discover()

    seen = merge(master, sightings)
    if args.cloud:
        import connectors
        cloud_merge(master, connectors.discover())
    if args.auto_name:
        named = auto_name(master)
        print(f"  auto-named {named} device(s) from discovery")
    save_master(master)
    write_reports(master)
    print(f"\n  {len(master)} MACs in {MASTER}  ·  {len(seen)} online this run")
    unnamed = sum(1 for r in master.values() if not r["Device"])
    if unnamed:
        print(f"  {unnamed} MACs have no friendly name yet — see {REPORT_MD}")
    pairs = suggest_pairs(master)
    if pairs:
        print(f"  {len(pairs)} possible wired+wifi device(s) detected — "
              f"run ./netinv.py pairs")


def cmd_wifi_scan(args):
    """Classify a pasted/exported Wi-Fi survey as mine vs neighbor."""
    src = args.file
    text = sys.stdin.read() if src in (None, "-") else open(src).read()
    items = classify_scan(parse_wifi_scan(text), load_master())
    if not items:
        print("  no BSSIDs parsed (paste the survey table, or pass a file)")
        return
    mine = [i for i in items if i["verdict"] == "MINE"]
    print(f"  parsed {len(items)} APs · {len(mine)} mine · {len(items)-len(mine)} neighbor"
          f"  (home SSIDs: {', '.join(sorted(home_ssids())) or 'none set'})\n")
    print(f"  {'BAND':7} {'CH':3} {'BSSID':18} {'RSSI':5} {'VERDICT':8} {'SSID / why'}")
    for i in sorted(items, key=lambda x: (x["verdict"] != "MINE", -(x["rssi"] or -999))):
        why = i["why"] or i["ssid"] or "(hidden)"
        print(f"  {i['band']:7} {i['chan']:3} {i['bssid']:18} "
              f"{(str(i['rssi'])+'dBm') if i['rssi'] is not None else '':5} "
              f"{i['verdict']:8} {why}")
    with open(WIFI_SCAN_OUT, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Band", "Channel", "BSSID", "SSID", "RSSI", "Verdict", "Why", "MatchMAC"])
        for i in items:
            w.writerow([i["band"], i["chan"], i["bssid"], i["ssid"], i["rssi"],
                        i["verdict"], i["why"], i["match"]])
    print(f"\n  wrote {WIFI_SCAN_OUT}")

    if args.add_mine:
        master = load_master()
        added = 0
        for i in mine:
            if i["bssid"] in master:
                continue
            row = {k: "" for k in FIELDS}
            row.update({"MAC": i["bssid"], "Vendor": "", "Source": "wifi-scan",
                        "LastSeen": TODAY, "Link": "Wi-Fi",
                        "Notes": f"Wi-Fi radio seen in scan ({i['ssid'] or 'hidden'})"})
            if i["match"]:
                row["Device"] = master[i["match"]]["Device"]
                row["Hostname"] = master[i["match"]]["Hostname"]
            elif i["why"].startswith("your SSID"):
                row["Device"] = f"Wi-Fi AP ({i['ssid']})"
            row["Status"] = "offline"
            master[i["bssid"]] = row
            added += 1
        save_master(master)
        write_reports(master)
        print(f"  added {added} of-mine BSSID(s) to inventory")


def cmd_pairs(args):
    """Show (or apply) suggested same-device MAC groupings."""
    master = load_master()
    groups = [("hostname", g) for g in suggest_pairs(master)]
    if args.by_mac:
        seen = {frozenset(r["MAC"] for r in g) for _, g in groups}
        for g in suggest_pairs_by_mac(master):
            if frozenset(r["MAC"] for r in g) not in seen:
                groups.append(("mac-seq", g))
    if not groups:
        print("  no multi-MAC devices detected"
              + ("" if args.by_mac else " by shared hostname (try --by-mac)"))
        return

    applied = 0
    for method, grp in groups:
        host = next((r["Hostname"] for r in grp if r["Hostname"]), "")
        names = sorted({r["Device"] for r in grp if r["Device"]})
        suggested = names[0] or host if names else host
        suggested = suggested or grp[0]["Hostname"] or grp[0]["MAC"]
        links = sorted({link_class(r["Link"]) for r in grp if link_class(r["Link"])})
        conf = "strong" if len(links) > 1 else ("ok" if method == "hostname" else "weak")
        print(f"\n  [{method}/{conf}] {host or grp[0]['Vendor']}"
              + (f"  (named: {', '.join(names)})" if names else ""))
        for r in grp:
            print(f"    {r['MAC']}  {r['IP']:15} {link_class(r['Link']) or '—':9} "
                  f"{r['Vendor'][:22]:22} {r['Device'] or '(unnamed)'}")
        macs = " ".join(r["MAC"] for r in grp)
        if args.apply:
            for r in grp:
                r["Device"] = suggested
            applied += 1
            print(f"    -> named all as '{suggested}'")
        else:
            print(f'    ./netinv.py name "{suggested}" {macs}')
    if args.apply:
        save_master(master)
        write_reports(master)
        print(f"\n  applied {applied} grouping(s)")


def cmd_name(args):
    master = load_master()
    macs = [norm_mac(m) for m in args.macs]
    changed = 0
    for mac in macs:
        if mac not in master:
            master[mac] = {k: "" for k in FIELDS}
            master[mac]["MAC"] = mac
            master[mac]["Source"] = "manual"
        master[mac]["Device"] = args.device
        changed += 1
    save_master(master)
    write_reports(master)
    print(f"  set Device='{args.device}' on {changed} MAC(s)")


def cmd_report(args):
    master = load_master()
    # refresh status against STALE_DAYS without scanning
    for row in master.values():
        if row["Status"] != "online":
            row["Status"] = stale_status(row["LastSeen"])
    save_master(master)
    write_reports(master)


def cmd_set_router_password(args):
    """Store the router login in the macOS Keychain as an internet-password."""
    import getpass
    user = args.user or input("Router username [admin]: ").strip() or "admin"
    pw = getpass.getpass(f"Router password for {ROUTER_HOST} (account '{user}'): ")
    if not pw:
        print("  aborted: empty password")
        return
    r = subprocess.run(
        ["security", "add-internet-password", "-s", ROUTER_HOST,
         "-a", user, "-w", pw, "-U", "-l", f"{ROUTER_HOST} (router admin)"],
        capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  stored router credentials for {ROUTER_HOST} in Keychain (account '{user}')")
    else:
        print(f"  ! failed to store: {r.stderr.strip()}")


CLOUD_REF = os.path.join(HERE, "cloud_devices.csv")


def cmd_set_cloud(args):
    """Store a cloud connector's credentials in the macOS Keychain."""
    import getpass
    import connectors
    prov = args.provider
    if prov not in connectors.PROVIDERS:
        print(f"  unknown provider '{prov}'. Options: {', '.join(connectors.PROVIDERS)}")
        return
    for field, is_secret, prompt in connectors.PROVIDERS[prov]:
        val = getpass.getpass(f"{prompt}: ") if is_secret else input(f"{prompt}: ").strip()
        if not val:
            print(f"  aborted: '{field}' empty")
            return
        connectors.kc_set(prov, field, val)
    print(f"  stored {prov} credentials in Keychain (service netinv-cloud-{prov})")


def cloud_merge(master, items):
    """Merge cloud connector results into the inventory. Cloud names are the
    user's own app names, so they fill a blank Device and update Hostname.
    Unmatched (no MAC/IP) items go to cloud_devices.csv as a reference list."""
    ip2mac = arp_table()
    matched = 0
    ref = []
    for it in items:
        mac = norm_mac(it["mac"]) if it["mac"] else ""
        if mac.count(":") != 5:
            mac = ip2mac.get(it["ip"], "")
        if not mac:
            ref.append(it)
            continue
        row = master.get(mac)
        if row is None:
            row = {k: "" for k in FIELDS}
            row["MAC"] = mac
            master[mac] = row
        if it["ip"]:
            row["IP"] = it["ip"]
        if name_quality(it["name"]) > name_quality(row["Hostname"]):
            row["Hostname"] = it["name"]
        if not row["Device"] and it["name"]:        # adopt the app's name
            row["Device"] = it["name"]
        srcs = set(filter(None, row["Source"].split("+"))) | {it["provider"]}
        row["Source"] = "+".join(sorted(srcs))
        matched += 1

    # write the reference list of name-only devices
    with open(CLOUD_REF, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Provider", "Name", "Model", "MAC", "IP"])
        for it in ref:
            w.writerow([it["provider"], it["name"], it["model"], it["mac"], it["ip"]])
    print(f"  cloud: matched {matched} to MACs; {len(ref)} name-only -> {CLOUD_REF}")
    return matched


def cmd_cloud(args):
    import connectors
    providers = args.providers or connectors.configured_providers()
    if not providers:
        print("  no cloud connectors configured. Run: ./netinv.py set-cloud <provider>")
        return
    items = connectors.discover(providers)
    master = load_master()
    cloud_merge(master, items)
    save_master(master)
    write_reports(master)
    unnamed = sum(1 for r in master.values() if not r["Device"])
    print(f"\n  {len(master)} MACs in {MASTER}  ·  {unnamed} still unnamed")


def cmd_show(args):
    master = load_master()
    q = (args.query or "").lower()
    rows = sorted(master.values(),
                  key=lambda r: (r["Device"] == "", r["Device"].lower(), ip_key(r["IP"])))
    for r in rows:
        if q and q not in r["Device"].lower() and q not in r["Hostname"].lower() \
                and q not in r["MAC"].lower():
            continue
        print(f"{r['Device'] or '(unnamed)':28} {r['IP']:15} {r['MAC']:18} "
              f"{r['Status']:8} {r['Vendor'][:24]:24} {r['Hostname']}")


def main():
    p = argparse.ArgumentParser(description="home network device inventory")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="gather sources, merge, write reports")
    u.add_argument("--scan", action="store_true", help="run local subnet scan")
    u.add_argument("--no-sweep", action="store_true", help="skip nmap, use existing ARP cache")
    u.add_argument("--router", action="store_true", help="scrape the Xfinity gateway")
    u.add_argument("--no-ssdp", action="store_true", help="skip SSDP/UPnP name discovery")
    u.add_argument("--cloud", action="store_true",
                   help="also pull names from configured smart-home clouds")
    u.add_argument("--auto-name", action="store_true",
                   help="fill blank friendly names from high-quality discovered names")
    u.add_argument("--import-csv", nargs="?", const=DEFAULT_IMPORT, default=None,
                   metavar="PATH", help=f"import old export (default {DEFAULT_IMPORT})")
    u.set_defaults(func=cmd_update)

    n = sub.add_parser("name", help="assign MAC(s) to a friendly device name")
    n.add_argument("device")
    n.add_argument("macs", nargs="+")
    n.set_defaults(func=cmd_name)

    r = sub.add_parser("report", help="rebuild reports from devices.csv")
    r.set_defaults(func=cmd_report)

    sp = sub.add_parser("set-router-password", help="store router login in macOS Keychain")
    sp.add_argument("--user", help="router username (default admin)")
    sp.set_defaults(func=cmd_set_router_password)

    wf = sub.add_parser("wifi-scan", help="classify a Wi-Fi survey as mine vs neighbor")
    wf.add_argument("file", nargs="?", default="-",
                    help="scan file (default: read pasted text from stdin)")
    wf.add_argument("--add-mine", action="store_true",
                    help="add BSSIDs identified as mine into the inventory")
    wf.set_defaults(func=cmd_wifi_scan)

    pr = sub.add_parser("pairs", help="show MACs likely to be the same device (wired+wifi)")
    pr.add_argument("--by-mac", action="store_true",
                    help="also use the looser same-OUI sequential-MAC heuristic")
    pr.add_argument("--apply", action="store_true", help="assign each group one shared name")
    pr.set_defaults(func=cmd_pairs)

    cl = sub.add_parser("cloud", help="pull device names from smart-home clouds")
    cl.add_argument("providers", nargs="*",
                    help="tuya wyze blink smartthings apple (default: all configured)")
    cl.set_defaults(func=cmd_cloud)

    sc = sub.add_parser("set-cloud", help="store a cloud connector's credentials in Keychain")
    sc.add_argument("provider", help="tuya | wyze | blink | smartthings | apple")
    sc.set_defaults(func=cmd_set_cloud)

    s = sub.add_parser("show", help="print current inventory")
    s.add_argument("query", nargs="?")
    s.set_defaults(func=cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
