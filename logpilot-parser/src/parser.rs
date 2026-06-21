//! Full log line parser (Stage 2 and Stage 3).
//!
//! Stage 2 (`parse_log_range`): Parses a range of lines with full field
//! extraction including error categorization, file path extraction,
//! version number extraction, and duration parsing.
//!
//! Stage 3 (`hydrate_log_detail`): Deep-parses a single log entry,
//! extracting every structured field available.
//!
//! This replaces `log_parser.py::_single_pass_scan()` and
//! `analyzers/pattern_analyzer.py::analyze_patterns()`.

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::time::Instant;

use regex::bytes::Regex as BytesRegex;
use std::sync::LazyLock;

use crate::types::{ErrorCategory, LineInfo, ParseResult};

// ============================================================
// Pre-compiled patterns (matching Python analyzers)
// ============================================================

/// Version conflict pattern: pkg@1.2.3 requires dep@4.5.6
static RE_VERSION_CONFLICT: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?P<package>[\w\-\.]+)\s*@?\s*(?P<expected>\d+\.\d+\.\d+).*?(?:requires|depends|peer).*?(?P<actual>[\w\-\.]+)\s*@?\s*(?P<actual_version>\d+\.\d+\.\d+)"
    ).unwrap()
});

/// Dependency error pattern
static RE_DEPENDENCY_ERROR: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?:(?:Could not find|No matching|Unable to resolve|Failed to download)\s+(?P<dependency>[\w\-\.\[\]=<>,;\s]+))"
    ).unwrap()
});

/// Build error pattern
static RE_BUILD_ERROR: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?P<build>(?:Build|Compilation|Linking)\s+(?:failed|error|failure)|(?:error[:\[][A-Z]+\d+)|(?:undefined\s+reference\s+to))"
    ).unwrap()
});

/// Test failure pattern
static RE_TEST_FAILURE: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?P<test>(?:FAILED|FAILURES|ERRORS)\s*$|(?:Tests?\s+(?:failed|run):\s*\d+)|(?:\d+\s+(?:failed|passed|error)\b)|(?:assert\s+.*\s*==\s*))"
    ).unwrap()
});

/// Network error pattern
static RE_NETWORK_ERROR: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?P<network>(?:Connection\s+(?:refused|reset|timed?\s*out))|(?:DNS\s+(?:resolution|lookup)\s+failed)|(?:Network\s+(?:unreachable|error))|(?:ECONNREFUSED|ETIMEDOUT|ENOTFOUND))"
    ).unwrap()
});

/// Permission error pattern
static RE_PERMISSION_ERROR: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?P<permission>(?:Permission\s+denied|EACCES|E?PERM)|(?:(?:access|permission)\s+(?:denied|forbidden|restricted))|(?:(?:cannot|unable\s+to)\s+(?:access|open|write|create)))"
    ).unwrap()
});

/// File path pattern
static RE_FILE_PATH: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?:(?:[/\\][\w\-\.]+)+[/\\]?[\w\-\.]+|(?:[\w\-\.]+\.(?:py|js|ts|java|go|rb|rs|cpp|c|h|sh|yaml|yml|json|toml|xml))"
    ).unwrap()
});

/// Version number extraction
static RE_VERSION: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"\b(\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9\.]+)?)\b").unwrap()
});

/// Duration extraction: "took 5.3s", "completed in 120ms", etc.
static RE_DURATION: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?:(?:took|spent|duration|elapsed|completed\sin|finished\sin)\s+(?:(?P<hours>\d+)\s*(?:h|hour|hours))?\s*(?:(?P<minutes>\d+)\s*(?:m|min|minutes?))?\s*(?:(?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec|seconds?))?)"
    ).unwrap()
});

/// Stack trace indicator
static RE_STACKTRACE: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(r"^\s+(?:at\s+|File\s+|from\s+|\.py:|:\d+:\d+\s+in\s+)").unwrap()
});

/// Error keyword for generic detection
static RE_ERROR_KEYWORD: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?i)(error|failed|fatal|exception|traceback|panic|denied|timeout|not\s+found|no\s+such\s+file|permission\s+denied|exit\s+code|assertion|abort|critical|seg(?:mentation)?\s+fault|\boom\b|killed)"
    ).unwrap()
});

