"""The MegaCapybara card, translated from the engine's ``/stats`` document.

MegaCapybara speaks none of the dialects the other collectors parse: no
``/props``, no ``/slots``, no ``/metrics``.  Everything the card and the
ledger need comes out of the one document its own launcher polls
(``/stats``), so the tests pin that translation:

* **The probe is the stats document itself** -- the engine answers it
  while idle, and it is the only place the live numbers live, so up/down
  and card content share it.
* **Every listed model is loaded** -- the vLLM-shaped ``/v1/models`` rows
  carry no ``status`` block, and the parent mapping would read the absence
  as unloaded and empty the card.
* **The lifetime totals feed the ledger** -- ``tokens_prefilled`` /
  ``tokens_generated`` map onto the counter names the ledger books, and
  every figure the engine does not publish (seconds, cached split, draft
  counters) stays ``None`` rather than guessed.
* **Working jobs map onto slot rows** -- the parked rows of ``jobs[]`` are
  the engine's resumable conversation cache (RAM tier), not slots: a
  four-slot server carries dozens of parked conversations, and one row each
  would read as "50 slots".  The slot section shows the working
  conversations (at most ``max_concurrency``); an idle engine has none, and
  the context card then carries the "空闲残留" footnote like the other
  engines.
"""

import time
import unittest
from unittest import mock

from collectors.megacapybara import MegaCapybaraEndpoint


def job(jid, state="parked", context=100000, generated=0, tok_s=0.0,
        prefill_frac=0.0, tier="ram"):
    return {"id": jid, "client": "", "state": state,
            "prefill_frac": prefill_frac, "context": context,
            "generated": generated, "tok_per_s": tok_s,
            "accepted_per_round": 0.0, "preset": "", "elapsed_s": 0.0,
            "slot": -1, "tier": tier, "continues": ""}


def stats(jobs=None, **over):
    base = {
        "model": "qwen3.8-27b-large.mcapy",
        "kv_mode": "6/4",
        "activations": "fp8",
        "speculator": "DFlash2",
        "max_context": 786432,
        "kv_paging": True,
        "max_concurrency": 4,
        "yarn_factor": 1.0,
        "conversation_context": 262144,
        "uptime_s": 1000.0,
        "decode_tps": 0.0,
        "prefill_tps": 0.0,
        "accepted_per_round": 0.0,
        "rows_per_round": 16.0,
        "round_ms": 0.0,
        "kv_used_tokens": 0,
        "kv_capacity_tokens": 786432,
        "kv_pool_free_tokens": 786432,
        "kv_context_tokens": 0,
        "checkpoints": 0,
        "requests_total": 0,
        "cache_hit_rate": None,
        "ram_tier_used_gb": 0.0,
        "ram_tier_gb": 172.9,
        "pinned_gb": 0.0,
        "pinned_budget_gb": 177.4,
        "disk_tier_used_gb": 0.0,
        "disk_tier_gb": 20.0,
        "queued": 0,
        "parked": 0,
        "active": 0,
        "swapped": 0,
        "tokens_generated": 0,
        "tokens_prefilled": 0,
        "jobs": jobs if jobs is not None else [],
    }
    base.update(over)
    return base


def make_endpoint():
    return MegaCapybaraEndpoint({"id": "mc", "label": "MegaCapybara",
                                 "base_url": "http://127.0.0.1:8085"})


class Registration(unittest.TestCase):
    def test_kind_dispatches_to_the_megacapybara_collector(self):
        from monitor import ENDPOINT_KINDS
        self.assertIs(ENDPOINT_KINDS["megacapybara"], MegaCapybaraEndpoint)


