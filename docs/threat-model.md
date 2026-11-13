# Threat model

One household's network inventory, in three copies (see [architecture.md](architecture.md)).
This page lists what is worth protecting, who might try, where the boundaries are,
and the control that holds each one. Revisit it when a copy, a route or a
credential is added.

## What is worth protecting

| Asset | Where | Why it matters |
|---|---|---|
| Vendor cloud logins (Tuya, Wyze, Blink, SmartThings, Govee, Flume, HA), router login, SMTP | Mac Keychain; owner-only `~/.netinv/credentials.json` on the Pi | control of cameras, locks, the router |
| The edit passcode | stored as a salted scrypt hash on each copy | lets a holder change names, rooms, monitors, the battery log |
| The agent token | Pi and betty credential stores | lets the holder read and rewrite the synced files on betty |
| The inventory itself: MACs, IPs, names, rooms, join/leave history | the CSVs on all three copies | a map of the house and when people come and go |

## Who might try

| Actor | Reach |
|---|---|
| A website open in a browser on the home Wi-Fi | can make the browser send requests to LAN addresses (CSRF, DNS rebinding, framing) |
| A guest or device on the home Wi-Fi | reaches the Pi's dashboard over plain HTTP; can sniff or ARP-spoof |
| Anyone on the internet | reaches net.aspl.net: Entra's sign-in page, and `/api/agent/*` |
| A device on the LAN that lies (SSDP, mDNS, Bluetooth names) | feeds names and URLs into the scan |
| Another local account on the Mac | can read process lists |

## Boundaries and their controls

| Boundary | Control | Where |
|---|---|---|
| Browser on the LAN → any copy | Host header allow-list (no DNS rebinding); POST needs `application/json` and a matching `Origin` (no CSRF); no CORS header; CSP with `frame-ancestors 'none'` and `X-Frame-Options: DENY` (no clickjacking); no value inside an inline handler | `make_server`, `HostCheck`, `_security_headers` |
| LAN client → Pi edits | passcode on every write except from the machine itself; loopback is exempt only without proxy headers, so Caddy's requests always need it; 10 misses in 10 min lock that client out | `_local_client`, `PasscodeGuard`, `passcode_ok` |
| Internet → net.aspl.net | Entra sign-in in Caddy for everything except the two agent paths; edits also need the passcode | betty `sites/net.aspl.net.caddy` |
| Internet → `/api/agent/*` | bearer token, constant-time compare, own lockout counted per visitor (`NETINV_TRUSTED_PROXIES` makes Caddy's `X-Forwarded-For` count; `deploy-betty.sh` refuses to finish without it); bodies over 1 MB read only after the token checks out; gzip capped at 256 MB | `_agent_ok`, `_agent_sync` |
| The agent token's reach | `/api/agent/*` only. It can rewrite every file in `sync.FILES` on betty, with or without `--allow-write`, and nothing outside them (names checked against the list: no path traversal, no credentials) | `sync.agent_import` |
| Pi → betty (the token in transit) | https only; redirects refused, so the token can't be sent on to another host | `sync.check_agent_url`, `agent_opener` |
| Lying LAN devices → the scan | SSDP descriptions fetched only over http from a literal LAN IP, without redirects; Bluetooth names trusted only for `ble` monitors with an explicit match | `ssdp_discover`, `monitor.match_reading` |
| Credentials between copies | `creds-export` refuses a terminal; `--only agent,serve` for a copy that scans nothing (betty holds only those two); sync never carries credentials | `cmd_creds` |
| Edits after the fact | every edit and refused edit through serve is a line in `audit.csv` (who, what, answer), kept per copy and never synced | `audit` |

## Known gaps, accepted for now

- **The Pi's dashboard is plain HTTP on the LAN**, so the passcode crosses the Wi-Fi in
  clear text and a guest who can sniff or ARP-spoof could capture it. A TLS front on
  the Pi's Caddy would close this. The Wi-Fi password is the boundary until then.
- **The browser keeps the passcode in `localStorage`** so each phone asks only once.
  The CSP and the escaping make a script injection unlikely, but a successful one could
  read it.
- **The Mac passes secrets to the `security` command as arguments** when storing them,
  so another local account could see them in a process list during that moment. The
  Mac has one user.
- **The scripts in `<script>` blocks are inline**, so the CSP allows `'unsafe-inline'`
  for scripts. Moving them to a file would allow a stricter policy.
