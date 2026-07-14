# 记忆检索机制修复 Spec v3.0

> 日期: 2026-07-14
> 基于: v2.0 全链路复评 + 新发现
> 状态: 待实施
> 优先级: P0（记忆系统根本缺陷）

## 与 v2 的差异

v2 识别了 8 个 Fix（Fix 0~7），v3 新增 3 个 Fix 并调整 2 个 Fix 方案：

| Fix | v2 方案 | v3 调整 | 原因 |
|-----|--------|---------|------|
| Fix 0 | 改调用方传参 `include_raw=True` | **改 `retrieve_memories_hybrid` 默认值为 `True`** | 调用点有 6+ 处（含 `_try_temporal_search`、CRAG retry、multi-query），改默认一处全覆盖，避免遗漏 |
| Fix 1 | 仅加 `_TEMPORAL_PATTERNS` 小时级词 | 同 + **同步修改 `_try_temporal_search` 默认 `include_raw=True`** | `_try_temporal_search` 也有独立 `include_raw=False` 默认值，v2 遗漏 |
| **Fix 8** | — | **新增：RRF 融合后内容去重** | `include_raw=True` 后同一内容的 raw+distilled 双版本都会进入候选，需去重避免冗余 |
| **Fix 9** | — | **新增→P0提升：蒸馏失败时存原文+标记，避免失忆** | 蒸馏3次全失败后raw记忆只有截断summary，关键细节丢失；回填完整原文+标记防重试 |
| **Fix 10** | — | **新增：`_hybrid_vec_search` 增加 `is_raw` 过滤一致性** | 向量路径目前不做 is_raw 过滤，FTS 做。`include_raw=True` 后两者行为应一致 |

---

## 已有优点（绝对不能破坏）

以下能力经过大量迭代验证，修改时必须保持不变：

1. **FSRS-DSR 间隔重复调度** — 艾宾浩斯遗忘曲线 + 访问强化，`_apply_fsrs_scoring` 评分逻辑
2. **七路 RRF 融合** — FTS + Vec + KG + ChildChunk + Spreading + Entity + KGv2，`reciprocal_rank_fusion` 加权融合
3. **冷/温/热三段路由** — cold=纯FTS（零Embedding开销），warm=FTS+低权向量，hot=均衡
4. **CRAG 检索质量评估** — 低置信度触发扩大候选集重试 + importance 兜底
5. **查询语义缓存** — QueryCache 基于嵌入余弦相似度，命中跳过完整流水线
6. **Scope 隔离** — user_id + agent_id 过滤，群聊多用户安全
7. **异步蒸馏管线** — raw→knowledge 的概念正确，只是检索侧过滤有误
8. **Reranker 精排** — `_hybrid_rerank` + Entity Boost
9. **查询变换** — rewrite + expand 并行，提升召回
10. **Topic Trigger 主动联想** — 话题关键词补充召回
11. **Entity 提取/链接** — mem0 SPEC 实体召回通道
12. **父子 Chunk** — 子chunk FTS+Vec → 映射父chunk，Contextual Retrieval
13. **扩散激活** — concept_graph 概念图激活
14. **KG v2** — 知识图谱事实检索
15. **Importance 兜底** — 最后防线按重要性排序
16. **Memory Governance** — tamper-evident 哈希链
17. **Deterministic Selector** — A1 精确匹配优化
18. **蒸馏重试** — 3 次异步重试（30s/60s 间隔）

---

## 全链路审计发现

### 写入链路（正常，无需修改）

```
用户消息 → encode_memory()
  → insert_episodic_memory(is_raw=1)      # 同步，毫秒级
  → vec.upsert(mem_id, summary)            # 同步嵌入
  → concept_graph.remember()               # 双写
  → child_chunks 批量写入                  # 父子Chunk
  → asyncio: entity_extract_and_link()     # 异步实体
  → asyncio: _distill_to_knowledge()       # 异步蒸馏 → is_raw=0
  → asyncio: _enrich_memory_async()        # 异步LLM结构化提取
  → invalidate_memory_count_cache()        # 冷启动路由感知
  → kg.auto_extract_and_merge()            # KG三元组
```

写入链路设计合理，`is_raw=1` 先写保证立即可用，蒸馏异步不阻塞。

### 检索链路（有缺陷）

