# 记忆检索机制修复 Spec v2.0

> 日期: 2026-07-14
> 状态: 待实施
> 优先级: P0（记忆系统根本缺陷，大量刚发生的事检索不到）

## 问题现象

用户反馈的核心问题：

1. **刚发生的事也不记得**：哪怕是压缩了上下文，问到相关细节时应该记得，但检索不到
2. **检索不到几小时前的事**：明明是几个小时之前发生的，检索不到
3. **时间感知错误**：过了4小时还说"刚才"
4. **上下文暂停/恢复时间不更新**：暂停后第二次启动，继续上一段时间点的对话
5. **胡编乱造/记不住**：给了具体时间也找不到，就编造内容

---

## 根因分析（按严重度排序）

### 🔴 根因0（P0致命）: `include_raw=False` 导致默认检索只查蒸馏后的知识，跳过所有原始记忆

**这是"刚发生的事不记得"的致命根因。**

完整链路：
1. `encode_memory()` 写入时 `is_raw=1`（原始记忆，L1736）
2. 异步触发 `_distill_to_knowledge()` 蒸馏为 `is_raw=0`（提炼知识，L1814-1830）
3. **蒸馏依赖硅基流动免费模型**，如果 API key 未配置 / 模型超时 / 速率限制 → 蒸馏失败 → 原始记忆永远是 `is_raw=1`
4. `retrieve_memories_hybrid()` 默认 `include_raw=False`（L420），导致 `is_raw_filter=0`（L449）
5. **FTS 检索 `search_memories_fts_scoped()` 加了 `AND em.is_raw = 0`**（L220-221）→ 跳过所有 `is_raw=1` 的原始记忆
6. 向量检索同样受 scope + is_raw 过滤

**结果：如果蒸馏没成功，刚写入的记忆在检索时完全不可见！**

即使蒸馏成功，也存在时间窗口问题：
- 原始记忆写入是同步的（毫秒级）
- 蒸馏是异步的（1-5秒，取决于免费模型响应）
- 在蒸馏完成前问任何问题 → 检索不到刚写入的记忆
- `QUERY_CACHE_TTL=300` 可能缓存了蒸馏前的空结果，5分钟内都不可能检索到

**影响范围**：所有新写入的记忆在蒸馏完成前都不可见。蒸馏失败则永久不可见。

### 🔴 根因1（P0）: `_TEMPORAL_PATTERNS` 缺少小时级时间词

`memory_manager.py` L36-44 只支持天级：大前天、前天、昨天、今天、上周、上个月、前几天

**完全没有**"刚才"、"刚刚"、"几小时前"、"N小时前"。

而 `query_transform.py` L24 的 `TEMPORAL_KEYWORDS` 包含"刚才"——意味着：
- `classify_intent()` 把"刚才"识别为 `temporal` ✓
- `_try_temporal_search()` 调用 `_parse_temporal_query()` 返回 `None` ✗
- 退回常规检索，不按时间过滤 → 召回旧记忆或空

### 🟡 根因2（P0）: `restore_from_db` 摘要无时间戳

`agent_context.py` L557-589 的 `restore_from_db()` 摘要格式：
```
· 爸爸: xxx → 小妲: yyy
```
LLM 完全不知道每条是什么时候发生的，无法区分4小时前和4天前。

对比 `_format_memory_retrieval()`（L440-470）已经在每条前注入 `[MM-DD HH:MM]` 时间戳。

### 🟡 根因3（P0）: `_build_time_context` 只注入当前时间，无"距上次对话多久"

`agent_context.py` L316-358 只告诉 LLM "当前时间是14:00"，不告诉"距上次对话过了4小时"→ LLM 延续"刚才"语义。

### 🟡 根因4（P1）: `QUERY_CACHE_TTL=300` 新记忆写入后5分钟内不可检索

`memory_manager.py` L259 缓存TTL=300秒。用户刚说了一件事，再问"刚才说了什么"→缓存命中返回旧结果。

更致命的是：`encode_memory()` 完成后没有调用 `_query_cache.invalidate()`（L1832-1837只调了 `invalidate_memory_count_cache`，没失效查询缓存）。

### 🟡 根因5（P1）: `_compute_recency_boost` 粒度太粗

"今天"内所有记忆都是1.0，无法区分2小时前和8小时前。

### 🟡 根因6（P1）: LLM幻觉兜底不足

空检索时 `_format_memory_retrieval()` L447 的元认知提示在 volatile 层，容易被冲淡。

### 🟠 根因7（P1）: `_generate_summary` 截断太狠，丢失细节

`memory_manager.py` L2147-2158 的 `_generate_summary()`:
- 每条消息截断到150字符（`content[:150]`）
- 最终summary截断到500字符
- 用户说的关键细节（数字、名称、代码片段）在150字符外就丢了

这导致即使记忆被正确写入和检索，summary中也没有足够细节回答用户的追问。

---

## 修复方案

### Fix 0: 默认检索包含原始记忆 (`include_raw=True`) [P0 最高优先级]

**推荐方案：检索时默认 `include_raw=True`，在 RRF 融合后由 Reranker 自然去重**

