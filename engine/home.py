"""Home device registry: rooms, types and one canonical name per device.

The standard is docs/home-standard.md. Two semicolon CSVs live next to
devices.csv: rooms.csv (Room;Floor;Aliases) and home.csv (one row per device a
person controls). `draft` seeds home.csv from devices.csv; `check` validates
both files against the standard. Standard library only."""
import csv
import os
import re

ROOM_FIELDS = ["Room", "Floor", "Aliases"]
APP_ROOM_FIELDS = ["App", "Device", "Room", "Id"]   # app_rooms.csv, from `home rooms`
# Via: the Device of the hub a device connects or pairs through.
# AppNames: for a device with no MAC, what each app calls it ('govee=H5054_A00A');
# devices with a MAC carry theirs in devices.csv, refreshed by cloud runs.
HOME_FIELDS = ["Device", "Room", "Type", "Name", "Description", "Native", "Expose", "Via",
               "AppNames"]
# types.csv and apps.csv hold the board's own edits; until one exists the
# built-in TYPES / APP_CATALOG below are the list.
TYPE_FIELDS = ["Type", "Infra"]          # Infra 'yes': never exposed to voice apps
APP_FIELDS = ["Key", "Label", "Portal", "Match"]


def parse_app_names(s):
    """'tuya=Loft; smartthings=Fan Light' -> {'tuya': 'Loft', ...}"""
    out = {}
    for part in (s or "").split(";"):
        p, _, n = part.partition("=")
        if p.strip() and n.strip():
            out[p.strip()] = n.strip()
    return out


def format_app_names(d):
    """Inverse of parse_app_names; ';' and '=' in a name become spaces."""
    clean = lambda s: " ".join(re.sub(r"[;=]", " ", s).split())  # noqa: E731
    return "; ".join(f"{p}={clean(n)}" for p, n in sorted(d.items()) if clean(n))

TYPES = ["light", "switch", "plug", "fan", "blind", "garage_door", "lock",
         "thermostat", "camera", "doorbell", "sensor", "speaker", "tv",
         "media_player", "sprinkler", "pool", "energy", "appliance", "hub",
         "network"]
# Infrastructure: tracked with a room, never exposed to a voice app.
INFRA_TYPES = {"hub", "network", "energy"}
VOICE_APPS = {"google", "alexa", "apple"}
APPS = {"ha", "smartthings"} | VOICE_APPS

TODO = "TODO"
MAX_NAME = 30
RESERVED = {"alexa", "google", "echo", "group", "scene", "all", "everything"}
# Brands that must not appear in a canonical Name (they belong in Description).
BRANDS = {"kasa", "wemo", "blink", "wyze", "hualai", "ecobee", "myq", "orbit",
          "b-hyve", "hue", "philips", "roku", "chromecast", "samsung", "sony",
          "onkyo", "logitech", "harmony", "tuya", "smartthings", "tp-link",
          "amazon", "nest", "flume", "sense", "solaredge", "waterguru",
          "u-bolt", "raysharp", "diivoo", "lifx", "sonos"}
_NAME_CHARS = re.compile(r"^[A-Za-z0-9 ]+$")

# Device name / hostname / vendor keywords -> (type, native app). First match wins.
_TYPE_RULES = [
    (r"doorbell", "doorbell", ""),
    (r"sync module", "hub", "blink"),
    (r"blink", "camera", "blink"),
    (r"wyze|hualai", "camera", "wyze"),
    (r"camera|cam\b|dvr|nvr|raysharp", "camera", ""),
    (r"ecobee", "thermostat", "ecobee"),
    (r"amazon smart thermostat", "thermostat", "alexa"),
    (r"thermostat|nest", "thermostat", ""),
    (r"garage|myq", "garage_door", "myq"),
    (r"lock|u-bolt|deadbolt", "lock", ""),
    (r"blind|shade|curtain", "blind", ""),
    (r"b-hyve|orbit|sprinkler|diivoo|rachio", "sprinkler", ""),
    (r"waterguru|pool", "pool", ""),
    (r"sensor|leak|motion|contact", "sensor", ""),
    (r"flume|sense|solaredge|inverter|energy|power ?max", "energy", ""),
    (r"echo|alexa", "speaker", "alexa"),
    (r"google home|home mini|nest audio", "speaker", "google"),
    (r"homepod|sonos|speaker", "speaker", ""),
    (r"\btv\b|television|qn\d", "tv", ""),
    (r"roku|chromecast|apple tv|fire ?tv|blu-ray|receiver|onkyo|harmony", "media_player", ""),
    (r"hub|bridge|smartthings|home assistant|sync module|gateway", "hub", ""),
    (r"extender|access point|wi-fi ap|router|xfinity", "network", ""),
    (r"kasa.*(switch|dimmer)|hs200|kp405", "switch", "kasa"),
    (r"kasa|hs10\d", "plug", "kasa"),
    (r"wemo", "plug", "wemo"),
    (r"led strip|strip lights|bulb|lamp|light", "light", ""),
    (r"plug|outlet|power strip", "plug", ""),
    (r"switch|dimmer", "switch", ""),
]
_NATIVE_BY_VENDOR = [(r"tuya|beken", "smartlife"),
                     (r"philips", "hue"), (r"physical graph|smartthings", "smartthings")]
# Never classified: people's computers and phones, printers, phones, consoles.
_SKIP = re.compile(r"mac studio|macbook|\bmac\b|galaxy|iphone|ipad|pixel|printer|"
                   r"epson|obi\d|voip|polycom|nas|qnap|tesla|wii|xbox|playstation|"
                   r"video conference", re.I)

