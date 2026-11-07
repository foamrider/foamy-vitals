//! Every GPU: a PCI display device (class `0x03xxxx`) bound to `amdgpu`,
//! `radeon`, `i915`, `xe`, `nvidia` or `nouveau`, read from its own device
//! directory, so a pair of cards never mixes one card's load with the other's
//! temperatures. Ported from the multi-GPU `stats.sh` (#2).
//!
//! Never wake a GPU. One that runtime power management has suspended is listed
//! as asleep, and none of its counters, sensors, connectors, DRM clients or
//! `nvidia-smi` entries is touched. While every popup is closed, a GPU that
//! may runtime-suspend is read at most every 30 s, so polling alone never
//! keeps it awake. Integrated GPUs keep the bar cadence (they suspend between
//! frames anyway), and so does an AMD or Intel GPU driving a connected
//! display (it never suspends). NVIDIA stays spaced whatever it drives, since
//! each reading costs an `nvidia-smi` run.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use crate::intel;
use crate::sensors::{self, degrees, glob, integer, pci_key, with_suffix};
use crate::state::Store;
use crate::text::{self, read_value};

/// Bar samples read a spaced-out GPU at most this often.
const SPACING_MS: i64 = 30_000;
/// Discovery order: drivers, then addresses within each.
const DRIVERS: [&str; 6] = ["amdgpu", "radeon", "i915", "xe", "nvidia", "nouveau"];
pub const NVIDIA_ERROR: &str = "NVIDIA readings unavailable. Check nvidia-smi.";

/// `^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$`.
pub fn is_pci_address(id: &[u8]) -> bool {
    let hex = |b: &u8| matches!(b, b'0'..=b'9' | b'a'..=b'f');
    id.len() == 12
        && id[..4].iter().all(hex)
        && id[4] == b':'
        && id[5..7].iter().all(hex)
        && id[7] == b':'
        && id[8..10].iter().all(hex)
        && id[10] == b'.'
        && (b'0'..=b'7').contains(&id[11])
}

fn is_tab(b: u8) -> bool {
    b == b'\t'
}

/// `^[0-9]{min,max}$`.
fn digits_within(value: &[u8], min: usize, max: usize) -> bool {
    text::is_digits(value) && (min..=max).contains(&value.len())
}

/// What a sample does with a GPU once its state is known.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Task {
    /// Asleep or carried: the reading is already set.
    Done,
    /// AMD, radeon or nouveau: read its sysfs files.
    Sysfs,
    /// In the one `nvidia-smi` query of the sample.
    Nvidia,
    /// In the one Intel scan of the sample; `spaced` widens its baseline window.
    Intel { spaced: bool },
}

/// One GPU of this sample: what it is, and the reading and state kept with it.
pub struct Gpu {
    pub id: Vec<u8>,
    dev: PathBuf,
    pub driver: &'static str,
    /// `integrated`, `discrete` or `null` (unknown); fixed for the boot.
    kind: Vec<u8>,
    asleep: bool,
    pub task: Task,
    /// `"busy":…,"vramUsed":…,"vramTotal":…,"memoryPrivate":…,"frequencyMHz":…,"thermals":[…],"error":"…"`.
    fragment: Vec<u8>,
    /// When the reading was taken (`-1`: nothing to carry); the NVIDIA
    /// shutdown limit (`unknown`, `none`, °C, `-` for other GPUs), its next
    /// retry and its failures so far. Kept as text, as the shell wrote them.
    at: Vec<u8>,
    crit: Vec<u8>,
    retry: Vec<u8>,
    fails: Vec<u8>,
}

/// A GPU as the previous sample left it.
struct Saved {
    kind: Vec<u8>,
    at: Vec<u8>,
    crit: Vec<u8>,
    retry: Vec<u8>,
    fails: Vec<u8>,
    fragment: Vec<u8>,
}

