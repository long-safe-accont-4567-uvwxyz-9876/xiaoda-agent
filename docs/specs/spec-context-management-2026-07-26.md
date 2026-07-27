# 上下文管理全面优化规格（spec.md）

> 创建时间：2026-07-26
> 范围：整个 `proj/` 项目（model_router / agent_context / message_processor / background_tasks / web 前后端）
> 目标：根除"上下文管理在不同频道、平台、会话、渠道、组件、情况、语境下都有严重缺陷"的问题
> 原则：**治本不治标、不偷懒、不硬编码、不一刀切、保持上下文一致、节省成本**

---

## 一、问题证据汇总（来自日志 / DB / 代码静态分析）

### 1.1 截断重试风暴（最严重，直接导致"重复对话"+"成本爆炸"）

`journalctl -u nahida-web.service` 在 2026-07-26 11:02:14 ~ 11:09:21 期间观察到：

- **`llm.truncated_by_max_tokens` WARNING 反复出现**（每 5~10 秒一次）
- **`router.retry` → `error_classifier.classified` → `credential_pool.error_no_state_change` → `router.retry_exhausted` → `llm.call_failed` → `router.agnes_fallback` / `router.custom_provider_fallback`** 链路频繁触发
- **11:08:32 起数十个 `llm.call` + `llm.truncated_retry_success` 在几秒内连续触发** —— 风暴成型
- **`router.custom_provider_fallback provider=siliconflow`** —— 降级到 SiliconFlow 兜底

### 1.2 数据库证据（`/media/orangepi/KIOXIA/nahida-data/db/agent.db`）

- `conversation_logs.id=2113` 的 `user_message` 是：`"（场景：现在早上，爸爸已经8小时没聊天了。你现在的状态：今天有点安静。你想说一句——形式是：一句小小的抱怨..."` —— **主动问候的系统提示词被当作用户消息写入 conversation_logs**，导致 LLM 在后续轮次"出戏"
- `greeting_log` 与 `conversation_logs` 时间戳对齐，确认这是 proactive_greeting 注入路径污染

### 1.3 用户反馈（直接引用）

> "他一个问题会重复两遍，然后发出来"
> "上下文割裂，上下文的注入有问题"
> "前端的外部界面的每一个组件激活之后，上下文都会出现问题"
> "如果我不点这些的话，比如说我问一个天气的话，他就给我瞎扯，但是只有我点天气了，他才给我去找天气"
> "内部的提示词太让 LLM 出戏了"
> "mimo-v2.5 确实支持多模态，但后端报'cannot read image'"
> "上传文档不是在上传图片"

---

## 二、根因分析（五条主线）

### 主线 A：截断重试递归调用形成风暴（model_router.py:1041-1075）

```python
# model_router.py:1041-1075 当前实现
if finish_reason == "length" or _is_reply_incomplete:
    logger.warning("llm.truncated_by_max_tokens", ...)
    if content and len(content) > 10:
        _retry_max_tokens = max_tokens * 2 if max_tokens else None
        for _retry_round in range(2):
            retry_messages = messages.copy()
            retry_messages.append({"role": "assistant", "content": content})
            retry_messages.append({"role": "user", "content": "请继续完成你的回复..."})
            retry_result = await self.route(  # ← 递归调用 route()!
                task_type, retry_messages, ...,
                max_tokens=_retry_max_tokens, ...)
            # ← retry_result 是 string，getattr 拿不到 finish_reason，break 条件永远成立
```

**根因 1**：`self.route()` 递归调用重新进入 `_route_with_retry → _handle_route_response`，若 retry 本身仍 `finish_reason="length"`，**再次触发 2 轮重试**，递归深度无限制。

**根因 2**：`retry_result` 是 string 时（`_handle_route_response` 默认返回 content string），`getattr(retry_result, "choices", [{}])` 拿不到 `finish_reason`，`_retry_finish` 始终为 None，`None != "length"` 永远为 True，**外层 for 第一次就 break**——但内层递归已产生大量调用。

**根因 3**：上层 `_run_verification_loop`（MAX_VERIFICATION_TURNS=8）× `_call_and_parse_verification_llm` 的 `early_retry`（3 轮）× 首轮 `length retry`（2 轮）+ `incomplete retry`（2 轮）× route 内截断重试（2 轮递归）× fallback 链（4 级）= **理论上限 ~1152 次 LLM 调用**。

