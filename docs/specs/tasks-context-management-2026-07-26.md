# 上下文管理优化任务分解（tasks.md）

> 配套 spec：`spec-context-management-2026-07-26.md`
> 执行原则：**每个任务独立 commit、可二分回滚、关键改动带 feature flag**

---

## 阶段一：P0 紧急修复（消除重试风暴 + 图片识别）

### Task 1.1：截断重试去递归化
**文件**：`/home/orangepi/.ai-agent/proj/model_router.py`
**位置**：line 1041-1075
**改动**：
- 新增内部方法 `_route_raw(task_type, messages, ...)`，跳过 `_handle_route_response` 的截断检测
- line 1053 `self.route(...)` 改为 `await self._route_raw(...)`
- 增加 `_truncation_retry_depth` 参数限制递归深度为 1
- Feature flag：`TRUNCATION_RETRY_DERECURSE=true`（默认 true，false 回退到旧行为）

**验收**：
- 单元测试：模拟连续 `finish_reason="length"`，确认最多 2 次 LLM 调用（非 2^N 次）
- 日志：`llm.truncated_retry_success` 1 小时内 ≤ 5 次

---

### Task 1.2：修复 finish_reason 检测 bug
**文件**：`/home/orangepi/.ai-agent/proj/model_router.py`
**位置**：line 1065-1068
**改动**：
- `_handle_route_response` 返回值改为 `(content, finish_reason)` 元组（保持向后兼容：调用方按需解包）
- 截断重试中直接调用 `_route_with_retry` 拿原始 response 对象
- 正确判断 retry 后是否仍截断，决定是否 break

**验收**：
- 单元测试：retry 后 `finish_reason="stop"` 时正确 break；`finish_reason="length"` 时继续重试

---

### Task 1.3：fallback 链透传 max_tokens
**文件**：`/home/orangepi/.ai-agent/proj/model_router.py`
**位置**：line 669, 691, 705-711
**改动**：
- `_try_fallback_chain` 签名增加 `original_max_tokens` 参数
- fallback 时取 `max(original_max_tokens, fallback_config.get("max_tokens", 1000))`
- line 711 硬编码 `1000` 改为 `max(original_max_tokens, 1000)`

**验收**：
- 集成测试：Web UI 传入 32768，触发 fallback 后 max_tokens 不低于 32768
- 日志：`router.*_fallback` 后不再立即出现 `llm.truncated_by_max_tokens`

---

### Task 1.4：收敛多层重试叠加
**文件**：`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**位置**：line 165-168（常量）、line 233/289/1919（重试循环）
**改动**：
- `MAX_VERIFICATION_TURNS` 8 → 4
- `early_retry` 范围 `range(3)` → `range(1)`
- 首轮 `length retry` + `incomplete retry` 改为：检测到 route 底层已做截断重试则跳过
- 增加全局 `_truncation_handled` ContextVar，让上层感知

**验收**：
- 单次请求最大 LLM 调用数 ≤ 8
- 集成测试：复杂工具调用场景仍能完成（验收循环 4 轮足够）

---

### Task 1.5：empty_content 错误不触发 fallback
**文件**：`/home/orangepi/.ai-agent/proj/utils/error_classifier.py`、`/home/orangepi/.ai-agent/proj/model_router.py`
**位置**：error_classifier.py:60-70（RECOVERY_MAP）、model_router.py:1096-1100
**改动**：
- 新增 `FailoverReason.EMPTY_REPLY`
- `RECOVERY_MAP[EMPTY_REPLY] = RecoveryAction.ABORT`
- `error_classifier._match_by_message` 识别 "empty_content" 关键词 → 返回 `EMPTY_REPLY`
- model_router.py:1096-1100 的 RuntimeError 改为抛 `LLMError(reason=FailoverReason.EMPTY_REPLY)`

**验收**：
- 单元测试：empty_content 不重试，直接降级到 DEGRADED_REPLY
- 日志：`router.retry_exhausted` 后跟 `llm.call_failed` 的频率下降 ≥ 80%

---

### Task 1.6：`_describe_images` 走安全客户端路径
**文件**：`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**位置**：line 2286-2322
**改动**：
- line 2289 / 2312 改用 `await self.router._select_client_for_provider("mimo")`
- 删除直接属性读取 `self.router._client`
- 增加日志：`agent.vision_client_acquired`

**验收**：
- 单元测试：模拟 `refresh_client` 并发触发，`_describe_images` 不抛 AttributeError
- 集成测试：上传图片不出现 "cannot read image"

---

