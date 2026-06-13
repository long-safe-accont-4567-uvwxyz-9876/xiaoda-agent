# Nahida Agent 🌿

> 运行在 Orange Pi 上的多智能体 AI 助手 — 以《原神》纳西妲为灵魂，40+ 工具赋能，三通道交互，认知系统闭环

<p align="center">
  <strong>多智能体</strong> · <strong>认知闭环</strong> · <strong>工具链</strong> · <strong>边缘部署</strong> · <strong>多模态</strong> · <strong>Web UI</strong>
</p>

***

## 为什么选择 Nahida Agent？

市面上有大量 AI Chatbot 项目，但 Nahida Agent 不只是"套壳 ChatGPT"。它是一个**完整的认知智能体**——能记住、能学习、能感知情绪、能调用工具、能自我改进。

| 对比维度 | 通用 Chatbot    | Nahida Agent               |
| ---- | ------------- | -------------------------- |
| 记忆   | 无状态 / 简单上下文窗口 | 情景记忆 + 向量检索 + 知识图谱 + 用户画像  |
| 学习   | 不会从对话中学习      | 自动提取规则、发现模式、自我改进           |
| 情绪   | 无             | 9 类情绪检测 → 贴纸 + 语音风格联动      |
| 工具   | 少量 API 调用     | 40+ 工具，含硬件控制（GPIO/I2C/PWM） |
| 多智能体 | 单一模型          | 5 角色人格 + 图编排 + 委托机制        |
| 部署   | 云端依赖          | 边缘设备运行，Docker 一键部署         |
| 交互   | 单一通道          | QQ Bot + Web UI + CLI 三通道  |

***

## 核心特色

### 🎭 多智能体人格系统

5 个独立角色人格，各有专属音色、贴纸集和人格 Prompt：

| 角色      | 定位        | 特色             |
| ------- | --------- | -------------- |
| **纳西妲** | 主人格 / 调度者 | 温柔智慧，负责路由和综合   |
| **可莉**  | 玩伴        | 活泼可爱，擅长聊天和游戏   |
| **银狼**  | 编程专家      | 技术导向，擅长代码和系统管理 |
| **昔涟**  | 知性助手      | 冷静理性，擅长分析和文档   |
| **尼可**  | 创意伙伴      | 灵感丰富，擅长图像和视频生成 |

**独到之处**：

- **图编排引擎**：类 LangGraph 的 TaskGraph，支持条件路由、并行执行、结果综合
- **委托机制**：子智能体可向主人格请求协助，深度限制防递归爆炸
- **ToolCallExtractor 统一接口**：标准 tool\_calls 和 DSML 文本标记统一提取，消除双路径重复

### 🧠 认知闭环系统

不是简单的"对话→回复"，而是**感知→记忆→学习→改进**的完整闭环：

```
对话输入
  ├→ 情绪检测 → 影响回复风格 + 贴纸选择 + 语音语调
  ├→ 情景记忆 → 向量检索相关历史，注入上下文
  ├→ 知识图谱 → 提取实体关系，构建用户知识网络
  ├→ 用户画像 → 动态更新性格画像，个性化回复
  ├→ 学习系统 → 从对话模式提取规则，自我改进
  └→ 笔记本 → 自动笔记、任务跟踪、关注点管理
```

**独到之处**：

- **记忆双存储**：结构化记忆（SQLite）+ 语义向量（sqlite-vec），检索时双路召回
- **画像自动演进**：无需冷启动问卷，从对话中自动建立和维护用户画像
- **学习闭环**：从对话中提取 insight → 分类（错误/功能请求/模式）→ 优先级排序 → 状态追踪

### 🔧 40+ 工具链

| 类别        | 工具                          | 亮点                              |
| --------- | --------------------------- | ------------------------------- |
| **文件操作**  | 列出/读取/写入/搜索文件               | 智能路径解析，安全沙箱                     |
| **代码执行**  | Python 沙箱                   | AST 审查 + 白名单内建函数 + 禁止模块检测       |
| **网络搜索**  | 多引擎搜索（Bing/Baidu/Google）    | 自动降级，引擎不可用时切换                   |
| **网页浏览**  | 抓取和提取网页内容                   | SSRF 防护（DNS 预检 + 内网 IP 过滤）      |
| **系统管理**  | Shell 命令 / Docker / systemd | 权限分级（DEFAULT/DEV/STRICT/BYPASS） |
| **硬件控制**  | GPIO / I2C / PWM / 传感器      | 边缘设备专属，容器内优雅降级                  |
| **AI 生成** | 图像 / 视频 / TTS 语音            | Agnes AI + MiMo TTS，速率限制 + 缓存   |
| **文档阅读**  | PDF / DOCX / Excel / PPT    | 多格式解析，智能截断                      |
| **视觉识别**  | 摄像头 + YOLOv5                | NPU 加速（RK3588S），实时目标检测          |
| **记忆管理**  | remember / recall / forget  | 工具级记忆操作，用户可控                    |
| **知识查询**  | Wolfram Alpha / 天气          | 结构化知识获取                         |

