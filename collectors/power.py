"""GPU power limit control.

The only part of the dashboard that writes instead of reads.  Everything here
returns a result dict rather than raising: a failed power change is a normal
outcome the UI has to render, not an exception.

Two facts drive the design:

* ``nvidia-smi -pl`` needs an elevated token on Windows.  Under a restricted
  token it exits with code 5 (ERROR_ACCESS_DENIED) before it touches the
  driver, so the privilege check runs first and the caller gets a precise
  reason instead of a generic failure.
* The limits are hardware dependent and can differ from the presets baked in
  here, so the range is read back from the card before anything is applied.
  On a card whose ``power.min_limit`` is above 150 W the eco preset is refused
  rather than silently clamped.

Error codes returned in ``error`` (the UI owns the human wording, which keeps
this module free of non-ASCII text):

``not_a_preset``   watts is not one of PRESETS
``not_elevated``   the process cannot change driver settings
``no_gpu``         nvidia-smi did not report a readable power limit
``out_of_range``   the preset falls outside this card's allowed range
``smi_failed``     nvidia-smi rejected the change for some other reason
"""

import ctypes
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# A fixed menu rather than free-form wattage.  The control is a segmented
# button group, and a whitelist means a malformed or hostile request cannot
# ask for 1 W or 100000 W.
PRESETS = [
    {"id": "eco", "watts": 150},
    {"id": "quiet", "watts": 300},
    {"id": "balanced", "watts": 450},
    {"id": "full", "watts": 600},
]
PRESET_WATTS = tuple(p["watts"] for p in PRESETS)
DEFAULT_WATTS = 600


def is_elevated():
    """True when this process can change driver settings."""
    if os.name != "nt":
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(args, timeout=10.0):
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "%s: %s" % (type(exc).__name__, exc)
    text = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, text.strip()


def _number(raw):
    text = (raw or "").strip()
    if not text or text.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_limits(index=0):
    """Return ``(current, minimum, maximum)`` in watts.

    Any element is None when the driver did not report it -- notably under a
    restricted token, where even the query is denied.
    """
    rc, out = _run([
        "nvidia-smi", "-i", str(index),
        "--query-gpu=power.limit,power.min_limit,power.max_limit",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return None, None, None
    line = next((ln for ln in out.splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None, None, None
    return _number(parts[0]), _number(parts[1]), _number(parts[2])


def _result(ok, error=None, **extra):
    payload = {"ok": ok, "error": error}
    payload.update(extra)
    return payload


def set_limit(watts, index=0):
    """Apply one preset and report what the card actually ended up at.

    ``applied`` is read back from nvidia-smi rather than echoed from the
    request: the driver is free to refuse or round, and the UI highlights the
    active preset from measured state, so a lie here would show up as a button
    that looks stuck.
    """
    try:
        watts = int(watts)
    except (TypeError, ValueError):
        return _result(False, "not_a_preset", requested=None)

    if watts not in PRESET_WATTS:
        return _result(False, "not_a_preset", requested=watts)

    if not is_elevated():
        return _result(False, "not_elevated", requested=watts)

    current, low, high = read_limits(index)
    if current is None and low is None and high is None:
        return _result(False, "no_gpu", requested=watts)

    # Guard before writing: a preset outside this card's window would either be
    # rejected by the driver or, worse, silently clamped to something the user
    # did not pick.
    if low is not None and watts < low:
        return _result(False, "out_of_range", requested=watts,
                       minimum=low, maximum=high)
    if high is not None and watts > high:
        return _result(False, "out_of_range", requested=watts,
                       minimum=low, maximum=high)

    rc, out = _run(["nvidia-smi", "-i", str(index), "-pl", str(watts)])
    if rc != 0:
        # Exit code 5 is ERROR_ACCESS_DENIED.  is_elevated() covers the common
        # case, but a policy or driver-level block can still reject the write.
        if rc == 5 or "permission" in out.lower():
            return _result(False, "not_elevated", requested=watts, detail=out)
        return _result(False, "smi_failed", requested=watts, detail=out)

    applied, _, _ = read_limits(index)
    return _result(
        True,
        requested=watts,
        applied=applied,
        previous=current,
        minimum=low,
        maximum=high,
    )
