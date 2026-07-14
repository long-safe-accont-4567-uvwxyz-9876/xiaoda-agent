# 记忆检索机制修复 v3 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复记忆检索系统 10 个缺陷，解决"刚发生的事不记得"的核心问题

**Architecture:** 按 P0→P1→P2 优先级分三批实施。每批内按依赖顺序执行。Fix 0 是最关键的 1 行改动，必须第一个实施。

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, loguru

## Global Constraints

- 不破坏已有优点（FSRS-DSR、七路 RRF、冷/温/热路由、CRAG、QueryCache、Scope 隔离等 18 项能力）
- 保持 Windows 兼容（pathlib.Path, json）
- 保持原子写入（temp + os.replace）
- 保持幂等性（重复调用安全）
- 保持零质量回退（默认开启，可通过环境变量关闭）
- 代码风格：无注释（除非用户要求），中文 docstring 保持现有风格

---

### Task 1: Fix 0 — `retrieve_memories_hybrid` 默认 `include_raw=True`

**Files:**
- Modify: `memory/memory_manager.py:421`

**Interfaces:**
- Consumes: 无前置依赖
- Produces: `retrieve_memories_hybrid(include_raw=True)` 默认行为变更，所有 6+ 处调用点自动生效

- [ ] **Step 1: 修改默认参数**

将 `memory/memory_manager.py` 第 421 行的 `include_raw: bool = False` 改为 `include_raw: bool = True`：

```python
# 修改前 (L421)
    async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                        use_reranker: bool = True,
                                        use_kg: bool = True,
                                        scope: Any | None = None,
                                        include_raw: bool = False) -> list[dict]:

# 修改后
    async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                        use_reranker: bool = True,
                                        use_kg: bool = True,
                                        scope: Any | None = None,
                                        include_raw: bool = True) -> list[dict]:
```

- [ ] **Step 2: 验证调用点**

确认以下调用点不再需要显式传 `include_raw=True`：
- `retrieve_memories` 简单路径 (L1117 附近)
- CRAG retry (L1139/L1208 附近)
- multi-query (L1389/L1431 附近)
- `_try_temporal_search` 内部调用（此方法有自己的 include_raw 参数，Task 2 处理）

- [ ] **Step 3: Commit**

```bash
git add memory/memory_manager.py
git commit -m "fix(memory): Fix 0 - default include_raw=True in retrieve_memories_hybrid"
```

---

### Task 2: Fix 1 — 小时级时间词 + `_try_temporal_search` 默认 `include_raw=True`

**Files:**
- Modify: `memory/memory_manager.py:47-65` (_TEMPORAL_PATTERNS)
- Modify: `memory/memory_manager.py:67-85` (_parse_temporal_query)
- Modify: `memory/memory_manager.py:1244` (_try_temporal_search 签名)
- Modify: `memory/query_transform.py:24` (TEMPORAL_KEYWORDS)

**Interfaces:**
- Consumes: Task 1 的 `include_raw=True` 默认值
- Produces: 小时级时间词解析能力，`_try_temporal_search(include_raw=True)` 默认行为

- [ ] **Step 1: 修改 `_TEMPORAL_PATTERNS`，增加小时级词**

在 `memory/memory_manager.py` 中，将 `_TEMPORAL_PATTERNS` 替换为：

```python
_TEMPORAL_PATTERNS = [
    (re.compile(r"刚才|刚刚"), 0, 0),
    (re.compile(r"(\d+)\s*小时前"), -1, -1),
    (re.compile(r"(\d+)\s*分钟前"), -1, -1),
    (re.compile(r"大前天"), 3, 1),
    (re.compile(r"前天"), 2, 1),
    (re.compile(r"昨天|昨日"), 1, 1),
    (re.compile(r"今天|今日"), 0, 1),
    (re.compile(r"上周"), 7, 7),
    (re.compile(r"上个月|上月"), 30, 30),
    (re.compile(r"前几天|前些天|最近"), 1, 7),
]
```

注意：小时级词必须排在天级词前面，因为"刚才"可能被"刚"子串误匹配。

- [ ] **Step 2: 修改 `_parse_temporal_query`，增加动态解析**

在 `_parse_temporal_query` 函数体开头（`for pattern` 循环之前），插入小时级匹配逻辑：

