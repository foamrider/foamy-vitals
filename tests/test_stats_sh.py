import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DROPPED = ["gpuTemp", "gpuThermals", "gpuBusy", "vramUsed", "vramTotal", "gpuIntel",
           "gpuMemoryPrivate", "gpuFrequencyMHz", "gpuEngines"]


def run(mode, runtime, path=None):
    # A private runtime directory keeps tests away from the live bar's cache.
    env = dict(os.environ, XDG_RUNTIME_DIR=runtime)
    if path:
        env["PATH"] = path + os.pathsep + env["PATH"]
    return subprocess.run(["bash", str(ROOT / "stats.sh"), mode], capture_output=True, text=True,
                          env=env, timeout=10, check=True).stdout


class StatsShTest(unittest.TestCase):
    def test_output_shape(self):
        with tempfile.TemporaryDirectory() as runtime:
            first = run("open", runtime)
            stats = json.loads(first)
            self.assertIsInstance(stats, dict)
            self.assertIsInstance(stats["gpus"], list)
            self.assertIsInstance(stats["gpuError"], str)
            for key in DROPPED:
                self.assertNotIn(key, stats)
            for gpu in stats["gpus"]:
                self.assertIsInstance(gpu["id"], str)
            # Every bar instance inside the cache window shares one sample.
            self.assertEqual(run("closed", runtime), first)

    def test_mode_and_state_reach_the_helper(self):
        with tempfile.TemporaryDirectory() as runtime, tempfile.TemporaryDirectory() as bin_dir:
            fake = Path(bin_dir) / "python3"
            fake.write_text('#!/bin/sh\nprintf \'[{"id":"%s","state":"%s"}]\' "$2" "$3"\n')
            fake.chmod(0o755)
            state = f"{runtime}/foamy-vitals-{os.geteuid()}"
            for mode, expected in (("open", "open"), ("closed", "closed"), ("", "closed"), ("bogus", "closed")):
                cache = Path(state, "cache")
                if cache.exists():
                    cache.unlink()
                stats = json.loads(run(mode, runtime, bin_dir))
                self.assertEqual(stats["gpus"], [{"id": expected, "state": state}], mode)
                self.assertEqual(stats["gpuError"], "")

    def test_failed_helper_degrades_to_no_gpus(self):
        with tempfile.TemporaryDirectory() as runtime, tempfile.TemporaryDirectory() as bin_dir:
            fake = Path(bin_dir) / "python3"
            fake.write_text('#!/bin/sh\nprintf \'{"not":"an array"}\'\n')
            fake.chmod(0o755)
            stats = json.loads(run("closed", runtime, bin_dir))
            self.assertEqual(stats["gpus"], [])
            self.assertEqual(stats["gpuError"], "GPU readings unavailable. Retrying…")


if __name__ == "__main__":
    unittest.main()
