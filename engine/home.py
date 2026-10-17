"""Home device registry: rooms, types and one canonical name per device.

The standard is docs/home-standard.md. Two semicolon CSVs live next to
devices.csv: rooms.csv (Room;Floor;Aliases) and home.csv (one row per device a
person controls). `draft` seeds home.csv from devices.csv; `check` validates
both files against the standard. Standard library only."""
import csv
import os
import re

ROOM_FIELDS = ["Room", "Floor", "Aliases"]
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


def draft(master_rows, rooms, home):
    """New home.csv rows for every named, controllable device in devices.csv
    that home.csv does not already list. Existing rows are never changed."""
    known = {h["Device"] for h in home if h["Device"]}
    out, seen = [], set()
    for r in master_rows:
        dev = r.get("Device", "").strip()
        if not dev or dev in known or dev in seen or _SKIP.search(dev):
            continue
        seen.add(dev)
        typ, native = guess_type(r)
        out.append({"Device": dev, "Room": guess_room(dev, rooms), "Type": typ,
                    "Name": "", "Description": " ".join(
                        x for x in [r.get("Vendor", ""), dev] if x),
                    "Native": native, "Expose": default_expose(typ)})
    # Known rooms first, grouped by room then type, so the sheet reads in order.
    return sorted(out, key=lambda r: (r["Room"] == TODO, r["Room"], r["Type"], r["Device"]))


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
