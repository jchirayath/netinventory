"""Unit tests for device monitors (monitor.py). Run: python -m unittest -v
Synthetic fixtures only (AA:BB:CC MACs, 10.x IPs); no network."""
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402
import connectors  # noqa: E402

T0 = dt.datetime(2026, 1, 1, 12, 0, 0)


def mon(device, **kw):
    m = {k: "" for k in monitor.FIELDS}
    m.update({"Device": device, "Enabled": "yes", **kw})
    return m


def row(device, mac, ip="10.0.0.5", status="online", hostname="", apps=""):
    return {"Device": device, "MAC": mac, "IP": ip, "Vendor": "", "Hostname": hostname,
            "Link": "", "Status": status, "FirstSeen": "", "LastSeen": "", "Source": "scan",
            "Notes": "", "RSSI": "", "AppNames": apps}


def reading(provider, name, level, online=True, mac=""):
    return {"provider": provider, "name": name, "mac": mac, "level": level,
            "pct": monitor.battery_pct(level), "online": online, "model": "", "seen": ""}


class TestParsing(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(monitor.parse_duration("30m"), 1800)
        self.assertEqual(monitor.parse_duration("2h"), 7200)
        self.assertEqual(monitor.parse_duration("1d"), 86400)
        self.assertEqual(monitor.parse_duration("45"), 2700)        # bare = minutes
        self.assertEqual(monitor.parse_duration("2 hours"), 7200)
        self.assertIsNone(monitor.parse_duration(""))
        self.assertIsNone(monitor.parse_duration("soon"))

    def test_battery_pct(self):
        self.assertEqual(monitor.battery_pct(84), 84)
        self.assertEqual(monitor.battery_pct("84"), 84)
        self.assertEqual(monitor.battery_pct("84%"), 84)
        self.assertEqual(monitor.battery_pct("low"), 10)
        self.assertEqual(monitor.battery_pct("middle"), 50)
        self.assertEqual(monitor.battery_pct("High"), 100)
        self.assertEqual(monitor.battery_pct(140), 100)
        self.assertIsNone(monitor.battery_pct(None))
        self.assertIsNone(monitor.battery_pct(True))
        self.assertIsNone(monitor.battery_pct("unavailable"))

    def test_validate(self):
        self.assertEqual(monitor.validate(mon("A", OfflineAfter="2h", BatteryBelow="20%",
                                              Check="tcp:8080")), [])
        bad = monitor.validate(mon("A", OfflineAfter="later", BatteryBelow="x",
                                   Check="carrier pigeon", Match="flume"))
        self.assertEqual(len(bad), 4)

    def test_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "monitors.csv")
            monitor.save(p, [mon("QNAP NAS", OfflineAfter="30m"), mon("Lock", Enabled="no")])
            got = monitor.load(p)
        self.assertEqual([m["Device"] for m in got], ["QNAP NAS", "Lock"])
        self.assertTrue(monitor.enabled(got[0]))
        self.assertFalse(monitor.enabled(got[1]))


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.master = {
            "AA:BB:CC:00:00:01": row("Jacob’s Mac Studio", "AA:BB:CC:00:00:01"),
            "AA:BB:CC:00:00:02": row("Front Door Camera", "AA:BB:CC:00:00:02",
                                     hostname="Front Door", apps="blink=Front Door"),
        }

    def test_rows_quote_insensitive(self):
        rows = monitor.rows_for(mon("jacob's mac studio"), self.master)
        self.assertEqual([r["MAC"] for r in rows], ["AA:BB:CC:00:00:01"])

    def test_rows_by_mac(self):
        rows = monitor.rows_for(mon("aa:bb:cc:00:00:02"), self.master)
        self.assertEqual(rows[0]["Device"], "Front Door Camera")

    def test_reading_by_provider_app_name(self):
        m = mon("Front Door Camera")
        rows = monitor.rows_for(m, self.master)
        rds = [reading("ha", "Front Door", 0), reading("blink", "Front Door", "low")]
        # blink=Front Door in AppNames picks Blink's, not HA's same-named sensor
        self.assertEqual(monitor.match_reading(m, rows, rds)["provider"], "blink")

    def test_reading_by_mac(self):
        m = mon("Front Door Camera")
        rows = monitor.rows_for(m, self.master)
        rds = [reading("tuya", "whatever", 50, mac="AA:BB:CC:00:00:02")]
        self.assertIsNotNone(monitor.match_reading(m, rows, rds))

    def test_explicit_match(self):
        rds = [reading("flume", "Flume Sensor", "low")]
        self.assertIsNotNone(monitor.match_reading(mon("Water", Match="flume=*"), [], rds))
        self.assertIsNotNone(monitor.match_reading(
            mon("Water", Match="flume=flume sensor"), [], rds))
        self.assertIsNone(monitor.match_reading(mon("Water", Match="tuya=*"), [], rds))

    def test_default_check(self):
        self.assertEqual(monitor.check_of(mon("A"), [{"MAC": "x"}]), "ping")
        self.assertEqual(monitor.check_of(mon("A"), []), "cloud")
        self.assertEqual(monitor.check_of(mon("A", Check="TCP:443"), []), "tcp:443")

    def test_candidate_ips_prefers_arp(self):
        rows = [row("A", "AA:BB:CC:00:00:01", ip="10.0.0.5")]
        ips = monitor.candidate_ips(rows, {"10.0.0.9": "AA:BB:CC:00:00:01",
                                           "10.0.0.7": "AA:BB:CC:00:00:99"})
        self.assertEqual(ips, ["10.0.0.9", "10.0.0.5"])


