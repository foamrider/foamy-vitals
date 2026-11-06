//! Process rankings (full samples): the five heaviest processes by CPU since
//! the last full sample and by resident memory, from one pass over
//! `/proc/<pid>/stat`, with the working directory's name for each.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::fs;
use std::os::fd::OwnedFd;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

use rustix::fs::{Mode, OFlags};

use crate::text::{self, SMALL, awk_fields, awk_num, awk_print, awk_strnum};

/// Kernel threads (`PF_KTHREAD`) are not ranked.
const PF_KTHREAD: f64 = 2_097_152.0;

struct Ranked {
    pid: Vec<u8>,
    name: Vec<u8>,
    amount: f64,
}

/// awk's `keep()`: insertion into a top-five list, earlier entries winning ties.
fn keep(list: &mut Vec<Ranked>, pid: &[u8], name: &[u8], value: f64) {
    if list.len() == 5 && value <= list[4].amount {
        return;
    }
    let mut at = list.len().min(4);
    while at > 0 && list[at - 1].amount < value {
        at -= 1;
    }
    if list.len() == 5 {
        list.pop();
    }
    list.insert(
        at,
        Ranked {
            pid: pid.to_vec(),
            name: name.to_vec(),
            amount: value,
        },
    );
}

/// `$proc/[0-9]*/stat` in the shell's glob order: `<pid>/stat`, sorted as
/// bytes (as whole paths, so "1/stat" sorts before "1-x/stat" would not).
fn stat_files(proc: &Path) -> Vec<Vec<u8>> {
    let mut paths: Vec<Vec<u8>> = match fs::read_dir(proc) {
        Ok(entries) => entries
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let name = entry.file_name();
                let name = name.as_bytes();
                name.first()
                    .is_some_and(u8::is_ascii_digit)
                    .then(|| [name, b"/stat"].concat())
            })
            .collect(),
        Err(_) => Vec::new(),
    };
    paths.sort_unstable();
    paths
}

/// The first line of a file (what `getline` reads), into `buffer`.
fn first_line<'a>(root: &OwnedFd, relative: &[u8], buffer: &'a mut Vec<u8>) -> Option<&'a [u8]> {
    use std::io::Read;
    buffer.clear();
    let flags = OFlags::RDONLY | OFlags::CLOEXEC;
    let mut file = fs::File::from(rustix::fs::openat(root, relative, flags, Mode::empty()).ok()?);
    let mut chunk = [0u8; 4096];
    loop {
        let read = file.read(&mut chunk).ok()?;
        if read == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if chunk[..read].contains(&b'\n') || buffer.len() as u64 >= SMALL {
            break;
        }
    }
    let end = buffer
        .iter()
        .position(|&b| b == b'\n')
        .unwrap_or(buffer.len());
    Some(&buffer[..end])
}

