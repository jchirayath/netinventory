#!/usr/bin/env python3
"""
Smart-home cloud connectors for netinv.

Each connector logs into a vendor cloud and returns the *user-assigned* device
names (the names you set in the app), plus a MAC and/or IP where the vendor
exposes them. netinv merges those names back into devices.csv:

    MAC present   -> matched directly (best)
    only IP       -> matched via the current ARP table
    name only     -> written to cloud_devices.csv as a reference list

Credentials live in the macOS Keychain as generic-passwords under the service
"netinv-cloud-<provider>", one account per field. Store them with:

    ./netinv.py set-cloud <provider>

Heavy vendor SDKs are imported lazily so the core tool stays dependency-free.
Install only what you use:
    pip install tinytuya          # tuya / smartlife
    pip install wyze-sdk          # wyze
    pip install blinkpy           # blink
    # smartthings and govee use the standard library only
"""

import json
import os
import re
import subprocess
import urllib.request

# provider -> ordered (field, is_secret, prompt) describing required credentials
PROVIDERS = {
    "tuya": [
        ("access_id",     False, "Tuya IoT Access ID / Client ID"),
        ("access_secret", True,  "Tuya IoT Access Secret / Client Secret"),
        ("region",        False, "API region (us, eu, cn, in)"),
    ],
    "wyze": [
        ("email",    False, "Wyze account email"),
        ("password", True,  "Wyze account password"),
        ("key_id",   False, "Wyze API Key ID"),
        ("api_key",  True,  "Wyze API Key"),
    ],
    "blink": [
        ("email",    False, "Blink account email"),
        ("password", True,  "Blink account password"),
    ],
    "smartthings": [
        ("token", True, "SmartThings Personal Access Token"),
    ],
    "apple": [
        ("apple_id", False, "Apple ID (email)"),
        ("password", True,  "Apple ID app-specific password"),
    ],
    "govee": [
        ("api_key", True, "Govee API key (Govee Home app > Profile > Settings > Apply for API Key)"),
    ],
    "kasa": [],   # local LAN protocol, no account needed
    "ha": [
        ("url",   False, "Home Assistant URL (e.g. http://192.168.1.204:8123)"),
        ("token", True,  "Home Assistant long-lived access token"),
    ],
}


# --------------------------------------------------------------------------- #
# Keychain helpers (generic-password, one account per credential field)
# --------------------------------------------------------------------------- #
def _service(provider):
    return f"netinv-cloud-{provider}"


def kc_set(provider, account, secret):
    subprocess.run(
        ["security", "add-generic-password", "-s", _service(provider),
         "-a", account, "-w", secret, "-U",
         "-l", f"netinv {provider} {account}"],
        check=True, capture_output=True, text=True)


