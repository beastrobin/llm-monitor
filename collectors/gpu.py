"""NVIDIA GPU telemetry collector.

Reads nvidia-smi in CSV mode rather than binding to NVML, so the dashboard
keeps working on a machine where pynvml is absent -- which is the whole point
of a zero-dependency build.

VRAM attribution comes from ``--query-compute-apps``: that reports the actual
compute tenants on the card instead of guessing from a total.
"""

import os
import subprocess
import time

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Field name -> output key.  Queried in one shot because each nvidia-smi call
# costs a process spawn.
GPU_FIELDS = [
    ("index", "index", str),
    ("name", "name", str),
    ("utilization.gpu", "util_pct", float),
    ("utilization.memory", "mem_util_pct", float),
    ("memory.used", "mem_used_mb", float),
    ("memory.total", "mem_total_mb", float),
    ("temperature.gpu", "temp_c", float),
    ("power.draw", "power_w", float),
    ("power.limit", "power_limit_w", float),
    # The min/max pair drives the power-limit control: the UI needs to know
    # which presets this particular card will actually accept.
    ("power.min_limit", "power_min_w", float),
    ("power.max_limit", "power_max_w", float),
    ("fan.speed", "fan_pct", float),
    ("clocks.sm", "clock_sm_mhz", float),
    ("clocks.mem", "clock_mem_mhz", float),
    ("pcie.link.gen.current", "pcie_gen", str),
    ("pcie.link.width.current", "pcie_width", str),
]

# Fallback for drivers that reject one of the optional fields above.
BASE_FIELDS = {
    "index", "name", "utilization.gpu", "memory.used", "memory.total",
    "temperature.gpu", "power.draw",
}


def _run(args, timeout=6.0):
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def _to_number(raw, cast):
    text = (raw or "").strip()
    if not text or text.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]"):
        return None
    try:
        return cast(text)
    except (TypeError, ValueError):
        return None


def _query_gpus(timeout=6.0):
    def build(fields):
        return [
            "nvidia-smi",
            "--query-gpu=" + ",".join(f for f, _, _ in fields),
            "--format=csv,noheader,nounits",
        ]

    rc, out = _run(build(GPU_FIELDS), timeout)
    if rc != 0:
        fallback = [f for f in GPU_FIELDS if f[0] in BASE_FIELDS]
        rc, out = _run(build(fallback), timeout)
        if rc != 0:
            return None, "nvidia-smi failed (rc=%s)" % rc
        fields = fallback
    else:
        fields = GPU_FIELDS

    gpus = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(fields):
            continue
        # Pre-seed the keys the dashboard reads unconditionally, so a fallback
        # query that drops optional columns still yields a consistent shape.
        gpu = {"mem_ratio": None, "power_min_w": None, "power_max_w": None}
        for (_, key, cast), raw in zip(fields, parts):
            gpu[key] = _to_number(raw, cast)
        used, total = gpu.get("mem_used_mb"), gpu.get("mem_total_mb")
        if used is not None and total:
            gpu["mem_ratio"] = used / total
        gpus.append(gpu)
    return gpus, None


# The tenant list changes slowly but costs its own nvidia-smi spawn, so it is
# refreshed on a slower cadence than utilisation.
PROCESS_TTL_SECONDS = 10.0
_process_cache = {"at": 0.0, "value": []}


def _query_processes():
    now = time.time()
    if now - _process_cache["at"] < PROCESS_TTL_SECONDS:
        return _process_cache["value"]

    def remember(value):
        _process_cache["at"] = now
        _process_cache["value"] = value
        return value

    rc, out = _run([
        "nvidia-smi",
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return remember([])
    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        name = parts[1] or ""
        used = _to_number(parts[2], float)
        is_llama = "llama" in name.lower()
        # Under WDDM every desktop process holds a GPU context, yet the driver
        # reports no per-process VRAM for most of them, which buries the real
        # tenants in noise.  Keep only rows carrying a number or belonging to
        # an inference server.
        if used is None and not is_llama:
            continue
        procs.append({
            "pid": _to_number(parts[0], int),
            "name": os.path.basename(name),
            "path": name,
            "used_mb": used,
            "is_llama": is_llama,
        })
    procs.sort(key=lambda p: (not p["is_llama"], -(p["used_mb"] or 0)))
    return remember(procs)


def collect(timeout=6.0):
    """Return GPU state, or a payload describing why it is unavailable."""
    gpus, error = _query_gpus(timeout)
    if error:
        return {"available": False, "error": error, "gpus": [], "processes": []}
    return {
        "available": True,
        "error": None,
        "gpus": gpus or [],
        "processes": _query_processes(),
    }
