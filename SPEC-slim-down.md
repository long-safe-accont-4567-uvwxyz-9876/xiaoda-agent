# SPEC: nahida-agent 主 Agent 瘦身 & 角色分离

> **版本**: v1.0  
> **日期**: 2026-08-01  
> **状态**: Draft  

---

## 1. 背景

nahida-agent 是一个以**情感陪伴 + 智能聊天**为核心方向的本地部署单用户 Agent 项目。随着功能迭代，主 Agent（小妲）积累了大量面向 coding agent 的重量级基础设施，导致：

- 核心循环膨胀至 2133 行，职责过多
- `core/` 目录 15,541 行中约 7,000+ 行对情感陪伴无用
- 记忆治理模块（matrix_governance 等）2,849 行纯属学术过度工程
- 启动时间、内存占用、维护成本均受影响

同时，项目已有子 Agent 小狼（xiaolang）专门负责编程任务，但主 Agent 仍保留了完整的 coding 工具链和重量级基础设施，导致角色边界模糊。

## 2. 目标

### 2.1 核心原则

1. **路由机制不变**：用户消息先到主 Agent，由主 Agent 决定是否需要分配给子 Agent
2. **主 Agent 保留轻量编程能力**：不取消主 Agent 的编程能力，但剥离重量级基础设施
3. **重型基础设施迁移给小狼**：chaos 工程、6 级恢复、并行 DAG 等由小狼承载
4. **主 Agent 专注情感陪伴**：瘦身后的主 Agent 更轻量、更快、更聚焦

### 2.2 量化目标

| 指标 | 当前 | 目标 | 减少 |
|------|------|------|------|
| `core/` 行数 | 15,541 | ~8,500 | **-45%** |
| `memory/` 治理模块 | 2,849 | ~200 | **-93%** |
| `chaos/` + `quality/` + `doctor/` | 2,907 | 0 | **-100%** |
| `security/` 非核心模块 | 1,525 | ~400 | **-74%** |
| 总计可削减 | — | **~13,000 行** | — |

---

## 3. 角色定义

### 3.1 小妲（xiaoda）— 主 Agent

**定位**：情感陪伴 + 智能调度 + 轻量编程

| 职责 | 说明 |
|------|------|
| ✅ 情感对话 | 陪伴、安慰、日常聊天 |
| ✅ 记忆管理 | 情感记忆、长期记忆、用户画像 |
| ✅ 子 Agent 调度 | 识别意图 → 路由/委托给合适的子 Agent |
| ✅ 轻量编程 | 简单代码片段、伪代码、概念解释 |
| ✅ TTS 语音合成 | 语音回复 |
| ✅ 主动关怀 | 定时问候、情绪检测、提醒 |
| ❌ 重型编程 | 项目级代码开发、debug、重构 |
| ❌ 系统运维 | 混沌工程、SLO 追踪、6 级恢复 |
| ❌ 学术治理 | prompt 矩阵治理、本体复杂度分析 |

**模型**：mimo（多模型路由，情感优先）

### 3.2 小狼（xiaolang）— 编程子 Agent

**定位**：专业编程 + 重型基础设施承载者

| 职责 | 说明 |
|------|------|
| ✅ 项目开发 | 代码编写、调试、重构、测试 |
| ✅ 系统运维 | 混沌工程测试、可靠性验证 |
| ✅ 复杂恢复 | 6 级恢复编排、降级检测 |
| ✅ 并行执行 | 多工具并行 DAG 调度 |
| ✅ 人工审批 | 高危操作审批流程 |
| ✅ Prompt 工程 | 矩阵治理、复杂度优化 |

**模型**：agnes-2.0-flash（编程优化）

### 3.3 其他子 Agent

| Agent | 定位 | 说明 |
|-------|------|------|
| 小莉（xiaoli） | 情感陪伴辅助 | 萌系聊天、安慰 |
| 小涟（xiaolian） | 信息搜索 | 搜索、查资料 |
| 小可（xiaoke） | 学术研究 | 论文、调研 |

---

## 4. 瘦身方案

### 4.1 Phase 1：直接砍掉（~6,093 行）

这些模块独立存在，无核心依赖，可安全删除。

