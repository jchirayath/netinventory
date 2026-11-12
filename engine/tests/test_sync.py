"""Unit tests for sync.py: the merge rules, and a full two-way sync between
two temporary data directories. Synthetic data only."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync  # noqa: E402

DEV_HDR = ["Device", "MAC", "IP", "Status", "Notes"]


def csvtext(header, *rows):
    return sync.dump(header, [dict(zip(header, r)) for r in rows])


def rows(text):
    return sync.parse(text)[1]


class TestMerge3(unittest.TestCase):
    H = ["Device", "Enabled", "OfflineAfter"]

    def m3(self, base, mine, theirs, mine_newer=True):
        out_m, out_t, _b = sync.merge_file("monitors.csv", csvtext(self.H, *base),
                                           csvtext(self.H, *mine), csvtext(self.H, *theirs),
                                           mine_newer)
        return rows(out_m), rows(out_t)

    def test_one_side_edit_wins_either_way(self):
        base = [("NAS", "yes", "30m")]
        m, t = self.m3(base, [("NAS", "no", "30m")], [("NAS", "yes", "2h")])
        self.assertEqual(m, t)
        self.assertEqual((m[0]["Enabled"], m[0]["OfflineAfter"]), ("no", "2h"))  # both kept

    def test_same_field_both_sides_newer_wins(self):
        base = [("NAS", "yes", "30m")]
        m, _ = self.m3(base, [("NAS", "yes", "1h")], [("NAS", "yes", "2h")], mine_newer=False)
        self.assertEqual(m[0]["OfflineAfter"], "2h")
        m, _ = self.m3(base, [("NAS", "yes", "1h")], [("NAS", "yes", "2h")], mine_newer=True)
        self.assertEqual(m[0]["OfflineAfter"], "1h")

    def test_additions_from_both(self):
        m, t = self.m3([], [("A", "yes", "1h")], [("B", "yes", "1h")])
        self.assertEqual(sorted(r["Device"] for r in m), ["A", "B"])
        self.assertEqual(sorted(r["Device"] for r in t), ["A", "B"])

    def test_delete_vs_untouched_deletes_but_edit_beats_delete(self):
        base = [("A", "yes", "1h"), ("B", "yes", "1h")]
        m, t = self.m3(base, [("B", "yes", "1h")],                 # mine deleted A
                       [("A", "yes", "1h"), ("B", "no", "1h")])    # theirs left A alone
        self.assertEqual([r["Device"] for r in m], ["B"])
        self.assertEqual([r["Device"] for r in t], ["B"])
        m, _ = self.m3(base, [("B", "yes", "1h")],                 # mine deleted A
                       [("A", "no", "1h"), ("B", "yes", "1h")])    # theirs edited A
        self.assertIn("A", [r["Device"] for r in m])


class TestDevices(unittest.TestCase):
    def test_only_names_and_notes_sync_and_nothing_is_deleted(self):
        base = csvtext(DEV_HDR, ("Cam", "AA:BB:CC:00:00:01", "10.0.0.5", "online", ""))
        mine = csvtext(DEV_HDR, ("Garage Cam", "AA:BB:CC:00:00:01", "10.0.0.5", "online", ""))
        theirs = csvtext(DEV_HDR, ("Cam", "AA:BB:CC:00:00:01", "10.0.0.9", "offline", "new batt"),
                         ("", "AA:BB:CC:00:00:02", "10.0.0.7", "online", ""))
        out_m, out_t, _ = sync.merge_file("devices.csv", base, mine, theirs)
        m, t = {r["MAC"]: r for r in rows(out_m)}, {r["MAC"]: r for r in rows(out_t)}
        mac = "AA:BB:CC:00:00:01"
        self.assertEqual((m[mac]["Device"], m[mac]["Notes"]), ("Garage Cam", "new batt"))
        self.assertEqual((t[mac]["Device"], t[mac]["Notes"]), ("Garage Cam", "new batt"))
        self.assertEqual((m[mac]["IP"], m[mac]["Status"]), ("10.0.0.5", "online"))   # mine's scan
        self.assertEqual((t[mac]["IP"], t[mac]["Status"]), ("10.0.0.9", "offline"))  # theirs'
        self.assertIn("AA:BB:CC:00:00:02", m)          # a device the other copy scanned
        # a MAC missing on one side is never taken as a deletion
        out_m, _, _ = sync.merge_file("devices.csv", base, csvtext(DEV_HDR), base)
        self.assertEqual(len(rows(out_m)), 1)


class TestScannerMode(unittest.TestCase):
    def test_scan_data_flows_from_scanner_edits_both_ways(self):
        base = csvtext(DEV_HDR, ("Cam", "AA:BB:CC:00:00:01", "10.0.0.5", "online", ""))
        mine = csvtext(DEV_HDR, ("Cam", "AA:BB:CC:00:00:01", "10.0.0.8", "offline", ""),
                       ("", "AA:BB:CC:00:00:03", "10.0.0.3", "online", ""))     # newly scanned
        theirs = csvtext(DEV_HDR, ("Garage Cam", "AA:BB:CC:00:00:01", "10.0.0.5", "online",
                                   "batteries 11/11"))                         # edited on the Pi
        out_m, out_t, _ = sync.merge_file("devices.csv", base, mine, theirs, scanner=True)
        m, t = {r["MAC"]: r for r in rows(out_m)}, {r["MAC"]: r for r in rows(out_t)}
        mac = "AA:BB:CC:00:00:01"
        for side in (m, t):
            self.assertEqual((side[mac]["Device"], side[mac]["Notes"]),
                             ("Garage Cam", "batteries 11/11"))                 # the Pi's edit
            self.assertEqual((side[mac]["IP"], side[mac]["Status"]),
                             ("10.0.0.8", "offline"))                            # the Mac's scan
        self.assertIn("AA:BB:CC:00:00:03", t)

    def test_push_files_only_in_scanner_mode(self):
        st = '{"QNAP NAS": {"up": true}}'
        m, t, _ = sync.merge_file("monitor_state.json", "", st, '{"old": 1}', scanner=True)
        self.assertEqual(t, st)
        m, t, _ = sync.merge_file("monitor_state.json", "", st, '{"old": 1}', scanner=False)
        self.assertEqual(t, '{"old": 1}')                  # each copy keeps its own

    def test_scanner_sync_end_to_end(self):
        with tempfile.TemporaryDirectory() as mac, tempfile.TemporaryDirectory() as pi:
            with open(os.path.join(mac, "monitor_state.json"), "w") as f:
                f.write('{"Lock": {"battery": {"pct": 10}}}')
            with open(os.path.join(mac, "events.csv"), "w") as f:
                f.write("Timestamp;Event\n2026-11-11T10:00:00;join\n")
            sync.sync(mac, sync.LocalTarget(pi), log=lambda *_: None, scanner=True)
            with open(os.path.join(pi, "monitor_state.json")) as f:
                self.assertIn('"pct": 10', f.read())
            self.assertTrue(os.path.exists(os.path.join(pi, "events.csv")))

    def test_ssh_key_passed_to_ssh(self):
        t = sync.target("jacobc@192.0.2.4:/opt/homehub/netinventory", key="~/.ssh/k")
        self.assertEqual(t.path, "/opt/homehub/netinventory")
        with mock.patch.object(sync.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="{}", stderr="")
            t.export()
        argv = run.call_args[0][0]
        self.assertIn("-i", argv)
        self.assertEqual(argv[argv.index("-i") + 1], os.path.expanduser("~/.ssh/k"))
        self.assertLess(argv.index("-i"), argv.index("--"))     # options before the host
        self.assertIn("cd /opt/homehub/netinventory/engine", argv[-1])


class TestBatteryLog(unittest.TestCase):
    H = ["Id", "Date", "Device", "Notes", "Updated", "Deleted"]

    def test_newer_updated_wins_and_tombstones_travel(self):
        mine = csvtext(self.H, ("a1", "2026-01-01", "Lock", "old", "2026-01-01T10:00:00", ""),
                       ("b2", "2026-01-02", "Cam", "", "2026-01-02T10:00:00", ""))
        theirs = csvtext(self.H, ("a1", "2026-01-01", "Lock", "fixed", "2026-01-03T10:00:00", ""),
                         ("b2", "2026-01-02", "Cam", "", "2026-01-04T10:00:00", "yes"),
                         ("c3", "2026-01-05", "Valve", "", "2026-01-05T10:00:00", ""))
        out, out2, _ = sync.merge_file("battery_log.csv", "", mine, theirs)
        self.assertEqual(out, out2)
        got = {r["Id"]: r for r in rows(out)}
        self.assertEqual(got["a1"]["Notes"], "fixed")
        self.assertEqual(got["b2"]["Deleted"], "yes")
        self.assertIn("c3", got)


class TestEndToEnd(unittest.TestCase):
    def test_two_way_sync_between_directories(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            H = ["Id", "Date", "Device", "Event", "Notes", "Updated", "Deleted"]
            with open(os.path.join(a, "battery_log.csv"), "w") as f:
                f.write(csvtext(H, ("a1", "2026-01-01", "Lock", "replaced", "", "2026-01-01T1", "")))
            with open(os.path.join(b, "battery_log.csv"), "w") as f:
                f.write(csvtext(H, ("b1", "2026-01-02", "Cam", "replaced", "", "2026-01-02T1", "")))
            MH = ["Device", "Enabled"]
            with open(os.path.join(b, "monitors.csv"), "w") as f:
                f.write(csvtext(MH, ("NAS", "yes")))
            changed = sync.sync(a, sync.LocalTarget(b), log=lambda *_: None)
            self.assertIn("battery_log.csv", changed)
            for d in (a, b):
                with open(os.path.join(d, "battery_log.csv")) as f:
                    self.assertEqual(sorted(r["Id"] for r in rows(f.read())), ["a1", "b1"])
            self.assertTrue(os.path.exists(os.path.join(a, "monitors.csv")))
            # second run: nothing to do
            self.assertEqual(sync.sync(a, sync.LocalTarget(b), log=lambda *_: None), {})
            # an edit on b, then a delete on a, both carry over
            time.sleep(0.01)
            with open(os.path.join(b, "monitors.csv"), "w") as f:
                f.write(csvtext(MH, ("NAS", "no"), ("Lock", "yes")))
            sync.sync(a, sync.LocalTarget(b), log=lambda *_: None)
            with open(os.path.join(a, "monitors.csv")) as f:
                self.assertEqual({r["Device"]: r["Enabled"] for r in rows(f.read())},
                                 {"NAS": "no", "Lock": "yes"})

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with open(os.path.join(b, "monitors.csv"), "w") as f:
                f.write(csvtext(["Device", "Enabled"], ("NAS", "yes")))
            changed = sync.sync(a, sync.LocalTarget(b), dry_run=True, log=lambda *_: None)
            self.assertIn("monitors.csv", changed)
            self.assertFalse(os.path.exists(os.path.join(a, "monitors.csv")))
            self.assertFalse(os.path.exists(os.path.join(a, ".sync")))

    def test_import_refuses_unknown_files(self):
        with tempfile.TemporaryDirectory() as a:
            with self.assertRaises(ValueError):
                sync.import_dir(a, {"../../.ssh/authorized_keys": "x"})

    def test_target_parsing(self):
        with tempfile.TemporaryDirectory() as a:
            self.assertIsInstance(sync.target(a), sync.LocalTarget)
        t = sync.target("pi@192.0.2.10:apps/netinventory")
        self.assertEqual((t.host, t.path), ("pi@192.0.2.10", "apps/netinventory"))
        self.assertEqual(sync.target("pi@192.0.2.10").path, "netinventory")
        for bad in ("-oProxyCommand=evil", "-oProxyCommandX", "host;rm", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                sync.target(bad)                                # never an ssh option or shell


if __name__ == "__main__":
    unittest.main()
