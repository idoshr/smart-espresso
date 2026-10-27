"""Corrupt shot and tank files are kept, and a shot commit does not stall."""
import json
import math
import os
import tempfile
import time
import unittest
from unittest import mock

from smart_espresso.machine_state import MachineStateClassifier
from smart_espresso.shot_history import ShotHistory
from smart_espresso.water_tank import WaterTank


class FakeClock:
    def __init__(self, start=500.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _finish(history):
    """Stop a writer when this build has one. Older builds have no thread."""
    flush = getattr(history, "flush", None)
    if flush is not None:
        flush()
    close = getattr(history, "close", None)
    if close is not None:
        close()


def _corrupt_names(directory, filename):
    prefix = filename + ".corrupt-"
    return sorted(name for name in os.listdir(directory) if name.startswith(prefix))


class TestCorruptShotHistory(unittest.TestCase):
    def test_truncated_garbage_and_empty_files_fall_back_to_the_backup(self):
        cases = {
            "truncated": lambda good: good[:40],
            "garbage": lambda good: b"{not json",
            "empty": lambda good: b"",
        }
        for name, spoil in cases.items():
            with self.subTest(name=name):
                self._recovers(spoil)

    def _recovers(self, spoil):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_100.0)
            for ml in (10.0, 20.0, 30.0):
                history.open()
                history.sample(0.0, 9.0, 1.2, 1.4)
                history.commit(3.0, ml)
            _finish(history)

            with open(path, "rb") as handle:
                good = handle.read()
            damaged = spoil(good)
            with open(path, "wb") as handle:
                handle.write(damaged)

            restored = ShotHistory(state_path=path)
            try:
                self.assertEqual(
                    [shot["ml"] for shot in restored.list_payload()["shots"]],
                    [20.0, 10.0],
                )
                kept = _corrupt_names(directory, "shots.json")
                self.assertEqual(len(kept), 1)
                with open(os.path.join(directory, kept[0]), "rb") as handle:
                    self.assertEqual(handle.read(), damaged)

                restored.open()
                restored.sample(0.0, 9.0, 1.2, 1.4)
                restored.commit(3.0, 15.0)
                with open(os.path.join(directory, kept[0]), "rb") as handle:
                    self.assertEqual(handle.read(), damaged)
            finally:
                _finish(restored)

    def test_null_and_a_bare_list_are_quarantined(self):
        for body in ("null\n", "[]\n"):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, "shots.json")
                    with open(path, "w") as handle:
                        handle.write(body)
                    history = ShotHistory(state_path=path)
                    try:
                        self.assertEqual(history.list_payload()["count"], 0)
                        self.assertFalse(os.path.exists(path))
                        self.assertEqual(
                            len(_corrupt_names(directory, "shots.json")), 1
                        )
                    finally:
                        _finish(history)

    def test_a_valid_empty_log_is_not_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            with open(path, "w") as handle:
                handle.write('{"version":1,"next_id":1,"shots":[]}\n')
            history = ShotHistory(state_path=path)
            try:
                self.assertEqual(history.list_payload()["count"], 0)
                self.assertTrue(os.path.exists(path))
                self.assertEqual(_corrupt_names(directory, "shots.json"), [])
            finally:
                _finish(history)

    def test_replace_failure_leaves_a_loadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_100.0)
            history.open()
            history.sample(0.0, 9.0, 1.2, 1.4)
            history.commit(3.0, 20.0)
            _finish(history)
            with open(path, "rb") as handle:
                good = handle.read()

            again = ShotHistory(state_path=path, now=lambda: 1_700_000_200.0)
            again.open()
            again.sample(0.0, 8.0, 1.2, 1.4)
            with mock.patch(
                "smart_espresso.json_store.os.replace",
                side_effect=OSError("killed mid-write"),
            ):
                again.commit(3.0, 18.0)
            _finish(again)

            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), good)
            json.loads(good)
            temps = [name for name in os.listdir(directory) if name.startswith(".tmp-")]
            self.assertEqual(temps, [])

            restored = ShotHistory(state_path=path)
            try:
                self.assertEqual(restored.list_payload()["count"], 1)
                self.assertEqual(restored.list_payload()["shots"][0]["ml"], 20.0)
            finally:
                _finish(restored)

    def test_a_save_fsyncs_the_file_and_the_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_100.0)
            calls = []
            real_fsync = os.fsync

            def spy(fd):
                calls.append(fd)
                return real_fsync(fd)

            history.open()
            history.sample(0.0, 9.0, 1.2, 1.4)
            with mock.patch("smart_espresso.json_store.os.fsync", spy):
                history.commit(3.0, 20.0)
            _finish(history)
            self.assertGreaterEqual(len(calls), 2)


