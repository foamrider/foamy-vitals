// Inline widget settings are the source of truth; invalid values use defaults.
var fields = [
  {key: "barDisplay", type: "enum", label: "Bar display", defaultValue: "adaptive", options: ["adaptive", "cpu", "memory", "gpu"]},
  {key: "useThemeWarningColors", type: "boolean", label: "Use theme warning colors", description: "When off, warnings use our red for light or dark backgrounds.", defaultValue: true},
  {key: "cpuWarning", type: "integer", label: "CPU", group: "Usage warnings", description: "Warns at or above these percentages.", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "memoryWarning", type: "integer", label: "RAM", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "gpuWarning", type: "integer", label: "GPU", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "vramWarning", type: "integer", label: "VRAM", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "cpuTemperatureMargin", type: "integer", label: "CPU", group: "Temperature warning margin", description: "Warn this many °C below the hardware limit.", unit: "°C", defaultValue: 15, min: 0, max: 100, step: 1},
  {key: "gpuTemperatureMargin", type: "integer", label: "GPU", group: "Temperature warning margin", unit: "°C", defaultValue: 15, min: 0, max: 100, step: 1},
  {key: "pinnedGpu", type: "pci", defaultValue: "", hidden: true} // set by clicking a popup GPU row, not in Settings
]
var visibleFields = fields.filter(function(f) { return !f.hidden })
function field(key) {
  for (var i = 0; i < fields.length; i++) if (fields[i].key === key) return fields[i]
  return null
}
function valid(key, value) {
  var f = field(key)
  if (!f) return false
  if (f.type === "boolean") return typeof value === "boolean"
  if (f.type === "pci") return typeof value === "string" && (value === "" || /^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$/.test(value))
  if (f.type === "enum") return typeof value === "string" && f.options.indexOf(value) >= 0
  return typeof value === "number" && isFinite(value) && Math.floor(value) === value && value >= f.min && value <= f.max
}
function value(settings, key) {
  var f = field(key)
  return f ? settings && valid(key, settings[key]) ? settings[key] : f.defaultValue : undefined
}

function optionLabel(value) {
  return {system: "System", en: "English", nb: "Norsk bokmål", adaptive: "Adaptive", cpu: "CPU", memory: "RAM", gpu: "GPU"}[value] || value
}

if (typeof module !== "undefined") module.exports = {fields: fields, visibleFields: visibleFields, field: field, valid: valid, value: value, optionLabel: optionLabel}
