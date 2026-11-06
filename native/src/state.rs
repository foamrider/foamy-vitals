//! The state directory and the files kept in it between calls.

use std::collections::HashMap;
use std::ffi::OsString;
use std::fs::{self, DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use rustix::fs::OFlags;

use crate::text::{self, SMALL};

/// Where samples keep their counters: `$XDG_RUNTIME_DIR/foamy-vitals-$EUID`,
/// else `/tmp/foamy-vitals-$EUID`, the first that is (or can be made) a real
/// directory owned by this user; outside a private runtime directory it must
/// also have mode 700. `None`: nowhere safe.
pub fn choose_dir(runtime: Option<OsString>, euid: u32) -> Option<PathBuf> {
    let runtime = runtime.unwrap_or_default();
    for root in [runtime.clone(), OsString::from("/tmp")] {
        if root.is_empty() || !Path::new(&root).is_dir() {
            continue;
        }
        let mut name = root.clone();
        name.push(format!("/foamy-vitals-{euid}"));
        let candidate = PathBuf::from(name);
        if !candidate.is_dir() && DirBuilder::new().mode(0o700).create(&candidate).is_ok() {
            // mkdir -m 700: exactly 700, whatever the umask.
            let _ = fs::set_permissions(&candidate, fs::Permissions::from_mode(0o700));
        }
        let Ok(link) = fs::symlink_metadata(&candidate) else {
            continue;
        };
        if !link.is_dir() || link.uid() != euid {
            continue;
        }
        let root_owned = fs::metadata(&root).is_ok_and(|m| m.uid() == euid);
        let root_link = fs::symlink_metadata(&root).is_ok_and(|m| m.file_type().is_symlink());
        if (root != runtime || !root_owned || root_link) && link.mode() & 0o777 != 0o700 {
            continue;
        }
        return Some(candidate);
    }
    None
}

/// The state files of one call: in the state directory, or (nowhere safe)
/// only in memory, so the readings still come out and the deltas start over.
pub enum Store {
    Dir(PathBuf),
    Memory(HashMap<String, Vec<u8>>),
}

impl Store {
    pub fn dir(&self) -> Option<&Path> {
        match self {
            Store::Dir(dir) => Some(dir),
            Store::Memory(_) => None,
        }
    }

    pub fn read(&self, name: &str) -> Option<Vec<u8>> {
        match self {
            Store::Dir(dir) => text::read_file(&dir.join(name), SMALL).ok(),
            Store::Memory(files) => files.get(name).cloned(),
        }
    }

    /// Replaces a file (`printf … > file`, never through a symlink).
    pub fn write(&mut self, name: &str, data: &[u8]) -> bool {
        match self {
            Store::Dir(dir) => OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .custom_flags(OFlags::NOFOLLOW.bits() as i32)
                .open(dir.join(name))
                .and_then(|mut file| file.write_all(data))
                .is_ok(),
            Store::Memory(files) => {
                files.insert(name.to_string(), data.to_vec());
                true
            }
        }
    }

    /// `[[ -s file ]]`.
    pub fn nonempty(&self, name: &str) -> bool {
        match self {
            Store::Dir(dir) => fs::metadata(dir.join(name)).is_ok_and(|m| m.len() > 0),
            Store::Memory(files) => files.get(name).is_some_and(|data| !data.is_empty()),
        }
    }
}

/// The generations and cache bookkeeping in `current`. The two times stay
/// the text they were read as until a sample replaces them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pointer {
    pub cpu_gen: u8,
    pub proc_gen: u8,
    pub proc_at: Vec<u8>,
    pub cache_gen: u8,
    pub cache_at: Vec<u8>,
    pub cache_mode: Vec<u8>,
}

impl Default for Pointer {
    fn default() -> Self {
        Pointer {
            cpu_gen: 0,
            proc_gen: 0,
            proc_at: b"0".to_vec(),
            cache_gen: 0,
            cache_at: b"-1".to_vec(),
            cache_mode: b"none".to_vec(),
        }
    }
}

fn generation(field: &[u8]) -> Option<u8> {
    match field {
        b"0" => Some(0),
        b"1" => Some(1),
        _ => None,
    }
}

fn signed_digits(field: &[u8]) -> bool {
    text::is_digits(field.strip_prefix(b"-").unwrap_or(field))
}

impl Pointer {
    /// `read -r p1 … p6 _ < current`, kept only when every field is valid.
    pub fn parse(data: &[u8]) -> Pointer {
        let (line, _) = text::first_line(data);
        let f = text::split_ifs(&line, text::is_ifs, 7);
        match (generation(f[0]), generation(f[1]), generation(f[3])) {
            (Some(cpu_gen), Some(proc_gen), Some(cache_gen))
                if text::is_digits(f[2]) && signed_digits(f[4]) =>
            {
                Pointer {
                    cpu_gen,
                    proc_gen,
                    proc_at: f[2].to_vec(),
                    cache_gen,
                    cache_at: f[4].to_vec(),
                    cache_mode: if f[5].is_empty() {
                        b"none".to_vec()
                    } else {
                        f[5].to_vec()
                    },
                }
            }
            _ => Pointer::default(),
        }
    }

    pub fn proc_at(&self) -> i64 {
        text::bash_int(&self.proc_at)
    }

    pub fn cache_at(&self) -> i64 {
        text::bash_int(&self.cache_at)
    }

    pub fn render(&self) -> Vec<u8> {
        let mut out = format!("{} {} ", self.cpu_gen, self.proc_gen).into_bytes();
        out.extend_from_slice(&self.proc_at);
        out.extend_from_slice(format!(" {} ", self.cache_gen).as_bytes());
        out.extend_from_slice(&self.cache_at);
        out.push(b' ');
        out.extend_from_slice(&self.cache_mode);
        out.push(b'\n');
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pointer_round_trip_and_validation() {
        let pointer = Pointer::parse(b"1 0 5000 1 4000 bar\n");
        assert_eq!(pointer.render(), b"1 0 5000 1 4000 bar\n");
        assert_eq!(Pointer::parse(b"garbage\n"), Pointer::default());
        assert_eq!(Pointer::parse(b"2 0 0 0 0 full\n"), Pointer::default());
        assert_eq!(Pointer::parse(b"0 0 0 0 -1").cache_mode, b"none");
        assert_eq!(Pointer::parse(b"0 1 7 1 -5 full extra").cache_at(), -5);
        assert_eq!(
            Pointer::parse(b"0 1 010 1 -5 full").render(),
            b"0 1 010 1 -5 full\n"
        );
    }

    #[test]
    fn memory_store() {
        let mut store = Store::Memory(HashMap::new());
        assert!(!store.nonempty("cpu.1"));
        assert!(store.write("cpu.1", b"x"));
        assert!(store.nonempty("cpu.1"));
        assert_eq!(store.read("cpu.1").as_deref(), Some(&b"x"[..]));
    }
}
