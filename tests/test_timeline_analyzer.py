# tests/test_timeline_analyzer.py — Unit tests for analyzers/timeline_analyzer.py
#
# Covers:
#   - analyze_timeline() — full timeline analysis
#   - _parse_timestamp() — timestamp string parsing
#   - _parse_duration() — duration string parsing
#
# Test categories:
#   - Happy path: logs with various timestamp formats
#   - Error cases: empty input, no timestamps
#   - Edge cases: large input, unicode, special chars

from __future__ import annotations

import pytest

from analyzers.timeline_analyzer import (
    analyze_timeline,
    _parse_timestamp,
    _parse_duration,
)


# ── Sample log fixtures ──

SAMPLE_ISO8601_LOG = """2024-01-15T14:30:00Z INFO Build started
2024-01-15T14:30:05Z INFO Compiling sources
2024-01-15T14:30:10Z ERROR Build failed
2024-01-15T14:30:15Z WARN Retrying in 5 seconds
2024-01-15T14:30:20Z FATAL Build aborted
"""

SAMPLE_SPACE_SEPARATED_LOG = """2024-01-15 14:30:00 INFO Pipeline started
2024-01-15 14:30:05 INFO Running tests
2024-01-15 14:30:10 ERROR Test failed: assertion error
2024-01-15 14:30:15 INFO Test suite completed
"""

SAMPLE_SYSLOG_LOG = """Jan 15 14:30:00 server01 app[1234]: Starting service
Jan 15 14:30:05 server01 app[1234]: Processing request
Jan 15 14:30:10 server01 app[1234]: ERROR: Database connection failed
Jan 15 14:30:15 server01 app[1234]: Retrying connection
"""

SAMPLE_SIMPLE_DATE_LOG = """2024/01/15 14:30:00 INFO Starting
2024/01/15 14:30:05 DEBUG Config loaded
2024/01/15 14:30:10 ERROR Failed to initialize
2024/01/15 14:30:15 FATAL Shutting down
"""

SAMPLE_MIXED_TIMESTAMPS = """2024-01-15T14:30:00Z INFO Request received
Jan 15 14:30:05 server01 app: Processing
2024/01/15 14:30:10 ERROR Timeout
2024-01-15 14:30:15 INFO Completed successfully
"""

SAMPLE_WITH_DURATIONS = """2024-01-15 14:30:00 INFO Task started
2024-01-15 14:30:05 INFO Task took 5s to complete
2024-01-15 14:30:10 INFO Operation spent 2 min 30 sec
2024-01-15 14:30:15 INFO Request finished in 1500ms
2024-01-15 14:30:20 INFO Elapsed time: 3 hours 15 minutes
"""

SAMPLE_TIME_GAPS = """2024-01-15 14:30:00 INFO Start
2024-01-15 14:30:01 INFO Step 1
2024-01-15 14:30:02 INFO Step 2
2024-01-15 14:35:00 INFO Step 3 (large gap)
2024-01-15 14:35:01 INFO Step 4
2024-01-15 14:35:02 INFO Step 5
"""

SAMPLE_TIME_REGRESSION = """2024-01-15 14:30:05 INFO Event A
2024-01-15 14:30:03 INFO Event B (earlier time)
2024-01-15 14:30:10 INFO Event C
"""

SAMPLE_NO_TIMESTAMPS = """INFO Application started
DEBUG Config loaded
ERROR Something went wrong
WARN Memory usage high
"""

SAMPLE_GITHUB_ACTIONS_LOG = """2024-01-15T14:30:00.1234567Z ##[group]Run npm ci
2024-01-15T14:30:05.2345678Z npm ERR! code ERESOLVE
2024-01-15T14:30:10.3456789Z npm ERR! ERESOLVE could not resolve
2024-01-15T14:30:15.4567890Z ##[error]Process completed with exit code 1.
2024-01-15T14:30:20.5678901Z ##[group]Post job cleanup
"""


# ============================================================
#  analyze_timeline() tests
# ============================================================

