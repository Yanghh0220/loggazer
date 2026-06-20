# analyzers/pattern_analyzer.py — 模式分析器
#
# ✅ 优化点: 使用预编译正则进行批量模式匹配
# ✅ 提取错误模式、重复模式、依赖关系等结构化信息

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# ✅ 优化点: 预编译所有模式匹配正则
_RE_VERSION_CONFLICT = re.compile(
    r"(?P<package>[\w\-\.]+)\s*@?\s*(?P<expected>\d+\.\d+\.\d+).*?(?:requires|depends|peer).*?"
    r"(?P<actual>[\w\-\.]+)\s*@?\s*(?P<actual_version>\d+\.\d+\.\d+)",
    re.IGNORECASE,
)

_RE_DEPENDENCY_ERROR = re.compile(
    r"(?:(?:Could not find|No matching|Unable to resolve|Failed to download)\s+"
    r"(?P<dependency>[\w\-\.\[\]=<>,;\s]+))",
    re.IGNORECASE,
)

_RE_BUILD_ERROR = re.compile(
    r"(?P<build>(?:Build|Compilation|Linking)\s+(?:failed|error|failure)"
    r"|(?:error[:\[][A-Z]+\d+)"
    r"|(?:undefined\s+reference\s+to))",
    re.IGNORECASE,
)

_RE_TEST_FAILURE = re.compile(
    r"(?P<test>(?:FAILED|FAILURES|ERRORS)\s*$"
    r"|(?:Tests?\s+(?:failed|run):\s*\d+)"
    r"|(?:\d+\s+(?:failed|passed|error)\b)"
    r"|(?:assert\s+.*\s*==\s*))",
    re.IGNORECASE,
)

_RE_NETWORK_ERROR = re.compile(
    r"(?P<network>(?:Connection\s+(?:refused|reset|timed?\s*out))"
    r"|(?:DNS\s+(?:resolution|lookup)\s+failed)"
    r"|(?:Network\s+(?:unreachable|error))"
    r"|(?:ECONNREFUSED|ETIMEDOUT|ENOTFOUND))",
    re.IGNORECASE,
)

_RE_PERMISSION_ERROR = re.compile(
    r"(?P<permission>(?:Permission\s+denied|EACCES|E?PERM)"
    r"|(?:(?:access|permission)\s+(?:denied|forbidden|restricted))"
    r"|(?:(?:cannot|unable\s+to)\s+(?:access|open|write|create)))",
    re.IGNORECASE,
)

_RE_FILE_PATH = re.compile(
    r"(?:(?:[/\\][\w\-\.]+)+[/\\]?[\w\-\.]+)"
    r"|(?:[\w\-\.]+\.(?:py|js|ts|java|go|rb|rs|cpp|c|h|sh|yaml|yml|json|toml|xml))",
)


def analyze_patterns(
    log_text: str,
    error_lines: list[str] | None = None,
) -> dict[str, Any]:
    """
    分析日志中的错误模式。

    ✅ 优化点: 使用预编译正则进行批量模式分类。

    参数:
        log_text: 原始日志文本
        error_lines: 预提取的错误行（可选）

    返回:
        模式分析结果字典:
        {
            "error_categories": dict,
            "version_conflicts": list[dict],
            "dependency_errors": list[str],
            "file_paths_mentioned": list[str],
            "repeated_errors": list[dict],
            "error_chains": list[list[str]],
        }
    """
    lines = error_lines if error_lines else log_text.splitlines()

    # ✅ 优化点: 分类计数器
    categories: dict[str, int] = defaultdict(int)
    version_conflicts: list[dict] = []
    dependency_errors: list[str] = []
    file_paths: set[str] = set()
    repeated_error_count: dict[str, int] = defaultdict(int)

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # 分类匹配
        if _RE_VERSION_CONFLICT.search(line_stripped):
            categories["version_conflict"] += 1
            m = _RE_VERSION_CONFLICT.search(line_stripped)
            if m:
                version_conflicts.append({
                    "package": m.group("package") or "unknown",
                    "expected": m.group("expected") or "unknown",
                    "actual_package": m.group("actual") or "unknown",
                    "actual_version": m.group("actual_version") or "unknown",
                })
        elif _RE_DEPENDENCY_ERROR.search(line_stripped):
            categories["dependency_error"] += 1
            m = _RE_DEPENDENCY_ERROR.search(line_stripped)
            if m:
                dep = m.group("dependency") or line_stripped[:150]
                dependency_errors.append(dep)
        elif _RE_BUILD_ERROR.search(line_stripped):
            categories["build_error"] += 1
        elif _RE_TEST_FAILURE.search(line_stripped):
            categories["test_failure"] += 1
        elif _RE_NETWORK_ERROR.search(line_stripped):
            categories["network_error"] += 1
        elif _RE_PERMISSION_ERROR.search(line_stripped):
            categories["permission_error"] += 1
        elif "error" in line_stripped.lower():
            categories["unknown_error"] += 1
        elif "warn" in line_stripped.lower():
            categories["warning"] += 1
        else:
            categories["info"] += 1

        # 提取文件路径
        for m in _RE_FILE_PATH.finditer(line_stripped):
            file_paths.add(m.group())

        # 统计重复错误（前 80 个字符作为签名）
        error_sig = line_stripped[:80]
        repeated_error_count[error_sig] += 1

    # 去重依赖错误
    unique_dep_errors = list(dict.fromkeys(dependency_errors))[:20]

    # 重复错误（出现 >= 2 次）
    repeated = [
        {"signature": sig, "count": count}
        for sig, count in repeated_error_count.items()
        if count >= 2
    ]
    repeated.sort(key=lambda x: -x["count"])
    repeated = repeated[:20]

    return {
        "error_categories": dict(categories),
        "version_conflicts": version_conflicts[:10],
        "dependency_errors": unique_dep_errors[:20],
        "file_paths_mentioned": sorted(file_paths)[:30],
        "repeated_errors": repeated,
        "total_categories": len(categories),
    }
