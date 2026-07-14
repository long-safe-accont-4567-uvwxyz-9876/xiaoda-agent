# 记忆检索机制修复 Design v3

> 日期: 2026-07-14
> 基于: memory-retrieval-fix-v3.md 全链路审计
> 状态: 已批准，待实施

## 概述

修复记忆检索系统的 10 个缺陷，解决"刚发生的事不记得"的核心问题。

## 修改文件清单

| 文件 | Fix | 修改内容 |
|------|-----|---------|
| `memory/memory_manager.py` | 0/1/4/5/7/8/9/10 | 核心检索逻辑修复 |
| `agent_context.py` | 2/3/6 | 上下文恢复与时间提示 |
| `memory/query_transform.py` | 1d | TEMPORAL_KEYWORDS 补充 |
| `memory/query_cache.py` | 4 | 增加 invalidate() 方法 |
| `db/db_memory.py` | 9 | 增加 update_emotion_label/update_memory_summary |

## 实施顺序

### 第一批 P0（解决"不记得"核心问题）

1. **Fix 0**: `retrieve_memories_hybrid` 默认 `include_raw=True`（1行改动）
2. **Fix 1**: 小时级时间词 + `_try_temporal_search` 默认 `include_raw=True`
3. **Fix 9**: 蒸馏失败时存原文+标记，避免失忆
4. **Fix 2**: `restore_from_db` 摘要注入时间戳
5. **Fix 3**: `_build_time_context` 增加对话间隔提示

### 第二批 P1（优化检索质量和防幻觉）

6. **Fix 4**: 记忆写入后立即失效查询缓存
7. **Fix 8**: RRF 融合后内容相似度去重
8. **Fix 5**: `_compute_recency_boost` 小时级粒度
9. **Fix 6**: 强化幻觉兜底
10. **Fix 7**: `_generate_summary` 放宽截断

### 第三批 P2（一致性健壮性）

11. **Fix 10**: `_hybrid_vec_search` 增加 is_raw 过滤一致性

## 不修改的部分

FSRS-DSR 评分、RRF 融合、冷/温/热路由、CRAG 评估、QueryCache 语义缓存机制、Scope 隔离、Entity/KG/Spreading/ChildChunk 通道、encode_memory 写入逻辑。