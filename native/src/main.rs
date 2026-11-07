//! `foamy-vitals-helper`: one JSON object of processor, memory, graphics and
//! thermal readings for the Vitals bar widget, from procfs, sysfs, Intel DRM
//! clients and `nvidia-smi`. No telemetry path needs privileges. It replaces
//! `stats.sh` and `intel_stats.py` and prints the same bytes as the
//! multi-GPU `stats.sh` of #2 did.
//!
//! # Arguments
//!
//! - `stats`: everything the popup shows (a "full" sample).
//! - `stats --bar`: what the bar needs (usage, memory, GPUs, temperatures),
//!   without process rankings; used while every popup is closed.
//!
//! Anything else prints `{"ok":false,"code":"INVALID_ARGUMENT","error":…}`
//! and exits 2 (`stats.sh` ignored unknown arguments). A helper bug prints
//! `{"ok":false,"code":"INTERNAL",…}` and exits 101. Otherwise the exit is 0
//! and stdout is exactly one line, the sample:
//!
//! ```text
//! {"mode":"full"|"bar","sampleTimeMs":N,"cpu":N|null,"cores":[N|null,…],
//!  "memUsedKb":N,"memTotalKb":N,"cpuTemp":N|null,"cpuTempCrit":N|null,
//!  "gpus":[<gpu>,…],"processes":[…],"memoryProcesses":[…],
//!  "network":{"interface":S,"rxBytes":N,"txBytes":N}|null}
//! ```
//!
//! One `<gpu>` per PCI display device (class `0x03…`) bound to `amdgpu`,
//! `radeon`, `i915`, `xe`, `nvidia` or `nouveau`, in that driver order and by
//! address within a driver:
//!
//! ```text
//! {"id":"dddd:bb:dd.f","vendor":"amd"|"intel"|"nvidia","driver":S,
//!  "name":S|null,"type":"integrated"|"discrete"|null,"asleep":B,
//!  "busy":N|null,"vramUsed":N|null,"vramTotal":N|null,"memoryPrivate":N|null,
//!  "frequencyMHz":N|null,"thermals":[{"temperature":N,"critical":N|null,
//!  "label":S},…],"error":S}
//! ```
//!
//! A runtime-suspended GPU is `"asleep":true` with nothing read and
//! `"error":""`. AMD/radeon/nouveau readings come from the GPU's own sysfs
//! files (an integrated AMD GPU's memory is VRAM plus GTT); NVIDIA from one
//! `--query-gpu` per sample; Intel from one scan of this user's DRM clients,
//! each GPU's keys after `vramTotal` exactly as `intel_stats.py` printed them
//! with `json.dumps` (floats in Python's `repr`, strings ASCII-escaped). When
//! the Intel collection fails as a whole (past 1.5 s, or where Python
//! crashed) every Intel GPU reads `"error":"Intel GPU readings unavailable.
//! Retrying…"` (the shell's fallback text, not escaped). Names come from
//! pci.ids (the bracketed product name when there is one).
//!
//! Untrusted text (process, directory, interface and GPU names) is written
//! byte for byte without control bytes, quotes and backslashes (`LC_ALL=C`
//! semantics; other bytes, also invalid UTF-8, pass through). Numbers keep
//! the formatting of the awk and Bash code they come from: whole percentages,
//! `pct` with one decimal, file values as written.
//!
//! # Environment
//!
//! `FOAMY_VITALS_PROC` (default `/proc`), `FOAMY_VITALS_SYS` (`/sys`) and
//! `FOAMY_VITALS_PCI_IDS` (`/usr/share/hwdata/pci.ids`) are for tests only;
//! the plugin never sets them. `XDG_RUNTIME_DIR` picks the state directory,
//! `HOME` a working directory not worth naming, `PATH` finds `nvidia-smi`.
//!
//! # State
//!
//! Counters live in `$XDG_RUNTIME_DIR/foamy-vitals-$EUID`, else
//! `/tmp/foamy-vitals-$EUID`: the first that is (or is created as, mode 700) a
//! real directory owned by this user, and outside a private runtime directory
//! also mode 700. With neither, the sample works from memory and every delta
//! starts over. In it, under an exclusive `flock` on `lock` (waited for; a
//! lock that can't be opened is skipped):
//!
//! - `current`: `cpu_gen proc_gen proc_at cache_gen cache_at cache_mode
//!   gpu_gen`; each `*.0`/`*.1` pair is double-buffered: a sample reads one
//!   generation, writes the other and only then records it here.
//! - `cpu.N`: `key total idle` per `/proc/stat` cpu line. Load is a delta
//!   against it, so the first call after boot reports `null`.
//! - `procs.N`: `#TOTAL jiffies` and `pid jiffies` per process (full samples).
//!   A baseline older than 15 s is replaced first, 250 ms before ranking.
//! - `cache.N`: the last sample line; a full sample younger than 2 s (by
//!   `/proc/uptime`) answers any call, a bar sample only bar calls.
//! - `gpus.N`: `boot<TAB>boot_id`, then per GPU `id type at crit retry fails
//!   reading` (tab-separated): its type (kept for the boot), when the reading
//!   was taken (`-1`: not carried), the NVIDIA shutdown limit (`unknown`,
//!   `none`, °C; `-` for others) with its retry time and failures, and the
//!   reading's JSON keys. Another boot's lines are ignored.
//! - `gpu-names`: `boot_id`, then `id<TAB>name` (`-`: pci.ids has none),
//!   kept for the boot.
//! - `intel-<ID>.json` (via `intel-<ID>.tmp` and a rename): each Intel GPU's
//!   baseline, `{"device","boot","time"(CLOCK_MONOTONIC ns),"clients"}`.
//!
//! Files are replaced with `O_TRUNC` and never written through a symlink.
//!
//! # Reads
//!
//! `/proc/stat`, `meminfo`, `net/route` (lowest-metric IPv4 default route),
//! `net/dev`, `uptime`, `[0-9]*/stat` and `<pid>/cwd` (readlink, top
//! processes only), `sys/kernel/random/boot_id`; `/sys/class/hwmon/hwmon*`,
//! `class/thermal/thermal_zone*`, `bus/pci/drivers/<driver>/*:*` and in each
//! GPU's device directory `class` and `power/runtime_status` always,
//! `power/control` only where it decides spacing (a non-integrated GPU in a
//! bar sample, or an Intel dGPU), the type files once per boot, `vendor` and
//! `device` while a name is not cached, and only while the GPU is awake its
//! readings: `drm/card*/card*-*/status` (only to decide spacing), sysfs
//! values and `hwmon/hwmon*/temp*`, or for Intel `drm/card<N>/gt/gt*/` and
//! `drm/card<N>/device/hwmon`. Intel: every `/proc/<pid>/fd/*` link of this
//! user's processes, `class/drm/<node>/device` for a link to a
//! `/dev/dri/{card,renderD}N` node, and the `fdinfo` only of a descriptor
//! whose node belongs to an Intel GPU being read, all within a 1 s budget.
//! pci.ids is read (until every model is found) only for a GPU without a
//! cached name. Files are read up to 1 MiB (`/proc` tables 16 MiB).
//!
//! # Children
//!
//! Only `nvidia-smi`, only for NVIDIA GPUs that are not runtime-suspended:
//! one `--query-gpu=pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu
//! --format=csv,noheader,nounits` for every NVIDIA GPU due a reading (in bar
//! samples, a GPU that may runtime-suspend is due at most every 30 s) and,
//! for those due a limit (until it answers once per boot, backing off 30 s
//! … 32 min), one `-q -x -d TEMPERATURE`. Either names its GPUs with
//! `-i <8-digit-domain bus ids>` only when that narrows it (another NVIDIA GPU
//! is asleep, spaced out or not being probed), so a lone GPU is asked exactly
//! as before. Each runs with the helper's environment plus `LC_ALL=C`, stdin
//! `/dev/null`, stderr dropped, at most 1 MiB of output, a 1.5 s deadline
//! (the process group is killed) and `PR_SET_PDEATHSIG`. The caller wraps
//! the helper in `timeout --kill-after=1s 5s`.
//!
//! # Diagnostics
//!
//! stderr carries `Intel telemetry: <reason>` when an Intel reading fails
//! (the reason's wording is not Python's exception text), and `Intel reader
//! failed: <reason>` where `intel_stats.py` let an exception escape (a sensor
//! file that is not UTF-8). Nothing else.
//!
//! # Deliberate differences from stats.sh and intel_stats.py
//!
//! - Usage errors reply in JSON with exit 2 (stats.sh ignored arguments).
//! - No stderr noise: Bash printed "No such file or directory" for an
//!   empty `hwmon`/`thermal` class directory, despite its `2>/dev/null`.
//! - Without a safe state directory the sample works from memory instead of
//!   a `mktemp -d` directory (and never exits 1 when that fails).
//! - An unset `HOME` no longer aborts full samples (`set -u` exited 1).
//! - State files are never written through a symlink, and the lock file is
//!   created 0600 and skipped when it is a symlink or someone else's.
//! - `nvidia-smi` gets `SIGKILL` (its whole process group) at 1.5 s instead
//!   of `SIGTERM` and `SIGKILL` 0.2 s later, at most 1 MiB of output, and
//!   stdin `/dev/null`.
//! - The Intel scan runs on a thread beside the other readings, with no
//!   interpreter to start; past 1.5 s from its start the sample uses the
//!   fallback text and the thread ends with the process.
//! - pci.ids is read directly (no awk), and `gpu-names` lists its GPUs by
//!   address instead of in hash order.
//! - Python's `\d` and `isdecimal` took any Unicode digit; only ASCII digits
//!   count now. Integers that don't fit 64 bits in fdinfo, sysfs or
//!   `intel-<ID>.json` are invalid (Python had no limit).
//! - Files are read up to the sizes above; a core number above 65535 in
//!   `/proc/stat` ends the `cores` list. Bash's octal reading of numbers with
//!   a leading zero is kept, except where Bash failed ("value too great for
//!   base", which aborted the sample), which now reads as decimal.

