# Nahida Agent 🌿

基于大语言模型的多智能体 QQ 聊天机器人，运行在 Orange Pi 5 Pro 上，搭载 NPU 加速推理。

以《原神》角色「纳西妲」为核心人格，支持多角色切换、工具调用、知识图谱、记忆系统、NPU 视觉识别等能力。

## ✨ 核心特性

### 🎭 多智能体系统
- **5 个角色人格**：纳西妲（主人格）、可莉、银狼、昔涟、尼可
- **图编排引擎**：类 LangGraph 的 TaskGraph，支持条件路由、并行执行、结果综合
- **角色间协作**：子智能体可向主人格请求协助，主人格综合多角色回复

### 🧠 认知系统
- **情景记忆**：向量检索 + 语义编码，自动在空闲时整理记忆
- **知识图谱**：从对话中提取实体-关系三元组，构建用户知识网络
- **用户画像**：自动建立和维护用户性格画像，冷启动友好
- **学习系统**：从对话模式中提取规则，自我改进
- **笔记本**：自动笔记、任务跟踪、关注点管理

### 🔧 工具调用（40+ 工具）
- **文件操作**：列出、读取、写入、搜索文件
- **代码执行**：Python 沙箱执行
- **网络搜索**：多引擎搜索（Bing/Baidu/Google）
- **网页浏览**：抓取和提取网页内容
- **系统管理**：Shell 命令、Docker、systemd 服务
- **硬件控制**：GPIO、I2C、SPI、传感器、LED、舵机（Orange Pi）
- **视觉识别**：摄像头捕获 + NPU 加速 YOLOv5 目标检测
- **文档阅读**：PDF/DOCX/Excel 解析
- **天气查询**、**计算器**、**Wolfram 知识计算**

### 🛡️ 可靠性设计
- **工具调用修复**：自动修复 LLM 输出的畸形 JSON 参数
- **AI 痕迹去除**：`humanize()` 函数剥离 LLM 的机器感表达
- **智能降级**：子系统故障时优雅降级，不中断对话
- **风暴检测**：防止工具调用循环
- **智能错误处理**：自修复建议 + 错误学习

### 🗣️ 多模态输出
- **TTS 语音合成**：角色专属音色（SiliconFlow API）
- **表情贴纸**：基于情绪自动选择
- **戳一戳响应**：上下文感知的戳一戳回复

### 📊 运维能力
- **API 成本追踪**：按任务类型统计，`/cost` 命令查看
- **Slash 命令**：15+ 管理命令（/status, /model, /hw, /cam 等）
- **结构化日志**：Loguru JSON 日志，30 天轮转
- **健康检查脚本**：`healthcheck.sh`

## 🏗️ 架构

```
QQ 消息 → qq_bot_adapter.py (botpy SDK)
    → AgentCore.process()
        → SecurityFilter (访问控制 + 内容过滤)
        → SlashCommandHandler (/命令处理)
        → TaskGraph (多智能体图编排)
            → RouterNode (规则 + LLM 路由)
            → ParallelAgentNode (并行子智能体)
            → SynthesisNode (结果综合)
        → ModelRouter (LLM API 路由 + 成本追踪)
            → ToolCallHandler → ToolExecutor → ToolRegistry
        → 后台任务 (记忆编码 / 画像更新 / 学习 / 笔记)
    → 回复 (文本 + 情绪 + 贴纸 + 语音)
```

## 📁 项目结构

