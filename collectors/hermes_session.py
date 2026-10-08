"""Read the latest desktop session's local inference route without modifying Hermes."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse


def latest_desktop_selection(home):
    """Return (local base URL, model name, provider) for the latest session.

    Desktop model switches are session-scoped and need not update config.yaml.
    With multiple sessions, last activity is our observable ordering; it does
    not claim to identify the foreground tab. Never read conversation messages
    or take an API key from session metadata.
    """
    database = Path(home) / "state.db"
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0.1)) as connection:
            row = connection.execute("""
                SELECT model_config, billing_base_url, model
                FROM sessions
                WHERE source = 'desktop' AND ended_at IS NULL
                  AND COALESCE(archived, 0) = 0 AND COALESCE(hidden, 0) = 0
                ORDER BY COALESCE(last_activity_at, started_at) DESC, started_at DESC
                LIMIT 1
            """).fetchone()
    except (OSError, sqlite3.Error):
        return None, None, None
    if row is None:
        return None, None, None
    try:
        config = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None, row[2], None
    if not isinstance(config, dict):
        return None, row[2], None
    runtime = config.get("gateway_runtime")
    if not isinstance(runtime, dict) or not runtime.get("provider"):
        runtime = config
    provider = runtime.get("provider")
    url = runtime.get("base_url")
    # Billing may describe the previous provider after a switch. Use it only
    # for sessions that have never persisted an explicit runtime selection.
    if not url and not runtime.get("provider"):
        url = row[1]
    if not isinstance(url, str):
        return None, row[2], provider
    try:
        parsed = urlparse(url)
        if (parsed.scheme not in ("http", "https")
                or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.path not in ("", "/", "/v1", "/v1/")
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return None, row[2], None
        parsed.port  # Reject malformed ports before handing the URL to the hub.
    except ValueError:
        return None, row[2], None
    return url, row[2], provider
