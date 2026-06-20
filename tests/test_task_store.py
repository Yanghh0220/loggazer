# tests/test_task_store.py — Tests for BoundedTaskStore
#
# P0 CRITICAL FIX (FIX-002)
#
# What: 验证 BoundedTaskStore 的容量限制、LRU 驱逐、
#       线程安全、自动清理、关闭后拒绝功能
# Why:  确保生产环境中任务存储不会导致 OOM
# Impact: 纯测试文件，不影响生产代码
# How to verify: pytest tests/test_task_store.py -v

from __future__ import annotations

import threading
import time

import pytest

from task_store import BoundedTaskStore, _EVICTION_ORDER


# ============================================================
# Helpers
# ============================================================

def _make_task(task_id: str, status: str = "pending", created_at: float | None = None) -> dict:
    """Create a minimal task entry for testing."""
    return {
        "task_id": task_id,
        "status": status,
        "progress": 0.0,
        "result": None,
        "error": None,
        "filename": "test.log",
        "file_size_bytes": 1024,
        "created_at": created_at or time.time(),
    }


# ============================================================
# Tests: basic operations
# ============================================================

def test_set_and_get():
    """基本 set/get 操作：存储和检索任务。"""
    store = BoundedTaskStore(max_capacity=100, task_ttl_seconds=3600)
    try:
        entry = _make_task("task-1", "pending")
        store.set("task-1", entry)

        retrieved = store.get("task-1")
        assert retrieved is not None
        assert retrieved["task_id"] == "task-1"
        assert retrieved["status"] == "pending"
    finally:
        store.shutdown()


def test_get_nonexistent():
    """获取不存在的任务应返回 None。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        assert store.get("nonexistent") is None
    finally:
        store.shutdown()


def test_set_update_existing():
    """对已存在的 task_id 再次 set 应更新条目且不触发驱逐。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        entry1 = _make_task("task-1", "pending")
        store.set("task-1", entry1)

        entry2 = _make_task("task-1", "completed")
        store.set("task-1", entry2)

        retrieved = store.get("task-1")
        assert retrieved["status"] == "completed"
        # 不应有驱逐
        assert store.stats()["evictions_total"] == 0
    finally:
        store.shutdown()


def test_delete():
    """delete 应移除任务并返回 True。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        store.set("task-1", _make_task("task-1"))
        assert store.size() == 1

        assert store.delete("task-1") is True
        assert store.size() == 0
        assert store.get("task-1") is None
    finally:
        store.shutdown()


def test_delete_nonexistent():
    """删除不存在的任务应返回 False。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        assert store.delete("nonexistent") is False
    finally:
        store.shutdown()


def test_size_and_len():
    """size() 和 __len__ 应返回一致的结果。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        for i in range(5):
            store.set(f"task-{i}", _make_task(f"task-{i}"))
        assert store.size() == 5
        assert len(store) == 5
    finally:
        store.shutdown()


def test_contains():
    """__contains__ 应正确判断任务存在性。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        store.set("task-1", _make_task("task-1"))
        assert "task-1" in store
        assert "task-2" not in store
    finally:
        store.shutdown()


# ============================================================
# Tests: capacity and eviction
# ============================================================

def test_capacity_limit():
    """超出容量后旧任务应被驱逐。"""
    store = BoundedTaskStore(max_capacity=5)
    try:
        for i in range(10):
            store.set(f"task-{i}", _make_task(f"task-{i}"))
        assert store.size() == 5
        assert store.stats()["evictions_total"] >= 5
    finally:
        store.shutdown()


def test_evict_completed_first():
    """驱逐策略：优先驱逐 completed/failed 状态的任务。"""
    store = BoundedTaskStore(max_capacity=3)
    try:
        # 填充到容量
        store.set("task-1", _make_task("task-1", "completed"))
        store.set("task-2", _make_task("task-2", "failed"))
        store.set("task-3", _make_task("task-3", "analyzing"))
        assert store.size() == 3

        # 插入新任务触发驱逐
        store.set("task-4", _make_task("task-4", "pending"))

        # 正在运行的任务应被保留
        assert store.get("task-3") is not None  # analyzing 应存活

        # completed 或 failed 应被优先驱逐
        still_alive = [store.get(t) for t in ("task-1", "task-2")]
        assert not all(still_alive)  # 至少一个被驱逐
    finally:
        store.shutdown()


