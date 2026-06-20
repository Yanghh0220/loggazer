# config.py - 集中管理配置项
#
# v2.0 — 性能优化更新 (2026-06-20)
# ✅ 新增: 异步任务 TTL、大文件分块、缓存磁盘持久化等配置
#
# 为什么需要这个文件？
# 1. 把散落在各处的配置集中管理，修改时不用到处找
# 2. 支持从环境变量读取，方便部署时覆盖默认值
# 3. 新增配置项时，只需要改这一个文件
#
# 配置读取优先级：
#   1. Streamlit Secrets（云端部署时使用，在 Streamlit Cloud 控制台配置）
#   2. 环境变量 / .env 文件（本地开发时使用）

import os

from dotenv import load_dotenv
load_dotenv()


def _get_secret(key: str, default=None):
    """
    统一的配置读取函数：优先 Streamlit Secrets，回退到环境变量

    为什么需要这个？
    - 本地开发用 .env 文件（os.getenv）
    - Streamlit Cloud 部署用 Secrets（st.secrets）
    - 一个函数搞定两种场景，业务代码不需要关心配置来源
    """
    try:
        import streamlit as st
        # st.secrets 的行为类似字典，key 不存在时抛出 KeyError
        value = st.secrets[key]
        # Streamlit Secrets 的值可能是 TOML 类型，统一转字符串
        return str(value) if value is not None else default
    except (KeyError, FileNotFoundError, TypeError):
        # KeyError: key 不存在于 secrets 中
        # FileNotFoundError: 没有 .streamlit/secrets.toml（本地开发正常情况）
        # TypeError: st.secrets 还未初始化
        return os.getenv(key, default)


# ============================================
# DeepSeek API 配置
# ============================================

# API 地址（默认值：https://api.deepseek.com）
# 如果你用其他兼容 OpenAI 的模型（如 Moonshot、智谱），改这里
DEEPSEEK_BASE_URL = _get_secret(
    "DEEPSEEK_BASE_URL",
    "https://api.deepseek.com"
)

# 模型名称（默认值：deepseek-chat）
# DeepSeek 可选：deepseek-chat（通用）、deepseek-coder（代码专用）
DEEPSEEK_MODEL = _get_secret(
    "DEEPSEEK_MODEL",
    "deepseek-chat"
)

# 温度参数（默认值：0）
# 0 = 最稳定、可重复；1 = 最有创意、随机
DEEPSEEK_TEMPERATURE = float(_get_secret(
    "DEEPSEEK_TEMPERATURE",
    "0"
))

# API Key
# 本地开发从 .env 读取 DEEPSEEK_API_KEY
# Streamlit Cloud 从 Secrets 读取 API_KEY 或 DEEPSEEK_API_KEY
DEEPSEEK_API_KEY = _get_secret("DEEPSEEK_API_KEY") or _get_secret("API_KEY")

# ============================================
# AI 提供商选择
# ============================================
# 可选值："openai"（兼容 DeepSeek/Moonshot/智谱）或 "claude"
AI_PROVIDER = _get_secret("AI_PROVIDER", "openai")

# AI 温度参数（默认 0.2，比 0 更稳定但不完全死板）
AI_TEMPERATURE = float(_get_secret("AI_TEMPERATURE", "0.2"))

