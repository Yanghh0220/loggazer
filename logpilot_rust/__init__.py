"""
LogPilot Rust Parser — Python Integration Layer

This module provides a Pythonic interface to the high-performance Rust
log parser. It falls back gracefully to the pure-Python implementation
when the Rust extension is not available.

Usage:
    from logpilot_rust import scan_log_stage1, full_single_pass

    # Stage 1: Fast scan (replaces log_indexer.py::build_index())
    result = scan_log_stage1("path/to/file.log")
    print(f"Lines: {result['stats']['total_lines']}")

    # With filters
    result = scan_log_stage1(
        "path/to/file.log",
        min_level="ERROR",
        keyword="connection",
        time_start_us=1705300000000000,
        time_end_us=1705400000000000,
    )

    # Full single pass (replaces log_parser.py::_single_pass_scan())
    result = full_single_pass(log_text, max_error_lines=30)

    # Parse a range (Stage 2)
    parsed = parse_log_range("path/to/file.log", start_line=1, end_line=100)

    # Hydrate detail (Stage 3)
    detail = hydrate_log_detail("ERROR: disk full at /dev/sda1")

    # Error categorization
    category = categorize_error("ImportError: No module named 'foo'")

Integration with existing code:

    In log_indexer.py, replace:
        from log_indexer import build_index
    with:
        from logpilot_rust import scan_log_stage1 as build_index_rust

    In log_parser.py, replace:
        from log_parser import _single_pass_scan
    with:
        from logpilot_rust import full_single_pass as _single_pass_scan
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================
# Try to import the native Rust extension
# ============================================================

_RUST_AVAILABLE = False
_logpilot_parser = None

try:
    import logpilot_parser as _logpilot_parser
    _RUST_AVAILABLE = True
    logger.info("LogPilot Rust parser loaded successfully")
except ImportError:
    logger.warning(
        "LogPilot Rust parser not available. "
        "Install it with: `cd logpilot-parser && maturin develop --release`\n"
        "Falling back to pure Python implementation."
    )

# ============================================================
# Public API — delegates to Rust or Python fallback
# ============================================================


def scan_log_stage1(
    file_path: str,
    min_level: Optional[str] = None,
    keyword: Optional[str] = None,
    time_start_us: Optional[int] = None,
    time_end_us: Optional[int] = None,
    progress_callback: Optional[callable] = None,
) -> Dict[str, Any]:
    """
    Stage 1: Fast scan of a log file.

    Extracts per-line metadata: timestamp, log level, byte offset,
    line number, line length, and message preview.

    Args:
        file_path: Path to the log file.
        min_level: Optional minimum log level filter (e.g., "ERROR").
        keyword: Optional keyword filter (case-insensitive).
        time_start_us: Start of time range in Unix microseconds.
        time_end_us: End of time range in Unix microseconds.
        progress_callback: Optional callback(current_line, estimated_total).

    Returns:
        dict with:
            - lines: list of per-line dicts
            - stats: aggregate statistics dict
    """
    if _RUST_AVAILABLE:
        try:
            return _logpilot_parser.scan_log_stage1_py(
                file_path,
                min_level=min_level,
                keyword=keyword,
                time_start_us=time_start_us,
                time_end_us=time_end_us,
            )
        except Exception as e:
            logger.warning("Rust scan failed, falling back to Python: %s", e)

    # Python fallback: use log_indexer
    return _py_scan_log_stage1(
        file_path, min_level, keyword, time_start_us, time_end_us, progress_callback
    )


def parse_log_range(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_results: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Stage 2: Parse a range of log lines with full field extraction.

    Args:
        file_path: Path to the log file.
        start_line: First line number (1-based, inclusive).
        end_line: Last line number (1-based, inclusive).
        max_results: Maximum results to return.

    Returns:
        List of parsed line dicts with error_category, file_paths,
        versions, duration_secs, is_stacktrace, etc.
    """
    if _RUST_AVAILABLE:
        try:
            return _logpilot_parser.parse_log_range_py(
                file_path,
                start_line=start_line,
                end_line=end_line,
                max_results=max_results,
            )
        except Exception as e:
            logger.warning("Rust parse_range failed: %s", e)

    # Python fallback: read lines directly
    return _py_parse_log_range(file_path, start_line, end_line, max_results)


