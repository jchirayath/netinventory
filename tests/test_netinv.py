"""Unit tests for netinventory pure functions. Run: python -m unittest -v
All fixtures are synthetic (AA:BB:CC / 00:11:22 MACs, 10.x IPs) — no real
network data. AA-prefixed MACs are locally-administered; 00/08-prefixed are
treated as global-OUI for the addressing-bit tests."""
import os
import sys
import unittest

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
