# DEPRECATED 模块清单

> 创建日期：2026-07-19（Task 7 / P1-2）
> 最近更新：2026-07-19（Task 13 / SubTask 13.4 — 全局排查补充 3 项遗漏：`memory/fluid_memory.py`、`core/degradation.py`、`security/security.py::_is_dev_mode`）
> 维护者：xiaoda-agent 项目组
> 目的：记录仓库中所有带 DEPRECATED 标记的模块，明确其真实使用情况、风险评估与迁移计划，避免误删导致生产故障。

## 概述

本仓库中"DEPRECATED"标记出现在两种语境：

1. **模块级弃用**：整个模块被新模块取代，但保留以兼容旧调用方。需评估是否可安全删除。
2. **配置/字段名中的 "deprecated" 字样**：仅是 JSON 字段名或变量名（如 `deprecated_names`、`_FALLBACK_DEPRECATED_NAMES`），**不代表模块本身被弃用**。本清单会明确区分。

---

## 1. `memory/cognitive_memory.py` — ACTIVE（待 dream engine v3 重构时迁移）

### 状态
**ACTIVE**（先前标记为 DEPRECATED，已于 v0.5.28 取消标记）

### 真实使用情况

**生产代码（5 处）：**
- `memory/cognitive_memory.py`（模块自身定义 `CognitiveMemory`、`MemoryEntry`、`Cluster`）
- `core/dream_engine_v2.py`
  - 第 27 行：`from memory.cognitive_memory import CognitiveMemory, MemoryEntry`
  - 第 66-82 行：`DreamEngineV2.__init__` 接受 `cognitive_memory: CognitiveMemory` 参数
  - 直接访问私有属性：`_episodic`、`_semantic`、`_episodic_index`、`_connections`
  - 调用 `remember()` 存储 cluster 摘要与 preference 模式
  - 在 `_phase_nrem` / `_phase_supersedes` / `_phase_rem` / `_phase_insight` / `_phase_afe_stage_s` / `_phase_dae` 全部 6 个阶段使用
- `core/j_space_bootstrap.py`
  - 第 76 行：`import memory.cognitive_memory as _cm`（在 `_wire_hooks()` 的 try/except 中）
  - 注入 `_structured_blackboard` 到 `cognitive_memory` 模块全局变量，使 J-Space Hook 能记录记忆存储事件
- `core/conflict_supersession.py`
  - 类型注解中接受 `MemoryEntry` 列表（通过 `memories: list[Any]` 鸭子类型，但实际依赖 `MemoryEntry.id/content/embedding/timestamp` 字段）
- `core/permanent_memory.py`
  - 自定义 `PermanentMemoryEntry` 类（**与 `MemoryEntry` 无关**，仅名称包含子串"MemoryEntry"）

**测试代码（6 个文件）：**
- `tests/test_cognitive_memory.py` — 直接测试 `CognitiveMemory` 全部方法
- `tests/test_dream_engine_v2.py` — 测试 6 阶段梦境引擎，依赖 `CognitiveMemory` 实例
- `tests/test_conflict_supersession.py` — 使用 `MemoryEntry` 构造测试数据
- `tests/test_bridge_memory.py` — 使用 `MemoryEntry` 构造测试数据
- `tests/test_hook_integration.py` — 第 179 行 `import memory.cognitive_memory as _cm` 验证 hook 接线
- `tests/test_v06_integration.py` — v0.6 集成测试，端到端验证 `CognitiveMemory` + `DreamEngineV2`

**文档：**
- `docs/superpowers/specs/2026-07-11-cognitive-architecture-v06-design.md`
- `docs/superpowers/specs/2026-07-12-fsrs-dsr-memory-design.md`
- `docs/superpowers/specs/2026-07-12-j-space-optimization-design.md`
- `docs/superpowers/plans/2026-07-11-cognitive-architecture-v06.md`
- `docs/superpowers/plans/2026-07-12-fsrs-dsr-memory.md`
- `docs/superpowers/plans/2026-07-12-j-space-optimization.md`
- `docs/fix_spec_P2.md`、`audit/fix_spec_P2.md`、`audit_fsrs_dsr_report.md`

### 风险评估

**直接删除会破坏以下功能：**
- `DreamEngineV2` 完全无法实例化（构造函数要求 `cognitive_memory` 参数）
- 6 阶段梦境整合流程全部失效（NREM/SUPERSEDES/REM/Insight/AFE/DAE）
- J-Space Hook 无法将记忆存储事件写入结构化黑板
- 6 个测试文件 collect 阶段失败（ImportError）

