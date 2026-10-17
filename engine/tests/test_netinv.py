"""Unit tests for netinventory pure functions. Run: python -m unittest -v
All fixtures are synthetic (AA:BB:CC / 00:11:22 MACs, 10.x IPs) — no real
network data. AA-prefixed MACs are locally-administered; 00/08-prefixed are
treated as global-OUI for the addressing-bit tests."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import netinv  # noqa: E402
import connectors  # noqa: E402


class TestMac(unittest.TestCase):
    def test_norm_pads_and_uppercases(self):
        self.assertEqual(netinv.norm_mac("aa:bb:cc:1:2:3"), "AA:BB:CC:01:02:03")
        self.assertEqual(netinv.norm_mac("00-11-22-33-44-05"), "00:11:22:33:44:05")

    def test_norm_empty(self):
        self.assertEqual(netinv.norm_mac(""), "")

    def test_local_admin(self):
        self.assertTrue(netinv.is_local_admin_mac("AA:BB:CC:00:00:01"))   # AA: bit 0x2 set
        self.assertTrue(netinv.is_local_admin_mac("A2:00:00:00:00:01"))   # A2: bit 0x2 set
        self.assertFalse(netinv.is_local_admin_mac("00:11:22:33:44:55"))  # 00: global
        self.assertFalse(netinv.is_local_admin_mac("08:00:27:00:00:01"))  # 08: global

    def test_oui_skips_random_mac(self):
        self.assertEqual(netinv.oui_lookup("AA:BB:CC:00:00:01"), "")  # LA -> no OUI

    def test_oui_resolves_known_prefix(self):
        # 00:00:0C is Cisco's classic OUI — stable in the bundled DB
        self.assertIn("Cisco", netinv.oui_lookup("00:00:0C:12:34:56"))


class TestSubnet(unittest.TestCase):
    def test_prefix_derivation(self):
        self.assertEqual(netinv.subnet_prefix("192.168.1.0/24"), "192.168.1.")
        self.assertEqual(netinv.subnet_prefix("10.0.0.0/24"), "10.0.0.")
        self.assertEqual(netinv.subnet_prefix("172.16.5.0/24"), "172.16.5.")

    def test_is_lan_ip_uses_configured_subnet(self):
        # patch the module-derived prefix to a 10.x net and confirm filtering
        orig = netinv.SUBNET
        try:
            netinv.SUBNET = "10.0.0.0/24"
            self.assertTrue(netinv.is_lan_ip("10.0.0.42"))
            self.assertFalse(netinv.is_lan_ip("10.0.0.255"))
            self.assertFalse(netinv.is_lan_ip("192.168.1.42"))
        finally:
            netinv.SUBNET = orig


class TestSortAndFilter(unittest.TestCase):
    def test_ip_key_orders_numerically(self):
        ips = ["10.0.0.20", "10.0.0.3", "10.0.0.100"]
        self.assertEqual(sorted(ips, key=netinv.ip_key),
                         ["10.0.0.3", "10.0.0.20", "10.0.0.100"])

    def test_ip_key_empty_sorts_last(self):
        self.assertGreater(netinv.ip_key(""), netinv.ip_key("10.0.0.254"))

    def test_better_prefers_meaningful(self):
        self.assertEqual(netinv.better("Acme", ""), "Acme")
        self.assertEqual(netinv.better("N/A", "Acme"), "Acme")
        self.assertEqual(netinv.better("", "Acme"), "Acme")


class TestNameQuality(unittest.TestCase):
    def test_friendly_beats_model_beats_junk(self):
        self.assertEqual(netinv.name_quality(""), 0)
        self.assertEqual(netinv.name_quality("aa:bb:cc:00:00:01"), 1)
        self.assertEqual(netinv.name_quality("wlan0-53"), 1)
        self.assertGreaterEqual(netinv.name_quality("Family Room TV"), 3)
        self.assertGreater(netinv.name_quality("Family Room TV"),
                           netinv.name_quality("wlan0-53"))


class TestLinkBand(unittest.TestCase):
    def test_link_class(self):
        self.assertEqual(netinv.link_class("Ethernet"), "Wired")
        self.assertEqual(netinv.link_class("Wi-Fi 5 GHz"), "Wireless")
        self.assertEqual(netinv.link_class(""), "")

    def test_band(self):
        self.assertEqual(netinv.band("Wi-Fi 2.4 GHz"), "2.4 GHz")
        self.assertEqual(netinv.band("Wi-Fi 5 GHz"), "5 GHz")
        self.assertEqual(netinv.band("Ethernet"), "Ethernet")
        self.assertEqual(netinv.band(""), "")


class TestStatus(unittest.TestCase):
    def test_empty_lastseen_is_defunct(self):
        self.assertEqual(netinv.stale_status(""), "defunct")

    def test_recent_is_offline(self):
        import datetime as dt
        recent = dt.date.today().isoformat()
        self.assertEqual(netinv.stale_status(recent), "offline")


class TestWifiScan(unittest.TestCase):
    SCAN = (
        "2.4 GHz\t6\tAA:BB:CC:00:00:01\tNeighborNet\t-84 dBm\tb,g,n\tWPA2\n"
        "AA:BB:CC:00:00:02\tMyHomeWiFi\t-61 dBm\tb,g,n\tWPA2\n"
        "11\tAA:BB:CC:00:00:03\t\t-66 dBm\tg,n\tWPA2\n"
    )

    def test_parse_counts_and_fields(self):
        items = netinv.parse_wifi_scan(self.SCAN)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["bssid"], "AA:BB:CC:00:00:01")
        self.assertEqual(items[0]["ssid"], "NeighborNet")
        self.assertEqual(items[0]["rssi"], -84)
        self.assertEqual(items[2]["ssid"], "")  # hidden


class TestRadioMatch(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(netinv.radio_match("AA:BB:CC:00:00:01", {"AA:BB:CC:00:00:01"}),
                         "AA:BB:CC:00:00:01")

    def test_la_bit_flip_same_last5(self):
        # client vs its AP/Direct radio: differ in first octet, share last 5
        self.assertEqual(netinv.radio_match("AE:11:22:33:44:55", {"AC:11:22:33:44:55"}),
                         "AC:11:22:33:44:55")

    def test_small_last_octet_offset(self):
        self.assertEqual(netinv.radio_match("AA:BB:CC:00:00:23", {"AA:BB:CC:00:00:20"}),
                         "AA:BB:CC:00:00:20")

    def test_no_match(self):
        self.assertIsNone(netinv.radio_match("AA:BB:CC:00:00:01", {"00:11:22:33:44:55"}))


class TestRouterParse(unittest.TestCase):
    HTML = (
        'var onlineHostMAC = ["AA:BB:CC:00:00:01"];\n'
        'var onlineHostNameArr = ["living-room-tv"];\n'
        'var offlineHostMAC = ["AA:BB:CC:00:00:09"];\n'
        'var offlineHostNameArr = ["old-thing"];\n'
        "IPv4 Address</b><br/></dd>10.0.0.20<dd><b id='macaddlocnew'>MAC Address"
        "</b><br/></dd>AA:BB:CC:00:00:01"
        "MAC Address</b><br/></dd>AA:BB:CC:00:00:01 lorem "
        "headers='rssi-level'> -72 dBm </td>"
        "headers='connection-type' id=ethcon><span id=ethcon>Wi-Fi 5 GHz</span>"
    )

    def test_parses_online_offline_ip_band(self):
        s = netinv.parse_router_html(self.HTML)
        by_mac = {x["mac"]: x for x in s}
        self.assertIn("AA:BB:CC:00:00:01", by_mac)
        self.assertTrue(by_mac["AA:BB:CC:00:00:01"]["online"])
        self.assertEqual(by_mac["AA:BB:CC:00:00:01"]["ip"], "10.0.0.20")
        self.assertEqual(by_mac["AA:BB:CC:00:00:01"]["link"], "Wi-Fi 5 GHz")
        self.assertFalse(by_mac["AA:BB:CC:00:00:09"]["online"])

    def test_parses_rssi(self):
        by_mac = {x["mac"]: x for x in netinv.parse_router_html(self.HTML)}
        # Wi-Fi client gets its signal; the offline one (no rssi cell) stays None
        self.assertEqual(by_mac["AA:BB:CC:00:00:01"]["rssi"], -72)
        self.assertIsNone(by_mac["AA:BB:CC:00:00:09"]["rssi"])


class TestCategorizeGuess(unittest.TestCase):
    def test_categorize(self):
        self.assertEqual(netinv.categorize({"Device": "Blink Mini Camera",
                                            "Hostname": "", "Vendor": ""}),
                         "Camera / Security")
        self.assertEqual(netinv.categorize({"Device": "Ecobee Thermostat",
                                            "Hostname": "", "Vendor": ""}),
                         "Thermostat")

    def test_guess_type_from_port(self):
        g = netinv.guess_type("Apple", {"ports": [62078], "services": ["62078:iphone-sync"]})
        self.assertIn("iPhone", g)


class TestEventsRetention(unittest.TestCase):
    def test_prune_drops_old_keeps_recent(self):
        import csv
        import datetime as dt
        import tempfile
        old = (dt.date.today() - dt.timedelta(days=400)).isoformat() + "T10:00:00"
        new = dt.date.today().isoformat() + "T10:00:00"
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Timestamp", "Event", "MAC"])
            w.writerow([old, "join", "AA:BB:CC:00:00:01"])
            w.writerow([new, "join", "AA:BB:CC:00:00:02"])
        orig_log, orig_days = netinv.EVENTS_LOG, netinv.EVENTS_RETENTION_DAYS
        try:
            netinv.EVENTS_LOG, netinv.EVENTS_RETENTION_DAYS = path, 365
            netinv.prune_events()
            with open(path, newline="") as f:
                rows = list(csv.reader(f, delimiter=";"))
        finally:
            netinv.EVENTS_LOG, netinv.EVENTS_RETENTION_DAYS = orig_log, orig_days
            os.remove(path)
        macs = [r[2] for r in rows[1:]]
        self.assertIn("AA:BB:CC:00:00:02", macs)     # recent kept
        self.assertNotIn("AA:BB:CC:00:00:01", macs)  # old pruned


class TestAlerts(unittest.TestCase):
    def _row(self, **kw):
        base = {"Device": "", "MAC": "AA:BB:CC:00:00:01", "IP": "10.0.0.5",
                "Vendor": "", "Hostname": "", "Link": "", "Status": "online",
                "FirstSeen": "2000-01-01", "LastSeen": "2000-01-01"}
        base.update(kw)
        return base

    def test_unknown_online_flagged(self):
        master = {"m": self._row()}  # online, no vendor, unnamed
        alerts = netinv.evaluate_alerts(master)
        self.assertTrue(any(a["rule"] == "unknown-online" for a in alerts))

    def test_named_wired_device_no_high_alert(self):
        master = {"m": self._row(Device="Office PC", Vendor="Intel", Status="online")}
        alerts = netinv.evaluate_alerts(master)
        self.assertFalse(any(a["severity"] == "high" for a in alerts))

    def test_severity_sort(self):
        master = {"a": self._row(Device="X", Status="offline"),   # low
                  "b": self._row()}                                # high
        sevs = [a["severity"] for a in netinv.evaluate_alerts(master)]
        self.assertEqual(sevs, sorted(sevs, key=lambda s: {"high": 0, "med": 1, "low": 2}[s]))

    def test_weak_signal_alert_fires_with_rssi(self):
        master = {"m": self._row(Device="Laptop", Vendor="Intel",
                                 Link="Wi-Fi 5 GHz", Status="online", RSSI="-80")}
        alerts = netinv.evaluate_alerts(master)
        weak = [a for a in alerts if a["rule"] == "weak-signal"]
        self.assertEqual(len(weak), 1)
        self.assertEqual(weak[0]["rssi"], -80)

    def test_weak_signal_alert_ignores_good_and_offline(self):
        master = {"good": self._row(MAC="AA:BB:CC:00:00:02", Link="Wi-Fi 5 GHz",
                                    Status="online", RSSI="-55"),
                  "off": self._row(MAC="AA:BB:CC:00:00:03", Link="Wi-Fi 5 GHz",
                                   Status="offline", RSSI="-88")}
        alerts = netinv.evaluate_alerts(master)
        self.assertFalse(any(a["rule"] == "weak-signal" for a in alerts))

    def test_rssi_of_parses_and_defaults(self):
        self.assertEqual(netinv.rssi_of({"RSSI": "-80"}), -80)
        self.assertIsNone(netinv.rssi_of({"RSSI": ""}))
        self.assertIsNone(netinv.rssi_of({}))
        self.assertEqual(netinv.rssi_label(-80), "-80 dBm (weak)")
        self.assertEqual(netinv.rssi_label(None), "")


class TestApi(unittest.TestCase):
    def _master(self):
        def row(**kw):
            base = {"Device": "", "MAC": "AA:BB:CC:00:00:01", "IP": "10.0.0.5",
                    "Vendor": "", "Hostname": "", "Link": "Ethernet",
                    "Status": "online", "FirstSeen": "2026-01-01",
                    "LastSeen": "2026-01-01", "Source": "scan", "Notes": ""}
            base.update(kw)
            return base
        return {"a": row(Device="Office PC", IP="10.0.0.9"),
                "b": row(MAC="AA:BB:CC:00:00:02", IP="10.0.0.2", Status="offline",
                         Link="Wi-Fi 5 GHz")}

    def test_devices_sorted_and_enriched(self):
        d = netinv.api_devices(self._master())
        self.assertEqual([x["IP"] for x in d], ["10.0.0.2", "10.0.0.9"])  # IP-sorted
        self.assertIn("category", d[0])
        self.assertIn("band", d[0])

    def test_signal_class_buckets(self):
        self.assertEqual(netinv.signal_class(-55), "good")
        self.assertEqual(netinv.signal_class(-67), "good")
        self.assertEqual(netinv.signal_class(-70), "fair")
        self.assertEqual(netinv.signal_class(-80), "weak")
        self.assertEqual(netinv.signal_class(None), "")

    def test_devices_expose_rssi_and_signal(self):
        m = self._master()
        m["b"]["RSSI"] = "-80"          # the Wi-Fi device
        d = {x["MAC"]: x for x in netinv.api_devices(m)}
        wifi = d["AA:BB:CC:00:00:02"]
        self.assertEqual(wifi["rssi"], -80)
        self.assertEqual(wifi["signal"], "weak")
        wired = d["AA:BB:CC:00:00:01"]  # no RSSI -> null / no bucket
        self.assertIsNone(wired["rssi"])
        self.assertEqual(wired["signal"], "")

    def test_summary_counts(self):
        s = netinv.api_summary(self._master())
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["online"], 1)
        self.assertEqual(s["by_band"].get("5 GHz"), 1)
        self.assertEqual(s["by_band"].get("Ethernet"), 1)


class TestMdns(unittest.TestCase):
    """mDNS/DNS-SD parsing and its fill-only vendor merge (synthetic packets)."""
    import struct as _struct

    def _lan_ip(self):
        # a valid host IP under whatever subnet is configured (default .9)
        oct3 = [int(x) for x in netinv.subnet_prefix().split(".") if x]
        return oct3 + [9]

    def _resp(self):
        """A response carrying A + SRV(compressed target) + TXT + PTR for one
        AirPlay instance 'Living Room' (model AppleTV6,2) on the LAN subnet."""
        st = self._struct
        buf = bytearray(st.pack(">HHHHHH", 0, 0x8400, 0, 4, 0, 0))
        a_off = len(buf)
        buf += netinv._dns_encode_name("appletv.local")
        buf += st.pack(">HHIH", 1, 1, 120, 4) + bytes(self._lan_ip())         # A
        inst = "DEADBEEF1234@Living Room._airplay._tcp.local"
        buf += netinv._dns_encode_name(inst)
        srv = st.pack(">HHH", 0, 0, 7000) + st.pack(">H", 0xC000 | a_off)      # SRV
        buf += st.pack(">HHIH", 33, 1, 120, len(srv)) + srv
        buf += netinv._dns_encode_name(inst)
        txt = b"\x0dmd=AppleTV6,2\x11fn=Living Room TV"                        # TXT
        buf += st.pack(">HHIH", 16, 1, 120, len(txt)) + txt
        buf += netinv._dns_encode_name("_airplay._tcp.local")
        ptr = netinv._dns_encode_name(inst)                                    # PTR
        buf += st.pack(">HHIH", 12, 1, 120, len(ptr)) + ptr
        return bytes(buf)

    def test_parse_links_a_srv_txt(self):
        a, srv, txt, inst = {}, {}, {}, {}
        netinv._parse_mdns(self._resp(), a, srv, txt, inst)
        self.assertEqual(a.get("appletv.local"),
                         ".".join(str(o) for o in self._lan_ip()))
        key = "deadbeef1234@living room._airplay._tcp.local"
        self.assertEqual(srv.get(key), "appletv.local")
        self.assertEqual(txt[key]["md"], "AppleTV6,2")
        self.assertEqual(txt[key]["fn"], "Living Room TV")

    def test_parse_collects_srv_port(self):
        a, srv, txt, inst, ports = {}, {}, {}, {}, {}
        netinv._parse_mdns(self._resp(), a, srv, txt, inst, ports)
        self.assertEqual(ports["deadbeef1234@living room._airplay._tcp.local"], 7000)

    def test_spotify_generic_labels_are_junk(self):
        for n in ("SpotifyConnect", "SpotifyConnect #4", "SpotifyConnect (2)",
                  "StreamSpotifyConnect"):
            self.assertEqual(netinv.name_quality(n), 1, n)
        self.assertEqual(netinv.name_quality("Amazon Echo Dot with clock"), 3)

    def test_spotify_getinfo_name_or_brand_model(self):
        import http.server
        import json
        import threading
        replies = {"/a": {"remoteName": "", "brandDisplayName": "Amazon",
                          "modelDisplayName": "Echo_Dot_with_clock"},
                   "/b": {"remoteName": "Onkyo  TX-NR676", "brandDisplayName": "ONKYO",
                          "modelDisplayName": "TX-NR676"},
                   "/c": {"remoteName": "", "brandDisplayName": "", "modelDisplayName": ""}}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps(replies[self.path.split("?")[0]]).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            with mock.patch.object(netinv, "is_lan_ip", return_value=True):
                got = netinv._spotify_names({k: ("127.0.0.1", port, k) for k in replies})
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(got, {"/a": ("Amazon Echo Dot with clock", "Echo Dot with clock"),
                               "/b": ("Onkyo TX-NR676", "TX-NR676")})

    def test_auto_name_numbers_look_alikes(self):
        m = {}
        for i, (dev, host) in enumerate([("Amazon Echo Dot", "x"), ("", "Amazon Echo Dot"),
                                         ("", "Amazon Echo Dot"), ("Mine", "Amazon Echo Show")]):
            mac = f"AA:BB:CC:00:00:0{i}"
            m[mac] = dict({k: "" for k in netinv.FIELDS}, MAC=mac, Device=dev, Hostname=host)
        self.assertEqual(netinv.auto_name(m), 2)
        self.assertEqual([r["Device"] for r in m.values()],
                         ["Amazon Echo Dot", "Amazon Echo Dot 2", "Amazon Echo Dot 3", "Mine"])

    def test_clean_model_rejects_numeric_metadata(self):
        self.assertEqual(netinv._clean_model("0,1,2"), "")   # _raop metadata list
        self.assertEqual(netinv._clean_model("AppleTV6,2"), "AppleTV6,2")

    def test_looks_like_id(self):
        self.assertTrue(netinv._looks_like_id("B61A2CDE8909DB5D-7BCE5BF6DAF65E9A"))
        self.assertTrue(netinv._looks_like_id("76aae0d3-a313-504f-9a9b-ccf7497a927f"))
        self.assertFalse(netinv._looks_like_id("Living Room"))
        self.assertFalse(netinv._looks_like_id("Epson837"))

    def test_model_fills_empty_vendor_only(self):
        # empty vendor -> model fills it
        m = {"AA:BB:CC:00:00:01": {k: "" for k in netinv.FIELDS}}
        m["AA:BB:CC:00:00:01"]["MAC"] = "AA:BB:CC:00:00:01"
        netinv.merge(m, [{"mac": "AA:BB:CC:00:00:01", "ip": "10.0.0.5",
                          "vendor": "", "model": "Mac15,11", "hostname": "Studio",
                          "source": "mdns", "online": True}])
        self.assertEqual(m["AA:BB:CC:00:00:01"]["Vendor"], "Mac15,11")

    def test_model_never_clobbers_oui_vendor(self):
        # real OUI vendor present -> model must NOT overwrite it
        m = {"00:11:22:33:44:55": {k: "" for k in netinv.FIELDS}}
        m["00:11:22:33:44:55"].update(MAC="00:11:22:33:44:55", Vendor="Apple, Inc.")
        netinv.merge(m, [{"mac": "00:11:22:33:44:55", "ip": "10.0.0.6",
                          "vendor": "", "model": "AppleTV6,2", "hostname": "Den",
                          "source": "mdns", "online": True}])
        self.assertEqual(m["00:11:22:33:44:55"]["Vendor"], "Apple, Inc.")


class TestApplyName(unittest.TestCase):
    def _master(self):
        return {"AA:BB:CC:00:00:01": {k: "" for k in netinv.FIELDS} |
                {"MAC": "AA:BB:CC:00:00:01"}}

    def test_renames_existing(self):
        m = self._master()
        self.assertEqual(netinv.apply_name(m, ["aa:bb:cc:0:0:1"], "  Office PC  "), 1)
        self.assertEqual(m["AA:BB:CC:00:00:01"]["Device"], "Office PC")  # trimmed

    def test_empty_name_clears(self):
        m = self._master()
        m["AA:BB:CC:00:00:01"]["Device"] = "Old"
        netinv.apply_name(m, ["AA:BB:CC:00:00:01"], "")
        self.assertEqual(m["AA:BB:CC:00:00:01"]["Device"], "")

    def test_length_capped(self):
        m = self._master()
        netinv.apply_name(m, ["AA:BB:CC:00:00:01"], "x" * 200)
        self.assertEqual(len(m["AA:BB:CC:00:00:01"]["Device"]), netinv.MAX_NAME_LEN)

    def test_create_false_skips_unknown(self):
        m = self._master()
        self.assertEqual(
            netinv.apply_name(m, ["DE:AD:BE:EF:00:01"], "Ghost", create=False), 0)
        self.assertNotIn("DE:AD:BE:EF:00:01", m)

    def test_create_true_adds_manual(self):
        m = self._master()
        self.assertEqual(netinv.apply_name(m, ["DE:AD:BE:EF:00:01"], "New"), 1)
        self.assertEqual(m["DE:AD:BE:EF:00:01"]["Source"], "manual")

    def test_bad_mac_ignored(self):
        m = self._master()
        self.assertEqual(netinv.apply_name(m, ["not-a-mac"], "X"), 0)


class TestConnectors(unittest.TestCase):
    def test_tuya_id_embeds_mac_for_hex_id(self):
        # all-hex WiFi id: trailing 12 hex are the MAC
        self.assertEqual(connectors._mac_from_tuya_id("0020032900aabbcc0011"),
                         "00:AA:BB:CC:00:11")

    def test_tuya_uuid_with_letters_yields_no_mac(self):
        self.assertEqual(connectors._mac_from_tuya_id("eb00112233445566klmno"), "")

    def test_norm_mac(self):
        self.assertEqual(connectors._norm_mac("aa:bb:cc:0:0:1"), "AA:BB:CC:00:00:01")


if __name__ == "__main__":
    unittest.main()
