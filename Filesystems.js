function parse(raw) {
  var entries = []
  String(raw).trim().split("\n").slice(1).forEach(function(line) {
    var match = line.trim().match(/^(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+)$/)
    if (!match || match[1].indexOf("/dev/") !== 0 || Number(match[3]) <= 0) return
    entries.push({device: match[1].replace(/\[.*\]$/, ""), mount: match[7], totalKb: Number(match[3]), usedKb: Number(match[4])})
  })
  // Root first so subvolumes of the same filesystem cannot hide its label.
  entries.sort(function(a, b) { return a.mount === "/" ? -1 : b.mount === "/" ? 1 : a.mount.localeCompare(b.mount) })
  var seen = {}
  return entries.filter(function(entry) {
    if (seen[entry.device]) return false
    seen[entry.device] = true
    return true
  })
}