class TestEvaluate(unittest.TestCase):
    def test_offline_alerts_once_after_threshold_then_recovers(self):
        m, st = mon("NAS", OfflineAfter="30m"), {}
        self.assertEqual(monitor.evaluate(m, st, True, None, T0), [])
        t = T0 + dt.timedelta(minutes=20)
        self.assertEqual(monitor.evaluate(m, st, False, None, t), [])     # not long enough
        t = T0 + dt.timedelta(minutes=31)
        a = monitor.evaluate(m, st, False, None, t)
        self.assertEqual([k for k, _ in a], ["offline"])
        self.assertIn("31m", a[0][1])
        t = T0 + dt.timedelta(minutes=60)
        self.assertEqual(monitor.evaluate(m, st, False, None, t), [])     # no repeat
        a = monitor.evaluate(m, st, True, None, T0 + dt.timedelta(hours=2))
        self.assertEqual([k for k, _ in a], ["online"])
        self.assertIn("2h", a[0][1])

    def test_unchecked_changes_nothing(self):
        m, st = mon("NAS", OfflineAfter="30m"), {"last_ok": T0.isoformat()}
        self.assertEqual(monitor.evaluate(m, st, None, None, T0 + dt.timedelta(days=3)), [])

    def test_offline_from_first_run_counts_from_now(self):
        m, st = mon("NAS", OfflineAfter="1h"), {}
        self.assertEqual(monitor.evaluate(m, st, False, None, T0), [])
        a = monitor.evaluate(m, st, False, None, T0 + dt.timedelta(hours=1))
        self.assertIn("not seen since monitoring began", a[0][1])

    def test_no_offline_threshold_never_alerts(self):
        m, st = mon("Cam"), {}
        monitor.evaluate(m, st, False, None, T0)
        self.assertEqual(monitor.evaluate(m, st, False, None, T0 + dt.timedelta(days=9)), [])

    def test_battery_low_once_then_replaced(self):
        m, st = mon("Lock", BatteryBelow="20"), {}
        self.assertEqual(monitor.evaluate(m, st, None, reading("tuya", "Lock", 25), T0), [])
        a = monitor.evaluate(m, st, None, reading("tuya", "Lock", 18), T0)
        self.assertEqual([k for k, _ in a], ["battery-low"])
        self.assertEqual(monitor.evaluate(m, st, None, reading("tuya", "Lock", 10), T0), [])
        # bouncing just above the threshold doesn't re-arm
        self.assertEqual(monitor.evaluate(m, st, None, reading("tuya", "Lock", 25), T0), [])
        a = monitor.evaluate(m, st, None, reading("tuya", "Lock", 100), T0)
        self.assertEqual([k for k, _ in a], ["battery-ok"])

    def test_battery_label_low(self):
        m, st = mon("Cam", BatteryBelow="20"), {}
        a = monitor.evaluate(m, st, None, reading("blink", "Cam", "low"), T0)
        self.assertEqual([k for k, _ in a], ["battery-low"])
        self.assertIn("blink: low", a[0][1])

    def test_status_line(self):
        st = {}
        monitor.evaluate(mon("X", BatteryBelow="20"), st, False, reading("ha", "X", 84), T0)
        self.assertEqual(monitor.status_line(st, T0 + dt.timedelta(hours=3)),
                         "offline 3h, battery 84%")


