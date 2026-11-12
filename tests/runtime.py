#!/usr/bin/env python3
"""Offscreen runtime checks of the real Panel on Omarchy 4.5 (the installed shell).

Two bar instances share one VitalsService: only the leader samples and owns
IPC, closed popups take bar-only samples, an open popup takes full ones,
the popup body is built on first open and its lists and charts stop
following samples while hidden, the single keyboard cursor walks the
dashboard, `s`/Esc enter and leave Settings, saves go inline, and Arabic
mirrors the popup.

Uses the foamy-kit harness (temp HOME/XDG, no Wayland/Hyprland, no host
scripts): FOAMY_KIT=/path/to/foamy-kit (default ~/GitHub/Personal/foamy-kit).
The stats helper (bin/<arch>/foamy-vitals-helper) runs for real against this
machine, with state in the temp dir.
  python3 tests/runtime.py

The shell is /usr/share/omarchy/shell, or the tree in FOAMY_HOST_45.
"""

import os
import sys
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
# The installed shell is 4.5; set before the harness reads it.
os.environ.setdefault("FOAMY_HOST_45", "/usr/share/omarchy/shell")
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]

SHELL = r"""
import QtQuick
import Quickshell
import qs.Commons
import "harness"
import "plugin" as Vitals

ShellRoot {
  id: shell
  property int detailChanges: 0
  property int barSamples: 0
  property int fullSamples: 0
  property var frozenRows: null
  property int switched: 0
  function check(ok, what) { console.log((ok ? "FOAMY OK " : "FOAMY FAIL ") + what) }
  // PanelKeyCatcher (host code) turns keys into these signals; drive them
  // directly, since offscreen windows get no keyboard focus.
  function down(view) { view.moveRequested(0, 1) }
  // The first FoamyNumberField under item (the CPU usage warning).
  function named(item, name) {
    if (!item) return null
    if (item.objectName === name) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = named(kids[i], name); if (found) return found }
    return null
  }
  function numberField(item) {
    if (!item) return null
    if (item.committed !== undefined && item.field !== undefined && item.stepSize !== undefined) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = numberField(kids[i]); if (found) return found }
    return null
  }
  // The text of the kit field's error line (a Text shown while its `message`
  // is set), or "". The offscreen popup itself isn't visible, so don't ask.
  function alertText(item) {
    if (!item) return ""
    if (item.message !== undefined && item.text !== undefined && item.message !== "") return String(item.text)
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = alertText(kids[i]); if (found !== "") return found }
    return ""
  }
  MockBar {
    id: bar
    moduleName: "foamy.vitals"
    function switchPanelFrom(owner, direction) { shell.switched += direction; return true }
  }
  Connections {
    target: Vitals.VitalsService
    function onDetailChanged() { shell.detailChanges++ }
    function onStatsChanged() {
      var mode = Vitals.VitalsService.stats.mode
      if (mode === "bar") shell.barSamples++
      else if (mode === "full") shell.fullSamples++
    }
  }
  FloatingWindow {
    visible: true; implicitWidth: 300; implicitHeight: 60
    Vitals.Panel { id: a; bar: bar; moduleName: "foamy.vitals"; settings: ({ cpuWarning: 1 }); width: 100; height: 32 }
    Vitals.Panel { id: b; bar: bar; moduleName: "foamy.vitals"; settings: ({ cpuWarning: 1 }); x: 140; width: 100; height: 32 }
  }
  FoamyWait { id: wait }
  readonly property var s: Vitals.VitalsService
  // Each step waits for what it needs (bounded, a timeout fails the run)
  // instead of a fixed tick, so a loaded machine only makes it slower.
  Component.onCompleted: wait.waitFor(function() { return a.member.isLeader !== b.member.isLeader && s.statsProcess.command.length > 0 }, 15000, step0, "a leader starts sampling")
  function step0() {
    var s = shell.s
    check(a.member.isLeader !== b.member.isLeader, "exactly one leader among two bar instances")
    check(s.statsProcess.command.indexOf("--bar") >= 0, "closed popups take bar-only samples")
    check(s.statsProcess.command.indexOf("foamy-vitals-helper") >= 0 && s.statsProcess.command.indexOf("stats") >= 0
          && s.statsProcess.command[0] === "timeout", "samples run the native helper inside timeout")
    check(a.popup.bodyItem === null && b.popup.bodyItem === null, "popup bodies are not built before the first open")
    wait.waitFor(function() { return shell.barSamples >= 1 }, 20000, step1, "a first bar sample")
  }
  function step1() {
    var s = shell.s
    check(s.telemetryError === "", "bar sample parsed: " + JSON.stringify(s.stats).slice(0, 120))
    check(!isNaN(a.readings.cpu) || s.samples === 1, "bar readings feed the bar text: " + a.barText)
    check(a.readings.warning === b.readings.warning, "both bars agree on the warning state")
    check(s.detail.processes.length === 0, "bar samples carry no process rankings")
    b.open()
    wait.waitFor(function() { return shell.fullSamples >= 1 }, 20000, step2, "a first full sample")
  }
  function step2() {
    var s = shell.s
    check(b.member.demanded && s.statsProcess.command.indexOf("--bar") < 0, "an open popup on a non-leader raises demand: full samples")
    check(b.popup.bodyItem !== null, "the body is built on first open")
    check(s.detail.processes.length > 0, "full sample ranks processes (" + s.detail.processes.length + ")")
    var view = b.popup.bodyItem
    check(view.live && view.processes.length === s.detail.processes.length, "the open view follows the rankings")
    check(view.cursor.index === -1, "no highlight on open")
    // Two GPUs, injected in this tick: the Graphics rows join the cursor walk.
    s.parse(JSON.stringify(Object.assign({}, s.stats, {
      mode: "full", sampleTimeMs: s.sampledAt + 1,
      gpus: [
        { id: "0000:c1:00.0", vendor: "amd", driver: "amdgpu", name: null, type: "integrated", asleep: false, busy: 12,
          vramUsed: 536870912, vramTotal: 4294967296, memoryPrivate: null, frequencyMHz: null, thermals: [], error: "" },
        { id: "0000:05:00.0", vendor: "amd", driver: "amdgpu", name: null, type: "discrete", asleep: false, busy: 40,
          vramUsed: 2147483648, vramTotal: 17179869184, memoryPrivate: null, frequencyMHz: null, thermals: [], error: "" }
      ]
    })))
    check(view.graphicsStops === 2 && view.cpuStart === 3, "two GPUs: two Graphics stops before the CPU rows (" + view.graphicsStops + ", " + view.cpuStart + ")")
    check(b.readings.barGpu && b.readings.barGpu.id === "0000:05:00.0", "unpinned: the bar follows the busiest GPU")
    shell.down(view)
    check(view.cursor.index === 0, "Down: cursor on Settings")
    shell.down(view)
    check(view.cursor.index === 1 && view.cursor.index === view.graphicsStart, "next: first GPU row")
    var pinSaves = bar.saved.length
    view.activateRequested()
    var pinSaved = bar.saved[bar.saved.length - 1]
    check(bar.saved.length === pinSaves + 1 && pinSaved.settings.pinnedGpu === "0000:c1:00.0", "Enter on a GPU row pins it (" + JSON.stringify(pinSaved && pinSaved.settings) + ")")
    check(b.readings.barGpu && b.readings.barGpu.id === "0000:c1:00.0", "the bar follows the pinned GPU once saved")
    // The cursor is on the second GPU row; losing a GPU must not leave it on a process row.
    view.cursor.hover(2)
    s.parse(JSON.stringify(Object.assign({}, s.stats, {
      mode: "full", sampleTimeMs: s.sampledAt + 2, gpus: s.stats.gpus.slice(0, 1)
    })))
    check(view.graphicsStops === 0 && view.cursor.index < view.cpuStart, "one GPU left: the cursor is not on a process row (" + view.cursor.index + ", cpuStart " + view.cpuStart + ")")
    check(view.cursor.index === -1, "no GPU rows left: the cursor resets (" + view.cursor.index + ")")
    var twoGpus = [
      { id: "0000:c1:00.0", vendor: "amd", driver: "amdgpu", name: null, type: "integrated", asleep: false, busy: 12,
        vramUsed: 536870912, vramTotal: 4294967296, memoryPrivate: null, frequencyMHz: null, thermals: [], error: "" },
      { id: "0000:05:00.0", vendor: "amd", driver: "amdgpu", name: null, type: "discrete", asleep: false, busy: 40,
        vramUsed: 2147483648, vramTotal: 17179869184, memoryPrivate: null, frequencyMHz: null, thermals: [], error: "" }
    ]
    s.parse(JSON.stringify(Object.assign({}, s.stats, { mode: "full", sampleTimeMs: s.sampledAt + 3, gpus: twoGpus })))
    // On a process row, a change in the GPU count shifts every row after
    // the Graphics rows: the cursor resets instead of drifting.
    view.cursor.hover(view.cpuStart)
    var onProcess = view.cursor.index
    s.parse(JSON.stringify(Object.assign({}, s.stats, { mode: "full", sampleTimeMs: s.sampledAt + 4, gpus: twoGpus.slice(0, 1) })))
    check(onProcess === 3 && view.cursor.index === -1, "a process row under the cursor: a GPU count change resets it (" + onProcess + " -> " + view.cursor.index + ")")
    s.parse(JSON.stringify(Object.assign({}, s.stats, { mode: "full", sampleTimeMs: s.sampledAt + 5, gpus: twoGpus })))
    view.cursor.hover(1)
    shell.down(view)
    shell.down(view)
    check(view.cursor.index === view.cpuStart, "next: first CPU process row")
    for (var i = 0; i < 40; i++) shell.down(view)
    check(view.cursor.index === view.btopStop, "Down walks to btop at the end (" + view.cursor.index + ")")
    check(view.flick.contentY > 0 || view.flick.contentHeight <= view.flick.height, "keyboard cursor scrolls into view")
    view.moveRequested(0, -1)
    check(view.cursor.index === view.coresStop, "Up from btop: the cores strip")
    view.moveRequested(1, 0)
    view.moveRequested(1, 0)
    check(view.hoveredCore === 2, "Right picks a thread on the cores strip")
    view.tabRequested(1)
    check(shell.switched === 1, "Tab switches to the next bar panel")
    view.cursor.emitTextKey(view, "s", Qt.ControlModifier)
    check(!b.editingSettings, "Ctrl+S is not the s shortcut")
    view.cursor.emitTextKey(view, "s")
    check(b.editingSettings && view.editingSettings, "s opens Settings")
    wait.waitFor(function() { return shell.numberField(b.popup.bodyItem.settingsPage) !== null }, 10000, step3, "the settings page is built")
  }
  function step3() {
    var s = shell.s
    var page = b.popup.bodyItem.settingsPage
    var cpuField = shell.numberField(page)
    check(cpuField && cpuField.value === 1, "the CPU warning field shows the saved value")
    var savesBefore = bar.saved.length
    b.settings = ({ cpuWarning: 77 })  // another monitor or the CLI changed it
    check(cpuField.value === 77 && cpuField.field.text === "77", "an external change shows while Settings is open (" + cpuField.field.text + ")")
    cpuField.field.focusInput()
    cpuField.field.editingFinished()   // focus then leave without typing
    check(bar.saved.length === savesBefore, "leaving the field unchanged saves nothing")
    check(cpuField.value === 77 && cpuField.field.text === "77", "the field keeps following the setting")
    b.settings = ({ cpuWarning: 66 })
    check(cpuField.field.text === "66", "and keeps following after an edit cycle")
    cpuField.field.pasteError = "Clipboard paste timed out. Copy the text again."
    check(shell.alertText(cpuField) === "Clipboard paste timed out. Copy the text again.", "a refused paste in a number field shows in the field (" + shell.alertText(cpuField) + ")")
    check(b.settingsError === "", "and not as a settings save error")
    cpuField.field.pasteError = ""
    check(shell.alertText(cpuField) === "", "the field's error line clears with it")
    check(b.popup.bodyItem.editingSettings && page.implicitHeight > 0, "settings page in place of the dashboard")
    var barChoice = shell.named(page, "barDisplay")
    check(barChoice && barChoice.compact && barChoice.label === "Show in the bar", "Show in the bar is the kit dropdown row")
    check(barChoice && Math.abs(barChoice.height - cpuField.height) <= 1, "the dropdown row is as tall as a number row (" + (barChoice ? barChoice.height : -1) + " vs " + cpuField.height + ")")
    b.save("cpuWarning", 80)
    var saved = bar.saved[bar.saved.length - 1]
    check(saved && saved.id === "foamy.vitals" && saved.settings.cpuWarning === 80, "inline save of the full entry")
    check(b.readings.preference("cpuWarning") === 80, "the saved value applies before the host reflects it")
    b.save("barDisplay", "memory")
    saved = bar.saved[bar.saved.length - 1]
    check(saved.settings.cpuWarning === 80 && saved.settings.barDisplay === "memory", "a second quick save keeps the first")
    b.save("cpuWarning", 0)
    check(b.settingsError !== "", "out-of-range value refused: " + b.settingsError)
    page.back()
    check(!b.editingSettings, "Esc leaves Settings")
    b.popup.bodyItem.closeRequested()
    check(!b.opened, "Esc closes the popup")
    a.openSettings()
    check(a.opened && a.editingSettings, "openSettings() on a closed popup lands on Settings")
    a.close()
    check(!a.editingSettings, "closing resets to the dashboard")
    // openSettings() above asked for a full sample; under load it is
    // still running here, and its rankings would land after the reset below.
    wait.waitFor(function() { return !s.statsProcess.running }, 15000, step4, "the last full sample ends")
  }
  function step4() {
    var s = shell.s
    var hidden = b.popup.bodyItem
    check(hidden !== null && !hidden.live, "the closed body is kept but no longer live")
    check(hidden.processes.length === 0 && hidden.cores.length === 0, "hidden lists read constants")
    check(!b.member.demanded, "demand drops when the popup closes")
    s.detail = { processes: [{ name: "stale", where: "", pct: 50 }], memoryProcesses: [] }
    s.fail("Telemetry unavailable. Retrying…")
    check(s.detail.processes.length === 0 && s.detail.memoryProcesses.length === 0, "a failed sample clears the rankings")
    s.detail = { processes: [{ name: "stale", where: "", pct: 50 }], memoryProcesses: [] }
    shell.detailChanges = 0
    shell.barSamples = 0
    // Bar samples come every 5 s while every popup is closed; two of them
    // show that only the first one changes the (dropped) rankings.
    wait.waitFor(function() { return shell.barSamples >= 2 }, 30000, step5, "two bar samples while closed")
  }
  function step5() {
    var s = shell.s
    check(shell.barSamples >= 2, "bar samples continue while closed (" + shell.barSamples + ")")
    check(shell.detailChanges === 1 && s.detail.processes.length === 0, "the first bar sample drops old rankings once, then no detail updates while closed (" + shell.detailChanges + ")")
    var fullBefore = shell.fullSamples
    a.open()
    wait.waitFor(function() { return a.popup.bodyItem !== null && shell.fullSamples > fullBefore && a.popup.bodyItem.width > 0 }, 15000, step6, "reopening takes a full sample")
  }
  function step6() {
    var body = a.popup.bodyItem
    check(body !== null, "leader popup built")
    check(body.band.x === 0 && body.band.width === body.width, "the hero band is full bleed")
    check(body.band.hasHero, "the band carries the host line")
    // The run uses Arabic: the popup mirrors, Settings moves to the left edge.
    var header = body.band
    check(header && body.LayoutMirroring.enabled, "RTL: the popup body is mirrored")
    var at = header.settingsButton.mapToItem(body, 0, 0)
    check(at.x < body.width / 2, "RTL: Settings sits at the leading (left) edge (" + Math.round(at.x) + ")")
    console.log("FOAMY DONE")
    Qt.quit()
  }
}
"""


def main():
    run = harness.Run("4.5", SHELL, plugin=PLUGIN, language="ar", prefix="foamy-vitals-runtime-")
    run.run(timeout=120)
    oks = run.log.count("FOAMY OK")
    # The real helper, df and hostnamectl run; no Omarchy command may.
    for line in run.host_commands():
        run.ok = False
        print(f"host command reached: {line}")
    if run.ok:
        print(f"4.5: {oks} checks passed")
        run.cleanup()
        return 0
    print(f"4.5: FAILED ({oks} ok)\n" + "\n".join(run.failures() or run.log.splitlines()[-40:]))
    print(f"log kept at {run.base}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
