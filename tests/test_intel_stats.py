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


def open_drm(proc, sys_root, pid, fd, pci, text, node=None):
    """Give pid a descriptor on pci's render node; text None makes its fdinfo raise if read."""
    node = node or "renderD" + str(128 + int(pci[5:7], 16))
    for name in ("fd", "fdinfo"):
        (proc / str(pid) / name).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "fd" / str(fd)).symlink_to("/dev/dri/" + node)
    if text is None:
        (proc / str(pid) / "fdinfo" / str(fd)).mkdir()
    else:
        (proc / str(pid) / "fdinfo" / str(fd)).write_text(text)
    drm = sys_root / "class/drm" / node
    if not drm.exists():
        drm.mkdir(parents=True)
        (sys_root / "devices" / pci).mkdir(parents=True, exist_ok=True)
        (drm / "device").symlink_to(sys_root / "devices" / pci)


def snapshot(at, *records):
    return dict(device=DEVICE, boot="boot", time=at,
                clients={ident: client for _, ident, client in (INTEL.parse_client(record, {DEVICE}) for record in records)})


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

    def test_baseline_window_follows_the_poll_interval(self):
        before = snapshot(1_000_000_000, fdinfo())
        after = snapshot(41_000_000_000, fdinfo(render=4_000_000_100))
        self.assertEqual(INTEL.utilization(after, before), {})
        self.assertEqual(INTEL.utilization(after, before, window_ns=60_000_000_000), {"render": 10, "video": 0})

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
            self.assertIsNone(INTEL.parse_client(text, {DEVICE}))
        for text in (fdinfo().replace("100 ns", "-1 ns"), fdinfo().replace("capacity-video: 2", "capacity-video: 0")):
            with self.assertRaises(ValueError):
                INTEL.parse_client(text, {DEVICE})
        for text in (fdinfo().replace("1024 KiB", "65 MiB"), fdinfo().replace("1024 KiB", "bad"),
                     fdinfo().replace("drm-shared-system0: 1024 KiB", "")):
            self.assertIsNone(INTEL.parse_client(text, {DEVICE})[2]["private"])
        self.assertIsNotNone(INTEL.parse_client(fdinfo().replace("i915", "xe"), {DEVICE}))

    def test_shared_descriptors_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, sys_root = Path(directory) / "proc", Path(directory) / "sys"
            for pid in (1, 2):
                open_drm(proc, sys_root, pid, 3, DEVICE, fdinfo())
                open_drm(proc, sys_root, pid, 4, DEVICE, fdinfo())
            self.assertEqual(len(INTEL.read_clients({DEVICE}, proc, sys_root)[DEVICE]), 1)
            with patch.object(INTEL.time, "monotonic", side_effect=[0, 2]):
                with self.assertRaises(TimeoutError):
                    INTEL.read_clients({DEVICE}, proc, sys_root)

    def test_only_descriptors_of_requested_devices_are_read(self):
        # Printing an xe fdinfo wakes its GPU, so an asleep or unrequested device's
        # fdinfo must never be opened: here opening one raises.
        asleep = "0000:03:00.0"
        with tempfile.TemporaryDirectory() as directory:
            proc, sys_root = Path(directory) / "proc", Path(directory) / "sys"
            open_drm(proc, sys_root, 1, 3, DEVICE, fdinfo())
            open_drm(proc, sys_root, 1, 4, asleep, None)
            open_drm(proc, sys_root, 2, 5, asleep, None, node="card1")
            open_drm(proc, sys_root, 2, 6, "0000:09:00.0", None, node="renderD200")
            (sys_root / "class/drm/renderD200/device").unlink()  # node without a device link
            (proc / "2/fd/7").symlink_to("/dev/null")
            (proc / "2/fdinfo/7").mkdir()
            clients = INTEL.read_clients({DEVICE}, proc, sys_root)
            self.assertEqual(list(clients), [DEVICE])
            self.assertEqual(list(clients[DEVICE]), ["1"])

    def fake_system(self, root, devices):
        """Build a card, PCI device and proc tree for each (pci id, card number)."""
        drm, proc = root / "drm", root / "proc"
        driver = root / "i915"
        driver.mkdir(exist_ok=True)
        cards = {}
        for pci, number in devices:
            device = root / pci
            device.mkdir()
            (device / "driver").symlink_to(driver)
            card = drm / f"card{number}"
            card.mkdir(parents=True)
            (card / "device").symlink_to(device)
            (card / "gt/gt0").mkdir(parents=True)
            (card / "gt/gt0/rps_act_freq_mhz").write_text("800\n")
            cards[pci] = card
        boot = proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("boot")
        return cards, proc

    def test_one_scan_attributes_clients_per_device(self):
        other = "0000:03:00.0"
        with tempfile.TemporaryDirectory() as directory:
            proc, sys_root = Path(directory) / "proc", Path(directory) / "sys"
            for pid, pci, ident in ((1, DEVICE, 1), (2, other, 1)):
                open_drm(proc, sys_root, pid, 3, pci, fdinfo(ident).replace(DEVICE, pci))
            open_drm(proc, sys_root, 3, 3, "0000:09:00.0", fdinfo().replace(DEVICE, "0000:09:00.0"))
            clients = INTEL.read_clients({DEVICE, other}, proc, sys_root)
            self.assertEqual(set(clients), {DEVICE, other})
            self.assertEqual([list(v) for v in clients.values()], [["1"], ["1"]])
            self.assertEqual(INTEL.read_clients({other, "0000:05:00.0"}, proc, sys_root),
                             {other: clients[other], "0000:05:00.0": {}})

    def test_snapshots_are_per_device(self):
        other = "0000:03:00.0"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards, proc = self.fake_system(root, [(DEVICE, 0), (other, 1)])
            sys_root = root / "sys"
            for number, pci in enumerate(cards):
                open_drm(proc, sys_root, 1, number, pci, "")
            states = {pci: root / f"intel-{pci}.json" for pci in cards}

            def sample(at, render):
                for number, pci in enumerate(cards):
                    (proc / "1/fdinfo" / str(number)).write_text(fdinfo(render=render).replace(DEVICE, pci))
                clients = INTEL.read_clients(set(cards), proc, sys_root)
                with patch.object(INTEL.time, "monotonic_ns", return_value=at):
                    return {pci: INTEL.collect_device(pci, cards[pci], states[pci], clients[pci], proc) for pci in cards}

            first = sample(1_000_000_000, 100)
            self.assertTrue(all(r["engines"] == {} and r["frequencyMHz"] == 800 for r in first.values()))
            second = sample(2_000_000_000, 500_000_100)
            for result in second.values():
                self.assertEqual(result["engines"]["render"], 50)
                self.assertEqual(result["busy"], 50)
            self.assertNotEqual(states[DEVICE].read_text(), states[other].read_text())

    def test_collect_device_state_and_failure_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards, proc = self.fake_system(root, [(DEVICE, 1)])
            card, device = cards[DEVICE], root / DEVICE
            sys_root = root / "sys"
            open_drm(proc, sys_root, 1, 3, DEVICE, fdinfo())
            fds = proc / "1/fdinfo"
            state = root / "intel.json"

            def collect(card_path=card):
                clients = INTEL.read_clients({DEVICE}, proc, sys_root)[DEVICE]
                return INTEL.collect_device(DEVICE, card_path, state, clients, proc)

            with patch.object(INTEL.time, "monotonic_ns", return_value=1_000_000_000):
                first = collect()
            self.assertIsNone(first["busy"])
            self.assertEqual(first["frequencyMHz"], 800)
            self.assertEqual(first["memoryPrivate"], 63 * 1048576)
            self.assertEqual(first["thermals"], [])
            self.assertEqual(first["error"], "")
            sensor = device / "hwmon/hwmon1"
            sensor.mkdir(parents=True)
            (sensor / "temp1_input").write_text("45000")
            (sensor / "temp1_crit").write_text("95000")
            (sensor / "temp1_label").write_text("GPU")
            (fds / "3").write_text(fdinfo(render=500_000_100))
            with patch.object(INTEL.time, "monotonic_ns", return_value=2_000_000_000):
                active = collect()
                self.assertEqual(active["busy"], 50)
                self.assertEqual(active["thermals"], [{"temperature": 45, "critical": 95, "label": "GPU"}])
            self.assertIsNone(collect(None)["frequencyMHz"])
            state = root / "missing/intel.json"
            failed = collect()
            self.assertEqual((failed["busy"], failed["engines"], failed["memoryPrivate"], failed["frequencyMHz"], failed["thermals"]),
                             (None, {}, None, None, []))
            self.assertEqual(failed["error"], "Intel GPU readings unavailable. Retrying…")
            state = root / "intel.json"
            for invalid in ('broken', '{}', '{"device":"x","boot":"b","time":true,"clients":{}}'):
                state.write_text(invalid)
                self.assertIsNone(INTEL.load_snapshot(state))
                self.assertIsNone(collect()["busy"])

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
