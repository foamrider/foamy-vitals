function valid(value) {
  return typeof value === "number" && isFinite(value)
}

function summarize(sensors, margin) {
  var selected = null
  var headroom = Infinity
  var readings = Array.isArray(sensors) ? sensors : []
  readings.forEach(function(sensor) {
    if (!sensor || !valid(sensor.temperature)) return
    var known = valid(sensor.critical) && sensor.critical > 0
    var remaining = known ? sensor.critical - sensor.temperature : Infinity
    // Compare each sensor with its own limit; a cooler memory sensor can be
    // closer to its limit than the GPU hotspot. Unknown limits cannot win this comparison.
    if (!selected || remaining < headroom
        || (remaining === Infinity && headroom === Infinity && sensor.temperature > selected.temperature)) {
      selected = sensor
      headroom = remaining
    }
  })
  var temperature = selected ? selected.temperature : NaN
  var critical = selected && headroom !== Infinity ? selected.critical : NaN
  var warning = Math.max(0, critical - margin)
  return {
    temperature: temperature,
    critical: critical,
    warning: warning,
    hot: valid(temperature) && valid(warning) && temperature >= warning,
    label: selected && typeof selected.label === "string" ? selected.label : "",
    meter: valid(temperature) && valid(critical) ? 100 * temperature / critical : NaN
  }
}
