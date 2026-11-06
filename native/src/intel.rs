//! Intel (i915/xe) GPU readings from the DRM fdinfo of this user's
//! processes, without perf privileges: the busiest engine since the last
//! sample, private allocations, the GT frequency and the GPU's own sensors.
//! A port of `intel_stats.py` that prints the same `json.dumps` text.

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, ErrorKind};
use std::mem::MaybeUninit;
use std::os::fd::OwnedFd;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use rustix::fs::{self as rfs, AtFlags, Mode, OFlags};
use rustix::io::Errno;
use serde_json::Value;

use crate::text::{self, SMALL, py_float, py_integer, py_json_str, py_memory, py_strip};

pub const UNAVAILABLE: &str = "Intel GPU readings unavailable. Retrying\u{2026}";
/// How long the process and descriptor scan may take.
pub const SCAN_BUDGET: Duration = Duration::from_secs(1);
/// The getdents buffer (glibc's `readdir` uses 32 KiB too).
const DIR_BUFFER: usize = 32 * 1024;
/// Readings older than this are not a baseline.
const MAX_ELAPSED_NS: i128 = 30_000_000_000;

/// A Python exception: what `collect` catches as `OSError`/`ValueError`.
#[derive(Debug)]
pub struct Failure(pub String);

fn os_error(error: &io::Error, path: &Path) -> Failure {
    let text = error.to_string();
    let text = text.split(" (os error").next().unwrap_or(&text).to_string();
    match error.raw_os_error() {
        Some(code) => Failure(format!("[Errno {code}] {text}: '{}'", path.display())),
        None => Failure(format!("{text}: '{}'", path.display())),
    }
}

/// `FileNotFoundError`, `ProcessLookupError`, `PermissionError`: skipped.
fn skippable(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        ErrorKind::NotFound | ErrorKind::PermissionDenied
    ) || matches!(error.raw_os_error(), Some(code) if code == Errno::SRCH.raw_os_error())
}

fn read_text(path: &Path) -> Result<String, Failure> {
    let data = text::read_file(path, SMALL).map_err(|error| os_error(&error, path))?;
    text::py_text(data).map_err(|error| {
        let at = error.utf8_error().valid_up_to();
        Failure(format!(
            "'utf-8' codec can't decode byte at position {at}: invalid utf-8 in '{}'",
            path.display()
        ))
    })
}

/// An insertion-ordered map, like a Python dict.
#[derive(Debug, Clone, PartialEq)]
pub struct Ordered<V>(pub Vec<(String, V)>);

impl<V> Default for Ordered<V> {
    fn default() -> Self {
        Ordered(Vec::new())
    }
}

