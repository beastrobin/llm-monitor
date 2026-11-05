"""Per-card power state: menus, indices and where a write lands.

The control used to assume one card.  A second GPU makes "the power limit"
ambiguous in three places that all fail silently if they get it wrong:

* the menu -- offering card 0's presets on card 1 is a write the driver
  clamps or refuses, and the panel then shows a tier nobody picked;
* the index -- a write that reaches the wrong card looks like a success and
  changes a limit the user did not touch;
* the refusal -- one laptop GPU that rejects software limits must not take
  the desktop card's buttons away.

These tests drive ``Hub.set_power_limit`` on a bare instance (no collectors,
no sockets) with ``power.set_limit`` patched out, so the assertions are about
routing and state, not about nvidia-smi being installed.
"""
import threading
import unittest

import monitor


def _card(index, low, high):
    return {"index": str(index), "power_min_w": low, "power_max_w": high}


def _bare_hub(cards=None):
    """A Hub with only the attributes the write path touches."""
    hub = object.__new__(monitor.Hub)
    hub.power_by_gpu = {}
    hub._mutex = threading.Lock()
    hub._snapshot = {"gpu": {"gpus": list(cards or [])}}
    hub._wake_cond = threading.Condition()
    hub._woken_gen = 0
    if cards:
        hub._refresh_power_menus(cards)
    return hub


class GpuIndexTests(unittest.TestCase):
    def test_accepts_the_text_the_collector_reports(self):
        # The collector reads the column as str, a fixture may hold an int.
        self.assertEqual(monitor.gpu_index({"index": "1"}), 1)
        self.assertEqual(monitor.gpu_index({"index": 1}), 1)
        self.assertEqual(monitor.gpu_index({"index": " 2 "}), 2)

    def test_falls_back_to_position_when_the_column_is_gone(self):
        # A fallback query rung drops the column; the card still has to be
        # addressable, and position is all that is left.
        self.assertEqual(monitor.gpu_index({}, 3), 3)
        self.assertEqual(monitor.gpu_index({"index": None}, 1), 1)
        self.assertEqual(monitor.gpu_index({"index": "n/a"}, 2), 2)


class MenuTests(unittest.TestCase):
    def test_each_card_gets_its_own_menu(self):
        # Two different windows must not share one menu: the derived menu is
        # 40/60/80/100 % of the card's own maximum.  The tuned menu grew to six
        # rungs on 2026-11-03 (150 W and 200 W added at the user's request), so
        # a card that spans 150-600 offers all six and the 175 W laptop still
        # gets its own four.
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 5.0, 175.0)])
        self.assertEqual([p["watts"] for p in hub.power_by_gpu[0]["presets"]],
                         [150, 200, 300, 400, 500, 600])
        self.assertEqual([p["watts"] for p in hub.power_by_gpu[1]["presets"]],
                         [70, 105, 140, 175])

    def test_the_new_low_rungs_follow_each_cards_floor(self):
        # 150 W is the floor of the card this was measured on, so the bottom
        # two rungs are filtered by each card's own window: a card whose
        # minimum is 250 W must lose them rather than offer a value the driver
        # silently clamps onto something nobody picked.
        hub = _bare_hub([_card(0, 250.0, 600.0)])
        self.assertEqual([p["watts"] for p in hub.power_by_gpu[0]["presets"]],
                         [300, 400, 500, 600])

    def test_a_sample_with_no_cards_leaves_the_menus_alone(self):
        # A transient nvidia-smi failure reports zero cards; blanking the menu
        # there would hide the control until the next good sample.
        hub = _bare_hub([_card(0, 100.0, 600.0)])
        before = [p["watts"] for p in hub.power_by_gpu[0]["presets"]]
        hub._refresh_power_menus([])
        self.assertEqual([p["watts"] for p in hub.power_by_gpu[0]["presets"]],
                         before)

    def test_a_card_that_disappears_takes_its_state_with_it(self):
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 100.0, 600.0)])
        self.assertEqual(sorted(hub.power_by_gpu), [0, 1])
        hub._refresh_power_menus([_card(0, 100.0, 600.0)])
        self.assertEqual(sorted(hub.power_by_gpu), [0])

    def test_the_primary_card_view_survives_as_a_property(self):
        # The ledger and the power card still read the single-card names.
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 5.0, 175.0)])
        self.assertEqual([p["watts"] for p in hub.power_presets],
                         [150, 200, 300, 400, 500, 600])
        self.assertTrue(hub.power_settable)
        self.assertIsNone(hub.power_applied_w)


class WriteRoutingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = monitor.power.set_limit

        def fake_set_limit(watts, index=0, presets=None):
            self.calls.append({"watts": watts, "index": index,
                               "presets": presets})
            return {"ok": True, "applied": watts, "previous": None}

        monitor.power.set_limit = fake_set_limit

    def tearDown(self):
        monitor.power.set_limit = self.original

    def test_the_index_and_that_cards_menu_reach_the_driver(self):
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 5.0, 175.0)])
        result = hub.set_power_limit(105, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["index"], 1)
        self.assertEqual(self.calls[0]["index"], 1)
        # The 175 W menu, not the 600 W one -- this is the clamp trap.
        self.assertEqual([p["watts"] for p in self.calls[0]["presets"]],
                         [70, 105, 140, 175])

    def test_an_unknown_card_is_refused_before_the_driver_is_asked(self):
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 100.0, 600.0)])
        result = hub.set_power_limit(400, 7)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_index")
        self.assertEqual(self.calls, [])

    def test_a_negative_index_is_refused(self):
        hub = _bare_hub([_card(0, 100.0, 600.0)])
        self.assertEqual(hub.set_power_limit(400, -1)["error"], "bad_index")
        self.assertEqual(self.calls, [])

    def test_applied_state_is_tracked_per_card(self):
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 100.0, 600.0)])
        hub.set_power_limit(300, 1)
        self.assertEqual(hub.power_by_gpu[1]["applied_w"], 300)
        self.assertIsNone(hub.power_by_gpu[0]["applied_w"])
        # The scalar view is card 0's, which nothing has written.
        self.assertIsNone(hub.power_applied_w)

    def test_a_refusal_only_marks_the_card_that_refused(self):
        hub = _bare_hub([_card(0, 100.0, 600.0), _card(1, 5.0, 175.0)])

        def refuse(watts, index=0, presets=None):
            return {"ok": False, "error": "unsupported"}

        monitor.power.set_limit = refuse
        hub.set_power_limit(105, 1)
        self.assertFalse(hub.power_by_gpu[1]["settable"])
        self.assertTrue(hub.power_by_gpu[0]["settable"])


if __name__ == "__main__":
    unittest.main()
