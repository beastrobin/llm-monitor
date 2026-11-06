"""Lifetime model buckets carry a per-build attribution map (v1.19).

The TOP-3 ranking tables read ``model_rows``.  v1.18 moved ledger naming from
the endpoint id to the booked engine build but only stamped combo rows, so
the ranking kept printing "mc/<model>" -- the card handle, which outlives
whichever engine sits behind it -- while the detail table right beside it
printed "basalt".  A model bucket is a LIFETIME sum, so unlike a day-scoped
combo it cannot carry one "last build wins" stamp: that would repaint an
earlier engine's tokens under whoever served last.  It accumulates output
tokens per build instead, and the UI names every brand that booked it.

The properties the ledger test protects:

* a booked sample adds its build's weight to the model bucket it booked;
* a bucket spanning two engines keeps BOTH weights;
* the map survives ``flush`` + reload (ledger data, not live state);
* ``reset`` clears it with every other book;
* a legacy file without the dimension recovers attribution from the combo
  stamps of the same bucket, and a persisted map is never re-seeded (no
  double-counting);
* pair readings (no version) and output-free samples book tokens but add no
  weight; trimming the bucket takes its map with it.
"""

import json
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


class ModelVersionAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "stats.json")
        self.ledger = stats.TokenLedger(self.path)
        self.addCleanup(self.tmp.cleanup)

    def _model_row(self, model, ledger=None):
        ledger = ledger or self.ledger
        for r in ledger.payload()["model_rows"]:
            if r["model"] == model:
                return r
        return None

    def test_booked_build_reaches_the_model_row(self):
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        row = self._model_row("m1")
        self.assertIsNotNone(row)
        self.assertEqual(row.get("engine_versions"), {"basalt 0.1.0": 10})

    def test_bucket_spanning_engines_keeps_both_weights(self):
        # The same model served by two builds/engines on the same handle:
        # neither may claim the row alone.
        self.ledger.observe([reading("mc/m1", 100, 5, "MegaCapybara 1.0")],
                            now=NOW, watts=300)
        # Absolute counters: 30 total output - 5 booked = 25 to basalt.
        self.ledger.observe([reading("mc/m1", 100, 30, "basalt 0.2.1")],
                            now=NOW + 2.0, watts=300)
        row = self._model_row("m1")
        self.assertIsNotNone(row)
        self.assertEqual(row["engine_versions"],
                         {"MegaCapybara 1.0": 5, "basalt 0.2.1": 25})

    def test_map_survives_flush_and_reload(self):
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        self.assertTrue(self.ledger.flush(force=True))
        revived = stats.TokenLedger(self.path)
        row = self._model_row("m1", revived)
        self.assertIsNotNone(row)
        self.assertEqual(row["engine_versions"], {"basalt 0.1.0": 10})
        # A reload must not re-seed a persisted map from the combos of the
        # same bucket on top of it: 15 total - 10 booked = +5, not +15.
        revived.observe([reading("basalt/m1", 100, 15, "basalt 0.1.0")],
                        now=NOW + 4.0, watts=300)
        row = self._model_row("m1", revived)
        self.assertIsNotNone(row)
        self.assertEqual(row["engine_versions"], {"basalt 0.1.0": 15})

    def test_reset_clears_the_maps(self):
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        self.ledger.reset()
        self.assertEqual(self.ledger.payload()["model_rows"], [])
        revived = stats.TokenLedger(self.path)
        self.assertEqual(revived.payload()["model_rows"], [])

    def test_legacy_file_seeds_from_combo_stamps(self):
        # A stats.json written before this dimension existed: the model
        # bucket is booked, the combos carry builds, model_versions absent.
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        # Second same-day sample raises the odometer to 14; one day/tier
        # combo ends stamped with the newest build that fed it.
        self.ledger.observe([reading("basalt/m1", 100, 14, "basalt 0.2.1")],
                            now=NOW + 2.0, watts=300)
        self.assertTrue(self.ledger.flush(force=True))
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        del data["model_versions"]
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        revived = stats.TokenLedger(self.path)
        # One day/tier combo carries one last-won build stamp (v1.18
        # semantics), so the seed honestly attributes the bucket's 14
        # output tokens to the build that stamp reads.
        row = self._model_row("m1", revived)
        self.assertIsNotNone(row)
        self.assertEqual(row["engine_versions"], {"basalt 0.2.1": 14})

    def test_pair_readings_and_output_free_samples_book_no_weight(self):
        # Hand-built pairs (ledger_check's shape) and prompt-only deltas
        # still book tokens; the row then honestly falls back to the id.
        self.ledger.observe([reading("loader/m1", 100, 10)[:2]],
                            now=NOW, watts=300)
        self.ledger.observe([reading("basalt/m2", 100, 0, "basalt 0.2.1")],
                            now=NOW + 2.0, watts=300)
        for name in ("m1", "m2"):
            row = self._model_row(name)
            self.assertIsNotNone(row)
            self.assertIsNone(row.get("engine_versions"))

    def test_orphan_map_dies_with_a_hand_edited_bucket(self):
        self.ledger.observe([reading("basalt/m1", 100, 10, "basalt 0.1.0")],
                            now=NOW, watts=300)
        self.assertTrue(self.ledger.flush(force=True))
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        del data["models"]["basalt/m1"]  # hand-edit the books out from under
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        revived = stats.TokenLedger(self.path)
        self.assertNotIn("basalt/m1", revived._model_ver)
        self.assertIsNone(self._model_row("m1", revived))


if __name__ == "__main__":
    unittest.main()
