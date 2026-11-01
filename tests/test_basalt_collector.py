"""The basalt card's counters, rebuilt from the finished-request ring.

Basalt's ``/metrics`` carries no lifetime ``totals`` -- only ``requests[]``,
a newest-first ring of finished requests, and a short ``history``.  The ledger
and the card's cumulative averages are counter-derived, so the collector
accumulates the ring itself.  The two properties that matter are therefore:

* **Exactly once** -- a request sitting in the ring across ten polls must be
  booked by the first poll that sees it and by none of the other nine.
* **Restart-safe** -- a basalt restart reuses the ``r_0001``-style ids, so the
  accumulation keys ids with their ``time`` and a fresh server adds its rows
  instead of being swallowed as "already booked".

The rest pins the translation into the llama.cpp counter names ``ModelSampler``
reads (prompt minus reused, ms -> seconds), the ``live`` gauges basalt does
report, and the mapping of basalt's separate ``/slots`` dump onto the per-slot
row shape the context cards read.
"""

import time
import unittest
from unittest import mock

from collectors.basalt import BasaltEndpoint


def request(rid, t, prompt=1000, reused=400, out=100, tok_s=200.0,
            prompt_ms=500.0, offered=80, accepted=60):
    return {"id": rid, "time": t, "prompt_tokens": prompt, "reused": reused,
            "output_tokens": out, "decode_tok_s": tok_s,
            "prompt_ms": prompt_ms, "drafts_offered": offered,
            "drafts_accepted": accepted}


def payload(requests, live=None, engine=None):
    return {"engine": engine or {"batch_slots": 3, "context": 262144},
            "live": live if live is not None else {"state": "idle",
                                                   "slots": {"total": 3,
                                                             "active": 0,
                                                             "queue_depth": 0}},
            "requests": requests}


def make_endpoint():
    return BasaltEndpoint({"id": "basalt", "label": "Basalt",
                           "base_url": "http://127.0.0.1:8085"})


class RingAccumulation(unittest.TestCase):
    def test_series_uses_the_llamacpp_counter_names(self):
        ep = make_endpoint()
        series = ep._series(payload([request("r_1", 100.0)]))
        # read tokens = whole prompt - reused; ms -> seconds.
        self.assertEqual(series["llamacpp:prompt_tokens_total"], 600.0)
        self.assertEqual(series["llamacpp:prompt_tokens_cached_total"], 400.0)
        self.assertEqual(series["llamacpp:prompt_seconds_total"], 0.5)
        self.assertEqual(series["llamacpp:tokens_predicted_total"], 100.0)
        # decode seconds = 100 tok / 200 tok/s = 0.5 s
        self.assertEqual(series["llamacpp:tokens_predicted_seconds_total"], 0.5)
        self.assertEqual(series["llamacpp:spec_decode_num_draft_tokens_total"], 80)
        self.assertEqual(
            series["llamacpp:spec_decode_num_accepted_tokens_total"], 60)

    def test_a_request_sitting_in_the_ring_is_booked_once(self):
        ep = make_endpoint()
        ring = [request("r_1", 100.0)]
        first = dict(ep._series(payload(ring)))
        again = None
        for _ in range(9):
            again = dict(ep._series(payload(ring)))
        self.assertEqual(first, again)

    def test_restarted_engine_reuses_ids_and_still_books(self):
        ep = make_endpoint()
        ep._series(payload([request("r_1", 100.0)]))
        # Same id, new process: a fresh ``time`` is a different request.
        series = ep._series(payload([request("r_1", 900.0)]))
        self.assertEqual(series["llamacpp:prompt_tokens_total"], 1200.0)
        self.assertEqual(series["llamacpp:tokens_predicted_total"], 200.0)

    def test_gauges_come_from_live_not_the_ring(self):
        ep = make_endpoint()
        live = {"state": "generating",
                "slots": {"total": 3, "active": 2, "queue_depth": 1}}
        series = ep._series(payload([], live=live))
        self.assertEqual(series["llamacpp:requests_processing"], 2)
        self.assertEqual(series["llamacpp:requests_deferred"], 1)
        # A solo request books no slot: the state word says it runs anyway.
        solo = {"state": "generating",
                "slots": {"total": 3, "active": 0, "queue_depth": 0}}
        self.assertEqual(ep._series(payload([], live=solo))
                         ["llamacpp:requests_processing"], 1)

    def test_slot_count_falls_back_to_the_engine_block(self):
        # With no /slots rows this poll (a scrape failure), total_slots
        # still names how many slots the engine runs (engine.batch_slots).
        ep = make_endpoint()
        ep._series(payload([]))  # caches the engine block
        with mock.patch("collectors.basalt._get_json", side_effect=OSError("down")):
            self.assertEqual(ep._slots(payload([])), [])
        self.assertEqual(ep._slots_error, "OSError: down")
        self.assertEqual(ep._engine_block.get("batch_slots"), 3)


