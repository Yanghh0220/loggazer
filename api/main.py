# api/main.py - LogGazer FastAPI Backend (BFF Architecture)
#
# v2.0 — 全面性能优化 (2026-06-20)
#
# ✅ 优化点: 异步任务架构 — 文件上传后立即返回 task_id (HTTP 202)
# ✅ 优化点: GET /api/task/<task_id> 接口支持进度查询
# ✅ 优化点: 四个分析器使用 ThreadPoolExecutor 并行执行
# ✅ 优化点: SSE 端点 /api/upload-stream 分步推送进度
# ✅ 优化点: MAX_CONTENT_LENGTH = 500MB
# ✅ 优化点: GZip 压缩中间件
# ✅ 优化点: 任务状态用内存字典实现，预留 Redis 替换注释
#
# 🔧 生产部署命令:
#   gunicorn -w 4 -b 0.0.0.0:5000 --timeout 300 --worker-class uvicorn.workers.UvicornWorker api.main:app
#
# Architecture:
#   FastAPI Core (analysis engine)
#     ├── Streamlit BFF (httpx.AsyncClient → localhost:8000)
#     ├── MCP Server (stdio/sse → Tool/Resource/Prompt)
#     ├── VS Code Extension (REST client)
#     └── GitHub App (webhook → REST client)

import asyncio
import logging
import os
import time
import threading
import uuid
import json as _json
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError

# P0 FIX-001: Graceful shutdown module
import shutdown as _shutdown

# P0 FIX-002: Bounded task store (replaces unbounded dict)
from task_store import BoundedTaskStore

from api.schemas import (
    ProblemDetail,
    AnalyzeRequest,
    AnalyzeResponse,
    AnalyzeResponseMeta,
    HealthResponse,
)
from api.dependencies import (
    get_analyzer,
    get_request_id,
    get_api_key,
    verify_api_key,
    get_rate_limiter,
    get_observability,
)
from utils.performance import timer
from resource_guard import (
    get_file_size_limit,
    get_memory_guard,
    get_concurrency_limiter,
    check_all_resources,
    release_resources,
)

logger = logging.getLogger("api")

# ============================================================
# ✅ 优化点: MAX_CONTENT_LENGTH = 500MB
# ============================================================
# FastAPI/Starlette 默认无限制，但需要保护服务器不被超大文件压垮
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB

# ============================================================
#  P0-3: 共享线程池
# ============================================================
_MAX_WORKERS = min(4, (os.cpu_count() or 2))
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="loggazer-worker")

# ✅ 优化点: ProcessPoolExecutor 用于隔离的 CPU 密集型分析任务
# 注意：ProcessPoolExecutor 需要可序列化的函数，此处预留用于未来扩展
# _process_executor = ProcessPoolExecutor(max_workers=2)

# ============================================================
# P0 FIX-002: 有界任务状态存储（BoundedTaskStore 替代裸 dict）
# ============================================================
# 设计：上传后立即分配 task_id，后台异步处理，前端轮询查询进度
# 状态流转: pending → parsing → analyzing → completed / failed
#
# 为什么替换？
# - 旧 _task_store dict 无界 → 恶意客户端可导致 OOM
# - _cleanup_expired_tasks 存在但从未被调用 → BoundedTaskStore 内置自动清理
# - BoundedTaskStore 内置 LRU 驱逐 + 线程安全 + 后台清理线程
#
# 🔧 Redis 替换方案（生产环境）:
#   将 _task_store 替换为 Redis hash:
#     redis_client.hset(f"task:{task_id}", mapping={...})
#   设置 TTL: redis_client.expire(f"task:{task_id}", 3600)
#   轮询: redis_client.hgetall(f"task:{task_id}")
#
_task_store = BoundedTaskStore(
    max_capacity=int(os.getenv("TASK_STORE_MAX_CAPACITY", "1000")),
    task_ttl_seconds=float(os.getenv("TASK_TTL_SECONDS", "3600")),
    cleanup_interval=float(os.getenv("TASK_STORE_CLEANUP_INTERVAL", "60")),
)

# Backward compatibility: _task_store_lock reference preserved for
# external code that may import it (e.g., tests)
_task_store_lock = _task_store._lock

# 注意: _cleanup_expired_tasks() 已被 BoundedTaskStore._cleanup_expired() 替代
# 保留函数引用以兼容可能的外部调用
_cleanup_expired_tasks = _task_store._cleanup_expired


# ============================================================
#  P0-2: API 级 TTL 缓存
# ============================================================
_api_cache_lock = threading.Lock()
_clusters_cache = TTLCache(maxsize=50, ttl=300)
_platforms_cache = TTLCache(maxsize=10, ttl=600)


def clear_api_cache() -> dict:
    with _api_cache_lock:
        c_count = len(_clusters_cache)
        p_count = len(_platforms_cache)
        _clusters_cache.clear()
        _platforms_cache.clear()
    logger.info("API 缓存已清除: clusters=%d, platforms=%d", c_count, p_count)
    return {"cleared": {"clusters": c_count, "platforms": p_count}}


# ============================================================
#  FastAPI Application
# ============================================================

