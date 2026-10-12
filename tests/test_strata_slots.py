"""The per-slot trend series, and the slot card readings behind them.

Two shapes have to survive both collector dialects: a busy slot (state,
tok_s and elapsed_s present) and an idle one (held_tokens only, everything
else null). The panel spends most of its time in the second shape, so a
change that only ever exercises the first would pass here and then show a
row of blanks on the dashboard.

The series keys are the other half of the contract. They are built from the
slot **id**, not its position in the list, so a curve stays attached to its
slot when the engine renumbers or drops one out of the batch -- and the
frontend addresses a row by exactly that id.

The two attributed readings -- the slot's prefill rate and its prefix hit --
are tested as a sequence of scrapes, because that is what produces them: the
engine reports neither per slot, and the collector derives them from what the
slot held on the *previous* frame.
"""

import time
import unittest
from unittest import mock

import monitor
from collectors.strata import StrataEndpoint, _reported_slot_count

BUSY_SLOT = {
    'slot': 0, 'state': 'decoding', 'prompt_tokens': 36287, 'generated': 1582,
    'elapsed_s': 1.4, 'tok_s': 1498.3,
}
READING_SLOT = {
    'slot': 1, 'state': 'reading', 'prompt_tokens': 67489, 'generated': 0,
    'elapsed_s': 1.1, 'tok_s': None,
}
IDLE_SLOT = {'slot': 2, 'state': 'idle', 'held_tokens': 67989}

CONTEXT = 524288


def payload(slots, context=CONTEXT):
    """One /metrics document carrying the given live.slots."""
    return {'engine': {'context': context}, 'live': {'slots': slots}}


def endpoint():
    return StrataEndpoint({'id': 'strata', 'label': 'Strata',
                           'base_url': 'http://127.0.0.1:8200'})


class StrataSlotTests(unittest.TestCase):
    def test_busy_slot_reports_rate_and_age(self):
        slot = endpoint()._slots(payload([BUSY_SLOT]))[0]
        self.assertEqual(slot['id'], 0)
        self.assertTrue(slot['is_processing'])
        self.assertEqual(slot['state'], 'decoding')
        self.assertEqual(slot['tok_s'], 1498.3)
        self.assertEqual(slot['elapsed_s'], 1.4)
        # A decoding slot is not reading a prompt, so no prefill rate belongs
        # to it, and no previous frame left a prefix for it: both None.
        self.assertIsNone(slot['prefill_tok_s'])
        self.assertIsNone(slot['kv_hit'])
        self.assertEqual(slot['n_prompt_tokens'], 36287)
        self.assertEqual(slot['n_decoded'], 1582)
        # A busy slot is measured as prompt + generated, the same total the
        # context card has always meant by "what this slot is holding".
        self.assertEqual(slot['ctx_used'], 36287 + 1582)
        self.assertAlmostEqual(slot['ctx_ratio'], (36287 + 1582) / CONTEXT, places=6)

    def test_reading_slot_has_no_rate_yet(self):
        # tok_s is generated / seconds since the first token, and there has
        # been no first token, so the engine reports null rather than 0.
        slot = endpoint()._slots(payload([READING_SLOT]))[0]
        self.assertTrue(slot['is_processing'])
        self.assertIsNone(slot['tok_s'])
        self.assertEqual(slot['elapsed_s'], 1.1)
        self.assertEqual(slot['ctx_used'], 67489)

    def test_idle_slot_keeps_held_tokens_and_reports_no_rates(self):
        # The idle branch of slots_view() has exactly one length to give, so
        # every rate-shaped field stays None. Null, not 0: the card shows an
        # em dash, and a zero would read like a real measurement.
        slot = endpoint()._slots(payload([IDLE_SLOT]))[0]
        self.assertFalse(slot['is_processing'])
        self.assertEqual(slot['state'], 'idle')
        self.assertEqual(slot['ctx_used'], 67989)
        self.assertIsNone(slot['tok_s'])
        self.assertIsNone(slot['elapsed_s'])
        self.assertIsNone(slot['n_prompt_tokens'])
        self.assertIsNone(slot['n_decoded'])
        self.assertIsNone(slot['n_prompt_tokens_processed'])
        self.assertIsNone(slot['n_prompt_tokens_cache'])

    def test_single_request_falls_back_to_the_live_block(self):
        # With no batch there are no slots; the one request in flight is
        # reported on `live` itself, and it still has to become slot 0.
        slots = endpoint()._slots({
            'engine': {'context': CONTEXT},
            'live': {'state': 'generating', 'prompt_tokens': 1000, 'generated': 40},
        })
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]['id'], 0)
        self.assertEqual(slots[0]['ctx_used'], 1040)