/// The awk ranking pass: reads the previous baseline, returns the new
/// baseline and the ranking lines (`P\tpid\tname\tpct`, `M\tpid\tname\tkb`).
pub fn rank(
    proc: &Path,
    previous: Option<&[u8]>,
    baseline_only: bool,
    page_kb: f64,
) -> (Vec<u8>, Vec<u8>) {
    // Uninitialised in awk until the first "cpu " line: printed as "".
    let mut now_total: Option<f64> = None;
    let mut ncpu = 0u32;
    if let Ok(stat) = text::read_file(&proc.join("stat"), text::TABLE) {
        for line in stat.split(|&b| b == b'\n') {
            if line.starts_with(b"cpu ") {
                for value in awk_fields(line).iter().skip(1) {
                    now_total = Some(now_total.unwrap_or(0.0) + awk_num(value));
                }
            } else if line.len() > 3 && line.starts_with(b"cpu") && line[3].is_ascii_digit() {
                ncpu += 1;
            }
        }
    }

    let mut seen: HashMap<Vec<u8>, f64> = HashMap::new();
    let mut prev_total: &[u8] = b"";
    let mut had_state = false;
    if let Some(previous) = previous {
        for line in previous
            .strip_suffix(b"\n")
            .unwrap_or(previous)
            .split(|&b| b == b'\n')
        {
            if line.is_empty() && previous.is_empty() {
                break;
            }
            let f = awk_fields(line);
            let key = f.first().copied().unwrap_or(b"");
            let value = f.get(1).copied().unwrap_or(b"");
            if key == b"#TOTAL" {
                prev_total = value;
                continue;
            }
            seen.insert(key.to_vec(), awk_num(value));
            had_state = true;
        }
    }
    let delta = now_total.unwrap_or(0.0) - awk_num(prev_total);
    // `prev_total <= 0`: numeric for a number, a string comparison otherwise.
    let prev_not_positive = match awk_strnum(prev_total) {
        Some(value) => value <= 0.0,
        None => prev_total <= &b"0"[..],
    };
    if baseline_only || prev_not_positive || delta <= 0.0 || ncpu == 0 {
        had_state = false;
    }

    let mut out = format!("#TOTAL {}\n", now_total.map(awk_print).unwrap_or_default()).into_bytes();
    let mut cpu: Vec<Ranked> = Vec::new();
    let mut memory: Vec<Ranked> = Vec::new();
    let mut buffer = Vec::with_capacity(1024);
    // Every file is opened relative to one descriptor of `proc`.
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::CLOEXEC;
    let root = rustix::fs::openat(rustix::fs::CWD, proc, flags, Mode::empty()).ok();
    let files = if root.is_some() {
        stat_files(proc)
    } else {
        Vec::new()
    };
    for relative in files {
        let Some(root) = &root else { break };
        let Some(line) = first_line(root, &relative, &mut buffer) else {
            continue;
        };
        if line.is_empty() {
            continue;
        }
        let Some(open) = line.iter().position(|&b| b == b'(') else {
            continue;
        };
        let Some(close) = line.iter().rposition(|&b| b == b')') else {
            continue;
        };
        if open < 1 || close <= open {
            continue;
        }
        let pid = &line[..open - 1];
        let raw_name = &line[open + 1..close];
        let rest = line.get(close + 2..).unwrap_or(b"");
        let f = awk_fields(rest);
        let field = |i: usize| f.get(i - 1).map_or(0.0, |v| awk_num(v));
        if (field(7) / PF_KTHREAD).trunc() % 2.0 != 0.0 {
            continue;
        }
        let jif = field(12) + field(13);
        out.extend_from_slice(pid);
        out.push(b' ');
        out.extend_from_slice(awk_print(jif).as_bytes());
        out.push(b'\n');
        let mut name = text::clean(raw_name);
        if name.is_empty() {
            name = b"?".to_vec();
        }
        let rss = field(22) * page_kb;
        if rss > 0.0 {
            keep(&mut memory, pid, &name, rss);
        }
        if !had_state {
            continue;
        }
        let used = match seen.get(pid) {
            Some(before) => jif - before,
            None => jif,
        };
        if used > 0.0 {
            keep(&mut cpu, pid, &name, 100.0 * f64::from(ncpu) * used / delta);
        }
    }

    let mut ranking = Vec::new();
    for entry in &cpu {
        line(&mut ranking, b'P', entry, &text::awk_f(entry.amount, 1));
    }
    for entry in &memory {
        line(&mut ranking, b'M', entry, &text::awk_d(entry.amount));
    }
    (out, ranking)
}

fn line(out: &mut Vec<u8>, kind: u8, entry: &Ranked, value: &str) {
    out.push(kind);
    out.push(b'\t');
    out.extend_from_slice(&entry.pid);
    out.push(b'\t');
    out.extend_from_slice(&entry.name);
    out.push(b'\t');
    out.extend_from_slice(value.as_bytes());
    out.push(b'\n');
}

fn is_tab(b: u8) -> bool {
    b == b'\t'
}

