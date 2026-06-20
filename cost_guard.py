# cost_guard.py — AI 成本守卫 & Token 预算控制器
#
# IMP-001: P1 High-Value Improvement
#
# 职责：
#   1. TokenBudget  — per-request 最大 token 硬限制（可配置，默认 32K）
#   2. DailyBudget  — 每日最大 USD 花费限制（可配置，默认 $10）
#   3. BudgetExceededError — 超额时显式抛出（非静默失败）
#   4. 剩余预算查询接口（供 UI/API 使用）
#   5. 预算状态持久化（Redis 优先，内存降级）
#   6. 多 provider 支持（不同 provider 单价不同）
#   7. 集成到 CostCalculator 现有流程
#   8. Prometheus metrics 暴露
#
# 设计原则：
#   - 硬限制 > 软警告：超额时抛出 BudgetExceededError，不静默失败
#   - 用户友好：异常包含 reset_at、used、limit、suggestion 等字段
#   - 持久化优先：Redis 存储每日累计，内存降级
#   - 零侵入：通过依赖注入集成，不修改现有调用链

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Literal

from cost_calculator import PRICING_TABLE, _DEFAULT_PRICING, CostCalculator

logger = logging.getLogger(__name__)


# ============================================================
#  自定义异常
# ============================================================

class BudgetExceededError(Exception):
    """
    预算超额异常 — 不是静默失败，而是结构化错误信息。

    属性:
        limit_type: "per_request" | "daily"
        message: 人类可读的错误信息
        reset_at: 预算重置时间（ISO 8601）
        used: 已使用量（tokens 或 USD）
        limit: 预算上限
        provider: AI provider 名称
        model: 模型名称
        suggestion: 用户可执行的建议操作
    """

    def __init__(
        self,
        limit_type: Literal["per_request", "daily"],
        message: str,
        reset_at: Optional[str] = None,
        used: float = 0.0,
        limit: float = 0.0,
        provider: str = "",
        model: str = "",
        suggestion: str = "",
    ):
        super().__init__(message)
        self.limit_type = limit_type
        self.message = message
        self.reset_at = reset_at
        self.used = used
        self.limit = limit
        self.provider = provider
        self.model = model
        self.suggestion = suggestion

    def to_dict(self) -> dict:
        """转换为用户友好的结构化错误响应"""
        return {
            "error": "budget_exceeded",
            "limit_type": self.limit_type,
            "message": self.message,
            "reset_at": self.reset_at,
            "used": round(self.used, 6),
            "limit": round(self.limit, 2),
            "provider": self.provider,
            "model": self.model,
            "suggestion": self.suggestion or "使用语义缓存中的相似分析结果",
        }

    def to_user_response(self) -> dict:
        """返回用户友好的结构化错误信息（用于 API 响应 / UI 展示）"""
        return self.to_dict()


# ============================================================
#  TokenBudget — per-request token 硬限制
# ============================================================

@dataclass
class TokenBudget:
    """
    Per-request Token 预算控制器。

    在每次 AI 调用前检查 (input_tokens + max_output_tokens) 是否
    超过 per-request 限制。超限时抛出 BudgetExceededError。

    参数:
        max_tokens: per-request 最大 token 数（默认 32K）
        max_output_tokens: 预留的输出 token 上限（默认 4K）
    """

    max_tokens: int = 32_000
    max_output_tokens: int = 4_096

    def check(
        self,
        input_tokens: int,
        provider: str = "",
        model: str = "",
    ) -> bool:
        """
        检查输入 token 数是否在 per-request 预算内。

        参数:
            input_tokens: 输入 prompt 的 token 数
            provider: AI provider 名称（用于错误信息）
            model: 模型名称（用于错误信息）

        返回:
            True 表示通过检查

        抛出:
            BudgetExceededError: 输入 token 数超过 per-request 限制
        """
        total_estimated = input_tokens + self.max_output_tokens

        if total_estimated > self.max_tokens:
            raise BudgetExceededError(
                limit_type="per_request",
                message=(
                    f"请求 Token 数 ({total_estimated:,}) 超过单次限制 "
                    f"({self.max_tokens:,})，请截取日志末尾关键部分后重试"
                ),
                used=total_estimated,
                limit=self.max_tokens,
                provider=provider,
                model=model,
                suggestion=(
                    f"当前输入约 {input_tokens:,} tokens，"
                    f"建议截取日志末尾 200-500 行（约 {self.max_tokens - self.max_output_tokens:,} tokens 以内）"
                ),
            )

        # 软警告：超过 80% 时记录日志
        ratio = total_estimated / self.max_tokens
        if ratio >= 0.8:
            logger.warning(
                "Token usage high: %d/%d (%.0f%%) for %s/%s",
                total_estimated, self.max_tokens, ratio * 100,
                provider, model,
            )

        return True

    def remaining(self, input_tokens: int) -> int:
        """返回剩余可用的 token 数"""
        return max(0, self.max_tokens - input_tokens - self.max_output_tokens)

    def get_limit(self) -> int:
        """返回 per-request token 限制"""
        return self.max_tokens