```python
# memory_manager.py - retrieve_memories 调用 retrieve_memories_hybrid 时:
results = await self.retrieve_memories_hybrid(
    query, k=k, use_reranker=use_reranker, use_kg=use_kg,
    include_raw=True  # ← 关键修改：默认包含原始记忆
)
```

理由：
- 实现最简单（改调用方传参）
- 原始记忆和蒸馏记忆可能重复，但 Reranker + FSRS-DSR 评分自然让蒸馏知识（更精炼）排前面
- 即使蒸馏失败/延迟，原始记忆仍可检索
- 不改变 retrieve_memories_hybrid 的接口语义（include_raw 参数仍可按需传 False）

### Fix 1: `_TEMPORAL_PATTERNS` 增加小时级时间词 [P0]

```python
_TEMPORAL_PATTERNS = [
    # 小时级（新增）
    (re.compile(r"刚才|刚刚|刚"), 0, 0),        # 特殊处理为最近1小时
    (re.compile(r"(\d+)\s*小时前"), -1, -1),     # N小时前（动态解析）
    (re.compile(r"(\d+)\s*分钟前"), -1, -1),     # N分钟前
    # 天级（原有）
    (re.compile(r"大前天"), 3, 1),
    (re.compile(r"前天"), 2, 1),
    (re.compile(r"昨天|昨日"), 1, 1),
    (re.compile(r"今天|今日"), 0, 1),
    (re.compile(r"上周"), 7, 7),
    (re.compile(r"上个月|上月"), 30, 30),
    (re.compile(r"前几天|前些天|最近"), 1, 7),
]
```

同时修改 `_parse_temporal_query()`：
- `span=0` 时返回 `[now - 1h, now]`
- 支持 `N小时前` / `N分钟前` 动态解析

### Fix 2: `restore_from_db` 摘要注入时间戳 [P0]

```python
ts = row.get("timestamp", 0)
if ts:
    time_str = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    summaries.append(f"· [{time_str}] {term}: {user_preview} → 小妲: {asst_preview}")
else:
    summaries.append(f"· {term}: {user_preview} → 小妲: {asst_preview}")
```

### Fix 3: `_build_time_context` 增加对话间隔提示 [P0]

```python
if self._last_message_time > 0:
    gap_seconds = now_ts - self._last_message_time
    if gap_seconds < 60: gap_desc = "刚刚"
    elif gap_seconds < 3600: gap_desc = f"{int(gap_seconds / 60)}分钟前"
    elif gap_seconds < 86400: gap_desc = f"{int(gap_seconds / 3600)}小时前"
    else: gap_desc = f"{int(gap_seconds / 86400)}天前"
    gap_text = f"距上次对话: {gap_desc}。如果间隔较长，不要用'刚才'等词指代上次对话。"
```

### Fix 4: 记忆写入后立即失效查询缓存 [P1]

在 `encode_memory()` L1832 附近加：
```python
if self._query_cache:
    self._query_cache.invalidate()
```

### Fix 5: `_compute_recency_boost` 小时级粒度 [P1]

```python
hours_ago = (now - dt).total_seconds() / 3600
if hours_ago <= 1: return 1.0      # 1小时内
if hours_ago <= 4: return 0.9      # 4小时内
if hours_ago <= 12: return 0.85    # 半天内
if days_ago <= 0: return 0.8       # 今天
if days_ago <= 1: return 0.7       # 昨天
if days_ago <= 7: return 0.5       # 一周内
```

### Fix 6: 强化幻觉兜底 [P1]

```python
return '[重要] 检索确认：没有找到相关记忆。绝对不要编造或推测过去发生的事。如实回答"我不记得了"。'
```

### Fix 7: `_generate_summary` 增加截断长度 [P1]

```python
# 当前: content[:150], summary[:500]
# 修改: content[:300], summary[:800]
```

---

## 实施优先级

| 优先级 | Fix | 预期效果 | 复杂度 |
|--------|-----|----------|--------|
| **P0** | **Fix 0: include_raw=True** | **刚发生的事立即可检索** | **极低（改1个传参）** |
| P0 | Fix 1: 小时级时间词 | "刚才/几小时前"能正确解析 | 中 |
| P0 | Fix 2: 摘要时间戳 | LLM知道每条历史什么时候的 | 低 |
| P0 | Fix 3: 对话间隔提示 | 不再把4小时前说成"刚才" | 低 |
| P1 | Fix 4: 缓存失效 | 新记忆写入后立即可检索 | 低 |
| P1 | Fix 5: 小时级recency | 更精确的时间排序 | 低 |
| P1 | Fix 6: 幻觉兜底 | 减少编造 | 低 |
| P1 | Fix 7: 摘要截断长度 | 更多细节保留 | 低 |

## 测试验证

1. 说话后立即问"刚才说了什么" → 应能召回（Fix 0 生效）
2. 说话后5分钟内问相关细节 → 应能召回（Fix 0 + Fix 4 生效）
3. 4小时后问"刚才说了什么" → 提示"距上次对话4小时"，不说"刚才"（Fix 3 生效）
4. 暂停对话后重新开始 → 显示当前时间，不延续旧时间（Fix 2 + Fix 3 生效）
5. 问"3小时前说了什么" → 按时间范围检索（Fix 1 生效）
6. 问完全不记得的事 → 诚实说"不记得"，不编造（Fix 6 生效）
