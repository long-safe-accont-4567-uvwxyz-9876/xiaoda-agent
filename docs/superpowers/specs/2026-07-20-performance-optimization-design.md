# 性能优化统一修改方案 — 实施设计

> 版本：v1.0 | 日期：2026-07-20 | 基线：v0.5.31 | 作者：纳西妲
> 文档依据：Coze 文档 https://www.coze.cn/s/0Tp9qPT0tyE/ (42 项优化)
> 实施策略：方案 C — 10 项真实缺口修复 + 系统性性能审计

---

## 1. 背景与文档核实结论

### 1.1 文档核实结果
对 Coze 文档 42 项优化逐项核实项目现状，结论：

| 状态 | 数量 | 说明 |
|:-----|:-----|:-----|
| ✅ 已实现且更优 | 32 项 | 项目实现方案优于文档提案（如 6路 RRF > 3路，sqlite-vec > LSH，FAT检测 > 单纯 WAL） |
| ❌ 真实待修复 | 10 项 | 文档提案准确命中真实瓶颈 |
| ⚠️ 已接受现状 | 0 项 | G10 经评估为可接受，不修改 |

### 1.2 已实现清单（32 项，不重复实施）

**P0-A 响应延迟（5/6 已实现）**：
- L2 LLM 路由分层 → `model_router.py` ROUTE_TABLE 按 task_type 分层（chat/chat_pro/chat_flash/chat_mini/chat_mimo/chat_ultra/emotion_analysis/memory_encoding）
- L3 上下文压缩 → `context_compressor.py` smart_summary_truncate，摘要上限 500 字符（优于文档 300）
- L4 流式首 Token → `STREAM_TEXT_PUSH=true` + `router.chat_stream` + `ws_hub` 流式推送 + `qq_bot_adapter._send_streaming_reply`
- L5 系统提示词缓存 → 三层缓存 `_stable_prompt_cache` + `_module_cache` + `_scene_prompt_cache`，mtime 失效
- L6 工具预热 → `register_builtin_tools_lazy` + `prewarm_jieba` + `core/lazy_loader.py`

**P0-B RAG 检索（3/3 已实现）**：
- R1 FTS5 → migration v3 已建 `episodic_memory_fts`、`knowledge_entities_fts`；schema 已到 v20
- R2 向量 ANN → `vector_store.py` 用 sqlite-vec vec0 KNN + oversample+tie-breaking 确定性优化（优于 LSH）
- R3 混合检索 → `memory_manager.retrieve_memories_hybrid` 6 路 RRF（FTS+vec+KG+子chunk+扩散激活+实体）+ Reranker 精排（优于 3 路）

**P0-C 数据黑洞（3/3 已修复）**：
- D1 LearningFeedback → `prompt_builder.py:1273-1289` 已调用 `get_relevant_lessons` + `get_strategy`
- D2 LearningLoop 约束 → `prompt_builder.py:1291-1303` 已调用 `get_active_constraints`
- D3 auto_promote → `memory/learning_manager.py:119-132` `get_system_prompt_additions` 已读 `get_promoted_learnings`

**P1-A Windows 卡顿（11/16 已实现）**：
- P0-1 延迟加载 → `register_builtin_tools_lazy`
- P0-2 WAL 模式 → `db/database.py` WAL + synchronous=NORMAL + cache_size=20MB + temp_store=MEMORY + mmap=64MB + FAT 检测
- P0-4 jieba 预热 → `prewarm_jieba`
- P0-5 time.sleep → 仅在后台线程（`_wait_for_server_ready`）
- P1-1 后台任务并行 → `asyncio.gather`
- P1-3 端口检测 → 后台线程轮询
- P1-4 loguru 异步 → `utils/logging_config.py` enqueue=True
- P1-6 tiered_cache async → `asyncio.to_thread(loader)`
- P1-11 KG N+1 → `get_relevance_boost_fast` 已修复
- P2-1 capability 缓存 → `_profile_cache`
- P2-3 tiered_cache 锁清理 → `len>256` 淘汰未锁定锁
- P2-5 prompt 缓存 LRU → `_SCENE_CACHE_MAX_SIZE`

