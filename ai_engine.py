# ai_engine.py - AI 调用引擎（结构化生成 + 降级路径）
#
# 职责：
#   1. 封装所有 AI API 的调用逻辑（OpenAI 兼容 / Claude）
#   2. 通过 Instructor 实现结构化生成（强制 AI 按 Schema 输出 JSON）
#   3. 统一处理异常（认证失败、频率超限、余额不足等）
#   4. 自动重试网络错误（指数退避）
#   5. 降级路径：Instructor 失败 → legacy 字符串解析 → 最小安全默认值
#
# 重试层级设计：
#   网络层：指数退避重试 3 次（1s → 2s → 4s）—— @_retry 装饰器
#   模式层：Instructor 自动重试 3 次（输出不符合 Schema 时）—— max_retries
#   校验层：Pydantic ValidationError → 提示词修正重试 2 次
#   降级层：所有重试耗尽 → call_ai_legacy() + _best_effort_parse_to_model()

import json
import os
import re
import time
import logging
import contextlib
import functools
from typing import Optional, Callable, Any

import requests

from config import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_TEMPERATURE,
    AI_PROVIDER,
    AI_TEMPERATURE,
    CLAUDE_API_KEY,
    CLAUDE_MODEL,
)
from cost_calculator import CostCalculator
from cost_guard import BudgetExceededError, get_cost_guard, CostGuard
from utils.performance import timer


# ============================================================
#  可观测性集成（延迟加载，避免循环依赖）
# ============================================================

_observability_instance = None


def _get_observability():
    """获取全局 ObservabilityManager 实例（延迟加载）"""
    global _observability_instance
    if _observability_instance is None:
        try:
            from observability import ObservabilityManager
            _observability_instance = ObservabilityManager()
        except Exception:
            pass
    return _observability_instance


def set_observability(obs):
    """注入 ObservabilityManager 实例（由 app.py 调用）"""
    global _observability_instance
    _observability_instance = obs


# ============================================================
#  CostGuard 集成（延迟加载）
# ============================================================

_cost_guard_instance = None


def _get_cost_guard() -> Optional[CostGuard]:
    """获取全局 CostGuard 实例（延迟加载）"""
    global _cost_guard_instance
    if _cost_guard_instance is None:
        try:
            # 尝试获取已存在的实例，或创建新的
            obs = _get_observability()
            # 使用与 observability 相同的 redis 连接
            redis_client = obs._redis if obs else None
            _cost_guard_instance = get_cost_guard(
                redis_client=redis_client,
                per_request_max_tokens=32_000,
                daily_max_usd=10.0,
                cost_calculator=CostCalculator(redis_client=redis_client),
            )
        except Exception as e:
            logger.debug("CostGuard 初始化延迟: %s", e)
            # 返回 None，调用方做降级处理
    return _cost_guard_instance


def set_cost_guard(guard: CostGuard) -> None:
    """注入 CostGuard 实例（由 app.py 调用）"""
    global _cost_guard_instance
    _cost_guard_instance = guard


def _classify_error(exception: Exception) -> str:
    """将异常分类为 error_type 标签值"""
    if isinstance(exception, AuthError):
        return "auth"
    elif isinstance(exception, RateLimitError):
        return "rate_limit"
    elif isinstance(exception, QuotaError):
        return "quota"
    elif isinstance(exception, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return "network"
    elif isinstance(exception, (json.JSONDecodeError, KeyError, ValueError)):
        return "parse"
    else:
        return "network"


@contextlib.contextmanager
def _null_context():
    """空上下文管理器（当 ObservabilityManager 不可用时使用）"""
    yield None


# ============================================================
#  日志配置
# ============================================================

logger = logging.getLogger("LogGazer")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)


# ============================================================
#  自定义异常类
# ============================================================

class APIError(Exception):
    """所有 API 异常的基类"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AuthError(APIError):
    """认证失败 — API Key 无效或已过期"""
    pass


class RateLimitError(APIError):
    """请求频率超限 — 调用太频繁了"""
    pass


class QuotaError(APIError):
    """余额不足 — API 账户没钱了"""
    pass


# ============================================================
#  重试装饰器（指数退避）
# ============================================================

def _retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
) -> Callable:
    """
    带指数退避的重试装饰器

    只对网络问题重试，认证/余额问题不重试。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            current_delay: float = delay

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (AuthError, RateLimitError, QuotaError):
                    raise
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"第 {attempt + 1} 次调用失败（{type(e).__name__}），"
                            f"{current_delay}秒后重试..."
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                        continue
                except Exception:
                    raise

            raise APIError(
                f"网络请求失败，已重试 {max_attempts} 次：{last_exception}",
                status_code=None,
            )

        return wrapper
    return decorator