```
retrieve_memories(query)
  → _query_cache.get()                     # 语义缓存
  → query_transformer.classify_intent()    # 意图路由
  → _try_temporal_search()                 # 时间词路径 ← Bug: include_raw=False
    → _parse_temporal_query()              # ← Bug: 缺"刚才/几小时前"
    → search_memories_fts_with_time()      # FTS+时间
    → search_memories_by_time_scoped()     # 纯时间
  → retrieve_memories_hybrid()             # 主检索 ← Bug: include_raw=False
    → _hybrid_fts_search_scoped(is_raw=0)  # FTS过滤掉raw
    → _hybrid_vec_search()                 # 向量不过滤is_raw ← 不一致
    → _kg_recall()                         # KG
    → _child_recall()                      # 子chunk
    → _spreading_recall()                  # 扩散
    → _entity_recall()                     # 实体
    → _kg_v2_recall()                      # KGv2
    → RRF融合 → Reranker精排 → 返回
  → _apply_fsrs_scoring()                  # FSRS评分
  → _compute_final_scores()                # 综合评分
  → _apply_topic_trigger()                 # 话题补充
  → _apply_kg_context_enhance()            # KG上下文增强
  → _query_cache.put()                     # 缓存写入
```

**关键发现**：向量路径 `_hybrid_vec_search` 不做 `is_raw` 过滤（调用 `get_memories_by_ids` 返回所有），而 FTS 路径过滤 `is_raw=0`。这意味着：
- 温/热用户：raw 记忆可能通过向量通道浮出，但 FTS 通道（通常是主通道）完全错过
- 冷用户：FTS-only，raw 记忆完全不可见
- **精确关键词匹配**（人名/代码/数字）依赖 FTS，raw 被过滤后这些精确匹配丢失

### 上下文恢复链路（有缺陷）

```
restore_from_db()
  → get_recent_conversations(limit=10)
  → 格式化为 "· 爸爸: xxx → 小妲: yyy"    ← Bug: 无时间戳
_build_time_context()
  → "当前时间：2026年7月14日 星期二 15:00"  ← Bug: 无对话间隔提示
```

### LLM 调用侧（有缺陷）

```
_format_memory_retrieval()
  → 有记忆: "[相关记忆]\n· [07-14 15:00] xxx"  # ← 已有时间戳（L440-470）
  → 无记忆: "[元认知提示] 我没有找到相关记忆..."  # ← 兜底太弱
_generate_summary()
  → content[:150], summary[:500]             # ← 截断太狠
```

---

## 修复方案（10 个 Fix）

### Fix 0: `retrieve_memories_hybrid` 默认 `include_raw=True` [P0 最高优先级]

**v3 改动：改接口默认值，非改调用方传参**

```python
# memory_manager.py L420
async def retrieve_memories_hybrid(self, query: str, k: int = 5,
                                    use_reranker: bool = True,
                                    use_kg: bool = True,
                                    scope: Any | None = None,
                                    include_raw: bool = True) -> list[dict]:  # ← False → True
```

**为什么改默认而非改调用方**：
- `retrieve_memories_hybrid` 有 6+ 处调用：`retrieve_memories` 简单路径(L1117)、CRAG retry(L1139/L1208)、multi-query(L1389/L1431)
- 改默认一处修复全部，避免遗漏
- `include_raw=False` 参数仍可显式传入（保留接口语义）
- Reranker + Fix 8 去重自然处理 raw+distilled 共存

**安全论证**：
- 冷启动路由：cold 路径 FTS 现在也能返回 raw 记忆，向量兜底逻辑不变
- 温/热路径：FTS 和向量行为一致，都能返回 raw 记忆
- Fallback 路径（L625-640）：原行为就是 `is_raw_filter=None`（查全部），改为默认后此路径极少触发，保留无碍
- **不破坏 scope 隔离**：scope 过滤在 FTS SQL 和向量 JOIN 中独立于 is_raw

### Fix 1: 小时级时间词 + `_try_temporal_search` 默认 `include_raw=True` [P0]

**1a. `_TEMPORAL_PATTERNS` 增加小时级词**：