```python
def _parse_temporal_query(query: str) -> tuple[float, float] | None:
    m = re.search(r"(\d+)\s*小时前", query)
    if m:
        hours = int(m.group(1))
        now = _datetime.datetime.now(_datetime.UTC).astimezone()
        start = now - _datetime.timedelta(hours=hours)
        return start.timestamp(), now.timestamp()

    m = re.search(r"(\d+)\s*分钟前", query)
    if m:
        minutes = int(m.group(1))
        now = _datetime.datetime.now(_datetime.UTC).astimezone()
        start = now - _datetime.timedelta(minutes=minutes)
        return start.timestamp(), now.timestamp()

    if re.search(r"刚才|刚刚", query):
        now = _datetime.datetime.now(_datetime.UTC).astimezone()
        start = now - _datetime.timedelta(hours=1)
        return start.timestamp(), now.timestamp()

    for pattern, offset_days, span_days in _TEMPORAL_PATTERNS:
        if pattern.search(query):
            now = _datetime.datetime.now(_datetime.UTC).astimezone()
            start_date = (now - _datetime.timedelta(days=offset_days + span_days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_date = (now - _datetime.timedelta(days=offset_days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) if offset_days > 0 else now
            return start_date.timestamp(), end_date.timestamp()
    return None
```

- [ ] **Step 3: 修改 `_try_temporal_search` 默认 `include_raw=True`**

将 `memory/memory_manager.py` 第 1244 行的 `include_raw: bool = False` 改为 `include_raw: bool = True`：

```python
# 修改前
    async def _try_temporal_search(self, query: str, k: int,
                                    scope: Any | None = None,
                                    include_raw: bool = False) -> list[dict] | None:

# 修改后
    async def _try_temporal_search(self, query: str, k: int,
                                    scope: Any | None = None,
                                    include_raw: bool = True) -> list[dict] | None:
```

- [ ] **Step 4: 补充 `TEMPORAL_KEYWORDS`**

在 `memory/query_transform.py` 的 `TEMPORAL_KEYWORDS` 集合中添加小时级词：

```python
# 修改前
    TEMPORAL_KEYWORDS: ClassVar[set[str]] = {"昨天", "前天", "今天", "上周", "上个月", "刚才", "之前", "那次", "那天", "那次对话"}

# 修改后
    TEMPORAL_KEYWORDS: ClassVar[set[str]] = {"昨天", "前天", "今天", "上周", "上个月", "刚才", "之前", "那次", "那天", "那次对话", "刚刚", "小时前", "分钟前"}
```

- [ ] **Step 5: Commit**

```bash
git add memory/memory_manager.py memory/query_transform.py
git commit -m "fix(memory): Fix 1 - hourly temporal patterns + temporal search include_raw=True"
```

---

### Task 3: Fix 9 — 蒸馏失败时存原文+标记，避免失忆

**Files:**
- Modify: `memory/memory_manager.py:1663-1850` (encode_memory — 传入 full_text)
- Modify: `memory/memory_manager.py:1976-2055` (_distill_to_knowledge — 增加 full_text 参数 + 失败标记检查)
- Modify: `db/db_memory.py` (增加 update_emotion_label, update_memory_summary)

**Interfaces:**
- Consumes: 无前置依赖
- Produces: `_save_fallback_raw()` 方法, `db_memory.update_emotion_label()`, `db_memory.update_memory_summary()`

- [ ] **Step 1: 在 `db/db_memory.py` 增加 `update_emotion_label` 和 `update_memory_summary`**

在 `db/db_memory.py` 的 `MemoryDB` 类中（`get_memories_by_ids` 方法之后）添加：

```python
    async def update_emotion_label(self, mem_id: int, label: str) -> None:
        await self._conn.execute(
            "UPDATE episodic_memories SET emotion_label = ? WHERE id = ?",
            (label, mem_id),
        )
        await self._conn.commit()

    async def update_memory_summary(self, mem_id: int, new_summary: str) -> None:
        await self._conn.execute(
            "UPDATE episodic_memories SET summary = ? WHERE id = ?",
            (new_summary, mem_id),
        )
        await self._conn.commit()
```

- [ ] **Step 2: 修改 `encode_memory` 传入完整原文**

在 `memory/memory_manager.py` 的 `encode_memory` 方法中，找到蒸馏异步调用部分（约 L1820-1830），将：

