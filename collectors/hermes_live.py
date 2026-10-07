"""Auto-discovery of the live model behind the Hermes router.

The Hermes router (llama.cpp router mode) serves each on-demand model from a
*separate* llama-server process on a throwaway port, so the model generating
tokens is not the one listening on the router's own port.  Two ways to find it,
tried in this order:

1. **Ask the router.**  ``GET /v1/models`` lists every preset with a ``status``
   block: ``value`` is ``loaded``/``unloaded`` and the launch args carry the
   ``--port`` the spawned child listens on.  That is the router stating which
   model it is serving, and it is right from the moment a preset finishes
   loading -- no request has to go past first.
2. **Read the log.**  Older builds, or a bare llama-server with no router in
   front, only say it by writing one line per proxied request:

       proxying request to model <name> on port <N>

   Kept as the fallback, for when nothing answers a router-shaped question.

The router is preferred because the log is *per instance* and one machine can
run several: two routers sharing a log file make the newest line name a model
belonging to the other one, and the panel then describes a model nobody is
using.  A router can only be asked about itself, so that failure mode does not
exist there.

Either way the answer is a child llama-server that answers ``/props``,
``/slots`` and ``/metrics`` without an api-key, scraped as an ordinary
single-model endpoint.  The pair changes on every model load, so a new delegate
is built each time it does; the sampler baseline and the ledger pick the change
up the same way they do a server restart.

Reading is cheap and tolerant: a missing log or an unreachable router reports
"no active model" and never touches the rest of the sample.
"""

import re
import time
from pathlib import Path

from .llama import LlamaEndpoint, _get_json

TAIL_BYTES = 256 * 1024
PROXY_RE = re.compile(r"proxying request to model (.+?) on port (\d+)")
# /v1/models says which preset is loaded, not how busy it is, so it changes only
# on load or unload.  Ten seconds keeps a load showing almost at once while
# still collapsing the two-second poll into one round trip per window.
MODELS_TTL = 10.0


def _port_from_args(args):
    """The ``--port`` value in a preset's launch args, or None.

    An unloaded preset carries ``--port 0``, which is not an address anything
    can be scraped at, so zero reads as "no port" rather than as port 0.
    """
    for index, item in enumerate(args):
        if item != "--port" or index + 1 >= len(args):
            continue
        try:
            port = int(args[index + 1])
        except (TypeError, ValueError):
            return None
        return port or None
    return None


