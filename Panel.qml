import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "History.js" as History
import "Filesystems.js" as Filesystems
import "Thermals.js" as Thermals
import "Preferences.js" as Preferences

Panel {
  id: root

  moduleName: "foamy.vitals"
  ipcTarget: "foamy.vitals"

  manageIpc: false
  readonly property bool vertical: bar && (bar.position === "left" || bar.position === "right")
  readonly property color foreground: Color.popups.text
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.tint(Color.popups.background, Qt.alpha(foreground, 0.70))
  readonly property color outline: Qt.tint(Color.popups.background, Qt.alpha(foreground, 0.18))
  readonly property string panelFont: "sans-serif"
  readonly property string language: Preferences.language(preference("language"), Qt.locale().name)
  function preference(key) { return Preferences.value(settings, key) }
  function tr(label) { return Preferences.text(label, language) }
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string statsCommand: decodeURIComponent(Qt.resolvedUrl("stats.sh").toString().replace(/^file:\/\//, ""))
  property string telemetryError: ""
  property string launchError: ""

  property string hostname: "Vitals"
  property string chassis: ""
  readonly property string deviceIcon: ({
    desktop: "monitor", laptop: "laptop", convertible: "laptop",
    server: "server", tablet: "tablet", handset: "smartphone",
    watch: "watch", embedded: "cpu", vm: "box", container: "box"
  })[chassis] || "monitor"
  property var disks: []
  property string diskStatus: "Reading disk capacity"
  property string loadAverage: "--"
  property string uptime: "--"

  function refreshDisks() {
    if (!diskProcess.running) diskProcess.running = true
  }

  function diskCapacity(usedKb, totalKb) {
    var divisor = totalKb >= 1073741824 ? 1073741824 : 1048576
    return (usedKb / divisor).toFixed(1) + " / " + (totalKb / divisor).toFixed(1)
      + (divisor === 1073741824 ? " TiB" : " GiB")
  }

  FileView {
    path: "/proc/sys/kernel/hostname"
    onLoaded: root.hostname = text().trim() || "Vitals"
  }
  // Chassis detection is stable for the session; keep it off the telemetry poll.
  Process {
    command: ["timeout", "3s", "hostnamectl", "chassis"]
    running: true
    stdout: StdioCollector { onStreamFinished: root.chassis = text.trim() }
    onExited: function(code) {
      if (code !== 0) {
        root.chassis = ""
        console.warn(root.moduleName + ": chassis query failed", code)
      }
    }
  }
  FileView {
    id: uptimeFile
    path: "/proc/uptime"
    onLoaded: {
      var minutes = Math.floor(Number(text().split(" ")[0]) / 60)
      root.uptime = Math.floor(minutes / 60) + "h " + (minutes % 60) + "m"
    }
  }
  FileView {
    id: loadFile
    path: "/proc/loadavg"
    onLoaded: root.loadAverage = text().trim().split(/\s+/).slice(0, 3).join(" / ")
  }
  Process {
    id: diskProcess
    command: ["timeout", "3s", "df", "-P", "-k", "-l", "-T"]
    environment: ({LC_ALL: "C"})
    stdout: StdioCollector {
      onStreamFinished: {
        root.disks = Filesystems.parse(text)
        root.diskStatus = root.disks.length ? "" : "No mounted local disks"
      }
    }
    onExited: function(code) {
      if (code !== 0) {
        root.disks = []
        root.diskStatus = "Disk capacity unavailable"
        console.warn(root.moduleName + ": disk capacity query failed", code)
      }
    }
  }
  // Capacity changes slowly; avoid filesystem queries on every telemetry poll.
  Timer { interval: 60000; running: root.opened; repeat: true; onTriggered: root.refreshDisks() }

  property var stats: ({})
  property int hoveredCore: -1
  property var history: ({cpu: [], memory: [], gpu: [], up: [], down: []})
  property var networkSnapshot: null
  property real sampledAt: 0
  property real networkUp: NaN
  property real networkDown: NaN
  readonly property string networkInterface: stats.network ? stats.network.interface : "Unavailable"
  readonly property color cpuColor: Color.accent
  readonly property color memoryColor: Qt.darker(Color.accent, 1.5)
  readonly property color gpuColor: Qt.tint(Color.accent, Qt.rgba(Color.urgent.r, Color.urgent.g, Color.urgent.b, 0.6))
  readonly property real networkScale: Math.pow(2, Math.ceil(Math.log(Math.max(1024, History.peak(history.up), History.peak(history.down)) * 1.1) / Math.LN2))

  function rate(value) {
    if (!isFinite(value)) return "--"
    return value >= 1048576 ? (value / 1048576).toFixed(1) + " MiB/s"
      : (value / 1024).toFixed(1) + " KiB/s"
  }

  function recordSample(parsed) {
    var time = Number(parsed.sampleTimeMs)
    // Cached samples can serve several callers; only append each sample once.
    if (!isFinite(time) || time <= sampledAt) return
    var net = History.networkRate(networkSnapshot, parsed.network, time)
    networkSnapshot = net.snapshot
    networkUp = net.up
    networkDown = net.down
    sampledAt = time
    history = {
      cpu: History.append(history.cpu, time, cpu),
      memory: History.append(history.memory, time, memory),
      gpu: History.append(history.gpu, time, reading("gpuBusy")),
      up: History.append(net.changed ? [] : history.up, time, net.up),
      down: History.append(net.changed ? [] : history.down, time, net.down)
    }
  }

  readonly property real busyCore: 85
  readonly property real busyProcess: 85
  readonly property real cpu: root.reading("cpu")
  readonly property real memory: root.reading("memTotalKb") > 0
    ? 100 * root.reading("memUsedKb") / root.reading("memTotalKb") : NaN
  readonly property real gpu: root.reading("gpuBusy")
  readonly property real vram: root.reading("vramTotal") > 0
    ? 100 * root.reading("vramUsed") / root.reading("vramTotal") : NaN
  readonly property bool showMemory: !isNaN(memory) && (isNaN(cpu) || memory > cpu)
  readonly property bool showGpu: !isNaN(gpu)
    && (isNaN(cpu) || gpu > cpu) && (isNaN(memory) || gpu > memory)
  readonly property var cpuThermal: Thermals.summarize([
    {temperature: root.reading("cpuTemp"), critical: root.reading("cpuTempCrit")}
  ], preference("cpuTemperatureMargin"))
  readonly property var gpuThermal: Thermals.summarize(stats.gpuThermals, preference("gpuTemperatureMargin"))
  readonly property real cpuTemp: cpuThermal.temperature
  readonly property real gpuTemp: gpuThermal.temperature
  readonly property var cores: (stats && Array.isArray(stats.cores)) ? stats.cores : []
  readonly property var processes: (stats && Array.isArray(stats.processes)) ? stats.processes : []
  readonly property var memoryProcesses: (stats && Array.isArray(stats.memoryProcesses)) ? stats.memoryProcesses : []
  readonly property bool hot: cpuThermal.hot || gpuThermal.hot
  readonly property bool cpuWarning: cpu >= preference("cpuWarning")
  readonly property bool memoryWarning: memory >= preference("memoryWarning")
  readonly property bool gpuWarning: gpu >= preference("gpuWarning")
  readonly property bool vramWarning: vram >= preference("vramWarning")
  readonly property bool warning: hot || cpuWarning || memoryWarning || gpuWarning || vramWarning
  readonly property string selectedMetric: preference("barDisplay") === "adaptive"
    ? (showGpu ? "gpu" : showMemory ? "memory" : "cpu") : preference("barDisplay")
  readonly property string barText: (selectedMetric === "gpu" ? "GPU " : selectedMetric === "memory" ? "RAM " : "CPU ")
    + root.percent(selectedMetric === "gpu" ? gpu : selectedMetric === "memory" ? memory : cpu)

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function reading(key) {
    var v = stats ? stats[key] : null
    if (v === null || v === undefined) return NaN
    var n = Number(v)
    return isFinite(n) ? n : NaN
  }

  function percent(v) {
    return isNaN(v) ? "--" : Math.round(v) + "%"
  }

  function degrees(v) {
    return isNaN(v) ? "--" : Math.round(v) + "°C"
  }

  function rpm(v) {
    if (isNaN(v)) return "--"
    return v <= 0 ? "stopped" : Math.round(v) + " rpm"
  }

  function gibFromKb(kb) {
    return isNaN(kb) ? NaN : kb / 1048576
  }

  function gibFromBytes(bytes) {
    return isNaN(bytes) ? NaN : bytes / 1073741824
  }

  function pair(used, total) {
    if (isNaN(used) || isNaN(total) || total <= 0) return "--"
    return used.toFixed(1) + " / " + total.toFixed(1) + " GiB"
  }

  function temperatureDetail(thermal) {
    var label = thermal.label ? tr(thermal.label) : ""
    return isNaN(thermal.critical) ? (label ? label + " · " : "") + tr("Limit unknown") : label
  }

  function hoveredCoreLabel() {
    if (hoveredCore < 0 || hoveredCore >= cores.length) return cores.length + " threads"
    var v = cores[hoveredCore]
    var shown = (v === null || v === undefined) ? "--" : Math.round(root.coreLoad(v)) + "%"
    return "Thread " + hoveredCore + "  ·  " + shown
  }

  function coreLoad(value) {
    if (value === null || value === undefined) return 0
    var n = Number(value)
    return isFinite(n) ? Math.max(0, Math.min(100, n)) : 0
  }

  function processLabel(entry) {
    if (!entry) return "?"
    var name = String(entry.name || "?")
    var where = String(entry.where || "")
    return where === "" ? name : name + " · " + where
  }

  function processShare(value) {
    var n = Number(value)
    if (!isFinite(n)) return "--"
    return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + "%"
  }

  function memoryAmount(kb) {
    var n = Number(kb)
    if (!isFinite(n) || n < 0) return "--"
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " GiB"
    return Math.round(n / 1024) + " MiB"
  }

  function refresh() {
    if (statsProcess.running) return
    statsProcess.running = true
    uptimeFile.reload()
    loadFile.reload()
  }

  function openBtop() {
    launchError = ""
    if (!btopCheck.running) btopCheck.running = true
  }
  Process {
    id: btopCheck
    command: ["sh", "-c", "command -v btop >/dev/null 2>&1"]
    onExited: function(code) {
      if (code !== 0) { root.launchError = root.tr("Install btop to use this shortcut."); return }
      Quickshell.execDetached(["omarchy-launch-or-focus-tui", "btop"])
      root.close()
    }
  }

  function failStats(message) {
    stats = ({})
    telemetryError = tr(message)
    networkSnapshot = null
    networkUp = NaN
    networkDown = NaN
    // Missing samples break history instead of presenting the last reading as current.
    var at = sampledAt + (opened ? 2000 : 5000)
    history = {
      cpu: History.append(history.cpu, at, NaN), memory: History.append(history.memory, at, NaN),
      gpu: History.append(history.gpu, at, NaN), up: History.append(history.up, at, NaN), down: History.append(history.down, at, NaN)
    }
    console.warn(root.moduleName + ": " + message)
  }

  function parseStats(raw) {
    try {
      var parsed = JSON.parse(raw)
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object"
          || typeof parsed.sampleTimeMs !== "number" || !isFinite(parsed.sampleTimeMs)
          || !Array.isArray(parsed.cores) || !Array.isArray(parsed.processes)
          || !Array.isArray(parsed.memoryProcesses)) throw new Error("Invalid sample")
      stats = parsed
      telemetryError = ""
      recordSample(parsed)
    } catch (error) { failStats("Telemetry unavailable. Retrying…") }
  }

  onOpenedChanged: {
    if (opened) {
      refresh()
      refreshDisks()
      statsFlick.contentY = 0
      Qt.callLater(function() { if (!root.editingSettings) statsFlick.forceActiveFocus() })
    } else editingSettings = false
  }

  Process {
    id: statsProcess
    command: ["timeout", "--kill-after=1s", "5s", root.statsCommand]
    stdout: StdioCollector { id: statsOutput; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0) root.failStats("Telemetry unavailable. Retrying…")
      else root.parseStats(statsOutput.text)
    }
  }

  // Sample every 2 seconds while open and every 5 seconds in the background.
  Timer {
    interval: root.opened ? 2000 : 5000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // Reserve the widest adaptive label so samples never resize the island.
  TextMetrics {
    id: barLabelMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    // Keep the reading close to its label while reserving a stable three-digit width.
    font.wordSpacing: -Style.space(4)
    text: "RAM 100%"
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    active: root.warning
    activeColor: root.urgent
    fixedWidth: root.vertical ? -1 : Math.ceil(barLabelMetrics.width) + Style.space(8)
    tooltipText: ""

    Row {
      id: barContent
      anchors.centerIn: parent
      spacing: Style.space(4)

      Text {
        textFormat: Text.PlainText
        anchors.verticalCenter: parent.verticalCenter
        width: Math.ceil(barLabelMetrics.width)
        height: Style.bar.iconCanvas
        text: root.barText
        color: root.warning ? root.urgent : root.barForeground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.wordSpacing: barLabelMetrics.font.wordSpacing
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        renderType: Text.NativeRendering
      }
    }

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openBtop()
      else root.toggle()
    }
  }

  property bool editingSettings: false
  property string settingsError: ""
  property var pendingPreferences: ({})
  function savePreference(key, value) {
    if (!Preferences.valid(key, value)) { settingsError = tr("Invalid setting."); return }
    settingsError = ""
    pendingPreferences[key] = value
    flushPreferences()
  }
  // Serialize writes so edits to separate fields cannot overwrite each other.
  function flushPreferences() {
    if (preferencesSave.running) return
    var keys = Object.keys(pendingPreferences)
    if (!keys.length) return
    var key = keys[0], value = pendingPreferences[key]
    delete pendingPreferences[key]
    preferencesSave.command = ["omarchy-shell", "shell", "setBarWidget", root.moduleName, key, " " + JSON.stringify(value), "{}"]
    preferencesSave.running = true
  }
  function openSettings() { editingSettings = true; open(); statsFlick.contentY = 0; Qt.callLater(function() { settingsPane.focusBack() }) }
  function closeSettings() { editingSettings = false; statsFlick.contentY = 0; Qt.callLater(function() { statsFlick.forceActiveFocus() }) }
  Process {
    id: preferencesSave
    stdout: StdioCollector { id: saveOutput; waitForEnd: true }
    onExited: function(code) {
      if (code !== 0 || saveOutput.text.trim() !== "ok") root.settingsError = root.tr("Could not save settings.")
      Qt.callLater(root.flushPreferences)
    }
  }
  IpcHandler {
    target: "foamy.vitals"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function settings(): void { root.openSettings() }
    function refresh(): void { root.refresh(); root.refreshDisks() }
  }

  VitalsPopup {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    // Focus the panel on opening; show button focus only during keyboard navigation.
    focusTarget: root.editingSettings ? settingsPane.backTarget : statsFlick
    padding: 0
    borderSpec: Border.flat(Qt.alpha(root.foreground, 0.15), 1)
    contentWidth: panel.fittedContentWidth(Style.space(600))
    contentHeight: panel.fittedContentHeight(root.editingSettings ? settingsPane.implicitHeight : contentColumn.implicitHeight)
    Flickable {
      id: statsFlick
      anchors.fill: parent
      clip: true
      contentWidth: width
      contentHeight: root.editingSettings ? settingsPane.implicitHeight : contentColumn.implicitHeight
      boundsBehavior: Flickable.StopAtBounds
      flickableDirection: Flickable.VerticalFlick
      interactive: contentHeight > height
      onContentHeightChanged: contentY = Math.max(0, Math.min(contentY, contentHeight - height))
      onHeightChanged: contentY = Math.max(0, Math.min(contentY, contentHeight - height))
      Keys.onEscapePressed: root.editingSettings ? root.closeSettings() : root.close()
      Keys.onPressed: function(event) {
        if (root.editingSettings) return
        if (event.key === Qt.Key_B) { root.openBtop(); event.accepted = true }
        else if (event.key === Qt.Key_R) { root.refresh(); event.accepted = true }
        else if (event.key === Qt.Key_S) { root.openSettings(); event.accepted = true }
        else if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
          contentY = Math.max(0, Math.min(contentHeight - height, contentY + (event.key === Qt.Key_Down ? 48 : -48)))
          event.accepted = true
        }
      }
      // Bring focused fields into view on short screens without moving other monitors.
      Connections {
        target: statsFlick.Window.window
        function onActiveFocusItemChanged() {
          var item = target.activeFocusItem
          if (!item) return
          var point = item.mapToItem(statsFlick.contentItem, 0, 0)
          if (point.y < statsFlick.contentY) statsFlick.contentY = Math.max(0, point.y - Style.space(8))
          else if (point.y + item.height > statsFlick.contentY + statsFlick.height)
            statsFlick.contentY = Math.max(0, Math.min(statsFlick.contentHeight - statsFlick.height, point.y + item.height - statsFlick.height + Style.space(8)))
        }
      }
      Controls.ScrollBar.vertical: Controls.ScrollBar { policy: Controls.ScrollBar.AsNeeded }
      SettingsPane {
        id: settingsPane
        visible: root.editingSettings
        width: statsFlick.width
        settings: root.settings
        language: root.language
        saving: preferencesSave.running
        error: root.settingsError
        onSave: function(key, value) { root.savePreference(key, value) }
        onClearError: root.settingsError = ""
        onBack: root.closeSettings()
      }
      Column {
        id: contentColumn
        visible: !root.editingSettings
        width: statsFlick.width
        Item {
          width: parent.width
          height: Style.space(136)
          Rectangle {
            anchors.fill: parent
            radius: Style.space(13)
            gradient: Gradient {
              orientation: Gradient.Horizontal
              GradientStop { position: 0; color: Qt.tint(Color.popups.background, Qt.alpha(root.foreground, 0.06)) }
              GradientStop { position: 1; color: Qt.tint(Color.popups.background, Qt.alpha(root.foreground, 0.15)) }
            }
          }
          Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: Style.space(14)
            gradient: Gradient {
              orientation: Gradient.Horizontal
              GradientStop { position: 0; color: Qt.tint(Color.popups.background, Qt.alpha(root.foreground, 0.06)) }
              GradientStop { position: 1; color: Qt.tint(Color.popups.background, Qt.alpha(root.foreground, 0.15)) }
            }
          }
          Column {
            anchors.fill: parent
            anchors.margins: Style.space(20)
            spacing: Style.space(14)
            RowLayout {
              width: parent.width
              spacing: Style.space(8)
              VitalsIcon { name: "activity"; Layout.preferredWidth: Style.space(15); Layout.preferredHeight: Style.space(15); color: root.dim }
              Label { text: "Vitals"; font.pixelSize: Style.space(13); Layout.fillWidth: true }
              VitalsAction {
                id: settingsButton
                Layout.preferredWidth: Style.space(32); Layout.preferredHeight: Style.space(32)
                radius: Style.space(7); iconSize: Style.space(16)
                iconName: "settings"; foreground: root.dim; tooltipText: root.tr("Settings")
                onClicked: root.openSettings()
              }
            }
            Column {
              width: parent.width
              spacing: Style.space(4)
              RowLayout {
                width: parent.width
                spacing: Style.space(10)
                VitalsIcon { name: root.deviceIcon; Layout.preferredWidth: Style.space(24); Layout.preferredHeight: Style.space(24); color: root.foreground }
                Label { Layout.fillWidth: true; text: root.hostname; font.pixelSize: Style.space(24) }
              }
              Caption { width: parent.width; text: root.tr("Up") + " " + root.uptime + " · " + root.tr("Load") + " " + root.loadAverage }
            }
          }
        }
        Column {
          id: body
          width: parent.width
          padding: Style.space(20)
          spacing: Style.space(20)
          Grid {
            width: body.width - body.padding * 2
            columns: width >= Style.space(440) ? 3 : 2
            columnSpacing: Style.space(22)
            rowSpacing: Style.space(20)
            readonly property real cellWidth: (width - columnSpacing * (columns - 1)) / columns
            Overview { glyph: "\uf2db"; width: parent.cellWidth; label: "CPU"; value: root.percent(root.cpu); detail: root.cores.length ? root.cores.length + " " + root.tr("threads") : "--"; meterValue: root.cpu; warning: root.cpuWarning }
            Overview { glyph: "󰍛"; width: parent.cellWidth; label: "RAM"; value: root.percent(root.memory); detail: root.pair(root.gibFromKb(root.reading("memUsedKb")), root.gibFromKb(root.reading("memTotalKb"))); meterValue: root.memory; warning: root.memoryWarning }
            Overview { glyph: "\uf2c9"; width: parent.cellWidth; label: root.tr("CPU TEMP"); value: root.degrees(root.cpuTemp); detail: root.temperatureDetail(root.cpuThermal); meterValue: root.cpuThermal.meter; warning: root.cpuThermal.hot }
            Overview { glyph: "\uf108"; width: parent.cellWidth; label: "GPU"; value: root.percent(root.gpu); meterValue: root.gpu; warning: root.gpuWarning }
            Overview { glyph: "󰍛"; width: parent.cellWidth; label: "VRAM"; value: root.percent(root.vram); detail: root.pair(root.gibFromBytes(root.reading("vramUsed")), root.gibFromBytes(root.reading("vramTotal"))); meterValue: root.vram; warning: root.vramWarning }
            Overview { glyph: "\uf2c9"; width: parent.cellWidth; label: root.tr("GPU TEMP"); value: root.degrees(root.gpuTemp); detail: root.temperatureDetail(root.gpuThermal); meterValue: root.gpuThermal.meter; warning: root.gpuThermal.hot }
          }
          Grid {
            id: detailsGrid
            width: body.width - body.padding * 2
            columns: width >= Style.space(500) ? 2 : 1
            columnSpacing: Style.space(28)
            rowSpacing: Style.space(20)
            readonly property real columnWidth: (width - columnSpacing * (columns - 1)) / columns
            Column {
              width: detailsGrid.columnWidth
              spacing: Style.space(12)
              SectionHeading { title: root.tr("Resource usage"); detail: "2 min" }
              Row {
                spacing: Style.space(14)
                Legend { text: "CPU"; dotColor: root.cpuColor }
                Legend { text: "RAM"; dotColor: root.memoryColor; dashed: true }
                Legend { text: "GPU"; dotColor: root.gpuColor }
              }
              HistoryPlot {
                width: parent.width; height: Style.space(90)
                topLabel: "100%"; middleLabel: "50%"; bottomLabel: "0%"
                maximum: 100
                series: [{points: root.history.cpu, color: root.cpuColor}, {points: root.history.memory, color: root.memoryColor, dashed: true}, {points: root.history.gpu, color: root.gpuColor}]
              }
            }
            ProcessSection { width: detailsGrid.columnWidth; title: root.tr("Top CPU processes"); entries: root.processes }
            Column {
              width: detailsGrid.columnWidth
              spacing: Style.space(12)
              SectionHeading { title: root.tr("Network traffic"); detail: root.networkInterface }
              RowLayout {
                width: parent.width
                spacing: Style.space(16)
                Column {
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  spacing: Style.space(5)
                  Legend { text: root.tr("Incoming"); dotColor: root.gpuColor }
                  Label { width: parent.width; text: root.rate(root.networkDown); font.pixelSize: Style.space(16) }
                }
                Column {
                  Layout.fillWidth: true
                  Layout.preferredWidth: 1
                  spacing: Style.space(5)
                  Legend { text: root.tr("Outgoing"); dotColor: root.cpuColor }
                  Label { width: parent.width; text: root.rate(root.networkUp); font.pixelSize: Style.space(16) }
                }
              }
              HistoryPlot {
                width: parent.width; height: Style.space(120)
                topLabel: root.rate(root.networkScale).replace(".0 ", " ")
                middleLabel: "0"
                bottomLabel: root.rate(root.networkScale).replace(".0 ", " ")
                maximum: root.networkScale; centered: true
                series: [{points: root.history.down, color: root.gpuColor}, {points: root.history.up, color: root.cpuColor, negative: true}]
              }
            }
            ProcessSection { width: detailsGrid.columnWidth; title: root.tr("Top memory processes"); entries: root.memoryProcesses; memoryMode: true }
            Column {
              width: detailsGrid.columnWidth
              spacing: Style.space(12)
              SectionHeading { title: root.tr("CPU cores"); detail: root.hoveredCore >= 0 ? root.hoveredCoreLabel() : "" }
              Item {
                width: parent.width; height: Style.space(18)
                Row {
                  id: coreRow
                  anchors.fill: parent
                  spacing: Style.space(3)
                  Repeater {
                    model: root.cores
                    Rectangle {
                      required property var modelData
                      required property int index
                      width: root.cores.length ? Math.max(0, (coreRow.width - coreRow.spacing * (root.cores.length - 1)) / root.cores.length) : 0
                      height: coreRow.height
                      radius: Style.space(2)
                      color: root.coreLoad(modelData) >= root.preference("cpuWarning") ? root.urgent : root.cpuColor
                      opacity: modelData === null ? 0.08 : root.hoveredCore === index ? 1 : 0.15 + 0.75 * root.coreLoad(modelData) / 100
                    }
                  }
                }
                MouseArea {
                  anchors.fill: parent; hoverEnabled: true
                  onPositionChanged: function(mouse) { root.hoveredCore = Math.min(root.cores.length - 1, Math.floor(mouse.x / Math.max(1, width) * root.cores.length)) }
                  onExited: root.hoveredCore = -1
                }
              }
            }
            Column {
              width: detailsGrid.columnWidth; spacing: Style.space(12)
              SectionHeading { title: root.tr("Storage") }
              Caption { width: parent.width; visible: root.diskStatus !== ""; text: root.tr(root.diskStatus) }
              Repeater {
                model: root.disks
                Column {
                  required property var modelData
                  width: detailsGrid.columnWidth; spacing: Style.space(8)
                  DataRow { label: parent.modelData.mount === "/" ? root.tr("Root filesystem") : parent.modelData.mount; value: root.diskCapacity(parent.modelData.usedKb, parent.modelData.totalKb) }
                  Meter { width: parent.width; value: 100 * parent.modelData.usedKb / parent.modelData.totalKb }
                }
              }
            }
          }
          Label { width: body.width - body.padding * 2; visible: root.telemetryError !== "" || root.launchError !== ""; text: root.telemetryError || root.launchError; color: root.urgent; wrapMode: Text.WordWrap; elide: Text.ElideNone; Accessible.role: Accessible.AlertMessage }
          Separator { width: body.width - body.padding * 2 }
          RowLayout {
            width: body.width - body.padding * 2
            VitalsAction { iconName: "terminal"; label: "btop"; tooltipText: "btop"; foreground: root.dim; onClicked: root.openBtop() }
            Item { Layout.fillWidth: true }
          }
        }
      }
    }
  }

  component Label: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.panelFont
    font.pixelSize: Style.space(13)
    elide: Text.ElideRight
  }
  component Caption: Label { color: root.dim; font.pixelSize: Style.space(11) }
  component Separator: Rectangle { height: 1; color: root.outline }
  component Overview: Column {
    id: overview
    property string label: ""
    property string glyph: ""
    property string value: "--"
    property string detail: ""
    property real meterValue: NaN
    property bool warning: false
    spacing: Style.space(6)
    RowLayout {
      width: parent.width
      Caption { text: overview.label; color: overview.warning ? root.urgent : root.dim; Layout.fillWidth: true }
      OpticalGlyph {
        Layout.preferredWidth: Style.space(14); Layout.preferredHeight: Style.space(14)
        text: overview.glyph; fontFamily: root.fontFamily; fontSize: Style.font.bodySmall
        color: overview.warning ? root.urgent : root.dim
      }
    }
    Item {
      width: parent.width
      implicitHeight: overviewValue.implicitHeight
      Label { id: overviewValue; text: overview.value; font.pixelSize: Style.space(24); color: overview.warning ? root.urgent : root.foreground }
      // Keep secondary detail beside the reading, as in the original Vitals cards.
      Caption {
        anchors.left: overviewValue.right; anchors.leftMargin: Style.space(8)
        anchors.right: parent.right; anchors.baseline: overviewValue.baseline
        text: overview.detail; horizontalAlignment: Text.AlignRight
        color: overview.warning ? root.urgent : root.dim
      }
    }
    Meter { width: parent.width; value: overview.meterValue; opacity: isFinite(overview.meterValue) ? 1 : 0; fillColor: overview.warning ? root.urgent : root.foreground }
  }
  component HistoryPlot: Item {
    id: historyPlot
    property alias series: plot.series
    property alias maximum: plot.maximum
    property alias centered: plot.centered
    property string topLabel: ""
    property string middleLabel: ""
    property string bottomLabel: ""
    readonly property real axisWidth: Math.ceil(Math.max(topTick.implicitWidth, middleTick.implicitWidth, bottomTick.implicitWidth)) + Style.space(8)
    Item {
      id: plotArea
      anchors.fill: parent
      Caption { id: topTick; width: historyPlot.axisWidth - Style.space(8); anchors.top: parent.top; text: historyPlot.topLabel; font.pixelSize: Style.space(10); horizontalAlignment: Text.AlignRight }
      Caption { id: middleTick; width: topTick.width; anchors.verticalCenter: parent.verticalCenter; text: historyPlot.middleLabel; font.pixelSize: Style.space(10); horizontalAlignment: Text.AlignRight }
      Caption { id: bottomTick; width: topTick.width; anchors.bottom: parent.bottom; text: historyPlot.bottomLabel; font.pixelSize: Style.space(10); horizontalAlignment: Text.AlignRight }
      HistoryChart {
        id: plot
        anchors.fill: parent; anchors.leftMargin: historyPlot.axisWidth
        endTime: root.sampledAt
        gridColor: Qt.alpha(root.foreground, 0.14); zeroColor: Qt.alpha(root.foreground, 0.40)
      }
    }
  }
  component Meter: Rectangle {
    property real value: NaN
    property color fillColor: root.foreground
    height: Style.space(4)
    radius: height / 2
    color: Qt.alpha(root.foreground, 0.12)
    Rectangle { width: parent.width * (isFinite(parent.value) ? Math.max(0, Math.min(100, parent.value)) / 100 : 0); height: parent.height; radius: parent.radius; color: parent.fillColor }
  }
  component Legend: Row {
    property string text: ""
    id: legend
    property color dotColor: root.cpuColor
    property bool dashed: false
    spacing: Style.space(6)
    Row {
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(2)
      Repeater {
        model: legend.dashed ? 3 : 1
        Rectangle { width: Style.space(legend.dashed ? 4 : 16); height: Style.space(2); radius: height / 2; color: legend.dotColor }
      }
    }
    Caption { text: parent.text }
  }
  component SectionHeading: Item {
    property string title: ""
    property string detail: ""
    width: parent.width
    implicitHeight: Math.max(sectionTitle.implicitHeight, sectionDetail.implicitHeight)
    Label { id: sectionTitle; anchors.left: parent.left; anchors.right: sectionDetail.left; anchors.rightMargin: Style.space(8); text: parent.title; font.weight: Font.DemiBold; font.pixelSize: Style.space(14) }
    Caption { id: sectionDetail; anchors.right: parent.right; width: Math.min(implicitWidth, parent.width * 0.65); text: parent.detail; horizontalAlignment: Text.AlignRight }
  }
  component DataRow: Item {
    property string label: ""
    property string value: ""
    property bool subdued: false
    width: parent.width
    implicitHeight: Math.max(rowLabel.implicitHeight, rowValue.implicitHeight)
    Caption { id: rowLabel; anchors.left: parent.left; anchors.right: rowValue.left; anchors.rightMargin: Style.space(8); text: parent.label }
    Label { id: rowValue; anchors.right: parent.right; width: Math.min(implicitWidth, parent.width * 0.65); text: parent.value; color: parent.subdued ? root.dim : root.foreground; font.pixelSize: Style.space(11); horizontalAlignment: Text.AlignRight }
  }
  component ProcessSection: Column {
    id: processSection
    property string title: ""
    property var entries: []
    property bool memoryMode: false
    spacing: Style.space(12)
    SectionHeading { title: processSection.title }
    DataRow { label: root.tr("Process"); value: processSection.memoryMode ? root.tr("Memory") : "CPU"; subdued: true }
    Caption { visible: processSection.entries.length === 0; text: "--" }
    Repeater {
      model: processSection.entries
      Item {
        required property var modelData
        width: processSection.width
        implicitHeight: Math.max(processName.implicitHeight, processValue.implicitHeight)
        Label { id: processName; anchors.left: parent.left; anchors.right: processValue.left; anchors.rightMargin: Style.space(12); text: root.processLabel(parent.modelData) }
        Label { id: processValue; anchors.right: parent.right; text: processSection.memoryMode ? root.memoryAmount(parent.modelData.rssKb) : root.processShare(parent.modelData.pct) }
        HoverHandler { id: processHover }
        PanelToolTip { visible: processHover.hovered; text: root.processLabel(parent.modelData) + (processSection.memoryMode ? "" : " · 100% = 1 thread"); fontFamily: root.panelFont }
      }
    }
  }
}