| 模块 | 文件 | 行数 | 理由 | 去向 |
|------|------|------|------|------|
| 混沌工程 | `chaos/` 全目录 | 2,563 | 纯测试基础设施，主循环不引用 | 迁移给小狼 |
| 6 级恢复编排 | `core/recovery_orchestrator.py` | 370 | 主循环不 import | 迁移给小狼 |
| SLA 导出 | `core/sla_exporter.py` | 255 | 仅被降级检测用 | 迁移给小狼 |
| SLO 追踪 | `core/slo_tracker.py` | 278 | 仅被降级检测用 | 迁移给小狼 |
| 并行 DAG | `core/parallel_dag.py` | 290 | 仅注释提及，实际未 import | 迁移给小狼 |
| 意图分解 | `core/intent_decomposition.py` | 124 | 无外部引用 | 删除 |
| 降级检测 | `core/degradation_detector.py` | 522 | EWMA+SLO，对 chatbot 过重 | 迁移给小狼 |
| 矩阵治理 | `memory/matrix_governance.py` | 1,289 | 学术级过度工程 | 迁移给小狼 |
| 提示词复杂度 | `memory/prompt_complexity.py` | 1,154 | 学术级过度工程 | 迁移给小狼 |
| 本体复杂度 | `memory/ontology_complexity.py` | 179 | 仅 KG 抽取可选引用 | 迁移给小狼 |
| 人工审批 | `security/human_approval.py` | 555 | 单用户不需要审批流 | 迁移给小狼 |
| 三轴退化 | `quality/triple_axis_degradation.py` | 107 | 独立无引用 | 删除 |
| 独立诊断 | `doctor/` 目录 | 237 | `core/doctor.py` 已有更完整版本 | 合并到 core/doctor.py |
| 取消令牌 | `core/cancel_token.py` | 95 | coding agent 基础设施 | 迁移给小狼 |
| TNR 自愈 | `core/tnr_self_heal.py` | 82 | 独立无引用 | 迁移给小狼 |

**解耦处理**（删除前需清理的引用）：
- `core/degradation_strategy.py` 中 `slo_tracker` 引用改为可选（`slo_tracker=None`）
- `core/behavioral_health.py` 中 `slo_tracker` 引用改为条件判断
- `memory/knowledge_graph.py` 中 `ontology_complexity` 引用改为默认 False
- `core/bootstrap.py` 中删除 `ContextGovernance` 初始化代码

### 4.2 Phase 2：简化保留（预计再减 ~3,656 行）

这些模块有轻量核心依赖，需保留接口但大幅简化实现。

| 模块 | 当前 | 目标 | 节省 | 简化方案 |
|------|------|------|------|----------|
| `hooks.py` | 649 行 | ~250 行 | ~400 | 保留 SecurityPreCheck + PreToolUse，8 类钩子砍为 2 类 |
| `circuit_breaker.py` | 281 行 | ~100 行 | ~180 | 6 信号 → 简单失败计数，4 状态 → GREEN/RED 两级 |
| `degradation_strategy.py` | 459 行 | ~200 行 | ~260 | 保留核心 4 级降级 + `is_feature_available` |
| `degradation.py` | 97 行 | 0 行 | ~97 | 合并入 degradation_strategy.py |
| `permission_manager.py` | 570 行 | ~250 行 | ~320 | 7 模式 → 3 模式（DEFAULT/DEV/BYPASS） |
| `plugins/` | 972 行 | ~400 行 | ~572 | 砍 testing.py + sdk.py + discovery.py |
| `self_wake.py` | 326 行 | ~80 行 | ~246 | 复杂唤醒 → 简单定时检查 |
| behavioral + J-Space 系列 | 933 行 | ~150 行 | ~783 | 保留 behavioral_health.py 核心评分 |
| meta_cognition 双文件 | 344 行 | ~150 行 | ~194 | 合并为一个模块 |
| `context_governance.py` | 227 行 | ~50 行 | ~177 | 哈希链 → 简单长度检查 |
| dream 双文件 | 927 行 | ~500 行 | ~427 | dream_engine_v2 合入 dream_consolidation |

### 4.3 Phase 3：迁移给小狼

砍掉的模块不是垃圾——它们是高质量的 coding agent 基础设施。将以下模块迁移到小狼的能力范围：

