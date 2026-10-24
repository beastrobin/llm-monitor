"""Durable token ledger built from llama-server counters.

The server exports its token totals as process-lifetime monotonic counters, so
they fall back to zero whenever the server restarts or a router unloads a
model.  This module folds every new counter reading into a ledger that outlives
both the server and this dashboard.

The detail that makes the fold survive a dashboard restart is that the last raw
counter value is persisted right next to the totals.  Without it the dashboard
would either lose whatever happened while it was down, or count the whole
process-lifetime total a second time on every start.

Each delta is booked into several buckets at once.  The lifetime, per-day and
per-model sums are the obvious ones; on top of those every delta is also
stamped with the GPU power limit that was in force at that moment, because
"how much did I run at 450 W" cannot be recovered after the fact from anything
the engine reports.  That stamping is temporal, not spatial: a power limit is a
whole-card property shared by every model loaded on it, so the number means
"tokens produced while the card was capped at X", never "tokens this model
consumed X watts worth of".

Failure policy: every file operation is best-effort.  A dashboard that cannot
persist still has to render, so a broken ledger degrades to in-memory counting
and reports ``persisted: false`` instead of raising.
"""

import json
import os
import time

# Ledger fields, named to match the keys ``ModelSampler`` already produces so
# the mapping between the two stays obvious.
#
# The two *_seconds entries are the engine's own elapsed time, not wall clock:
# they only advance while the engine is actually working.  That is what makes a
# rate derived from them honest -- idle seconds never get averaged into the
# denominator, so a quiet afternoon does not look like a slow model.
FIELDS = (
    "prompt_tokens",
    "predicted_tokens",
    "cached_tokens",
    "decode_steps",
    "prompt_seconds",
    "predicted_seconds",
)

# Which token total pairs with which duration to form a rate.
SPEED_PAIRS = (
    ("predicted_tokens", "predicted_seconds"),
    ("prompt_tokens", "prompt_seconds"),
)

# Writing on every sample (2 s apart) is pointless churn, and a few seconds of
# exposure to a hard kill is an acceptable trade.
FLUSH_SECONDS = 15.0
DEFAULT_KEEP_DAYS = 90

# Combos are (day, power tier, model) triples.  The count is bounded in normal
# use, but a script ramping the power limit one watt at a time would otherwise
# grow the file without limit, so both the stored set and the reported slice
# have caps.
COMBO_CAP = 3000
COMBO_LIMIT = 60
TIER_CAP = 128

# Power limit could not be read for this sample.  Kept as a real bucket rather
# than dropped, so "unknown" stays visible instead of being folded into a tier
# the work did not actually run at.
TIER_UNKNOWN = 0


def _blank():
    return dict((field, 0) for field in FIELDS)


def _ints(source):
    """Keep only the numeric entries of one bucket, as ints."""
    out = _blank()
    if isinstance(source, dict):
        for field in FIELDS:
            value = source.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[field] = int(value)
    return out


def _tier_key(watts):
    """Normalise a power limit into a bucket key.

    nvidia-smi reports ``450.00 W`` while the preset menu says ``450``; both
    have to land in the same bucket, so the value is rounded.  Anything
    unreadable degrades to the unknown bucket.
    """
    if isinstance(watts, bool) or not isinstance(watts, (int, float)):
        return TIER_UNKNOWN
    if watts <= 0:
        return TIER_UNKNOWN
    try:
        return int(round(float(watts)))
    except (TypeError, ValueError, OverflowError):
        return TIER_UNKNOWN


