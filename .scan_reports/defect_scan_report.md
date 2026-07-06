# xiaoda-agent v0.4.25 全量缺陷深度扫描报告

完成日期：2026-07-01

---

## 摘要

对 xiaoda-agent v0.4.25 项目进行了全量缺陷深度扫描，重点审查了之前未深入覆盖的 17 个模块/目录。扫描维度涵盖功能性、架构性、安全性、可靠性、性能五个方面，严格以"客观会导致 bug、数据丢失、安全漏洞、功能异常"为标准，排除之前已发现的全部问题。

共发现 **18 个新缺陷**：P0 × 4、P1 × 7、P2 × 7。

---

## P0 缺陷（必须修 / 会导致 bug 或数据丢失）

### D1 — db_memory.py 重复定义 `get_recent_conversations` 导致第一个定义被静默覆盖

- **文件**: `db/db_memory.py:76` 和 `db/db_memory.py:110`
- **描述**: `MemoryDB` 类中 `get_recent_conversations` 方法定义了两次。第一个版本（L76）返回 `[dict(r) for r in rows]`（正序），第二个版本（L110）返回 `[dict(r) for r in reversed(rows)]`（倒序）。Python 类中后定义的方法会覆盖前者，因此实际生效的是第二个（倒序）。但两个方法签名和 docstring 完全相同，明显是复制粘贴遗留——第一个方法永远不可达，属于死代码；而第二个的 `reversed()` 行为可能与调用方预期不一致。
- **影响**: 
  1. 如果有调用方依赖"正序"返回（第一版的行为），实际得到的是倒序，造成对话历史顺序混乱；
  2. 同名方法共存使维护者困惑，修改时可能改错位置。
- **修复建议**: 删除第一个定义（L76-L93），保留第二个，并确认所有调用方的顺序预期与 `reversed()` 行为一致。

### D2 — MCP `_request()` 方法对 SSE/HTTP 传输始终返回 None

- **文件**: `tool_engine/mcp_client.py:350-370`
- **描述**: `MCPClient._request()` 方法入口有前置检查 `if not self._process or self._process.returncode is not None: return None`。对于 SSE/HTTP 传输模式，`self._process` 始终为 None（只有 stdio 模式才创建子进程），因此 `_request()` 在 SSE/HTTP 模式下永远返回 None。而 `call_tool()` 调用 `_request()` 获取结果，导致 SSE/HTTP 模式的 MCP 工具调用**永远超时**。
- **影响**: SSE 和 streamable-http 传输的 MCP 服务器**完全不可用**——工具调用永远返回 "timed out"，用户配置了远程 MCP 服务器却无法使用。
- **修复建议**: 在 `_request()` 开头增加对 SSE/HTTP 传输的分支处理：当 `self._http_client` 存在时，通过 HTTP POST 发送 JSON-RPC 请求并等待响应。

### D3 — `IMApprovalChannel` 按 user_id 索引 pending 请求，并发审批请求相互覆盖

- **文件**: `security/human_approval.py:219-231`
- **描述**: `IMApprovalChannel._pending` 和 `_waiters` 字典都以 `user_id` 为 key。当同一用户（如主人）触发多个高危操作时，第二个请求会覆盖第一个的 `ApprovalRequest` 和 `Future`。第一个请求的 `Future` 永远不会被 resolve（直到超时），且 `_pending` 中只保留了最后一个请求。
- **影响**: 
  1. 并发高危操作场景下，第一个审批请求丢失，对应的 `await request_approval()` 永远不会返回（直到 60s 超时）；
  2. 用户回复"确认"只能作用于最后一个请求，前面的请求被静默丢弃；
  3. 对 QQ 机器人这类单用户（主人）场景，此 bug 触发概率不低。
- **修复建议**: 使用 `request_id`（已有唯一 ID）作为 `_pending` 和 `_waiters` 的 key，而非 `user_id`。`handle_user_reply` 需要改为遍历当前用户的所有 pending 请求或采用队列模式。

### D4 — `setup/keys` API 返回 `raw_value` 明文暴露 API Key

