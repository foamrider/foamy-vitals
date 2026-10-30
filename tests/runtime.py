#!/usr/bin/env python3
"""Offscreen runtime checks of the real Panel on both hosts (4.0 installed, 4.5 reference tree).

Two bar instances share one VitalsService: only the leader samples and owns
IPC, closed popups take bar-only samples, an open popup takes full ones,
the popup body is built on first open and its lists and charts stop
following samples while hidden, the single keyboard cursor walks the
dashboard, `s`/Esc enter and leave Settings, saves go inline, and on 4.5
Arabic mirrors the popup.

Uses the foamy-kit harness (temp HOME/XDG, no Wayland/Hyprland, no host
scripts): FOAMY_KIT=/path/to/foamy-kit (default ~/GitHub/Personal/foamy-kit).
stats.sh runs for real against this machine, with state in the temp dir.
  python3 tests/runtime.py [4.0|4.5|both]
"""
import os
import sys
from pathlib import Path

KIT = Path(os.environ.get("FOAMY_KIT", Path.home() / "GitHub/Personal/foamy-kit"))
sys.path.insert(0, str(KIT / "preview"))
import harness  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[1]

SHELL = r'''
import QtQuick
import Quickshell
import qs.Commons
import "harness"
import "plugin" as Vitals

ShellRoot {
  id: shell
  property int step: 0
  property int detailChanges: 0
  property int barSamples: 0
  property int fullSamples: 0
  property var frozenRows: null
  property int switched: 0
  readonly property bool modern: HOST === "4.5"
  function check(ok, what) { console.log((ok ? "FOAMY OK " : "FOAMY FAIL ") + what) }
  // PanelKeyCatcher (host code) turns keys into these signals; drive them
  // directly, since offscreen windows get no keyboard focus.
  function down(view) { view.moveRequested(0, 1) }
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
  Timer {
    interval: 700; repeat: true; running: true
    onTriggered: {
      var s = Vitals.VitalsService
      switch (shell.step++) {
      case 0:
        check(a.member.isLeader !== b.member.isLeader, "exactly one leader among two bar instances")
        check(s.statsProcess.command.indexOf("--bar") >= 0, "closed popups take bar-only samples")
        check(a.popup.bodyItem === null && b.popup.bodyItem === null, "popup bodies are not built before the first open")
        break
      case 1:
        if (shell.barSamples < 1) { shell.step--; break }
        check(s.telemetryError === "", "bar sample parsed: " + JSON.stringify(s.stats).slice(0, 120))
        check(!isNaN(a.readings.cpu) || s.samples === 1, "bar readings feed the bar text: " + a.barText)
        check(a.readings.warning === b.readings.warning, "both bars agree on the warning state")
        check(s.detail.processes.length === 0, "bar samples carry no process rankings")
        b.open()
        break
      case 2:
        if (shell.fullSamples < 1) { shell.step--; break }
        check(b.member.demanded && s.statsProcess.command.indexOf("--bar") < 0, "an open popup on a non-leader raises demand: full samples")
        check(b.popup.bodyItem !== null, "the body is built on first open")
        check(s.detail.processes.length > 0, "full sample ranks processes (" + s.detail.processes.length + ")")
        var view = b.popup.bodyItem
        check(view.live && view.processes.length === s.detail.processes.length, "the open view follows the rankings")
        check(view.cursor.index === -1, "no highlight on open")
        shell.down(view)
        check(view.cursor.index === 0, "Down: cursor on Settings")
        shell.down(view)
        check(view.cursor.index === 1, "next: first CPU process row")
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
        if (shell.modern) {  // 4.0's catcher passes no modifiers
          view.cursor.emitTextKey(view, "s", Qt.ControlModifier)
          check(!b.editingSettings, "Ctrl+S is not the s shortcut")
        }
        view.cursor.emitTextKey(view, "s")
        check(b.editingSettings && view.editingSettings, "s opens Settings")
        break
      case 3:
        var page = b.popup.bodyItem.settingsPage
        check(b.popup.bodyItem.editingSettings && page.implicitHeight > 0, "settings page in place of the dashboard")
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
        break
      case 4:
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
        break
      case 5: case 6: case 7: case 8: case 9: case 10: case 11:
        break  // about five seconds of closed popups
      case 12:
        check(shell.barSamples >= 1, "bar samples continue while closed (" + shell.barSamples + ")")
        check(shell.detailChanges === 1 && s.detail.processes.length === 0, "the first bar sample drops old rankings once, then no detail updates while closed (" + shell.detailChanges + ")")
        if (shell.modern) {
          a.open()
        }
        break
      case 13:
        if (shell.modern) {
          var body = a.popup.bodyItem
          check(body !== null, "leader popup built")
          check(Vitals.VitalsService.statsProcess.running || shell.fullSamples > 0, "open again: full samples")
          // The 4.5 run uses Arabic: the popup mirrors, Settings moves to the left edge.
          var header = null
          for (var i = 0; i < body.children.length; i++) if (body.children[i].settingsButton !== undefined) header = body.children[i]
          check(header && body.LayoutMirroring.enabled, "RTL: the popup body is mirrored")
          var at = header.settingsButton.mapToItem(body, 0, 0)
          check(at.x < body.width / 2, "RTL: Settings sits at the leading (left) edge (" + Math.round(at.x) + ")")
        }
        console.log("FOAMY DONE")
        Qt.quit()
      }
    }
  }
}
'''


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    status = 0
    for host in (["4.0", "4.5"] if which == "both" else [which]):
        qml = SHELL.replace("HOST", '"%s"' % host)
        run = harness.Run(host, qml, plugin=PLUGIN, language="ar" if host == "4.5" else None, prefix="foamy-vitals-runtime-")
        run.run(timeout=90)
        oks = run.log.count("FOAMY OK")
        if run.ok:
            print(f"{host}: {oks} checks passed")
            run.cleanup()
        else:
            status = 1
            print(f"{host}: FAILED ({oks} ok)\n" + "\n".join(run.failures() or run.log.splitlines()[-40:]))
            print(f"log kept at {run.base}")
    sys.exit(status)


if __name__ == "__main__":
    main()