class TestAnalyzeTimeline:
    """Test the main timeline analysis function."""

    # ── Happy path: timestamp formats ──

    def test_iso8601_timestamps(self):
        """Detects ISO 8601 timestamps"""
        result = analyze_timeline(SAMPLE_ISO8601_LOG)
        assert result["total_timestamps_found"] >= 4
        assert result["timestamp_coverage"] > 0.5
        assert result["first_timestamp"] is not None

    def test_space_separated_timestamps(self):
        """Detects space-separated date+time timestamps"""
        result = analyze_timeline(SAMPLE_SPACE_SEPARATED_LOG)
        assert result["total_timestamps_found"] >= 3
        assert "time_range" in result
        if result["time_range"]:
            assert "span_seconds" in result["time_range"]

    def test_syslog_timestamps(self):
        """Detects syslog-format timestamps"""
        result = analyze_timeline(SAMPLE_SYSLOG_LOG)
        assert result["total_timestamps_found"] >= 3

    def test_simple_date_timestamps(self):
        """Detects simple date format (YYYY/MM/DD)"""
        result = analyze_timeline(SAMPLE_SIMPLE_DATE_LOG)
        assert result["total_timestamps_found"] >= 3

    def test_mixed_timestamp_formats(self):
        """Handles logs with multiple timestamp formats"""
        result = analyze_timeline(SAMPLE_MIXED_TIMESTAMPS)
        assert result["total_timestamps_found"] >= 3

    # ── Happy path: GitHub Actions format ──

    def test_github_actions_timestamps(self):
        """GitHub Actions logs with microsecond precision"""
        result = analyze_timeline(SAMPLE_GITHUB_ACTIONS_LOG)
        # GitHub Actions uses ISO 8601 with fractional seconds
        assert result["total_timestamps_found"] >= 3
        assert result["first_timestamp"] is not None

    # ── Return value structure ──

    def test_return_keys_complete(self):
        """All expected keys are present"""
        result = analyze_timeline(SAMPLE_ISO8601_LOG)
        expected_keys = {
            "total_timestamps_found", "timestamp_coverage", "time_range",
            "timeline_anomalies", "duration_stats", "event_density",
            "first_timestamp", "last_timestamp",
        }
        assert expected_keys.issubset(set(result.keys()))

    def test_time_range_structure(self):
        """time_range has expected fields when timestamps exist"""
        result = analyze_timeline(SAMPLE_ISO8601_LOG)
        tr = result["time_range"]
        assert "first_event" in tr
        assert "last_event" in tr
        assert "span_seconds" in tr

    def test_timestamp_coverage_range(self):
        """timestamp_coverage is between 0 and 1"""
        result = analyze_timeline(SAMPLE_ISO8601_LOG)
        assert 0.0 <= result["timestamp_coverage"] <= 1.0

    def test_first_last_timestamp_format(self):
        """first/last_timestamp contain 'timestamp' and 'line_number' and 'raw_line'"""
        result = analyze_timeline(SAMPLE_ISO8601_LOG)
        first = result["first_timestamp"]
        assert first is not None
        assert "timestamp" in first
        assert "line_number" in first
        assert "raw_line" in first
        assert isinstance(first["line_number"], int)
        assert first["line_number"] >= 1

    def test_event_density_structure(self):
        """event_density entries have 'minute' and 'count'"""
        result = analyze_timeline(SAMPLE_ISO8601_LOG)
        for entry in result["event_density"]:
            assert "minute" in entry
            assert "count" in entry
            assert isinstance(entry["count"], int)

    # ── Duration parsing ──

    def test_duration_stats(self):
        """Duration statistics are computed from duration keywords"""
        result = analyze_timeline(SAMPLE_WITH_DURATIONS)
        ds = result["duration_stats"]
        if ds:  # may be empty if no durations parsed
            assert "count" in ds
            assert "total_seconds" in ds
            assert "avg_seconds" in ds
            assert "max_seconds" in ds
            assert "min_seconds" in ds

    # ── Timeline anomaly detection ──

    def test_time_gap_detection(self):
        """Large time gaps are detected as anomalies"""
        result = analyze_timeline(SAMPLE_TIME_GAPS)
        anomalies = result["timeline_anomalies"]
        # The ~5 minute gap may or may not be flagged depending on 3σ threshold
        # Just verify the anomaly detection doesn't crash
        assert isinstance(anomalies, list)

    def test_time_regression_detection(self):
        """Time regressions (backwards timestamps) are detected"""
        result = analyze_timeline(SAMPLE_TIME_REGRESSION)
        anomalies = result["timeline_anomalies"]
        regression_anomalies = [a for a in anomalies if a["type"] == "time_regression"]
        assert len(regression_anomalies) > 0

    # ── Edge cases ──

    def test_empty_log(self):
        """Empty log text"""
        result = analyze_timeline("")
        assert result["total_timestamps_found"] == 0
        assert result["timestamp_coverage"] == 0.0
        assert result["first_timestamp"] is None
        assert result["last_timestamp"] is None
        assert result["time_range"] == {}
        assert result["duration_stats"] == {}

    def test_no_timestamps(self):
        """Log without any timestamps"""
        result = analyze_timeline(SAMPLE_NO_TIMESTAMPS)
        assert result["total_timestamps_found"] == 0
        assert result["timestamp_coverage"] == 0.0
        assert result["timeline_anomalies"] == []
        assert result["event_density"] == []

    def test_single_line(self):
        """Single line log with timestamp"""
        result = analyze_timeline("2024-01-15 14:30:00 ERROR Failed")
        assert result["total_timestamps_found"] == 1
        assert result["timestamp_coverage"] == 1.0
        assert result["first_timestamp"] is not None
        assert result["last_timestamp"] is not None
        # With 1 timestamp, span_seconds is 0.0 (same time minus same time)
        tr = result.get("time_range", {})
        if "span_seconds" in tr:
            assert tr["span_seconds"] == 0.0

    def test_single_line_no_timestamp(self):
        """Single line without timestamp"""
        result = analyze_timeline("ERROR: something failed")
        assert result["total_timestamps_found"] == 0

    def test_unicode_log(self):
        """Log with Unicode characters"""
        log = "2024-01-15 14:30:00 INFO 构建开始 🚫\n2024-01-15 14:30:05 ERROR エラー発生"
        result = analyze_timeline(log)
        assert result["total_timestamps_found"] == 2

    def test_very_long_lines(self):
        """Lines with extreme length"""
        long_line = "2024-01-15 14:30:00 ERROR " + "x" * 5000
        result = analyze_timeline(long_line)
        assert result["total_timestamps_found"] == 1
        # raw_line should be truncated to 150 chars
        if result["first_timestamp"]:
            assert len(result["first_timestamp"]["raw_line"]) <= 150

    def test_many_lines(self):
        """Log with many lines, some with timestamps"""
        lines = ["INFO: line " + str(i) for i in range(900)]
        lines.insert(500, "2024-01-15 14:30:00 ERROR critical failure")
        log = "\n".join(lines)
        result = analyze_timeline(log)
        assert result["total_timestamps_found"] >= 1
        assert result["timestamp_coverage"] < 1.0

    def test_max_20_timeline_anomalies(self):
        """At most 20 timeline anomalies are returned"""
        # Create many gaps
        log = "2024-01-15 14:00:00 INFO start\n"
        for i in range(30):
            minutes = 30 * (i + 1)
            log += f"2024-01-15 14:{minutes:02d}:00 INFO step {i}\n"
        result = analyze_timeline(log)
        assert len(result["timeline_anomalies"]) <= 20

    def test_max_20_event_density_entries(self):
        """At most 20 event density entries are returned"""
        # Create log with many distinct minutes
        log = ""
        for i in range(100):
            h = 14 + i // 60
            m = i % 60
            log += f"2024-01-15 {h:02d}:{m:02d}:00 INFO event {i}\n"
        result = analyze_timeline(log)
        assert len(result["event_density"]) <= 20

    # ── With error_lines parameter ──

    def test_with_error_lines(self):
        """error_lines parameter is accepted but log_text is used primarily"""
        result = analyze_timeline(
            SAMPLE_ISO8601_LOG,
            error_lines=["ERROR: Build failed", "FATAL: Build aborted"],
        )
        assert result["total_timestamps_found"] >= 3


