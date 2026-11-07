"""Byte-for-byte comparison of two stats helpers on the same fake trees.

  FOAMY_VITALS_REFERENCE='["/path/to/old/stats.sh"]' python3 -I tests/compare_stats.py [-v]

Runs every scenario below twice, once with the reference helper (an argv
prefix as a JSON list, e.g. stats.sh from a 1.1.0 checkout, which also needs
its intel_stats.py beside it) and once with
FOAMY_HELPER (default: this checkout's helper), each on a fresh fake tree, and
requires identical stdout bytes, exit codes, state files (cpu.*, procs.*,
cache.*, gpus.*, current; gpu-names as a set of lines; intel-<ID>.json with
its clock field left out) and nvidia-smi calls after every step. Without
FOAMY_VITALS_REFERENCE it skips. Engine percentages depend on the real clock,
so the Intel scenarios keep to readings that don't (no baseline, a capped
engine, errors).

The multi-GPU reference is #2's stats.sh with its intel_stats.py beside it:
  git show <#2 head>:stats.sh > ref/stats.sh; git show <#2 head>:intel_stats.py > ref/intel_stats.py
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakesys import FIFO, HELPER, Tree, amd_files, helper_prefix, intel_fdinfo, nv_bus  # noqa: E402

REFERENCE = helper_prefix("FOAMY_VITALS_REFERENCE") if os.environ.get("FOAMY_VITALS_REFERENCE") else None
STATE_FILES = ("current", "cpu.0", "cpu.1", "procs.0", "procs.1", "cache.0", "cache.1", "gpus.0", "gpus.1")
PCI_IDS = ("# fixture\n"
           "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n"
           "\t150e  Strix [Radeon 880M / 890M]\n"
           "\t7590  Navi 44 [Radeon RX 9050 / 9060 XT]\n"
           "\t\t1002 0001  Subsystem [Not This]\n"
           "\t7591  Evil \"quote\" \\ back\\slash\n"
           "\t7592  [x]\n"
           "\t7593  a [b] c [d] e\n"
           "\t7594  [] [\n"
           "\t7595  \x01\t\x02\n"
           "\t7590  Duplicate [Second]\n"
           "8086  Intel Corporation\n"
           "\t7d55  Arrow Lake-P [Intel Graphics]\n"
           "10de  NVIDIA Corporation\n"
           "\t2786  AD104 [GeForce RTX 4070]\n"
           "C 03  Display controller\n"
           "\t0300  Not a device\n"
           "\t00  VGA compatible controller\n")
HOME = os.environ.get("HOME", "/nonexistent-home")


class Recorder:
    def __init__(self, tree):
        self.tree = tree
        self.steps = []

    def __call__(self, *args, env=None):
        result = self.tree.run_raw(*args, env=env)
        state = {}
        for name in STATE_FILES:
            path = self.tree.state / name
            if path.exists():
                state[name] = path.read_bytes()
        names = self.tree.state / "gpu-names"
        if names.exists():
            # The shell writes them in hash order.
            head, _, rest = names.read_bytes().partition(b"\n")
            state["gpu-names"] = (head, sorted(rest.split(b"\n")))
        for intel in sorted(self.tree.state.glob("intel-*.json")) if self.tree.state.exists() else ():
            try:
                raw = intel.read_bytes()
            except OSError as error:
                state[intel.name] = type(error).__name__
                continue
            try:
                snapshot = json.loads(raw)
                if isinstance(snapshot, dict):
                    snapshot.pop("time", None)
                state[intel.name] = snapshot
            except ValueError:
                state[intel.name] = raw
        log = self.tree.base / "nvidia-smi.log"
        calls = log.read_text() if log.exists() else ""
        self.steps.append({"args": args, "exit": result.returncode, "stdout": result.stdout,
                           "intel_error": b"Intel telemetry:" in result.stderr, "state": state, "calls": calls})


# --- scenarios: each gets a fresh Tree and a recorder `run` ------------------

def s_hostile_names(t, run):
    specs = {101: ('evil\x01na"me\\\x1b[31m', 80, 300000, "/home/user/pro\x02ject\"x", False),
             102: ("tab\there", 20, 1000, "/home/user/second", False),
             103: ("kworker/0:1", 999, 0, None, True),
             104: ("\x7f\x01\x02", 5, 10, None, False),
             105: ("bytes\udcff\udcfe", 1, 20, None, False),
             106: ("par (en) )s", 7, 30, "relative", False)}
    for pid, (name, _, rss, cwd, kernel) in specs.items():
        t.process(pid, name, 0, rss, cwd=cwd, kernel=kernel)
    run()
    t.advance(1, 2)
    for pid, (name, jiffies, rss, _, kernel) in specs.items():
        t.process(pid, name, jiffies, rss, kernel=kernel)
    run()
    run("--bar")
    t.advance(2, 4.5)
    run("--bar")
    run()


def s_ranking_ties_and_odd_stat_lines(t, run):
    for pid in (9, 10, 100, 11, 2, 1000):
        t.process(pid, f"same{pid}", 0, 50)
    run()
    t.advance(1, 2)
    for pid in (9, 10, 100, 11, 2, 1000):
        t.process(pid, f"same{pid}", 10, 50)
    odd = {"12abc": b"12abc (odd) S 1 1 1 0 -1 4194304 0 0 0 0 30 0 0 0 20 0 1 0 100 1000 40\n",
           "13": b"(13) S 1 1 1 0 -1 4194304 0 0 0 0 30 0 0 0 20 0 1 0 100 1000 40\n",
           "14": b"14 noparens S 1\n",
           "15": b"15 (short) S\n",
           "16": b"16 (floaty) S 1 1 1 0 -1 4194304 0 0 0 0 1.5 2.25 0 0 20 0 1 0 100 1000 1.5\n",
           "17": b"17 (neg) S 1 1 1 0 -1 -1 0 0 0 0 12abc 0 0 0 20 0 1 0 100 1000 3e2\n",
           "18": b"18 (empty)\n",
           "19": b"",
           "20": b"20 (hi\xff\xfe) S 1 1 1 0 -1 4194304 0 0 0 0 9 0 0 0 20 0 1 0 100 1000 77",
           "21": b"21\t(tabbed) S 1 1 1 0 -1 4194304 0 0 0 0 9 0 0 0 20 0 1 0 100 1000 77\n"}
    for pid, line in odd.items():
        (t.proc / pid).mkdir()
        (t.proc / pid / "stat").write_bytes(line)
    (t.proc / "22").mkdir()
    run()
    run("--bar")


def s_working_directories(t, run):
    targets = {201: HOME, 202: "/", 203: "/proc/1", 204: "/sys/x", 205: "/dev/pts", 206: "relative",
               207: "/a/b/", 208: "/a/\tb\t", 209: '/a/b"\x01c', 210: "/srv/project (deleted)",
               211: HOME + "/code", 212: "/procfs/x", 213: "/a/\xff\xfe"}
    for pid, target in targets.items():
        t.process(pid, f"cwd{pid}", 0, pid, cwd=target)
    (t.proc / "214").mkdir()
    t.process(214, "plaindir", 0, 214)
    (t.proc / "214/cwd").mkdir()
    run()
    t.advance(1, 2)
    for pid in targets:
        t.process(pid, f"cwd{pid}", pid - 200, pid)
    run()


def s_stale_baseline_and_cache(t, run):
    t.process(300, "busy", 0, 50)
    run()
    run("--bar")
    t.advance(1, 1.5)
    t.process(300, "busy", 40, 50)
    run()
    t.advance(2, 3)
    run("--bar")
    run()
    t.advance(3, 30)
    t.process(300, "busy", 90, 50)
    run("--bar")
    t.advance(4, 33)
    run()
    t.uptime(900)
    run()
    run("--bar")


def s_cpu_counters(t, run):
    t.process(300, "p", 0, 1)
    (t.proc / "stat").write_text("cpu  10 0 0 100\ncpu0 5 0 0 50\ncpu2 5 0 0 50\ncpufoo 1 2 3 4\nintr 5\n")
    run("--bar")
    t.uptime(1003)
    (t.proc / "stat").write_text("cpu  20 0 0 90 5 5\ncpu0 1.5 0 0 50\ncpu2 9e3 0 0 60\ncpufoo 1 2 3 4\n cpu3 1 1 1 1\ncpu-1 4 4 4 4\n")
    run("--bar")
    t.uptime(1006)
    (t.proc / "stat").write_text("cpu  20 0 0 90 5 5\ncpu0 1.5 0 0 50\ncpu2 9e3 0 0 60\n")
    run()
    t.uptime(1009)
    (t.proc / "stat").write_text("cpu  20000000000000000000 0 0 1\ncpu0 abc 0 0 x\ncpu1 12abc 0 0 3\n")
    run()
    t.uptime(1012)
    (t.proc / "stat").write_text("cpu  20000000000000000999 0 0 2\ncpu0 abd 0 0 x\ncpu1 13abc 0 0 4\n")
    run()
    t.uptime(1015)
    (t.proc / "stat").write_text("")
    run()
    t.uptime(1018)
    t.cpu(5)
    run()


def s_memory(t, run):
    for step, text in enumerate(("MemTotal: 100 kB\n", "MemTotal: 100 kB\nMemAvailable: 200 kB\n",
                                 "MemTotal: abc kB\nMemAvailable: 1 kB\n", "MemTotal: 12abc\nMemAvailable: 1.9\n",
                                 "MemTotal: -5\nMemAvailable: 1\n", "MemAvailable: 7\nMemTotal: 9\nMemTotal: 10\n", "")):
        (t.proc / "meminfo").write_text(text)
        t.uptime(1000 + 3 * step)
        run("--bar")


def s_network(t, run):
    route = t.proc / "net/route"
    dev = t.proc / "net/dev"
    header = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
    cases = [
        (header + "eth0\t00000000\t0\t3\t0\t0\t100\t0\nwlan0\t00000000\t0\t3\t0\t0\t50\t0\nlo\t00000000\t0\t3\t0\t0\t1\t0\n",
         "  eth0: 1 2 3 4 5 6 7 8 9 10\nwlan0:123456789012345678901 0 0 0 0 0 0 0 55 0\n"),
        (header + "eth0\t00000000\t0\t3\t0\t0\t100\t0\neth1\t00000000\t0\t3\t0\t0\t100\t0\n",
         "eth0: 1 2\neth1: 3 4 5 6 7 8 9 10 11\n"),
        (header + "eth0\t0000FEA9\t0\t3\t0\t0\t100\t0\n", "eth0: 1 2 3 4 5 6 7 8 9\n"),
        (header + "\x01\x02\t00000000\t0\t3\t0\t0\t100\t0\n", "\x01\x02: 1 2 3 4 5 6 7 8 9\n"),
        (header + "e th\t00000000\t0\t3\t0\t0\t1\t0\n", "e th: 1 2 3 4 5 6 7 8 9\n"),
        (header + "eth0\t00000000\t0\t3\t0\t0\t1\t0\n", "eth0: -5 2 3 4 5 6 7 8 1e3\neth0: 7 2 3 4 5 6 7 8 9\n"),
        (header + 'e"\\x\t00000000\t0\t3\t0\t0\t1\t0\n', 'e"\\x: 1 2 3 4 5 6 7 8 9\n'),
        (header, "eth0: 1 2 3 4 5 6 7 8 9\n"),
    ]
    for step, (routes, counters) in enumerate(cases):
        route.write_text(routes)
        dev.write_text(counters)
        t.uptime(1000 + 3 * step)
        run("--bar")


def s_missing_procfs_files(t, run):
    t.process(300, "p", 5, 1)
    for step, name in enumerate(("net/route", "net/dev", "meminfo", "stat", "uptime")):
        if name != "uptime":
            t.uptime(1000 + 3 * step)
        (t.proc / name).unlink()
        run()


def s_uptime_formats(t, run):
    for text in ("2000\n", "2003.5 1\n", "2007.123456 1\n", ".5\n", "abc\n", "2012.3x\n", "2015.00", "", "  2020.25\n"):
        (t.proc / "uptime").write_text(text)
        run("--bar")


def s_pointer_and_cache_files(t, run):
    run("--bar")
    state = t.state
    for step, pointer in enumerate(("garbage\n", "1 1 0 1 -1 bar\n", "2 0 0 0 0 full\n", "0 0 0 1 1000000 full",
                                    "0 0 5 0 999000 weird\n", "1 0 0 0 1001000\n", "")):
        (state / "current").write_text(pointer)
        (state / "cache.0").write_text('{"cached":0}\n')
        (state / "cache.1").write_text('{"cached":1}' if step % 2 else "not json\n")
        t.uptime(1001 + step * 0.5)
        run("--bar")
        run()


def s_cpu_temperatures(t, run):
    t.hwmon(0, "nvme", [("Composite", 40000, 80000)])
    t.hwmon(10, "coretemp", [("Core 0", 50000, 100000), ("Core 1", 51000, 100000), ("Package id 0", 62600, 105000)])
    t.hwmon(2, "coretemp", [("Package id 1", 1000, 1000)])
    run("--bar")
    shutil.rmtree(t.sys / "class/hwmon/hwmon10")
    shutil.rmtree(t.sys / "class/hwmon/hwmon2")
    d = t.hwmon(3, "k10temp", [("Tccd1", 70000, None), ("Tctl", -1600, 95000)])
    t.uptime(1003)
    run("--bar")
    (d / "temp2_input").write_text("abc\n")
    t.uptime(1006)
    run("--bar")
    (d / "temp2_input").unlink()
    zones = t.sys / "class/thermal"
    for index, (kind, temp) in enumerate((("acpitz", 30000), ("x86_pkg_temp", 64500), ("x86_pkg_temp", 1))):
        zone = zones / f"thermal_zone{index if index != 2 else 10}"
        zone.mkdir(parents=True)
        (zone / "type").write_text(kind + "\n")
        (zone / "temp").write_text(f"{temp}\n")
    t.uptime(1009)
    run("--bar")
    shutil.rmtree(t.sys / "class/hwmon/hwmon3")
    t.hwmon(4, "zenpower", [("Tdie", -600, 90000)])
    (t.sys / "class/hwmon/hwmon5").mkdir()
    (t.sys / "class/hwmon/hwmon5/name").write_text("k10temp")
    t.uptime(1012)
    run("--bar")


def s_gpu_sensors(t, run):
    d = t.gpu("0000:03:00.0", "amdgpu", files=amd_files(42, 1024, "4096 ", discrete=True),
              hwmon=[("edge", 48000, 100000), ("junction", 61000, 110000), ("mem", 70000, None),
                     ("other", 1, 2), (None, -1500, None), ("edge", 1000, 0), ("x", 2000, -5000)]
              + [("x", 1000 * n, None) for n in range(8, 12)])
    sensors = d / "hwmon/hwmon0"
    (sensors / "temp99_label").write_text("edge\n")
    (sensors / "tempX_input").write_text("5000\n")
    (sensors / "temp_input").write_text("5000\n")
    (sensors / "temp12_input").mkdir()
    (d / "hwmon/hwmon10").mkdir()
    (d / "hwmon/hwmon10/temp1_input").write_text("33000\n")
    (d / "hwmon/hwmon1-x").mkdir()
    (d / "hwmon/hwmon1-x/temp1_input").write_text("34000\n")
    (d / "hwmon/hwmon2").write_text("not a dir\n")
    t.gpu("0000:04:00.0", "amdgpu", files=amd_files("N/A", 1, 2, discrete=True))
    t.gpu("0000:05:00.0", "amdgpu", files=amd_files("007", "0012", "0100", discrete=True))
    t.gpu("0000:06:00.0", "amdgpu", files=amd_files("101", 3, 2, discrete=True))
    t.gpu("0000:07:00.0", "amdgpu", files=amd_files("1000", 1, "1234567890123456789", discrete=True))
    run("--bar")
    (d / "gpu_busy_percent").chmod(0)
    t.uptime(1003)
    run("--bar")
    for address in ("0000:04:00.0", "0000:05:00.0"):
        (t.sys / "bus/pci/drivers/amdgpu" / address).unlink()
    t.uptime(1006)
    run("--bar")


def s_gpu_types_and_apu_memory(t, run):
    tahiti = amd_files(3, 1024, 8 << 30)
    tahiti.update({"mem_info_vis_vram_total": f"{256 << 20}\n", "mem_info_gtt_used": "100\n",
                   "mem_info_gtt_total": f"{60 << 30}\n"})
    apu = amd_files(7, 512 << 20, 1 << 29)
    apu.update({"mem_info_vis_vram_total": f"{1 << 29}\n", "mem_info_gtt_used": f"{3 << 30}\n",
                "mem_info_gtt_total": f"{15 << 30}\n"})
    broken = amd_files(7, 491, 512)
    broken.update({"mem_info_gtt_used": "900\n", "mem_info_gtt_total": "800\n"})
    t.gpu("0000:02:00.0", "amdgpu", files=tahiti)
    t.gpu("0000:c1:00.0", "amdgpu", class_="0x038000", files=apu)
    t.gpu("0001:c1:00.0", "amdgpu", class_="0x038000", files=broken)
    t.gpu("0000:03:00.0", "radeon", files=amd_files(5, 1, 2))
    t.gpu("0000:08:00.0", "radeon", files=amd_files(5, 1, 2, discrete=True))
    t.gpu("0000:09:00.0", "amdgpu", class_="0x040300", files=amd_files(1, 1, 2))
    t.gpu("0000:0a:00.0", "ast", vendor="0x1a03")
    t.gpu("0000:0b:00.0", "vfio-pci", vendor="0x10de")
    t.gpu("0000:0c:00.0", None)
    t.gpu("0000:0d:00.0", "nouveau", vendor="0x10de", hwmon=[(None, 45000, None)])
    (t.sys / "bus/pci/drivers/amdgpu/0000:0E:00.0").mkdir()
    (t.sys / "bus/pci/drivers/amdgpu/0000:0E:00.0/class").write_text("0x030000\n")
    (t.sys / "bus/pci/drivers/amdgpu/.0000:0f:00.0").mkdir()
    (t.sys / "bus/pci/drivers/amdgpu/0000:10:00.0").symlink_to(t.base / "nowhere")
    for name in ("bind", "unbind", "new_id", "uevent", "module"):
        (t.sys / "bus/pci/drivers/amdgpu" / name).touch()
    run()
    # The type is kept for the boot, then read again.
    (t.device("0000:02:00.0") / "mem_info_vis_vram_total").unlink()
    t.advance(1, 3)
    run()
    (t.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
    t.advance(2, 6)
    run()


def s_names(t, run):
    ids = t.base / "pci.ids"
    ids.write_text(PCI_IDS)
    for index, device in enumerate(("0x7590", "0x7591", "0x7592", "0x7593", "0x7594", "0x7595", "0x7596",
                                    "0x759", "7590", "0X7590", "0x75AF")):
        t.gpu(f"0000:{index + 1:02x}:00.0", "amdgpu", device=device, files=amd_files(1, 1, 2, discrete=True))
    (t.device("0000:01:00.0") / "vendor").write_text("0x1002")
    t.gpu("0000:20:00.0", "i915", vendor="0x8086", device="0x7d55")
    t.gpu("0000:21:00.0", "nouveau", vendor="0x10DE", device="0x2786")
    run("--bar")
    ids.write_text(PCI_IDS.replace("Navi 44 [Radeon RX 9050 / 9060 XT]", "Changed [Other]")
                   .replace("\t7591", "\t7596  Now Known\n\t7591"))
    t.advance(1, 3)
    run("--bar")
    t.gpu("0000:30:00.0", "amdgpu", device="0x7596", files=amd_files(1, 1, 2, discrete=True))
    t.advance(2, 6)
    run("--bar")
    (t.state / "gpu-names").write_text("boot-1\n0000:01:00.0\tCached \"one\"\x01\n0000:02:00.0\t\x01\x02\nbad\tx\n"
                                       "0000:03:00.0\n0000:04:00.0\t\tTabbed\t\n0000:99:00.0\tGone\n0000:05:00.0\tCut")
    t.advance(3, 9)
    run("--bar")
    (t.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
    ids.unlink()
    t.advance(4, 12)
    run("--bar")


def s_nvidia(t, run):
    log, reply = t.nvidia(control="on")
    xml = t.base / "nvidia-smi.xml"
    replies = ["00000000:01:00.0, 37, 1178, 8192, 51\n",
               "00000000:01:00.0, 037, 0001178, 8192, 051\n",
               "00000000:01:00.0,101,9000,8192,51\r\n",
               "0000:01:00.0 , 5 , 10 , 8388608 , 40 , extra\n",
               "00000000:01:00.0, 5, 10, 20\n",
               "zz:01:00.0, 1, 2, 3, 4\n00000000:01:00.0, 9, 10, 20, 30",
               "00000000:01:00.0, [N/A], [Not Supported], 20, \n",
               "00000000:01:00.0, 1, 2, 3, 12345\n00000000:01:00.0, 9, 9, 9, 9\n",
               "00000000:01:00.0, 1, 2, 3, 4\n"]
    xmls = ['<gpu id="00000000:01:00.0"><gpu_temp_max_threshold> 098  C </gpu_temp_max_threshold></gpu>',
            '<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>0 C</gpu_temp_max_threshold></gpu>',
            '<gpu id="00000001:01:00.0"><gpu_temp_max_threshold>70 C</gpu_temp_max_threshold></gpu>',
            '<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>98C<x</gpu><gpu_temp_max_threshold>99 C</gpu_temp_max_threshold>',
            '<gpu id="0000:01:00.0"><gpu_temp_max_threshold>98 F</gpu_temp_max_threshold><gpu_temp_max_threshold>97 C</gpu_temp_max_threshold></gpu>',
            '<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>1234567 C</gpu_temp_max_threshold></gpu>'
            '<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>91 C</gpu_temp_max_threshold></gpu>',
            '<gpu id="00000000:01:00.0'] 
    for step, text in enumerate(replies):
        reply.write_text(text)
        xml.write_text(xmls[step % len(xmls)])
        (t.proc / "sys/kernel/random/boot_id").write_text(f"boot-{step}\n")
        t.advance(step, 3 * step)
        run()
    (t.bin / "nvidia-smi").unlink()
    t.advance(20, 100)
    run()


def s_nvidia_limits_back_off(t, run):
    log, _ = t.nvidia(control="on")
    failtemp = t.base / "nvidia-smi.failtemp"
    failtemp.write_text("")
    for step, seconds in enumerate((0, 10, 31, 40, 92, 95, 213, 214, 460, 1000, 2000, 4000)):
        if step == 8:
            failtemp.unlink()
        t.advance(step, seconds)
        run()


def s_nvidia_pair_narrowing(t, run):
    log, reply = t.nvidia(addresses=("0000:01:00.0", "0000:02:00.0", "0001:03:00.0"), control="on")
    xml = t.base / "nvidia-smi.xml"
    (t.device("0000:02:00.0") / "power/control").write_text("auto\n")
    xml.write_text('<nvidia_smi_log><gpu id="00000000:01:00.0"><temperature><gpu_temp_max_threshold>98 C'
                   '</gpu_temp_max_threshold></temperature></gpu><gpu id="00000001:03:00.0">'
                   '<gpu_temp_max_threshold>N/A</gpu_temp_max_threshold></gpu></nvidia_smi_log>\n')
    run()
    t.advance(1, 5)
    run("--bar")
    xml.unlink()
    t.advance(2, 31)
    run()
    reply.write_text("00000000:01:00.0, 37, 1178, 8192, 51\n00000000:01:00.0, 1, 1, 1, 1\n")
    t.advance(3, 40)
    run()
    (t.device("0001:03:00.0") / "power/runtime_status").write_text("suspended\n")
    reply.write_text("".join(f"{nv_bus(a)}, 20, 1, 2, 30\n" for a in ("0000:01:00.0", "0000:02:00.0", "0001:03:00.0")))
    t.advance(4, 80)
    run()
    t.advance(5, 85)
    run("--bar")
    (t.base / "nvidia-smi.failtemp").write_text("")
    (t.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
    t.advance(6, 200)
    run("--bar")


def s_gpu_state_file(t, run):
    t.nvidia(control="auto")
    t.gpu("0000:05:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2, discrete=True))
    run("--bar")
    good = (t.state / "gpus.1").read_text()
    lines = good.splitlines()
    for step, text in enumerate((
            "garbage\n",
            good[:-12],
            good.replace("boot\tboot-1", "boot\tboot-0"),
            lines[0] + "\n" + lines[1].replace("\t-1\t", "\t0123\t").replace("\t", "\t\t") + "\n",
            lines[0] + "\n" + "\t".join(lines[1].split("\t")[:6]) + '\t"busy":1,"error":""\n',
            lines[0] + "\n" + lines[1].replace("discrete", "weird") + "\n" + lines[2].replace("\t0\t", "\t8\t") + "\n",
            lines[0] + "\n" + lines[1] + "\n" + lines[1].replace('"busy":37', '"busy":99') + "\n",
            lines[0] + "\n" + lines[2].replace('"error":""', '"error":"x"'),
            "boot\tboot-1\n0000:01:00.0\tdiscrete\t1000000\tunknown\t0\t0\t\"busy\":5,\"error\":\"\"\t\n",
            "boot\tboot-1\n0000:01:00.0\tnull\t1018000\t1234567\t0\t0\t\"busy\":6,\"error\":\"\"\n",
            "",
    ), 1):
        for generation in (0, 1):
            (t.state / f"gpus.{generation}").write_text(text)
        t.advance(step, 3 * step)
        run("--bar")
    (t.state / "current").write_text("0 0 0 0 -1 none 1 extra\n")
    t.advance(20, 60)
    run("--bar")
    (t.state / "current").write_text("0 0 0 0 -1 none 7\n")
    t.advance(21, 63)
    run("--bar")


def s_spacing_and_displays(t, run):
    t.gpu("0000:c1:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2))
    t.gpu("0000:05:00.0", "amdgpu", control="auto", files=amd_files(20, 1, 2, discrete=True))
    for card, (address, state) in enumerate((("0000:03:00.0", "connected"), ("0000:04:00.0", "disconnected")), 1):
        files = amd_files(30, 1, 2, discrete=True)
        files.update({f"drm/card{card}/card{card}-DP-1/status": f"{state}\n",
                      f"drm/card{card}/card{card}-HDMI-A-1/status": "disconnected\n",
                      f"drm/card{card}/cardDP/status": "connected\n"})
        t.gpu(address, "amdgpu", control="auto", files=files)
    log, _ = t.nvidia(control="auto")
    connector = t.device("0000:01:00.0") / "drm/card9/card9-HDMI-A-1/status"
    connector.parent.mkdir(parents=True)
    connector.write_text("connected\n")
    t.gpu("0000:06:00.0", "amdgpu", control=" auto ", files=amd_files(40, 1, 2, discrete=True))
    run("--bar")
    for step, seconds in enumerate((5, 10, 29, 31, 35, 36, 62), 1):
        for address in ("0000:c1:00.0", "0000:05:00.0", "0000:03:00.0", "0000:04:00.0", "0000:06:00.0"):
            (t.device(address) / "gpu_busy_percent").write_text(f"{step}\n")
        t.advance(step, seconds)
        run() if step == 5 else run("--bar")
    t.uptime(900)
    run("--bar")


def s_suspend_and_wake(t, run):
    files = amd_files(10, 1, 2, discrete=True)
    d = t.gpu("0000:05:00.0", "amdgpu", control="auto", files=files, hwmon=[("edge", 50000, None)])
    log, _ = t.nvidia(addresses=("0000:01:00.0", "0000:02:00.0"), control="on")
    (t.device("0000:02:00.0") / "power/runtime_status").write_text("suspended\n")
    t.intel(status="suspended", control="auto")
    t.descriptor(41, 3, FIFO)
    run("--bar")
    (d / "gpu_busy_percent").write_text("50\n")
    t.advance(1, 5)
    run("--bar")
    (d / "power/runtime_status").write_text("suspended\n")
    for name in ("gpu_busy_percent", "hwmon/hwmon0/temp1_input"):
        (d / name).unlink()
        os.mkfifo(d / name)
    t.advance(2, 10)
    run("--bar")
    run()
    (d / "power/runtime_status").write_text("active\n")
    for name, value in (("gpu_busy_percent", "70\n"), ("hwmon/hwmon0/temp1_input", "60000\n")):
        (d / name).unlink()
        (d / name).write_text(value)
    (t.device("0000:02:00.0") / "power/runtime_status").write_text("active\n")
    t.advance(3, 15)
    run("--bar")


def s_power_control_read_only_when_used(t, run):
    t.gpu("0000:05:00.0", "amdgpu", files=amd_files(10, 1, 2, discrete=True))
    t.gpu("0000:c1:00.0", "amdgpu", files=amd_files(10, 1, 2))
    t.nvidia(control="on")
    t.intel(control="on")
    for address in ("0000:05:00.0", "0000:c1:00.0", "0000:01:00.0", "0000:00:02.0"):
        (t.device(address) / "power/control").unlink()
        os.mkfifo(t.device(address) / "power/control")
    run()


def s_intel(t, run):
    card, device = t.intel()
    t.descriptor(41, 3, intel_fdinfo(render=5))
    t.descriptor(41, 4, intel_fdinfo(2, render=7).replace("render", "rendér"))
    t.descriptor(42, 3, intel_fdinfo(3).replace("\n", "\r\n"))
    t.descriptor(42, 4, "drm-driver: i915\ndrm-driver: xe\n drm-pdev: x\n")
    t.descriptor(43, 5, None, target="socket:[1]")
    t.descriptor(43, 6, FIFO, target="/dev/dri/renderD200")
    t.descriptor(43, 7, FIFO, target="/dev/dri/card1x")
    (card / "gt/gt0").mkdir(parents=True)
    (card / "gt/gt0/rps_act_freq_mhz").write_text("  800 \n")
    (card / "gt/gt1").mkdir(parents=True)
    (card / "gt/gt1/rps_act_freq_mhz").write_text("abc\n")
    (card / "gt/gt2").mkdir(parents=True)
    (card / "gt/gt2/rps_act_freq_mhz").mkdir()
    sensor = device / "hwmon/hwmon3"
    sensor.mkdir(parents=True)
    for number, (value, crit, label) in enumerate(((45123, "95000", "GPU \"core\"\t°\U0001F525"), (1, None, None),
                                                    (0, "0", "\x7f"), (12345678901234567890, "1", "   pkg\x1c "),
                                                    (2000, None, None), (3000, "x", "b")), 1):
        (sensor / f"temp{number}_input").write_text(f"{value}\n")
        if crit is not None:
            (sensor / f"temp{number}_crit").write_text(crit)
        if label is not None:
            (sensor / f"temp{number}_label").write_text(label)
    (sensor / "temp5_crit").mkdir()
    (sensor / "temp5_label").mkdir()
    (sensor / "temp7_input").mkdir()
    run("--bar")
    t.intel_state(1_000_000_000, {"1": {"engines": {"render": 0, "video": 0}, "capacities": {"video": 2}, "private": 1}})
    t.descriptor(41, 3, intel_fdinfo(render=50_000_000_000))
    t.advance(1, 3)
    run()
    (t.state / "intel-0000:00:02.0.json").write_bytes(b'{"device":"\xff"}')
    t.advance(2, 6)
    run("--bar")
    (t.state / "intel-0000:00:02.0.json").write_text("[]")
    (t.proc / "41/fdinfo/3").write_bytes(b"drm-driver: i915\n\xff\n")
    t.advance(3, 9)
    run("--bar")
    (t.proc / "41/fdinfo/3").write_text(intel_fdinfo(render=1).replace("1024 KiB", "1 KiB"))
    (sensor / "temp2_input").write_text("x\n")
    t.advance(4, 12)
    run("--bar")
    (card / "device").unlink()
    t.advance(5, 15)
    run("--bar")
    (t.state / "intel-0000:00:02.0.json").unlink()
    (t.state / "intel-0000:00:02.0.json").mkdir()
    t.advance(6, 18)
    run("--bar")
    (t.proc / "sys/kernel/random/boot_id").unlink()
    t.advance(7, 21)
    run("--bar")


def s_intel_crash(t, run):
    card, _ = t.intel()
    t.intel(driver="xe", device="0000:03:00.0", card="card2")
    t.descriptor(41, 3, intel_fdinfo())
    (card / "gt/gt0").mkdir(parents=True)
    (card / "gt/gt0/rps_act_freq_mhz").write_bytes(b"\xff\n")
    run("--bar")
    (card / "gt/gt0/rps_act_freq_mhz").write_text("800\n")
    t.advance(1, 3)
    run("--bar")


def s_intel_cards(t, run):
    _, dgpu = t.intel(driver="xe", device="0001:03:00.0", card="card3", control="auto")
    t.intel(driver="i915", device="0000:00:02.0", card="card10")
    (dgpu / "drm/card03").mkdir()
    (dgpu / "drm/card03/gt/gt0").mkdir(parents=True)
    (dgpu / "drm/card03/gt/gt0/rps_act_freq_mhz").write_text("300\n")
    (dgpu / "drm/card3/gt/gt0").mkdir(parents=True)
    (dgpu / "drm/card3/gt/gt0/rps_act_freq_mhz").write_text("900\n")
    (dgpu / "drm/card20").mkdir()
    (dgpu / "drm/cardX").mkdir()
    (dgpu / "drm/card3-DP-1").mkdir()
    (t.sys / "class/drm/card10-eDP-1").mkdir()
    for name in ("bind", "unbind", "uevent"):
        (t.sys / "bus/pci/drivers/xe" / name).touch()
    t.descriptor(41, 3, intel_fdinfo(1, device="0001:03:00.0"), target="/dev/dri/renderD131")
    t.descriptor(41, 4, intel_fdinfo(1), target="/dev/dri/renderD138")
    t.descriptor(41, 5, intel_fdinfo(2, device="0001:03:00.0"), target="/dev/dri/renderD138")
    t.descriptor(42, 3, intel_fdinfo(1), target="/dev/dri/card10")
    run("--bar")
    (t.proc / "sys/kernel/random/boot_id").write_text("  spaced boot \n")
    t.advance(1, 3)
    run()
    t.advance(2, 6)
    run("--bar")
    t.intel_state(45_000_000_000, {"1": {"engines": {"render": 0, "video": 0}, "capacities": {"video": 2}, "private": 1}},
                  device="0001:03:00.0", boot="spaced boot")
    t.intel_state(45_000_000_000, {"1": {"engines": {"render": 0, "video": 0}, "capacities": {"video": 2}, "private": 1}},
                  boot="spaced boot")
    t.descriptor(41, 3, intel_fdinfo(1, render=90_000_000_000, device="0001:03:00.0"), target="/dev/dri/renderD131")
    t.advance(3, 40)
    run()


def s_mixed_vendors(t, run):
    (t.base / "pci.ids").write_text(PCI_IDS)
    t.intel()
    t.descriptor(41, 3, intel_fdinfo())
    t.nvidia(control="on")
    t.gpu("0000:05:00.0", "amdgpu", device="0x7590", files=amd_files(33, 1, 2, discrete=True),
          hwmon=[("edge", 50000, None)])
    t.gpu("0000:06:00.0", "amdgpu", device="0x7590", files=amd_files(34, 1, 2, discrete=True))
    t.gpu("0000:07:00.0", "nouveau", vendor="0x10de", device="0x2786", hwmon=[(None, 45000, None)])
    run()
    t.advance(1, 3)
    run("--bar")


def s_state_dir_fallbacks(t, run):
    t.process(300, "p", 5, 1)
    private = t.base / "private"
    private.mkdir(mode=0o700)
    (private / f"foamy-vitals-{os.geteuid()}").mkdir(mode=0o700)
    env = dict(t.env(), XDG_RUNTIME_DIR=str(private))
    t.runtime.rmdir()
    t.runtime.symlink_to(private)
    run()
    t.advance(1, 3)
    run()
    # Inside a private runtime directory the mode of the state directory is not checked.
    os.chmod(private / f"foamy-vitals-{os.geteuid()}", 0o755)
    t.advance(2, 6)
    run(env=env)


def s_fractional_rankings(t, run):
    # 3 CPUs, 300 jiffies per sample: one decimal of 100 * 3 * used / 300.
    def stat(tick):
        (t.proc / "stat").write_text(f"cpu  {100 * tick} 0 0 {200 * tick}\ncpu0 1 0 0 1\ncpu1 1 0 0 1\ncpu2 1 0 0 1\n")
    stat(1)
    for pid in range(400, 408):
        t.process(pid, f"f{pid}", 0, 3 * pid)
    run()
    stat(2)
    t.uptime(1002)
    for pid, used in zip(range(400, 408), (1, 2, 3, 7, 0.05, 0.15, 1.25, 0)):
        t.process(pid, f"f{pid}", used, 3 * pid)
    run()
    stat(3)
    t.uptime(1004)
    t.process(408, "fresh", 33, 1)
    run()
    t.uptime(1006)
    (t.state / "procs.0").write_text("#TOTAL abc\n400 0\n")
    (t.state / "procs.1").write_text("#TOTAL abc\n400 0\n")
    run()
    t.uptime(1008)
    (t.state / "procs.0").write_text("")
    (t.state / "procs.1").write_text("\n")
    run()


def s_odd_numbers_in_files(t, run):
    t.hwmon(0, "coretemp", [("Package id 0", "045000", "0777")])
    d = t.gpu("0000:03:00.0", "amdgpu", files=amd_files("007", "", "4096", discrete=True),
              hwmon=[("edge", " 047000 ", "0100000"), ("junction", "061000", "-0"), ("mem", "-01", "1e3")])
    (d / "mem_info_vram_total").write_bytes(b"4096")
    (d / "hwmon/hwmon0/temp1_label").write_bytes(b"edge")
    for text in ("0012.5 1\n", "1012.987\n", "1013.9999\n"):
        (t.proc / "uptime").write_text(text)
        run("--bar")
    (t.state / "current").write_text("0 0 0123 1 0777 full 1\n")
    (t.proc / "uptime").write_text("1020\n")
    run("--bar")


def s_intel_engine_order(t, run):
    t.intel(driver="xe")
    t.descriptor(41, 3, "drm-driver: xe\ndrm-pdev: 0000:00:02.0\ndrm-client-id: 7\n"
                        "drm-engine-video: 5 ns\ndrm-engine-render: 1 ns\ndrm-engine-video: 50000000000 ns\n"
                        "drm-total-vram0: 2 MiB\ndrm-shared-vram0: 1 MiB\ndrm-total-system0: 10\ndrm-shared-system0: 3\n")
    t.descriptor(42, 3, "drm-driver: xe\ndrm-pdev: 0000:00:02.0\ndrm-client-id: 8\n"
                        "drm-engine-copy: 9 ns\ndrm-engine-render: 0 ns\ndrm-engine-capacity-copy: 4\n")
    t.descriptor(43, 3, "drm-driver: xe\ndrm-pdev: 0000:00:02.0\ndrm-client-id: 7\ndrm-engine-x: 1 ns\n")
    run("--bar")
    t.intel_state(500_000_000, {"7": {"engines": {"video": 0, "render": 0, "z": 1}, "capacities": {}, "private": None},
                                "8": {"engines": {"copy": 10, "render": 0}, "capacities": {"copy": 4}, "private": 5}})
    t.advance(1, 3)
    run()


SCENARIOS = {name[2:]: value for name, value in sorted(globals().items()) if name.startswith("s_")}


@unittest.skipUnless(REFERENCE, "set FOAMY_VITALS_REFERENCE to the reference helper's argv prefix")
class CompareTest(unittest.TestCase):
    maxDiff = None

    def record(self, helper, scenario):
        base = Path(tempfile.mkdtemp(prefix="foamy-vitals-compare-"))
        try:
            tree = Tree(base, helper=helper)
            recorder = Recorder(tree)
            scenario(tree, recorder)
            return recorder.steps
        finally:
            for path in base.rglob("*"):
                if not path.is_symlink():
                    try:
                        path.chmod(0o700)
                    except OSError:
                        pass
            shutil.rmtree(base, ignore_errors=True)


def make_test(name, scenario):
    def test(self):
        expected = self.record(REFERENCE, scenario)
        actual = self.record(HELPER, scenario)
        self.assertEqual(len(actual), len(expected))
        for index, (want, got) in enumerate(zip(expected, actual)):
            with self.subTest(step=index, args=want["args"]):
                self.assertEqual(got["stdout"], want["stdout"])
                self.assertEqual(got, want)
    return test


for _name, _scenario in SCENARIOS.items():
    setattr(CompareTest, f"test_{_name}", make_test(_name, _scenario))


if __name__ == "__main__":
    unittest.main()
