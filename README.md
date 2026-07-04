# Xiaoda Agent 🌿

> 本项目为社区二创作品，非官方项目，仅供个人学习 Agent 技术使用。

> 运行在 windows和Linux系统上的多智能体 AI 助手 ，40+ 工具赋能，三通道交互，认知系统闭环，RAG 检索增强

<p align="center">
  <strong>多智能体</strong> · <strong>认知闭环</strong> · <strong>RAG 增强</strong> · <strong>插件系统</strong> · <strong>MCP 协议</strong> · <strong>边缘部署</strong>
</p>

***

## 为什么选择 Xiaoda Agent？

市面上有大量 AI Chatbot 项目，但 Xiaoda Agent 不只是"套壳 ChatGPT"。它是一个**完整的认知智能体**——能记住、能学习、能感知情绪、能调用工具、能自我改进。

| 对比维度   | 通用 Chatbot        | Xiaoda Agent                   |
| -------- | ------------------ | ------------------------------ |
| 记忆      | 无状态 / 简单上下文窗口 | 情景记忆 + 向量检索 + Reranker 精排 + 知识图谱 + 用户画像 |
| 检索      | 单路向量召回          | FTS5 BM25 + bge-m3 向量 → RRF 融合 → 交叉编码器精排 + KG 增强 |
| 学习      | 不会从对话中学习       | 自动提取规则、发现模式、自我改进           |
| 情绪      | 无                  | 9 类情绪检测 → 贴纸 + 语音风格联动      |
| 工具      | 少量 API 调用        | 40+ 内置工具 + MCP 协议扩展 + 插件系统  |
| 多智能体   | 单一模型             | 5 角色人格 + 图编排 + 委托机制        |
| 部署      | 云端依赖             | 边缘设备运行，Docker / 安装包一键部署    |
| 交互      | 单一通道             | QQ Bot + Web UI + CLI 三通道    |
| 配置      | 改配置文件重启        | WebUI 热生效配置（DND/问候/模型/工具）  |

***

## 核心特色

### 🎭 多智能体人格系统

5 个独立角色人格，各有专属音色、贴纸集和人格 Prompt：

| 角色      | 定位        | 特色             |
| ------- | --------- | -------------- |
| **小妲** | 主人格 / 调度者 | 温柔智慧，负责路由和综合   |
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

### 🔍 RAG 检索增强（三阶段优化）

从"单路向量召回"升级为"多路召回 → 融合 → 精排"的专业 RAG 管线：

```
用户查询
  ├→ QueryTransformer（查询变换）
  │    └→ 免费模型改写 + 多视角扩展（不占主模型配额）
  ├→ 双路召回
  │    ├→ FTS5 BM25 全文检索（关键词精确匹配）
  │    └→ bge-m3 向量检索（语义相似度）
  ├→ RRF 融合（Reciprocal Rank Fusion）
  │    └→ 过采样 3x 候选集
  ├→ Reranker 精排
  │    └→ bge-reranker-v2-m3 交叉编码器（SiliconFlow 免费）
  └→ KG 增强
       └→ 实体重叠 + 关系路径加权（最高 +0.5）
```

**独到之处**：

- **零成本精排**：Reranker 使用 SiliconFlow 免费 API，不增加任何开销
- **查询变换免费化**：QueryTransformer 使用 Qwen3-8B 免费模型，不占主模型配额
- **KG 评分融合**：`final = 0.65×rerank + 0.15×kg_boost + 0.20×(importance×decay)`
- **降级容错**：Reranker/QueryTransformer 不可用时自动降级，不影响主流程

### 🔧 工具链（40+ 内置 + MCP 扩展 + 插件）

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
| **MCP 协议** | 外部 MCP 服务器工具                | stdio/SSE/HTTP 三传输，自动发现注册       |

**独到之处**：

- **工具护栏**：频率限制 + 风暴检测 + 参数修复，防止工具调用失控
- **AST 沙箱**：Python 执行器使用语法树审查，比正则更难绕过
- **SSRF 防护**：DNS 解析预检 + HTTP 层 IP 验证，防 DNS Rebinding
- **每工具超时**：视频生成 240s、文档读取 120s、默认 60s，不再一刀切
- **结果压缩**：超长工具输出用免费模型压缩到 1/3，节省上下文窗口
- **MCP 扩展**：支持连接外部 MCP 服务器，工具能力无限扩展

### 🧩 插件系统

完整的插件生命周期管理，支持第三方扩展：

