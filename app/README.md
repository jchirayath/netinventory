# netinventory-app

macOS + iOS companion for [netinventory](https://github.com/jchirayath/netinventory).
Flutter app that reads the read-only JSON API served by `netinv serve` and shows
your network inventory — device list (searchable, status/band/category),
rollup summary, and alerts.

## Model

The Python engine (netinventory) is the **scanner agent** and runs on a Mac
(`netinv serve` exposes `/api/devices|summary|alerts|events`). This app is the
**viewer**:

- **macOS**: talks to the local agent at `http://127.0.0.1:8787`.
- **iOS**: can't scan (sandbox); point the Host (top-right) at the Mac's LAN IP.

## Run

```bash
# on the Mac, start the agent API:
cd ../netinventory && ./netinv.py serve        # http://127.0.0.1:8787

# the app:
flutter run -d macos        # or: flutter run -d <iphone>
```

## Signing

Uses Apple Developer team `VKUYJ3BS76`, bundle `com.aspl.net.netinventory`
(same account as the other apps). `flutter build macos/ios` compiles and
ad-hoc-signs from the CLI; for a team-signed + notarized build, open
`macos/Runner.xcworkspace` (or `ios/Runner.xcworkspace`) in Xcode once so
automatic signing registers the App ID.

## Requirements

- Flutter 3.41+
- macOS entitlements: `app-sandbox` + `network.client`
- iOS: `NSAllowsLocalNetworking` + Local Network usage (set in Info.plist)
