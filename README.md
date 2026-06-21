<div align="center">

# 🛩️ LogGazer

**AI 驱动的 CI/CD 日志分析助手 —— 从报错到修复，60 秒搞定**

把构建失败日志扔给我，我帮你翻译成人话，直接给你修复命令。

[![🚀 在线体验](https://img.shields.io/badge/🚀_在线体验_Click_Me-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)](https://loggazer-v-1-0.streamlit.app/)

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.37-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/Tests-Pytest-yellow?logo=pytest&logoColor=white)](https://pytest.org/)
[![DeepSeek](https://img.shields.io/badge/AI-DeepSeek-6366F1?logo=google&logoColor=white)](https://platform.deepseek.com/)
[![CI](https://github.com/Yanghh0220/LogGazer/actions/workflows/ci.yml/badge.svg)](https://github.com/Yanghh0220/LogGazer/actions/workflows/ci.yml)

[📖 快速开始](#-快速开始) · [✨ 功能特性](#-功能特性) · [🗺️ Roadmap](#%EF%B8%8F-roadmap) · [🐛 报告问题](https://github.com/Yanghh0220/LogGazer/issues)

</div>

---

## 🤔 LogGazer 解决什么问题？

每次 CI/CD 流水线挂了，你面对的是这样的场景：

> 几百行日志，翻了 20 分钟，发现只是一个 npm 依赖版本冲突……

| | 手动排查 | ChatGPT 直接问 | **LogGazer** |
|--|:--------:|:--------------:|:------------:|
| 耗时 | 15-30 分钟 | 3-5 分钟 | **< 1 分钟** |
| 需要自己找错误行？ | ❌需要 | ❌需要 | ✅ 自动提取 |
| 命令能直接复制执行？ | ❌ | ⚠️ 经常不准 | ✅ |
| 认识 CI/CD 平台？ | ❌不清楚 | ❌不认识 | ✅ 10+ 平台 |
| 需要注册登录？ | ❌需要 | ❌需要 | ✅ |

---

## 📊 数据流架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        LogGazer 数据流架构                               │
└─────────────────────────────────────────────────────────────────────────┘

  用户粘贴日志         log_parser.py          cache_engine.py
  ┌──────────┐       ┌──────────────┐      ┌──────────────────┐
  │          │──────▶│  · 平台识别   │─────▶│  · 生成日志指纹   │
  │  构建日志 │       │  · 错误提取   │      │  · 向量相似度检索  │
  │  (任意平台)│       │  · 智能截断   │      │  · Qdrant 本地存储 │
  └──────────┘       └──────────────┘      └────────┬─────────┘
                                                    │
                              ┌──────────────────────┤
                              │                      │
                     命中缓存 (≥0.92)          未命中 / RAG (<0.92)
                              │                      │
                              ▼                      ▼
                     直接返回缓存结果          prompt.py
                     (0 API 调用)            ┌─────────────┐
                                            │  · 构建提示词 │
                                            │  · 注入 RAG  │
                                            │    历史案例   │
                                            └──────┬──────┘
                                                   │
                                                   ▼
  Streamlit UI          config.py          analyzer.py
  ┌──────────────┐     ┌──────────┐      ┌──────────────┐
  │  · 错误摘要   │◀────│ 环境变量  │◀─────│  · 调用 DeepSeek│
  │  · 根因分析   │     │ API Key  │      │  · 解析 JSON   │
  │  · 修复建议   │     │ 模型配置  │      │  · 写入缓存    │
  │  · 排查命令   │     └──────────┘      └──────────────┘
  │  · 一键复制   │
  └──────────────┘
```

---

## ✨ 功能特性

### 核心能力

| 功能 | 说明 |
|------|------|
| 🤖 AI 根因分析 | 基于 DeepSeek 大模型深度分析错误原因 |
| 📖 中文解读 | 用通俗易懂的中文解释晦涩的报错信息 |
| 🔧 修复建议 | 提供 Top 3 修复方案，附带可直接复制的命令 |
| 🔍 排查命令 | 给出进一步诊断的命令，帮你确认问题 |
| 📋 一键复制 | 每个命令都可以一键复制到终端 |
| 📄 内置示例 | 3 种常见日志示例（npm / Docker / pytest），零门槛体验 |
| 🧠 智能预处理 | 自动识别平台、提取关键错误行、截断超长日志 |
| ⚡ 语义缓存 | 相同/相似日志秒级返回，零 API 调用，历史案例自动积累 |

### 支持的日志来源

<table>
<tr>
<td width="33%">

**📦 包管理器**
- npm / yarn / pnpm
- pip / poetry
- cargo (Rust)
- Gradle / Maven

</td>
<td width="33%">

**🐳 容器 & CI/CD**
- Docker Build
- GitHub Actions
- Jenkins
- GitLab CI

</td>
<td width="33%">

**🧪 测试框架**
- pytest
- Jest / Vitest
- JUnit

</td>
</tr>
</table>

> 💡 不在列表里的日志也能分析，只是平台识别的准确度可能稍低。

### 🔄 实时日志追踪 (NEW — Go)

| 功能 | 说明 |
|------|------|
| 📡 实时追踪 | `logtail` Go 守护进程，持续追踪日志文件（类似 `tail -f`） |
| 🌀 日志轮转处理 | 支持 rename+create、copytruncate、remove+recreate、symlink 变化 |
| 💾 断点续传 | 基于 inode/device 的检查点，重启后从上次位置继续 |
| 🔒 原子写入 | Temp → fsync → rename 确保检查点不损坏 |
| 📂 多文件支持 | 并发追踪多个文件，隔离状态 |
| 🪶 轻量依赖 | 仅依赖 `fsnotify`，标准库为主 |

```bash
# 从头部开始追踪
./logtail /var/log/app.log

# 从尾部开始（tail -f 模式），多文件
./logtail --start-at-end /var/log/app.log /var/log/nginx/access.log

# 自定义检查点目录和轮询间隔
./logtail --checkpoint-dir /var/lib/logpilot/ckpts --poll-interval 2s /var/log/*.log
```

详见 [`logtail/README.md`](logtail/README.md)

---

## 🏗️ 系统架构

```
用户输入日志（粘贴 or 点击示例）
         │
         ▼
┌─────────────────────────────────┐
│          analyzer.py            │
│  • 自动识别平台（7种）           │
│  • 提取关键错误行 + 上下文       │
│  • 过滤噪音，压缩日志体积        │
│  • 统计错误/警告/致命错误数量    │
└────────────────┬────────────────┘
                 │ 结构化日志数据
                 ▼
┌─────────────────────────────────┐
│           prompts.py            │
│  • 动态构建结构化 Prompt        │
│  • Few-shot 示例约束输出格式    │
│  • 根据错误严重程度调整分析重点  │
└────────────────┬────────────────┘
                 │ 完整 Prompt
                 ▼
┌─────────────────────────────────┐
│          ai_engine.py           │
│  • 指数退避重试（最多3次）       │
│  • 结构化异常分类                │
│    AuthError / RateLimitError   │
│    QuotaError / APIError        │
│  • 支持 DeepSeek/OpenAI/Claude  │
└────────────────┬────────────────┘
                 │
                 ▼
        结构化分析报告
   ┌────────────────────┐
   │  🔴 错误摘要        │
   │  🔍 根因分析(Top3)  │
   │  🛠️ 修复步骤        │
   │  📋 排查命令        │
   │  ⚡ 严重程度        │
   │  💡 预防建议        │
   └────────────────────┘
```

## ⚡ 语义缓存架构

LogGazer 内置基于向量检索的语义缓存层，对重复或相似的日志分析实现**零 API 调用**。

### 缓存策略（三级阈值）

```
相似度 ≥ 0.92  →  直接返回缓存结果（0 API 调用，毫秒级响应）
0.80 ≤ 相似度 < 0.92  →  注入 RAG 历史案例，增强 AI 分析
相似度 < 0.80  →  走全新 AI 分析，结果写入缓存
```

### 数据流

```
日志输入 → log_parser 提取 error_lines + platform
         → 标准化指纹（去时间戳/内存地址/UUID 等动态噪声）
         → SHA-256 精确匹配 + sentence-transformers 向量检索
         → Qdrant 本地向量库（内存模式 / 文件持久化）
```

### 技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| Embedding | sentence-transformers (all-MiniLM-L6-v2) | 纯本地运行，384 维，零 API 成本 |
| 向量数据库 | Qdrant (本地内存模式) | 零外部依赖，无需 Docker/云服务 |
| 距离度量 | Cosine | 适合文本嵌入，对长度变化不敏感 |
| 指纹生成 | SHA-256(normalized error_lines + platform) | 去动态噪声后精确匹配 |

### 缓存一致性

- **TTL 机制**：默认 30 天过期，防止过时修复命令
- **置信度衰减**：24h 后每 24h 下降 0.05，最低 0.5
- **平台隔离**：按 platform 过滤，npm 错误不会匹配 Docker 错误
- **优雅降级**：缓存层任何故障（Qdrant 崩溃 / Embedding 失败 / 磁盘满）自动降级到直接 AI 调用

### 配置项

在 `.env` 中可配置：

```bash
CACHE_ENABLED=true                    # 缓存总开关
CACHE_SIMILARITY_HIGH=0.92            # 直接命中阈值
CACHE_SIMILARITY_LOW=0.80             # RAG 上下文阈值
CACHE_TTL_HOURS=720                   # 缓存过期时间（小时）
CACHE_QDRANT_PATH=                    # 空 = 内存模式，路径 = 文件持久化
CACHE_EMBEDDING_MODEL=all-MiniLM-L6-v2  # Embedding 模型
```

---

## ⚡ 性能优化 (v1.2)

LogGazer 经过三轮系统性能优化（P0/P1/P2），显著提升用户体验和系统效率。

### 性能提升概览

| 指标 | 优化前 | 优化后 | 提升 |
|------|--------|--------|------|
| 页面加载（冷启动） | ~8s 白屏阻塞 | <1s UI 立即可见 | >8x |
| 二次分析（相同内容） | ~0.58ms 重新解析 | ~0.005ms 缓存命中 | 111x |
| get_error_stats 缓存命中 | ~0.35ms | ~0.001ms | 350x |
| detect_platform 缓存命中 | 0.028ms | 0.0002ms | 141x |
| LTTB 图表降采样 (1200→500点) | — | 0.66ms | 🆕 |
| 增量分析检查 | — | 0.0023ms | 🆕 |
| GZip 响应压缩 | — | 60-80% 体积缩减 | 🆕 |
| AI 调用超时保护 | 无限制 | 120s 自动返回 504 | 🆕 |
| 并发保护 | 无限制（OOM 风险） | 3并发 + 20队列 | 🆕 |

### 优化架构

```
P0 核心层（启动/缓存/算法）
├── P0-1 消除启动白屏 — 异步后端启动 + 指数退避轮询
├── P0-2 核心缓存体系 — 内容Hash缓存 (TTLCache) + API级缓存
├── P0-3 解除主线程阻塞 — ThreadPoolExecutor 隔离 CPU 密集型任务
└── P0-4 算法热路径优化 — 单遍扫描 + @lru_cache + 大文件分块

P1 系统层（数据传输/并发）
├── P1-1 前端渲染优化 — LTTB 降采样 + @st.cache_data + 分页
├── P1-2 数据传输优化 — GZipMiddleware + 分页API + 字段精简
├── P1-3 后端并发优化 — asyncio.gather 并行化 + 120s 超时控制
└── P1-4 增量分析能力 — 增量追踪 + NDJSON StreamingResponse

P2 体验层（UX/错误处理/资源保护）
├── P2-1 感知优化 — 实时进度条 + 分阶段展示 + 预期时间提示
├── P2-2 预加载 — 文件上传后后台预处理 + 启动预热
├── P2-3 错误体验 — 友好错误卡片 + 结果保留 + 一键重试
└── P2-4 资源保护 — 文件大小限制 + 内存保护 + 并发队列
```

### 性能配置

所有性能参数统一在 `config.py` 中管理，支持通过环境变量覆盖。关键参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `CONTENT_CACHE_TTL_SECONDS` | 300 | 分析结果缓存 TTL（秒） |
| `PARSED_CACHE_TTL_SECONDS` | 600 | 日志解析缓存 TTL（秒） |
| `ANALYSIS_TIMEOUT_SECONDS` | 120 | AI 分析超时 |
| `MAX_LOG_SIZE_CHARS` | 100000 | 文件大小硬限制 |
| `MAX_CONCURRENT_ANALYSES` | 3 | 最大并发分析数 |
| `LTTB_THRESHOLD` | 500 | 图表降采样触发阈值 |
| `LOG_CHUNK_SIZE` | 10000 | 大文件分块行数 |

### 性能测试

```bash
# 运行完整性能基准测试
PERF_DEBUG=1 python check_performance.py

# 运行所有单元测试
pytest tests/ -v

# 运行性能相关测试
pytest tests/test_analyzer.py tests/test_log_parser.py tests/test_fingerprint_engine.py -v
```

## ⚙️ 工程特性

| 特性 | 实现方式 |
|------|---------|
| ⚡ 语义缓存 | 本地 Embedding + Qdrant 向量检索，重复日志零 API 调用 |
| 🔄 自动重试 | 指数退避策略，超时/连接错误重试最多3次（1s→2s→4s） |
| 🔐 异常分类 | 自定义 AuthError/RateLimitError/QuotaError，对用户友好提示 |
| 📝 Prompt工程 | Few-shot示例 + RAG 历史案例注入，temperature=0.2保证稳定性 |
| 🧪 单元测试 | pytest覆盖核心逻辑，147+测试用例（含缓存集成测试） |
| 🔄 CI/CD | GitHub Actions自动运行代码检查和测试 |
| 📊 日志预处理 | 关键词提取+上下文截取，token消耗降低~80% |
| 🛡️ 资源保护 | 文件大小限制 + 内存监控 + 并发队列，优雅降级不崩溃 |

---

## 🚀 快速开始

### 前置条件

- **Python 3.11+**
- **DeepSeek API Key**（[点此注册](https://platform.deepseek.com/)，新用户有免费额度）

### 🆕 推荐启动方式：一键启动（自动管理后端）

LogGazer v1.1+ 内置 **BackendManager**，Streamlit 前端自动检测并拉起 FastAPI 后端。
**无需手工启动两个进程**，只需一个命令：

```bash
# 1. 克隆项目
git clone https://github.com/Yanghh0220/LogGazer.git
cd LogGazer

# 2. 创建虚拟环境
python -m venv venv

# 3. 激活虚拟环境
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置 API Key
cp .env.example .env
# 用编辑器打开 .env，填入你的 DeepSeek API Key

# 6. 运行系统自检（推荐）
python check_system.py --start-backend

# 7. 一键启动（只需一个命令！）
streamlit run app.py
# 浏览器打开 http://localhost:8501
# BackendManager 自动在后台启动 FastAPI 后端，无需手工操作
```

**常见场景**：

| 场景 | 操作 | 预期 |
|------|------|------|
| 全新环境 | `streamlit run app.py` → 贴日志 → 点"开始分析" | Backend 自动启动，分析正常完成 |
| 刷新页面 (F5) | 按 F5 → 点"开始分析" | 自动检测后端，已在运行则复用 |
| 后端崩溃 | BackendManager 自动检测并重启 | 点"重试连接"一键恢复 |
| 后端已启动 | 直接分析 | 复用已有实例，不重复启动 |

### 🛠️ 手动控制后端（可选）

```bash
# 手动启动/停止/查看后端状态
python backend_manager.py start     # 启动
python backend_manager.py stop      # 停止
python backend_manager.py status    # 查看状态
python backend_manager.py restart   # 重启

# 传统方式：手动启动两个终端（仍支持）
# 终端 1: python -m api.main
# 终端 2: streamlit run app.py
```

### 配置

所有配置通过环境变量统一管理（`.env` 文件）：

```bash
# 后端地址（前端自动连接）
LOGGAZER_API_URL=http://127.0.0.1:8000

# 开发模式：启用后端热重载
LOGGAZER_BACKEND_RELOAD=1

# API Key 认证（Cloud 模式）
LOGGAZER_API_KEY=your-secret-key
```

### 🏚️ Legacy 单进程模式（即将废弃）

```bash
# 直接启动 Streamlit（内嵌分析逻辑，无需额外后端）
streamlit run app.py
# 注意：此模式在 app.py 中直接 import analyzer，不经过 API 层
```

> ⚠️ Legacy 模式将在未来版本中移除，请尽快迁移到前后端分离模式。

### 运行测试

```bash
# 运行核心测试
pytest tests/ -v

# 运行 API 层测试
pytest api/tests/ -v

# 运行 BackendManager 测试
pytest tests/test_backend_manager.py -v

# 运行全部测试
pytest tests/ api/tests/ -v

# 系统自检
python check_system.py --verbose --start-backend
```

---

## 📖 使用方法

1. **粘贴日志** — 在输入框粘贴你的构建失败日志（或点击内置示例）
2. **点击分析** — 点击「🚀 开始分析」按钮
3. **查看结果** — AI 会在几秒内返回结构化分析报告：
   - 🔴 **错误摘要** — 一句话概括问题
   - 🔵 **原因分析** — AI 解读的错误根因
   - 🟢 **修复建议** — Top 3 修复方案，附带可执行命令
   - 🟣 **排查命令** — 进一步诊断的命令
4. **复制修复** — 点击命令旁的复制按钮，粘贴到终端执行

---

## 📁 项目结构

```
LogGazer/
├── app.py                  # 🎨 Streamlit 前端主入口
├── backend_manager.py      # 🔧 后端进程生命周期管理器（PID 文件 + 端口检测）
├── ai_engine.py            # 🤖 AI 调用引擎（重试机制 + 异常分类 + API 调用）
├── analyzer.py             # 🧠 日志分析（缓存集成 + AI 调用 + JSON 解析）
├── cache_engine.py         # ⚡ 语义缓存引擎（指纹生成 + 矢量检索 + RAG 上下文）
├── prompt.py               # 📝 Prompt 工程（系统提示词 + Few-shot + RAG 增强）
├── prompts.py              # 📝 Prompt 工程（Markdown 格式，备用）
├── log_parser.py           # 🔍 日志预处理（平台识别 / 错误提取 / 智能截断）
├── models.py               # 📐 类型定义（Pydantic 模型）
├── config.py               # ⚙️ 配置管理（环境变量 + 缓存配置）
├── check_system.py         # ✅ 系统自检（Python 版本 / 依赖 / API Key / 后端状态）
├── style.css               # 🎨 全局 CSS 样式
├── .env.example            # 🔑 环境变量模板
├── requirements.txt        # 📦 Python 依赖清单
├── .github/
│   └── workflows/
│       └── ci.yml          # 🔄 GitHub Actions CI 配置
├── .streamlit/
│   └── config.toml         # Streamlit UI 配置
├── api/                    # 🚀 FastAPI 后端
│   ├── main.py             #    后端入口（/v1/analyze, /v1/health, /healthz）
│   ├── schemas.py          #    Pydantic 请求/响应模型
│   └── dependencies.py     #    依赖注入（Auth / RateLimit / Observability）
├── scripts/                # 📜 启动脚本
│   ├── start_all.bat       #    Windows 一键启动
│   ├── start_all.sh        #    Linux/macOS 一键启动
│   ├── start_backend.bat   #    Windows 后端专用启动
│   └── start_backend.sh    #    Linux/macOS 后端专用启动
├── tests/                  # 🧪 单元测试
│   ├── conftest.py         #    Pytest 配置
│   ├── test_backend_manager.py  # BackendManager 测试 (38 tests)
│   ├── test_analyzer.py    #    日志分析模块测试
│   ├── test_analyzer_integration.py  # 缓存集成测试
│   ├── test_cache_engine.py          # 语义缓存引擎测试
│   ├── test_log_parser.py  #    日志解析模块测试
│   └── test_prompt.py      #    Prompt 模块测试
├── CLAUDE.md               # 🤖 AI 结对编程指南
├── LICENSE                 # 📄 MIT 开源许可证
└── README.md               # 📖 项目说明（你正在看的这个）
```

---

## 🛠️ Tech Stack

| 层级 | 技术 | 为什么选它 |
|------|------|-----------|
| **前端** | Streamlit | 零前端代码，纯 Python 写 Web UI |
| **后端** | Python 3.10+ | 简洁、生态好、新手友好 |
| **AI 模型** | DeepSeek V3 | 性价比高，中文能力强 |
| **AI SDK** | OpenAI Python SDK | DeepSeek 兼容 OpenAI 接口，换模型零改动 |
| **Embedding** | sentence-transformers | 纯本地运行，零 API 成本，384 维向量 |
| **向量数据库** | Qdrant (本地模式) | 内存/文件模式，无需外部服务 |
| **测试** | Pytest | Python 社区标准，上手简单 |
| **配置** | python-dotenv | 安全存储 API Key，不硬编码 |

### 📐 数据流

```
用户输入日志
    ↓
analyzer.py（平台识别 + 错误提取 + 统计）
    ↓
prompts.py（构建结构化 Prompt + Few-shot）
    ↓
ai_engine.py（重试机制 + 异常分类 + API 调用）
    ↓
结构化报告（摘要 / 根因 / 命令 / 严重程度）
```

---

## ♻️ 工程特性

- ♻️ **指数退避重试机制**（最多 3 次，处理网络抖动）
- 🔐 **结构化异常分类**（AuthError / RateLimitError / QuotaError）
- 🧪 **单元测试覆盖核心逻辑**（pytest）
- 🔄 **GitHub Actions CI 自动化检查**
- 📝 **Few-shot Prompt 工程**（稳定输出格式）

---

## 🗺️ Roadmap

### ✅ v1.0 — 已完成

- [x] 核心日志分析功能（DeepSeek AI 驱动）
- [x] 10+ CI/CD 平台自动识别
- [x] 结构化输出（错误摘要 / 根因 / 修复命令 / 排查命令）
- [x] 内置 3 种示例日志（npm / Docker / pytest）
- [x] 智能日志截断（保留头尾关键信息，中间省略）
- [x] 响应式 UI 设计 + 自定义 CSS 样式
- [x] 单元测试覆盖（log_parser + prompt）
- [x] 指数退避重试机制
- [x] 结构化异常处理
- [x] 单元测试
- [x] GitHub Actions CI
- [x] Few-shot Prompt 工程

### 🔜 v1.1 — 计划中

- [ ] 支持更多 AI 模型（OpenAI / Claude / 本地 Ollama）
- [ ] 日志文件拖拽上传（.log / .txt）
- [ ] 历史分析记录（浏览器 localStorage）
- [ ] 分析结果导出（Markdown / PDF）

### 🔮 v2.0 — 远期展望

- [ ] VS Code 插件版本
- [ ] GitHub Actions 集成（PR 评论自动分析）
- [ ] 暗色主题
- [ ] Docker 一键部署

---

## 🤝 Contributing

欢迎贡献！请查看 [CONTRIBUTING.md](./CONTRIBUTING.md) 了解：

- 如何本地运行项目
- Commit 信息规范（feat / fix / docs / test / ci / refactor）
- 如何提交 PR

## 📄 License

本项目基于 [MIT License](./LICENSE) 开源。

---

<div align="center">

**如果这个项目对你有帮助，请给一个 ⭐ Star 支持一下！**

Made with ❤️ by [Yanghh0220](https://github.com/Yanghh0220)

</div>
