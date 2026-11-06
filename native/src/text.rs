//! The text rules of the helpers this binary replaces, kept exactly: how
//! Bash's `read` splits a line, how gawk turns strings into numbers and
//! numbers into text, and how Python's `json.dumps`, `str.strip` and
//! `str.splitlines` treat text. Every function here is pure.

use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

/// Reads at most `cap` bytes of a file (following symlinks, as `bash <file`
/// and Python's `open` do).
pub fn read_file(path: &Path, cap: u64) -> io::Result<Vec<u8>> {
    let mut data = Vec::with_capacity(256);
    File::open(path)?.take(cap).read_to_end(&mut data)?;
    Ok(data)
}

/// Small sysfs/procfs attributes and state files.
pub const SMALL: u64 = 1 << 20;
/// Whole procfs tables (`/proc/stat` on a many-core machine, `/proc/net/dev`).
pub const TABLE: u64 = 16 << 20;

// --- Bash -----------------------------------------------------------------

/// The first line of `data` the way `read` sees it: up to the first newline,
/// NUL bytes dropped. The flag is read's status: a newline ended the line.
pub fn first_line(data: &[u8]) -> (Vec<u8>, bool) {
    let (line, complete) = match data.iter().position(|&b| b == b'\n') {
        Some(end) => (&data[..end], true),
        None => (data, false),
    };
    (line.iter().copied().filter(|&b| b != 0).collect(), complete)
}

/// `IFS` whitespace of the default `IFS` (space, tab, newline).
pub fn is_ifs(b: u8) -> bool {
    b == b' ' || b == b'\t' || b == b'\n'
}

/// `read -r v1 … vN` with an `IFS` made only of whitespace characters
/// (`ifs`): leading and trailing `IFS` runs are dropped, runs separate
/// fields, and the last variable gets the rest of the line. Missing fields
/// are empty.
pub fn split_ifs(line: &[u8], ifs: fn(u8) -> bool, count: usize) -> Vec<&[u8]> {
    let mut fields = Vec::with_capacity(count);
    let mut rest = trim_start_by(line, ifs);
    for index in 0..count {
        if index + 1 == count {
            fields.push(trim_end_by(rest, ifs));
            break;
        }
        let end = rest.iter().position(|&b| ifs(b)).unwrap_or(rest.len());
        fields.push(&rest[..end]);
        rest = trim_start_by(&rest[end..], ifs);
    }
    fields
}

/// `read -r var < path`: the first line with default-`IFS` whitespace
/// trimmed, or `None` when the file can't be opened or read (a directory).
/// Callers that write `read … || [[ -n $var ]] || continue` only skip an
/// empty value when no newline ended it, which [`read_var`] reports too.
pub fn read_var(path: &Path) -> Option<(Vec<u8>, bool)> {
    let data = read_file(path, SMALL).ok()?;
    let (line, complete) = first_line(&data);
    Some((trim_by(&line, is_ifs).to_vec(), complete))
}

/// [`read_var`] where a missing or unreadable file reads as empty.
pub fn read_value(path: &Path) -> Vec<u8> {
    read_var(path).map(|(value, _)| value).unwrap_or_default()
}

/// `read -r a b c … _ < path`: the first `count` default-`IFS` fields.
pub fn read_fields(path: &Path, count: usize) -> Option<Vec<Vec<u8>>> {
    let data = read_file(path, SMALL).ok()?;
    let (line, _) = first_line(&data);
    Some(
        split_ifs(&line, is_ifs, count + 1)
            .into_iter()
            .take(count)
            .map(<[u8]>::to_vec)
            .collect(),
    )
}

pub fn trim_start_by(data: &[u8], drop: fn(u8) -> bool) -> &[u8] {
    let start = data.iter().position(|&b| !drop(b)).unwrap_or(data.len());
    &data[start..]
}

pub fn trim_end_by(data: &[u8], drop: fn(u8) -> bool) -> &[u8] {
    let end = data.iter().rposition(|&b| !drop(b)).map_or(0, |i| i + 1);
    &data[..end]
}

pub fn trim_by(data: &[u8], drop: fn(u8) -> bool) -> &[u8] {
    trim_end_by(trim_start_by(data, drop), drop)
}

