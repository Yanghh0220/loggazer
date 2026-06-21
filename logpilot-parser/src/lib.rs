//! # LogPilot Parser — High-Performance Log Parsing Engine
//!
//! This crate implements the hottest log parsing paths in Rust, replacing
//! the Python-based parsing in `log_indexer.py` and `log_parser.py`.
//!
//! ## Architecture
//!
//! The parser is organized into three stages to support progressive loading:
//!
//! - **Stage 1** (`scan_log_stage1`): Fast mmap-based scan returning timestamp,
//!   log level, byte offset, line number, and message preview for every line.
//!   This is the primary replacement for `log_indexer.py::build_index()`.
//!
//! - **Stage 2** (`parse_log_range`): Parse logs within a byte range or time
//!   range with full field extraction (structured/semi-structured).
//!
//! - **Stage 3** (`hydrate_log_detail`): Deep-parse a single log entry when the
//!   user clicks on it — full field extraction, stack trace parsing, etc.
//!
//! ## Module Structure
//!
//! - `types` — Core data types shared across all modules
//! - `scanner` — Fast line-by-line file scanner (Stage 1)
//! - `timestamp` — Timestamp detection and normalization to Unix microseconds
//! - `level` — Log level detection (FATAL/ERROR/WARN/INFO/DEBUG/TRACE/UNKNOWN)
//! - `filters` — Filtering by level, keyword, and time range
//! - `parser` — Full per-line parsing with field extraction (Stage 2/3)
//! - `python_bindings` — PyO3 bindings for Python integration
//! - `tauri_commands` — Tauri command handlers for desktop integration

pub mod types;
pub mod scanner;
pub mod timestamp;
pub mod level;
pub mod filters;
pub mod parser;

#[cfg(feature = "python-bindings")]
pub mod python_bindings;

#[cfg(feature = "tauri-commands")]
pub mod tauri_commands;

// Re-export the main public API
pub use types::{
    LineInfo, LogLevel, ParseResult, ScanResult, ScanStats, TimeRange,
};
pub use scanner::scan_log_stage1;
pub use parser::{parse_log_range, hydrate_log_detail, ErrorCategory};
pub use filters::FilterOptions;