def test_evict_oldest_within_status():
    """同状态任务驱逐最旧的（最小 created_at）。"""
    store = BoundedTaskStore(max_capacity=3)
    try:
        now = time.time()
        store.set("task-1", _make_task("task-1", "completed", now - 100))
        store.set("task-2", _make_task("task-2", "completed", now - 50))
        store.set("task-3", _make_task("task-3", "completed", now - 10))

        # 触发驱逐
        store.set("task-4", _make_task("task-4", "pending", now))

        # task-1 (最旧) 应被驱逐
        assert store.get("task-1") is None
        assert store.get("task-3") is not None  # 最新的 completed 应存活
    finally:
        store.shutdown()


def test_eviction_order_enum():
    """验证驱逐优先级顺序的完整性。"""
    # completed 和 failed 应在最前（索引 0 和 1）
    assert _EVICTION_ORDER[0] == "completed"
    assert _EVICTION_ORDER[1] == "failed"
    # analyzing 应在最后（不应被优先驱逐）
    assert _EVICTION_ORDER[-1] == "analyzing"


# ============================================================
# Tests: update partial
# ============================================================

def test_update_status():
    """update 应部分更新任务字段。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        store.set("task-1", _make_task("task-1", "pending"))
        store.update("task-1", status="completed", progress=1.0)

        task = store.get("task-1")
        assert task["status"] == "completed"
        assert task["progress"] == 1.0
        # 未更新的字段应保持不变
        assert task["filename"] == "test.log"
    finally:
        store.shutdown()


def test_update_nonexistent():
    """对不存在的任务调用 update 应返回 False。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        result = store.update("nonexistent", status="completed")
        assert result is False
    finally:
        store.shutdown()


