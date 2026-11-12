"""Intel GPU readings (DRM fdinfo of this user's processes), black-box.

  python3 -I tests/test_intel_stats.py

The helper runs as a process against a fake tree (tests/fakesys.py) with bound
i915/xe devices, each with a DRM card and render node. One scan of /proc
attributes every DRM client to its GPU; a descriptor's fdinfo is opened only
once its /dev/dri node resolves to a GPU being read. Engine usage is a delta
against each GPU's baseline in the state directory (intel-<ID>.json,
CLOCK_MONOTONIC nanoseconds); the tests write that baseline themselves, a known
time ago, so the expected percentages are exact up to the few milliseconds the
helper takes to start. The sample shows the busiest engine (busy).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakesys import FIFO, INTEL_DEVICE, Tree, intel_fdinfo as fdinfo  # noqa: E402

UNAVAILABLE = "Intel GPU readings unavailable. Retrying…"
SECOND = 1_000_000_000
OTHER = "0000:03:00.0"
DEVICE_KEYS = ["id", "vendor", "driver", "name", "type", "asleep", "busy", "vramUsed", "vramTotal",
               "memoryPrivate", "frequencyMHz", "thermals", "error"]


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
        """Move the sample clock past the sample cache."""
        self.clock += 1
        self.tree.advance(self.clock, 3 * self.clock)

    def gpus(self, timeout=20):
        sample, stderr = self.tree.run("--bar", timeout=timeout)
        gpus = {gpu["id"]: gpu for gpu in sample["gpus"]}
        for gpu in gpus.values():
            self.assertEqual(list(gpu), DEVICE_KEYS)
            if gpu["vendor"] == "intel":
                # The Intel path never reports VRAM.
                self.assertEqual((gpu["vramUsed"], gpu["vramTotal"]), (None, None))
        return gpus, stderr

    def sample(self, device=INTEL_DEVICE):
        gpus, stderr = self.gpus()
        return gpus[device], stderr

    def saved(self, device=INTEL_DEVICE):
        return json.loads((self.tree.state / f"intel-{device}.json").read_text())

    def assertClose(self, value, expected):
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, expected, delta=1.0)

    def test_engine_deltas_capacity_and_private_memory(self):
        self.tree.descriptor(41, 3, fdinfo(render=500_000_100, video=2_000_000_000))
        self.tree.intel_state(10 * SECOND, {"1": client()})
        gpu, stderr = self.sample()
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual((gpu["vendor"], gpu["driver"], gpu["type"]), ("intel", "i915", "integrated"))
        # render 5 %, video 2 s on two engines over 10 s: 10 %.
        self.assertClose(gpu["busy"], 10.0)
        self.assertEqual(gpu["memoryPrivate"], 63 * 1048576)
        self.assertEqual(gpu["error"], "")
        saved = self.saved()
        self.assertEqual(saved["device"], INTEL_DEVICE)
        self.assertEqual(saved["boot"], "boot-1")
        self.assertEqual(saved["clients"], {"1": {"engines": {"render": 500_000_100, "video": 2_000_000_000},
                                                  "capacities": {"video": 2}, "private": 63 * 1048576}})

    def test_busy_engine_is_capped_at_100(self):
        self.tree.descriptor(41, 3, fdinfo(render=90 * SECOND))
        self.tree.intel_state(2 * SECOND, {"1": client(render=0)})
        gpu, _ = self.sample()
        self.assertEqual(gpu["busy"], 100.0)

    def test_cold_start_reboot_device_change_and_stale_state(self):
        self.tree.descriptor(41, 3, fdinfo(render=5 * SECOND))
        gpu, _ = self.sample()
        self.assertIsNone(gpu["busy"], "no baseline yet")
        self.assertEqual(self.saved()["clients"]["1"]["engines"]["render"], 5 * SECOND)
        for ago, kwargs in ((40 * SECOND, {}), (2 * SECOND, {"boot": "older"}), (-SECOND, {})):
            self.later()
            self.tree.intel_state(ago, {"1": client(render=0)}, **kwargs)
            gpu, _ = self.sample()
            self.assertIsNone(gpu["busy"], kwargs or ago)
        # Another device's baseline in this GPU's file counts nothing either.
        self.later()
        self.tree.intel_state(2 * SECOND, {"1": client(render=0)}, device=OTHER)
        os.replace(self.tree.state / f"intel-{OTHER}.json", self.tree.state / f"intel-{INTEL_DEVICE}.json")
        self.assertIsNone(self.sample()[0]["busy"])

    def test_client_churn_does_not_count_lifetime_usage(self):
        self.tree.descriptor(41, 3, fdinfo(1, render=1_000_000_100))
        self.tree.descriptor(41, 4, fdinfo(3, render=9_000_000_000))
        self.tree.intel_state(10 * SECOND, {"1": client(), "2": client()})
        gpu, _ = self.sample()
        self.assertClose(gpu["busy"], 10.0)

    def test_counter_regression_retains_high_water_mark(self):
        self.tree.descriptor(41, 3, fdinfo(render=400_000_000))
        self.tree.intel_state(10 * SECOND, {"1": client(render=500_000_000)})
        gpu, _ = self.sample()
        self.assertEqual(gpu["busy"], 0.0)
        self.assertEqual(self.saved()["clients"]["1"]["engines"]["render"], 500_000_000, "the baseline keeps the high-water mark")

    def test_separate_clients_on_one_engine_add_up(self):
        self.tree.descriptor(41, 3, fdinfo(1, render=2 * SECOND + 100))
        self.tree.descriptor(42, 3, fdinfo(2, render=3 * SECOND + 100))
        self.tree.intel_state(10 * SECOND, {"1": client(), "2": client()})
        gpu, _ = self.sample()
        self.assertClose(gpu["busy"], 50.0)
        self.assertEqual(gpu["memoryPrivate"], 2 * 63 * 1048576)

    def test_changed_capacity_and_missing_counters_are_not_invented(self):
        self.tree.descriptor(41, 3, fdinfo(video=5 * SECOND).replace("capacity-video: 2", "capacity-video: 1"))
        self.tree.intel_state(10 * SECOND, {"1": client()})
        gpu, _ = self.sample()
        self.assertEqual(gpu["busy"], 0.0, "the video engine changed capacity: only render counts")

    def test_unrelated_clients_are_ignored(self):
        for number, text in enumerate((fdinfo(1, driver="amdgpu"), fdinfo(2, device="0000:01:00.0"),
                                       fdinfo().replace("drm-client-id: 1", "drm-client-id: bad")), 3):
            self.tree.descriptor(41, number, text)
        gpu, stderr = self.sample()
        self.assertEqual((gpu["memoryPrivate"], gpu["error"]), (None, ""))
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(self.saved()["clients"], {})

    def test_xe_clients_count(self):
        self.tree.descriptor(41, 3, fdinfo(driver="xe"))
        gpu, _ = self.sample()
        self.assertEqual(gpu["memoryPrivate"], 63 * 1048576)

    def test_malformed_counters_fail_the_sample(self):
        for text in (fdinfo().replace("100 ns", "-1 ns"), fdinfo().replace("capacity-video: 2", "capacity-video: 0")):
            self.later()
            self.tree.descriptor(41, 3, text)
            gpu, stderr = self.sample()
            self.assertEqual((gpu["busy"], gpu["memoryPrivate"], gpu["frequencyMHz"], gpu["thermals"]), (None, None, None, []))
            self.assertEqual(gpu["error"], UNAVAILABLE)
            self.assertIn("Intel telemetry:", stderr)

    def test_unknown_private_memory(self):
        for number, text in enumerate((fdinfo().replace("1024 KiB", "65 MiB"), fdinfo().replace("1024 KiB", "bad"),
                                       fdinfo().replace("drm-shared-system0: 1024 KiB", "")), 1):
            self.later()
            self.tree.descriptor(40 + number, 3, text.replace("drm-client-id: 1", f"drm-client-id: {number}"))
            gpu, _ = self.sample()
            self.assertIsNone(gpu["memoryPrivate"], text)
        self.assertEqual([c["private"] for c in self.saved()["clients"].values()], [None, None, None])

    def test_shared_descriptors_are_deduplicated(self):
        for pid in (41, 42):
            self.tree.descriptor(pid, 3, fdinfo())
            self.tree.descriptor(pid, 4, fdinfo())
        gpu, _ = self.sample()
        self.assertEqual(gpu["memoryPrivate"], 63 * 1048576)
        self.assertEqual(list(self.saved()["clients"]), ["1"])

    def test_only_dri_descriptors_are_read(self):
        # fdinfo of a socket or a plain file is never opened: here it is a
        # directory, which would fail the whole sample if it were read.
        self.tree.descriptor(41, 5, None, target="socket:[1234]")
        self.tree.descriptor(41, 6, None, target="/home/user/notes.txt")
        self.tree.descriptor(41, 7, fdinfo(9), target="/dev/dri/card1")
        gpu, stderr = self.sample()
        self.assertEqual(gpu["error"], "")
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(list(self.saved()["clients"]), ["9"])

    def test_only_descriptors_of_gpus_being_read_are_opened(self):
        # Printing an xe fdinfo wakes its GPU, so an asleep or unbound device's
        # fdinfo must never be opened. Each one here is a FIFO: opening it blocks.
        self.tree.intel(driver="xe", device=OTHER, card="card2", status="suspended")
        self.tree.descriptor(41, 3, fdinfo())
        self.tree.descriptor(41, 4, FIFO, target="/dev/dri/renderD130")
        self.tree.descriptor(42, 5, FIFO, target="/dev/dri/card2")
        unbound = self.tree.sys / "devices/pci0000:00/0000:09:00.0"
        unbound.mkdir(parents=True)
        (self.tree.sys / "class/drm/renderD200").mkdir()
        (self.tree.sys / "class/drm/renderD200/device").symlink_to(unbound)
        self.tree.descriptor(42, 6, FIFO, target="/dev/dri/renderD200")
        (self.tree.sys / "class/drm/renderD201").mkdir()  # a node without a device link
        self.tree.descriptor(42, 7, FIFO, target="/dev/dri/renderD201")
        self.tree.descriptor(42, 8, FIFO, target="/dev/null")
        self.tree.descriptor(42, 9, FIFO, target="/dev/dri/by-path/pci-0000:00:02.0-render")
        gpus, stderr = self.gpus(timeout=5)
        self.assertEqual((gpus[INTEL_DEVICE]["memoryPrivate"], gpus[INTEL_DEVICE]["error"]), (63 * 1048576, ""))
        self.assertTrue(gpus[OTHER]["asleep"])
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(list(self.saved()["clients"]), ["1"])
        self.assertFalse((self.tree.state / f"intel-{OTHER}.json").exists(), "an asleep GPU keeps no baseline")

    def test_one_scan_attributes_clients_per_device(self):
        self.tree.intel(driver="xe", device=OTHER, card="card2")
        self.tree.descriptor(41, 3, fdinfo(1, render=500_000_100))
        self.tree.descriptor(42, 3, fdinfo(1, render=100, driver="xe", device=OTHER), target="/dev/dri/renderD130")
        self.tree.descriptor(42, 4, fdinfo(2, render=2_000_000_100, driver="xe", device=OTHER), target="/dev/dri/card2")
        self.tree.intel_state(10 * SECOND, {"1": client()})
        self.tree.intel_state(10 * SECOND, {"1": client(), "2": client()}, device=OTHER)
        gpus, _ = self.gpus()
        self.assertClose(gpus[INTEL_DEVICE]["busy"], 5.0)
        self.assertClose(gpus[OTHER]["busy"], 20.0)
        self.assertEqual((gpus[INTEL_DEVICE]["memoryPrivate"], gpus[OTHER]["memoryPrivate"]),
                         (63 * 1048576, 2 * 63 * 1048576))
        self.assertEqual(list(self.saved()["clients"]), ["1"])
        self.assertEqual(sorted(self.saved(OTHER)["clients"]), ["1", "2"])
        self.assertEqual(self.saved(OTHER)["device"], OTHER)

    def test_other_users_processes_are_skipped(self):
        if os.geteuid() == 0:
            self.skipTest("root owns everything")
        self.tree.descriptor(41, 3, fdinfo())
        # A process directory owned by someone else is not scanned: /proc/1 is root's.
        (self.tree.proc / "1").symlink_to("/proc/1")
        gpu, stderr = self.sample()
        self.assertEqual(gpu["error"], "")
        self.assertNotIn("Intel telemetry", stderr)
        self.assertEqual(list(self.saved()["clients"]), ["1"])

    def test_frequency_thermals_and_state_recovery(self):
        self.tree.descriptor(41, 3, fdinfo())
        for gt, mhz in (("gt0", 800), ("gt1", 1250)):
            (self.card / "gt" / gt).mkdir(parents=True)
            (self.card / "gt" / gt / "rps_act_freq_mhz").write_text(f"{mhz}\n")
        gpu, _ = self.sample()
        self.assertEqual(gpu["frequencyMHz"], 1250)
        self.assertEqual(gpu["thermals"], [])
        sensor = self.device / "hwmon/hwmon1"
        sensor.mkdir(parents=True)
        (sensor / "temp1_input").write_text("45000")
        (sensor / "temp1_crit").write_text("95000")
        (sensor / "temp1_label").write_text("GPU")
        (sensor / "temp2_input").write_text("51500\n")
        (sensor / "temp2_crit").write_text("0\n")
        (sensor / "temp2_label").write_text("  vram °\n")
        self.later()
        gpu, _ = self.sample()
        self.assertEqual(gpu["thermals"], [{"temperature": 45, "critical": 95, "label": "GPU"},
                                           {"temperature": 51.5, "critical": None, "label": "vram °"}])
        for invalid in ("broken", "{}", '{"device":"x","boot":"b","time":true,"clients":{}}'):
            (self.tree.state / f"intel-{INTEL_DEVICE}.json").write_text(invalid)
            self.later()
            gpu, _ = self.sample()
            self.assertIsNone(gpu["busy"])
            self.assertEqual(gpu["error"], "", "an unusable baseline is replaced, not an error")
        (self.card / "device").unlink()
        self.later()
        gpu, _ = self.sample()
        self.assertEqual((gpu["frequencyMHz"], gpu["thermals"], gpu["error"]), (1250, [], ""),
                         "sensors are read through the card's device link only")

    def test_one_failing_sensor_skips_only_that_sensor(self):
        for gt, content in (("gt0", "800\n"), ("gt1", None)):
            (self.card / "gt" / gt).mkdir(parents=True)
            if content is None:
                (self.card / "gt" / gt / "rps_act_freq_mhz").mkdir()  # unreadable frequency file
            else:
                (self.card / "gt" / gt / "rps_act_freq_mhz").write_text(content)
        sensor = self.device / "hwmon/hwmon1"
        sensor.mkdir(parents=True)
        (sensor / "temp1_input").write_text("45000")
        (sensor / "temp2_input").mkdir()  # unreadable sensor
        (sensor / "temp3_input").write_text("50000")
        (sensor / "temp3_crit").mkdir()  # unreadable limit: the sensor stays, without it
        (sensor / "temp3_label").mkdir()  # unreadable label: "GPU"
        gpu, stderr = self.sample()
        self.assertEqual((gpu["error"], gpu["frequencyMHz"]), ("", 800))
        self.assertEqual(gpu["thermals"], [{"temperature": 45.0, "critical": None, "label": "GPU"},
                                           {"temperature": 50.0, "critical": None, "label": "GPU"}])
        self.assertNotIn("Intel telemetry", stderr)

    def test_lowest_card_of_a_device_is_read(self):
        for number, mhz in ((3, 300), (1, 100)):
            gt = self.device / f"drm/card{number}/gt/gt0"
            gt.mkdir(parents=True)
            (gt / "rps_act_freq_mhz").write_text(f"{mhz}\n")
        (self.device / "drm/cardX").mkdir()
        (self.device / "drm/card1-eDP-1").mkdir()
        self.assertEqual(self.sample()[0]["frequencyMHz"], 100)
        shutil.rmtree(self.device / "drm")
        self.later()
        gpu, _ = self.sample()
        self.assertEqual((gpu["frequencyMHz"], gpu["error"]), (None, ""), "a GPU without a DRM card still reads")

    def test_missing_boot_id_is_reported(self):
        (self.tree.proc / "sys/kernel/random/boot_id").unlink()
        gpu, stderr = self.sample()
        self.assertEqual(gpu["error"], UNAVAILABLE)
        self.assertIn("Intel telemetry:", stderr)

    def test_every_intel_gpu_is_listed_in_address_order(self):
        self.tree.intel(driver="xe", device="0000:00:01.0", card="card0")
        gpus, _ = self.gpus()
        self.assertEqual(list(gpus), ["0000:00:02.0", "0000:00:01.0"], "i915 before xe, then by address")
        self.assertEqual((gpus["0000:00:01.0"]["type"], gpus["0000:00:02.0"]["type"]), ("discrete", "integrated"))


    def test_a_long_lock_wait_shortens_the_intel_wait(self):
        # The scan has 1.5 s from its start, but after a 4.2 s wait for the
        # state lock that would end past the Panel's `timeout 5s`: the
        # sample must reply in time, with the Intel fallback text.
        self.tree.descriptor(41, 3, FIFO)  # opening it blocks the scan
        self.tree.state.mkdir(mode=0o700)
        holder = subprocess.Popen(["flock", str(self.tree.state / "lock"), "sleep", "4.2"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.2)  # flock has the lock before the helper starts
        result = subprocess.run(["timeout", "--kill-after=1s", "5s", *self.tree.helper, "--bar"], env=self.tree.env(),
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, "killed by the wrapper")
        sample = json.loads(result.stdout)
        self.assertEqual(sample["gpus"][0]["error"], UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