# The apps that manage devices: (key, label, management portal, match). The
# key is what home.csv Native holds. A portal of LOCAL means the device's own
# web page (http://<its IP>), for gear managed on the LAN. Match runs on name,
# hostname and vendor; first hit wins, so a brand comes before its parent
# company (Blink before Amazon, PetCube before Wyze's Hualai chip).
LOCAL = "local"
APP_CATALOG = [
    ("blink", "Blink", "https://blinkforhome.com", r"blink|sync.module"),
    ("petcube", "Petcube", "https://petcube.com", r"petcube"),
    ("wyze", "Wyze", "https://view.wyze.com", r"wyze|hualai"),
    ("wansview", "Wansview", "https://www.wansview.com", r"wansview"),
    ("foscam", "Foscam", "https://www.myfoscam.com", r"foscam"),
    ("myq", "myQ", "https://www.myq.com", r"myq|chamberlain|liftmaster"),
    ("ecobee", "ecobee", "https://www.ecobee.com/consumerportal/", r"ecobee"),
    ("bhyve", "Orbit B-hyve", "https://techsupport.orbitbhyve.com", r"b-?hyve|orbit irrigation"),
    ("obihai", "Obihai (OBi)", LOCAL, r"obihai|\bobi\d"),
    ("feit", "Feit Electric", "https://www.feit.com", r"\bfeit\b"),
    ("govee", "Govee", "https://www.govee.com", r"govee"),
    ("kasa", "Kasa (TP-Link)", "https://www.tp-link.com/us/kasa-smart/", r"kasa|tp-link"),
    ("wemo", "Wemo", "https://www.wemo.com", r"wemo|belkin"),
    ("hue", "Philips Hue", "https://account.meethue.com", r"\bhue\b|philips|signify"),
    ("harmony", "Logitech Harmony", "https://setup.myharmony.com", r"harmony|logitech"),
    ("roku", "Roku", "https://my.roku.com", r"roku"),
    ("lg", "LG ThinQ", "https://www.lg.com/us/lg-thinq", r"\blg\b|lg electronics"),
    ("smartthings", "Samsung SmartThings", "https://my.smartthings.com",
     r"samsung|smartthings|physical graph"),
    ("onkyo", "Onkyo", LOCAL, r"onkyo|pioneer"),
    ("qnap", "QNAP", "https://www.myqnapcloud.com", r"qnap"),
    ("asus", "ASUS", LOCAL, r"asus|rp-ac"),
    ("xfinity", "Xfinity", "https://internet.xfinity.com", r"xfinity|technicolor|vantiva"),
    ("utec", "U-tec (U-Bolt)", "https://u-tec.com", r"u-bolt|u-tec|ultraloq"),
    ("sense", "Sense", "https://home.sense.com", r"\bsense\b"),
    ("solaredge", "SolarEdge", "https://monitoring.solaredge.com", r"solaredge"),
    ("flume", "Flume", "https://portal.flumewater.com", r"flume"),
    ("waterguru", "WaterGuru", "https://www.waterguru.com", r"waterguru"),
    ("iaqualink", "iAqualink", "https://www.iaqualink.com", r"iaqualink|jandy"),
    ("epson", "Epson Connect", "https://www.epsonconnect.com", r"epson"),
    ("tesla", "Tesla", "https://www.tesla.com/teslaaccount", r"tesla"),
    ("ha", "Home Assistant", "http://homeassistant.local:8123", r"home ?assistant"),
    ("apple", "Apple", "https://account.apple.com",
     r"apple|iphone|ipad|macbook|mac studio|\bmac\b|mac\d|homepod"),
    ("google", "Google Home", "https://home.google.com", r"google|chromecast|\bnest\b"),
    ("alexa", "Amazon Alexa", "https://alexa.amazon.com", r"amazon|echo|alexa"),
    ("smartlife", "Smart Life (Tuya)", "https://ismartlife.me", r"tuya|beken|smart ?life"),
]
APP_KEYS = [k for k, *_ in APP_CATALOG]
NO_APP = "none"          # Native set to "no app" on purpose (so nothing is guessed)
# AppNames platform keys that are a catalog app under another key.
APP_ALIASES = {"tuya": "smartlife"}
# Hubs and voice apps that also list other vendors' devices: being reported
# by one says nothing about which app the device itself belongs to.
AGGREGATORS = {"ha", "smartthings", "google", "alexa", "apple"}


def app_key(name):
    """A Native or AppNames value as a catalog key ('tuya' -> 'smartlife')."""
    n = (name or "").strip().lower()
    return APP_ALIASES.get(n, n)


def default_apps():
    """APP_CATALOG as apps.csv rows: the list until someone edits it."""
    return [{"Key": k, "Label": lbl, "Portal": url, "Match": pat}
            for k, lbl, url, pat in APP_CATALOG]


def load_apps(path):
    """apps.csv (Key;Label;Portal;Match), in match order; the built-in
    catalog while the file does not exist."""
    return _read(path, APP_FIELDS) if os.path.exists(path) else default_apps()


def _match(pattern, hay):
    try:
        return bool(pattern) and re.search(pattern, hay) is not None
    except re.error:                 # a hand-edited apps.csv; the board rejects these
        return False


def guess_app(row, apps=None):
    """The app a device most likely belongs to, from its name, hostname and
    vendor; '' if nothing matches."""
    hay = " ".join([row.get("Device", ""), row.get("Hostname", ""),
                    row.get("Vendor", "")]).lower()
    return next((a["Key"] for a in (default_apps() if apps is None else apps) if _match(a["Match"], hay)), "")


def device_app(native, reported, row, apps=None):
    """(app, set): a set Native wins; then the vendor cloud that reports the
    device (its AppNames, hubs aside); then a guess from name and vendor."""
    if native.strip():
        n = app_key(native)
        return ("" if n == NO_APP else n), True
    cloud = [app_key(p) for p in reported if app_key(p) not in AGGREGATORS]
    return (cloud[0] if cloud else guess_app(row, apps)), False