# ============================================
# Claude API 配置
# ============================================
CLAUDE_API_KEY = _get_secret("CLAUDE_API_KEY")
CLAUDE_MODEL = _get_secret("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ============================================
# 语义缓存配置
# ============================================

# 缓存总开关（默认开启）
CACHE_ENABLED = _get_secret("CACHE_ENABLED", "true").lower() == "true"

# 语义相似度阈值：>= 此值直接返回缓存结果
CACHE_SIMILARITY_HIGH = float(_get_secret("CACHE_SIMILARITY_HIGH", "0.92"))

# 语义相似度阈值：>= 此值且 < HIGH 时注入 RAG 上下文
CACHE_SIMILARITY_LOW = float(_get_secret("CACHE_SIMILARITY_LOW", "0.80"))

# 缓存 TTL（小时），默认 30 天
CACHE_TTL_HOURS = int(_get_secret("CACHE_TTL_HOURS", "720"))

# Qdrant 存储路径，空字符串 = 内存模式
CACHE_QDRANT_PATH = _get_secret("CACHE_QDRANT_PATH", "")

# Embedding 模型名称
CACHE_EMBEDDING_MODEL = _get_secret("CACHE_EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# ============================================
# 性能优化参数 (P0/P1/P2 优化阶段)
# ============================================

# --- 内容 Hash 缓存 (P0-2) ---
# 分析结果缓存 TTL（秒），默认 5 分钟
CONTENT_CACHE_TTL_SECONDS = int(_get_secret("CONTENT_CACHE_TTL_SECONDS", "300"))
# 分析结果缓存最大条目数
CONTENT_CACHE_MAXSIZE = int(_get_secret("CONTENT_CACHE_MAXSIZE", "500"))
# 日志解析结果缓存 TTL（秒），默认 10 分钟
PARSED_CACHE_TTL_SECONDS = int(_get_secret("PARSED_CACHE_TTL_SECONDS", "600"))
# 日志解析结果缓存最大条目数
PARSED_CACHE_MAXSIZE = int(_get_secret("PARSED_CACHE_MAXSIZE", "1000"))

# --- 增量分析 (P1-4) ---
# 增量追踪 TTL（秒），30 分钟后重新全量分析
INCREMENTAL_TTL_SECONDS = int(_get_secret("INCREMENTAL_TTL_SECONDS", "1800"))

# --- 线程池 (P0-3) ---
# API 端线程池大小（CPU 密集型任务隔离）
API_EXECUTOR_MAX_WORKERS = int(_get_secret("API_EXECUTOR_MAX_WORKERS",
    str(min(4, (os.cpu_count() or 2)))))
# Streamlit 端线程池大小
STREAMLIT_EXECUTOR_MAX_WORKERS = int(_get_secret("STREAMLIT_EXECUTOR_MAX_WORKERS", "2"))

# --- 超时控制 (P1-3) ---
# AI 分析超时（秒）
ANALYSIS_TIMEOUT_SECONDS = int(_get_secret("ANALYSIS_TIMEOUT_SECONDS", "120"))
# 后端健康检查超时（秒）
HEALTH_CHECK_TIMEOUT = float(_get_secret("HEALTH_CHECK_TIMEOUT", "3.0"))
# 后端启动总超时（秒）
STARTUP_TIMEOUT = float(_get_secret("STARTUP_TIMEOUT", "30.0"))
# API 请求超时（秒）
API_REQUEST_TIMEOUT = float(_get_secret("API_REQUEST_TIMEOUT", "180.0"))

# --- 日志处理 (P0-4) ---
# 日志截断最大字符数
MAX_LOG_LENGTH = int(_get_secret("MAX_LOG_LENGTH", "6000"))
# 大文件分块处理行数阈值
LOG_CHUNK_SIZE = int(_get_secret("LOG_CHUNK_SIZE", "10000"))

# --- 资源保护 (P2-4) ---
# 文件大小硬限制（字符数），超过拒绝
MAX_LOG_SIZE_CHARS = int(_get_secret("LOGGAZER_MAX_LOG_SIZE", "100000"))
# 文件大小软警告（字符数），超过时前端提示
FRONTEND_WARN_SIZE = int(_get_secret("LOGGAZER_WARN_SIZE", "50000"))
# 内存警告阈值（MB）
MEMORY_WARN_THRESHOLD_MB = float(_get_secret("LOGGAZER_MEMORY_WARN_MB", "500"))
# 内存拒绝阈值（MB）
MEMORY_REJECT_THRESHOLD_MB = float(_get_secret("LOGGAZER_MEMORY_REJECT_MB", "800"))
# 最大并发分析数
MAX_CONCURRENT_ANALYSES = int(_get_secret("LOGGAZER_MAX_CONCURRENT", "3"))
# 最大排队数
QUEUE_MAX_SIZE = int(_get_secret("LOGGAZER_QUEUE_MAX_SIZE", "20"))

# --- GZip 压缩 (P1-2) ---
# GZip 最小压缩阈值（字节），小于此值不压缩
GZIP_MINIMUM_SIZE = int(_get_secret("GZIP_MINIMUM_SIZE", "1024"))

# --- 前端渲染 (P1-1) ---
# LTTB 降采样阈值（超过此数据点数时触发降采样）
LTTB_THRESHOLD = int(_get_secret("LTTB_THRESHOLD", "500"))
# 簇列表每页条数
CLUSTER_PAGE_SIZE = int(_get_secret("CLUSTER_PAGE_SIZE", "5"))
# CSS 文件缓存 TTL（秒）
CSS_CACHE_TTL = int(_get_secret("CSS_CACHE_TTL", "3600"))
# 趋势查询缓存 TTL（秒）
TRENDING_CACHE_TTL = int(_get_secret("TRENDING_CACHE_TTL", "60"))

# --- API 级 TTL 缓存 (P0-2) ---
# API 响应缓存 TTL（秒）
API_CACHE_TTL_SECONDS = int(_get_secret("API_CACHE_TTL_SECONDS", "300"))
# API 响应缓存最大条目数
API_CACHE_MAXSIZE = int(_get_secret("API_CACHE_MAXSIZE", "200"))

# --- 重试与轮询 (P0-1) ---
# 后端启动轮询最大次数
BACKEND_POLL_MAX_ATTEMPTS = int(_get_secret("BACKEND_POLL_MAX_ATTEMPTS", "10"))
# 后端启动轮询基础延迟（秒）
BACKEND_POLL_BASE_DELAY = float(_get_secret("BACKEND_POLL_BASE_DELAY", "0.1"))
# 后端启动轮询最大延迟（秒）
BACKEND_POLL_MAX_DELAY = float(_get_secret("BACKEND_POLL_MAX_DELAY", "1.6"))

# --- ✅ 异步任务架构 (v2.0) ---
# 任务状态 TTL（秒），超时自动清理
TASK_TTL_SECONDS = int(_get_secret("TASK_TTL_SECONDS", "3600"))
# 任务轮询间隔（秒）
TASK_POLL_INTERVAL = float(_get_secret("TASK_POLL_INTERVAL", "0.5"))
# 任务最大轮询次数
TASK_MAX_POLLS = int(_get_secret("TASK_MAX_POLLS", "120"))
# MAX_CONTENT_LENGTH（字节），默认 500MB
MAX_CONTENT_LENGTH = int(_get_secret("MAX_CONTENT_LENGTH", "524288000"))

# --- ✅ 大文件分块 (v2.0) ---
# 文件读取块大小（字节），默认 10MB
FILE_CHUNK_SIZE = int(_get_secret("FILE_CHUNK_SIZE", "10485760"))
# 大文件阈值（字节），超过此值启用分块读取
LARGE_FILE_THRESHOLD = int(_get_secret("LARGE_FILE_THRESHOLD", "52428800"))

# --- ✅ 缓存磁盘持久化 (v2.0) ---
# 磁盘缓存 TTL（秒），默认 30 分钟
DISK_CACHE_TTL_SECONDS = int(_get_secret("DISK_CACHE_TTL_SECONDS", "1800"))
# 磁盘缓存目录
CACHE_DIR = _get_secret("CACHE_DIR", ".cache")

# --- ✅ 并行分析器 (v2.0) ---
# 分析器线程池大小
ANALYZER_POOL_SIZE = int(_get_secret("ANALYZER_POOL_SIZE", "4"))
# 分析器超时（秒）
ANALYZER_TIMEOUT = int(_get_secret("ANALYZER_TIMEOUT", "30"))

# --- ✅ 生产部署 (v2.0) ---
# Gunicorn worker 数量
GUNICORN_WORKERS = int(_get_secret("GUNICORN_WORKERS", "4"))
# Gunicorn 超时（秒）
GUNICORN_TIMEOUT = int(_get_secret("GUNICORN_TIMEOUT", "300"))