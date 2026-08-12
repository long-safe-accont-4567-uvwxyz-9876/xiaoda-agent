# Task 12 复审：VectorStore and Memory Integration

> 复审日期：2026-08-11。审查对象为 HEAD `dc8ecb7` 之上的当前未提交改动。

## 审查范围

- 规范：`.superpowers/sdd/local-ai-task-12-brief.md`
- 首审：`.superpowers/sdd/task-12-final-review.md`
- 实施报告：`.superpowers/sdd/task-12-local-ai-report.md`
- 重点代码：`local_ai/integration/embedding.py`、`local_ai/integration/reranker.py`、`memory/vector_store.py`、`memory/memory_manager.py`、`core/bootstrap.py`
- 闭环依赖：`local_ai/instances/manager.py`、`local_ai/runtimes/registry.py`、`web/routers/local_ai.py`
- 重点测试：`tests/test_local_ai_memory_integration.py`、`tests/test_local_ai_instances.py`

## 结论

### 规范结论：通过（Approved）

当前未提交修复已补齐首审指出的生产闭环缺口：`InstanceManager` 已恢复为完整实现，并新增 `resolve_runtime()`、`selection_identity()`；bootstrap 创建共享实例管理器，将 managed embedding/reranker service 分别注入 `VectorStore` 和 `MemoryManager`；停止或不健康的已选实例会显式报告不可用；缓存和 single-flight 均按选择身份隔离。任务指定测试及关联回归全部通过。

### 质量结论：有条件通过（Approved with Follow-ups）

未发现阻止 Task 12 合入的正确性问题，首审的 Critical-1、Critical-2 和缓存行为冲突均已解决。实现具备真实 InstanceManager 端到端覆盖，也覆盖缓存命中、切换实例、跨实例并发和陈旧完成等边界。

合入前仍建议完成两项非阻断清理：删除 `core/bootstrap.py` 中本任务新增但未使用的 `ModelPurpose` 导入；更新过时的实施报告，使 HEAD、测试数量和变更文件与当前 tree 一致。全范围 ruff 仍有 22 项错误，其中该未用导入是本任务可直接归属的问题，其余主要是被审文件已有的导入排序、局部未用导入和 E402。

## 首审问题复核

### Critical-1：`local_ai/instances/manager.py` 被清空

**状态：已解决。**

- 当前文件为 461 行，不再是 0 字节。
- `InstanceManager`、`InstanceInUseError`、`InstanceNotFoundError` 均可正常导入。
- 实例生命周期、路由绑定、健康检查、失败重试和 shutdown 行为由 `tests/test_local_ai_instances.py` 覆盖，实跑 53 项全部通过。

### Critical-2：缺少 `resolve_runtime` / `selection_identity`

**状态：已解决。**

- `resolve_runtime(purpose)` 会读取当前选中实例，检查实例状态、健康状态和 runtime 健康结果；已选择但停止、缺失或不健康时抛 `RuntimeValidationError`。
- `selection_identity(purpose)` 返回 `(selected_instance_id, generation)`，实例切换时 generation 递增，提供稳定且可区分的缓存身份。
- managed embedding/reranker service 将 manager 的运行时异常统一转换为 `LocalModelUnavailableError`，不会对已明确选择但不可用的实例静默降级。

### Important-1：停止实例后的缓存命中可能静默回退 bundled

**状态：已解决。**

- 停止实例后，manager 保留该 purpose 的选择身份，但移除实例和 runtime；后续 `resolve_runtime()` 明确抛不可用异常。
- `VectorStore.embed()` 在读取缓存前先调用 `selection_key()`，因此缓存命中也会先验证选中实例可用性。
- `test_cached_embedding_checks_stopped_selected_instance`、`test_stopped_local_embedding_does_not_fallback_to_bundled_bge` 和 `test_stopping_selected_local_embedding_preserves_unavailable_selection` 均通过。

### Important-2：实施报告与实际 tree 不一致

**状态：未解决，非代码阻断。**

- 报告仍称 HEAD 为 `e5f6d73`，当前实际 HEAD 为 `dc8ecb7`。
- 报告仍称指定回归为 16 项；当前 `test_local_ai_memory_integration.py` 已扩展至 21 项，指定三文件合计 28 项。
- 报告变更文件仍未反映 `local_ai/instances/manager.py`、`tests/test_local_ai_instances.py` 和 `web/routers/local_ai.py` 等当前修复范围。

