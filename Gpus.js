.import "History.js" as History

// Pure GPU model: ordering, bar selection, warnings and per-GPU history.
function rank(gpu) {
  return gpu.type === "integrated" ? 0 : gpu.type === "discrete" ? 1 : 2
}

function order(gpus) {
  var list = Array.isArray(gpus) ? gpus : []
  return list.filter(function(gpu) {
    return gpu && typeof gpu.id === "string" && gpu.id !== ""
  }).sort(function(a, b) {
    var delta = rank(a) - rank(b)
    if (delta !== 0) return delta
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
  })
}

function reading(gpu) {
  if (gpu.asleep) return 0
  return typeof gpu.busy === "number" && isFinite(gpu.busy) ? gpu.busy : -1
}

function barGpu(ordered, pinned) {
  var list = Array.isArray(ordered) ? ordered : []
  var best = null
  var score = -2
  for (var i = 0; i < list.length; i++) {
    if (pinned !== "" && list[i].id === pinned) return list[i]
    var value = reading(list[i])
    if (value > score) {
      best = list[i]
      score = value
    }
  }
  return best
}

function pinnedMissing(ordered, pinned) {
  if (pinned === "") return false
  var list = Array.isArray(ordered) ? ordered : []
  return !list.some(function(gpu) { return gpu.id === pinned })
}

function vram(gpu) {
  var used = gpu ? gpu.vramUsed : null
  var total = gpu ? gpu.vramTotal : null
  if (typeof used !== "number" || typeof total !== "number" || !isFinite(used) || !isFinite(total) || total <= 0) return NaN
  return 100 * used / total
}

function summarize(gpu, thermal, thresholds) {
  var none = {busyWarning: false, vramWarning: false, hot: false, warning: false}
  if (!gpu || gpu.asleep) return none
  var busy = typeof gpu.busy === "number" && isFinite(gpu.busy) && gpu.busy >= thresholds.gpu
  var memory = vram(gpu) >= thresholds.vram
  var hot = !!(thermal && thermal.hot)
  return {busyWarning: busy, vramWarning: memory, hot: hot, warning: busy || memory || hot}
}

function appendHistory(history, ordered, time) {
  var next = {}
  var present = {}
  var list = Array.isArray(ordered) ? ordered : []
  list.forEach(function(gpu) {
    present[gpu.id] = true
    var points = history && history[gpu.id] ? history[gpu.id] : []
    next[gpu.id] = History.append(points, time, gpu.asleep ? 0 : gpu.busy)
  })
  Object.keys(history || {}).forEach(function(id) {
    if (present[id]) return
    // A vanished GPU is missing telemetry: record a gap, never zero.
    var points = History.append(history[id], time, NaN)
    var seen = points.some(function(point) { return point.value !== null })
    if (seen) next[id] = points
  })
  return next
}

function colorSlot(ids, id) {
  var known = order((Array.isArray(ids) ? ids : []).map(function(value) { return {id: value, type: null} }))
  for (var i = 0; i < known.length; i++) if (known[i].id === id) return i
  return -1
}

if (typeof module !== "undefined") module.exports = {order: order, barGpu: barGpu, pinnedMissing: pinnedMissing, vram: vram, summarize: summarize, appendHistory: appendHistory, colorSlot: colorSlot}
