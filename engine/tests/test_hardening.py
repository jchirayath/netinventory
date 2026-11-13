"""Tests for the fixes from the engineering-standard review: atomic writes,
the hashed passcode, security headers, the audit trail, honest alerts and
battery detection, a blind scan, sync's local conflict check, one name key,
and the static snapshot. Synthetic data only."""
import argparse
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import battery  # noqa: E402
import home  # noqa: E402
import monitor  # noqa: E402
import netinv  # noqa: E402
import store  # noqa: E402
import sync  # noqa: E402
from test_battery import _ServerCase  # noqa: E402

T0 = dt.datetime(2026, 1, 1, 12, 0, 0)


def _text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _put(path, text, mode="w"):
    with open(path, mode, encoding="utf-8", newline="") as f:
        f.write(text)


class TestStore(unittest.TestCase):
    def test_write_is_whole_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "home.csv")
            store.write_rows(p, ["Device", "Room"], [{"Device": "TV", "Room": "Den"}])

            def boom():
                yield {"Device": "Lamp", "Room": "Hall"}
                raise RuntimeError("killed mid-write")
            with self.assertRaises(RuntimeError):
                store.write_rows(p, ["Device", "Room"], boom())
            self.assertEqual(store.read_rows(p, ["Device", "Room"]),
                             [{"Device": "TV", "Room": "Den"}])     # the old file, whole
            self.assertEqual(os.listdir(d), ["home.csv"])             # no temp file left

    def test_secret_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "token.json")
            store.write_text(p, "{}", mode=0o600)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


class TestPasscode(unittest.TestCase):
    def test_hash_verifies_and_hides_the_passcode(self):
        h = netinv.passcode_hash("correct horse")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertNotIn("correct", h)
        self.assertNotEqual(h, netinv.passcode_hash("correct horse"))   # salted
        self.assertTrue(netinv.passcode_ok("correct horse", h))
        self.assertFalse(netinv.passcode_ok("correct horsE", h))
        self.assertFalse(netinv.passcode_ok("x", "scrypt$zz$broken"))

    def test_plain_value_from_an_older_version_still_works(self):
        self.assertTrue(netinv.passcode_ok("old-plain-1", "old-plain-1"))
        self.assertFalse(netinv.passcode_ok("old-plain-2", "old-plain-1"))

    def test_upgrade_stores_a_hash(self):
        with mock.patch("connectors.kc_set") as kc_set:
            v = netinv._upgrade_passcode("plain-pass-1")
        self.assertTrue(netinv.passcode_ok("plain-pass-1", v))
        kc_set.assert_called_once_with("serve", "passcode", v)
        with mock.patch("connectors.kc_set") as kc_set:
            self.assertEqual(netinv._upgrade_passcode(v), v)        # already a hash
            self.assertEqual(netinv._upgrade_passcode(""), "")
        kc_set.assert_not_called()


class TestCredsOnly(unittest.TestCase):
    def test_export_only_named_providers(self):
        store_ = {("agent", "token"): "tok", ("serve", "passcode"): "pc",
                  ("tuya", "api_key"): "vendor-secret"}
        out = io.StringIO()
        with mock.patch("connectors.kc_get", lambda p, f: store_.get((p, f))), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(out, "isatty", return_value=False, create=True):
            netinv.cmd_creds(argparse.Namespace(action="export", only="agent,serve"))
        data = json.loads(out.getvalue())
        self.assertEqual(sorted(data["creds"]), ["agent", "serve"])
        self.assertEqual(data["tokens"], {})
        self.assertNotIn("vendor-secret", out.getvalue())


