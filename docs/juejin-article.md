# 🌿 Nahida Agent：运行在边缘设备上的多智能体 AI 助手，40+ 工具赋能，认知闭环系统

> 不是套壳 ChatGPT，而是一个完整的认知智能体——能记住、能学习、能感知情绪、能调用工具、能自我改进

## 前言

市面上有大量 AI Chatbot 项目，但大多数只是简单的 API 调用封装。今天给大家介绍一个不一样的项目——**Nahida Agent**，一个运行在 Orange Pi 边缘设备上的多智能体 AI 助手。

以《原神》纳西妲为灵魂，拥有 5 个独立角色人格、40+ 工具链、认知闭环系统，支持 QQ Bot + Web UI + CLI 三通道交互。

**项目地址**：[https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent](https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent)

---

## 项目亮点

| 对比维度 | 通用 Chatbot | Nahida Agent |
|---------|-------------|--------------|
| 记忆 | 无状态 / 简单上下文 | 情景记忆 + 向量检索 + 知识图谱 + 用户画像 |
| 学习 | 不会从对话中学习 | 自动提取规则、发现模式、自我改进 |
| 情绪 | 无 | 9 类情绪检测 → 贴纸 + 语音风格联动 |
| 工具 | 少量 API 调用 | 40+ 工具，含硬件控制（GPIO/I2C/PWM） |
| 多智能体 | 单一模型 | 5 角色人格 + 图编排 + 委托机制 |
| 部署 | 云端依赖 | 边缘设备运行，Docker 一键部署 |
| 交互 | 单一通道 | QQ Bot + Web UI + CLI 三通道 |

---

## 核心功能

### 🎭 多智能体人格系统

5 个独立角色人格，各有专属音色、贴纸集和人格 Prompt：

| 角色 | 定位 | 特色 |
|------|------|------|
| **纳西妲** | 主人格 / 调度者 | 温柔智慧，负责路由和综合 |
| **可莉** | 玩伴 | 活泼可爱，擅长聊天和游戏 |
| **银狼** | 编程专家 | 技术导向，擅长代码和系统管理 |
| **昔涟** | 知性助手 | 冷静理性，擅长分析和文档 |
| **尼可** | 创意伙伴 | 灵感丰富，擅长图像和视频生成 |

**独到之处**：
- **图编排引擎**：类 LangGraph 的 TaskGraph，支持条件路由、并行执行、结果综合
- **委托机制**：子智能体可向主人格请求协助，深度限制防递归爆炸
- **ToolCallExtractor 统一接口**：标准 tool_calls 和 DSML 文本标记统一提取

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

**核心实现**：

```python
# 记忆双存储：结构化记忆（SQLite）+ 语义向量（sqlite-vec）
class MemoryManager:
    async def recall(self, query: str, limit: int = 5) -> list[Memory]:
        # 1. 向量检索语义相关记忆
        vector_results = await self.vector_store.search(query, limit)
        # 2. SQLite 检索结构化记忆
        sql_results = await self.db.search_memories(query, limit)
        # 3. 双路召回合并
        return self.merge_results(vector_results, sql_results)
```

### 🔧 40+ 工具链

| 类别 | 工具 | 亮点 |
|------|------|------|
| **文件操作** | 列出/读取/写入/搜索文件 | 智能路径解析，安全沙箱 |
| **代码执行** | Python 沙箱 | AST 审查 + 白名单内建函数 |
| **网络搜索** | 多引擎搜索（Bing/Baidu/Google） | 自动降级，引擎不可用时切换 |
| **网页浏览** | 抓取和提取网页内容 | SSRF 防护（DNS 预检 + 内网 IP 过滤） |
| **系统管理** | Shell 命令 / Docker / systemd | 权限分级（DEFAULT/DEV/STRICT/BYPASS） |
| **硬件控制** | GPIO / I2C / PWM / 传感器 | 边缘设备专属，容器内优雅降级 |
| **AI 生成** | 图像 / 视频 / TTS 语音 | 速率限制 + 缓存 |
| **文档阅读** | PDF / DOCX / Excel / PPT | 多格式解析，智能截断 |
| **视觉识别** | 摄像头 + YOLOv5 | NPU 加速（RK3588S），实时目标检测 |

**工具护栏示例**：

```python
# 工具调用频率限制 + 风暴检测
class ToolGuardrails:
    def __init__(self):
        self.rate_limiter = RateLimiter(max_calls=10, window=60)
        self.storm_detector = StormDetector(threshold=5, interval=10)
    
    async def check(self, tool_name: str, args: dict) -> GuardResult:
        if not self.rate_limiter.allow(tool_name):
            return GuardResult(blocked=True, reason="频率限制")
        if self.storm_detector.is_storm(tool_name):
            return GuardResult(blocked=True, reason="工具风暴检测")
        return GuardResult(blocked=False)
```

### 🛡️ 安全与可靠性

| 机制 | 说明 |
|------|------|
| **权限分级** | DEFAULT / DEV / STRICT / BYPASS |
| **沙箱配置** | 默认阻止内网 IP、限制端口、限制文件访问路径 |
| **AST 代码审查** | 禁止 `__import__`/`eval`/`exec`/`open` 等 |
| **工具护栏** | 频率限制 + 风暴检测 + JSON 参数修复 |
| **SSRF 防护** | DNS 预检 + HTTP 层实际 IP 验证 |
| **委托深度限制** | 最大 2 层委托，防止递归爆炸 |

