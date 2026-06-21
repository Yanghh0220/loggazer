# api/schemas.py - API 专用 Pydantic 模型
#
# 职责：
#   1. 定义 API 层的请求/响应模型（AnalyzeRequest / AnalyzeResponse）
#   2. 定义 RFC 7807 Problem Details 错误格式
#   3. 定义 Health Check、Clusters 等端点的响应模型
#
# 与 models.py 的区别：
#   - models.py：核心领域模型（AnalysisResult, RootCause, FixSuggestion）
#   - api/schemas.py：API 传输层模型（请求验证、响应封装、元数据）
#
# 设计原则：
#   - 所有模型使用 Pydantic v2 语法（field_validator / model_validator）
#   - Field(description=...) 用于自动生成 OpenAPI 文档
#   - model_config["json_schema_extra"] 提供示例

from __future__ import annotations

from typing import Optional, Literal

from pydantic import BaseModel, Field, field_validator

from models import AnalysisResult


# ============================================================
#  Problem Details (RFC 7807)
# ============================================================

class ProblemDetail(BaseModel):
    """RFC 7807 Problem Details for HTTP APIs"""

    type: str = Field(
        default="about:blank",
        description="A URI reference that identifies the problem type",
    )
    title: str = Field(
        ...,
        description="A short, human-readable summary of the problem",
    )
    status: int = Field(
        ...,
        description="The HTTP status code",
    )
    detail: str = Field(
        ...,
        description="A human-readable explanation specific to this occurrence",
    )
    instance: Optional[str] = Field(
        None,
        description="A URI reference that identifies the specific occurrence",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "type": "https://loggazer.dev/errors/validation-error",
                "title": "Validation Error",
                "status": 422,
                "detail": "log_text cannot be only whitespace",
                "instance": "/v1/analyze",
            }]
        }
    }


# ============================================================
#  AnalyzeRequest / AnalyzeResponse
# ============================================================

class AnalyzeRequest(BaseModel):
    """POST /v1/analyze request body"""

    log_text: str = Field(
        ...,
        min_length=10,
        max_length=100000,
        description="Complete build failure log text (plain text)",
    )
    platform_hint: Optional[str] = Field(
        None,
        description="Optional platform hint, e.g. 'npm', 'docker', 'pytest'. "
                    "Auto-detect if omitted.",
    )
    include_rag: bool = Field(
        True,
        description="Enable RAG historical case augmentation",
    )
    cache_policy: Literal["auto", "force_refresh", "cache_only"] = Field(
        "auto",
        description="Cache strategy: auto=use cache if available, "
                    "force_refresh=skip cache, cache_only=only return cached",
    )

    @field_validator("log_text")
    @classmethod
    def not_only_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("log_text cannot be only whitespace")
        return v

    @field_validator("platform_hint")
    @classmethod
    def validate_platform(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            return None
        return v


class AnalyzeResponseMeta(BaseModel):
    """Metadata about the analysis execution"""

    duration_ms: float = Field(
        ...,
        description="Total analysis time in milliseconds",
    )
    cache_status: Literal["hit", "miss", "rag", "disabled"] = Field(
        ...,
        description="Cache layer result",
    )
    model_used: str = Field(
        ...,
        description="AI model name (e.g. deepseek-chat)",
    )
    cost_usd: float = Field(
        0.0,
        description="Estimated cost in USD",
    )
    platform_detected: str = Field(
        ...,
        description="Auto-detected platform",
    )


class AnalyzeResponse(BaseModel):
    """POST /v1/analyze response"""

    result: AnalysisResult
    meta: AnalyzeResponseMeta
    request_id: str = Field(
        ...,
        description="OpenTelemetry trace_id for end-to-end correlation",
    )


# ============================================================
#  Health Check
# ============================================================

class HealthResponse(BaseModel):
    """GET /v1/health response"""

    status: Literal["healthy", "degraded", "unhealthy"] = Field(
        ...,
        description="Overall health status",
    )
    version: str = Field("1.1.0")
    checks: dict = Field(
        ...,
        description="Individual component health checks",
    )
    uptime_seconds: float = Field(
        ...,
        description="Server uptime in seconds",
    )


# ============================================================
#  Clusters / Insights
# ============================================================

class ClusterItem(BaseModel):
    """Single cluster insight item"""

    cluster_id: int
    occurrence_count: int
    first_seen: str
    last_seen: str
    platform_distribution: dict
    avg_severity_score: float
    is_active: bool


class ClustersResponse(BaseModel):
    """GET /v1/clusters response"""

    clusters: list[ClusterItem]
    total: int


# ============================================================
#  Rate Limit
# ============================================================

class RateLimitHeaders(BaseModel):
    """Rate limit information returned in response headers"""

    limit: int = Field(..., description="Maximum requests per window")
    remaining: int = Field(..., description="Remaining requests in current window")
    retry_after: int = Field(0, description="Seconds to wait before retry if limited")


# ============================================================
#  Index Endpoints (v1.0 — 日志索引机制)
# ============================================================


class IndexBuildRequest(BaseModel):
    """POST /v1/index/build request body"""

    file_path: str = Field(
        ...,
        min_length=1,
        description="Absolute path to the log file to index",
    )
    force_rebuild: bool = Field(
        False,
        description="Force rebuild even if a valid index exists",
    )


class IndexStatusResponse(BaseModel):
    """GET /v1/index/status response"""

    file_path: str
    has_index: bool
    is_valid: bool
    validation_msg: str
    stats: dict = Field(default_factory=dict)


class IndexBuildResponse(BaseModel):
    """POST /v1/index/build response"""

    status: str = Field(..., description="index_hit | index_built | index_rebuilt | error")
    index_path: str | None = None
    validation: str = ""
    stats: dict = Field(default_factory=dict)
    source_path: str


class IndexListResponse(BaseModel):
    """GET /v1/index/list response"""

    indices: list[dict] = Field(default_factory=list)
    count: int = 0
