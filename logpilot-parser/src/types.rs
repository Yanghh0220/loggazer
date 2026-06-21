//! Core data types for the LogPilot parser.
//!
//! These types are designed to be:
//! - Compact (small stack footprint)
//! - Serializable (serde, for IPC to frontend)
//! - PyO3-compatible (when the python-bindings feature is enabled)

use serde::{Deserialize, Serialize};
use std::fmt;

// ============================================================
// Log Level
// ============================================================

/// Standard log levels, ordered by severity.
///
/// The `#[repr(u8)]` annotation makes this a single byte in memory
/// and enables fast comparison / sorting.
#[derive(
    Debug,
    Clone,
    Copy,
    PartialEq,
    Eq,
    PartialOrd,
    Ord,
    Hash,
    Serialize,
    Deserialize,
)]
#[repr(u8)]
pub enum LogLevel {
    Unknown = 0,
    Trace = 1,
    Debug = 2,
    Info = 3,
    Warn = 4,
    Error = 5,
    Critical = 6,
    Fatal = 7,
}

impl LogLevel {
    /// Convert from a string label (case-insensitive).
    pub fn from_str(s: &str) -> Self {
        // Fast path: check first character for common levels
        match s.as_bytes().first().copied() {
            Some(b'f' | b'F') => {
                if s.eq_ignore_ascii_case("fatal") {
                    return LogLevel::Fatal;
                }
            }
            Some(b'c' | b'C') => {
                if s.eq_ignore_ascii_case("critical") {
                    return LogLevel::Critical;
                }
            }
            Some(b'e' | b'E') => {
                if s.eq_ignore_ascii_case("error") {
                    return LogLevel::Error;
                }
            }
            Some(b'w' | b'W') => {
                if s.eq_ignore_ascii_case("warn") || s.eq_ignore_ascii_case("warning") {
                    return LogLevel::Warn;
                }
            }
            Some(b'i' | b'I') => {
                if s.eq_ignore_ascii_case("info") {
                    return LogLevel::Info;
                }
            }
            Some(b'd' | b'D') => {
                if s.eq_ignore_ascii_case("debug") {
                    return LogLevel::Debug;
                }
            }
            Some(b't' | b'T') => {
                if s.eq_ignore_ascii_case("trace") {
                    return LogLevel::Trace;
                }
            }
            _ => {}
        }
        LogLevel::Unknown
    }

    /// Return the canonical string representation.
    pub fn as_str(&self) -> &'static str {
        match self {
            LogLevel::Unknown => "UNKNOWN",
            LogLevel::Trace => "TRACE",
            LogLevel::Debug => "DEBUG",
            LogLevel::Info => "INFO",
            LogLevel::Warn => "WARN",
            LogLevel::Error => "ERROR",
            LogLevel::Critical => "CRITICAL",
            LogLevel::Fatal => "FATAL",
        }
    }

    /// Numeric severity for ordering (higher = more severe).
    pub fn severity(&self) -> u8 {
        *self as u8
    }
}

impl fmt::Display for LogLevel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// ============================================================
// Timestamp representation
// ============================================================

/// Normalized timestamp in Unix microseconds since epoch (UTC).
///
/// Using `i64` (not `u64`) to match Python's timestamp semantics and
/// allow negative values for pre-1970 dates (though rare in logs).
pub type TimestampUs = i64;

/// Sentinel value for "no timestamp extracted".
pub const NO_TIMESTAMP: TimestampUs = 0;

// ============================================================
// Line-level scan result (Stage 1 output)
// ============================================================

/// Per-line metadata produced by Stage 1 scan.
///
/// This is intentionally compact: ~40 bytes per line.
/// For a 1-million-line log file, that's ~40 MB — acceptable.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LineInfo {
    /// Unix microseconds since epoch (UTC), or 0 if no timestamp found.
    pub timestamp_us: TimestampUs,
    /// Detected log level.
    pub level: LogLevel,
    /// Byte offset of the start of this line in the source file.
    pub byte_offset: u64,
    /// 1-based line number.
    pub line_number: u32,
    /// Byte length of this line (including newline).
    pub line_length: u32,
    /// First N characters of the line content (for preview).
    /// Limited to MAX_PREVIEW_LENGTH (200 chars) to bound memory.
    pub message_preview: String,
}

/// Maximum characters in a message preview.
pub const MAX_PREVIEW_LENGTH: usize = 200;

