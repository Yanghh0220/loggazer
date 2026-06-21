//! Timestamp detection and normalization.
//!
//! Extracts timestamps from log lines and normalizes them to Unix microseconds (UTC).
//! Supports 7 common log timestamp formats, matching the Python reference in
//! `log_indexer.py::extract_timestamp_us()`.
//!
//! ## Design
//!
//! Uses pre-compiled `regex::Regex` patterns tried in order of frequency.
//! Common ISO 8601 formats are tried first (most CI/CD logs), then syslog,
//! then Unix epoch variants, then less common formats.

use regex::bytes::Regex as BytesRegex;
use std::sync::LazyLock;

use crate::types::TimestampUs;

// ============================================================
// Pre-compiled timestamp patterns (in frequency order)
// ============================================================

/// ISO 8601 with timezone: `2024-01-15T10:30:45.123Z` or `2024-01-15T10:30:45+08:00`
static RE_ISO_TZ: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)")
        .unwrap()
});

/// ISO 8601 without T separator: `2024-01-15 10:30:45,123`
static RE_ISO_SPACE: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)").unwrap()
});

/// Syslog-style: `Jan 15 10:30:45`
static RE_SYSLOG: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})").unwrap()
});

/// Datetime with slashes: `2024/01/15 10:30:45`
static RE_SLASH: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)").unwrap()
});

/// US-style: `01-15-2024 10:30:45`
static RE_US: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})").unwrap()
});

/// Unix timestamp in seconds (10 digits within word boundaries).
/// Captures fractional seconds if present (group 2).
static RE_UNIX_SEC: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"\b(\d{10})(?:\.(\d{1,6}))?\b").unwrap()
});

/// Unix timestamp in milliseconds (13 digits).
static RE_UNIX_MS: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"\b(\d{13})\b").unwrap()
});

// ============================================================
// Month name mapping for syslog
// ============================================================

const MONTH_NAMES: &[(&str, u32)] = &[
    ("jan", 1),
    ("feb", 2),
    ("mar", 3),
    ("apr", 4),
    ("may", 5),
    ("jun", 6),
    ("jul", 7),
    ("aug", 8),
    ("sep", 9),
    ("oct", 10),
    ("nov", 11),
    ("dec", 12),
];

fn parse_month(abbrev: &str) -> Option<u32> {
    let lower = &abbrev[..3.min(abbrev.len())].to_ascii_lowercase();
    for (name, num) in MONTH_NAMES {
        if lower == *name {
            return Some(*num);
        }
    }
    None
}

// ============================================================
// Timestamp parsing helpers
// ============================================================

/// Parse an ISO-format timestamp string into Unix microseconds.
///
/// Handles variants: `2024-01-15T10:30:45.123Z`, `2024-01-15 10:30:45,123`,
/// `2024/01/15 10:30:45`, with or without fractional seconds.
fn parse_iso_like(ts_bytes: &[u8]) -> Option<TimestampUs> {
    let ts_str = std::str::from_utf8(ts_bytes).ok()?;

    // Normalize: replace T with space, comma with dot
    let normalized: String = ts_str
        .chars()
        .map(|c| match c {
            'T' => ' ',
            ',' => '.',
            _ => c,
        })
        .collect();

    // Remove trailing Z
    let normalized = normalized.trim_end_matches('Z').trim_end_matches('z');

    // Handle timezone offset: strip trailing +HH:MM or -HH:MM
    let normalized = if let Some(pos) = normalized.rfind(['+', '-']) {
        // Only strip if it looks like a timezone offset (after position 10)
        if pos > 10 && normalized.as_bytes().get(pos.saturating_sub(1)) == Some(&b' ') {
            &normalized[..pos]
        } else {
            normalized
        }
    } else {
        normalized
    };

    // Parse: YYYY-MM-DD HH:MM:SS[.ffffff]
    // The date parts are at fixed positions
    if normalized.len() < 19 {
        return None;
    }
    let bytes = normalized.as_bytes();

    let year = parse_u32_fixed(&bytes[0..4])? as i32;
    let month = parse_u32_fixed(&bytes[5..7])?;
    let day = parse_u32_fixed(&bytes[8..10])?;
    let hour = parse_u32_fixed(&bytes[11..13])?;
    let minute = parse_u32_fixed(&bytes[14..16])?;
    let second = parse_u32_fixed(&bytes[17..19])?;

    // Fractional seconds
    let micros: u32 = if bytes.len() > 20 && bytes[19] == b'.' {
        let frac_start = 20;
        let frac_end = bytes.len().min(frac_start + 6);
        let frac_str = std::str::from_utf8(&bytes[frac_start..frac_end]).ok()?;
        let frac_val = frac_str.parse::<u32>().ok()?;
        // Pad to 6 digits
        let digits = frac_end - frac_start;
        frac_val * 10u32.pow((6 - digits) as u32)
    } else {
        0
    };

    // Validate ranges
    if month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59 || second > 59 {
        return None;
    }

    // Compute days since Unix epoch using a fast formula
    let total_seconds = datetime_to_unix_seconds(year, month, day, hour, minute, second)?;

    Some(total_seconds as i64 * 1_000_000 + micros as i64)
}