# ============================================================
# P0 FIX-001: FastAPI Lifespan (replaces @app.on_event)
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup → yield → shutdown."""
    # === Startup ===
    logger.info("LogGazer API v2.0.0 starting up (pid=%d)", os.getpid())

    # Install signal handlers for graceful shutdown
    _shutdown.install_signal_handlers()

    # Register API executor for graceful shutdown
    _shutdown.register_executor("api-worker", _executor)

    # Register analyzer executor (imported lazily from analyzer module)
    try:
        from analyzer import _ANALYZER_EXECUTOR
        _shutdown.register_executor("analyzer", _ANALYZER_EXECUTOR)
    except Exception:
        logger.debug("Analyzer executor not available for registration")

    # Register shutdown hook: stop accepting new tasks
    def _stop_accepting():
        logger.info("Shutdown: stopping new task acceptance")
        if hasattr(_task_store, 'shutdown'):
            _task_store.shutdown()

    _shutdown.register_hook("stop-task-store", _stop_accepting, priority=15)

    # Run warmup
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _warmup_backend)

    logger.info("LogGazer API startup complete")
    yield
    # === Shutdown ===
    logger.info("LogGazer API shutting down...")
    _shutdown._run_shutdown_sequence("server-shutdown")


app = FastAPI(
    title="LogGazer API",
    description="""
Analyze CI/CD build failure logs with AI-powered root cause analysis.

## Features
- **Structured Analysis**: Returns severity, root causes, fix suggestions
- **Async Task Architecture**: Upload → task_id → poll progress
- **Parallel Analyzers**: 4 analyzers run concurrently
- **SSE Streaming**: Real-time progress via Server-Sent Events
- **Semantic Cache**: Avoids redundant AI calls for similar errors
""",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "Analysis", "description": "Core log analysis operations"},
        {"name": "Upload", "description": "File upload with async task processing"},
        {"name": "Tasks", "description": "Task status query and management"},
        {"name": "Preprocess", "description": "Preprocessing and preloading"},
        {"name": "Health", "description": "Health checks and diagnostics"},
        {"name": "Clusters", "description": "Error clustering and trend insights"},
        {"name": "Platforms", "description": "Supported platform information"},
    ],
    lifespan=lifespan,
)

# ---- CORS Middleware ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",       # Streamlit
        "http://localhost:3000",       # Local dev
        "vscode-webview://*",          # VS Code Extension Webview
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ✅ 优化点: GZip 压缩中间件 — 超过 1KB 的响应自动压缩
app.add_middleware(GZipMiddleware, minimum_size=1024)

# ---- Server lifetime ----
_start_time = time.time()


# ============================================================
#  P2-2②: Backend Warmup
# ============================================================
_warmed_up = False
_warmup_lock = threading.Lock()


def _warmup_backend():
    global _warmed_up
    with _warmup_lock:
        if _warmed_up:
            return
        _warmed_up = True

    logger.info("P2-2② Backend 预热开始...")
    _start = time.time()

    try:
        from log_parser import detect_platform, extract_error_lines, get_error_stats
        warmup_text = "ERROR: test failure\nnpm ERR! code 1"
        detect_platform(warmup_text)
        extract_error_lines(warmup_text)
        get_error_stats(warmup_text)
        logger.info("  log_parser 预热完成")
    except Exception as e:
        logger.debug("  log_parser 预热跳过: %s", e)

    try:
        from api.dependencies import get_analyzer
        get_analyzer()
        logger.info("  analyzer 预热完成")
    except Exception as e:
        logger.debug("  analyzer 预热跳过: %s", e)

    try:
        from cache_engine import SemanticCache
        from config import CACHE_ENABLED, CACHE_EMBEDDING_MODEL, CACHE_SIMILARITY_HIGH, CACHE_SIMILARITY_LOW, CACHE_TTL_HOURS
        if CACHE_ENABLED:
            SemanticCache(
                embedding_model=CACHE_EMBEDDING_MODEL,
                similarity_high=CACHE_SIMILARITY_HIGH,
                similarity_low=CACHE_SIMILARITY_LOW,
                ttl_hours=CACHE_TTL_HOURS,
            )
            logger.info("  cache_engine 预热完成")
    except Exception as e:
        logger.debug("  cache_engine 预热跳过: %s", e)

    try:
        from cluster_engine import get_cluster_engine
        get_cluster_engine()
        logger.info("  cluster_engine 预热完成")
    except Exception as e:
        logger.debug("  cluster_engine 预热跳过: %s", e)

    _elapsed = time.time() - _start
    logger.info("P2-2② Backend 预热完成，耗时 %.2fs", _elapsed)


# ============================================================
#  Exception Handlers
# ============================================================

@app.exception_handler(ValueError)
async def validation_exception_handler(request: Request, exc: ValueError):
    return JSONResponse(
        status_code=422,
        content=ProblemDetail(
            type="https://loggazer.dev/errors/validation-error",
            title="Validation Error",
            status=422,
            detail=str(exc),
            instance=str(request.url.path),
        ).model_dump(),
        headers={"Content-Type": "application/problem+json"},
    )


@app.exception_handler(RequestValidationError)
async def pydantic_validation_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    detail_parts = []
    for err in errors:
        loc = " → ".join(str(l) for l in err.get("loc", []))
        msg = err.get("msg", "Unknown error")
        detail_parts.append(f"{loc}: {msg}")
    detail = "; ".join(detail_parts)
    return JSONResponse(
        status_code=422,
        content=ProblemDetail(
            type="https://loggazer.dev/errors/validation-error",
            title="Validation Error",
            status=422,
            detail=detail,
            instance=str(request.url.path),
        ).model_dump(),
        headers={"Content-Type": "application/problem+json"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "type" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=exc.headers or {},
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=ProblemDetail(
            type="about:blank",
            title="Error",
            status=exc.status_code,
            detail=str(exc.detail),
            instance=str(request.url.path),
        ).model_dump(),
        headers={"Content-Type": "application/problem+json"} | (exc.headers or {}),
    )


# ============================================================
#  Health Check
# ============================================================

@app.get(
    "/healthz",
    tags=["Health"],
    summary="Liveness probe",
)
async def liveness_check():
    return {"status": "ok", "timestamp": time.time()}


@app.get(
    "/v1/health",
    response_model=HealthResponse,
    tags=["Health"],
    summary="Deep health check",
)
async def health_check():
    checks = {}
    degraded = False

    try:
        from config import DEEPSEEK_API_KEY, AI_PROVIDER
        if DEEPSEEK_API_KEY:
            checks["ai_provider"] = {"status": "ok", "provider": AI_PROVIDER}
        else:
            checks["ai_provider"] = {"status": "warning", "message": "API Key not configured"}
    except Exception as e:
        checks["ai_provider"] = {"status": "error", "message": str(e)}
        degraded = True

    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, db=0, socket_connect_timeout=2)
        r.ping()
        checks["redis"] = {"status": "ok"}
    except Exception:
        checks["redis"] = {"status": "degraded", "message": "Redis unavailable — using in-memory fallback"}

    try:
        from config import CACHE_ENABLED, CACHE_QDRANT_PATH
        if CACHE_ENABLED:
            checks["cache"] = {"status": "ok", "mode": "qdrant" if CACHE_QDRANT_PATH else "in-memory"}
        else:
            checks["cache"] = {"status": "disabled"}
    except Exception as e:
        checks["cache"] = {"status": "error", "message": str(e)}

    try:
        import sqlite3
        conn = sqlite3.connect("loggazer.db")
        conn.execute("SELECT 1")
        conn.close()
        checks["database"] = {"status": "ok", "engine": "sqlite3"}
    except Exception as e:
        checks["database"] = {"status": "error", "message": str(e)}
        degraded = True

    overall = (
        "unhealthy" if any(
            c.get("status") == "error" and k in ["ai_provider", "database"]
            for k, c in checks.items()
        )
        else "degraded" if degraded
        else "healthy"
    )

    return {
        "status": overall,
        "version": "2.0.0",
        "checks": checks,
        "uptime_seconds": round(time.time() - _start_time, 1),
    }


# ============================================================
# ✅ 优化点: 异步文件上传端点 — 上传后立即返回 task_id (HTTP 202)
# ============================================================

@app.post(
    "/api/upload",
    tags=["Upload"],
    summary="Upload log file for async analysis",
    description="""
