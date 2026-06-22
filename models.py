# models.py - 结构化数据模型（Pydantic v2）
#
# v2.0 — 性能优化更新 (2026-06-20)
# ✅ 新增: TaskStatus / AnalysisStatistics / AnomalyResult 等异步任务和并行分析模型
#
# 职责：
# 1. 定义 AI 分析结果的强类型 Schema（运行时校验 + IDE 补全）
# 2. 通过 Field(description=...) 注入 LLM System Prompt 作为 Schema 约束
# 3. 通过 field_validator / model_validator 执行字段级安全校验
# 4. 导出 JSON Schema 供 API 文档或前端表单生成

from __future__ import annotations

import re
import shlex
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================
#  命令安全校验（静态分析，不执行命令）
# ============================================================

# 危险命令模式黑名单（正则匹配）
# 仅做静态语法和模式分析，绝不执行命令（subprocess.run / eval / exec 禁止）
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+(-[a-zA-Z]*\s+)*--no-preserve-root\s+/", re.IGNORECASE),
    re.compile(r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*)\s+/", re.IGNORECASE),
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"mkfs\.\w+\s+/dev/\w+", re.IGNORECASE),
    re.compile(r"dd\s+if=/dev/(zero|urandom|random)\s+of=/dev/\w+", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*(ba)?sh", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*python[23]?", re.IGNORECASE),   # curl | python
    re.compile(r":\(\)\{.*\}", re.IGNORECASE),  # Fork bomb
    re.compile(r"chmod\s+-R\s+777\s+/", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),  # 覆写磁盘设备
    re.compile(r"mv\s+/\s+", re.IGNORECASE),  # mv / ...
    re.compile(r"sudo\s+rm\b", re.IGNORECASE),  # sudo rm（严格拦截）
]

# 需要审核（review）的命令模式 — 允许但需标记
_REVIEW_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bsudo\b", re.IGNORECASE),            # 任何 sudo 命令
    re.compile(r"docker\s+system\s+prune", re.IGNORECASE),  # docker system prune
    re.compile(r"kill\s+-9\b", re.IGNORECASE),         # kill -9
    re.compile(r"docker\s+rm\s+-f", re.IGNORECASE),    # docker rm -f
    re.compile(r"kubectl\s+delete\b", re.IGNORECASE),  # kubectl delete
    re.compile(r"git\s+push\s+--force", re.IGNORECASE),  # git push --force
]


def validate_command_safety(command: str) -> str:
    """
    命令级安全校验：语法解析 + 黑名单 + 语义分析

    校验流程：
    1. shlex.split() 语法解析（捕获未闭合引号等语法错误）
    2. 危险模式黑名单正则匹配

    参数:
        command: 待校验的 bash 命令字符串

    返回:
        原始命令字符串（校验通过时）

    异常:
        ValueError: 语法错误或匹配危险模式时
    """
    if not command or not command.strip():
        raise ValueError("命令不能为空")

    # 1. shlex 语法校验
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise ValueError(f"无效的 shell 语法: {e}")

    if not tokens:
        raise ValueError("命令解析后为空")

    # 2. 危险模式黑名单
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            raise ValueError(
                f"检测到危险命令模式: {command[:60]}..."
            )

    return command


# ============================================================
#  Pydantic 数据模型
# ============================================================

class RootCause(BaseModel):
    """单个根因分析"""

    description: str = Field(
        ...,
        max_length=200,
        description="根因描述，具体且可操作",
    )
    probability: int = Field(
        ...,
        ge=0,
        le=100,
        description="可能性百分比（0-100）",
    )


