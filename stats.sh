#!/usr/bin/env bash
# One JSON object of processor, memory, graphics and thermal readings for the
# vitals bar plugin. Read sysfs, Intel DRM clients, and NVIDIA nvidia-smi.
# None of the telemetry paths needs privileges.
#
# Load is a delta against the previous call, so the first call after a reboot
# reports null and every call after it is a true average over the poll gap.
set -uo pipefail

# Counters live in a directory only this user can enter. The fallback root is
# world-writable, and a path another user can predict is a path they can plant a
# symlink at, pointing our truncating writes at a file of their choosing.
state_dir=""
for root in "${XDG_RUNTIME_DIR:-}" /tmp; do
  [[ -n $root && -d $root ]] || continue
  candidate="$root/foamy-vitals-$EUID"
  mkdir -m 700 "$candidate" 2>/dev/null
  [[ -d $candidate && ! -L $candidate ]] || continue
  owner=""
  mode=""
  read -r owner mode < <(stat -c '%u %a' "$candidate" 2>/dev/null)
  [[ $owner == "$EUID" && $mode == *700 ]] || continue
  state_dir="$candidate"
  break
done

# Nowhere safe to keep state, so work from a throwaway: the readings still come
# out, the deltas just start over on every call.
if [[ -z $state_dir ]]; then
  state_dir="$(mktemp -d)" || exit 1
  trap 'rm -rf -- "$state_dir"' EXIT
fi

cpu_state="$state_dir/cpu"
proc_state="$state_dir/procs"
cache="$state_dir/cache"
lock="$state_dir/lock"
cache_seconds=2

# One bar instance exists per monitor, so several copies of this script run at
# the same moment against the same counters. Unserialised they read each other
# half-written state and split one poll gap between them, which shows up as
# malformed output and shares collapsing to nothing. Take the lock, and let a
# fresh sample serve every caller.
exec 9>"$lock" 2>/dev/null && flock 9 2>/dev/null || true

if [[ -s $cache ]]; then
  cached_at="$(stat -c %Y "$cache" 2>/dev/null || echo 0)"
  age=$(($(date +%s) - cached_at))
  if ((age >= 0 && age < cache_seconds)); then
    cat "$cache"
    exit 0
  fi
fi

# First sensor block matching any of the given driver names, tried in order, so
# a caller can name its preferred hardware ahead of the generic fallbacks.
hwmon_named() {
  local want dir
  for want in "$@"; do
    for dir in /sys/class/hwmon/hwmon*; do
      [[ -r "$dir/name" ]] || continue
      if [[ "$(<"$dir/name")" == "$want" ]]; then
        printf '%s' "$dir"
        return 0
      fi
    done
  done
  return 1
}

thermal_zone_named() {
  local want="$1" zone
  for zone in /sys/class/thermal/thermal_zone*; do
    [[ -r "$zone/type" && "$(<"$zone/type")" == "$want" ]] || continue
    printf '%s' "$zone/temp"
    return 0
  done
  return 1
}

# Intel lists one sensor per core beside the whole-chip one and the whole-chip
# one is not always first, so it has to be found by label rather than position.
package_temp_input() {
  local dir="$1" label
  for label in "$dir"/temp*_label; do
    [[ -r $label ]] || continue
    case "$(<"$label")" in
      Package*|Tctl*|Tdie*)
        printf '%s' "${label%_label}_input"
        return 0
        ;;
    esac
  done
  printf '%s' "$dir/temp1_input"
}

degrees() {
  local raw
  raw="$(cat "$1" 2>/dev/null)" || { printf 'null'; return; }
  [[ $raw =~ ^-?[0-9]+$ ]] || { printf 'null'; return; }
  printf '%d' $(((raw + 500) / 1000))
}

number_or_null() {
  local raw
  raw="$(cat "$1" 2>/dev/null)" || { printf 'null'; return; }
  [[ $raw =~ ^[0-9]+$ ]] || { printf 'null'; return; }
  printf '%s' "$raw"
}

