#!/usr/bin/env python3
"""Sample accessible Intel DRM clients without perf privileges or extra packages."""

import json
import os
from pathlib import Path
import re
import sys
import time
from typing import TypedDict


class Client(TypedDict):
    engines: dict[str, int]
    capacities: dict[str, int]
    private: int | None


class Snapshot(TypedDict):
    device: str
    boot: str
    time: int
    clients: dict[str, Client]


class Thermal(TypedDict):
    temperature: float
    critical: float | None
    label: str


class Device(TypedDict):
    busy: float | None
    engines: dict[str, float]
    memoryPrivate: int | None
    frequencyMHz: int | None
    thermals: list[Thermal]
    error: str


DRM_NODE = re.compile(r"/dev/dri/(card\d+|renderD\d+)")


def integer(value: str, unit: str = "") -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*" + re.escape(unit) + r"\s*", value)
    return int(match[1]) if match else None


def memory(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*(KiB|MiB)?\s*", value)
    return int(match[1]) * {None: 1, "KiB": 1024, "MiB": 1048576}[match[2]] if match else None


def parse_client(text: str, devices: set[str]) -> tuple[str, str, Client] | None:
    fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    device = fields.get("drm-pdev", "").strip()
    if fields.get("drm-driver", "").strip() not in ("i915", "xe") or device not in devices:
        return None
    ident = fields.get("drm-client-id", "").strip()
    if not ident.isdecimal():
        return None
    engines: dict[str, int] = {}
    capacities: dict[str, int] = {}
    for key, value in fields.items():
        if key.startswith("drm-engine-capacity-"):
            count = integer(value)
            if count is None or count == 0:
                raise ValueError("Invalid Intel engine capacity")
            capacities[key.removeprefix("drm-engine-capacity-")] = count
        elif key.startswith("drm-engine-"):
            count = integer(value, "ns")
            if count is None:
                raise ValueError("Invalid Intel engine counter")
            engines[key.removeprefix("drm-engine-")] = count
    # Exclude all shared buffers: fdinfo cannot identify and deduplicate them
    # across clients. This is private allocation size, not total/resident VRAM.
    private = 0
    totals = {key.removeprefix("drm-total-"): memory(value)
              for key, value in fields.items() if key.startswith("drm-total-")}
    for region, total in totals.items():
        shared = memory(fields.get("drm-shared-" + region, ""))
        if total is None or shared is None or shared > total:
            return device, ident, {"engines": engines, "capacities": capacities, "private": None}
        private += total - shared
    return device, ident, {"engines": engines, "capacities": capacities, "private": private if totals else None}


def node_device(node: str, sys_root: Path) -> str | None:
    try:
        return (sys_root / "class/drm" / node / "device").resolve(strict=True).name.lower()
    except OSError:
        return None


def read_clients(devices: set[str], proc: Path = Path("/proc"),
                 sys_root: Path = Path("/sys")) -> dict[str, dict[str, Client]]:
    deadline = time.monotonic() + 1.0
    clients: dict[str, dict[str, Client]] = {device: {} for device in devices}
    owners: dict[str, str | None] = {}
    for process in proc.iterdir():
        if time.monotonic() > deadline:
            raise TimeoutError("Intel process scan exceeded its budget")
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for fd in (process / "fd").iterdir():
                if time.monotonic() > deadline:
                    raise TimeoutError("Intel descriptor scan exceeded its budget")
                try:
                    # Printing an xe fdinfo takes a runtime-PM reference and wakes the
                    # GPU. Resolve the descriptor first; only read requested devices.
                    node = DRM_NODE.fullmatch(os.readlink(fd))
                    if node is None:
                        continue
                    if node[1] not in owners:
                        owners[node[1]] = node_device(node[1], sys_root)
                    if owners[node[1]] not in devices:
                        continue
                    parsed = parse_client((process / "fdinfo" / fd.name).read_text(), devices)
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
                if parsed:
                    device, ident, client = parsed
                    # Processes may share a DRM file; count its client only once per device.
                    if ident not in clients[device]:
                        clients[device][ident] = client
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return clients


def load_snapshot(path: Path) -> Snapshot | None:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("device"), str) or not isinstance(raw.get("boot"), str):
        return None
    if type(raw.get("time")) is not int or raw["time"] < 0 or not isinstance(raw.get("clients"), dict):
        return None
    clients: dict[str, Client] = {}
    for ident, client in raw["clients"].items():
        if not isinstance(ident, str) or not isinstance(client, dict):
            return None
        counters: dict[str, dict[str, int]] = {}
        for field in ("engines", "capacities"):
            values = client.get(field)
            if not isinstance(values, dict) or any(not isinstance(k, str) or type(v) is not int or v < (1 if field == "capacities" else 0) for k, v in values.items()):
                return None
            counters[field] = values
        private = client.get("private")
        if private is not None and (type(private) is not int or private < 0):
            return None
        clients[ident] = {"engines": counters["engines"], "capacities": counters["capacities"], "private": private}
    return {"device": raw["device"], "boot": raw["boot"], "time": raw["time"], "clients": clients}