class SlotAttributionTests(unittest.TestCase):
    """The two readings the engine does not report per slot.

    Both come from a *sequence* of scrapes, so each test drives the same
    endpoint object twice -- a fresh endpoint per call would test nothing.
    """

    def payload(self, slots, prefill=None):
        return {'engine': {'context': CONTEXT},
                'live': {'slots': slots, 'prefill_tok_s_mean': prefill}}

    def test_prefill_belongs_to_the_one_slot_reading(self):
        ep = endpoint()
        # The engine reads one prompt at a time, so with exactly one slot in
        # `reading` the global rate is that slot's.
        rows = self.payload([READING_SLOT, IDLE_SLOT], prefill=1191.8)
        by_id = {s['id']: s for s in ep._slots(rows)}
        self.assertEqual(by_id[1]['prefill_tok_s'], 1191.8)
        self.assertIsNone(by_id[2]['prefill_tok_s'])

    def test_prefill_is_dropped_when_two_slots_read_at_once(self):
        # Ambiguous: the rate cannot be split, so nothing is claimed.
        ep = endpoint()
        second = dict(READING_SLOT, slot=2)
        by_id = {s['id']: s for s in
                 ep._slots(self.payload([READING_SLOT, second], prefill=1191.8))}
        self.assertIsNone(by_id[1]['prefill_tok_s'])
        self.assertIsNone(by_id[2]['prefill_tok_s'])

    def test_prefill_absent_when_engine_reports_no_rate(self):
        ep = endpoint()
        by_id = {s['id']: s for s in ep._slots(self.payload([READING_SLOT]))}
        self.assertIsNone(by_id[1]['prefill_tok_s'])

    def test_kv_hit_is_the_prefix_the_slot_held_when_it_turns_busy(self):
        ep = endpoint()
        # Frame 1: slot 0 idle, holding a 69,417 token conversation.
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 69417}]))
        # Frame 2: the same slot takes a 70,000 token prompt. The engine's
        # pick_slot reuses exactly that held prefix, so the slot's own hit
        # rate is 69417 / 70000.
        busy = {'slot': 0, 'state': 'decoding', 'prompt_tokens': 70000,
                'generated': 12, 'elapsed_s': 3.0, 'tok_s': 210.0}
        slot = ep._slots(self.payload([busy]))[0]
        self.assertAlmostEqual(slot['kv_hit'], 69417 / 70000, places=6)

    def test_kv_hit_survives_the_frames_of_one_request(self):
        # The hit is a property of the request, not of the scrape: it must not
        # flicker to None (or recompute) while the slot's counters move.
        ep = endpoint()
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 69417}]))
        first = {'slot': 0, 'state': 'decoding', 'prompt_tokens': 70000,
                 'generated': 1, 'elapsed_s': 1.0, 'tok_s': 1.0}
        later = dict(first, generated=500, elapsed_s=4.0, tok_s=166.7)
        self.assertEqual(ep._slots(self.payload([later]))[0]['kv_hit'],
                         ep._slots(self.payload([first]))[0]['kv_hit'])

    def test_kv_hit_cannot_exceed_the_prompt_it_matched(self):
        # A slot's held prefix is at most as long as the prompt it was matched
        # against. held > prompt means a different conversation arrived, so the
        # slot contributed nothing -- a hit rate above 100% is not a rate.
        ep = endpoint()
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 69417}]))
        fresh = {'slot': 0, 'state': 'decoding', 'prompt_tokens': 30000,
                 'generated': 12, 'elapsed_s': 3.0, 'tok_s': 210.0}
        slot = ep._slots(self.payload([fresh]))[0]
        self.assertEqual(slot['kv_hit'], 0.0)

    def test_kv_hit_is_none_for_a_slot_that_held_nothing(self):
        ep = endpoint()
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 0}]))
        busy = {'slot': 0, 'state': 'decoding', 'prompt_tokens': 70000,
                'generated': 12, 'elapsed_s': 3.0, 'tok_s': 210.0}
        self.assertIsNone(ep._slots(self.payload([busy]))[0]['kv_hit'])

    def test_a_new_prompt_replaces_the_previous_turns_hit(self):
        ep = endpoint()
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 69417}]))
        first = {'slot': 0, 'state': 'decoding', 'prompt_tokens': 70000,
                 'generated': 12, 'elapsed_s': 3.0, 'tok_s': 210.0}
        ep._slots(self.payload([first]))
        # The request ends, the slot keeps a longer prefix, and the next turn
        # is measured against THAT -- not the stale one.
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 70100}]))
        second = {'slot': 0, 'state': 'decoding', 'prompt_tokens': 71000,
                  'generated': 5, 'elapsed_s': 2.0, 'tok_s': 200.0}
        slot = ep._slots(self.payload([second]))[0]
        self.assertAlmostEqual(slot['kv_hit'], 70100 / 71000, places=6)

    def test_slots_the_engine_stops_reporting_are_forgotten(self):
        # Shrinking --batch: an id must not come back wearing another
        # conversation's prefix.
        ep = endpoint()
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 69417},
                                {'slot': 1, 'state': 'idle',
                                  'held_tokens': 833}]))
        ep._slots(self.payload([{'slot': 0, 'state': 'idle',
                                  'held_tokens': 69417}]))
        busy = {'slot': 1, 'state': 'decoding', 'prompt_tokens': 5000,
                'generated': 1, 'elapsed_s': 1.0, 'tok_s': 1.0}
        self.assertIsNone(ep._slots(self.payload([busy]))[0]['kv_hit'])