```python
_TEMPORAL_PATTERNS = [
    # 小时级（新增，必须排在天级前面，"刚才"可能被"刚"子串误匹配）
    (re.compile(r"刚才|刚刚"), 0, 0),              # 特殊：最近1小时
    (re.compile(r"(\d+)\s*小时前"), -1, -1),        # 动态：N小时前
    (re.compile(r"(\d+)\s*分钟前"), -1, -1),        # 动态：N分钟前
    # 天级（原有，顺序不变）
    (re.compile(r"大前天"), 3, 1),
    (re.compile(r"前天"), 2, 1),
    (re.compile(r"昨天|昨日"), 1, 1),
    (re.compile(r"今天|今日"), 0, 1),
    (re.compile(r"上周"), 7, 7),
    (re.compile(r"上个月|上月"), 30, 30),
    (re.compile(r"前几天|前些天|最近"), 1, 7),
]
```

**1b. `_parse_temporal_query` 增加动态解析**：

```python
def _parse_temporal_query(query: str) -> tuple[float, float] | None:
    # 新增：小时级匹配
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

    # "刚才/刚刚" → 最近1小时
    if re.search(r"刚才|刚刚", query):
        now = _datetime.datetime.now(_datetime.UTC).astimezone()
        start = now - _datetime.timedelta(hours=1)
        return start.timestamp(), now.timestamp()

    # 原有天级匹配逻辑...
    for pattern, offset_days, span_days in _TEMPORAL_PATTERNS:
        ...
```

**1c. `_try_temporal_search` 默认 `include_raw=True`**：

```python
# memory_manager.py L1244
async def _try_temporal_search(self, query: str, k: int,
                                scope: Any | None = None,
                                include_raw: bool = True) -> list[dict] | None:  # ← False → True
```

**1d. `TEMPORAL_KEYWORDS` 补充小时级词**：

```python
# query_transform.py L24
TEMPORAL_KEYWORDS: ClassVar[set[str]] = {
    "昨天", "前天", "今天", "上周", "上个月", "刚才", "之前", "那次", "那天", "那次对话",
    "刚刚", "小时前", "分钟前",  # 新增
}
```

### Fix 2: `restore_from_db` 摘要注入时间戳 [P0]

`conversation_logs` 表已有 `timestamp REAL NOT NULL` 列，直接使用：

```python
# agent_context.py L557-589
for row in rows:
    user_msg = row.get("user_message", "")
    asst_msg = row.get("assistant_reply", "")
    if not user_msg and not asst_msg:
        continue
    user_preview = user_msg[:60].replace("\n", " ") if user_msg else ""
    asst_preview = asst_msg[:60].replace("\n", " ") if asst_msg else ""
    # 新增：注入时间戳
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

**安全论证**：纯增补信息，不改已有格式结构，`[MM-DD HH:MM]` 前缀与 `_format_memory_retrieval` 已有格式一致。

### Fix 3: `_build_time_context` 增加对话间隔提示 [P0]

```python
# agent_context.py L316-358，在 return 前追加
gap_text = ""
if self._last_message_time > 0:
    gap_seconds = time.time() - self._last_message_time
    if gap_seconds < 60:
        gap_desc = "刚刚"
    elif gap_seconds < 3600:
        gap_desc = f"{int(gap_seconds / 60)}分钟前"
    elif gap_seconds < 86400:
        gap_desc = f"{int(gap_seconds / 3600)}小时前"
    elif gap_seconds < 2592000:  # 30天内
        gap_desc = f"{int(gap_seconds / 86400)}天前"
    else:
        gap_desc = ""  # 超过30天，不提示（可能是首次启动）
    if gap_desc:
        gap_text = f"距上次对话：{gap_desc}。如果间隔较长，不要用「刚才」「刚刚」等词指代上次对话内容。"

return (f"当前时间：{now.year}年{now.month}月{now.day}日 星期{weekday} "
        f"{hour:02d}:{minute:02d}（{period}）。这是小妲真切感受到的此刻，"
        f"是她回应时唯一参照的时间。历史消息中的任何时间表述均已过时，不得作为当前时间引用。"
        f"{gap_text}")
```

**安全论证**：
- `_last_message_time` 在 `signal_new_message()` 中更新（L153），每次用户消息触发
- 首次启动时 `_last_message_time=0.0`（L80），`gap_text` 为空，不影响
- 超过 30 天不提示，避免长期未使用时输出"873天前"之类的奇怪信息
- 不改变原有时间格式，只在末尾追加

### Fix 4: 记忆写入后立即失效查询缓存 [P1]

```python
# memory_manager.py L1832 附近，在 invalidate_memory_count_cache() 之后
self.invalidate_memory_count_cache()

