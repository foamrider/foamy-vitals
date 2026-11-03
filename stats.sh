#!/usr/bin/env bash
# One JSON object of processor, memory, graphics and thermal readings for the
# vitals bar plugin. Reads procfs, sysfs, Intel DRM clients and nvidia-smi.
# None of the telemetry paths needs privileges. Every GPU gets its own entry
# in "gpus"; which one the bar shows is the panel's choice.
#
#   stats.sh          everything the popup shows
#   stats.sh --bar    what the bar needs (usage, memory, GPU, temperatures),
#                     without process rankings; used while every popup is closed
#
# Load is a delta against the previous call, so the first call after a reboot
# reports null and every call after it is a true average over the poll gap.
#
# Cost matters: this runs every few seconds for as long as the shell runs. It
# uses shell builtins for every file read and keeps the process count to the
# lock, one awk for the system counters, and (full samples only) one awk for
# the process rankings and one find for their working directories. AMD and
# nouveau GPUs are read from sysfs with builtins. Only when such a GPU is due:
# one nvidia-smi query for every NVIDIA GPU, one Intel helper run for every
# Intel GPU, and once per boot one awk over pci.ids for the names. No jq.
set -uo pipefail
# Byte-wise text handling: process names are arbitrary bytes, and [[:cntrl:]]
# must mean the ASCII control characters for the JSON sanitising below.
export LC_ALL=C

mode=full
[[ ${1:-} == --bar ]] && mode=bar

# Overridable for tests only; the plugin never sets these.
proc="${FOAMY_VITALS_PROC:-/proc}"
sys="${FOAMY_VITALS_SYS:-/sys}"
pci_ids="${FOAMY_VITALS_PCI_IDS:-/usr/share/hwdata/pci.ids}"
here="${BASH_SOURCE[0]%/*}"
[[ $here == "${BASH_SOURCE[0]}" ]] && here=.

# Counters live in a directory only this user can enter. The fallback root is
# world-writable, and a path another user can predict is a path they can plant a
# symlink at, pointing our truncating writes at a file of their choosing.
state_dir=""
for root in "${XDG_RUNTIME_DIR:-}" /tmp; do
  [[ -n $root && -d $root ]] || continue
  candidate="$root/foamy-vitals-$EUID"
  [[ -d $candidate ]] || mkdir -m 700 "$candidate" 2>/dev/null
  [[ -d $candidate && ! -L $candidate && -O $candidate ]] || continue
  # Only this user can change the mode of a directory it owns. Inside a
  # private runtime directory nobody else can reach it either, so only the
  # shared fallback needs the mode checked (the one stat call left).
  if [[ $root != "${XDG_RUNTIME_DIR:-}" || ! -O $root || -L $root ]]; then
    perms=""
    read -r perms < <(stat -c '%a' "$candidate" 2>/dev/null)
    [[ $perms == *700 ]] || continue
  fi
  state_dir="$candidate"
  break
done

# Nowhere safe to keep state, so work from a throwaway: the readings still come
# out, the deltas just start over on every call.
if [[ -z $state_dir ]]; then
  state_dir="$(mktemp -d)" || exit 1
  trap 'rm -rf -- "$state_dir"' EXIT
fi

lock="$state_dir/lock"
pointer="$state_dir/current"
cache_ms=2000
# Older than this, a process baseline would average the rankings over the
# whole time the popup was closed; take a fresh one instead.
stale_ms=15000

# One bar instance exists per monitor; the panel polls from one of them, but
# a second shell or a manual refresh can still overlap. Serialise on the
# lock so nobody reads half-written state, and let a fresh sample serve every
# caller. Only the open is silenced: the script's own stderr stays connected.
{ exec 9>"$lock"; } 2>/dev/null && flock 9 2>/dev/null || true

