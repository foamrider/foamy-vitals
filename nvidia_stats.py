#!/usr/bin/env python3
"""Read NVIDIA telemetry without changing GPU settings or adding Python packages."""

import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from typing import TypedDict


class Thermal(TypedDict):
    temperature: float | None
    critical: float | None
    label: str


class Stats(TypedDict):
    gpuBusy: float | None
    vramUsed: int | None
    vramTotal: int | None
    gpuTemp: float | None
    gpuThermals: list[Thermal]
    gpuError: str


def number(text: str | None, unit: str, maximum: float | None = None) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*" + re.escape(unit) + r"\s*", text or "")
    if not match:
        return None
    value = float(match[1])
    return value if math.isfinite(value) and (maximum is None or value <= maximum) else None


def parse_stats(xml: str) -> Stats:
    gpus = sorted(ET.fromstring(xml).findall("gpu"), key=lambda gpu: gpu.get("id", ""))
    if not gpus:
        raise ValueError("NVIDIA driver returned no GPUs")
    # Keep every reading on one device; never combine one GPU's load with another's memory.
    gpu = gpus[0]
    used = number(gpu.findtext("fb_memory_usage/used"), "MiB", (2**53 - 1) / 1048576)
    total = number(gpu.findtext("fb_memory_usage/total"), "MiB", (2**53 - 1) / 1048576)
    if total is not None and (total <= 0 or (used is not None and used > total)):
        used = total = None
    temperature = number(gpu.findtext("temperature/gpu_temp"), "C")
    # The shutdown threshold is driver-reported. Target temperatures and T.Limit
    # headroom are different quantities and must not become invented critical limits.
    critical = number(gpu.findtext("temperature/gpu_temp_max_threshold"), "C")
    if critical is not None and critical <= 0:
        critical = None
    return {
        "gpuBusy": number(gpu.findtext("utilization/gpu_util"), "%", 100),
        "vramUsed": int(used * 1048576) if used is not None else None,
        "vramTotal": int(total * 1048576) if total is not None else None,
        "gpuTemp": temperature,
        "gpuThermals": [{"temperature": temperature, "critical": critical, "label": "GPU"}],
        "gpuError": "",
    }


def collect() -> Stats:
    try:
        result = subprocess.run(
            ["nvidia-smi", "-q", "-x"], capture_output=True, text=True, timeout=1.5, check=True
        )
        return parse_stats(result.stdout)
    except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError):
        # A failed probe must not leave stale successful readings in the panel.
        return {"gpuBusy": None, "vramUsed": None, "vramTotal": None, "gpuTemp": None,
                "gpuThermals": [], "gpuError": "NVIDIA telemetry unavailable; check nvidia-smi."}


if __name__ == "__main__":
    print(json.dumps(collect(), separators=(",", ":"), allow_nan=False))