| 迁移模块 | 给小狼的价值 |
|----------|-------------|
| `chaos/` 混沌工程 | 小狼的自测工具集，确保代码质量 |
| `recovery_orchestrator.py` | 小狼的容错处理，编程任务的 6 级恢复 |
| `parallel_dag.py` | 小狼的并行工具执行（如同时搜索+写代码+测试） |
| `human_approval.py` | 小狼的高危操作审批（如删除文件、修改配置） |
| `matrix_governance.py` + `prompt_complexity.py` | 小狼的 prompt 优化（编程场景确实需要精细 prompt） |
| `sla_exporter.py` + `slo_tracker.py` | 小狼的 SLA 监控（长时间编程任务的可用性追踪） |
| `degradation_detector.py` | 小狼的降级检测（EWMA 对长时间任务有价值） |

**迁移方式**：在子 Agent 配置中声明能力（capabilities），当小狼被激活时，延迟加载这些模块。

---

## 5. 情感陪伴核心增强

瘦身释放的资源应投入到情感陪伴核心能力的提升：

### 5.1 上下文压缩保留情感信息（Critical）

**问题**：`compress_history()` 压缩时丢失情感状态，长对话后小妲"忘记"用户情绪。

**方案**：
- 在压缩摘要中增加情感轨迹段
- 调用 `detect_emotion()` 为每条用户消息标注情绪
- 压缩后的 system message 包含"用户情绪变化轨迹"

### 5.2 记忆主动注入（Critical）

**问题**：记忆依赖用户主动触发 `recall`，关键时刻无法自动激活。

**方案**：
- 每次请求开始时，用最近对话 + 记忆目录做轻量 side-query
- 选出相关记忆（最多 3-5 条），注入到 user turn
- 失败时降级为关键词匹配

### 5.3 SOUL.md 凝练

**问题**：100+ 行的人格定义过长，稀释模型注意力。

**方案**：
- 压缩到 30-40 行（核心人格 + 称呼规则 + 回复风格 + 隐私保护）
- 技能清单移到 SKILLS.md（按需加载）

### 5.4 "每轮结束后提取记忆"

**问题**：用户偏好无法自动沉淀。

**方案**：
- 在 PostResponse Hook 中添加记忆提取步骤
- 用 LLM 从对话中提取用户偏好、情感事实
- 与现有记忆对比去重后写入 emotional memory

---

## 6. 实施路线图

### Phase 1：安全砍掉（1-2 天）

- [ ] 删除 `chaos/` 全目录
- [ ] 删除 `core/recovery_orchestrator.py`
- [ ] 删除 `core/sla_exporter.py` + `core/slo_tracker.py`
- [ ] 删除 `core/parallel_dag.py`
- [ ] 删除 `core/intent_decomposition.py`
- [ ] 删除 `core/degradation_detector.py`
- [ ] 删除 `memory/matrix_governance.py`
- [ ] 删除 `memory/prompt_complexity.py`
- [ ] 删除 `memory/ontology_complexity.py`
- [ ] 删除 `security/human_approval.py`
- [ ] 删除 `quality/triple_axis_degradation.py`
- [ ] 删除 `core/cancel_token.py` + `core/tnr_self_heal.py`
- [ ] 清理所有 import 引用
- [ ] 运行 ruff check + pytest 验证
- [ ] 提交到 `refactor/slim-down-phase1` 分支

### Phase 2：简化保留（2-3 天）

- [ ] 简化 `hooks.py`（649→250 行）
- [ ] 简化 `circuit_breaker.py`（281→100 行）
- [ ] 合并 degradation 三文件（1,078→200 行）
- [ ] 简化 `permission_manager.py`（570→250 行）
- [ ] 精简 `plugins/`（972→400 行）
- [ ] 简化 `self_wake.py`（326→80 行）
- [ ] 精简 behavioral/J-Space 系列（933→150 行）
- [ ] 合并 meta_cognition（344→150 行）
- [ ] 简化 `context_governance.py`（227→50 行）
- [ ] 合并 dream 双文件（927→500 行）
- [ ] 运行全量测试验证
- [ ] 提交到 `refactor/slim-down-phase2` 分支

### Phase 3：情感核心增强（3-5 天）

- [ ] 上下文压缩增加情感轨迹保留
- [ ] 实现记忆主动注入机制
- [ ] 凝练 SOUL.md（100+→30-40 行）
- [ ] 实现"每轮结束后提取记忆"
- [ ] 端到端情感陪伴质量测试
- [ ] 提交到 `feat/emotional-enhancement` 分支

