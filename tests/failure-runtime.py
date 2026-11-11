#!/usr/bin/env python3
"""Offscreen checks of the Panel's failure paths on Omarchy 4.5.

Fake commands stand in for the boundaries: `timeout` passes through to the
real one except for the stats helper, whose outcome a mode file picks (a
nonzero exit, output that is not a sample, exit 127 as on a CPU without a
helper); `df` fails.

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
}

SHELL = r'''
import QtQuick
import Quickshell
import Quickshell.Io
import "harness"
import "plugin" as Vitals

ShellRoot {
  id: shell
  property int step: 0
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
  MockBar { id: bar; moduleName: "foamy.vitals" }
  Connections {
    target: Vitals.VitalsService.statsProcess
    function onExited() { shell.exits++ }
  }
  FloatingWindow {
    visible: true; implicitWidth: 300; implicitHeight: 60
    Vitals.Panel { id: a; bar: bar; moduleName: "foamy.vitals"; width: 100; height: 32 }
  }
  // Steps wait on events, not sleeps: each one returns until its condition
  // holds (or 15 s pass, and the check then fails with what it saw).
  property int tries: 0
  function ready(condition) {
    if (condition || ++tries > 75) { tries = 0; return true }
    return false
  }
  Timer {
    interval: 200; repeat: true; running: true
    onTriggered: {
      var s = Vitals.VitalsService
      var sampled = !s.statsProcess.running && shell.exits > shell.seen
      switch (shell.step) {
      case 0:
        if (!shell.ready(s.samples >= 1 && !s.statsProcess.running)) return
        check(s.telemetryError === "", "the real helper answers first")
        shell.sampleIn("fail")
        break
      case 1:
        if (!shell.ready(sampled)) return
        check(s.telemetryError === "Telemetry unavailable. Retrying…" && s.stats.mode === undefined,
              "a failed sample is an error, not the last reading (" + s.telemetryError + ")")
        check(!s.helperUnavailable, "and sampling goes on")
        shell.sampleIn("garbage")
        break
      case 2:
        if (!shell.ready(sampled)) return
        check(s.telemetryError === "Telemetry unavailable. Retrying…" && s.stats.mode === undefined,
              "output that is not a sample is an error")
        shell.sampleIn("missing")
        break
      case 3:
        if (!shell.ready(sampled)) return
        check(s.helperUnavailable && s.telemetryError === "This plugin's helper isn't available for this CPU.",
              "exit 127: the helper is unavailable (" + s.telemetryError + ")")
        shell.calls = shell.text(callsFile).split("\n").length
        s.sample(false)
        check(!s.statsProcess.running && shell.text(callsFile).split("\n").length === shell.calls,
              "an unavailable helper is not run again")
        a.open()
        break
      case 4:
        if (!shell.ready(!s.diskProcess.running && s.diskStatus !== "Reading disk capacity")) return
        check(s.diskStatus === "Disk capacity unavailable" && s.disks.length === 0,
              "a failed df says so (" + s.diskStatus + ")")
        console.log("FOAMY DONE")
        Qt.quit()
      }
      shell.step++
    }
  }
}
'''


def main():
    run = harness.Run("4.5", SHELL, plugin=PLUGIN, prefix="foamy-vitals-failure-", fakebin=FAKEBIN)
    run.run(timeout=90)
    oks = run.log.count("FOAMY OK")
    if run.ok:
        print(f"4.5 failures: {oks} checks passed")
        run.cleanup()
        return 0
    print(f"4.5 failures: FAILED ({oks} ok)\n" + "\n".join(run.failures() or run.log.splitlines()[-40:]))
    print(f"log kept at {run.base}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