**结论：不能删除。** 已取消 DEPRECATED 标记，保留为 ACTIVE。

### 迁移计划

**目标架构**：`FSRSModel` + `DreamConsolidator`（位于 `memory/fsrs_model.py` 与 `core/dream_consolidation.py`）

**为何不能立即迁移**：
- `CognitiveMemory` 是 3 层认知记忆（Episodic/Semantic/Hopfield + Hebbian 连接图 + K-means 聚类 + 嵌入存储）
- `DreamConsolidator` 当前是单层 dict 存储（无嵌入、无连接图、无 Hopfield、无聚类），仅做 Ebbinghaus 衰减 + 合并 + 归档
- `dream_engine_v2` 6 个阶段全部依赖 `CognitiveMemory` 的内部数据结构与算法

**迁移工作量预估（~710 行，属架构性大改）**：
| 工作项 | 估算行数 |
|---|---|
| 为 `DreamConsolidator.Memory` 增加 `embedding` 字段 | ~20 |
| 为 `DreamConsolidator` 增加 3 层 episodic/semantic 分裂 | ~80 |
| 增加 Hebbian 连接图（`_connections`） | ~50 |
| 集成 `HopfieldLayer` | ~30 |
| 增加 K-means 聚类（`_rebuild_clusters`） | ~50 |
| 增加 `recall()` 混合检索方法 | ~50 |
| 增加 `self_attention_sweep()` / `connection_strength()` | ~30 |
| 重写 `dream_engine_v2.py` 全部 6 个阶段 | ~150 |
| 更新 6 个测试文件 | ~200 |
| 更新 `MemoryEntry.id` 类型（int → str）兼容性 | ~50 |
| **合计** | **~710** |

**迁移时机**：v0.7+ dream engine v3 重构任务，作为统一架构升级的一部分。

**迁移路径（字段/方法级映射）**：
| CognitiveMemory | → | FSRSModel + DreamConsolidator |
|---|---|---|
| `MemoryEntry.salience` | → | `FSRSModel.retrievability()` (R = e^(-t/S)) |
| `MemoryEntry.decay_factor` | → | `MemoryState.stability` |
| `CognitiveMemory.consolidate()` | → | `DreamConsolidator.consolidate_from_db()` |
| `CognitiveMemory._connections` (Hebbian) | → | DreamConsolidator 新增连接图字段 |
| `CognitiveMemory.self_attention_sweep()` | → | DreamConsolidator 新增方法 |
| `CognitiveMemory._episodic/_semantic` | → | DreamConsolidator 新增 3 层存储 |
| `CognitiveMemory._hopfield` | → | DreamConsolidator 集成 HopfieldLayer |

### 已执行的变更（v0.5.28，Task 7）

- [x] 文件头从 `⚠️ DEPRECATED` 改为 `ACTIVE — 待 dream engine v3 重构时迁移`
- [x] 移除模块级 `warnings.warn(DeprecationWarning)` 调用（先前在每次 import 时触发，污染测试输出）
- [x] 在文件头补充详细的"为何不能立即迁移"说明与迁移路径
- [x] 移除 `import warnings`（已无其他用途）

---

## 2. `utils/ssrf_guard.py` — DEPRECATED（保留兼容，可清理）

### 状态
**DEPRECATED** — 已被 `security/ssrf_guard.py` 取代，本文件保留仅为向后兼容。

### 真实使用情况

**生产代码：**
- 无直接生产代码引用（`tools/web_browse_enhanced.py` 等已切换到 `security.ssrf_guard`）

**测试代码（1 个文件，4 处引用）：**
- `tests/test_phase1_5_modules.py`
  - 第 126、133、139、145 行：`from utils.ssrf_guard import SSRFGuardV2`

### 风险评估

**直接删除会破坏：**
- `tests/test_phase1_5_modules.py` 4 个测试用例 collect 失败

**结论：** 可以删除，但需要先迁移测试。本批次不处理（不在 Task 7 范围内，仅记录）。

### 迁移计划

1. 将 `tests/test_phase1_5_modules.py` 中 4 处 `from utils.ssrf_guard import SSRFGuardV2` 改为 `from security.ssrf_guard import ...`（确认 API 等价性后）
2. 删除 `utils/ssrf_guard.py`
3. 在 `utils/__init__.py` 中确认无残留导出

### 文件内部标记

- 第 1 行：`"""[DEPRECATED] SSRF 防护 v2 ..."""`
- 第 3-9 行：`.. deprecated::` Sphinx 指令
- 第 21-22 行：`__deprecated__ = True` / `__deprecated_reason__`
- 第 102-110 行：`get_ssrf_guard()` 函数内 `warnings.warn(DeprecationWarning)`（懒触发，仅在调用时告警）

