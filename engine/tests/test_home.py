"""Unit tests for the home registry (docs/home-standard.md). Run: python -m unittest -v
Fixtures are the committed *.example.csv files plus synthetic rows — no real data."""
import os
import sys
import unittest

ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ENGINE)
import home  # noqa: E402

ROOMS = home.load_rooms(os.path.join(ENGINE, "rooms.example.csv"))
HOME = home.load_home(os.path.join(ENGINE, "home.example.csv"))
WORDS = home.room_words(ROOMS)


def row(**kw):
    r = {k: "" for k in home.HOME_FIELDS}
    r.update(Room="Kitchen", Type="light", Name="Kitchen Ceiling Light")
    r.update(kw)
    return r


class TestExamples(unittest.TestCase):
    def test_examples_pass_the_standard(self):
        errors, warnings = home.check(ROOMS, HOME)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])


class TestNames(unittest.TestCase):
    def problems(self, name, room="Kitchen"):
        return home.name_problems(name, room, WORDS)

    def test_good_name(self):
        self.assertEqual(self.problems("Kitchen Ceiling Light"), [])

    def test_alias_counts_as_room(self):
        self.assertEqual(self.problems("Den Lamp", "Family Room"), [])

    def test_must_start_with_room(self):
        self.assertIn("must start with its room", " ".join(self.problems("Ceiling Light")))

    def test_brand_rejected(self):
        self.assertIn("brand", " ".join(self.problems("Kitchen Kasa Plug")))

    def test_brackets_and_case_and_length(self):
        self.assertTrue(self.problems("Kitchen Plug (HS103)"))
        self.assertTrue(self.problems("Kitchen ceiling light"))
        self.assertTrue(self.problems("Kitchen " + "Very Long Name " * 3))

    def test_reserved_word(self):
        self.assertIn("reserved", " ".join(self.problems("Kitchen All Lights")))

    def test_tv_acronym_is_title_case(self):
        self.assertEqual(self.problems("Living Room TV", "Living Room"), [])


class TestCheck(unittest.TestCase):
    def test_todo_and_unknown_room_and_type(self):
        errors, _ = home.check(ROOMS, [row(Room="TODO", Type="TODO"),
                                       row(Room="Attic", Type="toaster")])
        text = " ".join(errors)
        for needle in ("Room not set", "Type not set", "'Attic' is not in rooms.csv",
                       "'toaster' is not one of"):
            self.assertIn(needle, text)

    def test_duplicate_names_case_insensitive(self):
        errors, _ = home.check(ROOMS, [row(), row(Name="Kitchen Ceiling Light")])
        self.assertIn("duplicates home.csv:2", " ".join(errors))

    def test_infrastructure_not_exposed_to_voice(self):
        errors, _ = home.check(ROOMS, [row(Type="hub", Name="Kitchen Hub",
                                           Expose="ha, alexa")])
        self.assertIn("infrastructure", " ".join(errors))

    def test_unknown_app(self):
        errors, _ = home.check(ROOMS, [row(Expose="ha, siri")])
        self.assertIn("unknown app(s) siri", " ".join(errors))

    def test_floor_required(self):
        errors, _ = home.check(home.starter_rooms(), [])
        self.assertIn("has no Floor", " ".join(errors))

    def test_device_missing_from_devices_csv_warns(self):
        errors, warnings = home.check(ROOMS, [row(Device="Gone Plug")], {"Other"})
        self.assertEqual(errors, [])
        self.assertIn("not in devices.csv", " ".join(warnings))


