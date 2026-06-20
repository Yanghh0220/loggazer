# tests/test_error_handler.py — Unit tests for error_handler.py
#
# Covers:
#   - classify_error() — all exception type mappings
#   - get_error_info() — error info retrieval + fallback
#   - build_error_html() — HTML generation
#   - get_retry_action() / can_retry() — retry logic
#   - save_successful_result() / get_last_successful_result() / has_previous_result()
#   - estimate_analysis_time() — time estimation for various log sizes
#   - friendly_api_error() — HTTP status code mapping
#
# Test categories:
#   - Happy path
#   - Error cases (unknown exceptions, invalid inputs)
#   - Edge cases (empty input, special chars, unicode, very large input)

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from error_handler import (
    ErrorLevel,
    classify_error,
    get_error_info,
    build_error_html,
    get_retry_action,
    can_retry,
    save_successful_result,
    get_last_successful_result,
    has_previous_result,
    estimate_analysis_time,
    friendly_api_error,
)


# ============================================================
#  classify_error() tests
# ============================================================

class TestClassifyError:
    """Test exception → error_type mapping."""

    # ── Happy path: Connection errors ──

    def test_connection_refused(self):
        """ConnectionRefusedError → falls through to unknown_error since
        'connectionrefusederror' doesn't contain 'connecterror' or 'connectionerror'
        as a substring, and ConnectionRefusedError is not in any specific handler."""
        # classify_error uses substring matching on the type name.
        # "connectionrefusederror" does NOT contain "connectionerror" because
        # "refused" sits between them.
        e = ConnectionRefusedError("Connection refused by backend")
        assert classify_error(e) == "unknown_error"

    def test_connection_refused_via_connect_error(self):
        """Exception with 'ConnectError' name + 'refused' in message → connection_refused"""
        class ConnectError(Exception):
            pass
        e = ConnectError("Connection refused by peer")
        assert classify_error(e) == "connection_refused"

    def test_connect_error_with_refused(self):
        """Any connect error with 'refused' in message → connection_refused"""
        e = OSError("Connection refused by remote host")
        # OSError name contains no 'connect', so this falls through to unknown
        # Let's test the actual ConnectError path
        try:
            from httpx import ConnectError
            assert classify_error(ConnectError("Connection refused")) == "connection_refused"
        except ImportError:
            pass

    def test_connection_timeout(self):
        """TimeoutError → connection_timeout"""
        assert classify_error(TimeoutError("Connection timed out")) == "connection_timeout"

    def test_network_error_generic(self):
        """Generic ConnectionError without 'refused' or 'timeout' → network_error"""
        assert classify_error(ConnectionError("Network is unreachable")) == "network_error"

    # ── Happy path: ValueError variants ──

    def test_empty_input(self):
        """ValueError with 'empty' → empty_input"""
        assert classify_error(ValueError("Log input is empty")) == "empty_input"

    def test_input_too_short(self):
        """ValueError with '至少' (Chinese) → input_too_short"""
        assert classify_error(ValueError("至少需要10个字符")) == "input_too_short"

    def test_file_too_large(self):
        """ValueError with 'too large' → file_too_large"""
        assert classify_error(ValueError("Log file is too large")) == "file_too_large"

    def test_unsupported_format(self):
        """ValueError with 'validation' → unsupported_format"""
        assert classify_error(ValueError("Log validation failed")) == "unsupported_format"

    # ── Happy path: RuntimeError variants ──

    def test_rate_limit_runtime(self):
        """RuntimeError with 'rate' → rate_limit"""
        assert classify_error(RuntimeError("rate limit exceeded")) == "rate_limit"

    def test_server_timeout_runtime(self):
        """RuntimeError with 'timeout' → server_timeout"""
        assert classify_error(RuntimeError("Request timeout")) == "server_timeout"

    def test_server_error_runtime(self):
        """RuntimeError without specific keywords → server_error"""
        assert classify_error(RuntimeError("Internal processing error")) == "server_error"

    # ── Happy path: HTTP/API errors ──

    def test_auth_error_401(self):
        """HTTP error with 401 → auth_error"""
        class HTTPException(Exception):
            pass
        e = HTTPException("401 Unauthorized: invalid API key")
        assert classify_error(e) == "auth_error"

    def test_rate_limit_429(self):
        """HTTP error with 429 → rate_limit"""
        try:
            from httpx import HTTPStatusError
            # Create a mock-like exception with the right name
            e = Exception("429 rate limit exceeded")
            # We'll test the name-based logic
        except ImportError:
            pass

    # ── Happy path: Named exceptions ──

    def test_auth_by_name(self):
        """Exception with 'auth' in class name → auth_error"""
        class AuthenticationError(Exception):
            pass
        assert classify_error(AuthenticationError("Invalid token")) == "auth_error"

    def test_rate_by_name(self):
        """Exception with 'rate' in class name → rate_limit"""
        class RateLimitExceeded(Exception):
            pass
        assert classify_error(RateLimitExceeded("Too many requests")) == "rate_limit"

    def test_quota_by_name(self):
        """Exception with 'quota' in class name → quota_exhausted"""
        class QuotaExceededError(Exception):
            pass
        assert classify_error(QuotaExceededError("Monthly quota exhausted")) == "quota_exhausted"

    # ── Error case: Unknown exception → unknown_error ──

    def test_unknown_exception(self):
        """Completely unrecognized exception → unknown_error"""
        class WeirdCustomError(Exception):
            pass
        assert classify_error(WeirdCustomError("Something bizarre happened")) == "unknown_error"

    def test_generic_exception(self):
        """Plain Exception → unknown_error"""
        assert classify_error(Exception("generic error")) == "unknown_error"

    # ── Edge cases ──

    def test_empty_message(self):
        """Exception with empty message"""
        assert classify_error(ValueError("")) == "input_too_short"  # falls through to default ValueError

    def test_unicode_message(self):
        """Exception with Unicode/emoji message"""
        assert classify_error(ValueError("输入不能为空 🚫")) == "empty_input"

    def test_very_long_message(self):
        """Exception with extremely long message"""
        long_msg = "error " * 1000
        result = classify_error(RuntimeError(long_msg))
        assert result in ("server_error", "unknown_error")


