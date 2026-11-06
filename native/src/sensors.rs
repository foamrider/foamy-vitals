//! Temperatures (hwmon, thermal zones), sysfs GPU load and NVIDIA readings.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use foamy_helper::child::Bounded;
use rustix::fs::{Access, access};

use crate::state::Store;
use crate::text::{self, read_value, read_var};

/// `dir/<prefix>*<suffix>` in the shell's glob order: entries not starting
/// with a dot, as full paths sorted by bytes.
pub fn glob(dir: &Path, prefix: &[u8], suffix: &[u8]) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut names: Vec<Vec<u8>> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().as_bytes().to_vec())
        .filter(|name| {
            name.first() != Some(&b'.')
                && name.len() >= prefix.len() + suffix.len()
                && name.starts_with(prefix)
                && name.ends_with(suffix)
        })
        .collect();
    names.sort_unstable();
    names
        .into_iter()
        .map(|name| dir.join(OsStr::from_bytes(&name)))
        .collect()
}

/// `[[ -r path ]]`.
pub fn readable(path: &Path) -> bool {
    access(path, Access::READ_OK).is_ok()
}

/// `degrees`: whole degrees from a millidegree file, or `null`.
fn degrees(path: &Path) -> Vec<u8> {
    let raw = read_value(path);
    let digits = raw.strip_prefix(b"-").unwrap_or(&raw);
    if !text::is_digits(digits) {
        return b"null".to_vec();
    }
    (text::bash_int(&raw).wrapping_add(500) / 1000)
        .to_string()
        .into_bytes()
}

/// `integer`: a non-negative integer file as written, or `null`.
fn integer(path: &Path) -> Vec<u8> {
    let raw = read_value(path);
    if text::is_digits(&raw) {
        raw
    } else {
        b"null".to_vec()
    }
}

fn with_suffix(path: &Path, from: &str, to: &str) -> PathBuf {
    let bytes = path.as_os_str().as_bytes();
    let stem = bytes.strip_suffix(from.as_bytes()).unwrap_or(bytes);
    PathBuf::from(OsStr::from_bytes(&[stem, to.as_bytes()].concat()))
}

pub struct Temperatures {
    pub cpu_temp: Vec<u8>,
    pub cpu_temp_crit: Vec<u8>,
    pub gpu_temp: Vec<u8>,
    pub gpu_thermals: Vec<u8>,
}

pub fn temperatures(sys: &Path) -> Temperatures {
    // First sensor block per driver name, in sysfs order.
    let mut hwmon: HashMap<Vec<u8>, PathBuf> = HashMap::new();
    for dir in glob(&sys.join("class/hwmon"), b"hwmon", b"") {
        let Some((name, _)) = read_var(&dir.join("name")) else {
            continue;
        };
        // Empty: skipped (an empty array subscript is an error in Bash).
        if name.is_empty() {
            continue;
        }
        hwmon.entry(name).or_insert(dir);
    }
    let named = |wanted: &[&[u8]]| wanted.iter().find_map(|name| hwmon.get(*name).cloned());

    let mut cpu_temp = b"null".to_vec();
    let mut cpu_temp_crit = b"null".to_vec();
    if let Some(dir) = named(&[b"k10temp", b"zenpower", b"coretemp"]) {
        // The whole-chip sensor is found by label, not position.
        let mut input = dir.join("temp1_input");
        for label_file in glob(&dir, b"temp", b"_label") {
            let Some((label, complete)) = read_var(&label_file) else {
                continue;
            };
            if label.is_empty() && !complete {
                continue;
            }
            if label.starts_with(b"Package")
                || label.starts_with(b"Tctl")
                || label.starts_with(b"Tdie")
            {
                input = with_suffix(&label_file, "_label", "_input");
                break;
            }
        }
        cpu_temp = degrees(&input);
        cpu_temp_crit = degrees(&with_suffix(&input, "_input", "_crit"));
    }
    if cpu_temp == b"null" {
        for zone in glob(&sys.join("class/thermal"), b"thermal_zone", b"") {
            let Some((kind, complete)) = read_var(&zone.join("type")) else {
                continue;
            };
            if (kind.is_empty() && !complete) || kind != b"x86_pkg_temp" {
                continue;
            }
            cpu_temp = degrees(&zone.join("temp"));
            // A different sensor's critical limit must not be attached to this fallback.
            cpu_temp_crit = b"null".to_vec();
            break;
        }
    }

    let mut gpu_temp = b"null".to_vec();
    let mut gpu_thermals = b"[".to_vec();
    if let Some(dir) = named(&[b"amdgpu", b"radeon", b"i915", b"xe", b"nouveau"]) {
        gpu_temp = degrees(&dir.join("temp1_input"));
        for input in glob(&dir, b"temp", b"_input") {
            if !readable(&input) {
                continue;
            }
            let temperature = degrees(&input);
            let critical = degrees(&with_suffix(&input, "_input", "_crit"));
            // Stable display labels; arbitrary driver strings stay out of JSON.
            let file = input.file_name().map_or(&b""[..], |name| name.as_bytes());
            let number = file.strip_prefix(b"temp").unwrap_or(file);
            let number = number.strip_suffix(b"_input").unwrap_or(number);
            let label: Vec<u8> = match &read_value(&with_suffix(&input, "_input", "_label"))[..] {
                b"edge" => b"Edge".to_vec(),
                b"junction" => b"Hotspot".to_vec(),
                b"mem" => b"Memory".to_vec(),
                _ => [&b"Sensor "[..], number].concat(),
            };
            if gpu_thermals.len() > 1 {
                gpu_thermals.push(b',');
            }
            gpu_thermals.extend_from_slice(b"{\"temperature\":");
            gpu_thermals.extend_from_slice(&temperature);
            gpu_thermals.extend_from_slice(b",\"critical\":");
            gpu_thermals.extend_from_slice(&critical);
            gpu_thermals.extend_from_slice(b",\"label\":\"");
            gpu_thermals.extend_from_slice(&label);
            gpu_thermals.extend_from_slice(b"\"}");
        }
    }
    gpu_thermals.push(b']');
    Temperatures {
        cpu_temp,
        cpu_temp_crit,
        gpu_temp,
        gpu_thermals,
    }
}

