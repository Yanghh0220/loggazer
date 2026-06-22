# agent_prompts.py - Multi-Agent 系统提示词分片化设计
#
# 职责：
# 1. 为 LangGraph 各节点定义独立的系统提示词
# 2. SYSTEM_PROMPT_ROUTER: 极简路由判断（<50 tokens）
# 3. SYSTEM_PROMPT_ANALYZER: 核心分析 + Tool 使用指南
# 4. SYSTEM_PROMPT_VALIDATOR: 结构化校验结果输出
# 5. human_review_prompt(): 人工审查请求生成

import json
from typing import Any


# ============================================================
#  Router 节点：极简路由判断
# ============================================================
# 目标：<50 tokens，纯规则判断，不依赖 LLM
# 实际上 Router 节点用纯 Python 代码实现，此 prompt 仅作备用

SYSTEM_PROMPT_ROUTER = """你是日志分析路由器。根据日志内容判断：
1. 平台类型（npm/pip/docker/github-actions/unknown）
2. 日志复杂度（simple: <10行, medium: 10-50行, complex: >50行）
3. 是否包含已知错误模式

输出 JSON: {"platform": "...", "complexity": "...", "has_known_pattern": bool}
只输出 JSON，无其他文字。"""


# ============================================================
#  Analyzer 节点：核心分析 + Tool 使用指南
# ============================================================

SYSTEM_PROMPT_ANALYZER = """你是一名资深的 DevOps 工程师和 CI/CD 专家，拥有 10 年以上的构建系统调试经验。

用户会给你一段构建失败的日志，你需要输出一份结构化的分析报告。

## 你可以使用的工具

你有两个外部工具可用于辅助分析：
1. **search_documentation(query, platform)** — 查询官方文档
   - 使用场景：遇到不熟悉的错误模式或 API 用法时
   - query: 搜索关键词（如 "ERESOLVE could not resolve"）
   - platform: 平台名称（如 "npm", "pip"）

2. **check_stackoverflow(query)** — 检索 Stack Overflow
   - 使用场景：遇到社区常见的疑难杂症时
   - query: 搜索关键词

**重要：只有在遇到不熟悉的错误模式时才使用工具。如果错误模式你已经熟悉，直接分析即可。**

## 输出格式

你必须严格按照下面的 JSON Schema 输出，不要有任何其他文字、解释、markdown 标记：

{schema}

## 硬性规则

1. **只返回 JSON**，不要有任何其他文字、解释、markdown 标记
2. **root_causes 中所有 probability 之和必须等于 100**，这是最重要的规则
3. **所有命令必须是可直接复制执行的 bash 命令**
4. **error_detail 保留英文原文**，方便用户对照原始日志
5. **其他字段用中文**，且新手能看懂
6. **如果日志信息不足**，诚实说明"日志信息不足，无法确定根因"，不要编造
7. **severity 判断标准**：
   - critical: 构建完全阻断，无法产出任何产物
   - high: 核心功能失败，但有 workaround
   - medium: 非核心功能失败（如测试、lint）
   - low: 警告或非致命问题
8. **fix_suggestions 必须返回 2-4 条**，按推荐度从高到低排列
9. **至少标记一条 recommended: true**（最推荐的方案）
10. **riskLevel 必须严格使用枚举值**：safe、warning、danger
11. **riskLabel 必须匹配 riskLevel**：safe→"安全"、warning→"谨慎"、danger→"高风险"
12. **command 字段可选**（无需执行命令时可留空字符串）
13. **禁止返回 HTML**：任何字段都禁止包含 HTML 标签、Markdown 或转义字符。必须是纯文本。
14. **debug_commands 至少 2 条**，帮助用户进一步排查
15. **prevention 是列表**，最多 3 条预防建议
16. **security_warning 留空**，除非你使用的命令有安全风险
17. **安全第一**：禁止生成以下危险命令：
    - `rm -rf /` 或 `rm -rf /*`
    - `curl ... | sh` 或 `wget ... | bash`
    - `dd if=/dev/zero of=/dev/...`
    - 任何格式化磁盘、覆写设备、反弹 shell 的命令
    - 对 /etc、/usr、/bin 等系统目录的破坏性写操作
"""


# ============================================================
#  Validator 节点：结构化校验结果输出
# ============================================================
# Validator 本身是纯确定性代码，不调用 LLM
# 此 prompt 用于当 Validator 需要 AI 辅助判断时（如语义分析）

SYSTEM_PROMPT_VALIDATOR = """你是一名安全审查专家。你需要判断以下 bash 命令是否安全。

对每条命令，输出 JSON：
```json
{
  "command": "原始命令",
  "safety_level": "safe|review|dangerous",
  "reason": "判断理由",
  "category": "destructive|data_exfiltration|privilege_escalation|supply_chain|null"
}
```

安全等级定义：
- safe: 无任何安全风险，可直接执行
- review: 有一定风险，建议人工审查后执行
- dangerous: 高危命令，必须人工确认，禁止自动执行

只输出 JSON，无其他文字。"""


# ============================================================
#  Summarizer 节点：整合输出
# ============================================================

