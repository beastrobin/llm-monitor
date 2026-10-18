"""Local LLM monitoring dashboard.

Serves a single-page dashboard plus three JSON endpoints:

* ``GET  /api/snapshot``     -- the current sample and the retained history,
  used once on page load so a refresh does not wipe the charts.
* ``GET  /api/stream``       -- server-sent events, one sample every interval.
* ``POST /api/power-limit``  -- apply a GPU power preset.
* ``POST /api/restart``      -- relaunch this process and exit.  The two
  writes are fenced off the same way; see ``Handler._write_refusal``.

Everything is built on the standard library: the collectors shell out to
nvidia-smi and call Win32 through ctypes, and the server is
http.server.ThreadingHTTPServer.
"""

import argparse
import json
import os
import queue
import socket
import subprocess
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
from collectors.llama import LlamaEndpoint, _get_json, _resolve_api_key
from collectors.strata import StrataEndpoint
from collectors.hermes_live import LiveEndpoint
from collectors.hermes_session import latest_desktop_selection

# The desktop launcher starts this through pythonw.exe, which has no console at
# all and therefore leaves sys.stdout and sys.stderr as None.  argparse writes
# to stdout for --help and --version before main() is ever reached, so a bare
# run would die on the first line instead of doing something diagnosable.  Give
# both a sink once, here; log() keeps its own durable copy in run.log.
for _stream_name in ("stdout", "stderr"):
    if getattr(sys, _stream_name) is None:
        setattr(sys, _stream_name, open(os.devnull, "w", encoding="utf-8"))
del _stream_name

# The one place the release is named.  It is stamped into the startup log, the
# /api/snapshot config and --version, so the console, the browser header and a
# bare invocation all agree; nothing else in the tree hard-codes it.
VERSION = "1.6"

# The router writes its startup line -- the port it listens on, the api-key it
# generated, and every request it proxies -- to this log, and that line is the
# only place an outside reader learns any of it.  Hermes does not keep that log
# in one fixed place: a desktop install parks its state under %LOCALAPPDATA%,
# while a CLI install keeps it wherever it was unpacked.  So the default is a
# list of candidates rather than a single path -- %LOCALAPPDATA% exists on every
# Windows box whether or not Hermes ever put anything there, and deriving from
# it alone left the model card empty with nothing on screen to explain why.
# resolve_live_log picks between them; see its docstring for the rule.
_LOCAL_APPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
LIVE_LOG_CANDIDATES = (
    Path(_LOCAL_APPDATA) / "hermes" / "logs" / "llama-server.log",
    Path("D:/AI/hermes/logs/llama-server.log"),
)
# What config.json is seeded with.  The resolver, not this string, decides.
DEFAULT_LIVE_LOG = str(LIVE_LOG_CANDIDATES[0])


def resolve_live_log(configured=None):
    """The router log to follow, and a one-line reason for the choice.

    An explicit ``live.log`` in config.json wins outright whenever that file is
    there -- the operator named it.  Otherwise take the candidates that exist,
    and when several do (a box carrying both a desktop and a CLI install) the
    newest one wins: the log being appended to is the only one that can name the
    model being served right now.  With none, hand back the configured path and
    say so; the collector degrades to "no active model" on its own, and the
    reason rides along to the log and the panel so the guess is never silent.
    """
    if configured:
        path = Path(configured)
        if path.is_file():
            return path, "configured in live.log"
    found = []
    for candidate in LIVE_LOG_CANDIDATES:
        try:
            found.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    if found:
        found.sort(key=lambda item: item[0], reverse=True)
        return found[0][1], "newest of %d candidate(s) on disk" % len(found)
    return Path(configured or DEFAULT_LIVE_LOG), "no candidate exists -- tried " + \
        ", ".join(str(c) for c in LIVE_LOG_CANDIDATES)