/// Load and VRAM from the first DRM card that reports `gpu_busy_percent`.
pub struct SysfsGpu {
    pub busy: Vec<u8>,
    pub vram_used: Vec<u8>,
    pub vram_total: Vec<u8>,
}

pub fn sysfs_gpu(sys: &Path) -> SysfsGpu {
    let mut gpu = SysfsGpu {
        busy: b"null".to_vec(),
        vram_used: b"null".to_vec(),
        vram_total: b"null".to_vec(),
    };
    let mut busy_files: Vec<PathBuf> = glob(&sys.join("class/drm"), b"card", b"")
        .into_iter()
        .map(|card| card.join("device/gpu_busy_percent"))
        .collect();
    busy_files.sort_unstable_by(|a, b| a.as_os_str().as_bytes().cmp(b.as_os_str().as_bytes()));
    for busy in busy_files {
        if !readable(&busy) {
            continue;
        }
        let device = busy.parent().unwrap_or(Path::new("."));
        gpu.busy = integer(&busy);
        gpu.vram_used = integer(&device.join("mem_info_vram_used"));
        gpu.vram_total = integer(&device.join("mem_info_vram_total"));
        break;
    }
    gpu
}

/// The lowest-addressed NVIDIA GPU (`$proc/driver/nvidia/gpus/*`, glob order).
pub fn nvidia_address(proc: &Path) -> Option<Vec<u8>> {
    glob(&proc.join("driver/nvidia/gpus"), b"", b"")
        .into_iter()
        .find(|entry| entry.is_dir())
        .and_then(|entry| entry.file_name().map(|name| name.as_bytes().to_vec()))
}

/// A bound i915/xe device (`drivers/{i915,xe}/*:*` exists).
pub fn intel_bound(sys: &Path) -> bool {
    ["i915", "xe"].iter().any(|driver| {
        let dir = sys.join("bus/pci/drivers").join(driver);
        let Ok(entries) = fs::read_dir(&dir) else {
            return false;
        };
        entries.filter_map(Result::ok).any(|entry| {
            let name = entry.file_name();
            let name = name.as_bytes();
            name.first() != Some(&b'.')
                && name.contains(&b':')
                && fs::metadata(entry.path()).is_ok()
        })
    })
}

