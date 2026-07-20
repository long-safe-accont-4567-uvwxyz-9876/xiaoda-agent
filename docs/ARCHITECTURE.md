# xiaoda-agent 架构文档

## 项目概述

xiaoda-agent 是一个多模态 AI Agent 平台，支持多 Agent 协作、记忆管理、情感系统、工具调用、WebSocket 实时通信和 Web UI 管理界面。

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                      用户接入层                          │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐             │
│  │ Web UI   │  │ QQ Bot   │  │ CLI       │             │
│  │ (Vue3)   │  │ Adapter  │  │ Client    │             │
│  └────┬─────┘  └────┬─────┘  └─────┬─────┘             │
│       │              │              │                    │
├───────┼──────────────┼──────────────┼────────────────────┤
│       ▼              ▼              ▼                    │
│  ┌─────────────────────────────────────────┐            │
│  │         Web 服务层 (FastAPI)             │            │
│  │  ┌─────────┐ ┌─────────┐ ┌───────────┐ │            │
│  │  │ REST API│ │WebSocket│ │Middleware │ │            │
│  │  │ Routers │ │  Hub    │ │(Auth/CORS)│ │            │
│  │  └────┬────┘ └────┬────┘ └───────────┘ │            │
│  └───────┼───────────┼─────────────────────┘            │
│          │           │                                  │
├──────────┼───────────┼──────────────────────────────────┤
│          ▼           ▼                                  │
│  ┌─────────────────────────────────────────┐            │
│  │           Agent 核心层                   │            │
│  │  ┌──────────┐ ┌──────────┐ ┌─────────┐ │            │
│  │  │AgentCore │ │  Model   │ │ Prompt  │ │            │
│  │  │(消息处理) │ │  Router  │ │ Builder │ │            │
│  │  └──────────┘ └──────────┘ └─────────┘ │            │
│  │  ┌──────────┐ ┌──────────┐ ┌─────────┐ │            │
│  │  │ Agent    │ │  Agent   │ │ Agent   │ │            │
│  │  │Dispatcher│ │ Context  │ │ Core    │ │            │
│  │  └──────────┘ └──────────┘ └─────────┘ │            │
│  └─────────────────────────────────────────┘            │
│          │              │              │                │
├──────────┼──────────────┼──────────────┼────────────────┤
│          ▼              ▼              ▼                │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐       │
│  │  记忆系统   │ │  情感系统   │ │  安全系统     │       │
│  │ MemoryMgr  │ │ Emotion    │ │ Security     │       │
│  │ Knowledge  │ │ Portrait   │ │ Instruction  │       │
│  │ Notebook   │ │ Sticker    │ │ Hierarchy    │       │
│  │ Learning   │ │ TTS        │ │ Canary       │       │
│  │ VectorStore│ │ Nudge      │ │ SSRF Guard   │       │
│  └────────────┘ └────────────┘ └──────────────┘       │
│          │              │              │                │
├──────────┼──────────────┼──────────────┼────────────────┤
│          ▼              ▼              ▼                │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐       │
│  │  工具引擎   │ │  数据层     │ │  基础设施     │       │
│  │ Registry   │ │ SQLite     │ │ Config       │       │
│  │ Executor   │ │ aiosqlite  │ │ Credential   │       │
│  │ Guardrails │ │ FTS5       │ │ Background   │       │
│  │ MCP Client │ │ Analytics  │ │ Tasks        │       │
│  │ Repair     │ │ Session    │ │ Circuit      │       │
│  │ Search     │ │ Store      │ │ Breaker      │       │
│  └────────────┘ └────────────┘ └──────────────┘       │
└─────────────────────────────────────────────────────────┘
```

## 核心模块说明

### 入口与启动

| 模块 | 文件 | 职责 |
|------|------|------|
| 主入口 | `agent.py` | 解析命令行参数、加载 .env、启动 uvicorn Web 服务器 |
| 配置 | `config.py` | 全局配置管理（.env 路径、API Key、模型名称、系统提示词） |
| 提示词构建 | `prompt_builder.py` | 构建 system prompt（含安全化版本）、工作区文件读取与模板初始化 |

### Agent 核心层

| 模块 | 文件 | 职责 |
|------|------|------|
| AgentCore | `agent_core/core.py` | 核心基类，组合各 Mixin：消息处理、工具调用、记忆管理 |
| 消息处理 | `agent_core/message_processor.py` | 消息接收、LLM 调用、流式响应、工具调用循环 |
| 共享黑板 | `agent_core/shared_blackboard.py` | 跨 Agent 共享状态（当前话题、用户偏好） |
| 子Agent管理 | `agent_core/sub_agent_manager.py` | 子 Agent 创建、调度、结果收集 |
| 工具执行 | `agent_core/tool_executor.py` | 工具调用分发与执行 |

### Agent 调度与路由

| 模块 | 文件 | 职责 |
|------|------|------|
| Agent 调度器 | `agent_dispatcher.py` | 多 Agent 路由决策（根据任务类型选择 Agent） |
| 上下文管理 | `agent_context.py` | 对话历史管理、上下文压缩、token 预算控制 |
| 模型路由 | `model_router.py` | LLM 请求路由（标准/Pro/Flash 模型选择）、凭证池、prompt 缓存 |
| 任务编排 | `task_orchestrator.py` | 复杂任务分解与并行执行 |

### Web 服务层

| 模块 | 文件 | 职责 |
|------|------|------|
| FastAPI 应用 | `web/server.py` | 应用生命周期管理、中间件注册、路由挂载 |
| WebSocket Hub | `web/ws_hub.py` | WebSocket 连接管理、流式消息推送、终端会话 |
| 认证 | `web/routers/auth.py` | JWT 登录/登出/令牌刷新、会话管理 |
| 聊天 | `web/routers/chat.py` | 对话端点、会话管理、斜杠命令 |

### 记忆系统

| 模块 | 文件 | 职责 |
|------|------|------|
| 记忆管理器 | `memory/memory_manager.py` | 记忆存取、检索、编码、遗忘曲线 |
| 知识图谱 | `memory/knowledge_graph.py` | 实体-关系三元组存储与查询 |
| 向量存储 | `memory/vector_store.py` | 语义向量索引（FAISS/余弦相似度） |
| 笔记本 | `memory/notebook_manager.py` | 用户笔记 CRUD、自动摘要 |
| 学习管理 | `memory/learning_manager.py` | 学习评估、知识巩固 |
| 上下文压缩 | `memory/context_compressor.py` | 对话历史压缩（CCR 算法） |
| 上下文治理 | `memory/context_governance.py` | token 预算分配、重要度排序 |

### 情感系统

核心枚举 `Emotion`（定义于 `emotion/emotion_enum.py`）共 16 种：HAPPY / EXCITED / LOVE / SHY / SAD / ANGRY / SURPRISED / CONFUSED / THINKING / PLAYFUL / MOVED / NEUTRAL / ANXIOUS / FEAR / CURIOUS / POUT。通过三层映射机制串联消费端：

- **`EMOTION_ALIASES`**：中文词/英文变体 → 核心枚举（约 100 个别名），输入端宽容归并
- **`TTS_STYLE_MAP`**：核心枚举 → TTS 细分风格（部分降级，如 EXCITED→happy、SURPRISED→fear）
- **`STICKER_FALLBACK`**：核心枚举 → 贴纸类别（部分降级，如 CURIOUS→confused）

| 模块 | 文件 | 职责 |
|------|------|------|
| 统一枚举 | `emotion/emotion_enum.py` | 16 种核心情绪 + 三层映射（别名/TTS 风格/贴纸降级） |
| 情感检测 | `emotion/emotion_simple.py` | 文本情感分析（中文关键词+规则） |
| 情感状态 | `emotion/emotion_state.py` | 情感状态机、衰减与转移 |
| 画像管理 | `emotion/portrait_manager.py` | 用户画像构建与更新 |
| 贴纸管理 | `emotion/sticker_manager.py` | 表情贴纸选择与发送（按 `STICKER_FALLBACK` 降级） |
| TTS 引擎 | `emotion/tts_engine.py` | 语音合成（GPT-SoVITS/EdgeTTS，按 `TTS_STYLE_MAP` 选风格） |
| 推送引擎 | `emotion/nudge_engine.py` | 主动推送时机决策 |

### 安全系统

| 模块 | 文件 | 职责 |
|------|------|------|
| 安全过滤 | `security/security.py` | 输入/输出安全过滤、敏感信息检测 |
| 指令层级 | `security/instruction_hierarchy.py` | 系统指令优先级保护（防越狱） |
| 金丝雀 | `security/canary.py` | Canary Token 泄露检测与轮换 |
| SSRF 防护 | `security/ssrf_guard.py` | URL 请求验证（防 SSRF 攻击） |
| 凭证库 | `security/credential_vault.py` | API 密钥加密存储 |
| 密钥代理 | `security/secrets_broker.py` | 密钥解密与分发 |
| 权限管理 | `security/permission_manager.py` | 工具/操作权限控制 |
| 人工审批 | `security/human_approval.py` | 高风险操作人工确认 |
| 沙箱配置 | `security/sandbox_config.py` | 代码执行沙箱安全策略 |

### 工具引擎

| 模块 | 文件 | 职责 |
|------|------|------|
| 工具注册 | `tool_engine/tool_registry.py` | 工具元数据注册、OpenAI 格式转换 |
| 工具执行 | `tool_engine/tool_executor.py` | 工具调用分发、超时控制、结果包装 |
| 工具护栏 | `tool_engine/tool_guardrails.py` | 工具调用安全检查（参数校验、频率限制） |
| 工具修复 | `tool_engine/tool_repair.py` | 工具调用错误自动修复 |
| 工具搜索 | `tool_engine/tool_search.py` | 语义+关键词混合工具检索 |
| MCP 客户端 | `tool_engine/mcp_client.py` | Model Context Protocol 客户端 |
| 错误规则 | `tool_engine/error_rule_pipeline.py` | 错误分类与恢复策略 |

### 内置工具集

| 工具 | 文件 | 功能 |
|------|------|------|
| 代码执行 | `tools/code_tools_v2.py` | Python 代码沙箱执行（AST 审查+受限 builtins） |
| 文件操作 | `tools/file_tools_v2.py` | 文件读写、搜索、目录操作 |
| 网页浏览 | `tools/web_browse_tools.py` | 网页抓取、内容提取 |
| 增强浏览 | `tools/web_browse_enhanced.py` | 深度网页分析、截图 |
| 网络搜索 | `tools/web_tools_v2.py` | 多引擎搜索聚合 |
| 邮件工具 | `tools/mail_tools.py` | 邮件收发管理 |
| 硬件工具 | `tools/hardware_tools.py` | I2C/SPI/GPIO 硬件交互 |
| 系统工具 | `tools/system_tools.py` | 系统信息、进程管理 |
| 记忆工具 | `tools/memory_tool.py` | 记忆查询与操作 |
| 视觉工具 | `tools/vision_tools.py` | 图像分析 |
| 文档工具 | `tools/document_tools.py` | 文档解析与转换 |
| 国内搜索 | `tools/domestic_search_tools.py` | 国内搜索引擎适配 |
| 多源搜索 | `tools/multi_search_tools.py` | 多源搜索聚合 |
| Agnes 工具 | `tools/agnes_tools.py` | Agnes AI 平台集成 |
| 推送工具 | `tools/nudge_tool.py` | 主动消息推送 |

### 数据层

| 模块 | 文件 | 职责 |
|------|------|------|
| 数据库管理 | `db/database.py` | SQLite 连接管理、Schema 迁移、PRAGMA 优化 |
| 记忆数据库 | `db/db_memory.py` | 记忆表 CRUD |
| 笔记数据库 | `db/db_notebook.py` | 笔记表 CRUD |
| 学习数据库 | `db/db_learning.py` | 学习记录表 CRUD |
| 知识数据库 | `db/db_knowledge.py` | 知识图谱表 CRUD |
| 分析数据库 | `db/db_analytics.py` | 使用统计与分析 |
| 会话存储 | `db/session_store.py` | 对话会话持久化 |
| 索引管理 | `db/index_manager.py` | 数据库索引优化 |

### 基础设施

| 模块 | 文件 | 职责 |
|------|------|------|
| 后台任务 | `core/background_tasks.py` | fire-and-forget 后台协程管理 |
| 熔断器 | `core/circuit_breaker.py` | 服务降级与熔断 |
| 降级策略 | `core/degradation_strategy.py` | LLM 降级策略（标准→Flash→本地） |
| 降级检测 | `core/degradation_detector.py` | 服务质量检测与降级触发 |
| 自愈 | `core/tnr_self_heal.py` | TNR 协议自愈恢复 |
| 医生 | `core/doctor.py` | 系统诊断与自动修复 |
| 引导 | `core/bootstrap.py` | 系统启动引导与依赖检查 |
| 配置重载 | `core/config_reloader.py` | 运行时配置热更新 |
| XP 系统 | `core/xp_system.py` | 用户经验值与等级 |
| 心理状态 | `core/mental_state.py` | Agent 心理状态建模 |
| 人格一致性 | `core/persona_coherence.py` | 人格一致性校验 |
| 错误码 | `core/error_codes.py` | 统一错误码定义 |

### QQ Bot 适配器

| 模块 | 文件 | 职责 |
|------|------|------|
| QQ 适配器 | `qq_bot_adapter.py` | QQ 官方机器人协议适配、消息收发、流式响应 |

## 数据流

### 对话处理流程

```
用户消息 → Web/QQ/CLI
    → AgentDispatcher（选择 Agent）
    → AgentCore.process()
        → AgentContext（加载对话历史）
        → PromptBuilder（构建 system prompt）
        → ModelRouter（选择 LLM 模型）
        → LLM API（流式调用）
            → 工具调用？
                → ToolExecutor → 工具执行 → 结果回注 → 继续调用
            → 最终回复
        → MemoryManager（存储对话记忆）
        → EmotionSystem（情感检测与响应）
        → WebSocket（流式推送）
    → 用户
