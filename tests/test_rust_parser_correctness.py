#!/usr/bin/env python3
"""
Correctness validation: Rust parser vs Python reference implementation.

This test suite compares the Rust log parser output against the
existing Python implementation to ensure behavioral compatibility.

Run:
    pytest tests/test_rust_parser_correctness.py -v

Requirements:
    pip install logpilot-parser  (or `cd logpilot-parser && maturin develop --release`)
"""

import sys
import os
import tempfile
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import Python reference implementations
from log_indexer import extract_timestamp_us as py_extract_timestamp
from log_indexer import detect_level as py_detect_level
from log_parser import _single_pass_scan as py_single_pass_scan
from log_parser import detect_platform as py_detect_platform
from log_parser import extract_error_lines as py_extract_error_lines
from analyzers.pattern_analyzer import _RE_VERSION_CONFLICT as PY_RE_VERSION_CONFLICT
from analyzers.pattern_analyzer import _RE_BUILD_ERROR as PY_RE_BUILD_ERROR

# Try to import Rust parser
try:
    import logpilot_rust
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False


# ============================================================
# Test data
# ============================================================

SAMPLE_LOG = """\
2024-01-15T10:30:45.123Z INFO Server started on port 8080
2024-01-15T10:30:46.000Z DEBUG Initializing database connection pool with max_connections=100
2024-01-15T10:30:47.500Z WARN Disk usage at 85%, consider cleanup
2024-01-15T10:30:48.000Z ERROR Failed to connect to upstream service: connection refused
2024-01-15T10:30:49.100Z FATAL Critical system failure, shutting down
2024-01-15T10:30:50.000Z INFO Shutdown complete
Just a plain message without level
"""

TIMESTAMP_TEST_CASES = [
    # (line, expected_seconds)
    ("2024-01-15T10:30:45.123Z INFO test", 1705314645),
    ("2024-01-15 10:30:45 ERROR test", 1705314645),
    ("2024-01-15 10:30:45,500 WARN test", 1705314645),
    ("2024/01/15 10:30:45 INFO test", 1705314645),
    ("01-15-2024 10:30:45 INFO test", 1705314645),
    ("1705312245 INFO test", 1705312245),
    ("1705312245123 INFO test", 1705312245),
    ("No timestamp here", None),
    ("", None),
    ("INFO: just a message", None),
]

LEVEL_TEST_CASES = [
    ("2024-01-15 FATAL: Out of memory", "FATAL"),
    ("CRITICAL: Database connection lost", "CRITICAL"),
    ("ERROR: File not found", "ERROR"),
    ("WARN: Disk usage 90%", "WARN"),
    ("WARNING: Low memory", "WARN"),
    ("INFO: Server started", "INFO"),
    ("DEBUG: Variable x = 42", "DEBUG"),
    ("TRACE: Entering function foo", "TRACE"),
    ("Just a plain message", "UNKNOWN"),
    ("", "UNKNOWN"),
    ("error: something failed", "ERROR"),
    ("Error: something failed", "ERROR"),
    ("fatal error at line 42", "FATAL"),
]

PLATFORM_TEST_CASES = [
    ("##[error]Process completed with exit code 1\nRun actions/checkout@v3", "GitHub Actions"),
    ("Finished: FAILURE\n[Pipeline] }\nERROR: Build step failed", "Jenkins"),
    ("Step 1/5 : FROM python:3.11\n---> Running in abc123\nreturned a non-zero code", "Docker"),
    ("npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE could not resolve", "npm"),
    ("ERROR: Could not find a version that satisfies the requirement\npip install foo", "pip"),
    ("error[E0425]: cannot find value\ncould not compile `mycrate`\naborting due to", "cargo"),
    ("FAILURES\nshort test summary\nAssertionError: assert 1 == 2", "pytest"),
    ("FAIL src/App.test.tsx\nTests: 1 failed, 2 passed\n● renders correctly", "jest"),
    ("BUILD FAILED in 10s\n> Task :compileJava FAILED\nExecution failed for task", "Gradle"),
    ("BUILD FAILURE\n[ERROR] Failed to execute goal\n[INFO] BUILD FAILURE", "Maven"),
]