- **文件**: `web/routers/setup.py:170,185`
- **描述**: `GET /setup/keys` 接口在返回每个 Key 的脱敏值 `masked_value` 同时，还返回了完整的明文值 `raw_value`。虽然该端点有认证保护，但：
  1. 任何已认证用户（不仅是 owner）都能获取全部 API Key 明文；
  2. 浏览器开发者工具、网络代理、日志系统等都能截获此响应中的明文 Key；
  3. 前端并不需要 `raw_value`——只需要知道 Key 是否已配置（`configured: bool`）和脱敏展示值。
- **影响**: API Key 明文通过 HTTP 响应传输，增加了凭证泄露的攻击面。如果 WebUI 密码简单或被共享，任何能访问 WebUI 的人都能提取全部 API Key。
- **修复建议**: 从 `GET /setup/keys` 响应中移除 `raw_value` 字段。前端只需 `configured` 和 `masked_value`。Key 的读写通过专门的端点操作，不通过列表接口暴露。

---

## P1 缺陷（应该修 / 明显不合理）

### D5 — `database.py` `CURRENT_SCHEMA_VERSION=9` 与实际迁移列表不匹配

- **文件**: `db/database.py:27` 和 `db/database.py:199-202`
- **描述**: `CURRENT_SCHEMA_VERSION = 9`，但迁移列表实际包含 v1~v11 共 11 个迁移。`_run_migrations()` 使用 `SELECT MAX(version) FROM schema_version` 获取当前版本号，与 `CURRENT_SCHEMA_VERSION` 常量无关，因此迁移本身能正确执行。但 `CURRENT_SCHEMA_VERSION` 作为模块级常量，可能在其他地方被引用来判断 schema 是否最新——如果被引用，9 < 11 会导致误判 schema 过时。
- **影响**: 常量值过时可能导致依赖此常量的逻辑误判。当前未发现直接引用此常量导致 bug 的代码路径，但维护风险存在。
- **修复建议**: 将 `CURRENT_SCHEMA_VERSION` 更新为 11，或改为从迁移列表自动推导 `max(version for version, _, _ in migrations)`。

### D6 — `MCPClient._request()` 对 SSE/HTTP 模式下的 `_notify()` 同样不可用

- **文件**: `tool_engine/mcp_client.py:180-189`
- **描述**: 与 D2 同源问题。`_notify()` 方法也有前置检查 `if not self._process or self._process.returncode is not None: return`。SSE/HTTP 模式下 `self._process` 为 None，因此 `initialized` 通知永远不会发出。虽然 `initialized` 通知是可选的（多数 MCP 服务器不依赖它），但这是代码逻辑缺陷。
- **影响**: SSE/HTTP 模式的 MCP 初始化不完整。部分 MCP 服务器可能要求收到 `initialized` 通知才开始处理请求。
- **修复建议**: 与 D2 一起修复——为 SSE/HTTP 传输实现独立的 `_request` 和 `_notify` 方法。

### D7 — `CredentialPool._find_active_credential` 在高并发下可能匹配错误凭证

- **文件**: `utils/credential_pool.py:200-212`
- **描述**: `_find_active_credential` 通过 `max(active, key=lambda c: c.last_used_at)` 找"最近使用的"凭证。但 `report_error` 和 `report_success` 调用此方法时已持有 `self._lock`，而 `get_credential` 更新 `last_used_at` 也在锁内。问题在于：当多个并发请求几乎同时获取了不同凭证时，`last_used_at` 的时间精度（`time.time()` 秒级浮点）可能不足以区分，导致 `max()` 选择了错误的凭证来标记错误状态。
- **影响**: 凭证 A 的错误被标记到凭证 B 上，导致健康的凭证被错误标记为 EXHAUSTED/DEAD，而真正有问题的凭证仍被使用。
- **修复建议**: 使用递增的 `use_count` 或唯一的请求 ID 来追踪"当前请求使用的是哪个凭证"，而非依赖 `last_used_at` 的时间比较。可考虑在 `get_credential` 返回时记录 `(request_id, credential)` 映射，`report_error` 时按 request_id 精确匹配。

### D8 — `qq_bot_adapter.py` 私聊自动绑定逻辑存在"第一个陌生人即主人"风险