/// The complete lines of a state file (`while read`: a last line without a
/// newline is never read), NUL bytes dropped.
fn complete_lines(data: &[u8]) -> Vec<Vec<u8>> {
    let mut lines: Vec<Vec<u8>> = data
        .split(|&b| b == b'\n')
        .map(|line| line.iter().copied().filter(|&b| b != 0).collect())
        .collect();
    lines.pop();
    lines
}

/// `gpus.N`: after a `boot<TAB>id` line, `id type at crit retry fails
/// fragment` per GPU, tab-separated. Another boot's state is ignored, and so
/// is any line that does not validate.
fn parse_saved(data: &[u8], boot: &[u8]) -> HashMap<Vec<u8>, Saved> {
    let mut saved = HashMap::new();
    let lines = complete_lines(data);
    let mut lines = lines.iter();
    if lines.next().map(Vec::as_slice) != Some(&[b"boot\t", boot].concat()[..]) {
        return saved;
    }
    for line in lines {
        let f = text::split_ifs(line, is_tab, 7);
        let (id, kind, at, crit, retry, fails, fragment) =
            (f[0], f[1], f[2], f[3], f[4], f[5], f[6]);
        let valid = is_pci_address(id)
            && matches!(kind, b"integrated" | b"discrete" | b"null")
            && (at == b"-1" || digits_within(at, 1, 18))
            && (matches!(crit, b"unknown" | b"none" | b"-") || digits_within(crit, 1, 6))
            && digits_within(retry, 1, 18)
            && matches!(fails, [b'0'..=b'7'])
            && fragment_shaped(fragment);
        if valid {
            saved.insert(
                id.to_vec(),
                Saved {
                    kind: kind.to_vec(),
                    at: at.to_vec(),
                    crit: crit.to_vec(),
                    retry: retry.to_vec(),
                    fails: fails.to_vec(),
                    fragment: fragment.to_vec(),
                },
            );
        }
    }
    saved
}

/// `'"busy":'*',"error":"'*'"'`.
fn fragment_shaped(fragment: &[u8]) -> bool {
    const HEAD: &[u8] = b"\"busy\":";
    const ERROR: &[u8] = b",\"error\":\"";
    fragment.len() > HEAD.len() + ERROR.len()
        && fragment.starts_with(HEAD)
        && fragment.ends_with(b"\"")
        && fragment[HEAD.len()..fragment.len() - 1]
            .windows(ERROR.len())
            .any(|window| window == ERROR)
}

/// A reading with nothing read, and its error (empty for a GPU asleep).
fn blank(error: &[u8]) -> Vec<u8> {
    let mut out = b"\"busy\":null,\"vramUsed\":null,\"vramTotal\":null,\"memoryPrivate\":null,\"frequencyMHz\":null,\"thermals\":[],\"error\":\"".to_vec();
    out.extend_from_slice(error);
    out.push(b'"');
    out
}

