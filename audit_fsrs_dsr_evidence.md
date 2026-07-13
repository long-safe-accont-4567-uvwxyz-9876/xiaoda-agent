# FSRS-DSR 审计证据文件

**生成日期**: 2026-07-12

---

## Evidence Block #1 — _apply_forget S_new 公式错误

- **文件**: memory/fsrs_model.py:152
- **声明**: `S_new = S * 0.5 * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)` 用当前 S 替代 S_0（初始稳定性），导致大 S 时遗忘后 S 反而暴涨
- **代码原文**: `S_new = S * 0.5 * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)`
- **数值验证**: S=300, D=5 → S_new = 300 × 0.5 × 0.83 × 3.13 ≈ 390 > S=300（遗忘后 S 暴涨 30%）
- **标准 FSRS**: S_new = S_0 × D^(-0.3) × ((S+1)^0.2 - 1)，S_0=初始稳定性=3.0 → S_new = 3.0 × 0.83 × 3.13 ≈ 7.8

## Evidence Block #2 — confirm_correct created_at 取错字段

- **文件**: memory/confirm_correct.py:70
- **声明**: created_at 错误地从 last_review 取值
- **代码原文**: `created_at=node.get("last_review", 0.0) or now_ts`
- **影响**: last_review=0.0 → falsy → now_ts，导致 BUFFER 阶段的 21 天门槛从"现在"开始计时

## Evidence Block #3 — concept_nodes 缺 created_at REAL 列

- **文件**: db/database.py v14 迁移 + memory/confirm_correct.py
- **声明**: concept_nodes 只有 created TEXT（ISO 字符串），无 created_at REAL
- **验证**: db_concept.py insert_node 参数无 created_at，v15 迁移也只增加 difficulty/stability/phase/last_review/reinforcement_count
- **影响**: 所有基于 concept_nodes 构建 MemoryState 的代码（confirm_correct）无法获取正确的 created_at

## Evidence Block #4 — consolidate_from_db difficulty 硬编码 1.0

- **文件**: core/dream_consolidation.py:242
- **代码原文**: `difficulty=1.0`
- **对比**: 同文件 line 176 的 consolidate_db 正确使用 `difficulty=mem.get("difficulty", 5.0)`
- **影响**: difficulty=1.0 → difficulty_factor=(10-1)/9=1.0（最大值），虚增 S 增长

## Evidence Block #5 — 新记忆 last_review=0 导致 R≈0 被过滤

- **文件**: memory/memory_manager.py:1651-1760 (encode_memory)
- **声明**: encode_memory 不设置 last_review，默认 0
- **验证**: insert_episodic_memory 不传入 last_review 参数，v15 迁移 DEFAULT 0
- **计算**: last_review=0 → elapsed_days ≈ 20500 → R = exp(-20500/3.0) ≈ 0 → should_filter(R=0) = True → 记忆被过滤
- **缓解**: _apply_fsrs_scoring line 1489 的 `last_review=0.0 or timestamp` fallback 到 timestamp，但不影响直接调用 MemoryState 的场景

## Evidence Block #6 — _apply_fsrs_scoring 每次实例化 FSRSModel

- **文件**: memory/memory_manager.py:1480
- **代码原文**: `_fsrs = FSRSModel()`
- **声明**: 每次检索评分都创建新实例
- **对比**: confirm_correct.py:29 和 dream_consolidation.py:61 都在 __init__ 中创建实例

## Evidence Block #7 — _apply_recall 双重 MemoryState 构造

- **文件**: memory/fsrs_model.py:135-146
- **代码原文**: 先构造临时 MemoryState（line 135-140）调 .transition()，再构造最终 MemoryState（line 141-146）
- **问题**: reinforcement_count + 1 出现两次（line 139 和 145），语义上只应 +1 一次，但两处值相同所以结果巧合正确

## Evidence Block #8 — Dream consolidate 不回写 FSRS 状态

- **文件**: core/dream_consolidation.py:155-290
- **声明**: consolidate_from_db 修改内存中 Memory 对象的 strength/importance，但不 UPDATE 数据库的 stability/difficulty/phase
- **验证**: 方法内无任何 `await memory_db.update_fsrs_state()` 或类似调用
- **影响**: 长期未 confirm 的记忆 FSRS 状态永远停留在初始值

## Evidence Block #9 — auto_link 每条边单独 commit

- **文件**: db/db_concept.py:131, 169
- **代码原文**: `await self._conn.commit()` 在 create_edge 和 auto_link 内部
- **声明**: auto_link 对 N 个匹配节点调用 2N 次 create_edge，每次 commit
- **影响**: O(N²) 磁盘 I/O

## Evidence Block #10 — INSERT OR REPLACE 覆盖 FSRS 状态

- **文件**: db/db_concept.py:44-56
- **代码原文**: `INSERT OR REPLACE INTO concept_nodes ...`
- **声明**: 主键冲突时删除旧行并插入新行，旧 FSRS 状态被清零
- **场景**: confirm_correct.correct() 创建新节点时，如果 md5[:12] 碰撞

## Evidence Block #11 — Dream strength=R 正反馈衰减

- **文件**: core/dream_consolidation.py:112-113
- **代码原文**: `m.strength = R`
- **计算**: 初始 strength=1.0 → R(30天, S=10) ≈ 0.05 → strength=0.05 → 下次 S=0.5 → R ≈ 0 → 被归档
- **影响**: 记忆一旦开始遗忘就加速被丢弃，与 Ebbinghaus 减速衰减矛盾

## Evidence Block #12 — FluidMemory 兼容层 S 线性公式与 FSRS 指数衰减不匹配

- **文件**: memory/fluid_memory.py:30-55
- **代码原文**: `stability = S_INIT + access_count * self.STABILITY_PER_ACCESS` = 3 + count × 14
- **计算**: access_count=4 → S=59 → R(30天) = exp(-30/59) ≈ 0.60，而旧 PERMANENT 逻辑 R=1.0

## Evidence Block #13 — MemoryPhase 枚举值容错缺失

- **文件**: memory/fsrs_model.py:8-14 + memory/memory_manager.py:1488
- **代码原文**: `MemoryPhase(r.get("phase", "buffer"))`
- **声明**: 数据库存了非法字符串时 ValueError 无 try-except 保护

## Evidence Block #14 — v15 迁移 last_review DEFAULT 0 语义冲突

- **文件**: db/database.py:999-1001
- **代码原文**: `ALTER TABLE ... ADD COLUMN last_review REAL DEFAULT 0`
- **声明**: last_review=0 表示 Unix epoch，非 "从未复习"语义
- **对比**: _apply_fsrs_scoring 用 `0.0 or timestamp` 做了 workaround，但其他入口（dream, confirm）无此保护

## Evidence Block #15 — confirm_correct.correct() 新节点不继承 FSRS 状态

- **文件**: memory/confirm_correct.py:119-132
- **声明**: insert_node 调用未传入 difficulty/stability/phase/last_review/reinforcement_count
- **验证**: insert_node 参数列表有这些参数（db_concept.py:37-42），但 correct() 未传入
- **影响**: 纠正后的记忆从零开始 FSRS 评分