class Probe(unittest.TestCase):
    def test_probe_reads_stats_and_names_the_brand(self):
        ep = make_endpoint()
        with mock.patch("collectors.megacapybara._get_json",
                        return_value=stats()) as fetched:
            out = ep.probe()
        self.assertEqual(out["mode"], "single")
        self.assertEqual(out["props"]["build_info"], "MegaCapybara")
        # the stats document, not a props or metrics path
        self.assertEqual(fetched.call_args[0][0],
                         "http://127.0.0.1:8085/stats")
        self.assertIsInstance(ep._stats, dict)

    def test_probe_rejects_a_non_object(self):
        ep = make_endpoint()
        with mock.patch("collectors.megacapybara._get_json",
                        return_value=[1, 2]):
            with self.assertRaises(ValueError):
                ep.probe()

    def test_base_url_change_drops_the_cached_document(self):
        ep = make_endpoint()
        ep._stats = stats()
        self.assertTrue(ep.set_base_url("http://127.0.0.1:9999"))
        self.assertIsNone(ep._stats)
        # an unchanged address keeps the document
        ep._stats = stats()
        self.assertFalse(ep.set_base_url("http://127.0.0.1:9999"))
        self.assertIsInstance(ep._stats, dict)


class ListModels(unittest.TestCase):
    def test_every_listed_model_is_loaded(self):
        ep = make_endpoint()
        payload = {"object": "list", "data": [
            {"id": "qwen3.8-27b", "object": "model", "created": 1,
             "owned_by": "megacapybara", "context_length": 262144,
             "max_model_len": 262144}]}
        with mock.patch("collectors.megacapybara._get_json",
                        return_value=payload):
            models = ep.list_models()
        self.assertEqual(models[0]["id"], "qwen3.8-27b")
        self.assertTrue(models[0]["loaded"])
        self.assertEqual(models[0]["context_length"], 262144)