```python
            if self.distiller:
                try:
                    _distill_task = asyncio.create_task(
                        self._distill_to_knowledge(
                            mem_id, summary, scope, importance, emotion
                        )
                    )
```

替换为：

```python
            full_text_parts = []
            for msg in exchanges[-6:]:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and content:
                    full_text_parts.append(f"用户说: {content}")
                elif role == "assistant" and content:
                    full_text_parts.append(f"小妲: {content}")
            full_text = "；".join(full_text_parts)[:2000]

            if self.distiller:
                try:
                    _distill_task = asyncio.create_task(
                        self._distill_to_knowledge(
                            mem_id, summary, scope, importance, emotion,
                            full_text=full_text
                        )
                    )
```

- [ ] **Step 3: 修改 `_distill_to_knowledge` 签名和逻辑**

将 `_distill_to_knowledge` 方法签名改为：

```python
    async def _distill_to_knowledge(self, raw_id: int, summary: str,
                                     scope: Any, importance: float = 0.5,
                                     emotion: str = "", _retry: int = 0,
                                     full_text: str = "") -> None:
```

在方法体开头（`if not self.distiller: return` 之后）添加失败标记检查：

```python
        try:
            existing = await self.memory.get_memory_by_id(raw_id)
            if existing and existing.get("emotion_label", "").endswith("distill_failed"):
                logger.debug("memory.distill_skip_failed", raw_id=raw_id)
                return
        except Exception:
            pass
```

将蒸馏返回空的重试分支（`if not distilled or not distilled.strip():`）的 else 分支改为：

```python
                else:
                    logger.warning("memory.distill_exhausted_retries", raw_id=raw_id)
                    await self._save_fallback_raw(raw_id, summary, full_text)
                return
```

将异常处理分支的重试 else 也改为：

```python
            if _retry < 2:
                delay = 30 * (_retry + 1)
                asyncio.get_event_loop().call_later(
                    delay,
                    lambda: asyncio.ensure_future(
                        self._distill_to_knowledge(raw_id, summary, scope,
                                                   importance, emotion, _retry + 1,
                                                   full_text=full_text)
                    ),
                )
            else:
                await self._save_fallback_raw(raw_id, summary, full_text)
```

- [ ] **Step 4: 添加 `_save_fallback_raw` 方法**

在 `_distill_to_knowledge` 方法之后添加：

```python
    async def _save_fallback_raw(self, raw_id: int, truncated_summary: str,
                                  full_text: str) -> None:
        try:
            if full_text and len(full_text) > len(truncated_summary):
                await self.memory.update_memory_summary(raw_id, full_text)
                logger.info("memory.fallback_raw_updated", raw_id=raw_id,
                           old_len=len(truncated_summary), new_len=len(full_text))
                if self.vec:
                    try:
                        await self.vec.upsert(raw_id, full_text)
                    except Exception as e:
                        logger.debug("memory.fallback_vec_upsert_failed", error=str(e))

            await self.memory.update_emotion_label(raw_id, "distill_failed")
        except Exception as e:
            logger.warning("memory.fallback_save_failed", raw_id=raw_id, error=str(e))
```

- [ ] **Step 5: Commit**

```bash
git add memory/memory_manager.py db/db_memory.py
git commit -m "fix(memory): Fix 9 - save full_text on distill failure + mark distill_failed"
```

---

### Task 4: Fix 2 — `restore_from_db` 摘要注入时间戳

**Files:**
- Modify: `agent_context.py:580-600` (restore_from_db 中的摘要格式化循环)

**Interfaces:**
- Consumes: 无前置依赖
- Produces: 历史摘要带时间戳 `[MM-DD HH:MM]` 前缀

- [ ] **Step 1: 修改 `restore_from_db` 中的摘要格式化**

将 `agent_context.py` 中 `restore_from_db` 方法的循环体：

```python
            for row in rows:
                user_msg = row.get("user_message", "")
                asst_msg = row.get("assistant_reply", "")
                if not user_msg and not asst_msg:
                    continue
                user_preview = user_msg[:60].replace("\n", " ") if user_msg else ""
                asst_preview = asst_msg[:60].replace("\n", " ") if asst_msg else ""
                summaries.append(f"· {term}: {user_preview} → 小妲: {asst_preview}")
```

替换为：