impl<V> Ordered<V> {
    fn get(&self, key: &str) -> Option<&V> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    fn set(&mut self, key: &str, value: V) {
        match self.0.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value,
            None => self.0.push((key.to_string(), value)),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Client {
    pub engines: Ordered<u64>,
    pub capacities: Ordered<u64>,
    pub private: Option<u128>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Snapshot {
    pub device: String,
    pub boot: String,
    pub time: u64,
    pub clients: Ordered<Client>,
}

/// One DRM client's fdinfo; `None` when it belongs to another driver,
/// device or has no numeric client id.
pub fn parse_client(text: &str, device: &str) -> Result<Option<(String, Client)>, Failure> {
    let mut fields: Ordered<&str> = Ordered::default();
    for line in text::py_splitlines(text) {
        if let Some((key, value)) = line.split_once(':') {
            fields.set(key, value);
        }
    }
    let field = |key: &str| fields.get(key).copied().unwrap_or("");
    if !matches!(py_strip(field("drm-driver")), "i915" | "xe")
        || py_strip(field("drm-pdev")) != device
    {
        return Ok(None);
    }
    let ident = py_strip(field("drm-client-id"));
    if !text::py_isdecimal(ident) {
        return Ok(None);
    }
    let mut engines = Ordered::default();
    let mut capacities = Ordered::default();
    for (key, value) in &fields.0 {
        if let Some(engine) = key.strip_prefix("drm-engine-capacity-") {
            match py_integer(value, "") {
                Some(count) if count > 0 => capacities.set(engine, count),
                _ => return Err(Failure("Invalid Intel engine capacity".into())),
            }
        } else if let Some(engine) = key.strip_prefix("drm-engine-") {
            match py_integer(value, "ns") {
                Some(count) => engines.set(engine, count),
                None => return Err(Failure("Invalid Intel engine counter".into())),
            }
        }
    }
    // Shared buffers are excluded: fdinfo can't deduplicate them across
    // clients. This is private allocation size, not total/resident VRAM.
    let mut private: u128 = 0;
    let mut regions = 0;
    for (key, value) in &fields.0 {
        let Some(region) = key.strip_prefix("drm-total-") else {
            continue;
        };
        regions += 1;
        let total = py_memory(value);
        let shared = py_memory(field(&format!("drm-shared-{region}")));
        match (total, shared) {
            (Some(total), Some(shared)) if shared <= total => private += total - shared,
            _ => {
                return Ok(Some((
                    ident.to_string(),
                    Client {
                        engines,
                        capacities,
                        private: None,
                    },
                )));
            }
        }
    }
    let private = (regions > 0).then_some(private);
    Ok(Some((
        ident.to_string(),
        Client {
            engines,
            capacities,
            private,
        },
    )))
}

/// Every DRM client of `device` held by this user's processes, scanning at
/// most until `deadline`. Paths are resolved from a descriptor of `proc`
/// (`statat`, `readlinkat`): a desktop session has thousands of descriptors.
pub fn read_clients(
    device: &str,
    proc: &Path,
    uid: u32,
    deadline: Instant,
) -> Result<Ordered<Client>, Failure> {
    let mut clients: Ordered<Client> = Ordered::default();
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC;
    let root = rfs::openat(rfs::CWD, proc, flags, Mode::empty())
        .map_err(|error| os_error(&error.into(), proc))?;
    // getdents with large buffers: /proc and every descriptor table.
    let mut buffer = vec![MaybeUninit::<u8>::uninit(); DIR_BUFFER];
    let mut fd_buffer = vec![MaybeUninit::<u8>::uninit(); DIR_BUFFER];
    let mut entries = rfs::RawDir::new(&root, &mut buffer);
    let mut link = Vec::with_capacity(256);
    while let Some(entry) = entries.next() {
        if Instant::now() > deadline {
            return Err(Failure("Intel process scan exceeded its budget".into()));
        }
        let entry = entry.map_err(|error| os_error(&error.into(), proc))?;
        let name = entry.file_name().to_bytes();
        if !std::str::from_utf8(name).is_ok_and(text::py_isdecimal) {
            continue;
        }
        let process = Process {
            root: &root,
            proc,
            name,
        };
        match process.scan(
            device,
            uid,
            deadline,
            &mut clients,
            &mut link,
            &mut fd_buffer,
        ) {
            Ok(()) | Err(Scan::Skip) => {}
            Err(Scan::Fail(failure)) => return Err(failure),
        }
    }
    Ok(clients)
}

enum Scan {
    Skip,
    Fail(Failure),
}

struct Process<'a> {
    root: &'a OwnedFd,
    proc: &'a Path,
    name: &'a [u8],
}

impl Process<'_> {
    fn path(&self, tail: &[u8]) -> PathBuf {
        let mut path = self.proc.as_os_str().as_bytes().to_vec();
        path.push(b'/');
        path.extend_from_slice(self.name);
        path.extend_from_slice(tail);
        PathBuf::from(std::ffi::OsString::from_vec(path))
    }

    fn relative(&self, tail: &[u8]) -> Vec<u8> {
        [self.name, tail].concat()
    }

    fn error(&self, error: Errno, tail: &[u8]) -> Scan {
        let error = io::Error::from(error);
        if skippable(&error) {
            Scan::Skip
        } else {
            Scan::Fail(os_error(&error, &self.path(tail)))
        }
    }

    fn scan(
        &self,
        device: &str,
        uid: u32,
        deadline: Instant,
        clients: &mut Ordered<Client>,
        link: &mut Vec<u8>,
        buffer: &mut [MaybeUninit<u8>],
    ) -> Result<(), Scan> {
        let stat = rfs::statat(self.root, self.name, AtFlags::empty())
            .map_err(|error| self.error(error, b""))?;
        if stat.st_uid != uid {
            return Ok(());
        }
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC;
        let fd_dir = rfs::openat(self.root, self.relative(b"/fd"), flags, Mode::empty())
            .map_err(|error| self.error(error, b"/fd"))?;
        let mut fds = rfs::RawDir::new(&fd_dir, buffer);
        while let Some(fd) = fds.next() {
            if Instant::now() > deadline {
                return Err(Scan::Fail(Failure(
                    "Intel descriptor scan exceeded its budget".into(),
                )));
            }
            let fd = fd.map_err(|error| self.error(error, b"/fd"))?;
            let number = fd.file_name().to_bytes();
            if number == b"." || number == b".." {
                continue;
            }
            // Only DRM device files have DRM client fdinfo; checking the link
            // first skips the read for every socket, pipe and file.
            let target = match rfs::readlinkat(&fd_dir, number, std::mem::take(link)) {
                Ok(target) => target.into_bytes(),
                Err(error) => match self.error(error, &[b"/fd/", number].concat()) {
                    Scan::Skip => continue,
                    failure => return Err(failure),
                },
            };
            let dri = target.starts_with(b"/dev/dri/");
            *link = target;
            link.clear();
            if !dri {
                continue;
            }
            let tail = [b"/fdinfo/", number].concat();
            let data = match rfs::openat(
                self.root,
                self.relative(&tail),
                OFlags::RDONLY | OFlags::CLOEXEC,
                Mode::empty(),
            ) {
                Ok(file) => {
                    use std::io::Read;
                    let mut data = Vec::with_capacity(1024);
                    match fs::File::from(file).take(SMALL).read_to_end(&mut data) {
                        Ok(_) => data,
                        Err(error) if skippable(&error) => continue,
                        Err(error) => return Err(Scan::Fail(os_error(&error, &self.path(&tail)))),
                    }
                }
                Err(error) => match self.error(error, &tail) {
                    Scan::Skip => continue,
                    failure => return Err(failure),
                },
            };
            let text = text::py_text(data).map_err(|_| {
                Scan::Fail(Failure(format!(
                    "'utf-8' codec can't decode '{}'",
                    self.path(&tail).display()
                )))
            })?;
            // Processes may share a DRM file; count its client only once.
            if let Some((ident, client)) = parse_client(&text, device).map_err(Scan::Fail)?
                && clients.get(&ident).is_none()
            {
                clients.0.push((ident, client));
            }
        }
        Ok(())
    }
}

fn counters(value: Option<&Value>, minimum: u64) -> Option<Ordered<u64>> {
    let object = value?.as_object()?;
    let mut out = Ordered::default();
    for (key, value) in object {
        let value = value
            .as_u64()
            .filter(|&v| v >= minimum && !value.is_f64())?;
        out.0.push((key.clone(), value));
    }
    Some(out)
}

/// The previous baseline; `None` when there is none or it is unusable.
pub fn parse_snapshot(text: &str) -> Option<Snapshot> {
    let raw: Value = serde_json::from_str(text).ok()?;
    let raw = raw.as_object()?;
    let device = raw.get("device")?.as_str()?.to_string();
    let boot = raw.get("boot")?.as_str()?.to_string();
    let time = raw.get("time").filter(|v| v.is_u64())?.as_u64()?;
    let mut clients = Ordered::default();
    for (ident, client) in raw.get("clients")?.as_object()? {
        let client = client.as_object()?;
        let engines = counters(client.get("engines"), 0)?;
        let capacities = counters(client.get("capacities"), 1)?;
        let private = match client.get("private") {
            None | Some(Value::Null) => None,
            Some(value) if value.is_u64() => value.as_u64().map(u128::from),
            Some(_) => return None,
        };
        clients.0.push((
            ident.clone(),
            Client {
                engines,
                capacities,
                private,
            },
        ));
    }
    Some(Snapshot {
        device,
        boot,
        time,
        clients,
    })
}

/// Busy percentage per engine since `previous`; raises the current counters
/// to the previous ones where the kernel's counters regressed (so the next
/// sample cannot count work twice).
pub fn utilization(current: &mut Snapshot, previous: Option<&Snapshot>) -> Ordered<f64> {
    let Some(previous) = previous else {
        return Ordered::default();
    };
    if current.device != previous.device || current.boot != previous.boot {
        return Ordered::default();
    }
    let elapsed = i128::from(current.time) - i128::from(previous.time);
    if !(0 < elapsed && elapsed <= MAX_ELAPSED_NS) {
        return Ordered::default();
    }
    let mut totals: Ordered<u128> = Ordered::default();
    let mut capacities: Ordered<u64> = Ordered::default();
    for (ident, client) in &mut current.clients.0 {
        let Some(old) = previous.clients.get(ident) else {
            continue;
        };
        for (engine, counter) in &mut client.engines.0 {
            let capacity = client.capacities.get(engine).copied().unwrap_or(1);
            let Some(&prior) = old.engines.get(engine) else {
                continue;
            };
            if capacity != old.capacities.get(engine).copied().unwrap_or(1) {
                continue;
            }
            let value = *counter;
            *counter = value.max(prior);
            let total =
                totals.get(engine).copied().unwrap_or(0) + u128::from(value.saturating_sub(prior));
            totals.set(engine, total);
            let most = capacities.get(engine).copied().unwrap_or(1).max(capacity);
            capacities.set(engine, most);
        }
    }
    let elapsed = elapsed as u64;
    Ordered(
        totals
            .0
            .into_iter()
            .map(|(engine, total)| {
                let capacity = capacities.get(&engine).copied().unwrap_or(1);
                let busy = text::py_div(100 * total, elapsed) / capacity as f64;
                let pct = text::py_round1(if busy < 100.0 { busy } else { 100.0 });
                (engine, pct)
            })
            .collect(),
    )
}

fn render_snapshot(snapshot: &Snapshot) -> Vec<u8> {
    let mut out = b"{\"device\":".to_vec();
    py_json_str(&mut out, &snapshot.device);
    out.extend_from_slice(b",\"boot\":");
    py_json_str(&mut out, &snapshot.boot);
    out.extend_from_slice(format!(",\"time\":{},\"clients\":{{", snapshot.time).as_bytes());
    for (index, (ident, client)) in snapshot.clients.0.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        py_json_str(&mut out, ident);
        out.extend_from_slice(b":{\"engines\":");
        render_counters(&mut out, &client.engines);
        out.extend_from_slice(b",\"capacities\":");
        render_counters(&mut out, &client.capacities);
        out.extend_from_slice(b",\"private\":");
        match client.private {
            Some(private) => out.extend_from_slice(private.to_string().as_bytes()),
            None => out.extend_from_slice(b"null"),
        }
        out.push(b'}');
    }
    out.extend_from_slice(b"}}");
    out
}

