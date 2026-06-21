//! Fast log file scanner (Stage 1).
//!
//! Performs a single-pass scan of a log file extracting:
//! - Byte offset and line number for each line
//! - Timestamp (normalized to Unix microseconds)
//! - Log level (FATAL/ERROR/WARN/INFO/DEBUG/TRACE/UNKNOWN)
//! - Message preview (first 200 chars)
//!
//! This replaces `log_indexer.py::build_index()` and is the primary
//! hot-path optimization. Uses memory-mapped I/O (`memmap2`) for
//! large files and a fast buffered reader for smaller ones.
//!
//! ## Performance Design
//!
//! 1. **Memory-mapped I/O**: For files > 10 MB, uses `mmap` to avoid
//!    userspace copies. The OS handles page caching optimally.
//!
//! 2. **Single pass**: Timestamp extraction, level detection, and line
//!    splitting all happen in one traversal of the file bytes.
//!
//! 3. **Zero-copy where possible**: Line bytes are sliced from the mmap
//!    buffer rather than copied.
//!
//! 4. **Preallocated buffers**: Vec with capacity hints avoids reallocation
//!    during scan.

use std::collections::HashMap;
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::time::Instant;

use memmap2::Mmap;

use crate::filters::{FilterOptions, LineFilter};
use crate::level::detect_level_bytes;
use crate::timestamp::extract_timestamp_us;
use crate::types::{
    LineInfo, LogLevel, ScanResult, ScanStats, MAX_PREVIEW_LENGTH, NO_TIMESTAMP,
};

// ============================================================
// Thresholds
// ============================================================

/// Files larger than this use memory-mapped I/O.
const MMAP_THRESHOLD: u64 = 10 * 1024 * 1024; // 10 MB

/// Progress callback interval (every N lines).
const PROGRESS_INTERVAL: u64 = 50_000;

// ============================================================
// Public API
// ============================================================

/// Scan a log file and extract per-line metadata (Stage 1).
///
/// This is the main entry point. It replaces `log_indexer.py::build_index()`.
///
/// # Arguments
///
/// * `file_path` - Path to the log file.
/// * `filter_opts` - Optional filters to apply during scan.
/// * `progress_callback` - Optional callback invoked every ~50k lines
///   with `(current_line, estimated_total_lines)`.
///
/// # Returns
///
/// `ScanResult` containing per-line metadata and aggregate statistics.
pub fn scan_log_stage1(
    file_path: &Path,
    filter_opts: Option<&FilterOptions>,
    progress_callback: Option<&dyn Fn(u64, u64)>,
) -> Result<ScanResult, ScannerError> {
    let start_time = Instant::now();

    let file_size = fs::metadata(file_path)
        .map_err(|e| ScannerError::Io(e.to_string()))?
        .len();

    let filter = filter_opts.map(LineFilter::from_options).unwrap_or_default();

    // Estimate line count for preallocation (assume ~200 bytes/line average)
    let estimated_lines = (file_size / 200).max(1000) as usize;

    // Choose scan strategy based on file size
    if file_size >= MMAP_THRESHOLD {
        scan_mmap(file_path, file_size, estimated_lines, &filter, progress_callback, start_time)
    } else {
        scan_buffered(file_path, file_size, estimated_lines, &filter, progress_callback, start_time)
    }
}

// ============================================================
// Memory-mapped scan (for files >= 10 MB)
// ============================================================

