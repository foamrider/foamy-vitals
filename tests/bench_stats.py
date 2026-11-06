"""Measure what one telemetry sample costs: CPU time, wall time, processes created.

  python3 -I tests/bench_stats.py [--script PATH] [--runs N] [--bar]

Each run is `timeout --kill-after=1s 5s <helper> [--bar]`, as the panel runs
it, 2.1 s after the previous one so the shared sample cache never answers. The
helper is --script PATH, else FOAMY_HELPER (an argv prefix as a JSON list),
else this checkout's helper as VitalsService.qml launches it (FoamyNative's
`bash -c 'exec -- "$1/bin/$HOSTTYPE/$2" …'`, so bash's start is counted). CPU is
user+system time of the whole process tree (getrusage of waited children).
Processes are counted in a private PID namespace (unshare, no privileges):
the PID of a final probe minus the PIDs used before the sample. Readings
come from the real /proc and /sys of this machine; state stays in a temp dir.
"""
import argparse
import os
from pathlib import Path
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakesys import ROOT  # noqa: E402

NATIVE = ["bash", "-c", 'exec -- "$1/bin/$HOSTTYPE/$2" "${@:3}"', "foamy-native", str(ROOT), "foamy-vitals-helper", "stats"]


def child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--script", help="run this program instead of the helper")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--bar", action="store_true", help="bar-only sample (popup closed)")
    args = parser.parse_args()
    runtime = Path(tempfile.mkdtemp(prefix="foamy-vitals-bench-"))
    os.chmod(runtime, 0o700)
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime))
    if args.script:
        helper = [str(Path(args.script).resolve())]
    elif os.environ.get("FOAMY_HELPER"):
        from fakesys import HELPER
        helper = HELPER
    else:
        helper = NATIVE
    command = ["timeout", "--kill-after=1s", "5s", *helper] + (["--bar"] if args.bar else [])
    namespaced = shutil.which("unshare") is not None
    cpu, wall, processes = [], [], []
    try:
        # Warm-up: the first sample has no deltas to compute.
        subprocess.run(command, env=env, stdout=subprocess.DEVNULL, check=True)
        for _ in range(args.runs):
            time.sleep(2.1)
            before_cpu, before_wall = child_cpu(), time.perf_counter()
            output = subprocess.run(command, env=env, capture_output=True, text=True, check=True).stdout
            wall.append(time.perf_counter() - before_wall)
            cpu.append(child_cpu() - before_cpu)
            if not output.startswith("{"):
                raise SystemExit("unexpected output: " + output[:200])
            if namespaced:
                time.sleep(2.1)
                # PID 1 is the namespace shell; the sample's processes follow it,
                # and the probe's own PID tells how many came before it.
                probe = subprocess.run(["unshare", "-Upf", "--map-current-user", "bash", "-c",
                                        '"$@" >/dev/null; bash -c \'echo $$\'; :', "bench"] + command,
                                       env=env, capture_output=True, text=True, check=True).stdout
                processes.append(int(probe.strip()) - 2)
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    mode = "bar" if args.bar else "full"
    print(f"{mode} sample, {args.runs} runs, {' '.join(helper)}")
    print(f"  cpu   mean {1000 * statistics.mean(cpu):6.1f} ms  median {1000 * statistics.median(cpu):6.1f} ms")
    print(f"  wall  mean {1000 * statistics.mean(wall):6.1f} ms  median {1000 * statistics.median(wall):6.1f} ms")
    if processes:
        print(f"  processes created per sample: median {statistics.median(processes):.0f} (min {min(processes)}, max {max(processes)})")


if __name__ == "__main__":
    main()
