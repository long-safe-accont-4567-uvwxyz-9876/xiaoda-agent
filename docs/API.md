# xiaoda-agent API 文档

## 概述

xiaoda-agent 提供 RESTful API 和 WebSocket 接口。所有 API 响应使用统一的 `Envelope` 包装格式：

```json
{
  "code": 0,
  "data": { ... },
  "message": "ok"
}
```

- `code=0` 表示成功，非零表示错误
- 认证端点使用 JWT Bearer Token（首次运行时免认证）

---

## 认证 API

### POST /auth/login
登录获取 JWT Token。

**请求体：**
```json
{ "password": "string" }
```

**响应：**
```json
{
  "code": 0,
  "data": {
    "access_token": "eyJ...",
    "token_type": "bearer",
    "expires_at": 1750000000
  }
}
```

### POST /auth/logout
登出当前 Token。

### POST /auth/revoke-all
撤销所有已签发的 Token。

---

## 聊天 API

### GET /commands
获取可用的斜杠命令列表。

### GET /sessions
获取对话会话列表。

### POST /sessions
创建新的对话会话。

---

## Agent 管理 API

### GET /agents
获取所有 Agent 列表。

### GET /agents/{name}
获取指定 Agent 的配置。

### POST /agents
创建新 Agent。

**请求体：**
```json
{
  "name": "string",
  "display_name": "string",
  "personality": "string",
  "model": "string"
}
```

### PUT /agents/{name}
更新 Agent 配置。

### DELETE /agents/{name}
删除 Agent。

### POST /agents/{name}/enable
启用 Agent。

### POST /agents/{name}/disable
禁用 Agent。

### GET /agents/{name}/permissions
获取 Agent 权限配置。

### PUT /agents/{name}/permissions
更新 Agent 权限配置。

### GET /agents/{name}/personality
获取 Agent 人格配置。

### PUT /agents/{name}/personality
更新 Agent 人格配置。

### POST /agents/{name}/model
切换 Agent 使用的模型。

### POST /agents/{name}/test
测试 Agent 对话。

### GET /agent-names
获取所有 Agent 名称映射。

### GET /agents/public-wallpaper
获取公共壁纸。

### POST /agents/{name}/wallpaper
上传 Agent 壁纸。

### GET /agents/{name}/stickers
获取 Agent 表情贴纸列表。

### GET /agents/{name}/stickers/file/{filename}
获取贴纸文件。

### POST /agents/{name}/stickers
上传贴纸。

### DELETE /agents/{name}/stickers/{filename}
删除贴纸。

---

## 模型管理 API

### GET /models/providers
获取所有模型供应商列表。

### POST /models/providers
添加模型供应商。

**请求体：**
```json
{
  "id": "string",
  "name": "string",
  "format": "openai",
  "base_url": "string",
  "api_key_env": "string"
}
```

### PUT /models/providers/{pid}
更新供应商配置。

### DELETE /models/providers/{pid}
删除供应商。

### POST /models/providers/{pid}/key
设置供应商 API Key。

### POST /models/providers/reorder
调整供应商优先级。

### GET /models/routes
获取模型路由表。

### PUT /models/routes/{task}
更新指定任务类型的模型路由。

### GET /models/chat-model
获取当前聊天模型配置。

### GET /models/credentials/status
获取所有凭证状态。

### GET /models/temperature
获取温度参数。

### PUT /models/temperature
更新温度参数。

### GET /models/usage
获取模型使用统计。

### GET /models/discover
自动发现可用模型。

### POST /models/chat-model
设置聊天模型。

---

## 工具 API

### GET /tools
获取所有已注册工具列表。

### PUT /tools/{name}
更新工具配置（启用/禁用）。

### POST /tools/{name}/invoke
直接调用工具。

**请求体：**
```json
{
  "arguments": { ... }
}
```

### GET /tools/{name}/stats
获取工具调用统计。

### GET /tools/limits
获取工具频率限制配置。

### POST /tools/{name}/test
测试工具调用。

### GET /skills
获取技能列表。

### GET /skills/{name}
获取指定技能详情。

### PUT /skills/{name}
更新技能配置。

### DELETE /skills/{name}
删除技能。

---

## 洞察 API

### GET /insight/emotion/current
获取当前情感状态。

### GET /insight/emotion/history
获取情感变化历史。

### GET /insight/portrait
获取用户画像。

### POST /insight/portrait/consolidate
手动触发画像整合。

### GET /insight/today
获取今日洞察摘要。

### GET /insight/memories
获取记忆列表（支持分页和搜索）。

**查询参数：**
- `q` - 搜索关键词
- `limit` - 返回数量
- `offset` - 偏移量

### POST /insight/memories
手动添加记忆。