/// A PCI address as `domain:bus:device.function`, the domain as a number and
/// the rest lowercase, so sysfs and nvidia-smi spellings compare equal.
pub fn pci_key(raw: &[u8]) -> Vec<u8> {
    let raw: Vec<u8> = raw
        .iter()
        .copied()
        .filter(|&b| !text::is_c_space(b))
        .collect();
    let lower = raw.to_ascii_lowercase();
    let colon = raw.iter().position(|&b| b == b':');
    if let Some(colon) = colon {
        let domain = &raw[..colon];
        let rest = &lower[colon + 1..];
        let two_colons = rest.contains(&b':');
        if two_colons && (1..=8).contains(&domain.len()) && domain.iter().all(u8::is_ascii_hexdigit)
        {
            let value =
                u64::from_str_radix(std::str::from_utf8(domain).unwrap_or("0"), 16).unwrap_or(0);
            let mut key = format!("{value:x}:").into_bytes();
            key.extend_from_slice(rest);
            return key;
        }
    }
    [&b"invalid:"[..], &lower].concat()
}

/// One nvidia-smi CSV value: digits, or `na`.
fn nv_field(value: &[u8]) -> Vec<u8> {
    let value = text::trim_by(value, text::is_c_space);
    if text::is_digits(value) {
        value.to_vec()
    } else {
        b"na".to_vec()
    }
}

/// The `--query-gpu` reply's row for `want`: utilization, used, total, temperature.
pub fn parse_query(stdout: &[u8], want: &[u8]) -> Option<[Vec<u8>; 4]> {
    // `while read`: a last line without a newline is not read.
    let end = stdout.iter().rposition(|&b| b == b'\n')?;
    let complete = &stdout[..end];
    for line in complete.split(|&b| b == b'\n') {
        let line: Vec<u8> = line.iter().copied().filter(|&b| b != 0).collect();
        let mut fields = line.split(|&b| b == b',');
        let mut next = || fields.next().unwrap_or(b"");
        let (bus, util, used, total, temp) = (next(), next(), next(), next(), next());
        if pci_key(bus) != want {
            continue;
        }
        return Some([
            nv_field(util),
            nv_field(used),
            nv_field(total),
            nv_field(temp),
        ]);
    }
    None
}

/// `<gpu_temp_max_threshold>[[:space:]]*([0-9]+)[[:space:]]*C[[:space:]]*<`, leftmost.
fn threshold(block: &[u8]) -> Option<&[u8]> {
    const TAG: &[u8] = b"<gpu_temp_max_threshold>";
    let mut from = 0;
    while let Some(at) = find(&block[from..], TAG) {
        let start = from + at + TAG.len();
        from = from + at + 1;
        let rest = text::trim_start_by(&block[start..], text::is_c_space);
        let digits = rest.iter().take_while(|b| b.is_ascii_digit()).count();
        if digits == 0 {
            continue;
        }
        let after = text::trim_start_by(&rest[digits..], text::is_c_space);
        let Some(after) = after.strip_prefix(b"C") else {
            continue;
        };
        if text::trim_start_by(after, text::is_c_space).first() == Some(&b'<') {
            return Some(&rest[..digits]);
        }
    }
    None
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

/// The shutdown threshold of `want` in `nvidia-smi -q -x -d TEMPERATURE`:
/// `Some(digits)`, `Some("none")` (this GPU reports none), `None` (not found).
pub fn parse_limit(xml: &[u8], want: &[u8]) -> Option<Vec<u8>> {
    const OPEN: &[u8] = b"<gpu id=\"";
    let mut xml: Vec<u8> = xml.iter().copied().filter(|&b| b != 0).collect();
    while xml.last() == Some(&b'\n') {
        xml.pop();
    }
    let mut rest = &xml[..];
    while let Some(at) = find(rest, OPEN) {
        rest = &rest[at + OPEN.len()..];
        let id_end = rest.iter().position(|&b| b == b'"').unwrap_or(rest.len());
        if pci_key(&rest[..id_end]) != want {
            continue;
        }
        let block = &rest[..find(rest, b"</gpu>").unwrap_or(rest.len())];
        return Some(match threshold(block) {
            Some(digits) if digits.iter().any(|&b| b != b'0') => digits.to_vec(),
            _ => b"none".to_vec(),
        });
    }
    None
}

/// The NVIDIA part of a sample.
pub struct Nvidia {
    pub busy: Vec<u8>,
    pub vram_used: Option<(Vec<u8>, Vec<u8>)>,
    pub temp: Vec<u8>,
    pub thermals: Vec<u8>,
    pub error: &'static str,
    pub suspended: bool,
}

const NVIDIA_TIMEOUT: Duration = Duration::from_millis(1500);
const NVIDIA_OUTPUT: usize = 1 << 20;
const QUERY: [&str; 2] = [
    "--query-gpu=pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
];
const LIMIT: [&str; 4] = ["-q", "-x", "-d", "TEMPERATURE"];

/// nvidia-smi with the old bounds: 1.5 s, `LC_ALL=C`, stderr dropped.
fn nvidia_smi(args: &[&str]) -> Option<foamy_helper::child::Output> {
    Bounded::new("nvidia-smi")
        .args(args)
        .env("LC_ALL", "C")
        .timeout(NVIDIA_TIMEOUT)
        .max_output(NVIDIA_OUTPUT)
        .run()
        .ok()
}

pub struct NvidiaInput<'a> {
    pub sys: &'a Path,
    pub proc: &'a Path,
    pub address: &'a [u8],
    pub bar: bool,
    pub now: i64,
}

