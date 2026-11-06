//! Processor load, memory and network counters: one pass over `/proc/stat`,
//! `/proc/meminfo`, `/proc/net/route` and `/proc/net/dev`, with the arithmetic
//! and number formatting of the awk program it replaces.

use std::collections::HashMap;
use std::io::ErrorKind;
use std::path::Path;

use crate::text::{self, TABLE, awk_fields, awk_num, awk_print, awk_strnum};

/// What one sample reports.
#[derive(Debug, PartialEq)]
pub struct Reading {
    /// `null` or a percentage.
    pub cpu: Vec<u8>,
    /// Comma-separated per-core percentages (`null` for unknown), or empty.
    pub cores: Vec<u8>,
    pub mem_total_kb: Vec<u8>,
    pub mem_used_kb: i64,
    /// `{"interface":…,"rxBytes":…,"txBytes":…}` or `null`.
    pub network: Vec<u8>,
}

/// The counters' result: the reading, and the new baseline (`None` when
/// `/proc/stat` had no `cpu` line, so the old file stays as it was).
pub struct Sample {
    pub reading: Reading,
    pub baseline: Option<Vec<u8>>,
}

enum Input {
    Lines(Vec<u8>),
    Skipped,
    Fatal,
}

/// gawk with a data file on the command line: an unopenable file is fatal
/// (no `END`), a directory is skipped with a warning.
fn input(path: &Path) -> Input {
    match text::read_file(path, TABLE) {
        Ok(data) => Input::Lines(data),
        Err(error) if error.kind() == ErrorKind::IsADirectory => Input::Skipped,
        Err(_) => Input::Fatal,
    }
}

fn lines(data: &[u8]) -> impl Iterator<Item = &[u8]> {
    let data = data.strip_suffix(b"\n").unwrap_or(data);
    data.split(|&b| b == b'\n')
        .filter(move |_| !data.is_empty())
}

/// An awk array subscript for a number.
fn subscript(n: f64) -> String {
    if n == n.trunc() && n.is_finite() {
        text::awk_d(n)
    } else {
        text::fmt_g(n, 6)
    }
}

/// The awk truth value of `iface` (`!iface`): a field that looks numeric is
/// true when non-zero, any other string when non-empty.
fn truthy(value: &[u8]) -> bool {
    match awk_strnum(value) {
        Some(n) => n != 0.0,
        None => !value.is_empty(),
    }
}

