import QtQuick

Canvas {
  id: chart
  property var series: []
  property real endTime: 0
  property real maximum: 100
  property bool centered: false
  property color gridColor: "#404040"
  property color zeroColor: gridColor

  antialiasing: true
  onSeriesChanged: requestPaint()
  onEndTimeChanged: requestPaint()
  onMaximumChanged: requestPaint()
  onGridColorChanged: requestPaint()
  onCenteredChanged: requestPaint()
  onZeroColorChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()

  onPaint: {
    var ctx = getContext("2d")
    ctx.reset()
    ctx.clearRect(0, 0, width, height)
    if (width <= 0 || height <= 0) return
    var axis = centered ? height / 2 : height - 1
    var span = centered ? height / 2 - 2 : height - 2
    ctx.strokeStyle = gridColor
    ctx.lineWidth = 1
    var levels = centered ? [-1, -0.5, 0, 0.5, 1] : [0, 0.25, 0.5, 0.75, 1]
    for (var g = 0; g < levels.length; g++) {
      ctx.strokeStyle = centered && levels[g] === 0 ? zeroColor : gridColor
      var y = Math.round(axis - levels[g] * span) + 0.5
      ctx.beginPath()
      ctx.moveTo(0, y)
      ctx.lineTo(width, y)
      ctx.stroke()
    }
    for (var s = 0; s < series.length; s++) {
      var data = series[s]
      var points = data.points || []
      var connected = false
      var previousTime = 0
      ctx.beginPath()
      for (var i = 0; i < points.length; i++) {
        var point = points[i]
        // Break the line for missing readings and suspend/resume gaps.
        if (point.value === null || !isFinite(point.value)) { connected = false; continue }
        var x = (point.time - endTime + 120000) / 120000 * width
        if (x < 0) { connected = false; continue }
        // Only the display direction is signed; traffic counters remain positive.
        var direction = centered && data.negative ? 1 : -1
        var valueY = axis + direction * Math.min(1, point.value / Math.max(1, maximum)) * span
        if (connected && point.time - previousTime <= 15000) ctx.lineTo(x, valueY)
        else ctx.moveTo(x, valueY)
        connected = true
        previousTime = point.time
      }
      ctx.strokeStyle = data.color
      ctx.lineWidth = 2
      ctx.setLineDash(data.dashed ? [4, 3] : [])
      ctx.stroke()
    }
  }
}
