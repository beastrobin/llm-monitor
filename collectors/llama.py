"""llama.cpp telemetry collector.

Polls a llama-server instance and turns its Prometheus counters into the
numbers a dashboard actually needs.

Two server shapes are supported and detected automatically:

* router mode -- one process fronts several model presets and loads them on
  demand.  Every telemetry endpoint then requires an explicit model query
  parameter (``/metrics?model=<id>&autoload=false``); without it the server
  answers 400 Bad Request.
* single model mode -- the classic one-process-per-model layout, where the
  same endpoints carry no model parameter at all.

Instant rates are derived from counter deltas instead of the exported gauges:
``llamacpp:predicted_tokens_seconds`` reads 0 whenever the server is idle, so
a dashboard built on the gauge alone would report a stall where there is only
a rest.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# Local endpoints must never travel through a system HTTP proxy.  On Windows
# urllib picks up the http_proxy environment variable by default, which sends
# 127.0.0.1 traffic to the proxy and fails.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_TIMEOUT = 5.0


def _resolve_api_key(spec):
    """Prefer an environment variable so keys need not live in config.json."""
    env_name = (spec.get("api_key_env") or "").strip()
    if env_name:
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    return (spec.get("api_key") or "").strip()


def _request(url, api_key, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_json(url, api_key, timeout=DEFAULT_TIMEOUT):
    return json.loads(_request(url, api_key, timeout))


def parse_prometheus(text):
    """Flatten a Prometheus exposition body into {series_name: value}.

    Names keep their label set verbatim, so ``..._per_pos_total{position="0"}``
    stays addressable.
    """
    series = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, raw = line.rpartition(" ")
        if not name:
            continue
        try:
            series[name] = float(raw)
        except ValueError:
            continue
    return series


def _delta(now_value, prev_value):
    """Counter difference, or None when the counter went backwards.

    A backwards counter means the server restarted, so the caller must drop
    the sample rather than report a negative rate.
    """
    if prev_value is None or now_value is None:
        return None
    if now_value < prev_value:
        return None
    return now_value - prev_value


def _ratio(numerator, denominator):
    if not denominator:
        return None
    return numerator / denominator


def _friendly_error(exc):
    """Short English diagnosis; the dashboard supplies the translated prefix."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "unauthorized -- check the API key"
        if exc.code == 400:
            return "bad request -- the endpoint likely needs a model parameter"
        return "HTTP %s %s" % (exc.code, exc.reason)
    text = str(exc).lower()
    if "10061" in text or "refused" in text:
        return "connection refused -- is the server running?"
    if "timed out" in text or "timeout" in text:
        return "request timed out"
    return "%s: %s" % (type(exc).__name__, exc)


def spec_type_from_args(args):
    """The draft/MTP type actually passed to the child process.

    ``/props`` reports ``speculative.types`` as ``"none"`` even while a
    ``--spec-type draft-mtp`` preset is generating, so the launch arguments are
    the only trustworthy source for "is speculative decoding on".
    """
    for index, arg in enumerate(args or []):
        if arg == "--spec-type" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--spec-type="):
            return arg.split("=", 1)[1]
    return ""