### Important-3：静态检查缺陷

**状态：部分解决。**

- 首审记录的 `memory/vector_store.py` 重复 `clear()` 已消除，当前仅有一个定义。
- `core/bootstrap.py` 的 `ModelPurpose` 未用导入仍存在，为本任务新增 F401。
- 对审查范围执行 ruff 得到 22 项错误；除上述 F401 外，主要为既有文件的 I001、E402 及局部未用导入。该结果不影响运行验证，但不满足全量 lint clean。
- `VectorStore._embedding_selection_key` 标注为 `int | None`，实际会保存 tuple 选择身份；运行正确，但建议改为 `Any | None` 或精确 tuple 类型，避免类型契约失真。

### Single-flight 实例身份隔离

**状态：已解决。**

- `_inflight` key 已由纯文本改为 `(selection_key, text)`，不同选中实例不共享旧 future。
- 同实例、同文本仍共享一次运行时调用。
- 异步嵌入完成时仅在当前 selection 与发起时一致时写入缓存，避免旧实例的迟到结果污染新实例缓存。
- 跨实例隔离、同实例共享和陈旧完成保护三类测试均通过。

## 规范核对

| 验收项 | 结果 | 证据 |
|---|---|---|
| `LocalEmbeddingService.embed(texts)` | 通过 | 支持 managed 与 bundled 路径，校验向量数量，runtime 调用经 `asyncio.to_thread` |
| `LocalRerankerService.score(query, documents)` | 通过 | 支持 selected runtime 与兼容 fallback，校验分数数量 |
| VectorStore 消费注入服务 | 通过 | bootstrap 注入 `embedding_service`；选中实例路径不再由 VectorStore 探测模型目录 |
| 保留 remote 与 bundled-BGE 兼容 | 通过 | 未选择实例时 local 使用 bundled fallback；remote 模式回归通过 |
| 已选实例停止/不健康时无静默回退 | 通过 | manager 保留选择身份并显式报错，缓存命中和未命中均覆盖 |
| 切换实例不复用旧向量 | 通过 | selection identity 触发缓存清理；single-flight 也按身份隔离 |
| bootstrap 生产装配 | 通过 | core 与 Web router 复用同一个 `core.local_ai_instances` |
| 指定回归 | 通过 | 21 + 4 + 3，共 28 项通过 |

## 质量观察

### 正向项

- 使用真实 `InstanceManager.start/stop` 的端到端测试，不仅依赖 Fake manager。
- 显式区分“从未选择实例”和“选择后实例不可用”，兼容 fallback 与 no-silent-fallback 语义不再冲突。
- `selection_identity` 同时包含实例 ID 与 generation，缓存身份不会只依赖模型文本或 runtime 对象偶然地址。
- single-flight 发起者和等待者都能收到 `LocalModelUnavailableError`，并避免跨实例 future 串用。
- Web router 复用 bootstrap 创建的实例管理器，避免 UI 启停实例与 memory service 观察不同 manager。

### 跟进项

1. 删除 `core/bootstrap.py` 中未使用的 `ModelPurpose` 导入。
2. 将 `VectorStore._embedding_selection_key` 类型从 `int | None` 修正为与实际 tuple 身份一致的类型。
3. 更新 `.superpowers/sdd/task-12-local-ai-report.md` 的 HEAD、测试结果和变更文件清单。
4. 若仓库要求本次触及文件 lint clean，应单独处理 ruff 存量项；避免把大范围格式噪声混入 Task 12 功能修复。

## 验证记录

```text
.venv/bin/python -m pytest tests/test_local_ai_memory_integration.py -q
21 passed in 2.54s

.venv/bin/python -m pytest tests/test_context_governance.py tests/test_local_embed_mode.py -q
7 passed in 4.37s

.venv/bin/python -m pytest tests/test_local_ai_instances.py -q
53 passed in 2.91s
```

```text
git diff --check -- <Task 12 与闭环依赖文件>
exit 0

.venv/bin/python -m py_compile <Task 12 与闭环依赖文件>
exit 0

.venv/bin/python -m ruff check <Task 12 与闭环依赖文件>
exit 1，22 errors
```

## 最终判定

- **Spec Compliance：通过**
- **Code Quality：有条件通过**
- **Merge Readiness：Task 12 功能可合入；建议先完成未用导入与报告一致性两项低成本清理**