class SlotSeriesTests(unittest.TestCase):
    """The flattened series the slot cards plot."""

    def flatten(self, slots):
        return monitor.Hub._flatten(monitor.Hub.__new__(monitor.Hub), {
            'gpu': {'gpus': []},
            'endpoints': [{
                'id': 'strata',
                'models': [{'id': 'm', 'metrics': {}, 'slots': slots}],
            }],
        })

    def test_one_series_per_slot_and_per_reading(self):
        values = self.flatten(endpoint()._slots(payload([BUSY_SLOT, IDLE_SLOT])))
        base = 'strata/m'
        self.assertEqual(values[base + '.slot0.ctx'], 36287 + 1582)
        self.assertEqual(values[base + '.slot0.ratio'], (36287 + 1582) / CONTEXT)
        self.assertEqual(values[base + '.slot0.prompt'], 36287)
        self.assertEqual(values[base + '.slot0.decoded'], 1582)
        self.assertEqual(values[base + '.slot0.tok_s'], 1498.3)
        self.assertEqual(values[base + '.slot0.elapsed'], 1.4)
        # The two attributed readings are flattened too, and are None for a
        # slot the attribution cannot name.
        self.assertIsNone(values[base + '.slot0.prefill'])
        self.assertIsNone(values[base + '.slot0.kv'])
        self.assertIsNone(values[base + '.slot2.prefill'])
        self.assertIsNone(values[base + '.slot2.kv'])
        # The headline context card is the max across slots and stays one
        # series; the per-slot ones are additional, not a replacement.
        self.assertEqual(values[base + '.ctx'], max((36287 + 1582) / CONTEXT,
                                                    67989 / CONTEXT))
        # An idle slot still plots its held length, and still plots nothing
        # for the readings the engine does not report.
        self.assertEqual(values[base + '.slot2.ctx'], 67989)
        self.assertIsNone(values[base + '.slot2.tok_s'])
        self.assertIsNone(values[base + '.slot2.decoded'])

    def test_series_keys_follow_the_slot_id_not_its_position(self):
        # Same slot, reported first in one scrape and last in the next. The
        # curve has to stay on .slot0 both times, or it jumps to another
        # slot's card whenever the engine reorders the batch.
        first = self.flatten(endpoint()._slots(payload([BUSY_SLOT, IDLE_SLOT])))
        second = self.flatten(endpoint()._slots(payload([IDLE_SLOT, BUSY_SLOT])))
        self.assertEqual(first['strata/m.slot0.tok_s'], second['strata/m.slot0.tok_s'])
        self.assertEqual(first['strata/m.slot2.ctx'], second['strata/m.slot2.ctx'])

    def test_idle_readings_stay_null_rather_than_zero(self):
        # A zero would plot as a dive to the floor on every idle scrape; null
        # leaves the sample out, which is what the curve is supposed to show.
        values = self.flatten(endpoint()._slots(payload([IDLE_SLOT])))
        self.assertIsNone(values['strata/m.slot2.tok_s'])
        self.assertIsNone(values['strata/m.slot2.elapsed'])
        self.assertIsNone(values['strata/m.slot2.prompt'])

    def test_attributed_readings_reach_the_series(self):
        # A reading the collector derives is worth nothing if the hub does not
        # flatten it: the card would sit on an empty curve forever.
        ep = endpoint()
        ep._slots({'engine': {'context': CONTEXT}, 'live': {'slots': [
            {'slot': 1, 'state': 'idle', 'held_tokens': 60000},
            IDLE_SLOT]}})
        rows = {'engine': {'context': CONTEXT}, 'live': {
            'slots': [READING_SLOT, IDLE_SLOT], 'prefill_tok_s_mean': 1191.8}}
        values = self.flatten(ep._slots(rows))
        self.assertEqual(values['strata/m.slot1.prefill'], 1191.8)
        self.assertAlmostEqual(values['strata/m.slot1.kv'], 60000 / 67489, places=6)


