import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import DEFAULT_STATE_PATH, ShotHistory
from smart_espresso.water_tank import WaterTank
from smart_espresso.web.status_server import StatusServer


class FakeClock:
    """Monotonic clock the test advances explicitly."""

    def __init__(self, start=500.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestShotHistory(unittest.TestCase):
    def history(self, **kwargs):
        kwargs.setdefault("state_path", None)
        kwargs.setdefault("now", lambda: 1_700_000_000.0)
        return ShotHistory(**kwargs)

    def pull_into(self, history, seconds=3.0, ml=20.0, head=9.0, flow=1.4, boiler=1.1):
        history.open()
        steps = int(seconds / 0.25) + 1
        for step in range(steps):
            history.sample(step * 0.25, head, boiler, flow)
        return history.commit(seconds, ml)

    def test_defaults(self):
        self.assertEqual(DEFAULT_STATE_PATH, "~/.smart_espresso/shot_history.json")
        self.assertEqual(ShotHistory.MAX_SHOTS, 200)

    def test_records_curves_and_peaks(self):
        history = self.history(sample_interval=0.5)
        history.open()
        history.sample(0.0, 2.0, 1.10, 0.2)
        history.sample(0.25, 12.0, 1.40, 3.0)  # between stored points
        history.sample(0.5, 9.0, 1.15, 1.4)
        history.sample(0.75, 8.5, None, 1.2)  # kept as the closing point
        shot = history.commit(2.5, 18.0)

        self.assertEqual(shot["id"], 1)
        self.assertEqual(shot["seconds"], 2.5)
        self.assertEqual(shot["ml"], 18.0)
        self.assertEqual(shot["peak_head_bar"], 12.0)
        self.assertEqual(shot["peak_boiler_bar"], 1.4)
        self.assertEqual(shot["peak_flow_mls"], 3.0)
        self.assertEqual([point["t"] for point in shot["samples"]], [0.0, 0.5, 0.75])
        self.assertIsNone(shot["samples"][-1]["boiler_bar"])
        started = datetime.fromisoformat(shot["started_at"])
        self.assertAlmostEqual(started.timestamp(), 1_700_000_000.0, places=0)

    def test_samples_before_open_are_ignored(self):
        history = self.history()
        history.sample(0.0, 9.0, 1.0, 1.0)
        self.assertIsNone(history.commit(10.0, 30.0))
        self.assertEqual(history.list_payload()["count"], 0)

    def test_abandon_does_not_keep_the_pull(self):
        history = self.history()
        history.open()
        history.sample(0.0, 9.0, 1.0, 1.0)
        history.abandon()
        self.assertEqual(history.list_payload()["shots"], [])
        # The next real pull still takes the first id.
        shot = self.pull_into(history)
        self.assertEqual(shot["id"], 1)

    def test_cap_keeps_the_newest_and_does_not_reuse_ids(self):
        history = self.history(max_shots=2)
        for index in range(4):
            self.pull_into(history, seconds=3.0 + index, ml=20.0 + index)

        payload = history.list_payload()
        self.assertEqual(payload["count"], 2)
        self.assertEqual([shot["id"] for shot in payload["shots"]], [4, 3])
        self.assertEqual([shot["seconds"] for shot in payload["shots"]], [6.0, 5.0])

        again = self.pull_into(history, seconds=9.0, ml=40.0)
        self.assertEqual(again["id"], 5)
        self.assertIsNone(history.get(3))
        self.assertEqual(history.get(5)["ml"], 40.0)

    def test_list_limit_is_newest_first_and_zero_is_empty(self):
        history = self.history()
        for _ in range(3):
            self.pull_into(history)

        self.assertEqual(
            [shot["id"] for shot in history.list_payload(limit=2)["shots"]], [3, 2]
        )
        self.assertEqual(history.list_payload(limit=0)["shots"], [])
        self.assertEqual(history.list_payload(limit=0)["count"], 3)
        summary = history.status_summary(limit=2)
        self.assertEqual(summary["count"], 3)
        self.assertEqual([shot["id"] for shot in summary["recent"]], [3, 2])
        self.assertNotIn("samples", summary["recent"][0])

    def test_non_finite_totals_drop_the_pull(self):
        history = self.history()
        history.open()
        history.sample(0.0, 9.0, 1.0, 1.0)
        self.assertIsNone(history.commit(float("nan"), 10.0))
        self.assertEqual(history.list_payload()["count"], 0)

    def test_curve_is_bounded_but_keeps_the_last_point_and_the_peak(self):
        history = self.history(sample_interval=1.0, max_samples=2)
        history.open()
        history.sample(0.0, 1.0, 1.0, 0.1)
        history.sample(1.0, 5.0, 1.0, 0.5)
        history.sample(2.0, 11.0, 1.0, 2.0)  # past the cap; peak still counts
        history.sample(3.0, 8.0, 1.0, 0.8)
        shot = history.commit(3.0, 12.0)

        self.assertEqual([point["t"] for point in shot["samples"]], [0.0, 1.0, 3.0])
        self.assertEqual(shot["peak_head_bar"], 11.0)

    def test_state_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "shots.json")
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_100.0)
            history.open()
            history.sample(0.0, 2.0, 1.1, 0.4)
            history.sample(1.0, 9.2, 1.15, 1.6)
            history.commit(26.0, 36.4)

            with open(path) as file:
                payload = json.load(file)
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["next_id"], 2)
            self.assertEqual(payload["shots"][0]["seconds"], 26.0)
            self.assertEqual(len(os.listdir(os.path.dirname(path))), 1)

            restored = ShotHistory(state_path=path)
            shot = restored.get(1)
            self.assertEqual(shot["ml"], 36.4)
            self.assertEqual(shot["samples"][1]["head_bar"], 9.2)
            self.assertEqual(shot["samples"][1]["flow_mls"], 1.6)
            self.assertEqual(shot["peak_head_bar"], 9.2)

            # Ids continue, so a shot saved before the reboot is not reused.
            restored.open()
            restored.sample(0.0, 8.0, 1.0, 1.0)
            self.assertEqual(restored.commit(4.0, 16.0)["id"], 2)

    def test_missing_file_starts_empty_and_does_not_create_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing.json")
            history = ShotHistory(state_path=path)
            self.assertEqual(history.list_payload()["shots"], [])
            self.assertFalse(os.path.exists(path))

    def test_unreadable_file_starts_empty(self):
        for content in (
            "not json",
            "null",
            "[]",
            "NaN",
            "Infinity",
            '{"shots": "nope"}',
            '{"shots": NaN}',
        ):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, "shots.json")
                    with open(path, "w") as file:
                        file.write(content)
                    history = ShotHistory(state_path=path)
                    self.assertEqual(history.list_payload()["count"], 0)
                    self.assertIsNone(history.get(1))

    def test_a_bad_shot_is_skipped_and_a_good_one_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            payload = {
                "version": 1,
                "next_id": 8,
                "shots": [
                    {
                        "id": 2,
                        "started_at": "2026-10-25T08:00:00+00:00",
                        "seconds": 27.0,
                        "ml": 36.0,
                        "samples": [
                            {
                                "t": 0,
                                "head_bar": 9.1,
                                "boiler_bar": 1.2,
                                "flow_mls": 1.4,
                            },
                            "nope",
                            {"t": float("nan"), "head_bar": 1},
                        ],
                    },
                    {"id": 3, "seconds": float("nan"), "ml": 10},
                    {"id": True, "started_at": "x", "seconds": 5, "ml": 5},
                    {"id": 2, "started_at": "duplicate", "seconds": 1, "ml": 1},
                ],
            }
            with open(path, "w") as file:
                json.dump(payload, file, allow_nan=True)

            history = ShotHistory(state_path=path, max_shots=10)
            self.assertEqual(
                [shot["id"] for shot in history.list_payload()["shots"]], [2]
            )
            shot = history.get(2)
            self.assertEqual(len(shot["samples"]), 1)
            self.assertEqual(shot["peak_head_bar"], 9.1)
            # next_id in the file is honoured, so the skipped id is not reused.
            history.open()
            history.sample(0.0, 8.0, None, 1.0)
            self.assertEqual(history.commit(4.0, 12.0)["id"], 8)

    def test_load_trims_to_the_configured_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            wide = ShotHistory(
                state_path=path, max_shots=5, now=lambda: 1_700_000_000.0
            )
            for _ in range(5):
                self.pull_into(wide)
            trimmed = ShotHistory(state_path=path, max_shots=2)
            self.assertEqual(
                [shot["id"] for shot in trimmed.list_payload()["shots"]], [5, 4]
            )

    def test_unchanged_log_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_000.0)
            self.pull_into(history)
            os.utime(path, (0, 0))

            history.open()
            history.sample(1.0, 9.0, 1.0, 1.0)
            history.abandon()
            history.list_payload()
            history.get(1)

            self.assertEqual(os.stat(path).st_mtime_ns, 0)
            self.assertEqual(os.listdir(directory), ["shots.json"])

    def test_in_progress_pull_is_not_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_000.0)
            history.open()
            history.sample(0.0, 9.0, 1.2, 1.4)
            history.sample(1.0, 9.1, 1.2, 1.5)
            self.assertFalse(os.path.exists(path))


