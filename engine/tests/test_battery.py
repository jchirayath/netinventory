"""Unit tests for battery tracking (battery.py), its API, and the LAN
passcode. Synthetic data only; the server binds to 127.0.0.1."""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import battery  # noqa: E402
import connectors  # noqa: E402
import netinv  # noqa: E402

TODAY = dt.date.today().isoformat()


class TestEntries(unittest.TestCase):
    def test_add_defaults_and_validation(self):
        rows = []
        e = battery.add_entry(rows, "  Front   Lock ", battery="2x  AA", notes="line1\nline2")
        self.assertEqual((e["Device"], e["Event"], e["Date"], e["Battery"], e["Notes"], e["Source"]),
                         ("Front Lock", "replaced", TODAY, "2x AA", "line1 line2", "manual"))
        self.assertEqual(len(e["Id"]), 12)
        for bad in ({"device": ""}, {"device": "x", "event": "exploded"},
                    {"device": "x", "date": "2026-13-40"}, {"device": "x", "level": "140"},
                    {"device": "x", "date": "2999-01-01"}):
            with self.assertRaises(ValueError, msg=bad):
                battery.add_entry([], bad.get("device"), bad.get("event", "replaced"),
                                  bad.get("date", ""), level=bad.get("level", ""))

    def test_edit_delete_tombstone(self):
        rows = []
        e = battery.add_entry(rows, "Lock", date="2026-01-02")
        self.assertIsNotNone(battery.edit_entry(rows, e["Id"], {"Notes": "fresh", "Source": "x"}))
        self.assertEqual((rows[0]["Notes"], rows[0]["Source"]), ("fresh", "manual"))
        self.assertTrue(battery.delete_entry(rows, e["Id"]))
        self.assertEqual(rows[0]["Deleted"], "yes")                # kept, so it syncs
        self.assertFalse(battery.delete_entry(rows, e["Id"]))
        self.assertIsNone(battery.edit_entry(rows, e["Id"], {"Notes": "x"}))

    def test_round_trip_hides_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "battery_log.csv")
            rows = []
            battery.add_entry(rows, "A", date="2026-01-01")
            gone = battery.add_entry(rows, "B", date="2026-01-02")
            battery.delete_entry(rows, gone["Id"])
            battery.save_log(p, rows)
            self.assertEqual([r["Device"] for r in battery.load_log(p)], ["A"])
            self.assertEqual(len(battery.load_log(p, include_deleted=True)), 2)


class TestLevels(unittest.TestCase):
    def test_history_on_change_and_auto_replaced(self):
        log, hist = [], []
        before = {"Lock": {"pct": 10, "provider": "tuya"}, "Cam": {"pct": 100}}
        after = {"Lock": {"pct": 100, "label": "high", "provider": "tuya"},
                 "Cam": {"pct": 100}, "New": {"pct": 80, "provider": "ha"}, "None": None}
        hist.append({"Time": "2026-02-01T00:00:00", "Device": "Cam", "Provider": "", "Level": "100",
                     "Pct": "100"})
        nh, nl = battery.record_levels(log, hist, before, after, now="2026-03-01T10:00:00")
        self.assertEqual(sorted(h["Device"] for h in nh), ["Lock", "New"])  # Cam unchanged
        self.assertEqual(len(nl), 1)
        self.assertEqual((nl[0]["Device"], nl[0]["Event"], nl[0]["Source"], nl[0]["Date"]),
                         ("Lock", "replaced", "auto", "2026-03-01"))
        self.assertIn("10% to 100%", nl[0]["Notes"])

    def test_first_reading_is_a_baseline_then_only_changes(self):
        hist = []
        battery.record_levels([], hist, {"Cam": {"pct": 100}}, {"Cam": {"pct": 100}})
        self.assertEqual([h["Pct"] for h in hist], ["100"])          # baseline
        battery.record_levels([], hist, {"Cam": {"pct": 100}}, {"Cam": {"pct": 100}})
        self.assertEqual(len(hist), 1)                               # unchanged: nothing

    def test_small_rise_or_high_start_is_not_a_replacement(self):
        log = []
        battery.record_levels(log, [], {"A": {"pct": 30}, "B": {"pct": 60}},
                              {"A": {"pct": 60}, "B": {"pct": 100}})
        self.assertEqual(log, [])


