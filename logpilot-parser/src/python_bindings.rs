//! PyO3 bindings for the LogPilot Rust parser.
//!
//! Exposes the core parsing functionality to Python as native extension
//! functions. The Python code in `log_indexer.py` and `log_parser.py`
//! can import `logpilot_parser` and call these functions directly.
//!
//! ## Usage from Python
//!
//! ```python
//! import logpilot_parser
//!
//! # Stage 1: fast scan
//! result = logpilot_parser.scan_log_stage1("path/to/file.log")
//! print(f"Lines: {result['stats']['total_lines']}")
//! print(f"Timestamps: {result['stats']['lines_with_timestamp']}")
//!
//! # Stage 2: parse a range
//! parsed = logpilot_parser.parse_log_range("path/to/file.log", 1, 100)
//!
//! # Stage 3: hydrate a single line
//! detail = logpilot_parser.hydrate_log_detail("ERROR: disk full")
//!
//! # Full single-pass (replaces _single_pass_scan)
//! result = logpilot_parser.full_single_pass(log_text, max_error_lines=30)
//! ```

use std::collections::HashMap;
use std::path::Path;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::filters::FilterOptions;
use crate::parser::{full_single_pass, hydrate_log_detail, parse_log_range, categorize_error};
use crate::scanner::scan_log_stage1;
use crate::types::{LineInfo, LogLevel, ScanResult, ScanStats};

// ============================================================
// Helper: convert ScanResult to Python dict
// ============================================================

fn scan_result_to_py(py: Python<'_>, result: &ScanResult) -> PyResult<PyObject> {
    let dict = PyDict::new(py);

    // Lines: list of dicts
    let lines_list = PyList::empty(py);
    for line in &result.lines {
        let line_dict = PyDict::new(py);
        line_dict.set_item("timestamp_us", line.timestamp_us)?;
        line_dict.set_item("level", line.level.as_str())?;
        line_dict.set_item("byte_offset", line.byte_offset)?;
        line_dict.set_item("line_number", line.line_number)?;
        line_dict.set_item("line_length", line.line_length)?;
        line_dict.set_item("message_preview", &line.message_preview)?;
        lines_list.append(line_dict)?;
    }
    dict.set_item("lines", lines_list)?;

    // Stats
    let stats_dict = PyDict::new(py);
    stats_dict.set_item("total_lines", result.stats.total_lines)?;
    stats_dict.set_item("file_size_bytes", result.stats.file_size_bytes)?;
    stats_dict.set_item("lines_with_timestamp", result.stats.lines_with_timestamp)?;
    stats_dict.set_item("timestamp_coverage", result.stats.timestamp_coverage)?;
    stats_dict.set_item("scan_duration_ms", result.stats.scan_duration_ms)?;

    // Level distribution
    let level_dist = PyDict::new(py);
    for (k, v) in &result.stats.level_distribution {
        level_dist.set_item(k, *v)?;
    }
    stats_dict.set_item("level_distribution", level_dist)?;

    // Time range
    if let Some(min_us) = result.stats.time_range_min_us {
        stats_dict.set_item("time_range_min_us", min_us)?;
    } else {
        stats_dict.set_item("time_range_min_us", py.None())?;
    }
    if let Some(max_us) = result.stats.time_range_max_us {
        stats_dict.set_item("time_range_max_us", max_us)?;
    } else {
        stats_dict.set_item("time_range_max_us", py.None())?;
    }

    dict.set_item("stats", stats_dict)?;

    Ok(dict.into())
}

// ============================================================
// Python-exposed functions
// ============================================================

/// Python wrapper for `scan_log_stage1`.
///
/// Args:
///     file_path: Path to the log file.
///     min_level: Optional minimum log level string (e.g. "ERROR").
///     keyword: Optional keyword to filter lines.
///     time_start_us: Optional start of time range (Unix microseconds).
///     time_end_us: Optional end of time range (Unix microseconds).
///
/// Returns:
///     dict with "lines" (list of dicts) and "stats" (dict).
#[pyfunction]
#[pyo3(signature = (file_path, min_level=None, keyword=None, time_start_us=None, time_end_us=None))]
fn scan_log_stage1_py(
    py: Python<'_>,
    file_path: String,
    min_level: Option<String>,
    keyword: Option<String>,
    time_start_us: Option<i64>,
    time_end_us: Option<i64>,
) -> PyResult<PyObject> {
    let path = Path::new(&file_path);

    // Build filter options
    let mut filter = FilterOptions::new();
    if let Some(level_str) = &min_level {
        filter.min_level = Some(LogLevel::from_str(level_str));
    }
    filter.keyword = keyword;
    if let (Some(start), Some(end)) = (time_start_us, time_end_us) {
        filter = filter.time_range(start, end);
    }

    match scan_log_stage1(path, Some(&filter), None) {
        Ok(result) => scan_result_to_py(py, &result),
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
            "Scanner error: {}",
            e
        ))),
    }
}