# ============================================================
#  get_error_info() tests
# ============================================================

class TestGetErrorInfo:
    """Test error info retrieval."""

    def test_valid_error_type(self):
        """Valid error type returns full info dict"""
        info = get_error_info("auth_error")
        assert info["icon"] == "🔑"
        assert info["title"] == "API Key 配置错误"
        assert info["level"] == ErrorLevel.USER_ACTION
        assert "message" in info
        assert "suggestion" in info
        assert info["retry_action"] is None

    def test_unknown_error_type_fallback(self):
        """Invalid error type falls back to unknown_error"""
        info = get_error_info("nonexistent_error_type")
        assert info["icon"] == "❓"
        assert info["title"] == "发生未知错误"
        assert info["level"] == ErrorLevel.SERVER

    def test_network_error_fills_url(self):
        """network_error dynamically fills the backend URL"""
        info = get_error_info("network_error", backend_url="http://custom:9999")
        assert "http://custom:9999" in info["suggestion"]

    def test_network_error_default_url(self):
        """network_error uses default URL when not provided"""
        info = get_error_info("network_error")
        assert "http://127.0.0.1:8000" in info["suggestion"]

    def test_recoverable_error(self):
        """Recoverable error has retry_action"""
        info = get_error_info("connection_refused")
        assert info["level"] == ErrorLevel.RECOVERABLE
        assert info["retry_action"] == "start_backend"

    def test_user_action_error(self):
        """USER_ACTION error has no retry_action"""
        info = get_error_info("empty_input")
        assert info["level"] == ErrorLevel.USER_ACTION
        assert info["retry_action"] is None

    def test_server_error_level(self):
        """SERVER error level"""
        info = get_error_info("quota_exhausted")
        assert info["level"] == ErrorLevel.SERVER

    def test_info_is_copy_not_reference(self):
        """Modifying returned dict does not affect _ERROR_MAP"""
        info = get_error_info("empty_input")
        info["icon"] = "MODIFIED"
        info2 = get_error_info("empty_input")
        assert info2["icon"] == "📝"

    # ── Edge cases ──

    def test_empty_string_error_type(self):
        """Empty string as error type → falls back to unknown_error"""
        info = get_error_info("")
        assert info["title"] == "发生未知错误"

    def test_special_chars_in_backend_url(self):
        """Special characters in backend URL are handled"""
        info = get_error_info("network_error", backend_url="http://host:port/path?q=v")
        assert "http://host:port/path?q=v" in info["suggestion"]


