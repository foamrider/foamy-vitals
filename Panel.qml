import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "kit"
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

  readonly property bool editingSettings: settingsState.editing
  // Paste errors from the settings fields; save errors live in the queue.
  property string pasteError: ""
  readonly property string settingsError: pasteError || (prefs.error ? FoamyI18n.t(prefs.error) : "")
  property string launchError: ""
  readonly property bool saving: prefs.busy
  readonly property alias popup: popup
  readonly property alias member: member
  readonly property alias readings: readings
  readonly property alias prefs: prefs

  // Saves in order, inline or through the CLI; reads see a value at once.
  FoamyPrefsQueue {
    id: prefs
    bar: root.bar
    moduleName: root.moduleName
    fields: Schema.fields
    settings: root.settings
  }
  FoamySettingsState { id: settingsState; panel: root; onHidden: root.pasteError = "" }

  VitalsReadings { id: readings; settings: prefs.effectiveSettings }

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
  }

  function refresh() {
    VitalsService.sample(true)
    VitalsService.refreshDisks()
  }

  function openSettings() { settingsState.show() }
  function closeSettings() { settingsState.hide() }

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
    pasteError = ""
    prefs.save(key, next)
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
        settings: prefs.effectiveSettings
        live: root.opened
        editingSettings: root.editingSettings
        saving: root.saving
        settingsError: root.settingsError
        retryable: root.pasteError === "" && prefs.canRetry
        launchError: root.launchError
        onCloseRequestedByView: root.editingSettings ? root.closeSettings() : root.close()
        onSettingsRequested: root.openSettings()
        onSettingsClosed: root.closeSettings()
        onRefreshRequested: root.refresh()
        onBtopRequested: root.openBtop()
        onSaveRequested: function(key, value) { root.save(key, value) }
        onRetryRequested: prefs.retry()
        onSwitchPanelRequested: function(direction) { root.switchPanel(direction) }
        onPasteFailed: function(message) { root.pasteError = message }
      }
    }
  }
}
