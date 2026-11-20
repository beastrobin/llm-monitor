"""One model, one row -- the ledger key must not care about letter case.

Engine Control serves a model under the unified contract name
(``Qwen3.8-Flash-Next``) while the engine's own ``/v1/models`` answers with the
same name lower-cased, and the ledger keyed rows on that exact string.  On
2026-11-21 the books held ``mc/Qwen3.8-Flash-Next`` beside
``mc/qwen3.8-flash-next``: the ranking showed one model twice, each row smaller
than the work it covered, and the cost-per-million on both flattered the engine.

The split cost more than tidy rows.  ``_seen`` keeps the raw odometer per key,
so when the spelling changed mid-run the new key looked like a brand-new pair
and baselined from whatever the counter already carried -- re-booking tokens the
other spelling had already booked (see ``test_a_spelling_change_does_not_double
_book``).

Two rules the tests hold: only the model name folds (two endpoint ids that
differ by case are two servers), and the row keeps a real spelling rather than
the lower-cased handle it is filed under.
"""

import json
import os
import tempfile
import unittest

from collectors.stats import (TokenLedger, fold_model_case, model_part_of,
                              normalize_model_key, readings_for)

T = 1795200000.0
MC = "mc/Qwen3.8-Flash-Next"
MC_LOWER = "mc/qwen3.8-flash-next"


def endpoint(model_id, prompt, predicted, endpoint_id="mc", version="Strata 0.1.63"):
    """One live endpoint as the sampler hands it to ``readings_for``."""
    return {
        "id": endpoint_id,
        "engine_version": version,
        "models": [{"id": model_id, "metrics": {"counters": {
            "prompt_tokens": prompt, "predicted_tokens": predicted,
        }}}],
    }


def observe(ledger, model_id, prompt, predicted, endpoint_id="mc"):
    ledger.observe(readings_for([endpoint(model_id, prompt, predicted,
                                          endpoint_id)]), now=T, watts=300)


def rows_by_model(ledger):
    return dict((row["model"], row) for row in ledger.payload()["model_rows"])


class KeyFolding(unittest.TestCase):
    def test_only_the_model_name_folds(self):
        """Two endpoint ids that differ by case stay two servers."""
        keys = [key for key, _, _, _ in
                readings_for([endpoint("M", 10, 1, endpoint_id="mc"),
                              endpoint("M", 10, 1, endpoint_id="MC")])]
        self.assertEqual(sorted(keys), ["MC/m", "mc/m"])

    def test_combo_keys_fold_their_model_part(self):
        self.assertEqual(normalize_model_key("2026-11-20|300|mc/Qwen3.8-27B"),
                         "2026-11-20|300|mc/qwen3.8-27b")
        self.assertEqual(model_part_of("2026-11-20|300|mc/Qwen3.8-27B"),
                         "Qwen3.8-27B")

    def test_fold_keeps_the_spelling_that_earned_more(self):
        buckets = {
            "mc/Big": {"predicted_tokens": 30, "prompt_tokens": 300},
            "mc/big": {"predicted_tokens": 90, "prompt_tokens": 900},
        }
        merged, names = fold_model_case(buckets)
        self.assertEqual(list(merged), ["mc/big"])
        self.assertEqual(merged["mc/big"]["predicted_tokens"], 120)
        self.assertEqual(names["mc/big"], "big")


