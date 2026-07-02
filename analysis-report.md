# netinventory → macOS/iOS Product: Analysis & Roadmap

> What it takes to turn the current Python CLI into a fully usable macOS + iOS
> application — architecture, features, and optimizations. **The CLI remains a
> first-class interface throughout** (automation, cron/launchd, power users).

## 0. Implementation status (autonomous pass)

Worked down the roadmap with tests + commits. **32 unit tests pass; CI green.**

**Phase 0 — hardening: DONE (except SQLite, see below)**
- ✅ Router session reuse + lockout cooldown (fixes the admin lockout)
- ✅ Unit test suite (32 tests, stdlib, synthetic fixtures) + GitHub Actions CI + leak-check
- ✅ Config file (`~/.netinv/config`) with env→config→default precedence
- ✅ Fixed hardcoded `192.168.1.` — LAN filter now derives from `subnet`
- ✅ Bundled OUI DB (gzip) — vendor fill works without nmap installed
- ✅ `pyproject.toml` (`netinv` console script, extras) + `DATA_DIR` decoupled from code dir
- ✅ LaunchAgent installer for scheduled scans; `events.csv` retention

**Phase 3 engine features (testable now): DONE**
- ✅ Alert-rule engine (`alerts`) + device history (`history`)

**Phase 1/2 backend bridge: DONE**
- ✅ `serve` — read-only JSON API (`/api/devices|summary|alerts|events`) + live web dashboard.
  This is the concrete surface a macOS GUI / iOS companion consumes.