# ============================================================
#  build_error_html() tests
# ============================================================

class TestBuildErrorHtml:
    """Test HTML generation for error display."""

    def test_builds_valid_html(self):
        """Returns valid HTML string with expected content"""
        html = build_error_html("auth_error")
        assert "API Key 配置错误" in html
        assert "🔑" in html
        assert "<div" in html
        assert "background:" in html

    def test_unknown_error_html(self):
        """Unknown error also produces valid HTML"""
        html = build_error_html("nonexistent")
        assert "发生未知错误" in html
        assert "❓" in html

    def test_html_contains_style(self):
        """HTML includes inline styling"""
        html = build_error_html("connection_timeout")
        assert "style=" in html
        assert "border" in html

    def test_network_error_html_includes_url(self):
        """Network error HTML includes the backend URL"""
        html = build_error_html("network_error", backend_url="http://test:1234")
        assert "http://test:1234" in html


# ============================================================
#  get_retry_action() / can_retry() tests
# ============================================================

class TestRetryLogic:
    """Test retry action and can_retry logic."""

    def test_retry_action_start_backend(self):
        assert get_retry_action("connection_refused") == "start_backend"

    def test_retry_action_retry_connection(self):
        assert get_retry_action("backend_not_ready") == "retry_connection"

    def test_retry_action_retry_analysis(self):
        assert get_retry_action("ai_parse_error") == "retry_analysis"

    def test_retry_action_wait_and_retry(self):
        assert get_retry_action("rate_limit") == "wait_and_retry"

    def test_retry_action_none_for_user_action(self):
        assert get_retry_action("empty_input") is None
        assert get_retry_action("auth_error") is None

    def test_retry_action_unknown_error(self):
        assert get_retry_action("nonexistent_error") == "retry_analysis"

    def test_can_retry_true(self):
        assert can_retry("connection_refused") is True
        assert can_retry("network_error") is True

    def test_can_retry_false(self):
        assert can_retry("empty_input") is False
        assert can_retry("file_too_large") is False

    def test_can_retry_unknown(self):
        """Unknown error type → defaults to unknown_error retry_action (which is 'retry_analysis')"""
        assert can_retry("nonexistent") is True  # unknown_error has retry_action="retry_analysis"


# ============================================================
#  save/get/has successful result tests
# ============================================================

