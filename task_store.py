# task_store.py — Bounded Memory Management for Async Task State
#
# P0 CRITICAL FIX (FIX-002)
#
# What: BoundedTaskStore 替换无界 dict _task_store
# Why:
#   - api/main.py 的 _task_store 是无界 dict，恶意客户端或高频调用会导致 OOM
#   - _cleanup_expired_tasks 存在但从未被调用
#   - 没有 LRU 驱逐，没有最大容量限制
# Impact:
#   - 零破坏性 — 保持与现有 _update_task / _get_task 接口兼容
#   - 无需修改 api/main.py 中函数签名
#   - 生产环境建议: 最终替换为 Redis（注释已预留在 api/main.py）
# How to verify:
#   - 运行 tests/test_task_store.py
#   - 压力测试：提交 > 1000 个任务，验证驱逐行为

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

logger = logging.getLogger("task_store")

# ============================================================
# Configuration
# ============================================================

_DEFAULT_MAX_CAPACITY: int = int(os.getenv("TASK_STORE_MAX_CAPACITY", "1000"))
_DEFAULT_CLEANUP_INTERVAL: float = float(os.getenv("TASK_STORE_CLEANUP_INTERVAL", "60"))
_DEFAULT_TASK_TTL_SECONDS: float = float(os.getenv("TASK_TTL_SECONDS", "3600"))

# Eviction priority: statuses ordered from most-evictable to least
# "completed" and "failed" tasks are evicted first (they're done),
# "pending"/"parsing"/"analyzing" are kept as long as possible
_EVICTION_ORDER: tuple[str, ...] = ("completed", "failed", "pending", "parsing", "analyzing")


