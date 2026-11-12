import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "kit"
import "kit/FoamyPrefs.js" as Prefs
import "Preferences.js" as Schema
import "History.js" as History
import "Gpus.js" as Gpus

// The Vitals popup body: hero band, readings dashboard and the settings page.
// Data comes from VitalsService (or the preview's mock values); the Panel
// owns sampling, saving and closing.
//
// Keyboard (one cursor, shared with the pointer): Up/Down/j/k walk Settings,
// the GPU rows (Enter pins), each process row, the CPU cores strip and btop,
// scrolling them into view;
// Left/Right/h/l pick a thread on the cores strip; Enter opens Settings or
// btop; s Settings, r refresh, b btop, Esc closes, Tab switches bar panels.
//
// `live` is false while the popup is hidden: every list and chart binding
// then reads a constant, so samples taken for the bar never rebuild them.
PanelKeyCatcher {
  id: view

  property var settings: ({})
  property bool live: true
  property bool editingSettings: false
  property bool saving: false
  property string settingsError: ""
  property bool retryable: false
  property string launchError: ""

  signal closeRequestedByView
  signal settingsRequested
  signal settingsClosed
  signal refreshRequested
  signal btopRequested
  signal saveRequested(string key, var value)
  signal retryRequested
  signal switchPanelRequested(int direction)

  readonly property alias cursor: cursor
  readonly property alias flick: flick
  readonly property alias settingsPage: settingsPage
  readonly property alias band: band

  implicitWidth: FoamyStyle.popupWidthWide
  implicitHeight: editingSettings ? settingsPage.implicitHeight : page.implicitHeight
  blocked: editingSettings

  VitalsReadings {
    id: r
    settings: view.settings
    stats: view.live ? VitalsService.stats : ({})
  }

  readonly property var emptyHistory: ({
      cpu: [],
      memory: [],
      gpus: {},
      up: [],
      down: []
    })
  readonly property var history: live ? VitalsService.history : emptyHistory
  readonly property var processes: live && Array.isArray(VitalsService.detail.processes) ? VitalsService.detail.processes : []
  readonly property var memoryProcesses: live && Array.isArray(VitalsService.detail.memoryProcesses) ? VitalsService.detail.memoryProcesses : []
  readonly property var cores: live ? r.cores : []
  readonly property var disks: live ? VitalsService.disks : []
  property int hoveredCore: -1

  readonly property color foreground: FoamyPalette.text
  readonly property color dim: FoamyPalette.secondaryText
  readonly property color cpuColor: Commons.Color.accent
  readonly property color memoryColor: Qt.darker(Commons.Color.accent, 1.5)
  readonly property color gpuColor: Qt.tint(Commons.Color.accent, Qt.alpha(Commons.Color.urgent, 0.6))
  // Later slots tint the accent; each stays ΔE ≥ 24 from CPU, RAM, GPU and each other in every bundled theme.
  readonly property var gpuTints: [Qt.rgba(0.95, 0.95, 0.24, 0.8), Qt.rgba(0.24, 0.95, 0.95, 0.8), Qt.rgba(0.95, 0.24, 0.77, 0.8)]
  function gpuSlotColor(slot) {
    return slot <= 0 ? gpuColor : Qt.tint(Commons.Color.accent, gpuTints[(slot - 1) % gpuTints.length])
  }
  // History lines in color-slot order: current rows first, then vanished GPUs.
  readonly property var gpuChartIds: Object.keys(history.gpus).sort(function (a, b) {
    return Gpus.colorSlot(r.gpus, history.gpus, a) - Gpus.colorSlot(r.gpus, history.gpus, b)
  })
  // Legend text: a current row's short name, else a vanished GPU's address.
  function gpuLegend(id) {
    var row = r.gpuRows.filter(function (gpu) {
      return gpu.id === id
    })[0]
    return row ? Gpus.displayName(row, r.gpuRows, true) : Gpus.shortName({
      id: id,
      vendor: ""
    })
  }
  readonly property real networkScale: Math.pow(2, Math.ceil(Math.log(Math.max(1024, History.peak(history.up), History.peak(history.down)) * 1.1) / Math.LN2))
  readonly property real cellWidth: (content.width - FoamyStyle.padding * 2 - overview.columnSpacing * (overview.columns - 1)) / overview.columns

  readonly property string deviceIcon: ({
      desktop: "monitor",
      laptop: "laptop",
      convertible: "laptop",
      server: "server",
      tablet: "tablet",
      handset: "smartphone",
      watch: "watch",
      embedded: "cpu",
      vm: "box",
      container: "box"
    })[VitalsService.chassis] || "monitor"

  // --- cursor stops: Settings, GPU rows, CPU process rows, memory rows, cores, btop
  readonly property int graphicsStart: 1
  readonly property int graphicsStops: live && r.multiGpu ? r.gpuRows.length : 0
  readonly property int cpuStart: graphicsStart + graphicsStops
  readonly property int memoryStart: cpuStart + processes.length
  readonly property int coresStop: cores.length ? memoryStart + memoryProcesses.length : -1
  readonly property int btopStop: memoryStart + memoryProcesses.length + (cores.length ? 1 : 0)

  FoamyCursor {
    id: cursor
    count: view.btopStop + 1
    onIndexChanged: if (index !== view.coresStop && !coresMouse.containsMouse)
      view.hoveredCore = -1
  }

  onLiveChanged: if (!live) {
    cursor.reset()
    hoveredCore = -1
    flick.contentY = 0
  }

  // When GPU rows vanish under the cursor, the numeric index would land on a
  // process row (Enter opens btop): move to the first GPU row, or reset. A
  // cursor past the GPU rows would drift onto another row, so it resets (the
  // index is not shifted: FoamyCursor clamps on count changes on its own, in
  // no fixed order with this handler). GPU-count changes are rare.
  property int previousGraphicsStops: 0
  Component.onCompleted: previousGraphicsStops = graphicsStops
  onGraphicsStopsChanged: {
    var was = previousGraphicsStops
    previousGraphicsStops = graphicsStops
    if (cursor.index >= graphicsStart + was)
      cursor.reset()
    else if (cursor.index >= graphicsStart && cursor.index >= graphicsStart + graphicsStops) {
      if (graphicsStops > 0)
        cursor.index = graphicsStart
      else
        cursor.reset()
    }
  }
  onEditingSettingsChanged: {
    cursor.reset()
    if (editingSettings) {
      settingsPage.resetScroll()
      Qt.callLater(settingsPage.focusBack)
    } else {
      Qt.callLater(view.forceActiveFocus)
    }
  }

  onMoveRequested: function (dx, dy) {
    if (dy === 0 && cursor.index === coresStop && cores.length) {
      hoveredCore = Math.max(0, Math.min(cores.length - 1, (hoveredCore < 0 ? 0 : hoveredCore + dx)))
      return
    }
    cursor.move(dy !== 0 ? dy : dx)
    if (cursor.index === coresStop && hoveredCore < 0)
      hoveredCore = 0
    revealCursor()
  }
  onActivateRequested: activate(cursor.index)
  onCloseRequested: closeRequestedByView()
  onTabRequested: function (direction) {
    switchPanelRequested(direction)
  }
  onTextKey: function (text, modifiers) {
    // Ctrl/Alt/Super shortcuts are not ours.
    if (!cursor.plain(modifiers))
      return
    if (text === "s")
      settingsRequested()
    else if (text === "r")
      refreshRequested()
    else if (text === "b")
      btopRequested()
  }

  function activate(index) {
    if (index === 0)
      settingsRequested()
    else if (index >= graphicsStart && index < cpuStart)
      togglePin(r.gpuRows[index - graphicsStart].id)
    else if (index > 0 && index !== coresStop && index <= btopStop)
      btopRequested()
  }

  // A row click or Enter pins that GPU to the bar, or unpins it.
  function togglePin(id) {
    saveRequested("pinnedGpu", r.pinnedGpu === id ? "" : id)
  }

  function stopItem(index) {
    if (index <= 0)
      return null
    if (index < cpuStart)
      return gpuRows.itemAt(index - graphicsStart)
    if (index < memoryStart)
      return cpuRows.itemAt(index - cpuStart)
    if (index < memoryStart + memoryProcesses.length)
      return memoryRows.itemAt(index - memoryStart)
    if (index === coresStop)
      return coresStrip
    return btopButton
  }

  function revealCursor() {
    var item = stopItem(cursor.index)
    if (!item) {
      flick.contentY = 0
      return
    }
    var point = item.mapToItem(flick.contentItem, 0, 0)
    var margin = FoamyStyle.gap
    if (point.y < flick.contentY)
      flick.contentY = Math.max(0, point.y - margin)
    else if (point.y + item.height > flick.contentY + flick.height)
      flick.contentY = Math.max(0, Math.min(flick.contentHeight - flick.height, point.y + item.height - flick.height + margin))
  }

  // --- formatting ------------------------------------------------------------------
  function percent(v) {
    return isNaN(v) ? "--" : FoamyI18n.number(Math.round(v)) + "%"
  }

  function degrees(v) {
    return isNaN(v) ? "--" : FoamyI18n.number(Math.round(v)) + "°C"
  }

  function pair(used, total) {
    if (isNaN(used) || isNaN(total) || total <= 0)
      return "--"
    return FoamyI18n.number(used, 1) + " / " + FoamyI18n.number(total, 1) + " GiB"
  }

  function rate(value) {
    if (!isFinite(value))
      return "--"
    return value >= 1048576 ? FoamyI18n.number(value / 1048576, 1) + " MiB/s" : FoamyI18n.number(value / 1024, 1) + " KiB/s"
  }

  function diskCapacity(usedKb, totalKb) {
    var divisor = totalKb >= 1073741824 ? 1073741824 : 1048576
    return FoamyI18n.number(usedKb / divisor, 1) + " / " + FoamyI18n.number(totalKb / divisor, 1) + (divisor === 1073741824 ? " TiB" : " GiB")
  }

  function processShare(value) {
    var n = Number(value)
    if (!isFinite(n))
      return "--"
    return FoamyI18n.number(n, n >= 100 ? 0 : 1) + "%"
  }

  function memoryAmount(kb) {
    var n = Number(kb)
    if (!isFinite(n) || n < 0)
      return "--"
    if (n >= 1048576)
      return FoamyI18n.number(n / 1048576, 1) + " GiB"
    return FoamyI18n.number(Math.round(n / 1024)) + " MiB"
  }

  function uptimeText(minutes) {
    if (minutes < 0)
      return "--"
    return FoamyI18n.t("%1h %2m", [Math.floor(minutes / 60), minutes % 60])
  }

  function loadText(values) {
    return values.length ? values.map(function (value) {
      return FoamyI18n.number(value, 2)
    }).join(" / ") : "--"
  }

  function temperatureDetail(thermal) {
    if (isNaN(thermal.temperature))
      return ""
    var label = thermal.label ? FoamyI18n.t(thermal.label) : ""
    return isNaN(thermal.critical) ? (label ? label + " · " : "") + FoamyI18n.t("Limit unknown") : label
  }

  function coreLoad(value) {
    if (value === null || value === undefined)
      return 0
    var n = Number(value)
    return isFinite(n) ? Math.max(0, Math.min(100, n)) : 0
  }

  function coresDetail() {
    if (hoveredCore < 0 || hoveredCore >= cores.length)
      return ""
    var v = cores[hoveredCore]
    return FoamyI18n.t("Thread %1 · %2", [hoveredCore, v === null || v === undefined ? "--" : percent(coreLoad(v))])
  }

  function processLabel(entry) {
    if (!entry)
      return "?"
    var name = String(entry.name || "?")
    var where = String(entry.where || "")
    return where === "" ? name : name + " · " + where
  }

  Flickable {
    id: flick
    visible: !view.editingSettings
    anchors.fill: parent
    clip: true
    contentWidth: width
    contentHeight: page.implicitHeight
    boundsBehavior: Flickable.StopAtBounds
    flickableDirection: Flickable.VerticalFlick
    interactive: contentHeight > height
    onContentHeightChanged: contentY = Math.max(0, Math.min(contentY, contentHeight - height))
    onHeightChanged: contentY = Math.max(0, Math.min(contentY, contentHeight - height))
    Controls.ScrollBar.vertical: Controls.ScrollBar {
      policy: Controls.ScrollBar.AsNeeded
    }

    // A wheel notch scrolls about three rows, however the mouse splits it.
    FoamyWheelScroll {
      id: wheel
      target: flick
    }

    Column {
      id: page
      width: flick.width

      // The foamy band: title row, then the host line (the dev hero, compact).
      FoamyHeroBand {
        id: band
        width: parent.width
        iconName: "activity"
        title: FoamyI18n.t("Vitals")
        settingsHasCursor: cursor.at(0)
        onSettingsClicked: view.settingsRequested()
        onSettingsHovered: function (hovered) {
          if (hovered)
            cursor.hover(0)
        }

        RowLayout {
          Layout.fillWidth: true
          spacing: FoamyStyle.gapLarge
          FoamyIcon {
            name: view.deviceIcon
            color: view.foreground
            Layout.preferredWidth: FoamyStyle.font.display
            Layout.preferredHeight: FoamyStyle.font.display
            Layout.alignment: Qt.AlignVCenter
          }
          Column {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: FoamyStyle.gapTiny
            Label {
              width: parent.width
              text: VitalsService.hostname
              font.pixelSize: FoamyStyle.font.display
            }
            Caption {
              width: parent.width
              text: FoamyI18n.t("Up %1 · Load %2", [view.uptimeText(VitalsService.uptimeMinutes), view.loadText(VitalsService.loadAverages)])
            }
          }
        }
      }

      Column {
        id: content
        width: flick.width
        topPadding: FoamyStyle.sectionGap
        leftPadding: FoamyStyle.padding
        rightPadding: FoamyStyle.padding
        bottomPadding: FoamyStyle.padding
        spacing: FoamyStyle.padding
        readonly property real inner: width - FoamyStyle.padding * 2

        // Errors first, where they are seen without scrolling.
        Label {
          width: content.inner
          visible: text !== ""
          text: FoamyI18n.t(VitalsService.telemetryError || view.launchError)
          color: Commons.Color.urgent
          font.pixelSize: FoamyStyle.font.bodySmall
          wrapMode: Text.WordWrap
          elide: Text.ElideNone
          Accessible.role: Accessible.AlertMessage
          Accessible.name: text
        }
        Caption {
          width: content.inner
          visible: r.gpuError !== ""
          text: FoamyI18n.t(r.gpuError === "Intel GPU readings unavailable. Retrying…" ? r.gpuError : "NVIDIA readings unavailable. Check nvidia-smi.")
          color: r.urgent
          wrapMode: Text.WordWrap
        }
        Caption {
          width: content.inner
          visible: r.anyIntel
          text: FoamyI18n.t("Intel GPU: busiest engine and private allocations from accessible processes for this user.")
          wrapMode: Text.WordWrap
        }

        Grid {
          id: overview
          width: content.inner
          columns: width >= Style.space(440) ? 3 : 2
          columnSpacing: Style.space(22)
          rowSpacing: FoamyStyle.padding
          Overview {
            glyph: "\u{f2db}"
            label: "CPU"
            value: view.percent(r.cpu)
            meterValue: r.cpu
            warning: r.cpuWarning
            detail: view.cores.length ? FoamyI18n.tn("%1 threads", view.cores.length) : "--"
          }
          Overview {
            glyph: "\u{f035b}"
            label: "RAM"
            value: view.percent(r.memory)
            meterValue: r.memory
            warning: r.memoryWarning
            detail: view.pair(r.reading("memUsedKb") / 1048576, r.reading("memTotalKb") / 1048576)
          }
          Overview {
            glyph: "\u{f2c9}"
            label: FoamyI18n.t("CPU temperature")
            value: view.degrees(r.cpuThermal.temperature)
            detail: view.temperatureDetail(r.cpuThermal)
            meterValue: r.cpuThermal.meter
            warning: r.cpuThermal.hot
          }
          // With two or more GPUs the Graphics table below replaces these tiles.
          Overview {
            visible: !r.multiGpu
            glyph: "\u{f108}"
            label: "GPU"
            value: view.percent(r.gpu)
            meterValue: r.gpu
            warning: r.gpuWarning
            detail: r.gpuSuspended ? FoamyI18n.t("Sleeping") : r.intelGpu && !isNaN(r.gpuReading("frequencyMHz")) ? FoamyI18n.number(Math.round(r.gpuReading("frequencyMHz"))) + " MHz" : ""
          }
          Overview {
            // An APU's memory is its VRAM carve-out plus GTT (system RAM), not VRAM.
            visible: !r.multiGpu
            glyph: "\u{f035b}"
            label: r.intelGpu || (r.barGpu && r.barGpu.vendor === "amd" && r.barGpu.type === "integrated") ? FoamyI18n.t("GPU memory") : "VRAM"
            value: r.intelGpu ? view.memoryAmount(r.gpuReading("memoryPrivate") / 1024) : view.percent(r.vram)
            detail: r.intelGpu ? FoamyI18n.t("Private") : view.pair(r.gpuReading("vramUsed") / 1073741824, r.gpuReading("vramTotal") / 1073741824)
            meterValue: r.vram
            warning: r.vramWarning
          }
          Overview {
            visible: !r.multiGpu
            glyph: "\u{f2c9}"
            label: FoamyI18n.t("GPU temperature")
            value: view.degrees(r.gpuThermal.temperature)
            detail: r.gpuSuspended ? FoamyI18n.t("Sleeping") : r.intelGpu && isNaN(r.gpuThermal.temperature) ? FoamyI18n.t("Not exposed") : view.temperatureDetail(r.gpuThermal)
            meterValue: r.gpuThermal.meter
            warning: r.gpuThermal.hot
          }
        }

        GraphicsSection {
          id: graphics
          Repeater {
            id: gpuRows
            model: r.multiGpu ? r.gpuRows : []
            GpuRow {
              section: graphics
            }
          }
        }
        Caption {
          width: content.inner
          visible: r.pinnedMissing
          text: FoamyI18n.t("Pinned GPU not connected")
          wrapMode: Text.WordWrap
        }

        Grid {
          id: details
          width: content.inner
          columns: width >= Style.space(500) ? 2 : 1
          columnSpacing: Style.space(28)
          rowSpacing: FoamyStyle.padding
          readonly property real columnWidth: (width - columnSpacing * (columns - 1)) / columns

          Column {
            width: details.columnWidth
            spacing: FoamyStyle.gapLarge
            SectionHeading {
              title: FoamyI18n.t("Resource usage")
              detail: FoamyI18n.t("Last 2 minutes")
            }
            Row {
              visible: !r.multiGpu
              spacing: FoamyStyle.sectionGap
              Legend {
                text: "CPU"
                dotColor: view.cpuColor
              }
              Legend {
                text: "RAM"
                dotColor: view.memoryColor
                dashed: true
              }
              Legend {
                text: "GPU"
                dotColor: view.gpuColor
              }
            }
            // One entry per GPU line; entries wrap and long names elide.
            Flow {
              visible: r.multiGpu
              width: parent.width
              spacing: FoamyStyle.sectionGap
              Legend {
                text: "CPU"
                dotColor: view.cpuColor
              }
              Legend {
                text: "RAM"
                dotColor: view.memoryColor
                dashed: true
              }
              Repeater {
                model: r.multiGpu ? view.gpuChartIds : []
                Legend {
                  required property string modelData
                  text: view.gpuLegend(modelData)
                  textWidth: Style.space(90)
                  dotColor: view.gpuSlotColor(Gpus.colorSlot(r.gpus, view.history.gpus, modelData))
                }
              }
            }
            HistoryPlot {
              width: parent.width
              height: Style.space(90)
              topLabel: view.percent(100)
              middleLabel: view.percent(50)
              bottomLabel: view.percent(0)
              maximum: 100
              series: !view.live ? [] : [
                {
                  points: view.history.cpu,
                  color: view.cpuColor
                },
                {
                  points: view.history.memory,
                  color: view.memoryColor,
                  dashed: true
                }
              ].concat(r.multiGpu ? view.gpuChartIds.map(function (id) {
                return {
                  points: view.history.gpus[id],
                  color: view.gpuSlotColor(Gpus.colorSlot(r.gpus, view.history.gpus, id))
                }
              }) : [
                {
                  points: view.history.gpus[r.gpus.length ? r.gpus[0].id : ""] || [],
                  color: view.gpuColor
                }
              ])
            }
          }

          ProcessSection {
            id: cpuSection
            width: details.columnWidth
            title: FoamyI18n.t("Top CPU processes")
            entries: view.processes
            Repeater {
              id: cpuRows
              model: view.processes
              ProcessRow {
                section: cpuSection
                first: view.cpuStart
              }
            }
          }

          Column {
            width: details.columnWidth
            spacing: FoamyStyle.gapLarge
            SectionHeading {
              title: FoamyI18n.t("Network traffic")
              detail: r.stats.network ? r.stats.network.interface : FoamyI18n.t("Unavailable")
            }
            RowLayout {
              width: parent.width
              spacing: Style.space(16)
              Column {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                spacing: Style.space(5)
                Legend {
                  text: FoamyI18n.t("Incoming")
                  dotColor: view.gpuColor
                }
                Label {
                  width: parent.width
                  text: view.rate(view.live ? VitalsService.networkDown : NaN)
                  font.pixelSize: FoamyStyle.font.heading
                }
              }
              Column {
                Layout.fillWidth: true
                Layout.preferredWidth: 1
                spacing: Style.space(5)
                Legend {
                  text: FoamyI18n.t("Outgoing")
                  dotColor: view.cpuColor
                }
                Label {
                  width: parent.width
                  text: view.rate(view.live ? VitalsService.networkUp : NaN)
                  font.pixelSize: FoamyStyle.font.heading
                }
              }
            }
            HistoryPlot {
              width: parent.width
              height: Style.space(120)
              topLabel: view.rate(view.networkScale)
              middleLabel: FoamyI18n.number(0)
              bottomLabel: view.rate(view.networkScale)
              maximum: view.networkScale
              centered: true
              series: view.live ? [
                {
                  points: view.history.down,
                  color: view.gpuColor
                },
                {
                  points: view.history.up,
                  color: view.cpuColor,
                  negative: true
                }
              ] : []
            }
          }

          ProcessSection {
            id: memorySection
            width: details.columnWidth
            title: FoamyI18n.t("Top memory processes")
            entries: view.memoryProcesses
            memoryMode: true
            Repeater {
              id: memoryRows
              model: view.memoryProcesses
              ProcessRow {
                section: memorySection
                first: view.memoryStart
              }
            }
          }

          Column {
            width: details.columnWidth
            spacing: FoamyStyle.gapLarge
            SectionHeading {
              title: FoamyI18n.t("CPU cores")
              detail: view.coresDetail()
            }
            FoamyCursorSurface {
              id: coresStrip
              width: parent.width
              height: Style.space(18) + FoamyStyle.gap
              hasCursor: cursor.at(view.coresStop) && view.coresStop >= 0
              Row {
                id: coreRow
                anchors.fill: parent
                anchors.margins: FoamyStyle.gapSmall
                spacing: Style.space(3)
                Repeater {
                  model: view.cores
                  Rectangle {
                    required property var modelData
                    required property int index
                    width: view.cores.length ? Math.max(0, (coreRow.width - coreRow.spacing * (view.cores.length - 1)) / view.cores.length) : 0
                    height: coreRow.height
                    radius: FoamyStyle.rounded ? Style.space(2) : 0
                    color: view.coreLoad(modelData) >= r.preference("cpuWarning") ? r.urgent : view.cpuColor
                    opacity: modelData === null ? 0.08 : view.hoveredCore === index ? 1 : 0.15 + 0.75 * view.coreLoad(modelData) / 100
                  }
                }
              }
              MouseArea {
                id: coresMouse
                anchors.fill: coreRow
                hoverEnabled: true
                onPositionChanged: function (mouse) {
                  cursor.hover(view.coresStop)
                  view.hoveredCore = Math.min(view.cores.length - 1, Math.max(0, Math.floor((FoamyI18n.rightToLeft ? width - mouse.x : mouse.x) / Math.max(1, width) * view.cores.length)))
                }
                onExited: if (!cursor.at(view.coresStop))
                  view.hoveredCore = -1
              }
            }
          }

          Column {
            width: details.columnWidth
            spacing: FoamyStyle.gapLarge
            SectionHeading {
              title: FoamyI18n.t("Storage")
            }
            Caption {
              width: parent.width
              visible: VitalsService.diskStatus !== ""
              text: FoamyI18n.t(VitalsService.diskStatus)
            }
            Repeater {
              model: view.disks
              Column {
                required property var modelData
                width: details.columnWidth
                spacing: FoamyStyle.gap
                DataRow {
                  label: parent.modelData.mount === "/" ? FoamyI18n.t("Root filesystem") : parent.modelData.mount
                  value: view.diskCapacity(parent.modelData.usedKb, parent.modelData.totalKb)
                }
                Meter {
                  width: parent.width
                  value: 100 * parent.modelData.usedKb / parent.modelData.totalKb
                }
              }
            }
          }
        }

        Rectangle {
          width: content.inner
          height: 1
          color: FoamyPalette.divider
        }
        FoamyAction {
          id: btopButton
          iconName: "terminal"
          label: "btop"
          tooltipText: FoamyI18n.t("Open btop")
          foreground: view.dim
          hasCursor: cursor.at(view.btopStop)
          onHoveredChanged: if (hovered)
            cursor.hover(view.btopStop)
          onClicked: view.btopRequested()
        }
      }
    }
  }

  // --- settings ------------------------------------------------------------------------
  FoamySettingsPage {
    id: settingsPage
    visible: view.editingSettings
    width: parent.width
    height: parent.height
    saving: view.saving
    error: view.settingsError
    retryable: view.retryable
    onBack: view.settingsClosed()
    onRetry: view.retryRequested()

    FoamySettingsSection {
      width: parent.width
      title: FoamyI18n.t("Bar")
      // A short choice: the kit row form, in line with the toggle below and
      // the compact number rows.
      FoamyDropdown {
        objectName: "barDisplay"
        width: parent.width
        compact: true
        label: FoamyI18n.t("Show in the bar")
        value: String(r.preference("barDisplay"))
        options: Prefs.field(Schema.fields, "barDisplay").options.map(function (v) {
          return {
            value: v,
            label: FoamyI18n.t(Schema.optionLabel(v))
          }
        })
        onChanged: function (value) {
          view.saveRequested("barDisplay", value)
        }
      }
      FoamyToggle {
        width: parent.width
        label: FoamyI18n.t("Use theme warning colors")
        description: FoamyI18n.t("When off, warnings use the theme's red.")
        checked: r.preference("useThemeWarningColors") === true
        Accessible.name: label
        onClicked: view.saveRequested("useThemeWarningColors", !checked)
      }
    }
    FoamySettingsSection {
      width: parent.width
      title: FoamyI18n.t("Usage warnings")
      description: FoamyI18n.t("Warns at or above these percentages.")
      NumberRow {
        key: "cpuWarning"
      }
      NumberRow {
        key: "memoryWarning"
      }
      NumberRow {
        key: "gpuWarning"
      }
      NumberRow {
        key: "vramWarning"
      }
    }
    FoamySettingsSection {
      width: parent.width
      title: FoamyI18n.t("Temperature warning margin")
      description: FoamyI18n.t("Warn this many °C below the hardware limit.")
      NumberRow {
        key: "cpuTemperatureMargin"
      }
      NumberRow {
        key: "gpuTemperatureMargin"
      }
    }
  }

  // --- parts -----------------------------------------------------------------------------
  component Label: Text {
    textFormat: Text.PlainText
    color: view.foreground
    font.family: FoamyStyle.fontFamily
    font.pixelSize: FoamyStyle.font.subtitle
    elide: Text.ElideRight
    horizontalAlignment: Text.AlignLeft
  }
  component Caption: Label {
    color: view.dim
    font.pixelSize: FoamyStyle.font.bodySmall
  }

  component Overview: Column {
    id: card
    property string label: ""
    property string glyph: ""
    property string value: "--"
    property string detail: ""
    property real meterValue: NaN
    property bool warning: false
    width: view.cellWidth
    spacing: Style.space(6)
    RowLayout {
      width: parent.width
      Caption {
        text: card.label
        color: card.warning ? r.urgent : view.dim
        Layout.fillWidth: true
      }
      OpticalGlyph {
        Layout.preferredWidth: Style.space(14)
        Layout.preferredHeight: Style.space(14)
        text: card.glyph
        fontFamily: FoamyStyle.monoFamily
        fontSize: FoamyStyle.font.bodySmall
        color: card.warning ? r.urgent : view.dim
      }
    }
    Item {
      width: parent.width
      implicitHeight: cardValue.implicitHeight
      Label {
        id: cardValue
        anchors.left: parent.left
        text: card.value
        font.pixelSize: FoamyStyle.font.display
        color: card.warning ? r.urgent : view.foreground
      }
      // Secondary detail beside the reading, aligned on its baseline.
      Caption {
        anchors.left: cardValue.right
        anchors.leftMargin: FoamyStyle.gap
        anchors.right: parent.right
        anchors.baseline: cardValue.baseline
        text: card.detail
        horizontalAlignment: Text.AlignRight
        color: card.warning ? r.urgent : view.dim
      }
    }
    Meter {
      width: parent.width
      value: card.meterValue
      opacity: isFinite(card.meterValue) ? 1 : 0
      fillColor: card.warning ? r.urgent : view.foreground
    }
  }

  component HistoryPlot: Item {
    id: plotRoot
    // Time runs left to right in both layout directions.
    LayoutMirroring.enabled: false
    LayoutMirroring.childrenInherit: true
    property alias series: chart.series
    property alias maximum: chart.maximum
    property alias centered: chart.centered
    property string topLabel: ""
    property string middleLabel: ""
    property string bottomLabel: ""
    readonly property real axisWidth: Math.ceil(Math.max(topTick.implicitWidth, middleTick.implicitWidth, bottomTick.implicitWidth)) + FoamyStyle.gap
    Caption {
      id: topTick
      width: plotRoot.axisWidth - FoamyStyle.gap
      anchors.top: parent.top
      text: plotRoot.topLabel
      font.pixelSize: FoamyStyle.font.caption
      horizontalAlignment: Text.AlignRight
    }
    Caption {
      id: middleTick
      width: topTick.width
      anchors.verticalCenter: parent.verticalCenter
      text: plotRoot.middleLabel
      font.pixelSize: FoamyStyle.font.caption
      horizontalAlignment: Text.AlignRight
    }
    Caption {
      id: bottomTick
      width: topTick.width
      anchors.bottom: parent.bottom
      text: plotRoot.bottomLabel
      font.pixelSize: FoamyStyle.font.caption
      horizontalAlignment: Text.AlignRight
    }
    HistoryChart {
      id: chart
      anchors.fill: parent
      anchors.leftMargin: plotRoot.axisWidth
      endTime: view.live ? VitalsService.sampledAt : 0
      gridColor: Qt.alpha(view.foreground, 0.14)
      zeroColor: Qt.alpha(view.foreground, 0.40)
    }
  }

  component Meter: Rectangle {
    property real value: NaN
    property color fillColor: view.foreground
    height: Style.space(4)
    radius: FoamyStyle.pillRadius(height)
    color: Qt.alpha(view.foreground, 0.12)
    Rectangle {
      anchors.left: parent.left
      width: parent.width * (isFinite(parent.value) ? Math.max(0, Math.min(100, parent.value)) / 100 : 0)
      height: parent.height
      radius: parent.radius
      color: parent.fillColor
    }
  }

  component Legend: Row {
    id: legend
    property string text: ""
    property real textWidth: -1  // > 0 caps the label, eliding with an ellipsis
    property color dotColor: view.cpuColor
    property bool dashed: false
    spacing: Style.space(6)
    Row {
      anchors.verticalCenter: parent.verticalCenter
      spacing: FoamyStyle.gapTiny
      Repeater {
        model: legend.dashed ? 3 : 1
        Rectangle {
          width: Style.space(legend.dashed ? 4 : 16)
          height: Style.space(2)
          radius: FoamyStyle.pillRadius(height)
          color: legend.dotColor
        }
      }
    }
    Caption {
      text: legend.text
      elide: Text.ElideRight
      width: legend.textWidth > 0 ? Math.min(implicitWidth, legend.textWidth) : implicitWidth
    }
  }

  component SectionHeading: Item {
    property string title: ""
    property string detail: ""
    width: parent.width
    implicitHeight: Math.max(sectionTitle.implicitHeight, sectionDetail.implicitHeight)
    Label {
      id: sectionTitle
      anchors.left: parent.left
      anchors.right: sectionDetail.left
      anchors.rightMargin: FoamyStyle.gap
      text: parent.title
      font.weight: Font.DemiBold
      font.pixelSize: FoamyStyle.font.title
    }
    Caption {
      id: sectionDetail
      anchors.right: parent.right
      width: Math.min(implicitWidth, parent.width * 0.65)
      text: parent.detail
      horizontalAlignment: Text.AlignRight
    }
  }

  component DataRow: Item {
    property string label: ""
    property string value: ""
    property bool subdued: false
    width: parent.width
    implicitHeight: Math.max(rowLabel.implicitHeight, rowValue.implicitHeight)
    Caption {
      id: rowLabel
      anchors.left: parent.left
      anchors.right: rowValue.left
      anchors.rightMargin: FoamyStyle.gap
      text: parent.label
    }
    Label {
      id: rowValue
      anchors.right: parent.right
      width: Math.min(implicitWidth, parent.width * 0.65)
      text: parent.value
      color: parent.subdued ? view.dim : view.foreground
      font.pixelSize: FoamyStyle.font.bodySmall
      horizontalAlignment: Text.AlignRight
    }
  }

  component ProcessSection: Column {
    id: section
    property string title: ""
    property var entries: []
    property bool memoryMode: false
    spacing: FoamyStyle.gapSmall
    SectionHeading {
      title: section.title
    }
    DataRow {
      label: FoamyI18n.t("Process")
      value: section.memoryMode ? FoamyI18n.t("Memory") : "CPU"
      subdued: true
    }
    Caption {
      visible: section.entries.length === 0
      text: "--"
    }
  }

  // One ranking row; part of the panel cursor. Enter or a click opens btop.
  component ProcessRow: FoamyCursorSurface {
    id: row
    required property var modelData
    required property int index
    property var section
    property int first: 0
    readonly property int stop: first + index
    width: section ? section.width + FoamyStyle.gap * 2 : 0
    x: -FoamyStyle.gap
    height: Math.max(processName.implicitHeight, processValue.implicitHeight) + FoamyStyle.gapSmall * 2
    hasCursor: cursor.at(stop)
    onHovered: cursor.hover(stop)
    onClicked: view.btopRequested()
    Label {
      id: processName
      anchors.left: parent.left
      anchors.leftMargin: FoamyStyle.gap
      anchors.right: processValue.left
      anchors.rightMargin: FoamyStyle.gapLarge
      anchors.verticalCenter: parent.verticalCenter
      text: view.processLabel(row.modelData)
    }
    Label {
      id: processValue
      anchors.right: parent.right
      anchors.rightMargin: FoamyStyle.gap
      anchors.verticalCenter: parent.verticalCenter
      text: row.section && row.section.memoryMode ? view.memoryAmount(row.modelData.rssKb) : view.processShare(row.modelData.pct)
    }
    PanelToolTip {
      visible: row.hasCursor
      text: view.processLabel(row.modelData) + (row.section && row.section.memoryMode ? "" : " · " + FoamyI18n.t("100% = 1 thread"))
      fontFamily: FoamyStyle.fontFamily
    }
  }

  // Every GPU in one table, shown in place of the GPU tiles once there are two.
  component GraphicsSection: Column {
    id: section
    // Name, busy, temperature and VRAM columns, as fractions of the row.
    readonly property var columns: [0.40, 0.24, 0.13, 0.23]
    function columnWidth(i) {
      return width * columns[i]
    }
    width: content.inner
    visible: r.multiGpu
    spacing: FoamyStyle.gapSmall
    SectionHeading {
      title: FoamyI18n.t("Graphics")
      detail: FoamyI18n.tn("%1 GPUs", r.gpus.length)
    }
    Row {
      width: parent.width
      Caption {
        width: section.columnWidth(0)
        text: FoamyI18n.t("GPU")
      }
      Caption {
        width: section.columnWidth(1)
        text: FoamyI18n.t("Busy")
      }
      Caption {
        width: section.columnWidth(2)
        text: FoamyI18n.t("Temp")
        horizontalAlignment: Text.AlignRight
      }
      Caption {
        width: section.columnWidth(3)
        text: FoamyI18n.t("VRAM")
        horizontalAlignment: Text.AlignRight
      }
    }
    // The GpuRow repeater goes here, declared where its id is visible to the view.
  }

  // One GPU; part of the panel cursor. Enter or a click pins it to the bar.
  component GpuRow: FoamyCursorSurface {
    id: row
    required property var modelData
    required property int index
    property var section
    readonly property int stop: view.graphicsStart + index
    readonly property string name: Gpus.displayName(modelData, r.gpuRows, false)
    readonly property bool pinned: r.pinnedGpu === modelData.id
    readonly property bool onBar: !!r.barGpu && r.barGpu.id === modelData.id
    readonly property bool asleep: modelData.asleep === true
    readonly property bool intel: modelData.vendor === "intel"
    readonly property string action: pinned ? FoamyI18n.t("Click to unpin") : FoamyI18n.t("Click to pin to bar")
    function value(key) {
      return VitalsService.reading(modelData, key)
    }
    width: section ? section.width + FoamyStyle.gap * 2 : 0
    x: -FoamyStyle.gap
    height: nameCell.implicitHeight + FoamyStyle.gapSmall * 2
    hasCursor: cursor.at(stop)
    onHovered: cursor.hover(stop)
    onClicked: view.togglePin(modelData.id)
    Accessible.role: Accessible.Button
    Accessible.name: name + " · " + action

    Row {
      anchors.left: parent.left
      anchors.leftMargin: FoamyStyle.gap
      anchors.verticalCenter: parent.verticalCenter
      Column {
        id: nameCell
        width: row.section ? row.section.columnWidth(0) : 0
        spacing: FoamyStyle.gapTiny
        Row {
          id: nameLine
          // Full cell width, so RTL lays the name out from the leading edge.
          width: parent.width
          spacing: Style.space(6)
          // The tag follows the name; only the name elides.
          Label {
            width: Math.min(implicitWidth, nameCell.width - FoamyStyle.gap - (barTag.visible ? barTag.width + nameLine.spacing : 0))
            text: row.name
          }
          Item {
            id: barTag
            visible: row.onBar
            anchors.verticalCenter: parent.verticalCenter
            width: row.pinned ? pinGlyph.width : barPill.width
            height: row.pinned ? pinGlyph.height : barPill.height
            OpticalGlyph {
              id: pinGlyph
              visible: row.pinned
              width: Style.space(12)
              height: Style.space(12)
              text: "\u{f0403}"
              fontFamily: FoamyStyle.monoFamily
              fontSize: Style.space(12)
              color: view.dim
            }
            Rectangle {
              id: barPill
              visible: !row.pinned
              width: barText.implicitWidth + Style.space(10)
              height: barText.implicitHeight + Style.space(2)
              radius: FoamyStyle.pillRadius(height)
              color: "transparent"
              border.width: 1
              border.color: FoamyPalette.divider
              Caption {
                id: barText
                anchors.centerIn: parent
                text: FoamyI18n.t("bar")
                font.pixelSize: FoamyStyle.font.caption
              }
            }
          }
        }
        Caption {
          width: parent.width
          text: row.modelData.type === "integrated" ? FoamyI18n.t("Integrated") : row.modelData.type === "discrete" ? FoamyI18n.t("Discrete") : ""
        }
      }
      Row {
        width: row.section ? row.section.columnWidth(1) : 0
        anchors.verticalCenter: parent.verticalCenter
        spacing: FoamyStyle.gap
        Meter {
          visible: !row.asleep
          width: Style.space(40)
          anchors.verticalCenter: parent.verticalCenter
          value: row.value("busy")
          fillColor: row.modelData.busyWarning ? r.urgent : view.foreground
        }
        Label {
          text: row.asleep ? FoamyI18n.t("Sleeping") : view.percent(row.value("busy"))
          color: row.asleep ? view.dim : row.modelData.busyWarning ? r.urgent : view.foreground
        }
      }
      Label {
        width: row.section ? row.section.columnWidth(2) : 0
        anchors.verticalCenter: parent.verticalCenter
        horizontalAlignment: Text.AlignRight
        text: row.asleep ? FoamyI18n.t("Sleeping") : view.degrees(row.modelData.thermal.temperature)
        color: row.asleep ? view.dim : row.modelData.hot ? r.urgent : view.foreground
      }
      Label {
        width: row.section ? row.section.columnWidth(3) : 0
        anchors.verticalCenter: parent.verticalCenter
        horizontalAlignment: Text.AlignRight
        text: row.asleep ? FoamyI18n.t("Sleeping") : row.intel ? view.memoryAmount(row.value("memoryPrivate") / 1024) : view.pair(row.value("vramUsed") / 1073741824, row.value("vramTotal") / 1073741824)
        color: row.asleep ? view.dim : row.modelData.vramWarning ? r.urgent : view.foreground
      }
    }
    PanelToolTip {
      visible: row.hasCursor
      text: [row.name, row.modelData.id, view.temperatureDetail(row.modelData.thermal), isNaN(row.value("frequencyMHz")) ? "" : FoamyI18n.number(Math.round(row.value("frequencyMHz"))) + " MHz", row.intel ? FoamyI18n.t("Intel GPU: busiest engine and private allocations from accessible processes for this user.") : "", row.action].filter(function (line) {
        return line !== ""
      }).join("\n")
      fontFamily: FoamyStyle.fontFamily
    }
  }

  // An integer setting: the kit field shows the saved value (following
  // changes from other monitors or the CLI), keeps a draft while editing,
  // and commits a valid changed number once; Escape restores it. Compact:
  // label on the start, a field sized to three digits, the unit after it.
  component NumberRow: FoamyNumberField {
    id: numberRow
    property string key: ""
    readonly property var schemaField: Prefs.field(Schema.fields, key)
    width: parent.width
    compact: true
    label: FoamyI18n.t(schemaField.label)
    unit: schemaField.unit || ""
    value: Prefs.value(Schema.fields, view.settings, key)
    from: schemaField.min
    to: schemaField.max
    stepSize: schemaField.step || 1
    field.Accessible.name: FoamyI18n.t(schemaField.group || "") + " " + FoamyI18n.t(schemaField.label)
    onCommitted: function (v) {
      view.saveRequested(key, v)
    }
  }
}