def app_catalog(apps=None):
    """The apps as the board sends them: [{key, label, portal, match}],
    by label (match order is the file's)."""
    out = [{"key": a["Key"], "label": a["Label"] or a["Key"], "portal": a["Portal"],
            "match": a["Match"]} for a in (default_apps() if apps is None else apps) if a["Key"]]
    return sorted(out, key=lambda a: a["label"].lower())


_APP_KEY = re.compile(r"^[a-z][a-z0-9]{0,23}$")
_PORTAL = re.compile(r"^https?://[^\s\"'<>]+$")


def _app_fields(label, portal, match):
    """Cleaned (label, portal, match); ValueError if one is not usable."""
    label, portal, match = " ".join(label.split()), portal.strip(), match.strip()
    if not label:
        raise ValueError("app name required")
    if len(label) > 40:
        raise ValueError("app name is longer than 40 characters")
    if portal and portal.lower() != LOCAL and not _PORTAL.match(portal):
        raise ValueError("portal must be an http(s):// address, 'local', or blank")
    if len(match) > 200:
        raise ValueError("match is longer than 200 characters")
    try:
        re.compile(match)
    except re.error as e:
        raise ValueError(f"match is not a valid pattern: {e}") from None
    return label, (LOCAL if portal.lower() == LOCAL else portal), match.lower()


def _find_app(apps, key):
    return next((a for a in apps if a["Key"] == app_key(key)), None)


def add_app(apps, label, portal="", match=""):
    """Add an app; its key comes from the name ('Ring Video' -> 'ringvideo').
    A new app goes first in match order, so a brand beats the parent company
    an older app matches on (Ring before Amazon). Returns the key."""
    label, portal, match = _app_fields(label, portal, match)
    key = re.sub(r"[^a-z0-9]", "", label.lower())[:24]
    if not _APP_KEY.match(key):
        raise ValueError("app name must start with a letter")
    if key == NO_APP or key in APP_ALIASES or _find_app(apps, key):
        raise ValueError(f"an app with key '{key}' already exists")
    apps.insert(0, {"Key": key, "Label": label, "Portal": portal, "Match": match})
    return key


def edit_app(apps, key, label, portal, match):
    """Change an app's name, portal or match. The key stays: home.csv Native
    and the cloud connectors refer to it."""
    row = _find_app(apps, key)
    if row is None:
        raise ValueError(f"unknown app '{key}'")
    row["Label"], row["Portal"], row["Match"] = _app_fields(label, portal, match)
    return row["Key"]


def default_types():
    """TYPES as types.csv rows: the list until someone edits it."""
    return [{"Type": t, "Infra": "yes" if t in INFRA_TYPES else ""} for t in TYPES]


def load_types(path):
    """types.csv (Type;Infra); the built-in TYPES while it does not exist."""
    return _read(path, TYPE_FIELDS) if os.path.exists(path) else default_types()


def type_names(types=None):
    return [t["Type"] for t in (default_types() if types is None else types) if t["Type"]]


def infra_types(types=None):
    """Types never offered to a voice app (hubs, network gear)."""
    return {t["Type"] for t in (default_types() if types is None else types)
            if t["Infra"].strip().lower() in ("yes", "y", "true", "1")}


_TYPE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def type_key(name):
    """'Garage Door' -> 'garage_door'; ValueError if it cannot be a type."""
    key = "_".join(re.sub(r"[-\s]+", " ", name.strip().lower()).split())
    if not key:
        raise ValueError("type name required")
    if not _TYPE_KEY.match(key) or key == TODO.lower():
        raise ValueError("a type is letters, digits and spaces, starting with a letter "
                         "(at most 30)")
    return key


def _find_type(types, typ):
    return next((t for t in types if t["Type"] == typ), None)


def _retype(h, new, infra_before, infra_after):
    """Point one home.csv row at another type. An Expose still at the old
    type's default follows the new one (a hub stops going to voice apps)."""
    if h["Expose"] == default_expose(h["Type"], infra_before):
        h["Expose"] = default_expose(new, infra_after)
    h["Type"] = new


def add_type(types, name, infra=False):
    """Add a type; returns its key ('Pool Pump' -> 'pool_pump')."""
    key = type_key(name)
    if _find_type(types, key):
        raise ValueError(f"type '{key}' already exists")
    types.append({"Type": key, "Infra": "yes" if infra else ""})
    return key


def edit_type(types, home, typ, name, infra):
    """Rename a type and/or set whether it is infrastructure. Its home.csv
    devices follow. Returns (key, devices changed)."""
    row = _find_type(types, typ)
    if row is None:
        raise ValueError(f"unknown type '{typ}'")
    key = type_key(name) if name.strip() else typ
    if key != typ and _find_type(types, key):
        raise ValueError(f"type '{key}' already exists")
    before = infra_types(types)
    row["Type"], row["Infra"] = key, "yes" if infra else ""
    after = infra_types(types)
    moved = 0
    for h in home:
        if h["Type"] == typ:
            _retype(h, key, before, after)
            moved += 1
    return key, moved


def delete_type(types, home, typ, move_to=""):
    """Drop a type; its home.csv devices move to `move_to` (another type) or
    to TODO. Returns how many devices moved."""
    row = _find_type(types, typ)
    if row is None:
        raise ValueError(f"unknown type '{typ}'")
    if move_to and (move_to == typ or not _find_type(types, move_to)):
        raise ValueError(f"cannot move devices to '{move_to}'")
    infra = infra_types(types)
    moved = 0
    for h in home:
        if h["Type"] == typ:
            _retype(h, move_to or TODO, infra, infra)
            moved += 1
    types.remove(row)
    return moved


