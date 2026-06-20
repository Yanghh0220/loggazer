# shutdown.py — Graceful Shutdown & Signal Handling
#
# P0 CRITICAL FIX (FIX-001)
#
# What: 全局优雅关闭模块，管理 ThreadPoolExecutor 生命周期
# Why:
#   - analyzer.py 的 _ANALYZER_EXECUTOR、app.py 的 _API_EXECUTOR、
#     api/main.py 的 _executor 从未被正确关闭
#   - 没有 SIGTERM/SIGINT 处理器，进程终止时 in-flight AI 分析任务
#     和 Qdrant/SQLite 写入操作会被强制中断，导致数据损坏
# Impact:
#   - 新增模块，零破坏性 — 仅在被调用时生效
#   - api/main.py 需从 @app.on_event 迁移到 lifespan 模式
#   - app.py 需注册 _API_EXECUTOR 并在 atexit 中触发关闭
# How to verify:
#   - 运行 tests/test_shutdown.py
#   - 手动：启动服务 → kill -SIGTERM <pid> → 检查日志中 shutdown 序列

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

logger = logging.getLogger("shutdown")

# ============================================================
# Configuration (environment-overridable)
# ============================================================

_SHUTDOWN_TIMEOUT_SECONDS: float = float(
    os.getenv("LOGGAZER_SHUTDOWN_TIMEOUT", "30")
)
_SHUTDOWN_POLL_INTERVAL: float = 0.1

# ============================================================
# Global registry
# ============================================================

_registry_lock = threading.Lock()
_executors: list[tuple[str, ThreadPoolExecutor]] = []
_hooks: list[tuple[str, Callable[[], None], int]] = []  # (name, callback, priority)
_shutting_down = threading.Event()
_shutdown_started = False

# Track the main thread so we don't install signal handlers in worker threads
_main_thread_id = threading.main_thread().ident


def is_shutting_down() -> bool:
    """Check if shutdown has been initiated. Threads can poll this."""
    return _shutting_down.is_set()


# ============================================================
# Executor registration
# ============================================================

def register_executor(name: str, executor: ThreadPoolExecutor) -> None:
    """
    Register a ThreadPoolExecutor for graceful shutdown.

    [CONFIRMED from analyzer.py:49-51] _ANALYZER_EXECUTOR
    [CONFIRMED from app.py:33] _API_EXECUTOR
    [CONFIRMED from api/main.py:76] _executor

    Args:
        name: Human-readable name for logging.
        executor: The ThreadPoolExecutor instance to manage.
    """
    with _registry_lock:
        # Deduplicate by object identity
        for _, existing in _executors:
            if existing is executor:
                logger.debug("Executor %r already registered as %r", executor, name)
                return
        _executors.append((name, executor))
    logger.info("Executor registered: %s (thread_prefix=%s)", name, executor._thread_name_prefix)


def register_hook(name: str, callback: Callable[[], None], priority: int = 100) -> None:
    """
    Register a shutdown hook callback.

    Hooks are called in priority order (lower = earlier).
    Built-in priorities:
      10 — stop accepting new work (set _shutting_down flag)
      20 — close executor pools
      50 — flush caches / close DB connections
      90 — final cleanup
      100 — default user hooks

    Args:
        name: Human-readable name for logging.
        callback: Zero-argument callable invoked during shutdown.
        priority: Execution order (lower runs first).
    """
    with _registry_lock:
        _hooks.append((name, callback, priority))
        _hooks.sort(key=lambda x: x[2])
    logger.debug("Shutdown hook registered: %s (priority=%d)", name, priority)


# ============================================================
# Shutdown sequence
# ============================================================

def _run_shutdown_sequence(signame: str) -> None:
    """
    Ordered shutdown sequence.

    1. Set the shutdown flag (stop accepting new work)
    2. Run registered hooks in priority order
    3. Shut down registered executors (within timeout)
    4. Force-exit on timeout
    """
    global _shutdown_started
    with _registry_lock:
        if _shutdown_started:
            return  # Prevent double-shutdown
        _shutdown_started = True

    logger.info(
        "Shutdown sequence initiated (signal=%s, pid=%d, timeout=%.0fs)",
        signame, os.getpid(), _SHUTDOWN_TIMEOUT_SECONDS,
    )
    _shutting_down.set()

    start_time = time.time()

    # Phase 1: Run registered hooks
    with _registry_lock:
        hooks_sorted = sorted(_hooks, key=lambda x: x[2])  # lower priority first

    for name, callback, _pri in hooks_sorted:
        try:
            logger.debug("Running shutdown hook: %s", name)
            callback()
        except Exception:
            logger.exception("Shutdown hook %r raised an exception", name)

    # Phase 2: Shut down executors
    deadline = start_time + _SHUTDOWN_TIMEOUT_SECONDS

    with _registry_lock:
        executors_copy = list(_executors)

    for name, executor in executors_copy:
        remaining = deadline - time.time()
        if remaining <= 0:
            logger.warning("Shutdown timeout reached, skipping executor: %s", name)
            break

        logger.info("Shutting down executor: %s", name)
        # Step 2a: Stop accepting new tasks
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python <3.9 doesn't support cancel_futures
            executor.shutdown(wait=False)

        # Step 2b: Wait for in-flight tasks with timeout
        poll_deadline = time.time() + min(remaining, _SHUTDOWN_TIMEOUT_SECONDS)
        try:
            while time.time() < poll_deadline:
                # Check if executor is idle by submitting a no-op
                try:
                    f = executor.submit(lambda: None)
                    f.result(timeout=1.0)
                except Exception:
                    # Executor might be shutting down — check thread count
                    pass
                # _threads is not public API but provides best-effort insight
                active = getattr(executor, '_threads', set())
                alive = sum(1 for t in active if t.is_alive())
                if alive == 0:
                    logger.info("Executor %s idle, proceeding", name)
                    break
                time.sleep(_SHUTDOWN_POLL_INTERVAL)
            else:
                logger.warning(
                    "Executor %s did not drain within timeout (active_threads=%d)",
                    name, alive if 'alive' in dir() else -1,
                )
        except Exception:
            logger.exception("Error while waiting for executor %s to drain", name)

    elapsed = time.time() - start_time
    logger.info("Shutdown sequence complete (%.2fs)", elapsed)

    if elapsed > _SHUTDOWN_TIMEOUT_SECONDS:
        logger.warning("Shutdown exceeded timeout — forcing exit")
        os._exit(1)

    # Normal exit — let the process terminate naturally
    # _exit(0) ensures we don't hang on non-daemon threads
    os._exit(0)


