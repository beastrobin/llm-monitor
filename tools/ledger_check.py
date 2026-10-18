"""Self-check for collectors/stats.py.

The ledger's only job is deciding how much of a counter reading is new, and
every rule it uses is invisible from the dashboard -- a wrong fold still shows
a plausible-looking number.  So the rules get exercised directly here,
including the restart and reload cases that are painful to reproduce by hand.

Run:  python tools/ledger_check.py
"""

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from collectors import stats  # noqa: E402


FAILED = []


def check(name, got, want):
    if got == want:
        print("  ok    %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s -- got %r want %r" % (name, got, want))


def sample_set(*models):
    """Build one sample's endpoint payload the way the collectors emit it."""
    return [{
        "id": "hermes",
        "models": [
            {
                "id": model_id,
                "metrics": {
                    "n_decode_total": steps,
                    "counters": {
                        "prompt_tokens": prompt,
                        "predicted_tokens": predicted,
                        "prompt_cached": cached,
                    },
                },
            }
            for model_id, prompt, predicted, cached, steps in models
        ],
    }]


def timed_set(model_id, prompt, predicted, prompt_seconds, predicted_seconds,
              endpoint="hermes"):
    """One sample that carries engine durations as well as token counters."""
    return [{
        "id": endpoint,
        "models": [{
            "id": model_id,
            "metrics": {
                "n_decode_total": 0,
                "counters": {
                    "prompt_tokens": prompt,
                    "predicted_tokens": predicted,
                    "prompt_cached": 0,
                    "prompt_seconds": prompt_seconds,
                    "predicted_seconds": predicted_seconds,
                },
            },
        }],
    }]