fn render_counters(out: &mut Vec<u8>, counters: &Ordered<u64>) {
    out.push(b'{');
    for (index, (key, value)) in counters.0.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        py_json_str(out, key);
        out.extend_from_slice(format!(":{value}").as_bytes());
    }
    out.push(b'}');
}

struct Thermal {
    temperature: f64,
    critical: Option<f64>,
    label: String,
}

/// What `collect` returns, in `json.dumps` order.
#[derive(Default)]
struct Stats {
    intel: bool,
    busy: Option<f64>,
    engines: Ordered<f64>,
    private: Option<u128>,
    frequency: Option<u64>,
    temp: Option<f64>,
    thermals: Vec<Thermal>,
    error: &'static str,
}

impl Stats {
    fn render(&self) -> Vec<u8> {
        let float = |value: Option<f64>| value.map_or_else(|| "null".to_string(), py_float);
        let mut out = format!(
            "{{\"gpuIntel\":{},\"gpuBusy\":{},\"gpuEngines\":{{",
            self.intel,
            float(self.busy)
        )
        .into_bytes();
        for (index, (engine, pct)) in self.engines.0.iter().enumerate() {
            if index > 0 {
                out.push(b',');
            }
            py_json_str(&mut out, engine);
            out.push(b':');
            out.extend_from_slice(py_float(*pct).as_bytes());
        }
        out.extend_from_slice(b"},\"gpuMemoryPrivate\":");
        out.extend_from_slice(
            self.private
                .map_or_else(|| "null".to_string(), |v| v.to_string())
                .as_bytes(),
        );
        out.extend_from_slice(b",\"gpuFrequencyMHz\":");
        out.extend_from_slice(
            self.frequency
                .map_or_else(|| "null".to_string(), |v| v.to_string())
                .as_bytes(),
        );
        out.extend_from_slice(
            format!(",\"gpuTemp\":{},\"gpuThermals\":[", float(self.temp)).as_bytes(),
        );
        for (index, thermal) in self.thermals.iter().enumerate() {
            if index > 0 {
                out.push(b',');
            }
            out.extend_from_slice(
                format!(
                    "{{\"temperature\":{},\"critical\":{},\"label\":",
                    py_float(thermal.temperature),
                    float(thermal.critical)
                )
                .as_bytes(),
            );
            py_json_str(&mut out, &thermal.label);
            out.push(b'}');
        }
        out.extend_from_slice(b"],\"gpuError\":");
        py_json_str(&mut out, self.error);
        out.push(b'}');
        out
    }
}