pub fn sample(proc: &Path, previous: Option<&[u8]>) -> Sample {
    let mut seen: HashMap<&[u8], (f64, f64)> = HashMap::new();
    if let Some(previous) = previous {
        for line in lines(previous) {
            let f = awk_fields(line);
            let key = f.first().copied().unwrap_or(b"");
            let total = f.get(1).map_or(0.0, |v| awk_num(v));
            let idle = f.get(2).map_or(0.0, |v| awk_num(v));
            seen.insert(key, (total, idle));
        }
    }

    let mut baseline: Option<Vec<u8>> = None;
    let mut overall = -1.0;
    let mut core: HashMap<String, f64> = HashMap::new();
    let mut ncore = 0.0f64;
    let mut memtotal: &[u8] = b"";
    let mut memavail: &[u8] = b"";
    let mut iface: Vec<u8> = Vec::new();
    let mut metric = 0.0;
    let (mut rx, mut tx, mut found): (Vec<u8>, Vec<u8>, bool) = (Vec::new(), Vec::new(), false);

    let names = ["stat", "meminfo", "net/route", "net/dev"];
    let files: Vec<Input> = names.iter().map(|name| input(&proc.join(name))).collect();
    let mut fatal = false;
    for (index, file) in files.iter().enumerate() {
        let data = match file {
            Input::Lines(data) => data,
            Input::Skipped => continue,
            Input::Fatal => {
                fatal = true;
                break;
            }
        };
        for line in lines(data) {
            let f = awk_fields(line);
            let field = |i: usize| f.get(i).copied().unwrap_or(b"");
            match index {
                0 if line.starts_with(b"cpu") => {
                    let key = field(0);
                    let idle = awk_num(field(4)) + awk_num(field(5));
                    let total: f64 = f.iter().skip(1).map(|v| awk_num(v)).sum();
                    let out = baseline.get_or_insert_with(Vec::new);
                    out.extend_from_slice(key);
                    out.extend_from_slice(
                        format!(" {} {}\n", awk_print(total), awk_print(idle)).as_bytes(),
                    );
                    let mut pct = -1.0;
                    if let Some(&(ptotal, pidle)) = seen.get(key) {
                        let dt = total - ptotal;
                        let mut di = idle - pidle;
                        if di < 0.0 {
                            di = 0.0;
                        }
                        if dt > 0.0 {
                            pct = ((100.0 * (dt - di) + dt / 2.0) / dt).trunc();
                            pct = pct.clamp(0.0, 100.0);
                        }
                    }
                    if key == b"cpu" {
                        overall = pct;
                    } else {
                        let n = awk_num(&key[3..]);
                        core.insert(subscript(n), pct);
                        if n + 1.0 > ncore {
                            ncore = n + 1.0;
                        }
                    }
                }
                1 => {
                    if field(0) == b"MemTotal:" {
                        memtotal = field(1);
                    } else if field(0) == b"MemAvailable:" {
                        memavail = field(1);
                    }
                }
                2 => {
                    if field(1) == b"00000000"
                        && field(0) != b"lo"
                        && (!truthy(&iface) || awk_num(field(6)) < metric)
                    {
                        iface = field(0).to_vec();
                        metric = awk_num(field(6));
                    }
                }
                3 => {
                    let Some(at) = line.iter().position(|&b| b == b':') else {
                        continue;
                    };
                    if iface.is_empty() {
                        continue;
                    }
                    let name: Vec<u8> = line[..at]
                        .iter()
                        .copied()
                        .filter(|&b| !text::is_c_space(b))
                        .collect();
                    if name != iface {
                        continue;
                    }
                    let v = awk_fields(&line[at + 1..]);
                    rx = v.first().map(|v| v.to_vec()).unwrap_or_default();
                    tx = v.get(8).map(|v| v.to_vec()).unwrap_or_default();
                    found = true;
                }
                _ => {}
            }
        }
    }

    let mut reading = Reading {
        cpu: b"null".to_vec(),
        cores: Vec::new(),
        mem_total_kb: b"0".to_vec(),
        mem_used_kb: 0,
        network: b"null".to_vec(),
    };
    if !fatal {
        // END, then the shell's checks of what awk printed.
        if overall >= 0.0 {
            reading.cpu = text::awk_print(overall).into_bytes();
        }
        let mut cores = Vec::new();
        let mut i = 0u32;
        // An absurd core number would make awk print millions of nulls.
        while f64::from(i) < ncore && i < 1 << 16 {
            if i > 0 {
                cores.push(b',');
            }
            match core.get(&i.to_string()) {
                Some(&pct) if pct >= 0.0 => cores.extend_from_slice(awk_print(pct).as_bytes()),
                _ => cores.extend_from_slice(b"null"),
            }
            i += 1;
        }
        reading.cores = cores;
        let total = text::awk_d(awk_num(memtotal)).into_bytes();
        let avail = text::awk_d(awk_num(memavail)).into_bytes();
        if text::is_digits(&total) && text::is_digits(&avail) {
            let used = text::bash_int(&total).wrapping_sub(text::bash_int(&avail));
            reading.mem_used_kb = used.max(0);
            reading.mem_total_kb = total;
        }
        if found {
            let name = text::clean(&iface);
            let name: Vec<u8> = name.into_iter().filter(|&b| !text::is_c_space(b)).collect();
            let rx = text::awk_f(awk_num(&rx), 0);
            let tx = text::awk_f(awk_num(&tx), 0);
            if !name.is_empty() && text::is_digits(rx.as_bytes()) && text::is_digits(tx.as_bytes())
            {
                let mut network = b"{\"interface\":\"".to_vec();
                network.extend_from_slice(&name);
                network.extend_from_slice(
                    format!("\",\"rxBytes\":{rx},\"txBytes\":{tx}}}").as_bytes(),
                );
                reading.network = network;
            }
        }
    }
    Sample { reading, baseline }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn tree(stat: &str) -> tempdir::Dir {
        let dir = tempdir::Dir::new();
        fs::create_dir(dir.0.join("net")).unwrap();
        fs::write(dir.0.join("stat"), stat).unwrap();
        fs::write(
            dir.0.join("meminfo"),
            "MemTotal: 100 kB\nMemAvailable: 40 kB\n",
        )
        .unwrap();
        fs::write(
            dir.0.join("net/route"),
            "Iface\tDestination\neth0\t00000000\t0\t0\t0\t0\t5\neth1\t00000000\t0\t0\t0\t0\t1\n",
        )
        .unwrap();
        fs::write(dir.0.join("net/dev"), "eth1:12 0 0 0 0 0 0 0 34\n").unwrap();
        dir
    }

    mod tempdir {
        pub struct Dir(pub std::path::PathBuf);
        impl Dir {
            pub fn new() -> Dir {
                use std::sync::atomic::{AtomicUsize, Ordering};
                static NEXT: AtomicUsize = AtomicUsize::new(0);
                let path = std::env::temp_dir().join(format!(
                    "foamy-vitals-cpu-{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
                std::fs::create_dir(&path).unwrap();
                Dir(path)
            }
        }
        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }

    #[test]
    fn load_is_a_delta_against_the_baseline() {
        let first = sample(
            &tree("cpu  100 0 0 1000\ncpu0 50 0 0 500\ncpu1 50 0 0 500\n").0,
            None,
        );
        assert_eq!(first.reading.cpu, b"null");
        assert_eq!(first.reading.cores, b"null,null");
        let baseline = first.baseline.unwrap();
        assert_eq!(baseline, b"cpu 1100 1000\ncpu0 550 500\ncpu1 550 500\n");
        let second = sample(
            &tree("cpu  200 0 0 1100\ncpu0 100 0 0 550\ncpu1 100 0 0 550\n").0,
            Some(&baseline),
        );
        assert_eq!(second.reading.cpu, b"50");
        assert_eq!(second.reading.cores, b"50,50");
        assert_eq!(second.reading.mem_total_kb, b"100");
        assert_eq!(second.reading.mem_used_kb, 60);
        assert_eq!(
            second.reading.network,
            br#"{"interface":"eth1","rxBytes":12,"txBytes":34}"#
        );
    }

    #[test]
    fn a_missing_table_ends_the_pass_like_gawk() {
        let dir = tree("cpu  1 0 0 1\n");
        fs::remove_file(dir.0.join("net/route")).unwrap();
        let result = sample(&dir.0, None);
        assert!(
            result.baseline.is_some(),
            "lines before the failure were written"
        );
        assert_eq!(result.reading.mem_total_kb, b"0");
        assert_eq!(result.reading.network, b"null");
    }
}