# 新增：失效查询缓存，新记忆写入后立即可检索
if self._query_cache:
    self._query_cache.invalidate()
```

**QueryCache 需要增加 `invalidate()` 方法**（如果不存在）：

```python
# memory/query_cache.py
def invalidate(self) -> None:
    """清空全部缓存条目。"""
    self._cache.clear()
```

**安全论证**：
- `encode_memory` 每次对话触发 1 次，清空缓存开销极低（dict.clear()）
- 下次检索会重新填充缓存，TTL 机制不变
- 避免新记忆写入后 5 分钟内不可检索的窗口

### Fix 5: `_compute_recency_boost` 小时级粒度 [P1]

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

        # 小时级粒度（新增）
        if hours_ago <= 1:    return 1.0    # 1小时内
        if hours_ago <= 4:    return 0.95   # 4小时内
        if hours_ago <= 12:   return 0.90   # 半天内
        # 天级（保留原有分级，数值微调保持排序一致）
        if days_ago <= 0:     return 0.85   # 今天（超过12小时）
        if days_ago <= 1:     return 0.70   # 昨天
        if days_ago <= 7:     return 0.50   # 一周内
        if days_ago <= 30:    return 0.30   # 一个月内
        if days_ago <= 90:    return 0.20   # 三个月内
        return 0.10
    except Exception:
        return 0.3
```

**安全论证**：
- 保持"越新越高"的单调性，不破坏排序
- 原有 days_ago<=0 返回 1.0，现在拆分为 4 档（1h/4h/12h/today），更精确
- 一周内 0.50 不变，一个月内 0.30 不变，不影响中长期记忆排序
- 90天+ 从 0.3 降到 0.2，让很旧的记忆更难浮出，合理

### Fix 6: 强化幻觉兜底 [P1]

```python
# agent_context.py _format_memory_retrieval() 空结果分支
return ('[重要·元认知] 检索确认：没有找到与用户问题相关的记忆。'
        '绝对不要编造、推测或暗示记得过去发生的事。'
        '如实回答"我不记得了"或"我没有关于这件事的记忆"。'
        '如果用户提供了具体细节，可以基于这些细节继续对话，但不要虚构未提供的细节。')
```

**安全论证**：
- 只改空结果时的提示文本，不改有结果时的格式
- 增加"基于用户提供的细节继续对话"指引，避免过度保守拒绝合理推理

### Fix 7: `_generate_summary` 放宽截断 [P1]

```python
# memory_manager.py L2147-2158
def _generate_summary(self, exchanges: list[dict]) -> str:
    parts = []
    for msg in exchanges[-6:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and content:
            parts.append(f"用户说: {content[:250]}")   # 150 → 250
        elif role == "assistant" and content:
            parts.append(content[:250])                 # 150 → 250

    summary = "；".join(parts)
    return summary[:700]                                # 500 → 700
```

**安全论证**：
- 每条 250 字 × 最多 6 条 = 最多 1500 字，join 后截断 700 字
- 增量约 40%，对上下文窗口影响很小（700 vs 500 字符）
- 保留更多数字、名称、代码片段等关键细节

### Fix 8: RRF 融合后内容相似度去重 [P1 新增]

**问题**：`include_raw=True` 后，同一内容的 raw(is_raw=1) 和 distilled(is_raw=0) 都会进入候选池。它们有不同 ID，RRF/Reranker 可能都返回，浪费 top-k 位。

**方案**：在 Reranker 之后、返回之前，做一轮轻量内容去重：

