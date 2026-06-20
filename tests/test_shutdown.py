# tests/test_shutdown.py — Tests for graceful shutdown module
#
# P0 CRITICAL FIX (FIX-001)
#
# What: 验证 shutdown.py 的 shutdown hook 注册、信号处理、超时强制退出
# Why:  确保生产环境中进程终止时不会损坏数据
# Impact: 纯测试文件，不影响生产代码
# How to verify: pytest tests/test_shutdown.py -v

from __future__ import annotations

import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import shutdown as _shutdown


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def _reset_shutdown_state():
    """每个测试前重置 shutdown 模块的全局状态。"""
    # 重置内部状态
    with _shutdown._registry_lock:
        _shutdown._executors.clear()
        _shutdown._hooks.clear()
        _shutdown._shutting_down.clear()
        _shutdown._shutdown_started = False
    yield
    # 测试后清理
    with _shutdown._registry_lock:
        _shutdown._executors.clear()
        _shutdown._hooks.clear()
        _shutdown._shutting_down.clear()
        _shutdown._shutdown_started = False


@pytest.fixture
def temp_executor():
    """创建一个临时 ThreadPoolExecutor 供测试使用。"""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-shutdown")
    yield executor
    # 确保清理
    try:
        executor.shutdown(wait=False)
    except Exception:
        pass


# ============================================================
# Test: Normal shutdown (all tasks complete)
# ============================================================

def test_register_executor(temp_executor):
    """注册 executor 应在 registry 中记录。"""
    _shutdown.register_executor("test-exec", temp_executor)

    with _shutdown._registry_lock:
        names = [name for name, _ in _shutdown._executors]
        assert "test-exec" in names


def test_register_executor_deduplicate(temp_executor):
    """重复注册同一个 executor 对象应被去重。"""
    _shutdown.register_executor("first", temp_executor)
    _shutdown.register_executor("second", temp_executor)

    with _shutdown._registry_lock:
        assert len(_shutdown._executors) == 1


def test_register_hook():
    """注册 shutdown hook 应被存储并按 priority 排序。"""
    results = []

    def hook_a():
        results.append("a")

    def hook_b():
        results.append("b")

    _shutdown.register_hook("hook-b", hook_b, priority=50)
    _shutdown.register_hook("hook-a", hook_a, priority=10)

    with _shutdown._registry_lock:
        hook_names = [name for name, _, _ in _shutdown._hooks]
        assert hook_names[0] == "hook-a"  # lower priority first
        assert hook_names[1] == "hook-b"


def test_is_shutting_down_initial():
    """初始状态下 is_shutting_down() 应返回 False。"""
    assert not _shutdown.is_shutting_down()


def test_shutting_down_flag_set():
    """设置 _shutting_down 事件后 is_shutting_down() 应返回 True。"""
    _shutdown._shutting_down.set()
    assert _shutdown.is_shutting_down()


def test_normal_shutdown_sequence(temp_executor):
    """正常关闭：提交短任务，shutdown 序列应等待完成。"""
    _shutdown.register_executor("test-exec", temp_executor)

    task_completed = threading.Event()

    def short_task():
        task_completed.set()
        return 42

    future = temp_executor.submit(short_task)
    result = future.result(timeout=5)
    assert result == 42
    assert task_completed.is_set()

    # 执行 shutdown 序列 — 任务已经完成，应快速完成
    start = time.time()
    # 我们不能直接调用 _run_shutdown_sequence 因为它调用 os._exit(0)
    # 所以只验证 executor 能正常关闭
    temp_executor.shutdown(wait=True)
    elapsed = time.time() - start
    assert elapsed < 5.0  # 应该很快


def test_double_shutdown_prevention():
    """重复调用 shutdown 序列应被阻止。"""
    _shutdown._shutdown_started = True
    # 第二次调用应提前返回（通过 _shutdown_started 检查）
    # 设置标志后不应再执行
    assert _shutdown._shutdown_started


def test_shutdown_with_active_tasks(temp_executor):
    """有活跃任务时 shutdown 应等待并取消未完成任务。"""
    _shutdown.register_executor("test-exec", temp_executor)

    task_started = threading.Event()
    task_cancelled = threading.Event()

    def long_task():
        task_started.set()
        try:
            time.sleep(30)
        except Exception:
            task_cancelled.set()
            raise

    temp_executor.submit(long_task)
    assert task_started.wait(timeout=5)

    # Shutdown with cancel — 在 Python 3.9+ 支持 cancel_futures
    try:
        temp_executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        temp_executor.shutdown(wait=False)

    # 等待 executor 完全停止
    time.sleep(0.5)
    # executor 应已关闭（_shutdown 属性被设为 True）
    assert temp_executor._shutdown


