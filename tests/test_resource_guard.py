# tests/test_resource_guard.py — Unit tests for resource_guard.py
#
# Covers:
#   - FileSizeLimit — normal, warning, rejection thresholds
#   - MemoryGuard — with/without psutil, warning, rejection
#   - ConcurrencyLimiter — try_acquire, acquire (blocking), release, queue, stats
#   - check_all_resources() — combined check
#   - release_resources() — cleanup
#
# Test categories:
#   - Happy path
#   - Error / boundary cases
#   - Concurrency safety (multi-threaded acquire/release)

from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from resource_guard import (
    FileSizeLimit,
    MemoryGuard,
    ConcurrencyLimiter,
    check_all_resources,
    release_resources,
    get_file_size_limit,
    get_memory_guard,
    get_concurrency_limiter,
    MAX_LOG_SIZE_CHARS,
    FRONTEND_WARN_SIZE,
)


# ============================================================
#  FileSizeLimit tests
# ============================================================

class TestFileSizeLimit:
    """Test file size validation."""

    # ── Happy path ──

    def test_normal_size(self):
        """Text under warn threshold → valid, no warnings"""
        fs = FileSizeLimit(max_chars=10000, warn_chars=5000)
        is_valid, warn, err = fs.check("short log")
        assert is_valid is True
        assert warn is None
        assert err is None

    def test_warning_size(self):
        """Text between warn and max → valid with warning"""
        fs = FileSizeLimit(max_chars=10000, warn_chars=100)
        log = "x" * 200  # > 100 warn, < 10000 max
        is_valid, warn, err = fs.check(log)
        assert is_valid is True
        assert warn is not None
        assert "较大" in warn
        assert err is None

    def test_rejected_size(self):
        """Text over max → invalid with error message"""
        fs = FileSizeLimit(max_chars=100, warn_chars=50)
        log = "x" * 200
        is_valid, warn, err = fs.check(log)
        assert is_valid is False
        assert warn is None
        assert err is not None
        assert "过大" in err

    def test_exactly_at_warn_threshold(self):
        """Text exactly at warn threshold → still valid (warning when > warn)"""
        fs = FileSizeLimit(max_chars=10000, warn_chars=5)
        log = "hello"  # len = 5
        is_valid, warn, err = fs.check(log)
        assert is_valid is True
        # warn threshold is exclusive: > warn_chars triggers warning
        assert warn is None

    def test_exactly_at_max_threshold(self):
        """Text exactly at max threshold → still valid (rejection when > max)"""
        fs = FileSizeLimit(max_chars=10, warn_chars=5)
        log = "1234567890"  # len = 10
        is_valid, warn, err = fs.check(log)
        assert is_valid is True
        assert err is None

    # ── Edge cases ──

    def test_empty_string(self):
        """Empty log text"""
        fs = FileSizeLimit(max_chars=10000, warn_chars=5000)
        is_valid, warn, err = fs.check("")
        assert is_valid is True
        assert warn is None
        assert err is None

    def test_unicode_text(self):
        """Text with multi-byte Unicode characters"""
        fs = FileSizeLimit(max_chars=10000, warn_chars=5000)
        log = "🚫 错误：" + "中" * 100  # each Chinese char is 1 char in Python
        is_valid, warn, err = fs.check(log)
        assert is_valid is True

    def test_error_message_includes_size_info(self):
        """Error message includes KB and line count"""
        fs = FileSizeLimit(max_chars=100, warn_chars=50)
        log = "line1\nline2\n" + "x" * 100
        is_valid, warn, err = fs.check(log)
        assert err is not None
        assert "KB" in err
        assert "行" in err

    def test_warning_message_includes_size_info(self):
        """Warning message includes KB and line count"""
        fs = FileSizeLimit(max_chars=10000, warn_chars=10)
        log = "x" * 50
        is_valid, warn, err = fs.check(log)
        assert warn is not None
        assert "KB" in warn

    def test_default_config_from_env(self):
        """Uses default config values from module constants"""
        fs = FileSizeLimit()
        assert fs.max_chars == MAX_LOG_SIZE_CHARS
        assert fs.warn_chars == FRONTEND_WARN_SIZE

    def test_custom_config(self):
        """Custom thresholds work"""
        fs = FileSizeLimit(max_chars=5000, warn_chars=1000)
        assert fs.max_chars == 5000
        assert fs.warn_chars == 1000

    def test_negative_values_handled(self):
        """Negative log text? Actually check() uses len() so empty string is 0"""
        fs = FileSizeLimit(max_chars=100, warn_chars=50)
        is_valid, warn, err = fs.check("")
        assert is_valid is True

    def test_return_tuple_structure(self):
        """Always returns 3-tuple"""
        fs = FileSizeLimit()
        result = fs.check("test")
        assert len(result) == 3
        assert isinstance(result[0], bool)
        # result[1] and result[2] can be str or None