class TestSummary(unittest.TestCase):
    def test_summary_intervals_and_order(self):
        log = []
        battery.add_entry(log, "Lock", date="2026-01-01", battery="4x AA")
        battery.add_entry(log, "Lock", date="2026-04-01")
        battery.add_entry(log, "Lock", event="checked", date="2026-05-01")
        hist = [{"Time": "2026-05-01T00:00:00", "Device": "Lock", "Provider": "tuya",
                 "Level": "50", "Pct": "50"}]
        out = battery.summarize(["Cam", "Lock"], log, hist,
                                {"Lock": {"pct": 50, "provider": "tuya"}, "Cam": {"pct": 5}},
                                today="2026-05-11")
        self.assertEqual([s["device"] for s in out], ["Cam", "Lock"])   # worst first
        lock = out[1]
        self.assertEqual((lock["battery"], lock["last_changed"], lock["days_since"],
                          lock["avg_days"], lock["next_due"], lock["changes"], lock["entries"]),
                         ("4x AA", "2026-04-01", 40, 90, "2026-06-30", 2, 3))
        self.assertEqual(lock["trend"], [{"t": "2026-05-01T00:00:00", "pct": 50}])
        self.assertIsNone(out[0]["avg_days"])


class TestDetails(unittest.TestCase):
    """battery_details: telling two 'Garage door' / 'Front Door' devices apart."""
    HOME = [
        {"Device": "", "Room": "Garage", "Type": "lock", "Name": "Garage Door Lock",
         "Description": "Smart Life DY QSLOCK id eb0f664qmit1aowt", "Native": "smartlife",
         "Expose": "", "Via": "", "AppNames": "tuya=Garage door"},
        {"Device": "U-Bolt WiFi Smart Lock", "Room": "Garage", "Type": "lock",
         "Name": "Garage Lock", "Description": "Espressif U-Bolt WiFi Smart Lock", "Native": "",
         "Expose": "", "Via": "", "AppNames": ""},
        {"Device": "", "Room": "Entry", "Type": "sensor", "Name": "Front Door Contact",
         "Description": "SmartThings zigbee 'Front Door' eui 000D6F000E49AC0F",
         "Native": "smartthings", "Expose": "", "Via": "", "AppNames": "smartthings=Front Door"},
        {"Device": "Front Door Camera", "Room": "Entry", "Type": "camera",
         "Name": "Front Door Camera", "Description": "Blink catalina 'Front Door'", "Native": "blink",
         "Expose": "", "Via": "", "AppNames": ""},
    ]
    MASTER = {
        "AA:BB:CC:00:00:01": {"Device": "Front Door Camera", "MAC": "AA:BB:CC:00:00:01", "IP": "",
                              "Vendor": "Amazon", "Hostname": "Front Door", "Status": "offline",
                              "AppNames": "blink=Front Door"},
        "AA:BB:CC:00:00:02": {"Device": "U-Bolt WiFi Smart Lock", "MAC": "AA:BB:CC:00:00:02",
                              "IP": "", "Vendor": "Espressif", "Hostname": "U-Bolt-WiFi",
                              "Status": "online", "AppNames": ""},
    }

    def details(self, device, match="", provider=""):
        import home
        mon = {"Device": device, "Enabled": "yes", "OfflineAfter": "", "BatteryBelow": "20",
               "Check": "cloud" if match else "", "Match": match, "Notes": ""}
        level = {"pct": 50, "provider": provider} if provider else None
        return netinv.battery_details(device, mon, level, self.MASTER, self.HOME,
                                      {"Kitchen Sensor": "Kitchen"},
                                      [{"Room": "Kitchen", "Floor": "", "Aliases": ""}],
                                      home.default_apps())

    def test_tuya_lock_found_by_cloud_name(self):
        d = self.details("Garage door", "tuya=Garage door", "tuya")
        self.assertEqual((d["name"], d["room"], d["type"], d["app"], d["model"]),
                         ("Garage Door Lock", "Garage", "lock", "smartlife", "Smart Life DY QSLOCK"))

    def test_ubolt_lock_by_device_name_and_app_guessed(self):
        d = self.details("U-Bolt WiFi Smart Lock")
        self.assertEqual((d["name"], d["type"], d["app"]), ("Garage Lock", "lock", "utec"))
        self.assertEqual(d["app_label"], "U-tec (U-Bolt)")

    def test_ha_front_door_is_the_smartthings_contact_not_the_camera(self):
        # The camera's hostname is also 'Front Door'; the HA reading is the contact.
        d = self.details("Front Door", "ha=Front Door", "ha")
        self.assertEqual((d["name"], d["type"], d["app"]),
                         ("Front Door Contact", "sensor", "smartthings"))
        self.assertNotIn("eui", d["model"])

    def test_unknown_device_falls_back_to_app_room_and_guessed_type(self):
        d = self.details("Kitchen Sensor", "tuya=Kitchen Sensor", "tuya")
        self.assertEqual((d["name"], d["room"], d["app"]), ("Kitchen Sensor", "Kitchen",
                                                            "smartlife"))
        self.assertEqual(d["type"], "sensor")