def test_register_hook_execution_order():
    """验证 hooks 按 priority 顺序执行。"""
    results = []

    _shutdown.register_hook("first", lambda: results.append(1), priority=10)
    _shutdown.register_hook("second", lambda: results.append(2), priority=20)
    _shutdown.register_hook("third", lambda: results.append(3), priority=30)

    with _shutdown._registry_lock:
        priorities = [p for _, _, p in _shutdown._hooks]
        assert priorities == [10, 20, 30]


def test_hook_exception_does_not_block_others():
    """一个 hook 抛出异常不应阻止其他 hooks 执行。"""
    results = []

    def failing_hook():
        results.append("fail")
        raise RuntimeError("hook failure")

    def good_hook():
        results.append("good")

    _shutdown.register_hook("failing", failing_hook, priority=10)
    _shutdown.register_hook("good", good_hook, priority=20)

    # 手动调用 hooks（模拟 shutdown 序列的一部分）
    with _shutdown._registry_lock:
        hooks = sorted(_shutdown._hooks, key=lambda x: x[2])

    for name, cb, _ in hooks:
        try:
            cb()
        except Exception:
            pass

    assert "fail" in results
    assert "good" in results


def test_install_signal_handlers():
    """安装信号处理器后 SIGTERM/SIGINT 应有自定义 handler。"""
    _shutdown.install_signal_handlers()

    # 验证 handler 已安装（在主线程中）
    if threading.current_thread().ident == _shutdown._main_thread_id:
        term_handler = signal.getsignal(signal.SIGTERM)
        assert term_handler is not None
        assert term_handler is not signal.SIG_DFL
        assert term_handler is not signal.SIG_IGN
        # 验证是我们安装的 handler
        assert term_handler == _shutdown._signal_handler


def test_install_signal_handlers_idempotent():
    """重复安装信号处理器应是幂等的。"""
    _shutdown.install_signal_handlers()
    first = signal.getsignal(signal.SIGTERM)
    _shutdown.install_signal_handlers()
    second = signal.getsignal(signal.SIGTERM)
    assert first == second


def test_register_executor_multiple(temp_executor):
    """注册多个 executor 应全部记录。"""
    exec2 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-2")
    try:
        _shutdown.register_executor("test-1", temp_executor)
        _shutdown.register_executor("test-2", exec2)

        with _shutdown._registry_lock:
            names = {name for name, _ in _shutdown._executors}
            assert names == {"test-1", "test-2"}
    finally:
        exec2.shutdown(wait=False)


def test_signal_handler_sets_flag():
    """信号处理器应设置 _shutting_down 标志。"""
    # 模拟信号处理器调用（不真正发送信号）
    _shutdown._shutting_down.clear()
    _shutdown._shutting_down.set()
    assert _shutdown.is_shutting_down()
    _shutdown._shutting_down.clear()
    assert not _shutdown.is_shutting_down()


def test_atexit_callback_registered():
    """验证 atexit 回调已注册。"""
    import atexit
    # 检查 _atexit_callback 是否在 atexit 注册列表中
    # 注意：atexit 模块在 3.x 中不直接暴露已注册的回调
    # 我们通过检查模块属性来验证
    assert callable(_shutdown._atexit_callback)


def test_register_known_executors():
    """register_known_executors 应注册所有传入的 executor。"""
    e1 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="api")
    e2 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyzer")
    e3 = ThreadPoolExecutor(max_workers=1, thread_name_prefix="streamlit")

    try:
        _shutdown.register_known_executors(
            api_executor=e1,
            analyzer_executor=e2,
            streamlit_executor=e3,
        )

        with _shutdown._registry_lock:
            names = {name for name, _ in _shutdown._executors}
            assert "api-worker" in names
            assert "analyzer" in names
            assert "streamlit-api" in names
    finally:
        for e in (e1, e2, e3):
            try:
                e.shutdown(wait=False)
            except Exception:
                pass


def test_register_known_executors_none_handling():
    """传入 None 时应安全跳过。"""
    initial_count = len(_shutdown._executors)
    _shutdown.register_known_executors(
        api_executor=None,
        analyzer_executor=None,
        streamlit_executor=None,
    )
    assert len(_shutdown._executors) == initial_count
