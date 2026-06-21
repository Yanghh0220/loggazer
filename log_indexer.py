# log_indexer.py — 日志索引文件机制
#
# v1.0 — 首次实现 (2026-06-21)
#
# 设计目标:
#   1. 首次打开日志文件时，扫描并生成 Parquet 索引文件
#   2. 再次打开同一文件时，直接加载索引，跳过重复解析
#   3. 自动检测索引失效（文件大小/mtime/指纹变化）并重建
#   4. 原子写入：先写临时文件，再 rename 到最终位置
#
# 索引文件命名: {original_filename}.loggazer
# 索引格式: Apache Parquet (via pyarrow)
# 索引 schema 版本: 1 (存储在 Parquet metadata 中)
#
# 索引内容 (每行一条记录):
#   - timestamp_us:    int64   Unix 微秒时间戳 (归一化)
#   - level:           string  日志级别 (ERROR/WARN/INFO/DEBUG/FATAL/UNKNOWN)
#   - byte_offset:     int64   在原始文件中的字节偏移
#   - line_number:     int32   行号 (1-based)
#   - line_length:     int32   该行字节长度 (含换行符)
#   - message_preview: string  该行前 200 字符的消息预览
#
# 索引元数据 (存储在 Parquet schema metadata 中):
#   - schema_version:       int    索引 schema 版本号
#   - source_file:          str    原始日志文件的绝对路径
#   - source_file_size:     int64  原始文件大小 (bytes)
#   - source_file_mtime:    float  原始文件最后修改时间 (Unix timestamp)
#   - source_fingerprint:   str    文件头部+尾部 SHA256 指纹
#   - parser_version:       str    构建索引的 parser 版本
#   - total_lines:          int64  总行数
#   - created_at:           str    索引创建时间 (ISO 8601)
#
# 失效检测规则 (任一满足即重建):
#   1. 原始文件不存在
#   2. 原始文件大小变化
#   3. 原始文件 mtime 变化
#   4. 文件指纹 (头部+尾部 4096 字节 SHA256) 不匹配
#   5. 索引 schema 版本不兼容
#   6. 索引文件损坏 (parquet 读取失败)

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 常量定义
# ============================================================

# 索引文件扩展名
INDEX_EXTENSION = ".loggazer"

# 当前索引 schema 版本 (向后兼容的基础)
# 升级规则: 增加新字段 → 递增版本号，旧版本索引自动重建
CURRENT_SCHEMA_VERSION = 1

# Parser 版本标识 (用于追踪索引构建时的 parser 逻辑版本)
PARSER_VERSION = "log_indexer.v1"

# 指纹采样: 头部和尾部各取多少字节做 SHA256
FINGERPRINT_SAMPLE_BYTES = 4096

# 消息预览最大字符数
MAX_PREVIEW_LENGTH = 200

# Parquet 写入参数
PARQUET_COMPRESSION = "zstd"  # zstd 提供良好的压缩比和速度平衡
PARQUET_ROW_GROUP_SIZE = 100_000  # 每 10 万行一个 row group

# ============================================================
# 日志级别检测
# ============================================================

# 预编译的日志级别正则 (按优先级排序)
_LOG_LEVEL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("FATAL", re.compile(r"\bFATAL\b", re.IGNORECASE)),
    ("CRITICAL", re.compile(r"\bCRITICAL\b", re.IGNORECASE)),
    ("ERROR", re.compile(r"\bERROR\b", re.IGNORECASE)),
    ("WARN", re.compile(r"\bWARN(?:ING)?\b", re.IGNORECASE)),
    ("INFO", re.compile(r"\bINFO\b", re.IGNORECASE)),
    ("DEBUG", re.compile(r"\bDEBUG\b", re.IGNORECASE)),
    ("TRACE", re.compile(r"\bTRACE\b", re.IGNORECASE)),
]


def detect_level(line: str) -> str:
    """从一行日志中检测日志级别。"""
    for level_name, pattern in _LOG_LEVEL_PATTERNS:
        if pattern.search(line):
            return level_name
    return "UNKNOWN"


# ============================================================
# 时间戳解析
# ============================================================