# Emits "overall <pct|null>", "cores <csv>" and "total <jiffies>", and rewrites
# the state file with this call's counters.
read_cpu() {
  awk -v prev="$cpu_state" -v out="$cpu_state.$$" '
    BEGIN {
      while ((getline line < prev) > 0) {
        split(line, f, " ")
        seen[f[1]] = 1; ptotal[f[1]] = f[2]; pidle[f[1]] = f[3]
      }
      close(prev)
    }

    /^cpu/ {
      key = $1
      idle = $5 + $6
      total = 0
      for (i = 2; i <= NF; i++) total += $i
      print key, total, idle > out

      pct = -1
      if (key in seen) {
        dt = total - ptotal[key]
        di = idle - pidle[key]
        if (di < 0) di = 0
        if (dt > 0) {
          pct = int((100 * (dt - di) + dt / 2) / dt)
          if (pct < 0) pct = 0
          if (pct > 100) pct = 100
        }
      }

      if (key == "cpu") {
        overall = pct
        absolute = total
      } else {
        n = substr(key, 4) + 0
        core[n] = pct
        if (n + 1 > ncore) ncore = n + 1
      }
    }

    END {
      close(out)
      printf "overall %s\n", (overall >= 0 ? overall : "null")
      csv = ""
      for (i = 0; i < ncore; i++) {
        v = (i in core && core[i] >= 0) ? core[i] : "null"
        csv = csv (i ? "," : "") v
      }
      printf "cores %s\n", csv
      printf "total %d\n", absolute + 0
    }
  ' /proc/stat
}

