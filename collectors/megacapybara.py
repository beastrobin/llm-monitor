"""MegaCapybara telemetry collector.

MegaCapybara (``E:\\AI\\MegaCapybara``) is the resident engine that replaced
the basalt build on port 8085.  It is an OpenAI-compatible server that speaks
none of the dialects this dashboard already parses: no ``/props``, no
``/slots``, no Prometheus ``/metrics``.  The telemetry lives where the
engine's own launcher reads it -- a single JSON document on ``/stats`` --
and the model list is a vLLM-shaped ``/v1/models``.

What the collector translates, and why each translation needs code:

* **Reachability.**  Both of the parent probes 404 here (llama.cpp's
  ``/props``, Strata's ``/metrics``), so :meth:`probe` reads ``/stats``
  instead: the engine answers it while fully idle as well as while
  generating, and it is the document the launcher polls for its
  Statistics tab, so a panel that cannot reach it cannot be "up".
* **Model list.**  The ``/v1/models`` rows carry no ``status`` block, and
  the parent mapping reads the absence as "unloaded" -- which would empty
  the card of the one model the process actually serves.  One
  MegaCapybara process loads exactly one model, so every listed model is
  stamped ``loaded: True``.
* **No counter dialect.**  ``/stats`` carries the lifetime token totals
  (``tokens_prefilled`` / ``tokens_generated``) -- enough for the ledger to
  book -- but no seconds, no cached split and no draft counters, so those
  stay ``None`` and the derived averages read "—" rather than guessing.
  The instantaneous rates come from the engine's own ``decode_tps`` /
  ``prefill_tps`` instead of counter deltas: there are no counters to
  difference on.
* **Slots are the working jobs, and only those.**  ``jobs[]`` mixes the
  working conversations with the *parked conversation cache* (held in the
  RAM tier, resumable).  A machine with four concurrency slots can carry
  fifty parked conversations, and drawing one row per cache entry dressed
  the cache up as slots and swamped the four rows the section is for -- a
  category error.  The launcher's own Jobs panel is where the cache is
  visible; this section maps the working jobs only.
* **MTP from the speculator name.**  There is no speculative counter
  block; ``speculator`` names the drafter (``DFlash2``) and the per-round
  figures (``accepted_per_round`` / ``rows_per_round``) are the engine's
  own acceptance readings.  With the speculator off, neither number is
  shown rather than reporting a 0% hit.
"""

import time

from .llama import LlamaEndpoint, _get_json, _ratio

# The two job states that do work; every other state (parked, swapped,
# waiting...) keeps its context but carries no live rate.
_BUSY_STATES = ("reading", "generating")