/// Bound GPUs in discovery order: `(id, device dir, driver)`. Devices are
/// named by PCI address in any domain; the driver's control files (bind,
/// new_id, uevent…) have no colon.
fn discover(sys: &Path) -> Vec<(Vec<u8>, PathBuf, &'static str)> {
    let mut found: Vec<(Vec<u8>, PathBuf, &'static str)> = Vec::new();
    let mut seen = HashSet::new();
    for driver in DRIVERS {
        let dir = sys.join("bus/pci/drivers").join(driver);
        for dev in glob(&dir, b"", b"") {
            let name = dev.file_name().map_or(&b""[..], |name| {
                std::os::unix::ffi::OsStrExt::as_bytes(name)
            });
            if !name.contains(&b':') || fs::metadata(&dev).is_err() {
                continue;
            }
            let id = name.to_ascii_lowercase();
            if !is_pci_address(&id) || seen.contains(&id) {
                continue;
            }
            if !read_value(&dev.join("class")).starts_with(b"0x03") {
                continue;
            }
            seen.insert(id.clone());
            found.push((id, dev, driver));
        }
    }
    found
}

/// True for a sysfs used/total pair worth showing.
fn pair(used: &[u8], total: &[u8]) -> bool {
    digits_within(used, 1, 18)
        && digits_within(total, 1, 18)
        && text::decimal(total) > 0
        && text::decimal(used) <= text::decimal(total)
}

/// `integrated`, `discrete` or `null` (unknown).
fn gpu_type(dev: &Path, driver: &str, id: &[u8]) -> &'static [u8] {
    match driver {
        "amdgpu" | "radeon" => {
            // Only dedicated VRAM has a memory vendor; APU carve-outs do not.
            // Older chips (Tahiti) report none, but a dGPU without resizable
            // BAR shows the CPU only a window of its VRAM, while a carve-out
            // is wholly visible.
            if fs::metadata(dev.join("mem_info_vram_vendor")).is_ok() {
                return b"discrete";
            }
            let visible = integer(&dev.join("mem_info_vis_vram_total"));
            let total = integer(&dev.join("mem_info_vram_total"));
            if pair(&visible, &total)
                && text::decimal(&visible) > 0
                && text::decimal(&visible) < text::decimal(&total)
            {
                b"discrete"
            } else if driver == "amdgpu" {
                b"integrated"
            } else {
                b"null"
            }
        }
        // Intel's iGPU is device 2 on bus 0, in whatever PCI domain.
        "i915" | "xe" if id.ends_with(b":00:02.0") => b"integrated",
        _ => b"discrete",
    }
}

/// A connector of the GPU reports a connected display. The status file is
/// the connector state DRM keeps; reading it probes nothing.
fn drives_display(dev: &Path) -> bool {
    glob(&dev.join("drm"), b"card", b"").iter().any(|card| {
        glob(card, b"card", b"").iter().any(|connector| {
            connector.file_name().is_some_and(|name| {
                std::os::unix::ffi::OsStrExt::as_bytes(name)[4..].contains(&b'-')
            }) && read_value(&connector.join("status")) == b"connected"
        })
    })
}

/// This sample's GPUs, each with what to do with it: the asleep and the
/// carried ones are done already.
pub fn plan(sys: &Path, bar: bool, now: i64, boot: &[u8], saved: Option<&[u8]>) -> Vec<Gpu> {
    let saved = saved
        .map(|data| parse_saved(data, boot))
        .unwrap_or_default();
    let mut gpus = Vec::new();
    for (id, dev, driver) in discover(sys) {
        let status = read_value(&dev.join("power/runtime_status"));
        let was = saved.get(&id);
        let kind = was.map_or_else(|| gpu_type(&dev, driver, &id).to_vec(), |s| s.kind.clone());
        let integrated = kind == b"integrated";
        let intel = matches!(driver, "i915" | "xe");
        // power/control only matters for a GPU that could be spaced out.
        let control = if !integrated && (bar || intel) {
            read_value(&dev.join("power/control"))
        } else {
            Vec::new()
        };
        let at = was.map_or_else(|| b"-1".to_vec(), |s| s.at.clone());
        let mut crit = was.map_or_else(|| b"-".to_vec(), |s| s.crit.clone());
        if driver == "nvidia" && crit == b"-" {
            crit = b"unknown".to_vec();
        }
        let mut gpu = Gpu {
            retry: was.map_or_else(|| b"0".to_vec(), |s| s.retry.clone()),
            fails: was.map_or_else(|| b"0".to_vec(), |s| s.fails.clone()),
            id,
            dev,
            driver,
            kind,
            asleep: false,
            task: Task::Done,
            fragment: Vec::new(),
            at: at.clone(),
            crit,
        };
        let auto = control == b"auto";
        let at = text::bash_int(&at);
        if status == b"suspended" {
            // Asleep is idle. The last reading is dropped, so a GPU that wakes
            // is read right away instead of showing a stale value.
            gpu.asleep = true;
            gpu.fragment = blank(b"");
            gpu.at = b"-1".to_vec();
        } else if bar
            && auto
            && !integrated
            && at >= 0
            && now.wrapping_sub(at) >= 0
            && now.wrapping_sub(at) < SPACING_MS
            && (driver == "nvidia" || !drives_display(&gpu.dev))
        {
            gpu.fragment = was.map(|s| s.fragment.clone()).unwrap_or_default();
        } else {
            gpu.task = match driver {
                "nvidia" => Task::Nvidia,
                "i915" | "xe" => Task::Intel {
                    // Spaced-out reads keep a baseline window that spans the gap.
                    spaced: auto && !integrated,
                },
                _ => Task::Sysfs,
            };
        }
        gpus.push(gpu);
    }
    gpus
}

