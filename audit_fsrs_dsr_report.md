# FSRS-DSR 全链路代码质量审计报告

**审计日期**: 2026-07-12  
**审计范围**: nahida-agent 记忆系统层 — FSRS-DSR 全链路（10 文件）  
**审计维度**: FSRS 算法正确性 / 兼容层 / 数据库迁移幂等性 / 类型安全 / 异步安全

---

## Bug 清单（按 P0 → P1 → P2 排序）

### P0 — 算法错误 / 数据丢失风险

#### Bug #1 — `_apply_forget` 中 D^(-0.3) 在 D→1 边界时 S_new 可无限趋近 0

**文件**: `memory/fsrs_model.py`  
**行号**: 152  
**描述**: `_apply_forget` 公式 `S_new = S * 0.5 * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)`。当 D=1（下限值）时，`D ** (-0.3) ≈ 1.0`，这不是问题。但核心问题在于 `((S + 1.0) ** 0.2) - 1.0` 这一项：当 S 很大（如 S=300，兼容层上限）时，`(301 ** 0.2) - 1.0 ≈ 3.13`，S_new = 300 × 0.5 × 1.0 × 3.13 ≈ 470，**遗忘了反而 S 暴涨**，与 FSRS 原始 forget 公式语义相反（遗忘应降低 S）。原始 FSRS 公式是 `S_new = S_0 * D^(-0.3) * ((S+1)^0.2 - 1)` 其中 S_0 是初始稳定性而非当前 S，这里错把 S 当作 S_0。  
**修复建议**: 将公式改为 `S_new = S_INIT * (D ** (-0.3)) * (((S + 1.0) ** 0.2) - 1.0)`，其中 `S_INIT = 3.0`，与标准 FSRS-4.5/5 公式对齐。当 S=300 时 S_new = 3.0 × 1.0 × 3.13 ≈ 9.4，合理降低。

#### Bug #2 — `_apply_forget` 在 S 极小时 ((S+1)^0.2 - 1) → 0 导致 S_new 趋近 0 后被 max(1.0) 截断，丢失历史信息

**文件**: `memory/fsrs_model.py`  
**行号**: 152–153  
**描述**: 当 S=1.0（下限），`((2.0) ** 0.2) - 1.0 ≈ 0.149`，S_new = 1.0 × 0.5 × D^(-0.3) × 0.149 ≈ 0.07，被 `max(1.0, S_new)` 截断为 1.0。反复遗忘后 S 永远停在 1.0 下限，无法区分"遗忘1次"和"遗忘10次"的记忆——它们 S 值相同，R(t) 相同，但实际衰减程度应不同。  
**修复建议**: 用更细粒度的下限（如 `max(0.1, S_new)`）或引入 `forget_count` 字段辅助区分。

#### Bug #3 — `confirm_correct.py` 中 `created_at` 错误地取 `last_review`

**文件**: `memory/confirm_correct.py`  
**行号**: 70  
**描述**: 构建 MemoryState 时 `created_at=node.get("last_review", 0.0) or now_ts`。`last_review` 是上次复习时间，不是创建时间。如果旧数据 `last_review=0.0`，则 `0.0 or now_ts` = `now_ts`（Python 的 falsy 逻辑），创建时间被设为当前时间——这导致 BUFFER 阶段的 21 天门槛永远不会超时，旧节点永远无法自然过渡到 REINFORCED/DECAY。如果 `last_review > 0`，则创建时间被设为上次复习时间，同样偏移了 `transition()` 中 `age_days = (now - created_at) / 86400` 的计算。  
**修复建议**: 数据库 concept_nodes 缺少 `created_at` 列（只有 `created` 是 ISO 字符串），需要：(1) v15 迁移增加 `created_at` REAL 列；或 (2) 从 `created` ISO 字符串解析时间戳；或 (3) 用 `0.0` 作为 `created_at` 的 fallback（让 `age_days` 足够大以正确触发过渡）。

#### Bug #4 — `concept_nodes` 表缺少 `created_at` REAL 列，confirm_correct 无法正确重建 MemoryState