**P1-B 数据库（5/5 已实现，schema 不同但等价）**：
- DB1/DB2/DB3 → 项目用 `memory_child_chunks`（非 `memory_chunks`），已有 `idx_child_parent` + `idx_child_type` + FK CASCADE
- DB4 updated_at → migration v19
- DB5 memory_versions 索引 → `idx_mv_memory ON memory_versions(memory_id, version)`
- 迁移幂等性 → dirty flag + retry + 独立 commit

**P1-C 并发 I/O（2/4 已实现）**：
- C1 并发工具执行 → `tool_call_handler.py:267` asyncio.gather
- C3 DAG 忙等待 → `parallel_dag.py` asyncio.Event 替代 sleep(0.05)

**P2 内存缓存（3/5 已实现）**：
- M1 场景缓存 LRU → `_SCENE_CACHE_MAX_SIZE`
- M4 原子写入 → `utils/atomic_write.py`
- M5 梦境整合 → `consolidate_from_db` 方法已写（但 scheduler 调错方法，见 G7）

---

## 2. 待修复的 10 项真实缺口

### 2.1 G1: 问候短路（L1）

**位置**：`agent_core/message_processor.py`

**问题**：每次"你好""在吗"都走 `_try_simple_chat_fast_path` 仍调 LLM（1-2s），未实现 <100ms 短路。

**方案**：
- 在 `_process_impl` 入口 slash 命令之后、`_try_simple_chat_fast_path` 之前加 `_try_greeting_shortcut`
- 正则匹配 `^(你好|您好|hi|hello|hey|在吗|在不在|嗨|早安|早上好|早|晚安|晚上好|谢谢|感谢|thanks|thx)\s*[!！。.？?]*$`
- 命中后直接返回 `ProcessResult(reply=..., emotion="greeting")`，**完全不调 LLM**
- 时段问候：5-12 点"早上好～新的一天开始啦！"，12-18 点"下午好～"，18-5 点"晚上好～今天辛苦啦！"
- 默认回复："你好呀～有什么可以帮你的吗？" / "不客气～"
- 开关：`ENABLE_GREETING_SHORTCUT=true`（默认开，环境变量控制）
- 群聊模式跳过（避免刷屏）

**验收**：
- 发"你好"→<100ms 返回
- 发"帮我写函数"→走完整流程不受影响
- 群聊中不触发短路

### 2.2 G2: WebSocket broadcast 背压（P1-7）

**位置**：`web/ws_hub.py:87-90`

**问题**：`broadcast` 串行 `send_to`，慢连接阻塞快连接。

**方案**：
```python
async def broadcast(self, event: dict) -> None:
    """fire-and-forget 扇出，5s 超时清理慢连接."""
    if not self._connections:
        return
    tasks = {asyncio.create_task(self._safe_send(cid, event)): cid
             for cid in list(self._connections)}
    done, pending = await asyncio.wait(tasks, timeout=5.0)
    for t in pending:
        cid = tasks[t]
        t.cancel()
        logger.warning("ws.broadcast_timeout", conn_id=cid)
        self.unregister(cid)

async def _safe_send(self, conn_id: str, event: dict) -> None:
    try:
        await self.send_to(conn_id, event)
    except Exception as e:
        logger.warning("ws.send_failed", conn_id=conn_id, error=str(e))
        self.unregister(conn_id)
```

**验收**：模拟 1 个慢连接（sleep 10s），其他连接 5s 内收到广播，慢连接被清理。

### 2.3 G3: mental_state debounce（P1-8）

**位置**：`core/mental_state.py:220-225`

**问题**：`_save` 每次情绪变动同步写盘，Windows 上造成卡顿。

**方案**：
- 新增 `_save_pending: bool = False` 和 `_save_loop_task: asyncio.Task | None`
- `_save` 改为标记 dirty + 启动 300ms 后的写盘任务（若尚未启动）
- 多次调用合并为 1 次写盘
- 写盘用 `asyncio.to_thread(self._state.save, self._state_path)`
- 新增 `flush()` 方法供退出时立即写盘
- `__del__` 或 shutdown hook 调用 `flush()`

```python
def _save(self) -> None:
    """debounce 300ms 写盘."""
    self._save_pending = True
    if self._save_loop_task is None or self._save_loop_task.done():
        self._save_loop_task = asyncio.create_task(self._save_debounced())

async def _save_debounced(self) -> None:
    await asyncio.sleep(0.3)  # debounce 窗口
    if not self._save_pending:
        return
    self._save_pending = False
    await asyncio.to_thread(self._state.save, self._state_path)

async def flush(self) -> None:
    """立即写盘（退出时调用）."""
    if self._save_loop_task and not self._save_loop_task.done():
        self._save_loop_task.cancel()
    await asyncio.to_thread(self._state.save, self._state_path)
```

