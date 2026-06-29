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
    devs = cloud.getdevices(True)  # verbose -> includes mac/ip when known
    if not isinstance(devs, list):
        print(f"  ! tuya: unexpected response: {str(devs)[:120]}")
        return []
    out = []
    for d in devs:
        out.append(_item("tuya", d.get("name"),
                         mac=d.get("mac", ""),
                         ip=d.get("ip", ""),
                         model=d.get("product_name", "") or d.get("category", ""),
                         online=bool(d.get("online", True))))
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


def discover_blink(c):
    """Blink via blinkpy (async). Returns camera names; no MAC (cloud cameras)."""
    try:
        import asyncio
        from aiohttp import ClientSession
        from blinkpy.blinkpy import Blink
        from blinkpy.auth import Auth
    except ImportError:
        print("  ! blink: `pip install blinkpy aiohttp` first")
        return []

    async def _run():
        async with ClientSession() as session:
            blink = Blink(session=session)
            blink.auth = Auth({"username": c["email"], "password": c["password"]},
                              no_prompt=False, session=session)
            await blink.start()
            # 2FA: if a key is required, blinkpy prompts on stdin for the PIN
            await blink.setup_post_verify()
            out = []
            for name, cam in blink.cameras.items():
                ip = ""
                attrs = getattr(cam, "attributes", {}) or {}
                ip = attrs.get("ip_address", "") or ""
                out.append(_item("blink", name, ip=ip,
                                 model=attrs.get("type", "")))
            return out

    try:
        return asyncio.run(_run())
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