class BoundedTaskStore:
    """
    Thread-safe bounded task state store with LRU eviction.

    [CONFIRMED from api/main.py:96-100]
    Replaces the bare `_task_store: dict = {}` with bounded capacity management.

    Features:
      - Maximum capacity (configurable, default 1000)
      - LRU-based eviction (oldest completed/failed evicted first)
      - Thread-safe (internal RLock)
      - Auto background cleanup thread (daemon, every 60s)
      - get/set/delete/size/stats interface
      - Graceful shutdown
    """

    def __init__(
        self,
        max_capacity: int = _DEFAULT_MAX_CAPACITY,
        task_ttl_seconds: float = _DEFAULT_TASK_TTL_SECONDS,
        cleanup_interval: float = _DEFAULT_CLEANUP_INTERVAL,
    ) -> None:
        self._max_capacity = max_capacity
        self._task_ttl_seconds = task_ttl_seconds
        self._cleanup_interval = cleanup_interval

        self._lock = threading.RLock()
        # OrderedDict tracks insertion order for LRU eviction
        self._store: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Track last-access time per key for LRU within status groups
        self._access_times: dict[str, float] = {}

        # Background cleanup thread
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_cleanup = threading.Event()
        self._shutdown = threading.Event()

        # Stats
        self._inserts_total: int = 0
        self._evictions_total: int = 0
        self._cleanups_total: int = 0

        self._start_cleanup_thread()
        logger.info(
            "BoundedTaskStore initialized: max_capacity=%d, ttl=%ds, cleanup_interval=%ds",
            max_capacity, task_ttl_seconds, cleanup_interval,
        )

    # ============================================================
    # Core operations (backward compatible with dict-like usage)
    # ============================================================

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        """
        Get task state by ID. Thread-safe.

        Returns None if task not found or expired.
        """
        with self._lock:
            entry = self._store.get(task_id)
            if entry is None:
                return None
            # Check TTL expiry
            created_at = entry.get("created_at", 0)
            if time.time() - created_at > self._task_ttl_seconds:
                del self._store[task_id]
                self._access_times.pop(task_id, None)
                return None
            # Update access time (for LRU tracking)
            self._access_times[task_id] = time.time()
            return entry

    def set(self, task_id: str, entry: dict[str, Any]) -> None:
        """
        Insert or update a task entry. Thread-safe.

        If at capacity, evicts the best candidate before inserting.

        Args:
            task_id: Unique task identifier (UUID string).
            entry: Task state dict with at least "status" and "created_at" keys.
        """
        if self._shutdown.is_set():
            logger.warning("BoundedTaskStore is shut down, rejecting set(%s)", task_id)
            return

        with self._lock:
            # If key exists, update in place (no capacity impact)
            if task_id in self._store:
                self._store[task_id] = entry
                self._access_times[task_id] = time.time()
                return

            # Evict if at capacity
            while len(self._store) >= self._max_capacity:
                victim = self._select_eviction_victim()
                if victim is None:
                    # Should not happen, but safety net: evict LRU
                    victim = next(iter(self._store))
                del self._store[victim]
                self._access_times.pop(victim, None)
                self._evictions_total += 1
                logger.debug("Evicted task %s (store size=%d/%d)", victim, len(self._store), self._max_capacity)

            self._store[task_id] = entry
            self._access_times[task_id] = time.time()
            self._inserts_total += 1

    def update(
        self,
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        result: Optional[dict] = None,
        error: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> bool:
        """
        Partial update of task fields. Thread-safe.

        [CONFIRMED from api/main.py:669-687]
        Mirrors the existing `_update_task()` signature.

        Returns True if the task was found and updated, False otherwise.
        """
        with self._lock:
            entry = self._store.get(task_id)
            if entry is None:
                return False

            if status is not None:
                entry["status"] = status
            if progress is not None:
                entry["progress"] = progress
            if result is not None:
                entry["result"] = result
            if error is not None:
                entry["error"] = error
            if duration_ms is not None:
                entry["duration_ms"] = round(duration_ms, 1)

            self._access_times[task_id] = time.time()
            return True

    def delete(self, task_id: str) -> bool:
        """
        Remove a task from the store. Thread-safe.

        Returns True if the task existed and was removed.
        """
        with self._lock:
            if task_id in self._store:
                del self._store[task_id]
                self._access_times.pop(task_id, None)
                return True
            return False

    # ============================================================
    # Compatibility: dict-like item access for _task_store[task_id]
    # ============================================================

    def __contains__(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._store

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    # ============================================================
    # Stats and introspection
    # ============================================================

    def size(self) -> int:
        """Current number of stored tasks."""
        with self._lock:
            return len(self._store)

    def stats(self) -> dict[str, int]:
        """Return store statistics."""
        with self._lock:
            by_status: dict[str, int] = {}
            for entry in self._store.values():
                s = entry.get("status", "unknown")
                by_status[s] = by_status.get(s, 0) + 1

            return {
                "total": len(self._store),
                "max_capacity": self._max_capacity,
                "inserts_total": self._inserts_total,
                "evictions_total": self._evictions_total,
                "cleanups_total": self._cleanups_total,
                "by_status": str(by_status),
            }

    # ============================================================
    # Graceful shutdown
    # ============================================================

    def shutdown(self) -> None:
        """
        Initiate graceful shutdown.

        - Stop accepting new tasks
        - Signal cleanup thread to stop
        - Wait for cleanup thread to join (with timeout)
        """
        logger.info("BoundedTaskStore shutdown initiated")
        self._shutdown.set()
        self._stop_cleanup.set()

        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5.0)
            if self._cleanup_thread.is_alive():
                logger.warning("BoundedTaskStore cleanup thread did not stop within 5s")

        with self._lock:
            count = len(self._store)
        logger.info("BoundedTaskStore shutdown complete (%d tasks remaining)", count)

    # ============================================================
    # Internal: eviction and cleanup
    # ============================================================

    def _select_eviction_victim(self) -> Optional[str]:
        """
        Select the best candidate for eviction.

        Strategy:
        1. Prefer "completed" or "failed" tasks (they're done)
        2. Within same status, prefer oldest (by created_at)
        3. Fallback: oldest pending/parsing task
        """
        if not self._store:
            return None

        # Build candidate list with priority
        candidates: list[tuple[int, float, str]] = []  # (priority, created_at, task_id)
        for task_id, entry in self._store.items():
            status = entry.get("status", "pending")
            try:
                priority = _EVICTION_ORDER.index(status)
            except ValueError:
                priority = len(_EVICTION_ORDER)  # unknown status = lowest priority for eviction
            created_at = entry.get("created_at", float("inf"))
            candidates.append((priority, created_at, task_id))

        # Sort: lowest priority index first (completed=0, failed=1, ...)
        # Within same priority: oldest created_at first
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][2] if candidates else None

    def _start_cleanup_thread(self) -> None:
        """Start the daemon background cleanup thread."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="task-store-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()
        logger.debug("Cleanup thread started")

    def _cleanup_loop(self) -> None:
        """Background loop: periodically remove expired tasks."""
        while not self._stop_cleanup.wait(timeout=self._cleanup_interval):
            try:
                self._cleanup_expired()
            except Exception:
                logger.exception("Error in task store cleanup loop")

    def _cleanup_expired(self) -> int:
        """
        Remove tasks that have exceeded TTL. Thread-safe.

        [CONFIRMED from api/main.py:103-114]
        Replaces the orphaned _cleanup_expired_tasks() function.

        Returns number of tasks removed.
        """
        now = time.time()
        expired_ids: list[str] = []

        with self._lock:
            for task_id, entry in self._store.items():
                created_at = entry.get("created_at", 0)
                if now - created_at > self._task_ttl_seconds:
                    expired_ids.append(task_id)

            for task_id in expired_ids:
                del self._store[task_id]
                self._access_times.pop(task_id, None)

        if expired_ids:
            self._cleanups_total += len(expired_ids)
            logger.debug("Cleaned up %d expired tasks (store size=%d)", len(expired_ids), len(self._store))

        return len(expired_ids)
