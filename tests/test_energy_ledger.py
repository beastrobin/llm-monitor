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



class TouPricingTest(unittest.TestCase):
    """Peak/valley billing: hourly split, boundaries, and flat fallback."""

    SH = {"peak_price_cny_per_kwh": 0.617, "valley_price_cny_per_kwh": 0.307,
          "peak_start_hour": 6, "peak_end_hour": 22}

    def setUp(self):
        self.tmp = _Tmp()

    def tearDown(self):
        self.tmp.close()

    def _ledger(self, tou=SH):
        return stats.TokenLedger(self.tmp.path, tou=tou)

    def test_sanitize_accepts_and_rejects(self):
        self.assertEqual(stats.sanitize_tou(self.SH), self.SH)
        self.assertIsNone(stats.sanitize_tou(None))
        self.assertIsNone(stats.sanitize_tou({}))
        self.assertIsNone(stats.sanitize_tou(
            {"peak_price_cny_per_kwh": -1, "valley_price_cny_per_kwh": 0.3}))
        self.assertIsNone(stats.sanitize_tou(
            {"peak_price_cny_per_kwh": 0.6, "valley_price_cny_per_kwh": 0.3,
             "peak_start_hour": 9, "peak_end_hour": 9}))
        self.assertIsNone(stats.sanitize_tou(
            {"peak_price_cny_per_kwh": 0.6, "valley_price_cny_per_kwh": 0.3,
             "peak_start_hour": 25, "peak_end_hour": 6}))

    def test_hour_boundaries_pick_the_right_rate(self):
        # peak span [6, 22): 5 is valley, 6 is peak, 21 is peak, 22 is valley.
        for hour, rate in ((5, 0.307), (6, 0.617), (21, 0.617), (22, 0.307)):
            self.assertAlmostEqual(stats._tou_price(
                stats.sanitize_tou(self.SH), hour), rate, places=9)

    def test_peak_span_wrapping_midnight(self):
        night = stats.sanitize_tou({"peak_price_cny_per_kwh": 1.0,
                                    "valley_price_cny_per_kwh": 0.2,
                                    "peak_start_hour": 22,
                                    "peak_end_hour": 6})
        self.assertTrue(stats._tou_price(night, 23) == 1.0)
        self.assertTrue(stats._tou_price(night, 3) == 1.0)
        self.assertTrue(stats._tou_price(night, 12) == 0.2)

    def test_bill_uses_the_hour_the_energy_landed_in(self):
        ledger = self._ledger()
        # Two samples one hour apart inside two different rate hours:
        # book from hour 5 (valley) into hour 6 (peak) via a real hour span.
        ledger.observe_power(gpu_w=360.0, cpu_pct=50.0,
                             now=NOW)                       # baseline
        ledger.observe_power(gpu_w=360.0, cpu_pct=50.0,
                             now=NOW + 10.0)                # one 10 s slice
        day = time.strftime("%Y-%m-%d")
        hour = str(time.localtime(NOW + 10.0).tm_hour)
        bucket = ledger._energy[day]
        self.assertIn(hour, bucket["by_hour"])
        # Charge it and cross-check _cost against the rate of that hour.
        cost, basis = ledger._cost(bucket)
        self.assertEqual(basis, "tou")
        part_wh = bucket["by_hour"][hour]["machine_wh"]
        want = part_wh / stats.PSU_EFFICIENCY * stats._tou_price(
            stats.sanitize_tou(self.SH), int(hour)) / 1000.0
        self.assertAlmostEqual(cost, want, places=6)

    def test_day_without_hourly_split_bills_flat(self):
        ledger = self._ledger()
        day = time.strftime("%Y-%m-%d")
        legacy = stats._blank_energy()
        legacy.pop("by_hour")
        ledger._energy[day] = legacy
        legacy.update({"machine_wh": 900.0, "hours": 3.0, "samples": 1})
        cost, basis = ledger._cost(ledger._energy[day])
        self.assertEqual(basis, "flat")
        self.assertAlmostEqual(cost, 900.0 / stats.PSU_EFFICIENCY
                               * stats.ENERGY_PRICE_CNY_PER_KWH / 1000.0,
                               places=6)


    def test_mixed_day_bills_split_at_tou_and_residual_flat(self):
        # The migration day: energy booked before the hourly books existed
        # sits in the same bucket as the new split hours.  The split hours
        # must be billed at TOU and the unsplit remainder at the flat price;
        # the row must not pretend to be a pure TOU bill.
        ledger = self._ledger()
        day = time.strftime("%Y-%m-%d")
        bucket = ledger._energy.setdefault(day, stats._blank_energy())
        legacy_wh = 1800.0
        hour = str(time.localtime(NOW).tm_hour)
        bucket["by_hour"][hour] = {"gpu_wh": 100.0, "machine_wh": 200.0}
        bucket["machine_wh"] = legacy_wh + 200.0
        cost, basis = ledger._cost(bucket)
        self.assertEqual(basis, "mixed")
        want = (200.0 / stats.PSU_EFFICIENCY
                * stats._tou_price(stats.sanitize_tou(self.SH), int(hour))
                + legacy_wh / stats.PSU_EFFICIENCY
                * stats.ENERGY_PRICE_CNY_PER_KWH) / 1000.0
        self.assertAlmostEqual(cost, want, places=6)

    def test_rows_and_pricing_shape_reach_the_ui(self):
        ledger = self._ledger()
        ledger.observe_power(gpu_w=300.0, cpu_pct=25.0, now=NOW)
        ledger.observe_power(gpu_w=300.0, cpu_pct=25.0, now=NOW + 10.0)
        payload = ledger.payload()
        self.assertEqual(payload["energy_pricing"]["mode"], "tou")
        self.assertEqual(payload["energy_pricing"]["tou"]["peak_start_hour"], 6)
        row = payload["energy_rows"][0]
        self.assertIn("cost_yuan", row)
        self.assertEqual(row["cost_basis"], "tou")
        # A flat-mode ledger labels its bills accordingly.
        flat = self._ledger(tou=None)
        self.assertEqual(flat.payload()["energy_pricing"]["mode"], "flat")

    def test_by_hour_survives_persist_and_rejects_garbage(self):
        ledger = self._ledger()
        ledger.observe_power(gpu_w=300.0, cpu_pct=25.0, now=NOW)
        ledger.observe_power(gpu_w=300.0, cpu_pct=25.0, now=NOW + 10.0)
        ledger.flush(force=True)
        with open(self.tmp.path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for bucket in raw["energy"].values():
            bucket["by_hour"]["99"] = {"gpu_wh": 5}      # impossible hour
            bucket["by_hour"]["7"] = {"machine_wh": "x"}  # not a number
            bucket["by_hour"]["8"] = "junk"
        revived = stats.TokenLedger(self.tmp.path, tou=self.SH)
        bucket = revived._energy[time.strftime("%Y-%m-%d")]
        self.assertNotIn("99", bucket["by_hour"])
        self.assertNotIn("7", bucket["by_hour"])
        self.assertNotIn("8", bucket["by_hour"])
        # The real hour survived and the bill is still 'tou', not flat.
        self.assertTrue(bucket["by_hour"])
        self.assertEqual(revived.payload()["energy_rows"][0]["cost_basis"], "tou")


if __name__ == "__main__":
    unittest.main()
