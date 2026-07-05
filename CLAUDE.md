# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

小妲 AI Agent：运行在 Orange Pi 4 Pro（ARM64, Debian 12）上的多 Agent AI 助手。以小妲为人格主体，下辖 4 个内置子代理（xiaoli 小莉/xiaolang 小狼/xiaolian 小涟/xiaoke 小可）。交互通道：QQ Bot、Web UI（Vue 3 + FastAPI）、CLI。

- Python 3.11 + asyncio + aiosqlite，虚拟环境 `.venv`
- LLM：小米 MiMo（mimo-v2.5 / -pro），降级链见 `model_router.py` 的 `ROUTE_TABLE`/`FALLBACK_ROUTE`
- 数据库：SQLite + sqlite-vec，**位于外挂存储** `/media/orangepi/KIOXIA/xiaoda-data/`（未挂载则服务拒绝启动）
- 配置/人格等 workspace 文件也在外挂存储（`config.py` 的 `WORKSPACE_DIR` 解析，回退 `~/.ai-agent/workspace`）

## 常用命令

```bash
# 语法检查（无 lint 配置，最低限度用编译检查）
.venv/bin/python -m py_compile <file.py>

# 测试（无 pytest 配置，测试是独立脚本，直接运行）
.venv/bin/python tests/e2e_test.py

# QQ Bot 生产服务（systemd，需 sudo）
sudo systemctl restart qq-agent && journalctl -u qq-agent -f

# Web UI 服务（独立进程，端口 8080，目前手动启动、无 systemd 单元）
setsid nohup .venv/bin/python agent.py --web > /tmp/webui.log 2>&1 < /dev/null & disown

# 前端构建（产物输出到 web/dist/，由 FastAPI 静态托管）
cd web/frontend && npm run build

# Web API 冒烟验证（密码在 .env 的 WEBUI_PASSWORD）
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"password":"<WEBUI_PASSWORD>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
curl -s http://127.0.0.1:8080/api/v1/agents -H "Authorization: Bearer $TOKEN"

# 数据库
sqlite3 /media/orangepi/KIOXIA/xiaoda-data/db/agent.db ".tables"
```

注意：**QQ Bot（qq_bot_adapter.py）和 Web UI（agent.py --web → web/server.py）是两个独立进程**，各自持有一个 AgentCore 实例。改了后端代码两个都要重启；改了前端只需 `npm run build`（dist 由运行中的 FastAPI 直接服务，无需重启，浏览器强刷即可）。

## 架构

### 核心调用链

```
用户消息 → 通道适配层（qq_bot_adapter / web/ws_hub / cli）
  → AgentCore.process()                  # agent_core.py，主体纳西妲
      安全检查 → 斜杠命令 → @mention 目标解析 → 表情包意图预选
      → AgentContext.build_messages()    # 三层提示：Stable/Context/Volatile
      → ModelRouter.route()              # ROUTE_TABLE 查表 + 重试 + 降级 + 凭证轮换
      → ToolCallHandler.handle()         # 工具并行执行 + 委托处理 + 结果总结
      → 后台任务（落库/记忆编码/画像）
      → ProcessResult(reply, emotion, sticker_path, audio_path, image_paths, video_path)
```

### 多 Agent 委托（本节描述当前实现，旧文档提到的 call_klee 已删除）

- 统一工具 `delegate_task(agent, task)`，在 `core/bootstrap.py::_register_delegate_tool()` **启动时动态注册**——描述里嵌入各子代理的 `route_description`，agent 参数带 enum 约束
- 执行端 `AgentCore.delegate_to_agent()` → `dispatcher.dispatch()`；可莉走 `delegate_to_klee()` 专用上下文
- 子代理不能再委托（`DELEGATE_BLOCKED_TOOLS` in agent_dispatcher.py + xiaoli_agent.py 的 `EXCLUDED_TOOLS`）
- 路由护栏：`agent_core.py` 检测到表情包意图（"表情包/贴纸"等关键词）时，当轮从工具列表硬移除 `delegate_task`——表情包由主体流程自动附带，委托出去会丢失
- 用户在 WebUI 顶栏直接切换子代理时，`web/ws_hub.py::process_and_serialize` 走 `core._dispatch_single_sub_agent()`（与 QQ 通道同款完整流程，带表情包/情绪/落库）。**不要**改回裸 `dispatcher.dispatch()`，那会丢掉所有媒体能力
- 子代理表情包匹配注意：可莉的标识名是 `keli`（不是 klee），判断用 `target.lower() in ("keli", "klee")`

