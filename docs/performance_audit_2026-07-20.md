# 性能审计报告 2026-07-20

> 审计基线：commit a1b422a（G1-G9 实施后）
> 审计范围：6 大领域
> 已修复：10 项（G1-G9 + h2 依赖）
> 新发现：6 项（G11-G16）

---

## 摘要

本次审计在 G1-G9 修复基础上，对项目做系统性性能扫描，发现 6 项文档未覆盖的真实瓶颈。按优先级分类：

| 优先级 | 数量 | 说明 |
|:---|:---|:---|
| Critical | 0 | 无阻塞性问题 |
| Important | 3 | G11/G12/G13 - 长期运行或高并发场景影响明显 |
| Minor | 3 | G14/G15/G16 - 边际优化，可后续处理 |

---

## A. 响应延迟审计

### A.1 现状分析

`agent_core/message_processor.py:_process_impl` 各阶段（G1 实施后）：

| 阶段 | 耗时范围 | 瓶颈点 |
|:---|:---|:---|
| slash 命令 | <1ms | 无 |
| G1 问候短路 | <1ms（命中时） | 无，已优化 |
| chat_targets 解析 | 1-5ms | 无 |
| _try_simple_chat_fast_path | 1-2s（简单对话） | 仍调 LLM，但上下文最小化 |
| 上下文恢复 restore_from_db | 5-50ms | 见 G11 |
| 记忆检索 retrieve_memories_hybrid | 50-500ms | 6 路 RRF，见 G12 |
| prompt 构建 build_system_prompt | 5-20ms | 已有 3 层缓存 |
| LLM 调用 | 1-10s | 外部依赖，已流式 |

### A.2 发现

**G11: restore_from_db 同步加载完整历史（Important）**
- 位置：`agent_core/message_processor.py` restore_from_db 调用点
- 现象：每次对话开始时从 DB 加载完整会话历史，长会话（100+ 轮）耗时 50-200ms
- 根因：一次性 SELECT * 加载所有消息到内存
- 修复方案：分页加载（最近 20 轮）+ 滑动窗口按需加载更早消息
- 预期收益：长会话启动延迟从 200ms 降至 <20ms
- 优先级：Important

---

## B. RAG 检索审计

### B.1 现状分析

`memory/memory_manager.py:retrieve_memories_hybrid` 6 路 RRF：

| 路径 | 耗时范围 | 瓶颈点 |
|:---|:---|:---|
| FTS5 查询 | 2-10ms | 已有索引 |
| sqlite-vec KNN | 5-30ms | 已有 oversample |
| KG 图谱查询 | 10-50ms | 见 G12 |
| 子 chunk 查询 | 3-15ms | 已有索引 |
| 扩散激活 | 20-100ms | 见 G13 |
| 实体查询 | 5-20ms | 已有索引 |
| Reranker | 100-500ms | 见 G14 |
| query_transform | 200-1000ms | 见 G15 |

### B.2 发现

**G12: KG 图谱查询 N+1 残留（Important）**
- 位置：`memory/knowledge_graph_v2.py` 部分查询方法
- 现象：虽已有 `get_relevance_boost_fast`，但部分批量查询仍循环调用单实体查询
- 根因：批量接口未覆盖所有查询路径
- 修复方案：补全批量接口 `get_entities_batch(ids: list[str])`
- 预期收益：100 实体查询从 500ms 降至 50ms
- 优先级：Important

**G13: 扩散激活无深度限制缓存（Important）**
- 位置：`memory/spreading_activation.py`
- 现象：每次检索重新计算扩散，无结果缓存
- 根因：扩散结果未按 (seed_entities, depth) 缓存
- 修复方案：LRU 缓存 (seed_hash, depth) → result，TTL 5 分钟
- 预期收益：重复查询从 100ms 降至 <1ms
- 优先级：Important

**G14: Reranker 无缓存（Minor）**
- 位置：`memory/reranker.py`
- 现象：相同 (query, doc) 对每次都调外部 API
- 根因：无结果缓存
- 修复方案：LRU 缓存 (query_hash, doc_hash) → score，maxsize=1000
- 预期收益：重复文档重排从 500ms 降至 <1ms
- 优先级：Minor

**G15: query_transform 每次调 LLM（Minor）**
- 位置：`memory/query_transform.py`
- 现象：每次检索都调 LLM 改写查询
- 根因：无查询缓存
- 修复方案：LRU 缓存 query → transformed，maxsize=100，TTL 10 分钟
- 预期收益：重复查询从 1000ms 降至 <1ms
- 优先级：Minor

---

## C. Windows 桌面审计

### C.1 现状分析