**验收**：连续 10 次 `update_emotion` 只触发 1 次磁盘写入。

### 2.4 G4: HTTP 连接池复用（C2）

**位置**：新建 `utils/http_pool.py` + 改造 40+ 调用点

**问题**：40+ 处 `async with httpx.AsyncClient(timeout=N)` 每次新建连接，TLS 握手 200-500ms。

**方案**：
```python
# utils/http_pool.py
import httpx
from typing import Optional

_shared_client: Optional[httpx.AsyncClient] = None

def get_shared_client() -> httpx.AsyncClient:
    """全局共享 httpx.AsyncClient 单例（连接池复用 + HTTP/2）."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
            timeout=httpx.Timeout(30.0, connect=5.0),
            http2=True,
        )
    return _shared_client

async def close_shared_client() -> None:
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None
```

**改造策略**：
- 高频调用点（reranker/query_transform/memory_distiller/knowledge_graph）优先改造
- 用 `client = get_shared_client(); resp = await client.get(url, timeout=httpx.Timeout(N))` 替代 `async with httpx.AsyncClient(timeout=N) as client`
- **禁止**修改 `client.timeout` 全局属性（会污染共享 client 影响其他并发请求）；只能通过 `client.get(url, timeout=...)` 在单次请求级别覆盖
- SSRF hook（`web_browse_enhanced`）保留 event_hooks（通过 `client = httpx.AsyncClient(..., event_hooks=...)` 临时实例化，这类场景不池化）

**验收**：抓包确认 TLS 握手次数显著减少；并发 10 个 HTTP 请求不创建 10 个新连接。

### 2.5 G5: WebSocket 心跳（C4）

**位置**：`web/ws_hub.py`

**问题**：无服务端心跳，死连接不感知。

**方案**：
- 每个连接注册时启动 `heartbeat_loop` 协程
- 30s 发 `{"type":"ping"}`
- 10s 内未收到 `{"type":"pong"}` 则关闭连接
- 已有的客户端 ping 响应（line 357-358）保留
- 连接关闭时取消心跳任务

```python
async def _heartbeat_loop(self, conn_id: str) -> None:
    while True:
        await asyncio.sleep(30)  # HEARTBEAT_INTERVAL
        try:
            await self.send_to(conn_id, {"type": "ping"})
            # 等待 pong（pong 处理在 on_message 中 set event）
            self._pong_events[conn_id] = asyncio.Event()
            await asyncio.wait_for(self._pong_events[conn_id].wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("ws.heartbeat_timeout", conn_id=conn_id)
            await self.close(conn_id, code=1001, reason="heartbeat timeout")
            return
        except Exception:
            self.unregister(conn_id)
            return
```

**验收**：模拟客户端断网，40s 后服务端清理连接，`active_count` 减少。

### 2.6 G6: recovery_orchestrator audit_log 上限（M3）

**位置**：`core/recovery_orchestrator.py:92`

**问题**：`self._audit_log: list[dict] = []` 无上限，长期运行内存泄漏。

**方案**：
```python
# 原：self._audit_log: list[dict] = []
# 新：
from collections import deque
self._audit_log: deque[dict] = deque(maxlen=500)
```

`get_audit_log(limit)` 已用 `[-limit:]` 切片，deque 兼容。

**验收**：触发 600 次恢复事件后，`len(_audit_log) == 500`。

### 2.7 G7: dream_consolidation scheduler 修 bug（M5）

**位置**：`core/dream_consolidation.py:412`

**问题**：scheduler 调用 `await self.consolidate()` 操作空字典 `_memories`，实际没做事。`consolidate_from_db` 方法已存在但未被 scheduler 使用。

**方案**：
- `DreamConsolidator.__init__` 接受可选 `memory_db` 引用
- `start_scheduler` 调用 `consolidate_from_db(self._memory_db)` 替代 `consolidate()`
- 保留 `consolidate()` 供单元测试和 `consolidate_from_db` 内部使用
- 全局 `get_dream_consolidator()` 工厂注入 `memory_db`

