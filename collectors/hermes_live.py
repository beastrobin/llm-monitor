"""Auto-discovery of the live model behind the Hermes router.

The Hermes router (llama.cpp router mode) loads on-demand models as
*separate* llama-server processes on throwaway ports.  It does not list them
in its own ``/v1/models`` -- every preset reads "unloaded" -- and it will not
proxy their telemetry (``/metrics?model=...`` answers 400).  The one place the
router reveals the truth is its log: every proxied request writes

    proxying request to model <name> on port <N>

This collector tails that log, follows the newest ``(name, port)`` pair and
scrapes it as an ordinary single-model llama endpoint.  The pair changes on
every model load, so a new delegate is built each time the log shows a
different one; the sampler baseline and the ledger pick the change up the
same way they do a server restart.

Reading is cheap (a tail of a few hundred KB) and tolerant: a missing or
unreadable log just reports "no active model" and never touches the rest of
the sample.
"""

import re
import time
from pathlib import Path

from .llama import LlamaEndpoint

TAIL_BYTES = 256 * 1024
PROXY_RE = re.compile(r"proxying request to model (.+?) on port (\d+)")


class LiveEndpoint(object):
    """A dashboard endpoint that always points at the model in use right now."""

    def __init__(self, log_path, label="Hermes 当前模型"):
        self.log_path = Path(log_path)
        self.id = "hermes"
        self.label = label
        self.base_url = None
        self._target = None
        self._delegate = None
        # Discover once up front so the startup log already names the port.
        target = self._read_target()
        if target is not None:
            self._delegate_for(target)

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
        """The newest (model, port) pair in the log tail, or None."""
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return None
        if size <= 0:
            return None
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(max(0, size - TAIL_BYTES))
                chunk = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        # The first line of the tail can be cut in half; a partial line either
        # still carries a full match or simply does not, so scanning all lines
        # and taking the last hit is exactly right.
        target = None
        for line in chunk.splitlines():
            match = PROXY_RE.search(line)
            if match:
                target = (match.group(1).strip(), int(match.group(2)))
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
            "backing_off": False,
            "live": True,
            "polled_at": time.time(),
        }

    def poll(self):
        target = self._read_target()
        if target is None:
            return self._offline("router log reports no active model")

        if self._target != target:
            self._delegate_for(target)

        result = self._delegate.poll()
        # Name the model after the alias the router actually used; the child's
        # /v1/models does not expose it under a stable key.
        if result.get("up") and result.get("models"):
            result["models"][0]["id"] = self._target[0]
        result["live"] = True
        return result
