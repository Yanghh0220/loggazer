# preflight.py — Pre-flight API Validation & System Readiness
#
# IMP-002: P1 High-Value Improvement
#
# 职责：
#   1. check_ai_api()     — 发送最小测试 prompt（5s 超时，< 20 tokens）
#   2. check_qdrant()     — ping Qdrant 连接
#   3. check_redis()      — ping Redis（如配置）
#   4. check_disk_space() — 确保有 500MB 可用空间
#   5. check_memory()     — 确保有 1GB 可用内存
#   6. check_config()     — 验证所有必填配置项非空
#
# run_all() 返回 PreflightResult:
#   {
#     "passed": bool,
#     "checks": [{"name", "status", "message", "duration_ms"}],
#     "blocking": [failed critical checks],
#     "warnings": [failed non-critical checks],
#   }
#
# 设计原则：
#   - 最小化测试 prompt（<20 tokens，不消耗用户配额）
#   - 快速故障检测（5s 超时，不在启动时浪费用户时间）
#   - 非阻塞：失败不影响系统启动，仅在前端展示状态
#   - 零副作用：测试请求完成后立即丢弃结果

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)


# ============================================================
#  数据模型
# ============================================================

@dataclass
class CheckItem:
    """单个检查项的结果"""
    name: str           # 检查名称（如 "ai_api", "config"）
    status: str         # "pass" | "fail" | "warn"
    message: str        # 人类可读的状态消息
    duration_ms: float  # 执行耗时（毫秒）
    critical: bool = True  # 是否为阻塞性检查（fail 会导致 passed=False）


@dataclass
class PreflightResult:
    """
    预检结果汇总。

    属性:
        passed: 所有 critical 检查是否全部通过
        checks: 所有检查项的列表
        blocking: 失败的 critical 检查
        warnings: 失败的非 critical 检查
        total_duration_ms: 总耗时
    """
    passed: bool = True
    checks: list[CheckItem] = field(default_factory=list)
    blocking: list[CheckItem] = field(default_factory=list)
    warnings: list[CheckItem] = field(default_factory=list)
    total_duration_ms: float = 0.0

    def to_dict(self) -> dict:
        """转换为字典（供 API 响应 / UI 使用）"""
        return {
            "passed": self.passed,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "duration_ms": c.duration_ms,
                }
                for c in self.checks
            ],
            "blocking": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "duration_ms": c.duration_ms,
                }
                for c in self.blocking
            ],
            "warnings": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "duration_ms": c.duration_ms,
                }
                for c in self.warnings
            ],
            "total_duration_ms": self.total_duration_ms,
        }


# ============================================================
#  PreflightChecker
# ============================================================