# 常见日志时间戳格式 → 编译好的正则 + strptime 格式
# 按常见程度排序，优先匹配高频格式
_TIMESTAMP_FORMATS: list[tuple[re.Pattern, str]] = [
    # ISO 8601 with timezone: 2024-01-15T10:30:45.123Z / 2024-01-15T10:30:45+08:00
    (
        re.compile(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
        ),
        "iso",
    ),
    # ISO 8601 without T: 2024-01-15 10:30:45,123
    (
        re.compile(
            r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)"
        ),
        "iso_space",
    ),
    # Syslog: Jan 15 10:30:45
    (
        re.compile(
            r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
        ),
        "syslog",
    ),
    # Unix timestamp (seconds): 1705312245 (10 digits, standalone)
    (
        re.compile(
            r"\b(\d{10})(?:\.(\d{1,6}))?\b"
        ),
        "unix_sec",
    ),
    # Unix timestamp (millis): 1705312245123 (13 digits, standalone)
    (
        re.compile(
            r"\b(\d{13})\b"
        ),
        "unix_ms",
    ),
    # Datetime with slashes: 2024/01/15 10:30:45
    (
        re.compile(
            r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?)"
        ),
        "slash",
    ),
    # US-style: 01-15-2024 10:30:45
    (
        re.compile(
            r"(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})"
        ),
        "us",
    ),
]