/// `[[:space:]]` in the C locale.
pub fn is_c_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// `[[:cntrl:]]` in the C locale.
pub fn is_c_cntrl(b: u8) -> bool {
    b < 0x20 || b == 0x7f
}

/// `^[0-9]+$`.
pub fn is_digits(data: &[u8]) -> bool {
    !data.is_empty() && data.iter().all(u8::is_ascii_digit)
}

/// Untrusted text for a JSON string, as `clean()` and the awk `gsub` did:
/// control bytes, quotes and backslashes dropped; every other byte kept.
pub fn clean(data: &[u8]) -> Vec<u8> {
    data.iter()
        .copied()
        .filter(|&b| !is_c_cntrl(b) && b != b'"' && b != b'\\')
        .collect()
}

/// A Bash integer constant (`$((x))` with `x` matching `-?[0-9]+`): decimal,
/// or octal with a leading zero when every digit is octal; 64-bit wrapping.
pub fn bash_int(data: &[u8]) -> i64 {
    let (negative, digits) = match data.split_first() {
        Some((b'-', rest)) => (true, rest),
        _ => (false, data),
    };
    let base = if digits.len() > 1
        && digits[0] == b'0'
        && digits.iter().all(|b| (b'0'..=b'7').contains(b))
    {
        8
    } else {
        10
    };
    let value = digits.iter().fold(0i64, |value, &b| {
        value
            .wrapping_mul(base)
            .wrapping_add(i64::from(b.wrapping_sub(b'0')))
    });
    if negative {
        value.wrapping_neg()
    } else {
        value
    }
}

/// `10#digits`: always decimal, 64-bit wrapping.
pub fn decimal(digits: &[u8]) -> i64 {
    digits.iter().fold(0i64, |value, &b| {
        value
            .wrapping_mul(10)
            .wrapping_add(i64::from(b.wrapping_sub(b'0')))
    })
}

// --- gawk (C locale) ------------------------------------------------------

/// Default field splitting (`$1…$NF`, `split(s, f, " ")`): runs of spaces,
/// tabs and newlines, none leading or trailing.
pub fn awk_fields(line: &[u8]) -> Vec<&[u8]> {
    line.split(|&b| is_ifs(b))
        .filter(|field| !field.is_empty())
        .collect()
}

/// The longest prefix of `data` that `strtod` accepts as a decimal number.
fn decimal_prefix(data: &[u8]) -> &[u8] {
    let mut end = 0;
    if matches!(data.first(), Some(b'+' | b'-')) {
        end = 1;
    }
    let int_start = end;
    while data.get(end).is_some_and(u8::is_ascii_digit) {
        end += 1;
    }
    let mut digits = end - int_start;
    if data.get(end) == Some(&b'.') {
        let mut frac = end + 1;
        while data.get(frac).is_some_and(u8::is_ascii_digit) {
            frac += 1;
        }
        digits += frac - end - 1;
        if digits > 0 {
            end = frac;
        }
    }
    if digits == 0 {
        return &[];
    }
    if matches!(data.get(end), Some(b'e' | b'E')) {
        let mut exp = end + 1;
        if matches!(data.get(exp), Some(b'+' | b'-')) {
            exp += 1;
        }
        let exp_digits = exp;
        while data.get(exp).is_some_and(u8::is_ascii_digit) {
            exp += 1;
        }
        if exp > exp_digits {
            end = exp;
        }
    }
    &data[..end]
}

fn awk_magic(data: &[u8]) -> Option<f64> {
    if data.len() != 4 || !matches!(data[0], b'+' | b'-') {
        return None;
    }
    let word = data[1..].to_ascii_lowercase();
    let value = match &word[..] {
        b"inf" => f64::INFINITY,
        b"nan" => f64::NAN,
        _ => return None,
    };
    Some(if data[0] == b'-' { -value } else { value })
}

/// gawk's string-to-number conversion (`x + 0`): leading blanks skipped,
/// the longest decimal prefix, `+inf`/`-nan` only with a sign, no hex.
pub fn awk_num(data: &[u8]) -> f64 {
    let text = trim_by(data, is_c_space);
    if let Some(value) = awk_magic(text) {
        return value;
    }
    let unsigned = match text.first() {
        Some(b'+' | b'-') => &text[1..],
        _ => text,
    };
    if unsigned.len() > 1 && unsigned[0] == b'0' && matches!(unsigned[1], b'x' | b'X') {
        return 0.0;
    }
    let prefix = decimal_prefix(text);
    std::str::from_utf8(prefix)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0)
}