// ============================================================
// Stage 1 aggregate result
// ============================================================

/// Complete result of a Stage 1 scan.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScanResult {
    /// Per-line metadata for every line in the file.
    pub lines: Vec<LineInfo>,
    /// Aggregate statistics.
    pub stats: ScanStats,
}

/// Aggregate statistics from a Stage 1 scan.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScanStats {
    /// Total number of lines scanned.
    pub total_lines: u64,
    /// Total file size in bytes.
    pub file_size_bytes: u64,
    /// Number of lines where a timestamp was successfully extracted.
    pub lines_with_timestamp: u64,
    /// Fraction of lines with timestamps (0.0 - 1.0).
    pub timestamp_coverage: f64,
    /// Distribution of log levels (level name → count).
    pub level_distribution: std::collections::HashMap<String, u64>,
    /// Earliest timestamp found (Unix microseconds), if any.
    pub time_range_min_us: Option<TimestampUs>,
    /// Latest timestamp found (Unix microseconds), if any.
    pub time_range_max_us: Option<TimestampUs>,
    /// Scan duration in milliseconds.
    pub scan_duration_ms: f64,
}

// ============================================================
// Time range for filtering
// ============================================================

/// A time range for filtering, in Unix microseconds.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct TimeRange {
    /// Inclusive start (Unix microseconds).
    pub start_us: TimestampUs,
    /// Inclusive end (Unix microseconds).
    pub end_us: TimestampUs,
}

impl TimeRange {
    pub fn contains(&self, ts: TimestampUs) -> bool {
        ts >= self.start_us && ts <= self.end_us
    }
}

// ============================================================
// Stage 2 / Stage 3 parse result
// ============================================================

/// Error category from pattern matching (maps to `_RE_ERROR_PATTERNS` in Python).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ErrorCategory {
    ImportError,
    SyntaxError,
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    OSError,
    HTTPError,
    TimeoutError,
    ConnectionError,
    OutOfMemory,
    Segfault,
    AssertionError,
    NullPointer,
    BuildError,
    TestFailure,
    NetworkError,
    PermissionError,
    VersionConflict,
    DependencyError,
    UnknownError,
    Warning,
    Info,
}

impl ErrorCategory {
    pub fn as_str(&self) -> &'static str {
        match self {
            ErrorCategory::ImportError => "ImportError",
            ErrorCategory::SyntaxError => "SyntaxError",
            ErrorCategory::TypeError => "TypeError",
            ErrorCategory::ValueError => "ValueError",
            ErrorCategory::KeyError => "KeyError",
            ErrorCategory::AttributeError => "AttributeError",
            ErrorCategory::OSError => "OSError",
            ErrorCategory::HTTPError => "HTTPError",
            ErrorCategory::TimeoutError => "TimeoutError",
            ErrorCategory::ConnectionError => "ConnectionError",
            ErrorCategory::OutOfMemory => "OutOfMemory",
            ErrorCategory::Segfault => "Segfault",
            ErrorCategory::AssertionError => "AssertionError",
            ErrorCategory::NullPointer => "NullPointer",
            ErrorCategory::BuildError => "BuildError",
            ErrorCategory::TestFailure => "TestFailure",
            ErrorCategory::NetworkError => "NetworkError",
            ErrorCategory::PermissionError => "PermissionError",
            ErrorCategory::VersionConflict => "VersionConflict",
            ErrorCategory::DependencyError => "DependencyError",
            ErrorCategory::UnknownError => "UnknownError",
            ErrorCategory::Warning => "Warning",
            ErrorCategory::Info => "Info",
        }
    }
}

/// Per-line parsed result with field extraction (Stage 2/3 output).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParseResult {
    /// Line metadata (same as Stage 1).
    pub line_info: LineInfo,
    /// Full raw line text.
    pub raw_text: String,
    /// Error category (if this is an error/warning line).
    pub error_category: Option<ErrorCategory>,
    /// Extracted file paths mentioned in the line.
    pub file_paths: Vec<String>,
    /// Extracted version numbers (e.g., "1.2.3").
    pub versions: Vec<String>,
    /// Extracted duration in seconds, if any (e.g., "took 5.3s").
    pub duration_secs: Option<f64>,
    /// Whether this line appears to be part of a stack trace.
    pub is_stacktrace: bool,
    /// Error signature (first 80 chars, for deduplication).
    pub error_signature: Option<String>,
}
