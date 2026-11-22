"""The stats helper against fake procfs/sysfs trees (FOAMY_VITALS_PROC/SYS) and the real system.

  python3 -I tests/test_stats.py

Every sample must stay valid JSON whatever the processes on the machine are
called: one control character in a process name used to make the panel's
JSON.parse fail, which blanked every bar reading ("Telemetry unavailable").

The helper runs as a process; FOAMY_HELPER is its argv prefix as a JSON list
(see tests/fakesys.py for the default), e.g. the aarch64 build through qemu:
  FOAMY_HELPER='["bin/aarch64/foamy-vitals-helper", "stats"]' python3 -I tests/test_stats.py
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakesys import FIFO, HELPER, NVIDIA_ADDRESS, Tree, amd_files, intel_fdinfo, isolated_environ, strict  # noqa: E402

PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
EVIL = 'evil\x01na"me\\\x1b[31m'
GPU_KEYS = [
    "id",
    "vendor",
    "driver",
    "name",
    "type",
    "asleep",
    "busy",
    "vramUsed",
    "vramTotal",
    "memoryPrivate",
    "frequencyMHz",
    "thermals",
    "error",
]
NVIDIA_ERROR = "NVIDIA readings unavailable. Check nvidia-smi."
INTEL_ERROR = "Intel GPU readings unavailable. Retrying…"
GIB = 1073741824
SECOND = 1_000_000_000


# Runs the helper ("$@" after two arguments) with an empty /tmp of its own,
# so the /tmp fallback is never the real one. $1 "planted" first makes that
# fallback a symlink to $2. Prints the sample, then whether the fallback held
# the state.
PRIVATE_TMP = """\
mount -t tmpfs tmpfs /tmp || exit 77
fallback=/tmp/foamy-vitals-$(id -u)
if [ "$1" = planted ]; then ln -s "$2" "$fallback" || exit 78; fi
shift 2
"$@" || exit
if [ -d "$fallback" ] && [ ! -L "$fallback" ] && [ -s "$fallback/current" ]; then
  echo "fallback: used"
else
  echo "fallback: unused"