**文件**: `db/database.py` (v14 迁移) + `memory/confirm_correct.py`  
**行号**: database.py: ~900 (v14 create concept_nodes)  
**描述**: concept_nodes 表只有 `created TEXT`（ISO 字符串）而没有 `created_at REAL`（时间戳），但 MemoryState 需要浮点时间戳做 `age_days` 计算。confirm_correct 在 line 70 用 `last_review` 替代（见 Bug #3），dream_consolidation 从不构建 concept_nodes 的 MemoryState。整个 concept_nodes 侧的 FSRS 状态无法正确运行。  
**修复建议**: v16 迁移为 concept_nodes 增加 `created_at REAL DEFAULT 0` 列，回填时从 `created` ISO 字符串解析。同时修复 confirm_correct line 70 为 `created_at=node.get("created_at", 0.0)`。

#### Bug #5 — `consolidate_from_db` 中 `difficulty` 硬编码为 1.0

**文件**: `core/dream_consolidation.py`  
**行号**: 242  
**描述**: `consolidate_from_db` 构建 MemoryState 时 `difficulty=1.0`，忽略了数据库中实际存储的 difficulty 值。而同一文件 line 176 的 `consolidate_db` 正确使用了 `difficulty=mem.get("difficulty", 5.0)`。difficulty=1.0 会导致：  
- `retrievability` 中 S 和 R 计算不受 D 直接影响（R 公式只用 S），但  
- 如果后续调用 `reinforce()`，`difficulty_factor = (10.0 - D) / 9.0 = 1.0`（最大值），**虚增 S 增长**  
- `_apply_forget` 中 `D ** (-0.3) = 1.0`，也会影响 S_new 计算  
**修复建议**: 改为 `difficulty=mem.get("difficulty", 5.0)`，与 `consolidate_db` 保持一致。

---

### P1 — 兼容层缺陷 / 数据一致性

#### Bug #6 — FluidMemory.score() 重建的 MemoryState 缺失 `difficulty` 字段，默认 D=5.0 可能与旧数据不一致

**文件**: `memory/fluid_memory.py`  
**行号**: 30–55  
**描述**: `score()` 方法从 `access_count` 和 `created_at` 重建 MemoryState，但 `difficulty` 始终使用默认值 5.0。旧 FluidMemory 没有 difficulty 概念，兼容层按 5.0 计算是合理的——但如果调用方已将真实 difficulty 存入 DB 并期望读取，该兼容层会丢弃它。更严重的是：`stability = S_INIT + access_count * STABILITY_PER_ACCESS` 最多 3.0 + 5×14.0 = 73.0，被 `min(stability, 300.0)` 截断。但这个线性 S 公式与 FSRS 的指数衰减 R(t) 不匹配——例如 `access_count=1, S=17, R(30天) ≈ 0.17`，而旧 FluidMemory 的指数衰减 `e^(-30/17) ≈ 0.17`，此时恰好一致；但 `access_count=5, S=73, R(30天) ≈ 0.66`，旧逻辑中永久记忆 R=1.0，这里 PERMANENT 分支 R=1.0 没问题，但 `access_count=4, S=59, R(30天) ≈ 0.60` 与旧逻辑差异大。  
**修复建议**: 兼容层应优先从传入参数读取 DB 中的 FSRS 状态（difficulty/stability/phase），只在缺失时 fallback 到线性估算。同时建议 `score()` 接受可选的 `MemoryState` 参数。

#### Bug #7 — FluidMemory.is_permanent() 与 FSRS transition() 判定不一致

**文件**: `memory/fluid_memory.py`  
**行号**: 57  
**描述**: `is_permanent()` 仅看 `access_count >= 5`，而 FSRS 的 PERMANENT 过渡条件是 `stability >= S_PERMANENT(30.0) AND reinforcement_count > 0 AND age > BUFFER_DAYS(21)`。两种判定会导致：access_count=3 但 S 已通过强化达到 30+ 的记忆，FluidMemory 不认为 permanent，FSRS 认为 permanent；反之 access_count=5 但 S 仍很低的记忆，FluidMemory 认为 permanent，FSRS 不认为。调用方混用两套判定会导致行为分歧。  
**修复建议**: 废弃 `is_permanent()`，统一使用 `FSRSModel` 的 `transition()` 判定。

#### Bug #8 — `_apply_recall` 和 `_apply_forget` 双重构造 MemoryState（性能浪费 + reinforcement_count 计算易出错）