class TestDraft(unittest.TestCase):
    MASTER = [
        {"Device": "Roku Express (Office)", "Vendor": "Roku", "Hostname": ""},
        {"Device": "Kitchen", "Vendor": "Tuya Smart", "Hostname": "wlan0"},
        {"Device": "Kasa Smart Switch (HS200) 2", "Vendor": "TP-Link", "Hostname": "HS200"},
        {"Device": "Kasa Smart Switch (HS200) 2", "Vendor": "TP-Link", "Hostname": "HS200"},
        {"Device": "Blink Camera 3", "Vendor": "Immedia", "Hostname": ""},
        {"Device": "SmartThings Hub", "Vendor": "Physical Graph", "Hostname": ""},
        {"Device": "Alex's MacBook", "Vendor": "Apple", "Hostname": ""},
        {"Device": "", "Vendor": "Unknown", "Hostname": ""},
    ]

    def drafted(self, existing=()):
        rooms = home.starter_rooms()
        return {r["Device"]: r for r in home.draft(self.MASTER, rooms, list(existing))}

    def test_room_from_brackets_and_bare_name(self):
        d = self.drafted()
        self.assertEqual(d["Roku Express (Office)"]["Room"], "Office")
        self.assertEqual(d["Kitchen"]["Room"], "Kitchen")
        self.assertEqual(d["Blink Camera 3"]["Room"], "TODO")

    def test_type_and_native(self):
        d = self.drafted()
        self.assertEqual(d["Roku Express (Office)"]["Type"], "media_player")
        self.assertEqual((d["Kitchen"]["Type"], d["Kitchen"]["Native"]), ("TODO", "smartlife"))
        self.assertEqual(d["Kasa Smart Switch (HS200) 2"]["Native"], "kasa")
        self.assertEqual(d["Blink Camera 3"]["Type"], "camera")

    def test_infrastructure_gets_no_voice_exposure(self):
        self.assertEqual(self.drafted()["SmartThings Hub"]["Expose"], "")

    def test_guess_type_edge_cases(self):
        g = lambda dev, vendor="": home.guess_type({"Device": dev, "Vendor": vendor})
        self.assertEqual(g("Blink Sync Module 2"), ("hub", "blink"))
        self.assertEqual(g("Gas sensor")[0], "sensor")
        self.assertEqual(g("Water Monitor", "Espressif Inc.")[1], "")   # chip maker, not app
        self.assertEqual(g("Amazon Echo 2"), ("speaker", "alexa"))
        self.assertEqual(home.default_expose("speaker"), "ha")

    def test_sorted_known_rooms_first(self):
        rooms = [r["Room"] for r in home.draft(self.MASTER, home.starter_rooms(), [])]
        self.assertEqual(rooms, sorted(rooms, key=lambda r: (r == "TODO", r)))

    def test_skips_computers_unnamed_duplicates_and_known(self):
        d = self.drafted(existing=[row(Device="Blink Camera 3")])
        self.assertNotIn("Alex's MacBook", d)
        self.assertNotIn("", d)
        self.assertNotIn("Blink Camera 3", d)
        self.assertEqual(len(d), 4)


class TestAppRooms(unittest.TestCase):
    def test_merge_adds_only_new_rooms_once(self):
        rooms = [dict(r) for r in ROOMS]
        before = [dict(r) for r in rooms]
        added = home.merge_rooms(rooms, ["Living Room", "den", "Bedroom ", "Closet",
                                         "Closet", "  Guest   Room "])
        self.assertEqual([r["Room"] for r in added], ["Bedroom", "Closet", "Guest Room"])
        self.assertTrue(all(r["Floor"] == "TODO" for r in added))
        self.assertEqual(rooms[:len(before)], before)      # existing rows untouched

    def test_draft_name_room_wins_then_app_room(self):
        master = [{"Device": "Kitchen", "Vendor": "Tuya Smart"},
                  {"Device": "Blinds", "Vendor": "Tuya Smart"},
                  {"Device": "Flood sensor", "Vendor": "Tuya Smart"}]
        device_rooms = {"Kitchen": "Living Room", "Blinds": "Den ",
                        "Flood sensor": "Attic"}
        d = {r["Device"]: r["Room"] for r in home.draft(master, ROOMS, [], device_rooms)}
        self.assertEqual(d["Kitchen"], "Kitchen")         # the name names a room
        self.assertEqual(d["Blinds"], "Family Room")      # app room, via alias
        self.assertEqual(d["Flood sensor"], "TODO")       # app room not in rooms.csv


class TestPushPlan(unittest.TestCase):
    def test_renames_only_linked_devices_whose_name_differs(self):
        app_rooms = [{"App": "tuya", "Device": "Kitchen Kettle", "Room": "", "Id": "a"},
                     {"App": "tuya", "Device": "Blinds", "Room": "", "Id": "b"},
                     {"App": "tuya", "Device": "Patio Light", "Room": "", "Id": "c"},
                     {"App": "tuya", "Device": "No Id", "Room": "", "Id": ""},
                     {"App": "tuya", "Device": "Gone", "Room": "", "Id": "z"}]
        home_rows = [row(Device="Blinds", Name="Family Room Blinds"),
                     row(Device="Patio Light", Name="")]
        app_names = {"a": "Kitchen", "b": "Blinds", "c": "Patio Light "}
        self.assertEqual(home.push_plan("tuya", app_rooms, home_rows, app_names),
                         [("a", "Kitchen", "Kitchen Kettle"),
                          ("b", "Blinds", "Family Room Blinds")])


