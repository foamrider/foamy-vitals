import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("gpu_stats", ROOT / "gpu_stats.py")
assert SPEC and SPEC.loader
GPU = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GPU)
VENDOR_IDS = {"amdgpu": "0x1002", "radeon": "0x1002", "i915": "0x8086", "xe": "0x8086",
              "nvidia": "0x10de", "nouveau": "0x10de"}
PCI_IDS = """# fixture
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t150e  Strix [Radeon 880M / 890M]
\t7590  Navi 44 [Radeon RX 9050 / 9060 XT]
\t\t1002 0001  Subsystem [Not This]
10de  NVIDIA Corporation
\t2786  AD104 [GeForce RTX 4070]
"""
SECOND = 1_000_000_000


def make_gpu(root, address, driver, class_="0x030000", control="on", status="active", delay="5000", files=None):
    device = root / "sys/bus/pci/devices" / address
    (device / "power").mkdir(parents=True)
    (device / "drm/card0").mkdir(parents=True)
    (device / "class").write_text(class_ + "\n")
    (device / "vendor").write_text(VENDOR_IDS.get(driver, "0x1af4") + "\n")
    (device / "device").write_text("0x0000\n")
    (device / "power/control").write_text(control + "\n")
    (device / "power/runtime_status").write_text(status + "\n")
    if delay is None:
        (device / "power/autosuspend_delay_ms").mkdir()  # reading raises, like EIO on real hardware
    else:
        (device / "power/autosuspend_delay_ms").write_text(delay + "\n")
    if driver:
        target = root / "sys/bus/pci/drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        (device / "driver").symlink_to(target)
    for name, content in (files or {}).items():
        path = device / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            path.mkdir()
        else:
            path.write_text(content)
    return device


def amd_files(busy, used, total, temps, discrete=False):
    files = {"gpu_busy_percent": f"{busy}\n", "mem_info_vram_used": f"{used}\n", "mem_info_vram_total": f"{total}\n"}
    if discrete:
        files["mem_info_vram_vendor"] = "samsung\n"
    for index, (label, value, critical) in enumerate(temps, 1):
        files[f"hwmon/hwmon0/temp{index}_input"] = f"{value}\n"
        if label:
            files[f"hwmon/hwmon0/temp{index}_label"] = label + "\n"
        if critical:
            files[f"hwmon/hwmon0/temp{index}_crit"] = f"{critical}\n"
    return files


class FakeNvidia:
    def __init__(self, devices=None):
        self.calls = []
        self.devices = devices or {}

    def __call__(self, addresses):
        self.calls.append(list(addresses))
        return {address: self.devices.get(address) or {
            "name": None, "busy": 1.0, "vramUsed": 2, "vramTotal": 4,
            "thermals": [{"temperature": 40.0, "critical": None, "label": "GPU"}], "error": ""}
            for address in addresses}


class GpuStatsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root / "sys"
        self.proc = self.root / "proc"
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-1\n")
        self.state = self.root / "state"
        self.state.mkdir()
        (self.sys / "bus/pci/devices").mkdir(parents=True)

    def collect(self, mode="closed", now=0, nvidia=None, pci_ids=None):
        return {gpu["id"]: gpu for gpu in GPU.collect(
            mode, self.state, sys_root=self.sys, proc=self.proc, pci_ids=pci_ids or self.root / "missing.ids",
            now_ns=now, nvidia=nvidia or FakeNvidia())}

    def test_discovers_only_gpu_drivers(self):
        for address, driver in (("0000:01:00.0", "amdgpu"), ("0000:02:00.0", "nvidia"),
                                ("0000:03:00.0", "nouveau"), ("0000:00:02.0", "i915"),
                                ("0000:04:00.0", "ast"), ("0000:06:00.0", "virtio-pci"),
                                ("0000:07:00.0", "vfio-pci"), ("0000:08:00.0", None)):
            make_gpu(self.root, address, driver)
        make_gpu(self.root, "0000:09:00.0", "amdgpu", class_="0x020000")
        cards = GPU.discover(self.sys)
        self.assertEqual([(card["id"], card["vendor"], card["driver"]) for card in cards],
                         [("0000:00:02.0", "intel", "i915"), ("0000:01:00.0", "amd", "amdgpu"),
                          ("0000:02:00.0", "nvidia", "nvidia"), ("0000:03:00.0", "nvidia", "nouveau")])
        self.assertEqual(cards[0]["card"].name, "card0")

    def test_amd_pair_reads_each_cards_own_hwmon(self):
        make_gpu(self.root, "0000:05:00.0", "amdgpu", files=amd_files(
            70, 1024, 8192, [("edge", 50000, 100000), ("junction", 61000, 110000), ("mem", 72000, None)], True))
        make_gpu(self.root, "0000:c1:00.0", "amdgpu", class_="0x038000", files=amd_files(
            12, 256, 512, [("edge", 57000, None)]))
        gpus = self.collect()
        dgpu, igpu = gpus["0000:05:00.0"], gpus["0000:c1:00.0"]
        self.assertEqual((dgpu["type"], dgpu["busy"], dgpu["vramUsed"], dgpu["vramTotal"]), ("discrete", 70, 1024, 8192))
        self.assertEqual(dgpu["thermals"], [{"temperature": 50.0, "critical": 100.0, "label": "Edge"},
                                            {"temperature": 61.0, "critical": 110.0, "label": "Hotspot"},
                                            {"temperature": 72.0, "critical": None, "label": "Memory"}])
        self.assertEqual((igpu["type"], igpu["busy"], igpu["vramUsed"], igpu["vramTotal"]), ("integrated", 12, 256, 512))
        self.assertEqual(igpu["thermals"], [{"temperature": 57.0, "critical": None, "label": "Edge"}])
        self.assertEqual((dgpu["vendor"], dgpu["driver"], dgpu["asleep"], dgpu["error"]), ("amd", "amdgpu", False, ""))

    def test_vendorless_dgpu_with_bar_window_is_discrete(self):
        # Mac Pro 6,1 FirePro D700 (Tahiti): no memory vendor, 256 MiB BAR of 6 GiB.
        tahiti = amd_files(3, 1024, 6442450944, [])
        tahiti.update({"mem_info_vis_vram_total": "268435456\n",
                       "mem_info_gtt_used": "100\n", "mem_info_gtt_total": "67514003456\n"})
        apu = amd_files(7, 491, 512, [])
        apu.update({"mem_info_vis_vram_total": "512\n"})
        make_gpu(self.root, "0000:02:00.0", "amdgpu", files=tahiti)
        make_gpu(self.root, "0000:c1:00.0", "amdgpu", class_="0x038000", files=apu)
        gpus = self.collect()
        dgpu, igpu = gpus["0000:02:00.0"], gpus["0000:c1:00.0"]
        self.assertEqual((dgpu["type"], dgpu["vramUsed"], dgpu["vramTotal"]), ("discrete", 1024, 6442450944))
        self.assertEqual(igpu["type"], "integrated")

    def test_apu_memory_adds_gtt_to_vram_carve_out(self):
        apu = amd_files(7, 491, 512, [])
        apu.update({"mem_info_gtt_used": "3700\n", "mem_info_gtt_total": "46800\n"})
        dgpu = amd_files(70, 1024, 8192, [], True)
        dgpu.update({"mem_info_gtt_used": "100\n", "mem_info_gtt_total": "46800\n"})
        make_gpu(self.root, "0000:c1:00.0", "amdgpu", class_="0x038000", files=apu)
        make_gpu(self.root, "0000:05:00.0", "amdgpu", files=dgpu)
        gpus = self.collect()
        self.assertEqual((gpus["0000:c1:00.0"]["vramUsed"], gpus["0000:c1:00.0"]["vramTotal"]), (4191, 47312))
        self.assertEqual((gpus["0000:05:00.0"]["vramUsed"], gpus["0000:05:00.0"]["vramTotal"]), (1024, 8192))

    def test_apu_without_gtt_keeps_vram_carve_out(self):
        make_gpu(self.root, "0000:c1:00.0", "amdgpu", class_="0x038000", files=amd_files(7, 491, 512, []))
        gpu = self.collect()["0000:c1:00.0"]
        self.assertEqual((gpu["vramUsed"], gpu["vramTotal"]), (491, 512))

    def test_suspended_device_is_not_read(self):
        make_gpu(self.root, "0000:05:00.0", "amdgpu", control="auto", status="suspended",
                 files={"gpu_busy_percent": None, "hwmon/hwmon0/temp1_input": None})
        make_gpu(self.root, "0000:01:00.0", "nvidia", control="auto", status="suspended")
        make_gpu(self.root, "0000:02:00.0", "nvidia")
        nvidia = FakeNvidia()
        for mode in ("closed", "open"):
            gpus = self.collect(mode, nvidia=nvidia)
            for address in ("0000:05:00.0", "0000:01:00.0"):
                gpu = gpus[address]
                self.assertTrue(gpu["asleep"])
                self.assertEqual((gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["thermals"], gpu["error"]),
                                 (None, None, None, [], ""))
            self.assertFalse(gpus["0000:02:00.0"]["asleep"])
        self.assertEqual(nvidia.calls, [["0000:02:00.0"], ["0000:02:00.0"]])

    def test_closed_mode_spaces_reads_for_autosuspend_devices(self):
        device = make_gpu(self.root, "0000:05:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2, [], True))
        card = GPU.discover(self.sys)[0]
        busy = device / "gpu_busy_percent"
        self.assertEqual(self.collect("closed", 0)["0000:05:00.0"]["busy"], 10)
        busy.write_text("50\n")
        self.assertEqual(self.collect("closed", 5 * SECOND)["0000:05:00.0"]["busy"], 10)
        self.assertEqual(self.collect("closed", 16 * SECOND)["0000:05:00.0"]["busy"], 50)
        self.assertFalse(GPU.poll_due(card, 0, 5 * SECOND, "closed"))
        self.assertFalse(GPU.poll_due(card, 0, 14 * SECOND, "closed"))  # 15 s floor beats 2 x 5 s
        self.assertTrue(GPU.poll_due(card, 0, 5 * SECOND, "open"))
        self.assertTrue(GPU.poll_due(card, None, 0, "closed"))
        busy.write_text("60\n")
        self.assertEqual(self.collect("open", 17 * SECOND)["0000:05:00.0"]["busy"], 60)

        (device / "power/autosuspend_delay_ms").unlink()
        (device / "power/autosuspend_delay_ms").mkdir()
        self.assertFalse(GPU.poll_due(card, 0, 29 * SECOND, "closed"))
        self.assertTrue(GPU.poll_due(card, 0, 30 * SECOND, "closed"))

        (device / "power/control").write_text("on\n")
        self.assertTrue(GPU.poll_due(card, 0, 1, "closed"))
        busy.write_text("70\n")
        self.assertEqual(self.collect("closed", 18 * SECOND)["0000:05:00.0"]["busy"], 70)

    def test_closed_mode_reads_integrated_gpus_every_call(self):
        # i915 and APUs autosuspend by default; spacing them would slow a lone iGPU's bar.
        device = make_gpu(self.root, "0000:c1:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2, []))
        make_gpu(self.root, "0000:00:02.0", "i915", control="auto")
        for second, busy in ((0, 10), (2, 20), (4, 30)):
            (device / "gpu_busy_percent").write_text(f"{busy}\n")
            gpu = self.collect("closed", second * SECOND)["0000:c1:00.0"]
            self.assertEqual((gpu["type"], gpu["busy"]), ("integrated", busy))
        for card in GPU.discover(self.sys):
            self.assertEqual(GPU.closed_interval(card), 0)
            self.assertTrue(GPU.poll_due(card, 0, 1, "closed"))

    def test_state_from_another_boot_or_malformed_is_ignored(self):
        device = make_gpu(self.root, "0000:05:00.0", "amdgpu", control="auto", files=amd_files(10, 1, 2, []))
        self.collect("closed", 0)
        (device / "gpu_busy_percent").write_text("50\n")
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-2\n")
        self.assertEqual(self.collect("closed", 1)["0000:05:00.0"]["busy"], 50)
        for text in ("not json", "[]", '{"boot":"boot-2","devices":{"0000:05:00.0":{"readAt":0,"reading":{"busy":NaN}}}}'):
            (self.state / "gpus.json").write_text(text)
            (device / "gpu_busy_percent").write_text("60\n")
            self.assertEqual(self.collect("closed", 2)["0000:05:00.0"]["busy"], 60)
        stored = json.loads((self.state / "gpus.json").read_text())
        self.assertEqual((stored["boot"], stored["devices"]["0000:05:00.0"]["readAt"]), ("boot-2", 2))

    def test_device_vanishing_mid_read_isolated(self):
        make_gpu(self.root, "0000:05:00.0", "amdgpu", files=amd_files(70, 1, 2, [("edge", 50000, None)]))
        make_gpu(self.root, "0000:c1:00.0", "amdgpu", files=amd_files(12, 1, 2, [("edge", 57000, None)]))
        real = GPU.read_sysfs

        def flaky(device, integrated=False):
            if device.name == "0000:05:00.0":
                raise FileNotFoundError(device / "gpu_busy_percent")
            return real(device, integrated)

        with patch.object(GPU, "read_sysfs", flaky), contextlib.redirect_stderr(io.StringIO()):
            gpus = self.collect()
        gone, other = gpus["0000:05:00.0"], gpus["0000:c1:00.0"]
        self.assertEqual((gone["busy"], gone["vramUsed"], gone["vramTotal"], gone["thermals"]), (None, None, None, []))
        self.assertNotEqual(gone["error"], "")
        self.assertEqual((other["busy"], other["thermals"][0]["temperature"], other["error"]), (12, 57.0, ""))

    def test_missing_vram_file_keeps_busy_and_temperatures(self):
        files = amd_files(40, 1, 2, [("edge", 50000, 100000)], True)
        del files["mem_info_vram_used"]
        make_gpu(self.root, "0000:05:00.0", "amdgpu", files=files)
        gpu = self.collect()["0000:05:00.0"]
        self.assertEqual((gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["error"]), (40, None, None, ""))
        self.assertEqual(gpu["thermals"], [{"temperature": 50.0, "critical": 100.0, "label": "Edge"}])

    def test_one_failing_sensor_file_skips_only_that_value(self):
        # A directory raises on read, like EIO/EINVAL from a single sysfs attribute.
        files = amd_files(40, 1, 2, [("edge", 50000, 100000), ("junction", 61000, None),
                                     ("mem", 72000, 95000), ("edge", 45000, None)], True)
        files["hwmon/hwmon0/temp1_input"] = None
        files["hwmon/hwmon0/temp3_label"] = None
        files["hwmon/hwmon0/temp3_crit"] = None
        files["hwmon/hwmon0/temp4_input"] = "garbage\n"
        make_gpu(self.root, "0000:05:00.0", "amdgpu", files=files)
        gpu = self.collect()["0000:05:00.0"]
        self.assertEqual((gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["error"]), (40, 1, 2, ""))
        self.assertEqual(gpu["thermals"], [{"temperature": 61.0, "critical": None, "label": "Hotspot"},
                                           {"temperature": 72.0, "critical": None, "label": "Sensor 3"}])

    def test_mixed_vendors_all_listed(self):
        make_gpu(self.root, "0000:00:02.0", "i915")
        make_gpu(self.root, "0000:01:00.0", "nvidia")
        make_gpu(self.root, "0000:05:00.0", "amdgpu", files=amd_files(33, 1, 2, [], True))
        nvidia = FakeNvidia({"0000:01:00.0": {
            "name": "NVIDIA GeForce RTX 4070", "busy": 88.0, "vramUsed": 3, "vramTotal": 12,
            "thermals": [{"temperature": 65.0, "critical": 93.0, "label": "GPU"}], "error": ""}})
        gpus = self.collect(nvidia=nvidia)
        self.assertEqual(list(gpus), ["0000:00:02.0", "0000:01:00.0", "0000:05:00.0"])
        intel, green, red = gpus.values()
        self.assertEqual(nvidia.calls, [["0000:01:00.0"]])
        self.assertEqual((green["vendor"], green["type"], green["busy"], green["vramUsed"], green["vramTotal"]),
                         ("nvidia", "discrete", 88.0, 3, 12))
        self.assertEqual((green["name"], green["shortName"]), ("NVIDIA GeForce RTX 4070", "RTX 4070"))
        self.assertEqual((intel["vendor"], intel["type"], intel["busy"], intel["error"]), ("intel", "integrated", None, ""))
        self.assertEqual((red["vendor"], red["type"], red["busy"]), ("amd", "discrete", 33))
        self.assertTrue((self.state / "intel-0000:00:02.0.json").exists())
        for gpu in gpus.values():
            self.assertEqual(set(gpu), {"id", "vendor", "driver", "name", "shortName", "type", "asleep", "busy",
                                        "vramUsed", "vramTotal", "memoryPrivate", "frequencyMHz", "thermals", "error"})

    def test_closed_mode_intel_keeps_its_baseline_across_spaced_reads(self):
        device = make_gpu(self.root, "0000:03:00.0", "xe", control="auto", delay=None)  # unreadable delay: 30 s gaps
        (self.sys / "class/drm/renderD128").mkdir(parents=True)
        (self.sys / "class/drm/renderD128/device").symlink_to(device)
        (self.proc / "100/fd").mkdir(parents=True)
        (self.proc / "100/fd/3").symlink_to("/dev/dri/renderD128")
        fds = self.proc / "100/fdinfo"
        fds.mkdir(parents=True)
        record = "drm-driver: xe\ndrm-pdev: 0000:03:00.0\ndrm-client-id: 7\ndrm-engine-render: {} ns\n"
        (fds / "3").write_text(record.format(0))
        with patch("time.monotonic_ns", return_value=0):
            self.assertIsNone(self.collect("closed", 0)["0000:03:00.0"]["busy"])
        (fds / "3").write_text(record.format(4 * SECOND))
        with patch("time.monotonic_ns", return_value=31 * SECOND):
            self.assertIsNone(self.collect("closed", 29 * SECOND)["0000:03:00.0"]["busy"])  # carried, not read
        with patch("time.monotonic_ns", return_value=40 * SECOND):
            gpu = self.collect("closed", 40 * SECOND)["0000:03:00.0"]
        self.assertEqual((gpu["busy"], gpu["error"]), (10.0, ""))

    def test_nouveau_row_has_temperature_only(self):
        make_gpu(self.root, "0000:01:00.0", "nouveau", files={"hwmon/hwmon3/temp1_input": "45000\n"})
        nvidia = FakeNvidia()
        gpu = self.collect(nvidia=nvidia)["0000:01:00.0"]
        self.assertEqual((gpu["vendor"], gpu["driver"], gpu["busy"], gpu["vramUsed"], gpu["vramTotal"], gpu["error"]),
                         ("nvidia", "nouveau", None, None, None, ""))
        self.assertEqual(gpu["thermals"], [{"temperature": 45.0, "critical": None, "label": "Sensor 1"}])
        self.assertEqual(nvidia.calls, [])

    def test_names_and_short_names(self):
        ids = self.root / "pci.ids"
        ids.write_text(PCI_IDS)
        self.assertEqual(GPU.gpu_name("1002", "7590", ids), "Radeon RX 9050 / 9060 XT")
        self.assertEqual(GPU.gpu_name("0x1002", "0x150E", ids), "Radeon 880M / 890M")
        self.assertIsNone(GPU.gpu_name("1002", "ffff", ids))
        self.assertIsNone(GPU.gpu_name("1002", "7590", self.root / "missing.ids"))
        card = {"id": "0000:05:00.0", "vendor": "amd", "driver": "amdgpu", "device": Path("/x"), "card": None}
        for name, short in (("Radeon RX 9050 / 9060 XT", "RX 9050 / 9060 XT"), ("Radeon 880M / 890M", "880M / 890M"),
                            ("NVIDIA GeForce RTX 4070", "RTX 4070"), (None, "AMD 05:00.0")):
            self.assertEqual(GPU.short_name(name, card), short)

        device = make_gpu(self.root, "0000:05:00.0", "amdgpu", files=amd_files(1, 1, 2, [], True))
        (device / "device").write_text("0x7590\n")
        gpu = self.collect(pci_ids=ids)["0000:05:00.0"]
        self.assertEqual((gpu["name"], gpu["shortName"]), ("Radeon RX 9050 / 9060 XT", "RX 9050 / 9060 XT"))
        ids.unlink()  # cached: pci.ids is only parsed for unseen devices
        self.assertEqual(self.collect(pci_ids=ids)["0000:05:00.0"]["name"], "Radeon RX 9050 / 9060 XT")
        (self.state / "gpus.json").unlink()
        gpu = self.collect(pci_ids=ids)["0000:05:00.0"]
        self.assertEqual((gpu["name"], gpu["shortName"]), (None, "AMD 05:00.0"))

    def test_output_is_strict_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "gpu_stats.py", "closed", temporary], cwd=ROOT, timeout=10,
                                    capture_output=True, text=True, env={**os.environ, "XDG_RUNTIME_DIR": temporary})
        self.assertEqual(result.returncode, 0, result.stderr)
        gpus = json.loads(result.stdout, parse_constant=lambda constant: self.fail(constant))
        self.assertIsInstance(gpus, list)
        for gpu in gpus:
            self.assertRegex(gpu["id"], r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
            self.assertIn(gpu["vendor"], ("amd", "intel", "nvidia"))
            self.assertIsInstance(gpu["thermals"], list)


if __name__ == "__main__":
    unittest.main()
