# Home device standard

One list of every smart-home device: which room it is in, what it is, and the
one name it has in every app. Home Assistant, SmartThings, Google Home, Alexa,
Apple Home and Smart Life are set up *from* this list, never edited first.

netinventory finds the devices (`devices.csv`, keyed by MAC). This standard adds
the layer on top: rooms, types and the canonical name.

## The registry

Two semicolon-separated files in the engine's data directory, next to
`devices.csv`. Both hold real household data, so both are git-ignored; the
`*.example.csv` files show the format with fictional devices.

**`rooms.csv`**: one row per room.

| Column | Rule | Example |
|---|---|---|
| `Room` | Title Case, unique, the word you would say aloud | `Family Room` |
| `Floor` | `Upstairs`, `Downstairs` or `Outside` (add one only if the house needs it) | `Downstairs` |
| `Aliases` | Other names people use, comma-separated (for voice apps) | `Den, TV Room` |

**`home.csv`**: one row per physical device that a person controls or watches.

| Column | Rule | Example |
|---|---|---|
| `Device` | The `Device` name in `devices.csv` (the join key; blank for devices with no network presence, e.g. Zigbee bulbs) | `Kasa Smart Switch (HS200) 2` |
| `Room` | A `Room` from `rooms.csv` (where it physically is) | `Family Room` |
| `Type` | One value from the type list below | `switch` |
| `Name` | The canonical name; see the naming rules | `Family Room Ceiling Light` |
| `Description` | Brand, model and what it controls (no semicolons). Brands live here, never in `Name` | `Kasa HS200 wall switch, 4 recessed cans` |
| `Native` | The app it is paired with first (its own cloud) | `kasa` |
| `Expose` | Where it should appear, comma-separated: `ha`, `google`, `alexa`, `apple`, `smartthings` | `ha, google, alexa` |

Devices that nobody controls (phones, laptops, the NAS, the router) stay in
`devices.csv` only. They need no room or type.

## Naming rules

1. **Room first, then the thing:** `Kitchen Ceiling Light`, `Driveway Camera`,
   `Landing Thermostat`. The name must start with the room (or one of its
   aliases).
2. **Say what it does, not what it is made by:** `Family Room Lamp`, not
   `Kasa Smart Plug (HS103) 2`. No brand, model number or brackets.
3. **Tell twins apart by position, not number:** `Garage Door Left` /
   `Garage Door Right`, `Patio Light Front` / `Patio Light Back`.
4. **A plug is named after what is plugged into it:** `Living Room Christmas
   Tree`, not `Living Room Plug`.
5. **Unique across the house**, Title Case, letters, digits and spaces only, at
   most 30 characters (Alexa and Google truncate long names).
6. **Avoid words the voice apps reserve:** do not name a device `Alexa`,
   `Google`, `Echo`, `Group`, `Scene`, `All` or `Everything`.

Because every app also knows the room, the room word does not have to be said
aloud in the room itself: "turn on the ceiling light" works on the kitchen
speaker.

## Types

Each type maps to what each app expects. Infrastructure types are never
exposed to voice apps (`netinv home check` enforces it).

| Type | Home Assistant | Google Home | Alexa | Apple Home |
|---|---|---|---|---|
| `light` | light | Light | Light | Lightbulb |
| `switch` | switch / light | Switch | Switch | Switch |
| `plug` | switch | Outlet | Plug | Outlet |
| `fan` | fan | Fan | Fan | Fan |
| `blind` | cover | Blinds | Blinds | Window Covering |
| `garage_door` | cover | Garage | Garage Door | Garage Door |
| `lock` | lock | Lock | Smart Lock | Lock |
| `thermostat` | climate | Thermostat | Thermostat | Thermostat |
| `camera` | camera | Camera | Camera | Camera |
| `doorbell` | camera / event | Doorbell | Doorbell | Doorbell |
| `sensor` | sensor / binary_sensor | Sensor | Contact / Motion sensor | Sensor |
| `speaker` | media_player | Speaker | Echo / Speaker | Speaker |
| `tv` | media_player | TV | TV | Television |
| `media_player` | media_player | Set-top / Streaming box | Streaming device | (not supported) |
| `sprinkler` | valve / switch | Sprinkler | (switch) | Irrigation |
| `pool` | sensor / switch | (sensor) | (sensor) | (sensor) |
| `energy` | sensor | (not exposed) | (not exposed) | (not exposed) |
| `appliance` | switch | (by device) | (by device) | (by device) |
| `hub` | (infrastructure) | | | |
| `network` | (infrastructure) | | | |

## What each app gets, and how

| App | Room | Name | How it is set |
|---|---|---|---|
| Home Assistant | Area (with floor) | Device name | Written from the registry through the HA API (the hub for everything else) |
| SmartThings | Room | Device label | Written through the SmartThings API |
| Google Home | Room | Name | Devices exposed through HA: sent by HA's Google Assistant integration, with the HA area as the room. Native Google/Nest devices: by hand, from the checklist |
| Alexa | Group | Name | Name comes through HA's Alexa skill. Groups are always set by hand, from the checklist |
| Apple Home | Room | Name | Name comes through HA's HomeKit Bridge. The room is chosen when pairing, from the checklist |
| Smart Life (Tuya) | Room | Name | The Smart Life app, from the checklist (the Tuya cloud API can read names but its room API is limited) |

Rule: **expose a device to Google, Alexa and Apple through Home Assistant
where it can be, not through the vendor's own skill as well.** Linking both
makes duplicate devices with different names. A vendor's own skill is used only
for a device HA cannot control.

## The process

1. `netinv update --scan --cloud`: find devices and name them in `devices.csv`.
2. `netinv home draft`: add every named but unclassified device to `home.csv`
   with a guessed room and type (`TODO` where it cannot guess).
3. Edit `rooms.csv` and `home.csv` in a spreadsheet: fix rooms, write names.
4. `netinv home check`: validate against the rules above. Fix until it passes.
5. Push to each app (Home Assistant first), then re-check drift after any
   change made inside an app.
