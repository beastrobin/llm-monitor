"""The card on a fixed port follows the engine, not the config file.

basalt and MegaCapybara have taken turns on 8085 four times in one week, and the
two share no telemetry route at all: a card left built for the engine that used
to live there polls a route that answers 404, reports a healthy server as
offline, and blanks the whole model section.  These tests pin the self-heal --
and, just as much, that a card which answers is never touched.
"""

import copy
import http.client
import unittest
import urllib.error
from unittest.mock import patch

import monitor


def resident_config(kind):
    """A panel with the one card this is about, and nothing else polling."""
    config = copy.deepcopy(monitor.DEFAULT_CONFIG)
    for section in ("live", "stats", "gpu", "system"):
        config[section]["enabled"] = False
    config["endpoints"] = [
        {"id": "mc", "label": "Basalt", "kind": kind,
         "base_url": "http://127.0.0.1:59101", "api_key": ""},
    ]
    return config


def not_found(url):
    """A route this engine does not have."""
    return urllib.error.HTTPError(url, 404, "Not Found",
                                http.client.HTTPMessage(), None)


class ResidentKindTests(unittest.TestCase):
    def setUp(self):
        # The swap says itself in run.log -- and a test suite has no business
        # writing the operator's log, so the lines are captured here instead
        # and asserted on.
        self.logged = []
        patcher = patch.object(monitor, "log", self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hub(self, kind="megacapybara"):
        with patch.object(monitor, "hermes_config_candidates", return_value=[]):
            hub = monitor.Hub(resident_config(kind))
        self.addCleanup(hub._pool.shutdown)
        return hub

    def card(self, hub):
        return next(ep for ep in hub.endpoints if ep.id == "mc")

    def answers(self, hub, up):
        """Replace the polling so a cycle reports one verdict, no network."""
        for endpoint in hub.endpoints:
            endpoint.poll = (lambda endpoint=endpoint, up=up: dict(
                id=endpoint.id, label=endpoint.label,
                base_url=endpoint.base_url, up=up, models=[]))

    def fails(self, hub):
        """One cycle reporting the card down: the heal only starts from a failure
        it actually saw, so a card has to be allowed to fail once first."""
        self.answers(hub, up=False)
        hub.sample()

    # --- the swap itself ---------------------------------------------------

    def test_a_card_on_the_wrong_engine_is_rebuilt_for_the_one_answering(self):
        hub = self.hub("megacapybara")
        self.fails(hub)
        before = self.card(hub)
        with patch.object(monitor, "resident_kind_at", return_value="basalt"):
            hub.sample()
        after = self.card(hub)
        self.assertIsNot(after, before)
        self.assertIsInstance(after, monitor.BasaltEndpoint)
        self.assertEqual(after.kind, "basalt")
        # The swap changes the dialect, nothing else about the card: the id is
        # the ledger key and the address is what Hermes is deduped against.
        self.assertEqual(after.id, "mc")
        self.assertEqual(after.label, "Basalt")
        self.assertEqual(after.base_url, "http://127.0.0.1:59101")
        # And it says what it did, in the one place the operator reads.
        self.assertEqual([line for line in self.logged if "re-read" in line], [
            "mc: the engine on http://127.0.0.1:59101 is basalt, not "
            "megacapybara -- card re-read as kind=basalt (config.json still "
            "says megacapybara)"])

    def test_the_new_card_is_polled_in_the_same_cycle_that_builds_it(self):
        """A card built after the refresh must not wait a cycle to be asked."""
        hub = self.hub("megacapybara")
        self.fails(hub)
        polled = []
        real_build = monitor.build_endpoint

        def build(spec):
            card = real_build(spec)
            card.poll = lambda: (polled.append(card.kind) or dict(
                id=card.id, base_url=card.base_url, up=True, models=[]))
            return card

        with patch.object(monitor, "resident_kind_at", return_value="basalt"), \
                patch.object(monitor, "build_endpoint", build):
            hub.sample()
        self.assertEqual(polled, ["basalt"])

    def test_a_card_that_answers_is_never_asked_what_engine_it_is(self):
        """The heal may not re-label a working card, however confident it is."""
        hub = self.hub("basalt")
        self.answers(hub, up=True)
        with patch.object(monitor, "resident_kind_at",
                          side_effect=AssertionError("probed a card that answers")):
            for _ in range(3):
                hub.sample()
        self.assertEqual(self.card(hub).kind, "basalt")

    def test_an_engine_that_names_itself_neither_way_leaves_the_card_alone(self):
        """Nothing recognised means nothing changed -- an offline card stays offline.

        Guessing a dialect would book one engine's tokens under another's name.
        """
        hub = self.hub("basalt")
        self.fails(hub)
        before = self.card(hub)
        with patch.object(monitor, "resident_kind_at", return_value=None):
            hub.sample()
        self.assertIs(self.card(hub), before)

    def test_the_engine_already_assumed_does_not_rebuild_the_card(self):
        hub = self.hub("basalt")
        self.fails(hub)
        before = self.card(hub)
        with patch.object(monitor, "resident_kind_at", return_value="basalt"):
            hub.sample()
        self.assertIs(self.card(hub), before)
        self.assertEqual([line for line in self.logged if "re-read" in line], [])

    def test_the_question_is_asked_once_per_interval_while_the_card_is_down(self):
        hub = self.hub("basalt")
        self.fails(hub)
        asked = []
        with patch.object(monitor, "resident_kind_at",
                          side_effect=lambda ep: asked.append(1) or None):
            for _ in range(5):
                hub.sample()
        self.assertEqual(len(asked), 1)

    # --- naming the engine from its own documents --------------------------

    def kind_for(self, answers):
        """What ``resident_kind_at`` makes of a server answering ``answers``."""
        def get_json(url, api_key, timeout=None):
            for path, value in answers.items():
                if url.endswith(path):
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise not_found(url)

        card = monitor.build_endpoint({"id": "probe", "kind": "basalt",
                                       "base_url": "http://127.0.0.1:59101"})
        with patch.object(monitor, "_get_json", get_json):
            return monitor.resident_kind_at(card)

    def test_basalt_is_named_by_its_health_banner(self):
        self.assertEqual(self.kind_for({
            "/health": {"status": "ok", "service": "basalt", "loaded": True},
            "/stats": not_found("http://x/stats"),
        }), "basalt")

    def test_megacapybara_is_named_by_its_statistics_document(self):
        self.assertEqual(self.kind_for({
            "/health": not_found("http://x/health"),
            "/stats": {"decode_tps": 244.7, "prefill_tps": 536.0,
                       "max_concurrency": 4, "tokens_generated": 904500},
        }), "megacapybara")

    def test_llama_cpp_answering_health_is_not_mistaken_for_basalt(self):
        """llama.cpp answers /health too -- only basalt signs it."""
        self.assertIsNone(self.kind_for({
            "/health": {"status": "ok"},
        }))

    def test_an_unrelated_document_on_stats_is_not_megacapybara(self):
        """One shared key would be a coincidence, so the vocabulary decides."""
        self.assertIsNone(self.kind_for({
            "/stats": {"decode_tps": 12.0, "uptime_s": 4000},
        }))

    def test_a_closed_port_names_nothing(self):
        self.assertIsNone(self.kind_for({}))


if __name__ == "__main__":
    unittest.main()