class FixSuggestion(BaseModel):
    """单条修复建议 — v3.0 增强版（riskLevel + recommended + command 可选）"""

    title: str = Field(
        ...,
        max_length=60,
        description="修复方案标题（简洁，不超过30字）",
    )
    description: str = Field(
        ...,
        max_length=400,
        description="详细说明（一句话讲清楚为什么）",
    )
    command: str = Field(
        default="",
        description="可选，具体可执行的命令",
    )
    riskLevel: Literal["safe", "warning", "danger"] = Field(
        default="safe",
        description="风险等级：safe=安全 / warning=谨慎 / danger=高风险",
    )
    riskLabel: str = Field(
        default="安全",
        description="风险标签中文：安全 / 谨慎 / 高风险",
    )
    recommended: bool = Field(
        default=False,
        description="是否为推荐方案",
    )
    # ---- 向后兼容：保留旧字段 safety_level（旧版调用方仍在使用）----
    safety_level: Literal["safe", "review", "dangerous"] = Field(
        default="safe",
        description="[DEPRECATED] 命令安全等级，请使用 riskLevel",
    )

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        """命令级安全校验：语法解析 + 黑名单（空命令跳过校验）"""
        if not v or not v.strip():
            return v  # 空命令是合法的，跳过校验
        return validate_command_safety(v)

    @model_validator(mode="after")
    def auto_mark_risk_level(self) -> "FixSuggestion":
        """
        自动标记风险等级

        1. 如果命令匹配 _REVIEW_PATTERNS（sudo / docker system prune / kill -9 等），
           且 riskLevel 为 safe → 自动升级为 warning
        2. 同步 riskLabel 与 riskLevel
        3. 同步旧字段 safety_level 与 riskLevel
        """
        # 命令风险自动检测
        if self.command and self.command.strip():
            for pattern in _REVIEW_PATTERNS:
                if pattern.search(self.command):
                    if self.riskLevel == "safe":
                        object.__setattr__(self, "riskLevel", "warning")
                    break

        # 同步 riskLabel
        label_map = {"safe": "安全", "warning": "谨慎", "danger": "高风险"}
        if not self.riskLabel or self.riskLabel not in label_map.values():
            object.__setattr__(self, "riskLabel", label_map.get(self.riskLevel, "安全"))

        # 向后兼容：同步旧字段 safety_level
        compat_map = {"safe": "safe", "warning": "review", "danger": "dangerous"}
        object.__setattr__(self, "safety_level", compat_map.get(self.riskLevel, "safe"))

        return self

    # ---- 向后兼容：支持 dict-style 访问 ----
    # app.py 和 cache_engine.py 使用 result.get("key") 和 s.get("key")
    # 提供 __getitem__ / get 方法桥接，避免一次性重写所有访问点

    def __getitem__(self, key: str) -> Any:
        # 兼容旧字段名 "safety_level" → 返回旧格式值
        if key == "safety_level":
            compat = {"safe": "safe", "warning": "review", "danger": "dangerous"}
            return compat.get(self.riskLevel, "safe")
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (AttributeError, KeyError):
            return default


class AnalysisResult(BaseModel):
    """
    AI 分析日志后的结构化返回结果

    这就是 analyze_log() 返回值的类型。
    通过 model_json_schema() 可导出 JSON Schema 用于 API 文档或前端表单。
    """

    error_summary: str = Field(
        ...,
        max_length=50,
        description="一句话错误摘要，<=50字",
    )
    error_detail: str = Field(
        ...,
        description="关键错误信息原文（保留英文）",
    )
    root_causes: list[RootCause] = Field(
        ...,
        min_length=1,
        max_length=5,
        description="2-5个根因分析，概率之和必须=100",
    )
    fix_suggestions: list[FixSuggestion] = Field(
        ...,
        min_length=1,
        max_length=4,
        description="1-4 条修复建议，AI 要求至少 2 条，至少标记一个 recommended: true",
    )
    debug_commands: list[str] = Field(
        ...,
        max_length=5,
        description="排查命令列表（有效 bash 命令）",
    )
    severity: Literal["low", "medium", "high", "critical"] = Field(
        ...,
        description="严重程度：low/medium/high/critical",
    )
    prevention: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="预防建议列表",
    )
    security_warning: str = Field(
        default="",
        description="当安全校验失败时附加的警告信息",
    )

    # 兼容旧字段名 reason → root_causes
    # app.py 中 result.get("reason") 仍需工作
    _REASON_ALIAS = True

    @model_validator(mode="after")
    def check_probabilities_sum(self) -> "AnalysisResult":
        """根因概率之和必须严格等于 100"""
        if self.root_causes:
            total = sum(c.probability for c in self.root_causes)
            if total != 100:
                raise ValueError(
                    f"根因概率之和必须等于 100，当前为 {total}"
                )
        return self

    @model_validator(mode="after")
    def check_debug_commands_valid(self) -> "AnalysisResult":
        """排查命令必须是有效 shell 语法"""
        for cmd in self.debug_commands:
            try:
                shlex.split(cmd)
            except ValueError as e:
                raise ValueError(
                    f"无效的排查命令: {cmd[:50]}... 错误: {e}"
                )
        return self

    # ---- 向后兼容：支持 dict-style 访问 ----
    # app.py 使用 result.get("error_summary", "无") 等方式访问
    # 提供 __getitem__ / get 方法桥接

    def __getitem__(self, key: str) -> Any:
        # 兼容旧字段名 "reason" → root_causes 的摘要文本
        if key == "reason":
            causes = self.root_causes
            if causes:
                return "；".join(
                    f"{c.description}（{c.probability}%）"
                    for c in causes
                )
            return ""
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (AttributeError, KeyError):
            return default