class TestClassifierRecording(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("smart_espresso.machine_state.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.history = ShotHistory(state_path=None, now=lambda: 1_700_000_000.0)
        self.classifier = MachineStateClassifier(shot_history=self.history)

    def settle(self, head, boiler, flow=0.0, total_ml=0.0, seconds=3.0, step=0.5):
        for _ in range(int(seconds / step)):
            self.classifier.update(head, boiler, flow, total_ml)
            self.clock.advance(step)
        return self.classifier.update(head, boiler, flow, total_ml)

    def pull(self, seconds, rate_mls=1.4, head=9.0, boiler=1.2):
        self.settle(0.0, boiler)
        total = 0.0
        for _ in range(seconds):
            total += rate_mls
            self.clock.advance(1.0)
            self.classifier.update(head, boiler, flow_mls=rate_mls, total_ml=total)
        self.settle(0.0, boiler, total_ml=total)
        return total

    def test_a_qualifying_pull_records_time_volume_and_curves(self):
        self.pull(27)

        shot = self.history.get(1)
        self.assertIsNotNone(shot)
        self.assertAlmostEqual(shot["seconds"], 26.0, places=1)
        self.assertAlmostEqual(shot["ml"], 36.4, places=1)
        self.assertEqual(shot["peak_head_bar"], 9.0)
        self.assertEqual(shot["peak_boiler_bar"], 1.2)
        self.assertEqual(shot["peak_flow_mls"], 1.4)
        self.assertEqual(shot["samples"][0]["t"], 0.0)
        self.assertGreaterEqual(shot["samples"][-1]["t"], 26.0)
        self.assertTrue(all(point["head_bar"] == 9.0 for point in shot["samples"]))
        self.assertTrue(all(point["flow_mls"] == 1.4 for point in shot["samples"]))
        self.assertAlmostEqual(
            datetime.fromisoformat(shot["started_at"]).timestamp(),
            1_700_000_000.0,
            places=0,
        )

        # The duration strip the dashboard already draws stays a short
        # in-memory list of seconds and volume, newest last.
        snapshot = self.classifier.snapshot()
        self.assertEqual(snapshot["last_shot"], snapshot["history"][-1])
        self.assertEqual(set(snapshot["last_shot"]), {"seconds", "ml"})
        self.assertAlmostEqual(snapshot["last_shot"]["seconds"], 26.0, places=1)
        self.assertEqual(snapshot["today"]["shots"], 1)

    def test_short_blip_is_not_recorded(self):
        self.settle(0.0, 1.2)
        self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=1.0)
        self.clock.advance(0.5)
        self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=2.0)
        self.settle(0.0, 1.2, total_ml=2.0)

        self.assertIsNone(self.classifier.snapshot()["last_shot"])
        self.assertEqual(self.history.list_payload()["count"], 0)

    def test_pause_stays_in_the_curve_and_out_of_the_duration(self):
        self.settle(0.0, 1.2)
        total = 0.0
        for _ in range(4):
            total += 1.4
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=total)
        for _ in range(3):
            self.clock.advance(1.0)
            self.classifier.update(8.0, 1.2, flow_mls=0.0, total_ml=total)
        for _ in range(4):
            total += 1.4
            self.clock.advance(1.0)
            self.classifier.update(9.0, 1.2, flow_mls=1.4, total_ml=total)
        self.settle(0.0, 1.2, total_ml=total)

        shot = self.history.get(1)
        # Flowing samples only: the opening tick is the zero, then 3 + 4.
        self.assertAlmostEqual(shot["seconds"], 7.0, places=1)
        self.assertTrue(any(point["flow_mls"] == 0.0 for point in shot["samples"]))
        self.assertGreater(shot["samples"][-1]["t"], shot["seconds"])

    def test_no_boiler_leaves_the_boiler_curve_empty(self):
        self.settle(0.0, 1.2)
        total = 0.0
        for _ in range(5):
            total += 1.4
            self.clock.advance(1.0)
            self.classifier.update(9.0, None, flow_mls=1.4, total_ml=total)
        self.settle(0.0, 1.2, total_ml=total)

        shot = self.history.get(1)
        self.assertIsNone(shot["peak_boiler_bar"])
        self.assertTrue(all(point["boiler_bar"] is None for point in shot["samples"]))
        self.assertGreater(shot["ml"], 0)


