# tests/test_pattern_analyzer.py — Unit tests for analyzers/pattern_analyzer.py
#
# Covers:
#   - analyze_patterns() — pattern classification and extraction
#
# Test categories:
#   - Happy path: various log types with known patterns
#   - Error cases: empty input, no patterns
#   - Edge cases: very large input, special chars, unicode

from __future__ import annotations

import pytest

from analyzers.pattern_analyzer import analyze_patterns


# ── Sample log fixtures ──

SAMPLE_VERSION_CONFLICT = """npm ERR! code ERESOLVE
npm ERR! ERESOLVE could not resolve
npm ERR! While resolving: react@18.2.0
npm ERR! Found: testing-library@13.4.0
npm ERR! node_modules/react
npm ERR!   react@18.2.0 from the root project
npm ERR! Could not resolve dependency:
npm ERR! peer react@17.0.0 requires testing-library@13.4.0
npm ERR!   react@"18.2.0" requires peer dep@2.0.0
"""

SAMPLE_DEPENDENCY_ERROR = """ERROR: Could not find package 'requests>=2.28.0'
ERROR: No matching distribution found for numpy==1.99.0
WARN: Unable to resolve dependency: flask-cors
ERROR: Failed to download pytorch from https://download.pytorch.org
"""

SAMPLE_BUILD_ERROR = """ERROR: Build failed with exit code 1
src/main.cpp:42:1: error: expected ';' before '}' token
ERROR: Compilation failed
undefined reference to 'some_function'
error[E0308]: mismatched types
"""

SAMPLE_TEST_FAILURE = """============================= test session starts ==============================
test_auth.py::test_login FAILED
test_api.py::test_create FAILED
FAILURES
Tests run: 10, Failed: 3, Passed: 7
assert 200 == 500
3 failed, 7 passed in 2.34s
"""

SAMPLE_NETWORK_ERROR = """ERROR: Connection refused to database at port 5432
FATAL: Connection reset by peer
WARN: DNS resolution failed for api.example.com
ERROR: Network unreachable: could not connect to service
ECONNREFUSED 127.0.0.1:8080
ETIMEDOUT: connection to redis timed out
"""

SAMPLE_PERMISSION_ERROR = """ERROR: Permission denied: /var/log/app/access.log
FATAL: EACCES: cannot access '/etc/config.yaml'
WARN: access forbidden to /admin/dashboard
ERROR: unable to write to output file /tmp/result.json
ERROR: cannot create directory /opt/data: permission restricted
"""

SAMPLE_MIXED_LOG = """2024-01-15 14:30:00 INFO Build started
2024-01-15 14:30:05 ERROR: Could not find package 'requests>=2.28.0'
2024-01-15 14:30:10 ERROR: Build failed with exit code 1
2024-01-15 14:30:12   at /app/src/main.py line 42
  File "/app/utils/helper.py", line 15, in process
2024-01-15 14:30:15 ERROR: Connection refused to pypi.org:443
2024-01-15 14:30:20 ERROR: Permission denied: /var/cache/pip
2024-01-15 14:30:25 npm ERR! react@18.2.0 requires peer testing-library@13.4.0
2024-01-15 14:30:30 WARN Deprecation warning
"""

SAMPLE_NORMAL_LOG = """2024-01-15 14:30:00 INFO Application started
2024-01-15 14:30:01 DEBUG Configuration loaded
2024-01-15 14:30:02 INFO Ready to accept connections
"""


# ============================================================
#  analyze_patterns() tests
# ============================================================