def _is_tier_key(key):
    """True when a persisted tier key is a plain non-negative integer."""
    try:
        return int(key) >= 0
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Energy integration for the 能耗面板 tab.
#
# The GPU's watts are NVML's measured draw; everything machine-level is an
# ESTIMATE.  This host is Windows, which exposes no RAPL-style per-package
# power telemetry to userland, so the CPU line comes from a flat utilisation
# model (idle floor + linear ramp to a package-style ceiling) and the platform
# constant covers board, RAM, SSDs and fans.  The UI labels these numbers 估算
# and the tooltip says why; only the GPU column is a measurement.  The model
# constants live here, next to the integrator, so a future correction changes
# the books and the display together.
#
# Coverage rule: watts are integrated only while the monitor samples, exactly
# like the token books.  A day the dashboard never ran shows no energy, which
# is why every energy row also carries the sampled seconds behind its hours.
# --------------------------------------------------------------------------
PLATFORM_W = 130.0        # board/RAM/SSD/fan baseline, machine-level estimate
CPU_BASE_W = 15.0         # CPU estimate at 0 % load
CPU_SLOPE_W = 65.0        # extra CPU watts on top of the floor at 100 % load
PSU_EFFICIENCY = 0.90     # wall = machine / efficiency (typical 80+ tier)
ENERGY_PRICE_CNY_PER_KWH = 0.60   # display assumption, labelled as such in UI
POWER_DT_CAP_S = 30.0     # never integrate across a longer gap than this


def _blank_energy():
    """One day's energy book (watt-hours; float, not the int token shape)."""
    return {"gpu_wh": 0.0, "cpu_est_wh": 0.0, "machine_wh": 0.0,
            "hours": 0.0, "samples": 0}


def _energy_fields(data):
    """Sanitise one persisted energy bucket, keeping unknown keys out."""
    bucket = _blank_energy()
    for field in bucket:
        value = data.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and value >= 0:
            bucket[field] = float(value) if field != "samples" else int(value)
    return bucket


def readings_for(endpoints):
    """Extract one ledger reading per (endpoint, model) from a single sample.

    A model that has not reported yet is skipped rather than booked as zero --
    a zero baseline would look like a counter reset on the next sample.
    """
    readings = []
    for endpoint in endpoints or []:
        endpoint_id = endpoint.get("id")
        for model in endpoint.get("models") or []:
            model_id = model.get("id")
            if not endpoint_id or not model_id:
                continue
            metrics = model.get("metrics") or {}
            counters = metrics.get("counters") or {}
            readings.append((
                "%s/%s" % (endpoint_id, model_id),
                {
                    "prompt_tokens": counters.get("prompt_tokens"),
                    "predicted_tokens": counters.get("predicted_tokens"),
                    "cached_tokens": counters.get("prompt_cached"),
                    "decode_steps": metrics.get("n_decode_total"),
                    "prompt_seconds": counters.get("prompt_seconds"),
                    "predicted_seconds": counters.get("predicted_seconds"),
                },
            ))
    return readings