def delete_app(apps, home, key):
    """Drop an app; its home.csv devices go back to a guess (Native blank).
    Returns how many devices it held."""
    row = _find_app(apps, key)
    if row is None:
        raise ValueError(f"unknown app '{key}'")
    apps.remove(row)
    moved = 0
    for h in home:
        if app_key(h["Native"]) == row["Key"]:
            h["Native"] = ""
            moved += 1
    return moved


# Seed for a missing rooms.csv; Floor is left TODO so `check` asks for it.
STARTER_ROOMS = [("Living Room", ""), ("Family Room", "Den"), ("Kitchen", ""),
                 ("Dining Room", "Dining"), ("Office", ""), ("Bedroom", ""),
                 ("Entry", "Front Door"), ("Stairway", "Stairs"), ("Garage", ""),
                 ("Driveway", ""), ("Backyard", "Patio"), ("Pool", "")]


def starter_rooms():
    return [{"Room": r, "Floor": TODO, "Aliases": a} for r, a in STARTER_ROOMS]


def _read(path, fields):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "").strip() for k in fields}
                for r in csv.DictReader(f, delimiter=";")]


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def load_rooms(path):
    return _read(path, ROOM_FIELDS)


def load_home(path):
    return _read(path, HOME_FIELDS)


def load_app_rooms(path):
    return _read(path, APP_ROOM_FIELDS)


def load_device_rooms(path):
    """{device name: app room} from app_rooms.csv."""
    return {r["Device"]: r["Room"] for r in load_app_rooms(path) if r["Device"]}