# ============================================================
# Signal handlers
# ============================================================

def _signal_handler(signum: int, frame) -> None:
    """Handle SIGTERM/SIGINT."""
    signame = signal.Signals(signum).name
    # Only the main thread should run the shutdown sequence
    if threading.current_thread().ident != _main_thread_id:
        logger.debug("Signal %s received on non-main thread, re-raising to main", signame)
        # Re-send to main thread
        signal.pthread_kill(threading.main_thread().ident, signum)
        return
    _run_shutdown_sequence(signame)


def install_signal_handlers() -> None:
    """
    Install SIGTERM and SIGINT handlers for graceful shutdown.

    Should be called from the main thread during application startup.
    Idempotent — multiple calls are safe.
    """
    # Only main thread should install handlers
    if threading.current_thread().ident != _main_thread_id:
        logger.debug("Skipping signal handler install on non-main thread")
        return

    for sig in (signal.SIGTERM, signal.SIGINT):
        prev = signal.getsignal(sig)
        if prev is not None and prev is not signal.SIG_DFL and prev is not signal.SIG_IGN:
            if prev == _signal_handler:
                logger.debug("Signal handler already installed for %s", sig.name)
                continue
            logger.debug(
                "Signal %s already has handler %r, replacing with shutdown handler",
                sig.name, prev,
            )
        try:
            signal.signal(sig, _signal_handler)
            logger.info("Installed %s handler for graceful shutdown", sig.name)
        except ValueError:
            logger.warning("Cannot install %s handler (not in main thread?)", sig.name)


# ============================================================
# atexit fallback (covers non-signal exits)
# ============================================================

def _atexit_callback() -> None:
    """Fallback shutdown trigger for normal process exit."""
    if not _shutting_down.is_set() and not _shutdown_started:
        logger.debug("atexit triggered — initiating shutdown")
        _run_shutdown_sequence("atexit")


atexit.register(_atexit_callback)


# ============================================================
# Convenience: register all known executors in one call
# ============================================================

def register_known_executors(
    api_executor: Optional[ThreadPoolExecutor] = None,
    analyzer_executor: Optional[ThreadPoolExecutor] = None,
    streamlit_executor: Optional[ThreadPoolExecutor] = None,
) -> None:
    """
    Register all known application executors.

    [CONFIRMED from api/main.py:76] _executor → api_executor
    [CONFIRMED from analyzer.py:49-51] _ANALYZER_EXECUTOR → analyzer_executor
    [CONFIRMED from app.py:33] _API_EXECUTOR → streamlit_executor

    Args:
        api_executor: api/main.py _executor
        analyzer_executor: analyzer.py _ANALYZER_EXECUTOR
        streamlit_executor: app.py _API_EXECUTOR
    """
    if api_executor is not None:
        register_executor("api-worker", api_executor)
    if analyzer_executor is not None:
        register_executor("analyzer", analyzer_executor)
    if streamlit_executor is not None:
        register_executor("streamlit-api", streamlit_executor)


# ============================================================
# Integration guide for api/main.py
# ============================================================
#
# Replace the current @app.on_event("startup") with a lifespan:
#
#   from contextlib import asynccontextmanager
#   import shutdown as _shutdown
#
#   @asynccontextmanager
#   async def lifespan(app: FastAPI):
#       # Startup
#       _shutdown.install_signal_handlers()
#       _shutdown.register_executor("api-worker", _executor)
#       from analyzer import _ANALYZER_EXECUTOR
#       _shutdown.register_executor("analyzer", _ANALYZER_EXECUTOR)
#       # ... warmup ...
#       yield
#       # Shutdown (triggered when server stops)
#       _shutdown._run_shutdown_sequence("server-shutdown")
#
#   app = FastAPI(lifespan=lifespan, ...)
#
# ============================================================
#
# Integration guide for app.py (Streamlit):
#
#   import shutdown as _shutdown
#
#   # Near the top, after creating _API_EXECUTOR:
#   _shutdown.register_executor("streamlit-api", _API_EXECUTOR)
#   _shutdown.install_signal_handlers()
#
# ============================================================
