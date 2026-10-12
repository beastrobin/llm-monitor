"""The page needs more than samples: version, pid, privilege, power menu.

``config_block`` used to exist only inside the ``/api/snapshot`` handler, which
the browser asked for exactly once per load.  A restart pressed on the dashboard
reloads the page while the port is changing hands, so that single request can
fail -- and every later frame then renders without a menu: the power control
lost its buttons and looked like it had vanished (the "页面内重启后功率板丢失"
report).  These tests pin the shape the frontend depends on, including the fact
that it must survive JSON encoding for the SSE hello frame.
"""
import json
import unittest

import monitor


class _Endpoint(object):
    def __init__(self, ep_id, label, base_url):
        self.id = ep_id
        self.label = label
        self.base_url = base_url


class _Hub(object):
    """Just the attributes config_block reads (the real hub derives several of
    them per sample, so they are optional by nature)."""

    elevated = True
    power_settable = True
    power_presets: list = []
    power_applied_w = None

    def __init__(self):
        self.config = {"server": {"interval_seconds": 2.0}}
        self.endpoints = [_Endpoint("strata", "Strata", "http://127.0.0.1:8200")]
        self.elevated = True
        self.power_presets = [{"id": "quiet", "watts": 300.0, "tuned": True}]
        self.power_settable = True
        self.power_applied_w = 300.0


class ConfigBlockTests(unittest.TestCase):
    def setUp(self):
        self.block = monitor.config_block(_Hub())

    def test_carries_everything_the_power_row_asks_for(self):
        # Losing any one of these silently degrades one control or badge; the
        # frontend reads them by name and has no fallback to invent.
        for key in ("version", "pid", "interval_seconds", "endpoints",
                    "elevated", "power_presets", "power_settable",
                    "power_applied_w"):
            self.assertIn(key, self.block)
        self.assertEqual(self.block["version"], monitor.VERSION)
        self.assertEqual(
            [(e["id"], e["base_url"]) for e in self.block["endpoints"]],
            [("strata", "http://127.0.0.1:8200")])

    def test_json_safe_for_an_sse_frame(self):
        # The block now rides on stream frames too; anything json.dumps chokes
        # on (a NaN from a driver reading, an object it cannot serialize) would
        # take the whole connection down instead of one row.
        encoded = json.dumps({"snapshot": {"ts": 1}, "config": self.block})
        self.assertIn('"power_presets"', encoded)

    def test_reads_live_hub_state_not_constants(self):
        hub = _Hub()
        hub.elevated = False
        hub.power_settable = False
        hub.power_applied_w = None
        block = monitor.config_block(hub)
        self.assertFalse(block["elevated"])
        self.assertFalse(block["power_settable"])
        self.assertIsNone(block["power_applied_w"])


if __name__ == "__main__":
    unittest.main()