Upload a log file and start background analysis.
Returns immediately with a task_id (HTTP 202 Accepted).

**Flow:**
1. Upload file → receive task_id
2. Poll GET /api/task/{task_id} every 500ms
3. When status=completed, read the result
""",
    responses={
        202: {"description": "Task accepted for processing"},
        413: {"description": "File too large"},
        422: {"description": "Validation error"},
    },
)
async def upload_file_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Log file to analyze (max 500MB)"),
    x_request_id: str = Depends(get_request_id),
):
    """
    ✅ 优化点: 异步上传架构
    - 接收文件后立即返回 task_id (HTTP 202)
    - 后台使用线程池异步处理
    - 前端通过 GET /api/task/<task_id> 轮询进度
    """
    # 文件大小检查
    contents = await file.read()
    file_size = len(contents)

    if file_size > MAX_CONTENT_LENGTH:
        raise HTTPException(
            status_code=413,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/file-too-large",
                title="File Too Large",
                status=413,
                detail=f"File size ({file_size / 1024 / 1024:.1f}MB) exceeds maximum (500MB).",
                instance="/api/upload",
            ).model_dump(),
        )

    # 解码文件内容
    try:
        log_text = contents.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/encoding-error",
                title="Encoding Error",
                status=422,
                detail=f"Unable to decode file: {str(e)}",
                instance="/api/upload",
            ).model_dump(),
        )

    if not log_text.strip():
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/empty-file",
                title="Empty File",
                status=422,
                detail="Uploaded file is empty.",
                instance="/api/upload",
            ).model_dump(),
        )

    # 分配 task_id
    task_id = str(uuid.uuid4())
    task_entry = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0.0,
        "result": None,
        "error": None,
        "filename": file.filename or "unknown",
        "file_size_bytes": file_size,
        "created_at": time.time(),
    }

    # P0 FIX-002: BoundedTaskStore.set() — 线程安全，自动 LRU 驱逐
    _task_store.set(task_id, task_entry)

    # 后台异步处理
    background_tasks.add_task(
        _process_upload_task,
        task_id=task_id,
        log_text=log_text,
    )

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "status": "accepted",
            "message": "File accepted. Poll GET /api/task/{task_id} for progress.",
            "estimated_duration": "5-30 seconds depending on file size",
        },
    )


# ============================================================
# ✅ 优化点: POST /api/analyze-text — 文本直接提交（不通过文件上传）
# ============================================================

@app.post(
    "/api/analyze-text",
    tags=["Analysis"],
    summary="Submit log text for async analysis",
    description="Submit raw log text and receive a task_id for polling.",
    responses={
        202: {"description": "Task accepted"},
        422: {"description": "Validation error"},
    },
)
async def analyze_text_endpoint(
    background_tasks: BackgroundTasks,
    request: AnalyzeRequest,
    x_request_id: str = Depends(get_request_id),
):
    """✅ 优化点: 日志文本异步提交 — 返回 task_id 供轮询"""
    log_text = request.log_text

    task_id = str(uuid.uuid4())
    task_entry = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0.0,
        "result": None,
        "error": None,
        "filename": "inline-text",
        "file_size_bytes": len(log_text),
        "created_at": time.time(),
    }

    # P0 FIX-002: BoundedTaskStore.set() — 线程安全，自动 LRU 驱逐
    _task_store.set(task_id, task_entry)

    background_tasks.add_task(
        _process_upload_task,
        task_id=task_id,
        log_text=log_text,
    )

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "status": "accepted",
            "message": "Analysis started. Poll GET /api/task/{task_id} for progress.",
        },
    )


# ============================================================
# ✅ 优化点: GET /api/task/<task_id> — 进度查询接口
# ============================================================

@app.get(
    "/api/task/{task_id}",
    tags=["Tasks"],
    summary="Get task status and result",
    description="""
