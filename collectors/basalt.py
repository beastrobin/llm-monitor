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
* **Slot rows.**  Strata carries per-slot rows inside ``/metrics``; basalt
  keeps its ``/metrics live.slots`` as just ``{total, active, queue_depth}``
  and answers a llama-shaped ``/slots`` dump instead -- the same table its
  own dashboard's Slots panel draws (``serve/service.py``'s ``slot_table``).
  ``_slots`` scrapes that endpoint and maps the rows; a failed scrape leaves
  the panel's "why is the breakdown empty" note with the reason
  (``slots_error``), never with invented rows.
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

from .llama import _get_json, _ratio
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
        # Filled by ``_slots`` each poll: a failed ``/slots`` scrape names the
        # reason the panel would otherwise only call "no data this round".
        self._slots_error = None

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
        """One entry per batch slot, from basalt's native ``/slots`` dump.

        ``live.slots`` in ``/metrics`` is only ``{total, active,
        queue_depth}``; the row-per-slot table lives on a llama-shaped
        ``/slots`` (``serve/service.py``'s ``slot_table``), which is what
        basalt's own dashboard draws.  Two of its readings have no llama
        equivalent and need a rule, both learned off the live server:

        * ``reused`` is the slot's own prefix-cache hit for the request it
          runs -- a direct measurement like llama.cpp's cache/processed
          split (Strata has to attribute), arriving a few polls into the
          request (it is ``null`` the first frames, then a count).  A count
          larger than its own prompt is counter skew, not a >100% hit, so
          the ratio clamps; ``null`` stays ``None`` so the card shows "—".
        * ``live.prefill_tok_s_mean`` is engine-global and keeps reporting
          the last read after the read ended (observed: 414 t/s during
          pure decoding), so it is attributed only to a slot whose state
          is ``prefill`` -- and only when exactly one slot is reading, the
          same one-reader rule Strata's ``_slots`` documents.

        Idle rows carry the slot's retained prefix (``ctx_used``) and its
        last finished request's draft counts; the engine fills the live
        request fields with zeros there, and zeros would read as
        measurements -- they map to ``None`` so the card shows "—".
        ``tok_s`` belongs to ``decoding`` only: a prefilling slot has no
        generated-per-second yet.
        """
        self._slots_error = None
        try:
            rows = _get_json(self._url("/slots"), self.api_key)
        except Exception as exc:  # noqa: BLE001 - one bad scrape must not kill the poll
            self._slots_error = "%s: %s" % (type(exc).__name__, exc)
            return []
        rows = rows if isinstance(rows, list) else []
        prefill = (payload.get("live") or {}).get("prefill_tok_s_mean")
        if not isinstance(prefill, (int, float)) or prefill <= 0:
            prefill = None
        reading = [r for r in rows if isinstance(r, dict) and r.get("state") == "prefill"]
        prefill_for = prefill if (prefill is not None and len(reading) == 1) else None
        slots = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            busy = bool(row.get("is_processing"))
            state = row.get("state")
            context = row.get("n_ctx")
            used = row.get("ctx_used")
            prompt = row.get("prompt_tokens") if busy else None
            reused = row.get("reused") if busy else None
            kv_hit = (min(1.0, reused / prompt)
                      if (reused is not None and prompt) else None)
            slots.append({
                "id": row.get("id"),
                "n_ctx": context,
                "is_processing": busy,
                "n_prompt_tokens": prompt or None,
                "n_prompt_tokens_processed":
                    (prompt - reused) if (prompt and reused is not None) else None,
                "n_prompt_tokens_cache": reused,
                "n_decoded": row.get("generated") if busy else None,
                "ctx_used": used,
                "ctx_ratio": _ratio(used, context),
                "state": state,
                "tok_s": row.get("tok_s") if state == "decoding" else None,
                "elapsed_s": None,
                "prefill_tok_s": prefill_for if state == "prefill" else None,
                "kv_hit": kv_hit,
                "spec_types": None,
                # Readings basalt gives per slot that llama.cpp does not:
                # the slot's own TTFT, draft window length, and the draft
                # counters of its last finished request.  No card draws
                # them yet; they ride along rather than being dropped.
                "ttft_s": row.get("ttft_s"),
                "draft_len": row.get("draft_len"),
                "drafts_offered": row.get("drafts_offered"),
                "drafts_accepted": row.get("drafts_accepted"),
            })
        return slots

    def _model_detail(self, model_id, params, now, preset_args=None):
        detail = super()._model_detail(model_id, params, now,
                                       preset_args=preset_args)
        # ``_reported_slot_count`` was written for Strata's row-per-slot
        # ``live.slots``; on basalt that key is a ``{total, active,
        # queue_depth}`` dict and its ``len`` is a key count that matches
        # the slot number only by accident.  Prefer the scraped ``/slots``
        # rows, then the engine block's ``batch_slots``.
        rows = detail.get("slots") or []
        batch = self._engine_block.get("batch_slots")
        if rows:
            detail["total_slots"] = len(rows)
        elif batch:
            detail["total_slots"] = batch
        if "slots" in detail and getattr(self, "_slots_error", None):
            detail["slots_error"] = self._slots_error
        return detail
