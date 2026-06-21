//! Tauri command handlers for the LogPilot desktop app.
//!
//! These commands are exposed to the frontend (React/Vue/Vanilla) via
//! Tauri's IPC bridge. The frontend calls `invoke("scan_log_stage1", {...})`
//! and receives JSON-serialized results.
//!
//! ## API Design
//!
//! The commands follow the staged loading pattern:
//!
//! 1. **scan_log_stage1** — Fast scan of an entire log file.
//!    Returns per-line metadata (timestamp, level, offset, line number, preview).
//!    The frontend uses this to render a scrollable log view with filtering.
//!
//! 2. **parse_log_range** — Parse a specific range of lines with full field
//!    extraction. Called when the user scrolls to or opens a section of the log.
//!
//! 3. **hydrate_log_detail** — Deep-parse a single log entry when clicked.
//!    Returns all structured fields, error categorization, and context.
//!
//! 4. **full_single_pass** — Replacement for `log_parser.py::_single_pass_scan()`.
//!    Returns platform detection, error lines, and statistics in one call.
//!
//! ## State Management
//!
//! The Tauri app maintains a `ParserState` in Tauri's managed state,
//! which holds the current scan results in memory so the frontend
//! doesn't need to re-request data it already has.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::State;

use crate::filters::FilterOptions;
use crate::parser::{full_single_pass, hydrate_log_detail, parse_log_range, categorize_error};
use crate::scanner::scan_log_stage1;
use crate::types::{LineInfo, LogLevel, ScanResult, TimeRange};

// ============================================================
// Managed state
// ============================================================

/// Application state holding the current scan results.
pub struct ParserState {
    /// The most recent Stage 1 scan result (lines + stats).
    pub current_scan: Mutex<Option<ScanResult>>,
    /// The file path of the currently loaded log.
    pub current_file: Mutex<Option<PathBuf>>,
}

impl Default for ParserState {
    fn default() -> Self {
        Self {
            current_scan: Mutex::new(None),
            current_file: Mutex::new(None),
        }
    }
}

// ============================================================
// Request/Response types
// ============================================================