def kc_get(provider, account):
    r = subprocess.run(
        ["security", "find-generic-password", "-s", _service(provider),
         "-a", account, "-w"],
        capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def creds(provider):
    """Return {field: value} for a provider, or None if not fully configured."""
    out = {}
    for field, _is_secret, _prompt in PROVIDERS.get(provider, []):
        val = kc_get(provider, field)
        if not val:
            return None
        out[field] = val
    return out


def configured_providers():
    return [p for p in PROVIDERS if creds(p) is not None]


# --------------------------------------------------------------------------- #
# normalized result shape: {provider, name, mac, ip, model, online}
# --------------------------------------------------------------------------- #
def _item(provider, name, mac="", ip="", model="", online=True):
    return {"provider": provider, "name": (name or "").strip(),
            "mac": (mac or "").strip(), "ip": (ip or "").strip(),
            "model": (model or "").strip(), "online": online}


def discover_tuya(c):
    """Tuya/SmartLife via tinytuya Cloud. Returns name + MAC + IP per device."""
    try:
        import tinytuya
    except ImportError:
        print("  ! tuya: `pip install tinytuya` first")
        return []
    cloud = tinytuya.Cloud(apiRegion=c["region"], apiKey=c["access_id"],
                           apiSecret=c["access_secret"])
    resp = cloud.getdevices(True)  # verbose -> raw {'result': [...], ...}
    devs = resp.get("result", []) if isinstance(resp, dict) else resp
    if not isinstance(devs, list):
        print(f"  ! tuya: unexpected response: {str(resp)[:160]}")
        return []

    # The cloud gives the app name but no LAN MAC (and only a public IP). WiFi
    # devices broadcast their id + LAN IP locally, so scan the LAN to map
    # cloud id -> LAN IP -> MAC (via ARP). Zigbee/BLE sub-devices don't
    # broadcast, so they correctly resolve to no MAC.
    id2ip = {}
    try:
        local = tinytuya.deviceScan(False, 18)
        for ip, info in local.items():
            did = info.get("gwId") or info.get("id")
            if did:
                id2ip[did] = ip
    except Exception as e:  # noqa: BLE001
        print(f"  ! tuya: local scan failed ({e}); using id-embedded MACs only")
    ip2mac = _resolve_arp(set(id2ip.values()))
    factory = _tuya_factory_macs(cloud, [d.get("id", "") for d in devs
                                         if not ip2mac.get(id2ip.get(d.get("id", ""), ""))])

    out = []
    for d in devs:
        did = d.get("id", "")
        lan_ip = id2ip.get(did, "")
        # Authoritative: the live LAN scan + ARP. Then the cloud's factory MAC,
        # which covers battery devices (door/flood sensors) that sleep instead
        # of broadcasting. Last, an id-embedded MAC for all-hex WiFi ids only
        # (cloud 'eb...' UUIDs would yield garbage).
        mac = ip2mac.get(lan_ip, "") if lan_ip else ""
        if not mac:
            mac = factory.get(did, "") or _mac_from_tuya_id(did)
        out.append(_item("tuya", d.get("name"), mac=mac, ip=lan_ip,
                         model=d.get("product_name", "") or d.get("category_name", ""),
                         online=bool(d.get("online", True))))
    print(f"  tuya: {len(devs)} cloud devices, {len(id2ip)} seen on the LAN")
    return out


def _tuya_cloud(c):
    import tinytuya
    return tinytuya.Cloud(apiRegion=c["region"], apiKey=c["access_id"],
                          apiSecret=c["access_secret"])


def tuya_rooms(c):
    """{room name: [(device name, device id)]} from the Tuya/SmartLife app's
    homes and rooms. Rooms with no devices are kept (empty list); devices in
    no room are listed under ''. The id is what `home push` renames by."""
    cloud = _tuya_cloud(c)
    devs = cloud.getdevices(True).get("result", [])
    out, placed = {}, set()
    for home_id in sorted({d.get("owner_id") for d in devs if d.get("owner_id")}):
        resp = cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms")
        if not resp.get("success"):
            print(f"  ! tuya: rooms for home {home_id}: {resp.get('msg', resp)}")
            continue
        for room in resp["result"].get("rooms", []):
            rd = cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms/{room['room_id']}/devices")
            items = [(d["name"], d["id"]) for d in (rd.get("result") or [])
                     if d.get("name") and d.get("id")]
            placed.update(i for _, i in items)
            out.setdefault(room.get("name", ""), []).extend(items)
    unplaced = [(d["name"], d["id"]) for d in devs
                if d.get("name") and d.get("id") and d["id"] not in placed]
    if unplaced:
        out.setdefault("", []).extend(unplaced)
    return out


def tuya_names(c):
    """{device id: current app name} for every Tuya/SmartLife device."""
    return {d["id"]: d.get("name", "") for d in _tuya_cloud(c).getdevices(True).get("result", [])
            if d.get("id")}


def tuya_rename(c, device_id, name):
    """Rename one device in the SmartLife app. Returns (ok, message)."""
    resp = _tuya_cloud(c).cloudrequest(f"/v1.0/devices/{device_id}", action="PUT",
                                       post={"name": name})
    if isinstance(resp, dict) and resp.get("success"):
        return True, "renamed"
    msg = resp.get("msg", resp) if isinstance(resp, dict) else resp
    return False, str(msg)


def _mac_from_tuya_id(s):
    """An all-hex Tuya WiFi device id embeds the MAC as its trailing 12 hex.
    Cloud 'eb...' UUIDs contain letters and embed no MAC -> return ''."""
    s = (s or "")
    if re.fullmatch(r"[0-9A-Fa-f]{16,24}", s):
        tail = s[-12:].upper()
        return ":".join(tail[i:i + 2] for i in range(0, 12, 2))
    return ""


def _tuya_factory_macs(cloud, ids):
    """{device id: MAC} from the cloud's factory records. The API takes a
    limited id list per call, so ask in batches; a failed batch is skipped."""
    out = {}
    ids = [i for i in ids if i]
    for n in range(0, len(ids), 20):
        try:
            r = cloud.cloudrequest("/v1.0/iot-03/devices/factory-infos",
                                   query={"device_ids": ",".join(ids[n:n + 20])})
        except Exception as e:  # noqa: BLE001
            print(f"  ! tuya: factory MACs failed ({e})")
            continue
        for f in (r or {}).get("result") or []:
            mac = (f.get("mac") or "").strip()
            if re.fullmatch(r"[0-9A-Fa-f]{12}", mac):
                out[f["id"]] = _norm_mac(":".join(mac[i:i + 2] for i in range(0, 12, 2)))
    return out


def _norm_mac(m):
    p = (m or "").replace("-", ":").split(":")
    if len(p) != 6:
        return (m or "").upper()
    try:
        return ":".join(f"{int(x, 16):02X}" for x in p)
    except ValueError:
        return m.upper()


def _resolve_arp(ips):
    """Ping each IP to refresh the ARP cache, then return {ip: MAC}."""
    if not ips:
        return {}
    procs = []
    for ip in ips:
        try:
            procs.append(subprocess.Popen(
                ["ping", "-c", "1", "-t", "2", ip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except Exception:  # noqa: BLE001
            pass
    for p in procs:
        try:
            p.wait(3)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    out = {}
    try:
        arp = subprocess.run(["arp", "-an"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return out
    for ln in arp.splitlines():
        a = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", ln)
        b = re.search(r"at ([0-9a-fA-F:]+) on", ln)
        if a and b:
            out[a.group(1)] = _norm_mac(b.group(1))
    return out


def discover_wyze(c):
    """Wyze via wyze-sdk. Returns nickname + MAC per device."""
    try:
        from wyze_sdk import Client
        from wyze_sdk.errors import WyzeApiError
    except ImportError:
        print("  ! wyze: `pip install wyze-sdk` first")
        return []
    try:
        client = Client(email=c["email"], password=c["password"],
                        key_id=c["key_id"], api_key=c["api_key"])
        out = []
        for d in client.devices_list():
            out.append(_item("wyze", getattr(d, "nickname", "") or getattr(d, "name", ""),
                             mac=getattr(d, "mac", ""),
                             model=getattr(d, "product_model", ""),
                             online=bool(getattr(d, "is_online", True))))
        return out
    except WyzeApiError as e:  # noqa: BLE001
        print(f"  ! wyze: {e}")
        return []


def blink_login_interactive():
    """Interactive Blink/Amazon 2FA login — RUN IN A REAL TERMINAL. Triggers the
    code, prompts for it live, then caches the token to ~/.netinv_blink.json so
    later `cloud blink` runs need no 2FA."""
    c = creds("blink")
    if not c:
        print("  blink not configured — run: ./netinv.py set-cloud blink")
        return
    try:
        import asyncio
        from aiohttp import ClientSession
        from blinkpy.blinkpy import Blink
        from blinkpy.auth import Auth, BlinkTwoFARequiredError
    except ImportError:
        print("  ! blink: ./.venv/bin/python -m pip install blinkpy aiohttp")
        return
    token_path = os.path.expanduser("~/.netinv_blink.json")

    async def _run():
        async with ClientSession() as session:
            blink = Blink(session=session)
            blink.auth = Auth({"username": c["email"], "password": c["password"]},
                              no_prompt=True, session=session)
            try:
                await blink.start()
                print("  logged in (no 2FA needed).")
            except BlinkTwoFARequiredError:
                print("  Blink/Amazon should have sent a code — check email AND text "
                      "messages (and your authenticator if your Amazon account uses one).")
                code = input("  Enter the 2FA code: ").strip()
                if not code:
                    print("  aborted (no code).")
                    return
                # send_2fa_code finishes the OAuth flow *and* the setup start()
                # skipped (urls, homescreen, cameras); blinkpy >= 0.25.
                if not await blink.send_2fa_code(code):
                    print("  ! blink: code rejected or setup failed; run blink-login again")
                    return
            await blink.save(token_path)
            os.chmod(token_path, 0o600)          # a login token: owner-only
            cams = list(blink.cameras.items())
            print(f"  success — token cached, {len(cams)} camera(s):")
            for name, cam in cams:
                a = getattr(cam, "attributes", {}) or {}
                print(f"    {name}  {a.get('type', '')}")
            print("  now run:  ./.venv/bin/python netinv.py cloud blink")

    try:
        asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"  ! blink login failed: {e}")


def discover_blink(c):
    """Blink via blinkpy (async). Returns camera names (no MAC — cloud cameras).

    2FA: Blink sends a code on first login from a new client. Pass it via the
    NETINV_BLINK_2FA env var (not a stdin prompt, so it works headless). The
    auth token is cached to ~/.netinv_blink.json so later runs skip 2FA."""
    try:
        import asyncio
        from aiohttp import ClientSession
        from blinkpy.blinkpy import Blink
        from blinkpy.auth import Auth, BlinkTwoFARequiredError
        from blinkpy.helpers.util import json_load
    except ImportError:
        print("  ! blink: `pip install blinkpy aiohttp` first")
        return []
    token_path = os.path.expanduser("~/.netinv_blink.json")
    code = os.environ.get("NETINV_BLINK_2FA", "").strip()

    async def _run():
        async with ClientSession() as session:
            blink = Blink(session=session)
            if os.path.exists(token_path):
                blink.auth = Auth(await json_load(token_path), session=session,
                                  no_prompt=True)
            else:
                blink.auth = Auth({"username": c["email"], "password": c["password"]},
                                  no_prompt=True, session=session)
            try:
                ok = await blink.start()
            except BlinkTwoFARequiredError:
                if not code:
                    print("  ! blink: 2FA required. Run `netinv blink-login` in a "
                          "terminal (or re-run with NETINV_BLINK_2FA=<code>)")
                    return None
                ok = await blink.send_2fa_code(code)
            if not ok:
                print("  ! blink: login or setup failed; run `netinv blink-login`")
                return None
            try:
                await blink.save(token_path)  # refreshed token -> no 2FA next time
                os.chmod(token_path, 0o600)
            except Exception:  # noqa: BLE001
                pass
            out = []
            for name, cam in blink.cameras.items():
                attrs = getattr(cam, "attributes", {}) or {}
                out.append(_item("blink", name, ip=attrs.get("ip_address", "") or "",
                                 model=attrs.get("type", "")))
            return out

    try:
        return asyncio.run(_run()) or []
    except Exception as e:  # noqa: BLE001
        print(f"  ! blink: {e}")
        return []


def discover_smartthings(c):
    """SmartThings via official REST API + PAT. Returns device labels."""
    req = urllib.request.Request(
        "https://api.smartthings.com/v1/devices",
        headers={"Authorization": f"Bearer {c['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        print(f"  ! smartthings: {e}")
        return []
    out = []
    for d in data.get("items", []):
        name = d.get("label") or d.get("name") or ""
        # MAC occasionally present for WiFi/OCF devices
        mac = ""
        for k in ("networkId", "macAddress"):
            v = d.get(k) or (d.get("ocf") or {}).get(k)
            if v and len(str(v)) == 12:
                mac = str(v)
        out.append(_item("smartthings", name, mac=mac,
                         model=(d.get("ocf") or {}).get("modelNumber", "")))
    return out


def govee_lan_scan(timeout=4.0):
    """{device id: (LAN IP, sku)} from Govee's LAN API: a multicast scan to
    239.255.255.250:4001, answered on UDP 4002. Only devices with LAN Control
    switched on in the Govee app answer."""
    import socket
    import time
    found = {}
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rx.bind(("", 4002))
        rx.settimeout(0.5)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    except OSError as e:
        print(f"  ! govee: LAN scan unavailable ({e})")
        return found
    msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    end, next_tx = time.time() + timeout, 0
    while time.time() < end:
        if time.time() >= next_tx:            # repeat: UDP multicast drops packets
            tx.sendto(msg, ("239.255.255.250", 4001))
            next_tx = time.time() + 1
        try:
            data, addr = rx.recvfrom(4096)
            d = json.loads(data)["msg"]["data"]
        except (socket.timeout, ValueError, KeyError, TypeError):
            continue
        if d.get("device"):
            found[d["device"].upper()] = (d.get("ip") or addr[0], d.get("sku", ""))
    rx.close()
    tx.close()
    return found


def _govee_cloud_devices(key):
    """[{device, sku, deviceName}] from the Govee OpenAPI, falling back to the
    older developer API (keys issued before 2024 may only work there)."""
    for url, pick in (
            ("https://openapi.api.govee.com/router/api/v1/user/devices",
             lambda j: j.get("data") or []),
            ("https://developer-api.govee.com/v1/devices",
             lambda j: [{"device": d.get("device"), "sku": d.get("model"),
                         "deviceName": d.get("deviceName")}
                        for d in (j.get("data") or {}).get("devices", [])])):
        req = urllib.request.Request(url, headers={"Govee-API-Key": key,
                                                   "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return pick(json.loads(r.read().decode("utf-8", "replace")))
        except Exception as e:  # noqa: BLE001
            err = e
    print(f"  ! govee: {err}")
    return []


def _mac_from_govee_id(device_id):
    """Govee ids are 8 bytes ('64:09:C5:32:37:36:2D:13'); the last 6 are a
    guess at the MAC. cloud_merge only uses it if that MAC is on the LAN."""
    p = (device_id or "").split(":")
    return ":".join(p[-6:]).upper() if len(p) == 8 else ""


def discover_govee(c):
    """Govee app names from the cloud API; the MAC from the LAN API (with LAN
    Control on) via ARP, else a guess from the device id."""
    devs = _govee_cloud_devices(c["api_key"])
    lan = govee_lan_scan()
    ip2mac = _resolve_arp({ip for ip, _ in lan.values()})
    out, seen = [], set()
    for d in devs:
        did = (d.get("device") or "").upper()
        seen.add(did)
        ip = lan.get(did, ("", ""))[0]
        mac = ip2mac.get(ip, "") if ip else ""
        out.append(_item("govee", d.get("deviceName"), mac=mac or _mac_from_govee_id(did),
                         ip=ip, model=d.get("sku", "")))
    for did, (ip, sku) in lan.items():        # LAN-only: answered but not on the account
        if did not in seen:
            out.append(_item("govee", f"Govee {sku}".strip(), mac=ip2mac.get(ip, ""),
                             ip=ip, model=sku))
    print(f"  govee: {len(devs)} cloud devices, {len(lan)} answered on the LAN")
    return out


def discover_apple(c):
    """Apple/iCloud (Find My) via pyicloud. Returns the device ROSTER (name +
    model) only - Apple exposes no MAC/IP, and Wi-Fi MACs are randomized, so
    these land in cloud_devices.csv as a reference. Map them to a MAC once by
    reading each device's Wi-Fi Address (Settings > Wi-Fi > (i)) and running
    `./netinv.py name "<name>" <mac>`; the private MAC is stable per network."""
    try:
        from pyicloud import PyiCloudService
    except ImportError:
        print("  ! apple: `pip install pyicloud` first")
        return []
    try:
        api = PyiCloudService(c["apple_id"], c["password"])
        if api.requires_2fa:
            code = input("  Apple 2FA code: ").strip()
            api.validate_2fa_code(code)
            if not api.is_trusted_session:
                api.trust_session()
        out = []
        for d in api.devices:
            content = getattr(d, "content", {}) or {}
            name = content.get("name", "")
            model = content.get("deviceDisplayName") or content.get("modelDisplayName", "")
            out.append(_item("apple", name, model=model))
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  ! apple: {e}")
        return []


# --------------------------------------------------------------------------- #
# Kasa (TP-Link) — the local protocol on port 9999, no cloud or account. JSON
# is XOR-"encrypted" with a rolling key (171); TCP adds a 4-byte length prefix.
# Older Kasa (HS100/HS103/HS200/KP405...) speaks it; newer KLAP-only firmware
# does not answer and is simply not found.
# --------------------------------------------------------------------------- #
KASA_PORT = 9999
KASA_MAX_ALIAS = 31        # longer aliases are refused by the device


def _kasa_xor(data, encrypt):
    key, out = 171, bytearray()
    for b in data:
        out.append(key ^ b)
        key = out[-1] if encrypt else b
    return bytes(out)


def kasa_discover(timeout=3, broadcast="255.255.255.255", arp=None):
    """[{ip, mac, alias, model}] for every Kasa device answering a UDP
    get_sysinfo broadcast. Any LAN host can answer and claim any MAC, so a
    reply counts only from port 9999 and when its MAC matches the sender's
    ARP entry (arp: {ip: MAC}, looked up if not given)."""
    import socket
    import time
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    found = {}
    try:
        msg = _kasa_xor(json.dumps({"system": {"get_sysinfo": {}}}).encode(), True)
        # UDP broadcast is lossy: re-send through the window, spaced apart.
        sends = [i * timeout / 4 for i in range(4)]
        start = time.monotonic()
        end = start + timeout
        while time.monotonic() < end:
            while sends and time.monotonic() - start >= sends[0]:
                sends.pop(0)
                s.sendto(msg, (broadcast, KASA_PORT))
            try:
                data, (ip, port) = s.recvfrom(8192)
            except socket.timeout:
                continue
            if ip in found or port != KASA_PORT:
                continue
            try:
                info = json.loads(_kasa_xor(data, False))["system"]["get_sysinfo"]
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(info, dict):
                continue
            found[ip] = {"ip": ip, "mac": _norm_mac(info.get("mac") or info.get("mic_mac") or ""),
                         "alias": str(info.get("alias") or "").strip(),
                         "model": str(info.get("model") or "").strip()}
    finally:
        s.close()
    if arp is None:
        arp = _resolve_arp(set(found))
    ok, dropped = [], []
    for d in sorted(found.values(), key=lambda d: d["ip"]):
        (ok if d["mac"] and arp.get(d["ip"]) == d["mac"] else dropped).append(d)
    for d in dropped:
        print(f"  ! kasa: ignoring {d['ip']}: claims MAC {d['mac'] or '?'}, "
              f"ARP says {arp.get(d['ip']) or 'nothing'}")
    return ok


def kasa_rename(ip, alias, timeout=4, port=KASA_PORT):
    """Set a Kasa device's app name over TCP. Returns (ok, message)."""
    import socket
    import struct
    if len(alias) > KASA_MAX_ALIAS:
        return False, f"longer than {KASA_MAX_ALIAS} characters"
    body = _kasa_xor(json.dumps({"system": {"set_dev_alias": {"alias": alias}}}).encode(), True)
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.sendall(struct.pack(">I", len(body)) + body)
            head = s.recv(4)
            if len(head) < 4:
                return False, "no reply"
            want, buf = min(struct.unpack(">I", head)[0], 65536), b""
            while len(buf) < want:
                chunk = s.recv(want - len(buf))
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(_kasa_xor(buf, False))["system"]["set_dev_alias"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, str(e)
    if isinstance(reply, dict) and reply.get("err_code") == 0:
        return True, "renamed"
    return False, str(reply)


def discover_kasa(_c):
    """Kasa app names (aliases) by MAC, straight from the devices on the LAN."""
    return [_item("kasa", d["alias"], mac=d["mac"], ip=d["ip"], model=d["model"])
            for d in kasa_discover() if d["alias"]]


# --------------------------------------------------------------------------- #
# Home Assistant — the websocket API (the only one that edits the device and
# area registries). Needs aiohttp, which blink already uses.
# --------------------------------------------------------------------------- #
def ha_call(c, commands, timeout=15):
    """Run [{"type": ..., ...}] websocket commands in order; return their
    results. Raises RuntimeError on auth failure or a failed command."""
    import asyncio
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("`pip install aiohttp` first")
    url = c["url"].rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(f"url must start with http:// or https:// (got {url!r})")
    ws_url = "ws" + url[4:] + "/api/websocket"

    async def run():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.ws_connect(ws_url, max_msg_size=32 * 1024 * 1024) as ws:
                await ws.receive_json()                               # auth_required
                await ws.send_json({"type": "auth", "access_token": c["token"]})
                if (await ws.receive_json()).get("type") != "auth_ok":
                    raise RuntimeError("Home Assistant rejected the token")
                out = []
                for i, cmd in enumerate(commands, start=1):
                    await ws.send_json({"id": i, **cmd})
                    while True:                                       # skip event messages
                        msg = await ws.receive_json()
                        if msg.get("id") == i and msg.get("type") == "result":
                            break
                    if not msg.get("success"):
                        raise RuntimeError(f"{cmd['type']}: {msg.get('error')}")
                    out.append(msg.get("result"))
                return out
    return asyncio.run(run())


def ha_registry(c):
    """(devices, areas) from Home Assistant: each device as {id, name, mac,
    area_id, model}; areas as {area_id: name}. mac is '' for devices with no
    network MAC connection (Zigbee, cloud-only, ...)."""
    devs, areas = ha_call(c, [{"type": "config/device_registry/list"},
                              {"type": "config/area_registry/list"}])
    out = []
    for d in devs or []:
        if d.get("entry_type") == "service":       # Sun, Forecast, Backup...: not devices
            continue
        macs = [_norm_mac(v) for k, v in d.get("connections") or [] if k == "mac"]
        out.append({"id": d["id"], "name": d.get("name_by_user") or d.get("name") or "",
                    "mac": macs[0] if macs else "", "area_id": d.get("area_id") or "",
                    "model": d.get("model") or ""})
    return out, {a["area_id"]: a["name"] for a in areas or []}


def discover_ha(c):
    """Home Assistant device names, by MAC where HA knows one."""
    try:
        devs, _ = ha_registry(c)
    except Exception as e:  # noqa: BLE001
        print(f"  ! ha: {e}")
        return []
    # Only devices with a MAC: HA also lists hundreds of entity-only and Zigbee
    # devices that would flood cloud_devices.csv with no way to match them.
    return [_item("ha", d["name"], mac=d["mac"], model=d["model"])
            for d in devs if d["name"] and d["mac"]]


DISCOVERERS = {
    "ha": discover_ha,
    "kasa": discover_kasa,
    "tuya": discover_tuya,
    "wyze": discover_wyze,
    "blink": discover_blink,
    "smartthings": discover_smartthings,
    "govee": discover_govee,
    "apple": discover_apple,
}


def discover(providers=None):
    """Run the given (or all configured) connectors; return normalized items."""
    if providers is None:
        providers = configured_providers()
    items = []
    for p in providers:
        c = creds(p)
        if c is None:
            print(f"  ! {p}: not configured (run ./netinv.py set-cloud {p})")
            continue
        got = DISCOVERERS[p](c)
        print(f"  {p}: {len(got)} devices")
        items += got
    return items