pub fn nvidia(input: &NvidiaInput, store: &mut Store) -> Nvidia {
    let power = input
        .sys
        .join("bus/pci/devices")
        .join(OsStr::from_bytes(input.address))
        .join("power");
    let runtime_status = read_value(&power.join("runtime_status"));
    let runtime_control = read_value(&power.join("control"));
    let mut result = Nvidia {
        busy: b"null".to_vec(),
        vram_used: None,
        temp: b"null".to_vec(),
        thermals: b"[]".to_vec(),
        error: "",
        suspended: false,
    };
    if runtime_status == b"suspended" {
        // Never wake a GPU that runtime power management has suspended.
        result.busy = b"0".to_vec();
        result.suspended = true;
        return result;
    }

    // at util used total temp crit boot retry fails
    let mut f: Vec<Vec<u8>> = vec![
        b"-1".to_vec(),
        vec![],
        vec![],
        vec![],
        vec![],
        vec![],
        vec![],
        b"0".to_vec(),
        b"0".to_vec(),
    ];
    if let Some(saved) = store.read("nvidia") {
        let (line, _) = text::first_line(&saved);
        f = text::split_ifs(&line, text::is_ifs, 10)
            .into_iter()
            .take(9)
            .map(<[u8]>::to_vec)
            .collect();
    }
    let valid = |field: &[u8], default: &[u8]| {
        if text::is_digits(field) {
            field.to_vec()
        } else {
            default.to_vec()
        }
    };
    // Kept as read until reassigned, as the shell printed them back.
    let mut retry_text = valid(&f[7], b"0");
    let mut fails_text = valid(&f[8], b"0");
    let mut retry = text::bash_int(&retry_text);
    let mut fails = text::bash_int(&fails_text);
    let mut at = text::bash_int(&valid(&f[0], b"-1"));
    let (mut util, mut used, mut total, mut temp) =
        (f[1].clone(), f[2].clone(), f[3].clone(), f[4].clone());
    let mut crit = f[5].clone();
    let saved_boot = f[6].clone();
    let boot_id = read_value(&input.proc.join("sys/kernel/random/boot_id"));
    let boot = if boot_id.is_empty() {
        b"none".to_vec()
    } else {
        boot_id
    };
    let now = input.now;

    let throttled =
        input.bar && runtime_control == b"auto" && at >= 0 && now - at >= 0 && now - at < 30_000;
    if !throttled {
        let want = pci_key(input.address);
        let reply = nvidia_smi(&QUERY).and_then(|output| parse_query(&output.stdout, &want));
        match reply {
            None => at = -1,
            Some([u, m, t, c]) => {
                (util, used, total, temp) = (u, m, t, c);
                at = now;
                // The shutdown threshold is fixed per GPU: once a probe
                // answers, keep it for the boot; retry failures with backoff.
                if saved_boot != boot {
                    crit = b"unknown".to_vec();
                    (retry, fails) = (0, 0);
                    (retry_text, fails_text) = (b"0".to_vec(), b"0".to_vec());
                }
                let answered = text::is_digits(&crit) || crit == b"none";
                if !answered && now >= retry {
                    crit = nvidia_smi(&LIMIT)
                        .filter(foamy_helper::child::Output::success)
                        .and_then(|output| parse_limit(&output.stdout, &want))
                        .unwrap_or_else(|| b"unknown".to_vec());
                    if crit == b"unknown" {
                        if fails < 7 {
                            fails += 1;
                            fails_text = fails.to_string().into_bytes();
                        }
                        retry = now.wrapping_add(
                            30_000i64.wrapping_mul(1i64.wrapping_shl((fails - 1) as u32)),
                        );
                    } else {
                        retry = 0;
                        fails_text = b"0".to_vec();
                    }
                    retry_text = retry.to_string().into_bytes();
                }
                let crit_text = if crit.is_empty() {
                    b"unknown".to_vec()
                } else {
                    crit.clone()
                };
                let mut line = Vec::new();
                let at_text = at.to_string().into_bytes();
                for (index, field) in [
                    &at_text,
                    &util,
                    &used,
                    &total,
                    &temp,
                    &crit_text,
                    &boot,
                    &retry_text,
                    &fails_text,
                ]
                .into_iter()
                .enumerate()
                {
                    if index > 0 {
                        line.push(b' ');
                    }
                    line.extend_from_slice(field);
                }
                line.push(b'\n');
                store.write("nvidia", &line);
            }
        }
    }

    if at >= 0 {
        // Unsupported readings and impossible values stay unknown.
        if text::is_digits(&util) && text::decimal(&util) <= 100 {
            result.busy = text::decimal(&util).to_string().into_bytes();
        }
        if text::is_digits(&used) && text::is_digits(&total) {
            let (used, total) = (text::decimal(&used), text::decimal(&total));
            if total > 0 && used <= total && total < 8_388_608 {
                result.vram_used = Some((
                    used.wrapping_mul(1_048_576).to_string().into_bytes(),
                    total.wrapping_mul(1_048_576).to_string().into_bytes(),
                ));
            }
        }
        if text::is_digits(&temp) {
            result.temp = text::decimal(&temp).to_string().into_bytes();
        }
        let critical = if text::is_digits(&crit) {
            crit
        } else {
            b"null".to_vec()
        };
        let mut thermals = b"[{\"temperature\":".to_vec();
        thermals.extend_from_slice(&result.temp);
        thermals.extend_from_slice(b",\"critical\":");
        thermals.extend_from_slice(&critical);
        thermals.extend_from_slice(b",\"label\":\"GPU\"}]");
        result.thermals = thermals;
    } else {
        result.error = "NVIDIA readings unavailable. Check nvidia-smi.";
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pci_keys_compare_across_spellings() {
        assert_eq!(pci_key(b"0000:01:00.0"), b"0:01:00.0");
        assert_eq!(pci_key(b" 00000000:01:00.0 "), b"0:01:00.0");
        assert_eq!(pci_key(b"00000001:0A:00.0"), b"1:0a:00.0");
        assert_eq!(pci_key(b"zz:01:00.0"), b"invalid:zz:01:00.0");
        assert_eq!(pci_key(b"01:00.0"), b"invalid:01:00.0");
    }

    #[test]
    fn query_rows_split_on_commas_only() {
        let want = pci_key(b"0000:01:00.0");
        let reply = b"00000001:01:00.0, 99, 10, 20, 90\n00000000:01:00.0, [Not Supported], 1178, 8192, 51\n";
        assert_eq!(
            parse_query(reply, &want),
            Some([
                b"na".to_vec(),
                b"1178".to_vec(),
                b"8192".to_vec(),
                b"51".to_vec()
            ])
        );
        assert_eq!(
            parse_query(b"00000000:01:00.0, 1, 2, 3, 4", &want),
            None,
            "no newline: not read"
        );
        assert_eq!(
            parse_query(b"00000000:01:00.0, 1\n", &want).unwrap()[1],
            b"na"
        );
    }

    #[test]
    fn limits_come_from_this_gpus_block() {
        let want = pci_key(b"0000:01:00.0");
        let xml = br#"<gpu id="00000001:01:00.0"><gpu_temp_max_threshold>70 C</gpu_temp_max_threshold></gpu><gpu id="00000000:01:00.0"><gpu_temp_max_threshold> 098  C </gpu_temp_max_threshold></gpu>"#;
        assert_eq!(parse_limit(xml, &want), Some(b"098".to_vec()));
        assert_eq!(parse_limit(br#"<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>N/A</gpu_temp_max_threshold></gpu>"#, &want), Some(b"none".to_vec()));
        assert_eq!(parse_limit(br#"<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>0 C</gpu_temp_max_threshold>"#, &want), Some(b"none".to_vec()));
        assert_eq!(parse_limit(br#"<gpu id="00000001:01:00.0">"#, &want), None);
        assert_eq!(
            parse_limit(br#"<gpu id="0000:01:00.0"><gpu_temp_max_threshold>98 F</gpu_temp_max_threshold><gpu_temp_max_threshold>97 C</gpu_temp_max_threshold>"#, &want),
            Some(b"97".to_vec())
        );
    }
}
