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

    def _slots(self, payload):
        """One entry per batch slot, in the shape the context card reads.

        ``held_tokens`` on an idle slot is ``len(prompt) + len(everything the
        slot fed)`` -- the tokens the slot keeps so the next turn of that
        conversation lands on it -- so it is measured the same way the busy
        case measures ``prompt_tokens + generated``.  Both are therefore "what
        this slot is holding", which is exactly what llama.cpp's slot view
        reports and what this panel's context card has always meant.
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
        slots = []
        for row in rows:
            busy = row.get("state") != "idle"
            if busy:
                used = (row.get("prompt_tokens") or 0) + (row.get("generated") or 0)
            else:
                used = row.get("held_tokens")
            slots.append({
                "id": row.get("slot"),
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
                "spec_types": None,
            })
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
        if isinstance(live.get("tok_s"), (int, float)):
            metrics["decode_tps_inst"] = float(live["tok_s"])
        # `prefill_tok_s_mean` is reported only while the engine is busy, and it
        # holds the current request's prefill rate from the moment it reads the
        # prompt until the request ends.  Not narrowed to the "reading" phase:
        # with parallel slots the newest request decides that phase, so a second
        # request still reading its prompt would leave this series empty for the
        # whole of the first one's decode.  A flat line through the decode phase
        # is what this card already gets from llama.cpp, whose prompt counter
        # likewise stops while the engine generates.
        if isinstance(live.get("prefill_tok_s_mean"), (int, float)):
            metrics["prefill_tps_inst"] = float(live["prefill_tok_s_mean"])
        detail["metrics"] = metrics
        # The draft counters are the ground truth for "is MTP running": Strata
        # reports no --spec-type launch argument to read it off, and a nonzero
        # offer count cannot be faked by an idle engine.
        counters = (detail["metrics"] or {}).get("counters") or {}
        detail["mtp_enabled"] = bool(counters.get("draft_tokens"))
        return detail
