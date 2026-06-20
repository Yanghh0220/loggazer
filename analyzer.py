# analyzer.py - AI 分析引擎（业务流程编排层）+ 并行分析器调度
#
# v2.0 — 全面性能优化 (2026-06-20)
# ✅ 优化点: 四个分析器使用 ThreadPoolExecutor 并行执行
# ✅ 优化点: @cached_analysis 装饰器实现方法级缓存
# ✅ 优化点: 基于文件内容 MD5 hash 的 .cache/ 目录持久化缓存
#
# 职责：编排日志分析流程，不包含任何 HTTP 调用、重试逻辑、异常类定义
# 设计原则：对外只暴露一个函数
#   - analyze_log(log) → 完整分析流程，返回 AnalysisResult 实例

import hashlib
import json
import os
import time
import logging
import threading
import functools
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Any, Callable

from cachetools import TTLCache

from prompts import (
    build_analysis_prompt,
    build_rag_augmented_prompt,
    build_system_prompt,
)
from log_parser import parse_log, get_error_stats, compute_content_hash
from models import AnalysisResult, ParsedLog
from config import (
    CACHE_ENABLED,
    CACHE_SIMILARITY_HIGH,
    CACHE_SIMILARITY_LOW,
    CACHE_TTL_HOURS,
    CACHE_QDRANT_PATH,
    CACHE_EMBEDDING_MODEL,
)
from utils.performance import timer

logger = logging.getLogger(__name__)


# ============================================================
# ✅ 优化点: 共享线程池（用于并行运行四个分析器）
# ============================================================
# 避免每次分析时创建/销毁线程池的开销
_ANALYZER_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="loggazer-analyzer",
)


# ============================================================
# ✅ 优化点: @cached_analysis 装饰器 — 方法级结果缓存
# ============================================================
# 功能：装饰分析器方法，基于 log_text 的 MD5 hash 自动缓存结果
# 缓存策略：
#   ① 内存 TTLCache（最快，<1ms 命中）
#   ② .cache/ 目录持久化（重启后仍可用）
#
# 使用方式：
#   @cached_analysis(ttl_seconds=600)
#   def my_analyzer(log_text):
#       ...

_CACHED_ANALYSIS_CACHE: TTLCache = TTLCache(maxsize=500, ttl=300)
_CACHED_ANALYSIS_LOCK = threading.Lock()

# ✅ 优化点: .cache/ 目录持久化缓存
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)


def _load_from_disk_cache(cache_key: str) -> Optional[dict]:
    """从 .cache/ 目录读取缓存的 JSON 结果。"""
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        # 检查 TTL（文件修改时间）
        mtime = cache_file.stat().st_mtime
        # 默认 TTL: 30 分钟
        if time.time() - mtime > 1800:
            cache_file.unlink(missing_ok=True)
            return None
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        cache_file.unlink(missing_ok=True)
        return None


def _save_to_disk_cache(cache_key: str, data: dict) -> None:
    """保存结果到 .cache/ 目录。"""
    cache_file = CACHE_DIR / f"{cache_key}.json"
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except OSError:
        pass  # 磁盘缓存失败不影响主流程