class TokenLedger(object):
    """Accumulates llama-server counters into daily, per-model and per-tier totals."""

    def __init__(self, path, keep_days=DEFAULT_KEEP_DAYS):
        self.path = path
        self.keep_days = keep_days
        self._seen = {}          # "endpoint/model" -> {field: raw counter}
        self._totals = _blank()  # field -> lifetime sum
        self._daily = {}         # "YYYY-MM-DD" -> field -> sum
        self._models = {}        # "endpoint/model" -> field -> sum
        self._tiers = {}         # "watts" -> field -> sum
        self._combos = {}        # "day|watts|endpoint/model" -> field -> sum
        self._energy = {}        # "YYYY-MM-DD" -> energy bucket (floats)
        self._power_ts = None    # last observe_power() timestamp
        self._tier_since = ""    # when tier attribution first booked anything
        self._resets = 0
        self._since = None
        self._dirty = False
        self._last_flush = 0.0
        self._error = ""
        # Payload cache: the dashboard calls payload() every sample even when
        # nothing changed, and rebuilding the combo rows is the one part of
        # the report that costs real time.  A generation counter is bumped at
        # every place state can change, so a cached payload is returned until
        # something actually moved.
        self._gen = 0
        self._payload_cache = None
        self._payload_day = None
        self._payload_gen = -1
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self):
        """Restore a previous ledger, ignoring anything malformed.

        The file is user-visible and survives upgrades, so a hand-edit or an
        older layout must not take the dashboard down.  A v1 file simply has no
        tier or combo buckets; the odometer fields it does have are read as
        usual and the new dimensions start counting from this run, which is the
        only honest thing to do -- nobody recorded the old power limits.
        """
        if not self.path:
            self._error = "no_path"
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - a bad file is not fatal
            self._error = "read_failed: %s" % exc
            return
        if not isinstance(data, dict):
            self._error = "bad_format"
            return

        for key, fields in (data.get("seen") or {}).items():
            if isinstance(fields, dict):
                self._seen[key] = dict(
                    (field, int(value)) for field, value in fields.items()
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool))
        if isinstance(data.get("totals"), dict):
            self._totals = _ints(data["totals"])
        for day, fields in (data.get("daily") or {}).items():
            if isinstance(fields, dict):
                self._daily[day] = _ints(fields)
        for key, fields in (data.get("models") or {}).items():
            if isinstance(fields, dict):
                self._models[key] = _ints(fields)
        for key, fields in (data.get("tiers") or {}).items():
            if isinstance(fields, dict) and _is_tier_key(key):
                self._tiers[key] = _ints(fields)
        for key, fields in (data.get("combos") or {}).items():
            if isinstance(fields, dict) and len(key.split("|", 2)) == 3:
                self._combos[key] = _ints(fields)
        for day, fields in (data.get("energy") or {}).items():
            if isinstance(fields, dict):
                self._energy[day] = _energy_fields(fields)
        if isinstance(data.get("tier_since"), str):
            self._tier_since = data["tier_since"]
        if isinstance(data.get("resets"), (int, float)):
            self._resets = int(data["resets"])
        if isinstance(data.get("since"), str):
            self._since = data["since"]
        self._trim()

    def flush(self, force=False):
        """Persist the ledger.  Returns True when a write actually happened.

        Writes go to a sibling temp file and are then renamed, so a crash
        mid-write cannot leave a truncated ledger behind for the next start.
        """
        if not self.path:
            return False
        now = time.time()
        if not force:
            if not self._dirty:
                return False
            if now - self._last_flush < FLUSH_SECONDS:
                return False
        payload = {
            "version": 3,
            "since": self._since,
            "tier_since": self._tier_since,
            "resets": self._resets,
            "totals": dict(self._totals),
            "daily": self._daily,
            "models": self._models,
            "tiers": self._tiers,
            "combos": self._combos,
            "energy": self._energy,
            "seen": self._seen,
        }
        tmp = self.path + ".tmp"
        try:
            # A configured subdirectory is a reasonable thing to ask for, and
            # failing the whole ledger over a missing folder would be a poor
            # trade, so create it rather than degrade to memory-only.
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2,
                          sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001 - stay up without persistence
            self._error = "write_failed: %s" % exc
            self._gen += 1
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self._error = ""
        self._dirty = False
        self._last_flush = now
        self._gen += 1
        return True

    # -- folding ----------------------------------------------------------

    def reset(self):
        """Zero every bucket while keeping the raw-counter baselines.

        The dashboard's clear button calls this.  Only the books are wiped:
        ``_seen`` survives on purpose, because the ledger books deltas off
        the last raw counter it read.  Without that baseline the very next
        sample would see the engine's whole process-lifetime counter as new
        work and book all of it back in -- a clear that instantly refills
        with someone else's history.

        Thread note: ``observe`` runs on the sampler thread while this runs
        on an HTTP handler thread.  The reassignments are atomic under the
        GIL; the worst interleaving costs one in-flight 2 s sample, which
        is acceptable next to the alternative of locking every fold.
        """
        self._totals = _blank()
        self._daily = {}
        self._models = {}
        self._tiers = {}
        self._combos = {}
        self._energy = {}
        self._tier_since = ""
        self._resets = 0
        self._since = None       # the next observe re-stamps it
        self._dirty = True
        self._gen += 1
        return self.flush(force=True)

    def observe(self, readings, now=None, watts=None):
        """Fold one sample's raw counters into the ledger.

        ``watts`` is the GPU power limit in force during this sample.  It is
        read once per sample rather than per model because it is a property of
        the card, and it is stamped onto every delta from this sample.  A limit
        changed mid-interval therefore lands entirely on the new tier for that
        interval -- with a two-second interval the error is one interval long,
        which is the best any sampler can do without driver-side timestamps.
        """
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        tier = _tier_key(watts)
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        day_bucket = self._daily.setdefault(day, _blank())
        for key, counters in readings:
            track = self._seen.setdefault(key, {})
            # Captured before the loop: the loop adds to ``track`` as it goes,
            # so asking again later would call a brand-new pair "known" from
            # its second field onwards.
            fresh_pair = not track
            model_bucket = self._models.setdefault(key, _blank())
            # One reset per pair per sample: a server restart rolls every
            # counter back at the same instant, so counting per field would
            # report four times the restarts that actually happened.
            reset = False
            added = _blank()
            for field in FIELDS:
                current = counters.get(field)
                if current is None:
                    continue
                try:
                    current = int(current)
                except (TypeError, ValueError):
                    continue
                previous = track.get(field)
                if previous is None:
                    # Two very different situations look the same here.
                    #
                    # A pair the ledger has never seen starts from whatever the
                    # counter already carries: starting at zero would silently
                    # drop the work the engine did before the dashboard came
                    # up.
                    #
                    # A *known* pair missing one field is not that.  It means
                    # this version added the field, or the engine only just
                    # started exporting it.  Claiming the whole counter there
                    # would invent history -- on the run that introduced
                    # engine-seconds it booked minutes of work against zero
                    # tokens and every derived rate read as "no data".  So the
                    # value is taken as the baseline and nothing is booked.
                    delta = current if fresh_pair else 0
                elif current >= previous:
                    delta = current - previous
                else:
                    # The counter went backwards, so the server restarted or
                    # a router unloaded the model.  Count from zero again
                    # rather than booking a negative interval.
                    delta = current
                    reset = True
                track[field] = current
                if delta > 0:
                    added[field] = delta
            if not any(added.values()):
                if reset:
                    self._resets += 1
                continue
            combo_key = "%s|%s|%s" % (day, tier, key)
            combo_bucket = self._combos.setdefault(combo_key, _blank())
            tier_bucket = self._tiers.setdefault(str(tier), _blank())
            if not self._tier_since:
                self._tier_since = stamp
            for field in FIELDS:
                delta = added[field]
                if not delta:
                    continue
                self._totals[field] += delta
                day_bucket[field] += delta
                model_bucket[field] += delta
                tier_bucket[field] += delta
                combo_bucket[field] += delta
            self._dirty = True
            self._gen += 1
            if reset:
                self._resets += 1
        if self._since is None:
            self._since = day
        self._trim()

    def observe_power(self, gpu_w=None, cpu_pct=None, now=None):
        """Integrate one sample's power draw into the daily energy books.

        ``gpu_w`` is the card's measured NVML draw; ``cpu_pct`` drives the
        CPU *estimate* described on the constants above.  The interval is the
        wall time since the previous call, capped at POWER_DT_CAP_S: a sleep,
        a stalled sampler or a long HTTP timeout must not book watts for time
        nobody was measuring, so the books simply lose coverage there rather
        than inventing energy.  The first call only sets the baseline.

        Machine watts always integrate platform + GPU + CPU, using each term's
        floor when that sensor is missing (platform alone when both are down),
        so a dropped GPU read shrinks the estimate instead of zeroing it.
        """
        if now is None:
            now = time.time()
        previous, self._power_ts = self._power_ts, now
        if previous is None:
            return
        dt = now - previous
        if dt <= 0:
            return
        if dt > POWER_DT_CAP_S:
            dt = POWER_DT_CAP_S

        def _clamped(value):
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return None

        gpu = _clamped(gpu_w) if gpu_w is not None else None
        cpu_pct = _clamped(cpu_pct) if cpu_pct is not None else None
        cpu = (CPU_BASE_W + CPU_SLOPE_W * min(cpu_pct, 100.0) / 100.0
               if cpu_pct is not None else None)
        if gpu is None and cpu is None:
            return

        hours = dt / 3600.0
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        bucket = self._energy.setdefault(day, _blank_energy())
        if gpu is not None:
            bucket["gpu_wh"] += gpu * hours
        if cpu is not None:
            bucket["cpu_est_wh"] += cpu * hours
        bucket["machine_wh"] += ((gpu if gpu is not None else 0.0)
                                 + (cpu if cpu is not None else CPU_BASE_W)
                                 + PLATFORM_W) * hours
        bucket["hours"] += hours
        bucket["samples"] += 1
        self._dirty = True
        self._gen += 1

    def _trim(self):
        """Drop daily buckets past the retention window.

        The early return keys off the *oldest* bucket rather than how many
        there are: one stale bucket plus today's is only two entries, so a
        count-based guard would keep an ancient bucket around indefinitely.
        """
        if self.keep_days > 0 and self._daily:
            cutoff = time.strftime("%Y-%m-%d", time.localtime(
                time.time() - self.keep_days * 86400.0))
            if min(self._daily) < cutoff:
                for day in sorted(d for d in self._daily if d < cutoff):
                    del self._daily[day]
                    self._dirty = True
                for key in sorted(k for k in self._combos
                                  if k.split("|", 2)[0] < cutoff):
                    del self._combos[key]
                    self._dirty = True
                self._gen += 1
        # Energy days ride the same retention window as the token days; the
        # check is separate because a machine that only ever idles can have
        # energy buckets on days no token was ever booked.
        if self.keep_days > 0 and self._energy:
            cutoff = time.strftime("%Y-%m-%d", time.localtime(
                time.time() - self.keep_days * 86400.0))
            stale = sorted(day for day in self._energy if day < cutoff)
            if stale:
                for day in stale:
                    del self._energy[day]
                self._dirty = True
                self._gen += 1
        # Tiers are lifetime totals, so they outlive the day window.  The cap
        # only exists for the pathological case of an unbounded number of
        # distinct limits ever set on the card.
        if len(self._tiers) > TIER_CAP:
            ranked = sorted(self._tiers.items(),
                            key=lambda item: (item[1]["predicted_tokens"],
                                              item[0]), reverse=True)
            self._tiers = dict(ranked[:TIER_CAP])
            self._dirty = True
            self._gen += 1
        if len(self._combos) > COMBO_CAP:
            # Keys start with the day, so dropping the smallest drops the
            # oldest first.
            for key in sorted(self._combos)[:len(self._combos) - COMBO_CAP]:
                del self._combos[key]
            self._dirty = True
            self._gen += 1

    # -- reporting --------------------------------------------------------

    @staticmethod
    def _speed(tokens, seconds):
        """Tokens per engine-second, or None when it cannot be computed."""
        try:
            tokens = float(tokens or 0)
            seconds = float(seconds or 0)
        except (TypeError, ValueError):
            return None
        if tokens <= 0 or seconds <= 0:
            return None
        return tokens / seconds

    def _detail(self, bucket):
        """One bucket as the flat dict the UI consumes."""
        row = dict((field, bucket[field]) for field in FIELDS)
        for tokens, seconds in SPEED_PAIRS:
            name = tokens.split("_")[0] + "_tps"
            row[name] = self._speed(bucket[tokens], bucket[seconds])
        return row

    def _recent(self, limit):
        out = []
        for day in sorted(self._daily)[-limit:]:
            bucket = self._daily[day]
            out.append({
                "day": day,
                "predicted_tokens": bucket["predicted_tokens"],
                "prompt_tokens": bucket["prompt_tokens"],
            })
        out.reverse()
        return out

    def _energy_rows(self, limit):
        """Newest-first daily energy rows; wall is derived, never booked."""
        rows = []
        for day in sorted(self._energy)[-limit:]:
            bucket = self._energy[day]
            rows.append({
                "day": day,
                "gpu_wh": round(bucket["gpu_wh"], 2),
                "cpu_est_wh": round(bucket["cpu_est_wh"], 2),
                "machine_wh": round(bucket["machine_wh"], 2),
                "wall_wh": round(bucket["machine_wh"] / PSU_EFFICIENCY, 2),
                "hours": round(bucket["hours"], 2),
                "samples": bucket["samples"],
            })
        rows.reverse()
        return rows

    def _tier_rows(self):
        rows = []
        for key, bucket in self._tiers.items():
            row = self._detail(bucket)
            row["watts"] = int(key)
            rows.append(row)
        rows.sort(key=lambda row: row["watts"], reverse=True)
        return rows

    def _combo_rows(self, limit):
        rows = []
        for key, bucket in self._combos.items():
            parts = key.split("|", 2)
            if len(parts) != 3:
                continue
            endpoint, _, model = parts[2].partition("/")
            row = self._detail(bucket)
            row["day"] = parts[0]
            row["watts"] = int(parts[1])
            row["endpoint"] = endpoint
            row["model"] = model or parts[2]
            rows.append(row)
        # Newest day first, then the busiest row of that day, so the top of the
        # table is what just happened.
        rows.sort(key=lambda row: (row["day"], row["watts"],
                                   row["predicted_tokens"]), reverse=True)
        return rows[:limit]

    def _model_rows(self, limit):
        """Per-model lifetime totals, ranked by output volume.

        The UI's TOP-3 tables read this: one row per (endpoint, model) with the
        same flat fields as the combo rows, so the front end can render both
        rankings from a single list without doing its own aggregation.
        """
        rows = []
        for key, bucket in self._models.items():
            endpoint, _, model = key.partition("/")
            row = self._detail(bucket)
            row["endpoint"] = endpoint
            row["model"] = model or key
            rows.append(row)
        rows.sort(key=lambda row: (row["predicted_tokens"], row["model"]),
                  reverse=True)
        return rows[:limit]

    def payload(self):
        day = time.strftime("%Y-%m-%d")
        # The combo-row build is the expensive part; return a cached payload
        # until either the state or the calendar day actually changes.
        if self._payload_cache is not None \
                and self._payload_day == day and self._payload_gen == self._gen:
            return self._payload_cache
        payload = {
            "available": True,
            "persisted": bool(self.path) and not self._error,
            "error": self._error,
            "path": self.path or "",
            "today": day,
            "today_totals": dict(self._daily.get(day) or _blank()),
            "totals": dict(self._totals),
            "models": dict(self._models),
            "tiers": self._tier_rows(),
            "combos": self._combo_rows(COMBO_LIMIT),
            "model_rows": self._model_rows(12),
            "tier_since": self._tier_since,
            "resets": self._resets,
            "since": self._since,
            "days": len(self._daily),
            "recent": self._recent(7),
            "energy_rows": self._energy_rows(7),
            "energy_price_cny_per_kwh": ENERGY_PRICE_CNY_PER_KWH,
            # The estimate model itself, so the live "now" cards in the UI
            # draw their numbers from the same constants the books use --
            # display and ledger are not allowed to disagree.
            "energy_model": {
                "platform_w": PLATFORM_W,
                "cpu_base_w": CPU_BASE_W,
                "cpu_slope_w": CPU_SLOPE_W,
                "psu_efficiency": PSU_EFFICIENCY,
            },
        }
        self._payload_cache = payload
        self._payload_day = day
        self._payload_gen = self._gen
        return payload