```

### WebSocket 协议

```
客户端 → 服务端:
  chat          发送对话消息
  terminal_input  终端输入
  terminal_resize 终端调整大小
  terminal_kill   终止终端会话

服务端 → 客户端:
  chat_delta    流式文本片段
  chat_done     对话完成
  tool_start    工具调用开始
  tool_end      工具调用结束
  health_progress 健康检查进度
  health_done   健康检查完成
  terminal_output 终端输出
  greeting      问候推送
```

## 部署架构

```
┌──────────────────────────────────────┐
│           单机部署                    │
│  ┌──────────────────────────────┐    │
│  │  agent.py (uvicorn)          │    │
│  │  ├─ FastAPI (REST + WS)      │    │
│  │  ├─ SQLite (WAL mode)        │    │
│  │  ├─ 文件存储 (data/)          │    │
│  │  └─ 前端静态文件 (dist/)      │    │
│  └──────────────────────────────┘    │
│  端口: 8082 (默认)                    │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│           Docker 部署                 │
│  docker-compose.yml                  │
│  ├─ xiaoda-agent:8082               │
│  │   ├─ /data (持久化)               │
│  │   └─ /app (代码)                  │
│  └─ 可选: Ollama:11434              │
└──────────────────────────────────────┘
```

## 关键设计决策

1. **SQLite + WAL**: 轻量级单机部署，WAL 模式支持并发读写，FAT 文件系统自动降级为 DELETE 模式
2. **多 Agent 架构**: 通过 AgentDispatcher 路由到不同人格的 Agent（小达、小可、小朗、小莉、小莲）
3. **凭证池**: 多 API Key 轮换，支持负载均衡和故障转移
4. **安全纵深防御**: 指令层级 > Canary Token > SSRF 防护 > 沙箱 > 人工审批
5. **降级策略**: 标准 → Pro → Flash → 本地模型，自动降级保证可用性
6. **PyInstaller 兼容**: frozen 模式下 .env 迁移到用户目录，确保非管理员可写