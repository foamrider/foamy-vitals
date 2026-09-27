import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("intel_stats", Path(__file__).resolve().parents[1] / "intel_stats.py")
assert SPEC and SPEC.loader
INTEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INTEL)
DEVICE = "0000:00:02.0"


def fdinfo(ident=1, render=100, video=0):
    return f'''drm-driver: i915
drm-pdev: {DEVICE}
drm-client-id: {ident}
drm-engine-render: {render} ns
drm-engine-video: {video} ns
drm-engine-capacity-video: 2
drm-total-system0: 64 MiB
drm-shared-system0: 1024 KiB
'''


def snapshot(at, *records):
    return dict(device=DEVICE, boot="boot", time=at,
                clients=dict(INTEL.parse_client(record, DEVICE) for record in records))


class IntelTest(unittest.TestCase):
    def test_engine_deltas_capacity_and_private_memory(self):
        before = snapshot(1_000_000_000, fdinfo())
        after = snapshot(2_000_000_000, fdinfo(render=500_000_100, video=1_000_000_000))
        self.assertEqual(INTEL.utilization(after, before), {"render": 50, "video": 50})
        self.assertEqual(after["clients"]["1"]["private"], 63 * 1048576)

    def test_cold_start_reboot_device_change_and_stale_state(self):
        current = snapshot(40_000_000_000, fdinfo())
        for previous in (None, snapshot(1, fdinfo()), copy.deepcopy(current),
                         dict(current, boot="older"), dict(current, device="0000:01:00.0")):
            self.assertEqual(INTEL.utilization(current, previous), {})

    def test_client_churn_does_not_count_lifetime_usage(self):
        before = snapshot(1_000_000_000, fdinfo(), fdinfo(2))
        after = snapshot(2_000_000_000, fdinfo(render=100_000_100), fdinfo(3, render=9_000_000_000))
        self.assertEqual(INTEL.utilization(after, before)["render"], 10)

    def test_counter_regression_retains_high_water_mark(self):
        before = snapshot(1_000_000_000, fdinfo(render=500_000_000))
        after = snapshot(2_000_000_000, fdinfo(render=400_000_000))
        self.assertEqual(INTEL.utilization(after, before)["render"], 0)
        next_sample = snapshot(3_000_000_000, fdinfo(render=600_000_000))
        self.assertEqual(INTEL.utilization(next_sample, after)["render"], 10)

    def test_malformed_and_unsupported_fields(self):
        for text in (fdinfo().replace("i915", "amdgpu"), fdinfo().replace(DEVICE, "0000:01:00.0"),
                     fdinfo().replace("drm-client-id: 1", "drm-client-id: bad")):
            self.assertIsNone(INTEL.parse_client(text, DEVICE))
        for text in (fdinfo().replace("100 ns", "-1 ns"), fdinfo().replace("capacity-video: 2", "capacity-video: 0")):
            with self.assertRaises(ValueError):
                INTEL.parse_client(text, DEVICE)
        for text in (fdinfo().replace("1024 KiB", "65 MiB"), fdinfo().replace("1024 KiB", "bad"),
                     fdinfo().replace("drm-shared-system0: 1024 KiB", "")):
            self.assertIsNone(INTEL.parse_client(text, DEVICE)[1]["private"])
        self.assertIsNotNone(INTEL.parse_client(fdinfo().replace("i915", "xe"), DEVICE))

    def test_shared_descriptors_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid in (1, 2):
                fds = proc / str(pid) / "fdinfo"
                fds.mkdir(parents=True)
                (fds / "3").write_text(fdinfo())
                (fds / "4").write_text(fdinfo())
            self.assertEqual(len(INTEL.read_clients(DEVICE, proc)), 1)
            with patch.object(INTEL.time, "monotonic", side_effect=[0, 2]):
                with self.assertRaises(TimeoutError):
                    INTEL.read_clients(DEVICE, proc)

    def test_collect_state_and_failure_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drm, proc = root / "drm", root / "proc"
            device = root / DEVICE
            driver = root / "i915"
            driver.mkdir()
            device.mkdir()
            (device / "driver").symlink_to(driver)
            card = drm / "card1"
            card.mkdir(parents=True)
            (card / "device").symlink_to(device)
            gt = card / "gt/gt0"
            gt.mkdir(parents=True)
            (gt / "rps_act_freq_mhz").write_text("800\n")
            boot = proc / "sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True)
            boot.write_text("boot")
            fds = proc / "1/fdinfo"
            fds.mkdir(parents=True)
            (fds / "3").write_text(fdinfo())
            state = root / "intel.json"
            with patch.object(INTEL.time, "monotonic_ns", return_value=1_000_000_000):
                first = INTEL.collect(state, drm, proc)
            self.assertTrue(first["gpuIntel"])
            self.assertIsNone(first["gpuBusy"])
            self.assertEqual(first["gpuFrequencyMHz"], 800)
            self.assertEqual(first["gpuMemoryPrivate"], 63 * 1048576)
            self.assertEqual(first["gpuThermals"], [])
            sensor = device / "hwmon/hwmon1"
            sensor.mkdir(parents=True)
            (sensor / "temp1_input").write_text("45000")
            (sensor / "temp1_crit").write_text("95000")
            (sensor / "temp1_label").write_text("GPU")
            (fds / "3").write_text(fdinfo(render=500_000_100))
            with patch.object(INTEL.time, "monotonic_ns", return_value=2_000_000_000):
                active = INTEL.collect(state, drm, proc)
                self.assertEqual(active["gpuBusy"], 50)
                self.assertEqual(active["gpuThermals"], [{"temperature": 45, "critical": 95, "label": "GPU"}])
            with patch.object(INTEL, "read_clients", side_effect=TimeoutError("scan budget")):
                failed = INTEL.collect(state, drm, proc)
                self.assertIsNone(failed["gpuBusy"])
                self.assertTrue(failed["gpuError"])
            for invalid in ('broken', '{}', '{"device":"x","boot":"b","time":true,"clients":{}}'):
                state.write_text(invalid)
                self.assertIsNone(INTEL.load_snapshot(state))
                self.assertIsNone(INTEL.collect(state, drm, proc)["gpuBusy"])
            (card / "device").unlink()
            self.assertFalse(INTEL.collect(state, drm, proc)["gpuIntel"])

    def test_changed_capacity_and_missing_counters_are_not_invented(self):
        before = snapshot(1_000_000_000, fdinfo())
        changed = fdinfo(video=1_000_000_000).replace("capacity-video: 2", "capacity-video: 1")
        self.assertNotIn("video", INTEL.utilization(snapshot(2_000_000_000, changed), before))
        self.assertEqual(INTEL.utilization(snapshot(2_000_000_000), before), {})
        # Separate clients on the same engine contribute once each.
        before = snapshot(1_000_000_000, fdinfo(1), fdinfo(2))
        after = snapshot(2_000_000_000, fdinfo(1, render=200_000_100), fdinfo(2, render=300_000_100))
        self.assertEqual(INTEL.utilization(after, before)["render"], 50)


if __name__ == "__main__":
    unittest.main()