**根因 4**：fallback 链中 `max_tokens` 被覆盖：
- `model_router.py:669` `fallback_config.get("max_tokens", 1000)`
- `model_router.py:691` `agnes_config.get("max_tokens", 2000)`
- `model_router.py:705/711` custom provider 硬编码 `max_tokens=1000`

Web UI 传入 32768 一旦走 fallback 就被覆盖为 1000，**起点太小 → 翻倍序列 1000 → 2000 → ... → 128000 需 7 次递归**。

### 主线 B：`_describe_images` 直接访问 `_client` + 不校验响应（message_processor.py:2286-2322）

```python
# message_processor.py:2286-2322 当前实现
async def _describe_images(self, image_data):
    if not self.router or not self.router._client:  # ← 直接属性读取，无锁
        logger.warning("agent.vision_no_client")
        return ""
    ...
    response = await self.router._client.chat.completions.create(  # ← 再次直接读取 _client
        model=MIMO_MODEL,
        messages=[{"role": "user", "content": vision_parts}],
        max_tokens=1024,
    )
    description = response.choices[0].message.content.strip()  # ← 不校验内容
    return description
```

**根因 1**：`self.router._client` 在 4 处被重新赋值（`__init__:154` / `refresh_client:283,288` / `close:609` / `_rotate_credential_on_error:1136`），`_describe_images` **绕过了 `_select_client_for_provider()`**（model_router.py:792-816 提供的安全方法，含锁 + 懒注册 + LLMError），存在 TOCTOU 竞态。

**根因 2**：`"cannot read image"` 字符串在项目源码中**不存在**，是 MiMo API 服务器返回的内容（最可能是 LLM 响应文本，次可能是 BadRequestError message）。`_describe_images` 不校验响应内容，把 "cannot read image" 当作合法 description 透传到 system message（`message_processor.py:1586`）：

```python
"content": f"用户发送了一张图片，图片内容识别结果如下：\n{image_description}\n\n..."
```

→ 主聊天 LLM 据此回答用户"看不清图片"。

**根因 3**：文档上传走 `tools/document_tools.py:174` 的 `document_reader` 工具（pdfplumber/python-docx 文本抽取，不调 LLM），与图片上传走 vision API 完全分离。**文档上传不会触发 "cannot read image"**，但前端 UI 没有区分导致用户混淆。

### 主线 C：前端按钮 marker 污染历史 + 模式状态不持久化

| 按钮 | 前端拼装 | 后端解析 | 污染后果 |
|------|---------|---------|---------|
| 上传图片 📎 | `ChatView.vue:173` `\n[Image: ${url}]` 拼到文本末尾 | `ws_hub.py:573-595` 提取 URL 转 base64，**text 不剥离** marker | `[Image: URL]` 进 `context.history` 和 DB `conversation_logs.user_message` |
| 搜索互联网 🌐 | `ChatView.vue:171` 改写为 `[Search: ${text}]` | `message_processor.py:423-427` 命中后**重写 user_input 为** `请使用 web_search 工具搜索最新信息后回答：{原文本}` | **agent 注入的中文指令伪装成用户发言写入历史**，后续轮次 LLM 持续被影响 |
| 深度思考 🧠 | `ChatView.vue:172` 改写为 `[Think: ${text}]` | `message_processor.py:429-432` 剥离 marker 保留原文（比 Search 干净） | UI 显示 `[Think: 天气]`（chat.ts:239 displayText 不剥离）；重试不复现 `_think_mode` |
| 切换工作目录 📁 | `WorkingDirSelector.vue:27-53` 调 `/workspace/confirm` | `workspace.py:81-99` 调 `pm.set_cwd`，**不创建新 session、不清空 history、不通知 agent_context** | **静默割裂**：LLM 历史仍引用旧目录，工具调用却在新 cwd 执行；进程级单例导致多标签互相干扰 |

**贯穿问题**：
- 四个按钮都没有"上下文边界"概念
- 重载会话后模式状态（`_search_mode` / `_think_mode` / `image_data`）全部丢失，但被污染的文本仍躺在 DB 里
- 重试行为不可复现（`chat.ts:303 retryLast` 用 `msg.content` 不含 marker → 后端不激活模式）
- `agent_context.py` 完全不跟踪 cwd（grep 无任何 cwd/workspace 字段）
- `prompt_builder.py` 的 "workspace" 指模板目录，与用户授权目录无关