/// Python error types
static RE_PYTHON_ERRORS: LazyLock<BytesRegex> = LazyLock::new(|| {
    BytesRegex::new(
        r"(?P<import>(?:ImportError|ModuleNotFoundError))|(?P<syntax>(?:SyntaxError|invalid\s+syntax))|(?P<type>(?:TypeError))|(?P<value>(?:ValueError))|(?P<key>(?:KeyError))|(?P<attr>(?:AttributeError))|(?P<os>(?:OSError|FileNotFoundError|PermissionError))|(?P<http>(?:HTTPError|4\d{2}|5\d{2}))|(?P<timeout>(?:TimeoutError|timed?\s*out))|(?P<connection>(?:ConnectionError|Connection\s+refused|ECONNREFUSED))|(?P<oom>(?:OutOfMemoryError|out\s+of\s+memory|OOM))|(?P<segfault>(?:Segmentation\s+fault|SIGSEGV))|(?P<assertion>(?:AssertionError|assert\s+failed))|(?P<null>(?:NullPointerException|NoneType.*has\s+no\s+attribute))"
    ).unwrap()
});

// ============================================================
// Public API: Stage 2 — parse a range of lines
// ============================================================

/// Parse log lines within a specified range and return fully parsed results.
///
/// # Arguments
///
/// * `file_path` - Path to the log file.
/// * `start_offset` / `end_offset` - Byte range to parse (inclusive).
/// * `max_results` - Maximum number of results to return.
///
/// This can be used after `scan_log_stage1()` to hydrate a selected range.
pub fn parse_log_range(
    file_path: &Path,
    start_line: Option<u64>,
    end_line: Option<u64>,
    max_results: Option<usize>,
) -> Result<Vec<ParseResult>, ParserError> {
    let _start_time = Instant::now();
    let max_results = max_results.unwrap_or(1000);

    let file = std::fs::File::open(file_path)
        .map_err(|e| ParserError::Io(format!("Failed to open: {}", e)))?;

    let reader = BufReader::with_capacity(1024 * 1024, file);
    let mut results = Vec::with_capacity(max_results.min(1000));
    let mut line_num: u64 = 0;
    let start = start_line.unwrap_or(1);
    let end = end_line.unwrap_or(u64::MAX);

    for line_result in reader.lines() {
        let line = line_result.map_err(|e| ParserError::Io(format!("Read error: {}", e)))?;
        line_num += 1;

        if line_num < start {
            continue;
        }
        if line_num > end {
            break;
        }
        if results.len() >= max_results {
            break;
        }

        let parsed = parse_single_line(&line, line_num as u32);
        results.push(parsed);
    }

    Ok(results)
}

// ============================================================
// Public API: Stage 3 — hydrate a single log detail
// ============================================================

/// Deep-parse a single log entry (Stage 3).
///
/// When a user clicks on a log line in the UI, this extracts every
/// structured field available from that specific line.
pub fn hydrate_log_detail(raw_line: &str, line_info: Option<&LineInfo>) -> ParseResult {
    let line_num = line_info.map(|li| li.line_number).unwrap_or(0);
    let mut parsed = parse_single_line(raw_line, line_num);

    // If we have pre-scanned line info, merge it
    if let Some(info) = line_info {
        parsed.line_info = info.clone();
    }

    parsed
}

// ============================================================
// Error categorization
// ============================================================