```python
# memory_manager.py，在 Reranker 精排后、return 之前
def _dedup_by_content_similarity(self, results: list[dict], threshold: float = 0.7) -> list[dict]:
    """轻量内容去重：Jaccard 相似度 > threshold 的保留 final_score 更高的。"""
    if len(results) <= 1:
        return results
    kept = []
    for r in results:
        summary = r.get("summary", "")
        words = set(summary)  # 字符级 Jaccard（中文友好，无需分词）
        is_dup = False
        for k in kept:
            k_words = set(k.get("summary", ""))
            if not words or not k_words:
                continue
            jaccard = len(words & k_words) / len(words | k_words)
            if jaccard > threshold:
                # 保留 final_score 更高的
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

**调用位置**：在 `retrieve_memories_hybrid` 返回前，对 `results` 调用。

**性能**：字符级 Jaccard，O(n²) 但 n ≤ k（通常 ≤ 5），微秒级。

**安全论证**：
- 只在 top-k 结果内去重，不影响 RRF 融合和 Reranker 的候选集大小
- threshold=0.7 只去除高度相似的（蒸馏版几乎是原文的子集），不会误去不同记忆
- 保留 final_score 更高的 → 蒸馏版通常得分更高（更精炼、关键词更好），符合预期

### Fix 9: 蒸馏彻底失败时存原文+标记，避免失忆 [P0 提升]

**问题**：蒸馏 3 次全失败后，raw 记忆永远是 `is_raw=1`。Fix 0 虽然让它可检索，但：
- **致命缺陷**：raw 记忆的 `summary` 是 `_generate_summary()` 截断版（150字/条、500字总计），蒸馏失败后这个截断版是唯一版本，关键细节（人名/数字/代码/具体描述）全部丢失
- 每次启动 agent 都可能对同一 raw 记忆重试蒸馏（浪费 API）
- 无标记区分"待蒸馏"和"蒸馏失败降级"

**方案**：三步联动——传入原文 → 失败时回填原文 → 标记防重试

**Step 1：`encode_memory` 传入完整原文**

```python
# memory_manager.py encode_memory() 中，L1813-1820
# 构建 full_text（原始对话全文，上限2000字）
full_text_parts = []
for msg in exchanges[-6:]:
    role = msg.get("role", "")
    content = msg.get("content", "")
    if role == "user" and content:
        full_text_parts.append(f"用户说: {content}")
    elif role == "assistant" and content:
        full_text_parts.append(f"小妲: {content}")
full_text = "；".join(full_text_parts)[:2000]  # 2000字上限，足够保留细节

# 异步触发蒸馏（传入完整原文）
if self.distiller:
    try:
        _distill_task = asyncio.create_task(
            self._distill_to_knowledge(
                mem_id, summary, scope, importance, emotion,
                full_text=full_text  # ← 新增参数
            )
        )
```

**Step 2：`_distill_to_knowledge` 签名增加 `full_text`，失败时回填**

```python
async def _distill_to_knowledge(self, raw_id: int, summary: str,
                                 scope: Any, importance: float = 0.5,
                                 emotion: str = "", _retry: int = 0,
                                 full_text: str = "") -> None:  # ← 新增
    """..."""
    if not self.distiller:
        return

    # 新增：检查是否已标记蒸馏失败，跳过重试
    try:
        existing = await self.memory.get_memory_by_id(raw_id)
        if existing and existing.get("emotion_label", "").endswith("distill_failed"):
            logger.debug("memory.distill_skip_failed", raw_id=raw_id)
            return
    except Exception:
        pass

    try:
        distilled = await self.distiller.distill([{"summary": summary, "timestamp": time.time()}])
        if not distilled or not distilled.strip():
            if _retry < 2:
                delay = 30 * (_retry + 1)
                logger.info("memory.distill_empty_retry", raw_id=raw_id,
                           retry=_retry + 1, delay_s=delay)
                asyncio.get_event_loop().call_later(
                    delay,
                    lambda: asyncio.ensure_future(
                        self._distill_to_knowledge(raw_id, summary, scope,
                                                   importance, emotion, _retry + 1,
                                                   full_text=full_text)  # ← 透传
                    ),
                )
            else:
                # ★ 核心改动：3次全失败 → 回填原文 + 标记
                logger.warning("memory.distill_exhausted_retries", raw_id=raw_id)
                await self._save_fallback_raw(raw_id, summary, full_text)
            return
        # ... 原有蒸馏成功逻辑不变 ...
    except Exception as e:
        logger.warning("memory.distill_to_knowledge_failed",
                      raw_id=raw_id, retry=_retry, error=str(e))
        if _retry < 2:
            delay = 30 * (_retry + 1)
            asyncio.get_event_loop().call_later(
                delay,
                lambda: asyncio.ensure_future(
                    self._distill_to_knowledge(raw_id, summary, scope,
                                               importance, emotion, _retry + 1,
                                               full_text=full_text)  # ← 透传
                ),
            )
        else:
            # ★ 异常也走回填
            await self._save_fallback_raw(raw_id, summary, full_text)