/// The reading of an amdgpu, radeon or nouveau GPU from its own sysfs files.
/// Each value is read on its own: one unreadable file costs only that value
/// or sensor, never the whole GPU.
fn sysfs_reading(dev: &Path, kind: &[u8]) -> Vec<u8> {
    let busy = integer(&dev.join("gpu_busy_percent"));
    let busy = if digits_within(&busy, 1, 3) && text::decimal(&busy) <= 100 {
        text::decimal(&busy).to_string().into_bytes()
    } else {
        b"null".to_vec()
    };
    let used = integer(&dev.join("mem_info_vram_used"));
    let total = integer(&dev.join("mem_info_vram_total"));
    let (used, total) = if pair(&used, &total) {
        let (mut used, mut total) = (text::decimal(&used), text::decimal(&total));
        // An APU's VRAM is a small firmware carve-out that fills first and
        // then spills into GTT (system RAM), so only their sum reflects GPU memory.
        if kind == b"integrated" {
            let gtt_used = integer(&dev.join("mem_info_gtt_used"));
            let gtt_total = integer(&dev.join("mem_info_gtt_total"));
            if pair(&gtt_used, &gtt_total) {
                used = used.wrapping_add(text::decimal(&gtt_used));
                total = total.wrapping_add(text::decimal(&gtt_total));
            }
        }
        (
            used.to_string().into_bytes(),
            total.to_string().into_bytes(),
        )
    } else {
        (b"null".to_vec(), b"null".to_vec())
    };
    // "$dev"/hwmon/hwmon*/temp*_input, as one sorted list.
    let mut inputs: Vec<PathBuf> = glob(&dev.join("hwmon"), b"hwmon", b"")
        .iter()
        .flat_map(|hwmon| glob(hwmon, b"temp", b"_input"))
        .collect();
    inputs.sort_unstable_by(|a, b| {
        a.as_os_str()
            .as_encoded_bytes()
            .cmp(b.as_os_str().as_encoded_bytes())
    });
    let mut thermals = Vec::new();
    for input in inputs {
        let temperature = degrees(&input);
        if temperature == b"null" {
            continue;
        }
        // Stable display labels; arbitrary driver strings stay out of JSON.
        let file = input
            .file_name()
            .map_or(&b""[..], std::os::unix::ffi::OsStrExt::as_bytes);
        let number = file.strip_prefix(b"temp").unwrap_or(file);
        let number = number.strip_suffix(b"_input").unwrap_or(number);
        if !text::is_digits(number) {
            continue;
        }
        let label: Vec<u8> = match &read_value(&with_suffix(&input, "_input", "_label"))[..] {
            b"edge" => b"Edge".to_vec(),
            b"junction" => b"Hotspot".to_vec(),
            b"mem" => b"Memory".to_vec(),
            _ => [&b"Sensor "[..], number].concat(),
        };
        let critical = degrees(&with_suffix(&input, "_input", "_crit"));
        let critical = if critical != b"null" && text::bash_int(&critical) > 0 {
            critical
        } else {
            b"null".to_vec()
        };
        if !thermals.is_empty() {
            thermals.push(b',');
        }
        for part in [
            &b"{\"temperature\":"[..],
            &temperature,
            b",\"critical\":",
            &critical,
            b",\"label\":\"",
            &label,
            b"\"}",
        ] {
            thermals.extend_from_slice(part);
        }
    }
    let mut out = Vec::with_capacity(256);
    for part in [
        &b"\"busy\":"[..],
        &busy,
        b",\"vramUsed\":",
        &used,
        b",\"vramTotal\":",
        &total,
        b",\"memoryPrivate\":null,\"frequencyMHz\":null,\"thermals\":[",
        &thermals,
        b"],\"error\":\"\"",
    ] {
        out.extend_from_slice(part);
    }
    out
}