class TestServeHardening(_ServerCase):
    def test_security_headers(self):
        self.start()
        import urllib.request
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            html = r.read().decode()
        self.assertNotIn('onclick="rename(', html)    # no value inside an inline handler

    def test_audit_records_edits_and_refusals(self):
        self.start(passcode="hunter22", local=False)
        self.assertEqual(self.post("/api/battery-log", {"device": "Garage Lock"}, "hunter22")[0],
                         200)
        with mock.patch("time.sleep"):
            self.assertEqual(self.post("/api/battery-log", {"device": "Garage Lock"}, "nope")[0],
                             401)
        # the line is written just after the reply goes out: give it a moment
        import time
        for _ in range(100):
            rows = store.read_rows(netinv.AUDIT_LOG, ["Who", "Action", "Target", "Status"])
            if len(rows) >= 2:
                break
            time.sleep(0.02)
        self.assertEqual([(r["Action"], r["Target"], r["Status"]) for r in rows],
                         [("battery-log", "Garage Lock", "200"),
                          ("battery-log", "Garage Lock", "401")])
        self.assertTrue(all(r["Who"] for r in rows))

    def test_refusals_without_a_passcode_attempt_are_not_audited(self):
        self.start(passcode="hunter22", local=False)
        import urllib.error
        import urllib.request
        for _ in range(5):                 # wrong Content-Type: 415, no passcode needed
            req = urllib.request.Request(self.base + "/api/battery-log", data=b"{}",
                                         headers={"Content-Type": "text/plain"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=10).close()
        self.assertFalse(os.path.exists(netinv.AUDIT_LOG))

    def test_reads_are_not_audited(self):
        self.start()
        self.get("/api/batteries")
        self.assertFalse(os.path.exists(netinv.AUDIT_LOG))


class TestBatteryDetection(unittest.TestCase):
    def levels(self, *readings):
        log, hist, before = [], [], {}
        for r in readings:
            after = {"Flume": r}
            battery.record_levels(log, hist, before, after)
            before = after
        return log

    def test_label_wobble_is_not_a_replacement(self):
        m = {"pct": 50, "label": "medium", "provider": "flume"}
        h = {"pct": 100, "label": "high", "provider": "flume"}
        self.assertEqual(self.levels(m, h, m, h), [])

    def test_provider_switch_is_not_a_replacement(self):
        self.assertEqual(self.levels({"pct": 30, "label": "30", "provider": "ha"},
                                     {"pct": 100, "label": "high", "provider": "tuya"}), [])

    def test_real_rises_still_count(self):
        self.assertEqual(len(self.levels({"pct": 15, "label": "15", "provider": "ha"},
                                         {"pct": 95, "label": "95", "provider": "ha"})), 1)
        self.assertEqual(len(self.levels({"pct": 10, "label": "low", "provider": "blink"},
                                         {"pct": 100, "label": "ok", "provider": "blink"})), 1)


def _row(device, mac):
    r = {k: "" for k in netinv.FIELDS}
    r.update({"Device": device, "MAC": mac, "IP": "10.0.0.5", "Status": "online"})
    return r


class TestBlindScan(unittest.TestCase):
    def test_no_lan_view_judges_no_one(self):
        m = {k: "" for k in monitor.FIELDS}
        m.update({"Device": "NAS", "Enabled": "yes", "OfflineAfter": "1m"})
        master = {"AA:BB:CC:00:00:01": _row("NAS", "AA:BB:CC:00:00:01")}
        state = {}
        probes = []
        for minutes in (0, 5, 60):
            a = monitor.run([m], master, state, seen_online=set(), lan_checked=False,
                            now=T0 + dt.timedelta(minutes=minutes),
                            probe_fn=lambda *x: probes.append(x))
            self.assertEqual(a, [])
        self.assertEqual(probes, [])
        self.assertNotIn("up", state["NAS"])           # 'not checked', not 'offline'


class TestUndeliveredAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(netinv, "EVENTS_LOG", os.path.join(self.tmp.name, "events.csv"))
        p.start()
        self.addCleanup(p.stop)
        for name in ("log", "warn", "error"):
            q = mock.patch.object(netinv, name)
            q.start()
            self.addCleanup(q.stop)
        self.master = {"AA:BB:CC:00:00:01": _row("NAS", "AA:BB:CC:00:00:01")}

    def test_failed_email_is_kept_and_resent(self):
        alerts = [("NAS", "offline", "offline for 31m")]
        with mock.patch.object(netinv, "email_settings", return_value={"to": "x@example.com"}), \
                mock.patch.object(netinv, "send_email", return_value=(False, "SMTP: 535")):
            left = netinv.deliver_monitor_alerts(alerts, self.master, [], True, False)
        self.assertEqual([a[:3] for a in left], [list(alerts[0])])
        sent = []
        with mock.patch.object(netinv, "email_settings", return_value={"to": "x@example.com"}), \
                mock.patch.object(netinv, "send_email",
                                  side_effect=lambda s, b: sent.append(b) or (True, "sent")):
            left2 = netinv.deliver_monitor_alerts([], self.master, [], True, False, pending=left)
        self.assertEqual(left2, [])
        self.assertIn("NAS", sent[0])

    def test_not_configured_is_not_queued_forever(self):
        with mock.patch.object(netinv, "email_settings", return_value={}):
            self.assertEqual(netinv.deliver_monitor_alerts(
                [("NAS", "offline", "x")], self.master, [], True, False), [])


class TestSyncLocalConflict(unittest.TestCase):
    def test_edit_during_sync_is_not_overwritten(self):
        hdr = "Room;Floor;Aliases\r\n"
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _put(os.path.join(a, "rooms.csv"), hdr + "Kitchen;1;\r\n")
            _put(os.path.join(b, "rooms.csv"), hdr + "Kitchen;1;\r\nOffice;2;\r\n")

            class Racing(sync.LocalTarget):
                def export(self):
                    out = super().export()
                    # serve saves an edit here while the other copy answers
                    _put(os.path.join(a, "rooms.csv"), "Garage;0;\r\n", "a")
                    return out
            with self.assertRaises(sync.Conflict):
                sync.sync(a, Racing(b), log=lambda *_: None)
            self.assertIn("Garage", _text(os.path.join(a, "rooms.csv")))
            sync.sync(a, sync.LocalTarget(b), log=lambda *_: None)     # next try merges all
            text = _text(os.path.join(a, "rooms.csv"))
            self.assertTrue(all(r in text for r in ("Kitchen", "Office", "Garage")))


class TestSyncMissingFile(unittest.TestCase):
    def test_a_wiped_copy_gets_the_file_back_instead_of_deleting_it_here(self):
        hdr = "Room;Floor;Aliases\r\n"
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _put(os.path.join(a, "rooms.csv"), hdr + "Kitchen;1;\r\nOffice;2;\r\n")
            sync.sync(a, sync.LocalTarget(b), log=lambda *_: None)    # both have it now
            os.remove(os.path.join(b, "rooms.csv"))                    # the other copy is wiped
            sync.sync(a, sync.LocalTarget(b), log=lambda *_: None)
            for d in (a, b):
                text = _text(os.path.join(d, "rooms.csv"))
                self.assertIn("Kitchen", text)
                self.assertIn("Office", text)


class TestStoreKeepsMode(unittest.TestCase):
    def test_rewrite_keeps_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.csv")
            store.write_text(p, "a", mode=0o600)
            store.write_text(p, "b")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


class TestServeEdges(_ServerCase):
    def test_garbled_request_still_gets_an_answer(self):
        import socket
        self.start()
        port = self.srv.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"NONSENSE\r\n\r\n")
            self.assertIn(b"400", s.recv(200))

    def test_tokenless_agent_probes_are_not_audited(self):
        with mock.patch.object(netinv, "agent_token", return_value="tok-for-tests-0123456789"):
            self.start()
        import urllib.error
        import urllib.request
        for _ in range(3):
            req = urllib.request.Request(self.base + "/api/agent/sync", data=b"{}",
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=10).close()
        self.assertFalse(os.path.exists(netinv.AUDIT_LOG))


class TestNameKey(unittest.TestCase):
    def test_quote_style_and_spacing_are_the_same_name(self):
        self.assertEqual(home.name_key("Jacob’s  Mac"), home.name_key("jacob's mac"))
        self.assertIs(monitor.norm, home.name_key)


class TestSnapshot(unittest.TestCase):
    def test_every_page_says_snapshot_and_none_polls(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(netinv, "api_payload", return_value={}):
            netinv.export_site(d)
            for rel in ("index.html", "rooms/index.html", "batteries/index.html"):
                html = _text(os.path.join(d, rel))
                self.assertIn("snapshot taken", html, rel)
                self.assertNotIn("setInterval(load", html, rel)


if __name__ == "__main__":
    unittest.main()