**独到之处**：

- **工具护栏**：频率限制 + 风暴检测 + 参数修复，防止工具调用失控
- **AST 沙箱**：Python 执行器使用语法树审查，比正则更难绕过
- **SSRF 防护**：DNS 解析预检 + HTTP 层 IP 验证，防 DNS Rebinding
- **每工具超时**：视频生成 240s、文档读取 120s、默认 60s，不再一刀切

### 🛡️ 安全与可靠性

| 机制                | 说明                                              |
| ----------------- | ----------------------------------------------- |
| **权限分级**          | DEFAULT（按置信度 block/warn）/ DEV / STRICT / BYPASS |
| **沙箱配置**          | 默认阻止内网 IP、限制端口、限制文件访问路径                         |
| **AST 代码审查**      | 禁止 `__import__`/`eval`/`exec`/`open` 等，白名单内建函数  |
| **工具护栏**          | 频率限制 + 风暴检测 + JSON 参数修复                         |
| **SSRF 防护**       | DNS 预检 + HTTP 层实际 IP 验证                         |
| **情绪标签剥离**        | 双重保护：sticker\_manager 清理 + 正则兜底                 |
| **委托深度限制**        | 最大 2 层委托，防止递归爆炸                                 |
| **TaskGraph 环检测** | 节点访问计数 + 全局/节点级超时                               |
| **并发安全**          | asyncio.Lock 保护共享状态，Semaphore 限制并行工具数           |
| **API Key 探活**    | SubAgent 启动时验证凭证有效性                             |

### 🗣️ 多模态输出

```
文本回复 → 情绪检测 → 贴纸选择 → 语音合成
    ↓           ↓           ↓           ↓
  用户可见    [emotion:happy]  🌸贴纸    MiMo TTS
    ↓           ↓
  剥离标签    统一枚举映射
```

- **9 类核心情绪**：HAPPY / SAD / ANGRY / ANXIOUS / SHY / CURIOUS / THINKING / FEAR / NEUTRAL
- **情绪→贴纸映射**：统一枚举 `Emotion` → `STICKER_FALLBACK` 字典
- **情绪→语音映射**：统一枚举 `Emotion` → `TTS_STYLE_MAP` 字典
- **TTS 缓存持久化**：合成结果缓存到磁盘，重复文本零延迟

### 🌐 三通道交互

| 通道         | 入口                  | 特点                                 |
| ---------- | ------------------- | ---------------------------------- |
| **QQ Bot** | `qq_bot_adapter.py` | 生产级，消息去重 + 分段发送 + SILK 语音          |
| **Web UI** | `web/server.py`     | FastAPI + Vue 3 + WebSocket，须弥主题   |
| **CLI**    | `cli.py`            | 打字机效果 + readline 历史 + NO\_COLOR 支持 |

**Web UI 特色**：

- 须弥主题（草元素配色 + 粒子特效 + 3D 卡片交互）
- 12 个功能视图：Chat / Agents / Models / Tools / MCP / Insight / Schedule / Media / Health / Dashboard / Settings / Login
- WebSocket 实时推送（工具调用状态 / 情绪变化 / 健康检查）
- Agent 独立壁纸系统

***

## 架构设计

### 整体架构

