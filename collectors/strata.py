"""Strata telemetry collector.

Strata (its own tree, served by ``serve/server.py``) is a llama.cpp-derived
server.  ``/props``, ``/slots``, ``/v1/models`` and ``/health`` answer in
llama.cpp's shapes, so a client that only sends chat completions cannot tell the
two apart.  ``/metrics`` is where they part: llama.cpp exports Prometheus text,
Strata returns one JSON document built for its own Monitor tab.

That difference fails silently, which is why this module exists.  The llama
collector pipes ``/metrics`` into :func:`..llama.parse_prometheus`; fed JSON,
every line fails to parse, the series comes back empty, and each counter lands
as ``None``.  The endpoint still looks perfectly healthy -- up, single model,
right name, right context window -- while every number on the model card is
blank, and nothing on screen connects the two.  Recognising the dialect is the
whole fix.

The JSON turns out to be a superset of the text it replaces, so the work here
is translation rather than measurement: read the engine's own totals, present
them under the counter names :class:`..llama.ModelSampler` already knows, and
let it derive the rates exactly as it does for llama.cpp.  One implementation of
"counter -> instantaneous rate", two wire formats.

The mapping below comes from the definitions in ``serve/server.py``, not from
guessing at the field names:

* ``totals.output_tokens`` with ``totals.decode_ms`` -- the generated tokens and
  the time the engine spent decoding them, the same pair llama.cpp calls
  ``tokens_predicted_total`` / ``tokens_predicted_seconds_total``.
* ``totals.prompt_tokens`` counts the whole prompt and ``totals.reused`` the
  part the cache served, so the tokens that actually crossed the forward pass
  are the **difference**; ``totals.prompt_ms`` times precisely those
  (``/v1/status`` computes ``prompt_n = prompt_tokens - cache_n`` beside the
  same ``prompt_ms``).  That is the split this panel already documents: cached
  and read tokens are mutually exclusive parts, and prefill speed counts only
  the read ones.
* ``totals.drafts_offered`` / ``totals.drafts_accepted`` -- the MTP counters.

Two numbers are deliberately left ``None`` rather than derived: the count of
verification steps (so "MTP per step" reads `--`; Strata reports what was
offered and accepted, never how many steps offered them) and
``n_decode_total``.

``totals`` is monotonic for the life of the service and returns to zero only
when the server itself restarts -- not when a model is reloaded inside one
lifetime.  The ledger already reads a backwards counter as a restart, so the
shape matches; a live model swap would be booked against the model that was
loaded first, which is the same limitation llama.cpp's counters carry and is
noted here so it is not later mistaken for a bug in the fold.

It is also **stepped**: a request's tokens and durations are added to the totals
when that request finishes, not while it runs (``serve/server.py`` sums them
beside the history entry it writes).  A difference between two scrapes is
therefore zero for most scrapes of a busy engine, so the two *instantaneous*
rates are taken from the engine's own reported rates instead -- see
:meth:`StrataEndpoint._model_detail`.  The cumulative averages, which are what
the ledger and the card's headline fall back to, come from the totals as they
do everywhere else.
"""

from .llama import LlamaEndpoint, _get_json, _ratio