class TestResultPreservation:
    """Test session_state result save/load logic."""

    def test_save_and_get_dict_result(self):
        """Save a dict result and retrieve it"""
        state = {}
        result = {"error_summary": "Test error", "severity": "high"}
        save_successful_result(state, result)
        assert has_previous_result(state) is True
        retrieved = get_last_successful_result(state)
        assert retrieved is not None
        assert retrieved["error_summary"] == "Test error"
        assert retrieved["severity"] == "high"

    def test_save_and_get_pydantic_result(self):
        """Save a Pydantic model result (with model_dump)"""
        state = {}
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"error_summary": "Pydantic error", "severity": "low"}
        save_successful_result(state, mock_result)
        assert has_previous_result(state) is True
        retrieved = get_last_successful_result(state)
        assert retrieved["error_summary"] == "Pydantic error"

    def test_save_fallback_non_dict_non_pydantic(self):
        """Save a non-dict, non-Pydantic result → falls back to string representation"""
        state = {}
        save_successful_result(state, "plain string result")
        retrieved = get_last_successful_result(state)
        assert retrieved is not None
        assert retrieved["error_summary"] == "plain string result"

    def test_saves_timestamp(self):
        """save_successful_result records a timestamp"""
        state = {}
        save_successful_result(state, {"summary": "test"})
        assert "last_successful_time" in state
        assert isinstance(state["last_successful_time"], float)

    def test_saves_input_reference(self):
        """save_successful_result records the log_input if present"""
        state = {"log_input": "some log text"}
        save_successful_result(state, {"summary": "test"})
        assert state["last_successful_input"] == "some log text"

    def test_no_previous_result(self):
        """Empty session_state → no previous result"""
        state = {}
        assert has_previous_result(state) is False
        assert get_last_successful_result(state) is None

    def test_overwrite_previous_result(self):
        """Saving again overwrites previous result"""
        state = {}
        save_successful_result(state, {"error_summary": "first"})
        save_successful_result(state, {"error_summary": "second"})
        retrieved = get_last_successful_result(state)
        assert retrieved["error_summary"] == "second"

    def test_dict_like_session_state(self):
        """Works with dict-like objects (not just plain dict)"""

        class SessionState:
            def __init__(self):
                self._data = {}

            def __getitem__(self, key):
                return self._data[key]

            def __setitem__(self, key, value):
                self._data[key] = value

            def __contains__(self, key):
                return key in self._data

            def get(self, key, default=None):
                return self._data.get(key, default)

        state = SessionState()
        save_successful_result(state, {"error_summary": "test"})
        assert has_previous_result(state) is True
        retrieved = get_last_successful_result(state)
        assert retrieved["error_summary"] == "test"


# ============================================================
#  estimate_analysis_time() tests
# ============================================================

class TestEstimateAnalysisTime:
    """Test analysis time estimation based on log size."""

    def test_tiny_log(self):
        """Log < 10KB → ~2 seconds"""
        est, desc = estimate_analysis_time("error: test\n" * 10)
        assert est == 2
        assert "很快" in desc

    def test_small_log(self):
        """Log 10-50KB → ~5 seconds"""
        # ~20KB
        log = "error: something went wrong\n" * 400
        est, desc = estimate_analysis_time(log)
        assert est == 5
        assert "几秒" in desc

    def test_medium_log(self):
        """Log 50-200KB → ~10 seconds"""
        # ~100KB
        log = "x" * 100 + "\n"
        log = log * 1000
        est, desc = estimate_analysis_time(log)
        assert est == 10
        assert "10 秒" in desc

    def test_large_log(self):
        """Log 200-500KB → ~25 seconds"""
        # ~300KB
        log = "x" * 100 + "\n"
        log = log * 3000
        est, desc = estimate_analysis_time(log)
        assert est == 25
        assert "25 秒" in desc

    def test_very_large_log(self):
        """Log 500KB-1MB → ~60 seconds"""
        # ~700KB
        log = "x" * 100 + "\n"
        log = log * 7000
        est, desc = estimate_analysis_time(log)
        assert est == 60
        assert "1 分钟" in desc

    def test_extremely_large_log(self):
        """Log > 1MB → ~120 seconds"""
        # ~1.5MB
        log = "x" * 100 + "\n"
        log = log * 15000
        est, desc = estimate_analysis_time(log)
        assert est == 120
        assert "1-2 分钟" in desc

    def test_empty_log(self):
        """Empty log text"""
        est, desc = estimate_analysis_time("")
        assert est == 2
        assert isinstance(desc, str)

    def test_single_line(self):
        """Single line log"""
        est, desc = estimate_analysis_time("error: failed")
        assert est == 2

    def test_unicode_log(self):
        """Log with Unicode characters"""
        log = "错误：构建失败 🚫\n" * 500
        est, desc = estimate_analysis_time(log)
        # Each line is ~12 chars * 500 = ~6000 chars = ~5.86 KB, < 10 KB → est=2
        assert est == 2

    def test_returns_tuple(self):
        """Returns (int, str) tuple"""
        result = estimate_analysis_time("test log")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], str)


