"""Byte-for-byte comparison of two stats helpers on the same fake trees.

  FOAMY_VITALS_REFERENCE='["/path/to/old/stats.sh"]' python3 -I tests/compare_stats.py [-v]

Runs every scenario below twice, once with the reference helper (an argv
prefix as a JSON list, e.g. stats.sh from an older checkout) and once with
FOAMY_HELPER (default: this checkout's helper), each on a fresh fake tree, and
requires identical stdout bytes, exit codes, state files (cpu.*, procs.*,
cache.*, current, nvidia; intel.json with its clock field left out) and
nvidia-smi calls after every step. Without FOAMY_VITALS_REFERENCE it skips.
Engine percentages depend on the real clock, so the Intel scenarios keep to
readings that don't (no baseline, a capped engine, errors).
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakesys import HELPER, Tree, helper_prefix, intel_fdinfo  # noqa: E402

REFERENCE = helper_prefix("FOAMY_VITALS_REFERENCE") if os.environ.get("FOAMY_VITALS_REFERENCE") else None
STATE_FILES = ("current", "cpu.0", "cpu.1", "procs.0", "procs.1", "cache.0", "cache.1", "nvidia")
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
        intel = self.tree.state / "intel.json"
        if intel.exists():
            try:
                snapshot = json.loads(intel.read_bytes())
                if isinstance(snapshot, dict):
                    snapshot.pop("time", None)
                state["intel.json"] = snapshot
            except ValueError:
                state["intel.json"] = intel.read_bytes()
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
    d = t.hwmon(1, "amdgpu", [("edge", 48000, 100000), ("junction", 61000, 110000), ("mem", 70000, None),
                              ("other", 1, 2), (None, -1500, None)] + [("x", 1000 * n, None) for n in range(6, 12)])
    (d / "temp99_label").write_text("edge\n")
    t.hwmon(0, "i915", [("", 1, 1)])
    cards = t.sys / "class/drm"
    for card, busy in (("card1", "42\n"), ("card0", "N/A\n"), ("card10", "7\n")):
        device = cards / card / "device"
        device.mkdir(parents=True)
        (device / "gpu_busy_percent").write_text(busy)
        (device / "mem_info_vram_used").write_text("1024\n")
        (device / "mem_info_vram_total").write_text("4096 \n")
    run("--bar")
    (cards / "card0/device/gpu_busy_percent").chmod(0)
    t.uptime(1003)
    run("--bar")
    for card in ("card0", "card1", "card10"):
        shutil.rmtree(cards / card)
    t.uptime(1006)
    run("--bar")


def s_nvidia(t, run):
    log, reply = t.nvidia(status="active", control="on")
    xml = t.base / "nvidia-smi.xml"
    replies = ["00000000:01:00.0, 37, 1178, 8192, 51\n",
               "00000000:01:00.0, 037, 0001178, 8192, 051\n",
               "00000000:01:00.0,101,9000,8192,51\r\n",
               "0000:01:00.0 , 5 , 10 , 8388608 , 40 , extra\n",
               "00000000:01:00.0, 5, 10, 20\n",
               "zz:01:00.0, 1, 2, 3, 4\n00000000:01:00.0, 9, 10, 20, 30",
               "00000000:01:00.0, [N/A], [Not Supported], 20, \n",
               "00000000:01:00.0, 1, 2, 3, 4\n"]
    xmls = ['<gpu id="00000000:01:00.0"><gpu_temp_max_threshold> 098  C </gpu_temp_max_threshold></gpu>',
            '<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>0 C</gpu_temp_max_threshold></gpu>',
            '<gpu id="00000001:01:00.0"><gpu_temp_max_threshold>70 C</gpu_temp_max_threshold></gpu>',
            '<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>98C<x</gpu><gpu_temp_max_threshold>99 C</gpu_temp_max_threshold>',
            '<gpu id="0000:01:00.0"><gpu_temp_max_threshold>98 F</gpu_temp_max_threshold><gpu_temp_max_threshold>97 C</gpu_temp_max_threshold></gpu>']
    for step, text in enumerate(replies):
        reply.write_text(text)
        xml.write_text(xmls[step % len(xmls)])
        (t.proc / "sys/kernel/random/boot_id").write_text(f"boot-{step}\n")
        t.advance(step, 3 * step)
        run()
    (t.bin / "nvidia-smi").unlink()
    t.advance(20, 100)
    run()


def s_nvidia_state_file(t, run):
    log, reply = t.nvidia(status="active", control="auto")
    run("--bar")
    for step, text in enumerate(("garbage\n", "1000000 50 1 2 3 98 boot-1 0 0\n", "1001000 na na na na unknown boot-1 99999999 7\n",
                                 "", "1004000 1 2 3 4\n", "x 1 2 3 4 5 6 7 8 9 10\n"), 1):
        (t.state / "nvidia").write_text(text)
        t.advance(step, 3 * step)
        run("--bar")
    t.advance(9, 40)
    run()


def s_nvidia_suspended_and_sysfs_first(t, run):
    t.nvidia(status="suspended")
    run()
    run("--bar")
    gpus = t.proc / "driver/nvidia/gpus"
    (gpus / "0000:00:00.0").write_text("not a dir")
    t.advance(1, 3)
    run()
    device = t.sys / "class/drm/card0/device"
    device.mkdir(parents=True)
    (device / "gpu_busy_percent").write_text("bad\n")
    (device / "mem_info_vram_used").write_text("5\n")
    (device / "mem_info_vram_total").write_text("6\n")
    t.advance(2, 6)
    run()
    power = t.sys / "bus/pci/devices/0000:01:00.0/power"
    (power / "runtime_status").write_text("active\n")
    t.advance(3, 9)
    run()
    (device / "gpu_busy_percent").write_text("12\n")
    t.advance(4, 12)
    run()


def s_intel(t, run):
    card, device = t.intel()
    t.descriptor(41, 3, intel_fdinfo(render=5))
    t.descriptor(41, 4, intel_fdinfo(2, render=7).replace("render", "rend\u00e9r"))
    t.descriptor(42, 3, intel_fdinfo(3).replace("\n", "\r\n"))
    t.descriptor(42, 4, "drm-driver: i915\ndrm-driver: xe\n drm-pdev: x\n")
    t.descriptor(43, 5, None, target="socket:[1]")
    (card / "gt/gt0").mkdir(parents=True)
    (card / "gt/gt0/rps_act_freq_mhz").write_text("  800 \n")
    (card / "gt/gt1").mkdir(parents=True)
    (card / "gt/gt1/rps_act_freq_mhz").write_text("abc\n")
    sensor = device / "hwmon/hwmon3"
    sensor.mkdir(parents=True)
    for number, (value, crit, label) in enumerate(((45123, "95000", "GPU \"core\"\t\u00b0\U0001F525"), (1, None, None),
                                                    (0, "0", "\x7f"), (12345678901234567890, "1", " \u2028 pkg\x1c ")), 1):
        (sensor / f"temp{number}_input").write_text(f"{value}\n")
        if crit is not None:
            (sensor / f"temp{number}_crit").write_text(crit)
        if label is not None:
            (sensor / f"temp{number}_label").write_text(label)
    t.hwmon(0, "i915", [("", 77000, 99000)])
    run("--bar")
    t.intel_state(1_000_000_000, {"1": {"engines": {"render": 0, "video": 0}, "capacities": {"video": 2}, "private": 1}})
    t.descriptor(41, 3, intel_fdinfo(render=50_000_000_000))
    t.advance(1, 3)
    run()
    (t.state / "intel.json").write_bytes(b'{"device":"\xff"}')
    t.advance(2, 6)
    run("--bar")
    (t.state / "intel.json").write_text("[]")
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


def s_intel_cards(t, run):
    t.intel(driver="xe", device="0001:03:00.0", card="card3")
    t.intel(driver="i915", device="0000:00:02.0", card="card10")
    (t.sys / "class/drm/card10-eDP-1").mkdir()
    (t.sys / "class/drm/cardX").mkdir()
    (t.sys / "class/drm/card7").mkdir()
    for name in ("bind", "unbind", "uevent"):
        (t.sys / "bus/pci/drivers/xe" / name).touch()
    run("--bar")
    (t.proc / "sys/kernel/random/boot_id").write_text("  spaced boot \n")
    t.advance(1, 3)
    run()


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