### Task 1.7：校验 vision 响应内容
**文件**：`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**位置**：line 2317 之后
**改动**：
- 新增 `VISION_FAILURE_PATTERNS = ["cannot read image", "unable to read", "i cannot read", "image not readable", "无法识别", "图片无法识别"]`
- 响应内容长度 < 10 或匹配失败模式 → 记录 WARNING 日志，返回 ""
- 走兜底分支（`message_processor.py:1589-1592`）

**验收**：
- 单元测试：模拟 MiMo 返回 "cannot read image"，确认走兜底
- 日志：`agent.vision_suspicious_response` 出现时已正确降级

---

### Task 1.8：捕获 BadRequestError 区分错误码
**文件**：`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**位置**：line 2320 `except Exception`
**改动**：
- 拆为 `except _openai_mod.BadRequestError as e:` 优先捕获
- 记录 `e.response.status_code`、`e.body`
- 其他异常仍走 `except Exception`

**验收**：
- 日志：`agent.vision_bad_request` 包含 status_code 和 body

---

### Task 1.9：前端区分文档上传 vs 图片上传
**文件**：`/home/orangepi/.ai-agent/proj/web/frontend/src/components/chat/PromptInput.vue`、`/home/orangepi/.ai-agent/proj/web/routers/chat.py`
**改动**：
- 上传组件增加 tab："图片" / "文档"
- 文档上传走 `/chat/upload-doc` 端点（新增）
- 后端 `document_reader` 工具调用注入，而非 vision API
- 在 user_input 中加 `[Doc: path]` marker（后端解析后转 tool_call）

**验收**：
- 上传 .pdf/.docx 不再触发 vision API
- 上传 .png/.jpg 仍走 vision API

---

## 阶段二：P1 前端按钮重构 + 模式持久化

### Task 2.1：WS payload 结构化字段
**文件**：`/home/orangepi/.ai-agent/proj/web/frontend/src/stores/chat.ts`、`/home/orangepi/.ai-agent/proj/web/ws_hub.py`
**改动**：
- 前端 `sendMessage` WS payload 增加 `search_mode` / `think_mode` / `image_url` / `doc_paths` 字段
- 后端 `_handle_chat` 接收这些字段，构造 `image_data`、设 `_search_mode` / `_think_mode`
- `text` 保持纯净（不再拼装 marker）
- Feature flag：`USE_STRUCTURED_MODE_FLAGS=true`（默认 true，false 回退到 marker 解析）

**验收**：
- 集成测试：四个按钮点击后发送，DB `user_message` 为纯净原文

---

### Task 2.2：`conversation_logs` 新增 mode_flags 列
**文件**：`/home/orangepi/.ai-agent/proj/db/database.py`、`/home/orangepi/.ai-agent/proj/db/migrations/`
**改动**：
- 新增 migration：`ALTER TABLE conversation_logs ADD COLUMN mode_flags TEXT DEFAULT '';`
- `insert_conversation_log` 签名增加 `mode_flags` 参数
- `background_tasks.py:181-189` 透传 mode_flags

**验收**：
- 数据库迁移成功
- 新记录 `mode_flags` 列正确写入 JSON

---

### Task 2.3：`get_messages` 返回 mode_flags
**文件**：`/home/orangepi/.ai-agent/proj/web/routers/chat.py`
**位置**：line 105-134
**改动**：
- 查询返回 `mode_flags` 列
- 前端 `loadSession` 解析 JSON，还原 `msg.searchMode` / `msg.thinkMode` / `msg.hasImage`
- `retryLast` 重发时带上原模式

**验收**：
- 重试按钮点击后行为与首次发送一致

---

### Task 2.4：`agent_context` 增加 cwd 字段
**文件**：`/home/orangepi/.ai-agent/proj/agent_context.py`、`/home/orangepi/.ai-agent/proj/web/routers/workspace.py`
**改动**：
- `AgentContext.__init__` 增加 `self.cwd: str = ""`
- `/workspace/confirm` 端点调用后同步更新 `agent_context.cwd`
- `_process_impl` 入口主动读取 `PermissionManager.cwd`

**验收**：
- 切换工作目录后，LLM 后续回复能感知新目录

---

### Task 2.5：prompt_builder 注入当前 cwd
**文件**：`/home/orangepi/.ai-agent/proj/prompt_builder.py`
**改动**：
- 系统 prompt 中增加 `<workspace>当前授权工作目录：{cwd}</workspace>`
- 从 `agent_context.cwd` 读取

**验收**：
- 日志：系统 prompt 包含当前 cwd
- LLM 回复中能引用当前目录

---

### Task 2.6：`/workspace/confirm` 追加系统消息
**文件**：`/home/orangepi/.ai-agent/proj/web/routers/workspace.py`
**位置**：line 81-99
**改动**：
- 调用 `agent_context.add_message("system", f"[系统] 用户已切换工作目录到：{path}")`
- 让历史记录可追溯