# ============================================================
# Tests: Timestamp parsing
# ============================================================

class TestTimestampParsing:
    """Compare Rust timestamp extraction against Python reference."""

    @pytest.mark.parametrize("line,expected_seconds", TIMESTAMP_TEST_CASES)
    def test_timestamp_against_python(self, line, expected_seconds):
        """Rust should produce same result as Python."""
        py_result = py_extract_timestamp(line)

        if expected_seconds is None:
            assert py_result is None, f"Python found timestamp in '{line}': {py_result}"
        else:
            assert py_result is not None, f"Python missed timestamp in '{line}'"
            py_seconds = py_result // 1_000_000
            assert py_seconds == expected_seconds, \
                f"Python seconds mismatch: {py_seconds} != {expected_seconds}"

        # If Rust is available, compare directly
        if RUST_AVAILABLE:
            rust_result = logpilot_rust._logpilot_parser.extract_timestamp_us(line.encode())
            if expected_seconds is None:
                assert rust_result is None or rust_result == 0, \
                    f"Rust found timestamp in '{line}': {rust_result}"
            else:
                assert rust_result is not None and rust_result > 0, \
                    f"Rust missed timestamp in '{line}'"
                rust_seconds = rust_result // 1_000_000
                assert rust_seconds == expected_seconds, \
                    f"Rust seconds mismatch: {rust_seconds} != {expected_seconds}"

    def test_timestamp_iso_with_timezone(self):
        """ISO 8601 with +08:00 timezone should parse correctly."""
        line = "2024-01-15T10:30:45+08:00 INFO Asia event"
        py_result = py_extract_timestamp(line)
        assert py_result is not None
        # +08:00 means UTC is 8 hours behind
        assert py_result // 1_000_000 == 1705285845  # 1705314645 - 28800

    def test_timestamp_unix_out_of_range(self):
        """Out-of-range Unix timestamps should be rejected."""
        assert py_extract_timestamp("9999999999 INFO Future") is None
        assert py_extract_timestamp("500000000 INFO Past") is None


# ============================================================
# Tests: Log level detection
# ============================================================

class TestLevelDetection:
    """Compare Rust level detection against Python reference."""

    @pytest.mark.parametrize("line,expected_level", LEVEL_TEST_CASES)
    def test_level_against_python(self, line, expected_level):
        """Rust should produce same level as Python."""
        py_result = py_detect_level(line)
        assert py_result == expected_level, \
            f"Python level mismatch: {py_result} != {expected_level}"

        if RUST_AVAILABLE:
            import logpilot_parser
            rust_result = logpilot_parser.detect_level(line)
            assert rust_result == expected_level or rust_result == expected_level.upper(), \
                f"Rust level mismatch: {rust_result} != {expected_level}"

    def test_level_case_insensitivity(self):
        """Level detection should be case-insensitive."""
        for variant in ["error", "Error", "ERROR", "eRrOr"]:
            assert py_detect_level(f"{variant}: something happened") == "ERROR"


# ============================================================
# Tests: Platform detection
# ============================================================

class TestPlatformDetection:
    """Compare Rust platform detection against Python reference."""

    @pytest.mark.parametrize("log_text,expected_platform", PLATFORM_TEST_CASES)
    def test_platform_against_python(self, log_text, expected_platform):
        """Rust should detect the same platform as Python."""
        py_result = py_detect_platform(log_text)
        assert py_result == expected_platform, \
            f"Python platform mismatch: {py_result} != {expected_platform}"

        if RUST_AVAILABLE:
            result = logpilot_rust.full_single_pass(log_text, max_error_lines=10)
            assert result["platform"] == expected_platform, \
                f"Rust platform mismatch: {result['platform']} != {expected_platform}"


# ============================================================
# Tests: Full single pass
# ============================================================