def cached_analysis(ttl_seconds: int = 600):
    """
    ✅ 优化点: 分析器方法缓存装饰器。

    自动基于 log_text 的 MD5 hash 缓存分析结果。
    先查内存缓存 → 再查磁盘缓存 → 都不命中则执行并写入两级缓存。

    参数:
        ttl_seconds: 缓存有效期（秒），默认 10 分钟
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(log_text: str, *args, **kwargs) -> dict:
            content_key = compute_content_hash(log_text)
            func_key = f"{func.__name__}:{content_key}"

            # 1. 内存缓存
            with _CACHED_ANALYSIS_LOCK:
                if func_key in _CACHED_ANALYSIS_CACHE:
                    logger.debug("@cached_analysis 内存命中: %s", func.__name__)
                    return _CACHED_ANALYSIS_CACHE[func_key]

            # 2. 磁盘缓存
            disk_key = hashlib.md5(func_key.encode()).hexdigest()
            disk_result = _load_from_disk_cache(disk_key)
            if disk_result is not None:
                logger.debug("@cached_analysis 磁盘命中: %s", func.__name__)
                with _CACHED_ANALYSIS_LOCK:
                    _CACHED_ANALYSIS_CACHE[func_key] = disk_result
                return disk_result

            # 3. 执行并缓存
            result = func(log_text, *args, **kwargs)

            with _CACHED_ANALYSIS_LOCK:
                _CACHED_ANALYSIS_CACHE[func_key] = result
            _save_to_disk_cache(disk_key, result)

            return result

        return wrapper
    return decorator


# ============================================================
#  P0-2: 文件内容 Hash 缓存（核心快速路径）
# ============================================================
_content_hash_cache: TTLCache = TTLCache(maxsize=500, ttl=300)
_parsed_log_cache: TTLCache = TTLCache(maxsize=1000, ttl=600)
_cache_lock = threading.Lock()


def _make_content_key(log_text: str) -> str:
    """基于日志内容生成 MD5 缓存 key"""
    return compute_content_hash(log_text)


def clear_content_cache() -> int:
    """清除所有内容 Hash 缓存和增量追踪，返回清除的条目数"""
    with _cache_lock:
        analysis_count = len(_content_hash_cache)
        parsed_count = len(_parsed_log_cache)
        _content_hash_cache.clear()
        _parsed_log_cache.clear()
        total = analysis_count + parsed_count
        logger.info("内容缓存已清除: 分析结果 %d 条, 解析结果 %d 条", analysis_count, parsed_count)

    with _incremental_tracker_lock:
        inc_count = len(_incremental_tracker)
        _incremental_tracker.clear()
        if inc_count:
            logger.info("增量追踪已清除: %d 条", inc_count)

    return total


def get_content_cache_stats() -> dict:
    """获取缓存统计信息"""
    with _cache_lock:
        return {
            "analysis_cache_size": len(_content_hash_cache),
            "analysis_cache_maxsize": _content_hash_cache.maxsize,
            "analysis_cache_ttl_seconds": _content_hash_cache.ttl,
            "parsed_cache_size": len(_parsed_log_cache),
            "parsed_cache_maxsize": _parsed_log_cache.maxsize,
            "parsed_cache_ttl_seconds": _parsed_log_cache.ttl,
        }


# ============================================================
#  语义缓存（延迟初始化单例）
# ============================================================

def _get_cache():
    """获取或初始化 SemanticCache 单例"""
    if not CACHE_ENABLED:
        return None
    try:
        from cache_engine import SemanticCache
        return SemanticCache(
            embedding_model=CACHE_EMBEDDING_MODEL,
            qdrant_path=CACHE_QDRANT_PATH or None,
            similarity_high=CACHE_SIMILARITY_HIGH,
            similarity_low=CACHE_SIMILARITY_LOW,
            ttl_hours=CACHE_TTL_HOURS,
        )
    except Exception as e:
        logger.warning("语义缓存初始化失败，将直接调用 AI: %s", e)
        return None


_cache_instance = None
_cache_initialized = False


def _get_or_create_cache():
    """获取缓存单例，首次调用时初始化"""
    global _cache_instance, _cache_initialized
    if not _cache_initialized:
        _cache_instance = _get_cache()
        _cache_initialized = True
    return _cache_instance


# ============================================================
#  P1-4①: 增量分析追踪
# ============================================================
_incremental_tracker: dict = {}
_incremental_tracker_lock = threading.Lock()
_INCREMENTAL_TTL_SECONDS = 1800


def _check_incremental(log_text: str, content_key: str) -> tuple[str | None, AnalysisResult | None]:
    with _incremental_tracker_lock:
        entry = _incremental_tracker.get(content_key)
        if entry is None:
            return None, None
        if time.time() - entry.get("timestamp", 0) > _INCREMENTAL_TTL_SECONDS:
            del _incremental_tracker[content_key]
            return None, None
        prev_line_count = entry.get("line_count", 0)
        current_lines = log_text.splitlines()
        current_line_count = len(current_lines)
        if current_line_count <= prev_line_count:
            prev_result = entry.get("result")
            if prev_result is not None:
                logger.info("增量分析: 行数未增加 (%d → %d), 直接返回上次结果",
                           prev_line_count, current_line_count)
                return None, prev_result
            return None, None
        new_lines = current_lines[prev_line_count:]
        new_text = "\n".join(new_lines)
        logger.info("增量分析: %d → %d 行, 新增 %d 行需要分析",
                   prev_line_count, current_line_count, len(new_lines))
        return new_text, entry.get("result")
    return None, None


def _update_incremental_tracker(content_key: str, log_text: str, result: AnalysisResult):
    with _incremental_tracker_lock:
        _incremental_tracker[content_key] = {
            "line_count": len(log_text.splitlines()),
            "result": result,
            "timestamp": time.time(),
        }


# ============================================================
# ✅ 优化点: 并行分析器调度 — 四个分析器并行执行
# ============================================================

def _run_parallel_analyzers(
    log_text: str,
    error_lines: list[str],
) -> dict[str, Any]:
    """
    使用 ThreadPoolExecutor 并行执行四个分析器。

    ✅ 优化点: 替代原来的串行调用，四个独立分析任务并行执行。
    总耗时 ≈ max(各分析器耗时) 而非 sum(各分析器耗时)。

    参数:
        log_text: 原始日志文本
        error_lines: 预提取的错误行

    返回:
        {
            "statistics": ...,
            "anomalies": ...,
            "patterns": ...,
            "timeline": ...,
        }
    """
    from analyzers.stats_analyzer import compute_statistics
    from analyzers.anomaly_detector import detect_anomalies
    from analyzers.pattern_analyzer import analyze_patterns
    from analyzers.timeline_analyzer import analyze_timeline

    results: dict[str, Any] = {}

    with timer("analyzer:并行分析器执行"):
        futures: dict[str, Any] = {}

        # 提交四个分析任务
        futures["statistics"] = _ANALYZER_EXECUTOR.submit(
            compute_statistics, log_text, error_lines
        )
        futures["anomalies"] = _ANALYZER_EXECUTOR.submit(
            detect_anomalies, log_text, error_lines
        )
        futures["patterns"] = _ANALYZER_EXECUTOR.submit(
            analyze_patterns, log_text, error_lines
        )
        futures["timeline"] = _ANALYZER_EXECUTOR.submit(
            analyze_timeline, log_text, error_lines
        )

        # 等待全部完成（任一失败不影响其他）
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=30)
            except Exception as e:
                logger.warning("分析器 %s 执行失败: %s", name, e)
                results[name] = {"error": str(e), "status": "failed"}

    return results


# ============================================================
#  完整日志分析流程（对外暴露）
# ============================================================

def analyze_log(log_text: str) -> AnalysisResult:
    """
    完整的日志分析流程。

    ✅ 优化点:
      - 内容 Hash 缓存快速路径（<1ms 命中）
      - 并行分析器执行（ThreadPoolExecutor，4 个分析器并行）
      - 语义缓存检索
      - 增量分析追踪

    参数:
        log_text: 用户粘贴的构建日志原文

    返回:
        AnalysisResult 实例
    """
    if not log_text or not log_text.strip():
        raise ValueError("日志内容不能为空")

    # ---- 1. P0-2: 内容 Hash 缓存快速路径 ----
    content_key = _make_content_key(log_text)
    with _cache_lock:
        if content_key in _content_hash_cache:
            cached = _content_hash_cache[content_key]
            if isinstance(cached, dict):
                cached = AnalysisResult.model_validate(cached)
            logger.info("内容Hash缓存命中: key=%s...", content_key[:16])
            return cached

    # ---- 1.5 P1-4①: 增量分析检查 ----
    new_lines_text, prev_result = _check_incremental(log_text, content_key)
    if prev_result is not None and new_lines_text is None:
        if isinstance(prev_result, dict):
            prev_result = AnalysisResult.model_validate(prev_result)
        with _cache_lock:
            _content_hash_cache[content_key] = prev_result
        return prev_result

    # ---- 2. 预处理日志 ----
    parsed = None
    stats = None
    with _cache_lock:
        if content_key in _parsed_log_cache:
            cached = _parsed_log_cache[content_key]
            parsed = cached["parsed"]
            stats = cached["stats"]

    if parsed is None:
        with timer("analyzer:日志预处理", record=True):
            parsed = parse_log(log_text)
            stats = get_error_stats(log_text)
        with _cache_lock:
            _parsed_log_cache[content_key] = {"parsed": parsed, "stats": stats}

    # ---- 2.5 ✅ 优化点: 并行运行四个分析器 ----
    parallel_results = _run_parallel_analyzers(
        log_text,
        parsed["error_lines"],
    )
    logger.info(
        "并行分析完成: stats=%s, anomalies=%s, patterns=%s, timeline=%s",
        "ok" if "error" not in parallel_results.get("statistics", {}) else "fail",
        "ok" if "error" not in parallel_results.get("anomalies", {}) else "fail",
        "ok" if "error" not in parallel_results.get("patterns", {}) else "fail",
        "ok" if "error" not in parallel_results.get("timeline", {}) else "fail",
    )

    # ---- 3. 缓存检索（透明层） ----
    cache = _get_or_create_cache()
    fingerprint: str | None = None
    cached_result: AnalysisResult | None = None
    rag_context: str = ""

    if cache is not None:
        try:
            with timer("analyzer:缓存检索", record=True):
                from cache_engine import generate_fingerprint
                with timer("analyzer:指纹生成"):
                    fingerprint = generate_fingerprint(parsed)
                cached_result = cache.get(fingerprint, parsed)

            if cached_result is not None:
                if isinstance(cached_result, dict):
                    cached_result = AnalysisResult.model_validate(cached_result)
                return cached_result

            with timer("analyzer:RAG上下文检索"):
                rag_context = cache.get_rag_context(fingerprint)

        except Exception as e:
            logger.warning("缓存层异常，降级到直接分析: %s", e)
            rag_context = ""

    # ---- 4. 构建提示词（注入并行分析结果） ----
    with timer("analyzer:构建提示词", record=True):
        # ✅ 优化点: 将并行分析结果注入提示词，提升 AI 分析质量
        enriched_stats = {
            **stats,
            "parallel_analysis": parallel_results,
        }

        user_prompt: str = build_analysis_prompt(
            source=parsed["platform"],
            error_lines=parsed["error_lines"],
            stats=enriched_stats,
            full_log_preview=parsed["truncated_log"],
        )

        if rag_context:
            user_prompt = build_rag_augmented_prompt(rag_context, user_prompt)

    # ---- 5. 构建 Schema 自省的 System Prompt ----
    with timer("analyzer:构建Schema提示词"):
        system_prompt = build_system_prompt(AnalysisResult.model_json_schema())

    # ---- 6. 调用结构化生成 ----
    with timer("analyzer:AI调用", record=True):
        try:
            from ai_engine import call_ai_structured
            result = call_ai_structured(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_retries=3,
            )
        except ImportError:
            logger.warning("ai_engine 不可用，走 legacy 路径")
            result = _legacy_analyze(user_prompt)

    # ---- 7. 确保返回值是 AnalysisResult 实例 ----
    with timer("analyzer:结果校验与反序列化", record=True):
        if isinstance(result, dict):
            try:
                result = AnalysisResult.model_validate(result)
            except Exception:
                from ai_engine import _best_effort_parse_to_model
                result = _best_effort_parse_to_model(
                    json.dumps(result, ensure_ascii=False), AnalysisResult
                )

    # ---- 8. 写入缓存 ----
    if cache is not None and fingerprint is not None:
        try:
            cache.set(fingerprint, result, {
                "platform": parsed["platform"],
                "error_lines": parsed["error_lines"],
            })
        except Exception as e:
            logger.warning("缓存写入失败: %s", e)

    # ---- 9. P0-2: 写入内容 Hash 缓存 ----
    with _cache_lock:
        _content_hash_cache[content_key] = result

    # ---- 9.5 更新增量分析追踪 ----
    _update_incremental_tracker(content_key, log_text, result)

    # ---- 10. 错误指纹 + 智能聚类 ----
    with timer("analyzer:聚类存储", record=True):
        _store_to_cluster_engine(log_text, parsed, result)

    return result


def _store_to_cluster_engine(
    log_text: str, parsed: dict, result: "AnalysisResult"
) -> None:
    """将分析结果存入聚类引擎（透明层）"""
    try:
        from fingerprint_engine import get_fingerprint_engine
        from cluster_engine import get_cluster_engine
        fp_engine = get_fingerprint_engine()
        cluster_engine = get_cluster_engine()
        fp = fp_engine.fingerprint(parsed["error_lines"], parsed["platform"])
        cluster_id = cluster_engine.assign_cluster(fp)
        cluster_engine.store_analysis(
            raw_log=log_text, fingerprint=fp, result=result, cluster_id=cluster_id,
        )
    except Exception as e:
        logger.debug("聚类引擎存储失败（不影响主流程）: %s", e)


def _legacy_analyze(user_prompt: str) -> AnalysisResult:
    """Legacy 分析路径"""
    from ai_engine import call_ai_legacy, _best_effort_parse_to_model, _create_fallback_model
    system_prompt = build_system_prompt(AnalysisResult.model_json_schema())
    result_text = call_ai_legacy(system_prompt, user_prompt)
    if result_text.startswith("⚠️"):
        return _create_fallback_model(AnalysisResult, result_text)
    return _best_effort_parse_to_model(result_text, AnalysisResult)


# ============================================================
#  Multi-Agent 分析入口
# ============================================================

def analyze_log_advanced(log_text: str) -> AnalysisResult:
    """
    Multi-Agent 分析入口（带降级到 analyze_log）。
    """
    if not log_text or not log_text.strip():
        raise ValueError("日志内容不能为空")

    content_key = _make_content_key(log_text)
    with _cache_lock:
        if content_key in _content_hash_cache:
            cached = _content_hash_cache[content_key]
            if isinstance(cached, dict):
                cached = AnalysisResult.model_validate(cached)
            return cached

    start_time = time.time()

    parsed = None
    stats = None
    with _cache_lock:
        if content_key in _parsed_log_cache:
            cached_parsed = _parsed_log_cache[content_key]
            parsed = cached_parsed["parsed"]
            stats = cached_parsed["stats"]

    if parsed is None:
        parsed = parse_log(log_text)
        stats = get_error_stats(log_text)
        with _cache_lock:
            _parsed_log_cache[content_key] = {"parsed": parsed, "stats": stats}

    rag_context = ""
    cache = _get_or_create_cache()
    fingerprint = None

    if cache is not None:
        try:
            from cache_engine import generate_fingerprint
            fingerprint = generate_fingerprint(parsed)
            cached_result = cache.get(fingerprint, parsed)
            if cached_result is not None:
                if isinstance(cached_result, dict):
                    cached_result = AnalysisResult.model_validate(cached_result)
                return cached_result
            rag_context = cache.get_rag_context(fingerprint)
        except Exception as e:
            logger.warning("[Advanced] 缓存层异常: %s", e)
            rag_context = ""

    try:
        from agent_graph import get_agent_graph
        graph = get_agent_graph()
        initial_state = {
            "log_text": log_text,
            "parsed_log": parsed,
            "error_stats": stats,
            "rag_context": rag_context or "",
            "iteration_count": 0,
            "fallback_used": False,
            "error_message": "",
            "tool_calls_made": [],
            "tool_results": "",
            "needs_retry": False,
            "human_review_needed": False,
        }
        final_state = graph.invoke(initial_state)
        final_report = final_state.get("final_report", {})

        if not final_report:
            return analyze_log(log_text)

        if isinstance(final_report, AnalysisResult):
            result = final_report
        elif isinstance(final_report, dict):
            try:
                result = AnalysisResult.model_validate(final_report)
            except Exception:
                return analyze_log(log_text)
        else:
            return analyze_log(log_text)

        if cache is not None and fingerprint is not None:
            try:
                cache.set(fingerprint, result, {
                    "platform": parsed["platform"],
                    "error_lines": parsed["error_lines"],
                })
            except Exception as e:
                logger.warning("[Advanced] 缓存写入失败: %s", e)

        with _cache_lock:
            _content_hash_cache[content_key] = result

        _store_to_cluster_engine(log_text, parsed, result)

        elapsed = time.time() - start_time
        logger.info("[Advanced] 分析完成，耗时 %.2fs", elapsed)
        return result

    except ImportError:
        return analyze_log(log_text)
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error("[Advanced] Agent 图执行失败 (%.2fs): %s: %s",
                     elapsed, type(e).__name__, str(e)[:200])
        return analyze_log(log_text)