# Uptime in milliseconds: the sample clock, and the age of cached state.
uptime_ms() {
  local up="" whole frac
  read -r up _ <"$proc/uptime" 2>/dev/null || [[ -n $up ]] || up=0
  whole="${up%%.*}"
  frac="${up#"$whole"}"
  frac="${frac#.}000"
  [[ $whole =~ ^[0-9]+$ ]] || whole=0
  frac="${frac:0:3}"
  [[ $frac =~ ^[0-9]{3}$ ]] || frac=000
  printf -v "$1" '%d' $((whole * 1000 + 10#$frac))
}
now=0
uptime_ms now

# State files are double-buffered: each run reads generation g and writes
# 1-g, and only then records the new generations in one short write to the
# pointer. A run killed half way leaves the previous complete state in use,
# never a truncated file (no mv needed, so no extra processes).
cpu_gen=0 proc_gen=0 proc_at=0 cache_gen=0 cache_at=-1 cache_mode=none gpu_gen=0
if [[ -r $pointer ]]; then
  p1="" p2="" p3="" p4="" p5="" p6="" p7=""
  read -r p1 p2 p3 p4 p5 p6 p7 _ <"$pointer" || true
  if [[ $p1 =~ ^[01]$ && $p2 =~ ^[01]$ && $p3 =~ ^[0-9]+$ && $p4 =~ ^[01]$ && $p5 =~ ^-?[0-9]+$ ]]; then
    cpu_gen=$p1 proc_gen=$p2 proc_at=$p3 cache_gen=$p4 cache_at=$p5 cache_mode=${p6:-none}
    # Pointers written before the GPU state existed have no 7th field.
    [[ $p7 =~ ^[01]$ ]] && gpu_gen=$p7
  fi
fi

age=$((now - cache_at))
if ((cache_at >= 0 && age >= 0 && age < cache_ms)) && [[ $cache_mode == full || ($cache_mode == bar && $mode == bar) ]]; then
  cached=""
  if IFS= read -r cached <"$state_dir/cache.$cache_gen" && [[ $cached == \{*\} ]]; then
    printf '%s\n' "$cached"
    exit 0
  fi
fi

# Untrusted text (process names, directory names, interface names) goes into
# JSON strings: drop control characters, quotes and backslashes. A single raw
# control byte made the whole sample unparseable and blanked the widget.
clean() {
  local value="${2//[[:cntrl:]\\\"]/}"
  printf -v "$1" '%s' "$value"
}

# var <- whole degrees from a millidegree file, or null. stderr is redirected
# before the file is opened, so a missing file is quiet too.
degrees() {
  local raw=""
  printf -v "$1" 'null'
  read -r raw 2>/dev/null <"$2" || [[ -n $raw ]] || return 0
  [[ $raw =~ ^-?[0-9]+$ ]] || return 0
  printf -v "$1" '%d' $(((raw + 500) / 1000))
}

# var <- a non-negative integer file, or null.
integer() {
  local raw=""
  printf -v "$1" 'null'
  read -r raw 2>/dev/null <"$2" || [[ -n $raw ]] || return 0
  [[ $raw =~ ^[0-9]+$ ]] || return 0
  printf -v "$1" '%s' "$raw"
}

# --- processor, memory and network: one awk over four small files ------------
next_cpu_gen=$((1 - cpu_gen))
cpu=null cores="" mem_total_kb=0 mem_avail_kb=0 network=null
while read -r key a b c; do
  case $key in
    overall) cpu="$a" ;;
    cores) cores="$a" ;;
    mem) mem_total_kb="$a" mem_avail_kb="$b" ;;
    net)
      [[ -n $a && $b =~ ^[0-9]+$ && $c =~ ^[0-9]+$ ]] &&
        network="{\"interface\":\"$a\",\"rxBytes\":$b,\"txBytes\":$c}"
      ;;
  esac