```python
            for row in rows:
                user_msg = row.get("user_message", "")
                asst_msg = row.get("assistant_reply", "")
                if not user_msg and not asst_msg:
                    continue
                user_preview = user_msg[:60].replace("\n", " ") if user_msg else ""
                asst_preview = asst_msg[:60].replace("\n", " ") if asst_msg else ""
                ts = row.get("timestamp", 0)
                if ts:
                    try:
                        time_str = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
                        summaries.append(f"· [{time_str}] {term}: {user_preview} → 小妲: {asst_preview}")
                    except (ValueError, TypeError, OSError):
                        summaries.append(f"· {term}: {user_preview} → 小妲: {asst_preview}")
                else:
                    summaries.append(f"· {term}: {user_preview} → 小妲: {asst_preview}")
```

- [ ] **Step 2: Commit**

```bash
git add agent_context.py
git commit -m "fix(context): Fix 2 - inject timestamps into restore_from_db summaries"
```

---

### Task 5: Fix 3 — `_build_time_context` 增加对话间隔提示

**Files:**
- Modify: `agent_context.py:316-358` (_build_time_context return 语句)

**Interfaces:**
- Consumes: `self._last_message_time`（在 `signal_new_message()` 中更新，L153 附近）
- Produces: 时间语境末尾追加对话间隔提示

- [ ] **Step 1: 修改 `_build_time_context` 的 return 语句**

将 `agent_context.py` 中 `_build_time_context` 方法的 return 语句：

```python
        return (f"当前时间：{now.year}年{now.month}月{now.day}日 星期{weekday} "
                f"{hour:02d}:{minute:02d}（{period}）。这是小妲真切感受到的此刻，"
                f"是她回应时唯一参照的时间。历史消息中的任何时间表述均已过时，不得作为当前时间引用。")
```

替换为：

```python
        gap_text = ""
        if self._last_message_time > 0:
            gap_seconds = time.time() - self._last_message_time
            if gap_seconds < 60:
                gap_desc = "刚刚"
            elif gap_seconds < 3600:
                gap_desc = f"{int(gap_seconds / 60)}分钟前"
            elif gap_seconds < 86400:
                gap_desc = f"{int(gap_seconds / 3600)}小时前"
            elif gap_seconds < 2592000:
                gap_desc = f"{int(gap_seconds / 86400)}天前"
            else:
                gap_desc = ""
            if gap_desc:
                gap_text = f"距上次对话：{gap_desc}。如果间隔较长，不要用「刚才」「刚刚」等词指代上次对话内容。"

        return (f"当前时间：{now.year}年{now.month}月{now.day}日 星期{weekday} "
                f"{hour:02d}:{minute:02d}（{period}）。这是小妲真切感受到的此刻，"
                f"是她回应时唯一参照的时间。历史消息中的任何时间表述均已过时，不得作为当前时间引用。"
                f"{gap_text}")
```

- [ ] **Step 2: Commit**

```bash
git add agent_context.py
git commit -m "fix(context): Fix 3 - add conversation gap hint to time context"
```

---

### Task 6: Fix 4 — 记忆写入后立即失效查询缓存

**Files:**
- Modify: `memory/query_cache.py` (增加 invalidate 方法)
- Modify: `memory/memory_manager.py:1837` (encode_memory 中调用 invalidate)

**Interfaces:**
- Consumes: 无前置依赖
- Produces: `QueryCache.invalidate()` 方法

- [ ] **Step 1: 在 `QueryCache` 中增加 `invalidate` 方法**

在 `memory/query_cache.py` 的 `QueryCache` 类中（`put` 方法之后）添加：

```python
    def invalidate(self) -> None:
        self._cache.clear()
```

- [ ] **Step 2: 在 `encode_memory` 中调用 `invalidate`**

在 `memory/memory_manager.py` 的 `encode_memory` 方法中，找到 `self.invalidate_memory_count_cache()` 调用（约 L1837），在其后添加：

```python
            self.invalidate_memory_count_cache()

            if self._query_cache:
                self._query_cache.invalidate()
```

- [ ] **Step 3: Commit**

```bash
git add memory/query_cache.py memory/memory_manager.py
git commit -m "fix(memory): Fix 4 - invalidate query cache after memory write"
```

---

### Task 7: Fix 8 — RRF 融合后内容相似度去重