```
plugins/
  ├── manifest.py      # YAML 清单解析
  ├── discovery.py     # 目录扫描发现
  ├── permissions.py   # 声明式权限白名单
  ├── sdk.py           # Plugin ABC + @register_tool + @subscribe
  ├── context.py       # 注入 memory/kg/mcp 等能力
  ├── manager.py       # FOUND→LOADED→ENABLED 状态机
  └── echo/            # 示例插件
```

- **声明式权限**：插件在 YAML 中声明网络/文件系统/工具权限，运行时强制检查
- **能力注入**：插件通过 Context 访问记忆、知识图谱、MCP 等系统能力
- **事件订阅**：`@subscribe` 装饰器订阅对话事件，无需轮询

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
| **QQ Bot** | `qq_bot_adapter.py` | 生产级，消息去重 + 分段发送 + SILK 语音 + 主人自动绑定 |
| **Web UI** | `web/server.py`     | FastAPI + Vue 3 + WebSocket，须弥主题   |
| **CLI**    | `cli.py`            | 打字机效果 + readline 历史 + NO\_COLOR 支持 |

**QQ Bot 特色**：

- 主人身份自动绑定：私聊首消息 / 拉群事件自动识别，无需手动配置 OpenID
- 群聊双 OpenID 匹配：同时检查 user_openid 和 member_openid
- 主动问候引擎：闲置超阈值自动发问候，时区感知 DND，与 WebUI 共享配额

**Web UI 特色**：

- 须弥主题（草元素配色 + 粒子特效 + 3D 卡片交互）
- 13 个功能视图：Chat / Agents / Models / Tools / MCP / Plugins / Insight / Schedule / Media / Health / Dashboard / Settings / Setup
- WebSocket 实时推送（工具调用状态 / 情绪变化 / 健康检查）
- Agent 独立壁纸系统
- **热生效配置**：DND 时段 / 问候配额 / 模型路由 / 工具开关，改完即时生效无需重启
- **Setup 向导**：首次运行引导，10+ Provider Key 在线验证（测试通过再保存）

### ⏰ 主动行为系统

双引擎协同，WebUI 统一调控：

| 引机 | 通道 | 触发方式 | 特色 |
| --- | --- | --- | --- |
| **NudgeEngine** | QQ | 用户闲置超阈值 | 问候 + 任务提醒 + 学习晋升 + 画像整合 + 数据清理 |
| **GreetingScheduler** | Web + QQ | 定时/随机计划 | fixed 定点 + random 窗口抽签，多段跨午夜 DND，补发机制 |

