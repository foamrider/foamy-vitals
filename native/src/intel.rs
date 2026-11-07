//! Intel (i915/xe) GPU readings from the DRM fdinfo of this user's
//! processes, without perf privileges: per GPU, the busiest engine since the
//! last sample, private allocations, the GT frequency and the GPU's own
//! sensors. A port of `intel_stats.py` that prints the same `json.dumps`
//! text: one scan of `/proc` attributes the clients of every requested GPU,
//! and each GPU keeps its own baseline (`intel-<ID>.json`).

use std::collections::{BTreeMap, HashMap};
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
pub const WINDOW_NS: i128 = 30_000_000_000;
/// The window of a GPU read on the spaced-out schedule (bar samples at most
/// every 30 s), so its busy is averaged over the gap rather than discarded.
pub const SPACED_WINDOW_NS: i128 = 60_000_000_000;

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

/// One DRM client's fdinfo as `(device, client id, client)`; `None` when it
/// belongs to another driver or an unrequested device, or has no numeric
/// client id.
pub fn parse_client(
    text: &str,
    devices: &[String],
) -> Result<Option<(String, String, Client)>, Failure> {
    let mut fields: Ordered<&str> = Ordered::default();
    for line in text::py_splitlines(text) {
        if let Some((key, value)) = line.split_once(':') {
            fields.set(key, value);
        }
    }
    let field = |key: &str| fields.get(key).copied().unwrap_or("");
    let device = py_strip(field("drm-pdev"));
    if !matches!(py_strip(field("drm-driver")), "i915" | "xe")
        || !devices.iter().any(|wanted| wanted == device)
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
                    device.to_string(),
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
        device.to_string(),
        ident.to_string(),
        Client {
            engines,
            capacities,
            private,
        },
    )))
}

/// The clients of each requested device, keyed by device.
pub type Clients = BTreeMap<String, Ordered<Client>>;

/// `/dev/dri/(card\d+|renderD\d+)`, whole: the DRM node a descriptor opens.
fn drm_node(target: &[u8]) -> Option<&[u8]> {
    let node = target.strip_prefix(b"/dev/dri/")?;
    let digits = node
        .strip_prefix(b"card")
        .or_else(|| node.strip_prefix(b"renderD"))?;
    text::is_digits(digits).then_some(node)
}

/// The PCI device a DRM node belongs to (`/sys/class/drm/<node>/device`
/// resolved, its name lowercased), or `None`.
fn node_device(drm: &Path, node: &[u8]) -> Option<String> {
    let device =
        fs::canonicalize(drm.join(std::ffi::OsStr::from_bytes(node)).join("device")).ok()?;
    Some(device.file_name()?.to_string_lossy().to_lowercase())
}

