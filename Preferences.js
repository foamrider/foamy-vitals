// Inline widget settings schema (mirrors manifest.json barWidget.schema).
// Validation and saving are foamy-kit's FoamyPrefs.js; label, group,
// description and unit are display hints for the settings page.
var fields = [
  {key: "barDisplay", type: "enum", label: "Show in the bar", defaultValue: "adaptive", options: ["adaptive", "cpu", "memory", "gpu"]},
  {key: "useThemeWarningColors", type: "boolean", label: "Use theme warning colors", description: "When off, warnings use the theme's red.", defaultValue: true},
  {key: "cpuWarning", type: "integer", label: "CPU", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "memoryWarning", type: "integer", label: "RAM", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "gpuWarning", type: "integer", label: "GPU", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "vramWarning", type: "integer", label: "VRAM", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "cpuTemperatureMargin", type: "integer", label: "CPU", group: "Temperature warning margin", unit: "°C", defaultValue: 15, min: 0, max: 100, step: 1},
  {key: "gpuTemperatureMargin", type: "integer", label: "GPU", group: "Temperature warning margin", unit: "°C", defaultValue: 15, min: 0, max: 100, step: 1}
]

function optionLabel(value) {
  return {adaptive: "Adaptive", cpu: "CPU", memory: "RAM", gpu: "GPU"}[value] || value
}

if (typeof module !== "undefined") module.exports = {fields: fields, optionLabel: optionLabel}
