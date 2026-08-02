<p align="center">
  <img src="assets/brand/netinventory-icon.png" alt="netinventory" width="112" height="112">
</p>

<h1 align="center">netinventory</h1>

<p align="center"><em>Every device on your network, named and accounted for.</em></p>

Home network device inventory — a **macOS scanner engine** plus a **macOS/iOS
companion app**. MAC-keyed and DHCP-resilient, with router/SSDP/cloud discovery,
active fingerprinting, and a Wi-Fi neighbor classifier.

```
netinventory/
├── engine/   → Python scanner engine + CLI (does all the scanning; runs on a Mac)
├── app/      → Flutter macOS/iOS companion (a viewer over the engine's API)
└── assets/   → brand mark + app screenshots
```

<p align="center">
  <img src="assets/screenshots/app-devices.png" alt="netinventory — device list" width="820">
</p>
<p align="center">
  <sub>The companion app. Every screenshot in this repo uses fictional demo
  devices (RFC 7042 documentation MACs) — never a real scan.</sub>
</p>

## Architecture

The engine is the scanner (Python + nmap/arp/SSDP/router scrape/cloud). It can't
run on iOS, so the split is deliberate:

- **`engine/`** runs on a Mac. `netinv serve` exposes a read-only JSON API
  (`/api/devices|summary|alerts|events`) plus a web dashboard. Renaming from the
  dashboard/app is opt-in: `netinv serve --allow-write` enables `POST /api/name`
  (off by default; the write path sends no CORS header, so cross-site pages
  can't reach it).
- **`app/`** is the viewer. On macOS it reads `http://127.0.0.1:8787`; on iOS
  (which can't scan) it points at the Mac's LAN IP.

## Quick start

**Engine (local runs, Python):**
```bash
cd engine
python3 netinv.py update --scan        # scan + merge + report
python3 netinv.py serve                 # JSON API + dashboard on :8787
python3 -m unittest discover -s tests   # tests (stdlib only)
```
Optional cloud/xlsx features use a venv:
```bash
cd engine && python3 -m venv .venv
./.venv/bin/python -m pip install -e '.[all]'
./.venv/bin/netinv update --scan --cloud
```
Full engine docs: [`engine/README.md`](engine/README.md).

**App (macOS/iOS):**
```bash
cd app
flutter run -d macos          # or a connected iPhone
```
App docs: [`app/README.md`](app/README.md). Uses Apple Developer team
`VKUYJ3BS76`, bundle `com.aspl.net.netinventory`.

## Brand

The mark is a scanner hub with three discovered nodes over a sweep, drawn as
vector geometry rather than shipped as art:

| | |
|---|---|
| `assets/brand/netinventory-icon.png` | 1024px master, dark tile |
| `assets/brand/netinventory-logo.svg` | flat mark, transparent |
| `app/lib/widgets.dart` → `BrandMark` | the same mark as a `CustomPainter`, so the in-app logo stays crisp at any size and needs no raster asset |

App icons (7 macOS sizes, 15 iOS) are generated from that geometry — see
[`app/README.md`](app/README.md#icons) for how to regenerate them after a change.

## Privacy

All real network data (`devices.csv`, reports, `inventory.xlsx`, `events.csv`,
SSIDs, router cookies) lives under `engine/` and is git-ignored. Credentials are
in the macOS Keychain. CI enforces that no data file or real MAC is ever
committed. See [`analysis-report.md`](analysis-report.md) for the productization
roadmap and status.

## License

MIT
