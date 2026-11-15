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
  stamped ``loaded: True``.  The id is taken from ``gateway.realModel`` when
  the launcher's gateway is in front (see :meth:`list_models`).
* **No counter dialect.**  ``/stats`` carries the lifetime token totals
  (``tokens_prefilled`` / ``tokens_generated``) -- enough for the ledger to
  book -- but no seconds, no cached split and no draft counters, so those
  stay ``None`` and the derived averages read "—" rather than guessing.
  The instantaneous rates come from the engine's own ``decode_tps`` /
  ``prefill_tps`` instead of counter deltas: there are no counters to
  difference on.  The ``decode_tps`` gauge reads 0.0 even while decoding,
  so the working jobs' own ``tok_per_s`` is the fallback.
* **Job states are classified by their numbers, not their words.**  The
  engine's vocabulary (``prefill``, ``tool call``, ``parked``) is not the
  one this dashboard uses (``reading`` / ``generating``); see
  :func:`_job_phase`.
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

# The job states that hold a concurrency slot.  A parked or swapped row is
# the engine's resumable conversation cache (RAM / disk tier), not a slot: a
# four-slot server carries dozens of them.
_CACHE_STATES = ("parked", "swapped", "cached", "evicted", "preempted")
# Bookkeeping states that are not work in progress either.
_IDLE_STATES = ("", "idle", "queued", "waiting", "done", "finished",
                "complete", "completed", "error", "failed", "cancelled",
                "canceled", "aborted")
# Names another engine (or a future build of this one) may use for the
# prompt-reading phase.  Only consulted when the job's own numbers are silent.
_READING_STATES = ("reading", "prefill", "prefill+decode", "prompt")