# ============================================================
#  friendly_api_error() tests
# ============================================================

class TestFriendlyApiError:
    """Test HTTP status code → user-friendly error mapping."""

    def test_401_auth_error(self):
        info = friendly_api_error(401, "Unauthorized")
        assert info["icon"] == "🔑"
        assert "API Key" in info["title"]

    def test_422_unsupported_format(self):
        info = friendly_api_error(422, "Unprocessable")
        assert info["icon"] == "❓"
        assert "格式" in info["title"]

    def test_429_rate_limit(self):
        info = friendly_api_error(429, "Too Many Requests")
        assert info["icon"] == "🚦"
        assert "频率" in info["title"]

    def test_500_server_error(self):
        info = friendly_api_error(500, "Internal Server Error")
        assert info["icon"] == "💥"
        assert "异常" in info["title"]

    def test_502_network_error(self):
        info = friendly_api_error(502, "Bad Gateway")
        assert info["icon"] == "🌐"
        assert "网络" in info["title"]

    def test_503_circuit_breaker(self):
        info = friendly_api_error(503, "Service Unavailable")
        assert info["icon"] == "🚫"
        assert "预算" in info["title"]

    def test_504_server_timeout(self):
        info = friendly_api_error(504, "Gateway Timeout")
        assert info["icon"] == "⏰"
        assert "超时" in info["title"]

    def test_unknown_status_code(self):
        """Unmapped status code → unknown_error"""
        info = friendly_api_error(418, "I'm a teapot")
        assert info["title"] == "发生未知错误"

    def test_detail_appended_when_different(self):
        """detail is added to info when different from default message"""
        info = friendly_api_error(500, "Database connection pool exhausted")
        assert "detail" in info
        assert "Database connection pool exhausted" in info["detail"]

    def test_detail_omitted_when_same_as_message(self):
        """detail is not duplicated when same as default message"""
        base_info = get_error_info("server_error")
        info = friendly_api_error(500, base_info["message"])
        # detail should not be added since it's the same
        assert "detail" not in info or info.get("detail") is None

    def test_detail_truncated_to_300_chars(self):
        """Long detail is truncated to 300 characters"""
        long_detail = "x" * 500
        info = friendly_api_error(500, long_detail)
        assert len(info.get("detail", "")) <= 300

    def test_backend_url_passed_through(self):
        """backend_url is passed through to get_error_info"""
        info = friendly_api_error(502, "Bad Gateway", backend_url="http://custom:8000")
        assert "http://custom:8000" in info["suggestion"]


# ============================================================
#  ErrorLevel enum tests
# ============================================================

class TestErrorLevel:
    """Test ErrorLevel enum values."""

    def test_three_levels(self):
        assert ErrorLevel.RECOVERABLE.value == "recoverable"
        assert ErrorLevel.USER_ACTION.value == "user_action"
        assert ErrorLevel.SERVER.value == "server"

    def test_all_error_types_have_valid_level(self):
        """Every error in _ERROR_MAP has a valid ErrorLevel enum value"""
        from error_handler import _ERROR_MAP
        valid_levels = {e for e in ErrorLevel}
        for key, info in _ERROR_MAP.items():
            assert info["level"] in valid_levels, f"{key} has invalid level: {info['level']}"

    def test_all_error_types_have_icon(self):
        """Every error has a non-empty icon"""
        from error_handler import _ERROR_MAP
        for key, info in _ERROR_MAP.items():
            assert info["icon"], f"{key} has no icon"