class TestBoard(unittest.TestCase):
    MASTER = [
        {"Device": "Kitchen Plug", "Vendor": "TP-Link", "Status": "offline"},
        {"Device": "Kitchen Plug", "Vendor": "TP-Link", "Status": "online"},
        {"Device": "Blink Camera", "Vendor": "Immedia", "Status": "online"},
        {"Device": "Alex's MacBook", "Vendor": "Apple", "Status": "online"},
        {"Device": "", "Vendor": "Unknown", "Status": "online"},
    ]

    def test_every_named_device_appears_once(self):
        home_rows = [row(Device="Kitchen Plug", Room="kitchen"),
                     row(Device="", Name="Kitchen Ceiling Light")]
        b = home.board(self.MASTER, ROOMS, home_rows)
        d = {x["id"]: x for x in b["devices"]}
        self.assertEqual(sorted(d), ["Alex's MacBook", "Blink Camera", "Kitchen Plug",
                                     "name:Kitchen Ceiling Light"])
        self.assertEqual(d["Kitchen Plug"]["room"], "Kitchen")     # canonical case
        self.assertEqual(d["Kitchen Plug"]["status"], "online")    # any MAC online
        self.assertEqual(d["Blink Camera"]["room"], "")            # not in home.csv
        self.assertEqual(d["Blink Camera"]["type"], "camera")
        self.assertEqual(b["rooms"][0], "Living Room")

    def test_seen_as_prefers_a_human_discovered_name(self):
        master = [{"Device": "Amazon Echo 2", "Hostname": "amazon-7646e5494", "IP": "10.0.0.2"},
                  {"Device": "Amazon Echo 2", "Hostname": "Amazon Echo Show", "IP": "10.0.0.3"},
                  {"Device": "Amazon Echo 2", "Hostname": "aa:bb:cc:dd:ee:ff", "IP": ""}]
        d = home.board(master, ROOMS, [])["devices"][0]
        self.assertEqual((d["seen_as"], d["ip"]), ("Amazon Echo Show", "10.0.0.3"))

    def test_room_not_in_rooms_csv_is_unassigned(self):
        b = home.board([], ROOMS, [row(Device="Plug", Room="Attic")])
        self.assertEqual(b["devices"][0]["room"], "")

    def test_set_room_updates_or_adds(self):
        rows = [row(Device="Kitchen Plug")]
        home.set_room(rows, self.MASTER, "Kitchen Plug", "Garage")
        self.assertEqual(rows[0]["Room"], "Garage")
        home.set_room(rows, self.MASTER, "Kitchen Plug", "")
        self.assertEqual(rows[0]["Room"], "TODO")
        added = home.set_room(rows, self.MASTER, "Alex's MacBook", "Kitchen")
        self.assertEqual((added["Room"], len(rows)), ("Kitchen", 2))
        self.assertIsNone(home.set_room(rows, self.MASTER, "Nope", "Kitchen"))
        master = self.MASTER + [{"Device": "", "Vendor": "Unknown"}]
        for key in ("", "name:"):
            self.assertIsNone(home.set_room(rows, master, key, "Kitchen"))
        self.assertEqual(len(rows), 2)

    def test_rename_everywhere(self):
        master = [dict(r) for r in self.MASTER]
        rows = [row(Device="Kitchen Plug", Name="")]
        app = [{"App": "tuya", "Device": "Kitchen Plug", "Room": "Kitchen"}]
        old, new_id = home.rename(rows, master, app, "Kitchen Plug", "  Kitchen  Kettle ")
        self.assertEqual((old, new_id), ("Kitchen Plug", "Kitchen Kettle"))
        self.assertEqual([r["Device"] for r in master[:2]], ["Kitchen Kettle"] * 2)  # every MAC
        self.assertEqual((rows[0]["Device"], rows[0]["Name"]), ("Kitchen Kettle", "Kitchen Kettle"))
        self.assertEqual(app[0]["Device"], "Kitchen Kettle")

    def test_rename_adds_home_row_and_rejects_taken_names(self):
        master = [dict(r) for r in self.MASTER]
        rows = []
        home.rename(rows, master, [], "Blink Camera", "Driveway Camera")
        self.assertEqual((rows[0]["Device"], rows[0]["Type"]), ("Driveway Camera", "camera"))
        with self.assertRaisesRegex(ValueError, "already used"):
            home.rename(rows, master, [], "Kitchen Plug", "driveway camera")
        with self.assertRaisesRegex(ValueError, "unknown device"):
            home.rename(rows, master, [], "Nope", "Anything")
        home.rename(rows, master, [], "Driveway Camera", "Driveway camera")   # case-only change

    def test_rename_blank_key_touches_nothing(self):
        master = [dict(r) for r in self.MASTER]
        rows = [row(Device="", Name="Kitchen Ceiling Light")]
        for key in ("", "  ", "name:", "name: "):
            with self.assertRaisesRegex(ValueError, "unknown device"):
                home.rename(rows, master, [], key, "Oops")
        self.assertEqual([r["Device"] for r in master], [r["Device"] for r in self.MASTER])
        with self.assertRaisesRegex(ValueError, "60"):
            home.rename(rows, master, [], "Kitchen Plug", "x" * 61)

    def test_rename_device_with_no_network_presence(self):
        rows = [row(Device="", Name="Kitchen Ceiling Light")]
        _, new_id = home.rename(rows, [], [], "name:Kitchen Ceiling Light", "Kitchen Pendant")
        self.assertEqual((new_id, rows[0]["Name"]), ("name:Kitchen Pendant", "Kitchen Pendant"))

    def test_add_room_dedupes_by_alias(self):
        rooms = [dict(r) for r in ROOMS]
        self.assertEqual(home.add_room(rooms, "den"), "Family Room")
        self.assertEqual(home.add_room(rooms, " Attic  Room"), "Attic Room")
        self.assertEqual(rooms[-1], {"Room": "Attic Room", "Floor": "TODO", "Aliases": ""})


if __name__ == "__main__":
    unittest.main()
