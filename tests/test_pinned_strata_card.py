"""A card an operator pinned must say so, instead of just going blank.

The desktop app hands its engine a fresh port and a fresh key with every model
load, so the Strata card follows it (see ``test_strata_follow.py``).  Setting
``"discover": false`` switches that following off -- the opt-out exists for a
server reached through a jump box -- and then the card polls the address in
``config.json`` come what may.  On 2026-11-20 that is exactly what happened by
accident: the address in config was the app's own web host, which answers
``/health`` with its single-page app and ``/v1/models`` with 401, and the engine
was living on another port entirely.  The card reported offline, ``run.log``
carried no clue that following was switched off, and the whole thing read as
"the panel lost the model again when I switched versions".

So the flag is asserted two ways: the card knows it is pinned (and the basalt
card, which pins itself by construction rather than by anyone's config, does
not claim it), and a pinned card really does stay put while its unpinned twin
moves.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from collectors.basalt import BasaltEndpoint
from collectors.strata import StrataEndpoint

APP = "http://127.0.0.1:47831"
ENGINE = "http://127.0.0.1:18500"
KEY = "b" * 64
RECORD = {
    "base_url": ENGINE,
    "api_key": KEY,
    "path": Path("deadbeef-0000-0000-0000-000000000000.json"),
}


def card(cls=StrataEndpoint, **spec):
    spec.setdefault("id", "basalt" if cls is BasaltEndpoint else "strata")
    spec.setdefault("kind", "basalt" if cls is BasaltEndpoint else "strata")
    spec.setdefault("base_url", APP + "/v1")
    return cls(spec)


class MovingEngine(object):
    """The app's web host at the configured address, an engine on the record's."""

    def __enter__(self):
        self.patchers = [
            # Source 1: this address is not an engine (a web page is not one).
            mock.patch("collectors.strata.engine_health", return_value=None),
            # Sources 2 and 3: the newest record names an engine, and it answers.
            mock.patch("collectors.strata.instance_files",
                       return_value=[RECORD["path"]]),
            mock.patch("collectors.strata.read_instance",
                       return_value=dict(RECORD)),
            mock.patch("collectors.strata.is_engine", return_value=True),
        ]
        for patcher in self.patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self.patchers:
            patcher.stop()


class PinnedFlag(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_config_pin_is_reported(self):
        """The setting an operator made is the one the card admits to."""
        c = card(instance_dir=self.dir.name, discover=False)
        self.assertTrue(c.pinned)

    def test_default_follows_and_says_nothing(self):
        """No ``discover`` key is the normal card: it follows, so it stays quiet."""
        self.assertFalse(card(instance_dir=self.dir.name).pinned)

    def test_explicit_yes_follows(self):
        self.assertFalse(card(instance_dir=self.dir.name, discover=True).pinned)

    def test_basalt_card_never_claims_a_pin(self):
        """A basalt card cannot follow anything by design, so it has nothing to admit.

        ``BasaltEndpoint`` sets ``discover`` off in its own constructor; printing
        "pinned" for every basalt card would train the reader to ignore the line.
        """
        c = card(cls=BasaltEndpoint, kind="basalt", instance_dir=self.dir.name)
        self.assertFalse(c.discover)
        self.assertFalse(c.pinned)


class PinDoesWhatItSays(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_pinned_card_stays_on_the_address_it_was_given(self):
        with MovingEngine():
            c = card(instance_dir=self.dir.name, discover=False)
            self.assertFalse(c.follow_now(force=True))
            self.assertEqual(c.base_url, APP)

    def test_the_same_card_without_the_pin_moves(self):
        """The only difference between the two cards is the setting.

        Without this the flag would be decoration: it has to be the reason the
        card above stayed, not some second rule nobody wrote down.
        """
        with MovingEngine():
            c = card(instance_dir=self.dir.name)
            self.assertTrue(c.follow_now(force=True))
            self.assertEqual(c.base_url, ENGINE)
            self.assertEqual(c.api_key, KEY)


if __name__ == "__main__":
    unittest.main()