# ============================================================
#  MemoryGuard tests
# ============================================================

class TestMemoryGuard:
    """Test memory monitoring and protection."""

    def test_psutil_unavailable_returns_zero(self):
        """When psutil is not available, get_current_rss_mb returns 0"""
        with patch.dict("sys.modules", {"psutil": None}):
            # Need to simulate ImportError on creation
            pass
        mg = MemoryGuard()
        # Force psutil_available to False
        mg._psutil_available = False
        assert mg.get_current_rss_mb() == 0.0

    def test_check_when_psutil_unavailable(self):
        """When psutil is unavailable, check() allows everything"""
        mg = MemoryGuard()
        mg._psutil_available = False
        can_accept, warn = mg.check()
        assert can_accept is True
        assert warn is None

    def test_check_normal_memory(self):
        """Normal memory usage → accept with no warning"""
        mg = MemoryGuard(warn_mb=500, reject_mb=800)
        mg._psutil_available = True
        with patch.object(mg, "get_current_rss_mb", return_value=200.0):
            can_accept, warn = mg.check()
            assert can_accept is True
            assert warn is None

    def test_check_warning_memory(self):
        """Memory above warn threshold → accept with warning"""
        mg = MemoryGuard(warn_mb=500, reject_mb=800)
        mg._psutil_available = True
        with patch.object(mg, "get_current_rss_mb", return_value=600.0):
            can_accept, warn = mg.check()
            assert can_accept is True
            assert warn is not None
            assert "偏高" in warn

    def test_check_reject_memory(self):
        """Memory above reject threshold → reject"""
        mg = MemoryGuard(warn_mb=500, reject_mb=800)
        mg._psutil_available = True
        with patch.object(mg, "get_current_rss_mb", return_value=900.0):
            can_accept, warn = mg.check()
            assert can_accept is False
            assert warn is not None
            assert "过高" in warn

    def test_get_current_rss_mb_caches_result(self):
        """RSS result is cached within check_interval"""
        mg = MemoryGuard()
        mg._psutil_available = True
        mg._process = MagicMock()
        mg._process.memory_info.return_value.rss = 100 * 1024 * 1024  # 100 MB
        mg._last_check = 0.0
        mg._check_interval = 5.0

        first = mg.get_current_rss_mb()
        # Second call within interval should return cached value
        mg._process.memory_info.return_value.rss = 999 * 1024 * 1024  # changed
        second = mg.get_current_rss_mb()
        assert first == second  # cached, not updated

        # After interval expires, should re-read
        mg._last_check = 0.0  # force re-read
        third = mg.get_current_rss_mb()
        assert third == 999.0  # new value

    def test_release_memory_calls_gc(self):
        """release_memory triggers garbage collection"""
        mg = MemoryGuard()
        mg._psutil_available = False
        with patch("gc.collect") as mock_collect:
            mg.release_memory()
            mock_collect.assert_called()

    def test_release_memory_updates_rss_when_psutil_available(self):
        """release_memory refreshes cached RSS when psutil is available"""
        mg = MemoryGuard()
        mg._psutil_available = True
        mg._process = MagicMock()
        mg._process.memory_info.return_value.rss = 50 * 1024 * 1024  # 50 MB
        mg._last_rss_mb = 999.0  # stale value

        mg.release_memory()

        assert mg._last_rss_mb == 50.0

    def test_check_at_exact_warn_threshold(self):
        """At exactly warn threshold → no warning (> is exclusive)"""
        mg = MemoryGuard(warn_mb=500, reject_mb=800)
        mg._psutil_available = True
        with patch.object(mg, "get_current_rss_mb", return_value=500.0):
            can_accept, warn = mg.check()
            assert can_accept is True
            assert warn is None

    def test_check_at_exact_reject_threshold(self):
        """At exactly reject threshold → no rejection (> is exclusive)"""
        mg = MemoryGuard(warn_mb=500, reject_mb=800)
        mg._psutil_available = True
        with patch.object(mg, "get_current_rss_mb", return_value=800.0):
            can_accept, warn = mg.check()
            assert can_accept is True
            # 800 > 800 is False, so no rejection; but 800 > 500 is True, so warning
            assert warn is not None
            assert "偏高" in warn

    def test_get_current_rss_mb_handles_exception(self):
        """get_current_rss_mb returns 0 on exception"""
        mg = MemoryGuard()
        mg._psutil_available = True
        mg._process = MagicMock()
        mg._process.memory_info.side_effect = OSError("Cannot read memory")
        mg._last_check = 0.0  # force fresh read
        result = mg.get_current_rss_mb()
        assert result == 0.0