class TestRun(unittest.TestCase):
    def setUp(self):
        self.master = {"AA:BB:CC:00:00:01": row("NAS", "AA:BB:CC:00:00:01"),
                       "AA:BB:CC:00:00:02": row("Cam", "AA:BB:CC:00:00:02", status="offline")}
        self.probes = []

    def fake_probe(self, result):
        def p(check, ips):
            self.probes.append((check, tuple(ips)))
            return result
        return p

    def test_watch_sweep_sends_nothing_extra(self):
        mons = [mon("NAS", OfflineAfter="1m")]
        state = {}
        monitor.run(mons, self.master, state, seen_online={"AA:BB:CC:00:00:01"},
                    now=T0, probe_fn=self.fake_probe(False))
        self.assertEqual(self.probes, [])
        self.assertTrue(state["NAS"]["up"])
        a = monitor.run(mons, self.master, state, seen_online=set(),
                        now=T0 + dt.timedelta(minutes=2), probe_fn=self.fake_probe(True))
        self.assertEqual(self.probes, [])
        self.assertEqual([k for _, k, _ in a], ["offline"])

    def test_tcp_check_probes_even_in_watch(self):
        mons = [mon("NAS", Check="tcp:8080")]
        monitor.run(mons, self.master, {}, seen_online={"AA:BB:CC:00:00:01"},
                    now=T0, probe_fn=self.fake_probe(True))
        self.assertEqual(self.probes, [("tcp:8080", ("10.0.0.5",))])

    def test_standalone_probes_once_per_device(self):
        mons = [mon("NAS"), mon("Cam", Enabled="no")]
        monitor.run(mons, self.master, {}, now=T0, probe_fn=self.fake_probe(True))
        self.assertEqual(self.probes, [("ping", ("10.0.0.5",))])

    def test_cloud_check_uses_reading_online(self):
        mons = [mon("Cam", Check="cloud", OfflineAfter="1h", BatteryBelow="20")]
        state = {}
        rds = [reading("blink", "Cam", "ok", online=False)]
        monitor.run(mons, self.master, state, rds, now=T0, probe_fn=self.fake_probe(True))
        a = monitor.run(mons, self.master, state, rds, now=T0 + dt.timedelta(hours=1),
                        probe_fn=self.fake_probe(True))
        self.assertEqual(self.probes, [])
        self.assertEqual([k for _, k, _ in a], ["offline"])


class TestBluetooth(unittest.TestCase):
    def test_ble_name(self):
        self.assertEqual(connectors.ble_name("GVH5101_4670"), "H5101_4670")
        self.assertEqual(connectors.ble_name("Govee_H5074_0447"), "H5074_0447")
        self.assertEqual(connectors.ble_name("ihoment_H5043_7038"), "H5043_7038")
        self.assertEqual(connectors.ble_name("GVHome"), "GVHome")     # not a model

    def test_govee_battery(self):
        self.assertEqual(connectors.govee_ble_battery(
            {0xEC88: bytes.fromhex("000c08c61e6402")}), 100)         # H5074
        self.assertEqual(connectors.govee_ble_battery(
            {0x0001: bytes.fromhex("0101031bfa56")}), 86)            # H5101
        self.assertIsNone(connectors.govee_ble_battery({0x004C: b"\x02\x15"}))

    def test_ble_check(self):
        mons = [mon("H5074_0447", Check="ble", OfflineAfter="1h", BatteryBelow="20",
                    Match="ble=H5074_0447")]
        heard = [reading("ble", "H5074_0447", 15), reading("ha", "H5074_0447", None, online=False)]
        other = [reading("ble", "Keys", None)]
        state = {}
        a = monitor.run(mons, {}, state, heard, now=T0)
        self.assertTrue(state["H5074_0447"]["up"])                  # HA's stale copy ignored
        self.assertEqual([k for _, k, _ in a], ["battery-low"])
        monitor.run(mons, {}, state, None, now=T0 + dt.timedelta(hours=5))   # not asked
        self.assertTrue(state["H5074_0447"]["up"])
        # the scan ran and didn't hear it: gone since its last broadcast at T0
        a = monitor.run(mons, {}, state, other, now=T0 + dt.timedelta(hours=6))
        self.assertEqual([k for _, k, _ in a], ["offline"])
        self.assertIn("6h", a[0][2])

    def test_ble_matches_govee_app_name(self):
        m = mon("Office Thermometer", Check="ble")
        home_rows = [{"Device": "Office Thermometer", "AppNames": "govee=H5101_4670"}]
        rd = monitor.match_reading(m, [], [reading("ble", "H5101_4670", 86)], home_rows)
        self.assertIsNotNone(rd)

    def test_spoofed_ble_name_is_not_trusted(self):
        # A phone renamed 'Front Door' must not feed a cloud monitor or an
        # unmatched ble monitor a battery or presence.
        spoof = [reading("ble", "Front Door", 100)]
        state = {}
        monitor.run([mon("Front Door", Check="cloud", BatteryBelow="20")], {}, state, spoof, now=T0)
        self.assertNotIn("battery", state["Front Door"])
        self.assertIsNone(state["Front Door"].get("up"))
        rd = monitor.match_reading(mon("Front Door", Check="ble"), [], spoof)
        self.assertIsNone(rd)

    def test_suggest_ble(self):
        rds = [reading("ble", "H5074_0447", 100), reading("ble", "H5043_7038", None),
               reading("ble", "Jacob's Keys", None)]
        got = {m["Device"]: m for m, _ in monitor.suggest({}, rds, [])}
        self.assertEqual((got["H5074_0447"]["Check"], got["H5074_0447"]["BatteryBelow"]),
                         ("ble", "20"))
        self.assertEqual(got["H5043_7038"]["BatteryBelow"], "20")
        self.assertNotIn("Jacob's Keys", got)


