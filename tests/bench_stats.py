"""Measure what one telemetry sample costs: CPU time, wall time, processes created.

  python3 -I tests/bench_stats.py [--script PATH] [--runs N] [--bar] [--fixture NAME]

Each run is `timeout --kill-after=1s 5s stats.sh [--bar]`, as Panel.qml runs it,
2.1 s after the previous one so the shared sample cache never answers. CPU is
user+system time of the whole process tree (getrusage of waited children).
Processes are counted in a private PID namespace (unshare, no privileges):
the PID of a final probe minus the PIDs used before the sample. Readings
come from the real /proc and /sys of this machine; state stays in a temp dir.

--fixture samples a fake /proc and /sys from test_stats.Tree instead (amd: an
integrated AMD GPU; nvidia, nvidia2: one or two NVIDIA GPUs; amd-nvidia: both
kinds). NVIDIA GPUs use power/control=on, so no bar sample is spaced out (the
worst case). The fake clock is moved on 2.1 s before each run instead of sleeping.
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

ROOT = Path(__file__).resolve().parents[1]
# -I leaves the script directory off sys.path; test_stats holds the fake tree.
sys.path.insert(0, str(Path(__file__).resolve().parent))
FIXTURES = ("amd", "nvidia", "nvidia2", "amd-nvidia")


def child_cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def build_fixture(base, name):
    """A fake tree for one fixture: (env overrides, advance), where advance() moves its clock on 2.1 s."""
    import test_stats
    tree = test_stats.Tree(base)
    (base / "pci.ids").write_text(test_stats.PCI_IDS)
    tree.hwmon(0, "k10temp", [("Tctl", 55000, None)])
    for pid, name_, cwd in ((101, "editor", "/home/u/one"), (102, "agent", "/home/u/two"), (103, "shell", "/home/u")):
        tree.process(pid, name_, 100, 2000, cwd=cwd)
    if name in ("amd", "amd-nvidia"):
        address = "0000:c1:00.0"
        files = test_stats.amd_files(12, 256 * 1048576, 512 * 1048576)
        files.update({"mem_info_gtt_used": f"{1024 * 1048576}\n", "mem_info_gtt_total": f"{8192 * 1048576}\n"})
        device = tree.gpu(address, "amdgpu", class_="0x038000", files=files, hwmon=[("edge", 50000, None)])
        # Upstream finds the AMD GPU through its DRM card and the hwmon class.
        (tree.sys / "class/drm").mkdir(parents=True, exist_ok=True)
        (tree.sys / "class/drm/card0").mkdir()
        (tree.sys / "class/drm/card0/device").symlink_to(device)
        tree.hwmon(1, "amdgpu", [("edge", 50000, None)])
    if name in ("nvidia", "amd-nvidia"):
        tree.nvidia(addresses=["0000:01:00.0"], control="on")
    elif name == "nvidia2":
        tree.nvidia(addresses=["0000:01:00.0", "0000:02:00.0"], control="on")
    clock = {"seconds": 1000.0, "tick": 0}

    def advance():
        clock["seconds"] += 2.1
        clock["tick"] += 1
        tree.uptime(clock["seconds"])
        tree.cpu(clock["tick"])
    env = {"FOAMY_VITALS_PROC": str(tree.proc), "FOAMY_VITALS_SYS": str(tree.sys),
           "FOAMY_VITALS_PCI_IDS": str(base / "pci.ids"), "PATH": f"{tree.bin}:{os.environ['PATH']}"}
    return env, advance


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--script", default=str(ROOT / "stats.sh"))
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--bar", action="store_true", help="bar-only sample (popup closed)")
    parser.add_argument("--fixture", choices=FIXTURES, help="sample a fake tree instead of this machine")
    args = parser.parse_args()
    runtime = Path(tempfile.mkdtemp(prefix="foamy-vitals-bench-"))
    os.chmod(runtime, 0o700)
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime))
    advance = lambda: time.sleep(2.1)
    if args.fixture:
        fixture_env, advance = build_fixture(runtime / "tree", args.fixture)
        env.update(fixture_env)
    command = ["timeout", "--kill-after=1s", "5s", args.script] + (["--bar"] if args.bar else [])
    namespaced = shutil.which("unshare") is not None
    cpu, wall, processes = [], [], []
    try:
        # Warm-up: the first sample has no deltas to compute.
        subprocess.run(command, env=env, stdout=subprocess.DEVNULL, check=True)
        for _ in range(args.runs):
            advance()
            before_cpu, before_wall = child_cpu(), time.perf_counter()
            output = subprocess.run(command, env=env, capture_output=True, text=True, check=True).stdout
            wall.append(time.perf_counter() - before_wall)
            cpu.append(child_cpu() - before_cpu)
            if not output.startswith("{"):
                raise SystemExit("unexpected output: " + output[:200])
            if namespaced:
                advance()
                # PID 1 is the namespace shell; the sample's processes follow it,
                # and the probe's own PID tells how many came before it.
                probe = subprocess.run(["unshare", "-Upf", "--map-current-user", "bash", "-c",
                                        '"$@" >/dev/null; bash -c \'echo $$\'; :', "bench"] + command,
                                       env=env, capture_output=True, text=True, check=True).stdout
                processes.append(int(probe.strip()) - 2)
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
    mode = "bar" if args.bar else "full"
    where = f"fixture {args.fixture}" if args.fixture else "this machine"
    print(f"{mode} sample, {args.runs} runs, {where}, {Path(args.script).resolve()}")
    print(f"  cpu   mean {1000 * statistics.mean(cpu):6.1f} ms  median {1000 * statistics.median(cpu):6.1f} ms")
    print(f"  wall  mean {1000 * statistics.mean(wall):6.1f} ms  median {1000 * statistics.median(wall):6.1f} ms")
    if processes:
        print(f"  processes created per sample: median {statistics.median(processes):.0f} (min {min(processes)}, max {max(processes)})")


if __name__ == "__main__":
    main()