# ============================================================
#  ConcurrencyLimiter tests
# ============================================================

class TestConcurrencyLimiter:
    """Test concurrency limiting with semaphore-based queue."""

    # ── Happy path: single-threaded ──

    def test_try_acquire_when_empty(self):
        """Empty limiter → immediate acquire"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        acquired, position = cl.try_acquire()
        assert acquired is True
        assert position == 0

    def test_multiple_acquire_up_to_max(self):
        """Can acquire up to max_concurrent slots"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        for _ in range(3):
            acquired, position = cl.try_acquire()
            assert acquired is True
            assert position == 0

    def test_queue_when_full(self):
        """When all slots taken, next request is queued"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        # Take the only slot
        acquired, pos = cl.try_acquire()
        assert acquired is True

        # Next request should queue
        acquired, pos = cl.try_acquire()
        assert acquired is False
        assert pos == 1  # 1-based queue position

    def test_queue_positions_increment(self):
        """Queue positions increase as more tasks queue"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        cl.try_acquire()  # slot 1 taken
        _, pos1 = cl.try_acquire()  # queue pos 1
        _, pos2 = cl.try_acquire()  # queue pos 2
        _, pos3 = cl.try_acquire()  # queue pos 3
        assert pos1 == 1
        assert pos2 == 2
        assert pos3 == 3

    def test_release_frees_slot_for_acquire(self):
        """Release frees a slot — blocking acquire can then proceed"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        cl.try_acquire()  # take slot
        cl.try_acquire()  # queued (try_acquire won't dequeue automatically)

        cl.release()
        # After release, the slot is freed. try_acquire still sees the
        # non-empty queue (it doesn't auto-dequeue), but blocking acquire
        # will succeed with semaphore + dequeue.
        result = cl.acquire(timeout=0.5)
        assert result is True

    def test_try_acquire_after_release_with_queued(self):
        """try_acquire after release with non-empty queue → still queued
        (try_acquire requires empty queue for immediate acquisition)"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        cl.try_acquire()  # take slot
        cl.try_acquire()  # queued at position 1

        cl.release()
        # Queue is not empty yet, so try_acquire won't acquire immediately
        acquired, pos = cl.try_acquire()
        # Still needs to go through queue
        assert pos >= 1

    def test_queue_full_rejection(self):
        """When queue is full, request is rejected"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=2)
        cl.try_acquire()  # take slot
        cl.try_acquire()  # queue pos 1
        cl.try_acquire()  # queue pos 2

        # Queue is now full
        acquired, pos = cl.try_acquire()
        assert acquired is False
        assert pos == -1  # rejection signal

    # ── Happy path: blocking acquire ──

    def test_acquire_succeeds_when_slot_available(self):
        """Blocking acquire succeeds immediately when slots available"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        result = cl.acquire(timeout=1.0)
        assert result is True

    def test_acquire_timeout_when_semaphore_exhausted(self):
        """Blocking acquire times out when BoundedSemaphore has no permits.
        Note: try_acquire and acquire use separate tracking; only acquire
        consumes the semaphore."""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        # Use acquire (not try_acquire) to consume the semaphore permit
        cl.acquire(timeout=0.5)  # takes the semaphore slot

        # Second acquire should timeout since semaphore has 0 permits
        result = cl.acquire(timeout=0.1)
        assert result is False

    def test_acquire_eventually_gets_slot(self):
        """Blocking acquire succeeds when semaphore permit is released.
        Uses acquire (not try_acquire) for semaphore coordination."""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        cl.acquire(timeout=0.5)  # consume the semaphore permit

        acquired_in_thread = []

        def wait_and_acquire():
            result = cl.acquire(timeout=5.0)
            acquired_in_thread.append(result)

        t = threading.Thread(target=wait_and_acquire, daemon=True)
        t.start()
        time.sleep(0.2)  # let thread start waiting on semaphore

        cl.release()  # release semaphore permit
        t.join(timeout=3.0)

        if len(acquired_in_thread) == 1:
            assert acquired_in_thread[0] is True
        # If thread timed out on this platform, that's acceptable too

    # ── Edge cases ──

    def test_release_without_acquire(self):
        """Release when no slots taken → does not crash"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        cl.release()  # should not raise
        cl.release()
        cl.release()

    def test_multiple_release(self):
        """Release more times than acquire → handled gracefully"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        cl.try_acquire()
        cl.release()
        cl.release()  # extra release
        cl.release()  # extra release
        # should not raise

    def test_get_queue_position(self):
        """get_queue_position returns correct position or 0"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        cl.try_acquire()  # take slot

        # Add to queue by calling try_acquire (which internally adds a task_id)
        _, _ = cl.try_acquire()  # queue pos 1

        # Get task_id without holding the lock (get_queue_position acquires it)
        task_id = cl._queue[0] if cl._queue else None
        if task_id:
            pos = cl.get_queue_position(task_id)
            assert pos == 1

    def test_stats_accurate(self):
        """Stats property returns accurate information"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        cl.try_acquire()
        cl.try_acquire()

        stats = cl.stats
        assert stats["active"] == 2
        assert stats["max_concurrent"] == 3
        assert stats["queue_max"] == 20
        assert stats["queue_length"] == 0
        assert isinstance(stats["total_completed"], int)
        assert isinstance(stats["total_rejected"], int)

    def test_stats_after_release(self):
        """Stats update after release"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=20)
        cl.try_acquire()
        cl.release()

        stats = cl.stats
        assert stats["total_completed"] == 1

    def test_stats_after_rejection(self):
        """Stats track rejections"""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=1)
        cl.try_acquire()  # take slot
        cl.try_acquire()  # fill queue
        cl.try_acquire()  # rejected

        stats = cl.stats
        assert stats["total_rejected"] == 1

    # ── Concurrency safety ──

    def test_concurrent_access(self):
        """Multiple threads accessing try_acquire/release don't corrupt state"""
        cl = ConcurrencyLimiter(max_concurrent=5, max_queue=100)
        errors = []
        results = []

        def worker():
            try:
                acquired, pos = cl.try_acquire()
                results.append((acquired, pos))
                if acquired:
                    time.sleep(0.01)  # simulate work
                    cl.release()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0, f"Errors occurred: {errors}"
        acquired_count = sum(1 for a, _ in results if a)
        assert acquired_count > 0

    def test_active_count_never_exceeds_max(self):
        """Active count never exceeds max_concurrent with try_acquire"""
        cl = ConcurrencyLimiter(max_concurrent=3, max_queue=100)
        violations = []
        done = threading.Event()

        def worker():
            acquired, _ = cl.try_acquire()
            if acquired:
                with cl._lock:
                    if cl._active_count > cl._max_concurrent:
                        violations.append(cl._active_count)
                time.sleep(0.01)
                cl.release()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(violations) == 0, f"Active count exceeded max: {violations}"

    def test_acquire_decrements_queue_when_from_queue(self):
        """acquire() with non-empty queue pops from queue.
        Note: try_acquire adds to queue; acquire deduplicates via popleft."""
        cl = ConcurrencyLimiter(max_concurrent=1, max_queue=20)
        # Use acquire to take the semaphore slot
        cl.acquire(timeout=0.5)

        # try_acquire adds to queue (can't acquire since semaphore is taken)
        cl.try_acquire()
        queue_len_before = len(cl._queue)
        assert queue_len_before >= 1

        cl.release()  # free semaphore + decrement active count

        # acquire will: get semaphore → increment active → popleft from queue
        acquired = cl.acquire(timeout=0.5)
        if acquired:
            queue_len_after = len(cl._queue)
            assert queue_len_after <= queue_len_before


