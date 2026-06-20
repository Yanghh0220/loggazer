# LogGazer - AI CI/CD 日志分析助手
# 主程序入口 (BFF Architecture)
#
# Data flow:
#   User Input (st.text_area) → httpx.AsyncClient → FastAPI /v1/analyze → AnalysisResult → UI Rendering
#
# Backend URL configured via LOGGAZER_API_URL env var (default: http://localhost:8000)

import os
import sys
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import streamlit as st
import httpx
from utils.performance import timer, lttb_downsample_1d
from error_handler import (
    classify_error,
    get_error_info,
    build_error_html,
    get_retry_action,
    can_retry,
    save_successful_result,
    get_last_successful_result,
    has_previous_result,
    estimate_analysis_time,
    friendly_api_error,
)
from resource_guard import get_file_size_limit, get_concurrency_limiter

# P0-3: 共享线程池（Streamlit 端 API 调用隔离）
_API_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="loggazer-api")

# P0 FIX-001: Register executor for graceful shutdown
import shutdown as _shutdown
_shutdown.register_executor("streamlit-api", _API_EXECUTOR)
_shutdown.install_signal_handlers()

# Backend API URL (configurable for local/cloud deployment)
BACKEND_URL = os.getenv(
    "LOGGAZER_API_URL", "http://127.0.0.1:8000"
).rstrip("/")

# Project root (where api/main.py lives)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================
# 可观测性初始化（全局单例）
# ============================================
import logging

logger = logging.getLogger(__name__)

# ============================================
# 页面导航（Sidebar 切换）
# ============================================
if "page" not in st.session_state:
    st.session_state["page"] = "analysis"

# 延迟初始化 ObservabilityManager（避免循环导入）
_observability = None