# Heaviest processes, scaled so 100% is one core fully used, the same convention
# top and btop print. A process absent from the previous sample was created
# inside this window, so all of its time counts; without that a freshly spawned
# hog is invisible exactly when it matters.
#
# The window is measured from the processor counter stored alongside the last
# process sample rather than assumed to be one poll, so a gap of any length
# still divides by the time that actually elapsed.
read_processes() {
  local now_total="$1" ncpu="$2"
  ((ncpu > 0)) || return 0

  printf '%s\n' /proc/[0-9]*/stat |
  awk -v prev="$proc_state" -v out="$proc_state.$$" -v now_total="$now_total" -v ncpu="$ncpu" '
    BEGIN {
      while ((getline line < prev) > 0) {
        split(line, f, " ")
        if (f[1] == "#TOTAL") { prev_total = f[2]; continue }
        seen[f[1]] = 1; pjif[f[1]] = f[2]
        had_state = 1
      }
      close(prev)
      delta = now_total - prev_total
      if (prev_total <= 0 || delta <= 0) had_state = 0
      print "#TOTAL", now_total > out
    }

    # Each file is read here rather than passed as an argument: a process
    # exiting mid-scan is routine, and awk treats an unopenable argument as
    # fatal, which truncated the state file and dropped the whole ranking.
    {
      path = $0
      line = ""
      if ((getline line < path) <= 0) { close(path); next }
      close(path)

      open_at = index(line, "(")
      close_at = 0
      for (i = length(line); i > 0; i--) if (substr(line, i, 1) == ")") { close_at = i; break }
      if (open_at < 2 || close_at <= open_at) next

      pid = substr(line, 1, open_at - 2)
      name = substr(line, open_at + 1, close_at - open_at - 1)
      split(substr(line, close_at + 2), f, " ")

      # Skip kernel threads (migration, kworker, ksoftirqd): the kernel keeping
      # house is not something to put in front of someone asking what is busy.
      if (int(f[7] / 2097152) % 2) next

      jif = f[12] + f[13]
      print pid, jif > out
      if (!had_state) next

      used = (pid in seen) ? jif - pjif[pid] : jif
      if (used <= 0) next
      busy[pid] = used
      label[pid] = name
    }

    END {
      close(out)
      n = 0
      for (pid in busy) { order[++n] = pid }
      for (i = 1; i < n; i++)
        for (j = i + 1; j <= n; j++)
          if (busy[order[j]] > busy[order[i]]) { t = order[i]; order[i] = order[j]; order[j] = t }

      for (i = 1; i <= n && i <= 5; i++) {
        pct = 100 * ncpu * busy[order[i]] / delta
        gsub(/[\\"\t]/, "", label[order[i]])
        printf "%s\t%s\t%.1f\n", order[i], label[order[i]], pct
      }
    }
  '
}

# Heaviest users of resident memory. VmRSS is the portion currently resident in
# RAM, so this ranking does not inflate a process merely because it reserved a
# large virtual address range.
read_memory_processes() {
  printf '%s\n' /proc/[0-9]*/status |
  awk '
    {
      path = $0
      pid = path
      sub("^/proc/", "", pid)
      sub("/status$", "", pid)

      name = ""
      rss = 0
      while ((getline line < path) > 0) {
        if (line ~ /^Name:[[:space:]]*/) {
          name = line
          sub(/^Name:[[:space:]]*/, "", name)
        } else if (line ~ /^VmRSS:[[:space:]]*/) {
          split(line, f, /[[:space:]]+/)
          rss = f[2] + 0
        }
      }
      close(path)

      if (name == "" || rss <= 0) next
      usage[pid] = rss
      label[pid] = name
    }

    END {
      n = 0
      for (pid in usage) order[++n] = pid
      for (i = 1; i < n; i++)
        for (j = i + 1; j <= n; j++)
          if (usage[order[j]] > usage[order[i]]) { t = order[i]; order[i] = order[j]; order[j] = t }

      for (i = 1; i <= n && i <= 5; i++) {
        gsub(/[\\"\t]/, "", label[order[i]])
        printf "%s\t%s\t%d\n", order[i], label[order[i]], usage[order[i]]
      }
    }
  '
}

cpu=null
cores=""
cpu_total=0
while read -r key value; do
  case $key in
    overall) cpu="$value" ;;
    cores) cores="$value" ;;
    total) cpu_total="$value" ;;
  esac
done < <(read_cpu)
mv -f "$cpu_state.$$" "$cpu_state" 2>/dev/null || true

# Never emit a bare key: a truncated reading has to degrade to null, not to
# output the panel cannot parse at all.
[[ $cpu =~ ^(null|[0-9]+)$ ]] || cpu=null
[[ $cores =~ ^(null|[0-9]+)(,(null|[0-9]+))*$ ]] || cores=""

ncpu=0
[[ -n $cores ]] && ncpu="$(awk -F, '{print NF}' <<<"$cores")"

# Several copies of the same program are common (one editor or agent per
# project), so name alone does not say which is which. The working directory
# does, when it is one worth naming.
where_of() {
  local cwd="" base
  cwd="$(readlink "/proc/$1/cwd" 2>/dev/null)" || return 0
  [[ -n $cwd && $cwd != "/" && $cwd != "$HOME" ]] || return 0
  [[ $cwd != /proc/* && $cwd != /sys/* && $cwd != /dev/* ]] || return 0
  base="${cwd##*/}"
  base="${base//[\\\"]/}"
  printf '%s' "$base"
}

ranking="$(read_processes "$cpu_total" "$ncpu")"
mv -f "$proc_state.$$" "$proc_state" 2>/dev/null || true

procs='['
first=1
while IFS=$'\t' read -r pid name pct; do
  [[ -n ${pct:-} ]] || continue
  ((first)) || procs+=','
  first=0
  procs+="{\"name\":\"$name\",\"where\":\"$(where_of "$pid")\",\"pct\":$pct}"
done <<<"$ranking"
procs+=']'

memory_ranking="$(read_memory_processes)"
memory_procs='['
first=1
while IFS=$'\t' read -r pid name rss_kb; do
  [[ -n ${rss_kb:-} ]] || continue
  ((first)) || memory_procs+=','
  first=0
  memory_procs+="{\"name\":\"$name\",\"where\":\"$(where_of "$pid")\",\"rssKb\":$rss_kb}"
done <<<"$memory_ranking"
memory_procs+=']'

read -r mem_total_kb mem_avail_kb < <(
  awk '/^MemTotal:/ { t=$2 } /^MemAvailable:/ { a=$2 } END { print t+0, a+0 }' /proc/meminfo
)
mem_used_kb=$((mem_total_kb - mem_avail_kb))

cpu_temp=null
cpu_temp_crit=null
if dir="$(hwmon_named k10temp zenpower coretemp)"; then
  input="$(package_temp_input "$dir")"
  cpu_temp="$(degrees "$input")"
  cpu_temp_crit="$(degrees "${input%_input}_crit")"
fi
if [[ $cpu_temp == null ]] && zone="$(thermal_zone_named x86_pkg_temp)"; then
  cpu_temp="$(degrees "$zone")"
  # A different sensor's critical limit must not be attached to this fallback.
  cpu_temp_crit=null
fi

gpu_temp=null
gpu_thermals='['
if dir="$(hwmon_named amdgpu radeon i915 xe nouveau)"; then
  gpu_temp="$(degrees "$dir/temp1_input")"
  first=1
  for input in "$dir"/temp*_input; do
    [[ -r $input ]] || continue
    temperature="$(degrees "$input")"
    critical="$(degrees "${input%_input}_crit")"
    # Use stable display labels and keep arbitrary driver strings out of JSON.
    label="Sensor ${input##*/temp}"
    label="${label%_input}"
    case "$(cat "${input%_input}_label" 2>/dev/null)" in
      edge) label=Edge ;;
      junction) label=Hotspot ;;
      mem) label=Memory ;;
    esac
    ((first)) || gpu_thermals+=','
    first=0
    gpu_thermals+="{\"temperature\":$temperature,\"critical\":$critical,\"label\":\"$label\"}"
  done
