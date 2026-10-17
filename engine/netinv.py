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
  - SSDP/UPnP and mDNS/DNS-SD name discovery  [--no-ssdp / --no-mdns to skip]
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
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))     # code + read-only assets
# Writable data lives in DATA_DIR (defaults to the code dir, so running from a
# clone is unchanged; an installed copy can point NETINV_HOME elsewhere).
DATA_DIR = os.environ.get("NETINV_HOME") or HERE
os.makedirs(DATA_DIR, exist_ok=True)
MASTER = os.path.join(DATA_DIR, "devices.csv")
REPORT_MD = os.path.join(DATA_DIR, "report.md")
REPORT_BY_DEVICE = os.path.join(DATA_DIR, "report-by-device.csv")
REPORT_XLSX = os.path.join(DATA_DIR, "inventory.xlsx")
HOME_SSIDS_FILE = os.path.join(DATA_DIR, "home_ssids.txt")
WIFI_SCAN_OUT = os.path.join(DATA_DIR, "wifi_scan.csv")
EVENTS_LOG = os.path.join(DATA_DIR, "events.csv")
ROOMS_CSV = os.path.join(DATA_DIR, "rooms.csv")      # docs/home-standard.md
HOME_CSV = os.path.join(DATA_DIR, "home.csv")
APP_ROOMS_CSV = os.path.join(DATA_DIR, "app_rooms.csv")  # written by `home rooms`
DEFAULT_IMPORT = os.path.expanduser("~/Documents/DevicesMacs.csv")

def _load_config():
    """Optional INI at $NETINV_CONFIG or ~/.netinv/config, section [netinv]."""
    import configparser
    path = os.environ.get("NETINV_CONFIG", os.path.expanduser("~/.netinv/config"))
    if not os.path.exists(path):
        return {}
    p = configparser.ConfigParser()
    try:
        p.read(path)
        return dict(p.items("netinv")) if p.has_section("netinv") else {}
    except (configparser.Error, OSError):
        return {}


_CONFIG = _load_config()


def _cfg(key, env, default):
    """Resolve a setting: env var wins, then config file, then default."""
    return os.environ.get(env) or _CONFIG.get(key) or default


ROUTER_HOST = _cfg("router_host", "NETINV_ROUTER", "192.168.1.1")
SUBNET = _cfg("subnet", "NETINV_SUBNET", "192.168.1.0/24")
# Cached gateway session (reused across runs to avoid re-login lockouts).
ROUTER_COOKIES = os.path.join(DATA_DIR, "router_cookies.txt")
ROUTER_COOLDOWN_FILE = os.path.join(DATA_DIR, ".router_cooldown")
ROUTER_COOLDOWN_MIN = int(_cfg("router_cooldown_min", "NETINV_ROUTER_COOLDOWN_MIN", "30"))
# A device not seen for this many days is reported as "defunct".
STALE_DAYS = int(_cfg("stale_days", "NETINV_STALE_DAYS", "30"))
# events.csv join-history retention.
EVENTS_RETENTION_DAYS = int(_cfg("events_retention_days",
                                 "NETINV_EVENTS_RETENTION_DAYS", "365"))


def subnet_prefix(subnet=None):
    """'192.168.1.0/24' -> '192.168.1.' (the /24 host prefix used for LAN checks)."""
    net = (subnet or SUBNET).split("/")[0]
    parts = net.split(".")
    return ".".join(parts[:3]) + "." if len(parts) == 4 else "192.168.1."

FIELDS = ["Device", "MAC", "IP", "Vendor", "Hostname", "Link", "Status",
          "FirstSeen", "LastSeen", "Source", "Notes", "RSSI"]
TODAY = dt.date.today().isoformat()


def now_stamp():
    return dt.datetime.now().isoformat(timespec="seconds")

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
    return bool(ip) and ip.startswith(subnet_prefix()) and not ip.endswith(".255")


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
            # backfill FirstSeen for rows that predate first-seen tracking
            if not rows[mac]["FirstSeen"]:
                rows[mac]["FirstSeen"] = rows[mac]["LastSeen"]
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
    # `arp` lives in /usr/sbin on Debian/Raspberry Pi OS, which is NOT on a
    # non-login shell's PATH — so a bare "arp" raises FileNotFoundError, the
    # except below swallows it, and the scan reports "0 hosts" as though the
    # network were empty. Look the binary up explicitly instead of trusting PATH.
    arp_bin = shutil.which("arp") or next(
        (p for p in ("/usr/sbin/arp", "/sbin/arp", "/usr/bin/arp") if os.path.exists(p)), None
    )
    if not arp_bin:
        print("  ! arp not found (looked on PATH, /usr/sbin, /sbin, /usr/bin)")
        return table
    try:
        arp = subprocess.run([arp_bin, "-an"], capture_output=True, text=True, timeout=15).stdout
    except Exception as e:  # noqa: BLE001
        print(f"  ! arp failed: {e}")
        return table
    for line in arp.splitlines():
        # Two output formats, and they differ in a way that matters:
        #   BSD/macOS    ? (10.0.0.42) at aa:bb:cc:00:11:22 on en0 ifscope [ethernet]
        #   GNU net-tools ? (10.0.0.42) at aa:bb:cc:00:11:22 [ether] on wlan0
        # The Linux form puts "[ether]" between the MAC and "on", so the older
        # pattern `at (...) on` matched nothing at all on Linux — every scan
        # silently returned an empty table. Anchor on the MAC's shape instead of
        # on the word that follows it. The {11,17} bound keeps "<incomplete>"
        # out without needing a separate check.
        m_ip = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", line)
        m_mac = re.search(r"at ([0-9a-fA-F:]{11,17})\b", line)
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


# Common DNS-SD service types worth probing directly. The meta-query
# (_services._dns-sd._udp.local) enumerates whatever else is advertised, but
# asking for the popular ones by name makes discovery faster and more reliable.
_MDNS_SERVICES = [
    "_services._dns-sd._udp.local",   # meta-query: list all advertised services
    "_airplay._tcp.local",            # Apple TV / AirPlay
    "_raop._tcp.local",               # AirPlay audio (HomePod, receivers)
    "_googlecast._tcp.local",         # Chromecast / Google / Nest
    "_spotify-connect._tcp.local",
    "_ipp._tcp.local", "_ipps._tcp.local", "_printer._tcp.local",  # printers
    "_hap._tcp.local", "_homekit._tcp.local",   # HomeKit accessories
    "_companion-link._tcp.local",     # Apple devices
    "_amzn-wplay._tcp.local",         # Amazon devices
    "_matter._tcp.local", "_matterc._udp.local",
    "_http._tcp.local", "_workstation._tcp.local",
]


def _dns_encode_name(name):
    out = b""
    for label in name.split("."):
        if label:
            b = label.encode("utf-8")
            out += bytes([len(b)]) + b
    return out + b"\x00"


