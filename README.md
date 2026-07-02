# netinventory

Home network device inventory — a **macOS scanner engine** plus a **macOS/iOS
companion app**. MAC-keyed and DHCP-resilient, with router/SSDP/cloud discovery,
active fingerprinting, and a Wi-Fi neighbor classifier.

```
netinventory/
├── engine/   → Python scanner engine + CLI (does all the scanning; runs on a Mac)
└── app/      → Flutter macOS/iOS companion (a viewer over the engine's API)
```

## Architecture

The engine is the scanner (Python + nmap/arp/SSDP/router scrape/cloud). It can't
run on iOS, so the split is deliberate:

- **`engine/`** runs on a Mac. `netinv serve` exposes a read-only JSON API
  (`/api/devices|summary|alerts|events`) plus a web dashboard.
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

## Privacy

All real network data (`devices.csv`, reports, `inventory.xlsx`, `events.csv`,
SSIDs, router cookies) lives under `engine/` and is git-ignored. Credentials are
in the macOS Keychain. CI enforces that no data file or real MAC is ever
committed. See [`analysis-report.md`](analysis-report.md) for the productization
roadmap and status.

## License

MIT
