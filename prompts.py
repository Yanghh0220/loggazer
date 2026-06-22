# prompts.py - Prompt 工程模块（Schema 自省版）
#
# 职责：
# 1. 定义 Few-shot 示例（让 AI 知道期望的输出长什么样）
# 2. 构建用户提示词（把日志预处理结果填进模板）
# 3. 动态注入 JSON Schema（让 AI "知道"自己要生成什么结构）
#
# Schema 自省设计：
# AnalysisResult.model_json_schema() 会自动生成 JSON Schema
# 这个 Schema 被注入到 System Prompt 中，让 LLM 明确知道：
# - 有哪些字段、每个字段的类型和约束
# - 枚举值（如 severity: "low" | "medium" | "high" | "critical"）
# - 列表长度限制（如 root_causes 最多 5 个）
# - 嵌套结构（如 RootCause 的 description + probability）
#
# 为什么不在 prompt 里手写 JSON 格式？
# - 手写容易和 Pydantic Schema 不一致（如当前的 "prevention" 是字符串 vs 列表）
# - Schema 自动生成，保证 prompt 和代码 100% 同步

import json
from typing import Optional


# ============================================================
#  Few-shot 示例
# ============================================================
# 示例输出必须符合 AnalysisResult 的完整 Schema
# 包含新增字段：severity（列表）、security_warning

FEW_SHOT_EXAMPLE = """
===== 参考示例 =====

【示例输入】
平台: npm
错误行:
npm ERR! code ERESOLVE
npm ERR! ERESOLVE could not resolve
npm ERR! Conflicting peer dependency: react@17.0.2

日志:
npm ERR! code ERESOLVE
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
npm ERR! this command with --force or --legacy-peer-deps

【示例输出】
{
    "error_summary": "npm 依赖解析冲突：react 版本不兼容",
    "error_detail": "npm ERR! ERESOLVE could not resolve\\nnpm ERR! Conflicting peer dependency: react@17.0.2",
    "root_causes": [
        {
            "description": "react-scripts@5.0.1 要求 react@^18.2.0，但 @testing-library/react@11.2.7 要求 react@^17.0.0，两个依赖对 react 的版本要求互相矛盾",
            "probability": 90
        },
        {
            "description": "package-lock.json 中锁定了旧版本的依赖树，与当前 package.json 不一致",
            "probability": 7
        },
        {
            "description": "npm 版本过低，依赖解析算法不完善",
            "probability": 3
        }
    ],
    "fix_suggestions": [
        {
            "title": "升级 @testing-library/react 到兼容 react 18 的版本",
            "description": "从根本上解决版本冲突，这是推荐的解决方式",
            "command": "npm install @testing-library/react@latest --save-dev",
            "riskLevel": "safe",
            "riskLabel": "安全",
            "recommended": true
        },
        {
            "title": "使用 --legacy-peer-deps 跳过 peer dependency 检查",
            "description": "这是最快的解决方式，跳过严格的版本兼容性检查，但可能隐藏潜在问题",
            "command": "npm install --legacy-peer-deps",
            "riskLevel": "warning",
            "riskLabel": "谨慎",
            "recommended": false
        },
        {
            "title": "降级 react 到 17.x 以匹配 testing-library",
            "description": "如果项目暂不允许使用 react 18，这是一种保守的解决方案",
            "command": "npm install react@17.0.2 react-dom@17.0.2",
            "riskLevel": "warning",
            "riskLabel": "谨慎",
            "recommended": false
        }
    ],
    "debug_commands": [
        "npm ls react",
        "npm why react",
        "npm outdated"
    ],
    "severity": "medium",
    "prevention": [
        "在 package.json 中使用更宽松的版本范围（如 ^ 而非 ~）",
        "定期运行 npm outdated 检查依赖更新",
        "使用 npm shrinkwrap 锁定依赖版本"
    ],
    "security_warning": ""
}
"""


# ============================================================
#  系统提示词模板
# ============================================================
# 使用 {schema} 占位符，由 build_system_prompt() 动态注入 JSON Schema
# 不再手写 JSON 格式描述，避免和 Pydantic Schema 不一致