**Files:**
- Modify: `memory/memory_manager.py` (增加 _dedup_by_content_similarity 方法 + 调用)

**Interfaces:**
- Consumes: Fix 0 的 `include_raw=True`（raw+distilled 双版本共存场景）
- Produces: `_dedup_by_content_similarity()` 方法

- [ ] **Step 1: 添加 `_dedup_by_content_similarity` 方法**

在 `memory/memory_manager.py` 的 `MemoryManager` 类中（`_compute_recency_boost` 方法附近）添加：

```python
    def _dedup_by_content_similarity(self, results: list[dict], threshold: float = 0.7) -> list[dict]:
        if len(results) <= 1:
            return results
        kept = []
        for r in results:
            summary = r.get("summary", "")
            words = set(summary)
            is_dup = False
            for k in kept:
                k_words = set(k.get("summary", ""))
                if not words or not k_words:
                    continue
                jaccard = len(words & k_words) / len(words | k_words)
                if jaccard > threshold:
                    if r.get("final_score", 0) <= k.get("final_score", 0):
                        is_dup = True
                        break
                    else:
                        kept.remove(k)
                        break
            if not is_dup:
                kept.append(r)
        return kept
```

- [ ] **Step 2: 在 `retrieve_memories_hybrid` 返回前调用去重**

在 `retrieve_memories_hybrid` 方法中，找到最终返回 results 之前的位置（在 Reranker 精排之后），添加去重调用。需要找到方法中 `return results` 或类似返回点，在返回前插入：

```python
        results = self._dedup_by_content_similarity(results)
```

具体位置需要根据代码流确定——在 Reranker 精排完成后、方法返回前。

- [ ] **Step 3: Commit**

```bash
git add memory/memory_manager.py
git commit -m "fix(memory): Fix 8 - dedup by content similarity after RRF fusion"
```

---

### Task 8: Fix 5 — `_compute_recency_boost` 小时级粒度

**Files:**
- Modify: `memory/memory_manager.py:1516-1545` (_compute_recency_boost 方法体)

**Interfaces:**
- Consumes: 无前置依赖
- Produces: 更精确的小时级时间新鲜度评分

- [ ] **Step 1: 替换 `_compute_recency_boost` 方法体**

将 `memory/memory_manager.py` 中的 `_compute_recency_boost` 方法替换为：

```python
    def _compute_recency_boost(self, item: dict) -> float:
        ts = item.get("timestamp") or item.get("created_at") or item.get("updated_at")
        if not ts:
            return 0.3
        try:
            if isinstance(ts, str):
                dt = _datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, (int, float)):
                dt = _datetime.datetime.fromtimestamp(ts)
            else:
                return 0.3

            now = _datetime.datetime.now(dt.tzinfo)
            delta = now - dt
            hours_ago = delta.total_seconds() / 3600
            days_ago = delta.days

            if hours_ago <= 1:
                return 1.0
            if hours_ago <= 4:
                return 0.95
            if hours_ago <= 12:
                return 0.90
            if days_ago <= 0:
                return 0.85
            if days_ago <= 1:
                return 0.70
            if days_ago <= 7:
                return 0.50
            if days_ago <= 30:
                return 0.30
            if days_ago <= 90:
                return 0.20
            return 0.10
        except Exception:
            return 0.3
```

- [ ] **Step 2: Commit**

```bash
git add memory/memory_manager.py
git commit -m "fix(memory): Fix 5 - hourly granularity for recency boost"
```

---

### Task 9: Fix 6 — 强化幻觉兜底

**Files:**
- Modify: `agent_context.py:443-445` (_format_memory_retrieval 空结果分支)

**Interfaces:**
- Consumes: 无前置依赖
- Produces: 更强的幻觉防护提示

- [ ] **Step 1: 替换空结果提示文本**

将 `agent_context.py` 中 `_format_memory_retrieval` 方法的空结果分支：

```python
            return '[元认知提示] 我没有找到相关记忆。如果用户问的是过去的事，请诚实说"我不记得了"；如果是不确定的信息，请说"我不太确定"。不要假装记得或编造。'
```

替换为：

```python
            return ('[重要·元认知] 检索确认：没有找到与用户问题相关的记忆。'
                    '绝对不要编造、推测或暗示记得过去发生的事。'
                    '如实回答"我不记得了"或"我没有关于这件事的记忆"。'
                    '如果用户提供了具体细节，可以基于这些细节继续对话，但不要虚构未提供的细节。')
```