### PUT /insight/memories/{memory_id}
更新记忆。

### DELETE /insight/memories/{memory_id}
删除记忆。

### GET /insight/knowledge/graph
获取知识图谱。

### GET /insight/knowledge/entities
获取知识实体列表。

### POST /insight/knowledge/entities
添加知识实体。

### PUT /insight/knowledge/entities/{name}
更新知识实体。

### DELETE /insight/knowledge/entities/{name}
删除知识实体。

### GET /insight/knowledge/relations
获取知识关系列表。

### POST /insight/knowledge/relations
添加知识关系。

### PUT /insight/knowledge/relations/{relation_id}
更新知识关系。

### DELETE /insight/knowledge/relations/{relation_id}
删除知识关系。

### GET /insight/notebook
获取笔记本列表。

### POST /insight/notebook
创建笔记。

### PUT /insight/notebook/{note_id}
更新笔记。

### DELETE /insight/notebook/{note_id}
删除笔记。

### GET /insight/learnings
获取学习记录列表。

### POST /insight/learnings
添加学习记录。

### PUT /insight/learnings/{learning_id}
更新学习记录。

### DELETE /insight/learnings/{learning_id}
删除学习记录。

### GET /insight/instincts
获取本能列表。

### POST /insight/instincts
添加本能。

### PUT /insight/instincts/{instinct_id}
更新本能。

### DELETE /insight/instincts/{instinct_id}
删除本能。

### GET /insight/xp
获取经验值信息。

### GET /insight/xp/levels
获取等级体系。

---

## 健康检查 API

### GET /health/self
基本自检（快速）。

### GET /health/probes
获取所有健康检查探针列表。

### POST /health/test/llm
测试 LLM 连通性。

### POST /health/test/tts
测试 TTS 服务。

### POST /health/test/video
测试视频服务。

### POST /health/test/mcp/{server}
测试 MCP 服务器连通性。

### POST /health/test/{probe_id:path}
测试指定探针。

### POST /health/test-all
运行全量自检（异步，通过 WebSocket 推送进度）。

### GET /health/report
获取最近一次自检报告。

### GET /health/system
获取系统信息（平台、CPU、内存、磁盘）。

---

## 系统管理 API

### GET /system/status
获取系统运行状态。

### GET /system/audit
获取审计日志。

### GET /system/metrics
获取系统指标。

### GET /system/logs
获取最近日志。

### GET /system/lan-addresses
获取局域网 IP 地址。

### GET /system/config
获取系统配置。

### PUT /system/config
更新系统配置（受白名单限制）。

### GET /system/permission-mode
获取权限模式。

### PUT /system/permission-mode
更新权限模式。

### POST /system/restart
重启服务。

### GET /system/doctor
运行系统诊断。

### POST /system/doctor/fix
自动修复诊断问题。

---

## 邮件 API

### GET /mail/config
获取邮件配置。

### PUT /mail/config
更新邮件配置。

### GET /mail/stats
获取邮件统计。

### GET /mail/inbox
获取收件箱列表。

### GET /mail/auth-status
获取邮件认证状态。

### POST /mail/auth-login
发起邮件 OAuth 登录。

---

## 日程 API

### GET /schedule/config
获取日程配置。

### PUT /schedule/config
更新日程配置。

### GET /schedule/dnd
获取免打扰时段。

### PUT /schedule/dnd
更新免打扰时段。

### GET /schedule/greetings
获取问候计划列表。

### POST /schedule/greetings
创建问候计划。

### PUT /schedule/greetings/{sid}
更新问候计划。

### DELETE /schedule/greetings/{sid}
删除问候计划。

### POST /schedule/test-greeting
测试问候推送。

### GET /schedule/history
获取问候历史。

---

## 媒体 API

### POST /media/tts
文本转语音。

**请求体：**
```json
{
  "text": "string",
  "agent": "string",
  "voice": "string"
}
```

### GET /media/tts/voices
获取可用语音列表。

### POST /media/tts/voices/{agent}
上传自定义语音。

### DELETE /media/tts/voices/{agent}/{name}
删除自定义语音。

### GET /media/tts/config
获取 TTS 配置。

### PUT /media/tts/config
更新 TTS 配置。

### POST /media/image
生成图片。

### POST /media/video
生成视频。

### GET /media/tasks
获取媒体任务列表。

### GET /media/tasks/{task_id}
获取媒体任务详情。

### DELETE /media/tasks/{task_id}
取消/删除媒体任务。

### GET /media/gallery
获取媒体画廊。

### DELETE /media/gallery/{type}/{name}
删除画廊项目。

---

## 工作流 API

### GET /workflows
获取工作流列表。

### GET /workflows/{wf_id}
获取工作流详情。