fn scan_mmap(
    file_path: &Path,
    file_size: u64,
    estimated_lines: usize,
    filter: &LineFilter,
    progress_callback: Option<&dyn Fn(u64, u64)>,
    start_time: Instant,
) -> Result<ScanResult, ScannerError> {
    let file = fs::File::open(file_path)
        .map_err(|e| ScannerError::Io(format!("Failed to open: {}", e)))?;

    // SAFETY: The file is read-only and we don't mutate it.
    let mmap = unsafe {
        Mmap::map(&file).map_err(|e| ScannerError::Io(format!("Failed to mmap: {}", e)))?
    };

    let data = mmap.as_ref();
    let data_len = data.len();

    let mut lines: Vec<LineInfo> = Vec::with_capacity(estimated_lines);
    let mut level_counts: HashMap<String, u64> = HashMap::new();
    let mut t_min: Option<i64> = None;
    let mut t_max: Option<i64> = None;
    let mut timestamps_found: u64 = 0;
    let mut current_offset: u64 = 0;
    let mut line_num: u64 = 0;
    let mut last_progress: u64 = 0;
    let mut line_start: usize = 0;

    for (pos, &byte) in data.iter().enumerate() {
        if byte == b'\n' {
            line_num += 1;
            let line_bytes = &data[line_start..pos];
            let line_len = (pos - line_start + 1) as u64; // +1 for newline

            // Process the line
            let (ts, level, preview) = process_line_bytes(line_bytes, line_len as usize);

            if ts != NO_TIMESTAMP {
                timestamps_found += 1;
                t_min = Some(t_min.map_or(ts, |v| v.min(ts)));
                t_max = Some(t_max.map_or(ts, |v| v.max(ts)));
            }

            *level_counts.entry(level.as_str().to_string()).or_insert(0) += 1;

            let info = LineInfo {
                timestamp_us: ts,
                level,
                byte_offset: current_offset,
                line_number: line_num as u32,
                line_length: line_len as u32,
                message_preview: preview,
            };

            // Apply filter (excluding keyword — that needs the full text)
            if filter.matches_info(&info) {
                lines.push(info);
            }

            current_offset += line_len;
            line_start = pos + 1;

            // Progress reporting
            if let Some(ref cb) = progress_callback {
                if line_num - last_progress >= PROGRESS_INTERVAL {
                    cb(line_num, estimated_lines as u64);
                    last_progress = line_num;
                }
            }
        }
    }

    // Handle trailing data without newline
    if line_start < data_len {
        line_num += 1;
        let line_bytes = &data[line_start..];
        let line_len = (data_len - line_start) as u64;

        let (ts, level, preview) = process_line_bytes(line_bytes, line_len as usize);

        if ts != NO_TIMESTAMP {
            timestamps_found += 1;
            t_min = Some(t_min.map_or(ts, |v| v.min(ts)));
            t_max = Some(t_max.map_or(ts, |v| v.max(ts)));
        }

        *level_counts.entry(level.as_str().to_string()).or_insert(0) += 1;

        let info = LineInfo {
            timestamp_us: ts,
            level,
            byte_offset: current_offset,
            line_number: line_num as u32,
            line_length: line_len as u32,
            message_preview: preview,
        };

        if filter.matches_info(&info) {
            lines.push(info);
        }
    }

    let elapsed_ms = start_time.elapsed().as_secs_f64() * 1000.0;
    let total_lines = line_num;

    Ok(ScanResult {
        lines,
        stats: ScanStats {
            total_lines,
            file_size_bytes: file_size,
            lines_with_timestamp: timestamps_found,
            timestamp_coverage: if total_lines > 0 {
                timestamps_found as f64 / total_lines as f64
            } else {
                0.0
            },
            level_distribution: level_counts,
            time_range_min_us: t_min,
            time_range_max_us: t_max,
            scan_duration_ms: elapsed_ms,
        },
    })
}

// ============================================================
// Buffered scan (for files < 10 MB)
// ============================================================

