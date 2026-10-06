"""GPU power limit control.

The only part of the dashboard that writes instead of reads.  Everything here
returns a result dict rather than raising: a failed power change is a normal
outcome the UI has to render, not an exception.

Four facts drive the design:

* ``nvidia-smi -pl`` needs an elevated token on Windows.  Under a restricted
  token it exits with code 5 (ERROR_ACCESS_DENIED) before it touches the
  driver, so the privilege check runs first and the caller gets a precise
  reason instead of a generic failure.
* The limits are hardware dependent, so the range is read back from the card
  before anything is applied, and the *menu* is derived from that range rather
  than hard-coded -- see ``presets_for``.
* Laptop GPUs refuse a software power limit outright.  The driver answers

      Changing power management limit is not supported in current scope

  and then -- this is the trap -- **still exits 0** and prints "All done."
  Only the text says no, so the text is parsed; a plain return-code check
  reports success for a change that never happened.
* Power read-back is two different columns.  ``power.limit`` is the software
  limit, which laptop GPUs never report; ``enforced.power.limit`` is the
  ceiling actually being held, which every card reports.  When the first is
  unreadable the second still bounds the write, so it is used as a best-effort
  verification (see ``set_limit``).

Error codes returned in ``error`` (the UI owns the human wording, which keeps
this module free of non-ASCII text):

``not_a_preset``   watts is not one of the presets offered for this card
``not_elevated``   the process cannot change driver settings
``no_gpu``         nvidia-smi did not report a readable power limit
``out_of_range``   the preset falls outside this card's allowed range
``unsupported``    the driver does not accept a software power limit (laptops)
``not_applied``    the command was accepted but the card is still at another limit
``smi_failed``     nvidia-smi rejected the change for some other reason
"""

import ctypes
import math
import os
import subprocess
import time

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# The ceiling sweep measured on a 600 W-class card.  These four values carry
# real data: the 150-600 W decode power curve (short and long context) flattens
# after ~450 W, and 400 W sits at the efficiency peak -- ~95% of peak speed at
# the best t/s-per-watt.  See power_curve/.  A card that spans this window
# keeps the menu; anything else gets one derived from its own limits.
#
# Balanced is 400 W, not 450 W, for the reason above.
TUNED_PRESETS = [
    {"id": "eco", "watts": 150},
    {"id": "quiet", "watts": 300},
    {"id": "balanced", "watts": 400},
    {"id": "full", "watts": 600},
]
TUNED_MAX_W = 575.0

# Where a derived menu lands inside the card's own window: 40 / 60 / 80 / 100 %
# of its maximum.  The four intents survive the rescaling -- an extreme quiet
# end, a mild daily cap, a near-peak setting, and the card's full TGP.  Snapped
# to 5 W, because that is the granularity the driver exposes anyway.
ROLE_IDS = ("eco", "quiet", "balanced", "full")
DERIVED_FRACTIONS = (0.40, 0.60, 0.80, 1.00)
WATTS_STEP = 5

# The power management agent always holds its ceiling at or below the software
# limit, so after a write the enforced ceiling is a usable witness even when the
# card will not report the software limit itself.  A ceiling this far above what
# was asked for means the write did not take.  The tolerance covers the wobble
# of Dynamic Boost and the driver's rounding.
VERIFY_TOLERANCE_W = 10.0
VERIFY_SETTLE_SECONDS = 0.5

# Substrings that mean "the driver said no" regardless of the exit code.
REFUSAL_MARKERS = (
    "not supported", "unsupported", "does not support", "not permitted",
    "not allowed", "permission",
)

# Query order matters only for readability; every value is optional.
POWER_QUERY = (
    "power.limit,enforced.power.limit,power.min_limit,power.max_limit,"
    "power.default_limit"
)


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
        value = float(text)
    except ValueError:
        return None
    # JSON has no NaN literal and the browser's parser rejects one, so a
    # non-finite reading would take the whole panel down instead of one card.
    return value if math.isfinite(value) else None