### 🌐 三通道交互

| 通道 | 入口 | 特点 |
|------|------|------|
| **QQ Bot** | `qq_bot_adapter.py` | 生产级，消息去重 + 分段发送 + SILK 语音 |
| **Web UI** | `web/server.py` | FastAPI + Vue 3 + WebSocket，须弥主题 |
| **CLI** | `cli.py` | 打字机效果 + readline 历史 |

**Web UI 特色**：
- 须弥主题（草元素配色 + 粒子特效 + 3D 卡片交互）
- 12 个功能视图：Chat / Agents / Models / Tools / MCP / Insight / Schedule / Media / Health / Dashboard / Settings / Login
- WebSocket 实时推送（工具调用状态 / 情绪变化 / 健康检查）
- Agent 独立壁纸系统

---

## 技术架构

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
    └────────────────────┘  └──────────────────────┘  └──────────────────────┘
```

### 核心模块拆分

AgentCore 从 1431 行 God Class 拆分为 5 个子模块：

| 模块 | 职责 |
|------|------|
| `core/bootstrap.py` | 启动引导，依赖注入 |
| `core/router_engine.py` | 统一路由决策（RoutingDecision 数据类） |
| `core/chat_processor.py` | 单轮对话主流程 |
| `core/tool_orchestrator.py` | 工具调用编排 |
| `core/background_tasks.py` | 后台任务队列（记忆/画像/学习/笔记） |

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

---

## 快速开始

### Docker 一键部署（推荐）

```bash
# 克隆项目
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent.git
cd nahida-agent

# 配置环境变量
cp .env.example .env
# 编辑 .env 填写 API 密钥

# 启动服务
docker compose up -d
```

访问 `http://localhost:8080` 即可使用 Web UI。

### 手动部署

```bash
# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env

# 启动 Web UI
python agent.py --web

# 或启动 CLI
python agent.py --cli
```

---

## 项目规模

| 指标 | 数值 |
|------|------|
| Python 模块 | 60+ |
| 生产代码 | ~15,000 行 |
| 测试代码 | ~6,000 行 |
| 工具数量 | 40+ |
| 角色人格 | 5 个 |
| 数据库表 | 21 张 + 22 索引 |
| Web API 路由 | 12 模块 |
| Web UI 视图 | 12 个 |

---

## 关键技术决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM API | MiMo (小米) | 国产模型，延迟低，成本可控，支持 TTS |
| 数据库 | SQLite (aiosqlite) | 单设备部署，零运维，WAL 模式并发 |
| 向量检索 | sqlite-vec | 小规模数据无需 ANN，与 SQLite 统一存储 |
| 情感系统 | 统一枚举 Emotion | 9 类情绪 → 贴纸/语音/显示 三路映射 |
| 工具调用 | ToolCallExtractor | 标准 tool_calls + DSML 统一提取 |
| 代码沙箱 | AST 审查 | 比正则更难绕过，禁止模块/内建白名单 |
| Web 框架 | FastAPI + Vue 3 | 异步原生 + 现代前端，WebSocket 实时通信 |
| 部署 | Docker | 一键复现，volume 持久化，硬件可选直通 |

---

## 实际应用场景

### 1. 智能家居控制

```python
# 通过硬件工具控制 GPIO
await agent.execute_tool("gpio_write", {
    "pin": 18,
    "value": 1,
    "description": "打开客厅灯"
})
```

### 2. 代码助手

```python
# 银狼（编程专家）帮你调试代码
await agent.delegate_task("yinlang", "帮我看看这段代码有什么问题")
```

### 3. 系统巡检

```python
# 纳西妲的「巡视领地」功能
巡检报告 = await agent.execute_tool("system_patrol", {})
# 输出：设备身份、资源状况、内存大户 TOP 5、网络状况、健康总结
```

### 4. 情感陪伴

```python
# 情绪检测 + 贴纸 + 语音联动
用户: "今天好累啊..."
纳西妲: [检测到 SAD 情绪] 
       → 选择安慰贴纸
       → TTS 合成温柔语音
       → "辛苦了，要不要听我讲个故事放松一下？"
```

---

## Web UI 预览

Web UI 采用须弥主题设计，草元素配色 + 粒子特效 + 3D 卡片交互：

- **Chat 视图**：实时对话，支持 Markdown 渲染、代码高亮
- **Agents 视图**：查看和管理 5 个角色人格
- **Tools 视图**：浏览 40+ 工具及其使用说明
- **Insight 视图**：查看学习系统提取的洞察
- **Dashboard 视图**：系统健康监控和指标统计

---

## 总结

Nahida Agent 不只是一个 AI Chatbot，而是一个完整的认知智能体系统：

1. **多智能体架构**：5 个角色人格，图编排引擎，委托机制
2. **认知闭环**：感知→记忆→学习→改进，真正的自我进化
3. **40+ 工具链**：从文件操作到硬件控制，覆盖各种场景
4. **安全可靠**：AST 沙箱、SSRF 防护、工具护栏、权限分级
5. **边缘部署**：运行在 Orange Pi 上，Docker 一键部署

如果你对 AI Agent、多智能体系统、边缘计算感兴趣，欢迎 Star 和贡献！

**项目地址**：[https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent](https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent)

---

## 标签

`AI Agent` `多智能体` `边缘计算` `原神` `纳西妲` `Python` `FastAPI` `Vue 3` `Docker` `认知系统` `工具链` `QQ Bot`