### 主线 D：空回复入库 + 主动问候系统提示词入库（background_tasks.py:181-189）

```python
# background_tasks.py:181-189 当前实现
await self.db.insert_conversation_log(
    user_id=user_id,
    source=source,
    user_message=user_input,  # ← 可能是 greeting 的系统提示词
    assistant_reply=reply,    # ← call_failed 时为空字符串
    emotion_label=emotion.get("primary", ""),
    model_used=model_used,
    session_id=session_id,
    auto_commit=False,
)
```

**根因 1**：`call_failed` 时 `reply=""` 仍写入 `conversation_logs`，`agent_context.py:792-801` 注入历史时：

```python
asst_msg = row.get("assistant_reply", "")
if not user_msg and not asst_msg:
    continue  # ← 仅当两者都空才跳过；user 非空 + asst 空 仍被注入
user_preview = user_msg[:200].replace("\n", " ") if user_msg else ""
asst_preview = asst_msg[:200].replace("\n", " ") if asst_msg else ""
summaries.append(f"· [{time_str}] {term}: {user_preview} → 小妲: {asst_preview}")
# asst_preview 为空，造成"用户说了 → 小妲没回"的上下文割裂
```

**根因 2**：proactive_greeting 的系统提示词（"（场景：现在早上..."）作为 `user_input` 传入 `_process_impl`，被原样写入 `conversation_logs.user_message`，后续轮次 LLM 把它当作"用户曾对我说过的话"，导致**人格出戏 + 画像污染 + 学习评估失真**。

### 主线 E：内部提示词让 LLM 出戏

**问题表现**：
- 主动问候的"场景设定"指令被当作用户消息
- 截断续写的"请继续完成你的回复，不要重复已说的内容"被追加到 messages，下一轮 LLM 会模仿这种指令风格
- `[Search: xxx]` marker 重写后变成"请使用 web_search 工具搜索最新信息后回答：xxx"，被注入历史
- 系统 prompt 中暴露过多内部机制（"工具调用"、"DSML"、"上下文压缩"等元词汇）

---

## 三、修复方案（按优先级排序）

### 🔴 P0：截断重试去递归化（主线 A）

**目标**：消除截断重试的递归调用，限制单次请求最大 LLM 调用数 ≤ 8 次。

#### 改动 1：`model_router.py:1041-1075` 截断重试不递归

将 `self.route(...)` 改为直接调用底层 `_route_with_retry` 或新增 `_route_raw`（不带截断检测的内部入口），让截断续写**只调用一次 LLM，不再递归**。

#### 改动 2：`model_router.py:1065-1068` 修复 finish_reason 检测

让 `_handle_route_response` 返回 `(content, finish_reason)` 元组（或在截断重试中直接调用 `_route_with_retry` 拿原始 response 对象），正确判断 retry 后是否仍截断。

#### 改动 3：`model_router.py:669/691/711` fallback 链透传 max_tokens

fallback 时取 `max(original_max_tokens, fallback_config max_tokens)`，**不允许 fallback 把 max_tokens 压到 1000**。

#### 改动 4：`message_processor.py` 收敛多层重试叠加

- `MAX_VERIFICATION_TURNS` 8 → 4
- `early_retry` 3 → 1
- 首轮 `length retry` + `incomplete retry` 改为：**route 底层已做截断重试，上层不再独立做**
- 设置全局开关 `_truncation_handled`，让上层感知

#### 改动 5：`empty_content` 错误不触发 fallback

为 `empty_content` 定义专门的 `FailoverReason.EMPTY_REPLY`，映射到 `RecoveryAction.ABORT`（不重试，直接降级到 DEGRADED_REPLY），避免在同一个 provider 上重试耗尽 → fallback 链放大风暴。

#### 改动 6：修正过时注释

`message_processor.py:1709` 注释 "ROUTE_TABLE 默认值（1500）" → 改为 131072。

---

### 🔴 P0：`_describe_images` 走安全客户端路径 + 校验响应（主线 B）

**目标**：mimo-v2.5 图片识别成功率 ≥ 95%，"cannot read image" 类响应被识别为失败。

#### 改动 7：`message_processor.py:2289, 2312` 改用 `_select_client_for_provider`

```python
# 改为
client = await self.router._select_client_for_provider("mimo")  # 含锁 + 懒注册 + LLMError
```