class TestShotHistoryApi(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "shots.json")
        self.history = ShotHistory(state_path=self.path, now=lambda: 1_700_000_000.0)
        self.server = StatusServer(
            port=8098,
            tank=WaterTank(state_path=None),
            shot_history=self.history,
        )
        self.client = self.server.app.test_client()

    def record(self, seconds, ml, head=9.0, flow=1.5, boiler=1.2):
        self.history.open()
        self.history.sample(0.0, head, boiler, flow)
        self.history.sample(seconds, head, boiler, flow * 0.5)
        return self.history.commit(seconds, ml)

    def test_list_and_detail_round_trip(self):
        first = self.record(26.0, 36.4)
        second = self.record(24.0, 32.0, head=8.5)

        listing = self.client.get("/api/shots")
        self.assertEqual(listing.status_code, 200)
        body = listing.get_json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(
            [shot["id"] for shot in body["shots"]], [second["id"], first["id"]]
        )
        self.assertNotIn("samples", body["shots"][0])
        self.assertEqual(body["shots"][0]["seconds"], 24.0)
        self.assertEqual(body["shots"][0]["ml"], 32.0)

        detail = self.client.get(f"/api/shots/{second['id']}")
        self.assertEqual(detail.status_code, 200)
        shot = detail.get_json()
        self.assertEqual(shot["samples"][0]["head_bar"], 8.5)
        self.assertEqual(shot["samples"][0]["flow_mls"], 1.5)
        self.assertEqual(shot["samples"][0]["boiler_bar"], 1.2)
        self.assertIn("started_at", shot)

        limited = self.client.get("/api/shots?limit=1")
        self.assertEqual(
            [shot["id"] for shot in limited.get_json()["shots"]], [second["id"]]
        )
        self.assertEqual(limited.get_json()["count"], 2)

    def test_unknown_shot_and_bad_limit(self):
        missing = self.client.get("/api/shots/9")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"], "shot not found")
        self.assertEqual(self.client.get("/api/shots?limit=no").status_code, 400)
        self.assertEqual(self.client.get("/api/shots?limit=-1").status_code, 400)
        self.assertEqual(self.client.get("/api/shots/nope").status_code, 404)

    def test_status_carries_a_summary_without_curves(self):
        self.record(26.0, 36.4)
        payload = self.client.get("/api/status").get_json()
        recent = payload["shots"]["recent"]
        self.assertEqual(payload["shots"]["count"], 1)
        self.assertEqual(recent[0]["ml"], 36.4)
        self.assertNotIn("samples", recent[0])

    def test_history_is_served_after_a_restart_without_sampling(self):
        self.record(26.0, 36.4, head=9.1, flow=1.8)
        # A new process: the file is the only input. No sensor update runs.
        restored = ShotHistory(state_path=self.path)
        server = StatusServer(
            port=8097,
            tank=WaterTank(state_path=None),
            shot_history=restored,
        )
        client = server.app.test_client()
        listing = client.get("/api/shots").get_json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["shots"][0]["seconds"], 26.0)
        detail = client.get("/api/shots/1").get_json()
        self.assertEqual(detail["ml"], 36.4)
        self.assertEqual(detail["samples"][0]["head_bar"], 9.1)
        self.assertEqual(detail["samples"][0]["flow_mls"], 1.8)

    def test_corrupt_file_does_not_break_the_server(self):
        path = os.path.join(self.directory.name, "broken.json")
        with open(path, "w") as file:
            file.write("{not json")
        history = ShotHistory(state_path=path)
        server = StatusServer(
            port=8096,
            tank=WaterTank(state_path=None),
            shot_history=history,
        )
        body = server.app.test_client().get("/api/shots").get_json()
        self.assertEqual(body, {"count": 0, "shots": []})


if __name__ == "__main__":
    unittest.main()
