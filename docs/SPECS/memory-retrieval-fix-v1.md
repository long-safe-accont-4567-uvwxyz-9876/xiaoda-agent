# 记忆检索机制修复 Spec v1.0

> 日期: 2026-07-14
> 状态: 待实施
> 优先级: P1（用户体验直接受损）

## 问题现象

用户反馈 4 个具体问题：

1. **检索不到几小时前的事**：明明是几个小时之前发生的，检索不到
2. **时间感知错误**：过了4小时还说"刚才"
3. **上下文暂停/恢复时间不更新**：暂停后第二次启动，继续上一段时间点的对话，没有用当前时间
4. **胡编乱造/记不住**：给了具体时间也找不到，就编造内容

## 根因分析

### 根因1: `_TEMPORAL_PATTERNS` 缺少"刚才/刚刚/刚刚"等短时词（问题1+2的直接原因）

`memory/memory_manager.py` L36-44 的 `_TEMPORAL_PATTERNS` 只支持天级时间词：
- 大前天、前天、昨天、今天、上周、上个月、前几天/最近

**完全没有**"刚才"、"刚刚"、"几小时前"、"1小时前"这类小时级时间词。

但 `query_transform.py` L24 的 `TEMPORAL_KEYWORDS` 包含"刚才"——这意味着：
- `classify_intent()` 把"刚才"识别为 `temporal` 意图 ✓
- `_try_temporal_search()` 调用 `_parse_temporal_query()` 但返回 `None` ✗
- 退回常规检索，但常规检索不按时间过滤 → 召回的可能是几天前的记忆

**影响**: 用户说"刚才说了什么"/"几小时前那个"时，时间解析直接失效，退回无时间约束的语义检索，大概率召回不相关的旧记忆或空结果。

### 根因2: `restore_from_db` 无时间戳注入（问题3的直接原因）

`agent_context.py` L557-589 的 `restore_from_db()` 从 `conversation_logs` 取最近10条，拼接成摘要：
```
· 爸爸: xxx → 小妲: yyy
```

**问题**: 摘要中完全没有时间戳！LLM看到这些摘要时不知道每条是什么时候发生的，只能按顺序推测"最近的"，无法区分4小时前和4天前。

对比 `_format_memory_retrieval()`（L440-470）已经在每条记忆前注入 `[MM-DD HH:MM]` 时间戳——但 `restore_from_db` 的摘要格式没有。

### 根因3: `_build_time_context` 只注入当前时间，无"距上次对话多久"（问题2+3）

`agent_context.py` L316-358 的 `_build_time_context()` 只告诉 LLM "当前时间是 XXX"，但没有告诉 LLM "距上次对话已经过了 X 小时"。

LLM 只知道现在几点，但不知道上次对话是什么时候结束的 → 自然会延续上次的"刚才"语义。

### 根因4: `QUERY_CACHE_TTL=300` 缓存5分钟内返回旧结果（问题1的间接原因）

`memory_manager.py` L259 设置 `QUERY_CACHE_TTL=300`（5分钟）。如果用户5分钟内问相似问题，直接返回缓存的检索结果。

**问题**: 这5分钟内新存入的记忆不会被反映到缓存结果中。比如用户刚说了一件事，再问"刚才说了什么"，缓存命中返回的是旧结果（没有包含刚说的那条）。

### 根因5: `_compute_recency_boost` 粒度太粗（问题1的间接原因）

`memory_manager.py` L1516-1543 的 `_compute_recency_boost`:
- 今天 → 1.0
- 7天内 → 0.8
- 30天内 → 0.5

**问题**: "今天"内所有记忆都是1.0，无法区分2小时前和8小时前。对于"刚才说了什么"这种需要小时级区分的查询，这个粒度完全不够。

### 根因6: LLM幻觉兜底不足（问题4的直接原因）

当检索返回空结果时，`_format_memory_retrieval()` L447 会注入：
> [元认知提示] 我没有找到相关记忆。如果用户问的是过去的事，请诚实说"我不记得了"

但这个提示是 volatile 层的，被其他上下文冲淡后 LLM 可能忽略，选择编造。

## 修复方案

### Fix 1: `_TEMPORAL_PATTERNS` 增加小时级时间词 [P0]

