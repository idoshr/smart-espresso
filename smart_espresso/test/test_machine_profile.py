"""Machine profiles: migration, unset tanks, and a deferred model switch."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from smart_espresso.json_store import atomic_write_json
from smart_espresso.machine_profile import (
    CATALOG,
    CATALOG_ORDER,
    GAGGIA_CLASSIC_E24_SOURCE,
    LEGACY_DEFAULT_TANK_ML,
    RANCILIO_SILVIA_SOURCE,
    UNSET_LABEL,
    MachineProfile,
    MachineProfileStore,
    ProfileError,
    require_source_url,
)
from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import ShotHistory
from smart_espresso.test.test_status_server import FakeClock
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer

DASHBOARD = {"X-Espresso-Dashboard": "1"}


class TestCatalog(unittest.TestCase):
    def test_cited_models_have_a_maker_url_and_a_capacity(self):
        gaggia = CATALOG["gaggia-classic-e24"]
        silvia = CATALOG["rancilio-silvia"]
        self.assertEqual(gaggia.tank_capacity_ml, 2100.0)
        self.assertEqual(gaggia.source_url, GAGGIA_CLASSIC_E24_SOURCE)
        self.assertTrue(gaggia.to_dict()["capacity_confirmed"])
        self.assertEqual(silvia.tank_capacity_ml, 2000.0)
        self.assertEqual(silvia.source_url, RANCILIO_SILVIA_SOURCE)
        self.assertTrue(silvia.to_dict()["capacity_confirmed"])

    def test_unconfirmed_builtin_has_no_capacity_figure(self):
        generic = CATALOG["generic"]
        self.assertIsNone(generic.tank_capacity_ml)
        self.assertIsNone(generic.source_url)
        self.assertTrue(generic.unconfirmed)
        self.assertFalse(generic.to_dict()["capacity_confirmed"])
        self.assertIsNone(CATALOG["custom"].tank_capacity_ml)

    def test_a_capacity_without_a_maker_url_is_rejected(self):
        invented = replace(
            CATALOG["generic"], tank_capacity_ml=2000.0, unconfirmed=True
        )
        with self.assertRaises(ProfileError) as raised:
            require_source_url(invented)
        self.assertIn("cannot carry a capacity", str(raised.exception))

    def test_theme_is_an_accent_id_only(self):
        for profile in CATALOG_ORDER:
            theme = profile.to_dict()["theme"]
            self.assertEqual(set(theme), {"accent"})
            accent = theme["accent"]
            self.assertNotIn("#", accent)
            self.assertNotIn("/", accent)
            self.assertFalse(accent.startswith("http"))
            self.assertNotIn(".", accent)


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_path = os.path.join(self.tmp.name, "machine_profile.json")
        self.tank_path = os.path.join(self.tmp.name, "water_tank.json")

    def store(self, environ=None, tank_path=None):
        return MachineProfileStore(
            state_path=self.profile_path,
            read_env=True,
            environ={} if environ is None else environ,
            tank_state_path=self.tank_path if tank_path is None else tank_path,
        )

    def write_tank(self, payload):
        atomic_write_json(self.tank_path, payload)

    def test_fresh_install_is_machine_not_set_and_skips_shots_left(self):
        missing = os.path.join(self.tmp.name, "absent.json")
        store = self.store(tank_path=missing)
        state = store.public()
        self.assertFalse(state["ok"])
        self.assertEqual(state["status"], "machine_not_set")
        self.assertEqual(state["label"], UNSET_LABEL)
        self.assertIsNone(state["tank_capacity_ml"])
        self.assertIsNone(state["id"])
        self.assertFalse(state["prompt_pick_model"])

        tank = WaterTank(state_path=None)
        tank.update(0.0)
        tank.update(80.0)
        store.apply(
            MachineStateClassifier(shot_history=ShotHistory(state_path=None)), tank
        )
        self.assertEqual(tank.used_ml, 80.0)
        self.assertIsNone(tank.capacity_ml)
        self.assertIsNone(tank.shots_left)
        snapshot = tank.snapshot()
        self.assertIsNone(snapshot["shots_left"])
        self.assertIsNone(snapshot["capacity_ml"])
        self.assertEqual(snapshot["status"], "machine_not_set")
        self.assertEqual(snapshot["used_ml"], 80.0)

    def test_existing_tank_migrates_onto_custom_and_keeps_shots_left(self):
        self.write_tank({"used_ml": 400, "refills": 2, "capacity_ml": 1600})
        store = self.store()
        state = store.public()
        self.assertTrue(state["prompt_pick_model"])
        self.assertEqual(state["id"], "custom")
        self.assertEqual(state["tank_capacity_ml"], 1600.0)
        self.assertEqual(state["tank_capacity_source"], "custom")
        self.assertEqual(state["status"], "ready")

        tank = WaterTank(capacity_ml=None, state_path=self.tank_path)
        store.apply(
            MachineStateClassifier(shot_history=ShotHistory(state_path=None)), tank
        )
        self.assertEqual(tank.used_ml, 400.0)
        self.assertEqual(tank.capacity_ml, 1600.0)
        self.assertEqual(tank.shots_left, int(1200 // WaterTank.SHOT_ML))
        self.assertEqual(tank.snapshot()["status"], "ok")

    def test_water_tank_ml_migrates_when_no_profile_is_saved(self):
        store = self.store(environ={"WATER_TANK_ML": "1750"}, tank_path=self.tank_path)
        self.assertEqual(store.active().tank_capacity_ml, 1750.0)
        self.assertTrue(store.public()["prompt_pick_model"])
        self.assertTrue(store.public()["ok"])

    def test_a_tank_file_without_capacity_uses_the_historical_default(self):
        self.write_tank({"used_ml": 100, "refills": 0})
        store = self.store()
        self.assertEqual(store.active().tank_capacity_ml, LEGACY_DEFAULT_TANK_ML)
        self.assertEqual(store.active().id, "custom")
        self.assertTrue(store.public()["prompt_pick_model"])

    def test_migration_is_idempotent_and_a_dismissed_prompt_stays_dismissed(self):
        self.write_tank({"used_ml": 400, "refills": 1, "capacity_ml": 1600})
        first = self.store()
        self.assertTrue(first.public()["prompt_pick_model"])
        again = self.store(environ={"WATER_TANK_ML": "1600"})
        self.assertEqual(again.active().tank_capacity_ml, 1600.0)
        self.assertTrue(again.public()["prompt_pick_model"])

        again.dismiss_prompt()
        again.select("custom", {"tank_capacity_ml": 1900})
        reloaded = self.store(environ={"WATER_TANK_ML": "1600"})
        self.assertEqual(reloaded.active().tank_capacity_ml, 1900.0)
        self.assertFalse(reloaded.public()["prompt_pick_model"])
        self.assertEqual(reloaded.public()["id"], "custom")

        # Dismissing a second time does not bring the prompt back.
        reloaded.dismiss_prompt()
        persisted = json.loads(open(self.profile_path, encoding="utf-8").read())
        self.assertFalse(persisted["prompt_pick_model"])
        third = self.store()
        self.assertFalse(third.public()["prompt_pick_model"])
        self.assertEqual(third.active().tank_capacity_ml, 1900.0)

    def test_unknown_id_is_machine_not_set_without_a_borrowed_capacity(self):
        store = self.store(environ={"MACHINE_PROFILE": "no-such-machine"})
        state = store.public()
        self.assertEqual(state["status"], "machine_not_set")
        self.assertEqual(state["label"], UNSET_LABEL)
        self.assertEqual(state["error"], "unknown_profile")
        self.assertEqual(state["requested_id"], "no-such-machine")
        self.assertIsNone(state["tank_capacity_ml"])
        self.assertIsNone(state["fallback_id"])
        self.assertFalse(state["prompt_pick_model"])


class TestCustomValues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "machine_profile.json")

    def store(self):
        return MachineProfileStore(state_path=self.path, read_env=False)

    def test_custom_capacity_is_validated_and_persisted(self):
        store = self.store()
        for bad in (0, -5, float("nan"), 100, 9000, True, "nope"):
            with self.subTest(bad=bad):
                with self.assertRaises(ProfileError):
                    store.select("custom", {"tank_capacity_ml": bad})
                self.assertIsNone(store.active())

        saved = store.select(
            "custom",
            {
                "tank_capacity_ml": 1800,
                "pressure": {"boiler_ready_min_bar": 1.0, "boiler_ready_max_bar": 1.4},
                "accent": "copper",
            },
        )
        self.assertEqual(saved["tank_capacity_ml"], 1800.0)
        self.assertEqual(saved["pressure"]["boiler_ready_min_bar"], 1.0)
        self.assertEqual(saved["theme"], {"accent": "copper"})
        self.assertFalse(saved["capacity_confirmed"])

        reloaded = self.store()
        self.assertEqual(reloaded.active().tank_capacity_ml, 1800.0)
        self.assertEqual(reloaded.active().pressure.boiler_ready_min_bar, 1.0)
        self.assertEqual(reloaded.active().accent, "copper")

        with self.assertRaises(ProfileError):
            store.select("custom", {"pressure": {"head_max_bar": 6}})
        self.assertEqual(store.active().pressure.head_max_bar, 12.0)
        self.assertEqual(store.active().tank_capacity_ml, 1800.0)

    def test_user_capacity_on_an_unconfirmed_model_is_not_a_spec_figure(self):
        store = self.store()
        state = store.select("generic", {"tank_capacity_ml": 1400})
        self.assertEqual(state["id"], "generic")
        self.assertEqual(state["tank_capacity_ml"], 1400.0)
        self.assertFalse(state["capacity_confirmed"])
        self.assertIsNone(state["source_url"])
        self.assertEqual(state["tank_capacity_source"], "user")
        self.assertIsNone(CATALOG["generic"].tank_capacity_ml)

        with self.assertRaises(ProfileError):
            store.select("gaggia-classic-e24", {"tank_capacity_ml": 1400})
        self.assertEqual(store.active().id, "generic")

    def test_switching_recounts_shots_left_and_keeps_used_water(self):
        store = self.store()
        store.select("gaggia-classic-e24")
        tank = WaterTank(capacity_ml=2100.0, state_path=None)
        tank.update(0.0)
        tank.update(1000.0)
        classifier = MachineStateClassifier(shot_history=ShotHistory(state_path=None))
        store.select("rancilio-silvia")
        store.apply(classifier, tank)
        self.assertEqual(tank.used_ml, 1000.0)
        self.assertEqual(tank.capacity_ml, 2000.0)
        self.assertEqual(tank.shots_left, int(1000 // WaterTank.SHOT_ML))
        self.assertNotEqual(tank.shots_left, int(1100 // WaterTank.SHOT_ML))


class TestDeferredSwitch(unittest.TestCase):
    def test_a_switch_during_a_shot_waits_until_the_shot_is_saved(self):
        clock = FakeClock()
        patcher = mock.patch("smart_espresso.machine_state.monotonic", clock)
        patcher.start()
        self.addCleanup(patcher.stop)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MachineProfileStore(
            state_path=os.path.join(tmp.name, "profile.json"), read_env=False
        )
        store.select("gaggia-classic-e24")
        history = ShotHistory(state_path=None, now=lambda: 1_700_000_000.0)
        tank = WaterTank(capacity_ml=2100.0, state_path=None)
        tank.update(0.0)
        tank.update(1000.0)
        server = StatusServer(
            tank=tank,
            shot_history=history,
            profiles=store,
        )
        classifier = server.classifier

        def step(head, flow, total):
            clock.advance(1.0)
            return classifier.update(head, 1.2, flow_mls=flow, total_ml=total)

        for _ in range(4):
            step(0.0, 0.0, 0.0)
        total = 0.0
        for _ in range(6):
            total += 1.4
            step(9.0, 1.4, total)
        self.assertTrue(history.current_payload()["active"])
        self.assertTrue(classifier.shot_is_open)

        client = server.app.test_client()
        response = client.post(
            "/api/machine-profile",
            json={"id": "rancilio-silvia"},
            headers=DASHBOARD,
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["pending"]["id"], "rancilio-silvia")
        self.assertEqual(body["pending"]["status"], "pending")
        self.assertEqual(body["pending"]["reason"], "shot_in_progress")
        self.assertEqual(body["id"], "gaggia-classic-e24")
        self.assertEqual(tank.capacity_ml, 2100.0)
        self.assertEqual(tank.used_ml, 1000.0)
        self.assertEqual(tank.shots_left, int(1100 // WaterTank.SHOT_ML))

        for _ in range(4):
            step(0.0, 0.0, total)
        self.assertEqual(history.list_payload()["count"], 1)
        self.assertFalse(history.current_payload()["active"])
        self.assertEqual(tank.used_ml, 1000.0)
        self.assertEqual(tank.capacity_ml, 2000.0)
        self.assertEqual(tank.shots_left, int(1000 // WaterTank.SHOT_ML))
        live = client.get("/api/machine-profile").get_json()
        self.assertEqual(live["id"], "rancilio-silvia")
        self.assertIsNone(live["pending"])
        self.assertEqual(live["status"], "ready")
        status = client.get("/api/status").get_json()
        self.assertEqual(status["machine_profile"]["id"], "rancilio-silvia")
        self.assertEqual(status["tank"]["shots_left"], tank.shots_left)

    def test_api_rejects_a_bad_switch_and_requires_the_dashboard_header(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MachineProfileStore(
            state_path=os.path.join(tmp.name, "profile.json"), read_env=False
        )
        store.select("custom", {"tank_capacity_ml": 1500})
        server = StatusServer(
            tank=WaterTank(capacity_ml=1500, state_path=None),
            shot_history=ShotHistory(state_path=None),
            profiles=store,
        )
        client = server.app.test_client()
        missing = client.post("/api/machine-profile", json={"id": "generic"})
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(store.active().id, "custom")

        unknown = client.post(
            "/api/machine-profile",
            json={"id": "nope"},
            headers=DASHBOARD,
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.get_json()["error"], "unknown_profile")
        self.assertEqual(store.active().id, "custom")
        self.assertEqual(server.tank.capacity_ml, 1500.0)

        listed = client.get("/api/machine-profiles").get_json()
        ids = [item["id"] for item in listed["profiles"]]
        self.assertIn("gaggia-classic-e24", ids)
        generic = next(item for item in listed["profiles"] if item["id"] == "generic")
        self.assertIsNone(generic["tank_capacity_ml"])

        prompt = client.post(
            "/api/machine-profile/prompt/dismiss",
            headers=DASHBOARD,
        )
        self.assertEqual(prompt.status_code, 200)
        self.assertFalse(prompt.get_json()["prompt_pick_model"])
        blocked = client.post("/api/machine-profile/prompt/dismiss")
        self.assertEqual(blocked.status_code, 403)


class TestProfileShape(unittest.TestCase):
    def test_handmade_profile_matches_the_dataclass_used_by_the_catalog(self):
        self.assertIsInstance(CATALOG["custom"], MachineProfile)

    def test_pressure_and_url_rules_reject_bad_values(self):
        store = MachineProfileStore(state_path=None)
        cases = (
            {"pressure": "high"},
            {"pressure": {"nope": 1}},
            {"pressure": {"head_max_bar": 20}},
            {"pressure": {"boiler_max_bar": 0.2}},
            {"pressure": {"head_band_low_bar": 11, "head_band_high_bar": 10}},
            {"pressure": {"boiler_ready_min_bar": 1.5, "boiler_ready_max_bar": 1.2}},
            {"pressure": {"pump_head_bar": 1}},
            {"pressure": {"shot_window_low_s": 40, "shot_window_high_s": 20}},
            {"pressure": {"head_max_bar": float("nan")}},
            {"accent": "Not A Token"},
            {"accent": "#ff00aa"},
            {"logo": "gaggia.png"},
        )
        for updates in cases:
            with self.subTest(updates=updates):
                with self.assertRaises(ProfileError):
                    store.select("custom", updates)
        with self.assertRaises(ProfileError):
            store.select(12)
        with self.assertRaises(ProfileError):
            store.select("custom", ["1800"])
        with self.assertRaises(ProfileError):
            store.select("generic", {"accent": "copper"})
        store.select("custom", {"tank_capacity_ml": " 1800 "})
        self.assertEqual(store.active().tank_capacity_ml, 1800.0)
        with self.assertRaises(ProfileError):
            store.select("custom", {"tank_capacity_ml": "nan"})
        with self.assertRaises(ProfileError):
            store.select("custom", {"tank_capacity_ml": ""})

        bare = replace(CATALOG["gaggia-classic-e24"], source_url="not a url")
        with self.assertRaises(ProfileError):
            require_source_url(bare)
        empty = replace(CATALOG["gaggia-classic-e24"], tank_capacity_ml=None)
        with self.assertRaises(ProfileError):
            require_source_url(empty)
        self.assertIsNone(require_source_url(CATALOG["custom"]))

        class Bare:
            pass

        bare_classifier = Bare()
        store.apply(bare_classifier, None)
        self.assertEqual(bare_classifier.ready_min_bar, 0.8)

    def test_corrupt_files_pending_switches_and_explicit_env_profiles(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile_path = os.path.join(tmp.name, "profile.json")
        tank_path = os.path.join(tmp.name, "tank.json")

        with open(profile_path, "w", encoding="utf-8") as handle:
            handle.write("{")
        broken = MachineProfileStore(
            state_path=profile_path,
            read_env=True,
            environ={},
            tank_state_path=tank_path,
        )
        self.assertEqual(broken.public()["error"], "profile_state_unreadable")
        self.assertEqual(broken.public()["status"], "machine_not_set")

        atomic_write_json(
            profile_path,
            {
                "machine_profile": None,
                "pending": {"id": "gaggia-classic-e24"},
                "prompt_pick_model": False,
            },
        )
        promoted = MachineProfileStore(state_path=profile_path, read_env=False)
        self.assertEqual(promoted.active().id, "gaggia-classic-e24")
        self.assertIsNone(promoted.public()["pending"])

        atomic_write_json(
            profile_path,
            {
                "machine_profile": "generic",
                "pending": {
                    "id": "custom",
                    "tank_capacity_ml": 1550,
                    "accent": "copper",
                },
                "user_capacities": {"generic": 1400},
                "prompt_pick_model": False,
            },
        )
        custom_pending = MachineProfileStore(state_path=profile_path, read_env=False)
        self.assertEqual(custom_pending.active().id, "custom")
        self.assertEqual(custom_pending.active().tank_capacity_ml, 1550.0)
        self.assertEqual(custom_pending.active().accent, "copper")

        atomic_write_json(
            profile_path,
            {"machine_profile": "generic", "pending": {"tank_capacity_ml": 1400}},
        )
        ignored = MachineProfileStore(state_path=profile_path, read_env=False)
        self.assertEqual(ignored.active().id, "generic")
        self.assertIsNone(ignored.public()["pending"])

        bad_payloads = (
            [],
            {"extra": 1},
            {},
            {"machine_profile": "  "},
            {"machine_profile": "custom", "custom": []},
            {
                "machine_profile": "custom",
                "custom": {"logo": "x", "tank_capacity_ml": 1},
            },
            {"machine_profile": "custom", "prompt_pick_model": "yes"},
            {"machine_profile": "generic", "user_capacities": []},
            {
                "machine_profile": "generic",
                "user_capacities": {"gaggia-classic-e24": 1},
            },
            {"machine_profile": "generic", "pending": "soon"},
            {"machine_profile": "generic", "migrated_from_tank": "yes"},
            {"machine_profile": None, "custom": {"tank_capacity_ml": 50}},
            {"machine_profile": "custom", "custom": {"accent": "Nope"}},
            {"machine_profile": "generic", "user_capacities": {"generic": 10}},
        )
        for index, payload in enumerate(bad_payloads):
            path = os.path.join(tmp.name, f"bad-{index}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            store = MachineProfileStore(state_path=path, read_env=False)
            self.assertEqual(
                store.public()["error"], "profile_state_unreadable", payload
            )

        atomic_write_json(tank_path, {"capacity_ml": 50, "used_ml": 10})
        warned = MachineProfileStore(
            state_path=os.path.join(tmp.name, "other.json"),
            read_env=True,
            environ={"WATER_TANK_ML": "nope", "MACHINE_PROFILE": "   "},
            tank_state_path=tank_path,
        )
        self.assertEqual(warned.public()["status"], "machine_not_set")

        chosen = MachineProfileStore(
            state_path=os.path.join(tmp.name, "chosen.json"),
            read_env=True,
            environ={"MACHINE_PROFILE": "generic", "WATER_TANK_ML": "1500"},
            tank_state_path=os.path.join(tmp.name, "missing-tank.json"),
        )
        self.assertEqual(chosen.active().id, "generic")
        self.assertEqual(chosen.active().tank_capacity_ml, 1500.0)
        self.assertFalse(chosen.public()["prompt_pick_model"])
        self.assertEqual(chosen.public()["status"], "ready")
        listed = chosen.list_profiles()
        generic = next(item for item in listed if item["id"] == "generic")
        self.assertEqual(generic["tank_capacity_ml"], 1500.0)
        self.assertEqual(generic["tank_capacity_source"], "user")

        named = MachineProfileStore(
            state_path=os.path.join(tmp.name, "named.json"),
            read_env=True,
            environ={"MACHINE_PROFILE": "custom", "WATER_TANK_ML": "1650"},
            tank_state_path=None,
        )
        self.assertEqual(named.active().id, "custom")
        self.assertEqual(named.active().tank_capacity_ml, 1650.0)
        self.assertFalse(named.public()["prompt_pick_model"])

        fixed = MachineProfileStore(
            state_path=os.path.join(tmp.name, "fixed.json"),
            read_env=True,
            environ={"MACHINE_PROFILE": "gaggia-classic-e24", "WATER_TANK_ML": "1500"},
            tank_state_path=tank_path,
        )
        self.assertEqual(fixed.active().tank_capacity_ml, 2100.0)

        memory = MachineProfileStore(state_path=None)
        self.assertFalse(memory.activate_pending())
        memory.select("generic", defer=True)
        self.assertEqual(memory.public()["status"], "pending")
        self.assertEqual(memory.public()["pending"]["id"], "generic")
        self.assertIsNone(memory.active())
        self.assertTrue(memory.activate_pending())
        self.assertEqual(memory.active().id, "generic")
        self.assertEqual(memory.public()["status"], "capacity_unconfirmed")
        tank = WaterTank(capacity_ml=2000, state_path=None)
        memory.apply(
            MachineStateClassifier(shot_history=ShotHistory(state_path=None)), tank
        )
        self.assertIsNone(tank.capacity_ml)
        self.assertEqual(tank.snapshot()["status"], "capacity_unconfirmed")
        self.assertIsNone(tank.shots_left)

        atomic_write_json(tank_path, ["nope"])
        empty = MachineProfileStore(
            state_path=os.path.join(tmp.name, "empty-tank.json"),
            read_env=True,
            environ={},
            tank_state_path=tank_path,
        )
        self.assertEqual(empty.public()["status"], "machine_not_set")

        client_store = MachineProfileStore(
            state_path=os.path.join(tmp.name, "api.json"), read_env=False
        )
        server = StatusServer(
            profiles=client_store, shot_history=ShotHistory(state_path=None)
        )
        self.assertIsNone(server.tank.capacity_ml)
        client = server.app.test_client()
        self.assertEqual(
            client.post("/api/machine-profile", json=[], headers=DASHBOARD).status_code,
            400,
        )
        self.assertEqual(
            client.post("/api/machine-profile", json={}, headers=DASHBOARD).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                "/api/machine-profile",
                json={"id": "custom", "logo": "x.png"},
                headers=DASHBOARD,
            ).status_code,
            400,
        )