/// Whether gawk treats a field as a number in comparisons (a "strnum": the
/// whole field, blanks aside, is a number), and its value.
pub fn awk_strnum(data: &[u8]) -> Option<f64> {
    let text = trim_by(data, is_c_space);
    if text.is_empty() {
        return None;
    }
    if let Some(value) = awk_magic(text) {
        return Some(value);
    }
    let unsigned = match text.first() {
        Some(b'+' | b'-') => &text[1..],
        _ => text,
    };
    if unsigned.len() > 1 && unsigned[0] == b'0' && matches!(unsigned[1], b'x' | b'X') {
        return None;
    }
    (decimal_prefix(text).len() == text.len()).then(|| awk_num(text))
}

fn nonfinite(x: f64) -> &'static str {
    if x.is_nan() {
        if x.is_sign_negative() { "-nan" } else { "+nan" }
    } else if x > 0.0 {
        "+inf"
    } else {
        "-inf"
    }
}

/// C's `%.<precision>g`.
pub fn fmt_g(x: f64, precision: usize) -> String {
    if !x.is_finite() {
        return nonfinite(x).trim_start_matches('+').to_string();
    }
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0".into()
        } else {
            "0".into()
        };
    }
    let precision = precision.max(1);
    let sci = format!("{:.*e}", precision - 1, x);
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let strip = |s: &str| -> String {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            s.to_string()
        }
    };
    if exponent < -4 || exponent >= precision as i32 {
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}e{sign}{:02}", strip(mantissa), exponent.unsigned_abs())
    } else {
        let decimals = (precision as i32 - 1 - exponent).max(0) as usize;
        strip(&format!("{x:.decimals$}"))
    }
}

/// gawk's `print x` for a number: integral values as integers, others with
/// `OFMT` (`%.6g`).
pub fn awk_print(x: f64) -> String {
    if !x.is_finite() {
        return nonfinite(x).into();
    }
    if x == x.trunc() {
        if x == 0.0 {
            return "0".into();
        }
        return format!("{x:.0}");
    }
    fmt_g(x, 6)
}

/// gawk's `printf "%d"`: truncated toward zero.
pub fn awk_d(x: f64) -> String {
    if !x.is_finite() {
        return nonfinite(x).into();
    }
    let t = x.trunc();
    if t == 0.0 {
        return "0".into();
    }
    format!("{t:.0}")
}

/// gawk's `printf "%.<n>f"`.
pub fn awk_f(x: f64, decimals: usize) -> String {
    if !x.is_finite() {
        return nonfinite(x).into();
    }
    format!("{x:.decimals$}")
}

// --- Python ---------------------------------------------------------------

