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

The desktop app moves the engine (see "where the engine is" below), so
``config.json`` cannot name it: this module finds the address and the key that
belong to the engine answering *now*, the same way the Hermes collector follows
its router's ``config.yaml``.

Finding the address is only half of it.  The engine answers ``/health`` to
anyone, and that body says ``"api_key": true`` to mean the rest of the server is
not open: ``/props``, ``/metrics`` and ``/v1/models`` need the key the app
generated along with the port.  A card that learns the port but not the key is
up, names the model, reports its context window and shows not one number -- the
same blank card the dialect mistake used to produce, only with a 401 in place of
a JSON error.  So the key is taken from the same record the address comes from,
and a scrape that comes back unauthorised goes and asks that record for it.
"""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from .llama import LlamaEndpoint, _get_json, _ratio

# ---------------------------------------------------------------------------
# Where the engine is
# ---------------------------------------------------------------------------
#
# The desktop app is not the engine.  It is a web UI with an OpenAI-compatible
# proxy in front of a ``serve/server.py`` process it starts itself, and it
# starts that process on a **port it picks at that moment**, guarded by a
# **key it generates at that same moment**.  Both change when a model is
# reloaded, let alone when the app restarts.  A collector pinned to the address
# from the last conversation is then in one of two useless states: the port is
# free and the card says "connection refused", or the port belongs to something
# else entirely and the card reports a model nobody is running.
#
# Asking the app itself does not work either.  Its server owns ``/`` and answers
# an unknown path -- ``/props``, ``/metrics``, ``/slots`` -- with the SPA's
# HTML, so a scrape fails with a JSON error rather than a 404, and the same
# HTML answers the health check that would otherwise identify it.
#
# What the app *does* answer is its own ``/v1/models``, and every model there
# carries ``meta.instanceId``.  That id is the file name of the engine record
# the app wrote when it started the process, and the record is exactly what
# ``serve.server --config`` was handed: ``host``, ``port``, ``api_key``, the
# launch arguments and the GGUF behind them.  Read from the user's own state
# directory, it is not a sniffed credential -- it is the app's private
# directory, which is also where its own key would have been found.
#
# Three sources, in this order, because each covers a case the others miss:
#
# 1. the address already in hand answers ``/health`` as a Strata engine.  That
#    is a hand-started server, or the engine this card followed last time, and
#    an explicitly configured working address outranks any guessing.
# 2. the app address (the configured one, or the one last followed) names an
#    instance id.  Precise even with two engines alive, since the id says which
#    one *this* URL serves -- and followed even when that engine does not
#    answer yet, because a model that is still loading is the model the reader
#    is waiting for, not a reason to go showing someone else's.
# 3. the newest engine record that answers, and only when no app answered at
#    all: the app's own port went stale -- it restarted and ``config.json`` has
#    not caught up -- so the records are the only address left.  This source is
#    a guess about *which* process is the app's, so it is verified before the
#    card moves.
#
# An address is never abandoned for a move that found nothing: the card keeps
# its last one and reports its real error, which reads truer than the name of a
# port with nothing behind it.

#: ``/health`` names the service; a llama.cpp server answers something else, and
#: an app answers with HTML.
SERVICE = "strata"

#: Where the app keeps its engine records, below the user's home directory.
INSTANCE_SUBPATH = (".strata", "data", "native-instances")

#: How often to look for the engine while one is answering.  The address moves
#: when a model is loaded, which is measured in seconds -- but a card that
#: re-resolved every cycle would spend two extra round trips per sample to
#: notice nothing had moved.
FOLLOW_SECONDS = 15.0
#: ... and while none is: recovery is what the reader is waiting for.
FOLLOW_RETRY_SECONDS = 5.0

#: How many engine records to try when nothing else named one, newest first.
#: Each costs one ``/health``, and a dead port costs about two seconds of
#: refused connection on Windows -- four of them would dominate a sample.
INSTANCE_SWEEP = 3

PROBE_TIMEOUT = 2.0


def without_api_suffix(url):
    """A server address from a client address.

    An operator pastes the URL they call -- ``host:port/v1`` -- while every
    collector here appends its own paths (``/props``, ``/metrics``, and
    ``/v1/models`` on top of the root).  Keeping the suffix would ask for
    ``/v1/v1/models`` and read as a server with no models.
    """
    trimmed = (url or "").rstrip("/")
    if trimmed.lower().endswith("/v1"):
        trimmed = trimmed[:-3].rstrip("/")
    return trimmed


def instance_directory(spec=None):
    """The directory holding the app's engine records.

    ``STRATA_INSTANCE_DIR`` and the endpoint's own ``instance_dir`` exist for
    the two cases the default cannot know: a second Windows user profile
    serving the models, and a machine where the app's state was moved off the
    account the dashboard runs under.
    """
    for value in (os.environ.get("STRATA_INSTANCE_DIR"),
                  (spec or {}).get("instance_dir")):
        value = (value or "").strip()
        if value:
            return Path(value)
    return Path.home().joinpath(*INSTANCE_SUBPATH)


def instance_files(directory):
    """Engine records on disk, the one written last first."""
    try:
        stamped = [(path.stat().st_mtime, path) for path in directory.glob("*.json")]
    except OSError:
        return []
    stamped.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in stamped]


def _record_url(record):
    host = str(record.get("host") or "127.0.0.1").strip()
    if host in ("", "0.0.0.0", "::", "*"):
        # Bound to every interface: the loopback one is the address to dial.
        host = "127.0.0.1"
    port = record.get("port")
    if not port:
        return None
    return "http://%s:%s" % (host, port)


def _address(url):
    """The ``(host, port)`` a URL dials, or None when there is no URL.

    The key lookup has to compare an address the operator typed with one the app
    wrote down, and the two spell the same server differently: ``localhost`` and
    ``127.0.0.1``, one with ``/v1`` still on the end.  Only host and port take
    part, because that is the whole address as far as connecting goes.
    """
    text = (url or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "//" in text else "//" + text)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def read_instance(path):
    """What one engine record says about the engine, or None.

    None covers "not there", "not readable" and "not a record" together: the
    caller's next move is the same for all three -- try the next source -- and a
    record the app is still writing is exactly the file that fails to parse.
    """
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    base_url = _record_url(record)
    if not base_url:
        return None
    return {
        "base_url": base_url,
        "api_key": str(record.get("api_key") or "").strip(),
        "model": str(record.get("model_name") or ""),
        "path": str(path),
    }


def _answers_engine(health):
    """Whether a ``/health`` body is a Strata engine's.

    The ``service`` tag is the clean answer.  The second shape is for an engine
    build predating it: ``model`` beside ``max_context`` is what only a loaded
    Strata model reports, and a llama.cpp ``/health`` says nothing that
    specific -- while the app's answer is not JSON at all.
    """
    if not isinstance(health, dict):
        return False
    if health.get("service") == SERVICE:
        return True
    return "model" in health and "max_context" in health


def engine_health(base_url, api_key="", timeout=PROBE_TIMEOUT):
    """The ``/health`` body of a Strata engine at this address, or None.

    The key is sent along but is not what makes the check work: the engine
    answers its health check to anyone, so the address can be identified before
    its counters are unlocked.  None covers down, HTML and llama.cpp in one
    stroke -- the caller's next move is the same for all three.
    """
    try:
        health = _get_json(base_url + "/health", api_key, timeout)
    except Exception:  # noqa: BLE001 - down, wrong dialect or a web page
        return None
    return health if _answers_engine(health) else None


def is_engine(base_url, api_key="", timeout=PROBE_TIMEOUT):
    """Whether this address is a Strata engine."""
    return engine_health(base_url, api_key, timeout) is not None


def _locks_telemetry(health):
    """Whether this engine keeps ``/props`` and ``/metrics`` behind a key.

    ``/health`` is open and reports ``"api_key": true`` to say the rest of the
    server is not.  Without that distinction a card that found the right port
    and none of its key would call itself healthy -- model name, context
    window, and every rate blank.
    """
    return bool(health and health.get("api_key"))


def _is_authorisation_failure(error):
    """Whether a scrape came back refusing the key.

    Matched loosely on purpose: :func:`..llama._friendly_error` turns a real 401
    into "unauthorized -- check the API key", while a proxy or a test double can
    word the same refusal differently.  The retry is harmless in the loose case
    -- adopting a key from the record either opens the counters or reports the
    same error again.
    """
    text = (error or "").lower()
    return "401" in text or "unauthoriz" in text



def instance_id_of_app(base_url, timeout=PROBE_TIMEOUT):
    """The instance id an app address publishes for the model it serves."""
    try:
        models = _get_json(base_url + "/v1/models", "", timeout)
    except Exception:  # noqa: BLE001 - not an app, or not up
        return None
    if not isinstance(models, dict):
        return None
    for item in models.get("data") or []:
        if isinstance(item, dict):
            instance = (item.get("meta") or {}).get("instanceId")
            if instance:
                return str(instance)
    return None


def _reported_slot_count(live, rows):
    """How many batch slots this engine runs.

    ``/metrics`` carries one row per slot even when every slot is idle, so the
    length of that list is normally the answer.  Keep ``live.parallel`` as the
    fallback: a frame without rows still has to say how many slots were asked
    for, because the panel shows *why* the breakdown is empty rather than
    hiding it -- and "no rows" has two different causes here.

    The second cause is the one users keep reporting as "the slots never leave
    idle".  With ``--batch`` a **lone** request goes back to the single-request
    path (serve/server.py's ``_may_go_solo`` / ``generate_batched``), where the
    engine decodes on the control lines and books nothing against a numbered
    slot; only from two concurrent requests up does each slot get its own
    reading row.  So ``live.running >= 1`` together with every slot ``idle`` is
    the engine's honest state, not a collector bug, and the UI says exactly that
    instead of letting six cards of em-dashes look like a broken scrape.
    """
    return len(rows) or live.get("parallel") or None


class StrataEndpoint(LlamaEndpoint):
    """One Strata server, scraped through its JSON ``/metrics``.

    Two things differ from the llama.cpp collector: ``_model_detail`` reads the
    JSON dialect, and ``poll`` first follows the server to wherever the desktop
    app has put it now (see "where the engine is" at the top of this module).
    The probe, the model list, the TTL caches, the failure backoff and
    ``set_base_url`` describe what an endpoint *is* rather than what it speaks,
    and they are inherited so the two dialects cannot drift apart.
    """

    #: What the card is standing for, when the address in ``config.json`` is
    #: the desktop **app** rather than the engine: the engine is what gets
    #: polled, and the app URL is the address the rest of the dashboard has to
    #: recognise as the same machine.  ``None`` when the configured address
    #: really was the engine.
    proxied_url = None
    #: How the current address was arrived at, for run.log and for an empty card.
    follow_reason = ""

    def __init__(self, spec, *args, **kwargs):
        spec = dict(spec)
        # Tolerate the URL as an operator writes it (".../v1", the one the app
        # tells you to paste) and hand the collector the server instead.
        spec["base_url"] = without_api_suffix(spec.get("base_url"))
        super().__init__(spec, *args, **kwargs)
        self._instance_dir = instance_directory(spec)
        # ``"discover": false`` opts out: a server reached through a jump box,
        # or one whose state directory this process cannot read, is then polled
        # exactly where config.json says it is.
        self.discover = bool(spec.get("discover", True))
        self._next_follow = 0.0
        self._engine_answered = False
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

    def _app_urls(self):
        """Addresses that could be the desktop app: this one, and the last one it was."""
        urls = []
        for url in (self.base_url, self.proxied_url):
            url = without_api_suffix(url)
            if url and url not in urls:
                urls.append(url)
        return urls

    def _named_by_app(self):
        """The engine an app address says it is serving, and that address.

        Returns ``(instance, app_url)`` or None.  None means the app did not
        answer at all, or named an instance whose record this process cannot
        read (another account's state directory, say) -- cases where the
        newest-record sweep is the only hint left.
        """
        for app_url in self._app_urls():
            instance_id = instance_id_of_app(app_url)
            if not instance_id:
                continue
            instance = read_instance(
                self._instance_dir / ("%s.json" % instance_id))
            if instance is not None:
                return instance, app_url
        return None

    def _records(self, skip):
        """Engine records worth trying, newest first, capped.

        The cap is not politeness: the app leaves one record per instance id it
        has ever started, so the directory fills with processes that are long
        gone, and each ask against a dead port costs about two seconds of
        refused connection on Windows.
        """
        for path in instance_files(self._instance_dir)[:INSTANCE_SWEEP]:
            instance = read_instance(path)
            if instance is None:
                continue
            key = (instance["base_url"], instance["api_key"])
            if key in skip:
                continue
            skip.add(key)
            yield instance, path.name

    def _records_for(self, base_url):
        """The engine records that describe this exact address, newest first.

        No HTTP here, just the files the app wrote: a card that reached the
        engine by sweep or by hand-typed address has the port and none of the
        key the process on it was started with, and the record for that port is
        the only place the key is written down.
        """
        wanted = _address(base_url)
        if wanted is None:
            return []
        matches = []
        for path in instance_files(self._instance_dir):
            instance = read_instance(path)
            if instance and _address(instance["base_url"]) == wanted:
                matches.append(instance)
        return matches

    def _unlock_key(self):
        """Adopt the key the app wrote down for the process on this address.

        Returns whether the key changed.  Reached in two places only: the engine
        identified itself and nothing was configured, or a scrape came back
        refusing the key in hand.  A key that opens the counters is never
        second-guessed -- nothing here asks the server whether a working key is
        the intended one -- and an engine that locks nothing never gets here.
        """
        for instance in self._records_for(self.base_url):
            key = instance.get("api_key")
            if key and key != self.api_key:
                self._adopt(
                    instance, self.proxied_url,
                    "the engine record %s carries the key for %s"
                    % (Path(instance["path"]).name, self.base_url),
                    True)
                return True
        return False

    def _adopt(self, instance, app_url, reason, answered):
        """Point this card at one engine record, key included.

        The key travels with the address because a record *is* a process: the
        next process to own the port owns a different key, and polling the new
        one with the old key answers 401 -- which reads on screen exactly like a
        server with no model.
        """
        moved = self.set_base_url(instance["base_url"], instance["api_key"])
        self._engine_answered = answered
        self.follow_reason = reason if answered else reason + " -- not answering yet"
        if app_url:
            # Remembered even when the address did not move: it is what tells
            # the dashboard this card already covers the app's address, so the
            # same server is not shown -- and its tokens not counted -- twice.
            self.proxied_url = app_url
        return moved

    def follow_now(self, force=False):
        """Re-resolve the engine address, honouring the throttle.

        Public because the dashboard logs the moment a card starts watching a
        different port, and that only works if the move happens where the
        dashboard is looking.  Returns whether the card moved -- to a new address
        or, when the address was right all along, to the key that opens it.
        """
        now = time.time()
        if not self.discover:
            return False
        if not force and now < self._next_follow:
            return False
        self._next_follow = now + (
            FOLLOW_SECONDS if self._engine_answered else FOLLOW_RETRY_SECONDS)

        # Source 1: this address is the engine, and it answers.  Nothing to
        # follow -- which is also the case for a hand-started
        # ``serve/server.py`` on a fixed port, the reason the app is not simply
        # assumed.
        health = engine_health(self.base_url, self.api_key)
        if health is not None:
            self._engine_answered = True
            # Right port, no key: the engine names itself to anyone and locks
            # everything else behind the key the app minted with this port.  A
            # hand-started server asks for nothing, so this is only reached when
            # the engine says it locks its counters and config.json has nothing
            # to offer.
            if _locks_telemetry(health) and not self.api_key and self._unlock_key():
                return True
            if not self.follow_reason:
                self.follow_reason = "%s answers /health as a Strata engine" % (
                    self.base_url,)
            return False

        # Source 2: the app names the engine it serves.  Followed even when
        # that engine does not answer yet -- an engine that is still loading is
        # the model the reader is waiting for, and a *different* engine that
        # happens to be up would fill this card with another model's numbers.
        named = self._named_by_app()
        if named is not None:
            instance, app_url = named
            return self._adopt(
                instance, app_url,
                "the app at %s is serving %s" % (app_url, instance["base_url"]),
                is_engine(instance["base_url"], instance["api_key"]))

        # Source 3: nothing named anything, so trust the newest record that
        # actually answers.  Verification is the whole point here -- an
        # unverified guess would put a stale record's port on the card and
        # leave the reader to work out which of the two is lying.
        for instance, name in self._records(set()):
            if is_engine(instance["base_url"], instance["api_key"]):
                return self._adopt(
                    instance, self.proxied_url,
                    "the newest engine record, %s" % name, True)

        # Nothing answered anywhere.  Stay where we are: the card then reports
        # the address it had with its real error, which is a truer sentence than
        # the name of a port with nothing behind it.
        self._engine_answered = False
        return False

    def poll(self):
        self.follow_now()
        result = super().poll()
        # A key that has outlived the process it belonged to is invisible to
        # ``follow_now``: the port still answers ``/health``, so there is no move
        # to make, and every counter the card wants says 401.  That is one file
        # read and one retry, taken in the same sample the error would have been
        # reported in -- the backoff would otherwise leave the card blank for up
        # to fifteen cycles over a key sitting in a file the collector already
        # knows how to open.
        if (self.discover and not result["up"]
                and _is_authorisation_failure(result.get("error"))
                and self._unlock_key()):
            result = super().poll()
        if self.follow_reason:
            result["follow_reason"] = self.follow_reason
        if self.proxied_url:
            result["proxied_url"] = self.proxied_url
        return result

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
                        # A prefix cannot be longer than the string it is a
                        # prefix of.  held > prompt therefore means this request
                        # is NOT continuing the conversation the slot held -- a
                        # different one arrived, and the slot gave nothing back.
                        # Reporting held/prompt there produced 244% and 19224%
                        # on the card, which is not a hit rate at all.
                        if not held or not prompt:
                            kv_hit = None
                        elif held > prompt:
                            kv_hit = 0.0
                        else:
                            kv_hit = held / prompt
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
        detail["total_slots"] = _reported_slot_count(live, live.get("slots") or [])
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
        # `running` matters on its own: it is how many requests are in flight,
        # while a slot row only says whether that numbered batch slot was
        # claimed.  They differ -- see `_reported_slot_count`.
        for key in ("state", "phase", "parallel", "queued", "waiting", "running",
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
        # TTFT is the engine's own measurement -- seconds from a request
        # arriving to the first token coming out -- and the retained request
        # history (newest first) is the only place it appears. A row carries
        # the number once its first token lands, so the newest row that has
        # one is "the last finished request"; the mean covers every row the
        # engine kept. llama.cpp /metrics has no such field anywhere, which is
        # why the card shows a dash there rather than inventing a proxy.
        ttfts = []
        for row in (recent or []):
            value = None
            if isinstance(row, dict):
                # ``first_engine_token_s`` is Strata's name for it; the basalt
                # engine (same Monitor dialect) calls the same measurement
                # ``ttft_s``.  Read either so one TTFT path serves both.
                value = row.get("first_engine_token_s")
                if not isinstance(value, (int, float)):
                    value = row.get("ttft_s")
            if isinstance(value, (int, float)):
                ttfts.append(float(value))
        if ttfts:
            metrics["ttft_s_last"] = ttfts[0]
            metrics["ttft_s_mean"] = sum(ttfts) / len(ttfts)
            metrics["ttft_n"] = len(ttfts)
        else:
            metrics["ttft_s_last"] = None
            metrics["ttft_s_mean"] = None
            metrics["ttft_n"] = 0
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