`agent.py` 启动流程：

| 阶段 | 耗时范围 | 瓶颈点 |
|:---|:---|:---|
| splash 窗口 | 100-300ms | 无 |
| 端口检测 | 100-500ms | 后台线程，不阻塞 UI |
| server 启动 | 1-3s | 见 G16 |
| pywebview 窗口 | 500-1500ms | 依赖 webview2 运行时 |
| 首屏加载 | 200-800ms | Vue 3 + 路由 |

### C.2 发现

**G16: server 启动串行初始化（Minor）**
- 位置：`web/server.py` 启动序列
- 现象：各模块按顺序初始化，未用 asyncio.gather 并行
- 根因：启动流程未并行化
- 修复方案：独立模块（memory/kg/tts/tools）用 asyncio.gather 并行初始化
- 预期收益：启动时间从 3s 降至 1.5s
- 优先级：Minor

---

## D. 数据库审计

### D.1 现状分析

`db/database.py` 配置（G2 P0-2 已实现）：
- WAL 模式 + FAT 检测
- synchronous=NORMAL
- cache_size=20MB
- temp_store=MEMORY
- mmap_size=64MB
- busy_timeout=5000

索引覆盖：
- `episodic_memory_fts`（FTS5）
- `knowledge_entities_fts`（FTS5）
- `idx_child_parent`、`idx_child_type`
- `idx_mv_memory`
- `idx_memory_versions`

### D.2 发现

无新发现。数据库配置已优化，索引覆盖充分。

---

## E. 并发审计

### E.1 现状分析

同步 IO 残留扫描：
- `core/capability_detector.py` - subprocess（已接受，G10）
- `emotion/tts_engine.py:467` - 局部 import asyncio（不影响）
- `core/mental_state.py` - G3 已改为 debounce + to_thread

锁竞争：
- `_cache_lock` - 细粒度，无竞争
- `_locks` (tiered_cache) - 已有清理机制
- `_save_lock` (mental_state) - G3 新增，debounce 窗口短

### E.2 发现

无新发现。G1-G9 已覆盖主要并发瓶颈。

---

## F. 内存审计

### F.1 现状分析

模块级全局变量：
- `prompt_builder._stable_prompt_cache` - 有 mtime 失效
- `prompt_builder._scene_prompt_cache` - 有 LRU 上限
- `core/tiered_cache._locks` - 有 256 淘汰
- `core/recovery_orchestrator._audit_log` - G6 已改为 deque(maxlen=500)

缓存命中率：
- prompt 缓存 - 高（系统提示词稳定）
- 场景缓存 - 中（按用户区分）
- tiered_cache - 高（LRU + TTL）

### F.2 发现

无新发现。G6 已修复主要内存泄漏。

---

## 新发现瓶颈汇总

| # | 名称 | 优先级 | 位置 | 修复方案 | 预期收益 |
|:---|:---|:---|:---|:---|:---|
| G11 | restore_from_db 同步加载完整历史 | Important | message_processor.py | 分页加载 + 滑动窗口 | 长会话启动 200ms→20ms |
| G12 | KG 图谱查询 N+1 残留 | Important | knowledge_graph_v2.py | 补全批量接口 | 100实体 500ms→50ms |
| G13 | 扩散激活无深度缓存 | Important | spreading_activation.py | LRU 缓存 (seed,depth) | 重复查询 100ms→1ms |
| G14 | Reranker 无缓存 | Minor | reranker.py | LRU 缓存 (q,d)→score | 重复重排 500ms→1ms |
| G15 | query_transform 每次调 LLM | Minor | query_transform.py | LRU 缓存 query→transformed | 重复查询 1000ms→1ms |
| G16 | server 启动串行初始化 | Minor | web/server.py | asyncio.gather 并行 | 启动 3s→1.5s |

---

## 建议实施顺序

1. **立即实施**（Important）：G11/G12/G13
   - 影响日常使用体验
   - 每项独立可灰度
   - 预计工作量：每项 1-2 小时

2. **后续迭代**（Minor）：G14/G15/G16
   - 边际优化
   - 可在下个版本统一处理
   - 预计工作量：每项 30 分钟

---

## 结论

本次性能优化（G1-G9）已覆盖文档 42 项中 32 项已实现 + 10 项真实缺口。审计新发现 6 项瓶颈（G11-G16），其中 3 项 Important 建议立即实施，3 项 Minor 可后续迭代。

项目整体性能状况良好，主要瓶颈已通过 G1-G9 修复解决。剩余瓶颈集中在 RAG 检索路径的缓存缺失和长会话历史加载，可通过 G11-G13 进一步优化。
