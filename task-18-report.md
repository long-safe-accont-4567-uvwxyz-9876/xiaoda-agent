# Task 18 复核报告

## 范围

- 计划：`docs/superpowers/plans/2026-08-09-local-ai-platform.md` 的 Task 18。
- 复核文件：`web/frontend/src/api/localAi.ts`、`web/frontend/src/stores/localAi.ts`、`web/frontend/src/api/ws.ts`、`tests/test_frontend_local_ai_contracts.py`。
- 约束：复核并补全现有未提交实现，不提交 Git 变更。

## 计划验收

- 已提供五类资源的 TypeScript 契约：设备、目录模型、已安装模型、下载任务、运行实例。
- 已提供加载、重扫、下载、暂停、恢复、取消、启动、停止、删除与存储目录偏好 API。
- Pinia store 使用按 ID 规范化的五类资源集合。
- 并发 `load()` 使用 generation 防止旧请求覆盖新请求。
- WebSocket 声明并订阅设备、下载、实例更新，支持解除订阅。

## 复核发现

现有实现的 generation 只处理并发 REST 加载，未处理 REST 与 WebSocket 的竞态。加载期间收到设备、下载或实例更新后，较旧的 REST 快照仍会覆盖实时状态。

进一步行为复核发现，按资源修订号合并整个当前集合会错误保留快照已删除的旧实体。最终实现改为按资源记录加载期间实际收到 WebSocket 更新的实体 ID：REST 快照先完整生效，仅这些 ID 使用当前实时实体覆盖同 ID 快照值。

后续重叠加载复核发现，`loadA` 尚未结束时启动 `loadB` 会无条件重置 `loadingUpdates`。因此 `loadA` 与 `loadB` 之间收到的 WebSocket 更新不再属于任何保护集合，`loadB` 的 REST 快照仍可覆盖实时状态。最终实现以 `loading` 从 `false` 变为 `true` 作为加载窗口起点，仅在进入新窗口时初始化保护集合；同一连续窗口内的重叠 `load()` 复用该集合。

## TDD 证据

### 红灯一

命令：

```bash
.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py::test_local_ai_store_preserves_websocket_updates_during_load -q
```

结果：`1 failed`，缺少 `resourceRevisions`，证明测试能识别 REST 覆盖实时更新的缺口。

### 绿灯一

增加逐资源修订号和条件替换后，目标文件测试与 `vue-tsc` 通过。随后复核发现直接跳过整个资源快照会遗漏未被实时更新的其他实体，因此继续第二轮 TDD。

### 红灯二

命令：

```bash
.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py::test_local_ai_store_preserves_websocket_updates_during_load -q
```

结果：`1 failed`，缺少 `reconcileSnapshot`，证明测试锁定了快照与实时集合合并行为。

### 绿灯二

实现 `reconcileSnapshot`，以 REST 快照为基础、当前实时集合为优先进行合并。

### 红灯三

新增真实 Pinia store 行为测试，通过延迟 REST 设备快照并在加载期间派发 WebSocket 设备更新，同时预置一个快照已删除的旧设备。

```bash
.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py::test_local_ai_store_only_preserves_ids_updated_during_load -q
```

结果：`1 failed`，明确报错“未被 WS 更新的旧实体未按快照删除”。测试同时验证加载期间同 ID 的 WebSocket 新值未被快照覆盖。

### 绿灯三

将资源级 revision 改为加载期间按资源维护更新 ID 集合。`reconcileSnapshot` 先索引完整 REST 快照，再仅覆盖集合中记录的 ID，因此实时更新受保护，快照删除也能生效。

### 红灯四

新增真实 Pinia store 行为测试，严格执行 `loadA → WS → loadB`，让 `loadB` 先返回旧快照，再让已失效的 `loadA` 返回。

```bash
.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py::test_local_ai_store_preserves_websocket_update_across_overlapping_loads -q
```

结果：`1 failed`，Node 行为断言明确报错“loadA 与 loadB 的重叠加载窗口丢失了 WS 更新”，证实第二次 `load()` 重置保护集合的竞态。

### 绿灯四

仅在 `loading.value` 为 `false` 时初始化 `loadingUpdates`。重叠加载仍使用 generation 决定哪个 REST 结果可生效，但同一连续加载窗口内收到的 WebSocket 更新 ID 不再因新一代加载启动而丢失。

重新运行同一目标测试，结果：`1 passed`。

## 最终验证

```bash
.venv/bin/python -m pytest tests/test_frontend_local_ai_contracts.py -q
```

结果：`9 passed`。

```bash
cd web/frontend && npx vue-tsc --noEmit
```

结果：退出码 `0`，无 TypeScript 错误输出。

```bash
.venv/bin/python -m pytest -q
```

结果：完整套件收集 `3464 items`，两次执行均在 `tests/test_episodic_memories_updated_at.py` 附近被外部 `KeyboardInterrupt` 中止，未得到完整通过结论；中止前一次为 `563 passed`，另一次运行至约 `29%`，没有观察到 Task 18 相关失败。

```bash
git diff --no-index --check /dev/null web/frontend/src/stores/localAi.ts
git diff --no-index --check /dev/null tests/test_frontend_local_ai_contracts.py
git diff --no-index --check /dev/null task-18-report.md
```

结果：退出码 `0`，无空白错误。

## 结论

Task 18 已补充第四轮行为级红灯、绿灯，覆盖 `loadA → WS → loadB` 重叠加载竞态。实现按连续加载窗口保护 WebSocket 更新，同时保留 generation 防旧请求覆盖与快照删除语义。目标测试与 `vue-tsc` 通过；完整 pytest 仍沿用此前外部 KeyboardInterrupt 的未完成结论，未执行提交。