**文件**: `memory/fsrs_model.py`  
**行号**: 135–146, 155–166  
**描述**: 两个方法都先构造一个临时 MemoryState 调用 `.transition(now)` 获取新 phase，然后重新构造最终 MemoryState。问题：  
1. 临时 MemoryState 的 `reinforcement_count` 在 `_apply_recall` 中被 +1 了两次（line 139 和 line 145），但语义上应该只 +1 一次。当前结果恰好正确（两处都 +1），但这是巧合——如果后续修改其中一处，就会产生 bug。  
2. 构造两个相同字段的对象再丢弃第一个，是性能浪费。  
**修复建议**: 提取为 `new_phase = self._compute_phase(D_new, S_new, state, now)`，只构造一次 MemoryState。

#### Bug #9 — `_apply_fsrs_scoring` 每次调用都 `FSRSModel()` 实例化，热路径性能浪费

**文件**: `memory/memory_manager.py`  
**行号**: 1480  
**描述**: 每次检索评分都创建新的 FSRSModel 实例。FSRSModel 是无状态的纯计算类，重复创建无意义。一次检索可能对 50+ 条记忆调用此方法。  
**修复建议**: 在 `__init__` 中创建 `self._fsrs = FSRSModel()`，复用实例。

#### Bug #10 — `encode_memory` 不初始化新记忆的 FSRS 状态

**文件**: `memory/memory_manager.py`  
**行号**: 1651–1760  
**描述**: `encode_memory` 插入新记忆时，不设置 difficulty/stability/phase/last_review/reinforcement_count，完全依赖数据库 DEFAULT 值（difficulty=5.0, stability=3.0, phase='buffer', last_review=0, reinforcement_count=0）。这意味着：  
- `last_review=0`，时间戳为 Unix epoch（1970-01-01），导致 `age_days ≈ 20500`，远超 BUFFER_DAYS(21)，transition() 立即判定为 DECAY（因为 reinforcement_count=0）——**新写入的记忆在 FSRS 评分时 R ≈ 0（因为 last_review 太旧），会被 should_filter 过滤掉**。  
- 这是严重的兼容性问题：刚写入的记忆在下次检索时可能被 FSRS 评分丢弃。  
**修复建议**: 插入记忆后，立即用 `estimate_initial_difficulty()` 计算 D，并将 `last_review=now` 写入。或在 `_apply_fsrs_scoring` 中对 `last_review=0` 的记忆做特殊处理（视为 R=1.0）。

#### Bug #11 — `_apply_fsrs_scoring` 中 `last_review=0.0` fallback 到 `timestamp` 但 `timestamp` 也可能是旧时间

**文件**: `memory/memory_manager.py`  
**行号**: 1489  
**描述**: `last_review=r.get("last_review", 0.0) or r.get("timestamp", 0.0)`。当 last_review=0（新记忆默认值），fallback 到 timestamp。但 timestamp 是记忆创建时间，不是"上次复习时间"——如果记忆创建于 3 天前且未被复习，last_review 应为 0（表示从未被复习），此时用 timestamp 替代会让 R(t) 偏高（elapsed_days=3 而非 ~20000）。虽然比 Unix epoch 好得多，但语义上不正确。  
**修复建议**: 在 encode_memory 时设置 `last_review=time.time()`（表示"创建即复习"），或在 MemoryState 中区分"从未复习"和"上次复习时间"。

#### Bug #12 — `consolidate_db` 和 `consolidate_from_db` 逻辑分裂，后者不更新数据库 FSRS 状态

**文件**: `core/dream_consolidation.py`  
**行号**: 107–140, 155–290  
**描述**: `consolidate_db` 只做归档（低 R → archive），不更新 S/D。`consolidate_from_db` 做完整 4 杆框架但只修改内存中的 Memory 对象，**不回写 FSRS 状态到数据库**。两方法都不更新 DB 中的 stability/difficulty/phase，FSRS 状态只在 `confirm()` 时被动更新——这意味着长期未被 confirm 的记忆，其 FSRS 状态永远停留在初始值。  
**修复建议**: consolidate_from_db 完成后，批量 `UPDATE episodic_memories SET stability=?, difficulty=?, phase=? WHERE id=?` 回写衰减后的 FSRS 状态。

#### Bug #13 — `auto_link` 每条边都单独 commit，O(N²) 写放大

