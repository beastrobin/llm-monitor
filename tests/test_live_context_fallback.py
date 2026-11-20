"""Where the context card gets its number when the slot rows say nothing.

Under ``--batch`` a lone request goes back to the engine's solo path and books
nothing against a numbered slot, so every slot row reads ``idle`` with an empty
context for the entire request (``collectors/strata.py``'s
``_reported_slot_count`` documents the mechanism).  On 2026-11-21 that made the
context card read "0.0% · 空间就绪" while the engine was 195,759 prompt tokens
into a 262,144-token context with a 99.7% KV hit -- a confident zero for a
quantity that was three quarters full.

The engine's live block knows the prompt it is holding, so the collector exports
it as its own field and both the card headline and the history curve fall back to
it.  These tests hold the collector end of that rule.
"""

import unittest

from collectors.strata import _live_ctx_ratio

CTX = 262144


def live(**fields):
    state = {"state": "generating", "running": 1}
    state.update(fields)
    return state


class LiveContextRatio(unittest.TestCase):
    def test_a_solo_request_reports_the_prompt_it_holds(self):
        """The frame that printed 0.0%: 195,759 prompt + 812 generated."""
        ratio = _live_ctx_ratio(
            live(prompt_tokens=195759, generated=812), CTX)
        self.assertAlmostEqual(ratio, 196571 / CTX, places=6)

    def test_a_streamed_prompt_uses_the_total(self):
        """Mid-prefill ``prompt_tokens`` is the part read; the total is the prompt."""
        ratio = _live_ctx_ratio(
            live(state="reading", prompt_read=90000, prompt_total=180000), CTX)
        self.assertAlmostEqual(ratio, 180000 / CTX, places=6)

    def test_an_idle_engine_does_not_keep_the_last_prompt(self):
        """The live block outlives its request; an idle engine is not full."""
        self.assertIsNone(_live_ctx_ratio(
            {"state": "idle", "running": 0, "queued": 0,
             "prompt_tokens": 195759, "generated": 812}, CTX))

    def test_a_queued_request_counts_as_busy(self):
        self.assertIsNotNone(_live_ctx_ratio(
            {"state": "idle", "running": 0, "queued": 2,
             "prompt_tokens": 1000}, CTX))

    def test_no_prompt_means_no_claim(self):
        self.assertIsNone(_live_ctx_ratio(live(generated=40), CTX))

    def test_no_context_length_means_no_ratio(self):
        self.assertIsNone(_live_ctx_ratio(live(prompt_tokens=1000), None))

    def test_a_longer_prompt_than_the_context_cannot_exceed_one(self):
        """A frame caught between a context resize and its request."""
        self.assertEqual(_live_ctx_ratio(live(prompt_tokens=CTX * 2), CTX), 1.0)


if __name__ == "__main__":
    unittest.main()