# Hermes picks a free port for its router on every start and writes the one it
# settled on into its own config.yaml.  A dashboard pinned to a fixed address
# therefore ends up describing whichever instance happens to own that port --
# and with a second router alive (the desktop app's beside a hand-started one)
# the pinned address is not even running the same model, so the panel reports a
# model nobody is serving while the one in use never appears.  The file sits
# beside the log the live endpoint already follows, so it is derived from that
# rather than baked into the tree.
HERMES_CONFIG_NAME = "config.yaml"
# Re-read session routing on the normal sampling cadence. A desktop model
# switch can change state.db without touching the default in config.yaml.
ROUTER_REFRESH_SECONDS = 2.0


def hermes_config_candidates(live_log=None):
    """The config.yaml paths to try, most specific first."""
    candidates = []
    if live_log is not None:
        # <hermes-home>/logs/llama-server.log -> <hermes-home>/config.yaml
        candidates.append(Path(live_log).parent.parent / HERMES_CONFIG_NAME)
    candidates.append(Path(_LOCAL_APPDATA) / "hermes" / HERMES_CONFIG_NAME)
    return candidates


def read_model_base_url(text):
    """The ``model.base_url`` from a Hermes config.yaml, or None.

    Parsed by hand instead of with a YAML library: this is the only key the
    dashboard wants, the file is machine-written with two levels of indentation
    at most, and pulling in PyYAML for one line would add the first dependency
    to a tree that deliberately has none.
    """
    in_model = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            # A top-level key: only model: opens the block we care about.
            in_model = stripped.split(":", 1)[0].strip() == "model"
            continue
        if not in_model:
            continue
        key, separator, value = stripped.partition(":")
        if separator and key.strip() == "base_url":
            value = value.strip().strip("'\"")
            if value:
                return value
    return None


def strip_api_suffix(url):
    """A llama.cpp base_url, from the endpoint URL Hermes writes.

    Hermes stores the address the way a client calls it -- ``host:port/v1`` --
    while every collector here appends its own path (``/v1/models``, ``/props``,
    ``/metrics``).  Keeping the suffix would ask the router for
    ``/v1/v1/models`` and get a 404 that reads exactly like a router with no
    models loaded.
    """
    trimmed = url.rstrip("/")
    if trimmed.lower().endswith("/v1"):
        trimmed = trimmed[:-3].rstrip("/")
    return trimmed


def resolve_hermes_router(configured, live_log=None, api_key=""):
    """The router Hermes is serving from right now, and why that one.

    The latest desktop session wins over config.yaml: changing a conversation's
    model does not change the global default. If that session has only a model
    name, a configured router may still be identified by its loaded preset.
    Otherwise Hermes' global config.yaml wins, then config.json. The reason
    string records which source supplied the address.
    """
    candidates = hermes_config_candidates(live_log)
    selected_model, selected_provider = None, None
    for candidate in candidates:
        session_url, selected_model, selected_provider = latest_desktop_selection(
            candidate.parent)
        if session_url:
            return strip_api_suffix(session_url), "latest desktop session in %s" % (
                candidate.parent / "state.db")
        if selected_model:
            break
    # Some Hermes sessions persist the provider and model but omit base_url.
    # Check the configured router's loaded preset before falling back to the
    # global default, which may still point at a service the user has stopped.
    if selected_model and selected_provider in ("llamacpp", "llamacpp-loader") and configured:
        try:
            payload = _get_json(strip_api_suffix(configured) + "/v1/models",
                                api_key, timeout=0.5)
            if any(item.get("id") == selected_model
                   and (item.get("status") or {}).get("value") == "loaded"
                   for item in payload.get("data", [])):
                return strip_api_suffix(configured), "loaded session model on config.json router"
        except Exception:
            pass  # No verified match: let the normal config.yaml fallback run.
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        url = read_model_base_url(text)
        if url:
            return strip_api_suffix(url), "model.base_url in %s" % candidate
    return strip_api_suffix(configured or ""), "no Hermes config.yaml -- using config.json"


# Which collector reads an endpoint, named in that endpoint's own spec.  Nearly
# every llama.cpp-shaped server exports Prometheus text at /metrics; Strata
# returns JSON instead, and a collector that assumes the wrong one reports an
# endpoint that is up with a model loaded and not one number to show for it --
# the whole model card blank, with nothing on screen to say why.  The dialect
# belongs to the server, so it is configured rather than sniffed: a probe would
# have to run on every start, including the starts where that server is down,
# and would still have to guess for anything that answers like llama.cpp.
# An unset or unknown kind reads as llama.cpp, so an old config.json keeps
# working untouched.
ENDPOINT_KINDS = {"llama": LlamaEndpoint, "strata": StrataEndpoint}


