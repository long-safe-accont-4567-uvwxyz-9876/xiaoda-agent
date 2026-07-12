# FSRS-DSR Memory System - Progress Ledger

## Plan: docs/superpowers/plans/2026-07-12-fsrs-dsr-memory.md

## Tasks

- [x] Task 1: 创建 fsrs_model.py 核心算法模块
- [x] Task 2: 数据库迁移 v15 — 添加 FSRS 列
- [x] Task 3: 修改 memory_manager.py 评分管线
- [x] Task 4: 修改 confirm_correct.py — 用 S/D 更新替代 weight+0.15
- [x] Task 5: 修改 concept_graph.py — 传入 D 初始化参数
- [x] Task 6: 修改 dream_consolidation.py — 用 FSRSModel 替代 FluidMemory
- [x] Task 7: 更新 fluid_memory.py — 添加兼容层
- [x] Task 8: 全量集成测试

## Completed

- Task 1: complete (commits 4c9080c..44f0908, 32/32 tests passed)
- Task 2: complete (commit 0c3405f, db migration v15 + db_memory/db_concept updates)
- Task 3: complete (commit 801d435, FSRS-DSR scoring pipeline)
- Task 4: complete (commit ff1d4ac, confirm uses FSRS reinforce)
- Task 5: complete (commit 58d2386, concept_graph D initialization)
- Task 6: complete (commit da3e372, dream consolidation uses FSRSModel)
- Task 7: complete (commit 6399233, FluidMemory compat layer)
- Task 8: complete (75/75 tests passed, all imports OK)

---

# Bug Fix P0/P1/P2/Quality - Progress Ledger

## Plan: docs/superpowers/plans/2026-07-12-bug-fix-p0-p1-p2-quality.md

## Tasks

- [ ] Task 1: P0-09 — background_tasks._spawn() 无事件循环保护
- [ ] Task 2: P0-05 — EventBus ContextVar 绑定泄漏
- [ ] Task 3: P0-07 — StructuredBlackboard tag/direction 索引不过期清理
- [ ] Task 4: P0-01 + P0-02 — FSRS 遗忘公式反转 + 极小值截断
- [ ] Task 5: P0-04 + P0-06 + P0-03 — DB v16 migration + encode_memory FSRS init + confirm_correct created_at
- [ ] Task 6: P0-08 — consolidate_from_db difficulty 硬编码
- [ ] Task 7: P0-10 + P0-11 — QQ C2C 流式配额 + SharedBlackboardDB asyncio.Lock
- [ ] Task 8: P0-12 + P0-13 — J-Space health 信号值域 + degradation 阈值
- [ ] Task 9: P1 BUG-01 + BUG-04 — sub_agent_manager 超时事件 + 不可用早退事件
- [ ] Task 10: P1 BUG-02 — CancelToken asyncio.create_task 无 loop 保护
- [ ] Task 11: P1 BUG-03 — QQUser deliver 关键字参数调用
- [ ] Task 12: P1 Bug #6 + #7 — FluidMemory 兼容层修复
- [ ] Task 13: P1 Bug #9 + #10 + #11 — memory_manager FSRS 优化
- [ ] Task 14: P1 Bug #13 — auto_link 每条边单独 commit O(N²)
- [ ] Task 15: Remaining P1 bugs (J-Space + Channel)
- [ ] Task 16: P2-CORE-01 through P2-CORE-07
- [ ] Task 17: P2-MEM-01 through P2-MEM-11
- [ ] Task 18: P2 J-Space + Channel bugs
- [ ] Task 19: Quality improvements per spec
- [ ] Task 20: Full integration test + cleanup

## Completed