/// Categorize an error line from its content.
pub fn categorize_error(line: &str) -> ErrorCategory {
    let line_bytes = line.as_bytes();

    // Try Python-specific error types first (most common in CI/CD)
    if let Some(caps) = RE_PYTHON_ERRORS.captures(line_bytes) {
        if caps.name("import").is_some() {
            return ErrorCategory::ImportError;
        }
        if caps.name("syntax").is_some() {
            return ErrorCategory::SyntaxError;
        }
        if caps.name("type").is_some() {
            return ErrorCategory::TypeError;
        }
        if caps.name("value").is_some() {
            return ErrorCategory::ValueError;
        }
        if caps.name("key").is_some() {
            return ErrorCategory::KeyError;
        }
        if caps.name("attr").is_some() {
            return ErrorCategory::AttributeError;
        }
        if caps.name("os").is_some() {
            return ErrorCategory::OSError;
        }
        if caps.name("http").is_some() {
            return ErrorCategory::HTTPError;
        }
        if caps.name("timeout").is_some() {
            return ErrorCategory::TimeoutError;
        }
        if caps.name("connection").is_some() {
            return ErrorCategory::ConnectionError;
        }
        if caps.name("oom").is_some() {
            return ErrorCategory::OutOfMemory;
        }
        if caps.name("segfault").is_some() {
            return ErrorCategory::Segfault;
        }
        if caps.name("assertion").is_some() {
            return ErrorCategory::AssertionError;
        }
        if caps.name("null").is_some() {
            return ErrorCategory::NullPointer;
        }
    }

    // Broader categories from CI/CD patterns
    if RE_VERSION_CONFLICT.is_match(line_bytes) {
        return ErrorCategory::VersionConflict;
    }
    if RE_DEPENDENCY_ERROR.is_match(line_bytes) {
        return ErrorCategory::DependencyError;
    }
    if RE_BUILD_ERROR.is_match(line_bytes) {
        return ErrorCategory::BuildError;
    }
    if RE_TEST_FAILURE.is_match(line_bytes) {
        return ErrorCategory::TestFailure;
    }
    if RE_NETWORK_ERROR.is_match(line_bytes) {
        return ErrorCategory::NetworkError;
    }
    if RE_PERMISSION_ERROR.is_match(line_bytes) {
        return ErrorCategory::PermissionError;
    }

    // Generic error detection
    if RE_ERROR_KEYWORD.is_match(line_bytes) {
        return ErrorCategory::UnknownError;
    }

    // Check for warnings
    let line_lower = line.to_lowercase();
    if line_lower.contains("warn") {
        return ErrorCategory::Warning;
    }

    ErrorCategory::Info
}

// ============================================================
// Internal: parse a single line fully
// ============================================================

fn parse_single_line(line: &str, line_num: u32) -> ParseResult {
    let line_bytes = line.as_bytes();

    // Error categorization
    let error_category = categorize_error(line);

    // File path extraction
    let file_paths: Vec<String> = RE_FILE_PATH
        .find_iter(line_bytes)
        .filter_map(|m| {
            let s = std::str::from_utf8(m.as_bytes()).ok()?;
            Some(s.to_string())
        })
        .take(10)
        .collect();

    // Version extraction
    let versions: Vec<String> = RE_VERSION
        .find_iter(line_bytes)
        .filter_map(|m| {
            let s = std::str::from_utf8(m.as_bytes()).ok()?;
            Some(s.to_string())
        })
        .take(5)
        .collect();

    // Duration extraction
    let duration_secs = RE_DURATION.captures(line_bytes).and_then(|caps| {
        let mut total: f64 = 0.0;
        if let Some(h) = caps.name("hours") {
            if let Ok(val) = std::str::from_utf8(h.as_bytes()).unwrap_or("0").parse::<f64>() {
                total += val * 3600.0;
            }
        }
        if let Some(m) = caps.name("minutes") {
            if let Ok(val) = std::str::from_utf8(m.as_bytes()).unwrap_or("0").parse::<f64>() {
                total += val * 60.0;
            }
        }
        if let Some(s) = caps.name("seconds") {
            if let Ok(val) = std::str::from_utf8(s.as_bytes()).unwrap_or("0").parse::<f64>() {
                total += val;
            }
        }
        if total > 0.0 { Some(total) } else { None }
    });

    // Stack trace detection
    let is_stacktrace = RE_STACKTRACE.is_match(line_bytes);

    // Error signature (first 80 chars of the stripped line)
    let stripped = line.trim();
    let error_signature = if error_category != ErrorCategory::Info
        && error_category != ErrorCategory::Warning
    {
        if stripped.len() <= 80 {
            Some(stripped.to_string())
        } else {
            let mut end = 80;
            while end > 0 && !stripped.is_char_boundary(end) {
                end -= 1;
            }
            Some(stripped[..end].to_string())
        }
    } else {
        None
    };

    // Line info (without timestamp/level — those are from Stage 1)
    let line_info = LineInfo {
        timestamp_us: 0,
        level: crate::types::LogLevel::Unknown,
        byte_offset: 0,
        line_number: line_num,
        line_length: line.len() as u32,
        message_preview: if line.len() <= 200 {
            line.to_string()
        } else {
            line[..200].to_string()
        },
    };

    ParseResult {
        line_info,
        raw_text: line.to_string(),
        error_category: if error_category == ErrorCategory::Info {
            None
        } else {
            Some(error_category)
        },
        file_paths,
        versions,
        duration_secs,
        is_stacktrace,
        error_signature,
    }
}