class TestSuggest(unittest.TestCase):
    def test_suggestions(self):
        master = {
            "AA:BB:CC:00:00:01": row("QNAP NAS", "AA:BB:CC:00:00:01"),
            "AA:BB:CC:00:00:02": row("Jacob's Mac Studio", "AA:BB:CC:00:00:02"),
            "AA:BB:CC:00:00:03": row("Anshul's MacBook Pro", "AA:BB:CC:00:00:03"),
            "AA:BB:CC:00:00:04": row("Front Door Camera", "AA:BB:CC:00:00:04",
                                     apps="blink=Front Door"),
            "AA:BB:CC:00:00:05": row("Wifi Smart Lock", "AA:BB:CC:00:00:05"),
            "AA:BB:CC:00:00:06": row("Old NAS", "AA:BB:CC:00:00:06", status="defunct"),
            "AA:BB:CC:00:00:07": row("Echo Dot with clock", "AA:BB:CC:00:00:07"),
            "AA:BB:CC:00:00:08": row("Shed Camera", "AA:BB:CC:00:00:08", status="defunct",
                                     apps="blink=Shed"),
            "AA:BB:CC:00:00:09": row("Mini Camera", "AA:BB:CC:00:00:09", status="offline"),
            "AA:BB:CC:00:00:0A": row("Patio Door Sensor", "AA:BB:CC:00:00:0A",
                                     apps="tuya=Patio Door Sensor"),
        }
        rds = [reading("blink", "Front Door", "ok"), reading("tuya", "Garage door", "high"),
               reading("blink", "Shed", "ok"), reading("tuya", "Patio Door Sensor", "high")]
        got = {m["Device"]: m for m, _ in monitor.suggest(
            master, rds, [mon("Wifi Smart Lock")], self_macs={"AA:BB:CC:00:00:02"})}
        self.assertEqual(got["QNAP NAS"]["OfflineAfter"], "30m")
        self.assertNotIn("Jacob's Mac Studio", got)         # this machine
        self.assertNotIn("Anshul's MacBook Pro", got)       # laptop
        self.assertNotIn("Wifi Smart Lock", got)            # already monitored
        self.assertNotIn("Old NAS", got)                    # defunct
        self.assertNotIn("Echo Dot with clock", got)        # 'clock' is not a lock
        cam = got["Front Door Camera"]
        self.assertEqual((cam["BatteryBelow"], cam["Check"]), ("20", "cloud"))
        lock = got["Garage door"]                          # cloud-only battery device
        self.assertEqual((lock["Check"], lock["Match"]), ("cloud", "tuya=Garage door"))
        # a sleepy battery camera keeps its own name even though it reads defunct
        self.assertEqual(got["Shed Camera"]["Check"], "cloud")
        self.assertNotIn("Shed", got)
        # a battery Wi-Fi sensor is judged by its cloud, never by ping
        self.assertEqual(got["Patio Door Sensor"]["Check"], "cloud")
        # a plugged-in camera already offline comes in switched off
        self.assertEqual((got["Mini Camera"]["Enabled"], got["Mini Camera"]["OfflineAfter"]),
                         ("no", "2h"))
        self.assertEqual(got["QNAP NAS"]["Enabled"], "yes")

    def test_rerun_adds_no_duplicates(self):
        master = {"AA:BB:CC:00:00:01": row("Outside Garbage Door Camera", "AA:BB:CC:00:00:01",
                                           apps="blink=Garbage Door"),
                  "AA:BB:CC:00:00:02": row("Living Room TV Hub", "AA:BB:CC:00:00:02")}
        rds = [reading("blink", "Garbage Door", "ok")]
        first = [m for m, _ in monitor.suggest(master, rds, [])]
        self.assertEqual([m["Device"] for m in first], ["Outside Garbage Door Camera"])
        self.assertEqual(monitor.suggest(master, rds, first), [])


class TestConnectorReadings(unittest.TestCase):
    def test_tuya_level_prefers_label_over_stuck_zero(self):
        st = [{"code": "residual_electricity", "value": 0}, {"code": "battery_state", "value": "high"}]
        self.assertEqual(connectors._tuya_level(st), "high")
        self.assertEqual(connectors._tuya_level([{"code": "battery_percentage", "value": 64}]), 64)
        self.assertEqual(connectors._tuya_level([{"code": "battery_state", "value": "low"}]), "low")
        self.assertIsNone(connectors._tuya_level([{"code": "switch_1", "value": True}]))


if __name__ == "__main__":
    unittest.main()
