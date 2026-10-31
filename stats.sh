#!/usr/bin/env bash
# One JSON object of processor, memory, graphics and thermal readings for the
# vitals bar plugin. Reads procfs, sysfs, Intel DRM clients and nvidia-smi.
# None of the telemetry paths needs privileges.
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
# the process rankings and one find for their working directories. No jq.
set -uo pipefail
# Byte-wise text handling: process names are arbitrary bytes, and [[:cntrl:]]
# must mean the ASCII control characters for the JSON sanitising below.
export LC_ALL=C

mode=full
[[ ${1:-} == --bar ]] && mode=bar

# Overridable for tests only; the plugin never sets these.
proc="${FOAMY_VITALS_PROC:-/proc}"
sys="${FOAMY_VITALS_SYS:-/sys}"
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
cpu_gen=0 proc_gen=0 proc_at=0 cache_gen=0 cache_at=-1 cache_mode=none
if [[ -r $pointer ]]; then
  p1="" p2="" p3="" p4="" p5="" p6=""
  read -r p1 p2 p3 p4 p5 p6 _ <"$pointer" || true
  if [[ $p1 =~ ^[01]$ && $p2 =~ ^[01]$ && $p3 =~ ^[0-9]+$ && $p4 =~ ^[01]$ && $p5 =~ ^-?[0-9]+$ ]]; then
    cpu_gen=$p1 proc_gen=$p2 proc_at=$p3 cache_gen=$p4 cache_at=$p5 cache_mode=${p6:-none}
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

# var <- whole degrees from a millidegree file, or null.
degrees() {
  local raw=""
  printf -v "$1" 'null'
  read -r raw <"$2" 2>/dev/null || [[ -n $raw ]] || return 0
  [[ $raw =~ ^-?[0-9]+$ ]] || return 0
  printf -v "$1" '%d' $(((raw + 500) / 1000))
}

# var <- a non-negative integer file, or null.
integer() {
  local raw=""
  printf -v "$1" 'null'
  read -r raw <"$2" 2>/dev/null || [[ -n $raw ]] || return 0
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

gpu_temp=null
gpu_thermals='['
if hwmon_named dir amdgpu radeon i915 xe nouveau; then
  degrees gpu_temp "$dir/temp1_input"
  for input in "$dir"/temp*_input; do
    [[ -r $input ]] || continue
    temperature=null critical=null
    degrees temperature "$input"
    degrees critical "${input%_input}_crit"
    # Stable display labels; arbitrary driver strings stay out of JSON.
    label="Sensor ${input##*/temp}"
    label="${label%_input}"
    driver_label=""
    read -r driver_label <"${input%_input}_label" 2>/dev/null || true
    case $driver_label in
      edge) label=Edge ;;
      junction) label=Hotspot ;;
      mem) label=Memory ;;
    esac
    [[ $gpu_thermals == '[' ]] || gpu_thermals+=','
    gpu_thermals+="{\"temperature\":$temperature,\"critical\":$critical,\"label\":\"$label\"}"
  done
fi
gpu_thermals+=']'

# --- graphics -------------------------------------------------------------------
gpu_busy=null
vram_used=null
vram_total=null
for busy in "$sys"/class/drm/card*/device/gpu_busy_percent; do
  [[ -r $busy ]] || continue
  integer gpu_busy "$busy"
  integer vram_used "${busy%/*}/mem_info_vram_used"
  integer vram_total "${busy%/*}/mem_info_vram_total"
  break
done

gpu_error=""
gpu_suspended=false
gpu_extra='"gpuIntel":false,"gpuMemoryPrivate":null,"gpuFrequencyMHz":null,"gpuEngines":{}'
gpu_json=""

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

nvidia_address=""
if [[ $gpu_busy == null ]]; then
  # Glob order is PCI address order: the lowest-addressed NVIDIA GPU.
  for entry in "$proc"/driver/nvidia/gpus/*; do
    [[ -d $entry ]] || continue
    nvidia_address="${entry##*/}"
    break
  done
fi

