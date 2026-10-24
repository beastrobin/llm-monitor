"""The energy books behind the 能耗面板 tab (v1.10).

Watts only become honest watt-hours if the integrator is strict about its
interval: the first sample must book nothing (no baseline to measure from),
a long stall must not book energy for time nobody sampled, and every
machine-level number must stay labelled as the estimate it is -- the same
model constants the UI renders.  These are the arithmetic traps the tab
cannot survive silently, so they are pinned here rather than eyeballed.
"""
import json
import os
import tempfile
import time
import unittest

from collectors import stats


NOW = time.time()


class _Tmp(object):
    def __init__(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)

    def close(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


class ObservePowerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _Tmp()
        self.ledger = stats.TokenLedger(self.tmp.path)

    def tearDown(self):
        self.tmp.close()

    def test_first_sample_only_sets_a_baseline(self):
        self.ledger.observe_power(gpu_w=400.0, cpu_pct=50.0, now=NOW)
        self.assertEqual(self.ledger.payload()["energy_rows"], [])

    def test_interval_is_integrated_in_watt_hours(self):
        # 360 W for exactly one hour is 360 Wh, and the machine estimate is
        # gpu + cpu(15+65*0.5) + platform, per the model shipped in stats.py.
        self.ledger.observe_power(gpu_w=360.0, cpu_pct=50.0, now=NOW)
        self.ledger.observe_power(gpu_w=360.0, cpu_pct=50.0,
                                  now=NOW + 3600.0 * 0.9)
        row = self.ledger.payload()["energy_rows"][0]
        expected_dt = min(stats.POWER_DT_CAP_S, 3240.0)  # capped! see next test
        self.assertAlmostEqual(row["gpu_wh"],
                               360.0 * expected_dt / 3600.0, places=2)
        cpu_w = stats.CPU_BASE_W + stats.CPU_SLOPE_W * 0.5
        self.assertAlmostEqual(row["cpu_est_wh"],
                               cpu_w * expected_dt / 3600.0, places=2)
        machine_w = 360.0 + cpu_w + stats.PLATFORM_W
        self.assertAlmostEqual(row["machine_wh"],
                               machine_w * expected_dt / 3600.0, places=2)
        self.assertAlmostEqual(row["wall_wh"],
                               row["machine_wh"] / stats.PSU_EFFICIENCY,
                               places=2)

    def test_long_gap_is_capped_not_invented(self):
        self.ledger.observe_power(gpu_w=400.0, cpu_pct=10.0, now=NOW)
        # Eight hours with nobody measuring (sleep, stall, reboot) must book
        # at most the cap, not eight hours of phantom energy.
        self.ledger.observe_power(gpu_w=400.0, cpu_pct=10.0,
                                  now=NOW + 8 * 3600.0)
        row = self.ledger.payload()["energy_rows"][0]
        self.assertAlmostEqual(row["gpu_wh"],
                               400.0 * stats.POWER_DT_CAP_S / 3600.0, places=2)
        self.assertAlmostEqual(row["hours"], stats.POWER_DT_CAP_S / 3600.0,
                               places=2)

    def test_backwards_clock_books_nothing(self):
        self.ledger.observe_power(gpu_w=400.0, cpu_pct=10.0, now=NOW)
        self.ledger.observe_power(gpu_w=400.0, cpu_pct=10.0, now=NOW + 10.0)
        self.ledger.observe_power(gpu_w=400.0, cpu_pct=10.0, now=NOW - 5.0)
        rows = self.ledger.payload()["energy_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["samples"], 1)

    def test_missing_sensors_book_nothing_but_gpu_still_shrinks_machine(self):
        self.ledger.observe_power(gpu_w=None, cpu_pct=None, now=NOW)
        self.ledger.observe_power(gpu_w=None, cpu_pct=None, now=NOW + 10.0)
        self.assertEqual(self.ledger.payload()["energy_rows"], [])
        # GPU-only sample: machine = gpu + cpu floor + platform (not zeroed).
        self.ledger.observe_power(gpu_w=200.0, cpu_pct=None, now=NOW + 20.0)
        row = self.ledger.payload()["energy_rows"][0]
        self.assertEqual(row["samples"], 1)
        self.assertAlmostEqual(row["machine_wh"],
                               (200.0 + stats.CPU_BASE_W + stats.PLATFORM_W)
                               * 10.0 / 3600.0, places=2)

    def test_reset_clears_energy_and_flush_persists_it(self):
        self.ledger.observe_power(gpu_w=300.0, cpu_pct=25.0, now=NOW)
        self.ledger.observe_power(gpu_w=300.0, cpu_pct=25.0, now=NOW + 10.0)
        self.assertEqual(len(self.ledger.payload()["energy_rows"]), 1)
        self.assertTrue(self.ledger.flush(force=True))
        with open(self.tmp.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], 3)
        self.assertIn("energy", data)

        revived = stats.TokenLedger(self.tmp.path)
        rows = revived.payload()["energy_rows"]
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["gpu_wh"],
                               300.0 * 10.0 / 3600.0, places=2)

        revived.reset()
        self.assertEqual(revived.payload()["energy_rows"], [])
        with open(self.tmp.path, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["energy"], {})

    def test_malformed_persisted_energy_is_sanitised(self):
        day = time.strftime("%Y-%m-%d")
        with open(self.tmp.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 3,
                       "energy": {day: {"gpu_wh": "many", "cpu_est_wh": -5,
                                        "machine_wh": 12.5, "hours": 1,
                                        "samples": 3, "junk": 9}}}, fh)
        ledger = stats.TokenLedger(self.tmp.path)
        row = ledger.payload()["energy_rows"][0]
        self.assertEqual(row["gpu_wh"], 0.0)     # not a number -> dropped
        self.assertEqual(row["cpu_est_wh"], 0.0)  # negative -> dropped
        self.assertEqual(row["machine_wh"], 12.5)
        self.assertNotIn("junk", row)

    def test_energy_days_trim_with_the_retention_window(self):
        ledger = stats.TokenLedger(self.tmp.path, keep_days=7)
        stale = time.strftime("%Y-%m-%d", time.localtime(time.time() - 40 * 86400))
        ledger._energy[stale] = stats._blank_energy()
        ledger._energy[time.strftime("%Y-%m-%d")] = stats._blank_energy()
        ledger._trim()
        rows = ledger.payload()["energy_rows"]
        self.assertEqual([row["day"] for row in rows],
                         [time.strftime("%Y-%m-%d")])

    def test_payload_carries_the_model_the_ui_renders(self):
        model = self.ledger.payload()["energy_model"]
        self.assertEqual(model["platform_w"], stats.PLATFORM_W)
        self.assertEqual(model["cpu_base_w"], stats.CPU_BASE_W)
        self.assertEqual(model["cpu_slope_w"], stats.CPU_SLOPE_W)
        self.assertEqual(model["psu_efficiency"], stats.PSU_EFFICIENCY)
        self.assertGreater(self.ledger.payload()["energy_price_cny_per_kwh"], 0)


if __name__ == "__main__":
    unittest.main()
