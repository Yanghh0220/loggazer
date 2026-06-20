# analyzers/timeline_analyzer.py — 时间线分析器
#
# ✅ 优化点: 提取日志时间戳并构建事件时间线
# ✅ 使用向量化操作检测时间异常（突发、间隔异常等）

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

# ✅ 优化点: 预编译时间戳匹配正则
_RE_TIMESTAMP = re.compile(
    r"(?P<iso8601>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"|(?P<syslog>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
    r"|(?P<simple_date>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"
    r"|(?P<unix_ts>\b1[3-9]\d{8}\d{1,3}\b)",
    re.IGNORECASE,
)

_RE_DURATION = re.compile(
    r"(?P<duration>(?:took|spent|duration|elapsed|completed\sin|finished\sin)\s+"
    r"(?:(?P<hours>\d+)\s*(?:h|hour|hours))?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:m|min|minutes?))?\s*"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec|seconds?))?)",
    re.IGNORECASE,
)

# 月份名称映射
_MONTH_MAP: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_timestamp(ts_str: str) -> datetime | None:
    """解析各种格式的时间戳字符串。"""
    ts_str = ts_str.strip()

    # ISO 8601: 2024-01-15T14:30:00Z or 2024-01-15 14:30:00
    iso_match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", ts_str
    )
    if iso_match:
        parts = iso_match.groups()
        try:
            return datetime(
                int(parts[0]), int(parts[1]), int(parts[2]),
                int(parts[3]), int(parts[4]), int(parts[5]),
            )
        except ValueError:
            return None

    # Syslog: Jan 15 14:30:00
    syslog_match = re.match(r"^(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})", ts_str)
    if syslog_match:
        month_str, day, hour, minute, second = syslog_match.groups()
        month = _MONTH_MAP.get(month_str[:3].lower())
        if month:
            try:
                return datetime(2024, month, int(day), int(hour), int(minute), int(second))
            except ValueError:
                return None

    # Simple date: 2024/01/15 14:30:00
    simple_match = re.match(r"^(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", ts_str)
    if simple_match:
        parts = simple_match.groups()
        try:
            return datetime(
                int(parts[0]), int(parts[1]), int(parts[2]),
                int(parts[3]), int(parts[4]), int(parts[5]),
            )
        except ValueError:
            return None

    # Unix timestamp (milliseconds)
    unix_match = re.match(r"^(\d{10,13})$", ts_str)
    if unix_match:
        ts_int = int(unix_match.group(1))
        if ts_int > 1e12:  # milliseconds
            ts_int //= 1000
        try:
            return datetime.fromtimestamp(ts_int)
        except (ValueError, OSError):
            return None

    return None


def _parse_duration(text: str) -> float | None:
    """从文本中解析持续时间（返回秒数）。"""
    m = _RE_DURATION.search(text)
    if not m:
        return None

    total = 0.0
    if m.group("hours"):
        total += int(m.group("hours")) * 3600
    if m.group("minutes"):
        total += int(m.group("minutes")) * 60
    if m.group("seconds"):
        total += float(m.group("seconds"))
    return total if total > 0 else None


def analyze_timeline(
    log_text: str,
    error_lines: list[str] | None = None,
) -> dict[str, Any]:
    """
    分析日志的时间线。

    ✅ 优化点:
      - 预编译正则提取时间戳
      - 向量化检测时间异常

    参数:
        log_text: 原始日志文本
        error_lines: 预提取的错误行（可选）

    返回:
        时间线分析结果字典
    """
    lines = log_text.splitlines()
    total_lines = len(lines)

    # 提取所有时间戳
    timestamps: list[dict] = []
    durations: list[float] = []

    for i, line in enumerate(lines):
        ts_match = _RE_TIMESTAMP.search(line)
        if ts_match:
            ts_str = ts_match.group()
            parsed = _parse_timestamp(ts_str)
            if parsed:
                timestamps.append({
                    "timestamp": parsed.isoformat(),
                    "line_number": i + 1,
                    "raw_line": line.strip()[:150],
                })

        dur = _parse_duration(line)
        if dur is not None:
            durations.append(dur)

    # ✅ 优化点: 使用 Pandas 向量化分析时间间隔
    timeline_anomalies: list[dict] = []

    if len(timestamps) >= 2:
        ts_list = [datetime.fromisoformat(t["timestamp"]) for t in timestamps]
        ts_series = pd.Series(ts_list)

        # 计算时间间隔
        intervals = ts_series.diff().dropna()
        intervals_seconds = intervals.dt.total_seconds()

        if len(intervals_seconds) > 0:
            mean_interval = float(intervals_seconds.mean())
            std_interval = float(intervals_seconds.std()) if len(intervals_seconds) > 1 else 0.0

            # ✅ 优化点: 检测异常间隔（>3σ）
            threshold = mean_interval + 3 * std_interval if std_interval > 0 else mean_interval * 5
            anomaly_indices = intervals_seconds[intervals_seconds > threshold].index

            for idx in anomaly_indices:
                if idx < len(timestamps):
                    timeline_anomalies.append({
                        "type": "time_gap",
                        "description": (
                            f"异常时间间隔: {intervals_seconds.loc[idx]:.1f}s "
                            f"(平均: {mean_interval:.1f}s)"
                        ),
                        "at_line": timestamps[idx]["line_number"],
                        "timestamp": timestamps[idx]["timestamp"],
                    })

            # ✅ 优化点: 检测时间倒退
            if (intervals_seconds < 0).any():
                neg_count = int((intervals_seconds < 0).sum())
                timeline_anomalies.append({
                    "type": "time_regression",
                    "description": f"检测到 {neg_count} 处时间倒退",
                })

    # 持续时间统计
    duration_stats = {}
    if durations:
        duration_series = pd.Series(durations)
        duration_stats = {
            "count": int(len(durations)),
            "total_seconds": round(float(duration_series.sum()), 1),
            "avg_seconds": round(float(duration_series.mean()), 2),
            "max_seconds": round(float(duration_series.max()), 2),
            "min_seconds": round(float(duration_series.min()), 2),
        }

    # 时间范围
    time_range = {}
    if timestamps:
        first_ts = timestamps[0]["timestamp"]
        last_ts = timestamps[-1]["timestamp"]
        time_range = {
            "first_event": first_ts,
            "last_event": last_ts,
        }
        try:
            delta = datetime.fromisoformat(last_ts) - datetime.fromisoformat(first_ts)
            time_range["span_seconds"] = delta.total_seconds()
        except (ValueError, TypeError):
            pass

    # 按分钟聚合事件密度
    event_density: list[dict] = []
    if timestamps:
        ts_df = pd.DataFrame([
            {"minute": datetime.fromisoformat(t["timestamp"]).strftime("%Y-%m-%d %H:%M")}
            for t in timestamps
        ])
        density = ts_df["minute"].value_counts().sort_index()
        # Top-20 最高密度时间段
        event_density = [
            {"minute": k, "count": int(v)}
            for k, v in density.nlargest(20).items()
        ]

    return {
        "total_timestamps_found": len(timestamps),
        "timestamp_coverage": round(len(timestamps) / max(total_lines, 1), 4),
        "time_range": time_range,
        "timeline_anomalies": timeline_anomalies[:20],
        "duration_stats": duration_stats,
        "event_density": event_density,
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
    }
