#!/usr/bin/env python3
"""Offscreen checks of the Panel's failure paths on Omarchy 4.5.

Fake commands stand in for the boundaries: `timeout` passes through to the
real one except for the stats helper, whose outcome a mode file picks (a
nonzero exit, output that is not a sample, exit 127 as on a CPU without a
helper, or a slow sample); `df` fails for every mount or for one, or lists no
local disk; `omarchy-cmd-present` and `omarchy-launch-or-focus-tui`
answer from and write to the run's out directory, so btop is never started.
The uptime and load files are pointed at the out directory too, to feed
them text that /proc never has, or a file that is not there.

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
  echo "$*" >>"$FOAMY_OUT/helper-calls"
  case $(cat "$FOAMY_OUT/helper-mode" 2>/dev/null) in
  slow) sleep 1 ;;
  fail) exit 1 ;;
  garbage) echo 'not a sample'; exit 0 ;;
  missing) exit 127 ;;
  esac ;;
esac
exec {TIMEOUT} "$@"
""",
    # df's outcome comes from a mode file too: nothing (every mount failed),
    # "partial" (rows, then exit 1 for one dead mount, as GNU df does), "ok",
    # or "empty" (only the header: no local disk).
    "df": """#!/bin/sh
mode=$(cat "$FOAMY_OUT/df-mode" 2>/dev/null)
if [ "$mode" = empty ]; then
  echo 'Filesystem Type 1024-blocks Used Available Capacity Mounted on'
  exit 0
fi
if [ "$mode" = partial ] || [ "$mode" = ok ]; then
  echo 'Filesystem Type 1024-blocks Used Available Capacity Mounted on'
  echo '/dev/nvme0n1p2 btrfs 2000 1000 1000 50% /'
  echo '/dev/sdb1 ext4 1000 200 800 20% /media/disk'
fi
[ "$mode" = ok ] && exit 0
echo 'df: /mnt/x: Input/output error' >&2
exit 1
""",
    "hostnamectl": "#!/bin/sh\necho laptop\n",
    "omarchy-cmd-present": '#!/bin/sh\n[ "$1" = btop ] && [ -e "$FOAMY_OUT/btop-present" ]\n',
    "omarchy-launch-or-focus-tui": '#!/bin/sh\nprintf "%s\\n" "$*" >>"$FOAMY_OUT/launched"\n',
}

