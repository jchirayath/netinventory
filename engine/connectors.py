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
    # smartthings uses the standard library only
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

    out = []
    for d in devs:
        did = d.get("id", "")
        lan_ip = id2ip.get(did, "")
        # Authoritative: the live LAN scan + ARP. Fall back to an id-embedded
        # MAC only for all-hex WiFi ids (cloud 'eb...' UUIDs would yield garbage).
        mac = ip2mac.get(lan_ip, "") if lan_ip else ""
        if not mac:
            mac = _mac_from_tuya_id(did)
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


def blink_run(c, work):
    """Log in to Blink (cached token, else credentials) and return
    `await work(blink)`, or None if login fails.

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
        return None
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
            return await work(blink)

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"  ! blink: {e}")
        return None


async def _blink_mac_ip(blink, cam):
    """(MAC, last check-in IP) from a camera's info endpoint; the camera list
    has neither. Minis, doorbells and sync modules report none: ('', '')."""
    from blinkpy import api
    attrs = getattr(cam, "attributes", {}) or {}
    try:
        res = await api.request_camera_info(blink, attrs.get("network_id"), cam.camera_id)
        info = ((res or {}).get("camera") or [{}])[0]
    except Exception:  # noqa: BLE001
        return "", ""
    return (_norm_mac(info.get("mac_address") or ""),
            (info.get("last_connect") or {}).get("ip_address") or "")


def discover_blink(c):
    """Blink camera names, with MAC and IP where the camera reports them."""
    async def work(blink):
        out = []
        for name, cam in blink.cameras.items():
            mac, ip = await _blink_mac_ip(blink, cam)
            out.append(_item("blink", name, mac=mac, ip=ip,
                             model=(getattr(cam, "attributes", {}) or {}).get("type", "")))
        return out
    return blink_run(c, work) or []


# Mini ('owl') cameras stay on Wi-Fi, so waking one changes nothing a sweep sees.
BLINK_ALWAYS_ON = {"owl", "mini"}
# Manufacturers seen on Blink cameras' MACs (older models use TI Wi-Fi chips).
BLINK_VENDORS = re.compile(r"amazon|blink|immedia|texas instruments", re.I)


def blink_locate(c, sweep, only=None, wait=45, settle=90, poll=2, log=print,
                 vendor=lambda mac: ""):
    """Wake each Blink camera in turn (a snapshot) and see which device joins
    the network. sweep() -> {MAC: IP} of hosts answering now; vendor(mac) ->
    manufacturer, used to prefer Blink-like newcomers. Returns one dict per
    camera: {name, kind, api_mac, mac, ip, how}, where how is 'blink' (Blink
    reported the MAC and it showed up), 'appeared' (the one new host, or the
    one Blink-made one), 'ambiguous', 'not seen' or 'always on'."""
    import asyncio
    import time

    async def work(blink):
        loop = asyncio.get_running_loop()
        ever = set()                 # every MAC any sweep in this run has seen

        def likely(hosts):
            """The Blink-made hosts, when vendors are known at all; else all."""
            if not any(vendor(m) for m in hosts):
                return hosts
            return {m: ip for m, ip in hosts.items() if BLINK_VENDORS.search(vendor(m))}

        async def live():
            now = await loop.run_in_executor(None, sweep)
            ever.update(now)
            return now

        # Warm-up: a sweep misses a few flaky hosts (a Mini, a plug), and one
        # that turned up later must not pass for the camera arriving.
        for _ in range(3):
            await live()
        results, claimed = [], set()
        for name, cam in blink.cameras.items():
            if only and name.lower() not in {o.lower() for o in only}:
                continue
            kind = str((getattr(cam, "attributes", {}) or {}).get("type", "")).lower()
            api_mac, _ = await _blink_mac_ip(blink, cam)
            r = {"name": name, "kind": kind, "api_mac": api_mac, "mac": "", "ip": "", "how": ""}
            if kind in BLINK_ALWAYS_ON or cam.__class__.__name__ == "BlinkCameraMini":
                r["how"] = "always on"
                results.append(r)
                log(f"  {name}: Mini, always on Wi-Fi — waking it cannot single it out")
                continue
            await live()
            before = set(ever)           # anything seen so far is not this camera
            log(f"  {name}: waking (snapshot)… {len(before)} hosts seen so far")
            try:
                await cam.snap_picture()
            except Exception as e:  # noqa: BLE001
                r["how"] = f"trigger failed: {e}"
                results.append(r)
                continue
            new, seen, end = {}, {}, time.monotonic() + wait
            while time.monotonic() < end:
                now = await live()
                for m, ip in now.items():
                    if m not in before and m not in claimed:
                        new[m] = ip
                        seen[m] = seen.get(m, 0) + 1
                if api_mac and api_mac in now:
                    r.update(mac=api_mac, ip=now[api_mac], how="blink")
                    break
                if not api_mac and len(new) == 1 and len(likely(new)) == 1 \
                        and seen[next(iter(new))] >= 2:
                    break                            # one Blink-like newcomer, seen twice
                await asyncio.sleep(poll)
            if not r["how"]:
                steady = {m: new[m] for m, n in seen.items() if n >= 2}
                # Other sleepy devices (a Wi-Fi lock, a sensor) wake on their own
                # schedule: only a Blink-made newcomer can be this camera.
                others = {}
                if likely(steady) != steady:
                    steady, others = likely(steady), steady
                if api_mac:
                    _, ip = await _blink_mac_ip(blink, cam)   # Blink's own check-in record
                    r.update(mac=api_mac, ip=ip, how="blink (not seen awake)")
                elif len(steady) == 1:
                    (m, ip), = steady.items()
                    r.update(mac=m, ip=ip, how="appeared")
                else:
                    r["how"] = ("ambiguous" if steady else
                                "no Blink newcomer" if others else "not seen")
                    r["candidates"] = steady or others or new
            if r["mac"]:
                claimed.add(r["mac"])
            log(f"    -> {r['how']}: {r['mac'] or '-'} {r['ip'] or ''}"
                + (f"  candidates {r.get('candidates')}" if r.get("candidates") else ""))
            results.append(r)
            # Let it fall back asleep before the next one, so wake-ups never overlap.
            end = time.monotonic() + settle
            while r["mac"] and time.monotonic() < end and r["mac"] in await live():
                await asyncio.sleep(poll)
        return results

    return blink_run(c, work) or []


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
