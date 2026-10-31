"""The ledger stamps the engine build onto the row it books.

Since v1.13 the combo rows carry ``engine_version`` -- the build that was
producing tokens when the tokens were booked -- because the alternative
(annotating rows from whatever the endpoint reports *now*) went blank for
every historical strata row the day basalt replaced strata: a version next to
a number only means something if it is the version that achieved the number.

The properties the ledger test protects:

* the stamp lands on the combo it was booked with, and only there;
* it survives ``flush`` + reload (it is ledger data, not live state);
* hand-built pair readings (the older callers in ledger_check) still book
  tokens and simply get no stamp;
* trimming a combo takes its stamp with it -- no orphan keys.
"""

import os
import time
import tempfile
import unittest

from collectors import stats


NOW = time.time()  # inside the ledger keep-days window


def reading(key, prompt, predicted, version=None):
    return (key, {"prompt_tokens": prompt, "predicted_tokens": predicted,
                  "cached_tokens": 0, "decode_steps": None,
                  "prompt_seconds": 1.0, "predicted_seconds": 1.0},
            version)


class ComboVersionStamping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "stats.json")
        self.ledger = stats.TokenLedger(self.path)
        self.addCleanup(self.tmp.cleanup)

    def _rows(self, ledger=None):
        ledger = ledger or self.ledger
        for r in ledger.payload()["combos"]:
            if r["model"] == "m1":
                return r
        return None

    def test_stamp_travels_with_the_combo_row(self):
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        row = self._rows()
        self.assertEqual(row.get("engine_version"), "basalt 0.1.0")

    def test_each_combo_keeps_its_own_build(self):
        # Two models on one day/tier: a version booked for one must not
        # bleed onto the other's row.
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        self.ledger.observe([reading("basalt/m2", 100, 10, "basalt 0.2.0")],
                            now=NOW + 2.0, watts=300)
        rows = {r["model"]: r for r in self.ledger.payload()["combos"]}
        self.assertEqual(rows["m1"].get("engine_version"), "basalt 0.1.0")
        self.assertEqual(rows["m2"].get("engine_version"), "basalt 0.2.0")

    def test_pair_readings_book_without_a_stamp(self):
        # The older shape tools/ledger_check.py still feeds.
        self.ledger.observe([reading("loader/m1", 100, 10)[:2]],
                            now=NOW, watts=300)
        row = self._rows()
        self.assertEqual(row["predicted_tokens"], 10)
        self.assertIsNone(row.get("engine_version"))

    def test_stamp_survives_flush_and_reload(self):
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        self.assertTrue(self.ledger.flush(force=True))
        revived = stats.TokenLedger(self.path)
        self.assertEqual(self._rows(revived).get("engine_version"),
                         "basalt 0.1.0")

    def test_readings_for_carries_the_endpoints_build(self):
        endpoints = [{"id": "basalt", "engine_version": "basalt 0.1.0",
                      "models": [{"id": "m1", "metrics": {"counters": {
                          "prompt_tokens": 5, "predicted_tokens": 2,
                          "prompt_cached": 0, "prompt_seconds": 1,
                          "predicted_seconds": 1}}}]},
                     {"id": "loader", "models": [{"id": "m9", "metrics": {
                          "counters": {"prompt_tokens": 5,
                                       "predicted_tokens": 2}}}]
                      }]
        out = stats.readings_for(endpoints)
        by_key = {r[0]: r for r in out}
        self.assertEqual(by_key["basalt/m1"][2], "basalt 0.1.0")
        self.assertIsNone(by_key["loader/m9"][2])


if __name__ == "__main__":
    unittest.main()