def hydrate_log_detail(
    raw_line: str,
    timestamp_us: int = 0,
    level: Optional[str] = None,
    byte_offset: int = 0,
    line_number: int = 0,
) -> Dict[str, Any]:
    """
    Stage 3: Deep-parse a single log entry.

    Args:
        raw_line: The raw log line text.
        timestamp_us: Pre-extracted Unix microseconds timestamp.
        level: Pre-detected log level string.
        byte_offset: Byte offset in the source file.
        line_number: 1-based line number.

    Returns:
        Detailed parse result dict.
    """
    if _RUST_AVAILABLE:
        try:
            return _logpilot_parser.hydrate_log_detail_py(
                raw_line,
                timestamp_us=timestamp_us,
                level=level,
                byte_offset=byte_offset,
                line_number=line_number,
            )
        except Exception as e:
            logger.warning("Rust hydrate_detail failed: %s", e)

    return _py_hydrate_log_detail(raw_line, timestamp_us, level, byte_offset, line_number)


def full_single_pass(
    log_text: str,
    max_error_lines: int = 30,
) -> Dict[str, Any]:
    """
    Full single-pass scan of log text.

    This directly replaces `log_parser.py::_single_pass_scan()`.

    Returns:
        dict with: platform, error_lines, total_lines, error_count,
        warning_count, fatal_count
    """
    if _RUST_AVAILABLE:
        try:
            return _logpilot_parser.full_single_pass_py(
                log_text, max_error_lines=max_error_lines
            )
        except Exception as e:
            logger.warning("Rust full_single_pass failed: %s", e)

    return _py_full_single_pass(log_text, max_error_lines)


def categorize_error(line: str) -> str:
    """Categorize a single error line into a known error type."""
    if _RUST_AVAILABLE:
        try:
            return _logpilot_parser.categorize_error_py(line)
        except Exception:
            pass
    return _py_categorize_error(line)


# ============================================================
# Python fallback implementations
# ============================================================


def _py_scan_log_stage1(
    file_path: str,
    min_level: Optional[str],
    keyword: Optional[str],
    time_start_us: Optional[int],
    time_end_us: Optional[int],
    progress_callback: Optional[callable],
) -> Dict[str, Any]:
    """Python fallback using log_indexer.build_index()."""
    import time

    from log_indexer import build_index as _build_index

    start = time.time()
    index_path, stats = _build_index(file_path, progress_callback=progress_callback)

    # Build line list from the index
    from log_indexer import load_index

    try:
        df = load_index(index_path)
        lines = []
        for _, row in df.iterrows():
            line_info = {
                "timestamp_us": int(row["timestamp_us"]),
                "level": row["level"],
                "byte_offset": int(row["byte_offset"]),
                "line_number": int(row["line_number"]),
                "line_length": int(row["line_length"]),
                "message_preview": row["message_preview"],
            }

            # Apply filters
            if min_level:
                from logpilot_rust import _level_severity
                if _level_severity(line_info["level"]) < _level_severity(min_level):
                    continue
            if keyword and keyword.lower() not in line_info["message_preview"].lower():
                continue
            if time_start_us and time_end_us:
                ts = line_info["timestamp_us"]
                if ts and (ts < time_start_us or ts > time_end_us):
                    continue

            lines.append(line_info)

        stats["scan_duration_ms"] = (time.time() - start) * 1000
        return {"lines": lines, "stats": stats}
    except Exception:
        return {"lines": [], "stats": {"total_lines": 0}}


def _py_parse_log_range(
    file_path: str,
    start_line: Optional[int],
    end_line: Optional[int],
    max_results: int,
) -> List[Dict[str, Any]]:
    """Python fallback: read file lines directly."""
    results = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if start_line and i < start_line:
                    continue
                if end_line and i > end_line:
                    break
                if len(results) >= max_results:
                    break
                results.append(_py_hydrate_log_detail(line.rstrip("\n\r"), 0, None, 0, i))
    except Exception:
        pass
    return results