/// `pathlib` glob of `dir/<prefix>*<suffix>` (directory order).
fn entries(dir: &Path, prefix: &str, suffix: &str) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter(|entry| {
            let name = entry.file_name();
            let name = name.as_bytes();
            name.len() >= prefix.len() + suffix.len()
                && name.starts_with(prefix.as_bytes())
                && name.ends_with(suffix.as_bytes())
        })
        .map(|entry| entry.path())
        .collect()
}

fn exists(path: &Path) -> bool {
    fs::metadata(path).is_ok()
}

fn lexists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

/// `value / 1000` with Python's correctly rounded int division.
fn thousandths(value: u64) -> f64 {
    text::py_div(u128::from(value), 1000)
}

pub struct Paths<'a> {
    pub proc: &'a Path,
    pub drm: &'a Path,
    /// The baseline, `intel.json` (none when no state directory is safe).
    pub state: Option<PathBuf>,
}

/// The Intel keys of a sample (one `json.dumps` object) and a diagnostic
/// for stderr.
pub fn collect(paths: &Paths, uid: u32, budget: Duration) -> (Vec<u8>, Option<String>) {
    let mut stats = Stats::default();
    match collect_into(paths, uid, budget, &mut stats) {
        Ok(()) => (stats.render(), None),
        Err(Failure(message)) => {
            let failed = Stats {
                intel: stats.intel,
                error: UNAVAILABLE,
                ..Stats::default()
            };
            (failed.render(), Some(format!("Intel telemetry: {message}")))
        }
    }
}

