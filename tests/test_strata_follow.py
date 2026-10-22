"""Finding the engine the desktop app started, instead of the address it used
to be at.

The app is a proxy with a web UI in front of a ``serve/server.py`` process it
starts on a **fresh port**, guarded by a **fresh key**, every time a model is
loaded.  Two failure shapes come out of that and both read as "Strata has no
model" on the dashboard: the recorded port is free (connection refused), or a
live server answers on it with HTML, because the address belongs to the app and
the app owns ``/`` (JSONDecodeError).

So the tests below are mostly about *which address the card ends up polling*,
and about the two cases where following would be worse than staying put: a
hand-started server that is exactly where config.json says it is, and a
half-started engine that answers nothing yet.

The app address is asserted alongside the engine address, not as an
afterthought: the dashboard hides the hermes card when another card already
covers the address Hermes names, and that only works while the card knows it is
standing in for a second address.
"""

import json
import os
import time
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from collectors.strata import StrataEndpoint, without_api_suffix

APP = "http://127.0.0.1:47831"
ENGINE = "http://127.0.0.1:18500"
OTHER = "http://127.0.0.1:19999"
KEY = "a" * 64
INSTANCE = "36be1ff4-be8f-46e7-a65d-aa19ba913a15"
OTHER_INSTANCE = "11111111-2222-3333-4444-555555555555"

HTML = "<!doctype html><html><head><title>Strata</title></head></html>"


class Servers(object):
    """Answers the way the app and an engine do, and records every ask.

    Only the shapes that matter here are real: a Strata engine's ``/health``
    (which the engine answers to anyone, and which says ``api_key: true`` to mean
    everything else is locked), the app's ``/v1/models`` (the one JSON thing an
    app address answers), and HTML for anything else -- including an app's
    ``/health``, which is why identifying the app by its health check is not an
    option.
    """

    def __init__(self, engine=ENGINE, key=KEY, app=APP, instance=INSTANCE,
                 up=True, app_up=True):
        self.engine, self.key, self.app = engine, key, app
        self.instance, self.up, self.app_up = instance, up, app_up
        self.calls = []

    def __call__(self, url, api_key="", timeout=2.0):
        self.calls.append((url, api_key))
        if url == self.app + "/v1/models":
            if not self.app_up:
                raise ConnectionRefusedError("refused")
            return {"data": [{"id": "unsloth-ud-iq4_xs", "object": "model",
                              "owned_by": "strata",
                              "meta": {"instanceId": self.instance,
                                       "label": "Qwen3.8 (Unsloth) UD-IQ4_XS"}}]}
        for base in (self.engine, OTHER):
            if not url.startswith(base):
                continue
            if url == base + "/health":
                if not self.up:
                    raise ConnectionRefusedError("refused")
                return {"status": "ok", "service": "strata", "loaded": True,
                        "model": "qwen3.8-flash-next", "max_context": 524288,
                        "api_key": True}
            if api_key != self.key:
                # The counters and the model list sit behind the key that came
                # with the process; the health check above does not.  A real
                # HTTPError, so the card's sentence is the one the dashboard
                # shows rather than a Python type name.
                raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)
            if url.endswith("/metrics"):
                return {"engine": {"context": 524288}, "live": {"running": 0},
                        "totals": {}}
            if url.endswith("/props"):
                return {"total_slots": 1, "build_info": "Strata 0.1.45",
                        "model_path": "D:/models/qwen3.8.gguf"}
            if url.endswith("/slots"):
                return []
            if url.endswith("/v1/models"):
                return {"data": [{"id": "qwen3.8-flash-next", "object": "model",
                                  "status": {"value": "loaded", "args": []}}]}
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def asked(self, url):
        return [call for call in self.calls if call[0] == url]


def record(port, key=KEY, model="qwen3.8-flash-next"):
    return {"host": "127.0.0.1", "port": port, "api_key": key,
            "model_name": model, "args": ["--mtp", "D:/mtp"]}


