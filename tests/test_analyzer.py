"""
tests/test_analyzer.py — 分析器核心流程测试

测试 analyze_log() 端到端流程：
1. 正常日志分析 → Mock AI 返回 AnalysisResult
2. 结构化生成失败 → 验证降级路径触发
3. 危险命令拦截 → 验证 Pydantic ValidationError
4. 空输入 → ValueError

所有 AI 调用均通过 Mock 实现，不依赖外部 API Key。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 sys.path 中
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from models import (
    AnalysisResult,
    FixSuggestion,
    RootCause,
)


# ============================================================
#  测试数据工厂
# ============================================================

def _make_mock_result(**overrides) -> AnalysisResult:
    """构造一个合法的 AnalysisResult 实例"""
    data = {
        "error_summary": "npm 依赖解析冲突",
        "error_detail": "npm ERR! ERESOLVE could not resolve",
        "root_causes": [
            {"description": "react 版本不兼容", "probability": 90},
            {"description": "package-lock.json 过期", "probability": 10},
        ],
        "fix_suggestions": [
            {
                "title": "使用 --legacy-peer-deps",
                "description": "跳过 peer dependency 检查",
                "command": "npm install --legacy-peer-deps",
                "riskLevel": "safe",
                "riskLabel": "安全",
                "recommended": True,
            },
            {
                "title": "升级依赖包版本",
                "description": "升级到兼容的最新版本",
                "command": "npm update",
                "riskLevel": "safe",
                "riskLabel": "安全",
                "recommended": False,
            },
        ],
        "debug_commands": ["npm ls react", "npm why react"],
        "severity": "medium",
        "prevention": ["使用更宽松的版本范围"],
        "security_warning": "",
    }
    data.update(overrides)
    return AnalysisResult.model_validate(data)


SAMPLE_NPM_LOG = """\
npm ERR! code ERESOLVE
npm ERR! ERESOLVE could not resolve
npm ERR! While resolving: react-scripts@5.0.1
npm ERR! Found: react@18.2.0
npm ERR! Conflicting peer dependency: react@17.0.2
npm ERR! Fix the upstream dependency conflict, or retry
npm ERR! this command with --force or --legacy-peer-deps
"""

# Auto-use fixture: disable cache + cluster engine to avoid heavy imports
@pytest.fixture(autouse=True)
def _disable_cache_and_cluster():
    """Mock cache and cluster engine layers to avoid heavy dependency imports."""
    with patch("analyzer._get_or_create_cache", return_value=None), \
         patch("analyzer._store_to_cluster_engine", return_value=None):
        yield


# ============================================================
#  测试：analyze_log() 正常流程
# ============================================================

class TestAnalyzeLogNormal:
    """测试 analyze_log() 正常分析流程（Mock AI）"""

    @patch("ai_engine.call_ai_structured")
    def test_analyze_returns_analysis_result(self, mock_structured):
        """正常 npm 日志分析返回 AnalysisResult 实例"""
        mock_structured.return_value = _make_mock_result()

        from analyzer import analyze_log

        result = analyze_log(SAMPLE_NPM_LOG)

        assert isinstance(result, AnalysisResult)
        assert result.error_summary == "npm 依赖解析冲突"
        assert result.severity == "medium"
        assert len(result.root_causes) == 2
        assert sum(c.probability for c in result.root_causes) == 100

    @patch("ai_engine.call_ai_structured")
    def test_analyze_calls_ai_with_correct_prompt(self, mock_structured):
        """验证 analyze_log 向 AI 传递了正确的 system/user prompt"""
        mock_structured.return_value = _make_mock_result()

        from analyzer import analyze_log

        analyze_log(SAMPLE_NPM_LOG)

        # 验证 call_ai_structured 被调用
        assert mock_structured.call_count == 1
        call_kwargs = mock_structured.call_args[1]
        assert "system_prompt" in call_kwargs
        assert "user_prompt" in call_kwargs
        assert "max_retries" in call_kwargs
        # user_prompt 应包含日志内容
        assert "ERESOLVE" in call_kwargs["user_prompt"]

    def test_empty_log_raises_value_error(self):
        """空日志输入抛出 ValueError"""
        from analyzer import analyze_log

        with pytest.raises(ValueError, match="不能为空"):
            analyze_log("")

    def test_whitespace_only_raises_value_error(self):
        """纯空白日志抛出 ValueError"""
        from analyzer import analyze_log

        with pytest.raises(ValueError, match="不能为空"):
            analyze_log("   \n  \t  ")

    @patch("ai_engine.call_ai_structured")
    def test_result_fields_accessible_by_dict_style(self, mock_structured):
        """AnalysisResult 支持 dict-style 访问（向后兼容）"""
        mock_structured.return_value = _make_mock_result()

        from analyzer import analyze_log

        result = analyze_log(SAMPLE_NPM_LOG)

        # dict-style get
        assert result.get("error_summary") == "npm 依赖解析冲突"
        assert result.get("severity") == "medium"
        assert result.get("nonexistent", "fallback") == "fallback"

        # dict-style []
        assert result["error_summary"] == "npm 依赖解析冲突"

        # reason alias
        reason = result.get("reason")
        assert "react 版本不兼容" in reason

    @patch("ai_engine.call_ai_structured")
    def test_result_fields_accessible_by_attribute(self, mock_structured):
        """AnalysisResult 支持属性访问"""
        mock_structured.return_value = _make_mock_result()

        from analyzer import analyze_log

        result = analyze_log(SAMPLE_NPM_LOG)

        assert result.error_summary == "npm 依赖解析冲突"
        assert result.severity == "medium"
        assert len(result.fix_suggestions) >= 2
        assert len(result.debug_commands) == 2


# ============================================================
#  测试：降级路径（直接测试 _legacy_analyze）
# ============================================================

class TestAnalyzeLogFallback:
    """测试 _legacy_analyze() 降级路径"""

    @patch("ai_engine.call_ai_legacy")
    def test_legacy_analyze_with_valid_json(self, mock_legacy):
        """降级路径：AI 返回合法 JSON 时正常解析"""
        mock_legacy.return_value = (
            '{"error_summary": "降级测试",'
            '"error_detail": "test",'
            '"root_causes": [{"description": "原因", "probability": 100}],'
            '"fix_suggestions": [{"title": "修复", "description": "描述",'
            '"command": "echo test", "safety_level": "safe"}],'
            '"debug_commands": ["echo debug"],'
            '"severity": "medium"}'
        )

        from analyzer import _legacy_analyze
        from prompts import build_system_prompt
        from models import AnalysisResult

        system_prompt = build_system_prompt(AnalysisResult.model_json_schema())
        result = _legacy_analyze("test user prompt")

        assert isinstance(result, AnalysisResult)
        assert result.error_summary == "降级测试"
        assert mock_legacy.call_count == 1

    @patch("ai_engine.call_ai_legacy")
    def test_legacy_analyze_with_api_error(self, mock_legacy):
        """降级路径：AI 返回 API 错误提示时创建 fallback 模型"""
        mock_legacy.return_value = "⚠️ API Key 未配置"

        from analyzer import _legacy_analyze

        result = _legacy_analyze("test user prompt")

        assert isinstance(result, AnalysisResult)
        # 降级为 fallback 模型
        assert result.error_summary == "AI 分析结果解析失败"
        assert result.security_warning != ""

    @patch("ai_engine.call_ai_legacy")
    def test_legacy_analyze_with_unparseable_text(self, mock_legacy):
        """降级路径：AI 返回无法解析的文本时创建 fallback 模型"""
        mock_legacy.return_value = "This is not JSON at all, just random text"

        from analyzer import _legacy_analyze

        result = _legacy_analyze("test user prompt")

        assert isinstance(result, AnalysisResult)
        assert result.security_warning != ""
        # fallback 模型至少应有基本结构
        assert len(result.root_causes) == 1
        assert result.root_causes[0].probability == 100

    @patch("ai_engine.call_ai_legacy")
    def test_legacy_analyze_with_markdown_fenced_json(self, mock_legacy):
        """降级路径：AI 返回 Markdown 围栏包裹的 JSON 时正常解析"""
        mock_legacy.return_value = (
            '```json\n'
            '{"error_summary": "围栏测试",'
            '"error_detail": "test",'
            '"root_causes": [{"description": "测试原因", "probability": 100}],'
            '"fix_suggestions": [{"title": "修复", "description": "描述",'
            '"command": "echo test", "safety_level": "safe"}],'
            '"debug_commands": ["echo debug"],'
            '"severity": "low"}\n'
            '```'
        )

        from analyzer import _legacy_analyze

        result = _legacy_analyze("test user prompt")

        assert isinstance(result, AnalysisResult)
        assert result.error_summary == "围栏测试"
        assert result.severity == "low"


# ============================================================
#  测试：模型校验拦截
# ============================================================

class TestModelValidationInAnalyze:
    """测试通过 analyze_log 间接验证的危险命令拦截"""

    def test_dangerous_command_rejected_at_model_level(self):
        """包含 rm -rf / 的 FixSuggestion 触发 ValidationError"""
        with pytest.raises(Exception) as exc_info:
            FixSuggestion(
                title="危险操作",
                description="删除根目录",
                command="rm -rf /",
                safety_level="dangerous",
            )
        error_msg = str(exc_info.value)
        assert "危险" in error_msg

    def test_safe_command_accepted(self):
        """安全命令通过校验"""
        suggestion = FixSuggestion(
            title="安全操作",
            description="安装依赖",
            command="npm install",
            safety_level="safe",
        )
        assert suggestion.command == "npm install"
        assert suggestion.safety_level == "safe"

    def test_review_command_auto_upgraded(self):
        """包含 sudo 的命令自动标记为 review"""
        suggestion = FixSuggestion(
            title="系统操作",
            description="需要管理员权限",
            command="sudo systemctl restart nginx",
            safety_level="safe",  # LLM 标记为 safe，但应被自动升级
        )
        # 命令通过（不抛异常），但安全等级被自动提升
        assert suggestion.safety_level == "review"

    def test_docker_system_prune_auto_upgraded(self):
        """docker system prune 自动标记为 review"""
        suggestion = FixSuggestion(
            title="清理 Docker",
            description="清理所有未使用的 Docker 资源",
            command="docker system prune -f",
            safety_level="safe",
        )
        assert suggestion.safety_level == "review"

    def test_kill_minus_9_auto_upgraded(self):
        """kill -9 自动标记为 review"""
        suggestion = FixSuggestion(
            title="强制终止",
            description="强制终止进程",
            command="kill -9 12345",
            safety_level="safe",
        )
        assert suggestion.safety_level == "review"

    def test_normal_command_stays_safe(self):
        """普通命令保持不变"""
        suggestion = FixSuggestion(
            title="正常安装",
            description="安装 npm 包",
            command="npm install react",
            safety_level="safe",
        )
        assert suggestion.safety_level == "safe"


# ============================================================
#  测试：analyze_log_advanced()
# ============================================================

class TestAnalyzeLogAdvanced:
    """测试 analyze_log_advanced() 的降级行为"""

    def test_empty_log_raises_value_error(self):
        """空日志抛出 ValueError"""
        from analyzer import analyze_log_advanced

        with pytest.raises(ValueError, match="不能为空"):
            analyze_log_advanced("")

    @patch("ai_engine.call_ai_structured")
    def test_falls_back_when_langgraph_unavailable(self, mock_structured):
        """LangGraph 不可用时降级到 analyze_log()"""
        mock_structured.return_value = _make_mock_result()

        from analyzer import analyze_log_advanced

        result = analyze_log_advanced(SAMPLE_NPM_LOG)

        # 由于 agent_graph 可能不可用，应降级到 analyze_log
        assert isinstance(result, AnalysisResult)
        assert result.error_summary == "npm 依赖解析冲突"