/// Reads every GPU due a sysfs reading.
pub fn read_sysfs(gpus: &mut [Gpu], now: i64) {
    for gpu in gpus.iter_mut().filter(|gpu| gpu.task == Task::Sysfs) {
        gpu.fragment = sysfs_reading(&gpu.dev, &gpu.kind);
        gpu.at = now.to_string().into_bytes();
    }
}

/// nvidia-smi's spelling of a PCI address: an 8-digit domain.
fn nvidia_bus(id: &[u8]) -> Vec<u8> {
    let domain = std::str::from_utf8(&id[..4])
        .ok()
        .and_then(|hex| u32::from_str_radix(hex, 16).ok())
        .unwrap_or(0);
    [format!("{domain:08x}").as_bytes(), &id[4..]].concat()
}

/// `-i a,b,…` when `narrow`, else nothing.
fn only(narrow: bool, ask: &[u8]) -> Vec<String> {
    if narrow {
        vec!["-i".into(), String::from_utf8_lossy(ask).into_owned()]
    } else {
        Vec::new()
    }
}

/// Reads every due NVIDIA GPU with one `--query-gpu` and, for the GPUs
/// without a shutdown limit that are due a retry, one limit probe. Each call
/// names its GPUs with `-i` only when that narrows it, so a lone NVIDIA GPU
/// is asked exactly as before. A suspended GPU is in no call.
pub fn read_nvidia(gpus: &mut [Gpu], now: i64) {
    read_nvidia_with(gpus, now, &mut |args| sensors::nvidia_smi(args));
}