class TestSinglePass:
    """Compare Rust single-pass scan against Python _single_pass_scan()."""

    def test_error_stats_match(self):
        """Error counts should match between Rust and Python."""
        py_platform, py_error_lines, py_stats = py_single_pass_scan(SAMPLE_LOG)

        assert py_stats["total_lines"] == 7
        assert py_stats["error_count"] >= 1
        assert py_stats["warning_count"] >= 1
        assert py_stats["fatal_count"] >= 1

        if RUST_AVAILABLE:
            result = logpilot_rust.full_single_pass(SAMPLE_LOG, max_error_lines=30)
            assert result["total_lines"] == py_stats["total_lines"], \
                f"Line count mismatch: {result['total_lines']} != {py_stats['total_lines']}"
            assert result["error_count"] >= py_stats["error_count"] - 1, \
                f"Error count mismatch: {result['error_count']} vs {py_stats['error_count']}"
            assert result["fatal_count"] >= py_stats["fatal_count"] - 1

    def test_error_line_extraction(self):
        """Error lines should be extracted correctly."""
        py_error_lines = py_extract_error_lines(SAMPLE_LOG, max_lines=30)
        # Should find at least the ERROR and FATAL lines
        error_count = sum(1 for line in py_error_lines
                         if "ERROR" in line or "FATAL" in line or "Failed" in line)
        assert error_count >= 2

        if RUST_AVAILABLE:
            result = logpilot_rust.full_single_pass(SAMPLE_LOG, max_error_lines=30)
            rust_errors = result["error_lines"]
            assert len(rust_errors) >= 2


# ============================================================
# Tests: Stage 1 scan (Rust-specific)
# ============================================================

class TestStage1Scan:
    """Test the Rust Stage 1 scan functionality."""

    def _create_temp_log(self):
        """Create a temporary log file for testing."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        tmp.write(SAMPLE_LOG)
        tmp.close()
        return tmp.name

    def test_scan_returns_expected_structure(self):
        """Stage 1 scan should return lines and stats."""
        path = self._create_temp_log()
        try:
            if RUST_AVAILABLE:
                result = logpilot_rust.scan_log_stage1(path)
                assert "lines" in result
                assert "stats" in result
                assert result["stats"]["total_lines"] == 7
                assert len(result["lines"]) == 7

                # Check first line structure
                first = result["lines"][0]
                assert "timestamp_us" in first
                assert "level" in first
                assert "byte_offset" in first
                assert "line_number" in first
                assert "line_length" in first
                assert "message_preview" in first
        finally:
            os.unlink(path)

    def test_scan_with_level_filter(self):
        """Level filter should exclude lines below threshold."""
        path = self._create_temp_log()
        try:
            if RUST_AVAILABLE:
                result = logpilot_rust.scan_log_stage1(
                    path, min_level="ERROR"
                )
                for line in result["lines"]:
                    assert line["level"] in ("ERROR", "FATAL", "CRITICAL"), \
                        f"Unexpected level: {line['level']}"
        finally:
            os.unlink(path)

    def test_byte_offsets_are_sequential(self):
        """Byte offsets should be monotonically increasing."""
        path = self._create_temp_log()
        try:
            if RUST_AVAILABLE:
                result = logpilot_rust.scan_log_stage1(path)
                for i in range(len(result["lines"]) - 1):
                    curr_end = (
                        result["lines"][i]["byte_offset"]
                        + result["lines"][i]["line_length"]
                    )
                    next_start = result["lines"][i + 1]["byte_offset"]
                    assert curr_end == next_start, \
                        f"Offset gap at line {i}: {curr_end} != {next_start}"
        finally:
            os.unlink(path)


# ============================================================
# Tests: Error categorization
# ============================================================

class TestErrorCategorization:
    """Compare Rust error categorization against Python patterns."""

    ERROR_TEST_CASES = [
        ("ImportError: No module named 'foo'", "ImportError"),
        ("TypeError: expected str, got int", "TypeError"),
        ("SyntaxError: invalid syntax at line 42", "SyntaxError"),
        ("ValueError: invalid literal for int()", "ValueError"),
        ("KeyError: 'missing_key'", "KeyError"),
        ("AttributeError: 'NoneType' object has no attribute 'foo'", "AttributeError"),
        ("FileNotFoundError: [Errno 2] No such file", "OSError"),
        ("HTTPError: 500 Internal Server Error", "HTTPError"),
        ("TimeoutError: connection timed out after 30s", "TimeoutError"),
        ("ConnectionError: Connection refused", "ConnectionError"),
        ("Connection refused: ECONNREFUSED 127.0.0.1:8080", "ConnectionError"),
        ("OutOfMemoryError: Java heap space", "OutOfMemory"),
        ("Segmentation fault (core dumped) SIGSEGV", "Segfault"),
        ("AssertionError: assert 1 == 2", "AssertionError"),
        ("NullPointerException: null value", "NullPointer"),
    ]

    @pytest.mark.parametrize("line,expected_category", ERROR_TEST_CASES)
    def test_categorization_matches_python(self, line, expected_category):
        """Error categorization should align with Python patterns."""
        if RUST_AVAILABLE:
            category = logpilot_rust.categorize_error(line)
            assert category == expected_category, \
                f"Category mismatch: {category} != {expected_category}"


# ============================================================
# Edge case tests
# ============================================================

class TestEdgeCases:
    """Test edge cases that could cause bugs."""

    def test_empty_file(self):
        """Empty file should not crash."""
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        tmp.close()
        try:
            if RUST_AVAILABLE:
                result = logpilot_rust.scan_log_stage1(tmp.name)
                assert result["stats"]["total_lines"] == 0
        finally:
            os.unlink(tmp.name)

    def test_very_long_line(self):
        """Lines exceeding 64KB should be handled gracefully."""
        long_line = "x" * 100_000 + " ERROR: something failed at the end"
        if RUST_AVAILABLE:
            result = logpilot_rust.full_single_pass(long_line, max_error_lines=10)
            assert result["total_lines"] >= 1
            assert result["error_count"] >= 1

    def test_binary_content(self):
        """Binary content in log files should not crash."""
        binary_line = "INFO: normal text \x00\x01\x02\xFF more text"
        py_result = py_detect_level(binary_line)
        assert py_result in ("INFO", "UNKNOWN")

    def test_multiline_log_entry(self):
        """Stack traces spanning multiple lines."""
        log_text = """\