class TestApiBatteries(unittest.TestCase):
    def test_locks_listed_without_a_reading_and_rows_carry_details(self):
        import home
        import monitor
        with tempfile.TemporaryDirectory() as d:
            paths = {k: os.path.join(d, v) for k, v in {
                "BATTERY_LOG": "battery_log.csv", "BATTERY_HISTORY": "battery_history.csv",
                "MONITORS_CSV": "monitors.csv", "MONITOR_STATE": "monitor_state.json",
                "HOME_CSV": "home.csv", "ROOMS_CSV": "rooms.csv", "APP_ROOMS_CSV": "app_rooms.csv",
                "APPS_CSV": "apps.csv", "MASTER": "devices.csv"}.items()}
            m = lambda dev, **kw: {**{k: "" for k in monitor.FIELDS}, "Device": dev,  # noqa: E731
                                   "Enabled": "yes", **kw}
            monitor.save(paths["MONITORS_CSV"], [m("U-Bolt WiFi Smart Lock", OfflineAfter="2h"),
                                                 m("QNAP NAS", OfflineAfter="30m"),
                                                 m("Garage door", BatteryBelow="20",
                                                   Match="tuya=Garage door")])
            home.write_csv(paths["HOME_CSV"], home.HOME_FIELDS, TestDetails.HOME)
            with mock.patch.multiple(netinv, **paths):
                out = netinv.api_batteries()["devices"]
        got = {s["device"]: s for s in out}
        self.assertEqual(set(got), {"U-Bolt WiFi Smart Lock", "Garage door"})  # not the NAS
        self.assertIsNone(got["U-Bolt WiFi Smart Lock"]["pct"])
        for s in out:
            for k in ("name", "room", "type", "app", "app_label", "model"):
                self.assertIn(k, s)
        self.assertEqual(got["Garage door"]["name"], "Garage Door Lock")


class TestCredentialFile(unittest.TestCase):
    def test_file_store_without_keychain(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "credentials.json")
            with mock.patch.dict(os.environ, {"NETINV_CREDENTIALS": p}), \
                    mock.patch.object(connectors, "_keychain", return_value=False):
                self.assertIsNone(connectors.kc_get("serve", "passcode"))
                connectors.kc_set("serve", "passcode", "s3cret!")
                self.assertEqual(connectors.kc_get("serve", "passcode"), "s3cret!")
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(os.path.dirname(p)).st_mode & 0o777, 0o700)
                connectors.kc_delete("serve", "passcode")
                self.assertIsNone(connectors.kc_get("serve", "passcode"))