class SlotMapping(unittest.TestCase):
    """basalt's separate /slots dump -> the per-slot row shape the card reads.

    Rows captured from the live server: a decoding slot, an idle slot that
    keeps a prefix, and a slot still reading its prompt.
    """

    def slots_dump(self):
        return [
            {"id": 0, "n_ctx": 262144, "is_processing": True, "state": "decoding",
             "tok_s": 318.1, "generated": 2139, "max_tokens": 2500,
             "prompt_tokens": 75, "prefill_done": 75, "reused": 60, "ttft_s": 0.2,
             "draft_len": 3.0, "ctx_used": 2214, "drafts_offered": 581,
             "drafts_accepted": 471},
            {"id": 1, "n_ctx": 262144, "is_processing": False, "state": "idle",
             "tok_s": 0.0, "generated": 0, "max_tokens": None, "prompt_tokens": 0,
             "prefill_done": 0, "reused": None, "ttft_s": None, "draft_len": None,
             "ctx_used": 42711, "drafts_offered": 668, "drafts_accepted": 456},
            {"id": 2, "n_ctx": 262144, "is_processing": True, "state": "prefill",
             "tok_s": 0.0, "generated": 0, "max_tokens": 500,
             "prompt_tokens": 4000, "prefill_done": 1500, "reused": 0,
             "ttft_s": None, "draft_len": None, "ctx_used": 4000,
             "drafts_offered": None, "drafts_accepted": None},
        ]

    def scrape(self, ep, live=None):
        dump = self.slots_dump()
        with mock.patch("collectors.basalt._get_json", return_value=dump):
            return ep._slots(payload([], live=live or {
                "state": "generating", "prefill_tok_s_mean": 5000.0,
                "slots": {"total": 3, "active": 2, "queue_depth": 0}}))

    def test_decoding_slot_carries_its_live_readings(self):
        ep = make_endpoint()
        s0 = self.scrape(ep)[0]
        self.assertTrue(s0["is_processing"])
        self.assertEqual(s0["state"], "decoding")
        self.assertEqual(s0["n_decoded"], 2139)
        self.assertEqual(s0["ctx_used"], 2214)
        # tok_s is the decode rate only while decoding.
        self.assertEqual(s0["tok_s"], 318.1)
        # kv_hit is the slot's own prefix reuse: 60/75, clamped to <= 1.
        self.assertAlmostEqual(s0["kv_hit"], 60 / 75)
        # ctx_ratio is used/n_ctx; a prefilling/decoding slot gets no global
        # prefill rate attributed to it (only a slot caught mid-read does).
        self.assertAlmostEqual(s0["ctx_ratio"], 2214 / 262144)
        self.assertIsNone(s0["prefill_tok_s"])
        # basalt extras ride along rather than being dropped.
        self.assertEqual(s0["ttft_s"], 0.2)
        self.assertEqual(s0["drafts_accepted"], 471)

    def test_idle_slot_keeps_prefix_and_blanks_the_rest(self):
        ep = make_endpoint()
        s1 = self.scrape(ep)[1]
        self.assertFalse(s1["is_processing"])
        self.assertEqual(s1["ctx_used"], 42711)
        # The engine fills live-request fields with zeros when idle; the card
        # must read "—" (None), not those zeros.
        self.assertIsNone(s1["n_decoded"])
        self.assertIsNone(s1["n_prompt_tokens"])
        self.assertIsNone(s1["tok_s"])
        self.assertIsNone(s1["kv_hit"])
        self.assertIsNone(s1["prefill_tok_s"])
        # Last request's draft counters survive on the idle row.
        self.assertEqual(s1["drafts_offered"], 668)

    def test_only_one_reading_slot_gets_the_global_prefill_rate(self):
        ep = make_endpoint()
        slots = self.scrape(ep)
        s2 = slots[2]  # the prefill slot
        self.assertEqual(s2["state"], "prefill")
        # tok_s is not a decode rate yet while reading its prompt.
        self.assertIsNone(s2["tok_s"])
        # Exactly one slot is prefilling, so the engine's one global prefill
        # rate belongs to it.
        self.assertEqual(s2["prefill_tok_s"], 5000.0)

    def test_prefill_rate_dropped_when_two_slots_read_at_once(self):
        ep = make_endpoint()
        dump = self.slots_dump()
        # Make slot 1 also a prefill reader -> the single-owner rule breaks.
        dump[1].update(is_processing=True, state="prefill", prompt_tokens=100)
        with mock.patch("collectors.basalt._get_json", return_value=dump):
            slots = ep._slots(payload([], live={
                "state": "generating", "prefill_tok_s_mean": 5000.0}))
        self.assertIsNone(slots[1]["prefill_tok_s"])
        self.assertIsNone(slots[2]["prefill_tok_s"])

    def test_kv_hit_clamps_when_reused_exceeds_prompt(self):
        ep = make_endpoint()
        dump = self.slots_dump()
        dump[0]["reused"] = 90  # > prompt_tokens 75: counter skew
        with mock.patch("collectors.basalt._get_json", return_value=dump):
            slots = ep._slots(payload([], live={"state": "generating"}))
        self.assertEqual(slots[0]["kv_hit"], 1.0)

    def model_detail(self, ep, dump=None, dump_error=None):
        """Drive the inherited _model_detail: /metrics+/props from the
        strata namespace, the /slots dump from the basalt one."""
        def slots_getter(*a, **kw):
            if dump_error:
                raise dump_error
            return dump if dump is not None else self.slots_dump()
        with mock.patch("collectors.strata._get_json", return_value=payload([])), \
                mock.patch("collectors.basalt._get_json", side_effect=slots_getter):
            return ep._model_detail("basalt-model", {}, time.time())

    def test_model_detail_counts_slots_from_the_rows(self):
        ep = make_endpoint()
        detail = self.model_detail(ep)
        self.assertEqual(detail["total_slots"], 3)
        self.assertEqual(len(detail["slots"]), 3)
        self.assertNotIn("slots_error", detail)

    def test_failed_scrape_names_the_reason(self):
        ep = make_endpoint()
        detail = self.model_detail(ep, dump_error=OSError("down"))
        self.assertEqual(detail["slots"], [])
        self.assertEqual(detail["slots_error"], "OSError: down")
        # The engine block still says how many slots exist...
        self.assertEqual(detail["total_slots"], 3)
        # ...and _series runs before the slot scrape on the same payload, so
        # the fallback number is in place even though _model_detail asked
        # for the engine block through the ring pass.


class Registration(unittest.TestCase):
    def test_kind_dispatches_to_the_basalt_collector(self):
        from monitor import ENDPOINT_KINDS
        self.assertIs(ENDPOINT_KINDS["basalt"], BasaltEndpoint)

    def test_discovery_is_off_a_hand_started_server(self):
        # A fixed-port hand-started server must never go hunting the .strata
        # instance directory for an app it was not started by.
        ep = make_endpoint()
        self.assertFalse(ep.discover)