```
                    ┌─────────────────────────────────────────┐
                    │           交互层 (3 通道)                │
                    │  QQ Bot  │  Web UI (FastAPI+Vue3)  │  CLI │
                    └────┬─────┴──────────┬──────────────┴──┬──┘
                         │                │                  │
                    ┌────▼────────────────▼──────────────────▼──┐
                    │              AgentCore 编排器              │
                    │  ┌──────────┐ ┌───────────┐ ┌──────────┐ │
                    │  │ Security │ │  Slash     │ │  Router   │ │
                    │  │ Filter   │ │  Commands  │ │  Engine   │ │
                    │  └──────────┘ └───────────┘ └─────┬────┘ │
                    │                                    │      │
                    │  ┌─────────────────────────────────▼────┐ │
                    │  │          TaskGraph 图编排             │ │
                    │  │  RouterNode → ParallelAgentNode →    │ │
                    │  │  SynthesisNode (条件路由+并行+综合)   │ │
                    │  └─────────────────┬───────────────────┘ │
                    │                    │                      │
                    │  ┌─────────────────▼───────────────────┐ │
                    │  │         ChatProcessor 对话处理        │ │
                    │  │  ModelRouter → ToolOrchestrator →    │ │
                    │  │  BackgroundTaskManager               │ │
                    │  └─────────────────┬───────────────────┘ │
                    └────────────────────┼─────────────────────┘
                                         │
              ┌──────────────────────────┼──────────────────────────┐
              │                          │                          │
    ┌─────────▼──────────┐  ┌───────────▼──────────┐  ┌───────────▼──────────┐
    │    认知系统         │  │     工具链 (40+)      │  │    输出系统           │
    │  情景记忆           │  │  文件/代码/搜索       │  │  情绪检测             │
    │  知识图谱           │  │  网络/系统/硬件       │  │  贴纸选择             │
    │  用户画像           │  │  AI生成/文档/视觉     │  │  TTS 语音合成         │
    │  学习系统           │  │  记忆/知识查询        │  │  文本去AI化           │
    │  笔记本             │  │                      │  │                      │
    └────────────────────┘  └──────────────────────┘  └──────────────────────┘
```

### 核心模块拆分

AgentCore 从 1431 行 God Class 拆分为 5 个子模块：

| 模块                          | 职责                          |
| --------------------------- | --------------------------- |
| `core/bootstrap.py`         | 启动引导，依赖注入                   |
| `core/router_engine.py`     | 统一路由决策（RoutingDecision 数据类） |
| `core/chat_processor.py`    | 单轮对话主流程                     |
| `core/tool_orchestrator.py` | 工具调用编排                      |
| `core/background_tasks.py`  | 后台任务队列（记忆/画像/学习/笔记）         |

### 数据流

```
用户消息
  │
  ├─ 1. 安全过滤（SecurityFilter）
  ├─ 2. 斜杠命令检查（SlashCommandHandler）
  ├─ 3. 路由决策（RouterEngine → RoutingDecision）
  │     ├─ 直接回复（nahida）
  │     ├─ 委托子智能体（keli/yinlang/xilian/nike）
  │     └─ 图编排（TaskGraph）
  ├─ 4. LLM 调用（ModelRouter + CredentialPool + ErrorClassifier）
  │     ├─ 工具调用 → ToolCallExtractor → ToolGuardrails → ToolExecutor
  │     └─ 纯文本回复
  ├─ 5. 后处理
  │     ├─ 情绪检测 → ensure_emotion_tag → strip_emotion_tag
  │     ├─ 贴纸选择（StickerManager）
  │     ├─ TTS 语音合成（TTSEngine + 缓存）
  │     └─ 文本去AI化（humanize）
  └─ 6. 后台任务（异步）
        ├─ 记忆编码（MemoryManager + VectorStore）
        ├─ 画像更新（PortraitManager）
        ├─ 学习提取（LearningManager）
        └─ 笔记记录（NotebookManager）
```

### 监控体系

```python
# metrics.py — 4 类指标，23 个埋点
Metrics.inc("tool.exec.success")           # 计数器
Metrics.observe("model.router.latency", t)  # 计时器 (avg/p95)
Metrics.gauge("memory.count", n)            # 仪表盘
Metrics.histogram("tool.exec.duration", t)  # 直方图
```

***

## 项目结构

