// Keep gaps as null values so missing telemetry never becomes a zero reading.
function append(points, time, value) {
  var next = points.filter(function(point) { return point.time > time - 120000 && point.time < time })
  next.push({time: time, value: typeof value === "number" && isFinite(value) && value >= 0 ? value : null})
  return next.slice(-121)
}

function peak(points) {
  return points.reduce(function(result, point) {
    return point.value === null ? result : Math.max(result, point.value)
  }, 0)
}

function networkRate(previous, current, time) {
  var valid = current && typeof current.interface === "string"
    && typeof current.rxBytes === "number" && isFinite(current.rxBytes) && current.rxBytes >= 0
    && typeof current.txBytes === "number" && isFinite(current.txBytes) && current.txBytes >= 0
  var snapshot = valid ? {interface: current.interface, rxBytes: current.rxBytes, txBytes: current.txBytes, time: time} : null
  var changed = !snapshot || !previous || snapshot.interface !== previous.interface
  var result = {snapshot: snapshot, up: NaN, down: NaN, changed: changed}
  // Reset the baseline after route changes, counter resets, or long sample gaps.
  if (changed || time <= previous.time || time - previous.time > 15000) return result
  var seconds = (time - previous.time) / 1000
  if (snapshot.rxBytes >= previous.rxBytes) result.down = (snapshot.rxBytes - previous.rxBytes) / seconds
  if (snapshot.txBytes >= previous.txBytes) result.up = (snapshot.txBytes - previous.txBytes) / seconds
  return result
}