def split_list(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def room_words(rooms):
    """Lower-case room name and alias -> canonical Room. A room's own name
    beats another room's alias (Driveway aliased 'Garage' keeps Garage)."""
    out = {}
    for r in rooms:
        for w in split_list(r["Aliases"]):
            out.setdefault(w.lower(), r["Room"])
    for r in rooms:
        out[r["Room"].lower()] = r["Room"]
    return out


def guess_room(device_name, rooms):
    """Room from '(Living Room)' brackets, a name that *is* a room, or a name
    that starts with a room word. Longest match wins; TODO if none."""
    words = room_words(rooms)
    text = device_name.lower()
    bracket = re.search(r"\(([^)]*)\)", text)
    longest_first = sorted(words, key=len, reverse=True)
    if bracket:                              # "Roku Express (Office)"
        for w in longest_first:
            if re.search(r"\b" + re.escape(w) + r"\b", bracket.group(1)):
                return words[w]
    for w in longest_first:                  # "Kitchen", "Kitchen Lamp"
        if text == w or text.startswith(w + " "):
            return words[w]
    return TODO


def clean_room(name):
    return " ".join(name.split())


def merge_rooms(rooms, app_rooms):
    """Add each app room name that rooms.csv does not already know (as a Room
    or an alias) with Floor TODO. Existing rows are never changed; repeated
    app names, such as two 'Closet' rooms, become one row. Returns the new rows."""
    words = room_words(rooms)
    added = []
    for name in map(clean_room, app_rooms):
        if name and name.lower() not in words:
            row = {"Room": name, "Floor": TODO, "Aliases": ""}
            added.append(row)
            words[name.lower()] = name
    rooms.extend(added)
    return added


def app_room(device_name, device_rooms, rooms):
    """The rooms.csv Room for a device's room in a vendor app, or TODO. The
    app's room name may be a Room or one of its aliases."""
    name = clean_room(device_rooms.get(device_name, "")).lower()
    return room_words(rooms).get(name, TODO) if name else TODO


def guess_type(row):
    """(type, native) from the device's name, hostname and vendor."""
    hay = " ".join([row.get("Device", ""), row.get("Hostname", ""),
                    row.get("Vendor", "")]).lower()
    native = ""
    for pattern, app in _NATIVE_BY_VENDOR:
        if re.search(pattern, row.get("Vendor", "").lower()):
            native = app
    for pattern, typ, app in _TYPE_RULES:
        if re.search(pattern, hay):
            return typ, app or native
    return TODO, native


def default_expose(typ, infra=None):
    """Where a new device of this type goes; infra is the set of
    infrastructure types (the built-in ones if None)."""
    if typ in (INFRA_TYPES if infra is None else infra) or typ == TODO:
        return ""
    if typ == "speaker":        # a voice speaker is the endpoint, not a target
        return "ha"
    return "ha, google, alexa, apple"


def draft(master_rows, rooms, home, device_rooms=None):
    """New home.csv rows for every named, controllable device in devices.csv
    that home.csv does not already list. Existing rows are never changed.
    A room in the device's name wins; device_rooms ({device name: app room})
    is the fallback, since apps file unsorted devices under a default room."""
    device_rooms = device_rooms or {}
    known = {h["Device"] for h in home if h["Device"]}
    out, seen = [], set()
    for r in master_rows:
        dev = r.get("Device", "").strip()
        if not dev or dev in known or dev in seen or _SKIP.search(dev):
            continue
        seen.add(dev)
        room = guess_room(dev, rooms)
        if room == TODO:
            room = app_room(dev, device_rooms, rooms)
        out.append(new_row(r, room))
    # Known rooms first, grouped by room then type, so the sheet reads in order.
    return sorted(out, key=lambda r: (r["Room"] == TODO, r["Room"], r["Type"], r["Device"]))


def new_row(master_row, room):
    """A home.csv row for one devices.csv row, with a guessed type."""
    dev = master_row.get("Device", "").strip()
    typ, native = guess_type(master_row)
    return {"Device": dev, "Room": room, "Type": typ, "Name": "",
            "Description": " ".join(x for x in [master_row.get("Vendor", ""), dev] if x),
            "Native": native, "Expose": default_expose(typ), "Via": "", "AppNames": ""}


def _key(h):
    """A home.csv row's id: its Device, or its Name for devices with no
    network presence (e.g. Zigbee bulbs)."""
    return h["Device"] or ("name:" + h["Name"] if h["Name"] else "")


def board(master_rows, rooms, home, types=None, catalog=None):
    """Every device for the room board: each home.csv row plus every named
    device in devices.csv that home.csv does not list yet (unassigned)."""
    status, first, seen_as = {}, {}, {}
    macs, live_ip, any_ip, apps = {}, {}, {}, {}
    for r in master_rows:
        dev = r.get("Device", "").strip()
        if not dev:
            continue
        first.setdefault(dev, r)
        for p, n in parse_app_names(r.get("AppNames", "")).items():
            apps.setdefault(dev, {}).setdefault(p, n)
        if status.get(dev) != "online":
            status[dev] = r.get("Status", "")
        # Every MAC (wired + Wi-Fi); the IP of an online MAC beats a stale one.
        mac = r.get("MAC", "").strip()
        if mac and mac not in macs.setdefault(dev, []):
            macs[dev].append(mac)
        ip = r.get("IP", "").strip()
        if ip:
            any_ip.setdefault(dev, ip)
            if r.get("Status") == "online":
                live_ip.setdefault(dev, ip)
        # The discovered name (e.g. 'Amazon Echo Show') tells look-alike
        # Device names apart; prefer a human one with a space, then any.
        host = r.get("Hostname", "").strip()
        if not host or re.fullmatch(r"[0-9a-f:]{12,17}", host.lower()):
            continue
        prev = seen_as.get(dev)
        if prev is None or (" " in host and " " not in prev[0]):
            seen_as[dev] = (host, r.get("IP", ""))
    room_set = {r["Room"].lower(): r["Room"] for r in rooms}

    def net(dev, via=False):
        host, ip = seen_as.get(dev, ("", ""))
        return {"seen_as": "" if via else host,
                "ip": live_ip.get(dev) or ip or any_ip.get(dev, ""),
                "macs": macs.get(dev, [])}

    devices, seen = [], set()
    for h in home:
        key = _key(h)
        if not key or key in seen:
            continue
        seen.add(key)
        # A hub-linked sensor with no network presence of its own shows the
        # hub's address and status; one with its own MAC (a Blink camera on
        # Wi-Fi, paired to a sync module) keeps its own and just notes the hub.
        via = h.get("Via", "")
        src = h["Device"] or via
        # A row's own AppNames count too: a cloud may list a device with no
        # MAC (Blink doorbells, Minis), so a scan cannot.
        reported = {**parse_app_names(h.get("AppNames", "")), **apps.get(h["Device"], {})}
        # The brand often lives only in Description ('Kasa HS200 wall switch').
        scanned = first.get(h["Device"], {})
        hay = {"Device": h["Device"] or h["Name"], "Hostname": scanned.get("Hostname", ""),
               "Vendor": " ".join([scanned.get("Vendor", ""), h["Description"]])}
        app, app_set = device_app(h.get("Native", ""), reported, hay, catalog)
        devices.append({"id": key, "device": h["Device"], "name": h["Name"],
                        "type": h["Type"], "room": room_set.get(h["Room"].lower(), ""),
                        "status": status.get(src, ""),
                        **net(src, not h["Device"] and bool(via)), "via": via,
                        "apps": reported, "app": app, "app_set": app_set})
    for dev in sorted(status):
        if dev not in seen:
            seen.add(dev)
            app, _ = device_app("", apps.get(dev, {}), first[dev], catalog)
            devices.append({"id": dev, "device": dev, "name": "",
                            "type": guess_type(first[dev])[0],
                            "room": "", "status": status[dev], **net(dev), "via": "",
                            "apps": apps.get(dev, {}), "app": app, "app_set": False})
    # Unnamed MACs the scan still sees, so nothing online is missing; the
    # board asks for a name before one can go in a room.
    for r in master_rows:
        mac = r.get("MAC", "").strip()
        if r.get("Device", "").strip() or not mac or r.get("Status") == "defunct":
            continue
        host = r.get("Hostname", "").strip()
        if re.fullmatch(r"[0-9a-f:]{12,17}", host.lower()):
            host = ""
        reported = parse_app_names(r.get("AppNames", ""))
        devices.append({"id": "mac:" + mac, "device": "", "name": "",
                        "type": guess_type(r)[0], "room": "", "status": r.get("Status", ""),
                        "seen_as": host, "ip": r.get("IP", "").strip(), "macs": [mac],
                        "vendor": r.get("Vendor", "").strip(), "unnamed": True, "via": "",
                        "apps": reported, "app": device_app("", reported, r, catalog)[0],
                        "app_set": False})
    # Every app column a device sits in: its own app, then each app that
    # reports it (a Kasa plug also shows under Home Assistant).
    for d in devices:
        d["in_apps"] = list(dict.fromkeys(k for k in [d["app"], *map(app_key, d["apps"])] if k))
    # The platforms that reported at least one device (from AppNames).
    platforms = sorted({p for d in devices for p in d["apps"]})
    # Every catalog app, then any app a device names that the catalog lacks.
    known = app_catalog(catalog)
    keys = {a["key"] for a in known}
    extra = sorted({k for d in devices for k in d["in_apps"] if k not in keys})
    known += [{"key": k, "label": k, "portal": "", "match": "", "extra": True} for k in extra]
    types = default_types() if types is None else types
    return {"rooms": [r["Room"] for r in rooms], "types": type_names(types),
            "platforms": platforms, "apps": known, "devices": devices,
            # what the board's edit forms show
            "room_info": [{"room": r["Room"], "floor": r["Floor"], "aliases": r["Aliases"]}
                          for r in rooms],
            "infra": sorted(infra_types(types))}


def set_room(home, master_rows, key, room):
    """Move one device to `room` ('' = unassigned). A device not yet in
    home.csv gets a drafted row. Returns the row, or None if key is unknown."""
    if not key.strip() or key.startswith("name:") and not key[5:].strip() \
            or key.startswith("mac:"):     # unnamed: the board names it first
        return None
    for h in home:
        if _key(h) == key:
            h["Room"] = room or TODO
            return h
    match = next((r for r in master_rows if r.get("Device", "").strip() == key), None)
    if not match:
        return None
    row = new_row(match, room or TODO)
    home.append(row)
    return row


def set_type(home, master_rows, key, typ, types=None):
    """Set one device's Type ('' = TODO). A device not yet in home.csv gets a
    drafted row. An Expose still at the old type's default follows the new
    type (so a hub stops being offered to voice apps). Returns the row, or
    None if the key is unknown; raises ValueError for an unknown type."""
    typ = typ or TODO
    if typ != TODO and typ not in type_names(types):
        raise ValueError(f"unknown type '{typ}'")
    row = next((h for h in home if _key(h) == key), None)
    if row is None and set_room(home, master_rows, key, TODO) is not None:
        row = home[-1]
    if row is None:
        return None
    infra = infra_types(types)
    _retype(row, typ, infra, infra)
    return row


def set_app(home, master_rows, key, app, catalog=None):
    """Set the app one device belongs to (home.csv Native): a catalog key,
    NO_APP for "no app", or '' to go back to the guess. A device not yet in
    home.csv gets a drafted row. Returns the row, or None if the key is
    unknown; raises ValueError for an app the catalog lacks."""
    app = app_key(app)
    if app and app != NO_APP and not _find_app(default_apps() if catalog is None else catalog,
                                                app):
        raise ValueError(f"unknown app '{app}'")
    row = next((h for h in home if _key(h) == key), None)
    if row is None and set_room(home, master_rows, key, TODO) is not None:
        row = home[-1]
    if row is None:
        return None
    row["Native"] = app
    return row


def delete(home, key):
    """Remove a device with no network presence (a 'name:' row: Blink camera,
    Govee sensor...). Devices with a MAC come back on the next scan, so they
    cannot be deleted here. Returns True if a row was removed."""
    if not key.startswith("name:") or not key[5:].strip():
        return False
    n = len(home)
    home[:] = [h for h in home if _key(h) != key]
    return len(home) < n


def rename(home, master_rows, app_rooms, key, new):
    """Give one device a new name everywhere netinventory keeps it: its
    devices.csv Device (all MACs), its home.csv Device and Name, and its
    app_rooms.csv row. Mutates the lists; returns (old Device, new id) or
    raises ValueError with a message for the user."""
    new = " ".join(new.split())
    if not new:
        raise ValueError("name required")
    if len(new) > 60:
        raise ValueError("name is longer than 60 characters")
    if key.startswith("mac:"):
        # An unnamed MAC on the board: naming it makes it a regular device.
        r = next((r for r in master_rows if r.get("MAC", "") == key[4:]
                  and not r.get("Device", "").strip()), None)
        if r is None:
            raise ValueError("unknown device")
        taken = {x.get("Device", "").strip().lower() for x in master_rows} \
            | {h["Device"].lower() for h in home} | {h["Name"].lower() for h in home}
        if new.lower() in taken:
            raise ValueError(f"'{new}' is already used by another device")
        r["Device"] = new
        row = new_row(r, TODO)
        row["Name"] = new
        home.append(row)
        return "", new
    old = key[5:] if key.startswith("name:") else key
    is_device = not key.startswith("name:")
    if not old.strip():   # a blank key would match every unnamed device
        raise ValueError("unknown device")
    if is_device and not any(r.get("Device", "").strip() == old for r in master_rows) \
            and not any(h["Device"] == old for h in home):
        raise ValueError("unknown device")
    if new.lower() != old.lower():
        taken = {r.get("Device", "").strip().lower() for r in master_rows} \
            | {h["Device"].lower() for h in home} | {h["Name"].lower() for h in home}
        if new.lower() in taken:
            raise ValueError(f"'{new}' is already used by another device")
    if is_device:
        for r in master_rows:
            if r.get("Device", "").strip() == old:
                r["Device"] = new
        for a in app_rooms:
            if a["Device"] == old:
                a["Device"] = new
        for h in home:                         # sensors linked through this hub
            if h.get("Via") == old:
                h["Via"] = new
    row = next((h for h in home if _key(h) == key), None)
    if row is None and is_device:
        match = next((r for r in master_rows if r.get("Device", "").strip() == new), None)
        row = new_row(match, TODO) if match else None
        if row:
            home.append(row)
    if row is None:
        raise ValueError("unknown device")
    if is_device:
        row["Device"] = new
    row["Name"] = new
    return old, (new if is_device else "name:" + new)


def push_plan(app, app_rooms, home, app_names):
    """[(id, app name, wanted name)] for each device linked in app_rooms.csv
    whose name in the app differs from its home.csv Name. Only a Name the
    user chose is pushed, never a fallback: a generic Device name such as
    'Kasa Smart Switch (HS200) 2' must not overwrite a good app name.
    app_names is {id: current name in the app}."""
    names = {h["Device"]: h["Name"] for h in home if h["Device"] and h["Name"]}
    plan = []
    for a in app_rooms:
        if a["App"] != app or not a["Id"] or a["Id"] not in app_names:
            continue
        want = names.get(a["Device"])
        if want and want != app_names[a["Id"]].strip():
            plan.append((a["Id"], app_names[a["Id"]], want))
    return plan


def app_room_plan(app, app_rooms, master_rows, home, rooms):
    """Room moves for one app: [{id, name, from, to, app_room}] for each app
    device whose app room is not its home.csv Room. Devices are linked by the
    app's own name (AppNames); one with no room here, or that links to rooms
    that disagree, is left alone. An app room named after a room or one of its
    aliases counts as that room; app_room is the existing app room to use, or
    None when the app needs a room created (named `to`).
    app_rooms: {app room name: [(app device name, app device id)]}."""
    words = room_words(rooms)
    canon = lambda r: words.get((r or "").strip().lower())  # noqa: E731
    by_dev = {h["Device"]: h for h in home if h["Device"]}
    wanted = {}
    for r in master_rows:
        n = parse_app_names(r.get("AppNames", "")).get(app)
        h = by_dev.get(r.get("Device", "").strip())
        if n and h:
            wanted.setdefault(n, set()).add(h["Room"])
    for h in home:
        n = parse_app_names(h.get("AppNames", "")).get(app)
        if n and not h["Device"]:
            wanted.setdefault(n, set()).add(h["Room"])
    existing = {canon(r): r for r in app_rooms if r and canon(r)}
    plan = []
    for app_room, devs in app_rooms.items():
        for name, dev_id in devs:
            want = {r for r in wanted.get(name, set()) if r and r != TODO}
            if len(want) != 1:
                continue
            to = want.pop()
            if canon(app_room) == to:
                continue
            plan.append({"id": dev_id, "name": name, "from": app_room, "to": to,
                         "app_room": existing.get(to)})
    return sorted(plan, key=lambda p: (p["to"], p["name"]))


def mac_push_plan(found, master_rows, home):
    """[(ip, app name, wanted name)] for LAN devices found by MAC (e.g. Kasa)
    whose app name differs from their home.csv Name (only a chosen Name is
    pushed; see push_plan). found is [{ip, mac, alias}]."""
    by_mac = {r.get("MAC", "").upper(): r.get("Device", "").strip() for r in master_rows}
    names = {h["Device"]: h["Name"] for h in home if h["Device"] and h["Name"]}
    plan = []
    for d in found:
        want = names.get(by_mac.get(d["mac"].upper(), ""))
        if want and want != d["alias"]:
            plan.append((d["ip"], d["alias"], want))
    return plan


def ha_match(devices, master_rows):
    """{HA device id: devices.csv Device}. By MAC where HA has one; otherwise
    by name: the HA name equals (ignoring case) one device's Device or its
    discovered Hostname, such as the Kasa app name a SmartThings mirror of a
    Kasa plug also carries. A name that fits two devices is skipped."""
    by_mac, by_name = {}, {}
    for r in master_rows:
        dev = r.get("Device", "").strip()
        if not dev:
            continue
        by_mac[r.get("MAC", "").upper()] = dev
        for n in (dev, r.get("Hostname", "").strip()):
            if n:
                by_name.setdefault(" ".join(n.lower().split()), set()).add(dev)
    out = {}
    for d in devices:
        if d["mac"]:
            dev = by_mac.get(d["mac"].upper())
        else:
            hits = by_name.get(" ".join(d["name"].lower().split()), set())
            dev = next(iter(hits)) if len(hits) == 1 else None
        if dev:
            out[d["id"]] = dev
    return out


def ha_push_plan(devices, areas, master_rows, home, rooms=()):
    """Home Assistant changes: [{id, old, name, area_id, old_area, area}]
    where name (home.csv Name) or area (home.csv Room) differ from HA, for
    devices ha_match() links. Only chosen values are pushed: no Name, no
    rename; no Room, no area change. An HA area named after one of the room's
    rooms.csv Aliases counts as that room ('Study' for Office). area_id is
    None when HA has no area for the room yet.
    devices: [{id, name, mac, area_id}]; areas: {id: name}."""
    match = ha_match(devices, master_rows)
    rows = {h["Device"]: h for h in home if h["Device"]}
    area_ids = {n.lower(): i for i, n in areas.items()}
    aliases = {r["Room"].lower(): [a.lower() for a in split_list(r["Aliases"])] for r in rooms}
    plan = []
    for d in devices:
        h = rows.get(match.get(d["id"], ""))
        if not h:
            continue
        name = h["Name"] if h["Name"] and h["Name"] != d["name"] else None
        room = h["Room"] if h["Room"] and h["Room"] != TODO else ""
        known = [room.lower()] + aliases.get(room.lower(), []) if room else []
        area = room if room and areas.get(d["area_id"], "").lower() not in known else None
        area_id = next((area_ids[k] for k in known if k in area_ids), None) if area else None
        if name or area:
            plan.append({"id": d["id"], "old": d["name"], "name": name,
                         "old_area": areas.get(d["area_id"], ""), "area": area,
                         "area_id": area_id})
    return plan


def add_room(rooms, room):
    """Append a room (Floor TODO) unless it, or an alias, already exists.
    Returns the canonical Room name."""
    room = clean_room(room)
    existing = room_words(rooms).get(room.lower())
    if existing:
        return existing
    rooms.append({"Room": room, "Floor": TODO, "Aliases": ""})
    return room


def _find_room(rooms, room):
    return next((r for r in rooms if r["Room"].lower() == clean_room(room).lower()), None)


def delete_room(rooms, home, room, move_to=""):
    """Drop `room` from rooms.csv; its home.csv devices move to `move_to`
    (another room) or become unassigned. Returns how many devices moved;
    raises ValueError for an unknown room or target."""
    row = _find_room(rooms, room)
    if row is None:
        raise ValueError(f"unknown room '{room}'")
    target = _find_room(rooms, move_to) if move_to else None
    if move_to and (target is None or target is row):
        raise ValueError(f"cannot move devices to '{move_to}'")
    moved = 0
    for h in home:
        if h["Room"].lower() == row["Room"].lower():
            h["Room"] = target["Room"] if target else TODO
            moved += 1
    rooms.remove(row)
    return moved


def edit_room(rooms, room, floor, aliases):
    """Set a room's Floor and Aliases (comma-separated). An alias another room
    already uses, as its name or an alias, is refused: it would map twice."""
    row = _find_room(rooms, room)
    if row is None:
        raise ValueError(f"unknown room '{room}'")
    floor = " ".join(floor.split())
    if len(floor) > 20:
        raise ValueError("floor is longer than 20 characters")
    if ";" in floor + aliases:
        raise ValueError("no semicolons")
    words = room_words([r for r in rooms if r is not row])
    clean, seen = [], {row["Room"].lower()}
    for a in map(clean_room, split_list(aliases)):
        if a.lower() in seen:
            continue
        if a.lower() in words:
            raise ValueError(f"'{a}' is already used by room '{words[a.lower()]}'")
        seen.add(a.lower())
        clean.append(a)
    row["Floor"], row["Aliases"] = floor or TODO, ", ".join(clean)
    return row


def rename_room(rooms, home, room, new):
    """Rename a room in rooms.csv and on every home.csv device in it. The old
    name becomes an alias, so an app room or a Name prefix that used it still
    maps. Returns how many devices were relabelled; raises ValueError for an
    unknown room or a name another room already uses."""
    row = _find_room(rooms, room)
    if row is None:
        raise ValueError(f"unknown room '{room}'")
    new, old = clean_room(new), row["Room"]
    if not new:
        raise ValueError("room name required")
    if len(new) > 40:
        raise ValueError("room name is longer than 40 characters")
    owner = room_words(rooms).get(new.lower())
    if owner and owner.lower() != old.lower():
        raise ValueError(f"'{new}' is already used by room '{owner}'")
    aliases = [a for a in split_list(row["Aliases"]) if a.lower() != new.lower()]
    if old.lower() != new.lower() and old.lower() not in (a.lower() for a in aliases):
        aliases.append(old)
    row["Room"], row["Aliases"] = new, ", ".join(aliases)
    moved = 0
    for h in home:
        if h["Room"].lower() == old.lower():
            h["Room"] = new
            moved += 1
    return moved


def check(rooms, home, master_names=None, types=None):
    """Validate against docs/home-standard.md. Returns (errors, warnings),
    each a list of strings."""
    errors, warnings = [], []
    room_set = {}
    for r in rooms:
        if not r["Room"]:
            errors.append("rooms.csv: a row has no Room")
            continue
        if r["Room"].lower() in room_set:
            errors.append(f"rooms.csv: duplicate room '{r['Room']}'")
        room_set[r["Room"].lower()] = r
        if not r["Floor"] or r["Floor"] == TODO:
            errors.append(f"rooms.csv: '{r['Room']}' has no Floor")
    words = room_words(rooms)

    names = {}
    for i, h in enumerate(home, start=2):    # row 1 is the header
        where = f"home.csv:{i} ({h['Name'] or h['Device'] or 'blank'})"
        room, typ, name = h["Room"], h["Type"], h["Name"]
        if room == TODO or not room:
            errors.append(f"{where}: Room not set")
        elif room.lower() not in room_set:
            errors.append(f"{where}: Room '{room}' is not in rooms.csv")
        if typ == TODO or not typ:
            errors.append(f"{where}: Type not set")
        elif typ not in type_names(types):
            errors.append(f"{where}: Type '{typ}' is not one of {', '.join(type_names(types))}")
        errors += [f"{where}: {e}" for e in name_problems(name, room, words)]
        if name:
            key = name.lower()
            if key in names:
                errors.append(f"{where}: Name duplicates home.csv:{names[key]}")
            names[key] = i
        expose = split_list(h["Expose"])
        bad = [a for a in expose if a not in APPS]
        if bad:
            errors.append(f"{where}: Expose has unknown app(s) {', '.join(bad)}; "
                          f"use {', '.join(sorted(APPS))}")
        if typ in infra_types(types) and VOICE_APPS & set(expose):
            errors.append(f"{where}: '{typ}' is infrastructure; do not expose it "
                          "to google, alexa or apple")
        if h["Device"] and master_names is not None and h["Device"] not in master_names:
            warnings.append(f"{where}: Device '{h['Device']}' is not in devices.csv")
        via = h.get("Via", "")
        if via and master_names is not None and via not in master_names:
            warnings.append(f"{where}: Via hub '{via}' is not in devices.csv")
    return errors, warnings


def name_problems(name, room, words):
    """Naming rules 1, 2, 5 and 6 of the standard, for one Name."""
    if not name:
        return ["Name not set"]
    out = []
    if not _NAME_CHARS.match(name):
        out.append(f"Name '{name}' may use only letters, digits and spaces")
    if len(name) > MAX_NAME:
        out.append(f"Name '{name}' is longer than {MAX_NAME} characters")
    if name != " ".join(w[:1].upper() + w[1:] for w in name.split()):
        out.append(f"Name '{name}' is not Title Case")
    lowered = name.lower()
    tokens = set(re.findall(r"[a-z0-9-]+", lowered))
    if tokens & RESERVED:
        out.append(f"Name '{name}' uses a reserved word: "
                   f"{', '.join(sorted(tokens & RESERVED))}")
    if tokens & BRANDS:
        out.append(f"Name '{name}' contains a brand ({', '.join(sorted(tokens & BRANDS))}); "
                   "put it in Description")
    if room and room != TODO:
        prefixes = [w for w, canon in words.items() if canon.lower() == room.lower()]
        if not any(lowered == p or lowered.startswith(p + " ") for p in prefixes):
            out.append(f"Name '{name}' must start with its room '{room}'")
    return out