### Web UI 全栈（web/）

```
web/frontend/         Vue 3 + TS + Pinia + Naive UI + Vite 源码
web/dist/             构建产物（FastAPI 托管，/assets/* 永久缓存、index.html 禁缓存）
web/server.py         FastAPI app 工厂；/media 挂载必须 follow_symlink=True（表情包是指向外挂盘的符号链接）
web/ws_hub.py         WebSocket 协议 + ProcessResult→JSON 序列化（媒体路径 → /media/ URL）
web/routers/          REST API（auth/chat/agents/models/tools/mcp/media/system/...）
web/agent_registry.py Agent CRUD + 权限矩阵 + 壁纸（DEFAULT_WALLPAPERS）；自建 Agent 持久化在 config/agents/*.json
```

- 鉴权：Bearer token（`/api/v1/auth/login`），WS 用 `?token=`；删除类接口额外要 `X-Confirm: yes` 头
- 每个 Agent 有 `wallpaper` 字段；前端 `AgentBackdrop.vue` 在切换 Agent 时交叉淡化背景。内置默认壁纸在 `web/frontend/public/assets/wallpapers/<name>.jpg`，用户上传的存 `web/media/wallpapers/`
- Skills 机制：`workspace/skills/*.md` 全文注入 system prompt（`config.py::load_skills()`，mtime 失效缓存），WebUI 的 Skills 页可上传/编辑，保存即生效无需重启
- 斜杠命令始终路由到主体 process（即使当前选了子代理）；前端命令名要去掉后端自带的 `/` 前缀再拼接，否则出现 `//cmd`

### 三套配置体系

| 类型 | 位置 | 用途 |
|------|------|------|
| 环境变量 | `.env` | API Key（MIMO_API_KEY 等）、WEBUI_PASSWORD、OWNER_IDS、KIOXIA_DATA_DIR |
| WebUI 运行时配置 | `config/webui_overrides.json`（经 `web/config_service.py` 读写） | ui.particles、tts、tools 开关、models.providers、ui.main_wallpaper |
| Workspace 人格文件 | `WORKSPACE_DIR/*.md`（SOUL/IDENTITY/USER/TOOLS/MEMORY/...） | 拼成 system prompt，60s TTL + mtime 失效缓存 |

## 关键陷阱

1. **SQLite 线程亲和**：`asyncio.to_thread()` 不保证同线程，连接必须 `check_same_thread=False`
2. **`ToolResult` 只有 `success/data/error`**，没有 `output` 属性
3. **prompt 模板字面花括号**：用 `.replace()` 不要用 `.format()`
4. **DSML 工具调用**：推理模型可能用 DSML 文本格式而非标准 `tool_calls`，`text_utils.py` 负责解析；新工具如需 DSML 支持要加进 `FAKE_XML_TOOL_PATTERN`
5. **botpy 被大量 monkey-patch**（qq_bot_adapter.py），升级 SDK 前先核对 patch 点
6. **静态文件符号链接**：Starlette `StaticFiles` 默认不跟随 symlink，新挂载点若内容含软链必须传 `follow_symlink=True`
7. **后台启动进程要 `setsid`**：直接 `nohup ... &` 会随当前 shell 会话被杀
8. **`config.py` 在 import 时执行 `load_dotenv()` 并解析外挂盘路径**——单元测试/脚本里 import 项目模块前先确认环境

## 新增工具/子代理检查清单

新工具：`tools/` 下用 `@register_tool()` 注册（异步函数直接 await）→ `config/workspace/TOOLS.md` 加使用规则 → 危险工具加进 `hooks.py::SecurityPreCheck` 与 `permission_manager.py::_SENSITIVE_TOOLS`。

新子代理：优先用 WebUI 的 Agent 管理页创建（落盘 `config/agents/*.json`，热插拔）；内置代理改 `core/bootstrap.py`。`route_description` 会进入 `delegate_task` 的工具描述，写清楚"何时召唤"。
