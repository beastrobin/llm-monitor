"""Basalt telemetry collector.

Basalt (``E:\\AI\\basalt``, a local build of jesdga95/basalt) is the resident
engine that replaced the Strata App stack.  It speaks the same JSON
``/metrics`` Monitor dialect as Strata and llama-shaped ``/props`` /
``/health`` (its ``/health`` answers ``"service": "basalt"``), so this module
is a thin specialisation of :class:`.strata.StrataEndpoint` rather than a new
dialect parser.

What actually differs, and why each difference needs code:

* **No ``totals``.**  Strata carries lifetime counters; basalt carries only
  a ring buffer of finished requests (``requests[]``) plus a short ``history``.
  The ledger and the cumulative card numbers are counter-derived, so this
  module rebuilds lifetime counters from the ring: each request id is booked
  once, the first time the poll sees it.  The ring is newest-first and holds
  a dozen finished requests while polls are 2 s apart, so a request cannot
  complete and be evicted between two scrapes on this engine; a basalt
  restart simply starts a new id sequence that adds nothing to the old one
  (ids repeat, so the id is keyed with its ``time``).
* **Slot rows.**  Strata reports one row per batch slot with live state;
  basalt's ``live.slots`` is just ``{total, active, queue_depth}``.  There is
  nothing to draw a per-slot card from, so ``_slots`` reports none and the
  panel shows no slot breakdown rather than invented rows.  The engine does
  say how many slots it runs (``engine.batch_slots``), so ``total_slots`` is
  still filled and the "why is the breakdown empty" note has its number.
* **TTFT name.**  Basalt's request rows call it ``ttft_s`` where Strata calls
  it ``first_engine_token_s``; :mod:`.strata` reads both names now, so the
  TTFT card works on both engines.
* **No desktop app to follow.**  Basalt is a hand-started server on a fixed
  port (``basalt-start.cmd``, port 8085) with no instance records and no key
  (``/health`` says ``"api_key": false``), so the Strata address-following
  machinery is switched off: the card polls exactly where ``config.json``
  says.
"""

import time

from .strata import StrataEndpoint


def _num(value):
    return float(value) if isinstance(value, (int, float)) else None


class BasaltEndpoint(StrataEndpoint):
    """One basalt server, scraped where ``config.json`` points."""

    def __init__(self, spec, *args, **kwargs):
        spec = dict(spec)
        # A hand-started server on a fixed port: nothing to follow, no key to
        # unlock.  Opting out of ``discover`` skips the Strata app/instance
        # probing entirely; source 1 of ``follow_now`` (this address answers
        # /health with a loaded model) still identifies the card's reason.
        spec["discover"] = False
        super().__init__(spec, *args, **kwargs)
        # Rebuilt lifetime counters (see module docstring) and the set of
        # request ids already booked into them.
        self._acc = {k: 0.0 for k in (
            "prompt_tokens", "reused", "prompt_ms", "output_tokens",
            "decode_ms", "drafts_offered", "drafts_accepted")}
        self._booked = set()
        self._engine_block = {}

    def _series(self, payload):
        """Lifetime counters rebuilt from the finished-request ring.

        Each row is booked exactly once (keyed ``id@time``), so repeated
        scrapes of the same ring add nothing; a request's numbers appear only
        when it finishes, which is the same "stepped" shape Strata's totals
        have and why both engines take their *instantaneous* rates from
        ``live`` rather than from these deltas.
        """
        for row in payload.get("requests") or []:
            if not isinstance(row, dict):
                continue
            key = "%s@%s" % (row.get("id"), row.get("time"))
            if key in self._booked:
                continue
            self._booked.add(key)
            out = _num(row.get("output_tokens")) or 0.0
            dts = _num(row.get("decode_tok_s"))
            self._acc["prompt_tokens"] += _num(row.get("prompt_tokens")) or 0.0
            self._acc["reused"] += _num(row.get("reused")) or 0.0
            self._acc["prompt_ms"] += _num(row.get("prompt_ms")) or 0.0
            self._acc["output_tokens"] += out
            # decode seconds = tokens / the engine's own reported rate for
            # that request -- the only decode-time figure basalt publishes.
            self._acc["decode_ms"] += (out / dts * 1000.0) if dts else 0.0
            self._acc["drafts_offered"] += _num(row.get("drafts_offered")) or 0.0
            self._acc["drafts_accepted"] += _num(row.get("drafts_accepted")) or 0.0
        self._engine_block = payload.get("engine") or {}

        acc = self._acc
        live = payload.get("live") or {}
        slots = live.get("slots")
        active = slots.get("active") if isinstance(slots, dict) else None
        read = max(0.0, acc["prompt_tokens"] - acc["reused"])
        return {
            "llamacpp:prompt_tokens_total": read,
            "llamacpp:prompt_tokens_cached_total": acc["reused"],
            "llamacpp:prompt_seconds_total": acc["prompt_ms"] / 1000.0,
            "llamacpp:tokens_predicted_total": acc["output_tokens"],
            "llamacpp:tokens_predicted_seconds_total": acc["decode_ms"] / 1000.0,
            "llamacpp:spec_decode_num_draft_tokens_total": acc["drafts_offered"],
            "llamacpp:spec_decode_num_accepted_tokens_total": acc["drafts_accepted"],
            # Gauges: basalt says how many slots are busy, not how many
            # requests run -- a solo request books no slot (same quirk Strata
            # documents), so fall back to the live state word.
            "llamacpp:requests_processing": (
                active if active else (1 if live.get("state") not in (None, "idle") else 0)),
            "llamacpp:requests_deferred": (
                slots.get("queue_depth") if isinstance(slots, dict) else live.get("queued")),
        }

    def _slots(self, payload):
        """No per-slot rows exist on basalt -- report none, invent none."""
        return []

    def _model_detail(self, model_id, params, now, preset_args=None):
        detail = super()._model_detail(model_id, params, now,
                                       preset_args=preset_args)
        # The inherited card wants a slot count; basalt names it in the
        # engine block instead of in a row-per-slot list.
        batch = self._engine_block.get("batch_slots")
        if batch and not detail.get("total_slots"):
            detail["total_slots"] = batch
        return detail
