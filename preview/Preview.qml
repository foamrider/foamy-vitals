import QtQuick
import "../kit"
import ".." as Vitals

// Popup states for the foamy-kit preview harness (mock data only: no
// processes, no files). render.py <plugin> [--states a,b]
Item {
  id: preview
  required property QtObject bar
  required property Item anchorItem
  property var previewStates: ["dashboard", "warning", "intel", "nvidia-sleeping", "graphics", "keyboard", "error", "settings", "settings-error"]
  property string previewState: "dashboard"
  property int settleMs: 700
  readonly property Item popup: popup

  readonly property real now: 600000
  function series(base, swing, phase) {
    var points = []
    for (var i = 0; i <= 60; i++) points.push({ time: now - 120000 + i * 2000, value: Math.max(0, base + swing * Math.sin(i / 6 + phase)) })
    return points
  }
  function sample(state) {
    var hot = state === "warning"
    var stats = {
      mode: "full", sampleTimeMs: now, cpu: hot ? 97 : 23,
      cores: [18, 32, 9, 64, 12, 7, 41, 22, 15, 5, 88, 13, 26, 9, 31, 17].map(function(v) { return hot ? Math.min(100, v + 70) : v }),
      memUsedKb: 11200000, memTotalKb: 32682348, cpuTemp: hot ? 91 : 54, cpuTempCrit: 100,
      gpus: [{
        id: "0000:03:00.0", vendor: "amd", driver: "amdgpu", name: "Radeon RX 7700 XT / 7800 XT", type: "discrete", asleep: false,
        busy: 12, vramUsed: 2147483648, vramTotal: 17095983104, memoryPrivate: null, frequencyMHz: null,
        thermals: [{ temperature: 47, critical: 110, label: "Edge" }, { temperature: 55, critical: 110, label: "Hotspot" }, { temperature: 66, critical: 108, label: "Memory" }],
        error: ""
      }],
      processes: [
        { name: "firefox", where: "", pct: hot ? 312.4 : 38.2 }, { name: "code", where: "foamy-vitals", pct: 21.7 },
        { name: "qs", where: "", pct: 6.1 }, { name: "Hyprland", where: "", pct: 4.8 }, { name: "pipewire", where: "", pct: 1.2 }],
      memoryProcesses: [
        { name: "firefox", where: "", rssKb: 2350000 }, { name: "code", where: "foamy-vitals", rssKb: 1180000 },
        { name: "steamwebhelper", where: "", rssKb: 820000 }, { name: "qs", where: "", rssKb: 410000 }, { name: "Hyprland", where: "", rssKb: 190000 }],
      network: { interface: "wlan0", rxBytes: 1, txBytes: 1 }
    }
    if (state === "intel") {
      stats.cores = stats.cores.slice(0, 8)
      stats.gpus = [{
        id: "0000:00:02.0", vendor: "intel", driver: "xe", name: "Intel Arc Graphics 130V / 140V", type: "integrated", asleep: false,
        busy: 34, vramUsed: null, vramTotal: null, memoryPrivate: 734003200, frequencyMHz: 1250, thermals: [], error: ""
      }]
    } else if (state === "nvidia-sleeping") {
      stats.gpus = [{
        id: "0000:01:00.0", vendor: "nvidia", driver: "nvidia", name: "GeForce RTX 4070 Max-Q / Mobile", type: "discrete", asleep: true,
        busy: null, vramUsed: null, vramTotal: null, memoryPrivate: null, frequencyMHz: null, thermals: [], error: ""
      }]
    } else if (state === "graphics") {
      // An APU, the pinned discrete card and a sleeping NVIDIA card.
      stats.gpus = [{
        id: "0000:c1:00.0", vendor: "amd", driver: "amdgpu", name: "Radeon 780M", type: "integrated", asleep: false,
        busy: 31, vramUsed: 1073741824, vramTotal: 8589934592, memoryPrivate: null, frequencyMHz: null,
        thermals: [{ temperature: 51, critical: 100, label: "Edge" }], error: ""
      }, stats.gpus[0], {
        id: "0000:05:00.0", vendor: "nvidia", driver: "nvidia", name: "GeForce RTX 4070", type: "discrete", asleep: true,
        busy: null, vramUsed: null, vramTotal: null, memoryPrivate: null, frequencyMHz: null, thermals: [], error: ""
      }]
    }
    return stats
  }

  // GPU history is keyed by PCI address: one line per mock GPU. An asleep
  // GPU draws 0, as the live history records it.
  function gpuHistory(state) {
    var history = {}
    var gpus = sample(state).gpus
    if (state === "graphics") {
      for (var i = 0; i < gpus.length; i++) history[gpus[i].id] = gpus[i].asleep ? series(0, 0, 0) : series(10 + i * 18, 9, 2 + i * 2)
      return history
    }
    history[gpus[0].id] = gpus[0].asleep ? series(0, 0, 0) : series(12, 9, 2)
    return history
  }

  function apply(state) {
    var s = Vitals.VitalsService
    s.hostname = state === "intel" ? "foamy-x1" : "foamy-desktop"
    s.chassis = state === "intel" || state === "nvidia-sleeping" ? "laptop" : "desktop"
    s.uptimeMinutes = 3 * 60 + 12
    s.loadAverages = [0.84, 0.66, 0.52]
    s.disks = [{ device: "/dev/nvme0n1p2", mount: "/", totalKb: 976000000, usedKb: 412000000 }, { device: "/dev/sda1", mount: "/data", totalKb: 1953000000, usedKb: 1520000000 }]
    s.diskStatus = ""
    s.networkDown = 2.4 * 1048576
    s.networkUp = 180 * 1024
    s.sampledAt = now
    s.history = { cpu: series(state === "warning" ? 80 : 22, 14, 0), memory: series(34, 2, 1), gpus: gpuHistory(state),
                  down: series(1.6 * 1048576, 1.1 * 1048576, 0.5), up: series(150 * 1024, 90 * 1024, 1.5) }
    if (state === "error") {
      s.stats = ({})
      s.detail = ({ processes: [], memoryProcesses: [] })
      s.telemetryError = "Telemetry unavailable. Retrying…"
      s.history = { cpu: [], memory: [], gpus: {}, up: [], down: [] }
      s.networkDown = NaN
      s.networkUp = NaN
    } else {
      s.stats = sample(state)
      s.detail = { processes: s.stats.processes, memoryProcesses: s.stats.memoryProcesses }
      s.telemetryError = ""
    }
  }

  onPreviewStateChanged: {
    apply(previewState)
    var view = popup.bodyItem
    if (!view) return
    view.cursor.reset()
    view.hoveredCore = -1
    if (previewState === "keyboard") {
      view.moveRequested(0, 1)
      view.moveRequested(0, 1)
      view.moveRequested(0, 1)
    }
  }
  Component.onCompleted: apply(previewState)

  FoamyPopup {
    id: popup
    anchorItem: preview.anchorItem
    bar: preview.bar
    open: true
    wide: true
    body: Component {
      Vitals.VitalsView {
        live: true
        settings: preview.previewState === "warning" ? ({ cpuWarning: 90 })
          : preview.previewState === "graphics" ? ({ pinnedGpu: "0000:03:00.0" }) : ({})
        editingSettings: preview.previewState.indexOf("settings") === 0
        settingsError: preview.previewState === "settings-error" ? "Couldn't save the setting." : ""
        retryable: preview.previewState === "settings-error"
      }
    }
  }
}