class ModelSampler(object):
    """Keeps the counter baseline for one (endpoint, model) pair."""

    def __init__(self):
        self.prev = None
        self.prev_at = None
        self.last = None

    def feed(self, series, now):
        """Return the derived metrics for one scrape.

        The first scrape of a session produces cumulative averages only --
        there is no baseline yet to difference against.
        """
        counters = {
            "prompt_tokens": series.get("llamacpp:prompt_tokens_total"),
            "prompt_cached": series.get("llamacpp:prompt_tokens_cached_total"),
            "prompt_seconds": series.get("llamacpp:prompt_seconds_total"),
            "predicted_tokens": series.get("llamacpp:tokens_predicted_total"),
            "predicted_seconds": series.get("llamacpp:tokens_predicted_seconds_total"),
            "draft_tokens": series.get("llamacpp:spec_decode_num_draft_tokens_total"),
            "accepted_tokens": series.get("llamacpp:spec_decode_num_accepted_tokens_total"),
            "drafts": series.get("llamacpp:spec_decode_num_drafts_total"),
        }

        out = {
            "decode_tps_avg": _ratio(counters["predicted_tokens"],
                                     counters["predicted_seconds"]),
            "prefill_tps_avg": _ratio(counters["prompt_tokens"],
                                      counters["prompt_seconds"]),
            "kv_hit_ratio": _ratio(counters["prompt_cached"],
                                   (counters["prompt_cached"] or 0)
                                   + (counters["prompt_tokens"] or 0)),
            "mtp_accept_ratio": _ratio(counters["accepted_tokens"],
                                       counters["draft_tokens"]),
            "mtp_tokens_per_step": _ratio(counters["accepted_tokens"],
                                          counters["drafts"]),
            "mtp_active": bool(counters["draft_tokens"]),
            "mtp_positions": {},
            "decode_tps_inst": None,
            "prefill_tps_inst": None,
            "requests_processing": series.get("llamacpp:requests_processing"),
            "requests_deferred": series.get("llamacpp:requests_deferred"),
            "busy_slots_per_decode": series.get("llamacpp:n_busy_slots_per_decode"),
            "n_decode_total": series.get("llamacpp:n_decode_total"),
            "n_tokens_max": series.get("llamacpp:n_tokens_max"),
            "counters": counters,
        }

        # Per-draft-position acceptance, e.g. how often the first and second
        # guessed token survive verification.  The denominator is the number
        # of verification steps, not the number of drafted tokens.
        drafts = counters["drafts"]
        prefix = "llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position=\""
        for name, value in series.items():
            if name.startswith(prefix):
                position = name[len(prefix):].rstrip("}").rstrip('"')
                out["mtp_positions"][position] = _ratio(value, drafts)

        # Rates are derived from the engine's own timers rather than a
        # wall-clock delta.  Dividing by wall time folds idle seconds into the
        # sample (a busy server reads far too low) and turns a burst prefill
        # into a nonsense spike.  Dividing by the seconds the engine actually
        # spent working measures the engine, not the observer.  A window with
        # no work keeps the previous reading, so an idle server shows its last
        # speed instead of dropping to zero.
        if self.prev is not None:
            dp = _delta(counters["predicted_tokens"], self.prev["predicted_tokens"])
            ds = _delta(counters["predicted_seconds"], self.prev["predicted_seconds"])
            # A backwards counter is a restart, not "no work": the previous
            # instantaneous rate belongs to the old process, so carrying it
            # forward would show the dead machine's speed as if it were live.
            reset = (
                counters["predicted_tokens"] is not None
                and self.prev["predicted_tokens"] is not None
                and counters["predicted_tokens"] < self.prev["predicted_tokens"]
            )
            if reset:
                self.last = None
            if dp is not None and ds is not None and ds > 0:
                out["decode_tps_inst"] = dp / ds
            elif self.last is not None:
                out["decode_tps_inst"] = self.last.get("decode_tps_inst")

            pp = _delta(counters["prompt_tokens"], self.prev["prompt_tokens"])
            ps = _delta(counters["prompt_seconds"], self.prev["prompt_seconds"])
            if pp is not None and ps is not None and ps > 0:
                out["prefill_tps_inst"] = pp / ps
            elif self.last is not None:
                out["prefill_tps_inst"] = self.last.get("prefill_tps_inst")

            out["window_seconds"] = now - self.prev_at if self.prev_at else None

        self.prev = counters
        self.prev_at = now
        self.last = out
        return out