class TestAnalyzePatterns:
    """Test the main pattern analysis function."""

    # ── Happy path: individual pattern types ──

    def test_version_conflict_detection(self):
        """Detects version conflicts in npm/pip logs"""
        result = analyze_patterns(SAMPLE_VERSION_CONFLICT)
        assert result["error_categories"].get("version_conflict", 0) >= 1
        assert len(result["version_conflicts"]) > 0
        vc = result["version_conflicts"][0]
        assert "package" in vc

    def test_dependency_error_detection(self):
        """Detects dependency resolution errors"""
        result = analyze_patterns(SAMPLE_DEPENDENCY_ERROR)
        assert result["error_categories"].get("dependency_error", 0) >= 2
        assert len(result["dependency_errors"]) > 0

    def test_build_error_detection(self):
        """Detects build/compilation errors"""
        result = analyze_patterns(SAMPLE_BUILD_ERROR)
        assert result["error_categories"].get("build_error", 0) >= 1

    def test_test_failure_detection(self):
        """Detects test failures"""
        result = analyze_patterns(SAMPLE_TEST_FAILURE)
        assert result["error_categories"].get("test_failure", 0) >= 1

    def test_network_error_detection(self):
        """Detects network connectivity errors"""
        result = analyze_patterns(SAMPLE_NETWORK_ERROR)
        assert result["error_categories"].get("network_error", 0) >= 2

    def test_permission_error_detection(self):
        """Detects permission/access errors"""
        result = analyze_patterns(SAMPLE_PERMISSION_ERROR)
        assert result["error_categories"].get("permission_error", 0) >= 2

    # ── Happy path: mixed patterns ──

    def test_mixed_patterns(self):
        """Multiple pattern types in a single log"""
        result = analyze_patterns(SAMPLE_MIXED_LOG)
        categories = result["error_categories"]
        # Should detect multiple categories
        assert len(categories) >= 3
        assert result["total_categories"] >= 3

    # ── Return value structure ──

    def test_return_keys_complete(self):
        """All expected keys are present"""
        result = analyze_patterns(SAMPLE_MIXED_LOG)
        expected_keys = {
            "error_categories", "version_conflicts", "dependency_errors",
            "file_paths_mentioned", "repeated_errors", "total_categories",
        }
        assert expected_keys.issubset(set(result.keys()))

    def test_error_categories_is_dict(self):
        """error_categories is a dict of category → count"""
        result = analyze_patterns(SAMPLE_MIXED_LOG)
        assert isinstance(result["error_categories"], dict)
        for k, v in result["error_categories"].items():
            assert isinstance(k, str)
            assert isinstance(v, int)
            assert v > 0

    def test_version_conflicts_limited_to_10(self):
        """At most 10 version conflicts are returned"""
        # Create many version conflict lines
        lines = [
            f"ERROR: package-{i}@1.0.0 requires peer dep-{i}@2.0.0"
            for i in range(30)
        ]
        result = analyze_patterns("\n".join(lines))
        assert len(result["version_conflicts"]) <= 10

    def test_dependency_errors_deduplicated(self):
        """Duplicate dependency errors are removed"""
        log = (
            "ERROR: Could not find package 'requests'\n"
            "ERROR: Could not find package 'requests'\n"
            "ERROR: Could not find package 'requests'\n"
        )
        result = analyze_patterns(log)
        # Should be deduplicated
        assert len(result["dependency_errors"]) <= 1

    def test_dependency_errors_limited_to_20(self):
        """At most 20 dependency errors are returned"""
        lines = [
            f"ERROR: Could not find dependency-{i}"
            for i in range(50)
        ]
        result = analyze_patterns("\n".join(lines))
        assert len(result["dependency_errors"]) <= 20

    def test_file_paths_extracted(self):
        """File paths are extracted from log lines"""
        log = (
            "ERROR: File /app/src/main.py:42 not found\n"
            "ERROR: Cannot read /etc/config.yaml\n"
            "File \"C:\\Users\\dev\\project\\utils.py\", line 15\n"
        )
        result = analyze_patterns(log)
        assert len(result["file_paths_mentioned"]) > 0

    def test_file_paths_sorted_and_limited(self):
        """File paths are sorted and limited to 30"""
        log = "\n".join([f"ERROR: file_{i}.py error" for i in range(50)])
        result = analyze_patterns(log)
        assert len(result["file_paths_mentioned"]) <= 30
        # Should be sorted
        paths = result["file_paths_mentioned"]
        if len(paths) > 1:
            assert paths == sorted(paths)

    def test_repeated_errors_detected(self):
        """Repeated error signatures are detected"""
        log = (
            "ERROR: Connection timeout\n"
            "ERROR: Connection timeout\n"
            "ERROR: Connection timeout\n"
        )
        result = analyze_patterns(log)
        assert len(result["repeated_errors"]) > 0
        re = result["repeated_errors"][0]
        assert re["count"] >= 3

    def test_repeated_errors_min_count_2(self):
        """Only errors appearing >= 2 times are 'repeated'"""
        log = (
            "ERROR: unique error A\n"
            "ERROR: unique error B\n"
            "ERROR: repeated error\n"
            "ERROR: repeated error\n"
        )
        result = analyze_patterns(log)
        signatures = {r["signature"] for r in result["repeated_errors"]}
        assert all(
            r["count"] >= 2 for r in result["repeated_errors"]
        )

    def test_repeated_errors_sorted_by_count(self):
        """Repeated errors sorted by count descending"""
        log = (
            "ERROR: rare\nERROR: rare\n"  # 2 times
            "ERROR: common\nERROR: common\nERROR: common\n"  # 3 times
        )
        result = analyze_patterns(log)
        counts = [r["count"] for r in result["repeated_errors"]]
        assert counts == sorted(counts, reverse=True)

    def test_total_categories(self):
        """total_categories matches len(error_categories)"""
        result = analyze_patterns(SAMPLE_MIXED_LOG)
        assert result["total_categories"] == len(result["error_categories"])

    # ── Normal log ──

    def test_normal_log(self):
        """Normal log with no errors → mostly 'info' category"""
        result = analyze_patterns(SAMPLE_NORMAL_LOG)
        assert result["error_categories"].get("info", 0) >= 2

    # ── Edge cases ──

    def test_empty_log(self):
        """Empty log text"""
        result = analyze_patterns("")
        assert result["total_categories"] == 0
        assert result["version_conflicts"] == []
        assert result["dependency_errors"] == []
        assert result["file_paths_mentioned"] == []
        assert result["repeated_errors"] == []

    def test_empty_lines_skipped(self):
        """Empty/whitespace-only lines are skipped"""
        result = analyze_patterns("\n  \n\t\n\n")
        assert result["total_categories"] == 0

    def test_single_line(self):
        """Single line log"""
        result = analyze_patterns("ERROR: Build failed with exit code 1")
        assert result["error_categories"].get("build_error", 0) == 1

    def test_unicode_log(self):
        """Log with Unicode characters"""
        log = "ERROR: 构建失败\nERROR: 権限が拒否されました: Permission denied\n"
        result = analyze_patterns(log)
        assert isinstance(result["error_categories"], dict)

    def test_very_long_lines(self):
        """Lines with extreme length"""
        long_line = "ERROR: " + "x" * 5000
        result = analyze_patterns(long_line)
        # Repeated errors signature truncates at 80 chars
        for re in result["repeated_errors"]:
            assert len(re["signature"]) <= 80

    def test_many_lines(self):
        """Log with many lines"""
        lines = ["INFO: line " + str(i) for i in range(1000)]
        log = "\n".join(lines)
        result = analyze_patterns(log)
        assert result["error_categories"].get("info", 0) == 1000

    def test_warning_category(self):
        """Lines with 'warn' go to warning category"""
        result = analyze_patterns("WARN: deprecated\nWARNING: obsolete")
        assert result["error_categories"].get("warning", 0) >= 1

    def test_unknown_error_category(self):
        """Lines with 'error' that don't match specific patterns → unknown_error"""
        result = analyze_patterns("ERROR: some random error message")
        assert result["error_categories"].get("unknown_error", 0) >= 1

    # ── With error_lines parameter ──

    def test_with_error_lines(self):
        """When error_lines provided, uses those instead of splitting log_text"""
        error_lines = [
            "ERROR: Build failed",
            "ERROR: Connection refused",
            "ERROR: Permission denied",
        ]
        result = analyze_patterns("", error_lines=error_lines)
        assert result["error_categories"].get("build_error", 0) >= 1
        assert result["error_categories"].get("network_error", 0) >= 1
        assert result["error_categories"].get("permission_error", 0) >= 1

    def test_version_conflict_structure(self):
        """Version conflict entries have expected fields"""
        result = analyze_patterns(SAMPLE_VERSION_CONFLICT)
        for vc in result["version_conflicts"]:
            assert "package" in vc
            assert "expected" in vc
            assert "actual_package" in vc
            assert "actual_version" in vc