/// The JSON lists from the ranking lines, as the shell assembled them:
/// `[{"name":…,"where":…,"pct":…},…]` and `[{…,"rssKb":…},…]`.
pub fn lists(proc: &Path, ranking: &[u8], home: Option<&[u8]>) -> (Vec<u8>, Vec<u8>) {
    let ranking = ranking.strip_suffix(b"\n").unwrap_or(ranking);
    let rows: Vec<Vec<&[u8]>> = ranking
        .split(|&b| b == b'\n')
        .map(|row| text::split_ifs(row, is_tab, 4))
        .collect();

    // Working directories worth naming: readlink only, nothing on the
    // target filesystem is touched.
    let mut place: HashMap<&[u8], Vec<u8>> = HashMap::new();
    for row in &rows {
        let (kind, pid) = (row[0], row[1]);
        if !(kind == b"P" || kind == b"M") || !text::is_digits(pid) {
            continue;
        }
        let mut link = proc.as_os_str().as_bytes().to_vec();
        link.push(b'/');
        link.extend_from_slice(pid);
        link.extend_from_slice(b"/cwd");
        let Ok(target) = fs::read_link(OsStr::from_bytes(&link)) else {
            continue;
        };
        let target = text::trim_by(target.as_os_str().as_bytes(), is_tab);
        if target.is_empty() || target == b"/" || Some(target) == home {
            continue;
        }
        if target.starts_with(b"/proc/")
            || target.starts_with(b"/sys/")
            || target.starts_with(b"/dev/")
        {
            continue;
        }
        let base = target.rsplit(|&b| b == b'/').next().unwrap_or(target);
        place.insert(pid, text::clean(base));
    }

    let mut procs = b"[".to_vec();
    let mut memory = b"[".to_vec();
    for row in &rows {
        let (kind, pid, name, value) = (row[0], row[1], row[2], row[3]);
        if !text::is_digits(pid) || value.is_empty() {
            continue;
        }
        let mut entry = b"{\"name\":\"".to_vec();
        entry.extend_from_slice(&text::clean(name));
        entry.extend_from_slice(b"\",\"where\":\"");
        if let Some(base) = place.get(pid) {
            entry.extend_from_slice(base);
        }
        entry.push(b'"');
        if kind == b"P" && is_decimal_number(value) {
            if procs.len() > 1 {
                procs.push(b',');
            }
            procs.extend_from_slice(&entry);
            procs.extend_from_slice(b",\"pct\":");
            procs.extend_from_slice(value);
            procs.push(b'}');
        } else if kind == b"M" && text::is_digits(value) {
            if memory.len() > 1 {
                memory.push(b',');
            }
            memory.extend_from_slice(&entry);
            memory.extend_from_slice(b",\"rssKb\":");
            memory.extend_from_slice(value);
            memory.push(b'}');
        }
    }
    procs.push(b']');
    memory.push(b']');
    (procs, memory)
}

/// `^[0-9]+(\.[0-9]+)?$`.
fn is_decimal_number(value: &[u8]) -> bool {
    match value.iter().position(|&b| b == b'.') {
        Some(dot) => text::is_digits(&value[..dot]) && text::is_digits(&value[dot + 1..]),
        None => text::is_digits(value),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ranked(amounts: &[f64]) -> Vec<f64> {
        let mut list = Vec::new();
        for (i, &amount) in amounts.iter().enumerate() {
            keep(&mut list, i.to_string().as_bytes(), b"x", amount);
        }
        list.iter().map(|entry| entry.amount).collect()
    }

    #[test]
    fn keeps_the_top_five_earliest_first() {
        assert_eq!(ranked(&[1.0, 5.0, 3.0]), vec![5.0, 3.0, 1.0]);
        assert_eq!(
            ranked(&[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 0.5]),
            vec![6.0, 5.0, 4.0, 3.0, 2.0]
        );
        let mut list = Vec::new();
        for pid in [b"a", b"b", b"c", b"d", b"e", b"f"] {
            keep(&mut list, pid, b"x", 1.0);
        }
        let pids: Vec<&[u8]> = list.iter().map(|entry| &entry.pid[..]).collect();
        assert_eq!(pids, vec![&b"a"[..], b"b", b"c", b"d", b"e"]);
    }

    #[test]
    fn ranking_lines_become_json() {
        let ranking = b"P\t12\tbusy\t80.0\nP\tx1\tbad\t1.0\nM\t12\tbusy\t4096\nM\t13\tmem\tinf\n";
        let (procs, memory) = lists(Path::new("/nonexistent"), ranking, None);
        assert_eq!(procs, br#"[{"name":"busy","where":"","pct":80.0}]"#);
        assert_eq!(memory, br#"[{"name":"busy","where":"","rssKb":4096}]"#);
        assert!(
            is_decimal_number(b"12.5") && !is_decimal_number(b"12.") && !is_decimal_number(b"inf")
        );
    }
}