class ModelDetail(unittest.TestCase):
    def detail(self, st):
        ep = make_endpoint()
        ep._stats = st
        return ep._model_detail("qwen3.8-27b", None, time.time())

    def test_identity_and_context(self):
        d = self.detail(stats())
        self.assertEqual(d["id"], "qwen3.8-27b")
        self.assertTrue(d["loaded"])
        # the per-conversation ceiling, not the KV pool total
        self.assertEqual(d["n_ctx"], 262144)
        self.assertEqual(d["total_slots"], 4)
        self.assertEqual(d["model_path"], "qwen3.8-27b-large.mcapy")

    def test_speculator_names_the_mtp(self):
        d = self.detail(stats(accepted_per_round=3.0, rows_per_round=16.0))
        self.assertTrue(d["mtp_enabled"])
        self.assertEqual(d["mtp_type"], "dflash2")
        self.assertEqual(d["spec_types"], "dflash2")
        m = d["metrics"]
        self.assertTrue(m["mtp_active"])
        # acceptance = accepted per round over the drafter's window
        self.assertAlmostEqual(m["mtp_accept_ratio"], 3.0 / 16.0)
        self.assertEqual(m["mtp_tokens_per_step"], 3.0)

    def test_speculator_off_shows_no_acceptance(self):
        d = self.detail(stats(speculator=""))
        self.assertFalse(d["mtp_enabled"])
        self.assertIsNone(d["mtp_type"])
        m = d["metrics"]
        self.assertFalse(m["mtp_active"])
        # no drafter, no acceptance figure -- a 0% bar would read as a
        # measurement
        self.assertIsNone(m["mtp_accept_ratio"])
        self.assertIsNone(m["mtp_tokens_per_step"])

    def test_counters_carry_the_lifetime_totals(self):
        d = self.detail(stats(tokens_prefilled=11838570,
                              tokens_generated=3477317))
        c = d["metrics"]["counters"]
        self.assertEqual(c["prompt_tokens"], 11838570)
        self.assertEqual(c["predicted_tokens"], 3477317)
        # seconds and the cached/draft splits have no source on this engine
        self.assertIsNone(c["prompt_seconds"])
        self.assertIsNone(c["predicted_seconds"])
        self.assertIsNone(c["prompt_cached"])
        self.assertIsNone(c["draft_tokens"])
        self.assertIsNone(c["drafts"])

    def test_rates_and_hit_rate_are_the_engine_s_own(self):
        d = self.detail(stats(decode_tps=276.5, prefill_tps=1388.0,
                              cache_hit_rate=0.947, active=1, queued=2))
        m = d["metrics"]
        self.assertEqual(m["decode_tps_inst"], 276.5)
        self.assertEqual(m["prefill_tps_inst"], 1388.0)
        self.assertAlmostEqual(m["kv_hit_ratio"], 0.947)
        self.assertEqual(m["requests_processing"], 1)
        self.assertEqual(m["requests_deferred"], 2)
        # the engine reports no decode/prefill seconds, so no cumulative
        # average can be derived from its counters
        self.assertIsNone(m["decode_tps_avg"])
        self.assertIsNone(m["prefill_tps_avg"])

    def test_parked_jobs_are_conversation_cache_not_slots(self):
        # A parked conversation is the engine's resumable cache (RAM tier),
        # not a slot: a four-slot server carries dozens of them, and one
        # row each would read as "50 slots".
        d = self.detail(stats(jobs=[job("cache-1", state="parked",
                                        context=195651)]))
        self.assertEqual(d["slots"], [])

    def test_mixed_jobs_show_the_working_rows_only(self):
        d = self.detail(stats(jobs=[
            job("cache-1", state="parked", context=195651),
            job("job-9", state="generating", context=10000, generated=812,
                tok_s=205.7),
            job("job-8", state="reading", context=5000,
                prefill_frac=0.4),
            job("cache-2", state="parked", context=180000)]))
        self.assertEqual([s["id"] for s in d["slots"]], ["job-9", "job-8"])
        # live state still reads the parked rows' neighbours: the working
        # job outranks everything for the hero lamps
        self.assertEqual(d["live_state"]["state"], "generating")

    def test_working_job_carries_rate_and_split(self):
        d = self.detail(stats(jobs=[job("job-9", state="generating",
                                        context=10000, generated=812,
                                        tok_s=205.7)]))
        s = d["slots"][0]
        self.assertTrue(s["is_processing"])
        self.assertEqual(s["n_decoded"], 812)
        self.assertEqual(s["n_prompt_tokens"], 10000 - 812)
        self.assertEqual(s["tok_s"], 205.7)

    def test_live_state_prefers_generating(self):
        d = self.detail(stats(jobs=[
            job("a", state="reading", context=5000, prefill_frac=0.4),
            job("b", state="generating", context=10000, generated=812,
                tok_s=205.7)]))
        live = d["live_state"]
        self.assertEqual(live["state"], "generating")
        self.assertEqual(live["generated"], 812)
        self.assertEqual(live["tok_s"], 205.7)

    def test_live_state_reading_carries_the_prompt(self):
        d = self.detail(stats(jobs=[job("a", state="reading",
                                        context=5000)]))
        live = d["live_state"]
        self.assertEqual(live["state"], "reading")
        self.assertEqual(live["prompt_tokens"], 5000)

    def test_live_state_idle_reports_the_queue(self):
        d = self.detail(stats(queued=3, jobs=[job("a", state="parked")]))
        live = d["live_state"]
        self.assertEqual(live["state"], "idle")
        self.assertEqual(live["queued"], 3)

    def test_no_jobs_gives_idle_and_no_slots(self):
        d = self.detail(stats())
        self.assertEqual(d["live_state"]["state"], "idle")
        self.assertEqual(d["slots"], [])
        # no finished-request log exists on this engine
        self.assertIsNone(d["last_request"])

    def test_missing_stats_still_shapes_a_detail(self):
        ep = make_endpoint()
        ep._stats = None
        d = ep._model_detail("x", None, time.time())
        self.assertIsNone(d["n_ctx"])
        self.assertIsNone(d["metrics"]["decode_tps_inst"])
        self.assertEqual(d["slots"], [])
        self.assertEqual(d["live_state"]["state"], "idle")


if __name__ == "__main__":
    unittest.main()
