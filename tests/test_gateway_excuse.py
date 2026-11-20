"""A gateway's excuse belongs to the card, not to the dialect the card speaks.

The engine behind a fixed port changes hands here: basalt, MegaCapybara and
Strata have all answered :8085, and the card stays configured for whichever
dialect the port last spoke.  The first version of this feature hung the excuse
off ``MegaCapybaraEndpoint``, and the live card -- built for Strata, pointed at
the gateway -- came back with ``error_detail: null`` while the gateway was
answering its status route happily.  Dialect is the wrong place to look.

The distinction is the whole point: ``HTTP 502 Bad Gateway`` names the address the
panel polls, and the thing that is actually down is behind it.  That sentence, in
the gateway's own words, is what turns "the card is offline" into "8185 is nobody,
and nothing answered the search over 8185-8190".
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from collectors.llama import LlamaEndpoint
from collectors.strata import StrataEndpoint

STATUS = {
    "ok": False,
    "upstream": "http://127.0.0.1:8185",
    "lastError": "upstream http://127.0.0.1:8185 is down and no engine "
                 "answers on 8185-8190",
}


class Gateway(BaseHTTPRequestHandler):
    """Refuses everything except its own status route, and counts those answers."""

    status_hits = 0
    serve_status = True

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        if self.path.startswith("/engine/status") and type(self).serve_status:
            body = json.dumps(STATUS).encode("utf-8")
            type(self).status_hits += 1
            code = 200
        else:
            body = b"bad gateway"
            code = 502
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silent during the suite
        pass


class GatewayExcuse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Gateway)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Gateway.status_hits = 0
        Gateway.serve_status = True

    def card(self, kind):
        # ``discover: false`` for the same reason the gateway card on this box
        # carries it: a Strata-dialect card left to itself follows the newest
        # engine record on the machine, and the test needs it to stay pointed at
        # the gateway whose excuse is under test.
        return kind({"id": "gw", "label": "Gateway", "base_url": self.base,
                     "discover": False})

    def test_the_strata_card_hears_the_gateway(self):
        """The card this box actually runs: Strata dialect, gateway address."""
        out = self.card(StrataEndpoint).poll()
        self.assertFalse(out["up"])
        self.assertEqual(out["error"], "HTTP 502 Bad Gateway")
        self.assertIn("no engine answers on 8185-8190",
                      out.get("error_detail") or "")

    def test_the_plain_llama_card_hears_it_too(self):
        self.assertIn("no engine answers on 8185-8190",
                      (self.card(LlamaEndpoint).poll().get("error_detail") or ""))

    def test_asked_once_per_interval_not_once_per_sample(self):
        """A down card backs off; the excuse must not be a second thing to knock on."""
        card = self.card(LlamaEndpoint)
        card.poll()
        card.poll()
        card.poll()
        self.assertEqual(Gateway.status_hits, 1)

    def test_a_server_with_no_such_route_costs_a_none(self):
        Gateway.serve_status = False
        out = self.card(LlamaEndpoint).poll()
        self.assertFalse(out["up"])
        self.assertIsNone(out.get("error_detail"))

    def test_a_moved_card_asks_the_server_it_landed_on(self):
        """An excuse read from one server is not carried to the next."""
        card = self.card(LlamaEndpoint)
        card.poll()
        self.assertEqual(Gateway.status_hits, 1)
        card.set_base_url("http://127.0.0.1:1")     # nothing answers here
        card.poll()
        self.assertEqual(Gateway.status_hits, 1, "换地址后还留着上家的说法")
        card.set_base_url(self.base)
        card.poll()
        self.assertEqual(Gateway.status_hits, 2)


if __name__ == "__main__":
    unittest.main()