### POST /workflows
创建工作流。

### PUT /workflows/{wf_id}
更新工作流。

### DELETE /workflows/{wf_id}
删除工作流。

### GET /workflows/{wf_id}/preview
预览工作流执行。

---

## 插件 API

### GET /plugins
获取已安装插件列表。

### GET /plugins/{plugin_id}
获取插件详情。

### POST /plugins/{plugin_id}/load
加载插件。

### POST /plugins/{plugin_id}/enable
启用插件。

### POST /plugins/{plugin_id}/disable
禁用插件。

### POST /plugins/{plugin_id}/reload
重载插件。

### POST /plugins/{plugin_id}/unload
卸载插件。

### GET /plugins/{plugin_id}/config
获取插件配置。

### PUT /plugins/{plugin_id}/config
更新插件配置。

### POST /plugins/discover
发现可用插件。

---

## MCP API

### GET /mcp/servers
获取 MCP 服务器列表。

### POST /mcp/servers
添加 MCP 服务器。

### PUT /mcp/servers/{name}
更新 MCP 服务器配置。

### DELETE /mcp/servers/{name}
删除 MCP 服务器。

### POST /mcp/servers/{name}/start
启动 MCP 服务器。

### POST /mcp/servers/{name}/restart
重启 MCP 服务器。

### POST /mcp/servers/{name}/stop
停止 MCP 服务器。

### GET /mcp/servers/{name}/tools
获取 MCP 服务器提供的工具列表。

### GET /mcp/templates
获取 MCP 服务器模板列表。

### GET /mcp/servers/{server_name}/health
获取 MCP 服务器健康状态。

### PUT /mcp/servers/{server_name}/tools/{tool_name}/enabled
启用/禁用 MCP 工具。

---

## 市场 API

### GET /plugins
浏览市场插件。

### POST /plugins/install
安装市场插件。

### POST /plugins/uninstall
卸载市场插件。

### GET /skills
浏览市场技能。

### POST /skills/install
安装市场技能。

### POST /skills/uninstall
卸载市场技能。

### GET /mcp
浏览市场 MCP 服务器。

### POST /mcp/install
安装市场 MCP 服务器。

### POST /mcp/uninstall
卸载市场 MCP 服务器。

---

## 安装向导 API

### GET /setup/first-run
检测是否首次运行。

### GET /setup/version
获取版本信息。

### GET /setup/keys
获取 API Key 配置状态（需认证）。

### POST /setup/test-key
测试单个 API Key（需认证）。

### POST /setup/keys
保存 API Key 配置（需认证）。

### GET /setup/user-profile
获取用户资料（需认证）。

### POST /setup/user-profile
保存用户资料（需认证）。

### GET /brand/signature
获取品牌签名。

### GET /setup/disclaimer-status
获取免责声明状态（需认证）。

### POST /setup/agree-disclaimer
同意免责声明（需认证）。

---

## WebSocket 接口

连接地址: `ws://{host}:{port}/ws`

### 客户端发送消息格式

```json
{
  "type": "message_type",
  "data": { ... }
}
```

### 消息类型

| type | 方向 | 说明 |
|------|------|------|
| `chat` | C→S | 发送对话消息 |
| `terminal_input` | C→S | 终端输入（需 conn_id 归属校验） |
| `terminal_resize` | C→S | 终端窗口大小调整（需 conn_id 归属校验） |
| `terminal_kill` | C→S | 终止终端会话（需 conn_id 归属校验） |

### 服务端推送消息类型

| type | 方向 | 说明 |
|------|------|------|
| `chat_delta` | S→C | 流式文本片段 |
| `chat_done` | S→C | 对话完成 |
| `tool_start` | S→C | 工具调用开始 |
| `tool_end` | S→C | 工具调用结束 |
| `tool_event` | S→C | 工具执行事件 |
| `health_progress` | S→C | 健康检查进度 |
| `health_done` | S→C | 健康检查完成 |
| `terminal_output` | S→C | 终端输出 |
| `greeting` | S→C | 问候推送 |
| `error` | S→C | 错误通知 |

### 安全机制

- WebSocket 连接数限制（默认 10）
- 终端会话操作需 conn_id 归属校验
- 连接注册使用原子操作防止 TOCTOU 竞态

---

## 错误处理

所有 API 错误响应格式：

```json
{
  "code": 40001,
  "message": "错误描述",
  "data": null
}
```

常见错误码：

| HTTP 状态码 | 场景 |
|-------------|------|
| 401 | 未认证或 Token 过期 |
| 403 | 权限不足 |
| 409 | 资源冲突（如全量自检已在进行中） |
| 422 | 请求参数验证失败 |
| 500 | 服务器内部错误 |