class TestCredsCopy(unittest.TestCase):
    def test_export_import_round_trip_and_path_safety(self):
        import io
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            def env(d):
                return [mock.patch.dict(os.environ, {"NETINV_CREDENTIALS": os.path.join(d, "c.json"),
                                                     "HOME": d}),
                        mock.patch.object(connectors, "_keychain", return_value=False)]
            for p in env(src):
                p.start()
            connectors.kc_set("tuya", "access_id", "id1")
            connectors.kc_set("serve", "passcode", "hunter22")
            with open(os.path.join(src, ".netinv_blink.json"), "w") as f:
                f.write('{"token": "t"}')
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                netinv.cmd_creds(argparse.Namespace(action="export"))
            mock.patch.stopall()
            data = json.loads(out.getvalue())
            data["tokens"]["../../evil"] = "x"                     # must be ignored
            data["creds"]["not-a-provider"] = {"a": "b"}           # must be ignored
            for p in env(dst):
                p.start()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(data))), \
                    mock.patch("builtins.print"):
                netinv.cmd_creds(argparse.Namespace(action="import"))
            self.assertEqual(connectors.kc_get("tuya", "access_id"), "id1")
            self.assertEqual(connectors.kc_get("serve", "passcode"), "hunter22")
            self.assertIsNone(connectors.kc_get("not-a-provider", "a"))
            tok = os.path.join(dst, ".netinv_blink.json")
            self.assertEqual(os.stat(tok).st_mode & 0o777, 0o600)
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(dst), "evil")))
            mock.patch.stopall()

    def test_export_refuses_a_terminal(self):
        with mock.patch.object(sys.stdout, "isatty", return_value=True), \
                self.assertRaises(SystemExit):
            netinv.cmd_creds(argparse.Namespace(action="export"))


