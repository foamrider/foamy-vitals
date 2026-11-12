#!/usr/bin/env python3
"""Offscreen checks of the Panel's failure paths on Omarchy 4.5.

Fake commands stand in for the boundaries: `timeout` passes through to the
real one except for the stats helper, whose outcome a mode file picks (a
nonzero exit, output that is not a sample, exit 127 as on a CPU without a
helper); `df` fails; `omarchy-cmd-present` and `omarchy-launch-or-focus-tui`
answer from and write to the run's out directory, so btop is never started.

  python3 tests/failure-runtime.py      (FOAMY_KIT, FOAMY_HOST_45 as in runtime.py)
"""
import os
import shutil
import sys
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
os.environ.setdefault("FOAMY_HOST_45", "/usr/share/omarchy/shell")
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]
TIMEOUT = shutil.which("timeout") or "/usr/bin/timeout"

FAKEBIN = {
    "timeout": f"""#!/bin/sh
case " $* " in
*foamy-vitals-helper*)
  echo call >>"$FOAMY_OUT/helper-calls"
  case $(cat "$FOAMY_OUT/helper-mode" 2>/dev/null) in
  fail) exit 1 ;;
  garbage) echo 'not a sample'; exit 0 ;;
  missing) exit 127 ;;
  esac ;;
esac
exec {TIMEOUT} "$@"
""",
    "df": "#!/bin/sh\necho 'df: /mnt/x: Input/output error' >&2\nexit 1\n",
    "hostnamectl": "#!/bin/sh\necho laptop\n",
    "omarchy-cmd-present": '#!/bin/sh\n[ "$1" = btop ] && [ -e "$FOAMY_OUT/btop-present" ]\n',
    "omarchy-launch-or-focus-tui": '#!/bin/sh\nprintf "%s\\n" "$*" >>"$FOAMY_OUT/launched"\n',
}

SHELL = r'''
import QtQuick
import Quickshell
import Quickshell.Io
import "harness"
import "plugin" as Vitals

ShellRoot {
  id: shell
  property int exits: 0
  property int seen: 0
  property int calls: 0
  readonly property string out: Quickshell.env("FOAMY_OUT")
  function check(ok, what) { console.log((ok ? "FOAMY OK " : "FOAMY FAIL ") + what) }
  function text(file) { file.reload(); return file.text() }
  // Run one sample in `mode` once the previous one has finished.
  function sampleIn(mode) {
    modeFile.setText(mode)
    seen = exits
    Vitals.VitalsService.sample(false)
  }
  FileView { id: modeFile; path: shell.out + "/helper-mode"; blockWrites: true; atomicWrites: false }
  FileView { id: callsFile; path: shell.out + "/helper-calls"; blockLoading: true; printErrors: false }
  FileView { id: launchedFile; path: shell.out + "/launched"; blockLoading: true; printErrors: false }
  FileView { id: btopFile; path: shell.out + "/btop-present"; blockWrites: true; atomicWrites: false }
  MockBar { id: bar; moduleName: "foamy.vitals" }
  Connections {
    target: Vitals.VitalsService.statsProcess
    function onExited() { shell.exits++ }
  }
  FloatingWindow {
    visible: true; implicitWidth: 300; implicitHeight: 60
    Vitals.Panel { id: a; bar: bar; moduleName: "foamy.vitals"; width: 100; height: 32 }
  }
  FoamyWait { id: wait }
  readonly property var s: Vitals.VitalsService
  // The sample started by sampleIn() has finished.
  function sampled() { return !s.statsProcess.running && shell.exits > shell.seen }
  // Steps wait on events, not sleeps: a timeout fails the run with its label.
  Component.onCompleted: wait.waitFor(function() { return s.samples >= 1 && !s.statsProcess.running }, 15000, step0, "the real helper answers")
  function step0() {
    check(s.telemetryError === "", "the real helper answers first")
    shell.sampleIn("fail")
    wait.waitFor(sampled, 15000, step1, "the failing sample ends")
  }
  function step1() {
    check(s.telemetryError === "Telemetry unavailable. Retrying…" && s.stats.mode === undefined,
          "a failed sample is an error, not the last reading (" + s.telemetryError + ")")
    check(!s.helperUnavailable, "and sampling goes on")
    shell.sampleIn("garbage")
    wait.waitFor(sampled, 15000, step2, "the garbage sample ends")
  }
  function step2() {
    check(s.telemetryError === "Telemetry unavailable. Retrying…" && s.stats.mode === undefined,
          "output that is not a sample is an error")
    shell.sampleIn("missing")
    wait.waitFor(sampled, 15000, step3, "the missing-helper sample ends")
  }
  function step3() {
    check(s.helperUnavailable && s.telemetryError === "This plugin's helper isn't available for this CPU.",
          "exit 127: the helper is unavailable (" + s.telemetryError + ")")
    shell.calls = shell.text(callsFile).split("\n").length
    s.sample(false)
    check(!s.statsProcess.running && shell.text(callsFile).split("\n").length === shell.calls,
          "an unavailable helper is not run again")
    a.openBtop()
    wait.waitFor(function() { return a.launchError !== "" }, 15000, step4, "the btop check ends")
  }
  function step4() {
    check(a.launchError === "Install btop to use this shortcut." && shell.text(launchedFile) === "",
          "btop missing: a hint, nothing launched (" + a.launchError + ")")
    btopFile.setText("yes\n")
    a.open()
    a.openBtop()
    wait.waitFor(function() { return shell.text(launchedFile) !== "" && !s.diskProcess.running }, 15000, step5, "btop is launched")
  }
  function step5() {
    check(shell.text(launchedFile) === "btop\n", "btop present: launched through omarchy-launch-or-focus-tui")
    check(!a.opened && a.launchError === "", "and the popup closes")
    check(s.diskStatus === "Disk capacity unavailable" && s.disks.length === 0,
          "a failed df says so (" + s.diskStatus + ")")
    console.log("FOAMY DONE")
    Qt.quit()
  }
}
'''


def main():
    run = harness.Run("4.5", SHELL, plugin=PLUGIN, prefix="foamy-vitals-failure-", fakebin=FAKEBIN)
    run.run(timeout=120)
    oks = run.log.count("FOAMY OK")
    # Every host command the Panel runs has a stand-in above.
    for line in run.host_commands():
        run.ok = False
        print(f"host command reached: {line}")
    if run.ok:
        print(f"4.5 failures: {oks} checks passed")
        run.cleanup()
        return 0
    print(f"4.5 failures: FAILED ({oks} ok)\n" + "\n".join(run.failures() or run.log.splitlines()[-40:]))
    print(f"log kept at {run.base}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