# ============================================================
#  HTTP 错误解析
# ============================================================

def _parse_http_error(response: requests.Response) -> APIError:
    """根据 HTTP 响应状态码返回对应的自定义异常实例"""
    status_code: int = response.status_code
    body_text: str = response.text[:500]

    if status_code == 401:
        return AuthError(message="API Key 无效或已过期", status_code=401)

    if status_code == 429:
        body_lower = body_text.lower()
        if "quota" in body_lower or "insufficient" in body_lower:
            return QuotaError(message="账户余额不足", status_code=429)
        return RateLimitError(message="请求频率超限，请稍后再试", status_code=429)

    if status_code >= 500:
        return APIError(message=f"服务端错误（{status_code}）：{body_text}", status_code=status_code)

    return APIError(message=f"请求失败（{status_code}）：{body_text}", status_code=status_code)


# ============================================================
#  OpenAI 兼容接口调用（底层传输）
# ============================================================

@_retry(max_attempts=3, delay=1.0, backoff=2.0)
def _call_openai_compatible(
    system_prompt: str,
    user_prompt: str,
    temperature: Optional[float] = None,
) -> str:
    """
    调用 OpenAI 兼容接口（DeepSeek / Moonshot / 智谱等）

    使用 requests 直接发送 HTTP 请求，不依赖 openai SDK。
    """
    logger.info(f"调用 OpenAI 兼容接口，模型：{DEEPSEEK_MODEL}")

    obs = _get_observability()
    effective_temp = temperature if temperature is not None else AI_TEMPERATURE

    # ---- IMP-001: 预算检查 (pre-call) ----
    guard = _get_cost_guard()
    input_estimated = CostCalculator.estimate_tokens(system_prompt + user_prompt)
    if guard:
        try:
            guard.check_before_call(
                model=DEEPSEEK_MODEL,
                provider="deepseek",
                input_tokens=input_estimated,
                output_tokens_estimated=4096,
            )
        except BudgetExceededError as bee:
            logger.warning("预算检查未通过: %s", bee.message)
            guard.record_budget_exceeded(bee.limit_type)
            raise

    with obs.trace_ai_call(
        provider="deepseek",
        model=DEEPSEEK_MODEL,
        temperature=effective_temp,
        prompt_length=len(system_prompt) + len(user_prompt),
    ) if obs else _null_context():
        url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        }
        payload = {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": effective_temp,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
        except Exception as e:
            if obs:
                obs.record_error(_classify_error(e))
            raise

        if response.status_code != 200:
            error = _parse_http_error(response)
            if obs:
                obs.record_error(_classify_error(error))
            raise error

        result: dict = response.json()
        content: str = result["choices"][0]["message"]["content"]

        # 记录 Token 消耗 + IMP-001: 成本记录
        usage = result.get("usage", {})
        input_tokens = usage.get("prompt_tokens", input_estimated)
        output_tokens = usage.get("completion_tokens", CostCalculator.estimate_tokens(content))

        if obs:
            obs.record_tokens(
                model=DEEPSEEK_MODEL,
                provider="deepseek",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status="success",
            )

        if guard:
            try:
                guard.record_after_call(
                    model=DEEPSEEK_MODEL,
                    provider="deepseek",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status="success",
                )
            except Exception:
                pass  # 成本记录失败不影响主流程

        return content


# ============================================================
#  Claude 接口调用（底层传输）
# ============================================================

@_retry(max_attempts=3, delay=1.0, backoff=2.0)
def _call_claude(
    system_prompt: str,
    user_prompt: str,
) -> str:
    """调用 Anthropic Claude API"""
    logger.info(f"调用 Claude 接口，模型：{CLAUDE_MODEL}")

    obs = _get_observability()

    # ---- IMP-001: 预算检查 (pre-call) ----
    guard = _get_cost_guard()
    input_estimated = CostCalculator.estimate_tokens(system_prompt + user_prompt)
    if guard:
        try:
            guard.check_before_call(
                model=CLAUDE_MODEL,
                provider="claude",
                input_tokens=input_estimated,
                output_tokens_estimated=4096,
            )
        except BudgetExceededError as bee:
            logger.warning("预算检查未通过: %s", bee.message)
            guard.record_budget_exceeded(bee.limit_type)
            raise

    with obs.trace_ai_call(
        provider="claude",
        model=CLAUDE_MODEL,
        temperature=AI_TEMPERATURE,
        prompt_length=len(system_prompt) + len(user_prompt),
    ) if obs else _null_context():
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": AI_TEMPERATURE,
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
        except Exception as e:
            if obs:
                obs.record_error(_classify_error(e))
            raise

        if response.status_code != 200:
            error = _parse_http_error(response)
            if obs:
                obs.record_error(_classify_error(error))
            raise error

        result: dict = response.json()
        content: str = result["content"][0]["text"]

        # 记录 Token 消耗 + IMP-001: 成本记录
        usage = result.get("usage", {})
        input_tokens = usage.get("input_tokens", input_estimated)
        output_tokens = usage.get("output_tokens", CostCalculator.estimate_tokens(content))

        if obs:
            obs.record_tokens(
                model=CLAUDE_MODEL,
                provider="claude",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status="success",
            )

        if guard:
            try:
                guard.record_after_call(
                    model=CLAUDE_MODEL,
                    provider="claude",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status="success",
                )
            except Exception:
                pass  # 成本记录失败不影响主流程

        return content


# ============================================================
#  Legacy 调用入口（字符串返回）
# ============================================================

def call_ai_legacy(system_prompt: str, user_prompt: str) -> str:
    """
    Legacy AI 调用：返回原始字符串，不做结构化解析

    作为 Instructor 结构化生成的降级路径。
    异常时返回以 "⚠️" 开头的 Markdown 错误提示。
    """
    from observability import CircuitBreakerError

    # 检查成本熔断器
    obs = _get_observability()
    if obs:
        cb_status = obs.check_cost_circuit_breaker()
        if cb_status == "tripped":
            logger.warning("成本熔断器触发，拒绝 API 调用")
            return (
                "⚠️ **本月分析额度已用尽**\n\n"
                "已切换至本地轻量模型，准确率可能有所下降。\n"
                "如需恢复完整功能，请联系管理员提升预算。"
            )

    # 检查 API Key
    if AI_PROVIDER == "claude":
        api_key = CLAUDE_API_KEY
        platform_name = "Claude"
        platform_url = "https://console.anthropic.com/"
    else:
        api_key = DEEPSEEK_API_KEY
        platform_name = "DeepSeek"
        platform_url = "https://platform.deepseek.com/"

    if not api_key or api_key == "your_api_key_here":
        return (
            f"⚠️ **API Key 未配置**\n\n"
            f"请前往 {platform_url} 获取 API Key"
        )

    try:
        with timer("ai_engine:API调用(legacy)", record=True):
            if AI_PROVIDER == "claude":
                result_text: str = _call_claude(system_prompt, user_prompt)
            else:
                result_text = _call_openai_compatible(system_prompt, user_prompt)

        logger.info(f"AI 调用成功，返回内容长度：{len(result_text)} 字符")
        return result_text

    except AuthError as e:
        logger.error(f"认证失败：{e.message}")
        if obs:
            obs.record_error("auth")
        return f"⚠️ **API Key 无效或已过期**\n\n{e.message}"

    except RateLimitError as e:
        logger.error(f"频率超限：{e.message}")
        if obs:
            obs.record_error("rate_limit")
        return "⚠️ **请求频率超限**\n\n请等待 30 秒后重试。"

    except QuotaError as e:
        logger.error(f"余额不足：{e.message}")
        if obs:
            obs.record_error("quota")
        return f"⚠️ **账户余额不足**\n\n请前往 {platform_url} 充值。"

    except BudgetExceededError as e:
        logger.warning("预算超额: %s (%s)", e.limit_type, e.message)
        if obs:
            obs.record_error("budget_exceeded")
        guard = _get_cost_guard()
        if guard:
            guard.record_budget_exceeded(e.limit_type)
        # 返回用户友好的结构化错误
        bee_response = e.to_user_response()
        return (
            f"⚠️ **{bee_response['message']}**\n\n"
            f"今日已使用: ${bee_response['used']:.2f} / ${bee_response['limit']:.2f}\n"
            f"配额重置时间: {bee_response['reset_at']}\n\n"
            f"💡 {bee_response['suggestion']}"
        )

    except APIError as e:
        logger.error(f"API 错误（{e.status_code}）：{e.message}")
        if obs:
            obs.record_error("network")
        return f"⚠️ **API 调用失败**\n\n{e.message}"

    except Exception as e:
        logger.error(f"未知异常：{type(e).__name__}: {str(e)[:200]}")
        if obs:
            obs.record_error("network")
        return f"⚠️ **发生了未知错误**\n\n`{type(e).__name__}: {str(e)[:200]}`"


# ============================================================
#  结构化生成（Instructor 集成）
# ============================================================
# 使用 instructor 库包装 OpenAI 客户端
# 通过 response_model=AnalysisResult 实现模式强制生成
# Instructor 自动处理：JSON 提取 → Pydantic 校验 → 失败重试

def _create_instructor_client():
    """
    创建 Instructor 包装的 OpenAI 客户端

    延迟创建，避免模块加载时就要求 API Key 可用。
    返回 (client, mode) 元组，或 (None, None) 表示不可用。
    """
    try:
        import instructor
        from openai import OpenAI

        client = instructor.from_openai(
            OpenAI(
                base_url=DEEPSEEK_BASE_URL,
                api_key=DEEPSEEK_API_KEY,
            ),
            mode=instructor.Mode.JSON,
        )
        logger.info("Instructor 客户端创建成功 (Mode.JSON)")
        return client, instructor.Mode.JSON
    except ImportError:
        logger.warning("instructor 库未安装，结构化生成不可用")
        return None, None
    except Exception as e:
        logger.warning("Instructor 客户端创建失败: %s", e)
        return None, None


# 模块级缓存（延迟初始化）
_instructor_client = None
_instructor_initialized = False


def _get_instructor_client():
    """获取 Instructor 客户端单例"""
    global _instructor_client, _instructor_initialized
    if not _instructor_initialized:
        _instructor_client, _ = _create_instructor_client()
        _instructor_initialized = True
    return _instructor_client


def _best_effort_parse_to_model(raw_text: str, model_class: type) -> Any:
    """
    从非结构化字符串中尽力解析为 Pydantic BaseModel

    策略：
    1. 剥离 Markdown 代码块围栏
    2. 尝试 json.loads()
    3. 过滤已知字段，用默认值填充缺失字段
    4. 如果 JSON 完全无法解析，返回最小安全默认值

    参数:
        raw_text: AI 返回的原始文本
        model_class: 目标 Pydantic 模型类

    返回:
        model_class 的实例（可能包含默认值）
    """
    if not raw_text:
        return _create_fallback_model(model_class, "AI 返回了空内容")

    # 1. 剥离 Markdown 代码块围栏
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # 2. 尝试 JSON 解析
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 尝试从文本中提取 JSON 子串（AI 可能在 JSON 前后加了自然语言）
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                return _create_fallback_model(
                    model_class,
                    f"AI 返回的内容无法解析为 JSON: {cleaned[:200]}..."
                )
        else:
            return _create_fallback_model(
                model_class,
                f"AI 返回的内容不包含 JSON: {cleaned[:200]}..."
            )

    # 3. 尝试通过 Pydantic 校验
    try:
        return model_class.model_validate(data)
    except Exception:
        pass

    # 4. 降级：过滤已知字段，用默认值填充
    try:
        known_fields = model_class.model_fields
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return model_class.model_validate(filtered)
    except Exception:
        return _create_fallback_model(
            model_class,
            f"AI 返回的 JSON 不符合 Schema: {str(data)[:200]}..."
        )


def _create_fallback_model(model_class: type, warning: str) -> Any:
    """
    创建最小安全默认的 AnalysisResult 实例

    当所有解析和重试都失败时，返回一个带有安全警告的默认结果。
    """
    if model_class.__name__ == "AnalysisResult":
        from models import AnalysisResult, RootCause, FixSuggestion
        return AnalysisResult(
            error_summary="AI 分析结果解析失败",
            error_detail="无法从 AI 返回的内容中提取结构化数据",
            root_causes=[
                RootCause(description="日志格式可能不标准，AI 无法正确解析", probability=100)
            ],
            fix_suggestions=[],
            debug_commands=["echo '请手动检查日志'"],
            severity="medium",
            prevention=["建议检查日志格式是否标准"],
            security_warning=warning,
        )

    # 其他模型类型的通用降级
    raise ValueError(f"不支持的降级模型类型: {model_class.__name__}")


def call_ai_structured(
    system_prompt: str,
    user_prompt: str,
    max_retries: int = 3,
) -> Any:
    """
    结构化生成入口：AI 必须输出符合 AnalysisResult Schema 的 JSON

    重试层级：
    1. instructor 自动处理模式不匹配（最多 max_retries 次）
    2. 每次重试内部包含网络层指数退避（1s → 2s → 4s）
    3. 若 max_retries 次后仍失败，降级为 call_ai_legacy() + 尽力解析

    参数:
        system_prompt: 系统提示词（包含 Schema 描述）
        user_prompt: 用户提示词
        max_retries: Instructor 最大重试次数

    返回:
        AnalysisResult 实例
    """
    from models import AnalysisResult

    # 检查成本熔断器
    obs = _get_observability()
    if obs:
        cb_status = obs.check_cost_circuit_breaker()
        if cb_status == "tripped":
            logger.warning("成本熔断器触发，拒绝结构化生成调用")
            from ai_engine import _create_fallback_model
            return _create_fallback_model(
                AnalysisResult,
                "本月分析额度已用尽，已切换至本地轻量模型"
            )

    client = _get_instructor_client()

    # Instructor 不可用时，直接走降级路径
    if client is None:
        logger.warning("Instructor 不可用，走 legacy 降级路径")
        raw_text = call_ai_legacy(system_prompt, user_prompt)
        return _best_effort_parse_to_model(raw_text, AnalysisResult)

    # 使用 Instructor 进行结构化生成
    try:
        # ---- IMP-001: 预算检查 (pre-call) ----
        guard = _get_cost_guard()
        input_estimated = CostCalculator.estimate_tokens(system_prompt + user_prompt)
        if guard:
            try:
                guard.check_before_call(
                    model=DEEPSEEK_MODEL,
                    provider="deepseek",
                    input_tokens=input_estimated,
                    output_tokens_estimated=4096,
                )
            except BudgetExceededError as bee:
                logger.warning("预算检查未通过: %s", bee.message)
                guard.record_budget_exceeded(bee.limit_type)
                return _create_fallback_model(
                    AnalysisResult,
                    bee.to_user_response()["message"],
                )

        with timer("ai_engine:结构化生成(Instructor)", record=True):
            result = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                response_model=AnalysisResult,
                max_retries=max_retries,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=DEEPSEEK_TEMPERATURE,
            )
        logger.info("结构化生成成功")

        # IMP-001: 记录成本（Instructor 模式下无法获取准确的 token，使用估算）
        if guard:
            try:
                guard.record_after_call(
                    model=DEEPSEEK_MODEL,
                    provider="deepseek",
                    input_tokens=input_estimated,
                    output_tokens=CostCalculator.estimate_tokens(
                        result.model_dump_json() if hasattr(result, 'model_dump_json') else str(result)
                    ),
                    status="success",
                )
            except Exception:
                pass

        return result

    except BudgetExceededError as bee:
        logger.warning(
            "Instructor 调用预算超额（%s: %s），返回降级结果",
            bee.limit_type, bee.message,
        )
        return _create_fallback_model(
            AnalysisResult,
            bee.to_user_response()["message"],
        )

    except Exception as e:
        error_name = type(e).__name__
        logger.warning(
            "Instructor 结构化生成失败（%s: %s），走降级路径",
            error_name, str(e)[:200],
        )

        # 降级：获取字符串结果，尽力解析为 BaseModel
        raw_text = call_ai_legacy(system_prompt, user_prompt)

        # 如果 legacy 返回的是错误提示（以 ⚠️ 开头），直接返回 fallback
        if raw_text.startswith("⚠️"):
            return _create_fallback_model(AnalysisResult, raw_text)

        return _best_effort_parse_to_model(raw_text, AnalysisResult)


# ============================================================
#  对外统一入口（兼容旧接口）
# ============================================================

def call_ai(system_prompt: str, user_prompt: str) -> str:
    """
    AI 调用的统一入口（字符串返回）

    保持与旧版 ai_engine.py 兼容的接口签名。
    内部委托给 call_ai_legacy()。
    """
    return call_ai_legacy(system_prompt, user_prompt)