class MegaCapybaraEndpoint(LlamaEndpoint):
    """One MegaCapybara server, scraped where ``config.json`` points."""

    def __init__(self, spec):
        super().__init__(spec)
        # The ``/stats`` document this poll round fetched; ``_model_detail``
        # builds the whole card from it, so no second round trip.
        self._stats = None

    def set_base_url(self, base_url, api_key=None):
        changed = super().set_base_url(base_url, api_key)
        if changed:
            # The document describes the instance that answered the old
            # address, so it leaves with everything else cached here.
            self._stats = None
        return changed

    def probe(self):
        """Reachability and telemetry in one document.

        ``/stats`` is what the engine's own launcher polls for the
        Statistics tab: it answers while idle and while generating, and it
        is the only document the engine publishes with the live numbers, so
        the up/down decision and the card's content share it.
        """
        stats = _get_json(self._url("/stats"), self.api_key)
        if not isinstance(stats, dict):
            raise ValueError("/stats answered no object")
        self._stats = stats
        # The engine publishes no version banner anywhere (no ``/props``,
        # no Server header, nothing in ``/stats``); the brand is all that
        # can be said honestly.
        return {"mode": "single", "props": {"build_info": "MegaCapybara"}}

    def list_models(self):
        """The model list: here every listed model is the loaded one.

        The vLLM-shaped rows carry no ``status`` block, and the parent
        mapping reads the absence as "unloaded" -- which would empty the
        card of the one model the process actually serves.  One
        MegaCapybara process loads exactly one model, so the list *is* the
        load state, with the per-model context the front end is told to set.
        """
        now = time.time()
        fetched, cached = self._models_cache
        if now - fetched < self.PROPS_TTL:
            return cached
        payload = _get_json(self._url("/v1/models"), self.api_key)
        models = []
        for item in payload.get("data", []):
            models.append({
                "id": item.get("id"),
                "loaded": True,
                "args": [],
                "context_length": item.get("context_length"),
            })
        self._models_cache = (now, models)
        return models

    def _model_detail(self, model_id, params, now, preset_args=None):
        """The card from this round's ``/stats`` document.

        Nothing here goes back to the engine: ``probe`` already fetched the
        document, and the endpoints the parent would scrape (``/props``,
        ``/slots``, ``/metrics``) do not exist on this engine, so re-fetching
        them would only burn round trips collecting 404s.
        """
        stats = self._stats if isinstance(self._stats, dict) else {}
        detail = {"id": model_id, "loaded": True, "error": None}
        spec = (stats.get("speculator") or "").strip()
        detail["preset_args"] = []
        detail["mtp_type"] = spec.lower() or None
        detail["mtp_enabled"] = bool(spec) and spec.lower() != "none"
        # The per-conversation ceiling, not the KV pool total (``max_context``):
        # it is the number ``/v1/models`` advertises and the one the front end
        # is told to set, so the context card's "上限" reads as configured.
        detail["n_ctx"] = stats.get("conversation_context")
        detail["total_slots"] = stats.get("max_concurrency")
        # The weights file, kept separate from the id: the id is the front-end
        # handle (``qwen3.8-27b``) and this names the file on disk.
        detail["model_path"] = stats.get("model")
        detail["spec_types"] = detail["mtp_type"]

        jobs = [j for j in (stats.get("jobs") or []) if isinstance(j, dict)]
        detail["slots"] = self._jobs_to_slots(stats, jobs)
        detail["live_state"] = self._live_state(stats, jobs)
        # The engine keeps no finished-request log, so the idle note
        # ("上次 N tok") has no source and stays off.
        detail["last_request"] = None

        rows = stats.get("rows_per_round")
        accepted = stats.get("accepted_per_round")
        mtp_ratio = None
        if detail["mtp_enabled"] and rows and accepted is not None:
            mtp_ratio = _ratio(accepted, rows)
        detail["metrics"] = {
            "decode_tps_avg": None,
            "prefill_tps_avg": None,
            "decode_tps_inst": stats.get("decode_tps"),
            "prefill_tps_inst": stats.get("prefill_tps"),
            "kv_hit_ratio": stats.get("cache_hit_rate"),
            "mtp_accept_ratio": mtp_ratio,
            "mtp_tokens_per_step": accepted if detail["mtp_enabled"] else None,
            "mtp_active": detail["mtp_enabled"],
            "mtp_positions": {},
            "requests_processing": stats.get("active"),
            "requests_deferred": stats.get("queued"),
            "busy_slots_per_decode": None,
            "n_decode_total": None,
            "n_tokens_max": None,
            "window_seconds": None,
            # Lifetime totals the engine publishes; the ledger books the
            # deltas.  Seconds and the cached/draft splits have no source on
            # this engine and stay None rather than guessed.
            "counters": {
                "prompt_tokens": stats.get("tokens_prefilled"),
                "prompt_cached": None,
                "prompt_seconds": None,
                "predicted_tokens": stats.get("tokens_generated"),
                "predicted_seconds": None,
                "draft_tokens": None,
                "accepted_tokens": None,
                "drafts": None,
            },
        }
        return detail

    @staticmethod
    def _jobs_to_slots(stats, jobs):
        """One slot row per *working* conversation -- the concurrency slots.

        ``jobs[]`` is the engine's per-conversation table, and its parked
        rows are the conversation *cache* (held in the RAM tier, resumable),
        not slots: a server with four concurrency slots carries every parked
        conversation in there, and mapping them all to slot rows would show
        fifty "slots" on a four-slot machine.  The cache stays where the
        engine shows it (the launcher's Jobs panel); an idle engine simply
        has no slot rows, and the context card then reads the "空闲残留"
        footnote exactly as it does for the other engines.
        """
        n_ctx = stats.get("conversation_context")
        slots = []
        for job in jobs:
            state = job.get("state") or "idle"
            if state not in _BUSY_STATES:
                continue
            context = job.get("context")
            generated = job.get("generated")
            slots.append({
                "id": job.get("id"),
                "n_ctx": n_ctx,
                "is_processing": True,
                # A working row splits its context into the prompt it is
                # reading/generated against and what it has generated.
                "n_prompt_tokens": context - generated
                    if context is not None else None,
                "n_prompt_tokens_processed": None,
                "n_prompt_tokens_cache": None,
                "n_decoded": generated,
                "ctx_used": context,
                "ctx_ratio": _ratio(context, n_ctx),
                "state": state,
                "tok_s": job.get("tok_per_s"),
                "elapsed_s": None,
                "prefill_tok_s": None,
                # Only the global hit rate is reported; a per-row figure
                # would be invented.
                "kv_hit": None,
                "spec_types": None,
            })
        return slots

    @staticmethod
    def _live_state(stats, jobs):
        """The hero lamps, in the engine's own vocabulary.

        The lamps (idle / reading / generating / queued) take one state
        word; the note beside them takes the figures of the job the engine
        is on.  A generating job outranks a reading one, the way the
        Strata live block does.
        """
        state = {"queued": stats.get("queued") or 0}
        for job in jobs:
            if job.get("state") == "generating":
                state.update({
                    "state": "generating",
                    "generated": job.get("generated"),
                    "tok_s": job.get("tok_per_s"),
                })
                return state
        for job in jobs:
            if job.get("state") == "reading":
                state.update({
                    "state": "reading",
                    "prompt_tokens": job.get("context"),
                })
                return state
        state["state"] = "idle"
        return state
