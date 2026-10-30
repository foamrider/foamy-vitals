import QtQuick
import qs.Commons as Commons
import "kit"
import "kit/FoamyPrefs.js" as Prefs
import "Preferences.js" as Schema
import "Thermals.js" as Thermals

// Readings and warning state derived from VitalsService.stats and the
// widget's settings. The bar widget and the popup view each hold one, so the
// bar warning colour and the popup cards always agree.
QtObject {
  id: root

  property var settings: ({})
  property var stats: VitalsService.stats

  function preference(key) { return Prefs.value(Schema.fields, settings, key) }
  function reading(key) { return VitalsService.reading(stats, key) }

  readonly property real cpu: reading("cpu")
  readonly property real memory: reading("memTotalKb") > 0 ? 100 * reading("memUsedKb") / reading("memTotalKb") : NaN
  readonly property real gpu: reading("gpuBusy")
  readonly property real vram: reading("vramTotal") > 0 ? 100 * reading("vramUsed") / reading("vramTotal") : NaN
  readonly property bool intelGpu: stats.gpuIntel === true
  readonly property bool gpuSuspended: stats.gpuSuspended === true
  readonly property var cores: Array.isArray(stats.cores) ? stats.cores : []
  readonly property var cpuThermal: Thermals.summarize([
    { temperature: reading("cpuTemp"), critical: reading("cpuTempCrit") }
  ], preference("cpuTemperatureMargin"))
  readonly property var gpuThermal: Thermals.summarize(stats.gpuThermals, preference("gpuTemperatureMargin"))

  readonly property bool cpuWarning: cpu >= preference("cpuWarning")
  readonly property bool memoryWarning: memory >= preference("memoryWarning")
  readonly property bool gpuWarning: gpu >= preference("gpuWarning")
  readonly property bool vramWarning: vram >= preference("vramWarning")
  readonly property bool hot: cpuThermal.hot || gpuThermal.hot
  readonly property bool warning: hot || cpuWarning || memoryWarning || gpuWarning || vramWarning

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