def _job_phase(job):
    """``"reading"``, ``"generating"`` or ``None`` -- is this job a slot?

    The engine names its phases in a vocabulary of its own, and the names are
    not the ones this dashboard uses: a working conversation is observed as
    ``prefill`` and then ``tool call``, never ``reading`` / ``generating``.
    Matching on those two words (the v1.16 mapping, written against the docs
    of the engine it replaced) dropped **every** MegaCapybara job on the
    floor, so a card sat on 空闲 / 0 t/s / no slot rows while the engine was
    decoding at 295 t/s -- the "有时候是空的" report of 2026-11-15.

    So the class comes from the job's own numbers, with the state word only
    as a tie-break: the cache states are excluded by name, and everything
    else is "reading" until the prompt is in (``prefill_frac < 1`` and
    nothing generated) and "generating" once tokens or a rate move.  A
    renamed or newly added state therefore keeps its row instead of
    disappearing.
    """
    state = str(job.get("state") or "").strip().lower()
    if state in _CACHE_STATES or state in _IDLE_STATES:
        return None
    # Deliberately NOT filtered on ``slot < 0``: the parked rows do carry -1,
    # but a working row the engine has not numbered yet must still count, and
    # dropping it would rebuild the very hole this function exists to close.
    generated = job.get("generated")
    generated = generated if isinstance(generated, (int, float)) else 0
    rate = job.get("tok_per_s")
    rate = rate if isinstance(rate, (int, float)) else 0
    if generated > 0 or rate > 0:
        return "generating"
    frac = job.get("prefill_frac")
    if isinstance(frac, (int, float)) and frac < 1:
        return "reading"
    if state in _READING_STATES:
        return "reading"
    return "generating"



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
        rows = [item for item in (payload.get("data") or [])
                if isinstance(item, dict)]
        # Port 8085 is the launcher's *gateway* in front of the engine: it
        # renames the one model it proxies to ``local-model`` and publishes the
        # engine's own id under ``gateway.realModel``.  Booking the alias puts
        # every model of the day in one ledger bucket (``mc/local-model``,
        # seen 2026-11-15) next to the same model's older rows under its real
        # name -- one model reading as two, with the day's tokens split between
        # them -- and the hero card announces "local-model" instead of the
        # model that is running.  The gateway describes exactly one upstream
        # model, so the real name is used only when the list has one row.
        gateway = payload.get("gateway")
        gateway = gateway if isinstance(gateway, dict) else {}
        real = gateway.get("realModel")
        real = real.strip() if isinstance(real, str) else ""
        models = []
        for item in rows:
            ident = item.get("id")
            own = real if (real and len(rows) == 1) else None
            models.append({
                "id": own or ident,
                # The alias is kept beside it: a ledger row printed as
                # "qwen3.8-flash-next" still has to be traceable to the name
                # the gateway answers to when someone greps the API.
                "alias": ident if own else None,
                "loaded": True,
                "args": [],
                "context_length": item.get("context_length") or item.get("max_context"),
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
        # The global ``decode_tps`` gauge sits at 0.0 for this engine even
        # while it decodes -- measured 2026-11-15: tokens_generated climbing
        # at ~113 t/s, one working job reporting tok_per_s = 295, and the
        # gauge flat at zero.  The per-job rate is the real reading, so the
        # card takes the sum of the working jobs when the gauge says nothing,
        # instead of printing "0.0 t/s" while the engine is busy (the "有时
        # 候是空的" half of the report).  A gauge that does report keeps the
        # floor: it is the engine's own single number and outranks a sum.
        decode_inst = stats.get("decode_tps")
        if not decode_inst:
            busy_rates = [j.get("tok_per_s") for j in jobs
                          if _job_phase(j) == "generating"]
            busy_rates = [r for r in busy_rates
                          if isinstance(r, (int, float)) and r > 0]
            if busy_rates:
                decode_inst = sum(busy_rates)
        jobs_listed = isinstance(stats.get("jobs"), list)
        busy_rows = [j for j in jobs if _job_phase(j)]
        # ``active`` is NOT the in-flight request count: it sits at 1 on a
        # completely idle engine (measured 2026-11-15 -- no working job row,
        # decode_tps 0, tokens not moving), because it counts attached
        # conversations.  Reading it as "处理中 1 请求" made the panel claim a
        # request was running all day.  The working rows are the honest count;
        # ``active`` stays only for a build that publishes no ``jobs`` key.
        requests_processing = len(busy_rows) if jobs_listed else stats.get("active")
        detail["metrics"] = {
            "decode_tps_avg": None,
            "prefill_tps_avg": None,
            "decode_tps_inst": decode_inst,
            "prefill_tps_inst": stats.get("prefill_tps"),
            "kv_hit_ratio": stats.get("cache_hit_rate"),
            "mtp_accept_ratio": mtp_ratio,
            "mtp_tokens_per_step": accepted if detail["mtp_enabled"] else None,
            "mtp_active": detail["mtp_enabled"],
            "mtp_positions": {},
            "requests_processing": requests_processing,
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
            phase = _job_phase(job)
            if phase is None:
                continue
            context = job.get("context")
            generated = job.get("generated")
            slots.append({
                "id": job.get("id"),
                "n_ctx": n_ctx,
                "is_processing": True,
                # A working row splits its context into the prompt it is
                # reading/generated against and what it has generated.
                "n_prompt_tokens": context - (generated or 0)
                    if isinstance(context, (int, float)) else None,
                "n_prompt_tokens_processed": None,
                "n_prompt_tokens_cache": None,
                "n_decoded": generated,
                "ctx_used": context,
                "ctx_ratio": _ratio(context, n_ctx),
                "state": phase,
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
        Strata live block does.  The phase comes from :func:`_job_phase`, not
        from the state word -- see its docstring for why matching the words
        left every lamp on 空闲 while the engine was decoding.
        """
        state = {"queued": stats.get("queued") or 0}
        for job in jobs:
            if _job_phase(job) == "generating":
                state.update({
                    "state": "generating",
                    "generated": job.get("generated"),
                    "tok_s": job.get("tok_per_s"),
                })
                return state
        for job in jobs:
            if _job_phase(job) == "reading":
                state.update({
                    "state": "reading",
                    "prompt_tokens": job.get("context"),
                })
                return state
        state["state"] = "idle"
        return state