fn read_nvidia_with(
    gpus: &mut [Gpu],
    now: i64,
    smi: &mut dyn FnMut(&[&str]) -> Option<foamy_helper::child::Output>,
) {
    let due: Vec<usize> = (0..gpus.len())
        .filter(|&index| gpus[index].task == Task::Nvidia)
        .collect();
    if due.is_empty() {
        return;
    }
    let bound = gpus.iter().filter(|gpu| gpu.driver == "nvidia").count();
    let mut keys: HashMap<Vec<u8>, usize> = HashMap::new();
    let mut ask = Vec::new();
    for &index in &due {
        keys.insert(pci_key(&gpus[index].id), index);
        if !ask.is_empty() {
            ask.push(b',');
        }
        ask.extend_from_slice(&nvidia_bus(&gpus[index].id));
    }
    let narrow = only(due.len() < bound, &ask);
    let mut args: Vec<&str> = sensors::QUERY.to_vec();
    args.extend(narrow.iter().map(String::as_str));
    let mut rows: HashMap<usize, [Vec<u8>; 4]> = HashMap::new();
    if let Some(output) = smi(&args) {
        for (key, row) in sensors::parse_query(&output.stdout) {
            if let Some(&index) = keys.get(&key) {
                rows.entry(index).or_insert(row);
            }
        }
    }

    // The shutdown threshold is fixed per GPU: once a probe answers (a limit,
    // or "none"), keep it for the boot; retry a failed probe, backing off from
    // 30 s up to 32 min.
    let mut asked: Vec<usize> = Vec::new();
    let mut unknown = 0;
    let mut ask = Vec::new();
    for &index in &due {
        let gpu = &mut gpus[index];
        if text::is_digits(&gpu.crit) || gpu.crit == b"none" {
            continue;
        }
        unknown += 1;
        if !rows.contains_key(&index) || now < text::bash_int(&gpu.retry) {
            continue;
        }
        asked.push(index);
        gpu.crit = b"unknown".to_vec();
        if !ask.is_empty() {
            ask.push(b',');
        }
        ask.extend_from_slice(&nvidia_bus(&gpu.id));
    }
    if !ask.is_empty() {
        let narrow = only(due.len() < bound || asked.len() < unknown, &ask);
        let mut args: Vec<&str> = sensors::LIMIT.to_vec();
        args.extend(narrow.iter().map(String::as_str));
        if let Some(output) = smi(&args).filter(foamy_helper::child::Output::success) {
            for (key, limit) in sensors::parse_limits(&output.stdout) {
                if let Some(&index) = keys.get(&key)
                    && asked.contains(&index)
                {
                    gpus[index].crit = limit;
                }
            }
        }
    }
    for &index in &asked {
        let gpu = &mut gpus[index];
        if gpu.crit == b"unknown" {
            let mut fails = text::bash_int(&gpu.fails);
            if fails < 7 {
                fails += 1;
                gpu.fails = fails.to_string().into_bytes();
            }
            let retry =
                now.wrapping_add(30_000i64.wrapping_mul(1i64.wrapping_shl((fails - 1) as u32)));
            gpu.retry = retry.to_string().into_bytes();
        } else {
            gpu.fails = b"0".to_vec();
            gpu.retry = b"0".to_vec();
        }
    }

    for &index in &due {
        let gpu = &mut gpus[index];
        let Some([util, used, total, temp]) = rows.remove(&index) else {
            gpu.fragment = blank(NVIDIA_ERROR.as_bytes());
            gpu.at = b"-1".to_vec();
            continue;
        };
        // Unsupported readings ("[N/A]", "[Not Supported]") and impossible
        // values stay unknown rather than being shown.
        let busy = if digits_within(&util, 1, 3) && text::decimal(&util) <= 100 {
            text::decimal(&util).to_string().into_bytes()
        } else {
            b"null".to_vec()
        };
        let (mut vram_used, mut vram_total) = (b"null".to_vec(), b"null".to_vec());
        if digits_within(&used, 1, 7) && digits_within(&total, 1, 7) {
            let (used, total) = (text::decimal(&used), text::decimal(&total));
            if total > 0 && used <= total && total < 8_388_608 {
                vram_used = (used * 1_048_576).to_string().into_bytes();
                vram_total = (total * 1_048_576).to_string().into_bytes();
            }
        }
        let crit = if text::is_digits(&gpu.crit) {
            gpu.crit.clone()
        } else {
            b"null".to_vec()
        };
        let mut thermals = Vec::new();
        if digits_within(&temp, 1, 4) {
            thermals.extend_from_slice(b"{\"temperature\":");
            thermals.extend_from_slice(text::decimal(&temp).to_string().as_bytes());
            thermals.extend_from_slice(b",\"critical\":");
            thermals.extend_from_slice(&crit);
            thermals.extend_from_slice(b",\"label\":\"GPU\"}");
        }
        let mut out = Vec::with_capacity(256);
        for part in [
            &b"\"busy\":"[..],
            &busy,
            b",\"vramUsed\":",
            &vram_used,
            b",\"vramTotal\":",
            &vram_total,
            b",\"memoryPrivate\":null,\"frequencyMHz\":null,\"thermals\":[",
            &thermals,
            b"],\"error\":\"\"",
        ] {
            out.extend_from_slice(part);
        }
        gpu.fragment = out;
        gpu.at = now.to_string().into_bytes();
    }
}

/// The GPUs due an Intel reading, in order.
pub fn intel_requests(gpus: &[Gpu]) -> Vec<intel::Request> {
    gpus.iter()
        .filter_map(|gpu| match gpu.task {
            Task::Intel { spaced } => Some(intel::Request {
                device: String::from_utf8_lossy(&gpu.id).into_owned(),
                spaced,
            }),
            _ => None,
        })
        .collect()
}