def address_key(url):
    """A comparable key for an endpoint address, or None for no address.

    Two endpoints watching one server have to be recognised as one server even
    when they spell it differently: Hermes writes whatever its own config holds,
    and ``localhost`` and ``127.0.0.1`` are the same machine.  Only the host and
    the port take part -- they are the entire address as far as connecting goes,
    so nothing else may decide whether two cards describe one server.
    """
    text = (url or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "//" in text else "//" + text)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def same_address(left, right):
    """Whether two endpoint URLs name the same host and port."""
    key = address_key(left)
    return key is not None and key == address_key(right)


def build_endpoint(spec):
    """The collector for one endpoint spec, chosen by its declared dialect."""
    kind = (spec.get("kind") or "llama").strip().lower()
    collector = ENDPOINT_KINDS.get(kind)
    if collector is None:
        log("endpoint %s: unknown kind %r, reading it as llama.cpp" % (
            spec.get("id"), spec.get("kind")))
        collector = LlamaEndpoint
    return collector(spec)


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "run.log"

# A rolling window onto the tail of the output, not an archive: a windowless
# launch has nothing else to read afterwards, and it must not grow without end.
MAX_LOG_BYTES = 1 << 20

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
    # The router serves each on-demand model from a child process on a
    # throwaway port, so this endpoint follows the router's own log to find
    # whichever model is live right now.
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
    """Timestamped line to the console, and always to run.log.

    The console copy is what a hand-run in a terminal keeps.  run.log is the
    copy that matters for the launched path: pythonw.exe has no console, so
    without it a windowless start would leave nothing behind to read when the
    dashboard fails to appear.
    """
    line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), message)
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.write_text("", encoding="utf-8")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


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
    """The GPU power wall in force for this sample, or None if unreadable.

    The ledger wants to say which tier a token total was produced at, and the
    only place that is knowable is here, at sample time.  A power limit belongs
    to the card rather than to any one model, so this returns a single number
    for the whole sample and the ledger stamps every delta with it.  With more
    than one GPU the first reported wall wins -- the dashboard is built around
    a single card, and guessing a per-model mapping would be worse than saying
    what was actually measured.

    The software limit is preferred, because that is the tier the user picked.
    Laptop GPUs do not report one -- Dynamic Boost owns the ceiling there -- so
    the enforced ceiling stands in, rounded to the 5 W grid the driver accepts:
    that ceiling moves every second, and a ledger row per wobble would say less
    than "about 125 W".
    """
    def usable(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) \
            and value > 0

    for item in (payload.get("gpu") or {}).get("gpus") or []:
        requested = item.get("power_requested_w")
        if usable(requested):
            return float(requested)
        enforced = item.get("power_enforced_w")
        if usable(enforced):
            return float(round(enforced / 5.0) * 5)
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