fn scan_buffered(
    file_path: &Path,
    file_size: u64,
    estimated_lines: usize,
    filter: &LineFilter,
    progress_callback: Option<&dyn Fn(u64, u64)>,
    start_time: Instant,
) -> Result<ScanResult, ScannerError> {
    let file = fs::File::open(file_path)
        .map_err(|e| ScannerError::Io(format!("Failed to open: {}", e)))?;

    let mut reader = BufReader::with_capacity(1024 * 1024, file); // 1MB buffer

    let mut lines: Vec<LineInfo> = Vec::with_capacity(estimated_lines);
    let mut level_counts: HashMap<String, u64> = HashMap::new();
    let mut t_min: Option<i64> = None;
    let mut t_max: Option<i64> = None;
    let mut timestamps_found: u64 = 0;
    let mut current_offset: u64 = 0;
    let mut line_num: u64 = 0;
    let mut last_progress: u64 = 0;

    let mut buf = Vec::with_capacity(64 * 1024); // 64KB line buffer

    loop {
        buf.clear();
        let bytes_read = reader
            .read_until(b'\n', &mut buf)
            .map_err(|e| ScannerError::Io(format!("Read error: {}", e)))?;

        if bytes_read == 0 {
            break; // EOF
        }

        line_num += 1;

        let (ts, level, preview) = process_line_bytes(&buf, bytes_read);

        if ts != NO_TIMESTAMP {
            timestamps_found += 1;
            t_min = Some(t_min.map_or(ts, |v| v.min(ts)));
            t_max = Some(t_max.map_or(ts, |v| v.max(ts)));
        }

        *level_counts.entry(level.as_str().to_string()).or_insert(0) += 1;

        let info = LineInfo {
            timestamp_us: ts,
            level,
            byte_offset: current_offset,
            line_number: line_num as u32,
            line_length: bytes_read as u32,
            message_preview: preview,
        };

        if filter.matches_info(&info) {
            lines.push(info);
        }

        current_offset += bytes_read as u64;

        // Progress reporting
        if let Some(ref cb) = progress_callback {
            if line_num - last_progress >= PROGRESS_INTERVAL {
                cb(line_num, estimated_lines as u64);
                last_progress = line_num;
            }
        }
    }

    let elapsed_ms = start_time.elapsed().as_secs_f64() * 1000.0;

    Ok(ScanResult {
        lines,
        stats: ScanStats {
            total_lines: line_num,
            file_size_bytes: file_size,
            lines_with_timestamp: timestamps_found,
            timestamp_coverage: if line_num > 0 {
                timestamps_found as f64 / line_num as f64
            } else {
                0.0
            },
            level_distribution: level_counts,
            time_range_min_us: t_min,
            time_range_max_us: t_max,
            scan_duration_ms: elapsed_ms,
        },
    })
}

// ============================================================
// Per-line processing (shared by both scan strategies)
// ============================================================

/// Process a single line: extract timestamp, level, and preview.
/// Returns `(timestamp_us, level, preview_string)`.
#[inline]
fn process_line_bytes(line_bytes: &[u8], _line_len: usize) -> (i64, LogLevel, String) {
    // Strip trailing \r\n for preview
    let content_bytes = if line_bytes.ends_with(b"\r\n") {
        &line_bytes[..line_bytes.len() - 2]
    } else if line_bytes.last() == Some(&b'\n') || line_bytes.last() == Some(&b'\r') {
        &line_bytes[..line_bytes.len() - 1]
    } else {
        line_bytes
    };

    // Timestamp extraction
    let ts = extract_timestamp_us(content_bytes).unwrap_or(NO_TIMESTAMP);

    // Level detection
    let level = detect_level_bytes(content_bytes);

    // Preview: first MAX_PREVIEW_LENGTH chars
    let preview = if content_bytes.len() <= MAX_PREVIEW_LENGTH {
        String::from_utf8_lossy(content_bytes).to_string()
    } else {
        // Find a valid UTF-8 boundary within the first 200 bytes
        let mut end = MAX_PREVIEW_LENGTH;
        while end > 0 && !is_char_boundary(content_bytes[end]) {
            end -= 1;
        }
        String::from_utf8_lossy(&content_bytes[..end]).to_string()
    };

    (ts, level, preview)
}

/// Check if a byte position is a valid UTF-8 character boundary.
#[inline]
fn is_char_boundary(byte: u8) -> bool {
    // In UTF-8, a byte is a character boundary if it's:
    // - 0xxxxxxx (ASCII)
    // - 10xxxxxx (continuation byte — NOT a boundary)
    // - 11xxxxxx (start byte — IS a boundary)
    byte & 0xC0 != 0x80
}

