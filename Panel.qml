import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "kit"
import "kit/FoamyPrefs.js" as Prefs
import "Preferences.js" as Schema

// One bar instance (4.5 draws one per monitor). Telemetry lives in the
// VitalsService singleton: the leader instance samples it (bar-only samples
// every 5 s while every popup is closed, full samples every 2 s while any is
// open) and owns the IPC target. Every instance draws its bar reading and its
// own lazily built popup.
Panel {
  id: root
  ipcTarget: "foamy.vitals"
  manageIpc: false

  readonly property bool vertical: bar && (bar.position === "left" || bar.position === "right")
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color barSurface: bar ? bar.background : Commons.Color.bar.background
  readonly property color barUrgent: readings.warningColor(barSurface, bar ? bar.urgent : Commons.Color.urgent)

  property bool editingSettings: false
  property string settingsError: ""
  property string launchError: ""
  property var lastSave: null
  property var queuedSaves: []
  readonly property bool saving: saveProcess.running
  readonly property alias popup: popup
  readonly property alias member: member
  readonly property alias readings: readings

  VitalsReadings { id: readings; settings: root.settings }

  FoamyMember { id: member; scope: VitalsService.memberScope; demanding: root.opened }

  // Bar readings every 5 s; the popup's every 2 s while any popup is open.
  Timer {
    running: member.isLeader
    repeat: true
    triggeredOnStart: true
    interval: member.demanded ? 2000 : 5000
    onTriggered: VitalsService.sample(member.demanded)
  }
  Timer {
    running: member.isLeader && member.demanded
    repeat: true
    interval: 60000
    onTriggered: VitalsService.refreshDisks()
  }

  FoamyIpc {
    target: "foamy.vitals"
    enabled: member.isLeader
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function settings(): void { root.openSettings() }
    function refresh(): void { root.refresh() }
  }

  onOpenedChanged: {
    if (opened) refresh()
    else editingSettings = false
  }

  function refresh() {
    VitalsService.sample(true)
    VitalsService.refreshDisks()
  }

  function openSettings() {
    editingSettings = true
    open()
  }
  function closeSettings() {
    editingSettings = false
  }

  function openBtop() {
    launchError = ""
    if (!btopCheck.running) btopCheck.running = true
  }
  // Checked on each use, so installing btop later needs no shell restart.
  Process {
    id: btopCheck
    command: ["sh", "-c", "command -v btop >/dev/null 2>&1"]
    onExited: function(code) {
      if (code !== 0) { root.launchError = "Install btop to use this shortcut."; return }
      Quickshell.execDetached(["omarchy-launch-or-focus-tui", "btop"])
      root.close()
    }
  }

  function save(key, next) {
    settingsError = ""
    lastSave = { key: key, value: next }
    var result = Prefs.save(root.bar, root.moduleName, Schema.fields, root.settings, key, next)
    if (!result.ok) {
      settingsError = FoamyI18n.t(result.error)
      return
    }
    if (result.command.length === 0) {
      lastSave = null
      return
    }
    queuedSaves = queuedSaves.concat([result.command])
    flushSaves()
  }
  function retrySave() {
    if (lastSave) save(lastSave.key, lastSave.value)
  }
  // CLI saves run one at a time, in order, so separate fields never race.
  function flushSaves() {
    if (saveProcess.running || queuedSaves.length === 0) return
    saveProcess.command = queuedSaves[0]
    queuedSaves = queuedSaves.slice(1)
    saveProcess.running = true
  }
  Process {
    id: saveProcess
    onExited: function(code) {
      if (code !== 0) root.settingsError = FoamyI18n.t("Couldn't save the setting.")
      else if (root.queuedSaves.length === 0) root.lastSave = null
      Qt.callLater(root.flushSaves)
    }
  }

  function percent(v) {
    return isNaN(v) ? "--" : FoamyI18n.number(Math.round(v)) + "%"
  }
  readonly property string barText: (readings.barMetric === "gpu" ? "GPU " : readings.barMetric === "memory" ? "RAM " : "CPU ")
    + percent(readings.barMetric === "gpu" ? readings.gpu : readings.barMetric === "memory" ? readings.memory : readings.cpu)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    active: readings.warning
    activeColor: root.barUrgent
    fixedWidth: root.vertical ? -1 : barLabel.labelWidth + Style.space(8)
    tooltipText: ""

    VitalsBarLabel {
      id: barLabel
      anchors.centerIn: parent
      text: root.barText
      warning: readings.warning
      foreground: root.barForeground
      warningColor: root.barUrgent
      fontFamily: root.fontFamily
    }

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openBtop()
      else root.toggle()
    }
  }

  FoamyPopup {
    id: popup
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.opened
    wide: true
    body: Component {
      VitalsView {
        settings: root.settings
        live: root.opened
        editingSettings: root.editingSettings
        saving: root.saving
        settingsError: root.settingsError
        retryable: root.settingsError !== "" && root.lastSave !== null
        launchError: root.launchError
        onCloseRequestedByView: root.editingSettings ? root.closeSettings() : root.close()
        onSettingsRequested: root.openSettings()
        onSettingsClosed: root.closeSettings()
        onRefreshRequested: root.refresh()
        onBtopRequested: root.openBtop()
        onSaveRequested: function(key, value) { root.save(key, value) }
        onRetryRequested: root.retrySave()
        onSwitchPanelRequested: function(direction) { root.switchPanel(direction) }
        onPasteFailed: function(message) { root.settingsError = message }
      }
    }
  }
}