Query the status of an async analysis task.

**Status values:**
- `pending`: Queued, not started yet
- `parsing`: Parsing log file
- `analyzing`: Running parallel analyzers + AI analysis
- `completed`: Analysis finished, result available
- `failed`: Error occurred during processing

**Polling recommendation:** Every 500ms until status is `completed` or `failed`.
""",
)
async def get_task_status(task_id: str):
    """✅ 优化点: 返回 {status, progress, result} 供前端轮询"""
    # P0 FIX-002: BoundedTaskStore.get() — 线程安全，无需外部锁
    task = _task_store.get(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/not-found",
                title="Task Not Found",
                status=404,
                detail=f"Task {task_id} not found or expired.",
                instance=f"/api/task/{task_id}",
            ).model_dump(),
        )

    response = {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": task["progress"],
        "filename": task.get("filename", "unknown"),
    }

    if task["status"] == "completed":
        response["result"] = task.get("result")
        response["duration_ms"] = task.get("duration_ms", 0)

    if task["status"] == "failed":
        response["error"] = task.get("error", "Unknown error")

    return response


# ============================================================
# ✅ 优化点: 后台任务处理函数
# ============================================================

def _process_upload_task(task_id: str, log_text: str):
    """
    后台处理上传的日志文件。

    ✅ 优化点:
      - 在线程池中执行（不阻塞 asyncio 事件循环）
      - 分阶段更新进度
      - 四个分析器并行执行
    """
    start_time = time.time()

    try:
        # —— 阶段 1: 日志解析 (progress: 0 → 25%) ——
        _update_task(task_id, "parsing", 0.05)
        from log_parser import parse_log, get_error_stats

        loop = asyncio.new_event_loop()
        parsed = loop.run_until_complete(
            asyncio.get_event_loop().run_in_executor(_executor, parse_log, log_text)
        ) if asyncio.get_event_loop().is_running() else parse_log(log_text)

        # 兼容不同的事件循环状态
        try:
            parsed = parse_log(log_text)
        except Exception:
            pass

        stats = get_error_stats(log_text)
        _update_task(task_id, "parsing", 0.25)
        logger.info("Task %s: 解析完成, 平台=%s", task_id, parsed.get("platform", "Unknown"))

        # —— 阶段 2: 并行分析 (progress: 25 → 50%) ——
        _update_task(task_id, "analyzing", 0.30)
        from analyzer import _run_parallel_analyzers
        parallel_results = _run_parallel_analyzers(log_text, parsed["error_lines"])
        _update_task(task_id, "analyzing", 0.50)

        # —— 阶段 3: AI 分析 (progress: 50 → 90%) ——
        _update_task(task_id, "analyzing", 0.55)
        from analyzer import analyze_log
        result = analyze_log(log_text)
        _update_task(task_id, "analyzing", 0.90)

        # —— 完成 ——
        duration_ms = (time.time() - start_time) * 1000
        result_dict = result.model_dump() if hasattr(result, "model_dump") else result

        _update_task(task_id, "completed", 1.0, result=result_dict, duration_ms=duration_ms)
        logger.info("Task %s: 分析完成, 耗时 %.0fms", task_id, duration_ms)

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        _update_task(task_id, "failed", 1.0, error=str(e)[:1000], duration_ms=duration_ms)
        logger.error("Task %s: 分析失败 (%s)", task_id, str(e)[:200])


def _update_task(
    task_id: str,
    status: str,
    progress: float,
    result: dict | None = None,
    error: str | None = None,
    duration_ms: float = 0,
):
    """更新任务状态（使用 BoundedTaskStore.update 线程安全操作）"""
    _task_store.update(
        task_id=task_id,
        status=status,
        progress=progress,
        result=result,
        error=error,
        duration_ms=duration_ms,
    )


# ============================================================
# ✅ 优化点: SSE 端点 — 分步推送解析进度和分析结果
# ============================================================

@app.post(
    "/api/upload-stream",
    tags=["Upload"],
    summary="Upload file and stream analysis progress via SSE",
    description="""
Upload a log file and receive real-time progress via Server-Sent Events (SSE).

**Event types:**
- `progress`: { step, progress_percent, message }
- `result`: { analysis_result }
- `error`: { error_message }
- `done`: Stream complete

