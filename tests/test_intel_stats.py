import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "intel_stats.py"
SPEC = importlib.util.spec_from_file_location("intel_stats", SCRIPT)
assert SPEC and SPEC.loader
INTEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INTEL)
DEVICE = "0000:00:02.0"
DEVICE_KEYS = {"busy", "memoryPrivate", "frequencyMHz", "thermals", "error"}


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
    """Give pid a descriptor on pci's render node; text None makes its fdinfo a FIFO that blocks if opened."""
    node = node or "renderD" + str(128 + int(pci[5:7], 16))
    for name in ("fd", "fdinfo"):
        (proc / str(pid) / name).mkdir(parents=True, exist_ok=True)
    (proc / str(pid) / "fd" / str(fd)).symlink_to("/dev/dri/" + node)
    if text is None:
        os.mkfifo(proc / str(pid) / "fdinfo" / str(fd))
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
        after = snapshot(46_000_000_000, fdinfo(render=4_500_000_100))
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
        self.assertEqual(INTEL.parse_client(fdinfo().replace("i915", "xe"), {DEVICE})[:2], (DEVICE, "1"))

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

    def test_only_dri_descriptors_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            proc, sys_root = Path(directory) / "proc", Path(directory) / "sys"
            open_drm(proc, sys_root, 1, 7, DEVICE, fdinfo(9), node="card1")
            # fdinfo of a socket or a plain file is never opened, whatever it holds.
            for fd, target in ((5, "socket:[1234]"), (6, "/home/user/notes.txt")):
                (proc / "1/fd" / str(fd)).symlink_to(target)
                (proc / "1/fdinfo" / str(fd)).write_text(fdinfo(fd + 2))
            reads = []
            real = Path.read_text
            def spy(path, *args, **kwargs):
                reads.append(path.name)
                return real(path, *args, **kwargs)
            with patch.object(Path, "read_text", spy):
                clients = INTEL.read_clients({DEVICE}, proc, sys_root)
            self.assertEqual(list(clients[DEVICE]), ["9"])
            self.assertEqual(reads, ["7"])

    def test_only_descriptors_of_requested_devices_are_read(self):
        # Printing an xe fdinfo wakes its GPU, so an asleep or unrequested device's
        # fdinfo must never be opened. Here each one is a FIFO: opening it blocks.
        asleep = "0000:03:00.0"
        with tempfile.TemporaryDirectory() as directory:
            proc, sys_root = Path(directory) / "proc", Path(directory) / "sys"
            open_drm(proc, sys_root, 1, 3, DEVICE, fdinfo())
            open_drm(proc, sys_root, 1, 4, asleep, None)
            open_drm(proc, sys_root, 2, 5, asleep, None, node="card1")
            open_drm(proc, sys_root, 2, 6, "0000:09:00.0", None, node="renderD200")
            (sys_root / "class/drm/renderD200/device").unlink()  # node without a device link
            (proc / "2/fd/7").symlink_to("/dev/null")
            os.mkfifo(proc / "2/fdinfo/7")
            result = {}
            scan = threading.Thread(target=lambda: result.update(INTEL.read_clients({DEVICE}, proc, sys_root)), daemon=True)
            scan.start()
            scan.join(1.5)
            if scan.is_alive():
                # Release the blocked open so the thread and the directory can go.
                for pid, fd in ((1, 4), (2, 5), (2, 6), (2, 7)):
                    try:
                        os.close(os.open(proc / str(pid) / "fdinfo" / str(fd), os.O_WRONLY | os.O_NONBLOCK))
                    except OSError:
                        pass
                scan.join(1)
                self.fail("the scan opened the fdinfo of an unrequested device")
            self.assertEqual(list(result), [DEVICE])
            self.assertEqual(list(result[DEVICE]), ["1"])

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
            self.assertTrue(all(r["busy"] is None and r["frequencyMHz"] == 800 for r in first.values()))
            second = sample(2_000_000_000, 500_000_100)
            for result in second.values():
                self.assertEqual(result["busy"], 50)
                self.assertEqual(set(result), DEVICE_KEYS)
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
            self.assertEqual(failed, INTEL.unavailable())
            self.assertEqual(failed, {"busy": None, "memoryPrivate": None, "frequencyMHz": None, "thermals": [],
                                      "error": "Intel GPU readings unavailable. Retrying…"})
            state = root / "intel.json"
            (proc / "sys/kernel/random/boot_id").unlink()
            self.assertEqual(collect(), INTEL.unavailable())
            (proc / "sys/kernel/random/boot_id").write_text("boot")
            for invalid in ('broken', '{}', '{"device":"x","boot":"b","time":true,"clients":{}}'):
                state.write_text(invalid)
                self.assertIsNone(INTEL.load_snapshot(state))
                self.assertIsNone(collect()["busy"])

    def test_one_failing_sensor_skips_only_that_sensor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards, proc = self.fake_system(root, [(DEVICE, 1)])
            card = cards[DEVICE]
            (card / "gt/gt1").mkdir()
            (card / "gt/gt1/rps_act_freq_mhz").mkdir()  # unreadable frequency file
            sensor = root / DEVICE / "hwmon/hwmon1"
            sensor.mkdir(parents=True)
            (sensor / "temp1_input").write_text("45000")
            (sensor / "temp2_input").mkdir()  # unreadable sensor
            (sensor / "temp3_input").write_text("50000")
            (sensor / "temp3_crit").mkdir()  # unreadable limit: the sensor stays, without it
            result = INTEL.collect_device(DEVICE, card, root / "intel.json", {}, proc)
            self.assertEqual(result["error"], "")
            self.assertEqual(result["frequencyMHz"], 800)
            self.assertEqual(sorted(t["temperature"] for t in result["thermals"]), [45, 50])
            self.assertEqual([t["critical"] for t in result["thermals"]], [None, None])

    def test_cli_prints_one_line_per_device(self):
        other = "0000:03:00.0"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, sys_root, state = root / "proc", root / "sys", root / "state"
            state.mkdir()
            boot = proc / "sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True)
            boot.write_text("boot")
            for pci in (DEVICE, other):
                device = sys_root / "bus/pci/devices" / pci
                for number in (3, 1):
                    card = device / f"drm/card{number}"
                    (card / "gt/gt0").mkdir(parents=True)
                    (card / "device").symlink_to(device)
                    (card / "gt/gt0/rps_act_freq_mhz").write_text(f"{number}00\n")
                open_drm(proc, sys_root, 1, int(pci[5:7], 16) + 3, pci, fdinfo().replace(DEVICE, pci))
            done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--proc", str(proc), "--sys", str(sys_root),
                                   "--spaced", other, str(state), DEVICE, other],
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(done.returncode, 0, done.stderr)
            lines = done.stdout.splitlines()
            self.assertEqual([line.split("\t", 1)[0] for line in lines], [DEVICE, other])
            for line in lines:
                reading = json.loads(line.split("\t", 1)[1])
                self.assertEqual(set(reading), DEVICE_KEYS)
                self.assertEqual(reading["frequencyMHz"], 100, "the lowest card is read")
                self.assertEqual(reading["memoryPrivate"], 63 * 1048576)
                self.assertEqual(reading["error"], "")
            self.assertEqual(sorted(p.name for p in state.iterdir()), [f"intel-{DEVICE}.json", f"intel-{other}.json"])
            for bad in (["--proc", str(proc), "--sys", str(sys_root), str(state)],
                        ["--proc", str(proc), "--sys", str(sys_root), str(state), "../x"]):
                self.assertNotEqual(subprocess.run([sys.executable, "-I", str(SCRIPT), *bad],
                                                   capture_output=True, timeout=10).returncode, 0)

    def test_legacy_form_reports_the_lowest_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc, sys_root = root / "proc", root / "sys"
            boot = proc / "sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True)
            boot.write_text("boot")
            driver = sys_root / "bus/pci/drivers/i915"
            driver.mkdir(parents=True)
            for pci, number in ((DEVICE, 1), ("0000:03:00.0", 0)):
                device = sys_root / "devices" / pci
                device.mkdir(parents=True)
                (device / "driver").symlink_to(driver)
                card = sys_root / f"class/drm/card{number}"
                (card / "gt/gt0").mkdir(parents=True)
                (card / "device").symlink_to(device)
                (card / "gt/gt0/rps_act_freq_mhz").write_text(f"{number + 7}00\n")
            open_drm(proc, sys_root, 1, 3, DEVICE, fdinfo())
            result = INTEL.collect(root / "intel.json", sys_root / "class/drm", proc)
            self.assertEqual(list(result), ["gpuIntel", "gpuBusy", "gpuEngines", "gpuMemoryPrivate",
                                            "gpuFrequencyMHz", "gpuTemp", "gpuThermals", "gpuError"])
            self.assertEqual((result["gpuIntel"], result["gpuFrequencyMHz"], result["gpuMemoryPrivate"], result["gpuError"]),
                             (True, 800, 63 * 1048576, ""))

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
