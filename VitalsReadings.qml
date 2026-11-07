import QtQuick
import qs.Commons as Commons
import "kit"
import "kit/FoamyPrefs.js" as Prefs
import "Preferences.js" as Schema
import "Thermals.js" as Thermals
import "Gpus.js" as Gpus

// Readings and warning state derived from VitalsService.stats and the
// widget's settings. The bar widget and the popup view each hold one, so the
// bar warning colour and the popup cards always agree.
QtObject {
  id: root

  property var settings: ({})
  property var stats: VitalsService.stats

  function preference(key) { return Prefs.value(Schema.fields, settings, key) }
  function reading(key) { return VitalsService.reading(stats, key) }
  // One reading of the bar GPU, or NaN without one.
  function gpuReading(key) { return VitalsService.reading(barGpu, key) }

  readonly property real cpu: reading("cpu")
  readonly property real memory: reading("memTotalKb") > 0 ? 100 * reading("memUsedKb") / reading("memTotalKb") : NaN
  readonly property var cores: Array.isArray(stats.cores) ? stats.cores : []
  readonly property var cpuThermal: Thermals.summarize([
    { temperature: reading("cpuTemp"), critical: reading("cpuTempCrit") }
  ], preference("cpuTemperatureMargin"))

  // Every GPU in display order, each with its own thermal summary and warnings.
  readonly property var gpus: Gpus.order(stats.gpus)
  readonly property string pinnedGpu: preference("pinnedGpu")
  readonly property var gpuRows: gpus.map(function(gpu) {
    var thermal = Thermals.summarize(gpu.thermals, root.preference("gpuTemperatureMargin"))
    var summary = Gpus.summarize(gpu, thermal, { gpu: root.preference("gpuWarning"), vram: root.preference("vramWarning") })
    return Object.assign({}, gpu, summary, { thermal: thermal })
  })
  // The bar follows the pinned GPU while it is connected, otherwise the busiest.
  readonly property var barGpu: Gpus.barGpu(gpuRows, pinnedGpu)
  readonly property bool multiGpu: gpus.length >= 2
  readonly property bool anyIntel: gpus.some(function(gpu) { return gpu.vendor === "intel" })
  readonly property string gpuError: {
    var failed = gpuRows.filter(function(row) { return typeof row.error === "string" && row.error !== "" })
    return failed.length ? failed[0].error : ""
  }
  readonly property bool anyGpuWarning: gpuRows.some(function(row) { return row.warning })
  // A failed sample lists no GPUs; that is not a missing pin. A row error
  // never removes a row, so it does not hide the caption.
  readonly property bool pinnedMissing: VitalsService.telemetryError === "" && Array.isArray(stats.gpus)
    && Gpus.pinnedMissing(gpus, pinnedGpu)

  // The bar GPU's readings. Asleep is idle (0 %); no GPU is no reading.
  readonly property real gpu: barGpu ? (barGpu.asleep ? 0 : gpuReading("busy")) : NaN
  readonly property real vram: Gpus.vram(barGpu)
  readonly property bool intelGpu: !!barGpu && barGpu.vendor === "intel"
  readonly property bool gpuSuspended: !!barGpu && barGpu.asleep === true
  readonly property var gpuThermal: barGpu ? barGpu.thermal : Thermals.summarize([], preference("gpuTemperatureMargin"))

  readonly property bool cpuWarning: cpu >= preference("cpuWarning")
  readonly property bool memoryWarning: memory >= preference("memoryWarning")
  readonly property bool gpuWarning: !!barGpu && barGpu.busyWarning
  readonly property bool vramWarning: !!barGpu && barGpu.vramWarning
  readonly property bool hot: cpuThermal.hot || gpuThermal.hot
  // Any GPU past a threshold warns, not only the one the bar shows.
  readonly property bool warning: cpuThermal.hot || cpuWarning || memoryWarning || anyGpuWarning

  readonly property bool showMemory: !isNaN(memory) && (isNaN(cpu) || memory > cpu)
  readonly property bool showGpu: !isNaN(gpu) && (isNaN(cpu) || gpu > cpu) && (isNaN(memory) || gpu > memory)
  readonly property string barMetric: preference("barDisplay") === "adaptive"
    ? (showGpu ? "gpu" : showMemory ? "memory" : "cpu") : preference("barDisplay")

  // Warnings use the theme's urgent colour, or with the toggle off the
  // theme's own red (colors.toml), darkened on light surfaces.
  function warningColor(surface, urgent) {
    if (preference("useThemeWarningColors")) return urgent
    return surface.hslLightness > 0.5 ? Qt.darker(FoamyPalette.error, 1.35) : FoamyPalette.error
  }
  readonly property color urgent: warningColor(Commons.Color.popups.background, Commons.Color.urgent)
}