def read_state(index=0):
    """Read the card's power-limit state in watts.

    ``requested`` is the software limit (what -pl writes) and is None on cards
    that never report it; ``enforced`` is the ceiling in force right now and is
    what the dashboard draws the power bar against.  ``minimum``, ``maximum``
    and ``default`` describe the window.  Every field is None when the driver
    did not report it -- notably under a restricted token on some setups, where
    even the query is denied.
    """
    empty = {"requested": None, "enforced": None, "minimum": None,
             "maximum": None, "default": None}
    rc, out = _run([
        "nvidia-smi", "-i", str(index), "--query-gpu=" + POWER_QUERY,
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return empty
    line = next((ln for ln in out.splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 5:
        return empty
    values = [_number(part) for part in parts[:5]]
    return dict(zip(("requested", "enforced", "minimum", "maximum", "default"),
                    values))


def presets_for(low, high):
    """The preset menu this card can actually accept, or [] when unreadable.

    The window comes from the card, so the menu has to as well: 150/300/400/600
    hard-coded on a card whose range is 5-175 W offers three buttons the driver
    has to refuse.  A card wide enough to cover the measured sweep keeps the
    tuned values, because those four carry the decode power curve; everything
    else is rescaled onto its own maximum.
    """
    if low is None or high is None or high <= low:
        return []

    if high >= TUNED_MAX_W:
        tuned = [dict(preset) for preset in TUNED_PRESETS
                 if low <= preset["watts"] <= high]
        if len(tuned) >= 2:
            for preset in tuned:
                preset["tuned"] = True
            return tuned

    menu, seen = [], set()
    for role, fraction in zip(ROLE_IDS, DERIVED_FRACTIONS):
        watts = int(round(high * fraction / WATTS_STEP)) * WATTS_STEP
        watts = int(max(low, min(high, watts)))
        if watts in seen:
            continue
        seen.add(watts)
        menu.append({"id": role, "watts": watts})
    return menu


def preset_watts(presets):
    return tuple(preset["watts"] for preset in (presets or []))


def _refusal(text):
    """True when the driver's own output says the change was refused.

    nvidia-smi exits 0 for a refused ``-pl`` on a laptop, so the exit code
    cannot be trusted; this is the check that can.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def _result(ok, error=None, **extra):
    payload = {"ok": ok, "error": error}
    payload.update(extra)
    return payload


def set_limit(watts, index=0, presets=None):
    """Apply one preset and report what the card actually ended up at.

    ``presets`` is the menu the caller published to the UI; passing it in keeps
    the accepted values and the offered buttons from drifting apart.  Without
    it the menu is derived from the card here.

    ``applied`` is read back from nvidia-smi rather than echoed from the
    request: the driver is free to refuse or round, and the UI highlights the
    active preset from measured state, so a lie here would show up as a button
    that looks stuck.  ``verified`` says whether that read-back could actually
    confirm the value -- on a laptop, where the software limit is not reported,
    it cannot, and the UI words the result as a request rather than a fact.
    """
    try:
        watts = int(watts)
    except (TypeError, ValueError):
        return _result(False, "not_a_preset", requested=None)

    if not presets:
        window = read_state(index)
        presets = presets_for(window["minimum"], window["maximum"])
    if not presets:
        # Nothing reported a window, so there is no whitelist to check against
        # and no honest way to say a value is acceptable.
        return _result(False, "no_gpu", requested=watts)
    if watts not in preset_watts(presets):
        return _result(False, "not_a_preset", requested=watts)

    if not is_elevated():
        return _result(False, "not_elevated", requested=watts)

    before = read_state(index)
    low, high = before["minimum"], before["maximum"]
    if low is None and high is None and before["requested"] is None \
            and before["enforced"] is None:
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
        if _refusal(out):
            return _result(False, "unsupported", requested=watts, detail=out)
        return _result(False, "smi_failed", requested=watts, detail=out)

    base = {"requested": watts, "previous": before["requested"],
            "minimum": low, "maximum": high}

    # Exit code 0 does not mean the driver did it: a laptop answers
    # "not supported in current scope" and still reports "All done."
    if _refusal(out):
        return _result(False, "unsupported", detail=out, **base)

    after = read_state(index)
    if after["requested"] is not None:
        if abs(after["requested"] - watts) >= 1:
            return _result(False, "not_applied", applied=after["requested"],
                           detail=out, **base)
        return _result(True, applied=after["requested"], verified=True,
                       enforced=after["enforced"], **base)

    # No software limit to read back.  The enforced ceiling still witnesses the
    # write: if the agent is holding something well above what we asked for,
    # the software limit is not in force.  Best effort -- the ceiling moves on
    # its own, so a mismatch is evidence but a match is not proof.
    time.sleep(VERIFY_SETTLE_SECONDS)
    after = read_state(index)
    ceiling = after["enforced"]
    if ceiling is not None and ceiling > watts + max(VERIFY_TOLERANCE_W,
                                                     watts * 0.05):
        return _result(False, "not_applied", applied=None, enforced=ceiling,
                       detail=out, **base)
    return _result(True, applied=None, verified=False, enforced=ceiling, **base)