SHELL = r"""
import QtQuick
import Quickshell
import Quickshell.Io
import "harness"
import "plugin" as Vitals
import "plugin/kit"

ShellRoot {
  id: shell
  property int exits: 0
  property int seen: 0
  property int calls: 0
  readonly property string out: Quickshell.env("FOAMY_OUT")
  function check(ok, what) { console.log((ok ? "FOAMY OK " : "FOAMY FAIL ") + what) }
  function text(file) { file.reload(); return file.text() }
  // The first item under `item` (depth first) for which pick(item) holds.
  function find(item, pick) {
    if (!item) return null
    if (pick(item)) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) {
      var hit = find(kids[i], pick)
      if (hit) return hit
    }
    return null
  }
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
  FileView { id: dfMode; path: shell.out + "/df-mode"; blockWrites: true; atomicWrites: false }
  FileView { id: uptimeText; path: shell.out + "/uptime"; blockWrites: true; atomicWrites: false }
  FileView { id: loadText; path: shell.out + "/loadavg"; blockWrites: true; atomicWrites: false }
  property int uptimeLoads: 0
  property int uptimeFailures: 0
  Connections {
    target: Vitals.VitalsService.uptimeFile
    function onLoaded() { shell.uptimeLoads++ }
    function onLoadFailed() { shell.uptimeFailures++ }
  }
  property int loadLoads: 0
  property int loadFailures: 0
  Connections {
    target: Vitals.VitalsService.loadFile
    function onLoaded() { shell.loadLoads++ }
    function onLoadFailed() { shell.loadFailures++ }
  }
  // The service reads `text` as its uptime file, then `then` runs. Its
  // path is pointed at the run's out directory instead of /proc/uptime.
  function uptimeIn(text, then, label) {
    uptimeText.setText(text)
    var before = uptimeLoads
    var path = shell.out + "/uptime"
    if (s.uptimeFile.path !== path)
      s.uptimeFile.path = path
    else
      s.uptimeFile.reload()
    wait.waitFor(function() { return shell.uptimeLoads > before }, 15000, then, label)
  }
  // The same for the load file, in place of /proc/loadavg.
  function loadIn(text, then, label) {
    loadText.setText(text)
    var before = loadLoads
    var path = shell.out + "/loadavg"
    if (s.loadFile.path !== path)
      s.loadFile.path = path
    else
      s.loadFile.reload()
    wait.waitFor(function() { return shell.loadLoads > before }, 15000, then, label)
  }
  // `file` (the uptime or load FileView) is pointed at a file that is not
  // there; `then` runs once its read has failed.
  function missingIn(file, failures, then, label) {
    var before = failures()
    file.path = shell.out + "/missing"
    wait.waitFor(function() { return failures() > before }, 15000, then, label)
  }
  property int diskRuns: 0
  Connections {
    target: Vitals.VitalsService.diskProcess
    function onExited() { shell.diskRuns++ }
  }
  // Run df in `mode` and continue once it has exited.
  function disksIn(mode, then, label) {
    dfMode.setText(mode)
    var before = diskRuns
    s.refreshDisks()
    wait.waitFor(function() { return shell.diskRuns > before && !s.diskProcess.running }, 15000, then, label)
  }
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
  // The host WidgetButton of a Panel: what a click on the bar reaches.
  function button(item) {
    if (!item) return null
    if (typeof item.triggerPress === "function" && item.activeColor !== undefined) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = button(kids[i]); if (found) return found }
    return null
  }
  // The sample started by sampleIn() has finished.
  function sampled() { return !s.statsProcess.running && shell.exits > shell.seen }
  // Steps wait on events, not sleeps: a timeout fails the run with its label.
  Component.onCompleted: wait.waitFor(function() { return s.samples >= 1 && !s.statsProcess.running }, 15000, step0, "the real helper answers")
  function step0() {
    check(s.telemetryError === "", "the real helper answers first")
    // The popup opens (Panel.refresh) while a bar sample is running: the
    // full sample it asks for runs once that one ends. The popup stays
    // closed here, so no 2 s tick could take a full sample instead.
    shell.sampleIn("slow")
    a.refresh()
    wait.waitFor(function() { return shell.exits >= shell.seen + 2 && !s.statsProcess.running }, 15000, queued, "two samples end")
  }
  function queued() {
    var calls = shell.text(callsFile).trim().split("\n")
    var last = calls[calls.length - 1]
    check(calls[calls.length - 2].indexOf("--bar") >= 0 && last.indexOf("stats") >= 0 && last.indexOf("--bar") < 0,
          "a refresh during a bar sample runs a full sample after it (" + calls.slice(-2).join(" | ") + ")")
    // The real helper ranks resident memory on every full sample.
    check(s.stats.mode === "full" && s.detail.memoryProcesses.length > 0,
          "and its readings land (" + s.stats.mode + ", " + s.detail.memoryProcesses.length + " memory rankings)")
    shell.sampleIn("fail")
    wait.waitFor(sampled, 15000, step1, "the failing sample ends")
  }
  function step1() {
    check(s.telemetryError === "Telemetry unavailable. Retrying…" && s.stats.mode === undefined,
          "a failed sample is an error, not the last reading (" + s.telemetryError + ")")
    // The full sample above filled the rankings; the helper's exit 1 must clear them.
    check(s.detail.processes.length === 0 && s.detail.memoryProcesses.length === 0, "a failed sample clears the rankings")
    var cpu = s.history.cpu
    check(cpu.length > 0 && cpu[cpu.length - 1].value === null, "a failed sample breaks the history")
    check(!s.helperUnavailable, "and sampling goes on")
    shell.sampleIn("garbage")
    wait.waitFor(sampled, 15000, step2, "the garbage sample ends")
  }
  function step2() {
    check(s.telemetryError === "Telemetry unavailable. Retrying…" && s.stats.mode === undefined,
          "output that is not a sample is an error")
    // The fake passes this one to the real helper.
    shell.sampleIn("")
    wait.waitFor(sampled, 15000, recovered, "the good sample ends")
  }
  function recovered() {
    // The reply may be the helper's cached full line, so the mode is not checked.
    check(s.telemetryError === "" && typeof s.stats.sampleTimeMs === "number" && Array.isArray(s.stats.cores),
          "the next good sample clears the error (" + s.telemetryError + ")")
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
    // A right-click on the bar is the btop shortcut.
    shell.button(a).triggerPress(Qt.RightButton)
    wait.waitFor(function() { return a.launchError !== "" }, 15000, step4, "the btop check ends")
  }
  function step4() {
    check(a.launchError === "Install btop to use this shortcut." && shell.text(launchedFile) === "",
          "btop missing: a hint, nothing launched (" + a.launchError + ")")
    btopFile.setText("yes\n")
    a.open()
    wait.waitFor(function() { return a.popup.bodyItem !== null }, 15000, pressB, "the popup body is built")
  }
  function pressB() {
    // `b` on the open dashboard launches btop.
    var view = a.popup.bodyItem
    // The error line says the helper is missing, readable on the popup
    // (main() picks a theme whose red reads 2.70:1 there).
    var said = FoamyI18n.t(s.telemetryError)
    var line = shell.find(view, function(item) { return item.text === said && item.color !== undefined })
    var ratio = line ? FoamyPalette.contrastRatio(line.color, FoamyPalette.background) : 0
    check(ratio >= 4.5, "the error line reads on the popup (" + (line ? line.color + ", " + ratio.toFixed(2) + ":1" : "not shown") + ")")
    view.cursor.emitTextKey(view, "b")
    wait.waitFor(function() { return shell.text(launchedFile) !== "" && !s.diskProcess.running }, 15000, step5, "btop is launched")
  }
  function step5() {
    check(shell.text(launchedFile) === "btop\n", "btop present: launched through omarchy-launch-or-focus-tui")
    check(!a.opened && a.launchError === "", "and the popup closes")
    check(s.diskStatus === "Disk capacity unavailable" && s.disks.length === 0,
          "a failed df says so (" + s.diskStatus + ")")
    shell.disksIn("partial", step6, "df with one dead mount ends")
  }
  function step6() {
    check(s.disks.length === 2 && s.disks[0].mount === "/" && s.disks[1].mount === "/media/disk",
          "one unreadable mount keeps the other disks (" + JSON.stringify(s.disks) + ")")
    check(s.diskStatus === "Some disks couldn't be read", "and says the list is partial (" + s.diskStatus + ")")
    shell.disksIn("ok", step7, "a good df ends")
  }
  function step7() {
    check(s.disks.length === 2 && s.diskStatus === "", "a good df clears the caption (" + s.diskStatus + ")")
    shell.disksIn("empty", step8, "df without local disks ends")
  }
  function step8() {
    check(s.disks.length === 0 && s.diskStatus === "No mounted local disks", "a df without local disks says so (" + s.diskStatus + ")")
    shell.uptimeIn("7260.50 100.00\n", step9, "a valid uptime is read")
  }
  function step9() {
    check(s.uptimeMinutes === 121, "an uptime of 7260.5 s reads 121 minutes (" + s.uptimeMinutes + ")")
    shell.uptimeIn("garbage 100.00\n", step10, "an unparsable uptime is read")
  }
  function step10() {
    // -1 shows as "--"; 0 would show as "0h 0m", a reading that isn't there.
    check(s.uptimeMinutes === -1, "an unparsable uptime is unknown, not 0 minutes (" + s.uptimeMinutes + ")")
    shell.uptimeIn("7260.50 100.00\n", step11, "a valid uptime is read again")
  }
  function step11() {
    check(s.uptimeMinutes === 121, "and a valid one reads again (" + s.uptimeMinutes + ")")
    shell.uptimeIn("", step12, "an empty uptime file is read")
  }
  function step12() {
    check(s.uptimeMinutes === -1, "an empty uptime file is unknown, not 0 minutes (" + s.uptimeMinutes + ")")
    shell.loadIn("0.84 0.66 0.52 1/123 4567\n", step13, "a valid load is read")
  }
  function step13() {
    check(JSON.stringify(s.loadAverages) === "[0.84,0.66,0.52]", "a valid load reads three averages (" + JSON.stringify(s.loadAverages) + ")")
    shell.loadIn("garbage 0.66 0.52 1/123 4567\n", step14, "an unparsable load is read")
  }
  function step14() {
    // An empty list shows as "--"; NaN would show as "Load NaN".
    check(s.loadAverages.length === 0, "an unparsable load is unknown, not NaN (" + JSON.stringify(s.loadAverages) + ")")
    shell.loadIn("0.84 0.66 0.52 1/123 4567\n", step15, "a valid load is read again")
  }
  function step15() {
    check(s.loadAverages.length === 3, "and a valid one reads again (" + JSON.stringify(s.loadAverages) + ")")
    shell.loadIn("", step16, "an empty load file is read")
  }
  function step16() {
    check(s.loadAverages.length === 0, "an empty load file is unknown, not 0.00 (" + JSON.stringify(s.loadAverages) + ")")
    shell.uptimeIn("7260.50 100.00\n", step17, "a valid uptime is read before a failed read")
  }
  function step17() {
    check(s.uptimeMinutes === 121, "the uptime reads 121 minutes before the failed read (" + s.uptimeMinutes + ")")
    shell.missingIn(s.uptimeFile, function() { return shell.uptimeFailures }, step18, "the uptime read fails")
  }
  function step18() {
    check(s.uptimeMinutes === -1, "a failed uptime read is unknown, not the last reading (" + s.uptimeMinutes + ")")
    shell.loadIn("0.84 0.66 0.52 1/123 4567\n", step19, "a valid load is read before a failed read")
  }
  function step19() {
    check(s.loadAverages.length === 3, "the load reads three averages before the failed read (" + JSON.stringify(s.loadAverages) + ")")
    shell.missingIn(s.loadFile, function() { return shell.loadFailures }, step20, "the load read fails")
  }
  function step20() {
    check(s.loadAverages.length === 0, "a failed load read is unknown, not the last reading (" + JSON.stringify(s.loadAverages) + ")")
    console.log("FOAMY DONE")
    Qt.quit()
  }
}
"""


def main():
    # A dark theme with a dim red (2.70:1 on the popup): the error line must
    # lighten until it reads.
    theme = KIT / "tests/fixtures/themes/dim-red.toml"
    run = harness.Run("4.5", SHELL, plugin=PLUGIN, theme=theme, prefix="foamy-vitals-failure-", fakebin=FAKEBIN)
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
