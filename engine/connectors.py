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
"netinv-cloud-<provider>", one account per field. Where there is no Keychain
(Linux, e.g. a Raspberry Pi) they go in ~/.netinv/credentials.json, readable
by its owner only ($NETINV_CREDENTIALS moves it). Store them with:

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
import urllib.error
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
    "flume": [
        ("client_id",     False, "Flume API Client ID (portal.flumewater.com > Settings > API Access)"),
        ("client_secret", True,  "Flume API Client Secret"),
        ("username",      False, "Flume account email"),
        ("password",      True,  "Flume account password"),
    ],
    # The Govee app's own account API: the only place leak sensors behind a
    # Govee hub report battery and online. Unofficial; see govee_app_login.
    "govee-app": [
        ("email",    False, "Govee account email"),
        ("password", True,  "Govee account password"),
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


def _keychain():
    import shutil
    import sys
    return sys.platform == "darwin" and shutil.which("security") is not None


def _cred_file():
    return os.path.expanduser(os.environ.get("NETINV_CREDENTIALS", "~/.netinv/credentials.json"))


def _cred_load():
    try:
        with open(_cred_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _cred_save(d):
    path = _cred_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)           # the mode above only applies to a new file
    os.replace(tmp, path)


def kc_set(provider, account, secret):
    if not _keychain():
        d = _cred_load()
        d.setdefault(provider, {})[account] = secret
        _cred_save(d)
        return
    subprocess.run(
        ["security", "add-generic-password", "-s", _service(provider),
         "-a", account, "-w", secret, "-U",
         "-l", f"netinv {provider} {account}"],
        check=True, capture_output=True, text=True)


def kc_delete(provider, account):
    if not _keychain():
        d = _cred_load()
        if account in d.get(provider, {}):
            del d[provider][account]
            _cred_save(d)
        return
    subprocess.run(
        ["security", "delete-generic-password", "-s", _service(provider),
         "-a", account], capture_output=True, text=True)


def kc_get(provider, account):
    if not _keychain():
        return _cred_load().get(provider, {}).get(account) or None
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


class TuyaRooms:
    """Room writes for the Smart Life app: create a room, put a device in it.
    Rooms are per home; a device's home is its owner_id."""

    def __init__(self, c):
        self.cloud = _tuya_cloud(c)
        self.home_of = {d["id"]: d.get("owner_id") for d in
                        self.cloud.getdevices(True).get("result", []) if d.get("id")}
        self._rooms = {}

    def rooms(self, home_id, fresh=False):
        """{room name (stripped): room_id} for one home."""
        if fresh or home_id not in self._rooms:
            r = self.cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms")
            self._rooms[home_id] = {x["name"].strip(): x["room_id"]
                                    for x in (r.get("result") or {}).get("rooms", [])}
        return self._rooms[home_id]

    def room_devices(self, home_id, room_id):
        r = self.cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms/{room_id}/devices")
        return {x.get("id") for x in (r.get("result") or [])}

    def move(self, device_ids, room):
        """Put the devices (one home's) in `room`, creating the room first if
        the home has none by that name. Tuya's room-devices call REPLACES the
        room's device list, so this sends what the room holds now plus the
        new devices in one call. Returns (ok, message)."""
        homes = {self.home_of.get(i) for i in device_ids}
        if None in homes or len(homes) != 1:
            return False, "unknown device or devices in different homes"
        home_id = homes.pop()
        if room.strip() not in self.rooms(home_id):
            r = self.cloud.cloudrequest(f"/v1.0/homes/{home_id}/room", action="POST",
                                        post={"name": room.strip()})
            if not r.get("success"):
                return False, f"create room: {r.get('msg', r)}"
            self.rooms(home_id, fresh=True)
        room_id = self.rooms(home_id).get(room.strip())
        if not room_id:
            return False, "room not found after create"
        keep = self.room_devices(home_id, room_id)
        ids = sorted(keep | set(device_ids))
        r = self.cloud.cloudrequest(f"/v1.0/homes/{home_id}/rooms/{room_id}/devices",
                                    action="POST", post={"device_ids": ",".join(ids)})
        if not r.get("success"):
            return False, str(r.get("msg", r))
        now = self.room_devices(home_id, room_id)
        lost = keep - now
        if lost or not set(device_ids) <= now:
            return False, f"room holds {len(now)} of {len(ids)} after the update"
        return True, "moved"


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
    """Ping each IP to refresh the ARP cache, then return {ip: MAC}. IPs come
    from LAN replies, so anything that is not a plain IPv4 address is dropped
    before it reaches the ping argv."""
    import ipaddress
    ok = set()
    for ip in ips or ():
        try:
            ok.add(str(ipaddress.IPv4Address(ip)))
        except ValueError:
            pass
    ips = sorted(ok)[:256]
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
    # Minis and doorbells aren't at this endpoint (404, which blinkpy logs as
    # "Connection error ... possibly down or throttled"), and their own
    # config endpoints carry no MAC or IP either.
    if getattr(cam, "camera_type", "") in ("mini", "doorbell"):
        return "", ""
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
        # Details sit under the integration type: d["lan"], d["zigbee"], d["ocf"]...
        # A LAN device's networkId is its MAC (12 hex); a Zigbee networkId is a
        # 4-hex short address, which the length check rules out.
        sub = d.get((d.get("type") or "").lower()) or {}
        mac = ""
        for v in (sub.get("networkId"), sub.get("macAddress"), (d.get("ocf") or {}).get("macAddress")):
            if v and re.fullmatch(r"[0-9A-Fa-f]{12}|([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", str(v)):
                mac = _norm_mac(":".join(re.findall(r"[0-9A-Fa-f]{2}", str(v))))
                break
        model = (sub.get("modelName") or (d.get("ocf") or {}).get("modelNumber")
                 or sub.get("manufacturerName") or "")
        out.append(_item("smartthings", name, mac=mac, model=model))
    return out


def _st(c, path, method="GET", body=None):
    """One SmartThings REST call; returns the decoded JSON (raises on HTTP errors)."""
    req = urllib.request.Request(
        "https://api.smartthings.com/v1" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {c['token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


def _st_items(c, path):
    """Every item of a paged SmartThings list."""
    out = []
    while path:
        data = _st(c, path)
        out.extend(data.get("items", []))
        nxt = (data.get("_links") or {}).get("next") or {}
        path = nxt.get("href", "").split("/v1", 1)[-1] if nxt.get("href") else ""
    return out


class SmartThingsRooms:
    """Rooms in the SmartThings app: read them, create one, put a device in it.
    Rooms are per location; a device carries its locationId and roomId."""

    def __init__(self, c):
        self.c = c
        self.devices = {d["deviceId"]: d for d in _st_items(c, "/devices")}
        self._rooms = {}

    def rooms(self, location_id, fresh=False):
        """{room name (stripped): roomId} for one location."""
        if fresh or location_id not in self._rooms:
            self._rooms[location_id] = {r["name"].strip(): r["roomId"] for r in
                                        _st_items(self.c, f"/locations/{location_id}/rooms")}
        return self._rooms[location_id]

    def app_rooms(self):
        """{room name: [(device label, deviceId)]} in the shape tuya_rooms
        returns; devices in no room sit under ''."""
        out = {}
        for loc in {d.get("locationId") for d in self.devices.values() if d.get("locationId")}:
            for name in self.rooms(loc):
                out.setdefault(name, [])
        for d in self.devices.values():
            names = {v: k for k, v in self.rooms(d["locationId"]).items()} if d.get("locationId") else {}
            out.setdefault(names.get(d.get("roomId"), ""), []).append(
                (d.get("label") or d.get("name") or "", d["deviceId"]))
        return out

    def move(self, device_ids, room):
        """Put the devices in `room`, creating it in their location when it is
        missing. Returns (ok, message)."""
        try:
            for i in device_ids:
                d = self.devices[i]
                loc = d["locationId"]
                room_id = self.rooms(loc).get(room.strip())
                if not room_id:
                    _st(self.c, f"/locations/{loc}/rooms", "POST", {"name": room.strip()})
                    room_id = self.rooms(loc, fresh=True).get(room.strip())
                if not room_id:
                    return False, "room not found after create"
                _st(self.c, f"/devices/{i}", "PUT",
                    {"label": d.get("label") or d.get("name"), "roomId": room_id})
                if _st(self.c, f"/devices/{i}").get("roomId") != room_id:
                    return False, f"'{d.get('label')}' did not take the room"
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return True, "moved"


def govee_lan_scan(timeout=4.0):
    """{device id: (LAN IP, sku)} from Govee's LAN API: a multicast scan to
    239.255.255.250:4001, answered on UDP 4002. Only devices with LAN Control
    switched on in the Govee app answer."""
    import socket
    import time
    found, clash = {}, set()
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
            did, sku = str(d.get("device", "")).upper(), str(d.get("sku", ""))
        except (socket.timeout, ValueError, KeyError, TypeError, AttributeError):
            continue
        # Unauthenticated UDP: take the packet's real source address, never the
        # self-reported ip, so a device can only describe its own MAC.
        if not re.fullmatch(r"([0-9A-F]{2}:){7}[0-9A-F]{2}", did):
            continue
        # The id itself is self-reported: an id two hosts claim is ambiguous
        # (one may be spoofing it), so it is dropped below. Checked before the
        # cap, so flooding the scan with ids cannot hide a second claim.
        if did in found:
            if found[did][0] != addr[0]:
                clash.add(did)
            continue
        if len(found) < 256:
            found[did] = (addr[0], sku if re.fullmatch(r"[A-Za-z0-9_-]{1,16}", sku) else "")
    rx.close()
    tx.close()
    for did in clash:
        found.pop(did, None)
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


def _govee_hub_mac(device_id):
    """A sensor paired to a Govee Wi-Fi hub has a colon-less 18-hex id: the
    hub's MAC with the locally-administered bit set, then the sensor's own
    suffix ('2EF43291D1D800A00A' -> hub 2C:F4:32:91:D1:D8)."""
    s = (device_id or "").upper()
    if not re.fullmatch(r"[0-9A-F]{18}", s):
        return ""
    first = int(s[:2], 16) & ~0x02
    return ":".join([f"{first:02X}"] + [s[i:i + 2] for i in range(2, 12, 2)])


def discover_govee(c):
    """Govee app names from the cloud API; the MAC from the LAN API (with LAN
    Control on) via ARP, else a guess from the device id."""
    devs = _govee_cloud_devices(c["api_key"])
    lan = govee_lan_scan()
    ip2mac = _resolve_arp({ip for ip, _ in lan.values()})
    out, seen, hubs = [], set(), {}
    for d in devs:
        did = (d.get("device") or "").upper()
        seen.add(did)
        hub = _govee_hub_mac(did)
        if hub:                                # the sensor itself has no Wi-Fi
            hubs.setdefault(hub, d.get("sku", ""))
            out.append(_item("govee", d.get("deviceName"), model=d.get("sku", "")))
            continue
        ip = lan.get(did, ("", ""))[0]
        mac = ip2mac.get(ip, "") if ip else ""
        out.append(_item("govee", d.get("deviceName"), mac=mac or _mac_from_govee_id(did),
                         ip=ip, model=d.get("sku", "")))
    for hub, sku in hubs.items():
        out.append(_item("govee", f"Govee Hub ({sku} sensors)", mac=hub, model="hub"))
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


# --------------------------------------------------------------------------- #
# Govee app account API (app2.govee.com). Undocumented: the endpoints and
# headers follow homebridge-govee. The developer API key above reports every
# Bluetooth and hub sensor offline; this one has their real state.
# --------------------------------------------------------------------------- #
GOVEE_APP = "https://app2.govee.com"
GOVEE_APP_VERSION = "7.4.10"
GOVEE_TOKEN_FILE = os.path.expanduser("~/.netinv_govee.json")


class GoveeCodeRequired(RuntimeError):
    """Govee emailed a verification code; finish with `netinv govee-login`."""


def _govee_app_headers(email, token=None):
    import time
    import uuid
    # Stable per account, like the app's install id: a new id every run
    # would look like a new phone logging in each time.
    client = "ni" + uuid.uuid5(uuid.NAMESPACE_URL, "netinv:" + email.lower()).hex[:30]
    h = {"appVersion": GOVEE_APP_VERSION, "clientId": client, "clientType": "1",
         "iotVersion": "0", "timestamp": str(int(time.time() * 1000)),
         "Content-Type": "application/json",
         "User-Agent": f"GoveeHome/{GOVEE_APP_VERSION} (com.ihoment.GoVeeSensor; build:8; "
                       "iOS 26.5.0) Alamofire/5.11.0"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h, client


def _govee_app_call(path, email, token=None, body=None):
    h, _client = _govee_app_headers(email, token)
    req = urllib.request.Request(GOVEE_APP + path, headers=h, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return {**json.loads(e.read().decode("utf-8", "replace") or "{}"), "_http": e.code}
        except ValueError:
            return {"_http": e.code, "message": str(e)}


def govee_app_login(c, code=None):
    """Log in to the Govee app API and cache the token (owner-only file).
    Raises GoveeCodeRequired after asking Govee to email a code, when the
    account wants one and none was given."""
    _h, client = _govee_app_headers(c["email"])
    body = {"email": c["email"], "password": c["password"], "client": client}
    if code:
        body["code"] = code
    j = _govee_app_call("/account/rest/account/v2/login", c["email"], body=body)
    if j.get("status") == 454:
        if code:
            raise RuntimeError("Govee rejected the verification code")
        _govee_app_call("/account/rest/account/v1/verification", c["email"],
                        body={"type": 8, "email": c["email"]})
        raise GoveeCodeRequired("Govee emailed a verification code; run: netinv govee-login")
    token = (j.get("client") or {}).get("token")
    if not token:
        raise RuntimeError(f"Govee login failed: {j.get('message') or j.get('status') or j}")
    with open(os.open(GOVEE_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"email": c["email"], "token": token}, f)
    os.chmod(GOVEE_TOKEN_FILE, 0o600)     # the mode above only applies to a new file
    return token


def _govee_cached_token(email):
    try:
        with open(GOVEE_TOKEN_FILE) as f:
            d = json.load(f)
        return d["token"] if d.get("email") == email else None
    except (OSError, ValueError, KeyError):
        return None


def govee_app_devices(c):
    """The account's devices with their deviceExt, logging in only when
    there's no cached token or it has expired."""
    def fetch(token):
        j = _govee_app_call("/bff-app/v1/device/list", c["email"], token=token)
        devs = (j.get("data") or {}).get("devices")
        return devs, j
    token = _govee_cached_token(c["email"])
    if token:
        devs, _j = fetch(token)
        if devs is not None:
            return devs
    devs, j = fetch(govee_app_login(c))      # stale or no token: one fresh login
    if devs is None:
        raise RuntimeError(f"Govee device list failed: {j.get('message') or j.get('status') or j}")
    return devs


def _json_field(s):
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def govee_app_reading(d):
    """One Govee app device -> a monitor reading, or None. Only sensors
    behind a Govee hub (those reporting gwonline: leak sensors) are live
    here; online only while the hub is too, as in the Govee app. Bluetooth
    thermometers sync only when a phone's Govee app is near, so their
    'offline' and battery can be years old (the Bluetooth scan reads them
    live), and Wi-Fi appliances are better judged on the network."""
    ext = d.get("deviceExt") or {}
    settings = _json_field(ext.get("deviceSettings"))
    data = _json_field(ext.get("lastDeviceData"))
    if "gwonline" not in data:
        return None
    online = bool(data.get("online")) and bool(data["gwonline"])
    # lastTime is the last leak alert, not a check-in: shown, never a heartbeat.
    last = data.get("lastTime") or 0
    seen = _epoch_iso(int(last) // 1000) if isinstance(last, (int, float)) and last > 0 else ""
    return _reading("govee-app", d.get("deviceName"), settings.get("battery"), online,
                    model=d.get("sku", ""), seen=seen)


def battery_govee_app(c):
    """Govee sensors' battery and online state from the app API: one call
    with a cached token. Covers H5054/H5058 leak sensors behind a hub."""
    return [r for r in (govee_app_reading(d) for d in govee_app_devices(c)) if r]


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


def flume_devices(c):
    """Flume devices from its REST API: the Wi-Fi bridge (type 1) and the
    battery sensor on the water meter (type 2). Two calls: token, device list."""
    import base64
    body = json.dumps({"grant_type": "password", "client_id": c["client_id"],
                       "client_secret": c["client_secret"], "username": c["username"],
                       "password": c["password"]}).encode()
    req = urllib.request.Request("https://api.flumewater.com/oauth/token", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        token = json.loads(r.read())["data"][0]["access_token"]
    # The user id is in the JWT's payload; no extra call needed.
    payload = token.split(".")[1]
    uid = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["user_id"]
    req = urllib.request.Request(
        f"https://api.flumewater.com/users/{uid}/devices?user=false&location=true",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()).get("data", [])


def discover_flume(c):
    """Flume names for the cloud reference list (the bridge is on Wi-Fi with
    no MAC in the API; the meter sensor is radio-only)."""
    try:
        devs = flume_devices(c)
    except Exception as e:  # noqa: BLE001
        print(f"  ! flume: {e}")
        return []
    return [_item("flume", _flume_name(d), model="bridge" if d.get("type") == 1 else "sensor",
                  online=bool(d.get("connected", True))) for d in devs]


def _flume_name(d):
    return "Flume Bridge" if d.get("type") == 1 else "Flume Sensor"


# --------------------------------------------------------------------------- #
# Battery + cloud-online readings, for monitors (see monitor.py). Each returns
# [{provider, name, mac, level, pct, online, model, seen}] with one or two
# calls per cloud: level is what the cloud said ('low', 84), pct its percent.
# --------------------------------------------------------------------------- #
def _reading(provider, name, level, online=None, mac="", model="", seen=""):
    from monitor import battery_pct
    return {"provider": provider, "name": (name or "").strip(), "mac": mac,
            "level": level, "pct": battery_pct(level), "online": online,
            "model": model, "seen": seen}


def battery_ha(c):
    """Every Home Assistant battery entity (whatever integration feeds it:
    SmartThings, Zigbee, BLE...), named after its device. One local call."""
    states, ents, devs = ha_call(c, [{"type": "get_states"},
                                     {"type": "config/entity_registry/list"},
                                     {"type": "config/device_registry/list"}], timeout=30)
    dev_name = {d["id"]: d.get("name_by_user") or d.get("name") or "" for d in devs or []}
    dev_mac = {d["id"]: next((_norm_mac(v) for k, v in d.get("connections") or [] if k == "mac"), "")
               for d in devs or []}
    ent_dev = {e["entity_id"]: e.get("device_id") for e in ents or []}
    out = []
    for s in states or []:
        a = s.get("attributes") or {}
        eid = s["entity_id"]
        if a.get("device_class") != "battery" or not eid.startswith(("sensor.", "binary_sensor.")):
            continue
        did = ent_dev.get(eid)
        name = dev_name.get(did) or re.sub(r"\s*battery$", "", a.get("friendly_name", ""), flags=re.I)
        online = s["state"] not in ("unavailable", "unknown")
        level = s["state"] if online else None
        if eid.startswith("binary_sensor."):          # on = battery low
            level = ("low" if s["state"] == "on" else "ok") if online else None
        out.append(_reading("ha", name, level, online, mac=dev_mac.get(did, ""),
                            seen=s.get("last_reported") or s.get("last_changed", "")))
    return out


# Tuya status codes that carry a battery, best first. residual_electricity is
# a percent on some locks and a stuck 0 on others, so a battery_state label
# beside it wins (see _tuya_level).
TUYA_PCT = ("battery_percentage", "residual_electricity", "va_battery", "battery")


def _tuya_level(status):
    codes = {x.get("code"): x.get("value") for x in status or []}
    pct = next((codes[k] for k in TUYA_PCT if isinstance(codes.get(k), (int, float))
                and not isinstance(codes.get(k), bool)), None)
    label = codes.get("battery_state")
    if label and (pct is None or pct == 0):
        return label
    return pct


def battery_tuya(c):
    """Tuya/Smart Life battery devices (locks, door and flood sensors). The
    device list carries each one's status, so this is a single call."""
    resp = _tuya_cloud(c).getdevices(True)
    devs = resp.get("result", []) if isinstance(resp, dict) else []
    out = []
    for d in devs:
        level = _tuya_level(d.get("status"))
        if level is None:
            continue
        seen = d.get("update_time")
        out.append(_reading("tuya", d.get("name"), level, bool(d.get("online")),
                            model=d.get("product_name", ""),
                            seen=_epoch_iso(seen) if seen else ""))
    return out


def _epoch_iso(t):
    import datetime as dt
    try:
        return dt.datetime.fromtimestamp(int(t)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def battery_blink(c):
    """Blink battery cameras and doorbells from the homescreen the login
    already fetched: 'ok' or 'low', and offline when Blink says so or the
    camera's sync module is down."""
    async def work(blink):
        hs = blink.homescreen or {}
        sync_up = {s.get("network_id"): s.get("status") == "online"
                   for s in hs.get("sync_modules", [])}
        out = []
        for d in hs.get("cameras", []) + hs.get("doorbells", []):
            if not d.get("battery"):
                continue                       # wired: Minis, some doorbells
            bars = (d.get("signals") or {}).get("battery")
            online = d.get("status") != "offline" and sync_up.get(d.get("network_id"), True)
            out.append(_reading("blink", d.get("name"), d["battery"], online,
                                model=d.get("type", "") + (f" {bars}/3 bars" if bars else ""),
                                seen=d.get("updated_at", "")))
        return out
    return blink_run(c, work) or []


def battery_flume(c):
    """Flume sensor battery ('high' / 'medium' / 'low') and connection."""
    out = []
    for d in flume_devices(c):
        if d.get("type") != 2:
            continue                          # the bridge is mains powered
        out.append(_reading("flume", _flume_name(d), d.get("battery_level"), d.get("connected"),
                            model="sensor", seen=d.get("last_seen", "")))
    return out


def ble_name(name):
    """A Bluetooth advertiser's name as the Govee app spells the device:
    'GVH5101_4670' / 'Govee_H5074_0447' / 'ihoment_H5043_7038' -> 'H5101_4670'."""
    return re.sub(r"^(GV(?=H\d)|Govee_|ihoment_)", "", (name or "").strip())


def govee_ble_battery(mfr):
    """Battery percent from a Govee thermometer's BLE advertisement, or None.
    mfr: {company id: bytes}. Layouts as decoded by Home Assistant's govee-ble."""
    d = mfr.get(0xEC88)
    if d is not None and len(d) == 7:          # H5074: temp, humidity, battery
        return d[5]
    if d is not None and len(d) == 6:          # H5072 / H5075: packed 3 bytes, battery
        return d[4]
    d = mfr.get(0x0001)
    if d is not None and len(d) == 6:          # H5101 / H5102 / H5174 / H5177
        return d[5]
    return None


def battery_ble(_c=None, seconds=30):
    """Bluetooth devices this Mac hears in a passive scan (it listens, sends
    nothing, and uses no network). Sensors that broadcast every few seconds
    still get missed by a short scan, hence 30s; a monitor's OfflineAfter
    spans many scans, so one miss never alerts. Every named advertiser counts as online,
    with a battery for Govee thermometers. None if the scan can't run, or
    hears nothing at all (Bluetooth off, or no permission), so nobody is
    called offline because the radio was."""
    try:
        import asyncio
        from bleak import BleakScanner
    except ImportError:
        return None
    heard = {}

    def seen(dev, adv):
        name = ble_name(adv.local_name or dev.name or "")
        if name:
            pct = govee_ble_battery(adv.manufacturer_data or {})
            if pct is not None or name not in heard:
                heard[name] = pct

    async def scan():
        async with BleakScanner(seen):
            await asyncio.sleep(seconds)
    try:
        asyncio.run(scan())
    except Exception as e:  # noqa: BLE001
        print(f"  ! bluetooth scan: {e}")
        return None
    if not heard:
        return None
    return [_reading("ble", n, pct, True, model="bluetooth") for n, pct in heard.items()]


BATTERY_READERS = {"ha": battery_ha, "tuya": battery_tuya, "blink": battery_blink,
                   "flume": battery_flume, "govee-app": battery_govee_app}


def batteries(providers=None, log=print, ble=True):
    """Battery/online readings from every configured cloud that has them,
    plus this Mac's Bluetooth scan. One failing source is reported and
    skipped, never fatal. A scan that heard nothing adds no readings, so any
    'ble' reading means it ran: how a 'ble' monitor tells 'not heard' from
    'not asked'."""
    out = []
    for p in providers or [p for p in BATTERY_READERS if creds(p) is not None]:
        c = creds(p)
        if c is None or p not in BATTERY_READERS:
            continue
        try:
            got = BATTERY_READERS[p](c)
        except Exception as e:  # noqa: BLE001
            log(f"  ! {p} batteries: {e}")
            continue
        out += got
    if ble:
        got = battery_ble()
        if got is None:
            log("  ! bluetooth: scan unavailable (pip install bleak; allow Bluetooth "
                "for this Python in System Settings > Privacy)")
        else:
            out += got
    return out


DISCOVERERS = {
    "ha": discover_ha,
    "flume": discover_flume,
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
    if providers is None:     # battery-only providers (govee-app) name nothing
        providers = [p for p in configured_providers() if p in DISCOVERERS]
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
