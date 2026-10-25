#!/usr/bin/env python3
"""List every PCI GPU with its own readings, without waking GPUs that are asleep."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, TypedDict

import intel_stats
import nvidia_stats


class Card(TypedDict):
    id: str
    vendor: str
    driver: str
    device: Path
    card: Path | None


class Reading(TypedDict):
    busy: float | None
    vramUsed: int | None
    vramTotal: int | None
    memoryPrivate: int | None
    frequencyMHz: int | None
    thermals: list[dict[str, Any]]
    error: str


DRIVERS = {"amdgpu": "amd", "radeon": "amd", "i915": "intel", "xe": "intel", "nvidia": "nvidia", "nouveau": "nvidia"}
VENDOR_NAMES = {"amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA"}
BRAND_WORDS = {"AMD", "ATI", "Radeon", "NVIDIA", "GeForce", "Intel", "Arc", "Graphics"}
LABELS = {"edge": "Edge", "junction": "Hotspot", "mem": "Memory"}
ADDRESS = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
ERROR = "GPU readings unavailable. Retrying…"


def empty(error: str = "") -> Reading:
    return {"busy": None, "vramUsed": None, "vramTotal": None, "memoryPrivate": None,
            "frequencyMHz": None, "thermals": [], "error": error}


def optional(path: Path) -> str | None:
    """One sysfs file's text, or None when that file alone is missing or unreadable."""
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def integer(path: Path) -> int | None:
    text = optional(path)
    return int(text) if text is not None and re.fullmatch(r"-?\d+", text) else None


def card_number(path: Path) -> int:
    return int(path.name.removeprefix("card"))


def discover(sys_root: Path) -> list[Card]:
    cards: list[Card] = []
    try:
        devices = sorted((sys_root / "bus/pci/devices").iterdir())
    except OSError:
        return cards
    for device in devices:
        try:
            # Only a bound graphics driver makes a GPU: passthrough stubs (vfio-pci)
            # and BMC framebuffers (ast) expose no readings worth a row.
            driver = (device / "driver").readlink().name if (device / "driver").is_symlink() else ""
            if driver not in DRIVERS or not ADDRESS.fullmatch(device.name.lower()):
                continue
            if not (device / "class").read_text().strip().startswith("0x03"):
                continue
            drm = sorted((p for p in device.glob("drm/card*") if p.name[4:].isdecimal()), key=card_number)
        except OSError:
            continue
        cards.append({"id": device.name.lower(), "vendor": DRIVERS[driver], "driver": driver,
                      "device": device, "card": drm[0] if drm else None})
    return cards


def gpu_type(card: Card) -> str | None:
    if card["vendor"] == "amd":
        # Only dedicated VRAM has a memory vendor; APU carve-outs do not.
        if (card["device"] / "mem_info_vram_vendor").exists():
            return "discrete"
        return "integrated" if card["driver"] == "amdgpu" else None
    if card["vendor"] == "intel":
        return "integrated" if card["id"] == "0000:00:02.0" else "discrete"
    return "discrete"


def gpu_name(vendor_id: str, device_id: str, pci_ids: Path) -> str | None:
    vendor = vendor_id.lower().removeprefix("0x")
    product = device_id.lower().removeprefix("0x")
    try:
        with pci_ids.open(encoding="utf-8", errors="replace") as lines:
            inside = False
            for line in lines:
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    if inside:
                        return None
                    inside = line[:4].lower() == vendor and line[4:6] == "  "
                elif inside and not line.startswith("\t\t") and line[1:5].lower() == product and line[5:7] == "  ":
                    name = line[7:].strip()
                    bracketed = re.search(r"\[(.+)\]", name)
                    return (bracketed[1] if bracketed else name) or None
    except OSError:
        return None
    return None


def short_name(name: str | None, card: Card) -> str:
    words = (name or "").split()
    while words and words[0] in BRAND_WORDS:
        words.pop(0)
    if words:
        return " ".join(words)
    if name:
        return name
    return f"{VENDOR_NAMES[card['vendor']]} {card['id'][5:]}"


