"""Keep the hermes endpoint in config.json in step with the running router.

Hermes regenerates its api-key on every start, so a config.json captured from
an earlier run answers 401 and the dashboard shows no models at all -- with
nothing on the page explaining that the key, not the server, is stale.  The
router prints what it chose in its own startup line::

    # spawn: ['...\\llama-server.exe', '--port', '18434',
    #          '--api-key', 'XXX', '--models-max', '1', ...]

This helper reads the newest such line and writes the pair into the endpoint
that points at that port, so the launcher can call it before every start and the
key never has to be fixed by hand again.

Deliberately quiet and idempotent: an unchanged key means no write, a missing
log means no write, and the config is only ever replaced through a temp file.
Run with --check to report the difference without touching anything.

Run:  python tools/sync_hermes_key.py [--check] [--config PATH] [--log PATH]
"""

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402  -- DEFAULT_CONFIG keeps one source of truth

# The launcher starts this through pythonw.exe, which has no console at all and
# leaves sys.stdout and sys.stderr as None.  Every print() below would raise on
# that account, before the config it exists to update had been written.
for _stream_name in ("stdout", "stderr"):
    if getattr(sys, _stream_name) is None:
        setattr(sys, _stream_name, open(os.devnull, "w", encoding="utf-8"))
del _stream_name

# The startup line, and the argv pairs inside it.  The list is a Python repr,
# so the quotes around each element are whatever the writing interpreter felt
# like using -- hence the both-quotes tolerance.
SPAWN_RE = re.compile(r"^#\s*spawn:\s*(\[.*\])\s*$")
ARGV_RE = re.compile(r"[\"'](--[a-z0-9-]+)[\"']\s*,\s*[\"']([^\"']*)[\"']")

TAIL_BYTES = 256 * 1024

EXIT_OK = 0
EXIT_NO_SOURCE = 1
EXIT_CHANGED = 1  # --check only: something would have been written


def read_spawn(log_path):
    """The newest (port, api_key) the router announced, or (None, None).

    The tail is read rather than the whole file: the log grows without bound
    (one line per proxied request) and only the newest spawn line matters.
    """
    try:
        size = log_path.stat().st_size
    except OSError:
        return None, None
    if size <= 0:
        return None, None
    try:
        with open(log_path, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None, None

    spawn = None
    for line in chunk.splitlines():
        match = SPAWN_RE.match(line.strip())
        if match:
            spawn = match.group(1)
    if spawn is None:
        return None, None

    argv = {}
    for name, value in ARGV_RE.findall(spawn):
        argv[name] = value
    port = argv.get("--port")
    return (int(port) if port and port.isdigit() else None), (argv.get("--api-key") or None)


def endpoint_port(spec):
    """The port an endpoint's base_url talks to, or None."""
    try:
        return urlparse(str(spec.get("base_url") or "")).port
    except ValueError:
        return None


def masked(key):
    """Enough of a key to recognise it, not enough to leak it into a log."""
    if not key:
        return "(empty)"
    if len(key) <= 10:
        return key[:2] + "..."
    return "%s...%s" % (key[:6], key[-4:])


def load_config(path):
    """The existing config, or monitor's defaults when there is none yet."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), True
    return copy.deepcopy(monitor.DEFAULT_CONFIG), False


def write_config(path, config):
    """Replace config.json atomically, matching how the ledger writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp:
            json.dump(config, temp, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def sync(config, port, api_key, log_path, apply=True):
    """Apply (port, api_key) to the config; return the list of changes."""
    changes = []
    endpoints = config.get("endpoints") or []

    # Only the endpoint that talks to this port is the router; matching on the
    # port rather than on the id keeps a renamed or second endpoint working.
    target = None
    for spec in endpoints:
        if isinstance(spec, dict) and endpoint_port(spec) == port:
            target = spec
            break
    if target is None and endpoints:
        target = endpoints[0]

    if target is not None:
        current = str(target.get("api_key") or "")
        if api_key and current != api_key:
            changes.append("endpoint %r: api_key %s -> %s"
                           % (target.get("id"), masked(current), masked(api_key)))
            if apply:
                target["api_key"] = api_key
        elif not api_key:
            changes.append("no api-key in the log -- nothing to copy")

    # The live endpoint follows a log file, and the built-in default used to be
    # one machine's absolute path.  Repoint it only when the configured path is
    # gone and the real one is there, so a deliberate override is left alone.
    live = config.get("live")
    if not isinstance(live, dict):
        # A config.json written before the live endpoint existed carries no
        # section for it, and the dashboard quietly falls back to
        # DEFAULT_CONFIG for the value.  The repoint has to consider that same
        # effective path, or the stale default survives on exactly the machines
        # that have it -- which is how the model card stayed empty.
        live = copy.deepcopy(monitor.DEFAULT_CONFIG.get("live") or {})
        config["live"] = live
    configured = Path(str(live.get("log") or ""))
    if log_path.exists() and str(configured) != str(log_path) \
            and not configured.exists():
        changes.append("live.log: %s -> %s" % (configured, log_path))
        if apply:
            live["log"] = str(log_path)

    return changes


def main():
    parser = argparse.ArgumentParser(
        description="Copy the running router's port and api-key into config.json")
    parser.add_argument("--config", default=str(monitor.CONFIG_PATH))
    parser.add_argument("--log", default=None,
                        help="router log to read (default: the first one that exists)")
    parser.add_argument("--check", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    config_path = Path(args.config)
    # The router log is not in the same place on every install, and a wrong
    # guess here fails silently: read_spawn finds nothing, the sync reports "no
    # source", and the panel keeps a key the router has already rotated away
    # from.  So borrow the same resolution the dashboard itself uses rather
    # than pinning one machine's layout.
    log_path = Path(args.log) if args.log else monitor.resolve_live_log()[0]

    try:
        config, existed = load_config(config_path)
    except (OSError, ValueError) as exc:
        print("cannot read %s -- %s: %s" % (config_path, type(exc).__name__, exc))
        return EXIT_NO_SOURCE

    port, api_key = read_spawn(log_path)
    if not api_key:
        print("no '# spawn:' api-key in %s -- leaving %s alone"
              % (log_path, config_path.name))
        return EXIT_NO_SOURCE

    changes = sync(config, port, api_key, log_path, apply=not args.check)
    if not changes:
        print("%s already matches the router (port %s, key %s)"
              % (config_path.name, port, masked(api_key)))
        return EXIT_OK

    for line in changes:
        print("  " + line)
    if args.check:
        print("--check: %s left untouched" % config_path.name)
        return EXIT_CHANGED

    write_config(config_path, config)
    print("wrote %s%s" % (config_path.name, "" if existed else " (first run)"))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