def _py_hydrate_log_detail(
    raw_line: str,
    timestamp_us: int = 0,
    level: Optional[str] = None,
    byte_offset: int = 0,
    line_number: int = 0,
) -> Dict[str, Any]:
    """Python fallback using existing analyzer logic."""
    from analyzers.pattern_analyzer import (
        _RE_VERSION_CONFLICT,
        _RE_BUILD_ERROR,
        _RE_TEST_FAILURE,
        _RE_NETWORK_ERROR,
        _RE_PERMISSION_ERROR,
        _RE_FILE_PATH,
        _RE_DEPENDENCY_ERROR,
    )
    from log_indexer import extract_timestamp_us, detect_level

    # Timestamp
    if not timestamp_us:
        ts = extract_timestamp_us(raw_line)
        timestamp_us = ts if ts else 0

    # Level
    if not level:
        level = detect_level(raw_line)

    return {
        "line_info": {
            "timestamp_us": timestamp_us,
            "level": level,
            "byte_offset": byte_offset,
            "line_number": line_number,
            "line_length": len(raw_line),
            "message_preview": raw_line[:200],
        },
        "raw_text": raw_line,
        "error_category": _py_categorize_error(raw_line),
        "file_paths": [m.group() for m in _RE_FILE_PATH.finditer(raw_line)][:10],
        "versions": [],  # simplified fallback
        "duration_secs": None,
        "is_stacktrace": bool(raw_line.startswith("  at ") or raw_line.startswith("  File ")),
        "error_signature": raw_line[:80] if "error" in raw_line.lower() else None,
    }


def _py_full_single_pass(log_text: str, max_error_lines: int) -> Dict[str, Any]:
    """Python fallback using log_parser._single_pass_scan()."""
    from log_parser import _single_pass_scan as _py_scan
    platform, error_lines, stats = _py_scan(log_text)
    return {
        "platform": platform,
        "error_lines": error_lines[:max_error_lines],
        "total_lines": stats["total_lines"],
        "error_count": stats["error_count"],
        "warning_count": stats["warning_count"],
        "fatal_count": stats["fatal_count"],
    }


def _py_categorize_error(line: str) -> Optional[str]:
    """Python fallback error categorization."""
    import re

    line_lower = line.lower()
    checks = [
        (r"importerror|modulenotfounderror", "ImportError"),
        (r"syntaxerror|invalid\s+syntax", "SyntaxError"),
        (r"typeerror", "TypeError"),
        (r"valueerror", "ValueError"),
        (r"keyerror", "KeyError"),
        (r"attributeerror", "AttributeError"),
        (r"oserror|filenotfounderror|permissionerror", "OSError"),
        (r"httperror|[45]\d{2}", "HTTPError"),
        (r"timeouterror|timed?\s*out", "TimeoutError"),
        (r"connectionerror|connection\s+refused|econnrefused", "ConnectionError"),
        (r"outofmemoryerror|out\s+of\s+memory|oom", "OutOfMemory"),
        (r"segmentation\s+fault|sigsegv", "Segfault"),
        (r"assertionerror|assert\s+failed", "AssertionError"),
        (r"nullpointerexception|nonetype.*has\s+no\s+attribute", "NullPointer"),
        (r"build\s+(?:failed|error|failure)|error\[[a-z]", "BuildError"),
        (r"tests?\s+(?:failed|run):", "TestFailure"),
        (r"connection\s+(?:refused|reset)|dns\s+(?:resolution|lookup)\s+failed", "NetworkError"),
        (r"permission\s+denied|eacces|eperm", "PermissionError"),
    ]
    for pattern, category in checks:
        if re.search(pattern, line_lower):
            return category
    if "error" in line_lower or "failed" in line_lower or "fatal" in line_lower:
        return "UnknownError"
    if "warn" in line_lower:
        return "Warning"
    return None


def _level_severity(level: str) -> int:
    """Numeric severity for level ordering."""
    order = {"UNKNOWN": 0, "TRACE": 1, "DEBUG": 2, "INFO": 3, "WARN": 4, "ERROR": 5, "CRITICAL": 6, "FATAL": 7}
    return order.get(level.upper(), 0)