/// Every DRM client of the requested devices held by this user's processes,
/// from one scan, at most until `deadline`. A descriptor's fdinfo is opened
/// only once its `/dev/dri` node resolves to a requested device: printing an
/// xe fdinfo takes a runtime-PM reference that wakes the GPU. Paths are
/// resolved from a descriptor of `proc` (`statat`, `readlinkat`): a desktop
/// session has thousands of descriptors.
pub fn read_clients(
    devices: &[String],
    proc: &Path,
    drm: &Path,
    uid: u32,
    deadline: Instant,
) -> Result<Clients, Failure> {
    let mut clients: Clients = devices
        .iter()
        .map(|device| (device.clone(), Ordered::default()))
        .collect();
    let mut owners: HashMap<Vec<u8>, Option<String>> = HashMap::new();
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
        let mut scan = Scanner {
            devices,
            drm,
            uid,
            deadline,
            clients: &mut clients,
            owners: &mut owners,
            link: &mut link,
        };
        match process.scan(&mut scan, &mut fd_buffer) {
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

/// What one scan carries from process to process.
struct Scanner<'a> {
    devices: &'a [String],
    drm: &'a Path,
    uid: u32,
    deadline: Instant,
    clients: &'a mut Clients,
    /// DRM node → its device, resolved once per scan.
    owners: &'a mut HashMap<Vec<u8>, Option<String>>,
    link: &'a mut Vec<u8>,
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

    fn scan(&self, scan: &mut Scanner, buffer: &mut [MaybeUninit<u8>]) -> Result<(), Scan> {
        let stat = rfs::statat(self.root, self.name, AtFlags::empty())
            .map_err(|error| self.error(error, b""))?;
        if stat.st_uid != scan.uid {
            return Ok(());
        }
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC;
        let fd_dir = rfs::openat(self.root, self.relative(b"/fd"), flags, Mode::empty())
            .map_err(|error| self.error(error, b"/fd"))?;
        let mut fds = rfs::RawDir::new(&fd_dir, buffer);
        while let Some(fd) = fds.next() {
            if Instant::now() > scan.deadline {
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
            let target = match rfs::readlinkat(&fd_dir, number, std::mem::take(scan.link)) {
                Ok(target) => target.into_bytes(),
                Err(error) => match self.error(error, &[b"/fd/", number].concat()) {
                    Scan::Skip => continue,
                    failure => return Err(failure),
                },
            };
            let node = drm_node(&target).map(<[u8]>::to_vec);
            *scan.link = target;
            scan.link.clear();
            let Some(node) = node else {
                continue;
            };
            // Then the node's device: other GPUs' descriptors are never read.
            let drm = scan.drm;
            let owner = scan
                .owners
                .entry(node)
                .or_insert_with_key(|node| node_device(drm, node));
            if !owner
                .as_ref()
                .is_some_and(|owner| scan.devices.contains(owner))
            {
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
            // Processes may share a DRM file; count its client only once per device.
            if let Some((device, ident, client)) =
                parse_client(&text, scan.devices).map_err(Scan::Fail)?
                && let Some(known) = scan.clients.get_mut(&device)
                && known.get(&ident).is_none()
            {
                known.0.push((ident, client));
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
pub fn utilization(
    current: &mut Snapshot,
    previous: Option<&Snapshot>,
    window_ns: i128,
) -> Ordered<f64> {
    let Some(previous) = previous else {
        return Ordered::default();
    };
    if current.device != previous.device || current.boot != previous.boot {
        return Ordered::default();
    }
    let elapsed = i128::from(current.time) - i128::from(previous.time);
    if !(0 < elapsed && elapsed <= window_ns) {
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

/// One GPU's readings, in `json.dumps` order.
struct Device {
    busy: Option<f64>,
    private: Option<u128>,
    frequency: Option<u64>,
    thermals: Vec<Thermal>,
    error: &'static str,
}

impl Device {
    fn unavailable() -> Device {
        Device {
            busy: None,
            private: None,
            frequency: None,
            thermals: Vec::new(),
            error: UNAVAILABLE,
        }
    }

    /// `{"busy":…,"memoryPrivate":…,"frequencyMHz":…,"thermals":[…],"error":…}`.
    fn render(&self) -> Vec<u8> {
        let float = |value: Option<f64>| value.map_or_else(|| "null".to_string(), py_float);
        let number = |value: Option<String>| value.unwrap_or_else(|| "null".to_string());
        let mut out = format!(
            "{{\"busy\":{},\"memoryPrivate\":{},\"frequencyMHz\":{},\"thermals\":[",
            float(self.busy),
            number(self.private.map(|v| v.to_string())),
            number(self.frequency.map(|v| v.to_string())),
        )
        .into_bytes();
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
        out.extend_from_slice(b"],\"error\":");
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

fn lexists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

/// `sorted()` of paths: component by component.
fn sort_paths(paths: &mut [PathBuf]) {
    paths.sort_by(|a, b| a.components().cmp(b.components()));
}

/// `value / 1000` with Python's correctly rounded int division.
fn thousandths(value: u64) -> f64 {
    text::py_div(u128::from(value), 1000)
}

pub struct Paths<'a> {
    pub proc: &'a Path,
    pub sys: &'a Path,
    /// Where each GPU's baseline (`intel-<ID>.json`) is kept; none when no
    /// state directory is safe.
    pub state: Option<PathBuf>,
}

/// A GPU to read: its PCI address and whether it is read on the spaced-out
/// schedule (a 60 s baseline window).
pub struct Request {
    pub device: String,
    pub spaced: bool,
}

/// What `intel_stats.py` would have let escape (an exception it does not
/// catch): the whole run fails, and every requested GPU reads as unavailable.
#[derive(Debug)]
pub struct Crash(pub String);

/// One reading per request, in order (`json.dumps` objects), or the crash;
/// and the diagnostics for stderr.
pub fn collect(
    paths: &Paths,
    requests: &[Request],
    uid: u32,
    budget: Duration,
) -> (Result<Vec<Vec<u8>>, Crash>, Vec<String>) {
    let mut diagnostics = Vec::new();
    let devices: Vec<String> = requests.iter().map(|r| r.device.clone()).collect();
    let deadline = Instant::now() + budget;
    let mut clients = match read_clients(
        &devices,
        paths.proc,
        &paths.sys.join("class/drm"),
        uid,
        deadline,
    ) {
        Ok(clients) => clients,
        Err(Failure(message)) => {
            diagnostics.push(format!("Intel telemetry: {message}"));
            let failed = Device::unavailable().render();
            return (Ok(vec![failed; requests.len()]), diagnostics);
        }
    };
    let mut readings = Vec::with_capacity(requests.len());
    for request in requests {
        let device = &request.device;
        let reading = collect_device(
            &Baseline {
                device,
                state: paths
                    .state
                    .as_ref()
                    .map(|dir| dir.join(format!("intel-{device}.json"))),
                proc: paths.proc,
                window_ns: if request.spaced {
                    SPACED_WINDOW_NS
                } else {
                    WINDOW_NS
                },
            },
            device_card(paths.sys, device),
            clients.remove(device).unwrap_or_default(),
            &mut diagnostics,
        );
        match reading {
            Ok(reading) => readings.push(reading.render()),
            Err(crash) => return (Err(crash), diagnostics),
        }
    }
    (Ok(readings), diagnostics)
}

/// The lowest-numbered `card<N>` of a PCI device
/// (`/sys/bus/pci/devices/<ID>/drm/card<N>`).
fn device_card(sys: &Path, device: &str) -> Option<PathBuf> {
    let drm = sys.join("bus/pci/devices").join(device).join("drm");
    entries(&drm, "card", "")
        .into_iter()
        .filter_map(|card| {
            let name = card.file_name()?.as_bytes();
            let digits = &name[4..];
            if !text::is_digits(digits) {
                return None;
            }
            // int(digits): compared without leading zeros, by length then digits.
            let start = digits
                .iter()
                .position(|&b| b != b'0')
                .unwrap_or(digits.len());
            let key = (digits.len() - start, digits[start..].to_vec());
            Some((key, card))
        })
        .min_by(|a, b| a.0.cmp(&b.0))
        .map(|(_, card)| card)
}

struct Baseline<'a> {
    device: &'a str,
    state: Option<PathBuf>,
    proc: &'a Path,
    window_ns: i128,
}

/// The engine usage since the stored baseline, and the new baseline stored.
fn baseline(
    baseline: &Baseline,
    clients: Ordered<Client>,
) -> Result<(Ordered<f64>, Snapshot), Failure> {
    let boot = py_strip(&read_text(
        &baseline.proc.join("sys/kernel/random/boot_id"),
    )?)
    .to_string();
    let time = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    let time = (time.tv_sec as u64)
        .wrapping_mul(1_000_000_000)
        .wrapping_add(time.tv_nsec as u64);
    let mut current = Snapshot {
        device: baseline.device.to_string(),
        boot,
        time,
        clients,
    };
    let previous = match &baseline.state {
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
    let engines = utilization(&mut current, previous.as_ref(), baseline.window_ns);
    if let Some(state) = &baseline.state {
        // Renamed into place so an interrupted sample never leaves half a baseline.
        let temporary = state.with_extension("tmp");
        fs::write(&temporary, render_snapshot(&current))
            .map_err(|error| os_error(&error, &temporary))?;
        fs::rename(&temporary, state).map_err(|error| os_error(&error, state))?;
    }
    Ok((engines, current))
}

/// A sensor file's text: `None` when it can't be read (that value is
/// skipped), a crash when it is not UTF-8 (Python let that escape).
fn sensor_text(path: &Path) -> Result<Option<String>, Crash> {
    match text::read_file(path, SMALL) {
        Ok(data) => text::py_text(data)
            .map(Some)
            .map_err(|_| Crash(format!("'utf-8' codec can't decode '{}'", path.display()))),
        Err(_) => Ok(None),
    }
}

/// One GPU's readings. A failed baseline makes it unavailable (with a
/// diagnostic); each frequency file and sensor is read on its own, so one
/// unreadable file costs only its own value.
fn collect_device(
    base: &Baseline,
    card: Option<PathBuf>,
    clients: Ordered<Client>,
    diagnostics: &mut Vec<String>,
) -> Result<Device, Crash> {
    let (engines, current) = match baseline(base, clients) {
        Ok(done) => done,
        Err(Failure(message)) => {
            diagnostics.push(format!("Intel telemetry: {message}"));
            return Ok(Device::unavailable());
        }
    };
    let mut frequencies = Vec::new();
    let mut thermals = Vec::new();
    if let Some(card) = &card {
        let mut files: Vec<PathBuf> = entries(&card.join("gt"), "gt", "")
            .into_iter()
            .filter(|gt| gt.is_dir())
            .map(|gt| gt.join("rps_act_freq_mhz"))
            .filter(|path| lexists(path))
            .collect();
        sort_paths(&mut files);
        for path in files {
            if let Some(value) = sensor_text(&path)?.and_then(|text| py_integer(&text, "")) {
                frequencies.push(value);
            }
        }
        // Only sensors attached to this GPU, never a CPU/package temperature or
        // another graphics device's sensor.
        let mut inputs: Vec<PathBuf> = entries(&card.join("device/hwmon"), "hwmon", "")
            .into_iter()
            .filter(|hwmon| hwmon.is_dir())
            .flat_map(|hwmon| entries(&hwmon, "temp", "_input"))
            .collect();
        sort_paths(&mut inputs);
        for input in inputs {
            let name = input
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let prefix = name.strip_suffix("_input").unwrap_or(&name);
            let Some(value) = sensor_text(&input)?.and_then(|text| py_integer(&text, "")) else {
                continue;
            };
            let critical = sensor_text(&input.with_file_name(format!("{prefix}_crit")))?
                .and_then(|text| py_integer(&text, ""));
            let label = sensor_text(&input.with_file_name(format!("{prefix}_label")))?
                .map_or_else(|| "GPU".to_string(), |text| py_strip(&text).to_string());
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
    Ok(Device {
        busy: engines.0.iter().map(|(_, pct)| *pct).reduce(f64::max),
        private: if !allocations.is_empty() && allocations.iter().all(Option::is_some) {
            Some(allocations.iter().flatten().sum())
        } else {
            None
        },
        frequency: frequencies.into_iter().max(),
        thermals,
        error: "",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEVICE: &str = "0000:00:02.0";

    fn devices() -> Vec<String> {
        vec![DEVICE.to_string()]
    }

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
            .map(|r| {
                let (_, ident, client) = parse_client(r, &devices()).unwrap().unwrap();
                (ident, client)
            })
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
        let busy = utilization(&mut after, Some(&before), WINDOW_NS);
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
            utilization(&mut after, Some(&before), WINDOW_NS).get("render"),
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
            assert!(
                utilization(&mut current, Some(&previous), WINDOW_NS)
                    .0
                    .is_empty()
            );
        }
        let mut other = snapshot(39_000_000_000, &[fdinfo(1, 0, 0)]);
        other.boot = "older".into();
        assert!(
            utilization(&mut current, Some(&other), WINDOW_NS)
                .0
                .is_empty()
        );
    }

    #[test]
    fn malformed_fields() {
        let good = fdinfo(1, 100, 0);
        assert!(
            parse_client(&good.replace("i915", "amdgpu"), &devices())
                .unwrap()
                .is_none()
        );
        assert!(
            parse_client(&good.replace("client-id: 1", "client-id: bad"), &devices())
                .unwrap()
                .is_none()
        );
        assert!(parse_client(&good.replace("100 ns", "-1 ns"), &devices()).is_err());
        assert!(
            parse_client(
                &good.replace("capacity-video: 2", "capacity-video: 0"),
                &devices()
            )
            .is_err()
        );
        let private = |text: String| parse_client(&text, &devices()).unwrap().unwrap().2.private;
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
            &devices(),
            Path::new("/proc"),
            Path::new("/sys/class/drm"),
            0,
            Instant::now() - Duration::from_secs(1),
        )
        .unwrap_err();
        assert_eq!(failure.0, "Intel process scan exceeded its budget");
    }

    #[test]
    fn spaced_gpus_keep_a_wider_window() {
        let before = snapshot(1_000_000_000, &[fdinfo(1, 100, 0)]);
        let mut after = snapshot(46_000_000_000, &[fdinfo(1, 4_500_000_100, 0)]);
        assert!(
            utilization(&mut after.clone(), Some(&before), WINDOW_NS)
                .0
                .is_empty()
        );
        assert_eq!(
            utilization(&mut after, Some(&before), SPACED_WINDOW_NS).get("render"),
            Some(&10.0)
        );
    }

    #[test]
    fn clients_name_their_device() {
        let other = "0000:03:00.0".to_string();
        let both = [DEVICE.to_string(), other.clone()];
        let text = fdinfo(1, 100, 0).replace(DEVICE, &other);
        assert_eq!(parse_client(&text, &both).unwrap().unwrap().0, other);
        assert!(parse_client(&text, &devices()).unwrap().is_none());
    }

    #[test]
    fn drm_nodes_are_card_or_render_nodes() {
        assert_eq!(drm_node(b"/dev/dri/renderD128"), Some(&b"renderD128"[..]));
        assert_eq!(drm_node(b"/dev/dri/card1"), Some(&b"card1"[..]));
        for target in [
            &b"/dev/dri/by-path/x"[..],
            b"/dev/dri/card",
            b"/dev/dri/card1x",
            b"/dev/dri/controlD64",
            b"socket:[1]",
            b"/dev/dri/renderD128 (deleted)",
        ] {
            assert_eq!(drm_node(target), None);
        }
    }

    #[test]
    fn devices_render_like_json_dumps() {
        let device = Device {
            busy: Some(50.0),
            private: Some(7),
            frequency: Some(800),
            thermals: vec![Thermal {
                temperature: 45.5,
                critical: None,
                label: "GPU \u{e9}".into(),
            }],
            error: "",
        };
        assert_eq!(
            String::from_utf8(device.render()).unwrap(),
            r#"{"busy":50.0,"memoryPrivate":7,"frequencyMHz":800,"thermals":[{"temperature":45.5,"critical":null,"label":"GPU \u00e9"}],"error":""}"#
        );
        assert_eq!(
            String::from_utf8(Device::unavailable().render()).unwrap(),
            r#"{"busy":null,"memoryPrivate":null,"frequencyMHz":null,"thermals":[],"error":"Intel GPU readings unavailable. Retrying\u2026"}"#
        );
    }
}