# ============================================================
#  ParsedLog — 日志预处理结果（BaseModel）
# ============================================================
# 从 TypedDict 升级为 BaseModel，获得运行时校验和更好的 IDE 支持


class ParsedLog(BaseModel):
    """
    日志预处理的结果

    这就是 parse_log() 返回值的类型。
    """

    platform: str = Field(
        default="Unknown",
        description="识别出的平台，如 npm、GitHub Actions",
    )
    error_lines: list[str] = Field(
        default_factory=list,
        description="提取的关键错误行",
    )
    truncated_log: str = Field(
        default="",
        description="截断后的日志文本",
    )
    is_truncated: bool = Field(
        default=False,
        description="是否进行了截断",
    )

    # ---- 向后兼容：支持 dict-style 访问 ----
    # log_parser.py 历史上返回 dict，现改为 BaseModel
    # 提供 __getitem__ / get 方法桥接 dict-style 访问

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


# ============================================================
# ✅ v2.0 优化点: 异步任务模型 — TaskStatus
# ============================================================
# 用于 GET /api/task/<task_id> 的响应，告知前端分析进度


class TaskStatus(BaseModel):
    """异步分析任务的状态模型"""

    task_id: str = Field(..., description="任务唯一标识符 (UUID v4)")
    status: str = Field(
        "pending",
        description="任务状态: pending | parsing | analyzing | completed | failed",
    )
    progress: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="进度百分比 (0.0 - 1.0)",
    )
    result: dict | None = Field(
        None,
        description="分析结果（仅 status=completed 时有值）",
    )
    error: str | None = Field(
        None,
        description="错误信息（仅 status=failed 时有值）",
    )
    filename: str | None = Field(
        None,
        description="上传的文件名",
    )
    duration_ms: float = Field(
        0.0,
        description="总耗时（毫秒）",
    )
    created_at: float = Field(
        0.0,
        description="任务创建时间（Unix timestamp）",
    )


# ============================================================
# ✅ v2.0 优化点: 并行分析结果模型
# ============================================================


class LogStatistics(BaseModel):
    """统计分析结果"""
    total_lines: int = 0
    log_level_distribution: dict = Field(default_factory=dict)
    error_density: float = 0.0
    error_count: int = 0
    warning_count: int = 0
    fatal_count: int = 0
    top_error_patterns: list[dict] = Field(default_factory=list)
    avg_line_length: float = 0.0
    max_line_length: int = 0
    lines_with_stacktrace: int = 0
    unique_error_messages: int = 0


class AnomalyResult(BaseModel):
    """异常检测结果"""
    anomalies: list[dict] = Field(default_factory=list)
    severity_distribution: dict = Field(default_factory=dict)
    burst_detected: bool = False
    anomaly_density: float = 0.0
    high_severity_count: int = 0
    total_anomalies: int = 0


class PatternResult(BaseModel):
    """模式分析结果"""
    error_categories: dict = Field(default_factory=dict)
    version_conflicts: list[dict] = Field(default_factory=list)
    dependency_errors: list[str] = Field(default_factory=list)
    file_paths_mentioned: list[str] = Field(default_factory=list)
    repeated_errors: list[dict] = Field(default_factory=list)
    total_categories: int = 0


class TimelineResult(BaseModel):
    """时间线分析结果"""
    total_timestamps_found: int = 0
    timestamp_coverage: float = 0.0
    time_range: dict = Field(default_factory=dict)
    timeline_anomalies: list[dict] = Field(default_factory=list)
    duration_stats: dict = Field(default_factory=dict)
    event_density: list[dict] = Field(default_factory=list)


class ParallelAnalysisResult(BaseModel):
    """并行分析器聚合结果"""
    statistics: LogStatistics | None = None
    anomalies: AnomalyResult | None = None
    patterns: PatternResult | None = None
    timeline: TimelineResult | None = None