/// Python wrapper for `parse_log_range`.
///
/// Args:
///     file_path: Path to the log file.
///     start_line: First line number (1-based, inclusive).
///     end_line: Last line number (1-based, inclusive).
///     max_results: Maximum results to return (default 1000).
///
/// Returns:
///     List of parsed line dicts.
#[pyfunction]
#[pyo3(signature = (file_path, start_line=None, end_line=None, max_results=1000))]
fn parse_log_range_py(
    py: Python<'_>,
    file_path: String,
    start_line: Option<u64>,
    end_line: Option<u64>,
    max_results: usize,
) -> PyResult<PyObject> {
    let path = Path::new(&file_path);

    match parse_log_range(path, start_line, end_line, Some(max_results)) {
        Ok(results) => {
            let list = PyList::empty(py);
            for r in &results {
                let dict = parse_result_to_py(py, r)?;
                list.append(dict)?;
            }
            Ok(list.into())
        }
        Err(e) => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
            "Parser error: {}",
            e
        ))),
    }
}

/// Python wrapper for `hydrate_log_detail`.
///
/// Args:
///     raw_line: The raw log line text.
///     timestamp_us: Optional pre-extracted timestamp.
///     level: Optional pre-detected log level string.
///     byte_offset: Optional byte offset.
///     line_number: Optional line number.
///
/// Returns:
///     Detailed parse result dict.
#[pyfunction]
#[pyo3(signature = (raw_line, timestamp_us=0i64, level=None, byte_offset=0u64, line_number=0u32))]
fn hydrate_log_detail_py(
    py: Python<'_>,
    raw_line: String,
    timestamp_us: i64,
    level: Option<String>,
    byte_offset: u64,
    line_number: u32,
) -> PyResult<PyObject> {
    let line_info = LineInfo {
        timestamp_us,
        level: level
            .map(|s| LogLevel::from_str(&s))
            .unwrap_or(LogLevel::Unknown),
        byte_offset,
        line_number,
        line_length: raw_line.len() as u32,
        message_preview: if raw_line.len() <= 200 {
            raw_line.clone()
        } else {
            raw_line[..200].to_string()
        },
    };

    let result = hydrate_log_detail(&raw_line, Some(&line_info));
    parse_result_to_py(py, &result)
}

/// Python wrapper for `full_single_pass`.
///
/// This is the direct replacement for `log_parser.py::_single_pass_scan()`.
///
/// Args:
///     log_text: Full log text content.
///     max_error_lines: Maximum number of error lines to extract (default 30).
///
/// Returns:
///     dict with platform, error_lines, total_lines, error_count,
///     warning_count, fatal_count.
#[pyfunction]
#[pyo3(signature = (log_text, max_error_lines=30))]
fn full_single_pass_py(py: Python<'_>, log_text: String, max_error_lines: usize) -> PyResult<PyObject> {
    let result = full_single_pass(&log_text, max_error_lines);

    let dict = PyDict::new(py);
    dict.set_item("platform", &result.platform)?;
    dict.set_item("error_lines", result.error_lines)?;
    dict.set_item("total_lines", result.total_lines)?;
    dict.set_item("error_count", result.error_count)?;
    dict.set_item("warning_count", result.warning_count)?;
    dict.set_item("fatal_count", result.fatal_count)?;

    Ok(dict.into())
}

/// Python wrapper for error categorization.
///
/// Categorizes a single error line into one of the known error types.
#[pyfunction]
fn categorize_error_py(line: String) -> String {
    let category = categorize_error(&line);
    category.as_str().to_string()
}

// ============================================================
// Helper: convert ParseResult to Python dict
// ============================================================

fn parse_result_to_py(py: Python<'_>, result: &crate::types::ParseResult) -> PyResult<PyObject> {
    let dict = PyDict::new(py);

    // Line info
    let li_dict = PyDict::new(py);
    li_dict.set_item("timestamp_us", result.line_info.timestamp_us)?;
    li_dict.set_item("level", result.line_info.level.as_str())?;
    li_dict.set_item("byte_offset", result.line_info.byte_offset)?;
    li_dict.set_item("line_number", result.line_info.line_number)?;
    li_dict.set_item("line_length", result.line_info.line_length)?;
    li_dict.set_item("message_preview", &result.line_info.message_preview)?;
    dict.set_item("line_info", li_dict)?;

    dict.set_item("raw_text", &result.raw_text)?;

    if let Some(ref cat) = result.error_category {
        dict.set_item("error_category", cat.as_str())?;
    } else {
        dict.set_item("error_category", py.None())?;
    }

    dict.set_item("file_paths", result.file_paths.clone())?;
    dict.set_item("versions", result.versions.clone())?;

    if let Some(dur) = result.duration_secs {
        dict.set_item("duration_secs", dur)?;
    } else {
        dict.set_item("duration_secs", py.None())?;
    }

    dict.set_item("is_stacktrace", result.is_stacktrace)?;

    if let Some(ref sig) = result.error_signature {
        dict.set_item("error_signature", sig)?;
    } else {
        dict.set_item("error_signature", py.None())?;
    }

    Ok(dict.into())
}

// ============================================================
// Module registration
// ============================================================

/// Python module initialization.
#[pymodule]
fn logpilot_parser(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(scan_log_stage1_py, m)?)?;
    m.add_function(wrap_pyfunction!(parse_log_range_py, m)?)?;
    m.add_function(wrap_pyfunction!(hydrate_log_detail_py, m)?)?;
    m.add_function(wrap_pyfunction!(full_single_pass_py, m)?)?;
    m.add_function(wrap_pyfunction!(categorize_error_py, m)?)?;
    Ok(())
}