def utilization(current: Snapshot, previous: Snapshot | None, window_ns: int = 30_000_000_000) -> dict[str, float]:
    if previous is None or current["device"] != previous["device"] or current["boot"] != previous["boot"]:
        return {}
    elapsed = current["time"] - previous["time"]
    # Callers that space reads out (gpu_stats.py's sleep policy) widen the window;
    # busy is then averaged over that longer gap rather than discarded.
    if not 0 < elapsed <= window_ns:
        return {}
    totals: dict[str, int] = {}
    capacities: dict[str, int] = {}
    for ident, client in current["clients"].items():
        old = previous["clients"].get(ident)
        if old is None:
            continue
        for engine, counter in client["engines"].items():
            capacity = client["capacities"].get(engine, 1)
            if engine not in old["engines"] or capacity != old["capacities"].get(engine, 1):
                continue
            # Kernel counters can briefly regress. Retain the high-water mark
            # until they catch up so the next sample cannot count work twice.
            prior = old["engines"][engine]
            client["engines"][engine] = max(counter, prior)
            totals[engine] = totals.get(engine, 0) + max(0, counter - prior)
            capacities[engine] = max(capacities.get(engine, 1), capacity)
    return {engine: round(min(100.0, 100 * total / elapsed / capacities[engine]), 1)
            for engine, total in totals.items()}


def collect_device(device: str, card: Path | None, state: Path, clients: dict[str, Client],
                   proc: Path = Path("/proc"), window_ns: int = 30_000_000_000) -> Device:
    try:
        current: Snapshot = {"device": device, "boot": (proc / "sys/kernel/random/boot_id").read_text().strip(),
                             "time": time.monotonic_ns(), "clients": clients}
        engines = utilization(current, load_snapshot(state), window_ns)
        # stats.sh owns the shared flock and a private state directory. Rename
        # atomically so interrupted helpers never leave half a baseline behind.
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, separators=(",", ":")))
        temporary.replace(state)
        frequencies = []
        thermals: list[Thermal] = []
        if card is not None:
            for path in card.glob("gt/gt*/rps_act_freq_mhz"):
                value = integer(path.read_text())
                if value is not None:
                    frequencies.append(value)
            # Only use sensors attached to this GPU, never a CPU/package temperature
            # or a sensor belonging to a different graphics device.
            for path in (card / "device").glob("hwmon/hwmon*/temp*_input"):
                value = integer(path.read_text())
                if value is None:
                    continue
                prefix = path.name.removesuffix("_input")
                limit = path.with_name(prefix + "_crit")
                critical = integer(limit.read_text()) if limit.exists() else None
                label = path.with_name(prefix + "_label")
                thermals.append({"temperature": value / 1000,
                                 "critical": critical / 1000 if critical else None,
                                 "label": label.read_text().strip() if label.exists() else "GPU"})
        allocations = [client["private"] for client in clients.values()]
        return {"busy": max(engines.values(), default=None), "engines": engines,
                "memoryPrivate": sum(v for v in allocations if v is not None) if allocations and all(v is not None for v in allocations) else None,
                "frequencyMHz": max(frequencies, default=None), "thermals": thermals, "error": ""}
    except (OSError, ValueError) as error:
        print(f"Intel telemetry: {error}", file=sys.stderr)
        return unavailable()


def unavailable() -> Device:
    return {"busy": None, "engines": {}, "memoryPrivate": None, "frequencyMHz": None, "thermals": [],
            "error": "Intel GPU readings unavailable. Retrying…"}