/// Applies the Intel readings (`None`: the run failed or timed out). Intel
/// reports no VRAM, so the two keys go in where the output order has them. A
/// failed read (the reader's own errors included) is never carried.
pub fn apply_intel(gpus: &mut [Gpu], readings: Option<Vec<Vec<u8>>>, now: i64) {
    let mut readings = readings.map(Vec::into_iter);
    for gpu in gpus
        .iter_mut()
        .filter(|gpu| matches!(gpu.task, Task::Intel { .. }))
    {
        let reading = readings.as_mut().and_then(Iterator::next);
        gpu.fragment = match reading {
            Some(object) => {
                const KEY: &[u8] = b",\"memoryPrivate\":";
                let body = &object[1..object.len() - 1];
                let at = body
                    .windows(KEY.len())
                    .position(|window| window == KEY)
                    .unwrap_or(body.len());
                [
                    &body[..at],
                    b",\"vramUsed\":null,\"vramTotal\":null",
                    &body[at..],
                ]
                .concat()
            }
            None => blank(intel::UNAVAILABLE.as_bytes()),
        };
        gpu.at = if gpu.fragment.ends_with(b"\"error\":\"\"") {
            now.to_string().into_bytes()
        } else {
            b"-1".to_vec()
        };
    }
}

/// Names from pci.ids (hwdata), which never change for a device, so they are
/// kept for the boot in `gpu-names` (a `boot_id` line, then `id<TAB>name`;
/// `-` for a GPU pci.ids does not know, so it is not looked up again). The
/// file is read only when a GPU has no cached name. Returns `id → name`.
pub fn names(
    gpus: &[Gpu],
    store: &mut Store,
    boot: &[u8],
    pci_ids: &Path,
) -> BTreeMap<Vec<u8>, Vec<u8>> {
    let mut names: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
    if let Some(data) = store.read("gpu-names") {
        let lines = complete_lines(&data);
        let mut lines = lines.iter();
        if lines.next().map(Vec::as_slice) == Some(boot) {
            for line in lines {
                let f = text::split_ifs(line, is_tab, 2);
                if !is_pci_address(f[0]) || f[1].is_empty() {
                    continue;
                }
                let name = text::clean(f[1]);
                names.insert(
                    f[0].to_vec(),
                    if name.is_empty() { b"-".to_vec() } else { name },
                );
            }
        }
    }
    let mut missed = false;
    let mut models: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for gpu in gpus {
        if names.get(&gpu.id).is_some_and(|name| !name.is_empty()) {
            continue;
        }
        missed = true;
        names.insert(gpu.id.clone(), b"-".to_vec());
        let hex4 = |value: &[u8]| {
            value.len() == 6
                && value.starts_with(b"0x")
                && value[2..].iter().all(u8::is_ascii_hexdigit)
        };
        let vendor = read_value(&gpu.dev.join("vendor"));
        let device = read_value(&gpu.dev.join("device"));
        if !hex4(&vendor) || !hex4(&device) {
            continue;
        }
        let model = [&vendor[2..], b":", &device[2..]]
            .concat()
            .to_ascii_lowercase();
        models.push((gpu.id.clone(), model));
    }
    if !missed {
        return names;
    }
    if !models.is_empty() {
        let wanted: Vec<&[u8]> = models.iter().map(|(_, model)| &model[..]).collect();
        let found = lookup(pci_ids, &wanted);
        for (id, model) in &models {
            names.insert(
                id.clone(),
                found.get(model).cloned().unwrap_or_else(|| b"-".to_vec()),
            );
        }
    }
    let mut file = boot.to_vec();
    file.push(b'\n');
    for (id, name) in &names {
        file.extend_from_slice(id);
        file.push(b'\t');
        file.extend_from_slice(name);
        file.push(b'\n');
    }
    store.write("gpu-names", &file);
    names
}

/// `vvvv  Vendor` opens a vendor's device list.
fn vendor_line(line: &[u8]) -> Option<&[u8]> {
    let hex = |b: &u8| matches!(b, b'0'..=b'9' | b'a'..=b'f');
    (line.len() >= 6 && line[..4].iter().all(hex) && &line[4..6] == b"  ").then(|| &line[..4])
}

/// `\tdddd  Device name` is one of its devices.
fn device_line(line: &[u8]) -> Option<(&[u8], &[u8])> {
    let hex = |b: &u8| matches!(b, b'0'..=b'9' | b'a'..=b'f');
    (line.len() >= 7 && line[0] == b'\t' && line[1..5].iter().all(hex) && &line[5..7] == b"  ")
        .then(|| (&line[1..5], &line[7..]))
}