**验收**：
- DB `conversation_logs` 出现 system 消息记录目录切换

---

### Task 2.7：PermissionManager 按 session 隔离
**文件**：`/home/orangepi/.ai-agent/proj/utils/permission.py`（或对应文件）
**改动**：
- `PermissionManager` 改为按 `session_id` 隔离 cwd
- 保留全局默认 cwd 作为兜底
- 多标签/多用户互不干扰

**验收**：
- 集成测试：两个标签页切换不同目录，工具调用基线不串味

---

### Task 2.8：过渡兼容旧 marker
**文件**：`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**位置**：line 421-432
**改动**：
- 保留 `[Search:]` / `[Think:]` / `[Image:]` 解析作为兜底
- 优先使用结构化字段（`ctx.search_mode` 等）
- 解析后从 user_input 中剥离 marker

**验收**：
- 旧客户端发送 marker 仍能正常工作
- 新客户端发送结构化字段不重复解析

---

## 阶段三：P1 空回复 + 主动问候治理

### Task 3.1：空回复不入库
**文件**：`/home/orangepi/.ai-agent/proj/core/background_tasks.py`
**位置**：line 178-193
**改动**：
- 增加守卫：`if not reply or not reply.strip():` 跳过 `insert_conversation_log`
- 改为记录到 `errors` 表便于排查
- 日志：`bg.skip_empty_reply`

**验收**：
- DB `conversation_logs.assistant_reply` 为空的记录数 ≤ 总数 1%

---

### Task 3.2：agent_context 注入时跳过空 assistant_reply
**文件**：`/home/orangepi/.ai-agent/proj/agent_context.py`
**位置**：line 792-801
**改动**：
- 增加 `if not asst_msg: continue` 守卫
- 避免注入"用户说了 → 小妲没回"的上下文割裂

**验收**：
- 单元测试：DB 中有空回复时，注入的历史摘要不出现空 asst_preview

---

### Task 3.3：proactive_greeting 走独立通道
**文件**：新增 `/home/orangepi/.ai-agent/proj/core/greeting_channel.py`
**改动**：
- 系统提示词作为 system message 注入，不作为 user message
- 问候生成后直接发送给用户，不写入 `conversation_logs.user_message`
- 仅在 `greeting_log` 表记录"问候已发送"
- 后续轮次 LLM 看到的是"小妲主动说了：xxx"（assistant 消息）
- Feature flag：`GREETING_INDEPENDENT_CHANNEL=true`（默认 true）

**验收**：
- DB `conversation_logs.user_message` 不含 `（场景：` 等系统提示词
- 主动问候功能仍正常工作

---

### Task 3.4：截断续写指令不入历史
**文件**：`/home/orangepi/.ai-agent/proj/model_router.py`
**位置**：line 1048-1060
**改动**：
- 确认 `retry_messages` 是 `messages.copy()` + 临时 append，不写回 `context.history`
- retry 后的合并 content 写回 history 时，**不包含** "请继续完成你的回复..." 指令

**验收**：
- 单元测试：截断重试后 `context.history` 不含续写指令

---

## 阶段四：P2 内部提示词收敛

### Task 4.1：系统 prompt 移除元词汇
**文件**：`/home/orangepi/.ai-agent/proj/prompt_builder.py`
**改动**：
- 审查全文，将"工具调用"、"DSML 协议"、"上下文压缩"、"记忆编码"改为角色化表达
- 例如："小妲可以通过世界树根系感知..." / "过往点滴" / "世界树的记忆"

**验收**：
- LLM 回复中不再出现技术性元词汇（除非用户主动问）

---

### Task 4.2：截断续写指令角色化
**文件**：`/home/orangepi/.ai-agent/proj/model_router.py`
**位置**：line 1052
**改动**：
- `"请继续完成你的回复，不要重复已说的内容。"` → `"（继续说完）"`
- 或改为 system message：`"继续生成未完成的回复"`

**验收**：
- 截断续写不影响 LLM 角色

---

### Task 4.3：`[Search:]` 模式不再重写 user_input
**文件**：`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**位置**：line 423-427
**改动**：
- 不再重写 `user_input = f"请使用 web_search 工具搜索最新信息后回答：{原文本}"`
- 改为通过 system prompt 注入：`"本次回复请使用 web_search 工具搜索最新信息后回答。"`
- 或通过 tool-forcing 逻辑实现

**验收**：
- DB `conversation_logs.user_message` 为用户原话
- LLM 仍能正确调用 web_search

---

### Task 4.4：上下文压缩摘要角色化
**文件**：`/home/orangepi/.ai-agent/proj/agent_context.py`
**位置**：line 208-210
**改动**：
- `"上下文压缩"` 关键词 → `"历史回顾"` 或 `"过往点滴"`
- 压缩摘要内容也角色化（避免"用户说了 X，小妲回了 Y"的技术性描述）