def config_block(hub):
    """The half of the page's input that is NOT a sample.

    ``/api/snapshot`` used to be the only place this was served, and the page
    asked for it exactly once at boot.  A restart pressed from the dashboard
    reloads the page while the port is being handed over, so that one request
    can fail -- and every frame after it arrives without a menu: the power card
    loses its presets and the version badge goes blank until someone refreshes by
    hand.  The block now also rides on each stream connection (see
    ``_stream``), which makes the handover self-healing.
    """
    return {
        "version": VERSION,
        "pid": os.getpid(),
        "interval_seconds": hub.config["server"]["interval_seconds"],
        "endpoints": [
            {"id": ep.id, "label": ep.label, "base_url": ep.base_url}
            for ep in hub.endpoints
        ],
        # The UI needs to know whether it may offer the power control before
        # the user clicks and gets a rejection.
        "elevated": hub.elevated,
        # Derived from the installed card, not from a constant: a menu
        # hard-coded for a 600 W card offers a 175 W laptop three settings its
        # driver has to refuse.
        "power_presets": hub.power_presets,
        # False once the driver has refused a software power limit (laptop
        # GPUs); the panel then reports the live ceiling instead of pretending
        # the buttons can work.
        "power_settable": hub.power_settable,
        "power_applied_w": hub.power_applied_w,
    }


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
        live = config.get("live") or {}
        # Resolved once at startup, not per sample: the choice depends on which
        # files exist, and re-deciding it every two seconds would follow a log
        # to wherever it happened to be mid-rotation.
        self.live_log, self.live_log_reason = None, ""
        if live.get("enabled", True):
            self.live_log, self.live_log_reason = resolve_live_log(live.get("log"))
        # The hermes endpoint follows the router Hermes names in its own
        # config.yaml, because that is the instance the application is talking
        # to.  The router comes up on a fresh port on every restart, so the
        # address in config.json goes stale; with a second router alive at once
        # (the desktop app's beside a hand-started one) the stale address
        # describes an instance running a different model, and the panel then
        # reports a model nobody is serving.  The live endpoint is anchored to
        # the same router: asked about themselves, two routers cannot answer for
        # each other, and a log names whichever instance writes it.
        specs = []
        self.router_url, self.router_reason, self.router_api_key = "", "", ""
        # The address config.json names, kept as the fallback for a later
        # re-resolve: it is what resolve_hermes_router falls back to, and the
        # hermes spec cannot supply it again once it has been overwritten.
        self.hermes_config_base = ""
        for spec in config["endpoints"]:
            spec = dict(spec)
            if spec.get("id") == "hermes":
                self.hermes_config_base = spec.get("base_url") or ""
                self.router_api_key = _resolve_api_key(spec)
                spec["base_url"], self.router_reason = resolve_hermes_router(
                    spec.get("base_url"), self.live_log, self.router_api_key)
                self.router_url = spec["base_url"]
            specs.append(spec)
        self.endpoints = [build_endpoint(spec) for spec in specs]
        # Held by reference so a router that moves can be followed: the address
        # is not a constant, and the two objects quoting it have to be told.
        self.hermes_endpoint = next(
            (endpoint for endpoint in self.endpoints if endpoint.id == "hermes"),
            None)
        self.live_endpoint = None
        if self.live_log is not None:
            self.live_endpoint = LiveEndpoint(
                self.live_log,
                router_url=self.router_url,
                api_key=self.router_api_key)
            self.endpoints.append(self.live_endpoint)
        # When the router address was last re-read, and whether the last sample
        # found it unreachable -- which forces the next re-read instead of
        # waiting out ROUTER_REFRESH_SECONDS.
        self._router_checked_at = time.time()
        self._router_unreachable = False
        # Which endpoint, if any, already covers the address Hermes follows.
        # Filled in by _active_endpoints() on every sample; named so the
        # startup log can say why the hermes card is not on screen.
        self.router_covered_by = ""
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
        # The preset menu for whatever card is installed.  Refreshed from each
        # sample (the card reports its own window there), emptied when no card
        # answers -- an empty menu is what hides the control in the UI.
        self.power_presets = []
        # A card can also refuse software power limits outright; the driver
        # says so only when asked, so the answer is remembered here and the
        # panel stops offering dead buttons for the rest of the run.
        self.power_settable = True
        # What this dashboard last set, for the case where the driver will not
        # report it back (laptops): without it the active-preset highlight has
        # nothing to stand on.  Any value the driver *does* report wins over
        # this, so someone else changing the limit still moves the highlight.
        self.power_applied_w = None
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

    def _refresh_router(self, force=False):
        """Follow session switches and router restarts on the sampling cadence.

        An unreachable router forces an immediate retry. Address changes clear
        both collectors' caches; a source/reason change at the same address
        preserves the counters used to calculate rates.
        """
        if self.hermes_endpoint is None:
            return
        now = time.time()
        if not force and now - self._router_checked_at < ROUTER_REFRESH_SECONDS:
            return
        self._router_checked_at = now
        url, reason = resolve_hermes_router(
            self.hermes_config_base, self.live_log, self.router_api_key)
        self.router_reason = reason
        if same_address(url, self.router_url):
            # Same host and port, so the same server: nothing to tell the
            # collectors.  Compared by address rather than by string, because a
            # config rewritten as `localhost` instead of `127.0.0.1` is a change
            # of spelling and not of server -- re-pushing it would drop every
            # cache and reset the rate baseline for nothing.  The reason still
            # updates: it may have changed (a config.yaml that has just
            # appeared, say) and it is what the panel shows on an empty card.
            return
        log("hermes router moved: %s -> %s (%s)" % (
            self.router_url or "none", url or "none", reason))
        self.router_url = url
        self.hermes_endpoint.set_base_url(url)
        if self.live_endpoint is not None:
            self.live_endpoint.set_router_url(url, self.router_api_key)

    def _active_endpoints(self):
        """The endpoints one sample polls, with an address shown only once.

        Hermes need not front a router of its own: pointed at a direct provider
        it names a plain server, and this dashboard very likely already watches
        that server through its own endpoint.  Polling both would put one
        machine on screen twice and -- because the ledger keys a reading by
        (endpoint, model) -- book every token twice.  So the address Hermes
        follows yields to an endpoint that already covers it, and the live card
        goes with it: that card exists to find the child process behind a
        router, and a server that is asked directly has no child to find.

        Decided per sample rather than at startup, because Hermes can be
        repointed (or can fall back to a router on its own port) at any moment,
        and both cards have to return the moment the addresses differ again.
        """
        addresses = set()
        for endpoint in self.endpoints:
            if endpoint is self.hermes_endpoint or endpoint is self.live_endpoint:
                continue
            key = address_key(endpoint.base_url)
            if key is not None:
                addresses.add(key)
        followed = address_key(self.router_url)
        covered = followed is not None and followed in addresses

        # Named without falling back on the followed endpoint itself: it carries
        # the same address by definition, and naming it would explain the
        # duplicate with the very card that is being hidden.
        self.router_covered_by = ""
        if covered:
            self.router_covered_by = next(
                endpoint.id for endpoint in self.endpoints
                if endpoint not in (self.hermes_endpoint, self.live_endpoint)
                and same_address(endpoint.base_url, self.router_url))

        active = []
        for endpoint in self.endpoints:
            if covered and endpoint in (self.hermes_endpoint, self.live_endpoint):
                continue
            active.append(endpoint)
        return active

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
                # Per-slot series, so a slot card can draw the same kind of
                # trend the big cards do.  Keyed by slot id rather than by list
                # position: ids are what the engine reports and what the UI
                # addresses a row by, so the curve stays attached to its slot
                # even when the engine renumbers or a slot drops out of the
                # batch.  A slot that goes idle reports nulls rather than zeros,
                # which is what leaves a gap in its curve instead of a dive to
                # the floor -- see slotCardValues.
                for slot in slots:
                    if slot.get("id") is None:
                        continue
                    sbase = "%s.slot%s" % (base, slot["id"])
                    values[sbase + ".ctx"] = slot.get("ctx_used")
                    values[sbase + ".ratio"] = slot.get("ctx_ratio")
                    values[sbase + ".prompt"] = slot.get("n_prompt_tokens")
                    values[sbase + ".decoded"] = slot.get("n_decoded")
                    values[sbase + ".tok_s"] = slot.get("tok_s")
                    values[sbase + ".prefill"] = slot.get("prefill_tok_s")
                    values[sbase + ".kv"] = slot.get("kv_hit")
                    values[sbase + ".elapsed"] = slot.get("elapsed_s")
        return values

    def sample(self):
        started = time.time()
        payload = {"ts": started, "interval": self.config["server"]["interval_seconds"]}

        # Before the endpoints are polled, not after: the router can move at any
        # time, and a cycle that reads the new address is a cycle that never
        # shows the old one as unreachable.  A router that failed last time
        # skips the interval so a restart costs one frame, not fifteen seconds.
        self._refresh_router(force=self._router_unreachable)
        # Which endpoints to poll this cycle.  Read after the refresh: whether
        # Hermes' address is a duplicate of another endpoint's is the very thing
        # the refresh can change.
        active = self._active_endpoints()

        gpu_future = (self._pool.submit(_timed(gpu.collect))
                      if self.collect_gpu else None)
        host_future = (self._pool.submit(_timed(self.system.collect))
                       if self.collect_host else None)
        endpoint_futures = [self._pool.submit(_timed(endpoint.poll))
                            for endpoint in active]

        timings = {"endpoints": []}
        endpoints = []
        for future in endpoint_futures:
            value, cost = future.result()
            endpoints.append(value)
            timings["endpoints"].append(round(cost))
        payload["endpoints"] = endpoints
        payload["current_endpoint_id"] = self.router_covered_by or (
            self.hermes_endpoint.id if self.hermes_endpoint is not None else None)

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

        # Remember whether the address Hermes follows answered, for the next
        # cycle's re-read.  Taken from the payload rather than from one endpoint
        # object so it holds either way round: when Hermes is followed directly
        # the answer comes from its own card, and when another endpoint already
        # covers that address -- and so is the one being polled -- it comes from
        # that one.
        followed = address_key(self.router_url)
        answered = [ep.get("up") for ep in endpoints
                    if followed is not None
                    and same_address(ep.get("base_url"), self.router_url)]
        self._router_unreachable = followed is not None and not any(answered)

        # The tier this sample gets booked under, computed once: the ledger
        # stamps it and the UI marks the tier row it is standing on with it, so
        # the two are not allowed to disagree.  It rides on the card record
        # because that is where the UI reads it from.
        tier = active_power_tier(payload)
        gpu_payload = payload.setdefault("gpu", {})
        cards = gpu_payload.get("gpus") or []
        if cards:
            cards[0]["power_tier_w"] = tier
        # The preset menu follows the card, and this sample already carries its
        # window -- deriving it here keeps one source of truth with the write
        # path (the handler passes this exact list back in) without spending an
        # extra nvidia-smi spawn on every request.
        menu = power.presets_for(
            cards[0].get("power_min_w") if cards else None,
            cards[0].get("power_max_w") if cards else None)
        if menu != self.power_presets:
            self.power_presets = menu
            log("power presets: %s" % (", ".join(
                "%s %dW" % (preset["id"], preset["watts"]) for preset in menu)
                or "none -- the card did not report its limits"))

        # Fold this sample's counters into the lifetime ledger.  It is pure
        # dict arithmetic over data already in hand, so it does not belong on
        # the thread pool, and it must not be able to fail the sample.
        if self.ledger is not None:
            self.ledger.observe(stats.readings_for(endpoints), watts=tier)
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

        The menu comes from the hub rather than from a constant, so the values
        accepted here are exactly the buttons the UI was shown.
        """
        result = power.set_limit(watts, presets=self.power_presets)
        if result.get("ok"):
            self.power_applied_w = result.get("applied")
            if self.power_applied_w is None:
                self.power_applied_w = result.get("requested")
            self.power_settable = True
            log("power limit -> %s W (was %s W)" % (
                result.get("applied", "unverified"), result.get("previous")))
        else:
            if result.get("error") == "unsupported":
                # The driver told us this card takes no software power limit at
                # all.  Nothing to retry, so the panel switches to reporting
                # the live ceiling instead of offering a write that cannot work.
                self.power_settable = False
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
        if path == "/api/restart":
            self._restart()
            return
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

    def _restart(self):
        """Relaunch this process, then let go of the port.

        The button exists because the launcher refuses to start a second copy
        while the dashboard answers (and the collector code only loads at
        startup), so applying a code change meant hunting the pid down by hand.
        The replacement is spawned detached -- it must outlive this process,
        which is its parent -- with the same interpreter and arguments, and an
        environment marker that makes it retry the bind: SO_EXCLUSIVEADDRUSE
        means the first attempts collide with this listener, and the retry
        rides out the handover.  The response goes out before the shutdown so
        the button learns the restart was accepted, not that the server died.
        """
        refusal = self._write_refusal()
        if refusal:
            log("rejected restart: %s" % refusal)
            self.close_connection = True
            self._send_json({"ok": False, "error": refusal}, 403)
            return

        self._send_json({"ok": True}, 200)
        env = os.environ.copy()
        env["LLM_MONITOR_RESTART"] = "1"
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen([sys.executable] + sys.argv, env=env, cwd=str(ROOT),
                             creationflags=flags, close_fds=True)
        except OSError as exc:
            log("restart failed to spawn the replacement: %s" % exc)
            return
        log("restart requested from the dashboard -- handing the port over")
        # shutdown() returns once serve_forever stops; main()'s finally then
        # flushes the ledger and closes the socket, which frees the port for
        # the replacement's retry loop.
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/snapshot":
            self._send_json({
                "snapshot": self.hub.snapshot(),
                "history": self.hub.history(),
                "config": config_block(self.hub),
            })
        elif path == "/api/stream":
            self._stream()
        else:
            self._static(unquote(path))

    def _send_frame(self, payload):
        """One SSE data frame, flushed now -- a buffered frame is an invisible one."""
        body = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        channel = self.hub.subscribe()
        try:
            # Config rides on EVERY frame, not just on a hello.  A restart
            # pressed on the dashboard reloads the page into the handover gap,
            # and the browser retries /api/stream eagerly -- it can connect a
            # second before the replacement process has taken its first sample.
            # Tying config to an opening frame that never got one left such a
            # page painting live cards with no power menu indefinitely (the
            # "页面内重启后功率板丢失" report, reproduced over CDP).  Repeating this
            # small block for one loopback client is cheaper than any amount of
            # handshake bookkeeping, and it also heals a page that stayed open
            # across a privilege or preset change without anybody refreshing.
            snapshot = self.hub.snapshot()
            if snapshot:
                self._send_frame({"snapshot": snapshot,
                                  "config": config_block(self.hub)})
            while True:
                try:
                    message = channel.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                frame = dict(message)
                frame["config"] = config_block(self.hub)
                self._send_frame(frame)
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
    parser.add_argument("--version", action="version",
                        version="llm-monitor %s" % VERSION)
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
    # A replacement spawned by /api/restart starts while its parent still
    # holds the port, and SO_EXCLUSIVEADDRUSE makes every early bind fail --
    # so it retries for a few seconds and rides out the handover, instead of
    # reading its own parent as an intruder on the port.  A plain launch gets
    # no retries: the "already running" report below should be instant.
    retry_until = time.monotonic() + 10.0 if os.environ.get("LLM_MONITOR_RESTART") else 0.0
    while True:
        try:
            server = DashboardServer((host, port), Handler)
            break
        except OSError as exc:
            if time.monotonic() >= retry_until:
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
                        log("then double-click start_monitor_admin.vbs")
                        code = EXIT_PORT_BUSY_READONLY
                raise SystemExit(code)
            time.sleep(0.5)
    server.daemon_threads = True

    log("llm-monitor v%s -- dashboard on http://%s:%d  (interval %.1fs, pid %d)" % (
        VERSION, host, port, config["server"]["interval_seconds"], os.getpid()))
    if hub.elevated:
        log("power limit control: enabled")
    else:
        log("power limit control: read-only -- nvidia-smi -pl needs an "
            "elevated token; start via start_monitor_admin.vbs to enable it")
    # The endpoints the first sample will poll.  A card that is left out is a
    # deliberate decision, so the line below says which one and why instead of
    # letting the reader wonder where it went.
    for endpoint in hub._active_endpoints():
        log("watching %s -> %s" % (
            endpoint.id, endpoint.base_url or getattr(endpoint, "log_path", None)))
    if hub.router_url:
        log("hermes router: %s (%s)" % (hub.router_url, hub.router_reason))
    elif hub.live_log is not None:
        log("live model log: %s (%s)" % (hub.live_log, hub.live_log_reason))
    if hub.router_covered_by:
        log("hermes card hidden: %s is already watched as %s -- one address, one "
            "card, and the ledger counts its tokens once" % (
                hub.router_url, hub.router_covered_by))
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