```

**Step 3：`_save_fallback_raw` 回填原文方法**

```python
async def _save_fallback_raw(self, raw_id: int, truncated_summary: str,
                              full_text: str) -> None:
    """蒸馏失败时：用完整原文替换截断summary + 标记distill_failed + 重嵌入。

    原文优先于截断summary，保证记忆细节不丢失。
    如果full_text为空（兼容旧调用），仅做标记不回填。
    """
    try:
        # 1. 用完整原文替换截断summary（如果有原文且比当前长）
        if full_text and len(full_text) > len(truncated_summary):
            await self.memory.update_memory_summary(raw_id, full_text)
            logger.info("memory.fallback_raw_updated", raw_id=raw_id,
                       old_len=len(truncated_summary), new_len=len(full_text))
            # 2. 重新嵌入，让向量检索也用上完整原文
            if self.vec:
                try:
                    await self.vec.upsert(raw_id, full_text)
                except Exception as e:
                    logger.debug("memory.fallback_vec_upsert_failed", error=str(e))

        # 3. 标记蒸馏失败，避免后续重复重试
        await self.memory.update_emotion_label(raw_id, "distill_failed")
    except Exception as e:
        logger.warning("memory.fallback_save_failed", raw_id=raw_id, error=str(e))
```

**db_memory.py 需增加两个方法**：

```python
async def update_emotion_label(self, mem_id: int, label: str) -> None:
    """更新 emotion_label（用于蒸馏失败标记等）。"""
    await self._conn.execute(
        "UPDATE episodic_memories SET emotion_label = ? WHERE id = ?",
        (label, mem_id),
    )
    await self._conn.commit()

async def update_memory_summary(self, mem_id: int, new_summary: str) -> None:
    """更新记忆摘要（蒸馏失败时回填原文用）。"""
    await self._conn.execute(
        "UPDATE episodic_memories SET summary = ? WHERE id = ?",
        (new_summary, mem_id),
    )
    await self._conn.commit()
```

**安全论证**：
- `full_text` 上限 2000 字，不会撑爆上下文（原始 _generate_summary 500字 → 回填后最多 2000 字，增量可控）
- 回填后重新嵌入（`vec.upsert`），向量检索也用完整原文，召回质量同步提升
- `emotion_label` 标记复用已有字段，零迁移成本
- 标记后 raw 记忆仍可检索（Fix 0），且 summary 是完整原文而非截断版
- 如果用户后续配置了有效 API key，可批量清除 `distill_failed` 标记 + 用原文重新蒸馏
- `full_text` 为空时降级为仅标记（兼容旧调用和外部直接调用 `_distill_to_knowledge` 的场景）
- **不破坏 Memory Governance**：回填原文是更新 summary 而非篡改哈希链，Governance 的 `record_initial_version` 在写入时已记录初始版本，后续更新走正常 UPDATE 路径

### Fix 10: `_hybrid_vec_search` 增加 is_raw 过滤一致性 [P2 新增]

**问题**：FTS 路径通过 `search_memories_fts_scoped(is_raw=...)` 过滤，向量路径通过 `get_memories_by_ids()` 不过滤。行为不一致。

**方案**：`_hybrid_vec_search` 增加 `is_raw` 参数，在 `get_memories_by_ids` 后做后过滤：

```python
async def _hybrid_vec_search(self, query: str, k: int,
                             candidate_ids: list[int] | None = None,
                             is_raw: int | None = None) -> list[dict]:  # 新增参数
    ...
    vec_mems = await self.memory.get_memories_by_ids(vec_ids)
    # 新增：is_raw 后过滤（与 FTS 行为一致）
    if is_raw is not None:
        vec_mems = [m for m in vec_mems if m.get("is_raw") == is_raw]
    ...