避免与 `refresh_client` / `_rotate_credential_on_error` 竞态。

#### 改动 8：`message_processor.py:2317` 校验 vision 响应内容

```python
description = response.choices[0].message.content.strip()
# 新增：识别已知失败模式
VISION_FAILURE_PATTERNS = [
    "cannot read image", "unable to read", "i cannot read",
    "image not readable", "无法识别", "图片无法识别",
]
if (not description or len(description) < 10
        or any(p in description.lower() for p in VISION_FAILURE_PATTERNS)):
    logger.warning("agent.vision_suspicious_response", content_preview=description[:100])
    return ""  # 走兜底分支
```

#### 改动 9：捕获 `BadRequestError` 区分具体错误码

```python
try:
    response = await client.chat.completions.create(...)
except _openai_mod.BadRequestError as e:
    logger.warning("agent.vision_bad_request",
                   status=e.response.status_code if e.response else None,
                   body=str(e.body)[:200])
    return ""
```

#### 改动 10：复用主路由重试链

`_describe_images` 改用 `self.router.route(task_type="chat", messages=..., tools=None)`，自动享受 `_route_with_retry` + 凭证轮换 + fallback 链 + prompt caching。

#### 改动 11：前端区分文档上传 vs 图片上传

- 上传组件加 tab 切换："图片" / "文档"
- 图片走现有 `[Image:]` marker 路径
- 文档走 `document_reader` 工具调用路径（在 user_input 中加 `[Doc: path]` marker，后端解析后注入 tool_call 而非 vision API）

---

### 🟠 P1：前端按钮 marker 重构 + 模式状态持久化（主线 C）

**目标**：用户气泡显示纯净原文；DB 存储纯净原文；模式状态可持久化、可还原、可重试复现。

#### 改动 12：用结构化字段替代文本 marker（彻底方案）

前端 `chat.ts sendMessage` 的 WS payload 改为：

```typescript
ws.send({
  type: 'chat',
  text,                    // 纯净用户原文
  search_mode: boolean,    // 搜索互联网按钮状态
  think_mode: boolean,     // 深度思考按钮状态
  image_url: string|null,  // 图片 URL（不再拼到 text 里）
  doc_paths: string[],     // 文档路径列表
})
```

后端 `ws_hub.py _handle_chat` 接收这些字段，构造 `image_data`、设 `_search_mode` / `_think_mode`，**text 保持纯净**。

#### 改动 13：`conversation_logs` 表新增 `mode_flags` 列

```sql
ALTER TABLE conversation_logs ADD COLUMN mode_flags TEXT DEFAULT '';
-- 存储 JSON: {"search":true,"think":false,"has_image":true,"doc_paths":["..."]}
```

#### 改动 14：`web/routers/chat.py:get_messages` 返回 `mode_flags`

前端 `loadSession` 时还原 `msg.searchMode` / `msg.thinkMode`，`retryLast` 重发时带上原模式。

#### 改动 15：`agent_context.py` 增加 `cwd` 字段

```python
class AgentContext:
    def __init__(self, ...):
        ...
        self.cwd: str = ""  # 当前授权工作目录
```

`PermissionManager.set_cwd` 时同步更新（或 `_process_impl` 入口主动读取）。

#### 改动 16：`prompt_builder.py` 注入当前 cwd

系统 prompt 中增加 `<workspace>当前授权工作目录：{cwd}</workspace>`，让 LLM 知道工具调用基线。

#### 改动 17：`/workspace/confirm` 端点追加系统消息

```python
await agent_context.add_message(
    "system",
    f"[系统] 用户已切换工作目录到：{path}。后续工具调用以此目录为基线。"
)
```

让历史记录可追溯，且 LLM 感知目录变化。

#### 改动 18：`PermissionManager` 改为按 session_id 隔离

避免单例共享导致 A 切换影响 B 的工具调用基线。

#### 改动 19：过渡兼容（向后兼容）

过渡期内同时支持旧 marker 格式：`message_processor.py:421-432` 保留 `[Search:]` / `[Think:]` 解析作为兜底，但优先使用结构化字段。

---

### 🟠 P1：空回复不入库 + 主动问候不污染历史（主线 D）

**目标**：DB 中 `conversation_logs` 只存"有意义的对话"；主动问候的系统提示词不进 user_message。

#### 改动 20：`background_tasks.py:181-189` 跳过空回复

