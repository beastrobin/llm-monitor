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
report, and the deliberate emptiness of the per-slot view.
"""

import time
import unittest

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

    def test_no_invented_slot_rows_but_the_slot_count_survives(self):
        ep = make_endpoint()
        ep._series(payload([]))  # caches the engine block
        self.assertEqual(ep._slots(payload([])), [])
        self.assertEqual(ep._engine_block.get("batch_slots"), 3)


class Registration(unittest.TestCase):
    def test_kind_dispatches_to_the_basalt_collector(self):
        from monitor import ENDPOINT_KINDS
        self.assertIs(ENDPOINT_KINDS["basalt"], BasaltEndpoint)

    def test_discovery_is_off_a_hand_started_server(self):
        # A fixed-port hand-started server must never go hunting the .strata
        # instance directory for an app it was not started by.
        ep = make_endpoint()
        self.assertFalse(ep.discover)