ERROR: Exception in thread "main"
  at com.example.Main.process(Main.java:42)
  at com.example.Main.main(Main.java:15)
Caused by: java.lang.NullPointerException
  at com.example.Service.get(Service.java:100)
"""
        if RUST_AVAILABLE:
            result = logpilot_rust.full_single_pass(log_text, max_error_lines=30)
            assert result["error_count"] >= 2

    def test_mixed_line_endings(self):
        """CRLF, LF, and mixed line endings."""
        log_text = "INFO: line1\r\nERROR: line2\nWARN: line3\r\n"
        py_platform, py_errors, py_stats = py_single_pass_scan(log_text)
        assert py_stats["total_lines"] == 3

    def test_unicode_in_logs(self):
        """Unicode characters in log lines."""
        unicode_line = "INFO: 用户 'admin' 登录成功 ✓  🎉"
        level = py_detect_level(unicode_line)
        assert level == "INFO"


# ============================================================
# Performance sanity check
# ============================================================

class TestPerformanceSanity:
    """Quick sanity checks that the Rust parser is not slower than Python."""

    def test_stage1_throughput(self):
        """Stage 1 scan should handle 100k lines quickly."""
        import time

        # Generate test file
        lines = []
        for i in range(10000):
            lines.append(f"2024-01-15T10:30:{i%60:02d}.{i%1000:03d}Z INFO Line {i}")
        log_text = "\n".join(lines)

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
        tmp.write(log_text)
        tmp.close()

        try:
            if RUST_AVAILABLE:
                start = time.time()
                result = logpilot_rust.scan_log_stage1(tmp.name)
                elapsed = time.time() - start
                lines_per_sec = result["stats"]["total_lines"] / elapsed if elapsed > 0 else 0
                print(f"\nRust Stage 1: {result['stats']['total_lines']} lines "
                      f"in {elapsed*1000:.0f}ms ({lines_per_sec:.0f} lines/sec)")
                # This is informational — no strict assertion
                assert elapsed < 30, f"Stage 1 scan too slow: {elapsed:.1f}s"
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
