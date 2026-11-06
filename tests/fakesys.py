"""Fake /proc and /sys trees for the black-box stats helper tests.

The helper runs as a process against these trees (FOAMY_VITALS_PROC and
FOAMY_VITALS_SYS, its test-only overrides) with a private XDG_RUNTIME_DIR, and
fake executables (nvidia-smi) first on PATH. FOAMY_HELPER is the helper's
argv prefix as a JSON list; the default is this checkout's helper.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import time

ROOT = Path(__file__).resolve().parents[1]
NVIDIA_ADDRESS = "0000:01:00.0"
INTEL_DEVICE = "0000:00:02.0"


def helper_prefix(variable="FOAMY_HELPER", default=None):
    """The helper argv prefix: $variable (a JSON list) or the default, program resolved to an absolute path."""
    raw = os.environ.get(variable)
    prefix = json.loads(raw) if raw else list(default or [str(ROOT / "stats.sh")])
    if not isinstance(prefix, list) or not prefix or not all(isinstance(part, str) for part in prefix):
        raise SystemExit(f"{variable} must be a JSON list of strings")
    program = prefix[0] if "/" in prefix[0] else shutil.which(prefix[0])
    if not program:
        raise SystemExit(f"helper program not found: {prefix[0]}")
    return [str(Path(program).resolve()), *prefix[1:]]


HELPER = helper_prefix()


def strict(text):
    """json.loads with strict=True rejects raw control characters, as JSON.parse does."""
    return json.loads(text, strict=True)


def intel_fdinfo(ident=1, render=100, video=0, driver="i915", device=INTEL_DEVICE):
    return (f"drm-driver: {driver}\n"
            f"drm-pdev: {device}\n"
            f"drm-client-id: {ident}\n"
            f"drm-engine-render: {render} ns\n"
            f"drm-engine-video: {video} ns\n"
            "drm-engine-capacity-video: 2\n"
            "drm-total-system0: 64 MiB\n"
            "drm-shared-system0: 1024 KiB\n")


class Tree:
    """A minimal /proc and /sys the helper can sample."""

    def __init__(self, base, helper=None):
        self.base = Path(base)
        self.helper = list(helper or HELPER)
        self.proc = self.base / "proc"
        self.sys = self.base / "sys"
        self.bin = self.base / "bin"
        self.runtime = self.base / "runtime"
        for path in (self.proc / "net", self.sys / "class/hwmon", self.bin):
            path.mkdir(parents=True)
        self.runtime.mkdir(mode=0o700)
        (self.proc / "meminfo").write_text("MemTotal:       16000000 kB\nMemFree: 1 kB\nMemAvailable:   12000000 kB\n")
        (self.proc / "net/route").write_text(
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "wl\x01an0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000\n")
        (self.proc / "net/dev").write_text(
            "Inter-|   Receive\n face |bytes\n"
            "wl\x01an0:12345678901 10 0 0 0 0 0 0 98765 20 0 0 0 0 0 0\n")
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("boot-1\n")
        self.uptime(1000.0)
        self.cpu(0)

    @property
    def state(self):
        return self.runtime / f"foamy-vitals-{os.geteuid()}"

    def uptime(self, seconds):
        (self.proc / "uptime").write_text(f"{seconds:.2f} 0.00\n")

    def advance(self, tick, seconds):
        self.cpu(tick)
        self.uptime(1000.0 + seconds)

    def cpu(self, tick):
        busy, idle = 100 + 50 * tick, 1000 + 50 * tick
        (self.proc / "stat").write_text(
            f"cpu  {busy * 2} 0 0 {idle * 2} 0 0 0 0 0 0\n"
            f"cpu0 {busy} 0 0 {idle} 0 0 0 0 0 0\n"
            f"cpu1 {busy} 0 0 {idle} 0 0 0 0 0 0\n")

    def process(self, pid, name, jiffies, rss_pages, cwd=None, kernel=False):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        flags = 2097152 if kernel else 4194304
        fields = ["S", "1", str(pid), str(pid), "0", "-1", str(flags), "0", "0", "0", "0",
                  str(jiffies), "0", "0", "0", "20", "0", "1", "0", "100", "1000", str(rss_pages)]
        raw = name if isinstance(name, bytes) else name.encode("utf-8", "surrogateescape")
        (directory / "stat").write_bytes(f"{pid} (".encode() + raw + b") " + " ".join(fields).encode() + b"\n")
        link = directory / "cwd"
        if cwd is not None and not link.is_symlink():
            link.symlink_to(cwd)

    def hwmon(self, index, name, temps, parent=None):
        directory = (parent or self.sys / "class/hwmon") / f"hwmon{index}"
        directory.mkdir(parents=True)
        if name is not None:
            (directory / "name").write_text(name + "\n")
        for number, (label, value, critical) in enumerate(temps, 1):
            (directory / f"temp{number}_input").write_text(f"{value}\n")
            if critical is not None:
                (directory / f"temp{number}_crit").write_text(f"{critical}\n")
            if label:
                (directory / f"temp{number}_label").write_text(label + "\n")
        return directory

    def nvidia(self, status="active", control="auto"):
        (self.proc / f"driver/nvidia/gpus/{NVIDIA_ADDRESS}").mkdir(parents=True)
        power = self.sys / f"bus/pci/devices/{NVIDIA_ADDRESS}/power"
        power.mkdir(parents=True)
        (power / "runtime_status").write_text(status + "\n")
        (power / "control").write_text(control + "\n")
        log = self.base / "nvidia-smi.log"
        reply = self.base / "nvidia-smi.reply"
        xml = self.base / "nvidia-smi.xml"
        failtemp = self.base / "nvidia-smi.failtemp"
        reply.write_text("00000000:01:00.0, 37, 1178, 8192, 51\n")
        script = self.bin / "nvidia-smi"
        script.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            printf '%s\\n' "$*" >>'{log}'
            case "$*" in
              *--query-gpu*) [ -s '{reply}' ] || exit 6; cat '{reply}' ;;
              *TEMPERATURE*) [ -e '{failtemp}' ] && exit 9; [ -s '{xml}' ] && exec cat '{xml}'; printf '<?xml version="1.0"?>\\n<nvidia_smi_log><gpu id="00000000:01:00.0"><temperature><gpu_temp>51 C</gpu_temp><gpu_temp_max_threshold>98 C</gpu_temp_max_threshold></temperature></gpu></nvidia_smi_log>\\n' ;;
            esac
            """))
        script.chmod(0o755)
        return log, reply

    def intel(self, driver="i915", device=INTEL_DEVICE, card="card1"):
        """A bound Intel GPU: driver dir, PCI device, DRM card. Returns (card dir, device dir)."""
        drivers = self.sys / "bus/pci/drivers" / driver
        (drivers / device).mkdir(parents=True, exist_ok=True)
        domain = device.split(":")[0]
        pci = self.sys / f"devices/pci{domain}:00/{device}"
        pci.mkdir(parents=True, exist_ok=True)
        if not (pci / "driver").is_symlink():
            (pci / "driver").symlink_to(drivers)
        directory = self.sys / "class/drm" / card
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / "device").is_symlink():
            (directory / "device").symlink_to(pci)
        return directory, pci

    def descriptor(self, pid, number, text, target="/dev/dri/renderD128"):
        """One open file of a fake process: its fd link and its fdinfo (a dir when text is None)."""
        for sub in ("fd", "fdinfo"):
            (self.proc / str(pid) / sub).mkdir(parents=True, exist_ok=True)
        link = self.proc / str(pid) / "fd" / str(number)
        if not link.is_symlink():
            link.symlink_to(target)
        info = self.proc / str(pid) / "fdinfo" / str(number)
        if text is None:
            info.mkdir(exist_ok=True)
        else:
            info.write_text(text)

    def intel_state(self, ago_ns, clients, device=INTEL_DEVICE, boot="boot-1"):
        """Write the Intel baseline as if a sample had been taken ago_ns before now."""
        self.state.mkdir(mode=0o700, exist_ok=True)
        snapshot = {"device": device, "boot": boot, "time": time.monotonic_ns() - ago_ns, "clients": clients}
        (self.state / "intel.json").write_text(json.dumps(snapshot, separators=(",", ":")))

    def env(self):
        return dict(os.environ, FOAMY_VITALS_PROC=str(self.proc), FOAMY_VITALS_SYS=str(self.sys),
                    XDG_RUNTIME_DIR=str(self.runtime), PATH=f"{self.bin}:{os.environ['PATH']}")

    def run_raw(self, *args, env=None):
        return subprocess.run([*self.helper, *args], env=env or self.env(), capture_output=True, timeout=20)

    def run(self, *args, check=True):
        result = self.run_raw(*args)
        if check:
            assert result.returncode == 0, result.stderr.decode(errors="replace")
        return strict(result.stdout.decode("utf-8", "replace")), result.stderr.decode("utf-8", "replace")