def _parse_iso_timestamp(ts_str: str) -> Optional[int]:
    """解析各种 ISO 变体时间戳，返回 Unix 微秒。

    注意: 所有时间戳统一解析为 UTC 后转为 Unix 时间戳。
    Python 的 datetime.timestamp() 会将 naive datetime 视为本地时间，
    因此必须显式使用 UTC timezone。
    """
    from datetime import timezone as _timezone

    # 标准化分隔符
    normalized = ts_str.replace("T", " ").replace(",", ".")

    # 检测时区
    is_utc = False
    if normalized.endswith("Z"):
        normalized = normalized[:-1].strip()
        is_utc = True
    elif "+" in normalized:
        # 包含时区偏移，暂不处理（保留原始语义）
        # 简化处理: 去除时区后缀，按 UTC 解析
        plus_pos = normalized.rfind("+")
        minus_pos = normalized.rfind("-")
        tz_pos = max(plus_pos, minus_pos)
        if tz_pos > 10:  # 确保不是日期中的连字符
            normalized = normalized[:tz_pos].strip()
            is_utc = True
    elif normalized.count("-") > 2 and normalized.rfind("-") > 10:
        normalized = normalized[: normalized.rfind("-")].strip()

    for fmt in [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(normalized, fmt)
            if is_utc:
                dt = dt.replace(tzinfo=_timezone.utc)
            else:
                dt = dt.replace(tzinfo=_timezone.utc)
            return int(dt.timestamp() * 1_000_000)
        except ValueError:
            continue
    return None


def _parse_syslog_timestamp(ts_str: str, year: Optional[int] = None) -> Optional[int]:
    """解析 syslog 风格时间戳 (Jan 15 10:30:45)。"""
    if year is None:
        year = datetime.now().year
    try:
        dt = datetime.strptime(f"{year} {ts_str}", "%Y %b %d %H:%M:%S")
        return int(dt.timestamp() * 1_000_000)
    except ValueError:
        return None


def extract_timestamp_us(line: str) -> Optional[int]:
    """从一行日志中提取归一化时间戳 (Unix 微秒)。

    返回 None 表示无法解析时间戳。
    """
    for pattern, fmt_type in _TIMESTAMP_FORMATS:
        m = pattern.search(line)
        if m is None:
            continue

        ts_str = m.group(1)
        if not ts_str:
            continue

        if fmt_type == "iso":
            result = _parse_iso_timestamp(ts_str)
            if result is not None:
                return result
        elif fmt_type == "iso_space":
            result = _parse_iso_timestamp(ts_str)
            if result is not None:
                return result
        elif fmt_type == "syslog":
            result = _parse_syslog_timestamp(ts_str)
            if result is not None:
                return result
        elif fmt_type == "unix_sec":
            try:
                sec = int(ts_str)
                # 合理的 Unix 秒范围: 2000年 ~ 2100年
                if 946684800 <= sec <= 4102444800:
                    frac = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                    if frac:
                        us = int(frac.ljust(6, "0")[:6])
                    else:
                        us = 0
                    return sec * 1_000_000 + us
            except ValueError:
                pass
        elif fmt_type == "unix_ms":
            try:
                ms = int(ts_str)
                # 合理的 Unix 毫秒范围
                if 946684800000 <= ms <= 4102444800000:
                    return ms * 1000
            except ValueError:
                pass
        elif fmt_type == "slash":
            result = _parse_iso_timestamp(ts_str)
            if result is not None:
                return result
        elif fmt_type == "us":
            try:
                dt = datetime.strptime(ts_str, "%m-%d-%Y %H:%M:%S")
                return int(dt.timestamp() * 1_000_000)
            except ValueError:
                pass

    return None


# ============================================================
# 文件指纹
# ============================================================


def compute_fingerprint(file_path: str, sample_bytes: int = FINGERPRINT_SAMPLE_BYTES) -> str:
    """计算文件指纹: 头部 + 尾部各 sample_bytes 的 SHA256。

    对于小于 2*sample_bytes 的文件，计算完整文件的 SHA256。
    """
    file_size = os.path.getsize(file_path)
    hasher = hashlib.sha256()

    with open(file_path, "rb") as f:
        if file_size <= 2 * sample_bytes:
            hasher.update(f.read())
        else:
            # 头部
            hasher.update(f.read(sample_bytes))
            # 尾部
            f.seek(-sample_bytes, os.SEEK_END)
            hasher.update(f.read(sample_bytes))

    return hasher.hexdigest()


# ============================================================
# 索引文件路径
# ============================================================


def index_path_for(source_path: str) -> str:
    """返回给定日志文件的索引文件路径。"""
    return source_path + INDEX_EXTENSION


# ============================================================
# 索引元数据读写
# ============================================================


def _build_metadata(source_path: str, total_lines: int) -> dict:
    """构建索引元数据字典。"""
    file_size = os.path.getsize(source_path)
    file_mtime = os.path.getmtime(source_path)
    fingerprint = compute_fingerprint(source_path)

    return {
        b"schema_version": str(CURRENT_SCHEMA_VERSION).encode("utf-8"),
        b"source_file": os.path.abspath(source_path).encode("utf-8"),
        b"source_file_size": str(file_size).encode("utf-8"),
        b"source_file_mtime": str(file_mtime).encode("utf-8"),
        b"source_fingerprint": fingerprint.encode("utf-8"),
        b"parser_version": PARSER_VERSION.encode("utf-8"),
        b"total_lines": str(total_lines).encode("utf-8"),
        b"created_at": datetime.now(timezone.utc).isoformat().encode("utf-8"),
    }


def read_index_metadata(index_path: str) -> Optional[dict]:
    """读取索引文件的元数据，不加载数据行。

    使用 pq.read_metadata() 避免在 Windows 上持久持有文件句柄。

    返回 dict，键为 str 类型。如果文件不存在或损坏，返回 None。
    """
    try:
        import pyarrow.parquet as pq

        # read_metadata 仅读取 footer，不保持文件打开
        file_meta = pq.read_metadata(index_path)
        raw_meta = file_meta.metadata
        if raw_meta is None:
            logger.warning("Index file has no metadata: %s", index_path)
            return None

        result = {}
        for key, value in raw_meta.items():
            result[key.decode("utf-8")] = value.decode("utf-8")
        return result
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Failed to read index metadata from %s: %s", index_path, e)
        return None


# ============================================================
# 索引验证
# ============================================================


class IndexValidationResult:
    """索引验证结果。"""

    def __init__(self, is_valid: bool, reason: str = ""):
        self.is_valid = is_valid
        self.reason = reason

    def __bool__(self) -> bool:
        return self.is_valid


def validate_index(source_path: str, index_path: str) -> IndexValidationResult:
    """验证索引文件是否仍然有效。

    检查项:
    1. 原始文件存在
    2. 索引文件存在且可读
    3. Schema 版本兼容
    4. 文件大小匹配
    5. 文件 mtime 匹配
    6. 文件指纹匹配
    """
    # 1. 原始文件必须存在
    if not os.path.isfile(source_path):
        return IndexValidationResult(False, "Source file does not exist")

    # 2. 索引文件必须存在
    if not os.path.isfile(index_path):
        return IndexValidationResult(False, "Index file does not exist")

    # 3. 读取元数据
    meta = read_index_metadata(index_path)
    if meta is None:
        return IndexValidationResult(False, "Index file is corrupt or missing metadata")

    # 4. Schema 版本检查
    try:
        schema_ver = int(meta.get("schema_version", "0"))
    except (ValueError, TypeError):
        return IndexValidationResult(False, "Invalid schema_version in index")
    if schema_ver != CURRENT_SCHEMA_VERSION:
        return IndexValidationResult(
            False,
            f"Schema version mismatch: index={schema_ver}, current={CURRENT_SCHEMA_VERSION}",
        )

    # 5. 文件大小检查
    current_size = os.path.getsize(source_path)
    try:
        indexed_size = int(meta.get("source_file_size", "-1"))
    except (ValueError, TypeError):
        return IndexValidationResult(False, "Invalid source_file_size in index")
    if current_size != indexed_size:
        return IndexValidationResult(
            False,
            f"File size changed: indexed={indexed_size}, current={current_size}",
        )

    # 6. mtime 检查
    current_mtime = os.path.getmtime(source_path)
    try:
        indexed_mtime = float(meta.get("source_file_mtime", "-1"))
    except (ValueError, TypeError):
        return IndexValidationResult(False, "Invalid source_file_mtime in index")
    if abs(current_mtime - indexed_mtime) > 0.001:  # 容忍浮点精度
        return IndexValidationResult(
            False,
            f"File mtime changed: indexed={indexed_mtime}, current={current_mtime}",
        )

    # 7. 指纹检查 (最可靠但最昂贵的检查，放在最后)
    current_fingerprint = compute_fingerprint(source_path)
    indexed_fingerprint = meta.get("source_fingerprint", "")
    if current_fingerprint != indexed_fingerprint:
        return IndexValidationResult(
            False,
            "File fingerprint mismatch (content changed despite same size+mtime)",
        )

    return IndexValidationResult(True, "Index is valid")


# ============================================================
# 索引构建
# ============================================================


def build_index(
    source_path: str,
    index_path: Optional[str] = None,
    progress_callback: Optional[callable] = None,
) -> tuple[str, dict]:
    """扫描日志文件并构建 Parquet 索引。

    参数:
        source_path: 原始日志文件路径
        index_path: 索引文件输出路径 (默认: source_path + .loggazer)
        progress_callback: 可选的进度回调，接收 (current_line, total_lines) 两参数

    返回:
        (index_path, stats_dict)
        stats_dict 包含: total_lines, lines_with_timestamp, level_distribution,
                         time_range_min, time_range_max, file_size_mb, build_duration_ms
    """
    start_time = time.time()

    if index_path is None:
        index_path = index_path_for(source_path)

    file_size = os.path.getsize(source_path)
    logger.info(
        "Building index for %s (%0.1f MB)...",
        source_path,
        file_size / (1024 * 1024),
    )

    # 预分配列表 — 使用 append 动态增长
    estimated_lines = max(file_size // 200, 1000)

    timestamps: list[int] = []
    levels: list[str] = []
    byte_offsets: list[int] = []
    line_numbers: list[int] = []
    line_lengths: list[int] = []
    previews: list[str] = []

    # 统计
    level_counts: dict[str, int] = {}
    timestamps_found = 0
    t_min_us: Optional[int] = None
    t_max_us: Optional[int] = None

    # 扫描文件
    current_offset = 0
    line_num = 0
    last_progress_report = 0

    with open(source_path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line_num += 1
            line_len = len(raw_line.encode("utf-8"))

            # 检测日志级别
            level = detect_level(raw_line)
            level_counts[level] = level_counts.get(level, 0) + 1

            # 提取时间戳
            ts_us = extract_timestamp_us(raw_line)
            if ts_us is not None:
                timestamps_found += 1
                if t_min_us is None or ts_us < t_min_us:
                    t_min_us = ts_us
                if t_max_us is None or ts_us > t_max_us:
                    t_max_us = ts_us

            # 消息预览 (截断)
            stripped = raw_line.strip()
            preview = stripped[:MAX_PREVIEW_LENGTH]

            # 添加到列表
            timestamps.append(ts_us if ts_us is not None else 0)
            levels.append(level)
            byte_offsets.append(current_offset)
            line_numbers.append(line_num)
            line_lengths.append(line_len)
            previews.append(preview)

            current_offset += line_len

            # 进度报告 (每 50000 行报告一次)
            if progress_callback and line_num - last_progress_report >= 50000:
                progress_callback(line_num, estimated_lines)
                last_progress_report = line_num

    total_lines = line_num
    logger.info("Scanned %d lines from %s", total_lines, source_path)

    # 构建 DataFrame
    df = pd.DataFrame(
        {
            "timestamp_us": pd.array(timestamps, dtype="int64"),
            "level": pd.array(levels, dtype="string"),
            "byte_offset": pd.array(byte_offsets, dtype="int64"),
            "line_number": pd.array(line_numbers, dtype="int32"),
            "line_length": pd.array(line_lengths, dtype="int32"),
            "message_preview": pd.array(previews, dtype="string"),
        }
    )

    # 构建元数据
    metadata = _build_metadata(source_path, total_lines)

    # 原子写入: 先写临时文件，再 rename
    tmp_path = index_path + ".tmp." + str(os.getpid())
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pandas(df)
        # 将元数据写入 Parquet schema metadata
        table = table.replace_schema_metadata(metadata)

        pq.write_table(
            table,
            tmp_path,
            compression=PARQUET_COMPRESSION,
            row_group_size=PARQUET_ROW_GROUP_SIZE,
            write_statistics=True,
        )

        # 原子 rename (Windows 上 os.replace 是原子的)
        os.replace(tmp_path, index_path)
        logger.info("Index written: %s (%d rows, %.1f MB)",
                     index_path, total_lines,
                     os.path.getsize(index_path) / (1024 * 1024))
    finally:
        # 清理临时文件
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    build_duration_ms = (time.time() - start_time) * 1000

    # 构建统计信息
    stats = {
        "total_lines": total_lines,
        "lines_with_timestamp": timestamps_found,
        "timestamp_coverage": round(timestamps_found / total_lines, 4) if total_lines > 0 else 0.0,
        "level_distribution": level_counts,
        "time_range_min_us": t_min_us,
        "time_range_max_us": t_max_us,
        "file_size_mb": round(file_size / (1024 * 1024), 2),
        "index_size_mb": round(os.path.getsize(index_path) / (1024 * 1024), 2),
        "build_duration_ms": round(build_duration_ms, 1),
    }

    return index_path, stats


# ============================================================
# 索引加载
# ============================================================


def load_index(index_path: str) -> pd.DataFrame:
    """从 Parquet 索引文件加载数据。

    返回包含以下列的 DataFrame:
    - timestamp_us, level, byte_offset, line_number, line_length, message_preview

    异常:
        FileNotFoundError: 索引文件不存在
        ValueError: 索引文件损坏
    """
    import pyarrow.parquet as pq

    if not os.path.isfile(index_path):
        raise FileNotFoundError(f"Index file not found: {index_path}")

    try:
        table = pq.read_table(index_path)
        df = table.to_pandas()
        logger.info("Loaded index: %s (%d rows)", index_path, len(df))
        return df
    except Exception as e:
        raise ValueError(f"Failed to load index file {index_path}: {e}") from e


def load_index_stats(index_path: str) -> dict:
    """仅加载索引统计信息（不读数据行）。"""
    meta = read_index_metadata(index_path)
    if meta is None:
        return {}

    stats = {
        "total_lines": int(meta.get("total_lines", "0")),
        "source_file_size": int(meta.get("source_file_size", "0")),
        "source_file_mtime": float(meta.get("source_file_mtime", "0")),
        "schema_version": int(meta.get("schema_version", "0")),
        "created_at": meta.get("created_at", ""),
    }

    # 记录索引文件大小
    try:
        stats["index_size_bytes"] = os.path.getsize(index_path)
    except OSError:
        pass

    return stats


# ============================================================
# 主入口: 智能打开日志文件
# ============================================================


def open_log(
    source_path: str,
    force_rebuild: bool = False,
    progress_callback: Optional[callable] = None,
) -> dict:
    """智能打开日志文件：优先使用索引，无效则扫描构建。

    这是外部调用者使用的主要入口。

    参数:
        source_path: 日志文件路径
        force_rebuild: 是否强制重建索引
        progress_callback: 索引构建时的进度回调

    返回:
        dict:
            status:        "index_hit" | "index_built" | "index_rebuilt" | "error"
            index_path:    索引文件路径
            stats:         统计信息 dict
            validation:    验证结果描述 (仅 rebuilt 时有意义)
            source_path:   原始文件路径
    """
    if not os.path.isfile(source_path):
        return {
            "status": "error",
            "index_path": None,
            "stats": {},
            "validation": "Source file does not exist",
            "source_path": source_path,
        }

    index_path = index_path_for(source_path)

    # 检查是否需要构建/重建
    if force_rebuild:
        need_build = True
        build_reason = "Forced rebuild"
    else:
        validation = validate_index(source_path, index_path)
        if validation.is_valid:
            # 索引有效，直接使用
            stats = load_index_stats(index_path)
            return {
                "status": "index_hit",
                "index_path": index_path,
                "stats": stats,
                "validation": "Index is valid",
                "source_path": source_path,
            }
        else:
            need_build = True
            build_reason = validation.reason

    # 需要构建/重建
    action = "rebuilt" if os.path.exists(index_path) else "built"
    try:
        _, stats = build_index(source_path, index_path, progress_callback)
        return {
            "status": f"index_{action}",
            "index_path": index_path,
            "stats": stats,
            "validation": build_reason,
            "source_path": source_path,
        }
    except Exception as e:
        logger.error("Failed to build index for %s: %s", source_path, e)
        return {
            "status": "error",
            "index_path": None,
            "stats": {},
            "validation": f"Build failed: {str(e)[:200]}",
            "source_path": source_path,
        }


def read_lines_by_offset(
    source_path: str,
    offsets_and_lengths: list[tuple[int, int]],
) -> list[str]:
    """根据字节偏移和长度读取原始日志行。

    用于按需加载完整行内容（避免一次性加载整个文件到内存）。

    参数:
        source_path: 日志文件路径
        offsets_and_lengths: [(byte_offset, line_length), ...] 列表

    返回:
        对应位置的原始行字符串列表
    """
    if not offsets_and_lengths:
        return []

    # 按 offset 排序以便顺序读取
    indexed = sorted(enumerate(offsets_and_lengths), key=lambda x: x[1][0])
    results = [""] * len(offsets_and_lengths)

    with open(source_path, "r", encoding="utf-8", errors="replace") as f:
        for orig_idx, (offset, length) in indexed:
            try:
                f.seek(offset)
                results[orig_idx] = f.read(length)
            except OSError:
                results[orig_idx] = ""

    return results


# ============================================================
# 诊断/管理工具
# ============================================================


def delete_index(source_path: str) -> bool:
    """删除指定日志文件的索引。"""
    index_path = index_path_for(source_path)
    if os.path.isfile(index_path):
        os.remove(index_path)
        logger.info("Deleted index: %s", index_path)
        return True
    return False


def list_indices(directory: str = ".") -> list[dict]:
    """列出目录下所有索引文件及其状态。"""
    results = []
    for entry in os.scandir(directory):
        if entry.is_file() and entry.name.endswith(INDEX_EXTENSION):
            index_path = entry.path
            source_path = index_path[: -len(INDEX_EXTENSION)]

            meta = read_index_metadata(index_path)
            if meta is None:
                validation = "corrupt"
            else:
                v_result = validate_index(source_path, index_path)
                validation = "valid" if v_result.is_valid else f"invalid: {v_result.reason}"

            index_size = os.path.getsize(index_path) if os.path.isfile(index_path) else 0

            results.append({
                "source_path": source_path,
                "index_path": index_path,
                "validation": validation,
                "index_size_mb": round(index_size / (1024 * 1024), 2),
                "meta": meta,
            })

    return results