- **共享配额**：两引擎共享 `greeting_log` 表计数，不会超过 WebUI 设置的每日上限
- **DND 统一**：两引擎读取同一份 `schedule.dnd_periods` 配置
- **CoT 清洗**：自动剥离推理模型思维链（`<think>` 标签 + CoT 前缀），防止泄漏到消息

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
    │    认知系统         │  │     工具链            │  │    输出系统           │
    │  情景记忆           │  │  40+ 内置工具         │  │  情绪检测             │
    │  RAG 检索增强       │  │  MCP 协议扩展         │  │  贴纸选择             │
    │  ├ QueryTransform  │  │  插件系统             │  │  TTS 语音合成         │
    │  ├ Reranker 精排   │  │  文件/代码/搜索       │  │  文本去AI化           │
    │  └ KG 增强         │  │  网络/系统/硬件       │  │                      │
    │  知识图谱           │  │  AI生成/文档/视觉     │  │  主动行为系统         │
    │  用户画像           │  │  记忆/知识查询        │  │  ├ NudgeEngine       │
    │  学习系统           │  │                      │  │  └ GreetingScheduler │
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
  │     ├─ 直接回复（xiaoda）
  │     ├─ 委托子智能体（keli/yinlang/xilian/nike）
  │     └─ 图编排（TaskGraph）
  ├─ 4. 记忆检索（RAG 管线）
  │     ├─ QueryTransformer 查询变换（免费模型）
  │     ├─ FTS5 BM25 + bge-m3 向量双路召回
  │     ├─ RRF 融合 → Reranker 精排
  │     └─ KG 增强（实体重叠 + 关系路径加权）
  ├─ 5. LLM 调用（ModelRouter + CredentialPool + ErrorClassifier）
  │     ├─ 工具调用 → ToolCallExtractor → ToolGuardrails → ToolExecutor
  │     │    └─ ResultWrapper 结果压缩（免费模型）
  │     └─ 纯文本回复
  ├─ 6. 后处理
  │     ├─ 情绪检测 → ensure_emotion_tag → strip_emotion_tag
  │     ├─ 贴纸选择（StickerManager）
  │     ├─ TTS 语音合成（TTSEngine + 缓存）
  │     └─ 文本去AI化（humanize）
  └─ 7. 后台任务（异步）
        ├─ 记忆编码（MemoryManager + VectorStore，免费模型）
        ├─ 知识图谱提取（KnowledgeGraph，免费模型）
        ├─ 画像更新（PortraitManager）
        ├─ 学习提取（LearningManager）
        └─ 笔记记录（NotebookManager，免费模型）
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
xiaoda-agent/
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
├── transports/               # Provider Transport 抽象层
│   ├── base.py               #   统一接口 + TransportResponse
│   ├── mimo_transport.py     #   小米 MiMo 适配
│   └── agnes_transport.py    #   Agnes AI 适配
├── tool_engine/              # 工具引擎
│   ├── tool_call_handler.py  #   工具调用处理（并行信号量）
│   ├── tool_executor.py      #   工具执行器（每工具超时）
│   ├── tool_registry.py      #   工具注册表
│   ├── tool_repair.py        #   工具调用修复（JSON 规范化）
│   ├── tool_guardrails.py    #   工具护栏（频率+风暴检测）
│   └── mcp_client.py         #   MCP 协议客户端（stdio/SSE/HTTP）
├── plugins/                  # 插件系统
│   ├── manifest.py           #   YAML 清单解析
│   ├── discovery.py          #   目录扫描发现
│   ├── permissions.py        #   声明式权限白名单
│   ├── sdk.py                #   Plugin ABC + 装饰器
│   ├── context.py            #   能力注入上下文
│   ├── manager.py            #   生命周期管理
│   └── echo/                 #   示例插件
├── memory/                   # 认知系统
│   ├── memory_manager.py     #   情景记忆 + RAG 管线集成
│   ├── vector_store.py       #   向量存储（线程安全 + 事务原子化）
│   ├── reranker.py           #   bge-reranker-v2-m3 交叉编码器精排
│   ├── query_transform.py    #   查询变换（免费模型改写+扩展）
│   ├── knowledge_graph.py    #   知识图谱 + 检索增强评分
│   ├── learning_manager.py   #   学习系统
│   ├── notebook_manager.py   #   笔记本（免费模型编码）
│   ├── portrait_manager.py   #   用户画像（移至 emotion/）
│   ├── context_compressor.py #   上下文压缩（Token 驱动）
│   └── context_usage.py      #   上下文使用分析
├── emotion/                  # 情感与主动行为
│   ├── emotion_enum.py       #   情感统一枚举系统
│   ├── emotion_simple.py     #   情绪检测
│   ├── sticker_manager.py    #   贴纸管理
│   ├── tts_engine.py         #   TTS 语音合成（缓存持久化）
│   ├── nudge_engine.py       #   主动问候引擎（QQ 通道）
│   └── portrait_manager.py   #   用户画像
├── web/                      # Web UI
│   ├── server.py             #   FastAPI 服务
│   ├── app.py                #   应用工厂
│   ├── ws_hub.py             #   WebSocket 中心
│   ├── config_service.py     #   WebUI 热生效配置层
│   ├── greeting_scheduler.py #   问候调度器（Web+QQ 通道）
│   ├── media_tasks.py        #   媒体任务管理
│   ├── routers/              #   13 个 API 路由模块
│   │   ├── setup.py          #     Setup 向导（10+ Key 在线验证）
│   │   ├── schedule.py       #     问候调度配置
│   │   ├── plugins.py        #     插件管理
│   │   ├── mcp.py            #     MCP 服务器管理
│   │   └── ...
│   ├── frontend/             #   Vue 3 + Naive UI 前端源码
│   └── dist/                 #   前端构建产物
├── utils/                    # 工具函数
│   ├── result_wrapper.py     #   工具结果压缩（免费模型）
│   ├── prompt_caching.py     #   提示词缓存（KV 缓存断点）
│   ├── credential_pool.py    #   API 凭证池（并发安全）
│   ├── error_classifier.py   #   错误分类器
│   ├── metrics.py            #   监控指标框架
│   ├── text_utils.py         #   文本处理（去AI化/截断/分段）
│   ├── atomic_write.py       #   原子文件写入
│   ├── lazy_deps.py          #   懒加载依赖
│   ├── npu_inference.py      #   NPU 推理（RK3588S）
│   ├── vision_service.py     #   视觉服务
│   ├── file_receiver.py      #   文件接收
│   ├── smart_error_handler.py #  智能错误处理
│   ├── nahida_acp.py         #   小妲 ACP 协议
│   └── logging_config.py     #   日志配置
├── security/                 # 安全模块
│   ├── security.py           #   安全过滤
│   ├── permission_manager.py #   权限管理（4 级分级）
│   └── sandbox_config.py     #   沙箱安全配置
├── tools/                    # 内置工具模块
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
│   ├── vision_tools.py       #   视觉识别
│   └── nudge_tool.py         #   主动消息工具
├── db/                       # 数据库
│   ├── database.py           #   数据库管理（自动迁移）
│   ├── schema.sql            #   Schema（21 表 + 22 索引）
│   ├── db_analytics.py       #   分析数据
│   ├── db_memory.py          #   记忆数据
│   ├── db_knowledge.py       #   知识数据
│   ├── db_learning.py        #   学习数据
│   ├── db_notebook.py        #   笔记本数据
│   └── session_store.py      #   会话存储
├── scripts/                  # 部署与运维脚本
│   ├── install-linux.sh      #   Linux 自解压安装器
│   ├── install-windows.ps1   #   Windows 安装
│   ├── installer.nsi         #   NSIS 安装包定义
│   ├── auto-update.sh/.bat   #   GitHub Release 自动更新
│   ├── healthcheck.sh        #   健康检查
│   ├── start.sh/.bat         #   启动脚本
│   └── build-release.sh      #   构建发布
├── config/                   # 配置
│   ├── agent.json5           #   主配置
│   ├── agents/               #   子智能体配置
│   └── workspace/*.md        #   8 个 Workspace Prompt
├── setup_wizard.py           # CLI 安装向导
├── config.py                 # 配置中心（环境变量驱动）
├── qq_bot_adapter.py         # QQ Bot 适配器
├── slash_commands.py         # 15+ Slash 命令
├── agent_context.py          # 对话上下文管理
├── Dockerfile                # Docker 镜像定义
├── docker-compose.yml        # 一键编排
├── xiaoda-agent.spec         # PyInstaller 打包定义
├── requirements.txt          # Python 依赖
├── .env.example              # 环境变量模板
├── .github/workflows/        # CI/CD（构建发布）
└── SETUP.md                  # 部署指南
```

***

## 快速开始

### Docker 部署（推荐）

#### 方案 A：开发用户（会 Git，推荐）

代码挂载模式，更新只需 `git pull` + 重启容器，无需重建镜像。

```bash
# 1. 首次操作
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent.git
cd xiaoda-agent
cp .env.example .env  # 编辑 .env 填写 API 密钥

# 2. 启动容器（代码挂载）
docker-compose -f docker-compose.dev.yml up -d

# 3. 后续更新（无需重建镜像）
git pull
docker-compose -f docker-compose.dev.yml restart
```

#### 方案 B：部署用户（纯镜像）

镜像部署模式，数据持久化，更新自动拉取增量镜像。

```bash
# 1. 首次操作
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent.git
cd xiaoda-agent
cp .env.example .env  # 编辑 .env 填写 API 密钥

# 2. 启动容器（镜像部署）
docker-compose -f docker-compose.prod.yml up -d

# 3. 后续更新（自动拉取增量镜像）
docker-compose -f docker-compose.prod.yml pull
docker-compose -f docker-compose.prod.yml up -d
```

访问 `http://localhost:8082` 即可使用。

### 安装包部署

从 [GitHub Releases](https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent/releases) 下载对应平台的安装包：

- **Linux**：`.run` 自解压安装器，`sudo bash xiaoda-agent-installer.run`
- **Windows**：`.exe` NSIS 安装包，双击运行

安装后通过 WebUI Setup 向导配置 API Key（支持在线验证）。

### 手动部署

详见 [SETUP.md](SETUP.md)。

### 必填 API Key

| Key | 用途 | 获取方式 |
| --- | --- | --- |
| `MIMO_API_KEY` | 主 LLM 模型（对话/工具调用） | [小米 MiMo](https://mimo.xiaomi.com) |
| `QQBOT_APP_ID` + `QQBOT_APP_SECRET` | QQ Bot | [QQ 开放平台](https://q.qq.com) |
| `EMBED_API_KEY` | 向量嵌入（bge-m3） | [SiliconFlow](https://siliconflow.cn) |

选填：`SILICONFLOW_API_KEY`（Reranker/查询变换，免费）、`AGNES_API_KEY`（AI 生成）、`WOLFRAM_ALPHA_KEY`（知识查询）等。

***

## 项目规模

| 指标         | 数值           |
| ---------- | ------------ |
| Python 模块  | 80+          |
| 生产代码       | \~18,000 行   |
| 测试代码       | \~6,000 行    |
| 内置工具       | 40+          |
| 角色人格       | 5 个          |
| 数据库表       | 21 张 + 22 索引 |
| Web API 路由 | 13 模块        |
| Web UI 视图  | 13 个         |
| RAG 优化阶段   | 3 阶段（P0-P2） |
| MCP 传输协议   | 3 种          |

***

## 关键技术决策

| 决策      | 选择                 | 理由                         |
| ------- | ------------------ | -------------------------- |
| LLM API | MiMo (小米)          | 国产模型，延迟低，成本可控，支持 TTS       |
| 免费模型    | SiliconFlow Qwen3-8B | 查询变换/记忆编码/结果压缩，零成本         |
| Reranker | bge-reranker-v2-m3 | SiliconFlow 免费 API，交叉编码器精排  |
| 数据库     | SQLite (aiosqlite) | 单设备部署，零运维，WAL 模式并发         |
| 向量检索    | sqlite-vec         | 小规模数据无需 ANN，与 SQLite 统一存储  |
| 全文检索    | FTS5 BM25          | SQLite 原生，关键词精确匹配          |
| 情感系统    | 统一枚举 Emotion       | 9 类情绪 → 贴纸/语音/显示 三路映射      |
| 工具调用    | ToolCallExtractor  | 标准 tool\_calls + DSML 统一提取 |
| 工具扩展    | MCP 协议 + 插件系统      | 外部工具无限扩展，声明式权限管理           |
| 代码沙箱    | AST 审查             | 比正则更难绕过，禁止模块/内建白名单         |
| Web 框架  | FastAPI + Vue 3    | 异步原生 + 现代前端，WebSocket 实时通信 |
| 配置管理    | ConfigService 热生效  | WebUI 改配置即时生效，无需重启         |
| 部署      | Docker + 安装包       | 一键复现，volume 持久化，跨平台安装包     |

***

## 免责声明

> 本 Agent 由纳西妲的老父亲-"飞"个人学习用途二创开发，禁止用户生成任何违禁内容，禁止用于任何商业用途，否则一切后果与开发者无关，由用户一人承担。

1. **学习研究**：本软件仅供个人学习研究使用，严禁用于任何商业用途
2. **内容责任**：用户使用本软件生成的所有内容由用户本人承担全部责任
3. **合法合规**：不得利用本软件生成违反法律法规的内容
4. **内容保证**：本软件不对生成内容的准确性、完整性作任何保证
5. **损失免责**：开发者不对用户使用本软件造成的任何直接或间接损失承担责任
6. **数据安全与隐私**：本软件在运行过程中会将用户的输入文本、对话内容、邮件数据等通过互联网传输至第三方大模型 API（如 SiliconFlow、OpenRouter、ModelScope 等）进行推理，用户应在敏感场景（如涉及个人隐私、商业机密、未成年人信息等）自行评估数据外发风险。开发者对第三方服务的隐私政策、数据留存策略、安全合规性不作任何背书或担保。
7. **AI 生成内容风险**：本软件输出的文本、代码、图片、表情等内容均由人工智能模型概率性生成，可能存在事实性错误、逻辑偏差、安全漏洞或不当内容。用户在对外引用、转发、商用、发布或用于医疗、法律、金融等高风险决策场景前，必须自行核实并由具备资质的专业人士复核。开发者不对 AI 生成内容的合法性、准确性、可用性、安全性作任何保证，由此产生的后果由用户本人承担。
8. **二创性质与第三方依赖**：本软件为个人学习用途的非官方二次创作项目，不与任何原作品权利方存在隶属或授权关系。本软件集成的第三方模型服务、MCP 服务、插件、表情素材、agently-cli 邮箱能力等组件，其服务条款、计费规则、可用性、政策变更及合规风险均由对应第三方主体负责。开发者对第三方服务的稳定性、连续性、合法性不承担任何责任，因第三方服务中断、变更、合规问题等造成的损失由用户自行承担。

***

## License

MIT