**验收**：
- LLM 看到的压缩摘要符合角色设定

---

## 阶段五：P2 跨渠道一致性

### Task 5.1：统一 max_tokens 配置入口
**文件**：`/home/orangepi/.ai-agent/proj/config.py`、`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**改动**：
- `WEB_UI_MAX_TOKENS` 改为 `CHANNEL_MAX_TOKENS` JSON 配置
- 各渠道按平台限制设置（web: 32768, qq_c2c: 1500, qq_group: 1000, cli: 8192）

**验收**：
- 各渠道 max_tokens 配置统一管理
- QQ 通道不再被超长回复截断

---

### Task 5.2：渠道感知的场景标识
**文件**：`/home/orangepi/.ai-agent/proj/agent_context.py`
**位置**：line 35-48
**改动**：
- `_SCENE_HINTS` 扩展为完整的渠道策略表
- 包含：允许的 max_tokens、允许的工具集、回复长度建议、隐私等级

**验收**：
- 各渠道场景标识完整

---

### Task 5.3：群聊多用户上下文隔离强化
**文件**：`/home/orangepi/.ai-agent/proj/agent_context.py`、`/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`
**改动**：
- 验证 `switch_user_context` 在群聊场景下被正确调用
- 凭证池、画像、记忆检索按用户隔离
- 工具调用结果不串味

**验收**：
- 群聊多用户场景无串话
- 用户 A 的隐私不泄露给用户 B

---

## 阶段六：验证与回归

### Task 6.1：端到端测试
**文件**：`/home/orangepi/.ai-agent/proj/tests/test_context_management_e2e.py`（新增）
**改动**：
- 每个按钮点击 → 发送 → 持久化 → 重载会话 → 校验
- 校验 `user_message` 是否为用户原话
- 校验模式是否可还原
- 校验重试是否复现首次行为

**验收**：
- 所有端到端测试通过

---

### Task 6.2：监控指标基线
**文件**：`/home/orangepi/.ai-agent/proj/utils/metrics.py`
**改动**：
- 新增指标：`llm.calls_per_request`（直方图）
- 新增指标：`context.empty_reply_skipped`（计数器）
- 新增指标：`vision.suspicious_response`（计数器）

**验收**：
- 指标可在监控面板查看

---

### Task 6.3：文档更新
**文件**：`/home/orangepi/.ai-agent/proj/docs/ARCHITECTURE.md`
**改动**：
- 更新上下文管理架构图
- 更新渠道差异说明
- 更新前端按钮状态机

**验收**：
- 文档与代码一致

---

## 任务依赖关系

```
阶段一（P0）:
  Task 1.1 → Task 1.2（修复 finish_reason 检测，依赖 1.1 的 _route_raw）
  Task 1.3（独立）
  Task 1.4（依赖 1.1）
  Task 1.5（独立）
  Task 1.6 → Task 1.7 → Task 1.8（_describe_images 三连）
  Task 1.9（独立，前端）

阶段二（P1）:
  Task 2.1 → Task 2.2 → Task 2.3（结构化字段链路）
  Task 2.4 → Task 2.5 → Task 2.6（工作目录上下文衔接）
  Task 2.7（独立）
  Task 2.8（依赖 2.1）

阶段三（P1）:
  Task 3.1（独立）
  Task 3.2（独立）
  Task 3.3（独立）
  Task 3.4（依赖 1.1）

阶段四（P2）:
  Task 4.1（独立）
  Task 4.2（依赖 1.1）
  Task 4.3（依赖 2.1）
  Task 4.4（独立）

阶段五（P2）:
  Task 5.1（独立）
  Task 5.2（独立）
  Task 5.3（独立）

阶段六（验证）:
  Task 6.1（依赖所有）
  Task 6.2（独立）
  Task 6.3（依赖所有）
```

---

## 执行顺序建议

1. **第一波**（P0 紧急，1-2 天）：Task 1.1, 1.2, 1.3, 1.4, 1.5（消除重试风暴）
2. **第二波**（P0 紧急，1 天）：Task 1.6, 1.7, 1.8, 1.9（修复图片识别）
3. **第三波**（P1 重要，2-3 天）：Task 2.1-2.8（前端按钮重构）
4. **第四波**（P1 重要，1 天）：Task 3.1, 3.2, 3.3, 3.4（空回复 + 主动问候）
5. **第五波**（P2 优化，1-2 天）：Task 4.1-4.4, 5.1-5.3（提示词 + 跨渠道）
6. **第六波**（验证，1 天）：Task 6.1, 6.2, 6.3

**总计**：约 7-10 个工作日