def _get_observability():
    """获取或初始化全局 ObservabilityManager 实例"""
    global _observability
    if _observability is None:
        try:
            import metrics_server
            from observability import ObservabilityManager
            import ai_engine

            # 尝试连接 Redis（可选，失败时降级到内存模式）
            redis_client = None
            try:
                import redis
                redis_client = redis.Redis(
                    host="localhost",
                    port=6379,
                    db=0,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                redis_client.ping()
                logger.info("Redis 连接成功")
            except Exception:
                logger.info("Redis 不可用，使用内存降级模式")
                redis_client = None

            # 创建 ObservabilityManager
            _observability = ObservabilityManager(
                redis_client=redis_client,
                monthly_budget=50.0,
                sampling_rate=0.1,  # 生产环境 10% 采样
            )

            # 注入到 ai_engine
            ai_engine.set_observability(_observability)

            # 启动 Metrics Server（独立线程，不阻塞 Streamlit）
            metrics_server.start(port=9090)

        except Exception as e:
            logger.warning("可观测性初始化失败（不影响核心功能）: %s", e)
    return _observability

# ============================================
#  BFF Helpers: BackendManager singleton + API call wrapper
# ============================================

@st.cache_resource
def _get_backend_manager():
    """
    Return a singleton BackendManager that persists across Streamlit reruns.

    Using @st.cache_resource ensures the manager (and the underlying PID file
    tracking) survives script re-execution, unlike session_state which is
    per-user-session and may be lost or corrupted on page refresh.
    """
    from backend_manager import BackendManager
    return BackendManager(backend_url=BACKEND_URL)


def check_backend_health() -> dict | None:
    """Check if the LogGazer Backend is reachable and healthy."""
    from backend_manager import check_backend_health as _check
    return _check(BACKEND_URL)


def call_analyze_via_api(log_text: str) -> dict:
    """
    Call LogGazer Backend API via HTTP (BFF pattern).

    Returns the full AnalyzeResponse as a dict, or raises with a user-friendly message.

    P0-3: 在 ThreadPoolExecutor 中执行 HTTP 调用，防止阻塞 Streamlit 主线程。
    """

    def _do_api_call() -> dict:
        with timer("frontend:API请求总耗时", record=True):
            try:
                with httpx.Client(timeout=180.0) as client:
                    resp = client.post(
                        f"{BACKEND_URL}/v1/analyze",
                        json={
                            "log_text": log_text,
                            "include_rag": True,
                            "cache_policy": "auto",
                        },
                        headers={
                            "Content-Type": "application/json",
                            "X-API-Key": os.getenv("LOGGAZER_API_KEY", ""),
                        },
                    )

                    if resp.status_code == 422:
                        detail = resp.json()
                        raise ValueError(detail.get("detail", str(detail)))
                    elif resp.status_code == 429:
                        detail = resp.json()
                        retry_after = resp.headers.get("Retry-After", "60")
                        raise RuntimeError(f"请求过于频繁，请在 {retry_after}s 后重试。")
                    elif resp.status_code == 503:
                        detail = resp.json()
                        raise RuntimeError(detail.get("detail", "服务暂时不可用，请稍后重试。"))
                    elif not resp.is_success:
                        detail = resp.json() if resp.text else {"detail": resp.reason_phrase}
                        raise ConnectionError(detail.get("detail", f"HTTP {resp.status_code}"))

                    return resp.json()

            except httpx.ConnectError:
                raise ConnectionError(
                    f"无法连接到 LogGazer Backend ({BACKEND_URL})。\n\n"
                    f"点击下方的「启动 Backend」按钮即可自动拉起后端。"
                )
            except httpx.TimeoutException:
                raise ConnectionError("分析请求超时，请检查网络或后端服务状态后重试。")

    # P0-3: 在线程池中执行，避免阻塞 Streamlit 事件循环
    future = _API_EXECUTOR.submit(_do_api_call)
    try:
        return future.result(timeout=190)  # 比 HTTP 超时多 10s 缓冲
    except FutureTimeoutError:
        future.cancel()
        raise ConnectionError("分析请求超时（超过 190 秒），请检查后端服务状态。")


# ============================================
#  BackendManager 初始化（尽早初始化，供 sidebar 使用）
# ============================================
manager = _get_backend_manager()

# ============================================
# 页面配置
# ============================================
st.set_page_config(
    page_title="LogGazer",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ============================================
# 全局样式 — 从 style.css 读取 (P1-1③: 缓存避免每帧重复读取)
# ============================================
# 为什么抽到独立文件？app.py 从 596 行降到 ~300 行，逻辑和样式分离
from pathlib import Path

@st.cache_data(ttl=3600, show_spinner=False)
def _load_css() -> str:
    """缓存加载 CSS 文件内容（P1-1③: 避免每次 rerun 都读文件）"""
    css_file = Path(__file__).parent / "style.css"
    return css_file.read_text(encoding="utf-8")

st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)

# ============================================
# 侧边栏
# ============================================
with st.sidebar:
    st.markdown("""
    <div style="padding: 8px 0 4px;">
        <div style="font-size: 1.2rem; font-weight: 700; color: #1a1a1a;">📋 LogGazer</div>
        <div style="font-size: 0.78rem; color: #a3a3a3; margin-top: 2px;">v1.1.1</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # ---- 页面导航 ----
    nav_col1, nav_col2 = st.columns(2)
    with nav_col1:
        if st.button("🔍 日志分析", use_container_width=True,
                      type="primary" if st.session_state["page"] == "analysis" else "secondary"):
            st.session_state["page"] = "analysis"
    with nav_col2:
        if st.button("📊 团队洞察", use_container_width=True,
                      type="primary" if st.session_state["page"] == "dashboard" else "secondary"):
            st.session_state["page"] = "dashboard"

    st.markdown("---")

    # 使用步骤
    st.markdown("""
    <div class="sidebar-section">
        <div class="sidebar-section-title">使用方法</div>
        <div class="step-row">
            <div class="step-num">1</div>
            <div class="step-text">粘贴构建失败日志</div>
        </div>
        <div class="step-row">
            <div class="step-num">2</div>
            <div class="step-text">点击「开始分析」</div>
        </div>
        <div class="step-row">
            <div class="step-num">3</div>
            <div class="step-text">查看分析结果与修复建议</div>
        </div>
        <div class="step-row">
            <div class="step-num">4</div>
            <div class="step-text">复制命令执行修复</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # 支持的日志类型
    st.markdown("""
    <div class="sidebar-section">
        <div class="sidebar-section-title">支持的日志</div>
        <div class="sidebar-item">GitHub Actions</div>
        <div class="sidebar-item">Jenkins</div>
        <div class="sidebar-item">Docker</div>
        <div class="sidebar-item">npm / pip / cargo</div>
        <div class="sidebar-item">pytest / jest</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # 技术栈
    st.markdown("""
    <div class="sidebar-section">
        <div class="sidebar-section-title">技术栈</div>
        <div class="sidebar-item">Python · Streamlit · DeepSeek</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # Backend status indicator — use manager directly (sidebar renders before backend_healthy is assigned)
    _backend_ok = manager.is_backend_running()
    if _backend_ok:
        st.markdown(
            '<div style="font-size: 0.75rem; color: #22c55e;">'
            '🟢 Backend 运行中</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div style="font-size: 0.75rem; color: #ef4444;">'
            '🔴 Backend 未连接</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # P0-2: 清除缓存按钮
    cache_col1, cache_col2 = st.columns([2, 1])
    with cache_col1:
        if st.button("🗑️ 清除缓存", use_container_width=True,
                      key="clear_cache_btn",
                      help="清除所有分析结果缓存，下次分析将重新计算"):
            try:
                from analyzer import clear_content_cache, get_content_cache_stats
                cleared = clear_content_cache()
                st.toast(f"✅ 已清除 {cleared} 条缓存", icon="🗑️")
            except Exception as e:
                st.toast(f"⚠️ 清除缓存失败: {e}", icon="❌")
    with cache_col2:
        if st.button("📊", use_container_width=True,
                      key="cache_stats_btn",
                      help="查看缓存统计"):
            try:
                from analyzer import get_content_cache_stats
                stats = get_content_cache_stats()
                st.toast(
                    f"分析缓存: {stats['analysis_cache_size']}/{stats['analysis_cache_maxsize']} | "
                    f"解析缓存: {stats['parsed_cache_size']}/{stats['parsed_cache_maxsize']}",
                    icon="📊"
                )
            except Exception:
                pass

    st.markdown("""
    <div style="font-size: 0.75rem; color: #a3a3a3;">
        Made with care by LogGazer
    </div>
    """, unsafe_allow_html=True)

# ============================================
# 页面路由
# ============================================
if st.session_state["page"] == "dashboard":
    # ---- 团队洞察 Dashboard ----
    st.markdown("""
    <div class="page-header">
        <div class="page-title">📊 团队洞察 Dashboard</div>
        <div class="page-desc">错误指纹聚类 · 趋势分析 · 修复建议聚合</div>
    </div>
    """, unsafe_allow_html=True)

    try:
        from cluster_engine import get_cluster_engine
        from analytics_dashboard import (
            generate_weekly_report,
            get_trend_chart_data,
            get_platform_distribution,
        )

        cluster_engine = get_cluster_engine()

        # 总览指标
        insight_col1, insight_col2, insight_col3 = st.columns(3)

        conn = cluster_engine._get_conn()
        try:
            total_analyses = conn.execute(
                "SELECT COUNT(*) FROM analysis_log"
            ).fetchone()[0]
            active_clusters = conn.execute(
                "SELECT COUNT(*) FROM error_cluster WHERE is_active = 1"
            ).fetchone()[0]
            resolved_count = conn.execute(
                "SELECT COUNT(*) FROM analysis_log "
                "WHERE resolution_status = 'resolved'"
            ).fetchone()[0]
        finally:
            conn.close()

        with insight_col1:
            st.metric("总分析次数", total_analyses)
        with insight_col2:
            st.metric("活跃错误簇", active_clusters)
        with insight_col3:
            st.metric("已解决", resolved_count)

        st.markdown("---")

        # 趋势图 + 平台分布
        chart_col1, chart_col2 = st.columns([2, 1])

        with chart_col1:
            st.subheader("📈 每日分析趋势")
            trend_data = get_trend_chart_data(cluster_engine, days=7)
            if trend_data["counts"]:
                # P1-1②: LTTB 降采样 — 超过 500 点时自动压缩
                counts = trend_data["counts"]
                dates = trend_data["dates"]
                if len(counts) > 500:
                    # 保留日期标签的关键点
                    sampled_counts = lttb_downsample_1d(counts, threshold=500)
                    # 同步日期标签（取降采样后对应位置的标签）
                    step = len(counts) / len(sampled_counts) if sampled_counts else 1
                    sampled_dates = [dates[min(int(i * step), len(dates) - 1)]
                                    for i in range(len(sampled_counts))]
                    chart_data = dict(zip(sampled_dates, sampled_counts))
                    st.caption(f"📉 已优化显示（共 {len(counts)} 个数据点，降采样至 {len(sampled_counts)} 个）")
                else:
                    chart_data = dict(zip(dates, counts))
                st.line_chart(chart_data, use_container_width=True)
            else:
                st.info("暂无数据，分析日志后将自动记录趋势。")

        with chart_col2:
            st.subheader("🖥️ 平台分布")
            # P1-1③: 缓存平台分布数据
            @st.cache_data(ttl=120, show_spinner=False)
            def _cached_platform_distribution():
                return get_platform_distribution(cluster_engine)

            platform_data = _cached_platform_distribution()
            if platform_data:
                # P1-1②: 超过 20 个平台时降采样（保留 Top-20）
                if len(platform_data) > 20:
                    sorted_items = sorted(platform_data.items(), key=lambda x: -x[1])
                    platform_data = dict(sorted_items[:20])
                    st.caption(f"📉 已优化显示（仅展示 Top-20 平台）")
                st.bar_chart(platform_data, use_container_width=True)
            else:
                st.info("暂无数据。")

        st.markdown("---")

        # Top 高频簇 (P1-1①: 分页展示)
        st.subheader("🔥 高频错误簇")

        # P1-1③: 缓存 trending 数据，避免每次 rerun 重新查询
        @st.cache_data(ttl=60, show_spinner=False)
        def _cached_trending_clusters(days: int, top_n: int):
            return cluster_engine.get_trending_clusters(days=days, top_n=top_n)

        trending = _cached_trending_clusters(days=7, top_n=50)

        if trending:
            # P1-1①: 分页控制
            if "cluster_page" not in st.session_state:
                st.session_state["cluster_page"] = 1

            page_size = 5
            total_clusters = len(trending)
            total_pages = max(1, (total_clusters + page_size - 1) // page_size)
            page = st.session_state["cluster_page"]

            # 确保页码有效
            if page > total_pages:
                st.session_state["cluster_page"] = total_pages
                page = total_pages

            start_idx = (page - 1) * page_size
            end_idx = min(start_idx + page_size, total_clusters)
            page_clusters = trending[start_idx:end_idx]

            for cluster in page_clusters:
                cid = cluster.get("cluster_id", "?")
                count = cluster.get("recent_count", 0)
                total = cluster.get("occurrence_count", 0)
                dist = cluster.get("platform_distribution", {})
                platforms = ", ".join(
                    f"{k}({v})" for k, v in sorted(
                        dist.items(), key=lambda x: -x[1]
                    )
                )
                severity = cluster.get("avg_severity_score", 0) or 0
                severity_icon = (
                    "🔴" if severity >= 3.5
                    else "🟠" if severity >= 2.5
                    else "🟡" if severity >= 1.5
                    else "🟢"
                )

                with st.expander(
                    f"{severity_icon} 簇 #{cid} — 本周 {count} 次 (累计 {total} 次) "
                    f"| {platforms or 'N/A'}"
                ):
                    samples = cluster.get("representative_samples", [])
                    if samples:
                        st.markdown("**代表性错误**:")
                        for s in samples[:2]:
                            fp_text = s.get("fingerprint", "N/A")
                            if isinstance(fp_text, str):
                                fp_text = fp_text[:200]
                            st.code(fp_text, language="text")

                    fixes = cluster.get("top_fix_suggestions", [])
                    if fixes:
                        st.markdown("**常用修复命令**:")
                        for fix in fixes[:2]:
                            cmd = fix.get("command", "N/A")
                            if isinstance(cmd, str):
                                st.code(cmd, language="bash")

                    avg_resolve = cluster.get("avg_resolution_time_minutes")
                    if avg_resolve:
                        st.markdown(
                            f"**平均解决时间**: {avg_resolve:.0f} 分钟"
                        )

            # P1-1①: 翻页控件
            if total_pages > 1:
                page_col1, page_col2, page_col3, page_col4, page_col5 = st.columns([1, 1, 2, 1, 1])
                with page_col1:
                    if st.button("◀◀ 首页", disabled=(page <= 1), key="cluster_first",
                                 use_container_width=True):
                        st.session_state["cluster_page"] = 1
                        st.rerun()
                with page_col2:
                    if st.button("◀ 上一页", disabled=(page <= 1), key="cluster_prev",
                                 use_container_width=True):
                        st.session_state["cluster_page"] = max(1, page - 1)
                        st.rerun()
                with page_col3:
                    st.markdown(
                        f"<div style='text-align:center;padding-top:5px;'>"
                        f"第 {page}/{total_pages} 页 (共 {total_clusters} 个簇)</div>",
                        unsafe_allow_html=True,
                    )
                with page_col4:
                    if st.button("下一页 ▶", disabled=(page >= total_pages), key="cluster_next",
                                 use_container_width=True):
                        st.session_state["cluster_page"] = min(total_pages, page + 1)
                        st.rerun()
                with page_col5:
                    if st.button("末页 ▶▶", disabled=(page >= total_pages), key="cluster_last",
                                 use_container_width=True):
                        st.session_state["cluster_page"] = total_pages
                        st.rerun()
        else:
            st.info("暂无错误簇数据。分析日志后将自动聚类。")

        st.markdown("---")

        # 完整周报
        st.subheader("📋 完整周报")
        with st.expander("展开查看 Markdown 周报", expanded=False):
            report = generate_weekly_report(cluster_engine)
            st.code(report, language="markdown")

    except Exception as e:
        st.error(f"Dashboard 加载失败: {e}")
        st.info("请先分析一些日志，系统将自动构建错误指纹和聚类。")

    # 页脚后直接结束
    st.markdown("""
    <div class="footer">
        LogGazer · Error Fingerprinting & Intelligent Clustering
    </div>
    """, unsafe_allow_html=True)
    st.stop()


# ============================================
# 示例日志（分析页面）
# ============================================
SAMPLE_LOGS = {
    "npm 依赖冲突": """npm ERR! code ERESOLVE
npm ERR! ERESOLVE could not resolve
npm ERR! While resolving: react-scripts@5.0.1
npm ERR! Found: react@18.2.0
npm ERR! node_modules/react
npm ERR!   react@"^18.2.0" from the root project
npm ERR!
npm ERR! Conflicting peer dependency: react@17.0.2
npm ERR! node_modules/react
npm ERR!   peer react@"^17.0.0" from @testing-library/react@11.2.7
npm ERR!
npm ERR! Fix the upstream dependency conflict, or retry
npm ERR! this command with --force or --legacy-peer-deps""",

    "Docker 构建失败": """Step 4/8 : RUN pip install -r requirements.txt
 ---> Running in 5a3b2c1d9e0f
ERROR: Could not find a version that satisfies the requirement tensorflow==2.15.0
ERROR: No matching distribution found for tensorflow==2.15.0
The command '/bin/sh -c pip install -r requirements.txt' returned a non-zero code: 1""",

    "Python 测试失败": """========================= FAILURES =========================
_______ test_user_login _______

    def test_user_login():
        response = client.post("/api/login", json={"username": "test", "password": "123"})
>       assert response.status_code == 200
E       assert 401 == 200
E        +  where 401 = <Response [401]>.status_code

tests/test_auth.py:15: AssertionError
========================= 1 failed, 12 passed ========================="""
}

# ============================================
# P0-1: 非阻塞后端健康检查 + 后台自动启动
# ============================================
# 问题：之前每次页面加载都会同步阻塞等待后端启动 (timeout=8s)，导致白屏。
# 修复：页面加载时只做快速健康检查（HTTP 请求 <1s）。如果后端未运行，
# 在后台线程中启动，前端立即渲染完整 UI。用户点击分析时才真正等待。
#
# BackendManager 使用 PID 文件 + 端口检测判断后端状态，
# 不依赖 session_state，因此 F5 刷新 / Streamlit rerun 都不会
# 导致状态丢失或错误。

# 初始化 session_state 中的后端启动状态
if "_backend_starting" not in st.session_state:
    st.session_state["_backend_starting"] = False

# P2-3: 保存上次成功分析结果（错误时不清空）
if "last_successful_result" not in st.session_state:
    st.session_state["last_successful_result"] = None

# P2-2①: 预处理状态追踪
if "preprocess_triggered" not in st.session_state:
    st.session_state["preprocess_triggered"] = False
if "preprocess_done" not in st.session_state:
    st.session_state["preprocess_done"] = False
if "preprocess_task_id" not in st.session_state:
    st.session_state["preprocess_task_id"] = None

# P2-1: 分析阶段追踪
if "analysis_error_info" not in st.session_state:
    st.session_state["analysis_error_info"] = None
if "analysis_retry_count" not in st.session_state:
    st.session_state["analysis_retry_count"] = 0

# 快速健康检查（仅 HTTP 请求，不等待进程启动）
backend_healthy = manager.is_backend_running()

if not backend_healthy and not st.session_state["_backend_starting"]:
    # 后端未运行 → 在后台线程中启动（非阻塞），前端立即渲染
    st.session_state["_backend_starting"] = True

    def _background_start_backend():
        """后台线程：启动后端进程并等待就绪（不阻塞 UI）"""
        try:
            manager.ensure_backend(timeout=30.0)
        except Exception:
            pass

    threading.Thread(target=_background_start_backend, daemon=True).start()

# 如果后台线程已完成启动，更新状态
if st.session_state["_backend_starting"] and manager.is_backend_running():
    st.session_state["_backend_starting"] = False
    backend_healthy = True

# ============================================
# 标题
# ============================================
st.markdown("""
<div class="page-header">
    <div class="page-title">📋 LogGazer</div>
    <div class="page-desc">粘贴构建失败日志，快速定位问题并获取修复建议</div>
</div>
""", unsafe_allow_html=True)

# 功能说明
st.markdown("""
<div class="features">
    <div class="feature-item">
        <div class="feature-icon">⚡</div>
        <div>
            <div class="feature-text-title">快速分析</div>
            <div class="feature-text-desc">精确定位错误根因</div>
        </div>
    </div>
    <div class="feature-item">
        <div class="feature-icon">📖</div>
        <div>
            <div class="feature-text-title">中文解读</div>
            <div class="feature-text-desc">通俗易懂的解释</div>
        </div>
    </div>
    <div class="feature-item">
        <div class="feature-icon">🔧</div>
        <div>
            <div class="feature-text-title">修复命令</div>
            <div class="feature-text-desc">可直接复制执行</div>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

# ============================================
# 示例日志按钮
# ============================================
st.markdown('<div class="sample-label">试试示例</div>', unsafe_allow_html=True)
cols = st.columns([1, 1, 1, 2])
sample_names = list(SAMPLE_LOGS.keys())
selected_sample = None

for i, name in enumerate(sample_names):
    with cols[i]:
        if st.button(name, key=f"sample_{i}", use_container_width=True):
            selected_sample = name

# ============================================
# ✅ 优化点: 输入模式切换 — 文本粘贴 / 文件上传
# ============================================
input_col1, input_col2 = st.columns([1, 1])
with input_col1:
    input_mode = st.radio(
        "输入方式",
        ["📝 粘贴文本", "📁 上传文件"],
        horizontal=True,
        key="input_mode",
        label_visibility="collapsed",
    )
    # 默认值
    if "input_mode" not in st.session_state:
        st.session_state["input_mode"] = "📝 粘贴文本"

# ============================================
# ✅ 优化点: 文件上传模式 — 异步轮询架构
# ============================================
# 设计: 上传文件 → 获得 task_id → 每 500ms 轮询 → 实时更新进度条 → 分步渲染结果

if st.session_state.get("input_mode") == "📁 上传文件":
    uploaded_file = st.file_uploader(
        "上传日志文件（支持 .log, .txt, .json, 最大 500MB）",
        type=["log", "txt", "json", "out", "err"],
        key="log_file_uploader",
        help="上传后自动开始异步分析，无需手动点击分析按钮",
    )

    if uploaded_file is not None:
        file_size_mb = len(uploaded_file.getvalue()) / (1024 * 1024)

        # 文件大小指示
        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;
                    padding:8px 14px;background:#f0fdf4;border:1px solid #bbf7d0;
                    border-radius:8px;font-size:0.82rem;">
            <span style="color:#16a34a;font-weight:600;">📁 {uploaded_file.name}</span>
            <span style="color:#737373;">·</span>
            <span style="color:#525252;">{file_size_mb:.1f} MB</span>
            <span style="color:#737373;">·</span>
            <span style="color:#525252;">⏱️ 预计 {max(3, file_size_mb * 2):.0f}s 完成</span>
        </div>
        """, unsafe_allow_html=True)

        # ✅ 优化点: 异步上传 + 轮询
        if "upload_task_id" not in st.session_state:
            st.session_state["upload_task_id"] = None
        if "upload_progress" not in st.session_state:
            st.session_state["upload_progress"] = 0.0
        if "upload_status" not in st.session_state:
            st.session_state["upload_status"] = "idle"
        if "upload_result" not in st.session_state:
            st.session_state["upload_result"] = None

        if st.button("🚀 开始分析", type="primary", use_container_width=True,
                     disabled=st.session_state["upload_status"] == "running",
                     key="upload_analyze_btn"):
            st.session_state["upload_status"] = "running"
            st.session_state["upload_progress"] = 0.0
            st.session_state["upload_result"] = None

            # 异步上传
            try:
                log_content = uploaded_file.getvalue().decode("utf-8", errors="replace")
                with httpx.Client(timeout=180.0) as client:
                    resp = client.post(
                        f"{BACKEND_URL}/api/upload",
                        files={"file": (uploaded_file.name, log_content, "text/plain")},
                    )
                    if resp.status_code == 202:
                        data = resp.json()
                        st.session_state["upload_task_id"] = data["task_id"]
                        st.session_state["upload_status"] = "polling"
                    else:
                        st.error(f"上传失败: {resp.text[:500]}")
                        st.session_state["upload_status"] = "error"
            except Exception as e:
                st.error(f"上传失败: {str(e)[:500]}")
                st.session_state["upload_status"] = "error"

        # ✅ 优化点: 轮询逻辑（每 500ms）
        if st.session_state["upload_status"] == "polling":
            task_id = st.session_state["upload_task_id"]
            progress_placeholder = st.empty()
            status_placeholder = st.empty()

            max_polls = 120  # 最多轮询 60 秒
            for attempt in range(max_polls):
                try:
                    with httpx.Client(timeout=5.0) as client:
                        resp = client.get(f"{BACKEND_URL}/api/task/{task_id}")
                        if resp.is_success:
                            task_data = resp.json()
                            status = task_data["status"]
                            progress = task_data.get("progress", 0.0)

                            st.session_state["upload_progress"] = progress

                            # ✅ 优化点: 实时进度条
                            progress_placeholder.progress(
                                progress,
                                text=f"{status.upper()} — {progress * 100:.0f}%"
                            )

                            if status == "parsing":
                                status_placeholder.info("📝 正在解析日志...")
                            elif status == "analyzing":
                                status_placeholder.info(f"🔬 正在并行分析... ({progress * 100:.0f}%)")

                            if status == "completed":
                                st.session_state["upload_status"] = "completed"
                                st.session_state["upload_result"] = task_data.get("result")
                                progress_placeholder.progress(1.0, text="✅ 分析完成 — 100%")
                                status_placeholder.success(
                                    f"✅ 分析完成！耗时 {task_data.get('duration_ms', 0):.0f}ms"
                                )
                                break
                            elif status == "failed":
                                st.session_state["upload_status"] = "error"
                                progress_placeholder.error(f"❌ 分析失败")
                                status_placeholder.error(task_data.get("error", "Unknown error")[:500])
                                break
                except Exception:
                    pass  # 轮询失败时静默重试

                time.sleep(0.5)  # ✅ 500ms 轮询间隔
            else:
                st.session_state["upload_status"] = "timeout"
                progress_placeholder.warning("⏰ 轮询超时，分析可能仍在后台进行")

        # ✅ 优化点: 分步渲染结果（分 Tab/Card 展示）
        if st.session_state["upload_status"] == "completed" and st.session_state["upload_result"]:
            result = st.session_state["upload_result"]
            _render_analysis_result(result, {"cache_status": "miss"})

            # 重置状态（允许重新上传）
            if st.button("🔄 分析新文件", key="reset_upload"):
                st.session_state["upload_task_id"] = None
                st.session_state["upload_status"] = "idle"
                st.session_state["upload_result"] = None
                st.session_state["upload_progress"] = 0.0
                st.rerun()

    st.markdown("---")
    st.caption("💡 或者切换到「📝 粘贴文本」模式直接粘贴日志内容")

# ============================================
# 日志输入（文本粘贴模式）
# ============================================
else:
    if selected_sample:
        st.session_state["log_input"] = SAMPLE_LOGS[selected_sample]

    log_input = st.text_area(
        label="构建日志",
        height=260,
        placeholder="在此粘贴构建失败日志...",
        value=st.session_state.get("log_input", ""),
        key="log_input",
        label_visibility="collapsed"
    )

# ============================================
#  P2-1③: 文件大小指示器 + 预估时间 + P2-4① 大小检查
# ============================================
# 在用户输入后、点击分析前，显示文件大小和预估时间
_log_size = len(log_input) if log_input else 0
if _log_size > 0:
    _log_kb = _log_size / 1024
    _log_lines = log_input.count('\n') + 1
    _est_seconds, _est_desc = estimate_analysis_time(log_input)
    fs_limit = get_file_size_limit()
    _is_valid_size, _size_warn, _size_err = fs_limit.check(log_input)

    if _size_err:
        st.error(f"⚠️ {_size_err}")
    elif _size_warn:
        st.warning(_size_warn)

    # 文件大小指示器
    _size_color = "#ef4444" if _log_kb > 80 else "#f59e0b" if _log_kb > 30 else "#22c55e"
    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;
                padding:8px 14px;background:#f9fafb;border:1px solid #e5e5e5;
                border-radius:8px;font-size:0.82rem;">
        <span style="color:{_size_color};font-weight:600;">{_log_kb:.1f} KB</span>
        <span style="color:#737373;">·</span>
        <span style="color:#737373;">{_log_lines} 行</span>
        <span style="color:#737373;">·</span>
        <span style="color:#525252;">⏱️ {_est_desc}</span>
    </div>
    """, unsafe_allow_html=True)

# P2-2①: 预处理触发 — 检测输入变化，后台预处理
if _log_size > 100 and not st.session_state["preprocess_triggered"]:
    st.session_state["preprocess_triggered"] = True
    # 在后台线程中触发预处理（不阻塞 UI）
    def _trigger_preprocess():
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(
                    f"{BACKEND_URL}/v1/preprocess",
                    json={"log_text": log_input, "include_rag": True, "cache_policy": "auto"},
                )
                if resp.is_success:
                    data = resp.json()
                    st.session_state["preprocess_task_id"] = data.get("task_id")
        except Exception:
            pass  # 静默失败，预处理是优化而非必需
    threading.Thread(target=_trigger_preprocess, daemon=True).start()
elif _log_size == 0:
    # 输入被清空，重置预处理状态
    st.session_state["preprocess_triggered"] = False
    st.session_state["preprocess_done"] = False
    st.session_state["preprocess_task_id"] = None

# 分析按钮
col1, col2, col3 = st.columns([2, 1, 2])
with col2:
    # P2-1④: 按钮即时反馈 — 分析中时禁用按钮
    _analyzing = st.session_state.get("analysis_phase") == "running"
    analyze_clicked = st.button(
        "⏳ 分析中..." if _analyzing else "开始分析",
        type="primary",
        use_container_width=True,
        disabled=_analyzing,
    )

# ============================================
#  Backend Recovery Panel (shared UI)
# ============================================

def _show_backend_recovery_panel(mgr):
    """Show a compact recovery panel when the backend is unreachable."""
    st.warning(
        f"⚠️ **LogGazer Backend 未就绪**\n\n"
        f"无法连接到 `{mgr.backend_url}`。"
    )

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if st.button("🚀 启动 Backend", type="primary", use_container_width=True,
                      key="recover_start"):
            with st.spinner("正在启动 Backend..."):
                ready = mgr.ensure_backend(timeout=25.0)
            if ready:
                st.toast("✅ Backend 已就绪", icon="✅")
                st.rerun()
            else:
                st.error("启动失败，请检查 Python 环境。"
                         f"手动运行: `{sys.executable} -m api.main`")
    with col_b:
        if st.button("🔄 重试连接", use_container_width=True,
                      key="recover_retry"):
            if mgr.is_backend_running():
                st.toast("✅ Backend 已就绪", icon="✅")
                st.rerun()
            else:
                st.toast("❌ 仍无法连接", icon="❌")

    # Show backend stderr log if available
    from backend_manager import BACKEND_LOG
    if BACKEND_LOG.exists():
        with st.expander("🔍 查看后端启动日志", expanded=False):
            try:
                log_content = BACKEND_LOG.read_text(encoding="utf-8", errors="replace")
                st.code(log_content[-3000:], language="text")
            except Exception:
                st.caption("(无法读取日志文件)")

    st.caption(f"或手动运行: `{sys.executable} -m api.main`")


# ============================================================
#  P2-1②: 结果渲染函数（复用：正常展示 + 错误后展示缓存结果）
# ============================================================

def _render_analysis_result(result, meta: dict | None = None):
    """渲染分析结果。result 可以是 AnalysisResult 实例或 dict。"""
    with timer("frontend:数据渲染", record=True):
        # ---- 元数据（缓存命中时快速提示） ----
        if meta and meta.get("cache_status") == "hit":
            st.info(
                f"⚡ 缓存命中 (耗时 {meta.get('duration_ms', 0):.0f}ms)",
                icon="⚡"
            )

        # ---- 安全警告 ----
        security_warning = result.get("security_warning", "") if isinstance(result, dict) else getattr(result, "security_warning", "")
        if security_warning:
            st.warning(f"⚠️ 安全提示：{security_warning}")

        # ---- 严重程度标签 ----
        severity = result.get("severity", "medium") if isinstance(result, dict) else getattr(result, "severity", "medium")
        severity_colors = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }
        severity_icon = severity_colors.get(severity, "⚪")
        st.markdown(f"""
        <div class="result-card result-card-left" style="border-left-color: #6b7280;">
            <div class="card-title">严重程度 {severity_icon} {severity.upper()}</div>
        </div>
        """, unsafe_allow_html=True)

        # ---- 错误摘要 ----
        error_summary = result.get("error_summary", "无") if isinstance(result, dict) else getattr(result, "error_summary", "无")
        st.markdown(f"""
        <div class="result-card result-card-left red">
            <div class="card-title">错误摘要</div>
            <div class="card-body">{error_summary}</div>
        </div>
        """, unsafe_allow_html=True)

        # ---- 关键错误信息 ----
        error_detail = result.get("error_detail", "无") if isinstance(result, dict) else getattr(result, "error_detail", "无")
        st.markdown("""
        <div class="result-card result-card-left red">
            <div class="card-title">关键错误信息</div>
        </div>
        """, unsafe_allow_html=True)
        st.code(error_detail, language="bash")

        # ---- 根因分析（结构化展示） ----
        root_causes = result.get("root_causes", []) if isinstance(result, dict) else getattr(result, "root_causes", [])
        if root_causes:
            causes_html = ""
            for i, cause in enumerate(root_causes, 1):
                desc = cause.get("description", "") if isinstance(cause, dict) else getattr(cause, "description", "")
                prob = cause.get("probability", 0) if isinstance(cause, dict) else getattr(cause, "probability", 0)
                bar_width = max(prob, 2)
                causes_html += f"""
                <div style="margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 2px;">
                        <span style="font-weight: 600; min-width: 36px;">{prob}%</span>
                        <span style="font-size: 0.9rem;">{desc}</span>
                    </div>
                    <div style="background: #e5e7eb; border-radius: 4px; height: 6px; overflow: hidden;">
                        <div style="background: #3b82f6; height: 100%; width: {bar_width}%; border-radius: 4px;"></div>
                    </div>
                </div>"""

            st.markdown(f"""
            <div class="result-card result-card-left blue">
                <div class="card-title">根因分析</div>
                <div class="card-body">{causes_html}</div>
            </div>
            """, unsafe_allow_html=True)

        # ---- 修复建议 ----
        suggestions = result.get("fix_suggestions", []) if isinstance(result, dict) else getattr(result, "fix_suggestions", [])
        if suggestions:
            items_html = ""
            for i, s in enumerate(suggestions, 1):
                title = s.get("title", "无标题") if isinstance(s, dict) else getattr(s, "title", "无标题")
                desc = s.get("description", "") if isinstance(s, dict) else getattr(s, "description", "")
                safety = s.get("safety_level", "safe") if isinstance(s, dict) else getattr(s, "safety_level", "safe")
                safety_badge = {
                    "safe": "🟢 安全",
                    "review": "🟡 需审核",
                    "dangerous": "🔴 危险",
                }.get(safety, "")
                items_html += f"""
                <div class="fix-item">
                    <div class="fix-title">
                        <span class="fix-num">{i}</span>{title}
                        <span style="font-size: 0.75rem; color: #6b7280; margin-left: 8px;">{safety_badge}</span>
                    </div>
                    <div class="fix-desc">{desc}</div>
                </div>"""

            st.markdown(f"""
            <div class="result-card result-card-left green">
                <div class="card-title">修复建议</div>
                {items_html}
            </div>
            """, unsafe_allow_html=True)

            # 修复命令
            for i, s in enumerate(suggestions, 1):
                cmd = s.get("command", "") if isinstance(s, dict) else getattr(s, "command", "")
                safety = s.get("safety_level", "safe") if isinstance(s, dict) else getattr(s, "safety_level", "safe")
                if not cmd:
                    continue
                if safety == "dangerous":
                    st.error(
                        f"🚫 **方案 {i} 的命令已被安全系统拦截**\n\n"
                        f"命令 `{cmd[:80]}...` 触发了危险模式匹配，"
                        f"请人工审核后再决定是否执行。"
                    )
                elif safety == "review":
                    st.warning(
                        f"⚠️ **方案 {i} 的命令需要管理员权限 / 影响范围较大，请确认后再执行**"
                    )
                    st.code(cmd, language="bash")
                else:
                    st.code(cmd, language="bash")

        # ---- 排查命令 ----
        debug_cmds = result.get("debug_commands", []) if isinstance(result, dict) else getattr(result, "debug_commands", [])
        if debug_cmds:
            st.markdown("""
            <div class="result-card result-card-left purple">
                <div class="card-title">排查命令</div>
            </div>
            """, unsafe_allow_html=True)
            for cmd in debug_cmds:
                st.code(cmd, language="bash")

        # ---- 预防建议 ----
        prevention = result.get("prevention", []) if isinstance(result, dict) else getattr(result, "prevention", [])
        if prevention:
            prevention_items = ""
            for tip in prevention:
                prevention_items += f"<li>{tip}</li>"
            st.markdown(f"""
            <div class="result-card result-card-left" style="border-left-color: #8b5cf6;">
                <div class="card-title">预防建议</div>
                <div class="card-body"><ul style="margin: 0; padding-left: 1.2rem;">{prevention_items}</ul></div>
            </div>
            """, unsafe_allow_html=True)


# ============================================================
#  P2-1①: 流式分析调用（带进度事件）
# ============================================================

def call_analyze_stream(log_text: str):
    """
    使用 NDJSON 流式端点进行带进度反馈的分析。

    每次 yield 一个事件 dict：
      {"type": "progress", "step": "...", "elapsed_ms": ...}
      {"type": "result", "result": {...}, "meta": {...}}
      {"type": "error", "error": "...", "error_type": "..."}
    """
    try:
        with httpx.Client(timeout=180.0) as client:
            with client.stream(
                "POST",
                f"{BACKEND_URL}/v1/analyze/stream",
                json={
                    "log_text": log_text,
                    "include_rag": True,
                    "cache_policy": "auto",
                },
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": os.getenv("LOGGAZER_API_KEY", ""),
                },
            ) as response:
                if response.status_code == 422:
                    detail = response.json()
                    raise ValueError(detail.get("detail", str(detail)))
                elif response.status_code == 429:
                    raise RuntimeError("请求过于频繁，请稍后重试。")
                elif response.status_code >= 500:
                    raise ConnectionError(
                        response.json().get("detail", f"服务端错误 (HTTP {response.status_code})")
                    )
                elif not response.is_success:
                    raise ConnectionError(f"HTTP {response.status_code}")

                for line in response.iter_lines():
                    if line:
                        yield json.loads(line)

    except httpx.ConnectError:
        raise ConnectionError(
            f"无法连接到 LogGazer Backend ({BACKEND_URL})。\n\n"
            f"点击下方的「启动 Backend」按钮即可自动拉起后端。"
        )
    except httpx.TimeoutException:
        raise ConnectionError("分析请求超时，请检查网络或后端服务状态后重试。")


# ============================================
# 分析 + 结果展示 (BFF Pattern) — P2 增强版
# ============================================
if analyze_clicked:
    if not log_input.strip():
        # P2-3①: 友好错误提示（不显示技术堆栈）
        st.markdown(build_error_html("empty_input", BACKEND_URL), unsafe_allow_html=True)
    else:
        # ---- P0-1: 指数退避轮询后端健康检查 ----
        if not manager.is_backend_running():
            max_attempts = 10
            base_delay = 0.1

            with st.status("⏳ 正在启动后端...") as poll_status:
                for attempt in range(1, max_attempts + 1):
                    poll_status.update(
                        label=f"⏳ 后端启动中，请稍候... ({attempt}/{max_attempts})"
                    )
                    delay = min(base_delay * (2 ** (attempt - 1)), 1.6)
                    time.sleep(delay)

                    if manager.is_backend_running():
                        poll_status.update(
                            label="✅ 后端已就绪！", state="complete"
                        )
                        backend_healthy = True
                        st.session_state["_backend_starting"] = False
                        break
                else:
                    poll_status.update(
                        label="❌ 后端启动超时", state="error"
                    )
                    _show_backend_recovery_panel(manager)
                    st.stop()

        if not manager.is_backend_running():
            _show_backend_recovery_panel(manager)
            st.stop()

        # 初始化可观测性
        obs = _get_observability()

        # ---- 限流检查 ----
        if obs:
            allowed, retry_after = obs.check_rate_limit(
                user_id="anonymous",
                max_requests=5,
                window_seconds=60,
            )
            if not allowed:
                st.markdown(build_error_html("rate_limit", BACKEND_URL), unsafe_allow_html=True)
                st.stop()

        # ---- 成本熔断器检查 ----
        if obs:
            cb_status = obs.check_cost_circuit_breaker()
            if cb_status == "tripped":
                st.markdown(build_error_html("circuit_breaker", BACKEND_URL), unsafe_allow_html=True)
                st.stop()
            elif cb_status == "warning":
                st.warning("⚠️ 本月分析额度已使用 80% 以上，请注意控制用量。")

        # ---- P2-4①: 前端文件大小验证（双重保险） ----
        fs_limit = get_file_size_limit()
        _valid, _warn, _err = fs_limit.check(log_input)
        if not _valid:
            st.markdown(build_error_html("file_too_large", BACKEND_URL), unsafe_allow_html=True)
            st.stop()

        # ---- 带追踪的分析 (BFF: HTTP call to FastAPI) ----
        if obs:
            obs.increment_active_requests()

        # P2-1: 设置分析状态
        st.session_state["analysis_phase"] = "running"
        result = None
        meta = None
        analysis_error = None

        # P2-1①: 使用 st.status() 实现实时进度反馈
        # P2-1③: 展示预估时间
        _est_sec, _est_desc = estimate_analysis_time(log_input)
        with st.status(
            f"⏳ 正在分析中... ({_est_desc})",
            expanded=True,
        ) as analysis_status:
            try:
                # P2-1: 尝试流式分析（带进度事件），失败时回退到常规调用
                try:
                    for event in call_analyze_stream(log_input):
                        etype = event.get("type", "")
                        if etype == "progress":
                            step = event.get("step", "")
                            elapsed = event.get("elapsed_ms", 0)
                            if step == "preprocessing":
                                analysis_status.update(
                                    label=f"✅ 日志解析完成 ({elapsed:.0f}ms) — "
                                          f"平台: {event.get('platform', 'Unknown')}"
                                )
                            elif step == "cache_check":
                                hit = event.get("cache_hit", False)
                                analysis_status.update(
                                    label=f"{'⚡ 缓存命中' if hit else '🔍 语义检索完成'} ({elapsed:.0f}ms)"
                                )
                            elif step == "ai_analysis":
                                analysis_status.update(
                                    label=f"🤖 AI 分析完成 ({elapsed:.0f}ms)"
                                )
                        elif etype == "result":
                            analysis_status.update(
                                label="✅ 分析完成！", state="complete"
                            )
                            result = event.get("result", {})
                            meta = event.get("meta", {})
                            break
                        elif etype == "error":
                            analysis_status.update(
                                label="❌ 分析失败", state="error"
                            )
                            raise RuntimeError(
                                event.get("error", "Unknown streaming error")
                            )
                except (httpx.ConnectError, httpx.TimeoutException):
                    raise
                except ConnectionError:
                    raise
                except Exception as stream_err:
                    # 流式失败 → 回退到常规 API 调用
                    logger = logging.getLogger(__name__)
                    logger.warning("流式分析失败，回退到常规调用: %s", stream_err)
                    analysis_status.update(label="⏳ 流式不可用，使用常规模式...")

                    api_response = call_analyze_via_api(log_input)
                    result = api_response.get("result", {})
                    meta = api_response.get("meta", {})

                    if meta.get("cache_status") == "hit":
                        analysis_status.update(
                            label=f"⚡ 缓存命中！(耗时 {meta.get('duration_ms', 0):.0f}ms)",
                            state="complete",
                        )
                    else:
                        analysis_status.update(
                            label="✅ 分析完成！", state="complete"
                        )

            except ValueError as e:
                if obs:
                    obs.record_error("validation")
                analysis_status.update(label="❌ 输入验证失败", state="error")
                analysis_error = e  # 保留原始异常对象供 classify_error 精确分类
            except RuntimeError as e:
                if obs:
                    obs.record_error("auth")
                analysis_status.update(label="❌ 服务暂时不可用", state="error")
                analysis_error = e
            except ConnectionError as e:
                if obs:
                    obs.record_error("network")
                analysis_status.update(label="❌ 连接失败", state="error")
                analysis_error = e
            except Exception as e:
                if obs:
                    obs.record_error("network")
                analysis_status.update(label="❌ 分析失败", state="error")
                analysis_error = e
            finally:
                if obs:
                    obs.decrement_active_requests()

        # ---- P2-3: 错误处理（友好提示 + 保留结果 + 重试按钮） ----
        if analysis_error is not None:
            st.session_state["analysis_phase"] = "error"

            # P2-3①: 友好错误提示（使用原始异常对象精确分类）
            error_type = classify_error(analysis_error)
            st.markdown(build_error_html(error_type, BACKEND_URL), unsafe_allow_html=True)

            # P2-3①: 针对具体错误类型给出额外操作建议
            error_info = get_error_info(error_type, BACKEND_URL)
            if error_type == "connection_refused":
                # 后端连接失败 → 展示恢复面板
                _show_backend_recovery_panel(manager)
            elif error_type == "connection_timeout":
                st.info(
                    "💡 **提示**：如果日志量较大，建议截取末尾关键错误部分（200-500行）重新分析。",
                    icon="💡"
                )

            # P2-3④: 一键重试按钮（仅可恢复的错误）
            retry_action = get_retry_action(error_type)
            if retry_action == "start_backend":
                col_a, col_b = st.columns([1, 3])
                with col_a:
                    if st.button("🚀 启动 Backend 并重试", type="primary", key="retry_start_backend"):
                        with st.spinner("正在启动 Backend..."):
                            ready = manager.ensure_backend(timeout=25.0)
                        if ready:
                            st.session_state["analysis_error_info"] = None
                            st.rerun()
                        else:
                            st.error("启动失败，请检查 Python 环境。")
            elif retry_action in ("retry_analysis", "retry_connection"):
                col_a, col_b, col_c = st.columns([1, 2, 3])
                with col_a:
                    if st.button("🔄 重试分析", type="primary", key="retry_analysis_btn"):
                        st.session_state["analysis_error_info"] = None
                        st.session_state["analysis_retry_count"] += 1
                        st.rerun()
                with col_b:
                    if st.button("📋 使用示例", key="use_sample_on_error"):
                        st.session_state["log_input"] = list(SAMPLE_LOGS.values())[0]
                        st.session_state["analysis_error_info"] = None
                        st.rerun()
            elif retry_action == "wait_and_retry":
                col_a, _ = st.columns([1, 3])
                with col_a:
                    if st.button("⏳ 等待后重试", key="retry_wait_btn"):
                        time.sleep(2)
                        st.session_state["analysis_error_info"] = None
                        st.rerun()

            # P2-3③: 保留上次成功结果（如果有）
            if has_previous_result(st.session_state):
                st.markdown("---")
                st.info("📋 **以下是您上次成功分析的结果**（本次分析失败前的数据）", icon="📋")
                with st.expander("展开查看上次结果", expanded=False):
                    _render_analysis_result(
                        st.session_state["last_successful_result"],
                        {"cache_status": "cached"}
                    )

            st.stop()

        # ---- 分析成功：格式化结果 ----
        if result is None:
            st.session_state["analysis_phase"] = "error"
            st.markdown(build_error_html("ai_parse_error", BACKEND_URL), unsafe_allow_html=True)
            st.stop()

        st.session_state["analysis_phase"] = "done"
        st.session_state["analysis_error_info"] = None

        from models import AnalysisResult as AR
        try:
            result_obj = AR.model_validate(result) if isinstance(result, dict) else result
        except Exception:
            result_obj = result  # 降级：保持原始 dict

        # P2-3③: 保存成功结果到 session_state（供错误后展示）
        save_successful_result(st.session_state, result_obj)

        # P2-1②: 渲染结果
        if meta and meta.get("cache_status") == "hit":
            st.info(
                f"⚡ 缓存命中 (耗时 {meta.get('duration_ms', 0):.0f}ms)",
                icon="⚡"
            )

        st.markdown("""
        <div class="status-tag">
            <div class="status-dot"></div>
            分析完成
        </div>
        """, unsafe_allow_html=True)

        _render_analysis_result(result_obj, meta)

# ============================================
# 页脚
# ============================================
st.markdown("""
<div class="footer">
    LogGazer · Powered by DeepSeek
</div>
""", unsafe_allow_html=True)