mod cpu;
mod gpus;
mod intel;
mod procs;
mod sensors;
mod state;
mod text;

use std::env;
use std::ffi::OsString;
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use foamy_helper::lock::FileLock;
use foamy_helper::{Failure, json};

use crate::state::{Pointer, Store};

const CACHE_MS: i64 = 2000;
const STALE_MS: i64 = 15_000;
/// The shell's `timeout 1.5s python3 intel_stats.py`, counted from the scan's start.
const INTEL_TIMEOUT: Duration = Duration::from_millis(1500);

#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    Full,
    Bar,
}

impl Mode {
    fn name(self) -> &'static [u8] {
        match self {
            Mode::Full => b"full",
            Mode::Bar => b"bar",
        }
    }
}

fn fail(failure: &Failure) -> ExitCode {
    let _ = json::print_line(&json::error_reply(failure));
    ExitCode::from(failure.exit_code)
}

fn main() -> ExitCode {
    // json::run's panic reply (INTERNAL, exit 101, no core dump); the raw sample line can't use it.
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        previous(info);
        let _ = json::print_line(&json::error_reply(&Failure::coded(
            "INTERNAL",
            "internal error",
        )));
        std::process::exit(101);
    }));
    let args: Vec<OsString> = env::args_os().skip(1).collect();
    let mode = match args.iter().map(|arg| arg.as_bytes()).collect::<Vec<_>>()[..] {
        [b"stats"] => Mode::Full,
        [b"stats", b"--bar"] => Mode::Bar,
        _ => {
            return fail(
                &Failure::coded(
                    "INVALID_ARGUMENT",
                    "usage: foamy-vitals-helper stats [--bar]",
                )
                .with_exit_code(2),
            );
        }
    };
    let line = sample(mode);
    let mut stdout = io::stdout().lock();
    match stdout.write_all(&line).and_then(|()| stdout.flush()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => ExitCode::from(1),
    }
}