/// `match(name, /\[.+\]/)`: the leftmost `[` with a `]` after it (at least
/// one byte between), to the last `]`.
fn bracketed(name: &[u8]) -> &[u8] {
    let Some(close) = name.iter().rposition(|&b| b == b']') else {
        return name;
    };
    match name.iter().position(|&b| b == b'[') {
        Some(open) if open + 1 < close => &name[open + 1..close],
        _ => name,
    }
}

/// The names of `models` (`vvvv:dddd`, lowercase) in pci.ids: "Navi 44
/// [Radeon RX 9050 / 9060 XT]" is the product in brackets. The first entry of
/// a model wins; reading stops once every model is found.
fn lookup(path: &Path, models: &[&[u8]]) -> HashMap<Vec<u8>, Vec<u8>> {
    let mut found = HashMap::new();
    let mut need: HashSet<&[u8]> = models.iter().copied().collect();
    let Ok(file) = File::open(path) else {
        return found;
    };
    let mut reader = BufReader::new(file);
    let mut vendor: Option<Vec<u8>> = None;
    let mut line = Vec::with_capacity(128);
    while !need.is_empty() {
        line.clear();
        match reader.read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        if line.last() == Some(&b'\n') {
            line.pop();
        }
        if let Some(id) = vendor_line(&line) {
            vendor = Some(id.to_vec());
            continue;
        }
        if let Some((device, name)) = device_line(&line) {
            let Some(vendor) = &vendor else {
                continue;
            };
            let model = [&vendor[..], b":", device].concat();
            if !need.remove(&model[..]) {
                continue;
            }
            let name = text::clean(bracketed(name));
            if !name.is_empty() {
                found.insert(model, name);
            }
            continue;
        }
        // Anything else unindented (the "C 03 …" class lists) closes the list.
        if line.first().is_some_and(|&b| b != b'\t' && b != b'#') {
            vendor = None;
        }
    }
    found
}

/// The `gpus` array's contents and the state file to keep (`gpus.N`).
pub fn render(gpus: &[Gpu], names: &BTreeMap<Vec<u8>, Vec<u8>>, boot: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let mut json = Vec::with_capacity(512 * gpus.len());
    let mut state = [&b"boot\t"[..], boot, b"\n"].concat();
    for (index, gpu) in gpus.iter().enumerate() {
        let vendor: &[u8] = match gpu.driver {
            "amdgpu" | "radeon" => b"amd",
            "i915" | "xe" => b"intel",
            _ => b"nvidia",
        };
        let name = match names.get(&gpu.id) {
            Some(name) if name != b"-" => [&b"\""[..], name, b"\""].concat(),
            _ => b"null".to_vec(),
        };
        let kind = if gpu.kind == b"null" {
            b"null".to_vec()
        } else {
            [&b"\""[..], &gpu.kind, b"\""].concat()
        };
        if index > 0 {
            json.push(b',');
        }
        for part in [
            &b"{\"id\":\""[..],
            &gpu.id,
            b"\",\"vendor\":\"",
            vendor,
            b"\",\"driver\":\"",
            gpu.driver.as_bytes(),
            b"\",\"name\":",
            &name,
            b",\"type\":",
            &kind,
            b",\"asleep\":",
            if gpu.asleep { b"true" } else { b"false" },
            b",",
            &gpu.fragment,
            b"}",
        ] {
            json.extend_from_slice(part);
        }
        for (index, part) in [
            &gpu.id,
            &gpu.kind,
            &gpu.at,
            &gpu.crit,
            &gpu.retry,
            &gpu.fails,
            &gpu.fragment,
        ]
        .into_iter()
        .enumerate()
        {
            if index > 0 {
                state.push(b'\t');
            }
            state.extend_from_slice(part);
        }
        state.push(b'\n');
    }
    (json, state)
}