class FollowTests(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        # The environment must not decide where these look: the dashboard runs
        # under a launcher that may set the variable.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("STRATA_INSTANCE_DIR", None)

    def endpoint(self, base_url=APP + "/v1", servers=None, **spec):
        spec.setdefault("id", "strata")
        spec.setdefault("kind", "strata")
        spec["base_url"] = base_url
        spec["instance_dir"] = str(self.dir)
        endpoint = StrataEndpoint(spec)
        if servers is not None:
            # Both modules are patched: the follow logic asks through
            # ``strata._get_json``, the scrape through ``llama._get_json``, and a
            # test that polls needs the same server behind both.
            for module in ("collectors.strata", "collectors.llama"):
                patcher = mock.patch(module + "._get_json", servers)
                patcher.start()
                self.addCleanup(patcher.stop)
        return endpoint

    def write(self, name, body, age=0.0):
        path = self.dir / (name + ".json")
        path.write_text(json.dumps(body), encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    # -- the address ------------------------------------------------------

    def test_app_address_leads_to_the_engine_behind_it(self):
        self.write(INSTANCE, record(18500))
        servers = Servers()
        endpoint = self.endpoint(servers=servers)
        self.assertTrue(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, ENGINE)
        self.assertEqual(endpoint.api_key, KEY)
        # And the app address survives as the one this card stands for, so the
        # dashboard does not open a second card -- and a second ledger row --
        # for the same machine.
        self.assertEqual(endpoint.proxied_url, APP)
        self.assertTrue(servers.asked(APP + "/v1/models"))

    def test_the_suffix_a_client_pastes_is_dropped(self):
        # Hermes, the app's own docs and a copy-paste all write host:port/v1.
        # Collectors append their own paths to the root, so keeping the suffix
        # asks for /v1/props and reads as a server with nothing loaded.
        self.assertEqual(self.endpoint(APP + "/v1/").base_url, APP)
        self.assertEqual(self.endpoint(APP).base_url, APP)
        self.assertEqual(without_api_suffix("http://h:1/V1"), "http://h:1")

    def test_a_key_that_no_longer_unlocks_the_port_is_replaced(self):
        # The address is the same and the process behind it is not: config.json
        # still carries the key of the engine that owned this port last time.
        # Nothing about the address is wrong, so `follow_now` has no move to
        # make -- the only sign the key is dead is the 401 on the counters.
        self.write(INSTANCE, record(18500))
        servers = Servers()
        endpoint = self.endpoint(ENGINE, api_key="0" * 64, servers=servers)
        self.assertFalse(endpoint.follow_now(force=True))  # the port is right
        result = endpoint.poll()
        self.assertEqual(endpoint.api_key, KEY)
        self.assertTrue(result["up"], result.get("error"))
        # And the reader is told which file the key came from.
        self.assertIn(INSTANCE, result["follow_reason"])
        # One health check, one failed probe, the record read off disk, and the
        # retry -- not a sweep of every address on the machine.
        self.assertEqual(len(servers.asked(APP + "/v1/models")), 0)
        self.assertTrue(any(key == KEY for _, key in servers.calls))

    def test_an_engine_address_with_no_key_borrows_the_app_s_one(self):
        # The other half of the same symptom, caught before the first scrape:
        # the health check identifies the engine and says its counters are locked,
        # and the key lives in the record the app wrote for that process.
        self.write(INSTANCE, record(18500))
        servers = Servers()
        endpoint = self.endpoint(ENGINE, servers=servers)
        self.assertTrue(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, ENGINE)  # no move, only the key
        self.assertEqual(endpoint.api_key, KEY)
        self.assertEqual(len(servers.calls), 1)  # just the health check

    def test_no_record_for_that_port_leaves_the_error_alone(self):
        # A hand-started engine whose key the operator knows is not in the
        # records.  Inventing one would cover a real mistake with a guessed one,
        # so the card keeps saying what the server said.
        servers = Servers()
        endpoint = self.endpoint(ENGINE, api_key="0" * 64, servers=servers)
        result = endpoint.poll()
        self.assertFalse(result["up"])
        self.assertIn("unauthorized", result["error"])
        self.assertEqual(endpoint.api_key, "0" * 64)

    def test_an_engine_where_configured_is_left_alone(self):
        # A hand-started serve/server.py on a fixed port.  Following would be
        # worse than useless here: a record elsewhere on the machine would move
        # the card onto someone else's model.
        servers = Servers()
        endpoint = self.endpoint(ENGINE, api_key=KEY, servers=servers)
        self.assertFalse(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, ENGINE)
        self.assertIsNone(endpoint.proxied_url)
        # ... and it was decided by the one cheap ask, not by a sweep.
        self.assertEqual(len(servers.calls), 1)

    def test_nothing_answers_moves_nothing(self):
        self.write(INSTANCE, record(18500, key="other"))
        servers = Servers(app_up=False, up=False)
        endpoint = self.endpoint(servers=servers)
        self.assertFalse(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, APP)

    def test_the_newest_answering_record_wins(self):
        # Two engines alive: the app's records outlive the processes, so
        # "newest that answers" is the model in use and the older one is the
        # process someone forgot to stop.
        self.write(OTHER_INSTANCE, record(19999), age=120)
        self.write(INSTANCE, record(18500))
        servers = Servers(app_up=False)  # the app itself is gone
        endpoint = self.endpoint(servers=servers)
        self.assertTrue(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, ENGINE)

    def test_an_app_named_engine_is_followed_while_it_loads(self):
        # The app answers, so the app decides which engine this card is about.
        # The older record's engine is up and could have been polled instead --
        # and the card would then show a live model that the reader is not
        # talking to, which is worse than a card saying "not answering yet".
        self.write(OTHER_INSTANCE, record(19999))
        self.write(INSTANCE, record(18500))
        servers = Servers(engine=OTHER)   # 18500 is still loading
        endpoint = self.endpoint(servers=servers)
        self.assertTrue(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, ENGINE)
        self.assertEqual(endpoint.proxied_url, APP)

    def test_an_unverified_record_does_not_move_the_card(self):
        # No app to name the engine, and the records only describe processes
        # that are gone: guessing would put a live-looking port on the card.
        self.write(INSTANCE, record(18500))
        servers = Servers(app_up=False, up=False)
        endpoint = self.endpoint(servers=servers)
        self.assertFalse(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, APP)

    def test_discover_false_polls_only_what_it_was_told(self):
        self.write(INSTANCE, record(18500))
        servers = Servers()
        endpoint = self.endpoint(servers=servers, discover=False)
        self.assertFalse(endpoint.follow_now(force=True))
        self.assertEqual(endpoint.base_url, APP)
        self.assertEqual(servers.calls, [])

    def test_the_throttle_spends_its_asks_once_per_window(self):
        self.write(INSTANCE, record(18500))
        servers = Servers()
        endpoint = self.endpoint(servers=servers)
        endpoint.follow_now(force=True)
        asked = len(servers.calls)
        endpoint.follow_now()
        self.assertEqual(len(servers.calls), asked)

    def test_ttft_comes_from_the_engine_s_request_history(self):
        """The card reads first-token seconds the engine already measured.

        ``requests`` is newest-first and carries ``first_engine_token_s`` once
        a request's first token landed -- the newest row that has the number
        is the last completed request; rows still waiting for their first
        token carry None and are skipped, never averaged in as zero.
        """
        class WithRequests(Servers):
            def __call__(self, url, api_key="", timeout=2.0):
                answer = super().__call__(url, api_key, timeout)
                if isinstance(answer, dict) and url.endswith("/metrics"):
                    answer["requests"] = [
                        {"first_engine_token_s": None},
                        {"first_engine_token_s": 12.5},
                        {"first_engine_token_s": 7.5},
                    ]
                return answer

        servers = WithRequests()
        endpoint = self.endpoint(ENGINE, api_key=KEY, servers=servers)
        result = endpoint.poll()
        metrics = (result.get("models") or [{}])[0].get("metrics") or {}
        self.assertEqual(metrics["ttft_s_last"], 12.5)
        self.assertEqual(metrics["ttft_s_mean"], 10.0)
        self.assertEqual(metrics["ttft_n"], 2)
        # And an engine without the history leaves every TTFT field empty --
        # the card shows a dash, it does not invent a latency.
        plain = self.endpoint(ENGINE, api_key=KEY, servers=Servers())
        quiet = (plain.poll().get("models") or [{}])[0].get("metrics") or {}
        self.assertIsNone(quiet["ttft_s_last"])
        self.assertEqual(quiet["ttft_n"], 0)

    def test_the_card_says_what_it_follows(self):
        # run.log and an empty card are the only places a reader learns which
        # process a number came from, so both get the reason and the address it
        # was reached through.
        self.write(INSTANCE, record(18500))
        servers = Servers()
        endpoint = self.endpoint(servers=servers)
        result = endpoint.poll()
        self.assertEqual(result["base_url"], ENGINE)
        self.assertIn("47831", result["follow_reason"])
        self.assertEqual(result["proxied_url"], APP)
        # The symptom this whole module exists for: a card that is up, with the
        # model named and its context window read, rather than an empty card
        # whose error is a JSON complaint about HTML.
        self.assertTrue(result["up"], result.get("error"))
        self.assertEqual(result["models"][0]["id"], "qwen3.8-flash-next")
        self.assertEqual(result["models"][0]["n_ctx"], 524288)


if __name__ == "__main__":
    unittest.main()