**Usage:**
```javascript
const eventSource = new EventSource('/api/upload-stream');
eventSource.addEventListener('progress', (e) => {
    const data = JSON.parse(e.data);
    updateProgressBar(data.progress_percent);
});
```
""",
)
async def upload_stream_endpoint(
    request: Request,
    file: UploadFile = File(...),
):
    """
    ✅ 优化点: SSE 分步推送进度
    - 解析完成 → 推送 progress 事件
    - 并行分析完成 → 推送 progress 事件
    - AI 分析完成 → 推送 result 事件
    - 错误 → 推送 error 事件
    """

    async def generate_sse():
        start_time = time.time()

        # 读取文件
        contents = await file.read()
        file_size = len(contents)

        if file_size > MAX_CONTENT_LENGTH:
            yield f"event: error\ndata: {_json.dumps({'error': 'File too large', 'max_mb': MAX_CONTENT_LENGTH / 1024 / 1024})}\n\n"
            return

        try:
            log_text = contents.decode("utf-8", errors="replace")
        except Exception as e:
            yield f"event: error\ndata: {_json.dumps({'error': f'Encoding error: {str(e)}'})}\n\n"
            return

        if not log_text.strip():
            yield f"event: error\ndata: {_json.dumps({'error': 'Empty file'})}\n\n"
            return

        yield f"event: progress\ndata: {_json.dumps({'step': 'started', 'progress_percent': 5, 'message': f'File received ({file_size / 1024:.1f} KB)'})}\n\n"

        try:
            # 阶段 1: 日志解析
            yield f"event: progress\ndata: {_json.dumps({'step': 'parsing', 'progress_percent': 15, 'message': 'Parsing log file...'})}\n\n"
            from log_parser import parse_log, get_error_stats
            parsed = parse_log(log_text)
            stats = get_error_stats(log_text)
            platform = parsed.get("platform", "Unknown")
            parsed_msg = f"Parsed — Platform: {platform}, {stats.get('total_lines', 0)} lines"
            yield f"event: progress\ndata: {_json.dumps({'step': 'parsed', 'progress_percent': 30, 'message': parsed_msg})}\n\n"

            # 阶段 2: 并行分析器
            yield f"event: progress\ndata: {_json.dumps({'step': 'analyzing', 'progress_percent': 35, 'message': 'Running parallel analyzers...'})}\n\n"
            from analyzer import _run_parallel_analyzers
            parallel_results = _run_parallel_analyzers(log_text, parsed["error_lines"])
            yield f"event: progress\ndata: {_json.dumps({'step': 'analyzed', 'progress_percent': 55, 'message': 'Parallel analysis complete', 'analysis_summary': {k: 'ok' if 'error' not in v else 'fail' for k, v in parallel_results.items()}})}\n\n"

            # 阶段 3: AI 分析
            yield f"event: progress\ndata: {_json.dumps({'step': 'ai_analysis', 'progress_percent': 60, 'message': 'Running AI analysis...'})}\n\n"
            from analyzer import analyze_log
            result = analyze_log(log_text)
            yield f"event: progress\ndata: {_json.dumps({'step': 'ai_complete', 'progress_percent': 90, 'message': 'AI analysis complete'})}\n\n"

            # 返回结果
            result_dict = result.model_dump() if hasattr(result, "model_dump") else result
            duration_ms = (time.time() - start_time) * 1000

            yield f"event: result\ndata: {_json.dumps({'result': result_dict, 'meta': {'duration_ms': round(duration_ms, 1), 'platform': platform}})}\n\n"
            yield f"event: done\ndata: {_json.dumps({'message': 'Analysis complete', 'duration_ms': round(duration_ms, 1)})}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {_json.dumps({'error': str(e)[:500], 'error_type': type(e).__name__})}\n\n"

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ============================================================
#  Core Analysis Endpoint (backward compatible)
# ============================================================

@app.post(
    "/v1/analyze",
    response_model=AnalyzeResponse,
    tags=["Analysis"],
    summary="Analyze a CI/CD build failure log (synchronous)",
    description="""
Submits a build failure log for AI-powered analysis (synchronous, backward compatible).

