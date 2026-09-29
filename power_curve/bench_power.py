"""Decode-throughput benchmark at one power cap.

Streams a fixed-length completion from the local llama-server, splits prefill
from decode with the time-to-first-token, and samples GPU power draw while it
runs. Emits one JSON line per run on stdout.
"""

import argparse
import json
import os
import statistics
import subprocess
import time
import urllib.request
import threading

PORT = None  # resolved at runtime from the monitor's live endpoint


def resolve_port():
    """The current live llama-server port, via the monitor's snapshot."""
    import urllib.request
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    r = op.open("http://127.0.0.1:8090/api/snapshot", timeout=5)
    d = json.loads(r.read().decode())
    for e in d["snapshot"]["endpoints"]:
        if e.get("live") and e.get("up"):
            return int(e["base_url"].rsplit(":", 1)[1])
    raise RuntimeError("no live llama endpoint in monitor snapshot")
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_TOKENS = 1024
PROMPT = open(os.path.join(HERE, "prompt.txt"), encoding="utf-8").read().strip() + (
    " Now list, as a numbered list, every distinct optimization technique "
    "mentioned in the passage above, with one sentence explaining each."
)


def fetch_counters(port):
    """Authoritative server counters for decode throughput."""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    body = op.open("http://127.0.0.1:%d/metrics" % port, timeout=10).read().decode("utf-8", errors="replace")
    out = {}
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        for key in ("llamacpp:tokens_predicted_total",
                    "llamacpp:tokens_predicted_seconds_total"):
            if line.startswith(key + " "):
                try:
                    out[key] = float(line.rsplit(" ", 1)[1])
                except ValueError:
                    pass
    return out


def wait_for_idle(port, timeout_s=120.0):
    """Block until the server has no in-flight request (so the run is clean).

    Returns True when idle, False if it was still busy after the timeout
    (the caller then skips that run rather than measuring a shared slot).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        m = fetch_counters(port)
        # requests_processing is exposed alongside the counters; read it raw.
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = op.open("http://127.0.0.1:%d/metrics" % port, timeout=10).read().decode("utf-8", errors="replace")
        busy = False
        for line in body.splitlines():
            if line.startswith("llamacpp:requests_processing "):
                try:
                    busy = float(line.rsplit(" ", 1)[1]) > 0
                except ValueError:
                    pass
                break
        if not busy:
            return True
        time.sleep(1.0)
    return False


def send_stream(port, max_tokens):
    """One streamed completion. Returns (prefill_s, total_s).

    A 180 s socket timeout guards against the request being deferred behind
    concurrent traffic on the same server: instead of hanging the sweep, the
    run is aborted and reported as an error.
    """
    import socket
    socket.setdefaulttimeout(180)
    body = json.dumps({
        "model": "Qwen3.8-27B-UD-Q4_K_M",
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "stream": True,
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    ttfb = None
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            for ch in obj.get("choices") or []:
                if (ch.get("delta") or {}).get("content"):
                    if ttfb is None:
                        ttfb = time.perf_counter() - t0
                    break
    total = time.perf_counter() - t0
    if ttfb is None:
        ttfb = 0.0
    return ttfb, total


class GpuSampler(threading.Thread):
    """Sample nvidia-smi power draw + SM clock at ~3 Hz in the background."""

    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self.power = []
        self.clock = []

    def run(self):
        while not self._stop.is_set():
            try:
                p = subprocess.run(
                    [r"C:\Windows\System32\nvidia-smi.exe", "-i", "0",
                     "--query-gpu=power.draw,clocks.sm",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, timeout=5,
                    creationflags=0x08000000,
                )
                parts = p.stdout.decode(errors="replace").strip().split(",")
                if len(parts) == 2:
                    self.power.append(float(parts[0]))
                    self.clock.append(float(parts[1]))
            except Exception:
                pass
            self._stop.wait(0.3)

    def stop(self):
        self._stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watts", type=int, required=True)
    ap.add_argument("--applied", type=float, required=True)
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args()

    port = resolve_port()
    print(json.dumps({"port": port, "note": "live llama-server port"}), flush=True)

    for i in range(args.runs + 1):  # +1 warmup
        if not wait_for_idle(port):
            print(json.dumps({"watts": args.watts, "applied_watts": args.applied,
                              "run": i, "error": "server busy (concurrent request), run skipped"}), flush=True)
            continue
        before = fetch_counters(port)
        sampler = GpuSampler()
        sampler.start()
        try:
            prefill, total = send_stream(port, MAX_TOKENS)
        except Exception as exc:  # deferred/stalled request — skip this run
            sampler.stop()
            print(json.dumps({"watts": args.watts, "applied_watts": args.applied,
                              "run": i, "error": "%s: %s" % (type(exc).__name__, exc)}), flush=True)
            continue
        sampler.stop()
        after = fetch_counters(port)
        dtok = after.get("llamacpp:tokens_predicted_total", 0) - before.get("llamacpp:tokens_predicted_total", 0)
        dtok_s = after.get("llamacpp:tokens_predicted_seconds_total", 0) - before.get("llamacpp:tokens_predicted_seconds_total", 0)
        decode_tps = dtok / dtok_s if dtok_s > 0 else 0.0
        wall_tps = (dtok - 1) / max(total - prefill, 1e-6) if dtok else 0.0
        rec = {
            "watts": args.watts,
            "applied_watts": args.applied,
            "run": i,
            "tokens": int(dtok),
            "prefill_s": round(prefill, 3),
            "total_s": round(total, 3),
            "decode_tps": round(decode_tps, 2),
            "wall_tps": round(wall_tps, 2),
            "power_avg_w": round(statistics.mean(sampler.power), 1) if sampler.power else None,
            "power_max_w": round(max(sampler.power), 1) if sampler.power else None,
            "clock_avg_mhz": round(statistics.mean(sampler.clock), 0) if sampler.clock else None,
            "clock_max_mhz": round(max(sampler.clock), 0) if sampler.clock else None,
        }
        print(json.dumps(rec), flush=True)


if __name__ == "__main__":
    main()
