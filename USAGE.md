# 纳西妲 AI Agent 使用说明

## 目录

- [1. 环境准备](#1-环境准备)
- [2. 安装步骤](#2-安装步骤)
- [3. 首次配置（新手引导）](#3-首次配置新手引导)
- [4. 启动方式](#4-启动方式)
- [5. CLI 交互界面](#5-cli-交互界面)
- [6. 命令参考](#6-命令参考)
- [7. QQ Bot 模式](#7-qq-bot-模式)
- [8. 多 Agent 系统](#8-多-agent-系统)
- [9. 配置项详解](#9-配置项详解)
- [10. 常见问题](#10-常见问题)

---

## 1. 环境准备

### 硬件要求

| 项目 | 最低要求 | 推荐配置 |
|------|---------|--------|
| CPU | ARMv8 双核 | ARMv8 八核（如全志 T507 big.LITTLE） |
| 内存 | 2 GB | 4 GB+ |
| 存储 | 8 GB SD 卡 | 32 GB+ SD 卡 + USB 外挂存储 |
| 摄像头 | 无 | USB 摄像头（用于视觉功能） |

### 软件要求

- **操作系统**: Debian 12 (bookworm) 或 Ubuntu 22.04+ ARM64
- **Python**: 3.10 或更高版本
- **网络**: 需要访问 `api.xiaomimimo.com`（MiMo API）

### 必需的 API 密钥

| 密钥 | 用途 | 获取方式 |
|------|------|--------|
| MIMO_API_KEY | MiMo 大模型对话（必需） | 访问 [xiaomimimo.com](https://xiaomimimo.com) 注册获取 |

### 可选的 API 密钥

| 密钥 | 用途 | 获取方式 |
|------|------|--------|
| QQBOT_APP_ID | QQ 机器人 | [QQ 开放平台](https://q.qq.com) |
| QQBOT_APP_SECRET | QQ 机器人 | 同上 |
| OWNER_IDS | 主人权限标识 | QQ 机器人的用户 OpenID |
| EMBED_API_KEY | 向量嵌入（记忆检索） | 任意 OpenAI 兼容嵌入 API |
| IMGBB_API_KEY | 图片上传 | [imgbb.com](https://api.imgbb.com/) |
| TAVILY_API_KEY | Tavily 搜索 | [tavily.com](https://tavily.com) |
| SILICONFLOW_API_KEY | SiliconFlow 子 Agent | [siliconflow.cn](https://siliconflow.cn) |
| OPENROUTER_API_KEY | OpenRouter 子 Agent | [openrouter.ai](https://openrouter.ai) |

---

## 2. 安装步骤

### 2.1 克隆项目

```bash
git clone <repo-url> nahida-agent
cd nahida-agent
```

### 2.2 安装 Python 依赖

```bash
pip install -r requirements.txt
```

依赖列表包含 19 个包：

| 类别 | 包名 |
|------|------|
| 核心 | python-dotenv, loguru, openai, aiosqlite, pydantic |
| QQ Bot | qq-botpy |
| 搜索 | primp, lxml, tavily-python |
| 文档 | pdfplumber, python-docx, python-pptx, openpyxl, html2text |
| 视觉 | numpy, opencv-python-headless |
| 向量 | sqlite-vec |
| 硬件 | smbus2 |
| Web UI | streamlit |

> 如果在 ARM 平台上安装 `opencv-python-headless` 遇到问题，可尝试 `pip install opencv-python-headless --no-binary :all:` 或从源码编译。

### 2.3 配置环境变量

有两种方式：

**方式一：交互式配置向导（推荐）**

```bash
python3 setup_wizard.py
```

向导会逐步引导你输入所有必需和可选的 API 密钥。

**方式二：手动编辑**

```bash
cp .env.example .env
nano .env
```

---

## 3. 首次配置（新手引导）

运行 `python3 setup_wizard.py` 会启动纳西妲主题的配置向导：

```
  ✿  世  界  的  记  忆  ，  由  我  来  守  护  ✿

  ✿  NAHIDA  ✿

  🌿  纳 西 妲 配 置 向 导  🌿

  +------------------------------------------------+
  |  首次运行配置向导  ·  白草净华  |
  +------------------------------------------------+
```

### 配置流程

向导分两步：

**第一步：必填配置**

| 配置项 | 说明 | 示例 |
|--------|------|------|
| MIMO_API_KEY | MiMo API 密钥 | `sk-xxxxxxxxxxxx` |
| QQBOT_APP_ID | QQ 机器人应用 ID | `1234567890` |
| QQBOT_APP_SECRET | QQ 机器人密钥 | `abcdefg1234567` |
| OWNER_IDS | 主人 QQ OpenID | `openid_xxxxx` |

> 如果你暂时不需要 QQ Bot 功能，QQBOT_APP_ID、QQBOT_APP_SECRET 和 OWNER_IDS 可以直接回车跳过，之后再用向导补充。

**第二步：选填配置**

7 个可选配置项，直接回车跳过即可。包括向量嵌入、图片上传、搜索、子 Agent 等 API 密钥。

### 配置摘要

向导结束时会显示配置摘要：

```
  🌿 配置摘要
  +------------------------------------------------+
  ✓ MIMO_API_KEY              sk-x****  MiMo API 密钥
  ✓ QQBOT_APP_ID              1234****  QQ Bot App ID
  ✗ QQBOT_APP_SECRET                    未配置
  ✓ OWNER_IDS                 open****  主人 ID
  ○ EMBED_API_KEY                       未配置（可选）
  ...
  +------------------------------------------------+
```

- `✓` 已配置
- `✗` 必填但未配置（需要补充）
- `○` 可选未配置

### 重新配置

随时可以再次运行向导修改配置：

```bash
python3 setup_wizard.py
```

已配置的密钥会以脱敏形式显示（如 `sk-x****`），直接回车保持现有值，输入新值则覆盖。

---

## 4. 启动方式

### 方式一：一键启动（推荐）

```bash
bash start.sh
```

启动流程：

1. 检查 `.env` 文件是否存在，不存在则自动运行配置向导
2. 检查 `MIMO_API_KEY` 是否已配置，未配置则提示运行向导
3. 尝试启动 QQ Bot 服务（如果系统中有 `qq-agent` 服务）
4. 启动 CLI 交互界面

### 方式二：仅启动 CLI

```bash
python3 cli.py
```

直接进入终端交互界面，不启动 QQ Bot 服务。

### 方式三：仅启动 QQ Bot

```bash
python3 qq_bot_adapter.py
```

启动 QQ 机器人适配器，通过 QQ 私聊与纳西妲对话。

### 方式四：Web UI

```bash
streamlit run web/app.py
```

在浏览器中访问 Streamlit 界面。

---

## 5. CLI 交互界面

启动后会看到纳西妲主题的欢迎界面：

```
  ✿  🌿  世  界  的  记  忆  ，  由  我  来  守  护  🌿  ✿

  ✿      _   _____    __  __________  ___      ✿
  ✿     / | / /   |  / / / /  _/ __ \/   |     ✿
  ✿    /  |/ / /| | / /_/ // // / / / /| |     ✿
  ✿   / /|  / ___ |/ __  // // /_/ / ___ |     ✿
  ✿  /_/ |_/_/  |_/_/ /_/___/_____/_/  |_|     ✿

  🌿  🌿  世  界  的  记  忆  ，  由  我  来  守  护  🌿  🌿

  +------------------------------------------------+
  |  纳西妲 AI Agent  ·  mimo-v2.5  ·  白草净华  |
  +------------------------------------------------+

  💬 直接输入消息跟纳西妲聊天
  📋 /help 查看所有命令
  🚪 exit 或 Ctrl+C 退出

  旅行者来啦～人家等好久了呢！🌿
```

### 基本操作

| 操作 | 说明 |
|------|------|
| 直接输入文字 | 与纳西妲对话 |
| `/命令` | 执行斜杠命令 |
| `exit` / `quit` / `q` | 退出 |
| `Ctrl+C` | 退出 |

### 状态提示

对话过程中会显示纳西妲的工作状态：

| 状态 | 含义 |
|------|------|
| 🌿 纳西妲正在想…… | 正在生成回复 |
| ✨ 人家在看看交给谁比较好～ | 正在路由到子 Agent |
| 🔍 人家帮你搜一下～ | 正在搜索 |
| 🌐 人家去网上看看～ | 正在浏览网页 |
| 💻 人家在跑命令～ | 正在执行 Shell 命令 |
| 🐍 人家在算东西～ | 正在执行 Python 代码 |
| 📷 人家看看摄像头～ | 正在拍照/分析 |

---

## 6. 命令参考

### 公共命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/cost` | 查看今日 API 消耗 | `/cost` |
| `/cost 7d` | 查看近 7 天 API 消耗 | `/cost 7d` |
| `/status` | 查看 Agent 运行状态 | `/status` |
| `/forget` | 清除短期对话记忆（保留长期记忆） | `/forget` |
| `/learn` | 查看学习记录 | `/learn` |
| `/note` | 查看笔记本和待办事项 | `/note` |
| `/hw` | 查看硬件状态（CPU/内存/磁盘/温度） | `/hw` |
| `/cam` | 拍照并分析画面 | `/cam` |
| `/cam snap` | 仅拍照保存，不做分析 | `/cam snap` |
| `/sys` | 查看系统运行状态 | `/sys` |
| `/help` | 显示帮助信息 | `/help` |

### 主人专属命令

以下命令仅对 `OWNER_IDS` 中配置的用户生效：

| 命令 | 说明 | 示例 |
|------|------|------|
| `/model mimo` | 切换到 MiMo 标准模式 | `/model mimo` |
| `/model mimo-pro` | 切换到 MiMo Pro 深度思考模式 | `/model mimo-pro` |
| `/model` | 查看当前模型模式 | `/model` |
| `/reset` | 重置对话上下文 | `/reset` |
| `/voice on` | 开启语音模式 | `/voice on` |
| `/voice off` | 关闭语音模式 | `/voice off` |
| `/agent 纳西妲` | 切换对话目标为纳西妲 | `/agent 纳西妲` |
| `/agent 可莉` | 切换对话目标为可莉 | `/agent 可莉` |

---

## 7. QQ Bot 模式

### 前提条件

1. 已在 [QQ 开放平台](https://q.qq.com) 创建机器人应用
2. 已配置 `QQBOT_APP_ID` 和 `QQBOT_APP_SECRET`
3. 已配置 `OWNER_IDS`（用于主人权限识别）

### 启动

```bash
python3 qq_bot_adapter.py
```

或使用 systemd 服务（如果已配置）：

```bash
sudo systemctl start qq-agent
sudo systemctl status qq-agent
```

### QQ 中的使用

- **私聊**：直接给机器人发消息即可对话
- **命令**：与 CLI 相同的斜杠命令均可用
- **图片**：可以发送图片，纳西妲会进行视觉分析
- **主动关怀**：如果配置了 Nudge 引擎，纳西妲会在长时间未对话时主动问候

---

## 8. 多 Agent 系统

纳西妲 AI Agent 内置了多个子 Agent，各有独立人格和能力：

| Agent | 名称 | 特点 | 切换命令 |
|-------|------|------|--------|
| nahida | 纳西妲 | 主 Agent，全能型助手 | `/agent 纳西妲` |
| klee | 可莉 | 活泼风格，擅长搜索和探索 | `/agent 可莉` |
| xilian | 昔涟 | 温柔风格，擅长分析和整理 | `/agent 昔涟` |
| yinlang | 银狼 | 酷飒风格，擅长技术和游戏 | `/agent 银狼` |
| nico | 尼可 | 神秘风格，擅长创意和想象 | `/agent 尼可` |

> 子 Agent 的可用性取决于是否配置了对应的 API 密钥（SILICONFLOW_API_KEY / OPENROUTER_API_KEY）。纳西妲（主 Agent）始终可用。

### Agent 调度

你也可以不手动切换，直接在对话中提出需求，纳西妲会自动判断是否需要将任务转交给合适的子 Agent 处理。

---

## 9. 配置项详解

所有配置项在 `.env` 文件中设置，可通过 `python3 setup_wizard.py` 交互式配置。

### MiMo 模型

| 变量 | 默认值 | 说明 |
|------|--------|------|
| MIMO_API_KEY | （空） | MiMo API 密钥，**必需** |
| MIMO_BASE_URL | `https://api.xiaomimimo.com/v1` | MiMo API 地址 |
| MIMO_MODEL_NAME | `mimo-v2.5` | 标准模式模型 ID |
| MIMO_PRO_MODEL_NAME | `mimo-v2.5-pro` | Pro 模式模型 ID |

### QQ Bot

| 变量 | 默认值 | 说明 |
|------|--------|------|
| QQBOT_APP_ID | （空） | QQ 机器人应用 ID |
| QQBOT_APP_SECRET | （空） | QQ 机器人应用密钥 |

### 权限

| 变量 | 默认值 | 说明 |
|------|--------|------|
| OWNER_IDS | （空） | 主人 QQ OpenID，多个用逗号分隔 |

### 向量嵌入（记忆检索）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| EMBED_API_KEY | （空） | 嵌入模型 API 密钥 |
| EMBED_BASE_URL | （空） | 嵌入模型 API 地址 |
| EMBED_MODEL | （空） | 嵌入模型名称 |

> 不配置嵌入模型时，记忆系统仍可工作，但无法进行语义向量检索。

### 主动关怀（Nudge 引擎）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| NUDGE_ENABLED | `true` | 是否启用主动关怀 |
| NUDGE_USER_OPENID | （空） | 目标用户 OpenID |
| NUDGE_GREETING_THRESHOLD | `3600` | 问候触发间隔（秒） |
| NUDGE_DND_START | `23` | 免打扰开始时间（小时） |
| NUDGE_DND_END | `7` | 免打扰结束时间（小时） |

### 图片上传

| 变量 | 默认值 | 说明 |
|------|--------|------|
| IMGBB_API_KEY | （空） | ImgBB 图片上传 API 密钥 |

### 数据存储

| 变量 | 默认值 | 说明 |
|------|--------|------|
| KIOXIA_DATA_DIR | `/media/orangepi/KIOXIA/nahida-data` | 外挂存储数据目录 |

> 如果外挂存储不可用，系统会自动回退到项目目录下的 `data/` 子目录。

### 可选 API

| 变量 | 默认值 | 说明 |
|------|--------|------|
| TAVILY_API_KEY | （空） | Tavily 搜索 API 密钥 |
| SILICONFLOW_API_KEY | （空） | SiliconFlow API 密钥 |
| OPENROUTER_API_KEY | （空） | OpenRouter API 密钥 |

---

## 10. 常见问题

### Q: 启动时提示 "MiMo client not initialized"

**原因**：`MIMO_API_KEY` 未配置。

**解决**：运行 `python3 setup_wizard.py` 配置 MiMo API 密钥，或手动编辑 `.env` 文件填入密钥。

### Q: QQ Bot 无法连接

**原因**：`QQBOT_APP_ID` 或 `QQBOT_APP_SECRET` 未配置，或 QQ 开放平台应用未审核通过。

**解决**：
1. 确认已在 QQ 开放平台创建应用并通过审核
2. 运行 `python3 setup_wizard.py` 配置 QQ Bot 凭据
3. 检查网络连接

### Q: 记忆系统不工作

**原因**：向量嵌入模型未配置。

**解决**：配置 `EMBED_API_KEY`、`EMBED_BASE_URL` 和 `EMBED_MODEL`。不配置时记忆系统仍可工作，但无法进行语义检索。

### Q: 摄像头功能不可用

**原因**：未连接 USB 摄像头，或 `opencv-python-headless` 未安装。

**解决**：
1. 确认 USB 摄像头已连接：`ls /dev/video*`
2. 安装依赖：`pip install opencv-python-headless`

### Q: 如何修改已配置的 API 密钥？

**解决**：运行 `python3 setup_wizard.py`，向导会显示当前配置的脱敏值，输入新值即可覆盖，直接回车保持不变。

### Q: 如何查看 API 消耗？

在 CLI 中输入 `/cost` 查看今日消耗，`/cost 7d` 查看近 7 天消耗。

### Q: 两种模型模式有什么区别？

| | MiMo 模式 | MiMo Pro 模式 |
|---|-----------|-------------|
| 模型 | mimo-v2.5 | mimo-v2.5-pro |
| 特点 | 响应快，适合日常对话 | 深度思考，推理更强 |
| 输入价格 | $0.10 / 百万 tokens | $0.20 / 百万 tokens |
| 输出价格 | $0.20 / 百万 tokens | $0.40 / 百万 tokens |

切换命令：`/model mimo` 或 `/model mimo-pro`

### Q: 如何重置对话？

- `/forget` — 清除短期对话记忆（保留长期记忆和画像）
- `/reset` — 完全重置对话上下文

### Q: 数据存储在哪里？

- 如果配置了 `KIOXIA_DATA_DIR` 且目录可用，数据存储在外挂存储
- 否则回退到项目目录下的 `data/` 子目录
- 数据库文件：`agent.db`（SQLite）
- 日志文件：`logs/` 目录