// ============================================================
// Scanner error
// ============================================================

#[derive(Debug, thiserror::Error)]
pub enum ScannerError {
    #[error("I/O error: {0}")]
    Io(String),
}

// ============================================================
// Tests
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;

    fn create_test_log() -> (NamedTempFile, String) {
        let mut file = NamedTempFile::new().unwrap();
        let content = "\
2024-01-15T10:30:45.123Z INFO Server started on port 8080
2024-01-15T10:30:46.000Z DEBUG Initializing database connection pool with max_connections=100
2024-01-15T10:30:47.500Z WARN Disk usage at 85%, consider cleanup
2024-01-15T10:30:48.000Z ERROR Failed to connect to upstream service: connection refused
2024-01-15T10:30:49.100Z FATAL Critical system failure, shutting down
2024-01-15T10:30:50.000Z INFO Shutdown complete
Just a plain message without level
";
        write!(file, "{}", content).unwrap();
        let path = file.path().to_string_lossy().to_string();
        (file, path)
    }

    #[test]
    fn test_scan_stage1_basic() {
        let (_file, path) = create_test_log();
        let result = scan_log_stage1(Path::new(&path), None, None).unwrap();

        assert_eq!(result.stats.total_lines, 7);
        assert!(result.stats.lines_with_timestamp >= 6);
        assert!(result.stats.timestamp_coverage > 0.8);

        let levels = &result.stats.level_distribution;
        assert!(levels.get("INFO").copied().unwrap_or(0) >= 2);
        assert!(levels.get("ERROR").copied().unwrap_or(0) >= 1);
        assert!(levels.get("FATAL").copied().unwrap_or(0) >= 1);
        assert!(levels.get("WARN").copied().unwrap_or(0) >= 1);
        assert!(levels.get("DEBUG").copied().unwrap_or(0) >= 1);
    }

    #[test]
    fn test_scan_stage1_with_level_filter() {
        let (_file, path) = create_test_log();
        let filter = FilterOptions::new().min_level(LogLevel::Error);
        let result = scan_log_stage1(Path::new(&path), Some(&filter), None).unwrap();

        // Should only include ERROR and FATAL
        for line in &result.lines {
            assert!(line.level >= LogLevel::Error);
        }
    }

    #[test]
    fn test_line_info_fields() {
        let (_file, path) = create_test_log();
        let result = scan_log_stage1(Path::new(&path), None, None).unwrap();

        // Check first line
        let first = &result.lines[0];
        assert_eq!(first.line_number, 1);
        assert!(first.byte_offset == 0);
        assert!(first.timestamp_us > 0);
        assert_eq!(first.level, LogLevel::Info);
        assert!(first.message_preview.contains("Server started"));
    }

    #[test]
    fn test_byte_offsets_sequential() {
        let (_file, path) = create_test_log();
        let result = scan_log_stage1(Path::new(&path), None, None).unwrap();

        // Each line's byte_offset + line_length should equal the next line's byte_offset
        for i in 0..result.lines.len() - 1 {
            let curr_end = result.lines[i].byte_offset + result.lines[i].line_length as u64;
            assert_eq!(
                curr_end,
                result.lines[i + 1].byte_offset,
                "Line {} byte offset mismatch: {} + {} != {}",
                i,
                result.lines[i].byte_offset,
                result.lines[i].line_length,
                result.lines[i + 1].byte_offset
            );
        }
    }

    #[test]
    fn test_progress_callback() {
        let (_file, path) = create_test_log();
        let mut callbacks = Vec::new();
        let cb = |current: u64, estimated: u64| {
            callbacks.push((current, estimated));
        };

        let _result = scan_log_stage1(Path::new(&path), None, Some(&cb)).unwrap();
        // With only 7 lines, no progress callbacks should fire (threshold is 50k)
        // This is fine — the callback is for large files
    }

    #[test]
    fn test_missing_file() {
        let result = scan_log_stage1(Path::new("nonexistent_file.log"), None, None);
        assert!(result.is_err());
    }
}