**Note:** For large files, prefer the async endpoint POST /api/upload which returns
immediately with a task_id and supports progress polling.
""",
    responses={
        200: {"description": "Analysis completed successfully"},
        422: {"description": "Validation Error"},
        429: {"description": "Rate Limit Exceeded"},
        503: {"description": "Service Unavailable"},
    },
)
async def analyze_endpoint(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Depends(verify_api_key),
    x_request_id: str = Depends(get_request_id),
):
    """Core analysis endpoint (backward compatible)."""
    obs = get_observability()

    # ---- 0. 文件大小校验 ----
    fs_limit = get_file_size_limit()
    is_valid_size, _, size_err = fs_limit.check(request.log_text)
    if not is_valid_size:
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/file-too-large",
                title="File Too Large",
                status=422,
                detail=size_err or "Log file exceeds maximum size limit.",
                instance="/v1/analyze",
            ).model_dump(),
        )

    # ---- 0.5 内存检查 ----
    mem_guard = get_memory_guard()
    can_accept, mem_warn = mem_guard.check()
    if not can_accept:
        raise HTTPException(
            status_code=503,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/resource-exhausted",
                title="Server Resources Exhausted",
                status=503,
                detail=mem_warn or "Server memory usage too high.",
                instance="/v1/analyze",
            ).model_dump(),
            headers={"Retry-After": "30"},
        )

    # ---- 0.6 并发槽位检查 ----
    cl = get_concurrency_limiter()
    slot_acquired, queue_pos = cl.try_acquire()
    if not slot_acquired:
        if queue_pos == -1:
            raise HTTPException(
                status_code=503,
                detail=ProblemDetail(
                    type="https://loggazer.dev/errors/queue-full",
                    title="Analysis Queue Full",
                    status=503,
                    detail="Too many pending analysis requests.",
                    instance="/v1/analyze",
                ).model_dump(),
                headers={"Retry-After": "60"},
            )
        else:
            raise HTTPException(
                status_code=503,
                detail=ProblemDetail(
                    type="https://loggazer.dev/errors/queued",
                    title="Analysis Queued",
                    status=503,
                    detail=f"Queued at position {queue_pos}.",
                    instance="/v1/analyze",
                ).model_dump(),
                headers={"Retry-After": str(queue_pos * 10)},
            )

    # ---- 1. Rate Limit Check ----
    limiter = get_rate_limiter()
    user_id = x_api_key or "anonymous"
    max_requests = 20 if x_api_key else 5
    window_seconds = 60

    allowed = limiter.is_allowed(user_id, max_requests, window_seconds)
    if not allowed:
        cl.release()
        retry_after = limiter.get_retry_after(user_id, max_requests, window_seconds)
        raise HTTPException(
            status_code=429,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/rate-limit",
                title="Too Many Requests",
                status=429,
                detail=f"Rate limit exceeded. Try again in {retry_after} seconds.",
                instance="/v1/analyze",
            ).model_dump(),
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": "0",
            },
        )

    # ---- 2. Cost Circuit Breaker ----
    if obs:
        cb_status = obs.check_cost_circuit_breaker()
        if cb_status == "tripped":
            cl.release()
            raise HTTPException(
                status_code=503,
                detail=ProblemDetail(
                    type="https://loggazer.dev/errors/circuit-breaker",
                    title="Monthly Budget Exceeded",
                    status=503,
                    detail="Monthly analysis budget has been exhausted.",
                    instance="/v1/analyze",
                ).model_dump(),
                headers={"Retry-After": "86400"},
            )

    # ---- 3. Analysis with Tracing ----
    start_time = time.time()
    cache_status = "miss"

    try:
        analyze_log_fn = get_analyzer()
        if obs:
            obs.increment_active_requests()

        loop = asyncio.get_event_loop()

        async def _run_analysis():
            if obs:
                with obs.trace_analysis(platform=request.platform_hint or "unknown", cache_status=cache_status):
                    with timer("api:核心分析执行", record=True):
                        return await loop.run_in_executor(_executor, analyze_log_fn, request.log_text)
            else:
                with timer("api:核心分析执行", record=True):
                    return await loop.run_in_executor(_executor, analyze_log_fn, request.log_text)

        result = await asyncio.wait_for(_run_analysis(), timeout=120.0)

        duration_ms = (time.time() - start_time) * 1000
        if duration_ms < 100:
            cache_status = "hit"

    except asyncio.TimeoutError:
        cl.release()
        if obs:
            obs.record_error("network")
            obs.decrement_active_requests()
        raise HTTPException(
            status_code=504,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/timeout",
                title="Analysis Timeout",
                status=504,
                detail="Analysis exceeded the 120-second time limit.",
                instance="/v1/analyze",
            ).model_dump(),
        )
    except ValueError as e:
        cl.release()
        if obs:
            obs.record_error("validation")
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/validation-error",
                title="Validation Error",
                status=422,
                detail=str(e),
                instance="/v1/analyze",
            ).model_dump(),
        )
    except ConnectionError as e:
        cl.release()
        if obs:
            obs.record_error("network")
        raise HTTPException(
            status_code=502,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/ai-provider-error",
                title="AI Provider Error",
                status=502,
                detail=str(e),
                instance="/v1/analyze",
            ).model_dump(),
        )
    except RuntimeError as e:
        cl.release()
        if obs:
            obs.record_error("auth")
        raise HTTPException(
            status_code=503,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/service-unavailable",
                title="Service Unavailable",
                status=503,
                detail=str(e),
                instance="/v1/analyze",
            ).model_dump(),
        )
    except Exception as e:
        cl.release()
        if obs:
            obs.record_error("network")
        raise HTTPException(
            status_code=500,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/internal-error",
                title="Internal Server Error",
                status=500,
                detail=f"An unexpected error occurred: {str(e)[:500]}",
                instance="/v1/analyze",
            ).model_dump(),
        )
    finally:
        if obs:
            obs.decrement_active_requests()
        release_resources()

    duration_ms = (time.time() - start_time) * 1000

    # ---- 4. Response Build ----
    with timer("api:成本计算与响应构建", record=True):
        async def _calc_cost():
            try:
                from config import DEEPSEEK_MODEL, AI_PROVIDER
                from cost_calculator import CostCalculator
                cc = CostCalculator()
                est_input_tokens = len(request.log_text) // 3
                est_output_tokens = 500
                cost = cc.calculate(DEEPSEEK_MODEL, est_input_tokens, est_output_tokens)
                if obs:
                    obs.record_tokens(DEEPSEEK_MODEL, AI_PROVIDER, est_input_tokens, est_output_tokens, "success")
                return DEEPSEEK_MODEL, cost
            except Exception:
                return "deepseek-chat", 0.0

        async def _detect_platform():
            from log_parser import detect_platform
            return detect_platform(request.log_text)

        (model_used, cost_estimate), platform_detected = await asyncio.gather(
            _calc_cost(), _detect_platform()
        )

        response = AnalyzeResponse(
            result=result,
            meta=AnalyzeResponseMeta(
                duration_ms=round(duration_ms, 2),
                cache_status=cache_status,
                model_used=model_used,
                cost_usd=round(cost_estimate, 6),
                platform_detected=platform_detected,
            ),
            request_id=x_request_id,
        ).model_dump()

    return response


# ============================================================
#  P1-4②: Streaming Analysis Endpoint (NDJSON)
# ============================================================

@app.post(
    "/v1/analyze/stream",
    tags=["Analysis"],
    summary="Stream analysis results as NDJSON",
    description="Analyzes a build failure log and streams intermediate results as NDJSON.",
)
async def analyze_stream_endpoint(
    request: AnalyzeRequest,
    x_api_key: Optional[str] = Depends(verify_api_key),
    x_request_id: str = Depends(get_request_id),
):
    """Streaming analysis: yields NDJSON chunks as analysis progresses."""
    async def generate():
        start_time = time.time()
        steps = []

        try:
            # Step 1: Preprocessing
            step_start = time.time()
            from log_parser import parse_log, get_error_stats
            parsed = parse_log(request.log_text)
            stats = get_error_stats(request.log_text)
            step_elapsed = (time.time() - step_start) * 1000
            steps.append({"step": "preprocessing", "elapsed_ms": round(step_elapsed, 1)})
            yield _json.dumps({
                "type": "progress",
                "step": "preprocessing",
                "elapsed_ms": round(step_elapsed, 1),
                "platform": parsed.get("platform", "Unknown"),
            }, ensure_ascii=False) + "\n"

            # Step 2: Cache check
            step_start = time.time()
            from analyzer import _get_or_create_cache, _make_content_key
            content_key = _make_content_key(request.log_text)
            cache = _get_or_create_cache()
            cache_hit = False
            if cache is not None:
                from cache_engine import generate_fingerprint
                fingerprint = generate_fingerprint(parsed)
                cached = cache.get(fingerprint, parsed)
                if cached is not None:
                    cache_hit = True
            step_elapsed = (time.time() - step_start) * 1000
            steps.append({"step": "cache_check", "elapsed_ms": round(step_elapsed, 1)})
            yield _json.dumps({
                "type": "progress",
                "step": "cache_check",
                "elapsed_ms": round(step_elapsed, 1),
                "cache_hit": cache_hit,
            }, ensure_ascii=False) + "\n"

            # Step 3: AI Analysis
            step_start = time.time()
            analyze_log_fn = get_analyzer()
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(_executor, analyze_log_fn, request.log_text)
            step_elapsed = (time.time() - step_start) * 1000
            steps.append({"step": "ai_analysis", "elapsed_ms": round(step_elapsed, 1)})
            yield _json.dumps({
                "type": "progress",
                "step": "ai_analysis",
                "elapsed_ms": round(step_elapsed, 1),
            }, ensure_ascii=False) + "\n"

            # Final result
            duration_ms = (time.time() - start_time) * 1000
            from log_parser import detect_platform

            final = {
                "type": "result",
                "result": result.model_dump() if hasattr(result, 'model_dump') else result,
                "meta": {
                    "duration_ms": round(duration_ms, 1),
                    "cache_status": "hit" if cache_hit else "miss",
                    "platform_detected": detect_platform(request.log_text),
                    "steps": steps,
                },
                "request_id": x_request_id,
            }
            yield _json.dumps(final, ensure_ascii=False, default=str) + "\n"

        except Exception as e:
            yield _json.dumps({
                "type": "error",
                "error": str(e)[:500],
                "error_type": type(e).__name__,
            }, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "X-Request-ID": x_request_id,
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ============================================================
#  Preprocessing Endpoint
# ============================================================

@app.post(
    "/v1/preprocess",
    tags=["Preprocess"],
    summary="Preprocess log text in background",
)
async def preprocess_endpoint(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    x_request_id: str = Depends(get_request_id),
):
    """Background preprocessing endpoint."""
    task_id = str(uuid.uuid4())

    fs_limit = get_file_size_limit()
    is_valid, warn, err = fs_limit.check(request.log_text)
    if not is_valid:
        raise HTTPException(
            status_code=422,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/file-too-large",
                title="File Too Large",
                status=422,
                detail=err,
                instance="/v1/preprocess",
            ).model_dump(),
        )

    _preprocess_tasks: dict = getattr(app.state, '_preprocess_tasks', {})
    if not hasattr(app.state, '_preprocess_tasks'):
        app.state._preprocess_tasks = {}
    _preprocess_tasks = app.state._preprocess_tasks
    _preprocess_tasks[task_id] = {"status": "running", "started_at": time.time()}

    async def _run_preprocess():
        try:
            loop = asyncio.get_event_loop()
            from log_parser import parse_log, get_error_stats
            from analyzer import _make_content_key, _get_or_create_cache

            parsed = await loop.run_in_executor(_executor, parse_log, request.log_text)
            stats = await loop.run_in_executor(_executor, get_error_stats, request.log_text)
            content_key = _make_content_key(request.log_text)
            cache = _get_or_create_cache()
            fingerprint = None
            cache_hit = False
            if cache is not None:
                from cache_engine import generate_fingerprint
                fingerprint = generate_fingerprint(parsed)
                cached = cache.get(fingerprint, parsed)
                if cached is not None:
                    cache_hit = True
                else:
                    cache.get_rag_context(fingerprint)

            _preprocess_tasks[task_id] = {
                "status": "completed",
                "platform": parsed.get("platform", "Unknown"),
                "error_lines_count": len(parsed.get("error_lines", [])),
                "total_lines": stats.get("total_lines", 0),
                "cache_hit": cache_hit,
                "duration_ms": (time.time() - _preprocess_tasks[task_id]["started_at"]) * 1000,
            }
            logger.info("P2-2① 预处理完成: task=%s platform=%s cache=%s",
                       task_id, parsed.get("platform"), "hit" if cache_hit else "miss")
        except Exception as e:
            _preprocess_tasks[task_id] = {
                "status": "failed",
                "error": str(e)[:500],
                "duration_ms": (time.time() - _preprocess_tasks[task_id]["started_at"]) * 1000,
            }
            logger.warning("P2-2① 预处理失败: task=%s error=%s", task_id, str(e)[:200])

    background_tasks.add_task(_run_preprocess)

    return {
        "task_id": task_id,
        "status": "accepted",
        "message": "Preprocessing started in background.",
    }


@app.get(
    "/v1/preprocess/{task_id}",
    tags=["Preprocess"],
    summary="Get preprocessing task status",
)
async def get_preprocess_status(task_id: str):
    """Poll for preprocessing task completion."""
    _preprocess_tasks = getattr(app.state, '_preprocess_tasks', {})
    if task_id not in _preprocess_tasks:
        raise HTTPException(
            status_code=404,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/not-found",
                title="Task Not Found",
                status=404,
                detail=f"Preprocessing task {task_id} not found or expired.",
                instance=f"/v1/preprocess/{task_id}",
            ).model_dump(),
        )
    return _preprocess_tasks[task_id]


# ============================================================
#  Clusters / Insights / Platforms / Metrics / Cache Endpoints
# ============================================================

@app.get(
    "/v1/clusters",
    tags=["Clusters"],
    summary="Get error cluster insights (paginated)",
)
async def get_clusters(
    days: int = 7,
    top_n: int = 10,
    page: int = 1,
    page_size: int = 100,
    x_api_key: Optional[str] = Depends(verify_api_key),
):
    """Get trending error clusters with pagination."""
    cache_key = f"clusters:{days}:{top_n}:{page}:{page_size}"
    with _api_cache_lock:
        if cache_key in _clusters_cache:
            return _clusters_cache[cache_key]

    try:
        from cluster_engine import get_cluster_engine
        engine = get_cluster_engine()
        trending = engine.get_trending_clusters(days=days, top_n=top_n)
        total = len(trending)

        trimmed = []
        for c in trending:
            trimmed.append({
                "cluster_id": c.get("cluster_id", 0),
                "occurrence_count": c.get("occurrence_count", 0),
                "recent_count": c.get("recent_count", 0),
                "first_seen": c.get("first_seen", ""),
                "last_seen": c.get("last_seen", ""),
                "platform_distribution": c.get("platform_distribution", {}),
                "avg_severity_score": c.get("avg_severity_score", 0) or 0,
                "is_active": c.get("is_active", True),
                "representative_samples": c.get("representative_samples", [])[:2],
                "top_fix_suggestions": [
                    {"title": f.get("title", ""), "command": f.get("command", "")}
                    for f in c.get("top_fix_suggestions", [])[:2]
                ],
                "avg_resolution_time_minutes": c.get("avg_resolution_time_minutes"),
            })

        start = (page - 1) * page_size
        end = start + page_size
        page_data = trimmed[start:end]

        result = {
            "data": page_data,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
            "params": {"days": days, "top_n": top_n},
        }
        with _api_cache_lock:
            _clusters_cache[cache_key] = result
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=ProblemDetail(
                type="https://loggazer.dev/errors/internal-error",
                title="Cluster Engine Error",
                status=500,
                detail=str(e),
                instance="/v1/clusters",
            ).model_dump(),
        )


@app.get(
    "/v1/platforms",
    tags=["Platforms"],
    summary="List supported platforms",
)
async def get_platforms():
    cache_key = "platforms"
    with _api_cache_lock:
        if cache_key in _platforms_cache:
            return _platforms_cache[cache_key]

    from log_parser import PLATFORM_SIGNATURES_COMPILED

    platforms = []
    for name, signatures in PLATFORM_SIGNATURES_COMPILED.items():
        platforms.append({
            "name": name,
            "detection_keywords": [s.pattern for s in signatures[:3]],
        })

    result = {"platforms": platforms, "total": len(platforms)}
    with _api_cache_lock:
        _platforms_cache[cache_key] = result
    return result


@app.get("/v1/metrics", tags=["Health"], summary="Prometheus-compatible metrics endpoint")
async def get_metrics():
    try:
        from prometheus_client import generate_latest, REGISTRY
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            content=generate_latest(REGISTRY).decode("utf-8"),
            media_type="text/plain; version=0.0.4",
        )
    except ImportError:
        return {"message": "prometheus_client not installed"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/v1/cache/clear", tags=["Health"], summary="Clear all caches")
async def clear_cache_endpoint(x_api_key: Optional[str] = Depends(verify_api_key)):
    cleared = {}
    api_result = clear_api_cache()
    cleared.update(api_result)
    try:
        from analyzer import clear_content_cache
        content_cleared = clear_content_cache()
        cleared["content_hash"] = content_cleared
    except Exception as e:
        cleared["content_hash_error"] = str(e)
    return {"status": "ok", "message": "All caches cleared", "details": cleared}


@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


# ============================================================
#  Entrypoint: python -m api.main
# ============================================================

if __name__ == "__main__":
    import uvicorn

    _use_reload = os.getenv("LOGGAZER_BACKEND_RELOAD", "0").lower() in ("1", "true", "yes")
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=_use_reload,
        log_level="info",
    )