```
nahida-agent/
├── agent.py                  # 主入口（Web/CLI 模式切换）
├── agent_core.py             # AgentCore 核心编排器
├── core/                     # AgentCore 子模块
│   ├── bootstrap.py          #   启动引导
│   ├── router_engine.py      #   路由引擎 + RoutingDecision
│   ├── chat_processor.py     #   对话处理
│   ├── tool_orchestrator.py  #   工具编排
│   ├── background_tasks.py   #   后台任务队列
│   └── delegation.py         #   委托机制数据类
├── agent_dispatcher.py       # 子智能体调度器 + ToolCallExtractor
├── task_orchestrator.py      # TaskGraph 图编排引擎
├── model_router.py           # LLM API 路由 + 凭证池 + 错误分类
├── tool_call_handler.py      # 工具调用处理（并行信号量）
├── tool_executor.py          # 工具执行器（每工具超时）
├── tool_registry.py          # 工具注册表
├── tool_repair.py            # 工具调用修复（JSON 规范化）
├── tool_guardrails.py        # 工具护栏（频率+风暴检测）
├── permission_manager.py     # 权限管理（4 级分级）
├── sandbox_config.py         # 沙箱安全配置
├── error_classifier.py       # 错误分类器（HTTP 状态码匹配）
├── credential_pool.py        # API 凭证池（并发安全）
├── emotion_enum.py           # 情感统一枚举系统
├── emotion_simple.py         # 情绪检测
├── sticker_manager.py        # 贴纸管理
├── tts_engine.py             # TTS 语音合成（缓存持久化）
├── security.py               # 安全过滤
├── text_utils.py             # 文本处理（去AI化/截断/分段）
├── context_compressor.py     # 上下文压缩（Token 驱动）
├── agent_context.py          # 对话上下文管理
├── knowledge_graph.py        # 知识图谱
├── portrait_manager.py       # 用户画像
├── memory_manager.py         # 情景记忆
├── vector_store.py           # 向量存储（线程安全 + 事务原子化）
├── nudge_engine.py           # 主动问候引擎（时区支持）
├── slash_commands.py         # 15+ Slash 命令
├── qq_bot_adapter.py         # QQ Bot 适配器
├── config.py                 # 配置中心（环境变量驱动）
├── database.py               # 数据库管理（自动迁移）
├── metrics.py                # 监控指标框架
├── cli.py                    # CLI 交互界面
├── cli_client.py             # WebSocket CLI 客户端
├── tools/                    # 工具模块
│   ├── system_tools.py       #   Shell/进程/Docker/服务
│   ├── hardware_tools.py     #   GPIO/I2C/PWM/传感器
│   ├── code_tools_v2.py      #   Python 沙箱（AST 审查）
│   ├── web_tools_v2.py       #   HTTP/API
│   ├── web_browse_tools.py   #   网页浏览（SSRF 防护）
│   ├── multi_search_tools.py #   多引擎搜索
│   ├── file_tools_v2.py      #   文件操作
│   ├── document_tools.py     #   文档阅读（PDF/DOCX/XLSX/PPT）
│   ├── memory_tool.py        #   记忆工具（bind 注入）
│   ├── agnes_tools.py        #   AI 生成（速率限制）
│   └── ...
├── web/                      # Web UI
│   ├── server.py             #   FastAPI 服务
│   ├── ws_hub.py             #   WebSocket 中心
│   ├── routers/              #   12 个 API 路由模块
│   ├── frontend/             #   Vue 3 + Naive UI 前端源码
│   └── dist/                 #   前端构建产物
├── db/
│   └── schema.sql            # 数据库 Schema（21 表 + 22 索引）
├── deploy/
│   └── qq-agent.service      # systemd 服务参考
├── docs/
│   ├── IMPROVEMENT_PLAN.md   # 改进计划（517 行深度审查）
│   └── WEBUI_DESIGN.md       # Web UI 设计文档
├── config/
│   ├── agent.json5           # 主配置
│   ├── agents/keli.json      # 子智能体配置
│   └── workspace/*.md        # 7 个 Workspace Prompt
├── Dockerfile                # Docker 镜像定义
├── docker-compose.yml        # 一键编排
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量模板
└── SETUP.md                  # 部署指南
```

***

## 快速开始

### Docker 一键部署（推荐）

```MariaDB&#x20;SQL
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent.git
cd nahida-agent
cp .env.example .env
# 编辑 .env 填写 API 密钥
docker compose up -d
```

访问 `http://localhost:8080` 即可使用。

### 手动部署

详见 [SETUP.md](SETUP.md)。

***

## 项目规模

| 指标         | 数值           |
| ---------- | ------------ |
| Python 模块  | 60+          |
| 生产代码       | \~15,000 行   |
| 测试代码       | \~6,000 行    |
| 工具数量       | 40+          |
| 角色人格       | 5 个          |
| 数据库表       | 21 张 + 22 索引 |
| Web API 路由 | 12 模块        |
| Web UI 视图  | 12 个         |

***

## 关键技术决策

| 决策      | 选择                 | 理由                         |
| ------- | ------------------ | -------------------------- |
| LLM API | MiMo (小米)          | 国产模型，延迟低，成本可控，支持 TTS       |
| 数据库     | SQLite (aiosqlite) | 单设备部署，零运维，WAL 模式并发         |
| 向量检索    | sqlite-vec         | 小规模数据无需 ANN，与 SQLite 统一存储  |
| 情感系统    | 统一枚举 Emotion       | 9 类情绪 → 贴纸/语音/显示 三路映射      |
| 工具调用    | ToolCallExtractor  | 标准 tool\_calls + DSML 统一提取 |
| 代码沙箱    | AST 审查             | 比正则更难绕过，禁止模块/内建白名单         |
| Web 框架  | FastAPI + Vue 3    | 异步原生 + 现代前端，WebSocket 实时通信 |
| 部署      | Docker             | 一键复现，volume 持久化，硬件可选直通     |

***

## License

MIT