```python
def __init__(self, ..., memory_db: Any = None) -> None:
    ...
    self._memory_db = memory_db

async def _run_scheduled(self) -> None:
    while True:
        ...
        await asyncio.sleep(wait)
        try:
            if self._memory_db is not None:
                await self.consolidate_from_db(self._memory_db)
            else:
                await self.consolidate()  # 降级
        except Exception as e:
            logger.error(f"Dream.scheduler.failed: {e}")
```

**验收**：scheduler 触发后日志显示 `consolidate_from_db duration=... archived=N`。

### 2.8 G8: context_compressor 缓存 async 化（P1-10）

**位置**：`memory/context_compressor.py:60-67, 69-79`

**问题**：`_cache_original` 和 `retrieve` 同步文件 IO。

**方案**（折中：只异步化 retrieve 读取路径，写入保持同步）：
- `compress_history` 仍是同步方法（被多处调用，改造风险大）
- `_cache_original` 在 `compress_history` 内保持同步（单次文件写，影响小）
- 新增 `retrieve_async`，用 `asyncio.to_thread(self.retrieve, ccr_key)` 包裹
- `retrieve_context` 工具（已是 async）改用 `retrieve_async`

```python
async def retrieve_async(self, ccr_key: str) -> str | None:
    """异步读取缓存，避免阻塞事件循环."""
    return await asyncio.to_thread(self.retrieve, ccr_key)
```

`retrieve_context` 工具实现改为 `content = await ctx_comp.retrieve_async(ccr_key)`。

**验收**：`retrieve_context` 工具调用不阻塞事件循环（用 `asyncio.get_event_loop().time()` 测量）。

### 2.9 G9: tts_engine read_bytes async（P1-9）

**位置**：`emotion/tts_engine.py:321`

**问题**：`path.read_bytes()` 同步读取音频文件，10MB 文件阻塞事件循环。

**方案**：
```python
# 原：data = path.read_bytes()
# 新：
data = await asyncio.to_thread(path.read_bytes)
```

调用方 `synthesize_voice_data_url` 改为 async（若已是 async 则直接 await）。

**验收**：10MB 音频上传期间事件循环不阻塞（其他协程可继续执行）。

### 2.10 G10: capability_detector subprocess（P2-4）— 已接受

**位置**：`core/capability_detector.py:190, 204, 217`

**评估**：`subprocess.check_output` 同步调用，但：
- `detect_capabilities()` 全局缓存（`_profile_cache`），仅启动时调用一次
- 调用耗时 <5s（timeout），不影响运行时性能
- 改为 async 收益微小，但破坏接口风险高

**结论**：不修改，标记为已接受。

---

## 3. 系统性性能审计计划

### 3.1 审计目标
找出文档未覆盖的真实性能瓶颈，补充到实施清单。

### 3.2 审计范围（6 大领域）

#### A. 响应延迟审计
- 测量 `process` 入口到回复各阶段耗时
- 检查 `restore_from_db`、`retrieve_memories_hybrid`、`build_system_prompt` 耗时
- 工具：在 `_process_impl` 各阶段加 `time.monotonic()` 计时日志

#### B. RAG 检索审计
- 6 路 RRF 各路查询耗时分布
- sqlite-vec KNN 在不同数据量下的耗时
- FTS5 查询计划（`EXPLAIN QUERY PLAN`）
- Reranker 是否阻塞、是否有缓存
- `query_transform` 是否每次都调 LLM

#### C. Windows 桌面审计
- `agent.py` 启动各阶段耗时
- pywebview 窗口创建耗时
- Web 前端首屏加载耗时
- `evaluate_js` 调用频率
- 静态资源大小

#### D. 数据库审计
- 慢查询日志
- 索引覆盖率（`EXPLAIN QUERY PLAN`）
- WAL checkpoint 频率
- 长事务

#### E. 并发审计
- 事件循环阻塞点
- 同步 IO 残留（grep `open(`、`requests.get`、`subprocess.run`）
- 锁竞争
- 后台任务数量

#### F. 内存审计
- 长期运行内存增长（tracemalloc）
- 模块级全局变量大小
- 缓存命中率与淘汰频率
- 大对象

### 3.3 审计产出
- `docs/performance_audit_2026-07-20.md` 报告
- 每个发现的瓶颈附带：测量数据、根因、修复方案、预期收益
- 新发现的瓶颈追加为 G11、G12...

---

## 4. 测试策略