fi
"""


def by_id(sample):
    return {gpu["id"]: gpu for gpu in sample["gpus"]}


def only(sample):
    assert len(sample["gpus"]) == 1, sample["gpus"]
    return sample["gpus"][0]


def temperature(gpu):
    return gpu["thermals"][0]["temperature"] if gpu["thermals"] else None


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="foamy-vitals-stats-"))
        # Registered first, so a setUp that fails (a full disk) leaves no tree behind.
        self.addCleanup(shutil.rmtree, self.base, True)
        self.tree = Tree(self.base)

    def advance(self, tick, seconds):
        self.tree.advance(tick, seconds)


class FakeTreeTest(TreeCase):
    def test_hostile_names_stay_valid_json(self):
        tree = self.tree
        # pid: (name, jiffies in the second sample, resident pages, cwd, kernel thread)
        specs = {
            101: (EVIL, 80, 300000, '/home/user/pro\x02ject"x', False),
            102: ("tab\there", 20, 1000, "/home/user/second", False),
            103: ("kworker/0:1", 999, 0, None, True),
            104: ("\x7f\x01\x02", 5, 10, None, False),
            105: ("bytes\udcff\udcfe", 1, 20, None, False),
        }
        for pid, (name, _, rss, cwd, kernel) in specs.items():
            tree.process(pid, name, 0, rss, cwd=cwd, kernel=kernel)
        first, _ = tree.run()
        self.assertEqual(first["processes"], [])
        self.advance(1, 2)
        for pid, (name, jiffies, rss, _cwd, kernel) in specs.items():
            tree.process(pid, name, jiffies, rss, kernel=kernel)
        sample, _ = tree.run()
        names = [entry["name"] for entry in sample["processes"]]
        self.assertEqual(names[0], "evilname[31m")
        self.assertIn("tabhere", names)
        self.assertNotIn("kworker/0:1", names, "kernel threads are not ranked")
        self.assertIn("?", names, "a name made only of control characters becomes ?")
        self.assertEqual(sample["processes"][0]["where"], "projectx")
        self.assertEqual(sample["processes"][0]["pct"], 80.0, "80 of 100 jiffies on 2 CPUs = 0.8 * 2 * 100 / 2")
        memory = sample["memoryProcesses"]
        self.assertEqual([entry["rssKb"] for entry in memory][:2], [300000 * PAGE_KB, 1000 * PAGE_KB])
        self.assertEqual(memory[0]["name"], "evilname[31m")
        self.assertEqual(sample["network"], {"interface": "wlan0", "rxBytes": 12345678901, "txBytes": 98765})
        self.assertEqual(sample["cpu"], 50, "50 busy of 100 jiffies per CPU between the samples")
        self.assertEqual(sample["cores"], [50, 50])
        for text in json.dumps(sample, ensure_ascii=False):
            self.assertFalse(ord(text) < 0x20 or ord(text) == 0x7F)

    def test_rankings_keep_only_the_top_five(self):
        tree = self.tree
        for pid in range(200, 220):
            tree.process(pid, f"p{pid}", 0, pid)
        tree.run()
        self.advance(1, 2)
        for pid in range(200, 220):
            tree.process(pid, f"p{pid}", pid - 199, pid)
        sample, _ = tree.run()
        self.assertEqual([entry["name"] for entry in sample["processes"]], ["p219", "p218", "p217", "p216", "p215"])
        self.assertEqual(
            [entry["name"] for entry in sample["memoryProcesses"]], ["p219", "p218", "p217", "p216", "p215"]
        )

    def test_process_names_and_directories_as_the_kernel_writes_them(self):
        # A name ends at the last ")" of the stat line ("(sd-pam)" runs in every session), and
        # "where" names only a project directory, never $HOME or a kernel directory.
        tree = self.tree
        home = str(self.base / "home")
        for pid, name, rss, cwd in (
            (401, "(sd-pam)", 500, "/"),
            (402, "Web Content", 400, home),
            (403, "a) (b", 300, home + "/project"),
            (404, "x", 200, "/proc/1"),
            (405, "no cwd", 100, None),
        ):
            tree.process(pid, name, 0, rss, cwd=cwd)
        result = tree.run_raw(env={**tree.env(), "HOME": home})
        self.assertEqual(result.returncode, 0, result.stderr)
        memory = strict(result.stdout.decode())["memoryProcesses"]
        self.assertEqual(
            [(entry["name"], entry["where"], entry["rssKb"]) for entry in memory],
            [
                ("(sd-pam)", "", 500 * PAGE_KB),
                ("Web Content", "", 400 * PAGE_KB),
                ("a) (b", "project", 300 * PAGE_KB),
                ("x", "", 200 * PAGE_KB),
                ("no cwd", "", 100 * PAGE_KB),
            ],
        )
        # Without HOME a full sample still answers (the shell script it replaced exited 1).
        self.advance(1, 3)
        env = tree.env()
        del env["HOME"]
        result = tree.run_raw(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(strict(result.stdout.decode())["memoryProcesses"]), 5)

    def test_bar_samples_skip_rankings_and_share_the_cache(self):
        tree = self.tree
        tree.process(300, "busy", 0, 50)
        full, _ = tree.run()
        self.assertEqual(full["mode"], "full")
        cached, _ = tree.run("--bar")
        self.assertEqual(cached["sampleTimeMs"], full["sampleTimeMs"], "a fresh full sample serves the bar")
        self.advance(1, 3)
        bar, _ = tree.run("--bar")
        self.assertEqual(bar["mode"], "bar")
        self.assertEqual(bar["processes"], [])
        self.assertEqual(bar["memoryProcesses"], [])
        self.assertEqual(bar["cpu"], 50)
        self.assertEqual(bar["cpuTemp"], None)
        full_again, _ = tree.run()
        self.assertEqual(full_again["mode"], "full", "a bar sample never answers a full request")
        self.assertEqual(full_again["sampleTimeMs"], bar["sampleTimeMs"])

    def test_the_cache_window_is_shorter_than_the_popup_poll(self):
        # The open popup asks every 2 s, but a tick can start late (a busy
        # event loop) and the next one on time: a full call 1.98 s after the
        # last one must get a new sample, not the previous one back.
        tree = self.tree
        tree.process(300, "busy", 0, 50)
        first, _ = tree.run()
        self.tree.uptime(1000.5)
        again, _ = tree.run()
        self.assertEqual(again["sampleTimeMs"], first["sampleTimeMs"], "a call 0.5 s later is served from the cache")
        self.advance(1, 1.98)
        tree.process(300, "busy", 40, 50)
        late, _ = tree.run()
        self.assertEqual(late["sampleTimeMs"], first["sampleTimeMs"] + 1980, "a call 1.98 s later is a new sample")
        self.assertEqual(late["processes"][0]["name"], "busy")

    def test_a_missing_table_blanks_only_its_own_readings(self):
        # A kernel built without IPv4 networking has no /proc/net/route, for
        # example: processor and memory must not go blank with the network.
        network = {"interface": "wlan0", "rxBytes": 12345678901, "txBytes": 98765}
        rows = (
            ("stat", (None, 4000000, 16000000, network)),
            ("meminfo", (50, 0, 0, network)),
            ("net/route", (50, 4000000, 16000000, None)),
            ("net/dev", (50, 4000000, 16000000, None)),
        )
        for table, expected in rows:
            with self.subTest(table):
                tree = Tree(self.base / table.replace("/", "-"))
                tree.run("--bar")
                tree.advance(1, 3)
                (tree.proc / table).unlink()
                sample, _ = tree.run("--bar")
                self.assertEqual(
                    (sample["cpu"], sample["memUsedKb"], sample["memTotalKb"], sample["network"]), expected
                )

    def test_first_rankings_after_a_bar_stretch_cover_a_short_window(self):
        # The popup opens after a stretch of bar-only samples: the CPU ranking must show
        # the last moment, not the whole stretch since the last full sample.
        tree = self.tree
        tree.process(300, "busy", 0, 50)
        tree.run()
        self.advance(1, 2)
        tree.process(300, "busy", 40, 50)
        sample, _ = tree.run()
        self.assertEqual([entry["name"] for entry in sample["processes"]], ["busy"], "a recent baseline ranks it")
        for step in range(2, 7):
            self.advance(step, 2 + 5 * (step - 1))
            tree.process(300, "busy", 20000 * (step - 1), 50)
            tree.run("--bar")
        self.advance(7, 32)
        sample, _ = tree.run()
        self.assertEqual(sample["processes"], [], "a baseline older than 15 s is replaced before ranking")
        self.assertEqual([entry["name"] for entry in sample["memoryProcesses"]], ["busy"])
        # Time went back (a new baseline is due): busy's new jiffies are not ranked either.
        tree.cpu(8)
        tree.process(300, "busy", 200000, 50)
        tree.uptime(900)
        sample, _ = tree.run()
        self.assertEqual(sample["processes"], [], "a baseline from the future is replaced before ranking")

    def test_unknown_arguments_get_a_json_usage_error(self):
        # tests/suite.sh runs the other architecture's helper only where this exit 2 comes back.
        tree = self.tree
        self.assertEqual(tree.helper[-1], "stats")
        program = tree.helper[:-1]
        for args in ([], ["--probe"], ["stats", "--probe"], ["stats", "--bar", "x"], ["--bar"]):
            with self.subTest(args=args):
                result = subprocess.run([*program, *args], env=tree.env(), capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stderr)
                lines = result.stdout.decode().splitlines()
                self.assertEqual(len(lines), 1, lines)
                reply = strict(lines[0])
                self.assertEqual((reply["ok"], reply["code"]), (False, "INVALID_ARGUMENT"))

    def test_cpu_temperature_comes_from_the_package_sensor(self):
        # cpuTempCrit sets the bar's warning colour, so a limit must belong to the sensor shown.
        def amd(tree):
            tree.hwmon(0, "nvme", [("Composite", 40000, 80000)])
            tree.hwmon(1, "k10temp", [("Tccd1", 70000, None), ("Tctl", 55400, 95000)])

        def intel(tree):
            return tree.hwmon(2, "coretemp", [("Core 0", 50000, 100000), ("Package id 0", 62600, 105000)])

        def intel_package_unreadable(tree):
            (intel(tree) / "temp2_input").write_text("abc\n")
            zone = tree.sys / "class/thermal/thermal_zone3"
            zone.mkdir(parents=True)
            (zone / "type").write_text("x86_pkg_temp\n")
            (zone / "temp").write_text("64500\n")

        def no_sensor(tree):
            pass

        rows = (
            ("k10temp Tctl, not the first label", amd, (55, 95)),
            ("coretemp Package, not Core 0", intel, (63, 105)),
            ("x86_pkg_temp zone, without coretemp's limit", intel_package_unreadable, (65, None)),
            ("no CPU sensor", no_sensor, (None, None)),
        )
        for name, build, expected in rows:
            with self.subTest(name):
                tree = Tree(self.base / build.__name__)
                build(tree)
                sample, _ = tree.run("--bar")
                self.assertEqual((sample["cpuTemp"], sample["cpuTempCrit"]), expected)
                # The keys VitalsService.parse and the readings read.
                self.assertEqual(
                    set(sample),
                    {
                        "mode",
                        "sampleTimeMs",
                        "cpu",
                        "cores",
                        "memUsedKb",
                        "memTotalKb",
                        "cpuTemp",
                        "cpuTempCrit",
                        "gpus",
                        "processes",
                        "memoryProcesses",
                        "network",
                    },
                )
        tree = Tree(self.base / "zenpower")
        tree.hwmon(4, "zenpower", [("Tdie", -600, 90000)])
        sample, _ = tree.run("--bar")
        self.assertIsInstance(sample["cpuTemp"], int, "a negative reading is a number, not a failed sample")
        self.assertEqual((sample["cpuTemp"], sample["cpuTempCrit"]), (-1, 90))

    def test_sensor_files_are_decimal_millidegrees_rounded_to_the_nearest_degree(self):
        # (file text, whole degrees): halves round up, below zero too, and a
        # leading zero is not octal (045000 once read as 19 °C).
        rows = (
            ("62600", 63),
            ("62500", 63),
            ("62499", 62),
            ("045000", 45),
            ("-600", -1),
            ("-1400", -1),
            ("-1500", -1),
            ("-1600", -2),
            ("-2000", -2),
            ("-045000", -45),
            ("-500", 0),
            ("0", 0),
        )
        for text, expected in rows:
            with self.subTest(text):
                tree = Tree(self.base / f"sensor{text}")
                tree.hwmon(1, "k10temp", [("Tctl", text, "075000")])
                files = amd_files(10, 1, 2, discrete=True)
                tree.gpu("0000:05:00.0", "amdgpu", files=files, hwmon=[("edge", text, "075000")])
                sample, _ = tree.run("--bar")
                self.assertEqual((sample["cpuTemp"], sample["cpuTempCrit"]), (expected, 75))
                self.assertEqual(only(sample)["thermals"], [{"temperature": expected, "critical": 75, "label": "Edge"}])

    def test_suspended_nvidia_gpu_is_never_woken(self):
        log, _ = self.tree.nvidia(status="suspended")
        for args in ((), ("--bar",)):
            self.advance(len(log.read_text()) if log.exists() else 0, 3 * (2 if args else 1))
            sample, _ = self.tree.run(*args)
            gpu = only(sample)
            self.assertTrue(gpu["asleep"])
            self.assertIsNone(gpu["busy"])
            self.assertIsNone(gpu["vramUsed"])
            self.assertEqual(gpu["error"], "")
        self.assertFalse(log.exists(), "nvidia-smi must not run while the GPU is suspended")

    def test_active_nvidia_gpu_uses_query_gpu_and_reads_the_limit_once(self):
        log, reply = self.tree.nvidia(status="active", control="on")
        sample, _ = self.tree.run()
        gpu = only(sample)
        self.assertEqual(
            (gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], temperature(gpu)), (37, 1178 * 1048576, 8192 * 1048576, 51)
        )
        self.assertEqual(gpu["thermals"], [{"temperature": 51, "critical": 98, "label": "GPU"}])
        self.assertEqual(
            (gpu["vendor"], gpu["driver"], gpu["type"], gpu["asleep"]), ("nvidia", "nvidia", "discrete", False)
        )
        calls = log.read_text().splitlines()
        self.assertEqual(
            calls,
            [
                "--query-gpu=pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu "
                "--format=csv,noheader,nounits",
                "-q -x -d TEMPERATURE",
            ],
            "a single NVIDIA GPU is asked exactly as upstream asks it",
        )
        self.advance(1, 3)
        self.tree.run("--bar")
        self.assertEqual(
            sum("TEMPERATURE" in call for call in log.read_text().splitlines()), 1, "limit cached per boot"
        )
        self.assertEqual(
            sum("--query-gpu" in call for call in log.read_text().splitlines()), 2, "control=on: no throttle"
        )
        reply.write_text("00000000:01:00.0, [N/A], 9000, 8192, [Not Supported]\n")
        self.advance(2, 6)
        sample, _ = self.tree.run()
        gpu = only(sample)
        self.assertEqual((gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], temperature(gpu)), (None, None, None, None))
        reply.write_text("")
        self.advance(3, 9)
        sample, _ = self.tree.run()
        self.assertIsNone(only(sample)["busy"])
        self.assertEqual(only(sample)["error"], NVIDIA_ERROR)

    def test_unsupported_nvidia_field_keeps_later_columns(self):
        _, reply = self.tree.nvidia(status="active", control="auto")
        reply.write_text("00000000:01:00.0, [Not Supported], 1178, 8192, 51\n")
        sample, _ = self.tree.run()
        gpu = only(sample)
        self.assertEqual(
            (gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], temperature(gpu)),
            (None, 1178 * 1048576, 8192 * 1048576, 51),
        )
        self.assertEqual(gpu["thermals"], [{"temperature": 51, "critical": 98, "label": "GPU"}])
        self.assertEqual(gpu["error"], "")
        # The carried reading stays aligned for throttled bar samples too.
        reply.write_text("00000000:01:00.0, 99, 1, 8192, 1\n")
        self.advance(1, 5)
        carried, _ = self.tree.run("--bar")
        self.assertEqual(only(carried), gpu)

    def test_nvidia_gpus_in_other_pci_domains_never_match(self):
        _, reply = self.tree.nvidia(status="active", control="on")
        reply.write_text("00000001:01:00.0, 99, 10, 20, 90\n00000000:01:00.0, 37, 1178, 8192, 51\n")
        (self.base / "nvidia-smi.xml").write_text(
            '<nvidia_smi_log><gpu id="00000001:01:00.0"><temperature><gpu_temp_max_threshold>70 C</gpu_temp_max_threshold></temperature></gpu>'
            '<gpu id="00000000:01:00.0"><temperature><gpu_temp_max_threshold>98 C</gpu_temp_max_threshold></temperature></gpu></nvidia_smi_log>\n'
        )
        sample, _ = self.tree.run()
        gpu = only(sample)
        self.assertEqual((gpu["busy"], temperature(gpu)), (37, 51))
        self.assertEqual(gpu["thermals"][0]["critical"], 98)
        reply.write_text("00000001:01:00.0, 99, 10, 20, 90\n")
        self.advance(1, 3)
        sample, _ = self.tree.run()
        self.assertIsNone(only(sample)["busy"], "only another domain's GPU answered")
        self.assertEqual(only(sample)["error"], NVIDIA_ERROR)

    def test_failed_limit_probe_is_retried_with_backoff(self):
        log, _ = self.tree.nvidia(status="active", control="on")
        failtemp = self.base / "nvidia-smi.failtemp"
        failtemp.write_text("")

        def limit_probes():
            return sum("TEMPERATURE" in call for call in log.read_text().splitlines())

        sample, _ = self.tree.run()
        self.assertIsNone(only(sample)["thermals"][0]["critical"])
        self.assertEqual(only(sample)["busy"], 37, "a failed limit probe never hides the readings")
        self.assertEqual(limit_probes(), 1)
        self.advance(1, 10)
        self.tree.run()
        self.assertEqual(limit_probes(), 1, "backs off for 30 s after a failure")
        failtemp.unlink()
        self.advance(2, 31)
        sample, _ = self.tree.run()
        self.assertEqual(limit_probes(), 2, "retried after the backoff")
        self.assertEqual(only(sample)["thermals"][0]["critical"], 98, "the limit arrives once the probe answers")
        self.advance(3, 400)
        sample, _ = self.tree.run()
        self.assertEqual(limit_probes(), 2, "an answered probe is kept for the boot")
        self.assertEqual(only(sample)["thermals"][0]["critical"], 98)
        # A new boot asks again.
        (self.tree.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
        self.advance(4, 410)
        self.tree.run()
        self.assertEqual(limit_probes(), 3)

    def test_failed_limit_probes_back_off_exponentially(self):
        log, _ = self.tree.nvidia(status="active", control="on")
        (self.base / "nvidia-smi.failtemp").write_text("")

        def limit_probes():
            return sum("TEMPERATURE" in call for call in log.read_text().splitlines())

        # Bar samples: control=on is never spaced, and they skip the process baseline wait.
        self.tree.run("--bar")
        self.assertEqual(limit_probes(), 1)
        seconds, tick = 0, 0
        # Each failure doubles the wait, up to 32 min.
        for gap in (30, 60, 120, 240, 480, 960, 1920, 1920):
            probes = limit_probes()
            tick += 1
            self.advance(tick, seconds + gap - 3)  # 3 s: past the sample cache
            self.tree.run("--bar")
            self.assertEqual(limit_probes(), probes, f"no retry before {gap} s")
            seconds += gap
            tick += 1
            self.advance(tick, seconds)
            self.tree.run("--bar")
            self.assertEqual(limit_probes(), probes + 1, f"retried {gap} s after the last failure")

    def test_gpu_without_a_limit_is_an_answer(self):
        log, _ = self.tree.nvidia(status="active", control="on")
        (self.base / "nvidia-smi.xml").write_text(
            '<nvidia_smi_log><gpu id="00000000:01:00.0"><temperature><gpu_temp_max_threshold>N/A</gpu_temp_max_threshold></temperature></gpu></nvidia_smi_log>\n'
        )
        sample, _ = self.tree.run()
        self.assertIsNone(only(sample)["thermals"][0]["critical"])
        self.advance(1, 120)
        self.tree.run()
        self.assertEqual(
            sum("TEMPERATURE" in call for call in log.read_text().splitlines()),
            1,
            "a reported missing limit is not retried",
        )

    def test_bar_samples_let_a_runtime_pm_gpu_sleep(self):
        log, _ = self.tree.nvidia(status="active", control="auto")
        self.tree.run("--bar")
        for step in range(1, 5):
            self.advance(step, 5 * step)
            sample, _ = self.tree.run("--bar")
            self.assertEqual(only(sample)["busy"], 37, "throttled samples reuse the last reading")

        def queries():
            return sum("--query-gpu" in call for call in log.read_text().splitlines())

        self.assertEqual(queries(), 1, "at most one query per 30 s while closed")
        self.advance(5, 31)
        self.tree.run("--bar")
        self.assertEqual(queries(), 2)
        self.advance(6, 33)
        self.tree.run()
        self.assertEqual(queries(), 3, "open popups always get a fresh reading")

    def test_intel_gpu_in_a_nonzero_pci_domain(self):
        """An Intel GPU outside PCI domain 0000 (multi-domain, VMD) is still found."""
        tree = self.tree
        tree.gpu(
            "0001:03:00.0", "xe", vendor="0x8086", device="0xe20b", files={"drm/card0/gt/gt0/rps_act_freq_mhz": "800\n"}
        )
        for name in ("bind", "unbind", "new_id", "uevent"):
            (tree.sys / "bus/pci/drivers/xe" / name).touch()
        sample, _ = tree.run("--bar")
        gpu = only(sample)
        self.assertEqual(
            (gpu["id"], gpu["vendor"], gpu["type"], gpu["error"]), ("0001:03:00.0", "intel", "discrete", "")
        )
        self.assertEqual(gpu["frequencyMHz"], 800)

    def test_driver_control_files_are_not_a_bound_intel_gpu(self):
        tree = self.tree
        driver = tree.sys / "bus/pci/drivers/i915"
        driver.mkdir(parents=True)
        for name in ("bind", "unbind", "new_id", "remove_id", "uevent", "module"):
            (driver / name).touch()
        sample, _ = tree.run("--bar")
        self.assertEqual(sample["gpus"], [])
        self.assertFalse(list(tree.state.glob("intel-*")), "no Intel GPU, no Intel scan")

    def test_state_files_are_never_written_through_a_symlink(self):
        tree = self.tree
        tree.gpu("0000:05:00.0", "amdgpu", files=amd_files(10, 1, 2, discrete=True))
        tree.process(300, "busy", 0, 50)
        tree.state.mkdir(mode=0o700)
        outside = self.base / "outside"
        outside.mkdir()
        names = ["current", "lock", "gpu-names"] + [
            f"{kind}.{n}" for kind in ("cache", "cpu", "procs", "gpus") for n in (0, 1)
        ]
        for name in names:
            (outside / name).write_text("keep\n")
            (tree.state / name).symlink_to(outside / name)
        tree.run()
        self.advance(1, 3)
        tree.process(300, "busy", 40, 50)
        sample, _ = tree.run()
        self.assertEqual((sample["mode"], only(sample)["busy"]), ("full", 10))
        self.assertEqual(
            {name: (outside / name).read_text() for name in names},
            dict.fromkeys(names, "keep\n"),
            "a planted state file is never written through",
        )

    def test_unsafe_state_directories_are_not_used(self):
        unshare = ["unshare", "--user", "--map-current-user", "--keep-caps", "--mount"]
        probe = shutil.which("unshare") and subprocess.run(
            [*unshare, "true"], env=isolated_environ(), capture_output=True, timeout=20
        )
        if not probe or probe.returncode:
            self.skipTest("needs an unprivileged user and mount namespace (unshare)")
        # The helper gets an empty /tmp of its own, so its fake tree must be elsewhere.
        base = Path(tempfile.mkdtemp(prefix="foamy-vitals-stats-ns-", dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, base, True)
        self.assertFalse(str(base).startswith("/tmp/"))
        # The helper can live under /tmp too (FOAMY_HELPER, or a checkout
        # there), and the private /tmp would hide it: run copies of the files
        # its argv names, kept next to the fake trees.
        staged = base / "helper"
        staged.mkdir()
        helper = [
            str(shutil.copy2(part, staged / f"{index}-{Path(part).name}")) if os.path.isfile(part) else part
            for index, part in enumerate(HELPER)
        ]
        for planted in (False, True):
            with self.subTest("the /tmp fallback is a symlink too" if planted else "the runtime one is a symlink"):
                tree = Tree(base / ("both" if planted else "runtime"), helper)
                os.chmod(tree.runtime, 0o755)
                targets = [tree.base / "runtime-target", tree.base / "fallback-target"]
                for target in targets:
                    target.mkdir()
                (tree.runtime / f"foamy-vitals-{os.geteuid()}").symlink_to(targets[0])
                mode = "planted" if planted else "-"
                result = subprocess.run(
                    [*unshare, "sh", "-c", PRIVATE_TMP, "sh", mode, str(targets[1]), *tree.helper],
                    env=tree.env(),
                    capture_output=True,
                    timeout=20,
                )
                if result.returncode == 77:
                    self.skipTest("cannot mount a private /tmp")
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                line, fallback = result.stdout.decode().splitlines()
                sample = strict(line)
                for target in targets:
                    self.assertEqual(
                        list(target.iterdir()), [], f"never writes through a planted symlink: {target.name}"
                    )
                if planted:
                    self.assertEqual(fallback, "fallback: unused")
                    self.assertIsNone(sample["cpu"], "no safe directory: the sample works from memory")
                else:
                    self.assertEqual(fallback, "fallback: used", "a symlinked runtime directory falls back to /tmp")


PCI_IDS = (
    "# fixture\n"
    "1002  Advanced Micro Devices, Inc. [AMD/ATI]\n"
    "\t150e  Strix [Radeon 880M / 890M]\n"
    "\t7590  Navi 44 [Radeon RX 9050 / 9060 XT]\n"
    "\t\t1002 0001  Subsystem [Not This]\n"
    '\t7591  Evil "quote" \\ back\\slash\n'
    "10de  NVIDIA Corporation\n"
    "\t2786  AD104 [GeForce RTX 4070]\n"
    "C 03  Display controller\n"
    "\t00  VGA compatible controller\n"
)


class GpuTest(TreeCase):
    def nvidia_calls(self, log, kind):
        """The -i argument of every logged nvidia-smi call of one kind."""
        calls = [call.split() for call in log.read_text().splitlines() if kind in call] if log.exists() else []
        return [call[call.index("-i") + 1] if "-i" in call else "" for call in calls]

    def test_discovers_only_gpu_drivers(self):
        tree = self.tree
        tree.nvidia(addresses=["0000:02:00.0"], control="on")
        tree.gpu("0000:01:00.0", "amdgpu", files=amd_files(1, 1, 2, discrete=True))
        tree.gpu("0000:03:00.0", "nouveau", vendor="0x10de")
        tree.gpu("0000:00:02.0", "i915", vendor="0x8086")
        tree.gpu("0000:04:00.0", "ast", vendor="0x1a03")
        tree.gpu("0000:07:00.0", "vfio-pci", vendor="0x10de")
        tree.gpu("0000:08:00.0", None)
        tree.gpu("0000:09:00.0", "amdgpu", class_="0x040300")
        sample, _ = tree.run()
        self.assertEqual(
            sorted((gpu["id"], gpu["vendor"], gpu["driver"]) for gpu in sample["gpus"]),
            [
                ("0000:00:02.0", "intel", "i915"),
                ("0000:01:00.0", "amd", "amdgpu"),
                ("0000:02:00.0", "nvidia", "nvidia"),
                ("0000:03:00.0", "nvidia", "nouveau"),
            ],
        )
        for gpu in sample["gpus"]:
            self.assertEqual(list(gpu), GPU_KEYS)

    def test_amd_pair_reads_each_cards_own_hwmon(self):
        tree = self.tree
        tree.gpu(
            "0000:05:00.0",
            "amdgpu",
            device="0x7590",
            files=amd_files(70, GIB, 8 * GIB, discrete=True),
            hwmon=[("edge", 50000, 100000), ("junction", 61000, 110000), ("mem", 72000, None)],
        )
        tree.gpu(
            "0000:c1:00.0", "amdgpu", class_="0x038000", files=amd_files(12, 256, 512), hwmon=[("edge", 57000, None)]
        )
        gpus = by_id(tree.run()[0])
        dgpu, igpu = gpus["0000:05:00.0"], gpus["0000:c1:00.0"]
        self.assertEqual(
            (dgpu["type"], dgpu["busy"], dgpu["vramUsed"], dgpu["vramTotal"]), ("discrete", 70, GIB, 8 * GIB)
        )
        self.assertEqual(
            dgpu["thermals"],
            [
                {"temperature": 50, "critical": 100, "label": "Edge"},
                {"temperature": 61, "critical": 110, "label": "Hotspot"},
                {"temperature": 72, "critical": None, "label": "Memory"},
            ],
        )
        self.assertEqual(
            (igpu["type"], igpu["busy"], igpu["vramUsed"], igpu["vramTotal"]), ("integrated", 12, 256, 512)
        )
        self.assertEqual(igpu["thermals"], [{"temperature": 57, "critical": None, "label": "Edge"}])
        self.assertEqual((dgpu["vendor"], dgpu["driver"], dgpu["asleep"], dgpu["error"]), ("amd", "amdgpu", False, ""))
        self.assertEqual((dgpu["memoryPrivate"], dgpu["frequencyMHz"]), (None, None))

    def test_vendorless_dgpu_with_bar_window_is_discrete(self):
        # Mac Pro 6,1 FirePro D700 (Tahiti): no memory vendor, a 256 MiB BAR of 8 GiB.
        tahiti = amd_files(3, 1024, 8 * GIB)
        tahiti.update(
            {
                "mem_info_vis_vram_total": f"{256 * 1048576}\n",
                "mem_info_gtt_used": "100\n",
                "mem_info_gtt_total": f"{60 * GIB}\n",
            }
        )
        apu = amd_files(7, 491, 512)
        apu["mem_info_vis_vram_total"] = "512\n"
        self.tree.gpu("0000:02:00.0", "amdgpu", files=tahiti)
        self.tree.gpu("0000:c1:00.0", "amdgpu", class_="0x038000", files=apu)
        self.tree.gpu("0000:03:00.0", "radeon", files=amd_files(5, 1, 2))
        gpus = by_id(self.tree.run()[0])
        dgpu, igpu, radeon = gpus["0000:02:00.0"], gpus["0000:c1:00.0"], gpus["0000:03:00.0"]
        self.assertEqual(
            (dgpu["type"], dgpu["vramUsed"], dgpu["vramTotal"]), ("discrete", 1024, 8 * GIB), "no GTT on a dGPU"
        )
        self.assertEqual(igpu["type"], "integrated")
        self.assertEqual((radeon["vendor"], radeon["type"]), ("amd", None))

    def test_apu_memory_adds_gtt_to_vram_carve_out(self):
        apu = amd_files(7, 512 * 1048576, GIB // 2)
        apu.update({"mem_info_gtt_used": f"{3 * GIB}\n", "mem_info_gtt_total": f"{15 * GIB}\n"})
        self.tree.gpu("0000:c1:00.0", "amdgpu", class_="0x038000", files=apu)
        gpu = only(self.tree.run()[0])
        self.assertEqual((gpu["type"], gpu["vramUsed"], gpu["vramTotal"]), ("integrated", 3.5 * GIB, 15.5 * GIB))

    def test_apu_without_gtt_keeps_vram_carve_out(self):
        self.tree.gpu("0000:c1:00.0", "amdgpu", class_="0x038000", files=amd_files(7, 491, 512))
        broken = amd_files(7, 491, 512)
        broken.update({"mem_info_gtt_used": "900\n", "mem_info_gtt_total": "800\n"})  # used > total: not a pair
        self.tree.gpu("0001:c1:00.0", "amdgpu", class_="0x038000", files=broken)
        for gpu in self.tree.run()[0]["gpus"]:
            self.assertEqual((gpu["type"], gpu["vramUsed"], gpu["vramTotal"]), ("integrated", 491, 512))

    def test_suspended_gpus_are_never_opened(self):
        tree = self.tree
        files = amd_files(1, 1, 2, discrete=True)
        files.update({"gpu_busy_percent": FIFO, "hwmon/hwmon0/temp1_input": FIFO})
        tree.gpu("0000:05:00.0", "amdgpu", control="auto", status="suspended", files=files)
        log, _ = tree.nvidia(addresses=["0000:01:00.0", "0000:02:00.0"], control="on")
        (tree.device("0000:02:00.0") / "power/runtime_status").write_text("suspended\n")
        # Printing an xe fdinfo wakes its GPU: a FIFO here blocks the sample if it is opened.
        tree.intel(control="auto", status="suspended")
        tree.descriptor(41, 3, FIFO)
        for step, args in enumerate(((), ("--bar",), ())):
            self.advance(step, 3 * step)
            sample, _ = tree.run(*args, timeout=5)
            gpus = by_id(sample)
            for address in ("0000:05:00.0", "0000:02:00.0", "0000:00:02.0"):
                gpu = gpus[address]
                self.assertTrue(gpu["asleep"], address)
                self.assertEqual((gpu["busy"], gpu["vramUsed"], gpu["thermals"], gpu["error"]), (None, None, [], ""))
            self.assertFalse(gpus["0000:01:00.0"]["asleep"])
            self.assertEqual(gpus["0000:01:00.0"]["busy"], 37)
        calls = self.nvidia_calls(log, "--query-gpu") + self.nvidia_calls(log, "TEMPERATURE")
        self.assertTrue(calls)
        for ids in calls:
            self.assertEqual(ids, "00000000:01:00.0", "a suspended GPU is never passed to nvidia-smi")
        self.assertFalse(list(tree.state.glob("intel-*")), "a suspended Intel GPU is never scanned")

    def test_one_bad_file_loses_one_value(self):
        files = amd_files(40, 1, 2, discrete=True)
        files.update({"mem_info_vram_used": None, "hwmon/hwmon0/temp2_input": None})
        self.tree.gpu(
            "0000:05:00.0",
            "amdgpu",
            files=files,
            hwmon=[("edge", 50000, 100000), ("junction", 61000, None), ("mem", 72000, 95000)],
        )
        sample, stderr = self.tree.run()
        gpu = only(sample)
        self.assertEqual((gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["error"]), (40, None, None, ""))
        self.assertEqual(
            gpu["thermals"],
            [
                {"temperature": 50, "critical": 100, "label": "Edge"},
                {"temperature": 72, "critical": 95, "label": "Memory"},
            ],
        )
        for name in ("mem_info", "temp2_input", "_crit", "_label", "hwmon0"):
            self.assertNotIn(name, stderr, "an unreadable or missing sysfs file is not a diagnostic")

    def test_gpu_gone_between_samples_drops_its_row(self):
        self.tree.gpu("0000:05:00.0", "amdgpu", files=amd_files(1, 1, 2, discrete=True))
        self.tree.gpu("0000:c1:00.0", "amdgpu", files=amd_files(2, 1, 2))
        self.assertEqual(len(self.tree.run()[0]["gpus"]), 2)
        (self.tree.sys / "bus/pci/drivers/amdgpu/0000:05:00.0").unlink()
        self.advance(1, 3)
        self.assertEqual(only(self.tree.run()[0])["id"], "0000:c1:00.0")

    def test_nouveau_row_has_temperature_only(self):
        log, _ = self.tree.nvidia(addresses=[])
        self.tree.gpu("0000:01:00.0", "nouveau", vendor="0x10de", device="0x2786", hwmon=[(None, 45000, None)])
        gpu = only(self.tree.run()[0])
        self.assertEqual(
            (gpu["vendor"], gpu["driver"], gpu["type"], gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["error"]),
            ("nvidia", "nouveau", "discrete", None, None, None, ""),
        )
        self.assertEqual(gpu["thermals"], [{"temperature": 45, "critical": None, "label": "Sensor 1"}])
        self.assertFalse(log.exists(), "nouveau is never asked through nvidia-smi")

    def test_two_nvidia_gpus_one_query_per_sample(self):
        # "" is a call without -i: every bound NVIDIA GPU is due, so nothing
        # needs narrowing and the command line is upstream's.
        tree = self.tree
        log, reply = tree.nvidia(addresses=["0000:01:00.0", "0000:02:00.0"], control="on")
        (tree.device("0000:02:00.0") / "power/control").write_text("auto\n")
        xml = self.base / "nvidia-smi.xml"
        xml.write_text(
            '<nvidia_smi_log><gpu id="00000000:01:00.0"><temperature><gpu_temp_max_threshold>98 C'
            "</gpu_temp_max_threshold></temperature></gpu></nvidia_smi_log>\n"
        )
        gpus = by_id(tree.run()[0])
        self.assertEqual(self.nvidia_calls(log, "--query-gpu"), [""], "both due: no -i")
        self.assertEqual(self.nvidia_calls(log, "TEMPERATURE"), [""], "both probed: no -i")
        self.assertEqual([gpus[a]["busy"] for a in ("0000:01:00.0", "0000:02:00.0")], [37, 37])
        self.assertEqual([gpus[a]["thermals"][0]["critical"] for a in ("0000:01:00.0", "0000:02:00.0")], [98, None])

        self.advance(1, 5)
        gpus = by_id(tree.run("--bar")[0])
        self.assertEqual(
            self.nvidia_calls(log, "--query-gpu"),
            ["", "00000000:01:00.0"],
            "the auto GPU is spaced out, so -i names only the due one",
        )
        self.assertEqual(gpus["0000:02:00.0"]["busy"], 37, "and carries its last reading")
        self.assertEqual(len(self.nvidia_calls(log, "TEMPERATURE")), 1, "the failed limit backs off")

        xml.unlink()
        self.advance(2, 31)
        gpus = by_id(tree.run()[0])
        self.assertEqual(self.nvidia_calls(log, "--query-gpu")[2:], [""])
        self.assertEqual(self.nvidia_calls(log, "TEMPERATURE")[1:], [""], "the only unknown limit is probed: no -i")
        self.assertEqual([gpus[a]["thermals"][0]["critical"] for a in ("0000:01:00.0", "0000:02:00.0")], [98, 98])

        reply.write_text("00000000:01:00.0, 37, 1178, 8192, 51\n")
        self.advance(3, 40)
        gpus = by_id(tree.run()[0])
        self.assertEqual(self.nvidia_calls(log, "--query-gpu")[3:], [""])
        self.assertEqual((gpus["0000:01:00.0"]["busy"], gpus["0000:01:00.0"]["error"]), (37, ""))
        self.assertEqual((gpus["0000:02:00.0"]["busy"], gpus["0000:02:00.0"]["error"]), (None, NVIDIA_ERROR))

    def test_limit_probe_narrows_only_when_a_gpu_is_left_out(self):
        tree = self.tree
        log, _ = tree.nvidia(addresses=["0000:01:00.0", "0000:02:00.0"], control="on")
        (self.base / "nvidia-smi.failtemp").write_text("")
        tree.run()
        self.assertEqual(self.nvidia_calls(log, "TEMPERATURE"), [""])
        # GPU 2 goes to sleep, and both limits are due a retry.
        (tree.device("0000:02:00.0") / "power/runtime_status").write_text("suspended\n")
        (self.base / "nvidia-smi.failtemp").unlink()
        self.advance(1, 31)
        gpus = by_id(tree.run()[0])
        self.assertEqual(
            self.nvidia_calls(log, "--query-gpu")[1:], ["00000000:01:00.0"], "a suspended GPU is never named"
        )
        self.assertEqual(self.nvidia_calls(log, "TEMPERATURE")[1:], ["00000000:01:00.0"])
        self.assertEqual(gpus["0000:01:00.0"]["thermals"][0]["critical"], 98)
        self.assertTrue(gpus["0000:02:00.0"]["asleep"])

    def test_integrated_runtime_pm_gpu_keeps_the_bar_cadence(self):
        igpu = self.tree.gpu("0000:c1:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2))
        dgpu = self.tree.gpu("0000:05:00.0", "amdgpu", control="auto", files=amd_files(20, 1, 2, discrete=True))
        gpus = by_id(self.tree.run("--bar")[0])
        self.assertEqual((gpus["0000:c1:00.0"]["type"], gpus["0000:c1:00.0"]["busy"]), ("integrated", 10))
        (igpu / "gpu_busy_percent").write_text("50\n")
        (dgpu / "gpu_busy_percent").write_text("60\n")
        self.advance(1, 5)
        gpus = by_id(self.tree.run("--bar")[0])
        self.assertEqual(gpus["0000:c1:00.0"]["busy"], 50, "an integrated GPU is read on every bar sample")
        self.assertEqual(gpus["0000:05:00.0"]["busy"], 20, "a discrete runtime-PM GPU is spaced out")

    def test_dgpu_driving_a_display_keeps_the_bar_cadence(self):
        # amdgpu leaves a desktop dGPU that drives a display awake for good,
        # so spacing it would only make the bar's reading stale.
        devices = {}
        for card, (address, state) in enumerate((("0000:03:00.0", "connected"), ("0000:04:00.0", "disconnected")), 1):
            files = amd_files(20, 1, 2, discrete=True)
            files.update(
                {
                    f"drm/card{card}/card{card}-DP-1/status": f"{state}\n",
                    f"drm/card{card}/card{card}-HDMI-A-1/status": "disconnected\n",
                }
            )
            devices[address] = self.tree.gpu(address, "amdgpu", control="auto", files=files)
        gpus = by_id(self.tree.run("--bar")[0])
        self.assertEqual([gpus[address]["busy"] for address in devices], [20, 20])
        for device in devices.values():
            (device / "gpu_busy_percent").write_text("60\n")
        self.advance(1, 5)
        gpus = by_id(self.tree.run("--bar")[0])
        self.assertEqual(gpus["0000:03:00.0"]["busy"], 60, "a connected display exempts the GPU from spacing")
        self.assertEqual(gpus["0000:04:00.0"]["busy"], 20, "without one the GPU stays spaced")

    def test_nvidia_driving_a_display_stays_spaced(self):
        # Upstream spaces an auto-PM NVIDIA GPU whatever it drives; the display
        # exemption must not add nvidia-smi calls to its bar samples.
        log, _ = self.tree.nvidia(status="active", control="auto")
        connector = self.tree.device(NVIDIA_ADDRESS) / "drm/card1/card1-HDMI-A-1/status"
        connector.parent.mkdir(parents=True)
        connector.write_text("connected\n")
        self.tree.run("--bar")
        self.advance(1, 5)
        self.tree.run("--bar")
        self.assertEqual(self.nvidia_calls(log, "--query-gpu"), [""], "a spaced NVIDIA GPU is queried once")

    def test_gpu_type_is_kept_for_the_boot(self):
        # A dGPU being hot-unplugged loses its memory files before its binding.
        device = self.tree.gpu("0000:05:00.0", "amdgpu", files=amd_files(10, 1, 2, discrete=True))
        self.assertEqual(only(self.tree.run()[0])["type"], "discrete")
        (device / "mem_info_vram_vendor").unlink()
        self.advance(1, 3)
        self.assertEqual(only(self.tree.run()[0])["type"], "discrete", "the type is read once per boot")
        (self.tree.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
        self.advance(2, 6)
        self.assertEqual(only(self.tree.run()[0])["type"], "integrated", "a new boot reads it again")

    def test_carried_then_suspended_then_woke_reads_fresh(self):
        tree = self.tree
        device = tree.gpu(
            "0000:05:00.0",
            "amdgpu",
            control="auto",
            files=amd_files(10, 1, 2, discrete=True),
            hwmon=[("edge", 50000, None)],
        )
        self.assertEqual(only(tree.run("--bar")[0])["busy"], 10)
        (device / "gpu_busy_percent").write_text("50\n")
        self.advance(1, 5)
        self.assertEqual(only(tree.run("--bar")[0])["busy"], 10, "carried while spaced out")
        # Asleep: nothing of it is opened (a FIFO would block the read).
        (device / "power/runtime_status").write_text("suspended\n")
        for name in ("gpu_busy_percent", "hwmon/hwmon0/temp1_input"):
            (device / name).unlink()
            os.mkfifo(device / name)
        self.advance(2, 10)
        gpu = only(tree.run("--bar", timeout=5)[0])
        self.assertTrue(gpu["asleep"])
        self.assertEqual(
            (gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["thermals"], gpu["error"]), (None, None, None, [], "")
        )
        # Awake again inside the old 30 s window: read now, not the carried 10.
        (device / "power/runtime_status").write_text("active\n")
        for name, value in (("gpu_busy_percent", "70\n"), ("hwmon/hwmon0/temp1_input", "60000\n")):
            (device / name).unlink()
            (device / name).write_text(value)
        self.advance(3, 15)
        gpu = only(tree.run("--bar")[0])
        self.assertEqual((gpu["asleep"], gpu["busy"], temperature(gpu)), (False, 70, 60))

    def test_mixed_vendors_all_listed(self):
        tree = self.tree
        card, device = tree.intel()
        tree.descriptor(41, 3, intel_fdinfo(render=550_000_100))
        (card / "gt/gt0").mkdir(parents=True)
        (card / "gt/gt0/rps_act_freq_mhz").write_text("900\n")
        (device / "hwmon/hwmon1").mkdir(parents=True)
        (device / "hwmon/hwmon1/temp1_input").write_text("44000\n")
        tree.intel_state(
            10 * SECOND,
            {"1": {"engines": {"render": 100, "video": 0}, "capacities": {"video": 2}, "private": 63 * 1048576}},
        )
        tree.nvidia(addresses=["0000:01:00.0"], control="on")
        tree.gpu("0000:05:00.0", "amdgpu", files=amd_files(33, 1, 2, discrete=True))
        sample, _ = tree.run()
        gpus = by_id(sample)
        self.assertEqual(sorted(gpus), ["0000:00:02.0", "0000:01:00.0", "0000:05:00.0"])
        intel, green, red = gpus["0000:00:02.0"], gpus["0000:01:00.0"], gpus["0000:05:00.0"]
        self.assertEqual(
            (
                intel["vendor"],
                intel["type"],
                intel["vramUsed"],
                intel["vramTotal"],
                intel["memoryPrivate"],
                intel["frequencyMHz"],
                intel["error"],
            ),
            ("intel", "integrated", None, None, 63 * 1048576, 900, ""),
        )
        # 550 ms of render time over a baseline at least 10 s old: a slow run only lowers it.
        self.assertGreater(intel["busy"], 0)
        self.assertLessEqual(intel["busy"], 6.5)
        self.assertEqual(intel["thermals"], [{"temperature": 44.0, "critical": None, "label": "GPU"}])
        self.assertEqual(
            (green["vendor"], green["type"], green["busy"], green["vramTotal"]),
            ("nvidia", "discrete", 37, 8192 * 1048576),
        )
        self.assertEqual((red["vendor"], red["type"], red["busy"]), ("amd", "discrete", 33))
        for gpu in sample["gpus"]:
            self.assertEqual(list(gpu), GPU_KEYS)
        self.assertEqual(
            [path.name for path in tree.state.glob("intel-*")],
            ["intel-0000:00:02.0.json"],
            "one baseline per Intel GPU",
        )

    def test_intel_failure_is_unavailable(self):
        tree = self.tree
        tree.intel()
        tree.intel(driver="xe", device="0000:03:00.0", card="card2", control="auto")
        tree.descriptor(41, 3, intel_fdinfo())
        tree.descriptor(41, 4, "", target="/dev/dri/renderD130")
        (tree.proc / "41/fdinfo/4").write_bytes(b"drm-driver: xe\n\xff\n")
        sample, stderr = tree.run()
        gpus = by_id(sample)
        for gpu in gpus.values():
            self.assertEqual(
                (gpu["busy"], gpu["memoryPrivate"], gpu["frequencyMHz"], gpu["thermals"], gpu["error"]),
                (None, None, None, [], INTEL_ERROR),
            )
        self.assertIn("Intel telemetry:", stderr, "a failed scan fails every Intel GPU")

        # Now only the dGPU fails (its baseline can't be written): the iGPU reads.
        (tree.proc / "41/fdinfo/4").write_text(intel_fdinfo(2, driver="xe", device="0000:03:00.0"))
        (tree.state / "intel-0000:03:00.0.json").mkdir()
        self.advance(1, 3)
        gpus = by_id(tree.run("--bar")[0])
        self.assertEqual((gpus["0000:00:02.0"]["memoryPrivate"], gpus["0000:00:02.0"]["error"]), (63 * 1048576, ""))
        self.assertEqual((gpus["0000:03:00.0"]["busy"], gpus["0000:03:00.0"]["error"]), (None, INTEL_ERROR))
        # A failed read is never carried: the spaced-out dGPU is read again next time.
        (tree.state / "intel-0000:03:00.0.json").rmdir()
        self.advance(2, 6)
        gpus = by_id(tree.run("--bar")[0])
        self.assertEqual((gpus["0000:03:00.0"]["memoryPrivate"], gpus["0000:03:00.0"]["error"]), (63 * 1048576, ""))

    def test_spaced_intel_dgpu_keeps_a_wider_baseline_window(self):
        tree = self.tree
        tree.intel(driver="xe", device="0000:03:00.0", card="card2", control="auto")
        tree.descriptor(
            41, 3, intel_fdinfo(render=4_500_000_100, driver="xe", device="0000:03:00.0"), target="/dev/dri/renderD130"
        )
        baseline = {"1": {"engines": {"render": 100, "video": 0}, "capacities": {"video": 2}, "private": 1}}
        tree.intel_state(45 * SECOND, baseline, device="0000:03:00.0")
        gpu = only(tree.run("--bar")[0])
        self.assertAlmostEqual(gpu["busy"], 10.0, delta=1.0, msg="45 s is inside a spaced GPU's 60 s window")
        (tree.device("0000:03:00.0") / "power/control").write_text("on\n")
        tree.intel_state(45 * SECOND, baseline, device="0000:03:00.0")
        self.advance(1, 3)
        self.assertIsNone(only(tree.run("--bar")[0])["busy"], "but outside the 30 s window of a GPU read every sample")

    def test_spaced_intel_dgpu_is_not_scanned_between_reads(self):
        # Opening an xe fdinfo wakes the GPU: between reads the last reading is carried.
        tree = self.tree
        tree.intel(driver="xe", device="0000:03:00.0", card="card2", control="auto")
        tree.descriptor(41, 3, intel_fdinfo(driver="xe", device="0000:03:00.0"), target="/dev/dri/renderD130")
        first = only(tree.run("--bar")[0])
        self.assertEqual((first["memoryPrivate"], first["error"]), (63 * 1048576, ""))
        (tree.proc / "41/fdinfo/3").unlink()
        tree.descriptor(41, 3, FIFO, target="/dev/dri/renderD130")  # opening it would block the scan
        self.advance(1, 5)
        self.assertEqual(only(tree.run("--bar", timeout=5)[0]), first, "carried for 30 s")

    def test_names_from_pci_ids_are_cached_and_clean(self):
        tree = self.tree
        ids = self.base / "pci.ids"
        ids.write_text(PCI_IDS)
        for address, device in (("0000:05:00.0", "0x7590"), ("0000:06:00.0", "0x7591"), ("0000:07:00.0", "0x7592")):
            tree.gpu(address, "amdgpu", device=device, files=amd_files(1, 1, 2, discrete=True))

        def names():
            return {gpu["id"]: gpu["name"] for gpu in tree.run()[0]["gpus"]}

        self.assertEqual(
            names(),
            {"0000:05:00.0": "Radeon RX 9050 / 9060 XT", "0000:06:00.0": "Evil quote  backslash", "0000:07:00.0": None},
        )
        ids.write_text(
            PCI_IDS.replace("Navi 44 [Radeon RX 9050 / 9060 XT]", "Changed [Other]").replace(
                "\t7591", "\t7592  Now Known\n\t7591"
            )
        )
        self.advance(1, 3)
        self.assertEqual(
            names(),
            {"0000:05:00.0": "Radeon RX 9050 / 9060 XT", "0000:06:00.0": "Evil quote  backslash", "0000:07:00.0": None},
            "cached for the boot, misses included",
        )
        (tree.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
        self.advance(2, 6)
        self.assertEqual(names()["0000:05:00.0"], "Other")
        self.assertEqual(names()["0000:07:00.0"], "Now Known")

    def test_gpu_cache_survives_a_killed_writer(self):
        device = self.tree.gpu("0000:05:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2, discrete=True))
        self.assertEqual(only(self.tree.run("--bar")[0])["busy"], 10)
        (device / "gpu_busy_percent").write_text("50\n")
        self.advance(1, 5)
        self.assertEqual(only(self.tree.run("--bar")[0])["busy"], 10, "carried while spaced out")
        generation = (self.tree.state / "current").read_text().split()[6]
        state = self.tree.state / f"gpus.{generation}"
        text = state.read_text()
        state.write_text(text[: len(text) - 12])
        self.advance(2, 10)
        self.assertEqual(only(self.tree.run("--bar")[0])["busy"], 50, "a truncated line is not a reading")
        state = self.tree.state / f"gpus.{(self.tree.state / 'current').read_text().split()[6]}"
        state.write_text("garbage\n")
        (device / "gpu_busy_percent").write_text("70\n")
        self.advance(3, 15)
        self.assertEqual(only(self.tree.run("--bar")[0])["busy"], 70)

    def test_a_long_lock_wait_still_replies_inside_the_qml_timeout(self):
        # The Panel runs the helper under `timeout --kill-after=1s 5s`. A
        # second shell's sample holding the state lock for about 3.7 s, then a
        # hung nvidia-smi (1.5 s of its own), would add up past that: the
        # wrapper kills the helper and the bar gets no sample at all.
        log, _ = self.tree.nvidia(control="on")
        (self.tree.bin / "nvidia-smi").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{log}'\nexec sleep 10\n")
        self.tree.state.mkdir(mode=0o700)
        # The holder says when it has the lock, so the helper surely waits.
        holder = subprocess.Popen(
            ["flock", str(self.tree.state / "lock"), "sh", "-c", "echo locked; exec sleep 3.7"],
            env=isolated_environ(),
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        started = time.monotonic()
        result = subprocess.run(
            ["timeout", "--kill-after=1s", "5s", *self.tree.helper, "--bar"],
            env=self.tree.env(),
            capture_output=True,
            timeout=20,
        )
        elapsed = time.monotonic() - started
        stderr = result.stderr.decode(errors="replace")
        self.assertEqual(result.returncode, 0, f"exit {result.returncode} after {elapsed:.1f} s: {stderr}")
        self.assertGreaterEqual(elapsed, 3.0, "the helper waited for the lock")
        sample = strict(result.stdout.decode())
        self.assertEqual(only(sample)["error"], NVIDIA_ERROR, "the query that didn't fit is an NVIDIA error")
        self.assertLess(elapsed, 5)


class RealSystemTest(unittest.TestCase):
    def test_real_gpus_are_valid_json(self):
        runtime = Path(tempfile.mkdtemp(prefix="foamy-vitals-real-"))
        os.chmod(runtime, 0o700)
        try:
            output = subprocess.run(
                HELPER, env=isolated_environ(XDG_RUNTIME_DIR=str(runtime)), capture_output=True, check=True, timeout=20
            ).stdout
        finally:
            shutil.rmtree(runtime, ignore_errors=True)
        sample = strict(output.decode("utf-8", "replace"))
        self.assertIsInstance(sample["gpus"], list)
        for gpu in sample["gpus"]:
            self.assertEqual(list(gpu), GPU_KEYS)
            self.assertRegex(gpu["id"], r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
            self.assertIn(gpu["vendor"], ("amd", "intel", "nvidia"))
            self.assertIn(gpu["type"], ("integrated", "discrete", None))
            self.assertIsInstance(gpu["asleep"], bool)
            self.assertIsInstance(gpu["error"], str)
            for thermal in gpu["thermals"]:
                self.assertEqual(list(thermal), ["temperature", "critical", "label"])

    def test_real_process_with_control_characters(self):
        """The bug as reported: a process renames itself with a control byte."""
        runtime = Path(tempfile.mkdtemp(prefix="foamy-vitals-real-"))
        os.chmod(runtime, 0o700)
        # It waits on stdin: idle, it still has resident memory, so the first full sample lists it.
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                textwrap.dedent("""\
            import sys
            open("/proc/self/comm", "wb").write(b"evil\\x01na\\"me")
            print("ready", flush=True)
            sys.stdin.read()
            """),
            ],
            env=isolated_environ(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            # The real /proc with only the child's process in it: on a busy
            # machine other processes would push it out of the top five.
            proc = runtime / "proc"
            proc.mkdir()
            for entry in os.listdir("/proc"):
                if not entry.isdigit() or entry == str(child.pid):
                    (proc / entry).symlink_to(Path("/proc") / entry)
            env = isolated_environ(XDG_RUNTIME_DIR=str(runtime), FOAMY_VITALS_PROC=str(proc))
            output = subprocess.run(HELPER, env=env, capture_output=True, check=True, timeout=20).stdout
            sample = strict(output.decode("utf-8", "replace"))
            names = [entry["name"] for entry in sample["memoryProcesses"]]
            self.assertIn("evilname", names, "the process must be listed, its name cleaned")
        finally:
            child.stdin.close()
            child.send_signal(signal.SIGKILL)
            child.wait()
            child.stdout.close()
            shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