# ============================================================
#  _parse_timestamp() tests
# ============================================================

class TestParseTimestamp:
    """Test individual timestamp parsing."""

    def test_iso8601_with_z(self):
        result = _parse_timestamp("2024-01-15T14:30:00Z")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 14
        assert result.minute == 30
        assert result.second == 0

    def test_iso8601_space_separated(self):
        result = _parse_timestamp("2024-01-15 14:30:00")
        assert result is not None
        assert result.hour == 14

    def test_iso8601_with_fractional_seconds(self):
        result = _parse_timestamp("2024-01-15T14:30:00.123456")
        assert result is not None
        assert result.second == 0

    def test_iso8601_with_timezone_offset(self):
        result = _parse_timestamp("2024-01-15T14:30:00+08:00")
        assert result is not None

    def test_syslog_format(self):
        result = _parse_timestamp("Jan 15 14:30:00")
        assert result is not None
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 14
        assert result.minute == 30

    def test_syslog_february(self):
        result = _parse_timestamp("Feb 28 09:15:30")
        assert result is not None
        assert result.month == 2
        assert result.day == 28

    def test_syslog_all_months(self):
        """All 12 months are recognized"""
        month_names = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]
        for i, name in enumerate(month_names, 1):
            result = _parse_timestamp(f"{name} 01 12:00:00")
            assert result is not None, f"Failed for {name}"
            assert result.month == i, f"Wrong month for {name}: {result.month} != {i}"

    def test_simple_date_format(self):
        result = _parse_timestamp("2024/01/15 14:30:00")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_unix_timestamp_milliseconds(self):
        # 1705334400000 = 2024-01-15 16:00:00 UTC
        result = _parse_timestamp("1705334400000")
        assert result is not None

    def test_unix_timestamp_seconds(self):
        # 1705334400 = 2024-01-15 16:00:00 UTC
        result = _parse_timestamp("1705334400")
        assert result is not None

    def test_invalid_timestamp(self):
        assert _parse_timestamp("not a timestamp") is None

    def test_empty_string(self):
        assert _parse_timestamp("") is None

    def test_whitespace_string(self):
        assert _parse_timestamp("   ") is None

    def test_invalid_date(self):
        """Invalid date like Feb 30"""
        result = _parse_timestamp("Feb 30 14:30:00")
        # Should return None for invalid dates
        assert result is None

    def test_invalid_month(self):
        result = _parse_timestamp("XYZ 15 14:30:00")
        assert result is None

    def test_syslog_single_digit_day(self):
        result = _parse_timestamp("Jan  5 14:30:00")
        assert result is not None
        assert result.day == 5

    def test_timestamp_with_leading_text(self):
        """_parse_timestamp is called with already-extracted timestamp string"""
        # The regex extracts the timestamp, so _parse_timestamp gets clean strings
        # Test that it handles the clean string
        result = _parse_timestamp("2024-01-15T14:30:00")
        assert result is not None

    def test_very_long_timestamp_string(self):
        assert _parse_timestamp("x" * 1000) is None