---

## 3. `tools/web_browse_enhanced.py` `_is_private_ip_async` — DEPRECATED（函数级，保留兼容测试）

### 状态
**DEPRECATED（函数级）** — 整个文件并未弃用，仅 `_is_private_ip_async` 函数被标记为 deprecated。

### 真实使用情况

**生产代码：** 无（`web_browse_enhanced.py` 主流程已切换到 `_ssrf_validate_url`，即 `security.ssrf_guard.validate_url`）

**测试代码：**
- `tests/test_web_browse_enhanced.py` 仍调用 `_is_private_ip_async`

### 风险评估

**直接删除函数会破坏：**
- `tests/test_web_browse_enhanced.py` 部分测试用例

**结论：** 函数级兼容垫片，可在迁移测试后清理。本批次不处理。

### 迁移计划

1. 评估 `tests/test_web_browse_enhanced.py` 中对 `_is_private_ip_async` 的断言是否能改用 `security.ssrf_guard.validate_url`
2. 如可迁移，删除 `_is_private_ip_async` 函数

### 文件内部标记

- 第 131-134 行：
  ```python
  async def _is_private_ip_async(hostname: str) -> bool:
      """[deprecated] 旧 IP 检查, 保留以兼容现有测试; 新流程改用 security.ssrf_guard.validate_url"""
      from tools.web_browse_tools import _is_private_ip
      return await asyncio.to_thread(_is_private_ip, hostname)
  ```

---

## 4. `emotion/emoji_config.py` — NOT DEPRECATED（字段名误判）

### 状态
**ACTIVE**（非弃用）

### 说明

文件中第 65 行的 `deprecated_names` 是 **JSON 配置字段名**，用于读取 agent 配置文件中的"已弃用别名列表"，将旧名替换为当前 `display_name`。这是配置字段的语义说明，**不代表 `emoji_config.py` 模块本身被弃用**。

### 真实使用情况

`emotion/emoji_config.py` 是 emoji 配置的活跃实现，被 `emotion/` 子系统持续使用。

### 结论

**无需任何迁移或清理。** 仅在本文档中说明，避免后续误判。

### 代码位置

- 第 65 行：`for old in cfg.get("deprecated_names", []):`

---

## 5. `config.py` `_FALLBACK_DEPRECATED_NAMES` — NOT DEPRECATED（变量名误判）

### 状态
**ACTIVE**（非弃用）

### 说明

`config.py` 中第 527 行定义的 `_FALLBACK_DEPRECATED_NAMES: dict[str, str]` 是**已弃用 agent 名到当前 agent key 的映射表**，用于在配置文件中使用旧 agent 名时自动 fallback。这是变量命名的语义说明，**不代表 `config.py` 模块本身被弃用**。

### 真实使用情况

- 第 527 行：定义映射表
- 第 545 行：`_get_deprecated_aliases_for_agent()` 反向查找
- 第 556 行：同上

`config.py` 是项目核心配置模块，处于活跃使用状态。

### 结论

**无需任何迁移或清理。** 仅在本文档中说明，避免后续误判。

---

## 6. `memory/fluid_memory.py` — DEPRECATED（兼容层，已迁移至 FSRS-DSR）

### 状态
**DEPRECATED** — `FluidMemory` 类保留为兼容层，内部委托给 `memory.fsrs_model.FSRSModel`。新代码应直接使用 `FSRSModel`。

### 真实使用情况

**生产代码：** 无（仅模块自身定义）

**测试代码（2 个文件）：**
- `tests/test_fluid_memory.py` — 直接测试 `FluidMemory` 兼容接口
- `tests/test_audit_batch_fixes.py` — 审计修复测试中引用

### 风险评估

**直接删除会破坏：**
- 2 个测试文件 collect 阶段失败

**结论：** 可以删除，但需要先迁移测试。本批次不处理（仅记录）。

### 迁移计划

1. 将 `tests/test_fluid_memory.py` 中 `FluidMemory` 引用改为 `FSRSModel`
2. 评估 `tests/test_audit_batch_fixes.py` 中对 `FluidMemory` 的依赖是否能去除
3. 删除 `memory/fluid_memory.py`

### 文件内部标记

- 第 1-4 行：模块 docstring 明确"已迁移至 FSRS-DSR 模型"，"保留为兼容层"
- 第 49-55 行：`score()` 方法内对 `peak_weight` 参数触发 `DeprecationWarning`（FSRS state 可用时忽略 peak_weight）

---