class LiveEndpoint(object):
    """A dashboard endpoint that always points at the model in use right now."""

    def __init__(self, log_path, label="Hermes 当前模型", router_url=None, api_key=""):
        self.log_path = Path(log_path)
        self.id = "hermes"
        self.label = label
        self.base_url = None
        # When set, the router is the authority and the log is not consulted.
        # The log belongs to whichever instance writes it, which is not
        # necessarily the router being asked about.
        self.router_url = (router_url or "").rstrip("/") or None
        self.api_key = api_key or ""
        self._target = None
        self._delegate = None
        self._models = (0.0, False, None, "")
        # Why the last read found nothing, kept in one of a few fixed codes so
        # the dashboard can name which of several very different causes it is --
        # a router with nothing loaded, a router that does not answer, a log
        # that is not where we looked, a log with nothing in it, or a log whose
        # newest request names a model the router has since unloaded.
        self.reason = ""
        # Which source the current target came from, so the panel can say where
        # the answer came from instead of guessing.
        self.source = ""
        # Discover once up front so the startup log already names the port.
        target = self._read_target()
        if target is not None:
            self._delegate_for(target)

    def set_router_url(self, router_url, api_key=None):
        """Follow a router that has moved to a new address.

        Hermes binds the router to a fresh port on every start, so the address
        read at startup stops answering the moment the application restarts.
        The cached verdict, the target and the delegate all describe the router
        that used to be there, so all of them are dropped; the next poll asks
        the new address and rebuilds from its answer.  Passing ``None`` means
        no router was found at all, which hands the endpoint back to the log --
        the only source left when nothing answers a router-shaped question.
        Returns whether the address actually changed.
        """
        router_url = (router_url or "").rstrip("/") or None
        if api_key is not None:
            self.api_key = api_key or ""
        if router_url == self.router_url:
            return False
        self.router_url = router_url
        self._models = (0.0, False, None, "")
        self._target = None
        self._delegate = None
        self.base_url = None
        self.reason = ""
        self.source = ""
        return True

    def _delegate_for(self, target):
        """Point the sampler at one (model, port) target."""
        self._target = target
        self.base_url = "http://127.0.0.1:%d" % target[1]
        # The spawned child carries no API key and answers /props, /slots
        # and /metrics without a model parameter, i.e. exactly the
        # single-model shape the delegate already handles.
        self._delegate = LlamaEndpoint({
            "id": self.id,
            "label": "Hermes 当前模型",
            "base_url": self.base_url,
            "api_key": "",
            "api_key_env": "",
        })

    def _read_target(self):
        """The (model, port) pair to follow, or None."""
        if self.router_url:
            return self._target_from_router()
        return self._target_from_log()

    def _target_from_router(self):
        """The (model, port) the router says it has loaded, or None."""
        now = time.time()
        fetched, valid, cached, cached_reason = self._models
        if valid and now - fetched < MODELS_TTL:
            # The cached verdict carries its own reason: "nothing loaded" and
            # "could not ask" both cache as no target, and re-deriving the
            # reason here would report the first as the second.
            self.reason = cached_reason
            self.source = "router" if cached else ""
            return cached

        try:
            payload = _get_json(self.router_url + "/v1/models", self.api_key)
        except Exception:  # noqa: BLE001 - any failure reads as "nothing named"
            # Deliberately not cached: a router that is briefly away (a restart,
            # a port change) should be re-asked on the next two-second cycle
            # instead of being written off for the rest of the window.
            self._models = (0.0, False, None, "")
            self.reason = "router unreachable"
            self.source = ""
            return None

        target = None
        for item in payload.get("data", []):
            status = item.get("status") or {}
            if status.get("value") != "loaded":
                continue
            port = _port_from_args(status.get("args") or [])
            if port:
                target = (item.get("id"), port)
                break
        reason = "" if target else "router reports no loaded model"
        self._models = (now, True, target, reason)
        self.reason = reason
        self.source = "router" if target else ""
        return target

    def _target_from_log(self):
        """The newest (model, port) pair in the log tail, or None."""
        try:
            size = self.log_path.stat().st_size
        except OSError:
            self.reason = "log missing"
            return None
        if size <= 0:
            self.reason = "log empty"
            return None
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(max(0, size - TAIL_BYTES))
                chunk = fh.read().decode("utf-8", errors="replace")
        except OSError:
            self.reason = "log unreadable"
            return None
        # The first line of the tail can be cut in half; a partial line either
        # still carries a full match or simply does not, so scanning all lines
        # and taking the last hit is exactly right.
        target = None
        for line in chunk.splitlines():
            match = PROXY_RE.search(line)
            if match:
                target = (match.group(1).strip(), int(match.group(2)))
        self.reason = "" if target is not None else "no proxy line in tail"
        self.source = "log" if target is not None else ""
        return target

    def _offline(self, error):
        return {
            "id": self.id,
            "label": self.label,
            "base_url": None,
            "up": False,
            "mode": None,
            "models": [],
            "error": error,
            # Carried so the panel can show where it looked: the whole class of
            # "the model card is empty" bugs is a wrong path, and a reader can
            # only tell that from an empty card if the path is on screen.  A
            # router that answered has been asked directly, so the path only
            # matters when the answer had to come from a log.
            "log_path": str(self.log_path),
            # And the address that was asked, for the same reason from the other
            # side: "the router did not answer" is nearly always a stale port,
            # and which port the panel believes in is the first thing a reader
            # needs.  None in log mode, where there is no router to name.
            "router_url": self.router_url,
            "log_reason": self.reason,
            "target_source": self.source,
            "backing_off": False,
            "live": True,
            "polled_at": time.time(),
        }

    def poll(self):
        target = self._read_target()
        if target is None:
            return self._offline("no active model")

        if self._target != target:
            self._delegate_for(target)

        result = self._delegate.poll()
        # Name the model after the alias the router actually used; the child's
        # /v1/models does not expose it under a stable key.
        if result.get("up") and result.get("models"):
            result["models"][0]["id"] = self._target[0]
        # Hand the panel the target as well as the verdict.  When a model is
        # named but nothing answers on its port, "which model is loaded" is the
        # whole explanation, and the child's own error string ("connection
        # refused") cannot supply it.
        result["target_model"] = self._target[0]
        result["target_port"] = self._target[1]
        result["target_source"] = self.source
        result["log_path"] = str(self.log_path)
        result["router_url"] = self.router_url
        result["log_reason"] = self.reason
        result["live"] = True
        return result