**文件**: `db/db_concept.py`  
**行号**: 55, 79, 106, 131, 169  
**描述**: ConceptDB 的所有写操作（insert_node, update_node, create_edge, update_edge, auto_link）都在方法末尾 `await self._conn.commit()`。`auto_link` 对每个共享 ≥3 keys 的节点调用 `create_edge` 两次（双向），每次 create_edge 都 commit 一次。如果有 100 个存活节点，最坏情况 200 次 commit。而 `confirm()` 对每条边也调用 `update_edge` 两次，每次 commit。  
**修复建议**: 所有 ConceptDB 方法增加 `auto_commit: bool = True` 参数，调用方可手动控制事务边界。`auto_link` 和 `confirm` 应在外层统一 commit。

#### Bug #14 — `get_alive_nodes()` 无分页，大量节点时 OOM 风险

**文件**: `db/db_concept.py`  
**行号**: 81–87  
**描述**: `get_alive_nodes()` 返回所有 valid_to IS NULL 的节点到内存字典。`auto_link` 调用它做全表扫描。当 concept_nodes 增长到数万条时，单次调用可能消耗大量内存。  
**修复建议**: auto_link 改用流式查询或分页，或在 SQL 层直接做 shared keys 匹配（`SELECT ... WHERE json_extract(keys, '$') ...`）。

---

### P2 — 类型安全 / 边界 / 代码质量

#### Bug #15 — `MemoryPhase` 枚举值与数据库 `phase` 列字符串可能不匹配

**文件**: `memory/fsrs_model.py` + `memory/confirm_correct.py`  
**行号**: fsrs_model.py:8–14, confirm_correct.py:65  
**描述**: MemoryPhase 有 `DECAY = "decayed"`，但数据库 v15 迁移注释写 `phase='decayed'`。而 `_apply_fsrs_scoring` line 1488 用 `MemoryPhase(r.get("phase", "buffer"))` 从字符串构造枚举。如果数据库中存了非法字符串（如 "decay" 而非 "decayed"），会抛 `ValueError` 导致整条记忆被跳过。  
**修复建议**: 在 MemoryPhase 构造处加 try-except，fallback 到 `MemoryPhase.BUFFER`；或在数据库层加 CHECK 约束。

#### Bug #16 — `_apply_recall` 中 `growth` 可为负数（WEAK_HIT + 高 difficulty）

**文件**: `memory/fsrs_model.py`  
**行号**: 130–133  
**描述**: 当 `signal=WEAK_HIT`（growth_factor=1.0）且 `D=10.0`（最高难度）时，`difficulty_factor = (10-10)/9 = 0.0`，`growth = 1.0 × 0.0 × retrievability_bonus = 0.0`，S_new = S × (1 + 0) = S，不变。这本身不是 bug，但如果 D 超过 10（由于浮点误差），`difficulty_factor < 0`，`growth < 0`，`S_new < S`，遗忘信号下 S 反而降低——而 `_update_difficulty` 中 WEAK_HIT 的 delta=0.0，不降低 D，形成恶性循环。虽然 D 被 clamp 到 [1,10]，但 `_update_difficulty` 的 mean-revert 公式中 `MEAN_REVERT * D_MEAN + (1-MEAN_REVERT) * (D + delta)` 在 D 接近 10 且 delta 为负时，D_new 可能因浮点精度低于 10，但 min(10.0, ...) 理论上应防止。  
**修复建议**: 在 `difficulty_factor` 计算后加 `max(0.0, difficulty_factor)` 防御性保护。

#### Bug #17 — FluidMemory.score() 中 `peak_weight` 参数被传入但旧逻辑可能未覆盖

**文件**: `memory/fluid_memory.py`  
**行号**: 26  
**描述**: `score()` 的签名保留了 `peak_weight` 参数，计算时 `similarity * peak_weight * R`。旧调用方可能传 `peak_weight < 1.0` 来降权，但新的 FSRS 状态中 `weight` 已经由 R 驱动（confirm_correct line 89: `new_weight = min(1.0, R)`），如果两边同时应用 peak_weight 和 R，会双重降权。  
**修复建议**: 废弃 peak_weight 参数，或明确文档说明它与 FSRS weight 的关系。

