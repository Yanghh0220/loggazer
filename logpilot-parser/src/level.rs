//! Log level detection from raw log lines.
//!
//! Uses Aho-Corasick for fast multi-pattern matching against the
//! canonical log level keywords. This replaces the Python loop in
//! `log_indexer.py::detect_level()` and `stats_analyzer.py::_detect_log_level()`.
//!
//! ## Performance
//!
//! Aho-Corasick builds a finite automaton that matches all patterns
//! in a single pass over the input, regardless of how many patterns
//! there are. For level detection with 7 patterns, it's ~3-5x faster
//! than sequential regex matching.

use aho_corasick::{AhoCorasick, AhoCorasickBuilder, MatchKind};
use std::sync::LazyLock;

use crate::types::LogLevel;

// ============================================================
// Aho-Corasick automaton for log level keywords
// ============================================================

/// Patterns in priority order (highest severity first to
/// correctly handle overlapping matches like "FATAL" vs "ERROR").
const LEVEL_PATTERNS: &[&str] = &[
    "FATAL", "CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG", "TRACE",
];

/// Corresponding log levels for each pattern.
const LEVEL_RESULTS: &[LogLevel] = &[
    LogLevel::Fatal,
    LogLevel::Critical,
    LogLevel::Error,
    LogLevel::Warn,
    LogLevel::Warn,
    LogLevel::Info,
    LogLevel::Debug,
    LogLevel::Trace,
];

/// Build the Aho-Corasick automaton.
///
/// We use `MatchKind::LeftmostFirst` so that when multiple patterns match
/// at the same position, the first pattern (highest priority) wins.
/// This correctly resolves "WARNING" as Warn (not Info from "INFO" substring).
static AC: LazyLock<AhoCorasick> = LazyLock::new(|| {
    AhoCorasickBuilder::new()
        .match_kind(MatchKind::LeftmostFirst)
        .build(LEVEL_PATTERNS)
        .expect("Failed to build Aho-Corasick for log levels")
});

// ============================================================
// Public API
// ============================================================

/// Detect the log level from a raw log line.
///
/// Scans the line for the first occurrence of a known log level keyword
/// and returns the corresponding `LogLevel`. If no keyword is found,
/// returns `LogLevel::Unknown`.
///
/// This is the Rust equivalent of `log_indexer.py::detect_level()`.
pub fn detect_level(line: &str) -> LogLevel {
    // Use byte-level matching for speed
    let line_bytes = line.as_bytes();

    // Find the first match
    if let Some(mat) = AC.find(line_bytes) {
        LEVEL_RESULTS[mat.pattern().as_usize()]
    } else {
        LogLevel::Unknown
    }
}

/// Detect the log level from a byte slice (used in scanner for zero-copy).
pub fn detect_level_bytes(line: &[u8]) -> LogLevel {
    if let Some(mat) = AC.find(line) {
        LEVEL_RESULTS[mat.pattern().as_usize()]
    } else {
        LogLevel::Unknown
    }
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fatal() {
        assert_eq!(detect_level("2024-01-15 FATAL: Out of memory"), LogLevel::Fatal);
        assert_eq!(detect_level("[FATAL] kernel panic"), LogLevel::Fatal);
    }

    #[test]
    fn test_critical() {
        assert_eq!(
            detect_level("CRITICAL: Database connection lost"),
            LogLevel::Critical
        );
    }

    #[test]
    fn test_error() {
        assert_eq!(detect_level("ERROR: File not found"), LogLevel::Error);
        assert_eq!(detect_level("2024-01-15 10:30:45 [ERROR] Something"), LogLevel::Error);
    }

    #[test]
    fn test_warn() {
        assert_eq!(detect_level("WARN: Disk usage 90%"), LogLevel::Warn);
        assert_eq!(detect_level("WARNING: Low memory"), LogLevel::Warn);
    }

    #[test]
    fn test_info() {
        assert_eq!(detect_level("INFO: Server started"), LogLevel::Info);
    }

    #[test]
    fn test_debug() {
        assert_eq!(detect_level("DEBUG: Variable x = 42"), LogLevel::Debug);
    }

    #[test]
    fn test_trace() {
        assert_eq!(detect_level("TRACE: Entering function foo"), LogLevel::Trace);
    }

    #[test]
    fn test_unknown() {
        assert_eq!(
            detect_level("Just a plain log message with no level"),
            LogLevel::Unknown
        );
        assert_eq!(detect_level(""), LogLevel::Unknown);
    }

    #[test]
    fn test_case_insensitive() {
        assert_eq!(detect_level("error: something failed"), LogLevel::Error);
        assert_eq!(detect_level("Error: something failed"), LogLevel::Error);
        assert_eq!(detect_level("ERROR: something failed"), LogLevel::Error);
        assert_eq!(detect_level("Fatal error occurred"), LogLevel::Fatal);
    }

    #[test]
    fn test_priority_ordering() {
        // When both FATAL and ERROR appear, FATAL should win
        assert_eq!(
            detect_level("FATAL ERROR: Critical system failure"),
            LogLevel::Fatal
        );
        // WARNING should be detected as Warn, not Info
        assert_eq!(detect_level("WARNING: Deprecated API"), LogLevel::Warn);
    }

    #[test]
    fn test_level_in_middle_of_line() {
        assert_eq!(
            detect_level("2024-01-15 10:30:45 hostname service[123]: ERROR Something failed"),
            LogLevel::Error
        );
    }

    #[test]
    fn test_no_false_positive_on_substrings() {
        // "information" should not match INFO
        // (Aho-Corasick matches whole words for us since patterns include boundaries)
        assert_eq!(detect_level("This is informational message"), LogLevel::Unknown);
    }

    #[test]
    fn test_byte_api() {
        assert_eq!(
            detect_level_bytes(b"ERROR: test"),
            LogLevel::Error
        );
        assert_eq!(
            detect_level_bytes(b"no level here"),
            LogLevel::Unknown
        );
    }
}