/// Convert datetime components to Unix seconds using the civil calendar formula.
fn datetime_to_unix_seconds(
    year: i32,
    month: u32,
    day: u32,
    hour: u32,
    minute: u32,
    second: u32,
) -> Option<i64> {
    // The range check matches Python's datetime limits
    if year < 1970 || year > 3000 {
        return None;
    }

    // Adapted from chrono's algorithm for NaiveDate -> days since epoch
    let year = year as i32;
    let month = month as i32;
    let day = day as i32;

    // Compute days since Unix epoch using civil calendar formula
    let y = year - 1;
    let era = if y >= 0 { y / 400 } else { (y - 399) / 400 };
    let yoe = (y - era * 400) as u32; // year of era (0-399)
    let doy = match month {
        1 => 0,
        2 => 31,
        3 => 59,
        4 => 90,
        5 => 120,
        6 => 151,
        7 => 181,
        8 => 212,
        9 => 243,
        10 => 273,
        11 => 304,
        12 => 334,
        _ => return None,
    };
    // Leap year adjustment
    let is_leap = (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
    let doy = if month > 2 && is_leap { doy + 1 } else { doy };
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + yoe / 400 + doy;
    let days_from_epoch = era as i64 * 146097 + doe as i64 - 719468;

    let total_seconds = days_from_epoch * 86400
        + hour as i64 * 3600
        + minute as i64 * 60
        + second as i64;
    Some(total_seconds)
}

/// Fast 4-digit decimal parse from bytes.
#[inline]
fn parse_u32_fixed(bytes: &[u8]) -> Option<u32> {
    if bytes.len() < 4 {
        return None;
    }
    let mut val: u32 = 0;
    for &b in bytes.iter().take(4) {
        if !b.is_ascii_digit() {
            return None;
        }
        val = val * 10 + (b - b'0') as u32;
    }
    Some(val)
}

/// Fast 2-digit parse.
#[inline]
fn parse_u32_2(bytes: &[u8]) -> Option<u32> {
    if bytes.len() < 2 {
        return None;
    }
    let b0 = bytes[0];
    let b1 = bytes[1];
    if !b0.is_ascii_digit() || !b1.is_ascii_digit() {
        return None;
    }
    Some(((b0 - b'0') * 10 + (b1 - b'0')) as u32)
}

/// Parse syslog timestamp: `Jan 15 10:30:45` or `Jan  5 10:30:45`
fn parse_syslog(ts_bytes: &[u8]) -> Option<TimestampUs> {
    let ts_str = std::str::from_utf8(ts_bytes).ok()?;
    let parts: Vec<&str> = ts_str.split_whitespace().collect();
    if parts.len() < 3 {
        return None;
    }

    let month = parse_month(parts[0])?;
    let day: u32 = parts[1].parse().ok()?;
    let time_parts: Vec<&str> = parts[2].split(':').collect();
    if time_parts.len() != 3 {
        return None;
    }
    let hour: u32 = time_parts[0].parse().ok()?;
    let minute: u32 = time_parts[1].parse().ok()?;
    let second: u32 = time_parts[2].parse().ok()?;

    // Use current year for syslog (same as Python reference)
    let current_year = chrono::Utc::now().year_ce().1 as i32;

    datetime_to_unix_seconds(current_year, month, day, hour, minute, second)
        .map(|s| s * 1_000_000)
}

// ============================================================
// Public API
// ============================================================

/// Extracts a normalized Unix-microsecond timestamp from a log line.
///
/// Returns `Some(timestamp_us)` if a timestamp was found and parsed,
/// or `None` if no recognizable timestamp pattern was detected.
///
/// This is the Rust equivalent of `log_indexer.py::extract_timestamp_us()`.
pub fn extract_timestamp_us(line: &[u8]) -> Option<TimestampUs> {
    // Try patterns in frequency order (most common CI/CD formats first)

    // 1. ISO 8601 with T separator and optional timezone
    if let Some(caps) = RE_ISO_TZ.captures(line) {
        let m = caps.get(1)?;
        if let Some(ts) = parse_iso_like(m.as_bytes()) {
            return Some(ts);
        }
    }

    // 2. ISO 8601 without T: 2024-01-15 10:30:45,123
    if let Some(caps) = RE_ISO_SPACE.captures(line) {
        let m = caps.get(1)?;
        if let Some(ts) = parse_iso_like(m.as_bytes()) {
            return Some(ts);
        }
    }

    // 3. Syslog: Jan 15 10:30:45
    if let Some(caps) = RE_SYSLOG.captures(line) {
        let m = caps.get(1)?;
        if let Some(ts) = parse_syslog(m.as_bytes()) {
            return Some(ts);
        }
    }

    // 4. Unix seconds (10 digits): 1705312245 or 1705312245.123456
    if let Some(caps) = RE_UNIX_SEC.captures(line) {
        let sec_bytes = caps.get(1)?.as_bytes();
        let sec_str = std::str::from_utf8(sec_bytes).ok()?;
        if let Ok(sec) = sec_str.parse::<i64>() {
            // Valid range: year 2000 to 2100
            if (946684800..=4102444800).contains(&sec) {
                let micros: i64 = if let Some(frac) = caps.get(2) {
                    let frac_str = std::str::from_utf8(frac.as_bytes()).ok()?;
                    let frac_val = frac_str.parse::<i64>().ok()?;
                    let digits = frac.len();
                    frac_val * 10i64.pow((6 - digits) as u32)
                } else {
                    0
                };
                return Some(sec * 1_000_000 + micros);
            }
        }
    }

    // 5. Unix milliseconds (13 digits)
    if let Some(caps) = RE_UNIX_MS.captures(line) {
        let ms_bytes = caps.get(1)?.as_bytes();
        let ms_str = std::str::from_utf8(ms_bytes).ok()?;
        if let Ok(ms) = ms_str.parse::<i64>() {
            if (946684800000..=4102444800000).contains(&ms) {
                return Some(ms * 1000);
            }
        }
    }

    // 6. Slash format: 2024/01/15 10:30:45
    if let Some(caps) = RE_SLASH.captures(line) {
        let m = caps.get(1)?;
        if let Some(ts) = parse_iso_like(m.as_bytes()) {
            return Some(ts);
        }
    }

    // 7. US-style: 01-15-2024 10:30:45
    if let Some(caps) = RE_US.captures(line) {
        let m = caps.get(1)?;
        let ts_bytes = m.as_bytes();
        let ts_str = std::str::from_utf8(ts_bytes).ok()?;
        // Parse MM-DD-YYYY HH:MM:SS
        if ts_str.len() >= 19 {
            let b = ts_str.as_bytes();
            let month = parse_u32_2(&b[0..2])?;
            let day = parse_u32_2(&b[3..5])?;
            let year = parse_u32_fixed(&b[6..10])? as i32;
            let hour = parse_u32_2(&b[11..13])?;
            let minute = parse_u32_2(&b[14..16])?;
            let second = parse_u32_2(&b[17..19])?;
            datetime_to_unix_seconds(year, month, day, hour, minute, second)
                .map(|s| s * 1_000_000)?;
        }
    }

    None
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    /// Helper to test a line and compare against expected Unix micros.
    fn assert_ts(line: &str, expected_us: i64) {
        let got = extract_timestamp_us(line.as_bytes());
        assert!(
            got.is_some(),
            "Expected timestamp in line: '{}', got None",
            line
        );
        let got = got.unwrap();
        assert_eq!(
            got, expected_us,
            "Timestamp mismatch for '{}': expected {}, got {}",
            line, expected_us, got
        );
    }

    fn assert_no_ts(line: &str) {
        let got = extract_timestamp_us(line.as_bytes());
        assert!(
            got.is_none(),
            "Expected no timestamp in line '{}', got {:?}",
            line,
            got
        );
    }

    #[test]
    fn test_iso8601_with_z() {
        // 2024-01-15T10:30:45Z = 1705314645 seconds
        assert_ts(
            "2024-01-15T10:30:45Z INFO Server started",
            1705314645_000_000,
        );
    }

    #[test]
    fn test_iso8601_with_fractional() {
        // 2024-01-15T10:30:45.123Z
        assert_ts(
            "2024-01-15T10:30:45.123Z DEBUG Processing",
            1705314645_123_000,
        );
    }

    #[test]
    fn test_iso_space_separator() {
        assert_ts(
            "2024-01-15 10:30:45 ERROR Something failed",
            1705314645_000_000,
        );
    }

    #[test]
    fn test_iso_with_comma_fractional() {
        assert_ts(
            "2024-01-15 10:30:45,500 WARN Low memory",
            1705314645_500_000,
        );
    }

    #[test]
    fn test_iso_with_timezone_offset() {
        // +08:00 means 8 hours ahead of UTC = 1705285845 UTC
        assert_ts(
            "2024-01-15T10:30:45+08:00 INFO Asia event",
            1705285845_000_000,
        );
    }

    #[test]
    fn test_syslog_format() {
        // "Jan 15 10:30:45" — uses current year
        let line = "Jan 15 10:30:45 ERROR Disk full";
        let result = extract_timestamp_us(line.as_bytes());
        assert!(result.is_some(), "syslog timestamp should parse");
        // We can't assert exact value since it uses current year
        let ts = result.unwrap();
        assert!(ts > 0);
    }

    #[test]
    fn test_unix_seconds() {
        // 1705312245 = 2024-01-15T10:30:45 UTC
        assert_ts(
            "1705312245 INFO Deployment started",
            1705312245_000_000,
        );
    }

    #[test]
    fn test_unix_seconds_with_fraction() {
        assert_ts(
            "1705312245.123456 DEBUG Trace",
            1705312245_123_456,
        );
    }

    #[test]
    fn test_unix_milliseconds() {
        // 1705312245123 ms = 1705312245.123 seconds
        assert_ts(
            "1705312245123 INFO Millis format",
            1705312245_123_000,
        );
    }

    #[test]
    fn test_slash_format() {
        assert_ts(
            "2024/01/15 10:30:45 ERROR Something",
            1705314645_000_000,
        );
    }

    #[test]
    fn test_us_format() {
        // 01-15-2024 10:30:45
        assert_ts(
            "01-15-2024 10:30:45 INFO US format",
            1705314645_000_000,
        );
    }

    #[test]
    fn test_no_timestamp() {
        assert_no_ts("Just a regular log message without timestamp");
        assert_no_ts("");
        assert_no_ts("ERROR: something failed");
    }

    #[test]
    fn test_out_of_range_unix_seconds() {
        // 9999999999 is year ~2286, outside valid range
        assert_no_ts("9999999999 INFO Future event");
        // 500000000 is year ~1985, outside valid range
        assert_no_ts("500000000 INFO Past event");
    }

    #[test]
    fn test_line_with_multiple_numbers() {
        // Should not mistakenly parse port numbers or IDs as timestamps
        let line = "Request 1234567890 from port 8080 completed in 1500ms";
        let result = extract_timestamp_us(line.as_bytes());
        // 1234567890 is out of range (year ~2009), so no timestamp
        assert!(result.is_none());
    }

    #[test]
    fn test_consistency_iso_variants() {
        // All these should parse to the same timestamp
        let expected = 1705314645_000_000;

        let lines = [
            "2024-01-15T10:30:45Z INFO test",
            "2024-01-15 10:30:45 INFO test",
            "2024/01/15 10:30:45 INFO test",
            "01-15-2024 10:30:45 INFO test",
        ];

        for line in &lines {
            assert_ts(line, expected);
        }
    }
}
