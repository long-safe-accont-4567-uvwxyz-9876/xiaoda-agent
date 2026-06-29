# 小妲 AI Agent 部署指南

多智能体 AI 助手，以《原神》纳西妲为人格主体，下辖可莉/银狼/昔涟/尼可子代理。支持 QQ Bot、Web UI、CLI 三种交互通道。

---

## 快速开始（Docker 推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent.git
cd xiaoda-agent

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填写 API 密钥（见下方"获取 API Key"章节）

# 3. 启动服务
docker compose up -d

# 4. 访问 Web UI
# 浏览器打开 http://localhost:8080
```

---

## 获取 API Key（从小白到配置）

本项目需要若干 API Key 才能运行。以下按**必填→推荐→可选**的顺序，手把手教你获取每一个。

### 必填：MiMo API Key

MiMo 是小米的大语言模型，本项目的主 LLM 和 TTS 语音合成都依赖它。

1. 打开 [https://xiaomimimo.com](https://xiaomimimo.com)
2. 点击右上角「注册/登录」，使用手机号注册
3. 登录后进入「控制台」→「API Keys」
4. 点击「创建新密钥」，复制生成的 Key
5. 填入 `.env`：
   ```
   MIMO_API_KEY=你复制的Key
   ```

> **费用说明**：MiMo 有免费额度，日常使用成本极低（约 ¥0.01/千 token）。

### 必填：QQ 机器人凭证

QQ 机器人用于接收和发送 QQ 消息。**你需要自己注册一个 QQ 机器人，不能使用别人的 AppID 和 Secret。**

1. 打开 [QQ 开放平台](https://q.qq.com)
2. 使用你的 QQ 号登录
3. 点击「创建机器人」→ 选择「QQ 频道机器人」或「QQ 群机器人」
4. 填写机器人名称、头像等信息，提交审核
5. 审核通过后，在「开发设置」页面找到：
   - **AppID**：类似 `102012345`
   - **AppSecret**：类似 `a1b2c3d4e5f6g7h8i9j0`
6. 填入 `.env`：
   ```
   QQBOT_APP_ID=你的AppID
   QQBOT_APP_SECRET=你的AppSecret
   ```

> **注意**：QQ 机器人需要通过腾讯审核才能上线。审核期间可以在沙箱环境测试。
> 如果不想使用 QQ Bot，可以设置 `ENABLE_QQ_BOT=false`，仅使用 Web UI 和 CLI。

### 必填：管理员用户 ID

管理员拥有最高权限（执行 Shell 命令、修改配置等）。

1. 启动机器人后，在 QQ 中给机器人发送任意消息
2. 查看日志（`docker compose logs -f agent` 或 `tail -f logs/agent.log`）
3. 日志中会出现类似 `user_openid=ABCDEFG123` 的信息
4. 将 OpenID 填入 `.env`：
   ```
   OWNER_IDS=ABCDEFG123
   ```
5. 多个管理员用逗号分隔：`OWNER_IDS=ABCDEFG123,HIJKLMN456`

> **首次部署提示**：可以先用空的 `OWNER_IDS=` 启动，从日志获取 OpenID 后再填写并重启。

### 推荐：Embedding 向量检索（记忆系统）

Embedding 用于情景记忆的语义检索，让 AI 能记住并回忆之前的对话。**不配置则记忆系统降级为关键词匹配。**

推荐使用 SiliconFlow（国内服务，有免费额度）：

1. 打开 [https://siliconflow.cn](https://siliconflow.cn)
2. 注册账号（支持手机号/邮箱）
3. 进入「API Keys」页面，点击「创建新密钥」
4. 复制 Key，填入 `.env`：
   ```
   EMBED_API_KEY=你复制的Key
   EMBED_BASE_URL=https://api.siliconflow.cn/v1
   EMBED_MODEL=BAAI/bge-large-zh-v1.5
   ```

> **替代方案**：任何 OpenAI 兼容的 Embedding API 都可以使用，修改 `EMBED_BASE_URL` 和 `EMBED_MODEL` 即可。

### 可选：其他 API Key

| API | 用途 | 获取地址 | 不配置的影响 |
|-----|------|---------|------------|
| **Agnes AI** | 图像/视频生成 | [https://agnes-ai.com](https://agnes-ai.com) → 注册 → API Keys | 无法生成图片和视频 |
| **Tavily** | AI 搜索引擎 | [https://tavily.com](https://tavily.com) → 注册 → API Keys | 搜索功能降级为 DuckDuckGo |
| **ImgBB** | 图片上传 | [https://api.imgbb.com](https://api.imgbb.com) → 注册 → API Key | 无法上传图片到外链 |
| **WolframAlpha** | 知识计算 | [https://products.wolframalpha.com/api/](https://products.wolframalpha.com/api/) → Get AppID | 无法使用精确数学/科学计算 |
| **SiliconFlow** | 备用 LLM | [https://siliconflow.cn](https://siliconflow.cn) → API Keys | 无影响（MiMo 为主） |
| **OpenRouter** | 备用 LLM | [https://openrouter.ai](https://openrouter.ai) → API Keys | 无影响（MiMo 为主） |
| **DeepSeek** | 备用 LLM | [https://platform.deepseek.com](https://platform.deepseek.com) → API Keys | 无影响（MiMo 为主） |
| **GitHub Token** | GitHub MCP | [https://github.com/settings/tokens](https://github.com/settings/tokens) → Generate new token (classic)，勾选 `repo, read:org` | 无法使用 GitHub MCP 工具 |

---

## 完整 .env 配置参考

```bash
cp .env.example .env
```

`.env.example` 中每个字段都有注释说明，包括获取方式。必填项为：

| 变量 | 说明 | 示例 |
|------|------|------|
| `MIMO_API_KEY` | MiMo API 密钥 | `sk-abc123...` |
| `QQBOT_APP_ID` | QQ 机器人 AppID | `102012345` |
| `QQBOT_APP_SECRET` | QQ 机器人 Secret | `a1b2c3d4...` |
| `OWNER_IDS` | 管理员 OpenID | `ABCDEFG123` |

其余均为可选项，不配置不影响基本功能。

---

## 部署方式

### 方式一：Docker 部署（推荐）

**前置要求**：安装 [Docker](https://docs.docker.com/get-docker/) 和 [Docker Compose](https://docs.docker.com/compose/install/)

```bash
# 构建并启动
docker compose up -d