# ============================================================
#  DailyBudget — 每日 USD 花费限制
# ============================================================

class DailyBudget:
    """
    每日 USD 花费预算控制器。

    特性:
      - Redis 持久化（优先），内存降级
      - 每日零点自动重置（基于日期 key）
      - 多 provider 单价支持
      - 剩余预算查询

    Redis key 设计:
      - loggazer:budget:daily:{YYYY-MM-DD} = 当日累计成本（USD float）

    参数:
        max_daily_usd: 每日最大花费（USD），默认 $10.00
        redis_client: redis.Redis 实例，None 时使用内存降级
        cost_calculator: CostCalculator 实例（用于计算单次调用成本）
    """

    def __init__(
        self,
        max_daily_usd: float = 10.0,
        redis_client=None,
        cost_calculator: Optional[CostCalculator] = None,
    ):
        self._max_daily = max_daily_usd
        self._redis = redis_client
        self._cost_calculator = cost_calculator or CostCalculator(redis_client=redis_client)

        # 内存降级存储
        self._memory_daily: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ---- 持久化 helpers ----

    @staticmethod
    def _today_key() -> str:
        """获取当天日期 key（格式：YYYY-MM-DD）"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _reset_at_iso() -> str:
        """获取下次重置时间（明天 00:00 UTC，ISO 8601 格式）"""
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return tomorrow.isoformat() + "Z"

    def _redis_key(self) -> str:
        return f"loggazer:budget:daily:{self._today_key()}"

    # ---- 累计 & 查询 ----

    def accumulate(self, cost_usd: float) -> float:
        """
        累加当天成本并返回最新总额。

        参数:
            cost_usd: 本次调用成本（USD）

        返回:
            累加后的当天总成本
        """
        today = self._today_key()

        if self._redis is not None:
            try:
                redis_key = self._redis_key()
                self._redis.incrbyfloat(redis_key, cost_usd)
                # 设置 TTL: 到明天凌晨 + 1h buffer
                seconds_until_midnight = (
                    datetime.now(timezone.utc)
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)
                    - datetime.now(timezone.utc)
                ).total_seconds()
                self._redis.expire(redis_key, int(seconds_until_midnight) + 3600)

                total = float(self._redis.get(redis_key) or 0)
                return round(total, 6)
            except Exception as e:
                logger.warning("Redis 每日成本累加失败，降级到内存: %s", e)

        # 内存降级
        with self._lock:
            current = self._memory_daily.get(today, 0.0)
            new_total = current + cost_usd
            self._memory_daily[today] = new_total
            # 清理旧日期的数据
            stale = [k for k in self._memory_daily if k != today]
            for k in stale:
                del self._memory_daily[k]
            return round(new_total, 6)

    def get_daily_accumulated(self) -> float:
        """读取当天累计成本（USD）"""
        today = self._today_key()

        if self._redis is not None:
            try:
                total = self._redis.get(self._redis_key())
                return round(float(total or 0), 6)
            except Exception as e:
                logger.warning("Redis 每日成本读取失败，降级到内存: %s", e)

        with self._lock:
            return round(self._memory_daily.get(today, 0.0), 6)

    # ---- 预算检查 ----

    def check(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int = 0,
        provider: str = "",
    ) -> bool:
        """
        检查当前调用是否会超过每日预算。

        流程:
          1. 估算本次调用成本
          2. 累加当日成本
          3. 判断是否超限

        参数:
            model: 模型名称
            input_tokens: 输入 token 数
            output_tokens: 预估输出 token 数（默认 0，后续更新）
            provider: AI provider 名称

        返回:
            True 表示通过检查（未超限）

        抛出:
            BudgetExceededError: 当日累计已超过每日预算
        """
        # 计算本次预估成本
        estimated_cost = self._cost_calculator.calculate(
            model, input_tokens, output_tokens
        )

        # 获取当前累计
        current = self.get_daily_accumulated()
        projected = current + estimated_cost

        if projected >= self._max_daily:
            raise BudgetExceededError(
                limit_type="daily",
                message=f"今日 AI 分析配额已用完（${self._max_daily:.2f}/天）",
                reset_at=self._reset_at_iso(),
                used=current,
                limit=self._max_daily,
                provider=provider,
                model=model,
                suggestion=(
                    f"今日已使用 ${current:.2f}，本次调用预估 ${estimated_cost:.4f}。"
                    f"配额将在 {self._reset_at_iso()} 重置。"
                ),
            )

        # 80% 告警
        ratio = projected / self._max_daily
        if ratio >= 0.8:
            logger.warning(
                "Daily budget at %.0f%%: $%.4f / $%.2f (provider=%s, model=%s)",
                ratio * 100, projected, self._max_daily, provider, model,
            )

        return True

    def accumulate_actual(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        provider: str = "",
    ) -> float:
        """
        记录实际发生的 AI 调用成本（调用成功后调用）。

        参数:
            model: 模型名称
            input_tokens: 实际输入 token 数
            output_tokens: 实际输出 token 数
            provider: AI provider 名称

        返回:
            本次调用成本（USD）
        """
        cost = self._cost_calculator.calculate(model, input_tokens, output_tokens)
        self.accumulate(cost)
        logger.debug(
            "Daily cost recorded: $%.6f (total: $%.4f) for %s/%s",
            cost, self.get_daily_accumulated(), provider, model,
        )
        return cost

    # ---- 查询接口 ----

    def remaining(self) -> float:
        """返回当日剩余预算（USD）"""
        return max(0.0, round(self._max_daily - self.get_daily_accumulated(), 6))

    def get_limit(self) -> float:
        """返回每日预算上限"""
        return self._max_daily

    def get_status(self) -> dict:
        """
        获取完整预算状态（供 UI/API 使用）。

        返回:
            {
                "daily_accumulated": float,
                "daily_budget": float,
                "daily_remaining": float,
                "usage_ratio": float,
                "reset_at": str (ISO 8601),
                "status": "normal" | "warning" | "exceeded",
            }
        """
        accumulated = self.get_daily_accumulated()
        remaining = self.remaining()
        ratio = accumulated / self._max_daily if self._max_daily > 0 else 0.0

        if ratio >= 1.0:
            status = "exceeded"
        elif ratio >= 0.8:
            status = "warning"
        else:
            status = "normal"

        return {
            "daily_accumulated": round(accumulated, 6),
            "daily_budget": round(self._max_daily, 2),
            "daily_remaining": round(remaining, 6),
            "usage_ratio": round(ratio, 4),
            "reset_at": self._reset_at_iso(),
            "status": status,
        }

    def reset_daily_budget(self, new_limit: Optional[float] = None) -> None:
        """
        重置每日预算（手动重置或修改上限）。

        参数:
            new_limit: 新的每日预算上限，None 表示保持当前值
        """
        if new_limit is not None:
            self._max_daily = new_limit

        today = self._today_key()
        if self._redis is not None:
            try:
                self._redis.delete(self._redis_key())
            except Exception as e:
                logger.warning("Redis 每日预算重置失败: %s", e)

        with self._lock:
            self._memory_daily.pop(today, None)

        logger.info("Daily budget reset (new limit: $%.2f)", self._max_daily)


# ============================================================
#  CostGuard — 统一的成本守卫（TokenBudget + DailyBudget）
# ============================================================

class CostGuard:
    """
    统一成本守卫：组合 TokenBudget 和 DailyBudget。

    在 AI 调用前执行双重检查：
      1. per-request token 限制
      2. 每日 USD 花费限制

    用法:
        guard = CostGuard(
            token_budget=TokenBudget(max_tokens=32_000),
            daily_budget=DailyBudget(max_daily_usd=10.0, redis_client=redis),
        )

        # 调用前检查
        guard.check_before_call(
            model="deepseek-chat",
            provider="deepseek",
            input_tokens=5000,
            output_tokens_estimated=1000,
        )

        # 调用后记录
        guard.record_after_call(
            model="deepseek-chat",
            provider="deepseek",
            input_tokens=5000,
            output_tokens=800,
        )
    """

    def __init__(
        self,
        token_budget: Optional[TokenBudget] = None,
        daily_budget: Optional[DailyBudget] = None,
    ):
        self.token_budget = token_budget or TokenBudget(max_tokens=32_000)
        self.daily_budget = daily_budget or DailyBudget(max_daily_usd=10.0)

        # Prometheus 指标（延迟初始化）
        self._metrics_available = False
        self._init_metrics()

    def _init_metrics(self) -> None:
        """初始化 Prometheus 指标"""
        try:
            from prometheus_client import Counter, Gauge

            # API cost total (by provider, model)
            self._metric_cost_total = Counter(
                "loggazer_api_cost_usd_total",
                "Total API cost in USD",
                labelnames=["provider", "model"],
            )

            # API tokens total (by provider, model, type)
            self._metric_tokens_total = Counter(
                "loggazer_api_tokens_total",
                "Total API tokens consumed",
                labelnames=["provider", "model", "type"],
            )

            # Daily budget remaining
            self._metric_daily_remaining = Gauge(
                "loggazer_daily_budget_remaining_usd",
                "Remaining daily budget in USD",
            )

            # Budget exceeded counter
            self._metric_budget_exceeded = Counter(
                "loggazer_budget_exceeded_total",
                "Total number of budget exceeded events",
                labelnames=["limit_type"],
            )

            self._metrics_available = True
            logger.info("Cost guard Prometheus metrics initialized")
        except ImportError:
            logger.debug("prometheus_client not installed, metrics disabled")
        except Exception as e:
            logger.warning("Cost guard metrics init failed: %s", e)

    # ---- 调用前检查 ----

    def check_before_call(
        self,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens_estimated: int = 0,
    ) -> bool:
        """
        AI 调用前执行预算检查。

        检查顺序:
          1. per-request token 限制
          2. 每日 USD 花费限制

        参数:
            model: 模型名称
            provider: AI provider
            input_tokens: 输入 token 数
            output_tokens_estimated: 预估输出 token 数

        返回:
            True 表示通过所有检查

        抛出:
            BudgetExceededError: 任一检查未通过
        """
        # 1. Per-request token 检查
        self.token_budget.check(
            input_tokens=input_tokens,
            provider=provider,
            model=model,
        )

        # 2. 每日预算检查
        self.daily_budget.check(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens_estimated,
            provider=provider,
        )

        return True

    # ---- 调用后记录 ----

    def record_after_call(
        self,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        status: str = "success",
    ) -> float:
        """
        AI 调用成功后记录实际成本。

        参数:
            model: 模型名称
            provider: AI provider
            input_tokens: 实际输入 token 数
            output_tokens: 实际输出 token 数
            status: 调用状态（success/error）

        返回:
            本次调用成本（USD）
        """
        cost = self.daily_budget.accumulate_actual(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider=provider,
        )

        # 更新 Prometheus 指标
        if self._metrics_available and status == "success" and cost > 0:
            try:
                self._metric_cost_total.labels(
                    provider=provider, model=model,
                ).inc(cost)
                self._metric_tokens_total.labels(
                    provider=provider, model=model, type="input",
                ).inc(input_tokens)
                self._metric_tokens_total.labels(
                    provider=provider, model=model, type="output",
                ).inc(output_tokens)
                self._metric_daily_remaining.set(self.daily_budget.remaining())
            except Exception:
                pass

        return cost

    def record_budget_exceeded(self, limit_type: str) -> None:
        """记录预算超额事件到 Prometheus"""
        if self._metrics_available:
            try:
                self._metric_budget_exceeded.labels(
                    limit_type=limit_type,
                ).inc()
            except Exception:
                pass

    # ---- 查询接口 ----

    def get_token_remaining(self, input_tokens: int) -> int:
        """返回 per-request 剩余可用 token 数"""
        return self.token_budget.remaining(input_tokens)

    def get_daily_remaining(self) -> float:
        """返回当日剩余预算（USD）"""
        return self.daily_budget.remaining()

    def get_full_status(self) -> dict:
        """
        获取完整成本状态（供 UI/API 使用）。

        返回:
            {
                "per_request": {"max_tokens": int, "remaining": int},
                "daily": {...},
                "circuit_breaker": "normal" | "warning" | "exceeded",
            }
        """
        daily = self.daily_budget.get_status()

        return {
            "per_request": {
                "max_tokens": self.token_budget.get_limit(),
            },
            "daily": daily,
            "circuit_breaker": daily["status"],
        }


# ============================================================
#  模块级单例（供全局使用）
# ============================================================

_cost_guard_instance: Optional[CostGuard] = None
_cost_guard_lock = threading.Lock()


def get_cost_guard(
    redis_client=None,
    per_request_max_tokens: int = 32_000,
    daily_max_usd: float = 10.0,
    cost_calculator: Optional[CostCalculator] = None,
) -> CostGuard:
    """
    获取或初始化全局 CostGuard 单例。

    参数:
        redis_client: redis.Redis 实例
        per_request_max_tokens: 单次请求最大 token 数
        daily_max_usd: 每日最大 USD 花费
        cost_calculator: CostCalculator 实例

    返回:
        CostGuard 单例
    """
    global _cost_guard_instance
    if _cost_guard_instance is None:
        with _cost_guard_lock:
            if _cost_guard_instance is None:
                _cost_guard_instance = CostGuard(
                    token_budget=TokenBudget(max_tokens=per_request_max_tokens),
                    daily_budget=DailyBudget(
                        max_daily_usd=daily_max_usd,
                        redis_client=redis_client,
                        cost_calculator=cost_calculator,
                    ),
                )
    return _cost_guard_instance


def reset_cost_guard() -> None:
    """重置全局 CostGuard 单例（用于测试）"""
    global _cost_guard_instance
    with _cost_guard_lock:
        _cost_guard_instance = None