```
ai-agent/
├── agent_core.py          # 核心编排器
├── agent_context.py       # 对话上下文 + Prompt 构建
├── agent_dispatcher.py    # 子智能体管理
├── task_orchestrator.py   # 图编排引擎
├── model_router.py        # LLM API 路由 + 成本追踪
├── tool_call_handler.py   # 工具调用处理
├── tool_executor.py       # 工具执行器
├── tool_registry.py       # 工具注册表
├── tool_repair.py         # 工具调用修复
├── knowledge_graph.py     # 知识图谱
├── portrait_manager.py    # 用户画像
├── memory_manager.py      # 情景记忆
├── learning_manager.py    # 学习系统
├── notebook_manager.py    # 笔记本
├── vector_store.py        # 向量存储 + 语义检索
├── npu_inference.py       # NPU 推理 (YOLOv5)
├── vision_service.py      # 视觉服务
├── tts_engine.py          # TTS 语音合成
├── emotion_simple.py      # 情绪检测
├── sticker_manager.py     # 贴纸管理
├── nudge_engine.py        # 戳一戳引擎
├── security.py            # 安全过滤
├── text_utils.py          # 文本处理 (去AI化/截断/分割)
├── result_wrapper.py      # 结果包装 (角色语音化)
├── smart_error_handler.py # 智能错误处理
├── slash_commands.py      # Slash 命令
├── qq_bot_adapter.py      # QQ Bot 适配器
├── klee_agent.py          # 可莉子智能体
├── config.py              # 配置中心
├── database.py            # 数据库管理
├── db_*.py                # 数据库子模块 (memory/knowledge/analytics/learning/notebook)
├── tools/                 # 工具模块
│   ├── system_tools.py    # Shell/进程/Docker/服务
│   ├── hardware_tools.py  # GPIO/I2C/SPI/传感器
│   ├── web_tools_v2.py    # HTTP/API
│   ├── web_browse_tools.py# 网页浏览
│   ├── multi_search_tools.py # 多引擎搜索
│   ├── file_tools_v2.py   # 文件操作
│   ├── code_tools_v2.py   # Python 执行
│   ├── document_tools.py  # 文档阅读
│   └── vision_tools.py    # 摄像头工具
├── web/app.py             # FastAPI Web 接口
├── cli.py                 # CLI 交互模式
└── *_personality.md       # 角色人格 Prompt
```

## 🚀 安装与运行

### 环境要求
- Python 3.10+
- Orange Pi 5 Pro (或其他 ARM SBC，NPU 功能需要 RK3588S)
- 外接存储（可选，用于数据和模型）

### 安装

```bash
cd ~/ai-agent
pip install -r requirements.txt
```

### 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
# 编辑 .env 填入 API 密钥
```

### 运行

QQ Bot 模式（生产环境）：
```bash
bash start_qqbot.sh
```

CLI 交互模式（开发调试）：
```bash
python cli.py
```

Web 模式：
```bash
python web/app.py
```

## 📊 项目规模

| 指标 | 数值 |
|------|------|
| 生产代码 | ~11,800 行 |
| 测试代码 | ~6,000 行 |
| Python 模块 | 42 个 |
| 工具模块 | 9 个 (40+ 工具) |
| 角色人格 | 5 个 |
| 数据库表 | 6 个 |

## 🔑 关键技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM API | MiMo (小米) | 国产模型，延迟低，成本可控 |
| 数据库 | SQLite (aiosqlite) | 单设备部署，零运维 |
| 向量检索 | 余弦相似度 + SQLite | 小规模数据无需 ANN 索引 |
| NPU 推理 | VIP Lite SDK (ctypes) | 原生 NPU 加速，YOLOv5 实时检测 |
| 日志 | Loguru (JSON) | 结构化日志，便于分析 |
| QQ SDK | botpy (monkey-patched) | 官方 SDK 功能不足，需补丁 |

## ⚠️ 已知限制

- **安全沙箱**：代码执行和 Shell 命令未做完整沙箱隔离，建议在受信环境运行
- **单用户上下文**：多用户共享对话上下文，暂无用户隔离
- **SSL 验证**：QQ Bot SDK 的 SSL 证书问题通过禁用验证绕过（待修复）
- **测试框架**：测试脚本未使用 pytest，无 CI/CD 集成
- **数据库迁移**：无 Schema 版本管理，变更需手动处理

## 📄 License

MIT