def closed_interval(card: Card) -> int:
    """Minimum gap between closed-popup reads in ns; 0 when reads are never spaced."""
    # Integrated GPUs autosuspend by default (i915, APUs). Spacing them would slow
    # a lone iGPU's bar, so they keep the normal cadence.
    if gpu_type(card) == "integrated":
        return 0
    device = card["device"]
    try:
        if (device / "power/control").read_text().strip() != "auto":
            return 0
    except OSError:
        return 0  # No runtime PM, so polling cannot keep the device awake.
    # Every read resets the idle timer. Leave gaps well past the autosuspend
    # delay so an idle GPU can actually suspend while the popup is closed.
    delay = integer(device / "power/autosuspend_delay_ms")
    return max(2 * delay * 1_000_000, 15 * 1_000_000_000) if delay is not None else 30 * 1_000_000_000


def poll_due(card: Card, last_read_ns: int | None, now_ns: int, mode: str) -> bool:
    if mode == "open" or last_read_ns is None or now_ns < last_read_ns:
        return True
    return now_ns - last_read_ns >= closed_interval(card)


def read_sysfs(device: Path) -> Reading:
    # Each value is read on its own, so one missing or failing file (EIO/EINVAL)
    # costs only that value or sensor, never the whole row.
    reading = empty()
    busy = integer(device / "gpu_busy_percent")
    reading["busy"] = busy if busy is not None and 0 <= busy <= 100 else None
    used = integer(device / "mem_info_vram_used")
    total = integer(device / "mem_info_vram_total")
    if total is not None and total > 0 and used is not None and 0 <= used <= total:
        reading["vramUsed"], reading["vramTotal"] = used, total
    inputs = sorted(device.glob("hwmon/hwmon*/temp*_input"),
                    key=lambda p: (p.parent.name, int(re.sub(r"\D", "", p.name) or 0)))
    for path in inputs:
        value = integer(path)
        if value is None:
            continue
        prefix = path.name.removesuffix("_input")
        critical = integer(path.with_name(prefix + "_crit"))
        # Map known driver labels to stable display strings; never pass arbitrary text through.
        name = LABELS.get(optional(path.with_name(prefix + "_label")) or "")
        reading["thermals"].append({"temperature": value / 1000,
                                    "critical": critical / 1000 if critical and critical > 0 else None,
                                    "label": name or "Sensor " + prefix.removeprefix("temp")})
    return reading


def number(value: Any) -> bool:
    return value is None or (type(value) in (int, float))


def valid_reading(raw: Any) -> bool:
    if not isinstance(raw, dict) or set(raw) != set(Reading.__annotations__):
        return False
    if not all(number(raw[key]) for key in ("busy", "vramUsed", "vramTotal", "memoryPrivate", "frequencyMHz")):
        return False
    return isinstance(raw["error"], str) and isinstance(raw["thermals"], list) and all(
        isinstance(t, dict) and set(t) == {"temperature", "critical", "label"} and number(t["temperature"])
        and number(t["critical"]) and isinstance(t["label"], str) for t in raw["thermals"])


def reject(constant: str) -> None:
    raise ValueError(f"Non-finite number {constant} in GPU state")


def load_state(path: Path, boot: str | None) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(), parse_constant=reject)
    except (OSError, ValueError):
        return {}
    if boot is None or not isinstance(raw, dict) or raw.get("boot") != boot or not isinstance(raw.get("devices"), dict):
        return {}
    devices = {}
    for ident, entry in raw["devices"].items():
        if not isinstance(entry, dict) or not (entry.get("name") is None or isinstance(entry["name"], str)):
            continue
        devices[ident] = {"name": entry.get("name")}
        # readAt and reading only ever travel together; one without the other is discarded.
        if type(entry.get("readAt")) is int and entry["readAt"] >= 0 and valid_reading(entry.get("reading")):
            devices[ident].update(readAt=entry["readAt"], reading=entry["reading"])
    return devices


def read_intel(cards: list[Card], state_dir: Path, proc: Path, sys_root: Path) -> dict[str, Reading]:
    if not cards:
        return {}
    try:
        # Only due cards are passed, so an asleep GPU's descriptors are never read.
        clients = intel_stats.read_clients({card["id"] for card in cards}, proc, sys_root)
    except (OSError, ValueError) as error:
        print(f"Intel telemetry: {error}", file=sys.stderr)
        clients = None
    readings: dict[str, Reading] = {}
    for card in cards:
        # Spaced reads land one stats.sh cadence past the interval; allow a
        # whole extra interval so Intel keeps its baseline instead of reading null.
        window = max(30_000_000_000, 2 * closed_interval(card))
        device = (intel_stats.collect_device(card["id"], card["card"], state_dir / f"intel-{card['id']}.json",
                                             clients[card["id"]], proc, window)
                  if clients is not None else intel_stats.unavailable())
        readings[card["id"]] = {**empty(device["error"]), "busy": device["busy"], "memoryPrivate": device["memoryPrivate"],
                                "frequencyMHz": device["frequencyMHz"], "thermals": device["thermals"]}
    return readings