class TestCorruptWaterTank(unittest.TestCase):
    def test_truncated_garbage_and_empty_files_fall_back_to_the_backup(self):
        cases = {
            "truncated": lambda good: good[:12],
            "garbage": lambda good: b"not-json",
            "empty": lambda good: b"",
        }
        for name, spoil in cases.items():
            with self.subTest(name=name):
                self._recovers(spoil)

    def _recovers(self, spoil):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tank.json")
            tank = WaterTank(state_path=path, save_interval=0.0)
            tank.update(0.0)
            tank.update(40.0)
            tank.update(80.0)
            with open(path, "rb") as handle:
                good = handle.read()
            damaged = spoil(good)
            with open(path, "wb") as handle:
                handle.write(damaged)

            restored = WaterTank(state_path=path, save_interval=0.0)
            self.assertEqual(restored.used_ml, 40.0)
            kept = _corrupt_names(directory, "tank.json")
            self.assertEqual(len(kept), 1)
            corrupt_path = os.path.join(directory, kept[0])
            with open(corrupt_path, "rb") as handle:
                self.assertEqual(handle.read(), damaged)

            restored.update(90.0)
            with open(corrupt_path, "rb") as handle:
                self.assertEqual(handle.read(), damaged)

    def test_replace_failure_leaves_a_loadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tank.json")
            tank = WaterTank(state_path=path, save_interval=0.0)
            tank.update(0.0)
            tank.update(50.0)
            with open(path, "rb") as handle:
                good = handle.read()

            with mock.patch(
                "smart_espresso.json_store.os.replace",
                side_effect=OSError("killed mid-write"),
            ):
                tank.update(80.0)

            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), good)
            reloaded = WaterTank(state_path=path)
            self.assertEqual(reloaded.used_ml, 50.0)
            temps = [name for name in os.listdir(directory) if name.startswith(".tmp-")]
            self.assertEqual(temps, [])


class TestShotCommitLatency(unittest.TestCase):
    def test_committing_update_p99_stays_under_20ms_with_200_shots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shots.json")
            shots = []
            for shot_id in range(1, 201):
                samples = [
                    {
                        "t": round(index * 0.25, 2),
                        "head_bar": 9.0,
                        "boiler_bar": 1.2,
                        "flow_mls": 1.4,
                    }
                    for index in range(120)
                ]
                shots.append(
                    {
                        "id": shot_id,
                        "started_at": "2026-10-26T12:00:00+00:00",
                        "seconds": 30.0,
                        "ml": 40.0,
                        "peak_head_bar": 9.0,
                        "peak_boiler_bar": 1.2,
                        "peak_flow_mls": 1.4,
                        "samples": samples,
                    }
                )
            with open(path, "w") as handle:
                json.dump(
                    {"version": 1, "next_id": 201, "shots": shots},
                    handle,
                    separators=(",", ":"),
                )

            clock = FakeClock()
            history = ShotHistory(state_path=path, now=lambda: 1_700_000_300.0)
            try:
                with mock.patch("smart_espresso.machine_state.monotonic", clock):
                    classifier = MachineStateClassifier(
                        shot_history=history, dwell_seconds=0.0
                    )
                    classifier.update(0.0, 1.2, 0.0, 0.0)
                    total = 0.0
                    times = []
                    for _ in range(100):
                        for _step in range(3):
                            total += 2.0
                            clock.advance(1.0)
                            classifier.update(9.0, 1.2, 2.0, total)
                        clock.advance(1.0)
                        started = time.perf_counter()
                        classifier.update(0.0, 1.2, 0.0, total)
                        times.append(time.perf_counter() - started)
                times.sort()
                p99 = times[math.ceil(0.99 * len(times)) - 1]
                self.assertLess(p99, 0.020)
            finally:
                _finish(history)

            restored = ShotHistory(state_path=path)
            try:
                payload = restored.list_payload()
                self.assertEqual(payload["count"], 200)
                self.assertEqual(payload["shots"][0]["ml"], 4.0)
            finally:
                _finish(restored)


if __name__ == "__main__":
    unittest.main()
