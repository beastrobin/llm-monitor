"""Auto-discovery of the live model behind the Hermes router.

The Hermes router (llama.cpp router mode) loads on-demand models as
*separate* llama-server processes on throwaway ports, so the model actually
generating tokens is not the one listening on the router's own port.  The
router does name it, though: every proxied request writes

    proxying request to model <name> on port <N>

This collector tails that log, follows the newest ``(name, port)`` pair and
scrapes it as an ordinary single-model llama endpoint -- straight at the child,
which answers ``/props``, ``/slots`` and ``/metrics`` without an api-key.  The
pair changes on every model load, so a new delegate is built each time the log
shows a different one; the sampler baseline and the ledger pick the change up
the same way they do a server restart.

Why not just read the router's own endpoints?  As of llama.cpp b11370 the
router *does* report ``/v1/models`` status ``loaded`` correctly, and it will
proxy telemetry for a named model (``/metrics?model=<id>&autoload=false``).  It
still cannot answer the one question this panel exists for -- which model is
being served *right now* -- without a log to interpret, because the router's
own body carries no counters of its own.  Keeping the child as a separate
endpoint also means the router's auth never has to be configured here.

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
        # Why the last read found nothing, kept in one of a few fixed codes so
        # the dashboard can name which of three very different causes it is: a
        # log that is not where we looked, a log with nothing in it, or a log
        # whose newest request names a model the router has since unloaded.
        self.log_reason = ""
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
            self.log_reason = "log missing"
            return None
        if size <= 0:
            self.log_reason = "log empty"
            return None
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(max(0, size - TAIL_BYTES))
                chunk = fh.read().decode("utf-8", errors="replace")
        except OSError:
            self.log_reason = "log unreadable"
            return None
        # The first line of the tail can be cut in half; a partial line either
        # still carries a full match or simply does not, so scanning all lines
        # and taking the last hit is exactly right.
        target = None
        for line in chunk.splitlines():
            match = PROXY_RE.search(line)
            if match:
                target = (match.group(1).strip(), int(match.group(2)))
        self.log_reason = "" if target is not None else "no proxy line in tail"
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
            # Carried so the panel can show where it looked.  The whole class of
            # "the model card is empty" bugs is a wrong path, and a reader can
            # only tell that from an empty card if the path is on screen.
            "log_path": str(self.log_path),
            "log_reason": self.log_reason,
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
        # Hand the panel the target as well as the verdict.  When the log names
        # a model but nothing answers on its port, "which model the router last
        # used" is the whole explanation, and the child's own error string
        # ("connection refused") cannot supply it.
        result["target_model"] = self._target[0]
        result["target_port"] = self._target[1]
        result["log_path"] = str(self.log_path)
        result["live"] = True
        return result