// ============================================================
// Public: single-pass scan (Stage 2 extension)
// ============================================================

/// Run a full single-pass parse (equivalent to Python `_single_pass_scan()`).
///
/// Extracts error lines, platform hints, and statistics in one pass.
/// This can be used as a drop-in replacement for `log_parser.py::_single_pass_scan()`.
pub fn full_single_pass(
    log_text: &str,
    max_error_lines: usize,
) -> SinglePassResult {
    let lines: Vec<&str> = log_text.lines().collect();
    let total_lines = lines.len();

    let mut error_lines: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut error_count: u64 = 0;
    let mut warning_count: u64 = 0;
    let mut fatal_count: u64 = 0;
    let mut platform_scores: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();

    // Platform detection patterns (abbreviated — full set in scanner)
    let log_lower = log_text.to_lowercase();

    for line in &lines {
        let stripped = line.trim();
        if stripped.is_empty() || stripped.len() < 5 {
            continue;
        }
        let line_lower = stripped.to_lowercase();

        // Error stats
        if line_lower.contains("fatal") {
            fatal_count += 1;
        }
        if line_lower.contains("error") {
            error_count += 1;
        }
        if line_lower.contains("warn") {
            warning_count += 1;
        }

        // Error line extraction
        if error_lines.len() < max_error_lines
            && RE_ERROR_KEYWORD.is_match(stripped.as_bytes())
        {
            if seen.insert(stripped.to_string()) {
                error_lines.push(stripped.to_string());
            }
        }
    }

    // Platform detection
    detect_platform_hint(&log_lower, &mut platform_scores);

    let platform = platform_scores
        .into_iter()
        .max_by_key(|(_, score)| *score)
        .map(|(name, _)| name)
        .unwrap_or_else(|| "Unknown".to_string());

    SinglePassResult {
        platform,
        error_lines,
        total_lines,
        error_count,
        warning_count,
        fatal_count,
    }
}

/// Result of a full single-pass parse.
#[derive(Debug, Clone)]
pub struct SinglePassResult {
    pub platform: String,
    pub error_lines: Vec<String>,
    pub total_lines: usize,
    pub error_count: u64,
    pub warning_count: u64,
    pub fatal_count: u64,
}

/// Detect CI/CD platform from log content.
fn detect_platform_hint(
    log_lower: &str,
    scores: &mut std::collections::HashMap<String, usize>,
) {
    let checks: &[(&str, &[&str])] = &[
        ("GitHub Actions", &["##[error]", "##[group]", "##[warning]", "run actions/", "error: process completed with exit code"]),
        ("Jenkins", &["finished: failure", "finished: success", "[pipeline]", "error: build step", "started by user"]),
        ("Docker", &["step ", "---> running in", "the command '/bin/sh -c", "returned a non-zero code", "error: failed to solve"]),
        ("npm", &["npm err!", "npm error", "npm warn", "eresolve could not resolve", "npm install"]),
        ("pip", &["error: could not find a version", "error: no matching distribution", "pip install", "resolutionimpossible"]),
        ("cargo", &["error[e0", "could not compile", "cargo build", "aborting due to"]),
        ("pytest", &["failures", "passed", "errors", "short test summary", "assert ", "assertionerror"]),
        ("jest", &["fail ", "tests:", "test suites:", "●", "expect(received)"]),
        ("Gradle", &["build failed", "build successful", "> task", "execution failed for task"]),
        ("Maven", &["build failure", "build success", "[error] failed to execute goal", "[info] build failure"]),
    ];

    for (platform, patterns) in checks {
        let score = patterns.iter().filter(|p| log_lower.contains(&p.to_lowercase())).count();
        if score > 0 {
            scores.insert(platform.to_string(), score);
        }
    }
}