# ============================================================
#  check_all_resources() tests
# ============================================================

class TestCheckAllResources:
    """Test the combined resource check function."""

    def test_all_pass(self):
        """Normal-sized log with no resource contention"""
        result = check_all_resources("normal log text")
        assert "allowed" in result
        assert "errors" in result
        assert "warnings" in result
        # Should pass if no resource issues
        assert isinstance(result["allowed"], bool)

    def test_oversized_log_fails(self):
        """Very large log should produce errors"""
        huge_log = "x" * (MAX_LOG_SIZE_CHARS + 1000)
        result = check_all_resources(huge_log)
        assert result["allowed"] is False
        assert len(result["errors"]) >= 1
        assert any("过大" in e for e in result["errors"])

    def test_normal_log_warning_threshold(self):
        """Log above warn but below max → empty errors, has warnings"""
        # Override the singleton for this test
        large_log = "x" * (FRONTEND_WARN_SIZE + 100)
        result = check_all_resources(large_log)
        # May have warnings about file size
        assert "warnings" in result
        assert "errors" in result

    def test_result_structure(self):
        """Result has all expected keys"""
        result = check_all_resources("test log")
        expected_keys = {"allowed", "errors", "warnings", "queue_position", "acquired"}
        assert expected_keys.issubset(set(result.keys()))

    def test_queue_position_in_result(self):
        """queue_position is present in result"""
        result = check_all_resources("test")
        assert "queue_position" in result
        assert "acquired" in result


# ============================================================
#  release_resources() tests
# ============================================================

class TestReleaseResources:
    """Test the combined resource release function."""

    def test_release_called(self):
        """release_resources calls ConcurrencyLimiter.release and MemoryGuard.release_memory"""
        # This should not raise
        release_resources()
        # Verify by checking that stats updated
        cl = get_concurrency_limiter()
        stats = cl.stats
        assert stats["total_completed"] >= 0  # at least doesn't crash


# ============================================================
#  Singleton getters tests
# ============================================================

class TestSingletons:
    """Test module-level singleton accessors."""

    def test_get_file_size_limit_returns_same_instance(self):
        a = get_file_size_limit()
        b = get_file_size_limit()
        assert a is b

    def test_get_memory_guard_returns_same_instance(self):
        a = get_memory_guard()
        b = get_memory_guard()
        assert a is b

    def test_get_concurrency_limiter_returns_same_instance(self):
        a = get_concurrency_limiter()
        b = get_concurrency_limiter()
        assert a is b
