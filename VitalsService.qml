pragma Singleton
import QtQuick
import Quickshell.Io
import "History.js" as History
import "Filesystems.js" as Filesystems

// Telemetry for every Vitals bar instance (4.5 draws one bar per monitor).
// A plugin qmldir singleton is one object per shell, so stats.sh, df and the
// history run once however many monitors there are. The leader Panel drives
// sample(); everything else only reads.
//
//   stats        the latest sample (bar and popup readings)
//   detail       the latest full sample: process rankings for the popup. Bar
//                samples never touch it, so lists stay put while hidden
//   history      two minutes of CPU, RAM, GPU and network readings
QtObject {
  id: service

  readonly property string memberScope: "foamy.vitals"
  readonly property string statsCommand: decodeURIComponent(Qt.resolvedUrl("stats.sh").toString().replace(/^file:\/\//, ""))

  property var stats: ({})
  property var detail: ({ processes: [], memoryProcesses: [] })
  property var history: ({ cpu: [], memory: [], gpu: [], up: [], down: [] })
  property var networkSnapshot: null
  property real sampledAt: 0
  property real networkUp: NaN
  property real networkDown: NaN
  property string telemetryError: ""
  property bool fullSample: false
  property int samples: 0

  property string hostname: "Vitals"
  property string chassis: ""
  property var loadAverages: []
  property int uptimeMinutes: -1
  property var disks: []
  property string diskStatus: "Reading disk capacity"
  property string lastDiagnostic: ""
  // Nothing is read or run until the leader asks for the first sample (the
  // preview harness feeds mock values instead).
  property bool started: false

  function reading(source, key) {
    var v = source ? source[key] : null
    if (v === null || v === undefined) return NaN
    var n = Number(v)
    return isFinite(n) ? n : NaN
  }

  // full: popup readings too (a popup is open); otherwise the bar-only sample.
  function start() {
    if (started) return
    started = true
    chassisProcess.running = true
  }

  function sample(full) {
    start()
    if (statsProcess.running) return
    fullSample = !!full
    statsProcess.command = ["timeout", "--kill-after=1s", "5s", statsCommand].concat(full ? [] : ["--bar"])
    statsProcess.running = true
    if (full) {
      uptimeFile.reload()
      loadFile.reload()
    }
  }

  function refreshDisks() {
    start()
    if (!diskProcess.running) diskProcess.running = true
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
    var total = reading(parsed, "memTotalKb")
    history = {
      cpu: History.append(history.cpu, time, reading(parsed, "cpu")),
      memory: History.append(history.memory, time, total > 0 ? 100 * reading(parsed, "memUsedKb") / total : NaN),
      gpu: History.append(history.gpu, time, reading(parsed, "gpuBusy")),
      up: History.append(net.changed ? [] : history.up, time, net.up),
      down: History.append(net.changed ? [] : history.down, time, net.down)
    }
  }

  function fail(message) {
    stats = ({})
    telemetryError = message
    networkSnapshot = null
    networkUp = NaN
    networkDown = NaN
    // Missing samples break history instead of presenting the last reading as current.
    var at = sampledAt + (fullSample ? 2000 : 5000)
    history = {
      cpu: History.append(history.cpu, at, NaN), memory: History.append(history.memory, at, NaN),
      gpu: History.append(history.gpu, at, NaN), up: History.append(history.up, at, NaN), down: History.append(history.down, at, NaN)
    }
    console.warn("foamy.vitals: " + message)
  }

  function parse(raw) {
    var parsed = null
    try {
      parsed = JSON.parse(raw)
    } catch (error) {
      parsed = null
    }
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object"
        || typeof parsed.sampleTimeMs !== "number" || !isFinite(parsed.sampleTimeMs)
        || !Array.isArray(parsed.cores) || !Array.isArray(parsed.processes)
        || !Array.isArray(parsed.memoryProcesses)) {
      fail("Telemetry unavailable. Retrying…")
      return
    }
    stats = parsed
    telemetryError = ""
    samples++
    if (parsed.mode !== "bar") detail = { processes: parsed.processes, memoryProcesses: parsed.memoryProcesses }
    recordSample(parsed)
  }

  property Process statsProcess: Process {
    stdout: StdioCollector { id: statsOutput; waitForEnd: true }
    // Helper diagnostics (Intel scan budget, nvidia-smi errors) go to the
    // shell log once per distinct message instead of disappearing.
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = text.trim()
        if (message !== "" && message !== service.lastDiagnostic) console.warn("foamy.vitals: " + message)
        service.lastDiagnostic = message
      }
    }
    onExited: function(code) {
      if (code !== 0) service.fail("Telemetry unavailable. Retrying…")
      else service.parse(statsOutput.text)
    }
  }

  property FileView hostnameFile: FileView {
    path: service.started ? "/proc/sys/kernel/hostname" : ""
    onLoaded: service.hostname = text().trim() || "Vitals"
  }
  property FileView uptimeFile: FileView {
    path: service.started ? "/proc/uptime" : ""
    onLoaded: service.uptimeMinutes = Math.floor(Number(text().split(" ")[0]) / 60)
  }
  property FileView loadFile: FileView {
    path: service.started ? "/proc/loadavg" : ""
    onLoaded: service.loadAverages = text().trim().split(/\s+/).slice(0, 3).map(Number)
  }

  // Chassis detection is stable for the session; ask once per shell.
  property Process chassisProcess: Process {
    command: ["timeout", "3s", "hostnamectl", "chassis"]
    stdout: StdioCollector { onStreamFinished: service.chassis = text.trim() }
    onExited: function(code) {
      if (code !== 0) {
        service.chassis = ""
        console.warn("foamy.vitals: chassis query failed", code)
      }
    }
  }

  // Capacity changes slowly: refreshed when a popup opens and every minute
  // while one stays open, never on the telemetry poll.
  property Process diskProcess: Process {
    command: ["timeout", "3s", "df", "-P", "-k", "-l", "-T"]
    environment: ({ LC_ALL: "C" })
    stdout: StdioCollector {
      onStreamFinished: {
        service.disks = Filesystems.parse(text)
        service.diskStatus = service.disks.length ? "" : "No mounted local disks"
      }
    }
    onExited: function(code) {
      if (code !== 0) {
        service.disks = []
        service.diskStatus = "Disk capacity unavailable"
        console.warn("foamy.vitals: disk capacity query failed", code)
      }
    }
  }
}
