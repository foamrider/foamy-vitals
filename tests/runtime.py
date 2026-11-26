#!/usr/bin/env python3
"""Offscreen runtime checks of the real Panel on Omarchy 4.5 (the installed shell).

Two bar instances share one VitalsService: only the leader samples and owns
the foamy.vitals IPC target (called with `qs ipc`), closed popups take
bar-only samples, an open popup takes full ones, the popup body is built on
first open and its lists and charts stop following samples while hidden, the
single keyboard cursor walks the dashboard, `s`/Esc enter and leave Settings,
saves go inline, a popup closed on Settings and opened again closes on a real
Esc (the popups draw into a simulated screen, so their window has the keys),
and Arabic mirrors the popup.

Uses the foamy-kit harness (temp HOME/XDG, no Wayland/Hyprland, no host
scripts): FOAMY_KIT=/path/to/foamy-kit (default ~/GitHub/Personal/foamy-kit).
The stats helper (bin/<arch>/foamy-vitals-helper) runs for real against this
machine, with state in the temp dir.
  python3 tests/runtime.py

The shell is /usr/share/omarchy/shell, or the tree in FOAMY_HOST_45.
"""

import os
import subprocess
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
import QtTest
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
  // PanelKeyCatcher (host code) turns keys into these signals; most steps
  // drive them directly. step6 presses a real Esc.
  function down(view) { view.moveRequested(0, 1) }
  // Whether the item that has the keys in panel's popup window is
  // `ancestor` or inside it.
  function keysIn(panel, ancestor) {
    var body = panel.popup.bodyItem
    var window = body ? body.Window.window : null
    for (var at = window ? window.activeFocusItem : null; at; at = at.parent)
      if (at === ancestor) return true
    return false
  }
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
  // The settings row of the number setting `key`.
  function numberRow(item, key) {
    if (!item) return null
    if (item.key === key && item.committed !== undefined) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = numberRow(kids[i], key); if (found) return found }
    return null
  }
  // The text of the kit field's error line (a Text shown while its `message`
  // is set), or "".
  function alertText(item) {
    if (!item) return ""
    if (item.message !== undefined && item.text !== undefined && item.message !== "") return String(item.text)
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = alertText(kids[i]); if (found !== "") return found }
    return ""
  }
  // The host WidgetButton of a Panel: what a click on the bar reaches,
  // and whose `active` draws the bar in the warning colour.
  function button(item) {
    if (!item) return null
    if (typeof item.triggerPress === "function" && item.activeColor !== undefined) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = button(kids[i]); if (found) return found }
    return null
  }
  // A full sample like the last one with `extra` on top, newer than it.
  function inject(extra) {
    s.parse(JSON.stringify(Object.assign({}, s.stats, { mode: "full", sampleTimeMs: s.sampledAt + 1 }, extra)))
  }
  // The first item under `item` whose text is `text`.
  function withText(item, text) {
    if (!item) return null
    if (item.text === text) return item
    var kids = item.children || []
    for (var i = 0; i < kids.length; i++) { var found = withText(kids[i], text); if (found) return found }
    return null
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
    visible: true; implicitWidth: 1920; implicitHeight: 1080
    // The screen the popups draw into, so an open popup has this window's keys.
    ScreenSim {}
    Vitals.Panel { id: a; bar: bar; moduleName: "foamy.vitals"; settings: ({ cpuWarning: 1 }); width: 100; height: 32 }
    Vitals.Panel { id: b; bar: bar; moduleName: "foamy.vitals"; settings: ({ cpuWarning: 1 }); x: 140; width: 100; height: 32 }
    Item { TestEvent { id: ev } }
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
    // Each GPU's chart line has its own colour; slot 0 keeps the single-GPU one.
    var slots = [1, 2, 3].map(function(slot) { return view.gpuSlotColor(slot) })
    check(Qt.colorEqual(view.gpuSlotColor(0), view.gpuColor), "the first GPU line keeps the GPU colour")
    check(!Qt.colorEqual(slots[0], slots[1]) && !Qt.colorEqual(slots[1], slots[2]) && !Qt.colorEqual(slots[0], slots[2])
          && slots.every(function(c) { return [view.cpuColor, view.memoryColor, view.gpuColor].every(function(o) { return !Qt.colorEqual(c, o) }) }),
          "later GPU lines differ from each other and from the CPU, RAM and GPU lines")
    check(Qt.colorEqual(view.gpuSlotColor(4), slots[0]), "the GPU line colours cycle")
    // What the bar shows and when it warns, on both bars. The CPU
    // temperature is cleared so a hot test machine does not warn.
    var twoGpusFor = function(vramC1) {
      return [
        { id: "0000:c1:00.0", vendor: "amd", driver: "amdgpu", name: null, type: "integrated", asleep: false, busy: 12,
          vramUsed: vramC1, vramTotal: 4294967296, memoryPrivate: null, frequencyMHz: null, thermals: [], error: "" },
        { id: "0000:05:00.0", vendor: "amd", driver: "amdgpu", name: null, type: "discrete", asleep: false, busy: 40,
          vramUsed: 2147483648, vramTotal: 17179869184, memoryPrivate: null, frequencyMHz: null, thermals: [], error: "" }
      ]
    }
    var calm = { memUsedKb: 10, memTotalKb: 100, cpuTemp: null, cpuTempCrit: null }
    // Both panels use cpuWarning 1, so CPU 1 % is exactly at the threshold.
    shell.inject(Object.assign({ cpu: 1, gpus: twoGpusFor(536870912) }, calm))
    check(b.barText === "GPU " + view.percent(40), "unpinned: the bar shows the busiest GPU (" + b.barText + ")")
    check(a.barText === b.barText, "both bars show the same reading (" + a.barText + ")")
    check(shell.button(a).active && shell.button(b).active, "CPU at its warning threshold warns on both bars")
    shell.inject(Object.assign({ cpu: 0, gpus: twoGpusFor(536870912) }, calm))
    check(!shell.button(a).active && !shell.button(b).active, "every reading below its threshold: no warning")
    // 99 % VRAM on the GPU the bar does not show.
    shell.inject(Object.assign({ cpu: 0, gpus: twoGpusFor(4252017623) }, calm))
    check(b.barText === "GPU " + view.percent(40) && shell.button(a).active && shell.button(b).active,
          "any GPU past its threshold warns, not only the bar GPU")
    shell.inject(Object.assign({ cpu: 0, gpus: twoGpusFor(536870912) }, calm))
    shell.down(view)
    check(view.cursor.index === 0, "Down: cursor on Settings")
    shell.down(view)
    check(view.cursor.index === 1 && view.cursor.index === view.graphicsStart, "next: first GPU row")
    var pinSaves = bar.saved.length
    view.activateRequested()
    var pinSaved = bar.saved[bar.saved.length - 1]
    check(bar.saved.length === pinSaves + 1 && pinSaved.settings.pinnedGpu === "0000:c1:00.0", "Enter on a GPU row pins it (" + JSON.stringify(pinSaved && pinSaved.settings) + ")")
    // The pin is saved from b's popup; a's settings come from the host, which this harness never updates.
    check(b.barText === "GPU " + view.percent(12), "the bar follows the pinned GPU once saved (" + b.barText + ")")
    view.activateRequested()
    check(bar.saved[bar.saved.length - 1].settings.pinnedGpu === "" && b.barText === "GPU " + view.percent(40),
          "Enter on the pinned row unpins it")
    view.activateRequested()
    view.cursor.hover(2)
    view.activateRequested()
    check(bar.saved[bar.saved.length - 1].settings.pinnedGpu === "0000:05:00.0", "Enter on another row moves the pin")
    view.cursor.hover(1)
    view.activateRequested()
    check(bar.saved[bar.saved.length - 1].settings.pinnedGpu === "0000:c1:00.0", "and back")
    // The cursor is on the second GPU row; losing a GPU must not leave it on a process row.
    view.cursor.hover(2)
    s.parse(JSON.stringify(Object.assign({}, s.stats, {
      mode: "full", sampleTimeMs: s.sampledAt + 2, gpus: s.stats.gpus.slice(0, 1)
    })))
    check(view.graphicsStops === 0 && view.cursor.index < view.cpuStart, "one GPU left: the cursor is not on a process row (" + view.cursor.index + ", cpuStart " + view.cpuStart + ")")
    // The history also holds this machine's real GPUs, so only the order of these two is checked.
    check(view.gpuChartIds[0] === "0000:c1:00.0" && view.gpuChartIds.indexOf("0000:05:00.0") > 0,
          "the current GPU's line comes before a vanished one (" + JSON.stringify(view.gpuChartIds) + ")")
    check(view.cursor.index === -1, "no GPU rows left: the cursor resets (" + view.cursor.index + ")")
    // A GPU whose hottest sensor has no known limit: the GPU temperature
    // card names the sensor and says so. Both parts must show, inside the
    // card's value row and so above its meter, not over it.
    shell.inject({ gpus: [
      { id: "0000:00:02.0", vendor: "intel", driver: "xe", name: null, type: "integrated", asleep: false, busy: 5,
        vramUsed: null, vramTotal: null, memoryPrivate: 1048576, frequencyMHz: 1250,
        thermals: [{ temperature: 71, critical: null, label: "vram_ch_1" }], error: "" }
    ] })
    var sensorDetail = shell.withText(view, "VRAM channel 1 · Limit unknown")
    check(sensorDetail !== null && !sensorDetail.truncated
          && sensorDetail.y + sensorDetail.height <= sensorDetail.parent.height + 0.5,
          "the GPU temperature card shows its whole detail line above the meter ("
          + (sensorDetail ? "cut " + sensorDetail.truncated + ", lines " + sensorDetail.lineCount
             + ", bottom " + (sensorDetail.y + sensorDetail.height).toFixed(1) + " of " + sensorDetail.parent.height.toFixed(1) : "not found") + ")")
    // A label the view does not know is shown as the driver wrote it, at
    // any length: it stops after two lines, so the card grows no more than
    // for the two-line detail above.
    var twoLineRow = sensorDetail ? sensorDetail.parent.height : 0
    var longLabel = "Hotspot of the second compute tile, in the words of some later driver release"
    shell.inject({ gpus: [
      { id: "0000:00:02.0", vendor: "intel", driver: "xe", name: null, type: "integrated", asleep: false, busy: 5,
        vramUsed: null, vramTotal: null, memoryPrivate: 1048576, frequencyMHz: 1250,
        thermals: [{ temperature: 71, critical: 100, label: longLabel }], error: "" }
    ] })
    var longDetail = shell.withText(view, longLabel)
    check(longDetail !== null && longDetail.lineCount <= 2 && longDetail.parent.height <= twoLineRow + 0.5,
          "a long sensor label stops after two lines (" + (longDetail ? "lines " + longDetail.lineCount
          + ", row " + longDetail.parent.height.toFixed(1) + " of " + twoLineRow.toFixed(1) : "not found") + ")")
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
    // A process name is drawn as plain text, never as markup.
    var kept = s.detail
    s.detail = { processes: [{ name: "<b>x</b>", where: "", pct: 1 }], memoryProcesses: [] }
    var nameText = shell.withText(view.stopItem(view.cpuStart), "<b>x</b>")
    check(nameText !== null && nameText.textFormat === Text.PlainText, "a process name is plain text")
    s.detail = kept
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
    // A number typed into a row and confirmed with Enter is saved under
    // that row's key; an out-of-range one is not saved.
    var memoryField = shell.numberRow(page, "memoryWarning")
    var savesBeforeTyped = bar.saved.length
    memoryField.field.text = "42"
    memoryField.field.textEdited()
    memoryField.field.accepted()
    var typed = bar.saved.slice(savesBeforeTyped).map(function(entry) { return entry.settings })
    check(typed.length === 1 && typed[0].memoryWarning === 42 && typed[0].cpuWarning !== 42,
          "a typed RAM warning is saved under its own key (" + JSON.stringify(typed) + ")")
    var savesBeforeInvalid = bar.saved.length
    memoryField.field.text = "0"
    memoryField.field.textEdited()
    memoryField.field.accepted()
    check(bar.saved.length === savesBeforeInvalid && shell.alertText(memoryField) !== "",
          "a typed RAM warning out of range is not saved and the field says why (" + shell.alertText(memoryField) + ")")
    memoryField.handleKey(Qt.Key_Escape)
    check(b.popup.bodyItem.editingSettings && page.implicitHeight > 0, "settings page in place of the dashboard")
    var barChoice = shell.named(page, "barDisplay")
    check(barChoice !== null && Math.abs(barChoice.height - cpuField.height) <= 1, "the dropdown row is as tall as a number row (" + (barChoice ? barChoice.height : -1) + " vs " + cpuField.height + ")")
    // Without a bar entry the queue refuses every save. A refused pick
    // must give way at once, not stay on show beside the error for the
    // dropdown's 10 s pick hold.
    var savedChoice = barChoice.value
    var savesBeforePick = bar.saved.length
    b.moduleName = ""
    barChoice.select("cpu")
    check(bar.saved.length === savesBeforePick && b.prefs.error !== "" && barChoice.shownValue === savedChoice,
          "a refused pick shows the saved choice again at once (shows " + barChoice.shownValue + ", saved " + savedChoice + ", error " + JSON.stringify(b.prefs.error) + ")")
    // A typed number refused the same way gives way too: the row shows the
    // saved number again at once, not the refused one beside the error.
    var refusedRow = shell.numberRow(page, "gpuWarning")
    var savedNumber = refusedRow.field.text
    refusedRow.field.text = "55"
    refusedRow.field.textEdited()
    refusedRow.field.accepted()
    check(refusedRow.value !== 55 && bar.saved.length === savesBeforePick && refusedRow.field.text === savedNumber,
          "a refused number shows the saved number again at once (shows " + refusedRow.field.text + ", saved " + savedNumber + ", error " + JSON.stringify(b.prefs.error) + ")")
    b.moduleName = "foamy.vitals"
    b.save("cpuWarning", 80)
    var saved = bar.saved[bar.saved.length - 1]
    check(saved && saved.id === "foamy.vitals" && saved.settings.cpuWarning === 80, "inline save of the full entry")
    check(b.readings.preference("cpuWarning") === 80, "the saved value applies before the host reflects it")
    b.save("barDisplay", "memory")
    check(b.barText.indexOf("RAM ") === 0, "Show in the bar: RAM applies at once (" + b.barText + ")")
    var savesBeforeRefused = bar.saved.length
    b.save("cpuWarning", 0)
    check(b.settingsError !== "" && bar.saved.length === savesBeforeRefused, "out-of-range value refused, nothing saved: " + b.settingsError)
    page.back()
    check(!b.editingSettings, "Esc leaves Settings")
    b.popup.bodyItem.closeRequested()
    check(!b.opened, "Esc closes the popup")
    a.openSettings()
    check(a.opened && a.editingSettings, "openSettings() on a closed popup lands on Settings")
    // The popup closes while the settings page holds the keys; step6 checks
    // that Esc closes it when it opens again.
    wait.waitFor(function() { return shell.keysIn(a, a.popup.bodyItem.settingsPage) }, 10000, function() {
      a.close()
      check(!a.editingSettings, "closing resets to the dashboard")
      // openSettings() above asked for a full sample; under load it is
      // still running here, and its rankings would land after the reset below.
      wait.waitFor(function() { return !s.statsProcess.running }, 15000, step4, "the last full sample ends")
    }, "the settings page has the keys")
  }
  function step4() {
    var s = shell.s
    var hidden = b.popup.bodyItem
    check(hidden !== null && !hidden.live, "the closed body is kept but no longer live")
    check(hidden.processes.length === 0 && hidden.cores.length === 0, "hidden lists read constants")
    check(!b.member.demanded, "demand drops when the popup closes")
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
    // A left-click on the bar opens the popup.
    shell.button(a).triggerPress(Qt.LeftButton)
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
    // This popup closed on Settings in step3. On open the host gives the
    // popup body the keys. Were they still on the hidden settings page, the
    // first Esc would go to its Back and do nothing.
    wait.waitFor(function() { return shell.keysIn(a, body) }, 10000, function() {
      ev.keyClick(Qt.Key_Escape, Qt.NoModifier, -1)
      check(!a.opened, "Esc closes the popup that was closed on Settings (keys on the hidden settings page: "
            + shell.keysIn(a, body.settingsPage) + ")")
      // Here main() calls `qs ipc call foamy.vitals settings`, which must
      // open a closed popup: close it also when the Esc above did not.
      a.close()
      console.log("FOAMY IPC READY")
      wait.waitFor(function() { return a.opened || b.opened }, 15000, step7, "the IPC call opens a popup")
    }, "the reopened popup's body has the keys")
  }
  function step7() {
    var leader = a.member.isLeader ? a : b
    var other = leader === a ? b : a
    check(leader.opened && leader.editingSettings && !other.opened, "foamy.vitals settings opens Settings on the leader only")
    console.log("FOAMY DONE")
    Qt.quit()
  }
}
"""


IPC_FUNCTIONS = {"open", "close", "toggle", "settings", "refresh"}


def ipc(run):
    """Once the scene is ready: one foamy.vitals target with its five
    functions, then call `settings`. Returns the failures."""
    if not run.wait_for_log("FOAMY IPC READY", 120):
        return ["no FOAMY IPC READY before the IPC call"]
    by_id = Path(run.env["XDG_RUNTIME_DIR"]) / "quickshell/by-id"
    ids = [p.name for p in by_id.iterdir()] if by_id.is_dir() else []
    if len(ids) != 1:
        return [f"expected one quickshell instance, found {ids}"]

    def qs(*args):
        return subprocess.run(
            ["qs", "ipc", "-i", ids[0], *args], env=run.env, capture_output=True, text=True, timeout=10
        )

    try:
        shown = qs("show")
        called = qs("call", "foamy.vitals", "settings") if shown.returncode == 0 else None
    except subprocess.TimeoutExpired as error:
        return [f"qs ipc did not answer: {error}"]
    if shown.returncode != 0:
        return ["qs ipc show failed: " + shown.stderr.strip()[-300:]]
    # `show` prints each target, then its functions indented below it.
    targets, functions, current = [], set(), None
    for line in shown.stdout.splitlines():
        words = line.split()
        if words[:1] == ["target"]:
            current = words[1] if len(words) > 1 else ""
            targets.append(current)
        elif words[:1] == ["function"] and current == "foamy.vitals":
            functions.add(words[1].split("(")[0])
    failures = []
    if targets.count("foamy.vitals") != 1:
        failures.append(f"two Panels register one foamy.vitals target, not {targets.count('foamy.vitals')}")
    if functions != IPC_FUNCTIONS:
        failures.append(f"foamy.vitals has open, close, toggle, settings and refresh, not {sorted(functions)}")
    if called.returncode != 0:
        failures.append("qs ipc call foamy.vitals settings failed: " + called.stderr.strip()[-300:])
    return failures


def main():
    run = harness.Run("4.5", SHELL, plugin=PLUGIN, language="ar", prefix="foamy-vitals-runtime-")
    run.start()
    try:
        failures = ipc(run)
        run.wait(60)
    finally:
        run.stop()
    oks = run.log.count("FOAMY OK")
    # The real helper, df and hostnamectl run; no Omarchy command may.
    failures += [f"host command reached: {line}" for line in run.host_commands()]
    failures += run.failures()
    # Quickshell keeps the first handler of a target and warns about each
    # later one: a non-leader that registers too shows up only here.
    if "another handler is registered for target foamy.vitals" in run.log:
        failures.append("FOAMY FAIL only the leader registers foamy.vitals")
    if "FOAMY DONE" in run.log and not failures:
        print(f"4.5: {oks} checks passed")
        run.cleanup()
        return 0
    print(f"4.5: FAILED ({oks} ok)\n" + "\n".join(failures or run.log.splitlines()[-40:]))
    print(f"log kept at {run.base}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