/// `str.isspace()` for one character.
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()`.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// `str.splitlines()`.
pub fn py_splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        let breaks = matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !breaks {
            continue;
        }
        lines.push(&s[start..at]);
        let mut next = at + c.len_utf8();
        if c == '\r' && chars.peek().map(|&(_, c)| c) == Some('\n') {
            chars.next();
            next += 1;
        }
        start = next;
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// Python's text-mode read of bytes (UTF-8, strict; universal newlines).
pub fn py_text(data: Vec<u8>) -> Result<String, std::string::FromUtf8Error> {
    let text = String::from_utf8(data)?;
    if !text.contains('\r') {
        return Ok(text);
    }
    Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// `str.isdecimal()` (ASCII digits; see the contract).
pub fn py_isdecimal(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// `re.fullmatch(r"\s*(\d+)\s*" + unit + r"\s*", s)` → the number.
pub fn py_integer(s: &str, unit: &str) -> Option<u64> {
    let s = s.trim_start_matches(py_isspace);
    let end = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    if end == 0 {
        return None;
    }
    let (digits, rest) = s.split_at(end);
    let rest = rest.trim_start_matches(py_isspace);
    let rest = rest.strip_prefix(unit)?;
    if !rest.trim_start_matches(py_isspace).is_empty() {
        return None;
    }
    digits.parse().ok()
}

/// `re.fullmatch(r"\s*(\d+)\s*(KiB|MiB)?\s*", s)` → bytes.
pub fn py_memory(s: &str) -> Option<u128> {
    let s = s.trim_start_matches(py_isspace);
    let end = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    if end == 0 {
        return None;
    }
    let (digits, rest) = s.split_at(end);
    let rest = rest.trim_start_matches(py_isspace);
    let (scale, rest) = if let Some(rest) = rest.strip_prefix("KiB") {
        (1024, rest)
    } else if let Some(rest) = rest.strip_prefix("MiB") {
        (1_048_576, rest)
    } else {
        (1, rest)
    };
    if !rest.trim_start_matches(py_isspace).is_empty() {
        return None;
    }
    digits.parse::<u64>().ok().map(|n| u128::from(n) * scale)
}

/// `repr(float)`, which `json.dumps` writes: the shortest round-trip digits,
/// positional for exponents -4…15, otherwise `d.ddde±XX`.
pub fn py_float(x: f64) -> String {
    if x == 0.0 {
        return if x.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    let sci = format!("{x:e}");
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if !(-4..16).contains(&exponent) {
        let tail = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits
        };
        let esign = if exponent < 0 { '-' } else { '+' };
        return format!("{sign}{tail}e{esign}{:02}", exponent.unsigned_abs());
    }
    let point = exponent + 1;
    if point <= 0 {
        format!(
            "{sign}0.{}{digits}",
            "0".repeat(point.unsigned_abs() as usize)
        )
    } else if point as usize >= digits.len() {
        format!(
            "{sign}{digits}{}.0",
            "0".repeat(point as usize - digits.len())
        )
    } else {
        let (whole, frac) = digits.split_at(point as usize);
        format!("{sign}{whole}.{frac}")
    }
}

/// An exact integer quotient `n / d` as Python's int true division gives
/// it: correctly rounded to the nearest double.
pub fn py_div(n: u128, d: u64) -> f64 {
    if d == 0 {
        return f64::INFINITY;
    }
    if n < (1 << 53) && d < (1 << 53) {
        return n as f64 / d as f64;
    }
    // Exact decimal expansion, then the correctly rounded parse.
    let d128 = u128::from(d);
    let mut text = (n / d128).to_string();
    let mut rem = n % d128;
    if rem != 0 {
        text.push('.');
        for _ in 0..40 {
            rem *= 10;
            text.push(char::from(b'0' + (rem / d128) as u8));
            rem %= d128;
            if rem == 0 {
                break;
            }
        }
        if rem != 0 {
            text.push('1');
        }
    }
    text.parse().unwrap_or(f64::INFINITY)
}

/// `round(x, 1)`.
pub fn py_round1(x: f64) -> f64 {
    format!("{x:.1}").parse().unwrap_or(x)
}

/// A JSON string as `json.dumps` writes it (`ensure_ascii=True`).
pub fn py_json_str(out: &mut Vec<u8>, s: &str) {
    out.push(b'"');
    for c in s.chars() {
        match c {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{08}' => out.extend_from_slice(b"\\b"),
            '\u{0c}' => out.extend_from_slice(b"\\f"),
            ' '..='~' => out.push(c as u8),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.extend_from_slice(format!("\\u{unit:04x}").as_bytes());
                }
            }
        }
    }
    out.push(b'"');
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bash_read_splitting() {
        assert_eq!(
            split_ifs(b"  a b\t c  ", is_ifs, 2),
            vec![&b"a"[..], b"b\t c"]
        );
        assert_eq!(split_ifs(b"a", is_ifs, 3), vec![&b"a"[..], b"", b""]);
        let tab = |b: u8| b == b'\t';
        assert_eq!(
            split_ifs(b"\tP\t\t12\tname x\t", tab, 4),
            vec![&b"P"[..], b"12", b"name x", b""]
        );
        assert_eq!(first_line(b"x\0y\nz"), (b"xy".to_vec(), true));
        assert_eq!(first_line(b"x"), (b"x".to_vec(), false));
    }

    #[test]
    fn bash_integers() {
        assert_eq!(bash_int(b"-1600"), -1600);
        assert_eq!(bash_int(b"010"), 8);
        assert_eq!(bash_int(b"089"), 89);
        assert_eq!(decimal(b"0037"), 37);
        assert_eq!((bash_int(b"-600") + 500) / 1000, 0);
        assert_eq!((bash_int(b"-1600") + 500) / 1000, -1);
    }

    #[test]
    fn awk_numbers() {
        assert_eq!(awk_num(b"12abc"), 12.0);
        assert_eq!(awk_num(b" 0x1A"), 0.0);
        assert_eq!(awk_num(b"1e3"), 1000.0);
        assert_eq!(awk_num(b"1e"), 1.0);
        assert_eq!(awk_num(b".5"), 0.5);
        assert_eq!(awk_num(b"+inf"), f64::INFINITY);
        assert_eq!(awk_num(b"inf"), 0.0);
        assert_eq!(awk_num(b"abc"), 0.0);
        assert_eq!(awk_strnum(b"0"), Some(0.0));
        assert_eq!(awk_strnum(b"12abc"), None);
        assert_eq!(awk_print(1e20), "100000000000000000000");
        assert_eq!(awk_print(9007199254740993.0), "9007199254740992");
        assert_eq!(awk_print(-1e-7), "-1e-07");
        assert_eq!(awk_print(0.30000000000000004), "0.3");
        assert_eq!(awk_print(1.5), "1.5");
        assert_eq!(awk_print(-0.0), "0");
        assert_eq!(awk_d(1.9), "1");
        assert_eq!(awk_d(1e20), "100000000000000000000");
        assert_eq!(awk_f(2.5, 0), "2");
        assert_eq!(awk_f(0.25, 1), "0.2");
        assert_eq!(awk_f(80.0, 1), "80.0");
        assert_eq!(fmt_g(123456.5, 6), "123456");
        assert_eq!(fmt_g(1234567.0, 6), "1.23457e+06");
        assert_eq!(fmt_g(0.0001, 6), "0.0001");
    }

    #[test]
    fn python_text() {
        assert_eq!(py_strip("\u{1c} a b \u{2028}\n"), "a b");
        assert_eq!(
            py_splitlines("a\r\nb\rc\u{85}d\n"),
            vec!["a", "b", "c", "d"]
        );
        assert_eq!(py_splitlines("a\n\nb"), vec!["a", "", "b"]);
        assert_eq!(py_integer(" 100 ns\n", "ns"), Some(100));
        assert_eq!(py_integer("-1 ns", "ns"), None);
        assert_eq!(py_integer("12\n", ""), Some(12));
        assert_eq!(py_integer("12 x", ""), None);
        assert_eq!(py_memory("64 MiB"), Some(64 << 20));
        assert_eq!(py_memory("1024 KiB "), Some(1 << 20));
        assert_eq!(py_memory("7"), Some(7));
        assert_eq!(py_memory(""), None);
        assert_eq!(py_memory("bad"), None);
        assert_eq!(py_text(b"a\r\nb\rc".to_vec()).unwrap(), "a\nb\nc");
        assert!(py_text(vec![0xff]).is_err());
    }

    #[test]
    fn python_floats() {
        for (x, repr) in [
            (50.0, "50.0"),
            (0.1, "0.1"),
            (45.123, "45.123"),
            (0.001, "0.001"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1e16, "1e+16"),
            (1.2345678901234568e16, "1.2345678901234568e+16"),
            (123456789012345.6, "123456789012345.6"),
            (1e15, "1000000000000000.0"),
            (100.0, "100.0"),
        ] {
            assert_eq!(py_float(x), repr, "{x}");
        }
        assert_eq!(py_round1(0.25), 0.2);
        assert_eq!(py_round1(0.45), 0.5);
        assert_eq!(py_round1(49.96), 50.0);
        assert_eq!(py_div(12345678901234567890, 1000), 12345678901234567.0);
        assert_eq!(py_div(51500, 1000), 51.5);
    }

    #[test]
    fn python_json_strings() {
        let mut out = Vec::new();
        py_json_str(&mut out, "GPU \"c\"\t\u{b0}\u{1F525}\u{7f}\\");
        let expected = concat!(r#""GPU \"c\"\t\u00b0"#, r"\ud83d\udd25\u007f", r#"\\""#);
        assert_eq!(String::from_utf8(out).unwrap(), expected);
    }

    #[test]
    fn cleaning() {
        assert_eq!(
            clean(b"evil\x01na\"me\\\x1b[31m\xff"),
            b"evilname[31m\xff".to_vec()
        );
    }
}