### 4.1 单元测试（每项独立）
| 测试文件 | 覆盖项 | 关键断言 |
|:---|:---|:---|
| `test_greeting_shortcut.py` | G1 | 命中正则返回正确 reply，非问候走原流程，群聊跳过 |
| `test_ws_broadcast_backpressure.py` | G2 | 慢连接不阻塞快连接，5s 超时清理 |
| `test_mental_state_debounce.py` | G3 | 多次更新合并为 1 次写盘，flush 立即写盘 |
| `test_http_pool.py` | G4 | 共享 client 单例、连接复用、HTTP/2 |
| `test_ws_heartbeat.py` | G5 | 死连接 40s 内清理 |
| `test_recovery_audit_log_maxlen.py` | G6 | 600 次事件后 len==500 |
| `test_dream_scheduler_calls_from_db.py` | G7 | scheduler 调用 consolidate_from_db |
| `test_context_compressor_async.py` | G8 | retrieve 不阻塞事件循环 |
| `test_tts_read_bytes_async.py` | G9 | 10MB 文件不阻塞 |

### 4.2 回归测试
- 全量 `pytest tests/ -x --timeout=60` 必须零失败
- 重点回归：
  - `test_qq_streaming.py`（流式不能受 G1 短路影响）
  - `test_phase1_5_modules.py`（tiered_cache 不能受 G2 影响）
  - `test_loguru_enqueue.py`（日志不能受 G3 影响）
  - `test_harness_verification.py`（配置开关）
  - `test_dream_engine_v2.py`（G7 修改后）

### 4.3 冒烟测试
1. **启动**：`python agent.py --desktop` 启动到可交互 < 3s
2. **聊天**：发"你好"→<200ms / 发"帮我写函数"→正常 LLM 流程
3. **记忆**：触发记忆检索 → 检查 6 路 RRF 日志正常
4. **WebSocket**：开 2 个 WebUI 连接 → broadcast 正常 / 杀一个 → 另一个不受影响
5. **长期**：跑 30 分钟 → 内存稳定不增长

---

## 5. 实施顺序

### Phase 1：低风险快速修复（独立、无依赖）
- G6 recovery_orchestrator deque（5 行改动）
- G7 dream scheduler 修 bug（1 行改动 + memory_db 注入）
- G10 capability_detector（不修改，标记接受）

### Phase 2：核心性能修复
- G1 问候短路（用户体感最强）
- G3 mental_state debounce（Windows 卡顿）
- G8 context_compressor async
- G9 tts_engine async

### Phase 3：连接稳定性
- G2 WS broadcast 背压
- G5 WS 心跳
- G4 HTTP 连接池（影响面最大，放最后）

### Phase 4：审计
- 6 大领域系统性审计
- 补充新发现瓶颈

### Phase 5：回归测试 + 冒烟测试
- 全量 pytest
- 启动/聊天/记忆/WS/长期 5 项冒烟

---

## 6. 配置开关

所有优化支持通过 `.env` 开关灰度启用，可独立回滚：

```ini
# .env 性能优化开关
ENABLE_GREETING_SHORTCUT=true       # G1 问候短路
ENABLE_WS_BACKPRESSURE=true         # G2 WS broadcast 背压
ENABLE_MENTAL_STATE_DEBOUNCE=true   # G3 mental_state debounce
ENABLE_HTTP_POOL=true               # G4 HTTP 连接池
ENABLE_WS_HEARTBEAT=true            # G5 WS 心跳
# G6/G7/G8/G9 默认启用，无开关（行为修复）
```

---

## 7. 验收标准

### 7.1 响应延迟
| 指标 | 当前 | 目标 | 测量方法 |
|:-----|:-----|:-----|:---------|
| 简单问候响应 | 1-2s | <100ms | 发"你好"计时 |
| 普通对话首 Token | 3-10s | <1s | 流式输出计时 |

### 7.2 稳定性
| 指标 | 当前 | 目标 | 测量方法 |
|:-----|:-----|:-----|:---------|
| WS 死连接清理 | 不感知 | 40s 内清理 | 模拟断网 |
| recovery 内存 | 持续增长 | 稳定 500 条 | 600 次事件后 |
| dream scheduler | 不工作 | 正常整合 | 日志验证 |

### 7.3 回归
- 全量 pytest 零失败
- 5 项冒烟测试全通过