class LlamaEndpoint(object):
    """One llama-server instance."""

    # A dead endpoint costs a full connect attempt every cycle, and Windows
    # needs roughly two seconds to refuse a loopback connection -- enough to
    # dominate the whole sample.  A failing endpoint therefore backs off
    # instead of being retried at full rate.
    BACKOFF_CAP = 15
    # /v1/models and per-model /props describe the model, not its activity:
    # they change only when the router (re)loads a preset, so caching them
    # cuts roughly half the round trips while /metrics and /slots stay live.
    # A miss on the loaded flag resolves within one TTL, which is fine for a
    # dashboard.
    PROPS_TTL = 60.0

    def __init__(self, spec):
        self.id = spec["id"]
        self.label = spec.get("label") or spec["id"]
        self.base_url = spec["base_url"].rstrip("/")
        self.api_key = _resolve_api_key(spec)
        self._samplers = {}
        self._failures = 0
        self._skip_cycles = 0
        self._last_error = None
        self._models_cache = (0.0, [])
        self._props_cache = {}

    def _offline_payload(self):
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "up": False,
            "mode": None,
            "models": [],
            "error": self._last_error or "unreachable",
            "backing_off": True,
            "polled_at": time.time(),
        }

    def _fail(self, exc, result):
        self._failures += 1
        self._last_error = _friendly_error(exc)
        self._skip_cycles = min(self.BACKOFF_CAP, 2 ** min(self._failures, 4))
        result["error"] = self._last_error
        result["backing_off"] = True
        return result

    def _succeed(self, result):
        self._failures = 0
        self._skip_cycles = 0
        self._last_error = None
        return result

    def _url(self, path, params=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _sampler(self, model_id):
        key = model_id or ""
        if key not in self._samplers:
            self._samplers[key] = ModelSampler()
        return self._samplers[key]

    def probe(self):
        """Cheap reachability and shape check."""
        props = _get_json(self._url("/props"), self.api_key)
        role = props.get("role")
        if role == "router":
            return {"mode": "router", "props": props}
        return {"mode": "single", "props": props}

    def list_models(self):
        """The model list, TTL-cached: a preset's status changes rarely."""
        now = time.time()
        fetched, cached = self._models_cache
        if now - fetched < self.PROPS_TTL:
            return cached
        payload = _get_json(self._url("/v1/models"), self.api_key)
        models = []
        for item in payload.get("data", []):
            status = item.get("status") or {}
            models.append({
                "id": item.get("id"),
                "loaded": status.get("value") == "loaded",
                "args": status.get("args") or [],
            })
        self._models_cache = (now, models)
        return models

    def _props_key(self, params):
        """A cache key for one /props query, from its exact parameter set."""
        if not params:
            return ""
        return urllib.parse.urlencode(params)

    def _cached_props(self, params, now):
        """A fresh or TTL-valid /props body for this query, or None."""
        key = self._props_key(params)
        entry = self._props_cache.get(key)
        if entry is not None and now - entry[0] < self.PROPS_TTL:
            return entry[1]
        return None

    def _model_detail(self, model_id, params, now, preset_args=None):
        detail = {"id": model_id, "loaded": True, "error": None}
        args = preset_args or []
        detail["preset_args"] = args
        spec_type = spec_type_from_args(args)
        detail["mtp_type"] = spec_type
        detail["mtp_enabled"] = bool(spec_type) and spec_type.lower() != "none"
        try:
            props = self._cached_props(params, now)
            if props is None:
                props = _get_json(self._url("/props", params), self.api_key)
                self._props_cache[self._props_key(params)] = (now, props)
        except Exception as exc:  # noqa: BLE001 - one bad model must not kill the poll
            detail["error"] = "%s: %s" % (type(exc).__name__, exc)
            return detail

        detail["n_ctx"] = props.get("n_ctx")
        detail["total_slots"] = props.get("total_slots")
        detail["model_path"] = props.get("model_path")
        detail["model_ftype"] = props.get("model_ftype")
        detail["modalities"] = props.get("modalities")
        settings = props.get("default_generation_settings") or {}
        if not detail.get("n_ctx"):
            detail["n_ctx"] = settings.get("n_ctx")
        detail["spec_types"] = (settings.get("params") or {}).get("speculative.types")

        try:
            slots_raw = _get_json(self._url("/slots", params), self.api_key)
            slots = []
            for slot in slots_raw if isinstance(slots_raw, list) else []:
                next_tokens = slot.get("next_token") or []
                decoded = next_tokens[0].get("n_decoded") if next_tokens else None
                prompt_tokens = slot.get("n_prompt_tokens") or 0
                used = prompt_tokens + (decoded or 0)
                slots.append({
                    "id": slot.get("id"),
                    "n_ctx": slot.get("n_ctx"),
                    "is_processing": slot.get("is_processing"),
                    "n_prompt_tokens": prompt_tokens,
                    "n_prompt_tokens_processed": slot.get("n_prompt_tokens_processed"),
                    "n_prompt_tokens_cache": slot.get("n_prompt_tokens_cache"),
                    "n_decoded": decoded,
                    "ctx_used": used,
                    "ctx_ratio": _ratio(used, detail.get("n_ctx")),
                    "spec_types": (slot.get("params") or {}).get("speculative.types"),
                })
            detail["slots"] = slots
            # The slot view reflects the speculative track actually in force;
            # the server-wide default can still read "none" while MTP runs.
            if slots and slots[0].get("spec_types"):
                detail["spec_types"] = slots[0]["spec_types"]
                if not detail["mtp_type"]:
                    detail["mtp_type"] = slots[0]["spec_types"]
                    detail["mtp_enabled"] = detail["mtp_type"].lower() != "none"
        except Exception as exc:  # noqa: BLE001
            detail["slots"] = []
            detail["slots_error"] = "%s: %s" % (type(exc).__name__, exc)

        try:
            body = _request(self._url("/metrics", params), self.api_key)
            series = parse_prometheus(body)
            detail["metrics"] = self._sampler(model_id).feed(series, now)
        except Exception as exc:  # noqa: BLE001
            detail["metrics"] = None
            detail["metrics_error"] = "%s: %s" % (type(exc).__name__, exc)

        return detail

    def poll(self):
        if self._skip_cycles > 0:
            self._skip_cycles -= 1
            return self._offline_payload()

        now = time.time()
        result = {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "up": False,
            "mode": None,
            "models": [],
            "error": None,
            "polled_at": now,
        }
        try:
            probe = self.probe()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, result)

        result["up"] = True
        result["mode"] = probe["mode"]

        try:
            known = self.list_models()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, result)

        if probe["mode"] == "router":
            # Only loaded presets serve traffic; unloaded ones would answer
            # 400 or spin up a model just to be scraped.  The full list (with
            # launch args) is still shipped so the UI can name every preset
            # and tell which of them turn MTP on even while none is loaded.
            result["available_models"] = [m["id"] for m in known]
            result["available_models_detail"] = known
            for item in known:
                if not item["loaded"]:
                    continue
                params = {"model": item["id"], "autoload": "false"}
                result["models"].append(
                    self._model_detail(item["id"], params, now, item.get("args")))
        else:
            result["available_models"] = [m["id"] for m in known]
            result["models"].append(self._model_detail(None, None, now))
            if result["models"] and result["models"][0].get("id") is None:
                result["models"][0]["id"] = known[0]["id"] if known else "default"

        return self._succeed(result)