class StrataEndpoint(LlamaEndpoint):
    """One Strata server, scraped through its JSON ``/metrics``.

    Only ``_model_detail`` differs from the llama.cpp collector: the probe, the
    model list, the TTL caches, the failure backoff and ``set_base_url`` all
    describe what an endpoint *is* rather than what it speaks, and they are
    inherited so the two dialects cannot drift apart.
    """

    def _series(self, payload):
        """The llama.cpp-named series to hand ``ModelSampler``.

        Names rather than values: the sampler's job is the delta between two
        scrapes and the restart rule when a counter goes backwards, and it
        should not learn a second vocabulary to keep doing it.
        """
        totals = payload.get("totals") or {}
        live = payload.get("live") or {}

        def number(key):
            value = totals.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        whole, reused = number("prompt_tokens"), number("reused")
        read = None if whole is None or reused is None else max(0.0, whole - reused)
        prompt_ms, decode_ms = number("prompt_ms"), number("decode_ms")
        return {
            "llamacpp:prompt_tokens_total": read,
            "llamacpp:prompt_tokens_cached_total": reused,
            "llamacpp:prompt_seconds_total":
                prompt_ms / 1000.0 if prompt_ms is not None else None,
            "llamacpp:tokens_predicted_total": number("output_tokens"),
            "llamacpp:tokens_predicted_seconds_total":
                decode_ms / 1000.0 if decode_ms is not None else None,
            "llamacpp:spec_decode_num_draft_tokens_total": number("drafts_offered"),
            "llamacpp:spec_decode_num_accepted_tokens_total": number("drafts_accepted"),
            # Gauges, not counters: they ride through the sampler unchanged and
            # are what the card's in-flight request row reads.
            "llamacpp:requests_processing": live.get("running"),
            "llamacpp:requests_deferred": live.get("waiting"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Two memories across scrapes, both needed because ``slots_view()`` is a
        # snapshot of one instant and says nothing about what came before it:
        #
        # ``_slot_held``  what each slot held the last time it was idle.  When
        #            that same slot turns busy, the engine's ``pick_slot`` has
        #            just reused exactly that prefix (serve/server.py matches
        #            the prompt against ``slot_held`` and takes it), so
        #            held ÷ prompt is that slot's own prefix hit rate -- the
        #            per-slot reading of the same idea the headline KV card
        #            measures globally off ``totals.reused``.
        # ``_slot_req``  the in-flight request per slot (its prompt length and
        #            the hit computed for it), so the number survives the
        #            2 s frames rather than flickering as the slot's counters
        #            move underneath it.
        self._slot_held = {}
        self._slot_req = {}

    def _slots(self, payload):
        """One entry per batch slot, in the shape the context card reads.

        ``held_tokens`` on an idle slot is ``len(prompt) + len(everything the
        slot fed)`` -- the tokens the slot keeps so the next turn of that
        conversation lands on it -- so it is measured the same way the busy
        case measures ``prompt_tokens + generated``.  Both are therefore "what
        this slot is holding", which is exactly what llama.cpp's slot view
        reports and what this panel's context card has always meant.

        Two readings the engine does NOT report per slot are attributed here
        rather than left blank, and both attributions rest on how the server
        actually works:

        * ``prefill_tok_s`` -- ``live.prefill_tok_s_mean`` is a single global
          number, but the engine admits **one prompt read at a time**: the
          control lines are a semaphore (``_take_control``), so while exactly
          one slot is in ``reading`` that rate belongs to that slot.  With two
          reading at once -- which the semaphore forbids, but a future engine
          may not keep -- the attribution is dropped rather than guessed.
        * ``kv_hit`` -- see ``_slot_held`` above.

        Both stay ``None`` when the engine gives nothing to attribute, and the
        card then reads "—" instead of a borrowed global average.
        """
        engine = payload.get("engine") or {}
        live = payload.get("live") or {}
        context = engine.get("context") or engine.get("max_context")
        rows = live.get("slots") or []
        if not rows and live.get("prompt_tokens") is not None:
            # No batch slots: the one request in flight is reported on `live`
            # itself rather than per slot.
            rows = [{"slot": 0, "state": live.get("state"),
                     "prompt_tokens": live.get("prompt_tokens"),
                     "generated": live.get("generated")}]
        prefill = live.get("prefill_tok_s_mean")
        if not isinstance(prefill, (int, float)):
            prefill = None
        reading = [row for row in rows if row.get("state") == "reading"]
        prefill_for = prefill if (prefill is not None and len(reading) == 1) else None
        slots = []
        seen = set()
        for row in rows:
            sid = row.get("slot")
            busy = row.get("state") != "idle"
            if sid is not None:
                seen.add(sid)
            # The slot's own prefix hit, carried across the frames of one
            # request: computed once when the request appears, then reused
            # while its prompt length is unchanged.
            kv_hit = None
            if sid is not None:
                if busy:
                    prompt = row.get("prompt_tokens")
                    previous = self._slot_req.get(sid)
                    if previous is not None and previous[0] == prompt:
                        kv_hit = previous[1]
                    else:
                        held = self._slot_held.get(sid)
                        kv_hit = _ratio(held, prompt) if held else None
                    self._slot_req[sid] = (prompt, kv_hit)
                else:
                    self._slot_held[sid] = row.get("held_tokens")
                    self._slot_req.pop(sid, None)
            if busy:
                used = (row.get("prompt_tokens") or 0) + (row.get("generated") or 0)
            else:
                used = row.get("held_tokens")
            slots.append({
                "id": sid,
                "n_ctx": context,
                "is_processing": busy,
                "n_prompt_tokens": row.get("prompt_tokens"),
                # `held_tokens` is a length, not the token ids, so the prompt
                # count is unknown while idle and the used total is not: report
                # one and not the other rather than inventing a split.
                "n_prompt_tokens_processed": None,
                "n_prompt_tokens_cache": None,
                "n_decoded": row.get("generated"),
                "ctx_used": used,
                "ctx_ratio": _ratio(used, context),
                # Per-slot rate and age, reported by the engine only while the
                # slot is busy (`slots_view()`): `tok_s` is generated divided by
                # the seconds since that request's first token -- the community
                # tg formula, but per slot instead of summed across slots, so a
                # parallel run stays comparable to a single-request benchmark.
                # Absent while idle, and the card shows "—" rather than 0.
                "state": row.get("state"),
                "tok_s": row.get("tok_s"),
                "elapsed_s": row.get("elapsed_s"),
                # Attributed, not measured per slot (see the docstring): the
                # engine's one-at-a-time prompt read makes the global prefill
                # rate belong to the slot that is reading, and the prefix the
                # slot held before this request is exactly what the engine
                # reused for it.
                "prefill_tok_s": prefill_for if (busy and row.get("state") == "reading") else None,
                "kv_hit": kv_hit,
                "spec_types": None,
            })
        # A slot the engine stopped reporting (a smaller --batch after a
        # restart) must not keep a prefix that no longer exists: its id could
        # come back attached to a different conversation.
        for stale in set(self._slot_held) - seen:
            self._slot_held.pop(stale, None)
            self._slot_req.pop(stale, None)
        return slots

    def _model_detail(self, model_id, params, now, preset_args=None):
        detail = {"id": model_id, "loaded": True, "error": None, "preset_args": [],
                  "mtp_type": "", "mtp_enabled": False}
        try:
            payload = _get_json(self._url("/metrics"), self.api_key)
        except Exception as exc:  # noqa: BLE001 - one bad scrape must not kill the poll
            detail["error"] = "%s: %s" % (type(exc).__name__, exc)
            return detail

        engine = payload.get("engine") or {}
        live = payload.get("live") or {}
        detail["n_ctx"] = engine.get("context") or engine.get("max_context")
        detail["total_slots"] = len(live.get("slots") or []) or None
        # The JSON metrics name neither the file nor its quantisation, but
        # /props does name the file -- read it through the inherited TTL cache
        # so the card's weight row says which GGUF is actually loaded, instead
        # of leaving the reader to trust a label hand-typed into config.json.
        # The quantisation stays empty rather than parsed out of the file
        # name: that would be guessing at a naming convention, not reading a
        # field.  A failed /props costs the label only, never the numbers.
        try:
            props = self._cached_props(params, now)
            if props is None:
                props = _get_json(self._url("/props", params), self.api_key)
                self._props_cache[self._props_key(params)] = (now, props)
        except Exception:  # noqa: BLE001 - the path is a label, not a measurement
            props = {}
        detail["model_path"] = props.get("model_path")
        detail["model_ftype"] = props.get("model_ftype")
        detail["modalities"] = {"vision": True} if engine.get("images") else None
        detail["spec_types"] = None
        # Strata's own Monitor tab leads with the engine's self-reported state:
        # generating or idle, thinking or answering, how far the prompt read or
        # the generation has come.  The counters can say what happened; only
        # the engine says what it is doing right now, and this panel has no
        # equivalent row -- so the fields ride through under their own key
        # rather than being folded into the counters.
        state = {}
        for key in ("state", "phase", "parallel", "queued", "waiting",
                    "elapsed_s", "max_tokens", "prompt_tokens", "prompt_read",
                    "prompt_total", "generated", "tok_s"):
            value = live.get(key)
            if value is not None:
                state[key] = value
        detail["live_state"] = state or None
        # The idle half of that panel reads "last: 839 tokens at 218.7 tok/s"
        # -- the live block says nothing once the last request is done, so the
        # engine's own request log (newest first) is where that comes from.
        recent = payload.get("requests")
        first = recent[0] if isinstance(recent, list) and recent and isinstance(recent[0], dict) else None
        detail["last_request"] = {
            "output_tokens": first.get("output_tokens"),
            "decode_tok_s": first.get("decode_tok_s"),
        } if first else None
        detail["slots"] = self._slots(payload)
        metrics = self._sampler(model_id).feed(self._series(payload), now)
        # The instantaneous series needs the engine's own rate, not a counter
        # delta.  Strata adds its totals up when a request *finishes*, beside
        # the history entry it writes (``serve/server.py``), so a delta between
        # two scrapes is zero for almost every scrape a running engine produces
        # -- the headline would sit on the cumulative average and the trend line
        # would stay empty at full tilt, which is a visible hole rather than a
        # missing nicety.  The engine does report a rate over the same kind of
        # window this card plots, so that is what the instantaneous value is
        # built from; the cumulative average stays counter-derived, exactly as
        # it is for llama.cpp, and the two agree whenever the engine is busy.
        # And when the engine reports no rate either -- the scrape where the
        # last request finishes and the engine turns idle -- the value is
        # **None**, never the sampler's delta: that one scrape carries the whole
        # request's token count, and dividing it by the window is the 10,000
        # t/s spike that used to squash every real reading into a flat line.
        tok_s = live.get("tok_s")
        prefill = live.get("prefill_tok_s_mean")
        metrics["decode_tps_inst"] = float(tok_s) if isinstance(tok_s, (int, float)) else None
        # `prefill_tok_s_mean` is reported only while the engine is busy, and it
        # holds the current request's prefill rate from the moment it reads the
        # prompt until the request ends.  Not narrowed to the "reading" phase:
        # with parallel slots the newest request decides that phase, so a second
        # request still reading its prompt would leave this series empty for the
        # whole of the first one's decode.  A flat line through the decode phase
        # is what this card already gets from llama.cpp, whose prompt counter
        # likewise stops while the engine generates.
        metrics["prefill_tps_inst"] = float(prefill) if isinstance(prefill, (int, float)) else None
        detail["metrics"] = metrics
        # The draft counters are the ground truth for "is MTP running": Strata
        # reports no --spec-type launch argument to read it off, and a nonzero
        # offer count cannot be faked by an idle engine.
        counters = (detail["metrics"] or {}).get("counters") or {}
        detail["mtp_enabled"] = bool(counters.get("draft_tokens"))
        return detail