class LiveBooking(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "stats.json")

    def test_two_spellings_land_in_one_row(self):
        ledger = TokenLedger(self.path)
        observe(ledger, "Qwen3.8-Flash-Next", 100, 10)
        observe(ledger, "qwen3.8-flash-next", 150, 25)
        rows = rows_by_model(ledger)
        self.assertEqual(len(rows), 1, "模型排行被拼写劈成两行: %s" % list(rows))
        self.assertEqual(list(rows.values())[0]["predicted_tokens"], 25)

    def test_a_spelling_change_does_not_double_book(self):
        """The odometer is per model, not per spelling.

        Before the key folded, the second spelling was a fresh pair and took the
        whole counter as its baseline, so the 10 output tokens already booked
        under the first spelling got booked again.
        """
        ledger = TokenLedger(self.path)
        observe(ledger, "Qwen3.8-Flash-Next", 100, 10)
        observe(ledger, "qwen3.8-flash-next", 150, 25)
        self.assertEqual(ledger.payload()["totals"]["predicted_tokens"], 25)

    def test_the_row_prints_a_real_name(self):
        """Filed lower-case, printed as an engine wrote it."""
        ledger = TokenLedger(self.path)
        observe(ledger, "Qwen3.8-Flash-Next", 100, 10)
        self.assertEqual(list(rows_by_model(ledger)), ["Qwen3.8-Flash-Next"])

    def test_the_name_survives_a_restart(self):
        ledger = TokenLedger(self.path)
        observe(ledger, "Qwen3.8-Flash-Next", 100, 10)
        ledger.flush(force=True)
        again = TokenLedger(self.path)
        self.assertEqual(list(rows_by_model(again)), ["Qwen3.8-Flash-Next"])


class LegacyLedger(unittest.TestCase):
    """A file written before bookings folded, loaded as it stands today."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "stats.json")
        seen = dict((f, 0) for f in ("prompt_tokens", "predicted_tokens",
                                     "cached_tokens", "decode_steps",
                                     "prompt_seconds", "predicted_seconds"))
        seen.update({"prompt_tokens": 200, "predicted_tokens": 20})
        lower = dict(seen, prompt_tokens=900, predicted_tokens=90)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "version": 3,
                "models": {MC: {"prompt_tokens": 200, "predicted_tokens": 20},
                           MC_LOWER: {"prompt_tokens": 900,
                                      "predicted_tokens": 90}},
                "combos": {"2026-11-20|300|" + MC: {"prompt_tokens": 200,
                                                    "predicted_tokens": 20},
                           "2026-11-20|300|" + MC_LOWER: {
                               "prompt_tokens": 900, "predicted_tokens": 90}},
                "combo_versions": {"2026-11-20|300|" + MC: "Strata 0.1.59",
                                   "2026-11-20|300|" + MC_LOWER: "Strata 0.1.63"},
                "model_versions": {MC: {"Strata 0.1.59": 20},
                                   MC_LOWER: {"Strata 0.1.63": 90}},
                "seen": {MC: seen, MC_LOWER: lower},
            }, fh)

    def test_the_two_rows_become_one_with_their_sum(self):
        rows = rows_by_model(TokenLedger(self.path))
        self.assertEqual(len(rows), 1)
        row = list(rows.values())[0]
        self.assertEqual(row["predicted_tokens"], 110)
        self.assertEqual(row["prompt_tokens"], 1100)

    def test_both_engines_keep_credit_for_the_merged_row(self):
        row = list(rows_by_model(TokenLedger(self.path)).values())[0]
        self.assertEqual(row["engine_versions"],
                         {"Strata 0.1.59": 20, "Strata 0.1.63": 90})

    def test_the_day_row_merges_and_names_both_builds(self):
        combos = TokenLedger(self.path).payload()["combos"]
        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["model"], "qwen3.8-flash-next")
        self.assertEqual(combos[0]["engine_version"], "Strata 0.1.59 + Strata 0.1.63")

    def test_the_odometer_folds_to_its_highest_reading(self):
        """One model is one counter stream, however it is spelled.

        The merged row holds 110 output tokens; the two raw tracks say 20 and 90.
        A live 95 must book 5.  Claiming only one track would book the gap to it
        as new work, and a row that invents tokens is worse than one that takes a
        low reading for a reset and books nothing.
        """
        ledger = TokenLedger(self.path)
        observe(ledger, "qwen3.8-flash-next", 950, 95)
        row = list(rows_by_model(ledger).values())[0]
        self.assertEqual(row["predicted_tokens"], 115)
        self.assertEqual(row["prompt_tokens"], 1150)
        self.assertEqual(ledger.payload()["resets"], 0)


if __name__ == "__main__":
    unittest.main()