done < <(awk -v prev="$state_dir/cpu.$cpu_gen" -v out="$state_dir/cpu.$next_cpu_gen" \
  -v statf="$proc/stat" -v memf="$proc/meminfo" -v routef="$proc/net/route" -v devf="$proc/net/dev" '
  BEGIN {
    while ((getline line < prev) > 0) {
      split(line, f, " ")
      seen[f[1]] = 1; ptotal[f[1]] = f[2]; pidle[f[1]] = f[3]
    }
    close(prev)
    overall = -1
  }

  FILENAME == statf && /^cpu/ {
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
    if (key == "cpu") overall = pct
    else {
      n = substr(key, 4) + 0
      core[n] = pct
      if (n + 1 > ncore) ncore = n + 1
    }
    next
  }

  FILENAME == memf {
    if ($1 == "MemTotal:") memtotal = $2
    else if ($1 == "MemAvailable:") memavail = $2
    next
  }

  # Follow the lowest-metric IPv4 default route, without summing bridge
  # members and double-counting their traffic.
  FILENAME == routef {
    if ($2 == "00000000" && $1 != "lo" && (!iface || $7 + 0 < metric)) { iface = $1; metric = $7 + 0 }
    next
  }

  # Large counters can run into the colon ("eth0:123456"), so split there.
  FILENAME == devf {
    at = index($0, ":")
    if (!at || iface == "") next
    name = substr($0, 1, at - 1)
    gsub(/[[:space:]]/, "", name)
    if (name != iface) next
    split(substr($0, at + 1), v, " ")
    rx = v[1]; tx = v[9]; found = 1
  }

  END {
    close(out)
    printf "overall %s\n", (overall >= 0 ? overall : "null")
    csv = ""
    for (i = 0; i < ncore; i++) {
      value = (i in core && core[i] >= 0) ? core[i] : "null"
      csv = csv (i ? "," : "") value
    }
    printf "cores %s\n", csv
    printf "mem %d %d\n", memtotal, memavail
    if (found) {
      gsub(/[[:cntrl:][:space:]\\"]/, "", iface)
      printf "net %s %.0f %.0f\n", iface, rx, tx
    }
  }
' "$proc/stat" "$proc/meminfo" "$proc/net/route" "$proc/net/dev" 2>/dev/null)
[[ -s $state_dir/cpu.$next_cpu_gen ]] && cpu_gen=$next_cpu_gen

# Never emit a bare key: a truncated reading has to degrade to null, not to
# output the panel cannot parse at all.
[[ $cpu =~ ^(null|[0-9]+)$ ]] || cpu=null
[[ $cores =~ ^(null|[0-9]+)(,(null|[0-9]+))*$ ]] || cores=""
[[ $mem_total_kb =~ ^[0-9]+$ && $mem_avail_kb =~ ^[0-9]+$ ]] || mem_total_kb=0 mem_avail_kb=0
mem_used_kb=$((mem_total_kb - mem_avail_kb))
((mem_used_kb >= 0)) || mem_used_kb=0

# --- process rankings (full samples only) -------------------------------------
# Heaviest processes by CPU, scaled so 100% is one core fully used (the top
# and btop convention), and by resident memory, from one pass over
# /proc/PID/stat. A process absent from the previous sample was created inside
# this window, so all of its time counts; without that a freshly spawned hog is
# invisible exactly when it matters. The window is measured from the processor
# counter stored with the last process sample, so a gap of any length still
# divides by the time that actually elapsed. Only the top five are kept while
# scanning (no full sort).
#
# Files are read with getline from ARGV inside BEGIN: a process exiting
# mid-scan is routine, and awk treats an unopenable input file as fatal.
rank_processes() { # $1 baseline in, $2 baseline out, $3 1 = baseline only
  awk -v prev="$1" -v out="$2" -v baseline_only="$3" -v statf="$proc/stat" '
    function keep(kind, pid, name, value,    i, n) {
      n = count[kind]
      if (n == 5 && value <= amount[kind, 5]) return
      if (n < 5) n = ++count[kind]
      for (i = n; i > 1 && amount[kind, i - 1] < value; i--) {
        amount[kind, i] = amount[kind, i - 1]; who[kind, i] = who[kind, i - 1]; label[kind, i] = label[kind, i - 1]
      }
      amount[kind, i] = value; who[kind, i] = pid; label[kind, i] = name
    }
    BEGIN {
      # Resident pages are counted in the kernel page size (16 KiB on some ARM).
      page = 4
      while ((getline line < "/proc/self/smaps") > 0)
        if (line ~ /^KernelPageSize:/) { split(line, k, /[[:space:]]+/); page = k[2] + 0; break }
      close("/proc/self/smaps")
      if (page <= 0) page = 4

      while ((getline line < statf) > 0) {
        if (line ~ /^cpu /) { n = split(line, f, " "); for (i = 2; i <= n; i++) now_total += f[i] }
        else if (line ~ /^cpu[0-9]/) ncpu++
      }
      close(statf)

      while ((getline line < prev) > 0) {
        split(line, f, " ")
        if (f[1] == "#TOTAL") { prev_total = f[2]; continue }
        seen[f[1]] = 1; pjif[f[1]] = f[2]
        had_state = 1
      }
      close(prev)
      delta = now_total - prev_total
      if (baseline_only || prev_total <= 0 || delta <= 0 || ncpu <= 0) had_state = 0
      print "#TOTAL", now_total > out

      for (a = 1; a < ARGC; a++) {
        path = ARGV[a]
        line = ""
        if ((getline line < path) <= 0) { close(path); continue }
        close(path)

        open_at = index(line, "(")
        close_at = 0
        for (i = length(line); i > 0; i--) if (substr(line, i, 1) == ")") { close_at = i; break }
        if (open_at < 2 || close_at <= open_at) continue

        pid = substr(line, 1, open_at - 2)
        name = substr(line, open_at + 1, close_at - open_at - 1)
        split(substr(line, close_at + 2), f, " ")

        # Skip kernel threads (migration, kworker, ksoftirqd): the kernel
        # keeping house is not what someone asking what is busy wants to see.
        if (int(f[7] / 2097152) % 2) continue

        jif = f[12] + f[13]
        print pid, jif > out
        # The name is any 15 bytes the process chose. See clean() above.
        gsub(/[[:cntrl:]\\"]/, "", name)
        if (name == "") name = "?"
        rss = f[22] * page
        if (rss > 0) keep("M", pid, name, rss)
        if (!had_state) continue
        used = (pid in seen) ? jif - pjif[pid] : jif
        if (used > 0) keep("P", pid, name, 100 * ncpu * used / delta)
      }
      close(out)
      for (i = 1; i <= count["P"]; i++) printf "P\t%s\t%s\t%.1f\n", who["P", i], label["P", i], amount["P", i]
      for (i = 1; i <= count["M"]; i++) printf "M\t%s\t%s\t%d\n", who["M", i], label["M", i], amount["M", i]
      exit
    }
  ' "$proc"/[0-9]*/stat 2>/dev/null
}

procs='[]'
memory_procs='[]'
if [[ $mode == full ]]; then
  next_proc_gen=$((1 - proc_gen))
  if ((now - proc_at > stale_ms || now < proc_at)); then
    # The popup just opened after a bar-only stretch: take a fresh baseline
    # and measure a short window, so the first rankings are current ones.
    rank_processes "$state_dir/procs.$proc_gen" "$state_dir/procs.$next_proc_gen" 1 >/dev/null
    if [[ -s $state_dir/procs.$next_proc_gen ]]; then
      proc_gen=$next_proc_gen
      next_proc_gen=$((1 - proc_gen))
    fi
    sleep 0.25
  fi
  ranking="$(rank_processes "$state_dir/procs.$proc_gen" "$state_dir/procs.$next_proc_gen" 0)"
  if [[ -s $state_dir/procs.$next_proc_gen ]]; then
    proc_gen=$next_proc_gen
    proc_at=$now
  fi

  # Several copies of the same program are common (one editor or agent per
  # project), so name alone does not say which is which. The working
  # directory does, when it is one worth naming. One find reads every link
  # (readlink only: nothing on the target filesystem is touched).
  declare -A where=()
  links=()
  while IFS=$'\t' read -r kind pid _; do
    [[ $kind == [PM] && $pid =~ ^[0-9]+$ ]] && links+=("$proc/$pid/cwd")
  done <<<"$ranking"
  if ((${#links[@]})); then
    while IFS=$'\t' read -r -d '' link target; do
      pid="${link#"$proc"/}"
      pid="${pid%/cwd}"
      [[ -n $target && $target != / && $target != "$HOME" ]] || continue
      [[ $target != /proc/* && $target != /sys/* && $target != /dev/* ]] || continue
      base=""
      clean base "${target##*/}"
      where[$pid]="$base"
    done < <(find "${links[@]}" -maxdepth 0 -printf '%p\t%l\0' 2>/dev/null)
  fi

  procs='['
  memory_procs='['
  while IFS=$'\t' read -r kind pid name value; do
    [[ $pid =~ ^[0-9]+$ && -n ${value:-} ]] || continue
    clean name "$name"
    entry="{\"name\":\"$name\",\"where\":\"${where[$pid]:-}\""
    if [[ $kind == P && $value =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
      [[ $procs == '[' ]] || procs+=','
      procs+="$entry,\"pct\":$value}"
    elif [[ $kind == M && $value =~ ^[0-9]+$ ]]; then
      [[ $memory_procs == '[' ]] || memory_procs+=','
      memory_procs+="$entry,\"rssKb\":$value}"
    fi
  done <<<"$ranking"
  procs+=']'
  memory_procs+=']'
fi

# --- temperatures ---------------------------------------------------------------
# First sensor block per driver name, in sysfs order.
declare -A hwmon=()
for dir in "$sys"/class/hwmon/hwmon*; do
  name=""
  read -r name <"$dir/name" 2>/dev/null || [[ -n $name ]] || continue
  [[ -n ${hwmon[$name]:-} ]] || hwmon[$name]="$dir"
done
hwmon_named() { # var name… : the first present block, in the order given
  local want
  for want in "${@:2}"; do
    [[ -n ${hwmon[$want]:-} ]] || continue
    printf -v "$1" '%s' "${hwmon[$want]}"
    return 0
  done
  return 1
}

cpu_temp=null
cpu_temp_crit=null
if hwmon_named dir k10temp zenpower coretemp; then
  # Intel lists one sensor per core beside the whole-chip one and the
  # whole-chip one is not always first, so find it by label, not position.
  input="$dir/temp1_input"
  for label_file in "$dir"/temp*_label; do
    label=""
    read -r label <"$label_file" 2>/dev/null || [[ -n $label ]] || continue
    case $label in Package* | Tctl* | Tdie*) input="${label_file%_label}_input"; break ;; esac
  done
  degrees cpu_temp "$input"
  degrees cpu_temp_crit "${input%_input}_crit"
fi
if [[ $cpu_temp == null ]]; then
  for zone in "$sys"/class/thermal/thermal_zone*; do
    type=""
    read -r type <"$zone/type" 2>/dev/null || [[ -n $type ]] || continue
    [[ $type == x86_pkg_temp ]] || continue
    degrees cpu_temp "$zone/temp"
    # A different sensor's critical limit must not be attached to this fallback.
    cpu_temp_crit=null
    break
  done
fi

# --- graphics -------------------------------------------------------------------
# A GPU is a PCI display device (class 0x03xxxx) bound to a GPU driver. Each
# one is read from its own device directory, so a pair of cards never mixes
# one card's load with the other's temperatures.
#
# Never wake a GPU. One that runtime power management has suspended (hybrid
# laptops, idle dGPUs) is listed as asleep, and none of its counters, sensors,
# DRM clients or nvidia-smi entries is touched. While every popup is closed, a
# GPU that may runtime-suspend is read at most every 30 s, so polling alone
# never keeps it awake. Integrated GPUs keep the bar cadence: they suspend
# between frames anyway, and spacing them would slow a lone iGPU's bar. So
# does a GPU driving a connected display: it never suspends (amdgpu keeps a
# desktop dGPU that drives a monitor awake), so spacing would only go stale.
gpu_spacing_ms=30000
pci_address='^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$'

# True for a sysfs used/total pair worth showing.
gpu_pair() {
  [[ $1 =~ ^[0-9]{1,18}$ && $2 =~ ^[0-9]{1,18}$ ]] && ((10#$2 > 0 && 10#$1 <= 10#$2))
}

# var <- integrated, discrete or null (unknown) for GPU id at dev.
gpu_type() { # var dev driver id
  local visible total
  case $3 in
    amdgpu | radeon)
      # Only dedicated VRAM has a memory vendor; APU carve-outs do not. Older
      # chips (Tahiti) report none, but a dGPU without resizable BAR shows the
      # CPU only a window of its VRAM, while a carve-out is wholly visible.
      if [[ -e $2/mem_info_vram_vendor ]]; then
        printf -v "$1" discrete
        return 0
      fi
      integer visible "$2/mem_info_vis_vram_total"
      integer total "$2/mem_info_vram_total"
      if gpu_pair "$visible" "$total" && ((10#$visible > 0 && 10#$visible < 10#$total)); then
        printf -v "$1" discrete
      elif [[ $3 == amdgpu ]]; then
        printf -v "$1" integrated
      else
        printf -v "$1" null
      fi
      ;;
    # Intel's iGPU is device 2 on bus 0, in whatever PCI domain.
    i915 | xe) if [[ ${4#*:} == 00:02.0 ]]; then printf -v "$1" integrated; else printf -v "$1" discrete; fi ;;
    *) printf -v "$1" discrete ;;
  esac
}

# True when a connector of the GPU at dev reports a connected display. The
# status file is the connector state DRM keeps; reading it probes nothing.
gpu_display() { # dev
  local file state
  for file in "$1"/drm/card*/card*-*/status; do
    state=""
    read -r state 2>/dev/null <"$file" || true
    [[ $state == connected ]] && return 0
  done
  return 1
}

# var <- a reading with nothing read, and its error ("" for a GPU asleep).
gpu_blank() {
  printf -v "$1" '"busy":null,"vramUsed":null,"vramTotal":null,"memoryPrivate":null,"frequencyMHz":null,"thermals":[],"error":"%s"' "$2"
}

# var <- the reading of an amdgpu, radeon or nouveau GPU from its own sysfs
# files. Each value is read on its own: one unreadable file (EIO, EINVAL)
# costs only that value or sensor, never the whole GPU.
sysfs_reading() { # var dev type
  local busy used total gtt_used gtt_total thermals="" input temperature critical label driver_label
  integer busy "$2/gpu_busy_percent"
  [[ $busy =~ ^[0-9]{1,3}$ ]] && ((10#$busy <= 100)) && busy=$((10#$busy)) || busy=null
  integer used "$2/mem_info_vram_used"
  integer total "$2/mem_info_vram_total"
  if gpu_pair "$used" "$total"; then
    used=$((10#$used)) total=$((10#$total))
    # An APU's VRAM is a small firmware carve-out that fills first and then
    # spills into GTT (system RAM), so only their sum reflects GPU memory.
    if [[ $3 == integrated ]]; then
      integer gtt_used "$2/mem_info_gtt_used"
      integer gtt_total "$2/mem_info_gtt_total"
      if gpu_pair "$gtt_used" "$gtt_total"; then
        used=$((used + 10#$gtt_used)) total=$((total + 10#$gtt_total))
      fi
    fi
  else
    used=null total=null
  fi
  for input in "$2"/hwmon/hwmon*/temp*_input; do
    degrees temperature "$input"
    [[ $temperature != null ]] || continue
    # Stable display labels; arbitrary driver strings stay out of JSON.
    label="${input##*/temp}"
    label="Sensor ${label%_input}"
    [[ $label =~ ^Sensor\ [0-9]+$ ]] || continue
    driver_label=""
    read -r driver_label 2>/dev/null <"${input%_input}_label" || true
    case $driver_label in
      edge) label=Edge ;;
      junction) label=Hotspot ;;
      mem) label=Memory ;;
    esac
    degrees critical "${input%_input}_crit"
    [[ $critical != null ]] && ((critical > 0)) || critical=null
    thermals+="${thermals:+,}{\"temperature\":$temperature,\"critical\":$critical,\"label\":\"$label\"}"
  done
  printf -v "$1" '"busy":%s,"vramUsed":%s,"vramTotal":%s,"memoryPrivate":null,"frequencyMHz":null,"thermals":[%s],"error":""' \
    "$busy" "$used" "$total" "$thermals"
}

# var <- a PCI address as "domain:bus:device.function" with the domain as a
# number and the rest lowercase, so sysfs ("0000:01:00.0") and nvidia-smi
# ("00000000:01:00.0") spellings compare equal and domains never collide.
pci_key() {
  local raw="${2//[[:space:]]/}" domain rest
  domain="${raw%%:*}"
  rest="${raw#*:}"
  if [[ $raw == *:*:* && $domain =~ ^[0-9A-Fa-f]{1,8}$ ]]; then
    printf -v "$1" '%x:%s' "$((16#$domain))" "${rest,,}"
  else
    printf -v "$1" 'invalid:%s' "${raw,,}"
  fi
}

# var <- one nvidia-smi CSV value as digits, or "na" ("[N/A]", "[Not
# Supported]", anything else), so the space-separated cache stays aligned.
nv_field() {
  local value="${2#"${2%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [[ $value =~ ^[0-9]+$ ]] || value=na
  printf -v "$1" '%s' "$value"
}

boot_id=""
read -r boot_id 2>/dev/null <"$proc/sys/kernel/random/boot_id" || true
boot_id="${boot_id:-none}"

# Bound devices are named by PCI address, in any domain ("0001:03:00.0");
# the driver's control files (bind, new_id, uevent…) have no colon.
gpu_ids=()
declare -A gpu_dev=() gpu_driver=()
for driver in amdgpu radeon i915 xe nvidia nouveau; do
  for dev in "$sys/bus/pci/drivers/$driver"/*:*; do
    [[ -e $dev ]] || continue
    id="${dev##*/}"
    id="${id,,}"
    [[ $id =~ $pci_address && -z ${gpu_driver[$id]:-} ]] || continue
    class=""
    read -r class 2>/dev/null <"$dev/class" || true
    [[ $class == 0x03* ]] || continue
    gpu_ids+=("$id")
    gpu_dev[$id]="$dev"
    gpu_driver[$id]="$driver"
  done
done

# The previous sample's readings, double-buffered like the counters. After a
# "boot<TAB>id" line, one line per GPU:
#   id  type  readAt (-1: nothing to carry)  limit (unknown|none|°C|-)  retryAt  failures  reading
# The type is fixed for the boot, so it is worked out once. Another boot's
# state is ignored. A line cut short by a killed run has no newline, and read
# never returns it.
declare -A gpu_type_was=() gpu_at=() gpu_crit=() gpu_retry=() gpu_fails=() gpu_frag=()
{
  if IFS= read -r line && [[ $line == "boot"$'\t'"$boot_id" ]]; then
    while IFS=$'\t' read -r id type at crit retry fails fragment; do
      [[ $id =~ $pci_address && $type =~ ^(integrated|discrete|null)$ && $at =~ ^(-1|[0-9]{1,18})$ &&
        $crit =~ ^(unknown|none|[0-9]{1,6}|-)$ && $retry =~ ^[0-9]{1,18}$ && $fails =~ ^[0-7]$ &&
        $fragment == '"busy":'*',"error":"'*'"' ]] || continue
      gpu_type_was[$id]=$type
      gpu_at[$id]=$at gpu_crit[$id]=$crit gpu_retry[$id]=$retry gpu_fails[$id]=$fails gpu_frag[$id]=$fragment
    done
  fi
} 2>/dev/null <"$state_dir/gpus.$gpu_gen"

# This sample, by id: the reading and the state to keep with it.
declare -A gpu_kind=() gpu_asleep=() out_frag=() out_at=() out_crit=() out_retry=() out_fails=()
nv_due=() intel_due=() intel_spaced=()
for id in "${gpu_ids[@]}"; do
  dev="${gpu_dev[$id]}"
  driver="${gpu_driver[$id]}"
  status="" control=""
  read -r status 2>/dev/null <"$dev/power/runtime_status" || true
  type=${gpu_type_was[$id]:-}
  [[ -n $type ]] || gpu_type type "$dev" "$driver" "$id"
  gpu_kind[$id]=$type
  # power/control only matters for a GPU that could be spaced out: one that
  # is not integrated, in a bar sample or (Intel) for its baseline window.
  [[ $type != integrated && ($mode == bar || $driver == i915 || $driver == xe) ]] &&
    read -r control 2>/dev/null <"$dev/power/control"
  gpu_asleep[$id]=false
  at=${gpu_at[$id]:--1}
  out_crit[$id]=${gpu_crit[$id]:--} out_retry[$id]=${gpu_retry[$id]:-0} out_fails[$id]=${gpu_fails[$id]:-0}
  [[ $driver == nvidia && ${out_crit[$id]} == - ]] && out_crit[$id]=unknown
  if [[ $status == suspended ]]; then
    # Asleep is idle. The last reading is dropped, so a GPU that wakes is
    # read right away instead of showing a stale value.
    gpu_asleep[$id]=true
    gpu_blank fragment ""
    out_frag[$id]=$fragment out_at[$id]=-1
  elif [[ $mode == bar && $control == auto && $type != integrated ]] && ((at >= 0 && now - at >= 0 && now - at < gpu_spacing_ms)) &&
    ! gpu_display "$dev"; then
    out_frag[$id]=${gpu_frag[$id]} out_at[$id]=$at
  else
    case $driver in
      nvidia) nv_due+=("$id") ;;
      i915 | xe)
        intel_due+=("$id")
        # Spaced-out reads keep a baseline window long enough to span the gap.
        [[ $control == auto && $type != integrated ]] && intel_spaced+=(--spaced "$id")
        ;;
      *)
        sysfs_reading fragment "$dev" "$type"
        out_frag[$id]=$fragment out_at[$id]=$now
        ;;
    esac
  fi
done

if ((${#nv_due[@]})); then
  # One query for every due NVIDIA GPU, named in nvidia-smi's 8-digit-domain
  # spelling; rows are matched back by pci_key.
  declare -A nv_id=() nv_row=() nv_asked=()
  nv_ask=""
  for id in "${nv_due[@]}"; do
    pci_key key "$id"
    nv_id[$key]=$id
    printf -v bus '%08x:%s' "$((16#${id%%:*}))" "${id#*:}"
    nv_ask+="${nv_ask:+,}$bus"
  done
  # CSV fields are split on commas only: "[Not Supported]" holds a space,
  # and splitting on spaces too would shift every later column.
  while IFS=',' read -r bus util used total temp _; do
    pci_key key "$bus"
    id="${nv_id[$key]:-}"
    [[ -n $id && -z ${nv_row[$id]:-} ]] || continue
    nv_field util "$util"
    nv_field used "$used"
    nv_field total "$total"
    nv_field temp "$temp"
    nv_row[$id]="$util $used $total $temp"
  done < <(timeout --kill-after=0.2s 1.5s nvidia-smi --query-gpu=pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu \
    --format=csv,noheader,nounits -i "$nv_ask" 2>/dev/null)

  # The shutdown threshold is fixed per GPU: once a probe answers (a limit,
  # or "none" when the GPU reports none), keep it for the boot. A failed probe
  # (timeout, error, the GPU missing from the reply) is not an answer: retry
  # it, backing off from 30 s up to 32 min. One probe asks every GPU that
  # answered the query, has no answer yet and is due a retry.
  nv_ask=""
  for id in "${nv_due[@]}"; do
    retry=${out_retry[$id]}
    [[ -n ${nv_row[$id]:-} && ! ${out_crit[$id]} =~ ^([0-9]+|none)$ ]] && ((now >= retry)) || continue
    nv_asked[$id]=1
    out_crit[$id]=unknown
    printf -v bus '%08x:%s' "$((16#${id%%:*}))" "${id#*:}"
    nv_ask+="${nv_ask:+,}$bus"
  done
  if [[ -n $nv_ask ]] && xml="$(timeout --kill-after=0.2s 1.5s nvidia-smi -q -x -d TEMPERATURE -i "$nv_ask" 2>/dev/null)"; then
    # Walk the <gpu id="…"> blocks and read the limit of each GPU asked.
    while [[ $xml == *'<gpu id="'* ]]; do
      xml="${xml#*'<gpu id="'}"
      pci_key key "${xml%%\"*}"
      id="${nv_id[$key]:-}"
      [[ -n $id && -n ${nv_asked[$id]:-} ]] || continue
      block="${xml%%'</gpu>'*}"
      out_crit[$id]=none
      if [[ $block =~ \<gpu_temp_max_threshold\>[[:space:]]*([0-9]{1,6})[[:space:]]*C[[:space:]]*\< ]] && ((10#${BASH_REMATCH[1]} > 0)); then
        out_crit[$id]=$((10#${BASH_REMATCH[1]}))
      fi
    done
  fi
  for id in "${!nv_asked[@]}"; do
    if [[ ${out_crit[$id]} == unknown ]]; then
      fails=${out_fails[$id]}
      ((fails < 7)) && fails=$((fails + 1))
      out_fails[$id]=$fails out_retry[$id]=$((now + 30000 * (1 << (fails - 1))))
    else
      out_fails[$id]=0 out_retry[$id]=0
    fi
  done

  for id in "${nv_due[@]}"; do
    if [[ -z ${nv_row[$id]:-} ]]; then
      gpu_error="NVIDIA readings unavailable. Check nvidia-smi."
      gpu_blank fragment "$gpu_error"
      out_frag[$id]=$fragment out_at[$id]=-1
      continue
    fi
    read -r util used total temp <<<"${nv_row[$id]}"
    # Unsupported readings ("[N/A]", "[Not Supported]") and impossible
    # values stay unknown rather than being shown.
    busy=null vram_used=null vram_total=null thermals=""
    [[ $util =~ ^[0-9]{1,3}$ ]] && ((10#$util <= 100)) && busy=$((10#$util))
    if [[ $used =~ ^[0-9]{1,7}$ && $total =~ ^[0-9]{1,7}$ ]] && ((10#$total > 0 && 10#$used <= 10#$total && 10#$total < 8388608)); then
      vram_used=$((10#$used * 1048576)) vram_total=$((10#$total * 1048576))
    fi
    crit=${out_crit[$id]}
    [[ $crit =~ ^[0-9]+$ ]] || crit=null
    [[ $temp =~ ^[0-9]{1,4}$ ]] && thermals="{\"temperature\":$((10#$temp)),\"critical\":$crit,\"label\":\"GPU\"}"
    printf -v fragment '"busy":%s,"vramUsed":%s,"vramTotal":%s,"memoryPrivate":null,"frequencyMHz":null,"thermals":[%s],"error":""' \
      "$busy" "$vram_used" "$vram_total" "$thermals"
    out_frag[$id]=$fragment out_at[$id]=$now
  done
fi

if ((${#intel_due[@]})); then
  # Same-user DRM counters need neither perf privileges nor a resident
  # process. One helper run samples every due Intel GPU and prints one
  # "ID<TAB>{json.dumps object}" line for each. Lines count only from a clean
  # exit, for a GPU that was asked, and are spliced in whole.
  declare -A intel_asked=() intel_frag=()
  for id in "${intel_due[@]}"; do intel_asked[$id]=1; done
  intel_out="$(timeout --kill-after=0.2s 1.5s python3 "$here/intel_stats.py" --proc "$proc" --sys "$sys" \
    "${intel_spaced[@]}" "$state_dir" "${intel_due[@]}")"
  intel_status=$?
  intel_line=$'^([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\\.[0-7])\t\\{"busy":[^\t]*\\}$'
  while ((intel_status == 0)) && IFS= read -r line; do
    [[ $line =~ $intel_line ]] || continue
    id="${BASH_REMATCH[1]}"
    body="${line#*$'\t'}"
    body="${body#\{}"
    body="${body%\}}"
    [[ -n ${intel_asked[$id]:-} && $body == *',"memoryPrivate":'* ]] || continue
    # Intel reports no VRAM: add the two keys where the output order has them.
    intel_frag[$id]="${body%%,\"memoryPrivate\":*},\"vramUsed\":null,\"vramTotal\":null,\"memoryPrivate\":${body#*,\"memoryPrivate\":}"
  done <<<"$intel_out"
  for id in "${intel_due[@]}"; do
    fragment="${intel_frag[$id]:-}"
    if [[ -z $fragment ]]; then
      gpu_error="Intel GPU readings unavailable. Retrying…"
      gpu_blank fragment "$gpu_error"
    fi
    # A failed read (the helper's own errors included) is never carried.
    out_at[$id]=-1
    [[ $fragment == *'"error":""' ]] && out_at[$id]=$now
    out_frag[$id]=$fragment
  done
fi

# Names come from pci.ids (hwdata) and never change for a device, so they are
# kept for the boot. One awk resolves every GPU without a cached name; a GPU
# pci.ids does not know is cached as "-" so it is not looked up again.
declare -A gpu_name=() gpu_model=() model_name=()
{
  if IFS= read -r line && [[ $line == "$boot_id" ]]; then
    while IFS=$'\t' read -r id name; do
      [[ $id =~ $pci_address && -n $name ]] || continue
      clean name "$name"
      gpu_name[$id]=${name:--}
    done
  fi
} 2>/dev/null <"$state_dir/gpu-names"
names_missed=0 models=""
for id in "${gpu_ids[@]}"; do
  [[ -z ${gpu_name[$id]:-} ]] || continue
  names_missed=1
  gpu_name[$id]=-
  vendor="" device=""
  read -r vendor 2>/dev/null <"${gpu_dev[$id]}/vendor" || true
  read -r device 2>/dev/null <"${gpu_dev[$id]}/device" || true
  [[ $vendor =~ ^0x[0-9A-Fa-f]{4}$ && $device =~ ^0x[0-9A-Fa-f]{4}$ ]] || continue
  model="${vendor:2}:${device:2}"
  gpu_model[$id]=${model,,}
  models+=" ${model,,}"
done
if ((names_missed)); then
  if [[ -n $models ]]; then
    while IFS=$'\t' read -r model name; do
      clean name "$name"
      [[ -n $model && -n $name ]] && model_name[$model]=$name
    done < <(awk -v want="$models" '
      BEGIN {
        n = split(want, list, " ")
        for (i = 1; i <= n; i++) if (!(list[i] in need)) { need[list[i]] = 1; left++ }
      }
      # A vendor line opens its device list; anything else unindented
      # (the "C 03 …" class lists at the end) closes it.
      /^[0-9a-f][0-9a-f][0-9a-f][0-9a-f]  / { vendor = substr($0, 1, 4); next }
      /^\t[0-9a-f][0-9a-f][0-9a-f][0-9a-f]  / {
        model = vendor ":" substr($0, 2, 4)
        if (vendor == "" || !(model in need)) next
        # "Navi 44 [Radeon RX 9050 / 9060 XT]": the bracketed part is the product.
        name = substr($0, 8)
        if (match(name, /\[.+\]/)) name = substr(name, RSTART + 1, RLENGTH - 2)
        print model "\t" name
        delete need[model]
        if (--left == 0) exit
        next
      }
      /^[^\t#]/ { vendor = "" }
    ' "$pci_ids" 2>/dev/null)
    for id in "${!gpu_model[@]}"; do
      gpu_name[$id]=${model_name[${gpu_model[$id]}]:--}
    done
  fi
  names="$boot_id"$'\n'
  for id in "${!gpu_name[@]}"; do
    names+="$id"$'\t'"${gpu_name[$id]}"$'\n'
  done
  printf '%s' "$names" 2>/dev/null >"$state_dir/gpu-names"
fi

gpus=""
gpu_state="boot"$'\t'"$boot_id"$'\n'
for id in "${gpu_ids[@]}"; do
  driver="${gpu_driver[$id]}"
  case $driver in
    amdgpu | radeon) vendor=amd ;;
    i915 | xe) vendor=intel ;;
    *) vendor=nvidia ;;
  esac
  name=null
  [[ ${gpu_name[$id]} == - ]] || name="\"${gpu_name[$id]}\""
  type=null
  [[ ${gpu_kind[$id]} == null ]] || type="\"${gpu_kind[$id]}\""
  gpus+="${gpus:+,}{\"id\":\"$id\",\"vendor\":\"$vendor\",\"driver\":\"$driver\",\"name\":$name,\"type\":$type,\"asleep\":${gpu_asleep[$id]},${out_frag[$id]}}"
  gpu_state+="$id"$'\t'"${gpu_kind[$id]}"$'\t'"${out_at[$id]}"$'\t'"${out_crit[$id]}"$'\t'"${out_retry[$id]}"$'\t'"${out_fails[$id]}"$'\t'"${out_frag[$id]}"$'\n'
done
next_gpu_gen=$((1 - gpu_gen))
printf '%s' "$gpu_state" 2>/dev/null >"$state_dir/gpus.$next_gpu_gen" && gpu_gen=$next_gpu_gen

printf -v json '{"mode":"%s","sampleTimeMs":%s,"cpu":%s,"cores":[%s],"memUsedKb":%s,"memTotalKb":%s,"cpuTemp":%s,"cpuTempCrit":%s,"gpus":[%s],"processes":%s,"memoryProcesses":%s,"network":%s}' \
  "$mode" "$now" "$cpu" "$cores" "$mem_used_kb" "$mem_total_kb" "$cpu_temp" "$cpu_temp_crit" "$gpus" "$procs" "$memory_procs" "$network"

next_cache_gen=$((1 - cache_gen))
if printf '%s\n' "$json" >"$state_dir/cache.$next_cache_gen" 2>/dev/null; then
  cache_gen=$next_cache_gen cache_at=$now cache_mode=$mode
fi
printf '%s %s %s %s %s %s %s\n' "$cpu_gen" "$proc_gen" "$proc_at" "$cache_gen" "$cache_at" "$cache_mode" "$gpu_gen" >"$pointer" 2>/dev/null
printf '%s\n' "$json"