SYSTEM_PROMPT_SUMMARIZER = """你是一名报告整合专家。你需要将分析结果和安全校验结果整合为最终报告。

输入包含：
1. AI 分析的原始结果（JSON）
2. 命令安全校验结果（JSON）

你需要：
1. 保持原始分析结果的结构不变
2. 根据安全校验结果更新每条命令的 safety_level
3. 如果存在 dangerous 命令，在 security_warning 字段中添加警告
4. 如果存在 review 命令，在 security_warning 中提示用户注意

输出格式与输入的分析结果 JSON 格式完全一致。
只输出 JSON，无其他文字。"""


# ============================================================
#  人工审查请求模板
# ============================================================

def human_review_prompt(
    dangerous_commands: list[dict[str, str]],
    analysis_summary: str,
) -> str:
    """
    当安全等级为 dangerous 或 review 时，生成面向用户的 Markdown 审查请求

    参数:
        dangerous_commands: 危险命令列表，每项包含 command, safety_level, reason, category
        analysis_summary: 分析摘要

    返回:
        Markdown 格式的审查请求字符串
    """
    parts = [
        "## ⚠️ 安全审查请求",
        "",
        "以下命令被安全校验标记为需要人工审查：",
        "",
    ]

    for i, cmd_info in enumerate(dangerous_commands, 1):
        level = cmd_info.get("safety_level", "unknown")
        emoji = "🔴" if level == "dangerous" else "🟡"
        parts.extend([
            f"### {emoji} 命令 {i}",
            f"```bash\n{cmd_info.get('command', 'N/A')}\n```",
            f"- **安全等级**: `{level}`",
            f"- **风险类别**: `{cmd_info.get('category', 'N/A')}`",
            f"- **原因**: {cmd_info.get('reason', 'N/A')}",
            "",
        ])

    parts.extend([
        "### 建议",
        "- 如果这些命令是误报，请确认后继续执行",
        "- 如果确实存在风险，请使用安全的替代方案",
        "- 可以在沙箱环境（如 Docker 容器）中先测试",
        "",
        f"### 分析摘要",
        analysis_summary,
    ])

    return "\n".join(parts)


def build_analyzer_user_prompt(
    source: str,
    error_lines: list[str],
    stats: dict,
    full_log_preview: str,
    rag_context: str = "",
    tool_results: str = "",
) -> str:
    """
    构建 Analyzer 节点的用户提示词

    整合了原 prompt.py 的 build_analysis_prompt 逻辑，
    额外支持 RAG 上下文和 Tool 调用结果注入。

    参数:
        source: 平台来源
        error_lines: 错误行列表
        stats: 日志统计信息
        full_log_preview: 截断后的日志文本
        rag_context: RAG 历史案例上下文
        tool_results: Tool 调用结果

    返回:
        完整的用户提示词字符串
    """
    parts: list[str] = []

    # 平台信息
    if source and source != "Unknown":
        parts.append(f"【日志来源平台】{source}")

    # 日志统计
    fatal_count = stats.get("fatal_count", 0)
    error_count = stats.get("error_count", 0)
    warning_count = stats.get("warning_count", 0)
    total_lines = stats.get("total_lines", 0)

    stats_text = (
        f"总行数: {total_lines} | "
        f"致命错误: {fatal_count} | "
        f"错误: {error_count} | "
        f"警告: {warning_count}"
    )
    parts.append(f"【日志统计】{stats_text}")

    # 动态严重程度提示
    if fatal_count > 0:
        severity_hint = "critical（存在致命错误，构建完全阻断）"
    elif error_count >= 5:
        severity_hint = "high（错误数量较多，核心功能可能受影响）"
    elif error_count >= 1:
        severity_hint = "medium（存在错误，需要修复）"
    else:
        severity_hint = "low（仅警告，可能不影响构建）"
    parts.append(f"【严重程度提示】{severity_hint}")

    # 预提取的错误行
    if error_lines:
        error_text = "\n".join(error_lines[:10])
        parts.append(f"【已识别的关键错误行】\n{error_text}")

    # 截断提示
    if len(full_log_preview) >= 6000:
        parts.append(
            "【注意】原始日志较长，以下为截断后的版本（保留了头部和尾部的关键信息），"
            "请基于可见内容进行分析。"
        )

    # 日志正文
    parts.append(f"【完整日志】\n{full_log_preview}")

    # RAG 上下文注入
    if rag_context and rag_context.strip():
        parts.extend([
            "",
            "【历史相似案例参考】",
            "⚠️ 以下案例为历史日志的修复记录，仅作参考，不要直接套用命令。",
            "请结合当前日志的具体情况独立分析，历史案例仅用于辅助判断。",
            "",
            rag_context,
        ])

    # Tool 调用结果注入
    if tool_results and tool_results.strip():
        parts.extend([
            "",
            "【外部工具检索结果】",
            "以下是你之前调用工具获取的参考信息，请结合这些信息进行分析：",
            "",
            tool_results,
        ])

    # 最终指令
    parts.extend([
        "",
        "请按照系统提示中的 JSON Schema 返回分析结果。",
        "特别注意：root_causes 的 probability 之和必须等于 100。",
    ])

    return "\n\n".join(parts)