class SlotPathShapeTests(unittest.TestCase):
    """The shape of a frame where nothing is booked against a numbered slot.

    A lone request under --batch runs on the engine's single-request path, which
    reports no per-slot row at all (measured live: every frame of one chat
    completion leaves `live.slots` all idle with frozen held_tokens, while two
    concurrent completions turn the rows reading/decoding).  The panel explains
    that state instead of showing six cards of em-dashes -- and it can only do so
    if the scrape keeps both numbers the explanation needs: how many slots the
    engine runs, and that a request really is in flight.
    """

    def test_rows_give_the_count_and_parallel_is_the_fallback(self):
        self.assertEqual(_reported_slot_count({'parallel': 2},
                                              [{'slot': 0}, {'slot': 1}]), 2)
        # No rows this frame is not "no slots": without the fallback the UI has
        # nothing to say about its own empty section.
        self.assertEqual(_reported_slot_count({'parallel': 2}, []), 2)
        self.assertIsNone(_reported_slot_count({}, []))

    def test_running_survives_a_frame_without_slot_rows(self):
        ep = endpoint()
        doc = {'engine': {'context': CONTEXT},
               'live': {'state': 'generating', 'phase': 'thinking',
                        'running': 1, 'parallel': 2}}
        with mock.patch('collectors.strata._get_json', return_value=doc):
            detail = ep._model_detail('m', {}, time.time())
        self.assertEqual(detail['total_slots'], 2)
        self.assertEqual(detail['slots'], [])
        self.assertEqual(detail['live_state']['running'], 1)


if __name__ == '__main__':
    unittest.main()