fn collect_into(
    paths: &Paths,
    uid: u32,
    budget: Duration,
    stats: &mut Stats,
) -> Result<(), Failure> {
    let mut devices: BTreeMap<String, PathBuf> = BTreeMap::new();
    for card in entries(paths.drm, "card", "") {
        let name = card.file_name().map_or(&b""[..], |name| name.as_bytes());
        if !name.get(4).is_some_and(u8::is_ascii_digit) || name.contains(&b'-') {
            continue;
        }
        let driver = card.join("device/driver");
        if !exists(&driver) {
            continue;
        }
        let Ok(driver) = fs::canonicalize(&driver) else {
            continue;
        };
        if !matches!(
            driver.file_name().and_then(|n| n.to_str()),
            Some("i915" | "xe")
        ) {
            continue;
        }
        let Ok(device) = fs::canonicalize(card.join("device")) else {
            continue;
        };
        let device = device
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        devices.insert(device, card);
    }
    let Some((device, card)) = devices.into_iter().next() else {
        return Ok(());
    };
    stats.intel = true;
    let deadline = Instant::now() + budget;
    let clients = read_clients(&device, paths.proc, uid, deadline)?;
    let boot = py_strip(&read_text(&paths.proc.join("sys/kernel/random/boot_id"))?).to_string();
    let time = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    let time = (time.tv_sec as u64)
        .wrapping_mul(1_000_000_000)
        .wrapping_add(time.tv_nsec as u64);
    let mut current = Snapshot {
        device,
        boot,
        time,
        clients,
    };
    let previous = match &paths.state {
        Some(state) => match text::read_file(state, SMALL) {
            Ok(data) => {
                let text = text::py_text(data).map_err(|_| {
                    Failure(format!("'utf-8' codec can't decode '{}'", state.display()))
                })?;
                parse_snapshot(&text)
            }
            Err(error) if error.kind() == ErrorKind::NotFound => None,
            Err(error) => return Err(os_error(&error, state)),
        },
        None => None,
    };
    let engines = utilization(&mut current, previous.as_ref());
    if let Some(state) = &paths.state {
        // Renamed into place so an interrupted sample never leaves half a baseline.
        let temporary = state.with_extension("tmp");
        fs::write(&temporary, render_snapshot(&current))
            .map_err(|error| os_error(&error, &temporary))?;
        fs::rename(&temporary, state).map_err(|error| os_error(&error, state))?;
    }
    let mut frequencies = Vec::new();
    for gt in entries(&card.join("gt"), "gt", "") {
        let path = gt.join("rps_act_freq_mhz");
        if !gt.is_dir() || !lexists(&path) {
            continue;
        }
        if let Some(value) = py_integer(&read_text(&path)?, "") {
            frequencies.push(value);
        }
    }
    // Only sensors attached to this GPU, never a CPU/package temperature or
    // another graphics device's sensor.
    let mut thermals = Vec::new();
    for hwmon in entries(&card.join("device/hwmon"), "hwmon", "") {
        if !hwmon.is_dir() {
            continue;
        }
        for input in entries(&hwmon, "temp", "_input") {
            let Some(value) = py_integer(&read_text(&input)?, "") else {
                continue;
            };
            let name = input
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let prefix = name.strip_suffix("_input").unwrap_or(&name);
            let limit = hwmon.join(format!("{prefix}_crit"));
            let critical = if exists(&limit) {
                py_integer(&read_text(&limit)?, "")
            } else {
                None
            };
            let label = hwmon.join(format!("{prefix}_label"));
            let label = if exists(&label) {
                py_strip(&read_text(&label)?).to_string()
            } else {
                "GPU".to_string()
            };
            thermals.push(Thermal {
                temperature: thousandths(value),
                critical: critical.filter(|&c| c != 0).map(thousandths),
                label,
            });
        }
    }
    let allocations: Vec<Option<u128>> = current
        .clients
        .0
        .iter()
        .map(|(_, client)| client.private)
        .collect();
    stats.busy = engines.0.iter().map(|(_, pct)| *pct).reduce(f64::max);
    stats.engines = engines;
    stats.private = if !allocations.is_empty() && allocations.iter().all(Option::is_some) {
        Some(allocations.iter().flatten().sum())
    } else {
        None
    };
    stats.frequency = frequencies.into_iter().max();
    stats.temp = thermals.iter().map(|t| t.temperature).reduce(f64::max);
    stats.thermals = thermals;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEVICE: &str = "0000:00:02.0";

    fn fdinfo(ident: u32, render: u64, video: u64) -> String {
        format!(
            "drm-driver: i915\ndrm-pdev: {DEVICE}\ndrm-client-id: {ident}\ndrm-engine-render: {render} ns\n\
             drm-engine-video: {video} ns\ndrm-engine-capacity-video: 2\ndrm-total-system0: 64 MiB\n\
             drm-shared-system0: 1024 KiB\n"
        )
    }

    fn snapshot(time: u64, records: &[String]) -> Snapshot {
        let clients = records
            .iter()
            .map(|r| parse_client(r, DEVICE).unwrap().unwrap())
            .collect();
        Snapshot {
            device: DEVICE.into(),
            boot: "boot".into(),
            time,
            clients: Ordered(clients),
        }
    }

    #[test]
    fn engine_deltas_capacity_and_private_memory() {
        let before = snapshot(1_000_000_000, &[fdinfo(1, 100, 0)]);
        let mut after = snapshot(2_000_000_000, &[fdinfo(1, 500_000_100, 1_000_000_000)]);
        let busy = utilization(&mut after, Some(&before));
        assert_eq!(
            busy.0,
            vec![("render".to_string(), 50.0), ("video".to_string(), 50.0)]
        );
        assert_eq!(after.clients.0[0].1.private, Some(63 * 1_048_576));
    }

    #[test]
    fn counter_regression_keeps_the_high_water_mark() {
        let before = snapshot(1_000_000_000, &[fdinfo(1, 500_000_000, 0)]);
        let mut after = snapshot(2_000_000_000, &[fdinfo(1, 400_000_000, 0)]);
        assert_eq!(
            utilization(&mut after, Some(&before)).get("render"),
            Some(&0.0)
        );
        assert_eq!(
            after.clients.0[0].1.engines.get("render"),
            Some(&500_000_000)
        );
    }

    #[test]
    fn stale_and_foreign_baselines_count_nothing() {
        let mut current = snapshot(40_000_000_000, &[fdinfo(1, 100, 0)]);
        for previous in [
            snapshot(1, &[fdinfo(1, 0, 0)]),
            snapshot(40_000_000_000, &[fdinfo(1, 0, 0)]),
        ] {
            assert!(utilization(&mut current, Some(&previous)).0.is_empty());
        }
        let mut other = snapshot(39_000_000_000, &[fdinfo(1, 0, 0)]);
        other.boot = "older".into();
        assert!(utilization(&mut current, Some(&other)).0.is_empty());
    }

    #[test]
    fn malformed_fields() {
        let good = fdinfo(1, 100, 0);
        assert!(
            parse_client(&good.replace("i915", "amdgpu"), DEVICE)
                .unwrap()
                .is_none()
        );
        assert!(
            parse_client(&good.replace("client-id: 1", "client-id: bad"), DEVICE)
                .unwrap()
                .is_none()
        );
        assert!(parse_client(&good.replace("100 ns", "-1 ns"), DEVICE).is_err());
        assert!(
            parse_client(
                &good.replace("capacity-video: 2", "capacity-video: 0"),
                DEVICE
            )
            .is_err()
        );
        let private = |text: String| parse_client(&text, DEVICE).unwrap().unwrap().1.private;
        assert_eq!(private(good.replace("1024 KiB", "65 MiB")), None);
        assert_eq!(
            private(good.replace("drm-shared-system0: 1024 KiB", "")),
            None
        );
        assert_eq!(
            private(
                good.replace("drm-total-system0: 64 MiB\n", "")
                    .replace("drm-shared-system0: 1024 KiB", "")
            ),
            None
        );
    }

    #[test]
    fn snapshots_round_trip_and_reject_wrong_types() {
        let snap = snapshot(5, &[fdinfo(1, 100, 0)]);
        let text = String::from_utf8(render_snapshot(&snap)).unwrap();
        assert_eq!(
            text,
            r#"{"device":"0000:00:02.0","boot":"boot","time":5,"clients":{"1":{"engines":{"render":100,"video":0},"capacities":{"video":2},"private":66060288}}}"#
        );
        assert_eq!(parse_snapshot(&text), Some(snap));
        for bad in [
            "broken",
            "{}",
            "[]",
            r#"{"device":"x","boot":"b","time":true,"clients":{}}"#,
            r#"{"device":"x","boot":"b","time":1.0,"clients":{}}"#,
            r#"{"device":"x","boot":"b","time":1,"clients":{"1":{"engines":{},"capacities":{"v":0},"private":null}}}"#,
        ] {
            assert!(parse_snapshot(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn the_scan_has_a_budget() {
        let failure = read_clients(
            DEVICE,
            Path::new("/proc"),
            0,
            Instant::now() - Duration::from_secs(1),
        )
        .unwrap_err();
        assert_eq!(failure.0, "Intel process scan exceeded its budget");
    }

    #[test]
    fn stats_render_like_json_dumps() {
        let stats = Stats {
            intel: true,
            busy: Some(50.0),
            engines: Ordered(vec![("render".into(), 50.0), ("vid\u{e9}o".into(), 0.0)]),
            private: Some(7),
            frequency: Some(800),
            temp: Some(45.5),
            thermals: vec![Thermal {
                temperature: 45.5,
                critical: None,
                label: "GPU".into(),
            }],
            error: "",
        };
        assert_eq!(
            String::from_utf8(stats.render()).unwrap(),
            r#"{"gpuIntel":true,"gpuBusy":50.0,"gpuEngines":{"render":50.0,"vid\u00e9o":0.0},"gpuMemoryPrivate":7,"gpuFrequencyMHz":800,"gpuTemp":45.5,"gpuThermals":[{"temperature":45.5,"critical":null,"label":"GPU"}],"gpuError":""}"#
        );
        let failed = Stats {
            intel: true,
            error: UNAVAILABLE,
            ..Stats::default()
        };
        assert!(String::from_utf8(failed.render()).unwrap().ends_with(
            r#""gpuThermals":[],"gpuError":"Intel GPU readings unavailable. Retrying\u2026"}"#
        ));
    }
}