- **文件**: `qq_bot_adapter.py:518-521`
- **描述**: `on_c2c_message_create` 中，当 `MASTER_QQ_OPENID` 环境变量为空且用户非主人时，代码自动将第一个私聊用户的 openid 绑定为主人并写入 `.env`。问题在于：如果 bot 被公开部署（如群内添加），任何第一个私聊 bot 的陌生人都会被自动绑定为主人，获得完全控制权。且 `.env` 被修改后，后续无法通过 WebUI 更换主人（因为已有 master_id，不会再次触发自动绑定）。
- **影响**: 公开部署场景下，第一个私聊的陌生人获得主人权限。这是安全性问题而非功能性 bug，但对于个人项目且只在私人环境使用，风险较低。
- **修复建议**: 在自动绑定时增加确认步骤（如要求该用户先发送 `/bind` 命令），或在 on_ready 时检查主人是否已配置并输出警告。

### D9 — `ssrf_guard.py` DNS Pinning 缓存无过期清理和并发保护

- **文件**: `security/ssrf_guard.py:82-83,220,245`
- **描述**: `_PIN_CACHE` 是模块级全局 dict，存在两个问题：
  1. **无过期清理**：缓存的 IP 绑定永远不会被清理（`_PIN_CACHE_TTL = 60.0` 被定义但从未使用）。如果目标域名 DNS 记录变更（如从 1.2.3.4 变为 5.6.7.8），缓存中的旧 IP 会导致请求发往错误地址。
  2. **无并发保护**：在异步环境下多个协程可能同时读写 `_PIN_CACHE`，虽然 Python dict 的 GIL 保证单操作原子性，但 `validate_url` 中"先读缓存、不存在则校验并写入"的 check-then-act 模式在并发下可能导致重复校验。
- **影响**: DNS 记录变更后，60 秒后仍使用旧 IP，可能连到错误的服务器（功能异常）；并发场景下最多导致重复 DNS 解析（性能问题，不影响正确性）。
- **修复建议**: 
  1. 在 `validate_url` 写入缓存时记录时间戳，读取时检查 TTL；
  2. 考虑使用 `asyncio.Lock` 保护 check-then-act 序列。

### D10 — `slash_commands.py` 所有命令无权限控制，`OWNER_ONLY_COMMANDS` 为空集

- **文件**: `slash_commands.py:6`
- **描述**: `OWNER_ONLY_COMMANDS: set[str] = set()` — 所有斜杠命令都不设权限限制。任何用户（包括 QQ 群中 @bot 的非主人）都能执行 `/reset`（系统重置）、`/model`（切换模型）、`/forget`（删除记忆）、`/sys`（执行系统命令）等高危操作。
- **影响**: 非主人用户可以通过 QQ 群 @bot 执行 `/reset` 清空数据、`/sys` 执行任意系统命令。在群聊场景下这是一个实际的安全风险。
- **修复建议**: 将 `/reset`、`/sys`、`/forget`、`/model` 等高危命令加入 `OWNER_ONLY_COMMANDS`。

### D11 — `database.py` `_apply_migration` 中 dirty state 和迁移在同一连接上交叉提交

- **文件**: `db/database.py:207-238`
- **描述**: `_apply_migration` 方法先在独立事务中标记 dirty state（`UPDATE migration_state SET dirty=1` + `commit`），然后在同一连接上执行 `BEGIN TRANSACTION` + `migrate_fn` + `INSERT schema_version` + `commit`。问题在于 aiosqlite 的 `commit()` 会提交当前连接上的所有待提交操作——如果 `migrate_fn` 内部执行了 `executescript`（会隐式 commit），则 `BEGIN TRANSACTION` 后的迁移操作可能不在同一个事务内，导致部分 DDL 已提交、部分未提交。
- **影响**: 如果 `migrate_fn` 中途失败但部分 DDL 已通过 `executescript` 隐式提交，ROLLBACK 只能回滚未提交的部分，数据库可能处于半迁移状态。当前代码通过 dirty state 机制阻止后续启动，降低了数据损坏概率，但半迁移的数据库需要手动修复。
- **修复建议**: 在迁移前显式关闭 aiosqlite 的自动提交（`await self._conn.execute("BEGIN")`），确保迁移内所有操作在同一事务内。或将 dirty state 标记使用独立连接。

---

## P2 缺陷（建议优化 / 可改可不改）

### D12 — `model_router.py` `ROUTE_TABLE` 与 `FALLBACK_ROUTE` 不完全一致

