import QtQuick
import qs.Ui
import ".." as Dev

// The dev (pre-kit) Vitals popup for `render.py --trio` "Before" shots. The
// dev Panel holds its popup (a KeyboardPanel fork) inline, so this builds
// the Panel at the bar button and keeps feeding it the same mock readings
// as Preview.qml (its own sampler would otherwise show this machine).
Item {
  id: preview
  required property QtObject bar
  required property Item anchorItem
  property var previewStates: ["dashboard", "warning", "intel", "error", "settings"]
  property string previewState: "dashboard"
  property int settleMs: 1500
  property Item fork: null
  readonly property Item popup: fork || panel

  readonly property real now: 600000
  function series(base, swing, phase) {
    var points = []
    for (var i = 0; i <= 60; i++) points.push({ time: now - 120000 + i * 2000, value: Math.max(0, base + swing * Math.sin(i / 6 + phase)) })
    return points
  }
  function stats(state) {
    var hot = state === "warning"
    var s = {
      sampleTimeMs: now, cpu: hot ? 97 : 23,
      cores: [18, 32, 9, 64, 12, 7, 41, 22, 15, 5, 88, 13, 26, 9, 31, 17].map(function(v) { return hot ? Math.min(100, v + 70) : v }),
      memUsedKb: 11200000, memTotalKb: 32682348, cpuTemp: hot ? 91 : 54, cpuTempCrit: 100,
      gpuBusy: 12, vramUsed: 2147483648, vramTotal: 17095983104, gpuTemp: 47,
      gpuThermals: [{ temperature: 47, critical: 110, label: "Edge" }, { temperature: 55, critical: 110, label: "Hotspot" }, { temperature: 66, critical: 108, label: "Memory" }],
      gpuError: "", gpuIntel: false, gpuMemoryPrivate: null, gpuFrequencyMHz: null, gpuEngines: {},
      processes: [
        { name: "firefox", where: "", pct: hot ? 312.4 : 38.2 }, { name: "code", where: "foamy-vitals", pct: 21.7 },
        { name: "qs", where: "", pct: 6.1 }, { name: "Hyprland", where: "", pct: 4.8 }, { name: "pipewire", where: "", pct: 1.2 }],
      memoryProcesses: [
        { name: "firefox", where: "", rssKb: 2350000 }, { name: "code", where: "foamy-vitals", rssKb: 1180000 },
        { name: "steamwebhelper", where: "", rssKb: 820000 }, { name: "qs", where: "", rssKb: 410000 }, { name: "Hyprland", where: "", rssKb: 190000 }],
      network: { interface: "wlan0", rxBytes: 1, txBytes: 1 }
    }
    if (state === "intel") {
      s.cores = s.cores.slice(0, 8)
      s.gpuIntel = true; s.gpuBusy = 34; s.vramUsed = null; s.vramTotal = null
      s.gpuMemoryPrivate = 734003200; s.gpuFrequencyMHz = 1250; s.gpuTemp = null; s.gpuThermals = []
    }
    return s
  }

  function feed() {
    var state = previewState
    panel.hostname = state === "intel" ? "foamy-x1" : "foamy-desktop"
    panel.chassis = state === "intel" ? "laptop" : "desktop"
    panel.uptimeMinutes = 192
    panel.loadAverages = [0.84, 0.66, 0.52]
    panel.disks = [{ device: "/dev/nvme0n1p2", mount: "/", totalKb: 976000000, usedKb: 412000000 }, { device: "/dev/sda1", mount: "/data", totalKb: 1953000000, usedKb: 1520000000 }]
    panel.diskStatus = ""
    panel.networkDown = 2.4 * 1048576
    panel.networkUp = 180 * 1024
    panel.sampledAt = now
    if (state === "error") {
      panel.stats = ({})
      panel.telemetryError = "Telemetry unavailable. Retrying…"
      panel.history = { cpu: [], memory: [], gpu: [], up: [], down: [] }
      panel.networkDown = NaN
      panel.networkUp = NaN
    } else {
      panel.stats = stats(state)
      panel.telemetryError = ""
      panel.history = { cpu: series(state === "warning" ? 80 : 22, 14, 0), memory: series(34, 2, 1), gpu: series(12, 9, 2),
                        down: series(1.6 * 1048576, 1.1 * 1048576, 0.5), up: series(150 * 1024, 90 * 1024, 1.5) }
    }
    if (!fork && FakeDisplay.overlay) {
      var items = FakeDisplay.overlay.children
      for (var i = 0; i < items.length; i++) if (items[i].anchorItem !== undefined && items[i].contentWidth !== undefined) fork = items[i]
    }
  }

  Dev.Panel {
    id: panel
    parent: preview.anchorItem
    anchors.fill: parent
    bar: preview.bar
    moduleName: "foamy.vitals"
    settings: ({ cpuWarning: 90 })
  }
  Timer { interval: 40; repeat: true; running: true; onTriggered: preview.feed() }
  onPreviewStateChanged: {
    feed()
    if (previewState === "settings") panel.openSettings()
    else { panel.closeSettings(); panel.open() }
  }
  Component.onCompleted: { feed(); panel.open() }
}
