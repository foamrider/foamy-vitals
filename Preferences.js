// Inline widget settings are the source of truth; invalid values use defaults.
var fields = [
  {key: "language", type: "enum", label: "Language", defaultValue: "system", options: ["system", "en", "nb"]},
  {key: "barDisplay", type: "enum", label: "Bar display", defaultValue: "adaptive", options: ["adaptive", "cpu", "memory", "gpu"]},
  {key: "cpuWarning", type: "integer", label: "CPU", group: "Usage warnings", description: "Turns red at or above these percentages.", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "memoryWarning", type: "integer", label: "RAM", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "gpuWarning", type: "integer", label: "GPU", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "vramWarning", type: "integer", label: "VRAM", group: "Usage warnings", unit: "%", defaultValue: 95, min: 1, max: 100, step: 1},
  {key: "cpuTemperatureMargin", type: "integer", label: "CPU", group: "Temperature warning margin", description: "Warn this many °C below the hardware limit.", unit: "°C", defaultValue: 15, min: 0, max: 100, step: 1},
  {key: "gpuTemperatureMargin", type: "integer", label: "GPU", group: "Temperature warning margin", unit: "°C", defaultValue: 15, min: 0, max: 100, step: 1}
]
function field(key) {
  for (var i = 0; i < fields.length; i++) if (fields[i].key === key) return fields[i]
  return null
}
function valid(key, value) {
  var f = field(key)
  if (!f) return false
  if (f.type === "enum") return typeof value === "string" && f.options.indexOf(value) >= 0
  return typeof value === "number" && isFinite(value) && Math.floor(value) === value && value >= f.min && value <= f.max
}
function value(settings, key) {
  var f = field(key)
  return f ? settings && valid(key, settings[key]) ? settings[key] : f.defaultValue : undefined
}
function language(mode, locale) {
  return mode === "en" || mode === "nb" ? mode : /^(nb|nn|no)(_|-|$)/i.test(locale || "") ? "nb" : "en"
}
function optionLabel(value) {
  return {system: "System", en: "English", nb: "Norsk bokmål", adaptive: "Adaptive", cpu: "CPU", memory: "RAM", gpu: "GPU"}[value] || value
}
var norwegian = {
  "Language": "Språk", "System": "System", "Settings": "Innstillinger", "Back": "Tilbake", "Saving…": "Lagrer…",
  "Bar display": "Visning i linjen", "Adaptive": "Automatisk", "Usage warnings": "Bruksvarsler",
  "Temperature warning margin": "Margin for temperaturvarsel",
  "Turns red at or above these percentages.": "Blir rødt ved disse prosentene eller høyere.",
  "Warn this many °C below the hardware limit.": "Varsler så mange °C under maskinvarens temperaturgrense.",
  "Invalid setting.": "Ugyldig innstilling.", "Could not save settings.": "Kunne ikke lagre innstillingene.",
  "Up": "Oppetid", "Load": "Last",
  "CPU TEMP": "CPU-TEMP", "GPU TEMP": "GPU-TEMP", "threads": "tråder", "CPU cores": "CPU-kjerner",
  "Resource usage": "Ressursbruk", "Network traffic": "Nettverkstrafikk", "Storage": "Lagring", "Process": "Prosess", "Root filesystem": "Rotfilsystem",
  "Incoming": "Innkommende", "Outgoing": "Utgående",
  "Top CPU processes": "Høyest CPU-bruk", "Top memory processes": "Høyest minnebruk",
  "Limit unknown": "Ukjent grense", "limit": "grense", "Package": "Pakke", "Hotspot": "Hotspot", "Edge": "Kant", "Memory": "Minne",
  "Unavailable": "Utilgjengelig", "No mounted local disks": "Ingen lokale disker montert",
  "Reading disk capacity": "Leser diskplass", "Disk capacity unavailable": "Diskplass utilgjengelig",
  "Telemetry unavailable. Retrying…": "Målinger utilgjengelige. Prøver igjen…",
  "Install btop to use this shortcut.": "Installer btop for å bruke snarveien."
}
function text(label, language) { return language === "nb" ? norwegian[label] || label : label }
if (typeof module !== "undefined") module.exports = {fields: fields, field: field, valid: valid, value: value, language: language, text: text, optionLabel: optionLabel}