## 7. `core/degradation.py` — DEPRECATED（兼容层，已迁移至 degradation_strategy）

### 状态
**DEPRECATED** — 4 级降级兼容层，所有逻辑委托给 `core.degradation_strategy.DegradationStrategy`。新代码应直接使用 `core.degradation_strategy`。

### 真实使用情况

⚠️ **被广泛使用（10 个生产文件）：**
- `core/agent_introspection.py`
- `core/j_space_bootstrap.py`
- `core/degradation_strategy.py`（反向引用，定义新实现）
- `tools/web_browse_enhanced.py`
- `agent_core/tool_executor.py`
- `agent_core/sub_agent_manager.py`
- `agent_core/message_processor.py`
- `chaos/tnr_protocol.py`
- `chaos/tnr_scenarios.py`
- `chaos/verify_degradation.py`

### 风险评估

**直接删除会破坏：** 上述 10 个生产文件的 import 链，导致大面积生产故障。

**结论：** 不能删除。**虽然模块自身标记为 DEPRECATED，但生产代码仍大量依赖。** 需要先迁移调用方再删除模块。

### 迁移计划

1. 逐个将上述 10 个生产文件的 `from core.degradation import ...` 改为 `from core.degradation_strategy import ...`
2. 验证 DegradationLevel 别名（FULL/DEGRADED/MINIMAL/EMERGENCY）的等价性
3. 删除 `core/degradation.py`
4. 在 `core/__init__.py` 中确认无残留导出

### 文件内部标记

- 第 1-7 行：模块 docstring 明确"⚠️ 本模块已弃用，仅保留向后兼容接口"
- 提供旧名别名：`DegradationLevel = _NewLevel`、`FULL/DEGRADED/MINIMAL/EMERGENCY` 常量

---

## 8. `security/security.py::_is_dev_mode()` — DEPRECATED（函数级，保留兼容）

### 状态
**DEPRECATED（函数级）** — `security/security.py` 模块本身未弃用，仅 `_is_dev_mode()` 函数被标记为 deprecated，已委托给 `PermissionManager.is_dev_mode()`。

### 真实使用情况

**生产代码：** 无直接调用方（仅在 `security/permission_manager.py` 第 213 行注释中作为"替代说明"被提及）

**测试代码：** 无

### 风险评估

**直接删除函数会破坏：** 无（无调用方）

**结论：** 函数级兼容垫片，可安全清理。本批次不处理（仅记录）。

### 迁移计划

1. 确认全仓库无 `_is_dev_mode` 调用（grep 验证已通过，仅自身定义与 permission_manager 注释）
2. 删除 `security/security.py` 中 `_is_dev_mode()` 函数

### 文件内部标记

- 第 17-20 行：
  ```python
  def _is_dev_mode() -> bool:
      """检查是否处于开发板模式 — 已弃用，请使用 PermissionManager"""
      from .permission_manager import get_permission_manager
      return get_permission_manager().is_dev_mode()
  ```

---

## 汇总表

| 模块/文件 | 状态 | 真实使用 | 风险 | 迁移时机 |
|---|---|---|---|---|
| `memory/cognitive_memory.py` | ACTIVE（已取消 DEPRECATED） | 5 生产文件 + 6 测试文件 | 删除会破坏 dream engine | v0.7+ dream engine v3 |
| `utils/ssrf_guard.py` | DEPRECATED | 仅 1 测试文件 | 删除会破坏 4 个测试 | 迁移测试后即可删除 |
| `tools/web_browse_enhanced.py::_is_private_ip_async` | DEPRECATED（函数级） | 仅 1 测试文件 | 删除会破坏部分测试 | 迁移测试后即可删除 |
| `emotion/emoji_config.py` | ACTIVE（误判） | 活跃使用 | 无 | 无需迁移 |
| `config.py` | ACTIVE（误判） | 活跃使用 | 无 | 无需迁移 |
| `memory/fluid_memory.py` | DEPRECATED（兼容层） | 仅 2 测试文件 | 删除会破坏 2 个测试 | 迁移测试后即可删除 |
| `core/degradation.py` | DEPRECATED（兼容层） | 10 个生产文件 | 删除会破坏生产代码 | 迁移 10 个调用方后可删除 |
| `security/security.py::_is_dev_mode` | DEPRECATED（函数级） | 无调用方 | 无 | 可直接删除 |

---

## 维护说明

- 新增 DEPRECATED 模块时，请同步更新本清单
- 每次版本发布前，复核清单中的迁移计划是否已推进
- 若某模块迁移完成，将其状态改为 `REMOVED` 并记录移除版本
