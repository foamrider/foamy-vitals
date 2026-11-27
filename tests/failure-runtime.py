#!/usr/bin/env python3
"""Offscreen checks of the Panel's failure paths on Omarchy 4.5.

Fake commands stand in for the boundaries: `timeout` passes through to the
real one except for the stats helper, whose outcome a mode file picks (a
nonzero exit, output that is not a sample, exit 127 as on a CPU without a
helper, a slow sample, or WARNING_SAMPLE); `df` fails for every mount or for one, or lists no
local disk; `omarchy-cmd-present` and `omarchy-launch-or-focus-tui`
answer from and write to the run's out directory, so btop is never started.
The uptime and load files are pointed at the out directory too, to feed
them text that /proc never has, or a file that is not there.

  python3 tests/failure-runtime.py      (FOAMY_KIT, FOAMY_HOST_45 as in runtime.py)
"""

import json
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
  warning) sed 's/"@NOW@"/'"$(date +%s%3N)"'/' "$FOAMY_OUT/warning-sample"; exit 0 ;;
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

# Past every default warning limit (Preferences.js): CPU, RAM, VRAM and
# GPU busy at 95 % or more, each temperature within 15 °C of its critical
# one. Two GPUs, so the Graphics table shows, and a GPU row error. The fake
# puts the time in place of "@NOW@".
WARNING_SAMPLE = {
    "mode": "full",
    "sampleTimeMs": "@NOW@",
    "cpu": 97,
    "cores": [97, 96],
    "memUsedKb": 31000000,
    "memTotalKb": 32000000,
    "cpuTemp": 90,
    "cpuTempCrit": 100,
    "gpus": [
        {
            "id": "0000:03:00.0",
            "vendor": "amd",
            "driver": "amdgpu",
            "name": "Radeon RX 7800 XT",
            "type": "discrete",
            "asleep": False,
            "busy": 99,
            "vramUsed": 16642998272,
            "vramTotal": 17179869184,
            "memoryPrivate": None,
            "frequencyMHz": None,
            "thermals": [{"temperature": 100, "critical": 110, "label": "Edge"}],
            "error": "",
        },
        {
            "id": "0000:01:00.0",
            "vendor": "nvidia",
            "driver": "nvidia",
            "name": "GeForce RTX 4070",
            "type": "discrete",
            "asleep": False,
            "busy": None,
            "vramUsed": None,
            "vramTotal": None,
            "memoryPrivate": None,
            "frequencyMHz": None,
            "thermals": [],
            "error": "nvidia-smi failed",
        },
    ],
    "processes": [],
    "memoryProcesses": [],
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
    shell.sampleIn("warning")
    wait.waitFor(sampled, 15000, warningOpen, "the warning sample ends")
  }
  // Warnings and the GPU row error, in the popup. main() picks a theme
  // whose red reads 2.70:1 on the popup: each text must lighten to read on
  // what it is drawn on.
  readonly property string gpuErrorText: FoamyI18n.t("NVIDIA readings unavailable. Check nvidia-smi.")
  // (Offscreen the popup's window is not shown, so `visible` is false
  // everywhere in it.)
  function shownText(view, text) {
    return shell.find(view, function(item) { return item.text === text && item.color !== undefined })
  }
  // The busy GPU's row in the Graphics table (the hidden GPU card shows
  // the same reading).
  function gpuRow(view) {
    return shell.find(view, function(item) { return item.modelData !== undefined && item.modelData.id === "0000:03:00.0" && item.fill !== undefined })
  }
  function warningOpen() {
    // Wait for the full sample the open starts as well: the helper's mode
    // changes only once it has ended.
    shell.seen = shell.exits
    a.open()
    wait.waitFor(function() {
      var view = a.popup.bodyItem
      var row = view ? shell.gpuRow(view) : null
      return shell.sampled() && row !== null && shell.shownText(row, view.percent(99)) !== null
    }, 15000, warningShown, "the popup shows the warning sample")
  }
  // `item` is in the warning state (not a plain colour) and reads on
  // `surface`; its colour as drawn ("#rrggbb": a stored colour value would
  // follow the property).
  function warns(item, surface, what) {
    var view = a.popup.bodyItem
    if (!item) {
      check(false, what + " is shown")
      return ""
    }
    var drawn = String(item.color)
    check(drawn !== String(view.foreground) && drawn !== String(view.dim), what + " is in the warning state (" + drawn + ")")
    shell.reads(item, surface, what)
    return drawn
  }
  function reads(item, surface, what) {
    var ratio = item ? FoamyPalette.contrastRatio(item.color, surface) : 0
    check(ratio >= 4.5, what + " reads (" + (item ? item.color + ", " + ratio.toFixed(2) + ":1" : "not shown") + ")")
  }
  // The texts of the busy GPU's row that warn: busy, temperature and VRAM.
  function rowTexts(view, row) {
    return [
      ["busy", shell.shownText(row, view.percent(99))],
      ["temperature", shell.shownText(row, view.degrees(100))],
      ["VRAM", shell.shownText(row, view.pair(16642998272 / 1073741824, 17179869184 / 1073741824))]
    ]
  }
  property var rowAtRest: []
  function warningShown() {
    var view = a.popup.bodyItem
    var card = a.popup.cardItem.color
    // The theme is in effect: the error red and the warning colour (the
    // theme's urgent colour with the default "Use theme warning colors")
    // do not read on the popup raw.
    var raw = FoamyPalette.contrastRatio(FoamyPalette.error, card)
    check(raw < 4.5, "the theme's raw red is under 4.5:1 on the popup (" + raw.toFixed(2) + ":1)")
    var rawWarning = FoamyPalette.contrastRatio(FoamyPalette.urgent, card)
    check(rawWarning < 4.5, "the theme's raw warning colour is under 4.5:1 on the popup (" + rawWarning.toFixed(2) + ":1)")
    shell.reads(shell.shownText(view, shell.gpuErrorText), card, "the GPU row error on the popup")
    // The cards that warn (with two GPUs the GPU cards are hidden): each
    // card's label, glyph, value and detail.
    ;["CPU", "RAM", FoamyI18n.t("CPU temperature")].forEach(function(label) {
      var tile = shell.find(view, function(item) { return item.label === label && item.glyph !== undefined && item.meterValue !== undefined })
      if (!tile) {
        check(false, "the " + label + " card is shown")
        return
      }
      shell.warns(shell.shownText(tile, tile.label), card, "the " + label + " card's label")
      shell.warns(shell.shownText(tile, tile.glyph), card, "the " + label + " card's glyph")
      shell.warns(shell.shownText(tile, tile.value), card, "the " + label + " card's value")
      shell.warns(shell.shownText(tile, tile.detail), card, "the " + label + " card's detail")
    })
    // A GPU row takes the cursor. At rest it draws nothing over the popup.
    var row = shell.gpuRow(view)
    check(!row.hasCursor, "the GPU row has no cursor at rest")
    shell.rowAtRest = shell.rowTexts(view, row).map(function(entry) {
      return shell.warns(entry[1], Qt.tint(card, row.color), "the GPU row's " + entry[0] + " warning at rest")
    })
    view.cursor.hover(row.stop)
    // Until the row's fill has settled at its cursor fill.
    wait.waitFor(function() { return row.hasCursor && row.color.a > 0 && Qt.colorEqual(row.color, row.fill) }, 5000, rowUnderCursor, "the cursor reaches the GPU row")
  }
  function rowUnderCursor() {
    var view = a.popup.bodyItem
    var card = a.popup.cardItem.color
    var row = shell.gpuRow(view)
    shell.rowTexts(view, row).forEach(function(entry, i) {
      var colour = shell.warns(entry[1], Qt.tint(card, row.color), "the GPU row's " + entry[0] + " warning under the cursor")
      check(colour !== "" && colour === shell.rowAtRest[i], "the GPU row's " + entry[0] + " warning keeps its colour under the cursor (" + shell.rowAtRest[i] + " at rest, " + colour + ")")
    })
    // The keys' cursor on the row (away and back): its accent border, the
    // same fill, so each warning keeps its colour. The plugin draws no fill
    // of its own for the keys: the colour checks guard against a kit change
    // that would give the keys' cursor another fill.
    view.moveRequested(0, 1)
    view.moveRequested(0, -1)
    wait.waitFor(function() { return row.hasCursor && Qt.colorEqual(row.color, row.fill) }, 5000, rowUnderKeys, "the keys' cursor is back on the GPU row")
  }
  function rowUnderKeys() {
    var view = a.popup.bodyItem
    var card = a.popup.cardItem.color
    var row = shell.gpuRow(view)
    check(row.byKeys && Qt.colorEqual(row.borderSpec.color, row.accent) && row.borderTop >= 1, "the keys' cursor on the GPU row draws its accent border")
    shell.rowTexts(view, row).forEach(function(entry, i) {
      var colour = shell.warns(entry[1], Qt.tint(card, row.color), "the GPU row's " + entry[0] + " warning under the keys' cursor")
      check(colour !== "" && colour === shell.rowAtRest[i], "the GPU row's " + entry[0] + " warning keeps its colour under the keys' cursor (" + shell.rowAtRest[i] + " at rest, " + colour + ")")
    })
    a.close()
    // A tick of the open popup may have started another sample: the mode
    // changes once none runs.
    wait.waitFor(function() { return !s.statsProcess.running }, 15000, function() {
      shell.sampleIn("missing")
      wait.waitFor(sampled, 15000, step3, "the missing-helper sample ends")
    }, "the popup's samples end")
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
    var ratio = line ? FoamyPalette.contrastRatio(line.color, a.popup.cardItem.color) : 0
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
    (run.out / "warning-sample").write_text(json.dumps(WARNING_SAMPLE) + "\n")
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
