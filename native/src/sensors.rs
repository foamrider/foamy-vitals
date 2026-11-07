//! CPU temperatures (hwmon, thermal zones), the sysfs value readers the GPU
//! readings share, and the `nvidia-smi` calls and their parsing.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use foamy_helper::child::Bounded;

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

/// `degrees`: whole degrees from a millidegree file, or `null`.
pub fn degrees(path: &Path) -> Vec<u8> {
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
pub fn integer(path: &Path) -> Vec<u8> {
    let raw = read_value(path);
    if text::is_digits(&raw) {
        raw
    } else {
        b"null".to_vec()
    }
}

pub fn with_suffix(path: &Path, from: &str, to: &str) -> PathBuf {
    let bytes = path.as_os_str().as_bytes();
    let stem = bytes.strip_suffix(from.as_bytes()).unwrap_or(bytes);
    PathBuf::from(OsStr::from_bytes(&[stem, to.as_bytes()].concat()))
}

pub struct Temperatures {
    pub cpu_temp: Vec<u8>,
    pub cpu_temp_crit: Vec<u8>,
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

    Temperatures {
        cpu_temp,
        cpu_temp_crit,
    }
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

/// The `--query-gpu` reply's rows, in order: the PCI key and utilization,
/// used, total, temperature (each digits or `na`).
pub fn parse_query(stdout: &[u8]) -> Vec<(Vec<u8>, [Vec<u8>; 4])> {
    // `while read`: a last line without a newline is not read.
    let Some(end) = stdout.iter().rposition(|&b| b == b'\n') else {
        return Vec::new();
    };
    let complete = &stdout[..end];
    let mut rows = Vec::new();
    for line in complete.split(|&b| b == b'\n') {
        let line: Vec<u8> = line.iter().copied().filter(|&b| b != 0).collect();
        let mut fields = line.split(|&b| b == b',');
        let mut next = || fields.next().unwrap_or(b"");
        let (bus, util, used, total, temp) = (next(), next(), next(), next(), next());
        rows.push((
            pci_key(bus),
            [
                nv_field(util),
                nv_field(used),
                nv_field(total),
                nv_field(temp),
            ],
        ));
    }
    rows
}

/// `<gpu_temp_max_threshold>[[:space:]]*([0-9]{1,6})[[:space:]]*C[[:space:]]*<`, leftmost.
fn threshold(block: &[u8]) -> Option<&[u8]> {
    const TAG: &[u8] = b"<gpu_temp_max_threshold>";
    let mut from = 0;
    while let Some(at) = find(&block[from..], TAG) {
        let start = from + at + TAG.len();
        from = from + at + 1;
        let rest = text::trim_start_by(&block[start..], text::is_c_space);
        let digits = rest.iter().take_while(|b| b.is_ascii_digit()).count();
        if !(1..=6).contains(&digits) {
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

/// The shutdown thresholds in `nvidia-smi -q -x -d TEMPERATURE`, block by
/// `<gpu id="…">` block, in order: the PCI key and the limit in °C, or
/// `none` (the GPU reports none). A GPU listed twice keeps its last block.
pub fn parse_limits(xml: &[u8]) -> Vec<(Vec<u8>, Vec<u8>)> {
    const OPEN: &[u8] = b"<gpu id=\"";
    // $(…): NUL bytes and trailing newlines dropped.
    let mut xml: Vec<u8> = xml.iter().copied().filter(|&b| b != 0).collect();
    while xml.last() == Some(&b'\n') {
        xml.pop();
    }
    let mut limits = Vec::new();
    let mut rest = &xml[..];
    while let Some(at) = find(rest, OPEN) {
        rest = &rest[at + OPEN.len()..];
        let id_end = rest.iter().position(|&b| b == b'"').unwrap_or(rest.len());
        let key = pci_key(&rest[..id_end]);
        let block = &rest[..find(rest, b"</gpu>").unwrap_or(rest.len())];
        let limit = match threshold(block).map(text::decimal) {
            Some(degrees) if degrees > 0 => degrees.to_string().into_bytes(),
            _ => b"none".to_vec(),
        };
        limits.push((key, limit));
    }
    limits
}

const NVIDIA_TIMEOUT: Duration = Duration::from_millis(1500);
const NVIDIA_OUTPUT: usize = 1 << 20;
pub const QUERY: [&str; 2] = [
    "--query-gpu=pci.bus_id,utilization.gpu,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
];
pub const LIMIT: [&str; 4] = ["-q", "-x", "-d", "TEMPERATURE"];

/// nvidia-smi with the old bounds: 1.5 s, `LC_ALL=C`, stderr dropped.
pub fn nvidia_smi(args: &[&str]) -> Option<foamy_helper::child::Output> {
    Bounded::new("nvidia-smi")
        .args(args)
        .env("LC_ALL", "C")
        .timeout(NVIDIA_TIMEOUT)
        .max_output(NVIDIA_OUTPUT)
        .run()
        .ok()
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
        let reply = b"00000001:01:00.0, 99, 10, 20, 90\n00000000:01:00.0, [Not Supported], 1178, 8192, 51\n";
        let rows = parse_query(reply);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].0, pci_key(b"0001:01:00.0"));
        assert_eq!(
            rows[1],
            (
                pci_key(b"0000:01:00.0"),
                [
                    b"na".to_vec(),
                    b"1178".to_vec(),
                    b"8192".to_vec(),
                    b"51".to_vec()
                ]
            )
        );
        assert!(
            parse_query(b"00000000:01:00.0, 1, 2, 3, 4").is_empty(),
            "no newline: not read"
        );
        assert_eq!(parse_query(b"00000000:01:00.0, 1\n")[0].1[1], b"na");
    }

    #[test]
    fn limits_come_from_each_gpus_block() {
        let xml = br#"<gpu id="00000001:01:00.0"><gpu_temp_max_threshold>70 C</gpu_temp_max_threshold></gpu><gpu id="00000000:01:00.0"><gpu_temp_max_threshold> 098  C </gpu_temp_max_threshold></gpu>"#;
        assert_eq!(
            parse_limits(xml),
            vec![
                (pci_key(b"0001:01:00.0"), b"70".to_vec()),
                (pci_key(b"0000:01:00.0"), b"98".to_vec())
            ]
        );
        let limit = |xml: &[u8]| parse_limits(xml).pop().map(|(_, limit)| limit);
        assert_eq!(limit(br#"<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>N/A</gpu_temp_max_threshold></gpu>"#), Some(b"none".to_vec()));
        assert_eq!(limit(br#"<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>0 C</gpu_temp_max_threshold>"#), Some(b"none".to_vec()));
        assert_eq!(limit(br#"<gpu id="00000000:01:00.0"><gpu_temp_max_threshold>1234567 C</gpu_temp_max_threshold>"#), Some(b"none".to_vec()));
        assert_eq!(limit(b"<nvidia_smi_log/>"), None);
        assert_eq!(
            limit(br#"<gpu id="0000:01:00.0"><gpu_temp_max_threshold>98 F</gpu_temp_max_threshold><gpu_temp_max_threshold>97 C</gpu_temp_max_threshold>"#),
            Some(b"97".to_vec())
        );
    }
}
