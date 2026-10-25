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


class Device(TypedDict):
    name: str | None
    busy: float | None
    vramUsed: int | None
    vramTotal: int | None
    thermals: list[Thermal]
    error: str


class Stats(TypedDict):
    gpuBusy: float | None
    vramUsed: int | None
    vramTotal: int | None
    gpuTemp: float | None
    gpuThermals: list[Thermal]
    gpuError: str


ERROR = "NVIDIA readings unavailable. Check nvidia-smi."


def number(text: str | None, unit: str, maximum: float | None = None) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*" + re.escape(unit) + r"\s*", text or "")
    if not match:
        return None
    value = float(match[1])
    return value if math.isfinite(value) and (maximum is None or value <= maximum) else None


def pci_address(bus_id: str) -> str | None:
    """Normalize nvidia-smi's 00000000:01:00.0 to the sysfs form 0000:01:00.0."""
    match = re.fullmatch(r"[0-9a-f]{4}([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])", bus_id.strip(), re.I)
    return match[1].lower() if match else None


def unavailable() -> Device:
    return {"name": None, "busy": None, "vramUsed": None, "vramTotal": None, "thermals": [], "error": ERROR}


def parse_device(gpu: ET.Element) -> Device:
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
        "name": (gpu.findtext("product_name") or "").strip() or None,
        "busy": number(gpu.findtext("utilization/gpu_util"), "%", 100),
        "vramUsed": int(used * 1048576) if used is not None else None,
        "vramTotal": int(total * 1048576) if total is not None else None,
        "thermals": [{"temperature": temperature, "critical": critical, "label": "GPU"}],
        "error": "",
    }


def parse_devices(xml: str) -> dict[str, Device]:
    devices: dict[str, Device] = {}
    for gpu in ET.fromstring(xml).findall("gpu"):
        address = pci_address(gpu.get("id", ""))
        if address:
            devices[address] = parse_device(gpu)
    return devices


def collect(addresses: list[str]) -> dict[str, Device]:
    if not addresses:
        return {}
    try:
        result = subprocess.run(
            ["nvidia-smi", "-q", "-x", "-i", ",".join("0000" + address for address in addresses)],
            capture_output=True, text=True, timeout=1.5, check=True,
        )
        found = parse_devices(result.stdout)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        # A failed probe must not leave stale successful readings in the panel.
        found = {}
    return {address: found.get(address) or unavailable() for address in addresses}


def legacy_stats() -> Stats:
    # stats.sh's legacy path uses this until the multi-GPU collector replaces it.
    try:
        result = subprocess.run(["nvidia-smi", "-q", "-x"], capture_output=True, text=True, timeout=1.5, check=True)
        devices = parse_devices(result.stdout)
        gpu = devices[min(devices)]
    except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError):
        return {"gpuBusy": None, "vramUsed": None, "vramTotal": None, "gpuTemp": None,
                "gpuThermals": [], "gpuError": "NVIDIA telemetry unavailable; check nvidia-smi."}
    return {"gpuBusy": gpu["busy"], "vramUsed": gpu["vramUsed"], "vramTotal": gpu["vramTotal"],
            "gpuTemp": gpu["thermals"][0]["temperature"], "gpuThermals": gpu["thermals"], "gpuError": ""}


if __name__ == "__main__":
    print(json.dumps(legacy_stats(), separators=(",", ":"), allow_nan=False))
