"""Telemetry collectors for the local LLM monitoring dashboard.

Each module exposes a small, dependency-free collector:

* ``llama``  -- llama.cpp server counters and slots
* ``gpu``    -- NVIDIA utilisation, VRAM and compute tenants
* ``system`` -- host CPU, memory, disks and inference process footprint
"""