fn env_path(name: &str, default: &str) -> PathBuf {
    // ${VAR:-default}
    env::var_os(name)
        .filter(|value| !value.is_empty())
        .map_or_else(|| PathBuf::from(default), PathBuf::from)
}

/// Uptime in milliseconds, as the shell computed it from `/proc/uptime`.
fn uptime_ms(proc: &Path) -> i64 {
    let up = text::read_fields(&proc.join("uptime"), 1)
        .and_then(|f| f.into_iter().next())
        .unwrap_or_default();
    let up = if up.is_empty() { b"0".to_vec() } else { up };
    let dot = up.iter().position(|&b| b == b'.').unwrap_or(up.len());
    let whole = &up[..dot];
    let mut frac = up[dot..].strip_prefix(b".").unwrap_or(&up[dot..]).to_vec();
    frac.extend_from_slice(b"000");
    frac.truncate(3);
    let whole = if text::is_digits(whole) {
        text::bash_int(whole)
    } else {
        0
    };
    let frac = if text::is_digits(&frac) {
        text::decimal(&frac)
    } else {
        0
    };
    whole.wrapping_mul(1000).wrapping_add(frac)
}

fn page_kb() -> f64 {
    let kb = rustix::param::page_size() / 1024;
    if kb == 0 { 4.0 } else { kb as f64 }
}