fi
gpu_thermals+=']'

disk_temp=null
if dir="$(hwmon_named nvme drivetemp)"; then disk_temp="$(degrees "$dir/temp1_input")"; fi

fan=null
if dir="$(hwmon_named thinkpad asus dell_smm apple_smc)"; then
  fan="$(number_or_null "$dir/fan1_input")"
fi
# Every other laptop names its sensor block after its own vendor, so past the
# known names the only thing left to go on is a block that reports a fan at all.
if [[ $fan == null ]]; then
  for reading in /sys/class/hwmon/hwmon*/fan1_input; do
    [[ -r $reading ]] || continue
    fan="$(number_or_null "$reading")"
    [[ $fan == null ]] || break
  done
fi

gpu_busy=null
vram_used=null
vram_total=null
for busy in /sys/class/drm/card*/device/gpu_busy_percent; do
  [[ -r $busy ]] || continue
  gpu_busy="$(number_or_null "$busy")"
  card="$(dirname "$busy")"
  vram_used="$(number_or_null "$card/mem_info_vram_used")"
  vram_total="$(number_or_null "$card/mem_info_vram_total")"
  break
done

# Follow the lowest-metric IPv4 default route, without summing bridge members
# and double-counting their traffic. Missing routes remain unavailable.
network="$(awk '
  FILENAME == "/proc/net/route" {
    if ($2 == "00000000" && $1 != "lo" && (!iface || $7 + 0 < metric)) {
      iface = $1; metric = $7 + 0
    }
    next
  }
  FILENAME == "/proc/net/dev" {
    name = $1; sub(/:$/, "", name)
    if (name == iface) { rx = $2; tx = $10; found = 1 }
    next
  }
  FILENAME == "/proc/uptime" { sampled = $1 * 1000 }
  END {
    printf "\"sampleTimeMs\":%.0f,\"network\":", sampled
    if (found) printf "{\"interface\":\"%s\",\"rxBytes\":%.0f,\"txBytes\":%.0f}", iface, rx, tx
    else printf "null"
  }