```python
# 新增守卫
if not reply or not reply.strip():
    logger.info("bg.skip_empty_reply", user_input_preview=user_input[:80])
    # 仍记录到 errors 表便于排查，但不进 conversation_logs
    await self.db.insert_error_log(...)
    return
```

#### 改动 21：`agent_context.py:792-801` 注入时跳过空 assistant_reply

```python
asst_msg = row.get("assistant_reply", "")
if not user_msg:
    continue
if not asst_msg:  # ← 新增：空回复不注入历史摘要
    continue
```

#### 改动 22：proactive_greeting 走独立通道

主动问候不应经过 `_process_impl` 主流程。新增 `greeting_channel.py`：

- 系统提示词作为 system message 注入，不作为 user message
- 问候生成后直接发送给用户，**不写入 `conversation_logs.user_message`**
- 仅在 `greeting_log` 表记录"问候已发送"
- 后续轮次 LLM 看到的是"小妲主动说了：xxx"（作为 assistant 消息），而非"用户对我说：场景设定..."

#### 改动 23：截断续写的"请继续"指令不入历史

`model_router.py:1052` 的 `"请继续完成你的回复，不要重复已说的内容"` 仅作为**临时 retry messages**，不写入 `context.history`（当前实现已是临时 copy，但需确认 retry 后的合并内容不包含此指令）。

---

### 🟡 P2：内部提示词收敛（主线 E）

**目标**：LLM 看不到任何"工具调用"、"DSML"、"上下文压缩"等元词汇，专注于角色扮演。

#### 改动 24：系统 prompt 移除元词汇

审查 `prompt_builder.py` 全文，将"工具调用"、"DSML 协议"、"上下文压缩"、"记忆编码"等内部机制词汇改为角色化表达（如"小妲可以通过世界树根系感知..."）。

#### 改动 25：截断续写指令改为角色化

```python
# 改为
retry_messages.append({"role": "user", "content": "（继续说完）"})
# 或
retry_messages.append({"role": "system", "content": "继续生成未完成的回复"})
```

#### 改动 26：`[Search:]` 模式不再重写 user_input

改为通过 system prompt 注入或 tool-forcing 逻辑（`_search_mode` 标志已存在，在 LLM 调用前拼到 system message：`"本次回复请使用 web_search 工具搜索最新信息后回答。"`）。

#### 改动 27：上下文压缩摘要角色化

`agent_context.py:208-210` 的 `"上下文压缩"` 关键词改为 `"历史回顾"` 或 `"过往点滴"`，让 LLM 看到的是角色化的总结而非技术性描述。

---

### 🟡 P2：跨渠道上下文一致性

**目标**：QQ / Web / CLI 三渠道的上下文管理统一规范，避免渠道差异导致的 bug。

#### 改动 28：统一 max_tokens 配置入口

`WEB_UI_MAX_TOKENS` 改为 `CHANNEL_MAX_TOKENS` JSON 配置：

```json
{
  "web": 32768,
  "qq_c2c": 1500,
  "qq_group": 1000,
  "cli": 8192
}
```

`message_processor.py:1706-1712` 读取此配置，QQ 通道按平台限制设置（避免超长回复被截断）。

#### 改动 29：渠道感知的场景标识

`agent_context.py:36-48` 的 `_SCENE_HINTS` 扩展为完整的渠道策略表，包含：
- 允许的 max_tokens
- 允许的工具集
- 回复长度建议
- 隐私等级

#### 改动 30：群聊多用户上下文隔离强化

`agent_context.py:109-114` 已有 `_user_histories` / `_user_summaries` / `_user_buffers`，需验证：
- 群聊场景下 `switch_user_context` 被正确调用
- 凭证池、画像、记忆检索按用户隔离
- 工具调用结果不串味

---

## 四、非目标（明确不做）

1. **不重写整个上下文管理子系统**——只修复具体缺陷，保持架构稳定
2. **不引入新的依赖**——使用项目已有的 loguru / aiosqlite / openai 库
3. **不修改 QQ Bot 协议层**——只改 AgentCore 层
4. **不删除已有功能**——`/compress` 斜杠命令、`switch_user_context` 等保留
5. **不强制迁移历史数据**——`mode_flags` 列默认空字符串，旧记录按原逻辑处理

---

## 五、验收标准（与 checklist.md 对齐）