```

调用处传入 `is_raw_filter`：

```python
# retrieve_memories_hybrid 中并行调用
fts_items, vec_items, ... = await asyncio.gather(
    self._hybrid_fts_search_scoped(query, recall_limit, scope, is_raw_filter),
    self._hybrid_vec_search(query, recall_limit, candidate_ids=candidate_ids, is_raw=is_raw_filter),  # 新增
    ...
)
```

**冷启动兜底路径也需同步**：

```python
# cold 路径向量兜底
vec_items = await self._hybrid_vec_search(query, recall_limit, is_raw=is_raw_filter)
```

**安全论证**：
- `include_raw=True` → `is_raw_filter=None` → 不过滤，行为与原来一致
- `include_raw=False` → `is_raw_filter=0` → FTS 和向量都只返回蒸馏知识，行为一致
- 不改变向量检索逻辑本身，只在结果集做轻量过滤

---

## 实施优先级

| 优先级 | Fix | 预期效果 | 复杂度 | 风险 |
|--------|-----|----------|--------|------|
| **P0** | **Fix 0: 默认 include_raw=True** | **刚发生的事立即可检索** | **极低（改1个默认值）** | **极低** |
| P0 | Fix 1: 小时级时间词 + temporal include_raw | "刚才/几小时前"正确解析+检索 | 中 | 低 |
| P0 | Fix 2: 摘要时间戳 | LLM知道每条历史什么时候的 | 低 | 极低 |
| P0 | Fix 3: 对话间隔提示 | 不再把4小时前说成"刚才" | 低 | 极低 |
| P1 | Fix 4: 缓存失效 | 新记忆写入后立即可检索 | 低 | 极低 |
| P1 | Fix 5: 小时级recency | 更精确的时间排序 | 低 | 低 |
| P1 | Fix 6: 幻觉兜底 | 减少编造 | 低 | 极低 |
| P1 | Fix 7: 摘要截断放宽 | 更多细节保留 | 低 | 极低 |
| P1 | Fix 8: 内容去重 | raw+distilled不冗余 | 低 | 低 |
| **P0** | **Fix 9: 蒸馏失败存原文+标记** | **蒸馏失败后原文本不丢失，避免失忆** | **中** | **低** |
| P2 | Fix 10: 向量is_raw一致性 | FTS/向量行为统一 | 低 | 极低 |

## 实施顺序

```
第一批（P0，解决"不记得"核心问题）：
  Fix 0 → Fix 1 → Fix 9 → Fix 2 → Fix 3

第二批（P1，优化检索质量和防幻觉）：
  Fix 4 → Fix 8 → Fix 5 → Fix 6 → Fix 7

第三批（P2，一致性健壮性）：
  Fix 10
```

Fix 0 必须第一个实施——1 行代码改动，立刻解决"刚发生的事不记得"的致命问题。

## 测试验证

1. ✅ 说话后立即问"刚才说了什么" → 应能召回（Fix 0 生效）
2. ✅ 说话后5分钟内问相关细节 → 应能召回（Fix 0 + Fix 4 生效）
3. ✅ 4小时后问"刚才说了什么" → 提示"距上次对话4小时"，不说"刚才"（Fix 3 生效）
4. ✅ 暂停对话后重新开始 → 显示当前时间，不延续旧时间（Fix 2 + Fix 3 生效）
5. ✅ 问"3小时前说了什么" → 按时间范围检索（Fix 1 生效）
6. ✅ 问完全不记得的事 → 诚实说"不记得"，不编造（Fix 6 生效）
7. ✅ 同一内容 raw+distilled 都存在 → 只返回1条（Fix 8 生效）
8. ✅ 蒸馏失败3次的记忆 → 原文回填（summary=完整原文，非截断版），向量重嵌入，不再重试蒸馏（Fix 0 + Fix 9 生效）
9. ✅ 向量检索与FTS检索 is_raw 行为一致（Fix 10 生效）

## 不需要修改的部分

以下组件经审计确认无问题，**明确不改动**：

- `encode_memory()` 写入逻辑：is_raw=1 先写 + 异步蒸馏，设计正确
- `_distill_to_knowledge()` 蒸馏逻辑：重试机制合理，只加失败标记
- FSRS-DSR 评分：`_apply_fsrs_scoring` + `_compute_final_scores` 逻辑正确
- RRF 融合：`reciprocal_rank_fusion` 权重配置合理
- 冷/温/热路由：三段路由逻辑正确，改 is_raw 默认值即可
- CRAG 评估：`RetrievalAssessor` 逻辑正确
- QueryCache 语义缓存：机制正确，只需加 invalidate
- Scope 隔离：user_id + agent_id 过滤正确
- Entity/KG/Spreading/ChildChunk 通道：逻辑正确
- `_format_memory_retrieval()`：已有时间戳注入（L440-470），只改空结果兜底
- `query_transform.py`：rewrite/expand/classify_intent 逻辑正确