#[derive(Debug, Serialize, Deserialize)]
pub struct ScanRequest {
    /// Absolute path to the log file.
    pub file_path: String,
    /// Minimum log level filter (e.g., "ERROR").
    pub min_level: Option<String>,
    /// Keyword filter (case-insensitive substring match).
    pub keyword: Option<String>,
    /// Start of time range in Unix microseconds.
    pub time_start_us: Option<i64>,
    /// End of time range in Unix microseconds.
    pub time_end_us: Option<i64>,
    /// Maximum lines to return (for large files, return a sample).
    pub max_lines: Option<usize>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ScanResponse {
    /// Status: "ok" or "error".
    pub status: String,
    /// Per-line metadata (empty on error).
    pub lines: Vec<LineInfoDto>,
    /// Aggregate statistics.
    pub stats: Option<StatsDto>,
    /// Error message (only when status == "error").
    pub error: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct LineInfoDto {
    pub timestamp_us: i64,
    pub level: String,
    pub byte_offset: u64,
    pub line_number: u32,
    pub line_length: u32,
    pub message_preview: String,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct StatsDto {
    pub total_lines: u64,
    pub file_size_bytes: u64,
    pub lines_with_timestamp: u64,
    pub timestamp_coverage: f64,
    pub level_distribution: HashMap<String, u64>,
    pub time_range_min_us: Option<i64>,
    pub time_range_max_us: Option<i64>,
    pub scan_duration_ms: f64,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ParseRangeRequest {
    pub file_path: String,
    pub start_line: Option<u64>,
    pub end_line: Option<u64>,
    pub max_results: Option<usize>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ParseDetailRequest {
    pub raw_line: String,
    pub timestamp_us: Option<i64>,
    pub level: Option<String>,
    pub byte_offset: Option<u64>,
    pub line_number: Option<u32>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SinglePassRequest {
    pub log_text: String,
    pub max_error_lines: Option<usize>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct SinglePassResponse {
    pub platform: String,
    pub error_lines: Vec<String>,
    pub total_lines: usize,
    pub error_count: u64,
    pub warning_count: u64,
    pub fatal_count: u64,
}

// ============================================================
// Tauri Commands
// ============================================================

/// Stage 1: Fast scan of a log file.
///
/// Returns per-line metadata suitable for rendering a scrollable log view.
/// The result is also stored in `ParserState` for subsequent queries.
#[tauri::command]
pub fn scan_log_stage1_command(
    state: State<ParserState>,
    request: ScanRequest,
) -> ScanResponse {
    let path = std::path::Path::new(&request.file_path);

    if !path.exists() {
        return ScanResponse {
            status: "error".into(),
            lines: vec![],
            stats: None,
            error: Some(format!("File not found: {}", request.file_path)),
        };
    }

    // Build filter options
    let mut filter = FilterOptions::new();
    if let Some(ref level_str) = request.min_level {
        filter.min_level = Some(LogLevel::from_str(level_str));
    }
    filter.keyword = request.keyword.clone();
    if let (Some(start), Some(end)) = (request.time_start_us, request.time_end_us) {
        filter = filter.time_range(start, end);
    }
    filter.max_lines = request.max_lines;

    match scan_log_stage1(path, Some(&filter), None) {
        Ok(result) => {
            let lines: Vec<LineInfoDto> = result
                .lines
                .iter()
                .map(|li| LineInfoDto {
                    timestamp_us: li.timestamp_us,
                    level: li.level.as_str().to_string(),
                    byte_offset: li.byte_offset,
                    line_number: li.line_number,
                    line_length: li.line_length,
                    message_preview: li.message_preview.clone(),
                })
                .collect();

            let stats = StatsDto {
                total_lines: result.stats.total_lines,
                file_size_bytes: result.stats.file_size_bytes,
                lines_with_timestamp: result.stats.lines_with_timestamp,
                timestamp_coverage: result.stats.timestamp_coverage,
                level_distribution: result.stats.level_distribution.clone(),
                time_range_min_us: result.stats.time_range_min_us,
                time_range_max_us: result.stats.time_range_max_us,
                scan_duration_ms: result.stats.scan_duration_ms,
            };

            // Store in state for subsequent queries
            let _ = state.current_file.lock().map(|mut f| *f = Some(path.to_path_buf()));
            let _ = state.current_scan.lock().map(|mut s| *s = Some(result));

            ScanResponse {
                status: "ok".into(),
                lines,
                stats: Some(stats),
                error: None,
            }
        }
        Err(e) => ScanResponse {
            status: "error".into(),
            lines: vec![],
            stats: None,
            error: Some(format!("Scan failed: {}", e)),
        },
    }
}

/// Stage 2: Parse a range of log lines with full field extraction.
#[tauri::command]
pub fn parse_log_range_command(
    state: State<ParserState>,
    request: ParseRangeRequest,
) -> serde_json::Value {
    let path = std::path::Path::new(&request.file_path);

    match parse_log_range(path, request.start_line, request.end_line, request.max_results) {
        Ok(results) => {
            let parsed: Vec<serde_json::Value> = results
                .iter()
                .map(|r| {
                    serde_json::json!({
                        "line_info": {
                            "timestamp_us": r.line_info.timestamp_us,
                            "level": r.line_info.level.as_str(),
                            "byte_offset": r.line_info.byte_offset,
                            "line_number": r.line_info.line_number,
                            "line_length": r.line_info.line_length,
                            "message_preview": r.line_info.message_preview,
                        },
                        "raw_text": r.raw_text,
                        "error_category": r.error_category.as_ref().map(|c| c.as_str()),
                        "file_paths": r.file_paths,
                        "versions": r.versions,
                        "duration_secs": r.duration_secs,
                        "is_stacktrace": r.is_stacktrace,
                        "error_signature": r.error_signature,
                    })
                })
                .collect();

            serde_json::json!({
                "status": "ok",
                "results": parsed,
                "count": results.len(),
            })
        }
        Err(e) => {
            serde_json::json!({
                "status": "error",
                "error": format!("{}", e),
            })
        }
    }
}

/// Stage 3: Deep-parse a single log entry.
#[tauri::command]
pub fn hydrate_log_detail_command(
    state: State<ParserState>,
    request: ParseDetailRequest,
) -> serde_json::Value {
    let line_info = LineInfo {
        timestamp_us: request.timestamp_us.unwrap_or(0),
        level: request
            .level
            .map(|s| LogLevel::from_str(&s))
            .unwrap_or(LogLevel::Unknown),
        byte_offset: request.byte_offset.unwrap_or(0),
        line_number: request.line_number.unwrap_or(0),
        line_length: request.raw_line.len() as u32,
        message_preview: if request.raw_line.len() <= 200 {
            request.raw_line.clone()
        } else {
            request.raw_line[..200].to_string()
        },
    };

    let result = hydrate_log_detail(&request.raw_line, Some(&line_info));

    serde_json::json!({
        "status": "ok",
        "line_info": {
            "timestamp_us": result.line_info.timestamp_us,
            "level": result.line_info.level.as_str(),
            "byte_offset": result.line_info.byte_offset,
            "line_number": result.line_info.line_number,
            "line_length": result.line_info.line_length,
            "message_preview": result.line_info.message_preview,
        },
        "raw_text": result.raw_text,
        "error_category": result.error_category.as_ref().map(|c| c.as_str()),
        "file_paths": result.file_paths,
        "versions": result.versions,
        "duration_secs": result.duration_secs,
        "is_stacktrace": result.is_stacktrace,
        "error_signature": result.error_signature,
    })
}

/// Full single-pass: Replaces `log_parser.py::_single_pass_scan()`.
#[tauri::command]
pub fn full_single_pass_command(request: SinglePassRequest) -> SinglePassResponse {
    let result = full_single_pass(&request.log_text, request.max_error_lines.unwrap_or(30));

    SinglePassResponse {
        platform: result.platform,
        error_lines: result.error_lines,
        total_lines: result.total_lines,
        error_count: result.error_count,
        warning_count: result.warning_count,
        fatal_count: result.fatal_count,
    }
}

/// Categorize a single error line.
#[tauri::command]
pub fn categorize_error_command(line: String) -> String {
    let category = categorize_error(&line);
    category.as_str().to_string()
}

/// Get current scan statistics from state (no re-scan).
#[tauri::command]
pub fn get_scan_stats(state: State<ParserState>) -> serde_json::Value {
    if let Ok(guard) = state.current_scan.lock() {
        if let Some(ref scan) = *guard {
            return serde_json::json!({
                "status": "ok",
                "stats": {
                    "total_lines": scan.stats.total_lines,
                    "file_size_bytes": scan.stats.file_size_bytes,
                    "lines_with_timestamp": scan.stats.lines_with_timestamp,
                    "timestamp_coverage": scan.stats.timestamp_coverage,
                    "level_distribution": scan.stats.level_distribution,
                    "time_range_min_us": scan.stats.time_range_min_us,
                    "time_range_max_us": scan.stats.time_range_max_us,
                    "scan_duration_ms": scan.stats.scan_duration_ms,
                }
            });
        }
    }
    serde_json::json!({
        "status": "no_data",
        "stats": null,
    })
}