class TestServer(unittest.TestCase):
    """A real server on a spare port, with the data files in a temp dir."""

    def start(self, allow_write=True, passcode="", local=True):
        self.tmp = tempfile.TemporaryDirectory()
        paths = {"BATTERY_LOG": "battery_log.csv", "BATTERY_HISTORY": "battery_history.csv",
                 "MONITORS_CSV": "monitors.csv", "MONITOR_STATE": "monitor_state.json"}
        self.patches = [mock.patch.object(netinv, k, os.path.join(self.tmp.name, v))
                        for k, v in paths.items()]
        self.patches.append(mock.patch.object(netinv, "serve_passcode", return_value=passcode))
        if local is not None:                      # None: the real loopback/proxy check
            self.patches.append(mock.patch.object(netinv, "_local_client", return_value=local))
        for p in self.patches:
            p.start()
        args = argparse.Namespace(host="127.0.0.1", port=0, allow_write=allow_write, lan=False)
        with mock.patch("builtins.print"):
            self.srv = netinv.make_server(args)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        if hasattr(self, "srv"):
            self.srv.shutdown()
            self.srv.server_close()
        for p in getattr(self, "patches", []):
            p.stop()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def post(self, path, body, passcode=None):
        h = {"Content-Type": "application/json"}
        if passcode is not None:
            h["X-Netinv-Passcode"] = passcode
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.read().decode()

    def test_log_edit_delete_round_trip(self):
        self.start()
        code, j = self.post("/api/battery-log", {"device": "Garage Lock", "battery": "4x AA",
                                                 "notes": "Energizer"})
        self.assertEqual(code, 200)
        eid = j["entry"]["Id"]
        data = json.loads(self.get("/api/batteries"))
        self.assertEqual([s["device"] for s in data["devices"]], ["Garage Lock"])
        self.assertEqual(data["devices"][0]["battery"], "4x AA")
        self.assertEqual(self.post("/api/battery-edit", {"id": eid, "notes": "Duracell"})[0], 200)
        one = json.loads(self.get("/api/battery?device=Garage%20Lock"))
        self.assertEqual(one["log"][0]["Notes"], "Duracell")
        self.assertEqual(self.post("/api/battery-delete", {"id": eid})[0], 200)
        self.assertEqual(json.loads(self.get("/api/batteries"))["log"], [])
        self.assertEqual(self.post("/api/battery-delete", {"id": eid})[0], 404)
        self.assertEqual(self.post("/api/battery-log", {"device": ""})[0], 400)

    def test_page_has_auth_helper_and_no_placeholders(self):
        self.start()
        for page in ("/", "/rooms", "/batteries"):
            html = self.get(page)
            self.assertIn("function apiPost", html, page)
            self.assertNotIn("__AUTHJS__", html, page)
            self.assertNotIn("__WRITABLE__", html, page)
            # relative links and reads, so a proxy prefix (/netinv/) keeps working
            self.assertNotIn('href="/', html, page)
            self.assertNotIn("fetch('/", html, page)

    def test_no_cors_header_so_other_sites_cannot_read(self):
        self.start()
        with urllib.request.urlopen(self.base + "/api/batteries", timeout=10) as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")

    def test_read_only_server_refuses(self):
        self.start(allow_write=False)
        self.assertEqual(self.post("/api/battery-log", {"device": "x"})[0], 403)

    def test_passcode_from_another_device(self):
        self.start(passcode="hunter22", local=False)
        with mock.patch("time.sleep"):
            code, j = self.post("/api/battery-log", {"device": "x"})
            self.assertEqual((code, j.get("auth")), (401, True))
            self.assertEqual(self.post("/api/battery-log", {"device": "x"}, "wrong")[0], 401)
        self.assertEqual(self.post("/api/battery-log", {"device": "x"}, "hunter22")[0], 200)

    def test_wrong_passcodes_lock_out(self):
        self.start(passcode="hunter22", local=False)
        with mock.patch("time.sleep"):
            for _ in range(netinv.PasscodeGuard.MAX):
                self.post("/api/battery-log", {"device": "x"}, "guess")
            # locked now, even with the right passcode
            self.assertEqual(self.post("/api/battery-log", {"device": "x"}, "hunter22")[0], 429)

    def test_lockout_is_per_client(self):
        g = netinv.PasscodeGuard()
        with mock.patch("time.sleep"):
            for _ in range(g.MAX):
                g.fail("10.0.0.66")
        self.assertTrue(g.locked("10.0.0.66"))
        self.assertFalse(g.locked("10.0.0.7"))             # everyone else can still edit

    def test_cross_site_form_post_is_not_a_miss(self):
        # A text/plain form post from another site must be refused before the
        # passcode check, so it can't use up anyone's attempts.
        self.start(passcode="hunter22", local=False)
        with mock.patch.object(netinv.PasscodeGuard, "fail") as fail:
            req = urllib.request.Request(self.base + "/api/battery-log", data=b"device=x",
                                         headers={"Content-Type": "text/plain"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 415)
            cm.exception.close()
            fail.assert_not_called()

    def test_dns_rebinding_host_refused(self):
        self.start(passcode="hunter22", local=True)
        port = self.srv.server_address[1]
        for path, data in (("/api/batteries", None), ("/api/battery-log", b'{"device":"x"}')):
            req = urllib.request.Request(self.base + path, data=data, headers={
                "Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}",
                "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 421, path)
            cm.exception.close()

    def test_host_check(self):
        h = netinv.HostCheck("0.0.0.0")
        self.assertTrue(h.ok("127.0.0.1:8787"))
        self.assertTrue(h.ok("localhost"))
        self.assertTrue(h.ok("[::1]:8787"))
        self.assertTrue(h.ok("127.0.0.1:9999"))              # a proxy's port is fine
        self.assertFalse(h.ok("evil.example"))
        self.assertFalse(h.ok("evil.example:8787"))
        self.assertFalse(h.ok(""))

    def test_rejection_reads_the_body_first(self):
        # A large body refused with 401 must still be read, so the client
        # gets the 401 instead of a connection reset mid-upload.
        import socket
        self.start(passcode="hunter22", local=False)
        body = json.dumps({"device": "x", "notes": "y" * 200_000}).encode()
        with mock.patch("time.sleep"), socket.create_connection(
                ("127.0.0.1", self.srv.server_address[1]), timeout=10) as s:
            s.sendall(b"POST /api/battery-log HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                      b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                      % (self.srv.server_address[1], len(body)) + body)
            self.assertIn(b" 401 ", s.recv(200))

    def test_oversized_body_refused(self):
        self.start()
        req = urllib.request.Request(self.base + "/api/battery-log", data=b"x",
                                     headers={"Content-Type": "application/json",
                                              "Content-Length": "5000000"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
        except (urllib.error.URLError, ConnectionError):
            code = 413          # some stacks see the close before the reply
        self.assertEqual(code, 413)

    def _post_via_proxy(self, body, passcode=None, xff="192.168.1.50"):
        h = {"Content-Type": "application/json", "X-Forwarded-For": xff}
        if passcode is not None:
            h["X-Netinv-Passcode"] = passcode
        req = urllib.request.Request(self.base + "/api/battery-log", data=json.dumps(body).encode(),
                                     headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_proxied_request_is_not_local(self):
        # Caddy on the Pi connects from loopback for every LAN client.
        self.start(passcode="hunter22", local=None)
        self.assertEqual(self.post("/api/battery-log", {"device": "x"})[0], 200)  # truly local
        with mock.patch("time.sleep"):
            self.assertEqual(self._post_via_proxy({"device": "x"}), 401)
        self.assertEqual(self._post_via_proxy({"device": "x"}, "hunter22"), 200)

    def test_proxied_write_without_passcode_configured_refused(self):
        self.start(passcode="", local=None)
        self.assertEqual(self._post_via_proxy({"device": "x"}), 403)
        self.assertEqual(self.post("/api/battery-log", {"device": "x"})[0], 200)  # this machine

    def test_proxy_lockout_keys_on_forwarded_client(self):
        self.start(passcode="hunter22", local=None)
        with mock.patch("time.sleep"):
            for _ in range(netinv.PasscodeGuard.MAX):
                self._post_via_proxy({"device": "x"}, "guess", xff="192.168.1.66")
        self.assertEqual(self._post_via_proxy({"device": "x"}, "hunter22", xff="192.168.1.66"), 429)
        self.assertEqual(self._post_via_proxy({"device": "x"}, "hunter22", xff="192.168.1.7"), 200)

    def test_client_key(self):
        self.assertEqual(netinv._client_key("127.0.0.1", {"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}),
                         "10.0.0.9")                          # the proxy's own entry
        self.assertEqual(netinv._client_key("192.168.1.5", {"X-Forwarded-For": "1.2.3.4"}),
                         "192.168.1.5")                       # a direct client can't forge it
        self.assertEqual(netinv._client_key("127.0.0.1", {}), "127.0.0.1")

    def test_this_mac_needs_no_passcode(self):
        self.start(passcode="hunter22", local=True)
        self.assertEqual(self.post("/api/battery-log", {"device": "x"})[0], 200)

    def test_lan_writes_without_passcode_refused(self):
        args = argparse.Namespace(host="127.0.0.1", port=0, allow_write=True, lan=True)
        with mock.patch.object(netinv, "serve_passcode", return_value=""), \
                self.assertRaises(SystemExit):
            netinv.make_server(args)


if __name__ == "__main__":
    unittest.main()