def at(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


def totals_of(ledger, field):
    return ledger.payload()["totals"][field]


def tier_of(ledger, watts):
    for row in ledger.payload()["tiers"]:
        if row["watts"] == watts:
            return row
    return None


def main():
    print("counter folding")
    ledger = stats.TokenLedger("")
    ledger.observe(stats.readings_for(sample_set(("m1", 100, 200, 0, 0))))
    check("first sighting books the whole counter",
          totals_of(ledger, "predicted_tokens"), 200)
    check("...and the prompt counter too",
          totals_of(ledger, "prompt_tokens"), 100)

    ledger.observe(stats.readings_for(sample_set(("m1", 150, 260, 0, 0))))
    check("only the delta is added", totals_of(ledger, "predicted_tokens"), 260)
    check("prompt delta as well", totals_of(ledger, "prompt_tokens"), 150)

    ledger.observe(stats.readings_for(sample_set(("m1", 150, 260, 0, 0))))
    check("an unchanged counter adds nothing",
          totals_of(ledger, "predicted_tokens"), 260)
    check("...and is not a reset", ledger.payload()["resets"], 0)

    ledger.observe(stats.readings_for(sample_set(("m1", 10, 20, 0, 0))))
    check("a counter that goes backwards counts from zero",
          totals_of(ledger, "predicted_tokens"), 280)
    check("...and is booked as a reset", ledger.payload()["resets"], 1)

    print("missing data")
    fresh = stats.TokenLedger("")
    fresh.observe([("hermes/m1", {
        "prompt_tokens": None, "predicted_tokens": None,
        "cached_tokens": None, "decode_steps": None})])
    check("null counters are ignored, not booked as zero",
          totals_of(fresh, "predicted_tokens"), 0)

    fresh.observe(stats.readings_for(sample_set(("m1", 100, 200, 0, 0))))
    fresh.observe(stats.readings_for(
        sample_set(("m1", 100, 200, 0, 0), ("m2", 0, 50, 0, 0))))
    check("a model that starts reporting later is not a reset",
          fresh.payload()["resets"], 0)
    check("...and its whole counter is claimed",
          totals_of(fresh, "predicted_tokens"), 250)

    print("fields added to an existing ledger")
    # Regression: the run that introduced engine-seconds claimed the whole
    # lifetime duration for models that were already being tracked, because
    # "field never seen" and "pair never seen" were treated alike.  The tier
    # buckets ended up holding minutes of work against zero tokens, so every
    # rate rendered as "--".
    upgraded = stats.TokenLedger("")
    upgraded.observe([("hermes/m1", {
        "prompt_tokens": 100, "predicted_tokens": 200,
        "cached_tokens": 0, "decode_steps": 0})], watts=450)
    check("a pair tracked before the upgrade keeps its total",
          totals_of(upgraded, "predicted_tokens"), 200)
    upgraded.observe([("hermes/m1", {
        "prompt_tokens": 100, "predicted_tokens": 200,
        "cached_tokens": 0, "decode_steps": 0,
        "prompt_seconds": 30.0, "predicted_seconds": 12.0})], watts=450)
    check("a field the ledger has never seen is seeded, not claimed",
          totals_of(upgraded, "predicted_seconds"), 0)
    check("...so no duration appears out of nowhere",
          tier_of(upgraded, 450)["predicted_seconds"], 0)
    check("...and no rate is invented from it",
          tier_of(upgraded, 450)["predicted_tps"], None)
    upgraded.observe([("hermes/m1", {
        "prompt_tokens": 100, "predicted_tokens": 300,
        "cached_tokens": 0, "decode_steps": 0,
        "prompt_seconds": 40.0, "predicted_seconds": 17.0})], watts=450)
    check("...and from then on only the delta is booked",
          totals_of(upgraded, "predicted_seconds"), 5)
    # Pinned deliberately.  This bucket's 200 tokens predate the duration
    # field, so its ratio divides old work by new time.  A bucket born after
    # the upgrade holds both from the same instant and has no such skew, and
    # the tier and detail buckets are always born after it -- so this records
    # the shape of the transition rather than pretending it does not exist.
    check("a bucket whose tokens predate its durations reports the skew",
          tier_of(upgraded, 450)["predicted_tps"], 60.0)

    print("daily buckets")
    # Anchor on the real clock: today_totals is keyed by the current date, so
    # a hard-coded date would start failing tomorrow.
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    yesterday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    daily = stats.TokenLedger("")
    daily.observe(stats.readings_for(sample_set(("m1", 0, 100, 0, 0))),
                  now=now - 86400)
    daily.observe(stats.readings_for(sample_set(("m1", 0, 250, 0, 0))),
                  now=now)
    recent = daily.payload()["recent"]
    check("newest day first", recent[0]["day"], today)
    check("the earlier day is the one before", recent[1]["day"], yesterday)
    check("that day holds only its own delta",
          recent[0]["predicted_tokens"], 150)
    check("the earlier day keeps its own",
          recent[1]["predicted_tokens"], 100)
    check("today_totals follows the current date",
          daily.payload()["today_totals"]["predicted_tokens"], 150)

    trimmed = stats.TokenLedger("", keep_days=2)
    trimmed.observe(stats.readings_for(sample_set(("m1", 0, 10, 0, 0))),
                    now=at("2020-01-01 12:00"))
    trimmed.observe(stats.readings_for(sample_set(("m1", 0, 20, 0, 0))))
    check("a bucket older than the window is dropped",
          trimmed.payload()["days"], 1)

    print("speed (engine seconds)")
    speed = stats.TokenLedger("")
    speed.observe(stats.readings_for(timed_set("m1", 1000, 500, 10.0, 5.0)),
                  watts=450)
    speed.observe(stats.readings_for(timed_set("m1", 2000, 1500, 30.0, 15.0)),
                  watts=450)
    check("engine durations are folded alongside the tokens",
          totals_of(speed, "predicted_seconds"), 15)
    check("a bucket's rate is its tokens over its own engine seconds",
          tier_of(speed, 450)["predicted_tps"], 100.0)
    check("...and the same rule for prefill",
          round(tier_of(speed, 450)["prompt_tps"], 4), round(2000 / 30.0, 4))
    # The whole point of using engine seconds: idle samples add nothing to the
    # denominator, so a quiet stretch cannot drag the reported rate down.
    check("idle time cannot dilute the rate",
          tier_of(speed, 450)["predicted_tps"] > 99, True)

    nospeed = stats.TokenLedger("")
    nospeed.observe(stats.readings_for(sample_set(("m1", 0, 100, 0, 0))),
                    watts=300)
    check("no durations means no rate, not a made-up zero",
          tier_of(nospeed, 300)["predicted_tps"], None)

    print("power tier attribution")
    tiered = stats.TokenLedger("")
    tiered.observe(stats.readings_for(sample_set(("m1", 0, 100, 0, 0))),
                   watts=450)
    tiered.observe(stats.readings_for(sample_set(("m1", 0, 250, 0, 0))),
                   watts=300)
    tiered.observe(stats.readings_for(sample_set(("m1", 0, 400, 0, 0))),
                   watts=450)
    tiers = tiered.payload()["tiers"]
    check("each distinct limit becomes its own row",
          [row["watts"] for row in tiers], [450, 300])
    check("a tier holds only the deltas produced while it was in force",
          (tier_of(tiered, 450)["predicted_tokens"],
           tier_of(tiered, 300)["predicted_tokens"]), (250, 150))
    check("the lifetime total is still counted exactly once",
          totals_of(tiered, "predicted_tokens"), 400)
    check("tier rows add back up to it once every delta is stamped",
          sum(row["predicted_tokens"] for row in tiers), 400)

    check("a measured limit lands in the same bucket as its preset",
          stats._tier_key(449.99), 450)
    check("an unreadable limit stays its own bucket rather than joining one",
          [stats._tier_key(None), stats._tier_key(0), stats._tier_key(-5),
           stats._tier_key("450"), stats._tier_key(True)], [0, 0, 0, 0, 0])

    unknown = stats.TokenLedger("")
    unknown.observe(stats.readings_for(sample_set(("m1", 0, 50, 0, 0))))
    check("a sample with no readable limit is recorded, not dropped",
          unknown.payload()["tiers"][0]["watts"], 0)

    print("day x model x tier detail")
    detail = stats.TokenLedger("")
    detail.observe(
        stats.readings_for(sample_set(("m1", 0, 100, 0, 0), ("m2", 0, 40, 0, 0))),
        now=at("2026-09-26 12:00"), watts=450)
    detail.observe(
        stats.readings_for(sample_set(("m1", 0, 260, 0, 0), ("m2", 0, 90, 0, 0))),
        now=at("2026-09-27 12:00"), watts=300)
    rows = detail.payload()["combos"]
    check("one row per day/model/tier combination", len(rows), 4)
    check("the newest day leads the table", rows[0]["day"], "2026-09-27")
    lookup = dict(((row["day"], row["watts"], row["model"]),
                   row["predicted_tokens"]) for row in rows)
    check("a row holds only its own delta",
          lookup[("2026-09-26", 450, "m1")], 100)
    check("the second day is separated from the first",
          lookup[("2026-09-27", 300, "m1")], 160)
    check("a second model gets its own row",
          lookup[("2026-09-27", 300, "m2")], 50)
    check("the endpoint is split out of the model key",
          (rows[0]["endpoint"], rows[0]["model"]), ("hermes", "m1"))

    detail.observe(
        stats.readings_for(sample_set(("m1", 0, 260, 0, 0), ("m2", 0, 90, 0, 0))),
        now=at("2026-09-27 12:30"), watts=300)
    check("an unchanged counter does not leave an empty row behind",
          len(detail.payload()["combos"]), 4)

    print("tier bookkeeping")
    check("tier_since is empty until something is booked",
          stats.TokenLedger("").payload()["tier_since"], "")
    stamped = stats.TokenLedger("")
    stamped.observe(stats.readings_for(sample_set(("m1", 0, 10, 0, 0))),
                    now=at("2026-09-27 09:15"), watts=450)
    check("tier_since records when attribution began",
          stamped.payload()["tier_since"], "2026-09-27 09:15")
    stamped.observe(stats.readings_for(sample_set(("m1", 0, 30, 0, 0))),
                    now=at("2026-09-28 08:00"), watts=300)
    check("...and is not moved by later samples",
          stamped.payload()["tier_since"], "2026-09-27 09:15")

    old = stats.TokenLedger("", keep_days=2)
    old.observe(stats.readings_for(sample_set(("m1", 0, 10, 0, 0))),
                now=at("2020-01-01 12:00"), watts=450)
    check("a combo row is dropped together with its day",
          old.payload()["combos"], [])
    check("...but a tier total is lifetime, so it stays",
          tier_of(old, 450)["predicted_tokens"], 10)

    capped = stats.TokenLedger("")
    original_cap = stats.COMBO_CAP
    stats.COMBO_CAP = 3
    try:
        for index, watts in enumerate((150, 300, 450, 600, 500)):
            capped.observe(stats.readings_for(
                sample_set(("m1", 0, 10 * (index + 1), 0, 0))), watts=watts)
        check("the stored combo set is capped", len(capped._combos), 3)
        check("...keeping the largest limits, which are the newest keys",
              sorted(key.split("|")[1] for key in capped._combos),
              ["450", "500", "600"])
    finally:
        stats.COMBO_CAP = original_cap

    print("clearing from the dashboard (reset)")
    cleared = stats.TokenLedger("")
    cleared.observe(stats.readings_for(sample_set(("m1", 1000, 5000, 70, 10))), watts=450)
    cleared.observe(stats.readings_for(sample_set(("m1", 1200, 5600, 90, 14))), watts=300)
    check("a ledger to be cleared actually holds numbers",
          totals_of(cleared, "predicted_tokens"), 5600)
    cleared.reset()
    after = cleared.payload()
    check("the clear zeroes the lifetime totals",
          totals_of(cleared, "predicted_tokens"), 0)
    check("...and every bucket with it",
          (after["days"], len(after["models"]), len(after["tiers"]),
           len(after["combos"])), (0, 0, 0, 0))
    # The whole point of keeping ``seen``: without the raw-counter baseline
    # the next sample would book the engine's process-lifetime counter as if
    # it were new work -- a clear that refills itself within two seconds.
    cleared.observe(stats.readings_for(sample_set(("m1", 1250, 5700, 95, 15))))
    check("the next sample books only the delta past the kept baseline",
          totals_of(cleared, "predicted_tokens"), 100)
    check("...and not the whole lifetime counter again",
          totals_of(cleared, "predicted_tokens") < 200, True)
    check("the reset drops the 'since' date", after["since"], None)
    check("...and the next sample re-stamps it",
          cleared.payload()["since"], time.strftime("%Y-%m-%d"))

    with tempfile.TemporaryDirectory() as tmp:
        clear_path = os.path.join(tmp, "cleared.json")
        keeper = stats.TokenLedger(clear_path)
        keeper.observe(stats.readings_for(sample_set(("m1", 1000, 5000, 70, 10))))
        check("the clear flushes to disk on the spot", keeper.reset(), True)
        reread = stats.TokenLedger(clear_path)
        check("the reloaded file shows empty buckets",
              (reread.payload()["days"], totals_of(reread, "predicted_tokens")),
              (0, 0))
        reread.observe(stats.readings_for(sample_set(("m1", 1100, 5300, 80, 12))))
        check("and its kept baseline carries across the reload",
              totals_of(reread, "predicted_tokens"), 300)

    print("persistence")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "stats.json")
        first = stats.TokenLedger(path)
        check("a fresh file reports persisted", first.payload()["persisted"], True)
        first.observe(stats.readings_for(sample_set(("m1", 100, 500, 7, 3))))
        check("flush on demand writes", first.flush(force=True), True)

        second = stats.TokenLedger(path)
        check("totals survive a restart",
              totals_of(second, "predicted_tokens"), 500)
        second.observe(stats.readings_for(sample_set(("m1", 120, 560, 9, 4))))
        check("the persisted counter is the baseline, not zero",
              totals_of(second, "predicted_tokens"), 560)
        check("the earlier stretch is not counted twice",
              totals_of(second, "prompt_tokens"), 120)

        third = stats.TokenLedger(path)
        check("per-model split survives",
              third.payload()["models"]["hermes/m1"]["cached_tokens"], 7)

        # The new dimensions have to survive a restart too, and they carry a
        # duration rather than only counters, so a lossy load would show up as
        # a rate that silently reads "—".
        tiered_path = os.path.join(tmp, "tiered.json")
        keeper = stats.TokenLedger(tiered_path)
        keeper.observe(stats.readings_for(timed_set("m1", 1000, 500, 10.0, 5.0)),
                       watts=450)
        keeper.flush(force=True)
        reloaded = stats.TokenLedger(tiered_path)
        check("tier buckets survive a restart",
              reloaded.payload()["tiers"][0]["watts"], 450)
        check("...with their engine seconds intact",
              reloaded.payload()["tiers"][0]["predicted_seconds"], 5)
        check("...so the rate still computes",
              reloaded.payload()["tiers"][0]["predicted_tps"], 100.0)
        check("combo rows survive a restart",
              len(reloaded.payload()["combos"]), 1)
        check("tier_since survives a restart",
              reloaded.payload()["tier_since"], keeper.payload()["tier_since"])

        # A file written by the previous version has no tier or combo buckets.
        # Reading it must not raise, and the counters it does hold have to keep
        # working as the baseline -- dropping them would re-count the whole
        # process lifetime on the first sample after the upgrade.
        legacy_path = os.path.join(tmp, "v1.json")
        with open(legacy_path, "w", encoding="utf-8") as fh:
            json.dump({
                "version": 1,
                "since": "2026-09-01",
                "resets": 2,
                "totals": dict((field, 0) for field in stats.FIELDS),
                "daily": {},
                "models": {},
                "seen": {"hermes/m1": {"predicted_tokens": 40}},
            }, fh)
        legacy = stats.TokenLedger(legacy_path)
        check("a v1 ledger still loads", legacy.payload()["persisted"], True)
        check("...with no tier rows yet", legacy.payload()["tiers"], [])
        check("...and no combo rows either", legacy.payload()["combos"], [])
        legacy.observe(stats.readings_for(sample_set(("m1", 0, 60, 0, 0))),
                       watts=450)
        check("the stored v1 counter still serves as the baseline",
              totals_of(legacy, "predicted_tokens"), 20)
        check("...and the new dimensions simply start counting from here",
              legacy.payload()["tiers"][0]["predicted_tokens"], 20)

        leftovers = [n for n in os.listdir(tmp) if n.endswith(".tmp")]
        check("no temp file is left behind", leftovers, [])

        broken = os.path.join(tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        recovered = stats.TokenLedger(broken)
        check("a corrupt file does not raise", recovered.payload()["available"], True)
        check("...and is reported as unpersisted",
              recovered.payload()["persisted"], False)
        recovered.observe(stats.readings_for(sample_set(("m1", 0, 20, 0, 0))))
        check("...but still counts in memory",
              totals_of(recovered, "predicted_tokens"), 20)

        nested = stats.TokenLedger(os.path.join(tmp, "deep", "stats.json"))
        nested.observe(stats.readings_for(sample_set(("m1", 0, 20, 0, 0))))
        check("a missing directory is created, not given up on",
              nested.flush(force=True), True)
        check("...and the file lands there",
              os.path.exists(os.path.join(tmp, "deep", "stats.json")), True)

        # A path that genuinely cannot be written (here: it is a directory)
        # must still leave a working, counting ledger behind.
        os.makedirs(os.path.join(tmp, "blocked"))
        blocked = stats.TokenLedger(os.path.join(tmp, "blocked"))
        blocked.observe(stats.readings_for(sample_set(("m1", 0, 20, 0, 0))))
        check("an unwritable path is not fatal",
              blocked.flush(force=True), False)
        check("...and it keeps counting in memory",
              totals_of(blocked, "predicted_tokens"), 20)

    memory_only = stats.TokenLedger("")
    check("no path means no persistence",
          memory_only.payload()["persisted"], False)
    check("...and flushing is a no-op", memory_only.flush(force=True), False)

    print("")
    if FAILED:
        print("%d check(s) failed: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