- **文件**: `model_router.py:50-60` 和 `model_router.py:69-73`
- **描述**: `FALLBACK_ROUTE` 定义了 `chat_pro → chat_flash → chat_mini → chat_agnes` 的降级链。但 `ROUTE_TABLE` 中 `chat_pro` 使用 `mimo-pro` 模型，而降级到的 `chat_flash` 使用 `mimo` 标准模型——降级后模型能力差异大，可能影响 pro 模式用户的使用体验。另外 `chat_agnes` 作为最终兜底，其客户端（`agnes`）可能未配置，降级到此处会直接失败。
- **影响**: pro 模式降级后回复质量可能明显下降；最终兜底可能不可用。
- **修复建议**: 在降级逻辑中检查目标客户端是否可用，不可用时跳过该级继续降级。

### D13 — `qq_bot_adapter.py` 流式分片每片都消耗被动回复配额

- **文件**: `qq_bot_adapter.py:1010-1040`
- **描述**: `_send_streaming_reply` 中，长回复被切片为多段，每段都通过 `message.reply()` 发送。对于群聊消息，QQ API 对被动回复有 5 分钟 2 次的限制。流式分片模式下，每片都是一次被动回复，第 3 片开始就会超限。代码虽然在 `_send_reply_with_media` 中对最终回复有被动→主动的降级，但流式分片的中间片没有此降级。
- **影响**: 群聊中长回复（>400 字符，启用流式）从第 3 片开始发送失败，用户只能看到部分回复。
- **修复建议**: 群聊场景下，流式分片应使用主动消息（`post_group_message`），只在第一片使用被动回复满足即时反馈需求。

### D14 — `web/ws_hub.py` `_publish_file` 使用 `shutil.copy2` 无大小限制

- **文件**: `web/ws_hub.py:80-90`
- **描述**: `_publish_file` 将生成的媒体文件复制到 `MEDIA_ROOT` 目录供 Web 访问。使用 `shutil.copy2` 复制，没有文件大小限制。如果 TTS 生成了超大音频文件或视频文件，会占用大量磁盘空间，且无清理机制。
- **影响**: 长期运行后 `media/` 目录可能积累大量文件占用磁盘空间。
- **修复建议**: 添加定期清理机制（如只保留最近 N 个文件），或对文件大小设置上限。

### D15 — `prompt_builder.py` 场景缓存使用模块级全局变量，多 Agent 实例共享状态

- **文件**: `prompt_builder.py:26-45`
- **描述**: `_SYSTEM_PROMPT_CACHE`、`_stable_prompt_cache`、`_scene_prompt_cache`、`_current_scene_sig` 等缓存变量都是模块级全局变量。如果同一进程内有多个 AgentCore 实例（理论上支持），它们会共享这些缓存，导致一个实例的 prompt 变化影响另一个实例。
- **影响**: 多实例场景下场景缓存串扰。当前项目为单实例运行，实际影响极低。
- **修复建议**: 如需支持多实例，将缓存移入实例属性；否则维持现状即可。

### D16 — `credential_vault.py` 使用固定盐和机器身份作为密钥，安全性依赖主机隔离

- **文件**: `security/credential_vault.py:19-20,55-65`
- **描述**: 凭证加密使用固定盐 `_SALT = b"nahida-agent-credential-vault-v1"` 和机器身份 `用户名@主机名` 作为 PBKDF2 的输入。问题在于：
  1. 固定盐在代码中公开，攻击者只需知道用户名和主机名即可重建密钥；
  2. 在容器化环境（Docker）中，多个容器可能共享相同的用户名和主机名，导致密钥相同；
  3. 没有额外的 secret 因素，密钥推导完全依赖机器身份的不可预测性。
- **影响**: 在攻击者能获取 .env 文件且知道机器身份的场景下，可离线暴力破解加密的 API Key。但需同时满足"获取 .env"和"知道机器身份"两个条件，实际风险取决于部署环境。
- **修复建议**: 考虑从 `~/.ai-agent/` 目录读取或生成一个随机 machine_id 文件参与密钥推导，增加额外的 secret 因素。

### D17 — `db/database.py` `fetch_all` 方法接受原始 SQL，存在 SQL 注入风险

