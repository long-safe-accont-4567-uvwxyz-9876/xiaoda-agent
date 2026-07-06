# xiaoda-agent v0.4.25 缺陷扫描证据文件

完成日期：2026-07-01

---

## D1 — db_memory.py 重复定义 get_recent_conversations

```
Source: File: db/db_memory.py
Excerpt (第一次定义, L76-93):
    async def get_recent_conversations(self, limit: int = 20, user_id: str = "") -> Any:
        """获取最近的对话记录。支持按 user_id 过滤（群聊场景下隔离不同用户的历史）。"""
        if user_id:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   WHERE user_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM conversation_logs
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]   ← 正序

Excerpt (第二次定义, L110-133):
    async def get_recent_conversations(self, limit: int = 20, user_id: str = "") -> Any:
        """获取最近的对话记录。支持按 user_id 过滤（群聊场景下隔离不同用户的历史）。"""
        # ... 相同查询逻辑 ...
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]   ← 倒序

Context: Python 类中同名方法后者覆盖前者，第一个定义是死代码
Confidence: HIGH
```

---

## D2 — MCP _request() 对 SSE/HTTP 始终返回 None

```
Source: File: tool_engine/mcp_client.py
Excerpt (L350-355):
    async def _request(self, msg: dict, timeout: float = 30.0) -> dict | None:
        """Send a JSON-RPC request and wait for the response."""
        if not self._process or self._process.returncode is not None:
            return None   ← SSE/HTTP 模式 self._process 始终为 None，永远走这个分支

Context: _connect_sse() 和 _connect_http() 不创建 self._process，只创建 self._http_client。
         但 _request() 只实现了 stdio 传输的读写逻辑，对 HTTP 传输未实现。
Confidence: HIGH
```

---

## D3 — IMApprovalChannel 并发审批请求覆盖

```
Source: File: security/human_approval.py
Excerpt (L219-231):
    async def request_approval(self, req: ApprovalRequest, is_owner: bool = False) -> ApprovalStatus:
        # ...
        self._pending[req.user_id] = req        ← 用 user_id 作 key
        self._waiters[req.user_id] = future     ← 用 user_id 作 key

Context: 当同一用户触发第二个高危操作时，self._pending[user_id] 被覆盖，
         第一个请求的 Future 永远不会被 resolve（直到超时）。
         QQ 机器人场景下主人是唯一的 user_id，此问题极易触发。
Confidence: HIGH
```

---

## D4 — setup/keys API 返回 raw_value 明文

```
Source: File: web/routers/setup.py
Excerpt (L160-185):
    for item in REQUIRED_KEYS:
        key = item["key"]
        val = current.get(key, "")
        keys.append({
            "key": key,
            # ...
            "masked_value": _mask_key_value(val),
            "raw_value": val,          ← 明文 API Key
        })

Context: 虽然端点有认证保护，但 API Key 明文通过 HTTP 响应传输增加了泄露风险。
         前端只需要知道 Key 是否已配置（configured: bool）和脱敏展示值。
Confidence: HIGH
```

---

## D5 — CURRENT_SCHEMA_VERSION 与实际迁移不匹配

```
Source: File: db/database.py
Excerpt (L27):
    CURRENT_SCHEMA_VERSION = 9

Excerpt (L199-202):
    migrations = [
        # ...
        (9, "memory_summaries+episodic_memories.distilled", self._migrate_v9),
        (10, "episodic_memories.entities+event_type+metadata_json", self._migrate_v10),
        (11, "memory_recall_notes", self._migrate_v11),
    ]

Context: CURRENT_SCHEMA_VERSION=9 但迁移列表实际到 v11。_run_migrations 不依赖此常量，
         但如果其他代码引用此常量判断 schema 是否最新，会误判。
Confidence: HIGH
```

---

## D7 — CredentialPool _find_active_credential 凭证追踪不精确

```
Source: File: utils/credential_pool.py
Excerpt (L200-212):
    def _find_active_credential(self, provider: str) -> Credential | None:
        """找到最近使用的活跃凭证（ok 或 exhausted 状态）"""
        creds = self._pool.get(provider, [])
        if not creds:
            return None
        active = [c for c in creds if c.state != CredentialState.DEAD]
        if not active:
            return None
        return max(active, key=lambda c: c.last_used_at)

Context: 使用 last_used_at 时间戳匹配"最近使用的凭证"。在高并发场景下，
         多个请求几乎同时获取不同凭证时，时间精度不足可能导致错误匹配。
Confidence: MEDIUM
```

---

## D8 — 私聊自动绑定第一个用户为主人

```
Source: File: qq_bot_adapter.py
Excerpt (L518-521):
    # 私聊自动绑定：首次私聊自动将发送者绑定为主人
    if not is_master and user_openid and not master_ids:
        _save_master_openid(user_openid)
        is_master = True

Context: 当 MASTER_QQ_OPENID 为空时，第一个私聊的用户自动成为主人。
         公开部署场景下，陌生人可以先发私聊获取主人权限。
Confidence: HIGH
```

---

## D9 — SSRF DNS Pinning 缓存无过期

```
Source: File: security/ssrf_guard.py
Excerpt (L82-83):
    _PIN_CACHE: dict[str, str] = {}
    _PIN_CACHE_TTL = 60.0  # 锁定有效期 (秒)

Excerpt (L220):
    _PIN_CACHE[hostname.lower()] = pinned_ip   ← 写入时未记录时间戳

Excerpt (L245):
    cached = _PIN_CACHE.get(hostname)   ← 读取时未检查 TTL

Context: _PIN_CACHE_TTL 被定义但从未使用。缓存写入时不记录时间戳，
         读取时不检查过期，导致 DNS 记录变更后仍使用旧 IP。
Confidence: HIGH
```

---

## D10 — 斜杠命令无权限控制

```
Source: File: slash_commands.py
Excerpt (L6):
    OWNER_ONLY_COMMANDS: set[str] = set()  # 所有命令均不设权限限制

Context: /reset、/sys、/forget 等高危命令对非主人也完全开放。
         群聊中任何用户 @bot 都可执行这些命令。
Confidence: HIGH
```

---

## D11 — 迁移事务与 dirty state 共享连接

```
Source: File: db/database.py
Excerpt (L207-238):
    async def _apply_migration(self, version, description, migrate_fn):
        # 标记 dirty（独立事务）
        await self._conn.execute("UPDATE migration_state SET dirty = 1 ...")
        await self._conn.commit()

        try:
            await self._conn.execute("BEGIN TRANSACTION")
            await migrate_fn()   ← 内部可能调用 executescript（隐式 commit）
            # ...

Context: aiosqlite 的 executescript 会隐式 commit，可能破坏事务边界。
         如果 migrate_fn 中途失败但部分 DDL 已通过 executescript 提交，
         ROLLBACK 只能回滚未提交的部分，数据库可能处于半迁移状态。
Confidence: MEDIUM
```

---

## D18 — "大前天"映射错误

```
Source: File: memory/memory_manager.py
Excerpt (L41):
    _TEMPORAL_PATTERNS = [
        (re.compile(r"前天|大前天"), 2, 1),       # 前天那一天
        (re.compile(r"昨天|昨日"), 1, 1),
        # ...

Context: "大前天"应该是 3 天前，但正则将"大前天"和"前天"合并为同一个模式，
         都映射到 offset=2（2 天前）。用户问"大前天"会得到"前天"的记忆。
Confidence: HIGH
```
