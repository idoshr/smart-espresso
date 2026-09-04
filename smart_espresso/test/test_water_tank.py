import json
import os
import tempfile
import unittest

from smart_espresso.water_tank import WaterTank


class TestWaterTank(unittest.TestCase):
    def tank(self, **kwargs):
        kwargs.setdefault("state_path", None)  # no disk writes by default
        return WaterTank(**kwargs)

    def test_counts_water_drawn_since_the_baseline(self):
        tank = self.tank()
        tank.update(0.0)
        tank.update(250.0)

        self.assertAlmostEqual(tank.used_ml, 250.0)
        self.assertAlmostEqual(tank.remaining_ml, 1750.0)
        self.assertAlmostEqual(tank.percent, 87.5)
        self.assertFalse(tank.is_low)

    def test_reset_marks_the_tank_as_refilled(self):
        tank = self.tank()
        tank.update(0.0)
        tank.update(1800.0)
        self.assertTrue(tank.is_low)

        tank.reset()
        self.assertEqual(tank.used_ml, 0.0)
        self.assertEqual(tank.refills, 1)
        self.assertFalse(tank.is_low)

        # Counting continues from the new baseline, not from zero.
        tank.update(1900.0)
        self.assertAlmostEqual(tank.used_ml, 100.0)

    def test_low_and_empty_thresholds(self):
        tank = self.tank(capacity_ml=2000.0, low_fraction=0.15)
        tank.update(0.0)

        tank.update(1699.0)
        self.assertFalse(tank.is_low)
        tank.update(1701.0)
        self.assertTrue(tank.is_low)
        self.assertFalse(tank.is_empty)

        tank.update(2100.0)
        self.assertTrue(tank.is_empty)
        self.assertEqual(tank.remaining_ml, 0.0)
        self.assertEqual(tank.percent, 0.0)
        self.assertEqual(tank.shots_left, 0)

    def test_sensor_counter_restart_does_not_lose_the_count(self):
        tank = self.tank()
        tank.update(0.0)
        tank.update(400.0)
        # Flow sensor was reset: its lifetime total goes backwards.
        tank.update(5.0)

        self.assertAlmostEqual(tank.used_ml, 405.0)

    def test_shots_left_estimate(self):
        tank = self.tank()
        tank.update(0.0)
        tank.update(2000.0 - 4 * WaterTank.SHOT_ML)
        self.assertEqual(tank.shots_left, 4)

    def test_state_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "tank.json")
            tank = WaterTank(state_path=path, save_interval=0.0)
            tank.update(0.0)
            tank.update(600.0)

            with open(path) as file:
                self.assertAlmostEqual(json.load(file)["used_ml"], 600.0)

            # New process: the flow sensor starts counting from zero again.
            restored = WaterTank(state_path=path, save_interval=0.0)
            self.assertAlmostEqual(restored.used_ml, 600.0)
            restored.update(0.0)
            restored.update(50.0)
            self.assertAlmostEqual(restored.used_ml, 650.0)

    def test_unreadable_state_file_starts_fresh(self):
        # Both a file that is not JSON at all and one holding valid JSON of the
        # wrong shape must leave the tank at zero rather than crash on boot.
        for content in ("not json", "null", "[]", '{"used_ml": "many"}'):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as directory:
                    path = os.path.join(directory, "tank.json")
                    with open(path, "w") as file:
                        file.write(content)

                    tank = WaterTank(state_path=path)
                    self.assertEqual(tank.used_ml, 0.0)
                    self.assertEqual(tank.refills, 0)

    def test_unchanged_counter_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tank.json")
            tank = WaterTank(state_path=path, save_interval=0.0)
            tank.update(0.0)
            tank.update(120.0)

            written_at = os.stat(path).st_mtime_ns
            for _ in range(50):  # an idle machine: same total, every tick
                tank.update(120.0)

            self.assertEqual(os.stat(path).st_mtime_ns, written_at)
            self.assertEqual(len(os.listdir(directory)), 1)  # no leftover temp files

            tank.update(130.0)
            self.assertNotEqual(os.stat(path).st_mtime_ns, written_at)

    def test_concurrent_reset_and_updates_stay_consistent(self):
        import threading

        tank = self.tank()
        tank.update(0.0)
        stop = threading.Event()

        def feed():
            total = 0.0
            while not stop.is_set():
                total += 1.0
                tank.update(total)

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        try:
            for _ in range(200):
                tank.reset()
                snapshot = tank.snapshot()
                # A snapshot must always describe one moment: the remaining
                # volume and the percentage cannot disagree.
                self.assertAlmostEqual(
                    snapshot["remaining_ml"],
                    round(snapshot["capacity_ml"] - snapshot["used_ml"], 1),
                    places=1,
                )
        finally:
            stop.set()
            writer.join(timeout=2)

        self.assertEqual(tank.refills, 200)

    def test_snapshot_shape(self):
        tank = self.tank()
        tank.update(0.0)
        tank.update(500.0)
        snapshot = tank.snapshot()

        self.assertEqual(snapshot["capacity_ml"], 2000.0)
        self.assertEqual(snapshot["used_ml"], 500.0)
        self.assertEqual(snapshot["remaining_ml"], 1500.0)
        self.assertEqual(snapshot["percent"], 75.0)
        self.assertFalse(snapshot["low"])


if __name__ == "__main__":
    unittest.main()