#### Bug #18 — v15 迁移 `last_review DEFAULT 0` 与 FSRS 算法语义冲突

**文件**: `db/database.py`  
**行号**: 999–1001  
**描述**: `last_review REAL DEFAULT 0` 意味着旧数据的 last_review=0（1970-01-01）。在 `_apply_fsrs_scoring` 中 `last_review=0.0 or timestamp` 依赖 falsy 判断，但 `0.0` 在 Python 中是 falsy，所以会 fallback 到 timestamp。然而如果有人在代码其他地方直接用 `state.retrievability(now)` 而不经过 `_apply_fsrs_scoring`（如 dream_consolidation），elapsed_days 会是 ~20000 天，R ≈ 0，记忆会被立即归档。  
**修复建议**: v16 迁移将 `last_review=0` 的行回填为 `timestamp` 值：`UPDATE episodic_memories SET last_review = timestamp WHERE last_review = 0`。

#### Bug #19 — `DreamConsolidator.consolidate()` 中 `m.strength = R` 覆盖了原始 strength

**文件**: `core/dream_consolidation.py`  
**行号**: 112–113  
**描述**: `m.strength = R` 直接将 strength 覆盖为当前 Retrievability。R 是 [0,1] 区间值，而 strength 在 Memory dataclass 中默认 1.0，语义是"衰减强度"。下次 consolidate 时 `stability = m.strength * 10.0`，如果上次 R=0.5，则 stability=5.0，导致 R 加速下降——**正反馈衰减**，记忆一旦开始遗忘就会加速被遗忘，与 Ebbinghaus 的"减速衰减"矛盾。  
**修复建议**: 不直接覆盖 strength，而是用 `m.strength = m.strength * R` 或 `m.strength = max(m.strength * 0.95, R)` 做渐进衰减。

#### Bug #20 — `DreamConsolidator` 是有状态单例但 `consolidate_from_db` 不更新内部 `_memories`

**文件**: `core/dream_consolidation.py`  
**行号**: 155–290  
**描述**: `consolidate_from_db` 从 DB 加载记忆并操作局部 `memories` 字典，但不更新 `self._memories`。而 `consolidate()` 操作 `self._memories`，`stats()` 也基于 `self._memories`。两个方法的数据源完全隔离，stats 统计不准确。  
**修复建议**: consolidate_from_db 完成后同步更新 self._memories，或去掉 `_memories` 字典，统一从 DB 读写。

#### Bug #21 — `_migrate_v15` 中 `UPDATE ... WHERE phase = 'buffer'` 在 ALTER ADD COLUMN 后可能匹配到所有行

**文件**: `db/database.py`  
**行号**: 1032–1037  
**描述**: v15 先 `ALTER TABLE ... ADD COLUMN phase TEXT DEFAULT 'buffer'`，然后 `UPDATE ... SET phase = 'permanent' WHERE access_count >= 5 AND phase = 'buffer'`。由于 ALTER TABLE 设置了 DEFAULT 'buffer'，所有旧行的 phase 都是 'buffer'，WHERE 条件退化为 `access_count >= 5`。这在逻辑上是正确的（access_count >= 5 的旧数据应标记为 permanent），但幂等性有问题：**如果 v15 迁移跑第二次**，phase 列已存在（第 987 行的 `if "phase" not in epi_cols` 检查会跳过 ALTER），但 UPDATE 仍会执行——不过此时 `access_count >= 5 AND phase = 'buffer'` 可能不再匹配（因为上次已更新为 permanent），所以实际上幂等。但如果有人在两次迁移之间创建了 access_count >= 5 的新记忆（phase='buffer'），它也会被更新为 permanent，这可能不是预期行为。  
**修复建议**: 在迁移入口处检查 `current >= 15` 则整体跳过，而非逐列检查。

#### Bug #22 — `confirm_correct.correct()` 新节点 `insert_node` 缺少 `source_mem_id` 和 FSRS 列

