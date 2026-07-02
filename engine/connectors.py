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
    return [p for p in PROVIDERS if creds(p)]


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
                await blink.auth.complete_2fa_login(code)
            await blink.setup_post_verify()
            await blink.save(token_path)
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
        from blinkpy.auth import Auth
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
                blink.auth = Auth(await json_load(token_path), session=session)
            else:
                blink.auth = Auth({"username": c["email"], "password": c["password"]},
                                  no_prompt=True, session=session)
            await blink.start()
            if getattr(blink, "key_required", False):
                if not code:
                    print("  ! blink: 2FA required — Blink just sent a code to your "
                          "email/phone. Re-run with NETINV_BLINK_2FA=<code>")
                    return None
                await blink.auth.send_auth_key(blink, code)
                await blink.setup_post_verify()
            try:
                blink.save(token_path)        # cache token -> no 2FA next time
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


DISCOVERERS = {
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
        if not c:
            print(f"  ! {p}: not configured (run ./netinv.py set-cloud {p})")
            continue
        got = DISCOVERERS[p](c)
        print(f"  {p}: {len(got)} devices")
        items += got
    return items
