# 纳西妲 AI Agent 部署指南

多智能体 AI 助手，以《原神》纳西妲为人格主体，下辖可莉/银狼/昔涟/尼可子代理。支持 QQ Bot、Web UI、CLI 三种交互通道。

## 快速开始（Docker 推荐）

### 1. 克隆仓库

```bash
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/nahida-agent.git
cd nahida-agent
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，至少填写以下必填项：

| 变量 | 说明 |
|------|------|
| `MIMO_API_KEY` | MiMo API 密钥（主 LLM） |
| `QQBOT_APP_ID` | QQ 机器人 AppID |
| `QQBOT_APP_SECRET` | QQ 机器人 Secret |
| `OWNER_IDS` | 管理员用户 ID（逗号分隔） |

### 3. 启动服务

```bash
docker compose up -d
```

访问 `http://localhost:8080` 即可使用 Web UI。

### 4. 查看日志

```bash
docker compose logs -f agent
```

## 手动部署

### 前置要求

- Python 3.11+
- ffmpeg（音频转码）
- Node.js 18+（仅前端开发时需要）

### 步骤

```bash
# 1. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env 填写 API 密钥

# 4. 启动 Web UI 模式
python agent.py --web 8080

# 或启动 CLI 模式
python cli.py

# 或启动 QQ Bot 模式
python qq_bot_adapter.py
```

### systemd 服务（Linux）

```bash
# 复制并编辑服务文件
cp deploy/qq-agent.service /etc/systemd/system/
# 编辑路径和用户名
sudo systemctl daemon-reload
sudo systemctl enable qq-agent
sudo systemctl start qq-agent
```

## 环境变量说明

### 必填

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MIMO_API_KEY` | MiMo API 密钥 | - |
| `QQBOT_APP_ID` | QQ 机器人 AppID | - |
| `QQBOT_APP_SECRET` | QQ 机器人 Secret | - |
| `OWNER_IDS` | 管理员用户 ID | - |

### 可选

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `KIOXIA_DATA_DIR` | 数据根目录 | `/data` (Docker) / `./data` (裸机) |
| `WEBUI_PORT` | Web UI 端口 | `8080` |
| `WEBUI_HOST` | Web UI 监听地址 | `0.0.0.0` |
| `WEBUI_PASSWORD` | Web UI 登录密码 | 空（无需密码） |
| `ENABLE_QQ_BOT` | 是否启用 QQ Bot | `true` |
| `ENABLE_NPU` | 启用 NPU 推理（仅 RK3588） | `false` |
| `EMBED_API_KEY` | Embedding API 密钥 | - |
| `EMBED_BASE_URL` | Embedding API 地址 | - |
| `EMBED_MODEL` | Embedding 模型名 | - |
| `AGNES_API_KEY` | Agnes AI 密钥（图像/视频） | - |
| `TAVILY_API_KEY` | Tavily 搜索密钥 | - |
| `IMGBB_API_KEY` | ImgBB 图片上传密钥 | - |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | GitHub MCP Token | - |
| `NUDGE_ENABLED` | 主动问候开关 | `true` |
| `NUDGE_TIMEZONE` | 问候时区 | `Asia/Shanghai` |

完整列表见 `.env.example`。

## 数据库

项目使用 SQLite，首次启动自动创建。Schema 定义在 `database.py` 中，独立文档见 `db/schema.sql`。

### 手动初始化（可选）

```bash
mkdir -p data/db
sqlite3 data/db/agent.db < db/schema.sql
```

## 前端开发

Web UI 使用 Vue 3 + Naive UI + Vite 构建。预构建产物已在 `web/dist/` 中，无需额外构建即可使用。

如需修改前端：

```bash
cd web/frontend
npm install
npm run dev    # 开发服务器
npm run build  # 构建到 web/dist/
```

## 硬件支持（可选）

在 ARM 设备（如 Orange Pi）上，支持以下硬件交互：

| 功能 | 工具 | 要求 |
|------|------|------|
| GPIO 控制 | `gpio_control` | 映射 `/sys/class/gpio` |
| PWM 输出 | `pwm_control` | 映射 `/sys/class/pwm` |
| I2C 通信 | `hardware_status` | 映射 `/dev/i2c-*` |
| 摄像头 | `capture_photo` | 映射 `/dev/video0` |
| 温度/电压监控 | `hardware_status` | 自动检测 |

Docker 部署时需在 `docker-compose.yml` 中取消 `devices` 注释。

## 项目结构

```
├── agent.py              # 主入口（Web/CLI 模式）
├── agent_core.py         # AgentCore 核心引擎
├── agent_dispatcher.py   # 子代理调度器
├── cli.py                # CLI 交互界面
├── cli_client.py         # WebSocket CLI 客户端
├── config.py             # 配置与路径管理
├── database.py           # 数据库管理
├── model_router.py       # 模型路由
├── qq_bot_adapter.py     # QQ Bot 适配器
├── tts_engine.py         # TTS 语音合成
├── sticker_manager.py    # 表情包管理
├── emotion_enum.py       # 情感枚举系统
├── core/                 # AgentCore 子模块
│   ├── bootstrap.py      # 启动引导
│   ├── router_engine.py  # 路由引擎
│   ├── chat_processor.py # 对话处理
│   ├── tool_orchestrator.py # 工具编排
│   └── delegation.py     # 委托机制
├── tools/                # 工具集
│   ├── hardware_tools.py # 硬件控制
│   ├── memory_tool.py    # 记忆管理
│   ├── code_tools_v2.py  # 代码执行
│   ├── agnes_tools.py    # AI 生成
│   └── ...
├── web/                  # Web UI
│   ├── server.py         # FastAPI 服务
│   ├── ws_hub.py         # WebSocket 中心
│   ├── routers/          # API 路由
│   ├── frontend/         # Vue 3 前端源码
│   └── dist/             # 前端构建产物
├── db/
│   └── schema.sql        # 数据库 Schema
├── deploy/
│   └── qq-agent.service  # systemd 服务参考
├── docs/
│   ├── IMPROVEMENT_PLAN.md # 改进计划
│   └── WEBUI_DESIGN.md    # Web UI 设计文档
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 常见问题

**Q: 启动报 `No API token found`**
A: 检查 `.env` 文件是否正确填写了 `MIMO_API_KEY`。

**Q: Web UI 无法访问**
A: 检查 `WEBUI_HOST` 是否为 `0.0.0.0`，防火墙是否放行 `WEBUI_PORT`。

**Q: QQ Bot 连接失败**
A: 确认 `QQBOT_APP_ID` 和 `QQBOT_APP_SECRET` 正确，且 QQ 机器人已通过审核。

**Q: 容器内硬件工具不可用**
A: 在 `docker-compose.yml` 中取消 `devices` 注释，映射对应设备节点。

**Q: 数据库在哪里**
A: Docker 部署：`/data/db/agent.db`（volume 持久化）；裸机部署：`./data/db/agent.db` 或 `$KIOXIA_DATA_DIR/db/agent.db`。