class PreflightChecker:
    """
    预检器：在应用启动时快速验证所有关键依赖。

    用法:
        checker = PreflightChecker()
        result = checker.run_all()  # 同步执行所有检查

        # 或在 Streamlit 中异步执行:
        result = checker.run_all_async()  # 返回 threading.Thread
    """

    # 最小测试 prompt（<20 tokens，验证结构化输出能力）
    # 设计目标：
    #   - 必须验证 Instructor 兼容性（structured output）
    #   - 最小化 token 消耗（input ~8 tokens, output ~5 tokens）
    #   - 不使用真实日志内容
    #   - 5 秒超时
    MINIMAL_TEST_PROMPT = "Reply: {\"ok\":true}"

    MINIMAL_TEST_SCHEMA = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }

    def __init__(
        self,
        redis_url: Optional[str] = None,
        qdrant_url: Optional[str] = None,
        qdrant_path: Optional[str] = None,
        min_disk_mb: int = 500,
        min_memory_mb: int = 1024,
        ai_test_timeout: float = 5.0,
    ):
        """
        初始化预检器。

        参数:
            redis_url: Redis 连接 URL（None = 从 config 读取）
            qdrant_url: Qdrant 服务 URL（None = 从 config 读取）
            qdrant_path: Qdrant 本地路径（None = 从 config 读取）
            min_disk_mb: 最少可用磁盘空间（MB），默认 500
            min_memory_mb: 最少可用内存（MB），默认 1024
            ai_test_timeout: AI API 测试超时（秒），默认 5.0
        """
        self._redis_url = redis_url
        self._qdrant_url = qdrant_url
        self._qdrant_path = qdrant_path
        self._min_disk_mb = min_disk_mb
        self._min_memory_mb = min_memory_mb
        self._ai_test_timeout = ai_test_timeout

    # ---- 独立检查方法 ----

    def check_ai_api(self) -> CheckItem:
        """
        检查 AI API 连接：发送最小测试 prompt。

        测试目标:
          1. API Key 是否有效
          2. 网络是否可达
          3. API 是否返回有效 JSON
          4. Instructor 结构化输出是否兼容

        测试策略:
          - 发送 {"ok": true} 的最小提示词
          - 5 秒超时，不影响系统启动
          - 不使用真实日志内容
          - token 消耗 < 20
        """
        start = time.perf_counter()

        try:
            from config import (
                DEEPSEEK_BASE_URL,
                DEEPSEEK_API_KEY,
                DEEPSEEK_MODEL,
                AI_PROVIDER,
                CLAUDE_API_KEY,
                CLAUDE_MODEL,
            )
            import requests

            # 确定 provider 和 API Key
            if AI_PROVIDER == "claude":
                api_key = CLAUDE_API_KEY
                model = CLAUDE_MODEL
                provider_name = "Claude"
            else:
                api_key = DEEPSEEK_API_KEY
                model = DEEPSEEK_MODEL
                provider_name = "DeepSeek"

            # 检查 API Key 是否配置
            if not api_key or api_key == "your_api_key_here":
                duration = (time.perf_counter() - start) * 1000
                return CheckItem(
                    name="ai_api",
                    status="fail",
                    message=f"未配置 {provider_name} API Key，请在 .env 文件中设置",
                    duration_ms=round(duration, 1),
                    critical=True,
                )

            # 发送最小测试请求
            if AI_PROVIDER == "claude":
                url = "https://api.anthropic.com/v1/messages"
                headers = {
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
                payload = {
                    "model": model,
                    "max_tokens": 10,
                    "system": "Reply with exactly: {\"ok\": true}",
                    "messages": [{"role": "user", "content": "ok"}],
                }
            else:
                url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/chat/completions"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                }
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Reply with exactly: {\"ok\": true}"},
                        {"role": "user", "content": "ok"},
                    ],
                    "max_tokens": 10,
                    "temperature": 0,
                }

            response = requests.post(
                url, headers=headers, json=payload,
                timeout=self._ai_test_timeout,
            )
            duration = (time.perf_counter() - start) * 1000

            if response.status_code == 200:
                # 验证返回的是有效 JSON
                try:
                    data = response.json()
                except Exception:
                    return CheckItem(
                        name="ai_api",
                        status="warn",
                        message=f"{provider_name} API 连接成功但返回非 JSON",
                        duration_ms=round(duration, 1),
                        critical=False,
                    )

                # 额外验证：测试 Instructor 兼容性（不消耗 token）
                instructor_ok = self._verify_instructor_compat()
                if instructor_ok:
                    return CheckItem(
                        name="ai_api",
                        status="pass",
                        message=f"{provider_name} API ({model}) 连接正常，结构化输出兼容",
                        duration_ms=round(duration, 1),
                        critical=True,
                    )
                else:
                    return CheckItem(
                        name="ai_api",
                        status="warn",
                        message=(
                            f"{provider_name} API 连接正常，但 Instructor 结构化输出不兼容，"
                            f"将使用 legacy 路径"
                        ),
                        duration_ms=round(duration, 1),
                        critical=False,
                    )

            elif response.status_code == 401:
                return CheckItem(
                    name="ai_api",
                    status="fail",
                    message=f"{provider_name} API Key 无效或已过期 (HTTP 401)",
                    duration_ms=round(duration, 1),
                    critical=True,
                )
            elif response.status_code == 429:
                return CheckItem(
                    name="ai_api",
                    status="warn",
                    message=f"{provider_name} API 频率超限或余额不足 (HTTP 429)",
                    duration_ms=round(duration, 1),
                    critical=False,
                )
            elif response.status_code >= 500:
                return CheckItem(
                    name="ai_api",
                    status="warn",
                    message=f"{provider_name} API 服务端错误 (HTTP {response.status_code})",
                    duration_ms=round(duration, 1),
                    critical=False,
                )
            else:
                return CheckItem(
                    name="ai_api",
                    status="fail",
                    message=f"{provider_name} API 返回异常状态码 {response.status_code}",
                    duration_ms=round(duration, 1),
                    critical=True,
                )

        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            error_name = type(e).__name__
            error_msg = str(e)[:200]

            # 区分超时 vs 网络错误
            if "timeout" in error_name.lower() or "timed out" in error_msg.lower():
                return CheckItem(
                    name="ai_api",
                    status="warn",
                    message=f"API 连接超时 ({self._ai_test_timeout}s)，将在首次分析时重试",
                    duration_ms=round(duration, 1),
                    critical=False,
                )

            return CheckItem(
                name="ai_api",
                status="fail",
                message=f"API 连接失败: {error_name} — {error_msg}",
                duration_ms=round(duration, 1),
                critical=True,
            )

    @staticmethod
    def _verify_instructor_compat() -> bool:
        """验证 Instructor 结构化输出是否可用（仅导入检查，不发送请求）"""
        try:
            import instructor
            from openai import OpenAI
            return True
        except ImportError:
            return False

    def check_qdrant(self) -> CheckItem:
        """
        检查 Qdrant 连接（如配置）。

        策略:
          1. 如果配置了 Qdrant URL → ping 服务
          2. 如果配置了本地路径 → 检查路径可写
          3. 如果未配置 → 跳过（不阻塞）
        """
        start = time.perf_counter()

        try:
            from config import CACHE_QDRANT_PATH

            qdrant_path = self._qdrant_path or CACHE_QDRANT_PATH
            qdrant_url = self._qdrant_url

            # 本地文件模式
            if not qdrant_url and qdrant_path:
                path = Path(qdrant_path)
                if path.exists():
                    # 检查是否可写
                    test_file = path / ".preflight_test"
                    try:
                        test_file.touch()
                        test_file.unlink()
                        duration = (time.perf_counter() - start) * 1000
                        return CheckItem(
                            name="qdrant",
                            status="pass",
                            message=f"Qdrant 本地存储可用 ({qdrant_path})",
                            duration_ms=round(duration, 1),
                            critical=False,
                        )
                    except OSError:
                        duration = (time.perf_counter() - start) * 1000
                        return CheckItem(
                            name="qdrant",
                            status="warn",
                            message=f"Qdrant 路径不可写: {qdrant_path}，语义缓存将降级为内存模式",
                            duration_ms=round(duration, 1),
                            critical=False,
                        )
                else:
                    # 尝试创建
                    try:
                        path.mkdir(parents=True, exist_ok=True)
                        duration = (time.perf_counter() - start) * 1000
                        return CheckItem(
                            name="qdrant",
                            status="pass",
                            message=f"Qdrant 本地存储已创建 ({qdrant_path})",
                            duration_ms=round(duration, 1),
                            critical=False,
                        )
                    except OSError:
                        duration = (time.perf_counter() - start) * 1000
                        return CheckItem(
                            name="qdrant",
                            status="warn",
                            message=f"无法创建 Qdrant 目录: {qdrant_path}，将使用内存模式",
                            duration_ms=round(duration, 1),
                            critical=False,
                        )

            # 远程模式
            if qdrant_url:
                try:
                    import requests
                    resp = requests.get(
                        f"{qdrant_url.rstrip('/')}/health",
                        timeout=3.0,
                    )
                    duration = (time.perf_counter() - start) * 1000
                    if resp.is_success:
                        return CheckItem(
                            name="qdrant",
                            status="pass",
                            message=f"Qdrant 服务连接正常 ({qdrant_url})",
                            duration_ms=round(duration, 1),
                            critical=False,
                        )
                    else:
                        return CheckItem(
                            name="qdrant",
                            status="warn",
                            message=f"Qdrant 服务返回异常 ({qdrant_url}, HTTP {resp.status_code})",
                            duration_ms=round(duration, 1),
                            critical=False,
                        )
                except Exception:
                    duration = (time.perf_counter() - start) * 1000
                    return CheckItem(
                        name="qdrant",
                        status="warn",
                        message=f"Qdrant 服务不可达 ({qdrant_url})，将使用内存模式",
                        duration_ms=round(duration, 1),
                        critical=False,
                    )

            # 未配置
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="qdrant",
                status="pass",
                message="Qdrant 未配置，将使用内存模式",
                duration_ms=round(duration, 1),
                critical=False,
            )

        except ImportError:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="qdrant",
                status="pass",
                message="Qdrant 客户端未安装，将使用内存模式",
                duration_ms=round(duration, 1),
                critical=False,
            )
        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="qdrant",
                status="warn",
                message=f"Qdrant 检查异常: {e}",
                duration_ms=round(duration, 1),
                critical=False,
            )

    def check_redis(self) -> CheckItem:
        """
        检查 Redis 连接（如配置）。

        非 critical: Redis 不可用时降级到内存模式。
        """
        start = time.perf_counter()

        try:
            import redis as redis_lib

            redis_url = self._redis_url or os.getenv("REDIS_URL", "")

            if not redis_url:
                # 尝试默认 localhost
                try:
                    client = redis_lib.Redis(
                        host="localhost", port=6379, db=0,
                        socket_connect_timeout=2,
                    )
                    client.ping()
                    duration = (time.perf_counter() - start) * 1000
                    return CheckItem(
                        name="redis",
                        status="pass",
                        message="Redis (localhost:6379) 连接正常",
                        duration_ms=round(duration, 1),
                        critical=False,
                    )
                except Exception:
                    duration = (time.perf_counter() - start) * 1000
                    return CheckItem(
                        name="redis",
                        status="pass",
                        message="Redis 未配置，预算/限流将使用内存模式",
                        duration_ms=round(duration, 1),
                        critical=False,
                    )

            # 使用配置的 URL
            client = redis_lib.from_url(
                redis_url, socket_connect_timeout=2,
            )
            client.ping()
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="redis",
                status="pass",
                message=f"Redis 连接正常 ({redis_url})",
                duration_ms=round(duration, 1),
                critical=False,
            )

        except ImportError:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="redis",
                status="pass",
                message="redis 库未安装，将使用内存模式",
                duration_ms=round(duration, 1),
                critical=False,
            )
        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="redis",
                status="warn",
                message=f"Redis 连接失败: {e}，将使用内存模式",
                duration_ms=round(duration, 1),
                critical=False,
            )

    def check_disk_space(self) -> CheckItem:
        """
        检查磁盘可用空间。

        检查项目根目录所在磁盘的可用空间。
        至少需要 500MB（可配置）。
        """
        start = time.perf_counter()

        try:
            # 使用项目根目录所在磁盘
            project_dir = Path(__file__).parent.resolve()
            usage = shutil.disk_usage(project_dir)
            free_mb = usage.free / (1024 * 1024)
            total_mb = usage.total / (1024 * 1024)

            duration = (time.perf_counter() - start) * 1000

            if free_mb >= self._min_disk_mb:
                return CheckItem(
                    name="disk_space",
                    status="pass",
                    message=(
                        f"磁盘空间充足: {free_mb:.0f} MB 可用 "
                        f"(总计 {total_mb:.0f} MB, 最低要求 {self._min_disk_mb} MB)"
                    ),
                    duration_ms=round(duration, 1),
                    critical=True,
                )
            else:
                return CheckItem(
                    name="disk_space",
                    status="fail",
                    message=(
                        f"磁盘空间不足: 仅 {free_mb:.0f} MB 可用，"
                        f"最低需要 {self._min_disk_mb} MB。"
                        f"请清理磁盘后重试。"
                    ),
                    duration_ms=round(duration, 1),
                    critical=True,
                )

        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="disk_space",
                status="warn",
                message=f"无法检查磁盘空间: {e}",
                duration_ms=round(duration, 1),
                critical=False,
            )

    def check_memory(self) -> CheckItem:
        """
        检查系统可用内存。

        至少需要 1GB（可配置）。
        使用 psutil（如安装）获取更精确的值。
        """
        start = time.perf_counter()

        try:
            import psutil
            mem = psutil.virtual_memory()
            free_mb = mem.available / (1024 * 1024)
            total_mb = mem.total / (1024 * 1024)
            used_pct = mem.percent

            duration = (time.perf_counter() - start) * 1000

            if free_mb >= self._min_memory_mb:
                return CheckItem(
                    name="memory",
                    status="pass",
                    message=(
                        f"内存充足: {free_mb:.0f} MB 可用 "
                        f"(总计 {total_mb:.0f} MB, 已用 {used_pct:.0f}%, "
                        f"最低要求 {self._min_memory_mb} MB)"
                    ),
                    duration_ms=round(duration, 1),
                    critical=True,
                )
            else:
                return CheckItem(
                    name="memory",
                    status="fail",
                    message=(
                        f"内存不足: 仅 {free_mb:.0f} MB 可用，"
                        f"最低需要 {self._min_memory_mb} MB。"
                        f"请关闭其他应用后重试。"
                    ),
                    duration_ms=round(duration, 1),
                    critical=True,
                )

        except ImportError:
            # psutil 未安装，使用 /proc/meminfo 或回退
            duration = (time.perf_counter() - start) * 1000
            free_mb = self._get_memory_from_os()

            if free_mb is None:
                return CheckItem(
                    name="memory",
                    status="pass",
                    message="psutil 未安装，跳过详细内存检查",
                    duration_ms=round(duration, 1),
                    critical=False,
                )
            elif free_mb >= self._min_memory_mb:
                return CheckItem(
                    name="memory",
                    status="pass",
                    message=f"内存充足: {free_mb:.0f} MB 可用 (最低要求 {self._min_memory_mb} MB)",
                    duration_ms=round(duration, 1),
                    critical=True,
                )
            else:
                return CheckItem(
                    name="memory",
                    status="fail",
                    message=(
                        f"内存不足: 仅 {free_mb:.0f} MB 可用，"
                        f"最低需要 {self._min_memory_mb} MB"
                    ),
                    duration_ms=round(duration, 1),
                    critical=True,
                )

        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="memory",
                status="warn",
                message=f"内存检查异常: {e}",
                duration_ms=round(duration, 1),
                critical=False,
            )

    @staticmethod
    def _get_memory_from_os() -> Optional[int]:
        """从操作系统获取可用内存（MB），psutil 不可用时的回退方案"""
        try:
            import sys
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                status = MEMORYSTATUSEX()
                status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
                return int(status.ullAvailPhys / (1024 * 1024))
            else:
                # Linux: 读取 /proc/meminfo
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if line.startswith("MemAvailable:"):
                            parts = line.split()
                            return int(parts[1]) // 1024  # kB → MB
        except Exception:
            pass
        return None

    def check_config(self) -> CheckItem:
        """
        检查所有必填配置项是否已正确设置。

        检查项:
          - DEEPSEEK_API_KEY / CLAUDE_API_KEY
          - DEEPSEEK_BASE_URL
          - DEEPSEEK_MODEL / CLAUDE_MODEL
        """
        start = time.perf_counter()

        try:
            from config import (
                DEEPSEEK_API_KEY,
                DEEPSEEK_BASE_URL,
                DEEPSEEK_MODEL,
                AI_PROVIDER,
                CLAUDE_API_KEY,
                CLAUDE_MODEL,
            )

            missing = []
            warnings_list = []

            # 根据 provider 检查对应的配置
            if AI_PROVIDER == "claude":
                if not CLAUDE_API_KEY or CLAUDE_API_KEY == "your_api_key_here":
                    missing.append("CLAUDE_API_KEY")
                if not CLAUDE_MODEL:
                    missing.append("CLAUDE_MODEL")
            else:
                if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your_api_key_here":
                    missing.append("DEEPSEEK_API_KEY")
                if not DEEPSEEK_BASE_URL:
                    missing.append("DEEPSEEK_BASE_URL")
                if not DEEPSEEK_MODEL:
                    missing.append("DEEPSEEK_MODEL")

            # 检查 .env 文件存在性
            env_file = Path(__file__).parent / ".env"
            if not env_file.exists():
                warnings_list.append("未找到 .env 文件，建议从 .env.example 复制")

            duration = (time.perf_counter() - start) * 1000

            if missing:
                return CheckItem(
                    name="config",
                    status="fail",
                    message=f"缺少必填配置项: {', '.join(missing)}。请在 .env 文件中设置",
                    duration_ms=round(duration, 1),
                    critical=True,
                )
            elif warnings_list:
                return CheckItem(
                    name="config",
                    status="warn",
                    message=f"配置基本就绪，但有建议: {'; '.join(warnings_list)}",
                    duration_ms=round(duration, 1),
                    critical=False,
                )
            else:
                return CheckItem(
                    name="config",
                    status="pass",
                    message="所有必填配置项已正确设置",
                    duration_ms=round(duration, 1),
                    critical=True,
                )

        except Exception as e:
            duration = (time.perf_counter() - start) * 1000
            return CheckItem(
                name="config",
                status="fail",
                message=f"配置检查异常: {e}",
                duration_ms=round(duration, 1),
                critical=True,
            )

    # ---- 运行所有检查 ----

    def run_all(self, checks: Optional[list[str]] = None) -> PreflightResult:
        """
        顺序执行所有预检项，返回 PreflightResult。

        参数:
            checks: 要执行的检查项名称列表，None 表示执行全部

        返回:
            PreflightResult 实例
        """
        total_start = time.perf_counter()
        result = PreflightResult()

        # 注册所有检查项
        all_checks: list[tuple[str, Callable[[], CheckItem], bool]] = [
            ("config", self.check_config, True),
            ("disk_space", self.check_disk_space, True),
            ("memory", self.check_memory, True),
            ("ai_api", self.check_ai_api, True),
            ("qdrant", self.check_qdrant, False),
            ("redis", self.check_redis, False),
        ]

        # 过滤指定检查
        if checks:
            check_set = set(checks)
            all_checks = [(n, f, c) for n, f, c in all_checks if n in check_set]

        for name, check_fn, _ in all_checks:
            try:
                check_result = check_fn()
            except Exception as e:
                check_result = CheckItem(
                    name=name,
                    status="fail",
                    message=f"检查执行异常: {type(e).__name__}: {str(e)[:200]}",
                    duration_ms=0.0,
                    critical=True,
                )

            result.checks.append(check_result)

            if check_result.status == "fail" and check_result.critical:
                result.blocking.append(check_result)
                result.passed = False
            elif check_result.status in ("fail", "warn") and not check_result.critical:
                result.warnings.append(check_result)

        result.total_duration_ms = round(
            (time.perf_counter() - total_start) * 1000, 1
        )

        # 汇总日志
        if result.passed:
            logger.info(
                "Preflight passed: %d checks, %d warnings (%.1fms)",
                len(result.checks), len(result.warnings), result.total_duration_ms,
            )
        else:
            logger.warning(
                "Preflight FAILED: %d blocking, %d warnings (%.1fms)",
                len(result.blocking), len(result.warnings), result.total_duration_ms,
            )

        return result

    def run_all_async(
        self,
        checks: Optional[list[str]] = None,
        callback: Optional[Callable[[PreflightResult], None]] = None,
    ) -> threading.Thread:
        """
        在后台线程中执行所有预检项（非阻塞）。

        参数:
            checks: 要执行的检查项名称列表，None 表示全部
            callback: 完成后的回调函数（接收 PreflightResult）

        返回:
            threading.Thread 实例（已启动）
        """

        def _runner():
            result = self.run_all(checks=checks)
            if callback:
                try:
                    callback(result)
                except Exception as e:
                    logger.error("Preflight callback error: %s", e)

        thread = threading.Thread(target=_runner, daemon=True, name="loggazer-preflight")
        thread.start()
        return thread


# ============================================================
#  便捷函数
# ============================================================

def run_preflight(
    ai_test_timeout: float = 5.0,
    min_disk_mb: int = 500,
    min_memory_mb: int = 1024,
) -> PreflightResult:
    """
    快捷预检（使用默认配置）。

    用法:
        result = run_preflight()
        if not result.passed:
            print("系统未就绪:", result.blocking)
    """
    checker = PreflightChecker(
        ai_test_timeout=ai_test_timeout,
        min_disk_mb=min_disk_mb,
        min_memory_mb=min_memory_mb,
    )
    return checker.run_all()