/// One sample, as the line to print.
fn sample(mode: Mode) -> Vec<u8> {
    let proc = env_path("FOAMY_VITALS_PROC", "/proc");
    let sys = env_path("FOAMY_VITALS_SYS", "/sys");
    let euid = rustix::process::geteuid().as_raw();
    let mut store = match state::choose_dir(env::var_os("XDG_RUNTIME_DIR"), euid) {
        Some(dir) => Store::Dir(dir),
        None => Store::Memory(Default::default()),
    };
    // One bar instance exists per monitor; a second shell or a manual
    // refresh can still overlap. Serialise so nobody reads half-written state.
    let _lock = store
        .dir()
        .and_then(|dir| FileLock::exclusive(&dir.join("lock")).ok());

    let now = uptime_ms(&proc);
    let mut pointer = store
        .read("current")
        .map(|data| Pointer::parse(&data))
        .unwrap_or_default();
    let cache_at = pointer.cache_at();
    let age = now.wrapping_sub(cache_at);
    let serves =
        pointer.cache_mode == b"full" || (pointer.cache_mode == b"bar" && mode == Mode::Bar);
    if cache_at >= 0 && (0..CACHE_MS).contains(&age) && serves {
        let name = format!("cache.{}", pointer.cache_gen);
        if let Some(data) = store.read(&name) {
            let (cached, complete) = text::first_line(&data);
            if complete && cached.first() == Some(&b'{') && cached.last() == Some(&b'}') {
                let mut line = cached;
                line.push(b'\n');
                return line;
            }
        }
    }

    // Every GPU and what this sample does with it first, so the Intel scan
    // can run beside the rest.
    let boot = text::read_value(&proc.join("sys/kernel/random/boot_id"));
    let boot = if boot.is_empty() {
        b"none".to_vec()
    } else {
        boot
    };
    let saved = store.read(&format!("gpus.{}", pointer.gpu_gen));
    let mut gpus = gpus::plan(&sys, mode == Mode::Bar, now, &boot, saved.as_deref());
    let requests = gpus::intel_requests(&gpus);
    let intel_job = (!requests.is_empty()).then(|| {
        let (send, receive) = mpsc::channel();
        let paths = (
            proc.clone(),
            sys.clone(),
            store.dir().map(Path::to_path_buf),
        );
        let uid = rustix::process::getuid().as_raw();
        thread::spawn(move || {
            let paths = intel::Paths {
                proc: &paths.0,
                sys: &paths.1,
                state: paths.2,
            };
            let _ = send.send(intel::collect(&paths, &requests, uid, intel::SCAN_BUDGET));
        });
        (receive, Instant::now() + INTEL_TIMEOUT)
    });

    // Processor, memory and network.
    let cpu_name = format!("cpu.{}", pointer.cpu_gen);
    let next_cpu = 1 - pointer.cpu_gen;
    let previous = store.read(&cpu_name);
    let counters = cpu::sample(&proc, previous.as_deref());
    let next_name = format!("cpu.{next_cpu}");
    if let Some(baseline) = &counters.baseline {
        store.write(&next_name, baseline);
    }
    if store.nonempty(&next_name) {
        pointer.cpu_gen = next_cpu;
    }
    let reading = counters.reading;

    // Process rankings (full samples only).
    let (mut processes, mut memory_processes) = (b"[]".to_vec(), b"[]".to_vec());
    if mode == Mode::Full {
        let page = page_kb();
        let mut next_proc = 1 - pointer.proc_gen;
        let proc_at = pointer.proc_at();
        if now.wrapping_sub(proc_at) > STALE_MS || now < proc_at {
            // The popup just opened after a bar-only stretch: take a fresh
            // baseline and measure a short window, so the first rankings are current.
            let previous = store.read(&format!("procs.{}", pointer.proc_gen));
            let (baseline, _) = procs::rank(&proc, previous.as_deref(), true, page);
            let name = format!("procs.{next_proc}");
            store.write(&name, &baseline);
            if store.nonempty(&name) {
                pointer.proc_gen = next_proc;
                next_proc = 1 - pointer.proc_gen;
            }
            thread::sleep(Duration::from_millis(250));
        }
        let previous = store.read(&format!("procs.{}", pointer.proc_gen));
        let (baseline, ranking) = procs::rank(&proc, previous.as_deref(), false, page);
        let name = format!("procs.{next_proc}");
        store.write(&name, &baseline);
        if store.nonempty(&name) {
            pointer.proc_gen = next_proc;
            pointer.proc_at = now.to_string().into_bytes();
        }
        let home = env::var_os("HOME");
        (processes, memory_processes) =
            procs::lists(&proc, &ranking, home.as_ref().map(|home| home.as_bytes()));
    }

    let temps = sensors::temperatures(&sys);
    gpus::read_sysfs(&mut gpus, now);
    gpus::read_nvidia(&mut gpus, now);
    if let Some((job, deadline)) = intel_job {
        let wait = deadline.saturating_duration_since(Instant::now());
        let readings = match job.recv_timeout(wait) {
            Ok((outcome, diagnostics)) => {
                for diagnostic in diagnostics {
                    let _ = writeln!(io::stderr(), "{diagnostic}");
                }
                match outcome {
                    Ok(readings) => Some(readings),
                    Err(intel::Crash(reason)) => {
                        let _ = writeln!(io::stderr(), "Intel reader failed: {reason}");
                        None
                    }
                }
            }
            Err(_) => None,
        };
        gpus::apply_intel(&mut gpus, readings, now);
    }
    let pci_ids = env_path("FOAMY_VITALS_PCI_IDS", "/usr/share/hwdata/pci.ids");
    let names = gpus::names(&gpus, &mut store, &boot, &pci_ids);
    let (gpu_json, gpu_state) = gpus::render(&gpus, &names, &boot);
    let next_gpu = 1 - pointer.gpu_gen;
    if store.write(&format!("gpus.{next_gpu}"), &gpu_state) {
        pointer.gpu_gen = next_gpu;
    }

    let mut json = b"{\"mode\":\"".to_vec();
    json.extend_from_slice(mode.name());
    json.extend_from_slice(format!("\",\"sampleTimeMs\":{now},\"cpu\":").as_bytes());
    for (value, key) in [
        (&reading.cpu[..], &b",\"cores\":["[..]),
        (&reading.cores, b"],\"memUsedKb\":"),
        (
            reading.mem_used_kb.to_string().as_bytes(),
            b",\"memTotalKb\":",
        ),
        (&reading.mem_total_kb, b",\"cpuTemp\":"),
        (&temps.cpu_temp, b",\"cpuTempCrit\":"),
        (&temps.cpu_temp_crit, b",\"gpus\":["),
        (&gpu_json, b"],\"processes\":"),
        (&processes, b",\"memoryProcesses\":"),
        (&memory_processes, b",\"network\":"),
        (&reading.network, b"}\n"),
    ] {
        json.extend_from_slice(value);
        json.extend_from_slice(key);
    }

    let next_cache = 1 - pointer.cache_gen;
    if store.write(&format!("cache.{next_cache}"), &json) {
        pointer.cache_gen = next_cache;
        pointer.cache_at = now.to_string().into_bytes();
        pointer.cache_mode = mode.name().to_vec();
    }
    store.write("current", &pointer.render());
    json
}