**Deliberately deferred / needs intervention:**
- ⏸ **SQLite migration** — NOT done, on purpose. `devices.csv` is a *feature*
  (hand-editable in Numbers/Excel). The right design keeps the editable CSV as
  the registry and adds a SQLite **time-series** store for sightings/history
  (a superset of today's `events.csv`), not a wholesale CSV replacement. It's a
  large change touching real data, so it should be done as its own reviewed step.
- 🚫 **Native macOS/iOS apps** — cannot be built or tested in this environment:
  requires Xcode, an Apple Developer account, code signing/notarization, and
  visual/device verification. The `serve` API is the ready integration point; a
  SwiftUI menu-bar app (macOS) and companion (iOS) would consume it. Scaffolding
  a Swift project is possible but compiling/signing/running it is not.

---

## 1. Executive summary

A macOS app is very feasible; a native iOS *scanner* is not — but an iOS
**companion viewer** is. The current engine is Python driving `nmap`, `arp`, raw
sockets, and `security`/`curl` shell-outs; none of that runs in a sandboxed iOS
app, and iOS restricts local-network scanning even with entitlements. So the
product shape is: **a macOS "scanner agent" that owns all discovery, a shared
data store with history, an iOS companion that views (and optionally hits the
router API directly), and the CLI kept as a thin frontend over the same core
library.** The single most important refactor is extracting today's scripts into
an importable **core engine** that the CLI, the macOS GUI, and any service all
share — everything else builds on that.

## 2. Top 5 must-do

| # | Item | Why | Effort |
|---|------|-----|--------|
| 1 | **Extract a `netinv` core library** (engine) from the two scripts; CLI becomes one frontend | Prerequisite for GUI, service, tests, iOS backend. Today logic + I/O + presentation are entangled | L |
| 2 | **Replace `devices.csv` with SQLite** (+ a `sightings`/`events` time-series) and a migration | Enables history, concurrency, sync, per-device pages; CSV can't do time-series or multi-writer | M |
| 3 | **macOS menu-bar agent** wrapping the engine (scheduled scans, notifications, device list) | Turns "run a command" into an always-on product; keeps the Mac as the scanner | L |
| 4 | **Router session reuse + rate-limiting** (we triggered a real admin lockout this session) | Current code re-logs-in every run → gateway brute-force lockout. A shipped app cannot do this | S |
| 5 | **Credential + secrets model via native Keychain APIs** (not `security` shell-outs) and a plan for syncable cloud tokens | Sandbox/notarization forbids some shell-outs; needed for App Store and for iOS to reuse tokens | M |

## 3. The iOS reality (the crux)

**Hard blockers for a native iOS scanner:**
- No Python runtime, no `nmap`, no `arp`, no raw/link-layer sockets in the App Store sandbox.
- ARP-table/OUI-on-LAN identification depends on link-layer access iOS doesn't grant.
- The **Local Network privacy entitlement** is required even for mDNS/Bonjour + unicast, and Apple reviews its use; background scanning is essentially disallowed.
- SSDP/mDNS *is* possible on iOS (Network.framework / `NWBrowser`), but it's a fraction of what the Mac can see.

**Recommended way around it:** iOS is a **viewer**, not a scanner. It reads the
inventory + events the **macOS agent** publishes to a shared store (CloudKit or
Supabase), with optional *direct* router-API calls for a lightweight "who's on
now" live view. This keeps a real iOS app without pretending it can scan.

## 4. Findings by dimension

### Architecture & Platform (highest leverage)
| Sev | Area | Finding | Fix | Effort |
|-----|------|---------|-----|--------|
| critical | iOS core | Python+nmap+arp can't run on iOS | iOS = companion viewer over shared backend; scanning stays on macOS | L |
| high | macOS packaging | Ships/needs `nmap`; App-Store sandbox can't bundle+exec it cleanly | Distribute the agent as a **notarized non-App-Store app** (direct/Homebrew/Sparkle), or reimplement ARP+ping sweep natively in Swift to qualify for the Store | L |
| high | modularity | `netinv.py` mixes discovery, merge, reporting, CLI, I/O | Extract `netinv` package: `sources/`, `store`, `enrich`, `report`, `cli` | L |
| med | distribution | No signing/notarization/updates today | Developer ID signing + notarization + Sparkle (macOS) auto-update | M |

### UX & App Design
| Sev | Area | Finding | Fix | Effort |
|-----|------|---------|-----|--------|
| high | first-run | No onboarding; CLI assumes nmap/venv/Keychain set up | Guided setup: permission prompts (Local Network), router login, connector wizards | M |
| high | core screens | No GUI for the rich data (xlsx tabs, wifi-scan, pairs) | Screens: Devices list, Device detail, Network map, Events timeline, Connectors, Settings | L |
| med | states | CLI has no loading/empty/error/permission states a GUI needs | Design explicit states incl. "scan needs a Mac agent" on iOS | M |
| med | notifications | New-device alerts are macOS `osascript` banners only | Native `UNUserNotification` w/ actions (Name it / Trust / Block) | S |
| low | visualization | `Link`/band, fingerprint, pairs are text-only | Badges, band chips, topology graph, per-device fingerprint card | M |

### Features & Roadmap
| Sev | Area | Finding | Fix | Effort |
|-----|------|---------|-----|--------|
| high | history | No per-device timeline (first/last seen only) | Time-series sightings → device history, "typically online" patterns | M |
| high | alerts | Only "new device" is detected | Rule engine: unknown device joins, known device offline, new device on band X, MAC randomization changes | M |
| high | presence | Data supports it but no "who's home" | Map personal devices → people; presence/occupancy view | M |
| med | topology | No network map | Graph from gateway → wired/wireless/band, live status | M |
| med | multi-site | Single subnet hardcoded (`192.168.1.`) | Multi-network/site model; per-site agents | M |
| med | integrations | No outward hooks | Webhooks, Home Assistant, HomeKit "presence", MQTT publish | M |
| med | auto-ID | Fingerprint hints not turned into names | Signature→label rules; bundle OUI DB; add mDNS + DHCP-option fingerprinting | M |
| low | scheduling | `watch` loop only; no launchd | launchd/LaunchAgent for background scans on macOS | S |

### Backend, Data & Sync
| Sev | Area | Finding | Fix | Effort |
|-----|------|---------|-----|--------|
| high | store | Flat CSV = no history, no concurrency, race on save | SQLite (WAL) locally; devices + sightings + events tables | M |
| high | sync | No cross-device data path | Mac agent publishes to CloudKit (Apple-native, free) or Supabase; iOS subscribes | L |
| med | retention | Events/wifi_scan grow unbounded, no rollup | Retention + daily rollups; keep raw N days | S |
| med | creds sync | Keychain is per-Mac; cloud tokens needed on iOS too | iCloud Keychain items or a secured backend-held token per connector | M |
| low | schema | CSV column adds are ad-hoc migrations | Versioned schema + migrations in the store layer | S |

### Security, Privacy & Store Readiness
| Sev | Area | Finding | Fix | Effort |
|-----|------|---------|-----|--------|
| high | router creds | Stores gateway **admin** password + auto-scrapes it | Least-privilege where possible; explicit consent; document; session reuse to avoid lockouts | M |
| high | privacy labels | App handles MACs, device names, network layout (PII-ish) | App Store privacy nutrition labels; on-device-first; clear data policy | S |
| high | Local Network | Entitlement + review scrutiny for scanning | Purpose strings, minimal scope, user-initiated scans | M |
| med | token files | `~/.netinv_blink.json`, `~/.smartlife` on disk | Move into Keychain; chmod/permissions audit; never sync in cleartext | S |
| med | active fingerprint | Port/banner scanning is sensitive | Restrict to own LAN, disclose, make opt-in (already incremental) | S |
| low | repo hygiene | Secrets/data already git-ignored (verified this session) | Keep the leak-check discipline in CI | S |

## 5. Cross-cutting themes (root causes)

1. **Monolith → core library.** One refactor unblocks GUI, iOS backend, tests, and service packaging. Do this first.
2. **Local file → data store + sync.** CSV is the ceiling on history, concurrency, and multi-device. SQLite now, cloud sync for iOS.
3. **macOS-only shell-outs → native APIs / service boundary.** `security`, `nmap`, `arp`, `curl` are fine for a notarized Mac agent but block the Store and iOS; isolate them behind the agent.
4. **Single-user local tool → optional multi-device product**, without losing the local-first, CLI-first ethos.

## 6. Quick wins (do now, on the current CLI)

- **Router session reuse + backoff** — fixes the lockout we hit; biggest reliability win, small effort.
- **Bundle an OUI database** so vendor fill doesn't depend on a local nmap install.
- **SQLite store** behind the existing commands (keep CSV export) — unlocks history immediately.
- **`pip install`-able package + `pipx`** so it's not venv-fiddly; keeps CLI first-class.
- **launchd LaunchAgent** to run `update`/`watch` on a schedule.
- **Config file** (`~/.netinv/config`) for subnet/router/SSIDs instead of hardcoded `192.168.1.`.
- **Retention/rollup** for `events.csv`.
- Add a **CI leak-check** (the MAC/secret grep we ran manually) so the repo stays clean.

## 7. Phased roadmap

**Phase 0 — Harden the CLI (weeks):** core-library refactor, SQLite, config file,
router session reuse, OUI bundling, pipx packaging, launchd, tests. *CLI stays the
primary interface and the reference implementation.*

**Phase 1 — MVP: macOS agent (1–2 months):** SwiftUI (or PyObjC/Tauri) menu-bar
app over the core engine — Devices list, Device detail, Events, Connectors,
native notifications, scheduled scans. Notarized direct distribution. CLI ships
alongside and shares the same store.

**Phase 2 — v1: iOS companion (1–2 months):** CloudKit/Supabase sync from the Mac
agent; iOS read-only viewer (list, detail, push alerts, widget, "who's home"),
plus optional direct router-API live view.

**Phase 3 — v2: advanced (ongoing):** network topology map, alert-rule engine,
multi-site, presence/occupancy, integrations (HomeKit/Home Assistant/webhooks/
MQTT), smarter auto-identification (mDNS + DHCP fingerprinting), reporting.

## 8. What wasn't covered (honesty)

- No live usability testing or performance profiling; effort tags are rough.
- Didn't deep-compare cross-platform frameworks (SwiftUI vs Tauri vs Flutter vs
  Briefcase) — a spike is warranted before committing the GUI stack.
- Android not considered.
- Cost/pricing/business model and App Store review edge-cases not assessed.
- The Scout mapped the code, but the 5 specialist passes were rate-limited; this
  report is the author's synthesis from full working knowledge of the codebase,
  not the multi-agent panel. Re-runnable if a clean multi-agent pass is wanted.