def collect(mode: str, state_dir: Path, sys_root: Path = Path("/sys"), proc: Path = Path("/proc"),
            pci_ids: Path = Path("/usr/share/hwdata/pci.ids"), now_ns: int | None = None,
            nvidia=nvidia_stats.collect) -> list[dict]:
    now = time.monotonic_ns() if now_ns is None else now_ns
    try:
        boot: str | None = (proc / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = None
    stored = load_state(state_dir / "gpus.json", boot)
    cards = discover(sys_root)
    asleep: set[str] = set()
    due: list[Card] = []
    for card in cards:
        try:
            status = (card["device"] / "power/runtime_status").read_text().strip()
        except OSError:
            status = ""
        if status == "suspended":
            asleep.add(card["id"])
        elif poll_due(card, stored.get(card["id"], {}).get("readAt"), now, mode):
            due.append(card)

    readings: dict[str, Reading] = {}
    nvidia_ids = [card["id"] for card in due if card["driver"] == "nvidia"]
    names: dict[str, str] = {}
    # nvidia-smi (1.5 s) and the Intel scan (1.0 s) are bounded separately; run
    # them side by side so both timing out still fits the caller's 2.5 s limit.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(nvidia, nvidia_ids) if nvidia_ids else None
        readings.update(read_intel([card for card in due if card["vendor"] == "intel"], state_dir, proc, sys_root))
        for card in due:
            if card["vendor"] == "amd" or card["driver"] == "nouveau":
                try:
                    readings[card["id"]] = read_sysfs(card["device"])
                except (OSError, ValueError) as error:
                    # A device unplugged mid-read loses its own readings only.
                    print(f"GPU {card['id']}: {error}", file=sys.stderr)
                    readings[card["id"]] = empty(ERROR)
        try:
            found = pending.result() if pending else {}
        except Exception as error:  # noqa: BLE001 - a crashed probe must still yield rows
            print(f"NVIDIA telemetry: {error}", file=sys.stderr)
            found = {}
    for ident in nvidia_ids:
        device = found.get(ident) or nvidia_stats.unavailable()
        if device["name"]:
            names[ident] = device["name"]
        readings[ident] = {**empty(device["error"]), "busy": device["busy"], "vramUsed": device["vramUsed"],
                           "vramTotal": device["vramTotal"], "thermals": device["thermals"]}

    gpus: list[dict] = []
    devices: dict[str, dict[str, Any]] = {}
    for card in cards:
        ident = card["id"]
        entry = stored.get(ident, {})
        if ident in names:
            name = names[ident]
        elif "name" in entry:
            name = entry["name"]
        else:
            try:
                vendor, product = ((card["device"] / f).read_text().strip() for f in ("vendor", "device"))
                name = gpu_name(vendor, product, pci_ids)
            except OSError:
                name = None
        devices[ident] = {"name": name}
        if ident in asleep:
            # Drop the carried reading so a GPU that wakes is read right away, not shown stale.
            reading = empty()
        elif ident in readings:
            reading = readings[ident]
            devices[ident].update(readAt=now, reading=reading)
        else:
            reading = entry["reading"]
            devices[ident].update(readAt=entry["readAt"], reading=reading)
        gpus.append({"id": ident, "vendor": card["vendor"], "driver": card["driver"], "name": name,
                     "shortName": short_name(name, card), "type": gpu_type(card), "asleep": ident in asleep,
                     **reading})

    if boot is not None:
        try:
            # stats.sh holds the flock; rename atomically so a killed run leaves no half file.
            temporary = state_dir / "gpus.json.tmp"
            temporary.write_text(json.dumps({"boot": boot, "devices": devices}, separators=(",", ":"), allow_nan=False))
            temporary.replace(state_dir / "gpus.json")
        except (OSError, ValueError) as error:
            print(f"GPU state: {error}", file=sys.stderr)
    return gpus


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("open", "closed"))
    parser.add_argument("state", type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.mode, args.state), separators=(",", ":"), allow_nan=False))