SYSTEM_PROMPT_TEMPLATE = """你是一名资深的 DevOps 工程师和 CI/CD 专家，拥有 10 年以上的构建系统调试经验。

用户会给你一段构建失败的日志，你需要输出一份结构化的分析报告。

## 输出格式

你必须严格按照下面的 JSON Schema 输出，不要有任何其他文字、解释、markdown 标记：

{schema}

## 硬性规则

1. **只返回 JSON**，不要有任何其他文字、解释、markdown 标记
2. **禁止返回 HTML**：任何字段（title、description、command 等）**绝对禁止**包含 HTML 标签（如 `<div>`、`<span>`、`<code>`、`</div>` 等）、HTML 实体（如 `&lt;`、`&gt;`、`&amp;`）、Markdown 格式、转义字符、或任何格式化代码。**必须是纯文本。违反此规则将导致输出无法渲染。**
18. **纯文本验证**：再次强调 — title、description、command 字段中**不得出现 `<`、`>`、`&` 字符**（命令中合法的 `&&` 除外）。如果你在输出中包含 `</div>`、`<div class="...">`、`<code>` 等模式，你的输出将被系统拒绝。
3. **root_causes 中所有 probability 之和必须等于 100**，这是最重要的规则
4. **所有命令必须是可直接复制执行的 bash 命令**
5. **error_detail 保留英文原文**，方便用户对照原始日志
6. **其他字段用中文**，且新手能看懂
7. **如果日志信息不足**，诚实说明"日志信息不足，无法确定根因"，不要编造
8. **severity 判断标准**：
   - critical: 构建完全阻断，无法产出任何产物
   - high: 核心功能失败，但有 workaround
   - medium: 非核心功能失败（如测试、lint）
   - low: 警告或非致命问题
9. **fix_suggestions 必须返回 2-4 条**，按推荐度从高到低排列
10. **至少标记一条 recommended: true**（最推荐的方案）
11. **riskLevel 必须严格使用枚举值**：safe、warning、danger
12. **riskLabel 必须匹配 riskLevel**：safe→"安全"、warning→"谨慎"、danger→"高风险"
13. **command 字段可选**（无需执行命令时可留空字符串）
14. **debug_commands 至少 2 条**，帮助用户进一步排查
15. **prevention 是列表**，最多 3 条预防建议
16. **security_warning 留空**，除非你使用的命令有安全风险
17. **安全第一**：禁止生成以下危险命令：
    - `rm -rf /` 或 `rm -rf /*`
    - `curl ... | sh` 或 `wget ... | bash`
    - `dd if=/dev/zero of=/dev/...`
    - 任何格式化磁盘、覆写设备、反弹 shell 的命令
    - 对 /etc、/usr、/bin 等系统目录的破坏性写操作
""" + FEW_SHOT_EXAMPLE


# ============================================================
#  旧版 SYSTEM_PROMPT（兼容未迁移的调用方）
# ============================================================
# 保留旧版常量，供 analyzer.py 的 legacy 路径使用

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.replace("{schema}", "(Schema 将由结构化生成引擎自动注入)")


# ============================================================
#  Schema 自省：动态构建 System Prompt
# ============================================================

def build_system_prompt(schema: dict) -> str:
    """
    构建包含 JSON Schema 的系统提示词

    将 AnalysisResult.model_json_schema() 注入到 System Prompt 中，
    让 LLM 明确知道输出结构的字段名、类型、约束和描述。

    参数:
        schema: Pydantic 模型的 JSON Schema 字典

    返回:
        完整的系统提示词字符串
    """
    schema_json = json.dumps(schema, indent=2, ensure_ascii=False)
    return SYSTEM_PROMPT_TEMPLATE.replace("{schema}", schema_json)


# ============================================================
#  用户提示词构建函数
# ============================================================

def build_analysis_prompt(
    source: str,
    error_lines: list[str],
    stats: dict,
    full_log_preview: str,
) -> str:
    """
    构建发送给 AI 的用户提示词

    把日志预处理的结果（平台、错误行、统计信息、日志原文）
    按照模板拼接成完整的提示词。
    """
    parts: list[str] = []

    # ---- 平台信息 ----
    if source and source != "Unknown":
        parts.append(f"【日志来源平台】{source}")

    # ---- 日志统计 ----
    fatal_count: int = stats.get("fatal_count", 0)
    error_count: int = stats.get("error_count", 0)
    warning_count: int = stats.get("warning_count", 0)
    total_lines: int = stats.get("total_lines", 0)

    stats_text: str = (
        f"总行数: {total_lines} | "
        f"致命错误: {fatal_count} | "
        f"错误: {error_count} | "
        f"警告: {warning_count}"
    )
    parts.append(f"【日志统计】{stats_text}")

    # ---- 动态严重程度提示 ----
    if fatal_count > 0:
        severity_hint = "critical（存在致命错误，构建完全阻断）"
    elif error_count >= 5:
        severity_hint = "high（错误数量较多，核心功能可能受影响）"
    elif error_count >= 1:
        severity_hint = "medium（存在错误，需要修复）"
    else:
        severity_hint = "low（仅警告，可能不影响构建）"
    parts.append(f"【严重程度提示】{severity_hint}")

    # ---- 预提取的错误行 ----
    if error_lines:
        error_text: str = "\n".join(error_lines[:10])
        parts.append(f"【已识别的关键错误行】\n{error_text}")

    # ---- 截断提示 ----
    if len(full_log_preview) >= 6000:
        parts.append(
            "【注意】原始日志较长，以下为截断后的版本（保留了头部和尾部的关键信息），"
            "请基于可见内容进行分析。"
        )

    # ---- 日志正文 ----
    parts.append(f"【完整日志】\n{full_log_preview}")

    # ---- 最终指令 ----
    parts.append(
        "请按照系统提示中的 JSON Schema 返回分析结果。\n"
        "特别注意：root_causes 的 probability 之和必须等于 100。"
    )

    return "\n\n".join(parts)


# ============================================================
#  RAG 增强提示词构建
# ============================================================

_RAG_SECTION_TEMPLATE = """

【历史相似案例参考】
⚠️ 以下案例为历史日志的修复记录，仅作参考，不要直接套用命令。
请结合当前日志的具体情况独立分析，历史案例仅用于辅助判断。

{rag_context}
"""


def build_rag_augmented_prompt(
    rag_context: str, base_prompt: str
) -> str:
    """
    在基础提示词末尾注入 RAG 历史案例上下文
    """
    if not rag_context or not rag_context.strip():
        return base_prompt

    rag_section = _RAG_SECTION_TEMPLATE.format(rag_context=rag_context)
    return base_prompt + rag_section