// ============================================================
// Error type
// ============================================================

#[derive(Debug, thiserror::Error)]
pub enum ParserError {
    #[error("I/O error: {0}")]
    Io(String),
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_categorize_error_python() {
        assert_eq!(
            categorize_error("ImportError: No module named 'foo'"),
            ErrorCategory::ImportError
        );
        assert_eq!(
            categorize_error("TypeError: expected str, got int"),
            ErrorCategory::TypeError
        );
        assert_eq!(
            categorize_error("SyntaxError: invalid syntax at line 42"),
            ErrorCategory::SyntaxError
        );
        assert_eq!(
            categorize_error("ValueError: invalid literal for int()"),
            ErrorCategory::ValueError
        );
    }

    #[test]
    fn test_categorize_error_network() {
        assert_eq!(
            categorize_error("Connection refused: connect to 127.0.0.1:8080"),
            ErrorCategory::ConnectionError
        );
        assert_eq!(
            categorize_error("ECONNREFUSED: port not open"),
            ErrorCategory::ConnectionError
        );
    }

    #[test]
    fn test_categorize_build_error() {
        assert_eq!(
            categorize_error("Build failed: error[E0425]: cannot find value"),
            ErrorCategory::BuildError
        );
    }

    #[test]
    fn test_categorize_non_error() {
        assert_eq!(
            categorize_error("Server started successfully on port 8080"),
            ErrorCategory::Info
        );
        assert_eq!(
            categorize_error("WARN: deprecated API will be removed"),
            ErrorCategory::Warning
        );
    }

    #[test]
    fn test_extract_file_paths() {
        let parsed = parse_single_line(
            "ERROR: File \"/home/user/project/src/main.py\", line 42, in process",
            1,
        );
        assert!(!parsed.file_paths.is_empty());
        assert!(parsed.file_paths.iter().any(|p| p.contains("main.py")));
    }

    #[test]
    fn test_extract_versions() {
        let parsed = parse_single_line(
            "ERROR: package react@18.2.0 requires react-dom@18.2.0 but got 17.0.1",
            1,
        );
        assert!(parsed.versions.len() >= 2);
        assert!(parsed.versions.contains(&"18.2.0".to_string()));
    }

    #[test]
    fn test_duration_extraction() {
        let parsed = parse_single_line(
            "INFO: Build took 5 minutes 30 seconds to complete",
            1,
        );
        assert!(parsed.duration_secs.is_some());
        let dur = parsed.duration_secs.unwrap();
        assert!((dur - 330.0).abs() < 1.0); // 5*60 + 30 = 330
    }

    #[test]
    fn test_error_signature() {
        let parsed = parse_single_line(
            "ERROR: Failed to connect to database at localhost:5432 — connection refused",
            1,
        );
        assert!(parsed.error_signature.is_some());
        let sig = parsed.error_signature.unwrap();
        assert!(sig.len() <= 80);
        assert!(sig.contains("Failed to connect"));
    }

    #[test]
    fn test_full_single_pass() {
        let log_text = "\
2024-01-15T10:30:45Z INFO Server started
2024-01-15T10:30:46Z ERROR Connection failed
2024-01-15T10:30:47Z WARN Memory usage high
2024-01-15T10:30:48Z FATAL Out of memory
2024-01-15T10:30:49Z ERROR Timeout connecting to upstream
##[error]Process completed with exit code 1
";

        let result = full_single_pass(log_text, 30);

        assert_eq!(result.total_lines, 6);
        assert_eq!(result.error_count, 2);
        assert_eq!(result.warning_count, 1);
        assert_eq!(result.fatal_count, 1);
        assert_eq!(result.platform, "GitHub Actions");
        assert!(result.error_lines.len() >= 2);
    }
}