```python
_TEMPORAL_PATTERNS = [
    # 小时级（新增）
    (re.compile(r"刚才|刚刚|刚"), 0, 0),        # 最近1小时内（offset=0, span=0 → 特殊处理为最近1小时）
    (re.compile(r"(\d+)\s*小时前"), -1, -1),     # N小时前（需动态解析，-1标记为动态）
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
- `span=0` 时返回 `[now - 1h, now]`（最近1小时）
- 支持 `N小时前` / `N分钟前` 的动态解析

### Fix 2: `restore_from_db` 摘要注入时间戳 [P0]

```python
# 当前（无时间）:
summaries.append(f"· {term}: {user_preview} → 小妲: {asst_preview}")

# 修复后（带时间）:
ts = row.get("timestamp", 0)
if ts:
    time_str = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    summaries.append(f"· [{time_str}] {term}: {user_preview} → 小妲: {asst_preview}")
else:
    summaries.append(f"· {term}: {user_preview} → 小妲: {asst_preview}")
```

### Fix 3: `_build_time_context` 增加"距上次对话时间间隔" [P0]

```python
# 在时间语境末尾追加:
if self._last_message_time > 0:
    gap_seconds = now_ts - self._last_message_time
    if gap_seconds < 60:
        gap_desc = "刚刚"
    elif gap_seconds < 3600:
        gap_desc = f"{int(gap_seconds / 60)}分钟前"
    elif gap_seconds < 86400:
        gap_desc = f"{int(gap_seconds / 3600)}小时前"
    else:
        gap_desc = f"{int(gap_seconds / 86400)}天前"
    gap_text = f"距上次对话: {gap_desc}。如果间隔较长，不要用'刚才'等词指代上次对话。"
```

### Fix 4: 记忆写入后立即失效查询缓存 [P1]

在 `insert_episodic_memory` 完成后调用 `self._query_cache.invalidate()`，确保下次检索能命中新记忆。

或者更精细：只失效与新记忆语义相似的缓存条目（但成本较高，先走全量失效）。

### Fix 5: `_compute_recency_boost` 增加小时级粒度 [P1]

```python
# 当前: 今天=1.0, 7天内=0.8
# 修复后: 
hours_ago = (now - dt).total_seconds() / 3600
if hours_ago <= 1: return 1.0      # 1小时内
if hours_ago <= 4: return 0.9      # 4小时内
if hours_ago <= 12: return 0.85    # 半天内
if days_ago <= 0: return 0.8       # 今天
if days_ago <= 1: return 0.7       # 昨天
if days_ago <= 7: return 0.5       # 一周内
```

### Fix 6: 强化幻觉兜底——空检索时注入更强的元认知提示 [P1]

在 `_format_memory_retrieval()` 返回空结果时，提升元认知提示的强度和位置：

```python
# 将元认知提示从 volatile 层移到 system 层（更显眼）
# 或者在 prompt 中用更强的措辞:
return '[重要] 检索确认：我没有找到与用户查询相关的记忆。绝对不要编造或推测过去发生的事。如实回答"我不记得了"或"我不太确定"。'
```

## 实施优先级

| 优先级 | Fix | 预期效果 | 复杂度 |
|--------|-----|----------|--------|
| P0 | Fix 1: 小时级时间词 | "刚才/几小时前"能正确解析时间范围 | 中 |
| P0 | Fix 2: 摘要时间戳 | LLM知道每条历史是什么时候的 | 低 |
| P0 | Fix 3: 对话间隔提示 | LLM不再把4小时前说成"刚才" | 低 |
| P1 | Fix 4: 缓存失效 | 新记忆立即可检索 | 低 |
| P1 | Fix 5: 小时级recency | 更精确的时间排序 | 低 |
| P1 | Fix 6: 幻觉兜底 | 减少编造 | 低 |

## 测试验证

1. 说话后5分钟内问"刚才说了什么" → 应能召回
2. 4小时后问"刚才说了什么" → 应提示"距上次对话4小时"，不说"刚才"
3. 暂停对话后重新开始 → 应显示当前时间，不延续旧时间
4. 问"3小时前说了什么" → 应按时间范围检索
5. 问完全不记得的事 → 应诚实说"不记得"，不编造
