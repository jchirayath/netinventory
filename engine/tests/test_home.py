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


if __name__ == "__main__":
    unittest.main()