- [ ] **Step 2: Commit**

```bash
git add agent_context.py
git commit -m "fix(context): Fix 6 - strengthen hallucination guard in memory retrieval"
```

---

### Task 10: Fix 7 — `_generate_summary` 放宽截断

**Files:**
- Modify: `memory/memory_manager.py:2147-2158` (_generate_summary 方法体)

**Interfaces:**
- Consumes: 无前置依赖
- Produces: 更长的摘要保留更多细节

- [ ] **Step 1: 修改 `_generate_summary` 截断参数**

将 `memory/memory_manager.py` 中的 `_generate_summary` 方法：

```python
    def _generate_summary(self, exchanges: list[dict]) -> str:
        parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                parts.append(f"用户说: {content[:150]}")
            elif role == "assistant" and content:
                parts.append(content[:150])

        summary = "；".join(parts)
        return summary[:500]
```

替换为：

```python
    def _generate_summary(self, exchanges: list[dict]) -> str:
        parts = []
        for msg in exchanges[-6:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and content:
                parts.append(f"用户说: {content[:250]}")
            elif role == "assistant" and content:
                parts.append(content[:250])

        summary = "；".join(parts)
        return summary[:700]
```

- [ ] **Step 2: Commit**

```bash
git add memory/memory_manager.py
git commit -m "fix(memory): Fix 7 - relax summary truncation 150/500 -> 250/700"
```

---

### Task 11: Fix 10 — `_hybrid_vec_search` 增加 is_raw 过滤一致性

**Files:**
- Modify: `memory/memory_manager.py:808` (_hybrid_vec_search 签名 + 后过滤)
- Modify: `memory/memory_manager.py` (retrieve_memories_hybrid 中并行调用处传入 is_raw)

**Interfaces:**
- Consumes: Fix 0 的 `is_raw_filter` 计算逻辑
- Produces: FTS 和向量路径 is_raw 过滤行为一致

- [ ] **Step 1: 修改 `_hybrid_vec_search` 签名和后过滤**

将 `memory/memory_manager.py` 中的 `_hybrid_vec_search` 方法签名：

```python
    async def _hybrid_vec_search(self, query: str, k: int,
                                 candidate_ids: list[int] | None = None) -> list[dict]:
```

改为：

```python
    async def _hybrid_vec_search(self, query: str, k: int,
                                 candidate_ids: list[int] | None = None,
                                 is_raw: int | None = None) -> list[dict]:
```

在方法体中 `vec_mems = await self.memory.get_memories_by_ids(vec_ids)` 之后添加后过滤：

```python
            vec_mems = await self.memory.get_memories_by_ids(vec_ids)
            if is_raw is not None:
                vec_mems = [m for m in vec_mems if m.get("is_raw") == is_raw]
```

- [ ] **Step 2: 修改 `retrieve_memories_hybrid` 中的并行调用**

在 `retrieve_memories_hybrid` 方法中找到并行调用 `_hybrid_vec_search` 的位置（asyncio.gather 或顺序调用），传入 `is_raw=is_raw_filter` 参数。

如果使用 asyncio.gather 并行调用，修改为：
```python
    self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids, is_raw=is_raw_filter),
```

如果 cold 路径有独立的向量兜底调用，也同步修改：
```python
    vec_items = await self._hybrid_vec_search(query, recall_limit, is_raw=is_raw_filter)
```

- [ ] **Step 3: Commit**

```bash
git add memory/memory_manager.py
git commit -m "fix(memory): Fix 10 - add is_raw filter to _hybrid_vec_search for consistency"
```

---

### Task 12: 清理临时文件 + 最终验证

**Files:**
- Delete: `_fetch_spec.py`
- Delete: `_fetch.bat`
- Delete: `_spec_output.txt`

- [ ] **Step 1: 删除临时文件**

删除实施过程中创建的临时文件。

- [ ] **Step 2: 运行现有测试验证无回归**

```bash
python -m pytest tests/ -x -q --tb=short 2>&1 | head -50
```

- [ ] **Step 3: 最终 Commit**

```bash
git add -A
git commit -m "chore: cleanup temp files after memory retrieval fix v3"
```