### 5.1 日志侧

- [ ] 连续 1 小时内 `llm.truncated_retry_success` 出现次数 ≤ 5
- [ ] `router.retry_exhausted` + `llm.call_failed` + `router.*_fallback` 链路触发频率 ≤ 1 次/10 分钟
- [ ] 无 `agent.vision_suspicious_response` WARNING（或出现时已正确走兜底）

### 5.2 数据库侧

- [ ] `conversation_logs.assistant_reply` 为空的记录数 ≤ 总数 1%
- [ ] `conversation_logs.user_message` 不含 `（场景：` / `请使用 web_search 工具` / `[Image:` / `[Search:` / `[Think:` 等 marker
- [ ] `mode_flags` 列正确持久化模式状态

### 5.3 用户体验侧

- [ ] 用户气泡显示纯净原文，无 `[Search:]` / `[Think:]` / `[Image:]` marker
- [ ] 重试按钮点击后行为与首次发送一致（模式状态可还原）
- [ ] mimo-v2.5 上传图片不再出现 "cannot read image"
- [ ] 切换工作目录后，LLM 后续回复能感知新目录
- [ ] 主动问候不再让 LLM 出戏
- [ ] 单次请求 LLM 调用次数 ≤ 8

### 5.4 成本侧

- [ ] 单次请求平均 LLM 调用次数下降 ≥ 60%
- [ ] 单次请求平均 token 消耗下降 ≥ 30%（消除重试风暴的浪费）

---

## 六、风险评估

### 6.1 高风险

- **改动 1（截断重试去递归）**：可能影响长回复的完整性。**缓解**：保留单层 2 轮重试，仅消除递归；增加 `incomplete_reply` 检测兜底。
- **改动 12（结构化字段替代 marker）**：前后端协议变更，需同步发布。**缓解**：改动 19 保留旧 marker 兼容。

### 6.2 中风险

- **改动 15-18（工作目录上下文衔接）**：`PermissionManager` 改为按 session 隔离可能影响现有授权流程。**缓解**：保留全局默认 cwd，session 级覆盖。
- **改动 22（proactive_greeting 独立通道）**：可能影响问候的个性化能力。**缓解**：问候生成仍用主 LLM，仅不写入 conversation_logs。

### 6.3 低风险

- 改动 6/25/26/27（注释/提示词角色化）：纯文本修改，无逻辑变更。
- 改动 20/21（空回复不入库）：仅增加守卫，不影响正常流程。

---

## 七、回滚策略

1. 每个改动独立 commit，便于二分回滚
2. 关键改动（1/12/22）增加 feature flag：
   - `TRUNCATION_RETRY_DERECURSE=true`
   - `USE_STRUCTURED_MODE_FLAGS=true`
   - `GREETING_INDEPENDENT_CHANNEL=true`
3. 出问题时设为 `false` 即可回退到旧行为

---

## 八、参考文件

- `/home/orangepi/.ai-agent/proj/model_router.py`（截断重试 line 1041-1075，fallback 链 line 644-719，ROUTE_TABLE line 79-95，`_select_client_for_provider` line 792-816）
- `/home/orangepi/.ai-agent/proj/agent_core/message_processor.py`（`_describe_images` line 2286-2322，`_run_verification_loop` line 179-411，marker 解析 line 421-432，max_tokens 解析 line 1683-1720）
- `/home/orangepi/.ai-agent/proj/agent_context.py`（历史注入 line 792-801，群聊隔离 line 109-148）
- `/home/orangepi/.ai-agent/proj/core/background_tasks.py`（`insert_conversation_log` line 181-189）
- `/home/orangepi/.ai-agent/proj/web/ws_hub.py`（`[Image:]` 提取 line 573-595）
- `/home/orangepi/.ai-agent/proj/web/routers/workspace.py`（`/workspace/confirm` line 81-99）
- `/home/orangepi/.ai-agent/proj/web/frontend/src/views/ChatView.vue`（marker 拼装 line 168-181）
- `/home/orangepi/.ai-agent/proj/web/frontend/src/stores/chat.ts`（displayText 剥离 line 235-253，retryLast line 293-309）
- `/home/orangepi/.ai-agent/proj/utils/credential_pool.py`（`error_no_state_change` line 181）
- `/home/orangepi/.ai-agent/proj/utils/error_classifier.py`（UNKNOWN 分类 line 193）