**文件**: `memory/confirm_correct.py`  
**行号**: 119–132  
**描述**: `correct()` 创建新节点时调用 `self.db.insert_node()`，传入了 `weight`, `peak_weight`, `confidence`, `access_count`, `keys` 等参数，但**没有传入 `source_mem_id`、`difficulty`、`stability`、`phase`、`last_review`、`reinforcement_count`**。新节点将使用数据库默认值（difficulty=5.0, stability=3.0, phase='buffer', last_review=0, reinforcement_count=0），而不是继承旧节点的 FSRS 状态。这导致纠正后的记忆从零开始 FSRS 评分，丢失了旧节点的学习历史。  
**修复建议**: 从旧节点继承 FSRS 状态（stability 保持，difficulty 可微调），设置 `last_review=now_ts, reinforcement_count=0`。

#### Bug #23 — `estimate_initial_difficulty` 返回 [1,10] 但 `D_INIT=5.0` 加上调整项可能溢出

**文件**: `memory/fsrs_model.py`  
**行号**: 80–92  
**描述**: `D_INIT=5.0`，长度 >200 时 `D += 1.5`，有情感时 `D += 1.0`，有抽象关键词时 `D += 2.0`，三者叠加 D=5+1.5+1+2=9.5，仍在 [1,10] 范围内。但如果长度 >200 + 情感 + 抽象 + `_FACT_KEYWORDS` 命中（-2.0），则 D=5+1.5+1+2-2=7.5。实际上 `_FACT_KEYWORDS` 和 `_ABSTRACT_KEYWORDS` 是 elif 关系，不会同时命中。最极端情况 D=5+1.5+1+2=9.5，被 `min(10.0, ...)` 截断。计算正确但边界值紧密，建议测试覆盖。  
**修复建议**: 低风险，增加单元测试覆盖边界值。

#### Bug #24 — CognitiveMemory 与 FSRS-DSR 完全断联，两套衰减逻辑并存

**文件**: `memory/cognitive_memory.py`  
**行号**: 全文件  
**描述**: CognitiveMemory 有自己的 salience/decay_factor/access_count 衰减体系，与 FSRS-DSR 的 D/S/R 体系完全独立。两者可能同时操作同一批记忆数据，导致状态冲突。例如 CognitiveMemory 的 `consolidate()` 按自己的阈值转移记忆到 Semantic 层，而 FSRS 的 transition() 按自己的阈值判定 phase——两套状态机互不感知。  
**修复建议**: 明确 CognitiveMemory 与 FSRS 的边界——如果 CognitiveMemory 仍在使用，需要让它也通过 FSRSModel 做衰减评分，或标记为 deprecated 并迁移。

#### Bug #25 — `db_concept.py` 中 `insert_node` 使用 `INSERT OR REPLACE` 会静默覆盖已有节点

**文件**: `db/db_concept.py`  
**行号**: 44–56  
**描述**: `INSERT OR REPLACE INTO concept_nodes` 在主键冲突时会删除旧行并插入新行，导致旧行的所有列被新值覆盖。如果旧节点已有 FSRS 状态（stability/difficulty/phase）但 insert_node 传入的参数缺少这些字段（使用默认值），则旧状态被清零。`confirm_correct.correct()` 在创建新节点时恰好会调用 insert_node，如果节点 ID 冲突（md5[:12] 碰撞），旧节点的全部数据会被覆盖。  
**修复建议**: 改为 `INSERT OR IGNORE` 或 `INSERT ... ON CONFLICT(id) DO UPDATE SET ...` 精确更新指定列。

---

## 审计总结

| 严重级别 | 数量 | 核心风险 |
|---------|------|---------|
| P0 | 5 | 遗忘公式 S 暴涨、created_at 错误导致 phase 永远不转移、difficulty 硬编码 |
| P1 | 9 | 兼容层评分偏差、新记忆 last_review=0 被 FSRS 过滤、Dream 不回写 FSRS 状态、auto_link 性能 |
| P2 | 11 | 枚举值不匹配、双重构造、正反馈衰减、INSERT OR REPLACE 覆盖 |

### 最关键的修复优先级

1. **Bug #1** — `_apply_forget` S_new 公式用当前 S 替代 S_0，遗忘后 S 暴涨
2. **Bug #10** — 新记忆 `last_review=0` 导致 FSRS 评分 R≈0 被过滤
3. **Bug #3+#4** — concept_nodes 缺 `created_at` 列，confirm 无法正确运行 FSRS
4. **Bug #5** — Dream `consolidate_from_db` difficulty 硬编码 1.0
5. **Bug #12** — Dream 整合后不回写 FSRS 状态到数据库
