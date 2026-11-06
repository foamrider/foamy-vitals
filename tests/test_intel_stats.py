"""Intel GPU readings (DRM fdinfo of this user's processes), black-box.

  python3 -I tests/test_intel_stats.py

The helper runs as a process against a fake tree (tests/fakesys.py) with a
bound i915/xe device. Engine usage is a delta against the baseline kept in the
state directory (intel.json, CLOCK_MONOTONIC nanoseconds); the tests write that
baseline themselves, a known time ago, so the expected percentages are exact up
to the few milliseconds the helper takes to start.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakesys import INTEL_DEVICE, Tree, intel_fdinfo as fdinfo  # noqa: E402

UNAVAILABLE = "Intel GPU readings unavailable. Retrying…"
SECOND = 1_000_000_000


def client(render=100, video=0, capacity=2, private=63 * 1048576):
    return {"engines": {"render": render, "video": video}, "capacities": {"video": capacity}, "private": private}


class IntelTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="foamy-vitals-intel-"))
        self.tree = Tree(self.base)
        self.card, self.device = self.tree.intel()
        self.clock = 0

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def later(self):
        """Move the sample clock past the 2 s sample cache."""
        self.clock += 1
        self.tree.advance(self.clock, 3 * self.clock)

    def sample(self):
        sample, stderr = self.tree.run("--bar")
        self.assertTrue(sample["gpuIntel"], sample)
        # The Intel path never reports VRAM or a suspended GPU.
        self.assertEqual((sample["vramUsed"], sample["vramTotal"], sample["gpuSuspended"]), (None, None, False))
        return sample, stderr

    def saved(self):
        return json.loads((self.tree.state / "intel.json").read_text())

    def assertClose(self, value, expected):
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, expected, delta=1.0)

    def test_engine_deltas_capacity_and_private_memory(self):
        self.tree.descriptor(41, 3, fdinfo(render=500_000_100, video=1_000_000_000))
        self.tree.intel_state(10 * SECOND, {"1": client()})
        sample, stderr = self.sample()
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(sorted(sample["gpuEngines"]), ["render", "video"])
        self.assertClose(sample["gpuEngines"]["render"], 5.0)
        self.assertClose(sample["gpuEngines"]["video"], 5.0)
        self.assertClose(sample["gpuBusy"], 5.0)
        self.assertEqual(sample["gpuMemoryPrivate"], 63 * 1048576)
        self.assertEqual(sample["gpuError"], "")
        saved = self.saved()
        self.assertEqual(saved["device"], INTEL_DEVICE)
        self.assertEqual(saved["boot"], "boot-1")
        self.assertEqual(saved["clients"], {"1": {"engines": {"render": 500_000_100, "video": 1_000_000_000},
                                                  "capacities": {"video": 2}, "private": 63 * 1048576}})

    def test_busy_engine_is_capped_at_100(self):
        self.tree.descriptor(41, 3, fdinfo(render=90 * SECOND))
        self.tree.intel_state(2 * SECOND, {"1": client(render=0)})
        sample, _ = self.sample()
        self.assertEqual(sample["gpuEngines"], {"render": 100.0, "video": 0.0})
        self.assertEqual(sample["gpuBusy"], 100.0)

    def test_cold_start_reboot_device_change_and_stale_state(self):
        self.tree.descriptor(41, 3, fdinfo(render=5 * SECOND))
        sample, _ = self.sample()
        self.assertEqual((sample["gpuBusy"], sample["gpuEngines"]), (None, {}), "no baseline yet")
        self.assertEqual(self.saved()["clients"]["1"]["engines"]["render"], 5 * SECOND)
        for ago, kwargs in ((40 * SECOND, {}), (2 * SECOND, {"boot": "older"}),
                            (2 * SECOND, {"device": "0000:01:00.0"}), (-SECOND, {})):
            self.later()
            self.tree.intel_state(ago, {"1": client(render=0)}, **kwargs)
            sample, _ = self.sample()
            self.assertEqual((sample["gpuBusy"], sample["gpuEngines"]), (None, {}), kwargs or ago)

    def test_client_churn_does_not_count_lifetime_usage(self):
        self.tree.descriptor(41, 3, fdinfo(1, render=1_000_000_100))
        self.tree.descriptor(41, 4, fdinfo(3, render=9_000_000_000))
        self.tree.intel_state(10 * SECOND, {"1": client(), "2": client()})
        sample, _ = self.sample()
        self.assertClose(sample["gpuEngines"]["render"], 10.0)

    def test_counter_regression_retains_high_water_mark(self):
        self.tree.descriptor(41, 3, fdinfo(render=400_000_000))
        self.tree.intel_state(10 * SECOND, {"1": client(render=500_000_000)})
        sample, _ = self.sample()
        self.assertEqual(sample["gpuEngines"]["render"], 0.0)
        self.assertEqual(self.saved()["clients"]["1"]["engines"]["render"], 500_000_000, "the baseline keeps the high-water mark")

    def test_separate_clients_on_one_engine_add_up(self):
        self.tree.descriptor(41, 3, fdinfo(1, render=2 * SECOND + 100))
        self.tree.descriptor(42, 3, fdinfo(2, render=3 * SECOND + 100))
        self.tree.intel_state(10 * SECOND, {"1": client(), "2": client()})
        sample, _ = self.sample()
        self.assertClose(sample["gpuEngines"]["render"], 50.0)
        self.assertEqual(sample["gpuMemoryPrivate"], 2 * 63 * 1048576)

    def test_changed_capacity_and_missing_counters_are_not_invented(self):
        self.tree.descriptor(41, 3, fdinfo(video=SECOND).replace("capacity-video: 2", "capacity-video: 1"))
        self.tree.intel_state(10 * SECOND, {"1": client()})
        sample, _ = self.sample()
        self.assertNotIn("video", sample["gpuEngines"])
        self.assertIn("render", sample["gpuEngines"])

    def test_unrelated_clients_are_ignored(self):
        for number, text in enumerate((fdinfo(1, driver="amdgpu"), fdinfo(2, device="0000:01:00.0"),
                                       fdinfo().replace("drm-client-id: 1", "drm-client-id: bad")), 3):
            self.tree.descriptor(41, number, text)
        sample, stderr = self.sample()
        self.assertEqual((sample["gpuMemoryPrivate"], sample["gpuError"]), (None, ""))
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(self.saved()["clients"], {})

    def test_xe_clients_count(self):
        self.tree.descriptor(41, 3, fdinfo(driver="xe"))
        sample, _ = self.sample()
        self.assertEqual(sample["gpuMemoryPrivate"], 63 * 1048576)

    def test_malformed_counters_fail_the_sample(self):
        for text in (fdinfo().replace("100 ns", "-1 ns"), fdinfo().replace("capacity-video: 2", "capacity-video: 0")):
            self.later()
            self.tree.descriptor(41, 3, text)
            sample, stderr = self.sample()
            self.assertEqual((sample["gpuBusy"], sample["gpuEngines"], sample["gpuThermals"]), (None, {}, []))
            self.assertEqual(sample["gpuError"], UNAVAILABLE)
            self.assertIn("Intel telemetry:", stderr)

    def test_unknown_private_memory(self):
        for number, text in enumerate((fdinfo().replace("1024 KiB", "65 MiB"), fdinfo().replace("1024 KiB", "bad"),
                                       fdinfo().replace("drm-shared-system0: 1024 KiB", "")), 1):
            self.later()
            self.tree.descriptor(40 + number, 3, text.replace("drm-client-id: 1", f"drm-client-id: {number}"))
            sample, _ = self.sample()
            self.assertIsNone(sample["gpuMemoryPrivate"], text)
        self.assertEqual([c["private"] for c in self.saved()["clients"].values()], [None, None, None])

    def test_shared_descriptors_are_deduplicated(self):
        for pid in (41, 42):
            self.tree.descriptor(pid, 3, fdinfo())
            self.tree.descriptor(pid, 4, fdinfo())
        sample, _ = self.sample()
        self.assertEqual(sample["gpuMemoryPrivate"], 63 * 1048576)
        self.assertEqual(list(self.saved()["clients"]), ["1"])

    def test_only_dri_descriptors_are_read(self):
        # fdinfo of a socket or a plain file is never opened: here it is a
        # directory, which would fail the whole sample if it were read.
        self.tree.descriptor(41, 5, None, target="socket:[1234]")
        self.tree.descriptor(41, 6, None, target="/home/user/notes.txt")
        self.tree.descriptor(41, 7, fdinfo(9), target="/dev/dri/card1")
        sample, stderr = self.sample()
        self.assertEqual(sample["gpuError"], "")
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(list(self.saved()["clients"]), ["9"])

    def test_other_users_processes_are_skipped(self):
        if os.geteuid() == 0:
            self.skipTest("root owns everything")
        self.tree.descriptor(41, 3, fdinfo())
        # A process directory owned by someone else is not scanned: /proc/1 is root's.
        (self.tree.proc / "1").symlink_to("/proc/1")
        sample, stderr = self.sample()
        self.assertEqual(sample["gpuError"], "")
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(list(self.saved()["clients"]), ["1"])

    def test_frequency_thermals_and_state_recovery(self):
        self.tree.descriptor(41, 3, fdinfo())
        for gt, mhz in (("gt0", 800), ("gt1", 1250)):
            (self.card / "gt" / gt).mkdir(parents=True)
            (self.card / "gt" / gt / "rps_act_freq_mhz").write_text(f"{mhz}\n")
        sample, _ = self.sample()
        self.assertEqual(sample["gpuFrequencyMHz"], 1250)
        self.assertEqual((sample["gpuThermals"], sample["gpuTemp"]), ([], None))
        sensor = self.device / "hwmon/hwmon1"
        sensor.mkdir(parents=True)
        (sensor / "temp1_input").write_text("45000")
        (sensor / "temp1_crit").write_text("95000")
        (sensor / "temp1_label").write_text("GPU")
        (sensor / "temp2_input").write_text("51500\n")
        (sensor / "temp2_crit").write_text("0\n")
        (sensor / "temp2_label").write_text("  vram °\n")
        self.later()
        sample, _ = self.sample()
        thermals = sorted(sample["gpuThermals"], key=lambda t: t["temperature"])
        self.assertEqual(thermals, [{"temperature": 45, "critical": 95, "label": "GPU"},
                                    {"temperature": 51.5, "critical": None, "label": "vram °"}])
        self.assertEqual(sample["gpuTemp"], 51.5)
        for invalid in ("broken", "{}", '{"device":"x","boot":"b","time":true,"clients":{}}'):
            (self.tree.state / "intel.json").write_text(invalid)
            self.later()
            sample, _ = self.sample()
            self.assertIsNone(sample["gpuBusy"])
            self.assertEqual(sample["gpuError"], "", "an unusable baseline is replaced, not an error")
        (self.card / "device").unlink()
        self.later()
        sample, _ = self.tree.run("--bar")
        self.assertFalse(sample["gpuIntel"], "a bound driver without a DRM card is not an Intel GPU")

    def test_missing_boot_id_is_reported(self):
        (self.tree.proc / "sys/kernel/random/boot_id").unlink()
        sample, stderr = self.sample()
        self.assertEqual(sample["gpuError"], UNAVAILABLE)
        self.assertIn("Intel telemetry:", stderr)

    def test_lowest_pci_address_wins(self):
        self.tree.intel(driver="xe", device="0000:00:01.0", card="card0")
        for card, mhz in (("card0", 300), ("card1", 900)):
            gt = self.tree.sys / "class/drm" / card / "gt/gt0"
            gt.mkdir(parents=True)
            (gt / "rps_act_freq_mhz").write_text(f"{mhz}\n")
        sample, _ = self.sample()
        self.assertEqual(sample["gpuFrequencyMHz"], 300)
        self.assertEqual(self.saved()["device"], "0000:00:01.0")


if __name__ == "__main__":
    unittest.main()