' /proc/net/route /proc/net/dev /proc/uptime)"
gpu_error='""'
intel='{}'
if [[ $gpu_busy == null && -d /proc/driver/nvidia/gpus ]]; then
  # The existing two-second cache also bounds NVIDIA polling across bar instances.
  # Prefer the discrete GPU over an Intel fallback and keep all its readings together.
  nvidia="$(python3 "$(dirname "$(readlink -f "$0")")/nvidia_stats.py")"
  if jq -e 'type == "object"' >/dev/null 2>&1 <<<"$nvidia"; then
    gpu_busy=$(jq -c '.gpuBusy' <<<"$nvidia")
    vram_used=$(jq -c '.vramUsed' <<<"$nvidia")
    vram_total=$(jq -c '.vramTotal' <<<"$nvidia")
    gpu_temp=$(jq -c '.gpuTemp' <<<"$nvidia")
    gpu_thermals=$(jq -c '.gpuThermals' <<<"$nvidia")
    gpu_error=$(jq -c '.gpuError' <<<"$nvidia")
  else
    vram_used=null
    vram_total=null
    gpu_temp=null
    gpu_thermals='[]'
    gpu_error='"NVIDIA telemetry helper failed."'
  fi
elif [[ $gpu_busy == null ]]; then
  # Same-user DRM counters need neither perf privileges nor a resident process.
  intel="$(timeout --kill-after=0.2s 1.5s python3 "$(dirname "$(readlink -f "$0")")/intel_stats.py" "$state_dir/intel.json")"
  if jq -e 'type == "object" and (.gpuIntel | type == "boolean")' >/dev/null 2>&1 <<<"$intel"; then
    gpu_busy=$(jq -c '.gpuBusy' <<<"$intel")
    gpu_error=$(jq -c '.gpuError' <<<"$intel")
    if [[ $(jq -r '.gpuIntel' <<<"$intel") == true ]]; then
      gpu_temp=$(jq -c '.gpuTemp' <<<"$intel")
      gpu_thermals=$(jq -c '.gpuThermals' <<<"$intel")
    fi
  else
    intel='{}'
    gpu_error='"Intel GPU readings unavailable. Retrying…"'
  fi
fi

{
  printf '{'
  printf '"cpu":%s,"cores":[%s],' "$cpu" "$cores"
  printf '"memUsedKb":%s,"memTotalKb":%s,' "$mem_used_kb" "$mem_total_kb"
  printf '"cpuTemp":%s,"gpuTemp":%s,"diskTemp":%s,"fanRpm":%s,' "$cpu_temp" "$gpu_temp" "$disk_temp" "$fan"
  printf '"cpuTempCrit":%s,"gpuThermals":%s,' "$cpu_temp_crit" "$gpu_thermals"
  printf '"gpuBusy":%s,"vramUsed":%s,"vramTotal":%s,' "$gpu_busy" "$vram_used" "$vram_total"
  printf '"gpuError":%s,' "$gpu_error"
  printf '"gpuIntel":%s,"gpuMemoryPrivate":%s,"gpuFrequencyMHz":%s,"gpuEngines":%s,' \
    "$(jq -c '.gpuIntel // false' <<<"$intel")" "$(jq -c '.gpuMemoryPrivate' <<<"$intel")" \
    "$(jq -c '.gpuFrequencyMHz' <<<"$intel")" "$(jq -c '.gpuEngines // {}' <<<"$intel")"
  printf '"processes":%s,"memoryProcesses":%s,' "$procs" "$memory_procs"
  printf '%s' "$network"
  printf '}\n'
} >"$cache.$$"

mv -f "$cache.$$" "$cache" 2>/dev/null || true
cat "$cache"