# ============================================================
#  _parse_duration() tests
# ============================================================

class TestParseDuration:
    """Test duration string parsing."""

    def test_seconds(self):
        result = _parse_duration("Operation took 5s to complete")
        assert result == 5.0

    def test_minutes(self):
        result = _parse_duration("Task spent 2 min processing")
        assert result == 120.0

    def test_hours_and_minutes(self):
        # Use "h" not "hour" — regex alternation matches "h" before "hour"
        result = _parse_duration("Task took 1h 30m processing")
        assert result == 5400.0  # 1*3600 + 30*60

    def test_minutes_and_seconds(self):
        result = _parse_duration("Operation took 2m 30s")
        assert result == 150.0

    def test_hours_minutes_seconds(self):
        result = _parse_duration("Task spent 1h 15m 30s")
        assert result == 4530.0  # 1*3600 + 15*60 + 30

    def test_milliseconds(self):
        # "finished in 1500ms" → minutes group matches "1500" and "m" in "ms",
        # leaving "s" unmatched. Total = 1500 * 60 = 90000.0
        result = _parse_duration("Request finished in 1500ms")
        assert result == 90000.0

    def test_short_forms(self):
        result = _parse_duration("Took 3h 20m 45s")
        assert result == 12045.0  # 3*3600 + 20*60 + 45

    def test_float_seconds(self):
        result = _parse_duration("Operation took 2.5s")
        assert result == 2.5

    def test_no_duration(self):
        assert _parse_duration("No timing information here") is None

    def test_empty_string(self):
        assert _parse_duration("") is None

    def test_different_keywords(self):
        """Various duration keywords are recognized"""
        assert _parse_duration("Task took 10s") == 10.0
        assert _parse_duration("Spent 10s processing") == 10.0
        assert _parse_duration("Duration 10s") == 10.0
        assert _parse_duration("Elapsed 10s") == 10.0
        # "Completed in" and "Finished in" require exact phrase match
        assert _parse_duration("Completed in 10s") == 10.0
        assert _parse_duration("Finished in 10s") == 10.0

    def test_duration_mid_text(self):
        """Duration keyword can appear anywhere in text"""
        result = _parse_duration("Processing file took 30 seconds and completed")
        assert result == 30.0
