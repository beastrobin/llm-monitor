"""Local LLM monitoring dashboard.

Serves a single-page dashboard plus three JSON endpoints:

* ``GET  /api/snapshot``     -- the current sample and the retained history,
  used once on page load so a refresh does not wipe the charts.
* ``GET  /api/stream``       -- server-sent events, one sample every interval.
* ``POST /api/power-limit``  -- apply a GPU power preset.  The only write on
  this server; see ``Handler._write_refusal`` for how it is fenced off.

Everything is built on the standard library: the collectors shell out to
nvidia-smi and call Win32 through ctypes, and the server is
http.server.ThreadingHTTPServer.
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from collectors import gpu, power, stats, system
from collectors.llama import LlamaEndpoint
from collectors.hermes_live import LiveEndpoint

DEFAULT_LIVE_LOG = "D:/AI/hermes/logs/llama-server.log"

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
CONFIG_PATH = ROOT / "config.json"

HISTORY_LEN = 900
KEEPALIVE_SECONDS = 25.0
PROBE_TIMEOUT = 2.0

# Distinct codes so the launcher can react instead of guessing, because the
# right follow-up differs by who owns the port:
#   1 -- generic failure.
#   2 -- an elevated dashboard owns it; it already has the power control, so
#        just point the browser at it.
#   3 -- a read-only dashboard owns it; it can be browsed, but taking over
#        means stopping it first.
#   4 -- something that is not a dashboard owns it; opening a browser would
#        show the wrong thing, so only surface the diagnosis.
EXIT_FAILURE = 1
EXIT_PORT_BUSY = 2
EXIT_PORT_BUSY_READONLY = 3
EXIT_PORT_BUSY_FOREIGN = 4


class ConfigError(ValueError):
    """A user error in config.json, worded for a human rather than a trace."""

DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 8090, "interval_seconds": 2.0},
    "endpoints": [
        {
            "id": "hermes",
            "label": "Hermes Router",
            "base_url": "http://127.0.0.1:18434",
            "api_key": "",
            "api_key_env": "HERMES_LLAMA_API_KEY",
        },
    ],
    "gpu": {"enabled": True},
    "system": {"enabled": True},
    # The engine reports token totals as process-lifetime counters, so they go
    # back to zero on every restart.  The ledger folds them into a file to give
    # the dashboard daily and lifetime usage that outlives the server.
    "stats": {"enabled": True, "path": "stats.json", "keep_days": 90},
    # The router loads on-demand models on throwaway ports and never lists
    # them, so this endpoint follows the router's own log to find whichever
    # model is live right now.
    "live": {"enabled": True, "log": DEFAULT_LIVE_LOG},
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


def log(message):
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), message))
    sys.stdout.flush()


def _probe_existing(host, port):
    """Ask the instance already holding this port about itself.

    A refused bind only says the port is taken.  It does not say whether the
    holder can touch the GPU power limit -- and that is the one thing the user
    needs in order to choose between "just open the browser" and "stop that
    copy first".  So read it off the holder's own ``/api/snapshot``.
    """
    address = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    # urlopen would honour http_proxy, which hijacks loopback requests.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://%s:%d/api/snapshot" % (address, port)
    try:
        with opener.open(url, timeout=PROBE_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure means "cannot tell"
        return None
    config = payload.get("config")
    return config if isinstance(config, dict) else None


def _timed(fn):
    """Wrap a collector so each sample reports what it cost to collect."""
    def run():
        started = time.time()
        value = fn()
        return value, (time.time() - started) * 1000.0
    return run


def active_power_tier(payload):
    """The GPU power limit in force for this sample, or None if unreadable.

    The ledger wants to say which tier a token total was produced at, and the
    only place that is knowable is here, at sample time.  A power limit belongs
    to the card rather than to any one model, so this returns a single number
    for the whole sample and the ledger stamps every delta with it.  With more
    than one GPU the first reported limit wins -- the dashboard is built around
    a single card, and guessing a per-model mapping would be worse than saying
    what was actually measured.
    """
    for item in (payload.get("gpu") or {}).get("gpus") or []:
        watts = item.get("power_limit_w")
        if isinstance(watts, (int, float)) and not isinstance(watts, bool) \
                and watts > 0:
            return watts
    return None


def load_config(path):
    if not path.exists():
        log("no config.json found, writing defaults to %s" % path)
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    data = json.loads(path.read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged["server"].update(data.get("server") or {})
    if data.get("endpoints"):
        merged["endpoints"] = data["endpoints"]
    merged["gpu"].update(data.get("gpu") or {})
    merged["system"].update(data.get("system") or {})
    merged["stats"].update(data.get("stats") or {})
    merged["live"].update(data.get("live") or {})
    return merged


def validate_config(config):
    """Raise ConfigError on the first field a hand-edited config got wrong.

    A missing ``id`` or ``base_url`` would otherwise crash the startup inside
    ``LlamaEndpoint.__init__`` with a bare traceback; this turns it into the
    same "one human sentence + exit 1" treatment every other user error gets.
    """
    for spec in config["endpoints"]:
        if not isinstance(spec, dict):
            raise ConfigError(
                "config.json: endpoints must be a list of objects, got %s"
                % type(spec).__name__)
        for field in ("id", "base_url"):
            if not isinstance(spec.get(field), str) or not spec[field].strip():
                raise ConfigError(
                    'config.json: an endpoint is missing its "%s" field' % field)
    if not isinstance(config["server"].get("port"), int) \
            or not 0 < config["server"]["port"] < 65536:
        raise ConfigError(
            "config.json: server.port must be an integer between 1 and 65535")


def build_ledger(config):
    """Build the token ledger the config asks for, or None when it is off.

    A relative ``path`` is resolved against the project root so the launcher
    works no matter which directory it was started from.
    """
    settings = config.get("stats") or {}
    if not settings.get("enabled", True):
        return None
    name = settings.get("path") or "stats.json"
    path = name if os.path.isabs(name) else str(ROOT / name)
    try:
        keep_days = int(settings.get("keep_days") or 0)
    except (TypeError, ValueError):
        keep_days = 0
    return stats.TokenLedger(path, keep_days=keep_days or stats.DEFAULT_KEEP_DAYS)


class Hub(object):
    """Owns the collectors, the latest sample and the chart history."""

    def __init__(self, config):
        self.config = config
        self.endpoints = [LlamaEndpoint(spec) for spec in config["endpoints"]]
        live = config.get("live") or {}
        if live.get("enabled", True):
            self.endpoints.append(LiveEndpoint(
                live.get("log") or DEFAULT_LIVE_LOG))
        self.system = system.SystemCollector()
        self.collect_gpu = bool(config["gpu"].get("enabled", True))
        self.collect_host = bool(config["system"].get("enabled", True))

        self._mutex = threading.Lock()
        self._snapshot = None
        self._timestamps = deque(maxlen=HISTORY_LEN)
        self._series = {}
        self._subscribers = []
        self.samples = 0
        # Whether this process may change driver settings.  Read once: the
        # token cannot change under a running process, and the UI needs it
        # before it can decide whether to offer the power control at all.
        self.elevated = power.is_elevated()
        # A power-limit change should land on the charts immediately instead of
        # waiting out the interval, so the sampler waits on this rather than
        # sleeping blind.  A generation counter plus a condition (not a bare
        # Event) closes the race where a second set() lands between wait() and
        # clear(): the loop only proceeds once the generation it waited for is
        # the one in force, so no wake-up is ever swallowed.
        self._wake_lock = threading.Lock()
        self._wake_cond = threading.Condition(self._wake_lock)
        self._woken_gen = 0
        # Sampling is dominated by nvidia-smi spawns and HTTP round trips.
        # Running the sources concurrently keeps one cycle inside the interval.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="collect")
        # Lifetime token totals.  Built last because this is the only part of
        # the Hub that touches the disk, and a slow or malformed stats file
        # must not delay the first sample.  None when the feature is disabled.
        self.ledger = build_ledger(config)

    def _push(self, key, value):
        series = self._series.get(key)
        if series is None:
            series = deque(maxlen=HISTORY_LEN)
            # Pad with None so every series stays aligned with the shared
            # timestamp axis even after a mid-session restart of the endpoint.
            series.extend([None] * len(self._timestamps))
            self._series[key] = series
        series.append(value)

    def _flatten(self, payload):
        """Pull the chartable scalars out of one sample.

        Every card on the dashboard draws a trend, so anything that can be a
        card needs a series here; a card whose key is missing simply stays
        blank instead of erroring.
        """
        values = {}
        for item in payload.get("gpu", {}).get("gpus") or []:
            index = item.get("index")
            values["gpu%s.util" % index] = item.get("util_pct")
            values["gpu%s.mem" % index] = item.get("mem_ratio")
            values["gpu%s.temp" % index] = item.get("temp_c")
            values["gpu%s.power" % index] = item.get("power_w")
            values["gpu%s.memutil" % index] = item.get("mem_util_pct")
            values["gpu%s.fan" % index] = item.get("fan_pct")
        host = payload.get("system") or {}
        if host.get("available"):
            values["host.cpu"] = (host.get("cpu") or {}).get("load_pct")
            memory = host.get("memory") or {}
            values["host.mem"] = memory.get("used_ratio")
            values["host.commit"] = memory.get("commit_ratio")
            values["host.cache"] = memory.get("cache_mb")
            values["host.handles"] = memory.get("handle_count")
            procs = host.get("processes") or []
            values["host.procmem"] = procs[0].get("working_set_mb") if procs else None

        for endpoint in payload.get("endpoints") or []:
            for model in endpoint.get("models") or []:
                metrics = model.get("metrics") or {}
                base = "%s/%s" % (endpoint["id"], model.get("id"))
                values[base + ".decode"] = metrics.get("decode_tps_inst")
                values[base + ".prefill"] = metrics.get("prefill_tps_inst")
                values[base + ".mtp"] = metrics.get("mtp_accept_ratio")
                values[base + ".kv"] = metrics.get("kv_hit_ratio")
                values[base + ".step"] = metrics.get("mtp_tokens_per_step")
                slots = model.get("slots") or []
                ratios = [s.get("ctx_ratio") for s in slots
                          if s.get("ctx_ratio") is not None]
                values[base + ".ctx"] = max(ratios) if ratios else None
        return values

    def sample(self):
        started = time.time()
        payload = {"ts": started, "interval": self.config["server"]["interval_seconds"]}

        gpu_future = (self._pool.submit(_timed(gpu.collect))
                      if self.collect_gpu else None)
        host_future = (self._pool.submit(_timed(self.system.collect))
                       if self.collect_host else None)
        endpoint_futures = [self._pool.submit(_timed(endpoint.poll))
                            for endpoint in self.endpoints]

        timings = {"endpoints": []}
        endpoints = []
        for future in endpoint_futures:
            value, cost = future.result()
            endpoints.append(value)
            timings["endpoints"].append(round(cost))
        payload["endpoints"] = endpoints

        if gpu_future is not None:
            payload["gpu"], cost = gpu_future.result()
            timings["gpu"] = round(cost)
        else:
            payload["gpu"] = {"available": False, "error": "disabled",
                              "gpus": [], "processes": []}

        if host_future is not None:
            payload["system"], cost = host_future.result()
            timings["system"] = round(cost)
        else:
            payload["system"] = {"available": False, "error": "disabled"}

        payload["timings"] = timings
        payload["sample_ms"] = (time.time() - started) * 1000.0

        # Fold this sample's counters into the lifetime ledger.  It is pure
        # dict arithmetic over data already in hand, so it does not belong on
        # the thread pool, and it must not be able to fail the sample.
        if self.ledger is not None:
            self.ledger.observe(stats.readings_for(endpoints),
                                watts=active_power_tier(payload))
            self.ledger.flush()
            payload["stats"] = self.ledger.payload()
        else:
            payload["stats"] = {"available": False, "error": "disabled"}

        values = self._flatten(payload)
        with self._mutex:
            self._timestamps.append(payload["ts"])
            for key, value in values.items():
                self._push(key, value)
            self._snapshot = payload
            self.samples += 1
        return payload, {"ts": payload["ts"], "values": values}

    def snapshot(self):
        with self._mutex:
            return self._snapshot

    def history(self):
        with self._mutex:
            return {
                "timestamps": list(self._timestamps),
                "series": {k: list(v) for k, v in self._series.items()},
            }

    def subscribe(self):
        channel = queue.Queue(maxsize=8)
        with self._mutex:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel):
        with self._mutex:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def set_power_limit(self, watts):
        """Apply a GPU power preset, then resample so the UI reads the truth.

        The response already carries what nvidia-smi read back, but the chart
        series and the other GPU cards come from the sampler -- waking it means
        the whole panel converges on the new state within a frame instead of
        sitting stale for an interval.
        """
        result = power.set_limit(watts)
        if result.get("ok"):
            log("power limit -> %s W (was %s W)" % (
                result.get("applied"), result.get("previous")))
        else:
            log("power limit change refused: %s (requested %s W)" % (
                result.get("error"), watts))
        with self._wake_cond:
            self._woken_gen += 1
            self._wake_cond.notify()
        return result

    def broadcast(self, message):
        with self._mutex:
            targets = list(self._subscribers)
        for channel in targets:
            try:
                channel.put_nowait(message)
            except queue.Full:
                # A stalled client must not hold up the sampler; drop the frame
                # and let the next one through.
                pass


def sample_loop(hub):
    interval = float(hub.config["server"]["interval_seconds"])
    max_sample_ms = max(30000, interval * 1000 * 5)  # 5x interval or 30s cap
    while True:
        started = time.time()
        sample_done = False
        try:
            payload, point = hub.sample()
            # Ship the whole snapshot on every frame, not just the flattened
            # values.  The cards read from the snapshot, so a frame carrying
            # only values would advance the charts while leaving every number
            # frozen -- which reads as "the page stopped refreshing".
            hub.broadcast({"snapshot": payload, "point": point})
            sample_done = True
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log("sample failed: %s: %s" % (type(exc).__name__, exc))
        # Interruptible wait: an out-of-band change (a power-limit write) cuts
        # it short so the next sample reflects it right away.  The generation
        # is snapshotted once, then the condition is waited on with a timeout
        # and re-checked; a write that lands while the loop is between checks
        # is caught by the next comparison, so no wake is swallowed.  (A bare
        # Event + clear() can lose exactly that late set.)
        with hub._wake_cond:
            wanted = hub._woken_gen
            remaining = interval - (time.time() - started)
            if not sample_done:
                remaining = max(1.0, remaining * 0.5)  # faster retry on failure
            deadline = time.time() + max(0.2, remaining)
            while hub._woken_gen == wanted:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                hub._wake_cond.wait(timeout=remaining)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _static(self, relative):
        target = (FRONTEND / relative.lstrip("/")).resolve()
        # is_relative_to, not a string prefix check: a sibling directory whose
        # name merely starts with the frontend's name (frontend-evil/) must
        # not pass the gate.
        if not target.is_relative_to(FRONTEND) or not target.is_file():
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(),
                   CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))

    def _read_json(self, limit=4096):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # Writes are the only thing here that can change the machine, and this
    # server answers any page the browser has open.  Three checks:
    #
    # * Host must be the loopback name we bound to -- blocks DNS rebinding,
    #   where an attacker's hostname resolves to 127.0.0.1 and the browser then
    #   treats the request as same-origin.
    # * Origin, when the browser sends one, must be loopback too.  A form post
    #   from another site always carries this.
    # * A custom header is required, and this is the check that actually holds:
    #   it forces a CORS preflight, and this server never answers OPTIONS, so
    #   cross-origin script cannot get the real request out at all.  A plain
    #   form post clears the first two checks on some browsers but cannot set
    #   a header.
    LOOPBACK = ("127.0.0.1", "localhost", "::1")

    def _write_refusal(self):
        """None when the request may proceed, otherwise an error code."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            hostname = host.split("]")[0].lstrip("[")
        else:
            hostname = host.rsplit(":", 1)[0]
        if hostname not in self.LOOPBACK:
            return "bad_host"

        origin = self.headers.get("Origin")
        if origin and origin != "null":
            try:
                if (urlparse(origin).hostname or "") not in self.LOOPBACK:
                    return "bad_origin"
            except ValueError:
                return "bad_origin"

        if self.headers.get("X-LLM-Monitor") != "1":
            return "missing_header"
        return None

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/power-limit":
            self.close_connection = True
            self._send_json({"ok": False, "error": "not_found"}, 404)
            return

        refusal = self._write_refusal()
        if refusal:
            log("rejected power-limit write: %s" % refusal)
            self.close_connection = True
            self._send_json({"ok": False, "error": refusal}, 403)
            return

        body = self._read_json()
        if not isinstance(body, dict) or "watts" not in body:
            self.close_connection = True
            self._send_json({"ok": False, "error": "bad_body"}, 400)
            return

        result = self.hub.set_power_limit(body.get("watts"))
        self._send_json(result, 200 if result.get("ok") else 409)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/snapshot":
            self._send_json({
                "snapshot": self.hub.snapshot(),
                "history": self.hub.history(),
                "config": {
                    "pid": os.getpid(),
                    "interval_seconds": self.hub.config["server"]["interval_seconds"],
                    "endpoints": [
                        {"id": ep.id, "label": ep.label, "base_url": ep.base_url}
                        for ep in self.hub.endpoints
                    ],
                    # The UI needs to know whether it may offer the power
                    # control before the user clicks and gets a rejection.
                    "elevated": self.hub.elevated,
                    "power_presets": power.PRESETS,
                    "power_default_watts": power.DEFAULT_WATTS,
                },
            })
        elif path == "/api/stream":
            self._stream()
        else:
            self._static(unquote(path))

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        channel = self.hub.subscribe()
        try:
            snapshot = self.hub.snapshot()
            if snapshot:
                frame = "data: " + json.dumps({"snapshot": snapshot},
                                              ensure_ascii=False) + "\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            while True:
                try:
                    message = channel.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                frame = "data: " + json.dumps(message, ensure_ascii=False) + "\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(channel)


class DashboardServer(ThreadingHTTPServer):
    """A server that refuses to share its port.

    ``http.server`` sets ``SO_REUSEADDR``, which on Windows does not merely
    allow a quick restart -- it lets a *second* process bind an address another
    process is already listening on.  Both then accept connections, so
    double-clicking the launcher while a dashboard is up leaves you talking to
    whichever instance happened to win the race, and the page shows another
    process's data.  ``SO_EXCLUSIVEADDRUSE`` turns that silent hijack into an
    outright bind failure the caller can report.
    """

    allow_reuse_address = False

    def handle_error(self, request, client_address):
        """Swallow disconnects instead of printing a traceback.

        The SSE stream is a long-lived connection and browsers drop it on
        refresh, on tab close, and whenever the page navigates.  socketserver
        treats every one of those as an unhandled exception and dumps a full
        traceback, which buries the failures that actually matter.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        ThreadingHTTPServer.server_bind(self)


def main():
    parser = argparse.ArgumentParser(description="Local LLM monitoring dashboard")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config))
    except (OSError, ValueError) as exc:
        # A malformed config is a user mistake, not a crash: say which file and
        # what was wrong instead of dumping a traceback nobody reads.
        log("cannot read %s -- %s: %s" % (args.config, type(exc).__name__, exc))
        raise SystemExit(EXIT_FAILURE)
    # CLI overrides first, then validation: a user can fix a bad port with
    # --port without touching the file, and validation still catches what
    # survives the overrides.
    if args.host:
        config["server"]["host"] = args.host
    if args.port:
        config["server"]["port"] = args.port
    if args.interval:
        config["server"]["interval_seconds"] = args.interval
    try:
        validate_config(config)
    except ConfigError as exc:
        log("%s" % exc)
        raise SystemExit(EXIT_FAILURE)

    hub = Hub(config)
    Handler.hub = hub

    worker = threading.Thread(target=sample_loop, args=(hub,), daemon=True)
    worker.start()

    host = config["server"]["host"]
    port = int(config["server"]["port"])
    try:
        server = DashboardServer((host, port), Handler)
    except OSError as exc:
        log("cannot listen on %s:%d -- %s" % (host, port, exc))
        existing = _probe_existing(host, port)
        if existing is None:
            log("the port is taken, but it does not answer like a dashboard; "
                "stop whatever holds it, or start with --port <other>")
            code = EXIT_PORT_BUSY_FOREIGN
        else:
            holder_pid = existing.get("pid", "unknown")
            elevated = bool(existing.get("elevated"))
            log("a dashboard is already running on this port "
                "(pid %s, power limit control: %s)"
                % (holder_pid, "enabled" if elevated else "read-only"))
            if elevated:
                log("open http://%s:%d in the browser -- it can already "
                    "change the GPU power limit, no second copy needed"
                    % (host, port))
                code = EXIT_PORT_BUSY
            else:
                log("that copy can only monitor. To get the power control, "
                    "stop it first:")
                log("    taskkill /PID %s /F" % holder_pid)
                log("then start start_monitor_admin.bat")
                code = EXIT_PORT_BUSY_READONLY
        raise SystemExit(code)
    server.daemon_threads = True

    log("dashboard on http://%s:%d  (interval %.1fs, pid %d)" % (
        host, port, config["server"]["interval_seconds"], os.getpid()))
    if hub.elevated:
        log("power limit control: enabled")
    else:
        log("power limit control: read-only -- nvidia-smi -pl needs an "
            "elevated token; start via start_monitor_admin.bat to enable it")
    for endpoint in hub.endpoints:
        log("watching %s -> %s" % (endpoint.id, endpoint.base_url))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        # A Ctrl-C must not cost the last stretch of counted tokens; the
        # periodic flush may not have run yet.
        if hub.ledger is not None:
            hub.ledger.flush(force=True)
        server.server_close()


if __name__ == "__main__":
    main()