### Phase 4：迁移给小狼（1 天）

- [ ] 在小狼配置中声明重型能力
- [ ] 实现延迟加载机制
- [ ] 验证小狼能正常使用迁移的模块
- [ ] 提交到 `feat/wolf-capabilities` 分支

---

## 7. 验收标准

### 7.1 功能验收

- [ ] `ruff check .` 零 F 类错误
- [ ] `pytest tests/` 全部通过
- [ ] 主 Agent 情感对话功能正常（对话、记忆、TTS、主动关怀）
- [ ] 子 Agent 路由功能正常（@mention、意图识别、委托）
- [ ] 小狼编程能力不受影响（代码编写、调试、测试）

### 7.2 性能验收

| 指标 | 当前基线 | 目标 |
|------|---------|------|
| 启动时间 | TBD | 减少 200-500ms |
| 内存占用 | TBD | 减少 3-5MB |
| 每次请求 hook 开销 | 8 类钩子检查 | 2 类钩子检查（-60%） |

### 7.3 代码验收

- [ ] `core/` 目录 ≤ 9,000 行
- [ ] `memory/` 治理模块 ≤ 200 行
- [ ] `chaos/` + `quality/` + `doctor/` 目录清空
- [ ] 总削减 ≥ 10,000 行

---

## 8. 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 删除 degradation_detector 导致降级检测缺失 | 🟡 中 | 保留 degradation_strategy 核心降级逻辑 |
| 删除 matrix_governance 导致 prompt 质量退化 | 🟢 低 | 情感陪伴场景不需要自动 prompt 优化 |
| 简化 hooks.py 导致安全检查缺失 | 🟡 中 | 保留 SecurityPreCheck |
| 删除 doctor/ 导致诊断能力下降 | 🟢 低 | core/doctor.py 已包含更完整逻辑 |
| 上下文压缩改动导致对话断裂 | 🟡 中 | 充分测试情感轨迹保留逻辑 |
| 记忆主动注入导致 prompt 过长 | 🟡 中 | 设置注入上限（3-5 条） |

---

## 9. 附录

### 9.1 模块依赖关系图

```
agent_core/core.py（主循环）
├── 必须保留
│   ├── model_router.py ← 模型路由
│   ├── agent_context.py ← 上下文管理
│   ├── memory/memory_manager.py ← 记忆管理
│   ├── emotion/* ← 情感系统（核心！）
│   ├── db/database.py ← 数据库
│   ├── prompt_builder.py ← 提示词构建
│   └── agent_dispatcher.py ← 子Agent路由
│
├── 需要简化（有直接 import）
│   ├── hooks.py [649→250]
│   ├── core/circuit_breaker.py [281→100]
│   ├── core/bootstrap.py [保留，精简初始化]
│   ├── core/background_tasks.py [保留，精简唤醒]
│   ├── core/failure_trigger.py [保留]
│   └── core/mental_state.py [保留]
│
├── 可延迟加载
│   ├── core/self_wake.py [326→80]
│   └── core/j_space_bootstrap.py [条件加载]
│
└── 完全不需要（砍掉）
    ├── chaos/* [2,563→砍]
    ├── core/recovery_orchestrator.py [370→砍]
    ├── core/sla_exporter.py [255→砍]
    ├── core/slo_tracker.py [278→砍]
    ├── core/parallel_dag.py [290→砍]
    ├── core/intent_decomposition.py [124→砍]
    ├── core/degradation_detector.py [522→砍]
    ├── memory/matrix_governance.py [1,289→砍]
    ├── memory/prompt_complexity.py [1,154→砍]
    ├── memory/ontology_complexity.py [179→砍]
    ├── security/human_approval.py [555→砍]
    ├── quality/triple_axis_degradation.py [107→砍]
    └── doctor/* [237→合并]
```

### 9.2 参考文档

- [ShareAI 教程 — 20 课渐进式 Agent 构建](https://learn.shareai.run/zh/)
- [nahida-agent 教程对照分析报告](./nahida-agent-tutorial-review.md)
- [nahida-agent 瘦身方案详细分析](./nahida-agent-slim-down.md)
- [nahida-agent 多 Agent 路由协作分析](./nahida-agent-multi-agent-review.md)
