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
from fakesys import HELPER, Tree, strict  # noqa: E402

PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
EVIL = 'evil\x01na"me\\\x1b[31m'


class FakeTreeTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="foamy-vitals-stats-"))
        self.tree = Tree(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def advance(self, tick, seconds):
        self.tree.advance(tick, seconds)

    def test_hostile_names_stay_valid_json(self):
        tree = self.tree
        # pid: (name, jiffies in the second sample, resident pages, cwd, kernel thread)
        specs = {101: (EVIL, 80, 300000, "/home/user/pro\x02ject\"x", False),
                 102: ("tab\there", 20, 1000, "/home/user/second", False),
                 103: ("kworker/0:1", 999, 0, None, True),
                 104: ("\x7f\x01\x02", 5, 10, None, False),
                 105: ("bytes\udcff\udcfe", 1, 20, None, False)}
        for pid, (name, _, rss, cwd, kernel) in specs.items():
            tree.process(pid, name, 0, rss, cwd=cwd, kernel=kernel)
        first, _ = tree.run()
        self.assertEqual(first["processes"], [])
        self.advance(1, 2)
        for pid, (name, jiffies, rss, cwd, kernel) in specs.items():
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
            self.assertFalse(ord(text) < 0x20 or ord(text) == 0x7f)

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
        self.assertEqual([entry["name"] for entry in sample["memoryProcesses"]], ["p219", "p218", "p217", "p216", "p215"])

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

    def test_temperatures_and_amd_gpu(self):
        tree = self.tree
        tree.hwmon(0, "nvme", [("Composite", 40000, 80000)])
        tree.hwmon(1, "k10temp", [("Tccd1", 70000, None), ("Tctl", 55400, 95000)])
        tree.hwmon(2, "amdgpu", [("edge", 48000, 100000), ("junction", 61000, 110000), ("mem", 70000, 105000)])
        card = tree.sys / "class/drm/card1/device"
        card.mkdir(parents=True)
        (card / "gpu_busy_percent").write_text("42\n")
        (card / "mem_info_vram_used").write_text("1024\n")
        (card / "mem_info_vram_total").write_text("4096\n")
        sample, _ = tree.run("--bar")
        self.assertEqual((sample["cpuTemp"], sample["cpuTempCrit"]), (55, 95))
        self.assertEqual((sample["gpuBusy"], sample["vramUsed"], sample["vramTotal"], sample["gpuTemp"]), (42, 1024, 4096, 48))
        self.assertEqual([t["label"] for t in sample["gpuThermals"]], ["Edge", "Hotspot", "Memory"])

    def test_suspended_nvidia_gpu_is_never_woken(self):
        log, _ = self.tree.nvidia(status="suspended")
        for args in ((), ("--bar",)):
            self.advance(len(log.read_text()) if log.exists() else 0, 3 * (2 if args else 1))
            sample, _ = self.tree.run(*args)
            self.assertTrue(sample["gpuSuspended"])
            self.assertEqual(sample["gpuBusy"], 0)
            self.assertIsNone(sample["vramUsed"])
            self.assertEqual(sample["gpuError"], "")
        self.assertFalse(log.exists(), "nvidia-smi must not run while the GPU is suspended")

    def test_active_nvidia_gpu_uses_query_gpu_and_reads_the_limit_once(self):
        log, reply = self.tree.nvidia(status="active", control="on")
        sample, _ = self.tree.run()
        self.assertEqual((sample["gpuBusy"], sample["vramUsed"], sample["vramTotal"], sample["gpuTemp"]),
                         (37, 1178 * 1048576, 8192 * 1048576, 51))
        self.assertEqual(sample["gpuThermals"], [{"temperature": 51, "critical": 98, "label": "GPU"}])
        self.assertFalse(sample["gpuSuspended"])
        calls = log.read_text().splitlines()
        self.assertTrue(any("--query-gpu=" in call for call in calls))
        self.assertTrue(all(" -q " not in f" {call} " or "TEMPERATURE" in call for call in calls), "no full -q query")
        self.advance(1, 3)
        self.tree.run("--bar")
        self.assertEqual(sum("TEMPERATURE" in call for call in log.read_text().splitlines()), 1, "limit cached per boot")
        self.assertEqual(sum("--query-gpu" in call for call in log.read_text().splitlines()), 2, "control=on: no throttle")
        reply.write_text("00000000:01:00.0, [N/A], 9000, 8192, [Not Supported]\n")
        self.advance(2, 6)
        sample, _ = self.tree.run()
        self.assertEqual((sample["gpuBusy"], sample["vramUsed"], sample["vramTotal"], sample["gpuTemp"]), (None, None, None, None))
        reply.write_text("")
        self.advance(3, 9)
        sample, _ = self.tree.run()
        self.assertIsNone(sample["gpuBusy"])
        self.assertEqual(sample["gpuError"], "NVIDIA readings unavailable. Check nvidia-smi.")

    def test_unsupported_nvidia_field_keeps_later_columns(self):
        _, reply = self.tree.nvidia(status="active", control="on")
        reply.write_text("00000000:01:00.0, [Not Supported], 1178, 8192, 51\n")
        sample, _ = self.tree.run()
        self.assertEqual((sample["gpuBusy"], sample["vramUsed"], sample["vramTotal"], sample["gpuTemp"]),
                         (None, 1178 * 1048576, 8192 * 1048576, 51))
        self.assertEqual(sample["gpuThermals"], [{"temperature": 51, "critical": 98, "label": "GPU"}])
        self.assertEqual(sample["gpuError"], "")
        # The cache line stays aligned for throttled bar samples too.
        cached = (self.tree.runtime / f"foamy-vitals-{os.geteuid()}/nvidia").read_text().split()
        self.assertEqual(cached[1:6], ["na", "1178", "8192", "51", "98"])

    def test_nvidia_gpus_in_other_pci_domains_never_match(self):
        _, reply = self.tree.nvidia(status="active", control="on")
        reply.write_text("00000001:01:00.0, 99, 10, 20, 90\n00000000:01:00.0, 37, 1178, 8192, 51\n")
        (self.base / "nvidia-smi.xml").write_text(
            '<nvidia_smi_log><gpu id="00000001:01:00.0"><temperature><gpu_temp_max_threshold>70 C</gpu_temp_max_threshold></temperature></gpu>'
            '<gpu id="00000000:01:00.0"><temperature><gpu_temp_max_threshold>98 C</gpu_temp_max_threshold></temperature></gpu></nvidia_smi_log>\n')
        sample, _ = self.tree.run()
        self.assertEqual((sample["gpuBusy"], sample["gpuTemp"]), (37, 51))
        self.assertEqual(sample["gpuThermals"][0]["critical"], 98)
        reply.write_text("00000001:01:00.0, 99, 10, 20, 90\n")
        self.advance(1, 3)
        sample, _ = self.tree.run()
        self.assertIsNone(sample["gpuBusy"], "only another domain's GPU answered")
        self.assertEqual(sample["gpuError"], "NVIDIA readings unavailable. Check nvidia-smi.")

    def test_failed_limit_probe_is_retried_with_backoff(self):
        log, _ = self.tree.nvidia(status="active", control="on")
        failtemp = self.base / "nvidia-smi.failtemp"
        failtemp.write_text("")
        limit_probes = lambda: sum("TEMPERATURE" in call for call in log.read_text().splitlines())
        sample, _ = self.tree.run()
        self.assertIsNone(sample["gpuThermals"][0]["critical"])
        self.assertEqual(sample["gpuBusy"], 37, "a failed limit probe never hides the readings")
        self.assertEqual(limit_probes(), 1)
        self.advance(1, 10)
        self.tree.run()
        self.assertEqual(limit_probes(), 1, "backs off for 30 s after a failure")
        failtemp.unlink()
        self.advance(2, 31)
        sample, _ = self.tree.run()
        self.assertEqual(limit_probes(), 2, "retried after the backoff")
        self.assertEqual(sample["gpuThermals"][0]["critical"], 98, "the limit arrives once the probe answers")
        self.advance(3, 400)
        sample, _ = self.tree.run()
        self.assertEqual(limit_probes(), 2, "an answered probe is kept for the boot")
        self.assertEqual(sample["gpuThermals"][0]["critical"], 98)
        # A new boot asks again.
        (self.tree.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
        self.advance(4, 410)
        self.tree.run()
        self.assertEqual(limit_probes(), 3)

    def test_failed_limit_probes_back_off_exponentially(self):
        log, _ = self.tree.nvidia(status="active", control="on")
        (self.base / "nvidia-smi.failtemp").write_text("")
        limit_probes = lambda: sum("TEMPERATURE" in call for call in log.read_text().splitlines())
        seconds, tick = 0, 0
        for expected_gap in (30, 60, 120):
            self.tree.run()
            probes = limit_probes()
            tick += 1
            self.advance(tick, seconds + expected_gap - 3)  # 3 s: past the 2 s sample cache
            self.tree.run()
            self.assertEqual(limit_probes(), probes, f"no retry before {expected_gap} s")
            seconds += expected_gap
            tick += 1
            self.advance(tick, seconds)

    def test_gpu_without_a_limit_is_an_answer(self):
        log, _ = self.tree.nvidia(status="active", control="on")
        (self.base / "nvidia-smi.xml").write_text('<nvidia_smi_log><gpu id="00000000:01:00.0"><temperature><gpu_temp_max_threshold>N/A</gpu_temp_max_threshold></temperature></gpu></nvidia_smi_log>\n')
        sample, _ = self.tree.run()
        self.assertIsNone(sample["gpuThermals"][0]["critical"])
        self.advance(1, 120)
        self.tree.run()
        self.assertEqual(sum("TEMPERATURE" in call for call in log.read_text().splitlines()), 1, "a reported missing limit is not retried")

    def test_bar_samples_let_a_runtime_pm_gpu_sleep(self):
        log, _ = self.tree.nvidia(status="active", control="auto")
        self.tree.run("--bar")
        for step in range(1, 5):
            self.advance(step, 5 * step)
            sample, _ = self.tree.run("--bar")
            self.assertEqual(sample["gpuBusy"], 37, "throttled samples reuse the last reading")
        queries = lambda: sum("--query-gpu" in call for call in log.read_text().splitlines())
        self.assertEqual(queries(), 1, "at most one query per 30 s while closed")
        self.advance(5, 31)
        self.tree.run("--bar")
        self.assertEqual(queries(), 2)
        self.advance(6, 33)
        self.tree.run()
        self.assertEqual(queries(), 3, "open popups always get a fresh reading")

    def test_helper_diagnostics_reach_stderr(self):
        tree = self.tree
        bound = tree.sys / "bus/pci/drivers/i915/0000:00:02.0"
        bound.mkdir(parents=True)
        device = tree.base_device = tree.sys / "devices/pci0000:00/0000:00:02.0"
        (device / "driver").parent.mkdir(parents=True)
        (device / "driver").symlink_to(tree.sys / "bus/pci/drivers/i915")
        card = tree.sys / "class/drm/card0"
        card.mkdir(parents=True)
        (card / "device").symlink_to(device)
        (tree.proc / "sys/kernel/random/boot_id").unlink()
        sample, stderr = tree.run()
        self.assertTrue(sample["gpuIntel"])
        self.assertEqual(sample["gpuError"], "Intel GPU readings unavailable. Retrying…")
        self.assertIn("Intel telemetry:", stderr, "the lock must not swallow the script's stderr")

    def test_intel_gpu_in_a_nonzero_pci_domain(self):
        """An Intel GPU outside PCI domain 0000 (multi-domain, VMD) is still found."""
        tree = self.tree
        (tree.sys / "bus/pci/drivers/xe/0001:03:00.0").mkdir(parents=True)
        for name in ("bind", "unbind", "new_id", "uevent"):
            (tree.sys / "bus/pci/drivers/xe" / name).touch()
        device = tree.sys / "devices/pci0001:00/0001:03:00.0"
        device.mkdir(parents=True)
        (device / "driver").symlink_to(tree.sys / "bus/pci/drivers/xe")
        card = tree.sys / "class/drm/card0"
        (card / "gt/gt0").mkdir(parents=True)
        (card / "device").symlink_to(device)
        (card / "gt/gt0/rps_act_freq_mhz").write_text("800\n")
        sample, _ = tree.run("--bar")
        self.assertTrue(sample["gpuIntel"])
        self.assertEqual(sample["gpuFrequencyMHz"], 800)

    def test_driver_control_files_are_not_a_bound_intel_gpu(self):
        tree = self.tree
        driver = tree.sys / "bus/pci/drivers/i915"
        driver.mkdir(parents=True)
        for name in ("bind", "unbind", "new_id", "remove_id", "uevent", "module"):
            (driver / name).touch()
        sample, _ = tree.run("--bar")
        self.assertFalse(sample["gpuIntel"])

    def test_state_directory_rejects_unsafe_fallbacks(self):
        os.chmod(self.tree.runtime, 0o755)
        target = self.base / "elsewhere"
        target.mkdir()
        (self.tree.runtime / f"foamy-vitals-{os.geteuid()}").symlink_to(target)
        sample, _ = self.tree.run()
        self.assertEqual(sample["mode"], "full")
        self.assertEqual(list(target.iterdir()), [], "never writes through a planted symlink")


class RealSystemTest(unittest.TestCase):
    def test_real_process_with_control_characters(self):
        """The bug as reported: a busy process renames itself with a control byte."""
        runtime = Path(tempfile.mkdtemp(prefix="foamy-vitals-real-"))
        os.chmod(runtime, 0o700)
        burner = subprocess.Popen([sys.executable, "-I", "-c", textwrap.dedent("""\
            import sys
            open("/proc/self/comm", "wb").write(b"evil\\x01na\\"me")
            print("ready", flush=True)
            while True: pass
            """)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(burner.stdout.readline().strip(), "ready")
            env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime))
            subprocess.run(HELPER, env=env, capture_output=True, check=True)
            time.sleep(2.2)
            output = subprocess.run(HELPER, env=env, capture_output=True, check=True).stdout
            sample = strict(output.decode("utf-8", "replace"))
            names = [entry["name"] for entry in sample["processes"] + sample["memoryProcesses"]]
            self.assertIn("evilname", names, "the burner is the busiest process; it must be listed, cleaned")
        finally:
            burner.send_signal(signal.SIGKILL)
            burner.wait()
            burner.stdout.close()
            shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