def _dns_read_name(buf, off):
    """Parse a (possibly compression-pointer'd) DNS name.
    Return (labels, offset-past-name-in-the-record-stream)."""
    labels, jumped, next_off, steps = [], False, off, 0
    while off < len(buf):
        length = buf[off]
        if length == 0:
            off += 1
            if not jumped:
                next_off = off
            break
        if length & 0xC0 == 0xC0:              # compression pointer
            if off + 1 >= len(buf):
                break
            ptr = ((length & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                next_off = off + 2
            off, jumped, steps = ptr, True, steps + 1
            if steps > 64:                     # guard against pointer loops
                break
            continue
        off += 1
        labels.append(buf[off:off + length].decode("utf-8", "replace"))
        off += length
    return labels, next_off


def _parse_mdns(buf, a_recs, srv, txt, instances, ports=None):
    """Accumulate A / PTR / SRV / TXT records from one mDNS response packet.
    `ports`, if given, collects each SRV record's port."""
    if len(buf) < 12:
        return
    qd, an, ns, ar = struct.unpack(">HHHH", buf[4:12])
    off = 12
    for _ in range(qd):                        # skip questions
        _, off = _dns_read_name(buf, off)
        off += 4
    for _ in range(an + ns + ar):
        name_labels, off = _dns_read_name(buf, off)
        if off + 10 > len(buf):
            return
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        rend = off + rdlen
        low = ".".join(name_labels).lower()
        if rtype == 1 and rdlen == 4:                       # A record
            ip = ".".join(str(b) for b in buf[off:off + 4])
            if is_lan_ip(ip):
                a_recs[low] = ip
        elif rtype == 12:                                   # PTR -> instance
            inst_labels, _ = _dns_read_name(buf, off)
            if inst_labels and not inst_labels[0].startswith("_"):
                instances.setdefault(".".join(inst_labels).lower(), inst_labels[0])
        elif rtype == 33 and rdlen >= 7:                    # SRV -> target host
            tgt_labels, _ = _dns_read_name(buf, off + 6)
            srv[low] = ".".join(tgt_labels).lower()
            if ports is not None and off + 6 <= len(buf):   # truncated packet: no port
                ports[low] = struct.unpack(">H", buf[off + 4:off + 6])[0]
            if name_labels:
                instances.setdefault(low, name_labels[0])
        elif rtype == 16:                                   # TXT key=value pairs
            d, p = {}, off
            while p < rend:
                l = buf[p]
                p += 1
                if l == 0 or p + l > rend:
                    break
                kv = buf[p:p + l].decode("utf-8", "replace")
                p += l
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    d[k.lower()] = v
            if d:
                txt.setdefault(low, {}).update(d)
        off = rend


def mdns_discover(timeout=3):
    """mDNS / DNS-SD discovery (source 'mdns'). Multicast a PTR query for a set
    of common service types plus the DNS-SD meta-query, then parse the A/PTR/
    SRV/TXT replies. DNS-SD *instance* names are user-assigned (e.g. 'Living
    Room Apple TV') and TXT 'fn='/'md=' give friendly-name/model — all excellent
    discovered data, and unlike OUI they survive MAC randomization. IP -> MAC
    via the ARP table."""
    a_recs, srv, txt, instances, ports = {}, {}, {}, {}, {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(timeout)
        header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
        for svc in _MDNS_SERVICES:
            pkt = header + _dns_encode_name(svc) + struct.pack(">HH", 12, 1)  # PTR/IN
            try:
                s.sendto(pkt, ("224.0.0.251", 5353))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                break
            if is_lan_ip(addr[0]):
                try:
                    _parse_mdns(data, a_recs, srv, txt, instances, ports)
                except (struct.error, IndexError, ValueError):
                    pass                     # one malformed packet must not end the scan
    except OSError as e:
        print(f"  ! mDNS failed: {e}")
        return []
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass

    ip2mac = arp_table()
    spotify = _spotify_names({
        inst: (a_recs.get(srv.get(inst)), ports.get(inst), txt.get(inst, {}).get("cpath", "/"))
        for inst in instances if inst.endswith("._spotify-connect._tcp.local")})
    out, seen_ip = [], set()
    # 1) service instances -> friendly name + model, resolved to an IP via SRV/A
    for inst_low, label in instances.items():
        host = srv.get(inst_low)
        ip = a_recs.get(host) if host else None
        if not ip:
            continue
        t = dict(txt.get(inst_low, {}))
        if inst_low in spotify:                  # getInfo name beats 'SpotifyConnect #4'
            t["fn"], t["md"] = spotify[inst_low]
        name = t.get("fn") or t.get("n") or label            # fn= beats a UUID label
        # AirPlay/RAOP instances are 'DEADBEEF1234@Living Room' -> keep the label.
        name = re.sub(r"^[0-9A-Fa-f]{6,}@", "", name).strip()
        # md=/am= is a model string; skip _raop's numeric 'md=0,1,2' metadata list.
        model = _clean_model(t.get("md") or t.get("model") or t.get("am") or "")
        mac = ip2mac.get(ip)
        if mac and name and not _looks_like_id(name):
            # model is carried separately: it fills an *empty* Vendor but must
            # never clobber the authoritative OUI manufacturer (see merge()).
            out.append({"mac": mac, "ip": ip, "vendor": "", "model": model,
                        "hostname": name, "source": "mdns", "online": True})
            seen_ip.add(ip)
    # 2) plain A-record hostnames for IPs no service instance covered
    for host, ip in a_recs.items():
        if ip in seen_ip:
            continue
        name = host.split(".")[0]
        mac = ip2mac.get(ip)
        if mac and name and not _looks_like_id(name):
            out.append({"mac": mac, "ip": ip, "vendor": "", "hostname": name,
                        "source": "mdns", "online": True})
            seen_ip.add(ip)
    print(f"  mDNS discovered {len(out)} named devices")
    return out


def _spotify_names(targets, timeout=2):
    """{instance: (name, model)} from each Spotify Connect speaker's zeroconf
    getInfo. targets is {instance: (ip, port, cpath)}. Speakers that report
    their own name (receivers, TVs) give it; Echos leave remoteName blank, so
    the brand + model ('Amazon Echo Dot with clock') stands in."""
    import json as _json
    from concurrent.futures import ThreadPoolExecutor

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):   # a speaker never redirects; refuse
            return None                         # so a reply can't point us off-LAN
    opener = urllib.request.build_opener(_NoRedirect)

    def one(item):
        inst, (ip, port, cpath) = item
        if not ip or not is_lan_ip(ip) or not 0 < (port or 0) < 65536 \
                or not re.fullmatch(r"/?[A-Za-z0-9_\-/]*", cpath or "") or ".." in cpath:
            return inst, None
        url = f"http://{ip}:{port}/{cpath.lstrip('/')}?action=getInfo"
        try:
            with opener.open(url, timeout=timeout) as r:
                info = _json.loads(r.read(65536))
        except Exception:  # noqa: BLE001 — a silent speaker just keeps its label
            return inst, None
        if not isinstance(info, dict):
            return inst, None
        model = " ".join(str(info.get("modelDisplayName") or "").replace("_", " ").split())
        brand = " ".join(str(info.get("brandDisplayName") or "").split())
        brand = brand[:1].upper() + brand[1:]
        name = " ".join(str(info.get("remoteName") or "").split())
        if not name and model:
            name = model if model.lower().startswith(brand.lower()) else f"{brand} {model}".strip()
        return inst, ((name, model) if name else None)

    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return {k: v for k, v in pool.map(one, targets.items()) if v}


def _clean_model(s):
    """A TXT model value is only useful if it names something. Reject pure
    digit/comma lists (e.g. _raop 'md=0,1,2' metadata formats, not a model)."""
    s = (s or "").strip()
    return s if re.search(r"[A-Za-z]", s) else ""


def _looks_like_id(name):
    """True for opaque machine IDs (long hex blobs / UUIDs) that make poor names."""
    n = (name or "").strip()
    return bool(re.fullmatch(r"[0-9A-Fa-f]{8,}(-[0-9A-Fa-f]{4,})*", n)) \
        or bool(re.fullmatch(r"[0-9A-Fa-f-]{25,}", n))


def _router_valid(html):
    """A real connected-devices page carries the device JS arrays."""
    return "onlineHostMAC" in html or "offlineHostMAC" in html


def _router_cooldown_remaining():
    """Minutes left on the self-imposed lockout cooldown (0 = clear)."""
    if not os.path.exists(ROUTER_COOLDOWN_FILE):
        return 0
    try:
        t = dt.datetime.fromisoformat(open(ROUTER_COOLDOWN_FILE).read().strip())
    except (ValueError, OSError):
        return 0
    left = ROUTER_COOLDOWN_MIN - (dt.datetime.now() - t).total_seconds() / 60
    return max(0, int(left + 0.999))


def router_scan(user, password):
    """Scrape the Xfinity/Technicolor gateway's connected-devices page.

    Session reuse: the authenticated cookie is cached to router_cookies.txt and
    tried first on every run, so we log in only when the session has expired.
    Repeated logins trip the gateway's brute-force lockout — this avoids that.
    On a detected lockout we back off for ROUTER_COOLDOWN_MIN minutes.
    """
    cd = _router_cooldown_remaining()
    if cd > 0:
        print(f"  ! router in lockout cooldown (~{cd} min left); skipping login")
        return []

    base = f"http://{ROUTER_HOST}"
    cj = http.cookiejar.MozillaCookieJar(ROUTER_COOKIES)
    if os.path.exists(ROUTER_COOKIES):
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
        except Exception:  # noqa: BLE001
            pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 netinv")]

    # 1) Try the cached session first — no login.
    html = ""
    try:
        html = opener.open(f"{base}/connected_devices_computers.jst",
                           timeout=15).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        html = ""

    if _router_valid(html):
        print("  router: reusing cached session (no login)")
    else:
        # 2) Session missing/expired -> log in once.
        if not password:
            print("  ! router: no password available; skipping")
            return []
        try:
            opener.open(f"{base}/index.jst", timeout=10).read()
            data = urllib.parse.urlencode(
                {"username": user, "password": password, "locale": "false"}).encode()
            opener.open(f"{base}/check.jst", data=data, timeout=10).read()
            html = opener.open(f"{base}/connected_devices_computers.jst",
                               timeout=15).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            print(f"  ! router login/scrape failed: {e}")
            return []
        if not _router_valid(html):
            if "locked" in html.lower():
                with open(ROUTER_COOLDOWN_FILE, "w") as f:
                    f.write(now_stamp())
                print(f"  ! router account LOCKED (too many logins). Backing off "
                      f"{ROUTER_COOLDOWN_MIN} min; the cached session will be reused after.")
            else:
                print("  ! router login failed (no device data — check password)")
            return []
        try:
            cj.save(ignore_discard=True, ignore_expires=True)
            os.chmod(ROUTER_COOKIES, 0o600)
        except Exception:  # noqa: BLE001
            pass
        print("  router: logged in (session cached for reuse)")

    # success -> clear any stale cooldown marker
    if os.path.exists(ROUTER_COOLDOWN_FILE):
        try:
            os.remove(ROUTER_COOLDOWN_FILE)
        except OSError:
            pass

    with open(os.path.join(DATA_DIR, "router_devices_raw.html"), "w") as f:
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

    # MAC -> Wi-Fi signal strength ('-NN dBm') from the per-device rssi-level
    # cell. The lookahead keeps the match inside one device block (Ethernet
    # clients show a non-numeric cell and are skipped, leaving rssi absent).
    rssi_by_mac = {}
    for m in re.finditer(
            r"MAC Address</b><br/?></dd>\s*(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})"
            r"(?:(?!MAC Address</b>).)*?headers='rssi-level'[^>]*>\s*(-?\d+)\s*dBm",
            html, re.S):
        rssi_by_mac[norm_mac(m.group(1))] = int(m.group(3))

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
                "rssi": rssi_by_mac.get(mac),
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
            or low.startswith(("wlan0", "lwip", "espressif")) \
            or re.fullmatch(r"(stream)?spotifyconnect( ?[#(]\d+\)?)?", low):
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
            row["FirstSeen"] = now_stamp()        # brand-new device
            master[mac] = row
        row["IP"] = better(s["ip"], row["IP"])
        row["Vendor"] = better(s["vendor"], row["Vendor"])
        # A discovered model (e.g. mDNS 'md=Mac15,11') is fill-only: it names a
        # device whose MAC is randomized so OUI found nothing, but it must not
        # override a real manufacturer that OUI/router already supplied.
        if s.get("model") and not row["Vendor"]:
            row["Vendor"] = s["model"]
        row["Link"] = better(s.get("link", ""), row["Link"])
        # RSSI is a live per-scan reading from the gateway; refresh when present.
        if s.get("rssi") is not None:
            row["RSSI"] = str(s["rssi"])
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


def band(link):
    """Extract the radio band (or Ethernet) from the router's connection medium:
    '2.4 GHz' / '5 GHz' / '6 GHz' / 'Ethernet' / 'Wi-Fi' / ''."""
    l = (link or "").lower()
    if "ethernet" in l or "eth" in l:
        return "Ethernet"
    m = re.search(r"(2\.4|5|6)\s*ghz", l)
    if m:
        return m.group(1) + " GHz"
    if "wi-fi" in l or "wifi" in l or "wlan" in l:
        return "Wi-Fi (band?)"
    return ""


# --------------------------------------------------------------------------- #
# OUI vendor lookup (offline, via nmap's prefix database)
# --------------------------------------------------------------------------- #
_OUI_DB = None


def _oui_db():
    """Load {6-hex-prefix: vendor}. Prefer the bundled gzip (works without nmap
    installed); fall back to a system nmap-mac-prefixes file."""
    global _OUI_DB
    if _OUI_DB is not None:
        return _OUI_DB
    _OUI_DB = {}
    import glob
    import gzip

    def _ingest(lines):
        for line in lines:
            m = re.match(r"([0-9A-Fa-f]{6})\s+(.+)", line)
            if m:
                _OUI_DB[m.group(1).upper()] = m.group(2).strip()

    bundled = os.path.join(HERE, "data", "oui-prefixes.txt.gz")
    if os.path.exists(bundled):
        try:
            with gzip.open(bundled, "rt", encoding="utf-8", errors="replace") as f:
                _ingest(f)
            if _OUI_DB:
                return _OUI_DB
        except OSError:
            pass
    cands = (glob.glob("/opt/homebrew/Cellar/nmap/*/share/nmap/nmap-mac-prefixes")
             + ["/opt/homebrew/share/nmap/nmap-mac-prefixes",
                "/usr/local/share/nmap/nmap-mac-prefixes",
                "/usr/share/nmap/nmap-mac-prefixes"])
    path = next((p for p in cands if os.path.exists(p)), None)
    if path:
        with open(path, encoding="utf-8", errors="replace") as f:
            _ingest(f)
    return _OUI_DB


def is_local_admin_mac(mac):
    """True if the MAC is locally-administered (randomized/private — no real OUI)."""
    try:
        return bool(int(mac[:2], 16) & 0x2)
    except (ValueError, IndexError):
        return False


def oui_lookup(mac):
    """Manufacturer for a MAC via OUI, or '' (randomized MACs have no real OUI)."""
    if not mac or is_local_admin_mac(mac):
        return ""
    return _oui_db().get(mac.replace(":", "")[:6].upper(), "")


def enrich_vendors(master):
    """Fill a blank Vendor from the MAC's OUI. Returns how many were filled."""
    n = 0
    for row in master.values():
        if not row["Vendor"]:
            v = oui_lookup(row["MAC"])
            if v:
                row["Vendor"] = v
                n += 1
    return n


# --------------------------------------------------------------------------- #
# new-device detection / event log
# --------------------------------------------------------------------------- #
def prune_events():
    """Drop events older than EVENTS_RETENTION_DAYS so the log stays bounded.
    Rows with an unparseable timestamp are kept (fail-safe)."""
    if not os.path.exists(EVENTS_LOG) or EVENTS_RETENTION_DAYS <= 0:
        return
    cutoff = dt.date.today() - dt.timedelta(days=EVENTS_RETENTION_DAYS)
    try:
        with open(EVENTS_LOG, newline="") as f:
            rows = list(csv.reader(f, delimiter=";"))
    except OSError:
        return
    if len(rows) < 2:
        return
    header, data = rows[0], rows[1:]
    kept = []
    for r in data:
        try:
            keep = dt.date.fromisoformat(r[0][:10]) >= cutoff
        except (ValueError, IndexError):
            keep = True
        if keep:
            kept.append(r)
    if len(kept) != len(data):
        with open(EVENTS_LOG, "w", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            w.writerows(kept)


def log_events(rows, kind="join"):
    """Append device events to events.csv (append-only history of joins)."""
    if not rows:
        return
    newfile = not os.path.exists(EVENTS_LOG)
    ts = now_stamp()
    with open(EVENTS_LOG, "a", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if newfile:
            w.writerow(["Timestamp", "Event", "MAC", "IP", "Vendor",
                        "Hostname", "Band", "Device", "Source"])
        for r in rows:
            w.writerow([ts, kind, r["MAC"], r["IP"], r["Vendor"], r["Hostname"],
                        band(r["Link"]), r["Device"], r["Source"]])


def notify(title, message):
    """Best-effort macOS Notification Center banner."""
    try:
        msg = message.replace('"', "'").replace("\\", "")
        ttl = title.replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{ttl}" sound name "Submarine"'],
            capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001
        pass


def report_new(master, before_macs, do_notify=False):
    """Print, log, and optionally notify about MACs not present before this run."""
    new = [master[m] for m in master if m not in before_macs]
    if not new:
        return new
    log_events(new, kind="join")
    print(f"\n  *** {len(new)} NEW device(s) joined the network ***")
    for r in new:
        print(f"    + {(r['IP'] or '(no ip)'):15} {r['MAC']}  "
              f"{(band(r['Link']) or '—'):9} {(r['Vendor'][:24] or '?'):24} "
              f"{r['Hostname'] or r['Device'] or ''}")
    if do_notify:
        for r in new:
            label = r["Device"] or r["Hostname"] or r["Vendor"] or r["MAC"]
            notify("New device on network", f"{label} ({r['IP'] or r['MAC']})")
    print(f"  logged to {EVENTS_LOG}")
    return new


# --------------------------------------------------------------------------- #
# alert-rule engine  (pure: evaluate rules over the inventory)
# --------------------------------------------------------------------------- #
ALERT_RULES = [
    ("unknown-online", "high",
     "Unidentified device online (no vendor, unnamed)",
     lambda r: r["Status"] == "online" and not r["Vendor"] and not r["Device"]),
    ("new-today", "high", "Device first seen today",
     lambda r: (r.get("FirstSeen") or "")[:10] == TODAY),
    ("unnamed-online", "med", "Online device with no friendly name",
     lambda r: r["Status"] == "online" and not r["Device"]),
    ("named-offline", "low", "Known device is offline",
     lambda r: bool(r["Device"]) and r["Status"] in ("offline", "defunct")),
    ("weak-signal", "med", "Weak Wi-Fi signal (<= -75 dBm) — drops/DHCP failures likely",
     lambda r: r["Status"] == "online" and signal_class(rssi_of(r)) == "weak"),
]


def evaluate_alerts(master, rules=ALERT_RULES):
    """Return alert dicts for rows matching each rule. Pure and testable."""
    out = []
    for r in master.values():
        for rid, sev, desc, pred in rules:
            try:
                if pred(r):
                    out.append({
                        "rule": rid, "severity": sev, "desc": desc,
                        "device": r["Device"] or r["Hostname"] or "(unnamed)",
                        "mac": r["MAC"], "ip": r["IP"], "band": band(r["Link"]),
                        "rssi": rssi_of(r),
                    })
            except Exception:  # noqa: BLE001
                pass
    order = {"high": 0, "med": 1, "low": 2}
    return sorted(out, key=lambda a: order.get(a["severity"], 9))


def signal_class(rssi):
    """Bucket a Wi-Fi RSSI (dBm) into good / fair / weak for at-a-glance UI.
    None (wired or unknown) -> ''. Thresholds follow common Wi-Fi guidance:
    >= -67 solid, -68..-74 usable, <= -75 marginal (drops, slow DHCP)."""
    if rssi is None:
        return ""
    if rssi >= -67:
        return "good"
    if rssi >= -74:
        return "fair"
    return "weak"


def rssi_of(row):
    """Parse a master row's stored RSSI ('-80') into an int, or None if the
    device has no Wi-Fi signal reading (wired / never reported)."""
    v = str(row.get("RSSI", "")).strip()
    return int(v) if v.lstrip("-").isdigit() else None


def rssi_label(rssi):
    """Human 'RSSI dBm (bucket)' for reports, or '' when absent."""
    if rssi is None:
        return ""
    sc = signal_class(rssi)
    return f"{rssi} dBm ({sc})" if sc else f"{rssi} dBm"


def group_rssi(rows):
    """Weakest (lowest) RSSI across a device's rows, or None if none are Wi-Fi.
    A device may span a wired + Wi-Fi NIC; the weakest link is what drops."""
    vals = [v for v in (rssi_of(r) for r in rows) if v is not None]
    return min(vals) if vals else None


def api_devices(master):
    """Inventory as a list of JSON-ready dicts (sorted by IP)."""
    out = []
    for r in sorted(master.values(), key=lambda x: ip_key(x["IP"])):
        d = {k: r.get(k, "") for k in FIELDS}
        d["category"] = categorize(r)
        d["band"] = band(r["Link"])
        d["link_class"] = link_class(r["Link"])
        rssi = rssi_of(r)
        d["rssi"] = rssi
        d["signal"] = signal_class(rssi)
        out.append(d)
    return out


def api_summary(master):
    """Rollup counts for a dashboard."""
    devs = list(master.values())

    def tally(fn):
        c = {}
        for r in devs:
            k = fn(r) or "unknown"
            c[k] = c.get(k, 0) + 1
        return c

    return {
        "total": len(devs),
        "online": sum(1 for r in devs if r["Status"] == "online"),
        "unnamed": sum(1 for r in devs if not r["Device"]),
        "by_status": tally(lambda r: r["Status"]),
        "by_band": tally(lambda r: band(r["Link"]) or "unknown"),
        "by_category": tally(categorize),
    }


def device_history(query):
    """Join-history rows from events.csv for a device (match MAC or name)."""
    if not os.path.exists(EVENTS_LOG):
        return []
    q = query.lower()
    out = []
    with open(EVENTS_LOG, newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    for r in rows:
        hay = " ".join(str(v) for v in r.values()).lower()
        if q in hay:
            out.append(r)
    return out


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
    """Home SSID(s) from: NETINV_HOME_SSIDS env, config [netinv] home_ssids
    (both comma-separated), and home_ssids.txt (one per line)."""
    out = set()
    for src in (os.environ.get("NETINV_HOME_SSIDS", ""), _CONFIG.get("home_ssids", "")):
        out |= {s.strip().lower() for s in src.split(",") if s.strip()}
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
    cols = ["IP", "Device", "Category", "Link", "Band", "Signal", "Status", "MAC",
            "Vendor", "Hostname", "FirstSeen", "LastSeen", "Source", "Notes"]
    widths = [14, 28, 20, 10, 10, 16, 9, 19, 26, 24, 19, 12, 14, 24]
    for c, (name, wdt) in enumerate(zip(cols, widths)):
        ws.set_column(c, c, wdt)
        ws.write(0, c, name, hdr)
    for i, r in enumerate(rows, start=1):
        vals = [r["IP"], r["Device"], categorize(r), link_class(r["Link"]),
                band(r["Link"]), rssi_label(rssi_of(r)), r["Status"], r["MAC"],
                r["Vendor"], r["Hostname"],
                r["FirstSeen"], r["LastSeen"], r["Source"], r["Notes"]]
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
    cols2 = ["Device", "Category", "Status", "Signal", "Connection", "#MACs",
             "IP(s)", "MAC(s)", "Vendor", "LastSeen", "Notes"]
    widths2 = [28, 20, 12, 16, 22, 7, 30, 40, 26, 12, 24]
    for c, (name, wdt) in enumerate(zip(cols2, widths2)):
        ws2.set_column(c, c, wdt)
        ws2.write(0, c, name, hdr)
    grp_sorted = sorted(groups.items(),
                        key=lambda kv: ip_key(min((x["IP"] for x in kv[1] if x["IP"]),
                                                  default="")))
    for i, (name, grp) in enumerate(grp_sorted, start=1):
        disp = "" if name.startswith("(unnamed)") else name
        conns = sorted({band(x["Link"]) for x in grp if band(x["Link"])})
        vals = [disp, categorize(grp[0]),
                "/".join(sorted({x["Status"] for x in grp})),
                rssi_label(group_rssi(grp)),
                " + ".join(conns),
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
    nxt = table(nxt, "Devices by Band", tally(lambda r: band(r["Link"]) or "Unknown"))
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
    prune_events()   # keep the join history bounded
    # group by device (unnamed -> each MAC is its own group)
    groups = OrderedDict()
    for row in sorted(master.values(),
                      key=lambda r: (r["Device"] == "", r["Device"].lower(), ip_key(r["IP"]))):
        key = row["Device"] or f"(unnamed) {row['MAC']}"
        groups.setdefault(key, []).append(row)

    # by-device CSV
    with open(REPORT_BY_DEVICE, "w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Device", "Status", "Signal", "IPs", "MACs", "Vendors",
                    "Hostnames", "LastSeen", "Notes"])
        for name, rows in groups.items():
            disp = name if not name.startswith("(unnamed)") else ""
            w.writerow([
                disp,
                "/".join(sorted({r["Status"] for r in rows})),
                rssi_label(group_rssi(rows)),
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
        f.write("| Device | Status | Signal | IP(s) | MAC(s) | Vendor | Last seen | Notes |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for name, rows in named:
            f.write("| {} | {} | {} | {} | {} | {} | {} | {} |\n".format(
                name,
                "/".join(sorted({r["Status"] for r in rows})),
                rssi_label(group_rssi(rows)) or "—",
                "<br>".join(sorted({r["IP"] for r in rows if r["IP"]}, key=ip_key)),
                "<br>".join(r["MAC"] for r in rows),
                "<br>".join(sorted({r["Vendor"] for r in rows if r["Vendor"]})),
                max((r["LastSeen"] for r in rows), default=""),
                " ".join(sorted({r["Notes"] for r in rows if r["Notes"]})),
            ))
        f.write(f"\n## Unnamed MACs ({len(unnamed)}) — assign a friendly name\n\n")
        f.write("Use: `./netinv.py name \"Friendly Name\" <mac> [<mac> ...]`\n\n")
        f.write("| Status | Signal | IP | MAC | Vendor | Hostname |\n|---|---|---|---|---|---|\n")
        for _, rows in unnamed:
            r = rows[0]
            f.write(f"| {r['Status']} | {rssi_label(rssi_of(r)) or '—'} | {r['IP']} "
                    f"| {r['MAC']} | {r['Vendor']} | {r['Hostname']} |\n")
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
    overwrites a name you've set. Returns the number of devices auto-named.
    A name already taken gets a number ('Amazon Echo Dot 2'): one Device name
    is one physical device, so three Dots must not merge into one."""
    taken = {r["Device"].lower() for r in master.values() if r["Device"]}
    n = 0
    for row in master.values():
        if row["Device"]:
            continue
        if name_quality(row["Hostname"]) >= 3:
            name, i = row["Hostname"], 1
            while name.lower() in taken:
                i += 1
                name = f"{row['Hostname']} {i}"
            row["Device"] = name
            taken.add(name.lower())
            n += 1
    return n


def cmd_update(args):
    master = load_master()
    before = set(master)                 # MACs known before this run
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
        if not args.no_mdns:
            sightings += mdns_discover()

    seen = merge(master, sightings)
    filled = enrich_vendors(master)
    if filled:
        print(f"  filled {filled} vendor(s) via OUI lookup")
    if args.cloud:
        import connectors
        cloud_merge(master, connectors.discover())
    if args.auto_name:
        named = auto_name(master)
        print(f"  auto-named {named} device(s) from discovery")
    if args.fingerprint:
        # new joins first, then still-unnamed online devices not yet fingerprinted
        new = [master[m] for m in master if m not in before]
        todo = new + [r for r in master.values()
                      if not r["Device"] and r["Status"] == "online" and r["IP"]
                      and not r["Notes"].startswith("fp:") and r not in new]
        todo = todo[:args.fp_limit]
        if todo:
            print(f"  fingerprinting {len(todo)} device(s) (active nmap)...")
            fingerprint_rows(todo, fast=args.fp_fast, apply_notes=True, verbose=False)
    save_master(master)
    write_reports(master)
    # an imported export isn't a real "join", so only flag new devices when we
    # actually looked at the live network this run
    if do_local or args.router:
        report_new(master, before, do_notify=args.notify)
    print(f"\n  {len(master)} MACs in {MASTER}  ·  {len(seen)} online this run")
    unnamed = sum(1 for r in master.values() if not r["Device"])
    if unnamed:
        print(f"  {unnamed} MACs have no friendly name yet — see {REPORT_MD}")
    pairs = suggest_pairs(master)
    if pairs:
        print(f"  {len(pairs)} possible wired+wifi device(s) detected — "
              f"run ./netinv.py pairs")


def cmd_watch(args):
    """Poll the network on an interval and alert when a new device joins."""
    import time
    use_router = not args.no_router
    print(f"  watching {SUBNET} every {args.interval}s "
          f"(router={'on' if use_router else 'off'}, notify={'on' if args.notify else 'off'})"
          f" — Ctrl-C to stop")
    creds = get_router_creds() if use_router else (None, None)
    n = 0
    while True:
        n += 1
        master = load_master()
        before = set(master)
        sightings = []
        if use_router and creds[1]:
            sightings += router_scan(*creds)
        sightings += local_scan(do_sweep=not args.no_sweep)
        if not args.no_ssdp:
            sightings += ssdp_discover()
        if not args.no_mdns:
            sightings += mdns_discover()
        seen = merge(master, sightings)
        auto_name(master)
        save_master(master)
        write_reports(master)
        new = report_new(master, before, do_notify=args.notify)
        if not new:
            print(f"  [{now_stamp()}] poll #{n}: {len(seen)} online, no new devices")
        if args.once:
            break
        time.sleep(args.interval)


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


# --------------------------------------------------------------------------- #
# active fingerprinting (ports + service banners -> device-type guess)
# --------------------------------------------------------------------------- #
# port -> (service hint, device-type hint)
_PORT_HINTS = {
    554: ("RTSP", "IP camera"), 8554: ("RTSP", "IP camera"),
    8060: ("Roku ECP", "Roku / TV"), 8008: ("Google Cast", "Chromecast / Nest"),
    8009: ("Google Cast", "Chromecast / Nest"), 9999: ("TP-Link Kasa", "Kasa plug/switch"),
    6668: ("Tuya", "Tuya/SmartLife device"), 1883: ("MQTT", "IoT hub/broker"),
    8883: ("MQTT/TLS", "IoT hub/broker"), 5000: ("UPnP/AirPlay", "media/NAS"),
    7000: ("AirPlay", "Apple TV/HomePod"), 3689: ("DAAP", "Apple media"),
    631: ("IPP", "printer"), 9100: ("JetDirect", "printer"), 515: ("LPD", "printer"),
    32400: ("Plex", "media server"), 5353: ("mDNS", ""), 1900: ("SSDP", ""),
    22: ("SSH", "computer/server"), 3306: ("MySQL", "server"), 445: ("SMB", "computer/NAS"),
    62078: ("iOS-sync", "iPhone/iPad"), 49152: ("UPnP", "media/IoT"),
}


def fingerprint_host(ip, fast=True):
    """nmap service scan over a curated IoT port set + HTTP banner grab."""
    ports = "21,22,23,80,443,445,515,554,631,1883,3306,3689,5000,7000,8000,"\
            "8008,8009,8060,8080,8443,8554,8883,9100,9999,32400,49152,62078"
    info = {"ports": [], "services": [], "http": ""}
    try:
        out = subprocess.run(
            ["nmap", "-Pn", "-n", "-T4", "--open", "-p", ports]
            + (["-sV", "--version-light"] if not fast else []) + [ip],
            capture_output=True, text=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
        return info
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)/tcp\s+open\s+(\S+)\s*(.*)$", line)
        if not m:
            continue
        port = int(m.group(1))
        info["ports"].append(port)
        name, extra = m.group(2).strip(), m.group(3).strip()
        svc = f"{name} {extra}".strip()
        if svc and svc != "unknown":
            info["services"].append(f"{port}:{svc}")
    # HTTP banner (Server header + <title>)
    for port, scheme in ((80, "http"), (8080, "http"), (443, "https"), (8008, "http")):
        if port in info["ports"]:
            try:
                r = subprocess.run(
                    ["curl", "-sk", "-m", "4", "-D", "-", f"{scheme}://{ip}:{port}/"],
                    capture_output=True, text=True, timeout=6).stdout
                srv = re.search(r"^Server:\s*(.+)$", r, re.M | re.I)
                ttl = re.search(r"<title>([^<]{1,60})</title>", r, re.I)
                bits = [b.group(1).strip() for b in (srv, ttl) if b]
                if bits:
                    info["http"] = " | ".join(bits)
                    break
            except Exception:  # noqa: BLE001
                pass
    return info


def guess_type(vendor, info):
    """Heuristic device-type label from vendor + open ports + service/HTTP banners."""
    types, ports = [], set(info.get("ports", []))
    for p in ports:
        hint = _PORT_HINTS.get(p, ("", ""))[1]
        if hint:
            types.append(hint)
    blob = (vendor + " " + info.get("http", "") + " "
            + " ".join(info.get("services", []))).lower()
    for kw, t in (("broadband|gateway|router|openwrt|technicolor|vantiva", "router/gateway"),
                  ("iphone-sync", "iPhone/iPad"), ("roku|dial", "Roku / TV"),
                  ("hue|hap", "Hue / HomeKit"), ("epson|ipp|jetdirect|printer", "printer"),
                  ("dahua|hikvision|raysharp|rtsp|camera", "IP camera"),
                  ("synology|qnap|smb|netbios", "NAS / computer"),
                  ("sonos|airplay|spotify", "speaker / media"),
                  ("plex", "media server"), ("mqtt", "IoT hub"),
                  ("espressif|tuya|tasmota|smartlife", "ESP/smart-home device")):
        if re.search(kw, blob):
            types.append(t)
    # de-dup preserving order
    seen, out = set(), []
    for t in types:
        if t and t not in seen:
            seen.add(t); out.append(t)
    return ", ".join(out[:3])


def fingerprint_rows(rows, fast=False, apply_notes=False, verbose=True):
    """Fingerprint each row; optionally record findings to Notes. Returns
    [(row, info, guess)]. Notes are only set when there's a real signal, and
    they're prefixed 'fp:' so a later run can skip already-fingerprinted rows."""
    results = []
    for r in rows:
        if not r["IP"]:
            continue
        info = fingerprint_host(r["IP"], fast=fast)
        g = guess_type(r["Vendor"], info)
        results.append((r, info, g))
        if verbose:
            print(f"  {r['IP']:15} {r['MAC']}  {r['Vendor'][:22] or '?':22}")
            if info.get("ports"):
                print(f"      ports: {','.join(str(p) for p in sorted(info['ports']))}")
            if info.get("services"):
                print(f"      svc:   {'; '.join(info['services'][:5])}")
            if info.get("http"):
                print(f"      http:  {info['http']}")
            print(f"      ==> guess: {g or '(unclear — vendor only)'}\n")
        if apply_notes:
            parts = [f"fp: {r['Vendor'] or '?'}"]
            if g:
                parts.append(g)
            if info.get("ports"):
                parts.append("ports " + ",".join(str(p) for p in sorted(info["ports"])))
            r["Notes"] = "; ".join(parts)
    return results


def cmd_fingerprint(args):
    master = load_master()
    enrich_vendors(master)
    if args.target:
        t = norm_mac(args.target) if ":" in args.target and len(args.target) == 17 else args.target
        rows = [r for r in master.values() if r["MAC"] == t or r["IP"] == args.target]
    else:
        rows = [r for r in master.values()
                if not r["Device"] and r["Status"] == "online" and r["IP"]][:args.limit]
    if not rows:
        print("  nothing to fingerprint (give an IP/MAC, or run a scan first)")
        return
    print(f"  fingerprinting {len(rows)} device(s) — this runs active nmap scans\n")
    fingerprint_rows(rows, fast=args.fast, apply_notes=args.apply)
    if args.apply:
        save_master(master)
        write_reports(master)
        print("  wrote fingerprint hints to Notes")


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


MAX_NAME_LEN = 64


def apply_name(master, macs, device, *, create=True):
    """Assign a friendly Device name to one or more MACs. Pure over `master`
    (mutates it, returns the count changed). `device` is trimmed/capped; an
    empty string clears the name. When create is False, unknown MACs are
    skipped (used by the API, which only renames devices it already knows)."""
    device = (device or "").strip()[:MAX_NAME_LEN]
    changed = 0
    for raw in macs:
        mac = norm_mac(raw)
        if mac.count(":") != 5:
            continue
        if mac not in master:
            if not create:
                continue
            master[mac] = {k: "" for k in FIELDS}
            master[mac]["MAC"] = mac
            master[mac]["Source"] = "manual"
        master[mac]["Device"] = device
        changed += 1
    return changed


def cmd_name(args):
    master = load_master()
    changed = apply_name(master, args.macs, args.device)
    save_master(master)
    write_reports(master)
    print(f"  set Device='{args.device}' on {changed} MAC(s)")


def cmd_home(args):
    import home
    master = load_master()
    rooms = home.load_rooms(ROOMS_CSV)
    rows = home.load_home(HOME_CSV)
    if args.action == "rooms":
        import connectors
        if args.source != "tuya":
            print(f"  {args.source} has no rooms to import; rooms come from tuya")
            sys.exit(1)
        c = connectors.creds(args.source)
        if not c:
            print(f"  {args.source} is not configured; run: netinv set-cloud {args.source}")
            sys.exit(1)
        app = connectors.tuya_rooms(c)
        if not rooms:
            rooms = home.starter_rooms()
        added = home.merge_rooms(rooms, app)
        home.write_csv(ROOMS_CSV, home.ROOM_FIELDS, rooms)
        # Keep netinventory's name for a device already linked (it may have
        # been renamed on the board but not pushed yet); new ones take the app's.
        known = {a["Id"]: a["Device"] for a in home.load_app_rooms(APP_ROOMS_CSV) if a["Id"]}
        links = [{"App": args.source, "Device": known.get(i, d.strip()),
                  "Room": home.clean_room(r), "Id": i}
                 for r, devs in app.items() for d, i in devs]
        home.write_csv(APP_ROOMS_CSV, home.APP_ROOM_FIELDS, links)
        for r in added:
            print(f"  + {r['Room']}")
        print(f"  {args.source}: {len([r for r in app if r])} room(s), {len(links)} device(s); "
              f"added {len(added)} room(s) to {ROOMS_CSV} (set each Floor)")
        print("  To rename a room, keep the app's name in Aliases so devices still map.")
        return
    if args.action == "push":
        import connectors
        if args.source == "kasa":       # local: match by MAC, rename by IP
            plan = home.mac_push_plan(connectors.kasa_discover(), master.values(), rows)
            rename = lambda target, name: connectors.kasa_rename(target, name)  # noqa: E731
        else:
            c = connectors.creds(args.source)
            if not c:
                print(f"  {args.source} is not configured; run: netinv set-cloud {args.source}")
                sys.exit(1)
            app_rooms = home.load_app_rooms(APP_ROOMS_CSV)
            if not any(a["Id"] for a in app_rooms):
                print(f"  no {args.source} device ids yet; run: netinv home rooms --from {args.source}")
                sys.exit(1)
            plan = home.push_plan(args.source, app_rooms, rows, connectors.tuya_names(c))
            rename = lambda target, name: connectors.tuya_rename(c, target, name)  # noqa: E731
        if args.device:
            plan = [p for p in plan if args.device in (p[1], p[2])]
        failed = 0
        for target, old, new in plan:
            if args.dry_run:
                print(f"  would rename '{old}' -> '{new}'")
                continue
            ok, msg = rename(target, new)
            failed += not ok
            print(f"  {'ok  ' if ok else 'FAIL'} '{old}' -> '{new}'" + ("" if ok else f": {msg}"))
        verb = "to rename" if args.dry_run else "renamed"
        print(f"  {args.source}: {len(plan) - failed} {verb}, {failed} failed")
        if failed:
            sys.exit(1)
        return
    if args.action == "draft":
        if not rooms:
            rooms = home.starter_rooms()
            home.write_csv(ROOMS_CSV, home.ROOM_FIELDS, rooms)
            print(f"  wrote {len(rooms)} starter rooms to {ROOMS_CSV} (set each Floor)")
        new = home.draft(master.values(), rooms, rows,
                         home.load_device_rooms(APP_ROOMS_CSV))
        home.write_csv(HOME_CSV, home.HOME_FIELDS, rows + new)
        todo = sum(1 for r in new if home.TODO in (r["Room"], r["Type"]))
        print(f"  added {len(new)} device(s) to {HOME_CSV}; {todo} need a room or type")
        return
    names = {r["Device"] for r in master.values() if r["Device"]}
    errors, warnings = home.check(rooms, rows, names)
    for w in warnings:
        print(f"  warn  {w}")
    for e in errors:
        print(f"  error {e}")
    print(f"  {len(rows)} device(s), {len(rooms)} room(s): "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    if errors:
        sys.exit(1)


def cmd_report(args):
    master = load_master()
    enrich_vendors(master)
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


CLOUD_REF = os.path.join(DATA_DIR, "cloud_devices.csv")


def cmd_blink_login(args):
    """Interactive Blink 2FA login (run in a real terminal)."""
    import connectors
    connectors.blink_login_interactive()


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
        if mac.count(":") != 5 and it["ip"]:
            mac = ip2mac.get(it["ip"], "")
        # Only enrich devices already on the network. A cloud account also lists
        # Zigbee/BLE sub-devices (no WiFi MAC) and devices whose id-derived MAC
        # we can't confirm -> those go to the reference list, never new rows.
        if not mac or mac not in master:
            ref.append(it)
            continue
        row = master[mac]
        if it["ip"]:
            row["IP"] = it["ip"]
        # >=: a name the user typed in an app ('Garage') beats an equally
        # scored network hostname ('HS200-47').
        if it["name"] and name_quality(it["name"]) >= name_quality(row["Hostname"]):
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


_DASHBOARD = """<!doctype html><meta charset=utf-8><title>netinventory</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--head:#1F4E78}
*{box-sizing:border-box}
body{font:13px system-ui;margin:1rem;color:#111}
h1{font-size:1.2rem;margin:.2rem 0}
#sum{color:#555;margin:.2rem 0 .6rem}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:.5rem}
.bar input,.bar select{padding:5px 8px;font:13px system-ui;border:1px solid #cdd;border-radius:6px}
#q{width:18rem;flex:0 1 18rem}
.chip{border:1px solid #ccd;border-radius:14px;padding:3px 10px;cursor:pointer;background:#fff;user-select:none}
.chip.on{background:var(--head);color:#fff;border-color:var(--head)}
.spacer{flex:1}
.btn{border:1px solid #ccd;border-radius:6px;padding:4px 9px;background:#fff;cursor:pointer}
.wrap{overflow-x:auto;border:1px solid #eee;border-radius:6px}
table{border-collapse:collapse;width:100%}
th,td{padding:4px 8px;border-bottom:1px solid #eee;text-align:left;white-space:nowrap}
thead th{background:var(--head);color:#fff;position:sticky;top:0;cursor:pointer;z-index:2;user-select:none}
th .ar{font-size:10px;opacity:.95}
th .pri{font-size:9px;background:#ffffff3d;border-radius:6px;padding:0 4px;margin-left:2px}
tr.f th{position:sticky;top:26px;background:#eef2f8;z-index:1;padding:2px 6px}
tr.f input,tr.f select{width:100%;min-width:70px;padding:2px 4px;font:12px system-ui;border:1px solid #cdd;border-radius:4px}
tbody tr:hover{background:#f0f6ff}
tr.ev td{background:#fbfcfe}
tr.grp{cursor:pointer;font-weight:600;background:#e2eaf5!important}
tr.grp:hover td{background:#dae4f2}
tr.grp .roll{font-weight:400;color:#3a516b;margin-left:10px}
.caret{display:inline-block;width:12px}
tr.weak td{box-shadow:inset 3px 0 #c5221f}
tr.dim td{color:#9aa}
.pill{background:#eef;border-radius:8px;padding:1px 6px;font-size:12px}
.edit{cursor:pointer;opacity:.35;margin-left:6px}.edit:hover{opacity:1}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:middle}
.sig-good{color:#137333}.sig-fair{color:#b06000}.sig-weak{color:#c5221f;font-weight:600}
</style>
<h1>netinventory</h1><div id=sum>loading…</div>
<div class=bar>
 <input id=q placeholder="search all fields…">
 <label>Group <select id=gb>
  <option value="">(none)</option><option value="band">Band</option>
  <option value="category">Category</option><option value="Status">Status</option>
  <option value="signal">Signal</option></select></label>
 <span class=chip id=cw>Only weak</span>
 <span class=chip id=co>Only offline</span>
 <span class=chip id=cu>Only unnamed</span>
 <span class=chip id=cd>Hide defunct</span>
 <span class=spacer></span>
 <a class=btn href="/rooms" style="text-decoration:none;color:inherit">Rooms</a>
 <button class=btn id=rst>Reset</button>
</div>
<div class=wrap><table><thead><tr id=hr></tr><tr class=f id=fr></tr></thead><tbody id=t></tbody></table></div>
<script>
const WRITABLE=__WRITABLE__;
const COLS=[
 {k:'Status',l:'Status',t:'status'},
 {k:'IP',l:'IP',t:'ip'},
 {k:'Device',l:'Device',t:'text',g:d=>d.Device||d.Hostname||''},
 {k:'category',l:'Category',t:'enum'},
 {k:'band',l:'Band',t:'enum'},
 {k:'signal',l:'Signal',t:'sig'},
 {k:'Vendor',l:'Vendor',t:'text'},
 {k:'MAC',l:'MAC',t:'mac'},
 {k:'Hostname',l:'Host',t:'text'},
 {k:'LastSeen',l:'Last seen',t:'text'},
 {k:'Source',l:'Source',t:'enum'},
];
const SIGORD={good:0,fair:1,weak:2,'~none':3};
const SIGLBL={good:'Good signal',fair:'Fair signal',weak:'Weak signal','~none':'Wired / no signal'};
const ST={q:'',group:'',sorts:[{k:'IP',dir:1}],filt:{},weak:false,off:false,unnamed:false,def:false,collapsed:{},built:false};
let D=[];
const val=(d,c)=>c.g?c.g(d):(c.k==='signal'?d.rssi:d[c.k]);
const ipn=ip=>(''+ (ip||'')).split('.').reduce((a,p)=>a*256+(parseInt(p,10)||0),0);
const esc=s=>(''+ (s==null?'':s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
function sig(d){if(d.rssi==null)return '<span class=dim>—</span>';
 const b={good:'▂▄▆',fair:'▂▄_',weak:'▂__'}[d.signal]||'';
 return `<span class=sig-${d.signal} title="${d.rssi} dBm">${b} ${d.rssi}<span style=font-size:11px> dBm</span></span>`}
function dot(s){const c=s=='online'?'#137333':(s=='defunct'?'#999':'#e08600');
 return `<span class=dot style=background:${c}></span>`}
function cmp1(a,b,c){
 if(c.t=='ip')return ipn(a.IP)-ipn(b.IP);
 const x=(''+ (val(a,c)||'')).toLowerCase(),y=(''+ (val(b,c)||'')).toLowerCase();
 return x<y?-1:x>y?1:0}
function cmp(a,b){
 for(const s of ST.sorts){const c=COLS.find(x=>x.k==s.k);
  if(c.t=='sig'){const x=a.rssi,y=b.rssi;
   if(x==null&&y==null){}else if(x==null)return 1;else if(y==null)return -1;
   else{const r=(x-y)*s.dir;if(r)return r}}
  else{const r=cmp1(a,b,c)*s.dir;if(r)return r}}
 return 0}
function gkey(d){if(ST.group=='signal')return d.signal||'~none';
 if(ST.group=='band')return d.band||'(none)';
 if(ST.group=='category')return d.category||'(uncategorized)';
 return d.Status||'(unknown)'}
function glbl(k){return ST.group=='signal'?(SIGLBL[k]||k):k}
function pass(d){
 if(ST.q&&!JSON.stringify(d).toLowerCase().includes(ST.q.toLowerCase()))return false;
 if(ST.weak&&d.signal!='weak')return false;
 if(ST.off&&d.Status!='offline')return false;
 if(ST.unnamed&&(''+ (d.Device||'')).trim())return false;
 if(ST.def&&d.Status=='defunct')return false;
 for(const k in ST.filt){const fv=ST.filt[k];if(!fv)continue;const c=COLS.find(x=>x.k==k);
  if(c.t=='enum'||c.t=='status'){if((''+ (val(d,c)||''))!=fv)return false}
  else if(c.t=='sig'){const tgt=fv=='~none'?'':fv;if((d.signal||'')!=tgt)return false}
  else{if(!(''+ (val(d,c)||'')).toLowerCase().includes(fv.toLowerCase()))return false}}
 return true}
function cell(d,c){
 if(c.t=='status')return dot(d.Status)+esc(d.Status);
 if(c.t=='sig')return sig(d);
 if(c.t=='mac')return `<span class=pill>${esc(d.MAC)}</span>`;
 if(c.k=='Device'){const base=d.Device?esc(d.Device):`<i>${esc(d.Hostname||'—')}</i>`;
  return base+(WRITABLE?` <span class=edit title="rename" onclick="rename('${esc(d.MAC)}')">&#9998;</span>`:'')}
 return esc(val(d,c))}
function rowHtml(d,ev){
 const cls=[d.signal=='weak'?'weak':'',d.Status!='online'?'dim':'',ev?'ev':''].filter(Boolean).join(' ');
 return `<tr class="${cls}">`+COLS.map(c=>`<td>${cell(d,c)}</td>`).join('')+'</tr>'}
function renderHead(){
 hr.innerHTML=COLS.map(c=>{const i=ST.sorts.findIndex(s=>s.k==c.k);const s=ST.sorts[i];
  const ar=s?`<span class=ar>${s.dir>0?'▲':'▼'}</span>`:'';
  const pri=(i>=0&&ST.sorts.length>1)?`<span class=pri>${i+1}</span>`:'';
  return `<th data-k="${c.k}">${c.l}${ar}${pri}</th>`}).join('')}
function buildFilters(){
 const distinct=k=>[...new Set(D.map(d=>''+ (d[k]||'')).filter(Boolean))].sort();
 fr.innerHTML=COLS.map(c=>{
  if(c.t=='enum'||c.t=='status'){
   const opts=(c.t=='status'?['online','offline','defunct']:distinct(c.k))
     .map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
   return `<th><select data-k=${c.k}><option value="">all</option>${opts}</select></th>`}
  if(c.t=='sig'){const opts=['good','fair','weak','~none']
    .map(v=>`<option value="${v}">${v=='~none'?'(none)':v}</option>`).join('');
   return `<th><select data-k=${c.k}><option value="">all</option>${opts}</select></th>`}
  return `<th><input data-k=${c.k} placeholder="filter"></th>`}).join('');
 fr.querySelectorAll('[data-k]').forEach(el=>el.addEventListener('input',e=>{
  ST.filt[e.target.dataset.k]=e.target.value;renderBody()}))}
function renderBody(){
 let rows=D.filter(pass).sort(cmp);
 let html='',n=0;
 if(!ST.group){html=rows.map((d)=>rowHtml(d,(n++)%2==1)).join('')}
 else{const groups={};rows.forEach(d=>{const k=gkey(d);(groups[k]=groups[k]||[]).push(d)});
  const keys=Object.keys(groups).sort((a,b)=>ST.group=='signal'?(SIGORD[a]-SIGORD[b]):(a<b?-1:1));
  for(const k of keys){const g=groups[k];const on=g.filter(d=>d.Status=='online').length;
   const rs=g.map(d=>d.rssi).filter(v=>v!=null);
   const avg=rs.length?Math.round(rs.reduce((a,v)=>a+v,0)/rs.length):null;
   const cid=ST.group+':'+k;const col=ST.collapsed[cid];
   const roll=`<span class=roll>${g.length} device${g.length!=1?'s':''} · ${on} online`+
     (avg!=null?` · avg ${avg} dBm`:'')+`</span>`;
   html+=`<tr class=grp data-cid="${esc(cid)}"><td colspan=${COLS.length}>`+
     `<span class=caret>${col?'▸':'▾'}</span>${esc(glbl(k))}${roll}</td></tr>`;
   if(!col){n=0;html+=g.map(d=>rowHtml(d,(n++)%2==1)).join('')}}}
 t.innerHTML=html||`<tr><td colspan=${COLS.length} style=color:#999;padding:12px>no devices match</td></tr>`;
 t.querySelectorAll('tr.grp').forEach(r=>r.onclick=()=>{
  const id=r.dataset.cid;ST.collapsed[id]=!ST.collapsed[id];renderBody()});
 shown.textContent=rows.length}
function render(){renderHead();renderBody()}
let shownTotal={total:0,online:0,unnamed:0};const shown={set textContent(v){upSum(v)}};
function upSum(v){sum.innerHTML=`<b>${v}</b> shown · ${shownTotal.total} total · `+
  `${shownTotal.online} online · <span style="cursor:pointer;text-decoration:underline dotted" `+
  `title="filter to unnamed" onclick="tog('unnamed')">${shownTotal.unnamed} unnamed</span>`}
function tog(f){ST[f]=!ST[f];({weak:cw,off:co,unnamed:cu,def:cd})[f].classList.toggle('on',ST[f]);renderBody()}
async function rename(mac){const d=D.find(x=>x.MAC==mac);if(!d)return;
 const name=prompt('Device name for '+mac+' (blank to clear):',d.Device||'');
 if(name==null)return;
 try{const r=await fetch('/api/name',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({mac,name})});
  if(!r.ok){const e=await r.json().catch(()=>({}));alert('Rename failed: '+(e.error||r.status));return}
  const j=await r.json();d.Device=j.name;renderBody()}
 catch(e){alert('Rename failed: '+e)}}
function reset(){ST.q='';ST.group='';ST.sorts=[{k:'IP',dir:1}];ST.filt={};ST.weak=ST.off=ST.unnamed=ST.def=false;
 ST.collapsed={};q.value='';gb.value='';[cw,co,cu,cd].forEach(c=>c.classList.remove('on'));
 fr.querySelectorAll('[data-k]').forEach(el=>el.value='');render()}
hr.addEventListener('click',e=>{const th=e.target.closest('th');if(!th)return;const k=th.dataset.k;
 const cur=ST.sorts.find(s=>s.k==k);
 if(e.shiftKey){if(cur)cur.dir*=-1;else ST.sorts.push({k,dir:1})}
 else{if(ST.sorts.length==1&&cur)cur.dir*=-1;else ST.sorts=[{k,dir:1}]}render()});
q.addEventListener('input',()=>{ST.q=q.value;renderBody()});
gb.addEventListener('change',()=>{ST.group=gb.value;renderBody()});
cw.onclick=()=>tog('weak');co.onclick=()=>tog('off');cu.onclick=()=>tog('unnamed');cd.onclick=()=>tog('def');rst.onclick=reset;
async function load(){D=await (await fetch('/api/devices')).json();
 const s=await (await fetch('/api/summary')).json();shownTotal=s;
 if(!ST.built){buildFilters();ST.built=true}render()}
load();setInterval(load,15000)</script>"""


# Room board: drag a device onto a room (or right-click > Move to) to set its
# Room in home.csv. Same look as _DASHBOARD; writes need --allow-write.
_ROOMS_PAGE = """<!doctype html><meta charset=utf-8><title>netinventory rooms</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--head:#1F4E78}
*{box-sizing:border-box}
body{font:13px system-ui;margin:1rem;color:#111;background:#fff}
h1{font-size:1.2rem;margin:.2rem 0}
#sum{color:#555;margin:.2rem 0 .6rem}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:.7rem}
.bar input{padding:5px 8px;font:13px system-ui;border:1px solid #cdd;border-radius:6px}
#q{width:18rem;flex:0 1 18rem}
.spacer{flex:1}
.btn{border:1px solid #ccd;border-radius:6px;padding:4px 9px;background:#fff;cursor:pointer;
 font:13px system-ui;color:inherit;text-decoration:none}
.ro{background:#fff4e5;border:1px solid #f0c48a;border-radius:6px;padding:6px 10px;margin-bottom:.7rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;align-items:start}
.room{border:1px solid #dde3ec;border-radius:8px;background:#f7f9fc;min-height:90px}
.room h2{font-size:13px;margin:0;padding:6px 10px;background:var(--head);color:#fff;
 border-radius:7px 7px 0 0;display:flex;justify-content:space-between}
.room h2 .n{font-weight:400;opacity:.8}
.room.unassigned h2{background:#8a5a00}
.room.over{outline:2px dashed var(--head);outline-offset:-2px;background:#e6eef9}
.list{padding:6px;display:flex;flex-direction:column;gap:4px}
.dev{background:#fff;border:1px solid #e3e7ee;border-radius:6px;padding:4px 8px;cursor:grab;
 display:flex;align-items:center;gap:6px;user-select:none}
.dev:hover{border-color:#9db4d3}
.dev.saving{opacity:.5}
.dev .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dev .ren{width:100%;font:13px system-ui;padding:2px 4px;border:1px solid #9db4d3;border-radius:4px}
.dev .sub{display:block;font-size:11px;color:#6b7a8c;overflow:hidden;text-overflow:ellipsis}
.dev .ty{font-size:11px;color:#3a516b;background:#eef2f8;border-radius:8px;padding:0 6px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none}
.empty{color:#9aa;padding:4px 4px 8px}
#menu{position:fixed;display:none;background:#fff;border:1px solid #ccd;border-radius:6px;
 box-shadow:0 4px 14px #0002;padding:4px 0;z-index:10;max-height:70vh;overflow:auto;min-width:170px}
#menu div{padding:4px 12px;cursor:pointer;white-space:nowrap}
#menu div:hover{background:#f0f6ff}
#menu .hd{color:#777;cursor:default;font-size:11px}#menu .hd:hover{background:none}
#menu .cur{font-weight:600}
</style>
<h1>Rooms</h1><div id=sum>loading…</div>
<div id=ro class=ro style=display:none>Read-only: start with
 <code>netinv serve --allow-write</code> to move devices.</div>
<div class=bar>
 <input id=q placeholder="filter devices…">
 <span class=spacer></span>
 <form id=addf style="display:flex;gap:6px;margin:0">
  <input id=newroom placeholder="new room name" maxlength=40>
  <button class=btn id=add type=submit>+ Room</button>
 </form>
 <a class=btn href="/">Devices</a>
</div>
<div id=msg class=ro style=display:none></div>
<div class=grid id=g></div>
<div id=menu></div>
<script>
const WRITABLE=__WRITABLE__;
let B={rooms:[],devices:[]},Q='';
const esc=s=>(''+ (s==null?'':s)).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const label=d=>d.name||d.device||d.id;
const color=s=>s=='online'?'#137333':(s=='offline'?'#e08600':'#bbb');
function card(d){
 const ty=d.type&&d.type!='TODO'?`<span class=ty>${esc(d.type.replace('_',' '))}</span>`:'';
 const tip=[d.name&&d.device&&d.name!=d.device?d.device:'',d.seen_as,d.ip].filter(Boolean).join(' · ');
 const sub=d.seen_as&&d.seen_as.toLowerCase()!=label(d).toLowerCase()?`<span class=sub>${esc(d.seen_as)}</span>`:'';
 return `<div class=dev draggable=${WRITABLE} data-id="${esc(d.id)}" title="${esc(tip)}">`+
  `<span class=dot style=background:${color(d.status)}></span>`+
  `<span class=nm>${esc(label(d))}${sub}</span>${ty}</div>`}
function render(){
 const shown=B.devices.filter(d=>!Q||[label(d),d.device,d.type,d.seen_as,d.ip].join(' ').toLowerCase().includes(Q));
 const by={'':[]};B.rooms.forEach(r=>by[r]=[]);
 shown.forEach(d=>(by[d.room]||by['']).push(d));
 Object.values(by).forEach(l=>l.sort((a,b)=>label(a).localeCompare(label(b))));
 g.innerHTML=['',...B.rooms].map(r=>`<div class="room${r?'':' unassigned'}" data-room="${esc(r)}">`+
  `<h2><span>${r?esc(r):'Unassigned'}</span><span class=n>${by[r].length}</span></h2>`+
  `<div class=list>${by[r].map(card).join('')||'<div class=empty>drop devices here</div>'}</div></div>`).join('');
 const un=B.devices.filter(d=>!d.room).length;
 sum.textContent=`${B.devices.length} devices · ${B.rooms.length} rooms · ${un} unassigned`}
async function post(path,body){
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error(j.error||r.status);return j}
async function move(id,room){
 const d=B.devices.find(x=>x.id==id);if(!d||d.room==room)return;
 const was=d.room;d.room=room;render();
 try{await post('/api/room',{id,room})}
 catch(e){d.room=was;render();say('Move failed: '+e.message)}}
let dragId=null;
g.addEventListener('dragstart',e=>{const el=e.target.closest('.dev');if(!el)return;
 dragId=el.dataset.id;e.dataTransfer.setData('text/plain',dragId);e.dataTransfer.effectAllowed='move'});
// The device id travels in dataTransfer, so the drop works even when the
// browser fires dragend first (dragId is only a "drag in progress" flag).
const isDev=e=>[...e.dataTransfer.types].includes('text/plain');
const dropOn=(e,rm)=>{e.preventDefault();e.dataTransfer.dropEffect='move';
 g.querySelectorAll('.over').forEach(x=>x!=rm&&x.classList.remove('over'));rm.classList.add('over')};
g.addEventListener('dragenter',e=>{const rm=e.target.closest('.room');if(rm&&isDev(e))dropOn(e,rm)});
g.addEventListener('dragover',e=>{const rm=e.target.closest('.room');if(rm&&isDev(e))dropOn(e,rm)});
g.addEventListener('dragleave',e=>{const rm=e.target.closest('.room');
 if(rm&&!rm.contains(e.relatedTarget))rm.classList.remove('over')});
g.addEventListener('drop',e=>{const rm=e.target.closest('.room');if(!rm)return;
 e.preventDefault();rm.classList.remove('over');const id=e.dataTransfer.getData('text/plain')||dragId;
 dragId=null;if(id)move(id,rm.dataset.room)});
g.addEventListener('dragend',()=>{dragId=null;g.querySelectorAll('.over').forEach(x=>x.classList.remove('over'))});
g.addEventListener('contextmenu',e=>{const el=e.target.closest('.dev');if(!el||!WRITABLE)return;
 e.preventDefault();const d=B.devices.find(x=>x.id==el.dataset.id);
 menu.innerHTML=`<div data-act=rename>Rename…</div><div class=hd>Move ${esc(label(d))} to</div>`+['',...B.rooms].map(r=>
  `<div data-room="${esc(r)}" class="${r==d.room?'cur':''}">${r?esc(r):'Unassigned'}</div>`).join('');
 menu.dataset.id=d.id;menu.style.display='block';
 const w=menu.offsetWidth,h=menu.offsetHeight;
 menu.style.left=Math.min(e.clientX,innerWidth-w-8)+'px';menu.style.top=Math.min(e.clientY,innerHeight-h-8)+'px'});
menu.addEventListener('click',e=>{
 if(e.target.closest('[data-act=rename]')){menu.style.display='none';startRename(menu.dataset.id);return}
 const it=e.target.closest('[data-room]');if(!it)return;
 menu.style.display='none';move(menu.dataset.id,it.dataset.room)});
// Rename in place on the card: Enter saves, Escape or leaving the field cancels.
let editing=false;
function startRename(id){const d=B.devices.find(x=>x.id==id);
 const el=[...g.querySelectorAll('.dev')].find(x=>x.dataset.id==id);if(!d||!el)return;
 editing=true;el.draggable=false;const nm=el.querySelector('.nm');
 nm.innerHTML=`<input class=ren value="${esc(label(d))}" maxlength=60>`;
 const inp=nm.querySelector('input');inp.focus();inp.select();let done=false;
 const finish=async save=>{if(done)return;done=true;const name=inp.value.trim();
  if(!save||!name||name==label(d)){editing=false;render();return}
  try{const j=await post('/api/rename',{id,name});
   Object.assign(d,{id:j.id,name:j.name,device:d.device?j.name:d.device});
   if(j.warnings.length)say(`Renamed to '${j.name}'. Not to the naming standard yet: `+j.warnings.join('; '))}
  catch(e){say('Rename failed: '+e.message)}
  editing=false;render()};
 inp.addEventListener('keydown',e=>{if(e.key=='Enter')finish(true);if(e.key=='Escape')finish(false)});
 inp.addEventListener('blur',()=>finish(false))}
document.addEventListener('click',e=>{if(!menu.contains(e.target))menu.style.display='none'});
document.addEventListener('keydown',e=>{if(e.key=='Escape')menu.style.display='none'});
// Inline input and message, not prompt()/alert(): embedded browsers (e.g. VS
// Code's Simple Browser) silently block those dialogs.
let msgT;function say(t){msg.textContent=t;msg.style.display='block';
 clearTimeout(msgT);msgT=setTimeout(()=>msg.style.display='none',6000)}
addf.addEventListener('submit',async e=>{e.preventDefault();const name=newroom.value.trim();
 if(!name){newroom.focus();return}
 try{const j=await post('/api/room-add',{room:name});newroom.value='';
  if(B.rooms.includes(j.room))say(`'${j.room}' already exists`);else{B.rooms.push(j.room);render()}}
 catch(e){say('Add room failed: '+e.message)}});
q.addEventListener('input',()=>{Q=q.value.toLowerCase();render()});
if(!WRITABLE){ro.style.display='block';addf.style.display='none'}
async function load(){if(dragId!=null||editing||menu.style.display=='block')return;
 B=await (await fetch('/api/board')).json();render()}
load();setInterval(load,15000)</script>"""


def cmd_serve(args):
    """Read-only JSON API + a tiny dashboard for a GUI/iOS companion to consume."""
    import http.server
    import json as _json

    def payload(path):
        master = load_master()
        enrich_vendors(master)
        if path == "/api/devices":
            return api_devices(master)
        if path == "/api/summary":
            return api_summary(master)
        if path == "/api/alerts":
            return evaluate_alerts(master)
        if path == "/api/events":
            return device_history("")  # all
        if path == "/api/board":
            import home
            return home.board(list(master.values()), home.load_rooms(ROOMS_CSV),
                              home.load_home(HOME_CSV))
        return None

    allow_write = getattr(args, "allow_write", False)
    import threading
    write_lock = threading.Lock()

    def _send(self, code, body, ctype, cors=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cors:  # only GET reads are cross-origin shareable; writes are
            # same-origin-gated in do_POST (CORS headers alone don't stop CSRF)
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            page = {"/": _DASHBOARD, "/index.html": _DASHBOARD,
                    "/rooms": _ROOMS_PAGE}.get(path)
            if page:
                # inject whether writes are enabled so the UI shows edit controls
                html = page.replace("__WRITABLE__", "true" if allow_write else "false")
                _send(self, 200, html.encode(), "text/html; charset=utf-8", cors=True)
                return
            data = payload(path)
            if data is None:
                self.send_error(404)
                return
            _send(self, 200, _json.dumps(data).encode(), "application/json", cors=True)

        def do_POST(self):
            path = self.path.split("?")[0]
            if path not in ("/api/name", "/api/room", "/api/room-add", "/api/rename"):
                self.send_error(404)
                return
            if not allow_write:
                _send(self, 403, b'{"error":"read-only; start with --allow-write"}',
                      "application/json")
                return
            # CSRF defense for the state-changing write. Requiring a strict
            # application/json body forces a CORS preflight for any cross-origin
            # caller (which this server never approves), so a malicious page
            # cannot slip in a CORS-"simple" text/plain POST. As a second gate,
            # a present Origin must match our own host.
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                _send(self, 415,
                      b'{"error":"Content-Type must be application/json"}',
                      "application/json")
                return
            origin = self.headers.get("Origin")
            if origin:
                from urllib.parse import urlparse
                if urlparse(origin).netloc != self.headers.get("Host", ""):
                    _send(self, 403, b'{"error":"cross-origin write rejected"}',
                          "application/json")
                    return
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = _json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except (ValueError, TypeError):
                _send(self, 400, b'{"error":"bad JSON"}', "application/json")
                return
            with write_lock:   # one read-modify-write of the CSVs at a time
                if path == "/api/name":
                    self._name(req)
                elif path == "/api/rename":
                    self._rename(req)
                else:
                    self._room(path, req)

        def _rename(self, req):
            """Board rename: one friendly name for devices.csv, home.csv and
            app_rooms.csv. Naming-rule problems come back as warnings."""
            import home
            master = load_master()
            rows = home.load_home(HOME_CSV)
            app = home.load_app_rooms(APP_ROOMS_CSV)
            try:
                old, new_id = home.rename(rows, list(master.values()), app,
                                          str(req.get("id", "")), str(req.get("name", "")))
            except ValueError as e:
                _send(self, 400, _json.dumps({"error": str(e)}).encode(), "application/json")
                return
            save_master(master)
            write_reports(master)
            home.write_csv(HOME_CSV, home.HOME_FIELDS, rows)
            if app:
                home.write_csv(APP_ROOMS_CSV, home.APP_ROOM_FIELDS, app)
            row = next(h for h in rows if (h["Device"] or "name:" + h["Name"]) == new_id)
            rooms = home.load_rooms(ROOMS_CSV)
            warnings = home.name_problems(row["Name"], row["Room"], home.room_words(rooms))
            _send(self, 200, _json.dumps({"ok": True, "id": new_id, "name": row["Name"],
                                          "old": old, "warnings": warnings}).encode(),
                  "application/json")

        def _room(self, path, req):
            import home
            rooms = home.load_rooms(ROOMS_CSV)
            room = home.clean_room(str(req.get("room", "")))
            if path == "/api/room-add":
                if not room:
                    _send(self, 400, b'{"error":"room name required"}', "application/json")
                    return
                room = home.add_room(rooms, room)
                home.write_csv(ROOMS_CSV, home.ROOM_FIELDS, rooms)
                _send(self, 200, _json.dumps({"ok": True, "room": room}).encode(),
                      "application/json")
                return
            canon = {r["Room"].lower(): r["Room"] for r in rooms}
            if room and room.lower() not in canon:
                _send(self, 400, b'{"error":"unknown room"}', "application/json")
                return
            room = canon.get(room.lower(), "")
            rows = home.load_home(HOME_CSV)
            row = home.set_room(rows, list(load_master().values()),
                                str(req.get("id", "")), room)
            if row is None:
                _send(self, 404, b'{"error":"unknown device"}', "application/json")
                return
            home.write_csv(HOME_CSV, home.HOME_FIELDS, rows)
            _send(self, 200, _json.dumps({"ok": True, "room": room}).encode(),
                  "application/json")

        def _name(self, req):
            mac, name = req.get("mac", ""), req.get("name", "")
            master = load_master()
            # create=False: the UI only renames devices the scanner already knows
            if apply_name(master, [mac], name, create=False) != 1:
                _send(self, 404, b'{"error":"unknown MAC"}', "application/json")
                return
            save_master(master)
            write_reports(master)
            row = master[norm_mac(mac)]
            _send(self, 200,
                  _json.dumps({"ok": True, "mac": row["MAC"],
                               "name": row["Device"]}).encode(),
                  "application/json")

    srv = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"  netinventory API on http://{args.host}:{args.port}  "
          f"(/, /rooms, /api/devices, /api/summary, /api/alerts, /api/events, /api/board)")
    print(f"  writes: {'ENABLED (POST /api/name, /api/room, /api/room-add)' if allow_write else 'disabled (read-only)'}")
    if args.host not in ("127.0.0.1", "localhost"):
        print("  ! bound beyond localhost — data is served to the LAN unauthenticated")
        if allow_write:
            print("  ! --allow-write + LAN bind: anyone on the network can rename devices")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


def cmd_alerts(args):
    master = load_master()
    enrich_vendors(master)
    alerts = evaluate_alerts(master)
    if args.rule:
        alerts = [a for a in alerts if a["rule"] == args.rule]
    if not alerts:
        print("  no alerts")
        return
    icon = {"high": "!!", "med": "! ", "low": "· "}
    for a in alerts:
        print(f"  {icon.get(a['severity'], '  ')} [{a['rule']}] {a['device']:26} "
              f"{a['ip']:15} {a['mac']}  {a['band']}")
    print(f"\n  {len(alerts)} alert(s). Filter with --rule <id>; "
          f"rules: {', '.join(r[0] for r in ALERT_RULES)}")
    if args.notify:
        highs = [a for a in alerts if a["severity"] == "high"]
        for a in highs[:5]:
            notify("Network alert", f"{a['desc']}: {a['device']} ({a['ip'] or a['mac']})")


def cmd_history(args):
    hist = device_history(args.query)
    cur = [r for r in load_master().values()
           if args.query.lower() in " ".join(
               [r["Device"], r["Hostname"], r["MAC"], r["IP"]]).lower()]
    if cur:
        print("  current inventory:")
        for r in cur:
            print(f"    {r['Device'] or '(unnamed)':26} {r['IP']:15} {r['MAC']}  "
                  f"{r['Status']}  first={r['FirstSeen'][:10]} last={r['LastSeen']}")
    print(f"\n  join events ({len(hist)}):" if hist else "\n  no join events logged")
    for r in hist:
        print(f"    {r.get('Timestamp', '')}  {r.get('Event', '')}  "
              f"{r.get('IP', '')}  {r.get('MAC', '')}  {r.get('Band', '')}")


def cmd_show(args):
    master = load_master()
    q = (args.query or "").lower()
    rows = sorted(master.values(), key=lambda r: ip_key(r["IP"]))
    for r in rows:
        # query matches name / hostname / MAC / connection (e.g. "5 ghz", "wired")
        hay = " ".join([r["Device"], r["Hostname"], r["MAC"], r["Link"],
                        link_class(r["Link"])]).lower()
        if q and q not in hay:
            continue
        print(f"{r['IP']:15} {band(r['Link']) or '—':9} {r['Device'] or '(unnamed)':28} "
              f"{r['MAC']:18} {r['Status']:8} {r['Vendor'][:22]:22} {r['Hostname']}")


def main():
    p = argparse.ArgumentParser(description="home network device inventory")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="gather sources, merge, write reports")
    u.add_argument("--scan", action="store_true", help="run local subnet scan")
    u.add_argument("--no-sweep", action="store_true", help="skip nmap, use existing ARP cache")
    u.add_argument("--router", action="store_true", help="scrape the Xfinity gateway")
    u.add_argument("--no-ssdp", action="store_true", help="skip SSDP/UPnP name discovery")
    u.add_argument("--no-mdns", action="store_true", help="skip mDNS/DNS-SD name discovery")
    u.add_argument("--cloud", action="store_true",
                   help="also pull names from configured smart-home clouds")
    u.add_argument("--auto-name", action="store_true",
                   help="fill blank friendly names from high-quality discovered names")
    u.add_argument("--notify", action="store_true",
                   help="macOS notification when a new device joins")
    u.add_argument("--fingerprint", action="store_true",
                   help="active-fingerprint new joins + unnamed online devices")
    u.add_argument("--fp-fast", action="store_true", help="fingerprint: ports only (skip -sV)")
    u.add_argument("--fp-limit", type=int, default=10,
                   help="max devices to fingerprint per run (default 10)")
    u.add_argument("--import-csv", nargs="?", const=DEFAULT_IMPORT, default=None,
                   metavar="PATH", help=f"import old export (default {DEFAULT_IMPORT})")
    u.set_defaults(func=cmd_update)

    w = sub.add_parser("watch", help="poll on an interval and alert when a new device joins")
    w.add_argument("--interval", type=int, default=300, help="seconds between polls (default 300)")
    w.add_argument("--no-router", action="store_true", help="local scan only (skip gateway)")
    w.add_argument("--no-sweep", action="store_true", help="skip nmap, use existing ARP cache")
    w.add_argument("--no-ssdp", action="store_true", help="skip SSDP/UPnP name discovery")
    w.add_argument("--no-mdns", action="store_true", help="skip mDNS/DNS-SD name discovery")
    w.add_argument("--notify", action="store_true", help="macOS notification on new device")
    w.add_argument("--once", action="store_true", help="run a single poll and exit")
    w.set_defaults(func=cmd_watch)

    n = sub.add_parser("name", help="assign MAC(s) to a friendly device name")
    n.add_argument("device")
    n.add_argument("macs", nargs="+")
    n.set_defaults(func=cmd_name)

    hm = sub.add_parser("home", help="rooms, types and canonical names "
                                     "(docs/home-standard.md)")
    hm.add_argument("action", choices=["rooms", "draft", "check", "push"],
                    help="rooms: import rooms from an app into rooms.csv; "
                         "draft: add unclassified devices to home.csv; "
                         "check: validate rooms.csv + home.csv; "
                         "push: rename devices in an app to netinventory's names")
    hm.add_argument("--from", "--to", dest="source", choices=["tuya", "kasa"], default="tuya",
                    help="app for rooms (--from tuya) or push (--to tuya|kasa); default tuya")
    hm.add_argument("--device", help="push: only this device (old or new name)")
    hm.add_argument("--dry-run", action="store_true", help="push: show, change nothing")
    hm.set_defaults(func=cmd_home)

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

    fp = sub.add_parser("fingerprint", help="probe ports/banners to identify unknown devices")
    fp.add_argument("target", nargs="?", help="IP or MAC (default: unnamed online devices)")
    fp.add_argument("--fast", action="store_true", help="port scan only, skip -sV service detection")
    fp.add_argument("--limit", type=int, default=8, help="max devices when scanning all (default 8)")
    fp.add_argument("--apply", action="store_true", help="write findings to each device's Notes")
    fp.set_defaults(func=cmd_fingerprint)

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

    bl = sub.add_parser("blink-login", help="interactive Blink 2FA login (run in a terminal)")
    bl.set_defaults(func=cmd_blink_login)

    s = sub.add_parser("show", help="print current inventory")
    s.add_argument("query", nargs="?")
    s.set_defaults(func=cmd_show)

    al = sub.add_parser("alerts", help="evaluate alert rules against the inventory")
    al.add_argument("--rule", help="show only this rule id")
    al.add_argument("--notify", action="store_true", help="macOS notification for high alerts")
    al.set_defaults(func=cmd_alerts)

    h = sub.add_parser("history", help="join history + current record for a device")
    h.add_argument("query", help="MAC or name substring")
    h.set_defaults(func=cmd_history)

    sv = sub.add_parser("serve", help="read-only JSON API + dashboard (for a GUI/iOS app)")
    sv.add_argument("--host", default="127.0.0.1", help="bind host (default localhost)")
    sv.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    sv.add_argument("--allow-write", action="store_true",
                    help="enable POST /api/name and /api/room so the dashboard/app "
                         "can rename devices and move them between rooms "
                         "(default: read-only)")
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