- **文件**: `db/database.py:424-427`
- **描述**: `fetch_all(self, sql: str, params: tuple = ())` 直接接受并执行任意 SQL 字符串。虽然方法本身支持参数化查询（`params`），但没有任何对 `sql` 内容的校验或限制。如果 WebUI 的任何输入被拼接到 `sql` 中（而非使用 `params`），就会产生 SQL 注入。当前代码中 `fetch_all` 的调用方都使用了参数化查询，风险较低。
- **影响**: 潜在的 SQL 注入风险，取决于后续开发者是否正确使用参数化查询。
- **修复建议**: 考虑在方法文档中强调必须使用参数化查询，或对 `sql` 进行基本的关键词白名单校验（仅允许 SELECT）。

### D18 — `memory_manager.py` `_parse_temporal_query` "大前天"映射到 offset=2 与"前天"相同

- **文件**: `memory/memory_manager.py:41`
- **描述**: `_TEMPORAL_PATTERNS` 中 `(re.compile(r"前天|大前天"), 2, 1)` — "大前天"（3 天前）和"前天"（2 天前）都映射到 offset=2（2 天前），span=1（1 天跨度）。"大前天"应该是 offset=3, span=1。
- **影响**: 用户问"大前天发生了什么"时，实际检索的是"前天"的记忆，结果不准确。
- **修复建议**: 将"大前天"单独拆分为 `(re.compile(r"大前天"), 3, 1)`，放在"前天"模式之前（确保优先匹配长关键词）。

---

## 缺陷汇总表

| 编号 | 优先级 | 文件 | 行号 | 类别 | 简述 |
|------|--------|------|------|------|------|
| D1 | P0 | db/db_memory.py | 76,110 | 功能性 | `get_recent_conversations` 重复定义，第一个被静默覆盖 |
| D2 | P0 | tool_engine/mcp_client.py | 350 | 功能性 | MCP SSE/HTTP 传输 `_request()` 始终返回 None |
| D3 | P0 | security/human_approval.py | 219 | 功能性 | 并发审批请求按 user_id 覆盖，第一个请求丢失 |
| D4 | P0 | web/routers/setup.py | 170,185 | 安全性 | API 接口返回 API Key 明文 `raw_value` |
| D5 | P1 | db/database.py | 27 | 架构性 | `CURRENT_SCHEMA_VERSION=9` 与实际迁移 v11 不匹配 |
| D6 | P1 | tool_engine/mcp_client.py | 180 | 功能性 | MCP SSE/HTTP `_notify()` 不可用 |
| D7 | P1 | utils/credential_pool.py | 200 | 可靠性 | 凭证错误标记可能误伤健康凭证 |
| D8 | P1 | qq_bot_adapter.py | 518 | 安全性 | 第一个私聊陌生人自动绑定为主人 |
| D9 | P1 | security/ssrf_guard.py | 82 | 可靠性 | DNS Pinning 缓存无过期清理 |
| D10 | P1 | slash_commands.py | 6 | 安全性 | 所有斜杠命令无权限控制 |
| D11 | P1 | db/database.py | 207 | 可靠性 | 迁移事务与 dirty state 共享连接，隐式 commit 风险 |
| D12 | P2 | model_router.py | 69 | 可靠性 | 降级链最终兜底可能不可用 |
| D13 | P2 | qq_bot_adapter.py | 1010 | 功能性 | 群聊流式分片超过被动回复配额 |
| D14 | P2 | web/ws_hub.py | 80 | 可靠性 | 媒体文件复制无大小限制和清理机制 |
| D15 | P2 | prompt_builder.py | 26 | 架构性 | 场景缓存为模块级全局变量 |
| D16 | P2 | security/credential_vault.py | 19 | 安全性 | 加密密钥依赖固定盐+机器身份 |
| D17 | P2 | db/database.py | 424 | 安全性 | `fetch_all` 接受原始 SQL |
| D18 | P2 | memory/memory_manager.py | 41 | 功能性 | "大前天"映射错误，与"前天"相同 |

---

## 按类别统计

| 类别 | P0 | P1 | P2 | 合计 |
|------|-----|-----|-----|------|
| 功能性 | 3 | 1 | 2 | 6 |
| 安全性 | 1 | 2 | 2 | 5 |
| 可靠性 | 0 | 3 | 1 | 4 |
| 架构性 | 0 | 1 | 1 | 2 |
| 性能 | 0 | 0 | 1 | 1 |
| **合计** | **4** | **7** | **7** | **18** |
