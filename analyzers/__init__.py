# analyzers/__init__.py — 并行分析器包
#
# ✅ 优化点: 四个分析器在解析完成后使用 ThreadPoolExecutor 并行执行
# 而非串行执行，大幅降低总分析时间。
#
# 分析器列表:
#   - anomaly_detector: 异常检测（向量化条件过滤）
#   - pattern_analyzer: 模式分析（正则模式匹配）
#   - timeline_analyzer: 时间线分析（时间戳提取与排序）
#   - stats_analyzer: 统计分析（Pandas 向量化统计）

from .anomaly_detector import detect_anomalies
from .pattern_analyzer import analyze_patterns
from .timeline_analyzer import analyze_timeline
from .stats_analyzer import compute_statistics

__all__ = [
    "detect_anomalies",
    "analyze_patterns",
    "analyze_timeline",
    "compute_statistics",
]