# 查看日志
docker compose logs -f agent

# 停止服务
docker compose down

# 重新构建（代码更新后）
docker compose up -d --build
```

**数据持久化**：数据库、日志、凭证等存储在 Docker volume `agent-data` 中，删除容器不会丢失数据。

**端口配置**：默认 8080，可通过 `.env` 中的 `WEBUI_PORT` 修改。

### 方式二：手动部署

**前置要求**：
- Python 3.11+
- ffmpeg（音频转码，`sudo apt install ffmpeg`）

```bash
# 1. 克隆仓库
git clone https://github.com/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent.git
cd xiaoda-agent

# 2. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填写 API 密钥

# 5a. 启动 Web UI 模式（推荐）
python agent.py --web 8080

# 5b. 启动 CLI 模式（开发调试）
python cli.py

# 5c. 启动 QQ Bot 模式（生产环境）
python qq_bot_adapter.py
```

### 方式三：systemd 服务（Linux 生产环境）

```bash
# 复制并编辑服务文件
cp deploy/qq-agent.service /etc/systemd/system/
# 编辑其中的路径和用户名
sudo systemctl daemon-reload
sudo systemctl enable qq-agent
sudo systemctl start qq-agent

# 查看状态
sudo systemctl status qq-agent
```

---

## 功能模块与 API Key 对应关系

不同功能需要不同的 API Key。以下帮你按需配置：

| 你想要的功能 | 需要的 API Key | 必填程度 |
|------------|--------------|---------|
| 基本对话 | `MIMO_API_KEY` | 必填 |
| QQ 消息收发 | `QQBOT_APP_ID` + `QQBOT_APP_SECRET` | 必填（或关闭 QQ Bot） |
| AI 记忆（语义检索） | `EMBED_API_KEY` | 推荐 |
| TTS 语音合成 | `MIMO_API_KEY`（同一个） | 已包含 |
| AI 生成图片/视频 | `AGNES_API_KEY` | 可选 |
| 网络搜索 | 无（DuckDuckGo 免费） | 默认可用 |
| 更好的搜索 | `TAVILY_API_KEY` | 可选 |
| 数学/科学计算 | `WOLFRAMALPHA_API_KEY` | 可选 |
| 图片上传 | `IMGBB_API_KEY` | 可选 |
| GitHub 操作 | `GITHUB_PERSONAL_ACCESS_TOKEN` | 可选 |
| 硬件控制（GPIO/I2C） | 无需 API Key | 需 ARM 设备 |

---

## 数据库

项目使用 SQLite，首次启动自动创建。Schema 定义在 `database.py` 中，独立文档见 `db/schema.sql`。

### 手动初始化（可选）

```bash
mkdir -p data/db
sqlite3 data/db/agent.db < db/schema.sql
```

### 数据库位置

| 部署方式 | 路径 |
|---------|------|
| Docker | `/data/db/agent.db`（volume 持久化） |
| 手动部署 | `./data/db/agent.db` 或 `$KIOXIA_DATA_DIR/db/agent.db` |

---

## 前端开发

Web UI 使用 Vue 3 + Naive UI + Vite 构建。预构建产物已在 `web/dist/` 中，无需额外构建即可使用。

如需修改前端：

```bash
cd web/frontend
npm install
npm run dev    # 开发服务器（热更新）
npm run build  # 构建到 web/dist/
```

---

## 硬件支持（可选）

在 ARM 设备（如 Orange Pi 5 Pro）上，支持以下硬件交互：

| 功能 | 工具 | 要求 |
|------|------|------|
| GPIO 控制 | `gpio_control` | 映射 `/sys/class/gpio` |
| PWM 输出 | `pwm_control` | 映射 `/sys/class/pwm` |
| I2C 通信 | `hardware_status` | 映射 `/dev/i2c-*` |
| 摄像头 | `capture_photo` | 映射 `/dev/video0` |
| 温度/电压监控 | `hardware_status` | 自动检测 |
| NPU 视觉识别 | YOLOv5 | RK3588/RK3588S + `ENABLE_NPU=true` |

Docker 部署时需在 `docker-compose.yml` 中取消 `devices` 注释并映射对应设备节点。

---

## 常见问题

### 启动相关

**Q: 启动报 `No API token found`**
A: 检查 `.env` 文件是否正确填写了 `MIMO_API_KEY`，确认 `.env` 文件在项目根目录。

**Q: 启动报 `QQBOT_APP_ID is not set`**
A: 如果不需要 QQ Bot，在 `.env` 中设置 `ENABLE_QQ_BOT=false`。如果需要，按上方指引注册 QQ 机器人。

**Q: Web UI 无法访问**
A: 检查 `WEBUI_HOST` 是否为 `0.0.0.0`，防火墙是否放行 `WEBUI_PORT`。

**Q: Docker 构建失败**
A: 确认 Docker 版本 >= 20.10，Docker Compose 版本 >= 2.0。

### 功能相关

**Q: AI 不记得之前的对话**
A: 需要配置 `EMBED_API_KEY` 启用语义向量检索。不配置时记忆系统降级为关键词匹配。

**Q: QQ Bot 连接失败**
A: 确认 `QQBOT_APP_ID` 和 `QQBOT_APP_SECRET` 正确，且 QQ 机器人已通过审核。可在 QQ 开放平台查看审核状态。

**Q: TTS 语音消息不工作**
A: TTS 使用 MiMo API，确认 `MIMO_API_KEY` 有效。Docker 部署需要容器内安装 ffmpeg（Dockerfile 已包含）。

**Q: 搜索功能报错**
A: 默认使用 DuckDuckGo（免费，无需 API Key）。如需更好的搜索质量，配置 `TAVILY_API_KEY`。

### 数据相关

**Q: 数据库在哪里**
A: Docker 部署：`/data/db/agent.db`（volume 持久化）；裸机部署：`./data/db/agent.db`。

**Q: 如何备份数据**
A: Docker：`docker compose exec agent sqlite3 /data/db/agent.db ".backup /data/db/backup.db"`；裸机：直接复制 `data/db/agent.db` 文件。

**Q: 如何重置数据库**
A: 删除 `agent.db` 文件后重启，会自动重建。