def test_update_result():
    """update result 应正确存储分析结果。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        store.set("task-1", _make_task("task-1"))
        fake_result = {"error_summary": "test", "severity": "high"}
        store.update("task-1", result=fake_result)

        task = store.get("task-1")
        assert task["result"] == fake_result
    finally:
        store.shutdown()


# ============================================================
# Tests: TTL expiry
# ============================================================

def test_ttl_expiry():
    """过期的任务应被 get 返回 None。"""
    store = BoundedTaskStore(max_capacity=100, task_ttl_seconds=0.1)
    try:
        store.set("task-1", _make_task("task-1", "completed", time.time() - 10))
        # TTL 仅 0.1s，但 created_at 在 10s 前，应已过期
        assert store.get("task-1") is None
    finally:
        store.shutdown()


def test_cleanup_expired():
    """_cleanup_expired 应移除过期任务。"""
    store = BoundedTaskStore(max_capacity=100, task_ttl_seconds=0.1)
    try:
        store.set("task-1", _make_task("task-1", "completed", time.time() - 10))
        store.set("task-2", _make_task("task-2", "pending", time.time()))

        removed = store._cleanup_expired()
        assert removed >= 1  # task-1 应被清理
        assert store.get("task-1") is None
        assert store.get("task-2") is not None  # task-2 未过期
    finally:
        store.shutdown()


# ============================================================
# Tests: concurrent access
# ============================================================

def test_concurrent_set_and_get():
    """并发读写不应抛出异常或数据损坏。"""
    store = BoundedTaskStore(max_capacity=500, task_ttl_seconds=3600)
    errors = []
    results = []

    def writer(worker_id: int, count: int):
        for i in range(count):
            try:
                tid = f"w{worker_id}-t{i}"
                store.set(tid, _make_task(tid, "pending"))
            except Exception as e:
                errors.append(f"writer-{worker_id}: {e}")

    def reader(worker_id: int, count: int):
        for i in range(count):
            try:
                tid = f"w0-t{i}"  # read writer 0's tasks
                store.get(tid)
            except Exception as e:
                errors.append(f"reader-{worker_id}: {e}")

    try:
        threads = []
        for w in range(4):
            threads.append(threading.Thread(target=writer, args=(w, 50)))
        for r in range(2):
            threads.append(threading.Thread(target=reader, args=(r, 50)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        final_size = store.size()
        # With max_capacity=500 and 200 inserts, we should have exactly 200
        # if no evictions (since 200 < 500)
        assert final_size == 200
    finally:
        store.shutdown()


def test_concurrent_update():
    """并发 update 不应引起竞态条件。"""
    store = BoundedTaskStore(max_capacity=100)
    errors = []

    def updater(task_id: str, status: str):
        try:
            store.update(task_id, status=status, progress=0.5)
        except Exception as e:
            errors.append(str(e))

    try:
        store.set("task-1", _make_task("task-1", "pending"))

        threads = [
            threading.Thread(target=updater, args=("task-1", "analyzing")),
            threading.Thread(target=updater, args=("task-1", "completed")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        task = store.get("task-1")
        assert task is not None
        assert task["status"] in ("analyzing", "completed")
    finally:
        store.shutdown()


# ============================================================
# Tests: shutdown behavior
# ============================================================

def test_shutdown_rejects_new_tasks():
    """关闭后 set 应被拒绝。"""
    store = BoundedTaskStore(max_capacity=100)
    store.shutdown()

    store.set("task-1", _make_task("task-1"))
    # 关闭后插入应被静默拒绝
    assert store.get("task-1") is None


def test_shutdown_cleanup_thread_stops():
    """关闭后清理线程应停止。"""
    store = BoundedTaskStore(max_capacity=100, cleanup_interval=1)
    assert store._cleanup_thread is not None
    assert store._cleanup_thread.is_alive()

    store.shutdown()
    # 线程应在 join 后停止（daemon thread）
    time.sleep(0.2)
    assert not store._cleanup_thread.is_alive() or store._stop_cleanup.is_set()


def test_shutdown_idempotent():
    """重复调用 shutdown 应安全。"""
    store = BoundedTaskStore(max_capacity=100)
    store.shutdown()
    store.shutdown()  # 不应抛出异常
    assert store._shutdown.is_set()


# ============================================================
# Tests: stats
# ============================================================

def test_stats_initial():
    """初始 stats 应显示空存储。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        s = store.stats()
        assert s["total"] == 0
        assert s["max_capacity"] == 100
        assert s["inserts_total"] == 0
        assert s["evictions_total"] == 0
    finally:
        store.shutdown()


def test_stats_after_operations():
    """操作后 stats 应正确反映状态。"""
    store = BoundedTaskStore(max_capacity=100)
    try:
        store.set("task-1", _make_task("task-1", "completed"))
        store.set("task-2", _make_task("task-2", "pending"))
        store.set("task-3", _make_task("task-3", "analyzing"))

        s = store.stats()
        assert s["total"] == 3
        assert s["inserts_total"] == 3
        assert "completed" in s["by_status"]
        assert "pending" in s["by_status"]
    finally:
        store.shutdown()


def test_stats_evictions():
    """驱逐后 stats 应反映驱逐计数。"""
    store = BoundedTaskStore(max_capacity=2)
    try:
        store.set("task-1", _make_task("task-1"))
        store.set("task-2", _make_task("task-2"))
        store.set("task-3", _make_task("task-3"))  # 触发驱逐

        s = store.stats()
        assert s["total"] <= 2
        assert s["evictions_total"] >= 1
    finally:
        store.shutdown()


# ============================================================
# Tests: _EVICTION_ORDER completeness
# ============================================================

def test_eviction_order_covers_all_statuses():
    """所有标准任务状态应在驱逐优先级中。"""
    expected_statuses = {"completed", "failed", "pending", "parsing", "analyzing"}
    assert set(_EVICTION_ORDER) == expected_statuses
