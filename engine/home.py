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
HOME_FIELDS = ["Device", "Room", "Type", "Name", "Description", "Native", "Expose"]

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
    """Lower-case room name and alias -> canonical Room."""
    out = {}
    for r in rooms:
        for w in [r["Room"]] + split_list(r["Aliases"]):
            out[w.lower()] = r["Room"]
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


def default_expose(typ):
    if typ in INFRA_TYPES or typ == TODO:
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
            "Native": native, "Expose": default_expose(typ)}


def _key(h):
    """A home.csv row's id: its Device, or its Name for devices with no
    network presence (e.g. Zigbee bulbs)."""
    return h["Device"] or ("name:" + h["Name"] if h["Name"] else "")


def board(master_rows, rooms, home):
    """Every device for the room board: each home.csv row plus every named
    device in devices.csv that home.csv does not list yet (unassigned)."""
    status, first, seen_as = {}, {}, {}
    for r in master_rows:
        dev = r.get("Device", "").strip()
        if not dev:
            continue
        first.setdefault(dev, r)
        if status.get(dev) != "online":
            status[dev] = r.get("Status", "")
        # The discovered name (e.g. 'Amazon Echo Show') tells look-alike
        # Device names apart; prefer a human one with a space, then any.
        host = r.get("Hostname", "").strip()
        if not host or re.fullmatch(r"[0-9a-f:]{12,17}", host.lower()):
            continue
        prev = seen_as.get(dev)
        if prev is None or (" " in host and " " not in prev[0]):
            seen_as[dev] = (host, r.get("IP", ""))
    room_set = {r["Room"].lower(): r["Room"] for r in rooms}
    devices, seen = [], set()
    for h in home:
        key = _key(h)
        if not key or key in seen:
            continue
        seen.add(key)
        host, ip = seen_as.get(h["Device"], ("", ""))
        devices.append({"id": key, "device": h["Device"], "name": h["Name"],
                        "type": h["Type"], "room": room_set.get(h["Room"].lower(), ""),
                        "status": status.get(h["Device"], ""), "seen_as": host, "ip": ip})
    for dev in sorted(status):
        if dev not in seen:
            seen.add(dev)
            host, ip = seen_as.get(dev, ("", ""))
            devices.append({"id": dev, "device": dev, "name": "",
                            "type": guess_type(first[dev])[0],
                            "room": "", "status": status[dev], "seen_as": host, "ip": ip})
    return {"rooms": [r["Room"] for r in rooms], "devices": devices}


def set_room(home, master_rows, key, room):
    """Move one device to `room` ('' = unassigned). A device not yet in
    home.csv gets a drafted row. Returns the row, or None if key is unknown."""
    if not key.strip() or key.startswith("name:") and not key[5:].strip():
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


def ha_push_plan(devices, areas, master_rows, home):
    """Home Assistant changes, matched by MAC: [{id, old, name, area_id,
    old_area, area}] where name (home.csv Name) or area (home.csv Room) differ
    from HA. Only chosen values are pushed: no Name, no rename; no Room, no
    area change. area_id is None when HA has no area of that name yet.
    devices: [{id, name, mac, area_id}]; areas: {area_id: name}."""
    by_mac = {r.get("MAC", "").upper(): r.get("Device", "").strip() for r in master_rows}
    rows = {h["Device"]: h for h in home if h["Device"]}
    area_ids = {n.lower(): i for i, n in areas.items()}
    plan = []
    for d in devices:
        h = rows.get(by_mac.get(d["mac"].upper(), "")) if d["mac"] else None
        if not h:
            continue
        name = h["Name"] if h["Name"] and h["Name"] != d["name"] else None
        room = h["Room"] if h["Room"] and h["Room"] != TODO else ""
        area = room if room and room.lower() != areas.get(d["area_id"], "").lower() else None
        if name or area:
            plan.append({"id": d["id"], "old": d["name"], "name": name,
                         "old_area": areas.get(d["area_id"], ""), "area": area,
                         "area_id": area_ids.get(area.lower()) if area else None})
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


def check(rooms, home, master_names=None):
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
        elif typ not in TYPES:
            errors.append(f"{where}: Type '{typ}' is not one of {', '.join(TYPES)}")
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
        if typ in INFRA_TYPES and VOICE_APPS & set(expose):
            errors.append(f"{where}: '{typ}' is infrastructure; do not expose it "
                          "to google, alexa or apple")
        if h["Device"] and master_names is not None and h["Device"] not in master_names:
            warnings.append(f"{where}: Device '{h['Device']}' is not in devices.csv")
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