intel=0
if [[ $gpu_busy == null && -z $nvidia_address ]]; then
  # Bound devices are named by PCI address, in any domain ("0001:03:00.0");
  # the driver's control files (bind, new_id, uevent…) have no colon.
  for bound in "$sys"/bus/pci/drivers/i915/*:* "$sys"/bus/pci/drivers/xe/*:*; do
    [[ -e $bound ]] && intel=1 && break
  done
fi

if [[ -n $nvidia_address ]]; then
  # Keep load, memory and temperature from one GPU. Never wake a GPU that
  # runtime power management has suspended (hybrid laptops): nvidia-smi would
  # resume it and keep it from sleeping. A suspended GPU is idle.
  power="$sys/bus/pci/devices/$nvidia_address/power"
  runtime_status="" runtime_control=""
  read -r runtime_status <"$power/runtime_status" 2>/dev/null || true
  read -r runtime_control <"$power/control" 2>/dev/null || true
  gpu_thermals='[]'
  gpu_temp=null
  if [[ $runtime_status == suspended ]]; then
    gpu_busy=0
    gpu_suspended=true
  else
    nv_file="$state_dir/nvidia"
    nv_at=-1 nv_util="" nv_used="" nv_total="" nv_temp="" nv_crit="" nv_boot="" nv_retry=0 nv_fails=0
    [[ -r $nv_file ]] && read -r nv_at nv_util nv_used nv_total nv_temp nv_crit nv_boot nv_retry nv_fails _ <"$nv_file" 2>/dev/null
    [[ $nv_retry =~ ^[0-9]+$ ]] || nv_retry=0
    [[ $nv_fails =~ ^[0-9]+$ ]] || nv_fails=0
    [[ $nv_at =~ ^[0-9]+$ ]] || nv_at=-1
    boot_id=""
    read -r boot_id <"$proc/sys/kernel/random/boot_id" 2>/dev/null || true
    # While every popup is closed, a GPU that may runtime-suspend is sampled
    # at most every 30 s, so polling alone never keeps it awake.
    if [[ $mode == bar && $runtime_control == auto ]] && ((nv_at >= 0 && now - nv_at >= 0 && now - nv_at < 30000)); then
      :
    else
      nv_util="" nv_used="" nv_total="" nv_temp="" nv_found=0 want="" have=""
      pci_key want "$nvidia_address"
      # CSV fields are split on commas only: "[Not Supported]" holds a space,
      # and splitting on spaces too would shift every later column.
      while IFS=',' read -r bus util used total temp _; do
        pci_key have "$bus"
        [[ $have == "$want" ]] || continue
        nv_field nv_util "$util"
        nv_field nv_used "$used"
        nv_field nv_total "$total"
        nv_field nv_temp "$temp"
        nv_found=1
        break
      done < <(timeout --kill-after=0.2s 1.5s nvidia-smi --query-gpu=pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu \
        --format=csv,noheader,nounits 2>/dev/null)
      if ((!nv_found)); then
        nv_at=-1
      else
        nv_at=$now
        # The shutdown threshold is fixed per GPU: once a probe answers (a
        # limit, or "none" when this GPU reports none), keep it for the boot.
        # A failed probe (timeout, error, this GPU missing from the reply) is
        # not an answer: retry it, backing off from 30 s up to 32 min.
        if [[ $nv_boot != "${boot_id:-none}" ]]; then
          nv_crit=unknown nv_retry=0 nv_fails=0
        fi
        if [[ ! $nv_crit =~ ^([0-9]+|none)$ ]] && ((now >= nv_retry)); then
          nv_crit=unknown
          if xml="$(timeout --kill-after=0.2s 1.5s nvidia-smi -q -x -d TEMPERATURE 2>/dev/null)"; then
            # Walk the <gpu id="…"> blocks and read the limit of this GPU only.
            while [[ $xml == *'<gpu id="'* ]]; do
              xml="${xml#*'<gpu id="'}"
              pci_key have "${xml%%\"*}"
              [[ $have == "$want" ]] || continue
              block="${xml%%'</gpu>'*}"
              nv_crit=none
              if [[ $block =~ \<gpu_temp_max_threshold\>[[:space:]]*([0-9]+)[[:space:]]*C[[:space:]]*\< ]] && ((10#${BASH_REMATCH[1]} > 0)); then
                nv_crit="${BASH_REMATCH[1]}"
              fi
              break
            done
          fi
          if [[ $nv_crit == unknown ]]; then
            ((nv_fails < 7)) && nv_fails=$((nv_fails + 1))
            nv_retry=$((now + 30000 * (1 << (nv_fails - 1))))
          else
            nv_retry=0 nv_fails=0
          fi
        fi
        printf '%s %s %s %s %s %s %s %s %s\n' "$nv_at" "$nv_util" "$nv_used" "$nv_total" "$nv_temp" "${nv_crit:-unknown}" "${boot_id:-none}" "$nv_retry" "$nv_fails" >"$nv_file" 2>/dev/null
      fi
    fi
    if ((nv_at >= 0)); then
      # Unsupported readings ("[N/A]", "[Not Supported]") and impossible
      # values stay unknown rather than being shown.
      gpu_busy=null
      [[ $nv_util =~ ^[0-9]+$ ]] && ((10#$nv_util <= 100)) && gpu_busy=$((10#$nv_util))
      if [[ $nv_used =~ ^[0-9]+$ && $nv_total =~ ^[0-9]+$ ]] && ((10#$nv_total > 0 && 10#$nv_used <= 10#$nv_total && 10#$nv_total < 8388608)); then
        vram_used=$((10#$nv_used * 1048576))
        vram_total=$((10#$nv_total * 1048576))
      fi
      [[ $nv_temp =~ ^[0-9]+$ ]] && gpu_temp=$((10#$nv_temp))
      [[ $nv_crit =~ ^[0-9]+$ ]] || nv_crit=null
      gpu_thermals="[{\"temperature\":$gpu_temp,\"critical\":$nv_crit,\"label\":\"GPU\"}]"
    else
      gpu_busy=null
      gpu_error="NVIDIA readings unavailable. Check nvidia-smi."
    fi
  fi
elif ((intel)); then
  # Same-user DRM counters need neither perf privileges nor a resident process.
  helper="$(timeout --kill-after=0.2s 1.5s python3 "$here/intel_stats.py" --proc "$proc" --drm "$sys/class/drm" "$state_dir/intel.json")"
  # The helper prints one json.dumps() object holding every Intel key, so it
  # is spliced in whole instead of being taken apart field by field.
  if [[ $helper == \{\"gpuIntel\":*\} && $helper != *$'\n'* ]]; then
    helper="${helper#\{}"
    gpu_json="\"vramUsed\":null,\"vramTotal\":null,\"gpuSuspended\":false,${helper%\}}"
  else
    gpu_busy=null
    gpu_error="Intel GPU readings unavailable. Retrying…"
    gpu_extra='"gpuIntel":true,"gpuMemoryPrivate":null,"gpuFrequencyMHz":null,"gpuEngines":{}'
  fi
fi

if [[ -z $gpu_json ]]; then
  gpu_json="\"gpuBusy\":$gpu_busy,\"vramUsed\":$vram_used,\"vramTotal\":$vram_total,\"gpuTemp\":$gpu_temp,"
  gpu_json+="\"gpuThermals\":$gpu_thermals,\"gpuError\":\"$gpu_error\",\"gpuSuspended\":$gpu_suspended,$gpu_extra"
fi

printf -v json '{"mode":"%s","sampleTimeMs":%s,"cpu":%s,"cores":[%s],"memUsedKb":%s,"memTotalKb":%s,"cpuTemp":%s,"cpuTempCrit":%s,%s,"processes":%s,"memoryProcesses":%s,"network":%s}' \
  "$mode" "$now" "$cpu" "$cores" "$mem_used_kb" "$mem_total_kb" "$cpu_temp" "$cpu_temp_crit" "$gpu_json" "$procs" "$memory_procs" "$network"

next_cache_gen=$((1 - cache_gen))
if printf '%s\n' "$json" >"$state_dir/cache.$next_cache_gen" 2>/dev/null; then
  cache_gen=$next_cache_gen cache_at=$now cache_mode=$mode
fi
printf '%s %s %s %s %s %s\n' "$cpu_gen" "$proc_gen" "$proc_at" "$cache_gen" "$cache_at" "$cache_mode" >"$pointer" 2>/dev/null
printf '%s\n' "$json"
