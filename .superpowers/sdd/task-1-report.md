# 任务 1 审查修复报告

## 状态

已完成全部指定审查项，未提交。

## 修复内容

- `CatalogModel.revision` 与 `InstalledModel.revision` 统一只接受 7–64 位十六进制字符串，拒绝短哈希、分支名、非 hex 字符、超长值及首尾空白。
- `ComputeDevice.backends` 接受 `ExecutionBackend` 实例或可解析映射；映射会规范化为冻结的 `ExecutionBackend`，其它类型明确拒绝。
- 递归冻结与序列化仅接受严格 JSON 值：字符串、布尔值、整数、有限浮点数、`None`、字符串键映射以及列表/元组。
- 明确拒绝 `NaN`、正负无穷、`Path`、集合、非字符串映射键及其它任意对象，避免 `json.dumps` 的宽松扩展泄漏到传输契约。
- 所有记录的往返测试增加 `json.dumps(payload, allow_nan=False)`，直接验证严格 JSON 可序列化性。

## TDD 证据

先增加审查项回归测试并运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_contracts.py -q
```

红灯结果为 `10 failed, 12 passed`，失败点分别命中 revision 约束、backend 规范化/拒绝，以及 `NaN`、`Infinity`、`Path` 拒绝。

完成最小实现后，同一测试文件结果为 `24 passed`。

## 最终验证

```bash
.venv/bin/python -m pytest tests/test_local_ai_contracts.py tests/test_phase6_modules.py -q
.venv/bin/python -m ruff check local_ai tests/test_local_ai_contracts.py
.venv/bin/python -m compileall -q local_ai
git diff --check -- local_ai tests/test_local_ai_contracts.py
```

结果：

- 测试：`68 passed in 2.40s`。
- Ruff：`All checks passed!`。
- Python 编译检查：通过。
- 差异空白检查：通过。

## 文件范围

- 修改 `local_ai/contracts.py`。
- 修改 `tests/test_local_ai_contracts.py`。
- 更新 `.superpowers/sdd/task-1-report.md`。
- 工作区原有大量其它修改和未跟踪文件均未改动、未清理、未暂存。
- 未执行 commit、push、reset、checkout、clean 或 stash。

## 追加修复：跨平台路径与字段类型边界

### 修复内容

- 绝对目录同时按 POSIX 与 Windows 路径语义校验，在 Linux 上也接受 Windows 盘符路径和 UNC 路径，同时拒绝两种平台的相对路径。
- `quantization`、`license` 与 `error` 显式要求为字符串或 `None`，避免类型注解被错误当作运行时校验。
- catalog 文件清单的 `sha256` 严格要求为 64 位十六进制摘要，拒绝短值、超长值及非十六进制字符。
- 所有声明为 `Mapping` 的记录字段先验证容器类型，再执行递归冻结，避免列表等 JSON 集合绕过字段契约。

### TDD 证据

四类回归测试均先通过临时恢复旧行为验证红灯，再恢复最小修复验证绿灯：

- 跨平台绝对路径：`2 failed, 4 passed` → `6 passed`。
- 可选字符串：`3 failed` → `3 passed`。
- SHA256：`4 failed` → `4 passed`。
- Mapping 字段：`9 failed` → `9 passed`。

红灯均由目标缺陷直接触发；临时旧行为在对应红灯后立即恢复，未保留在最终工作树中。

### 最终验证

```bash
.venv/bin/python -m pytest tests/test_local_ai_contracts.py tests/test_phase6_modules.py -q
.venv/bin/python -m ruff check local_ai tests/test_local_ai_contracts.py
.venv/bin/python -m compileall -q local_ai
git diff --check -- local_ai tests/test_local_ai_contracts.py .superpowers/sdd/task-1-report.md
```

结果：

- 测试：`123 passed in 4.83s`。
- Ruff：`All checks passed!`。
- Python 编译检查：通过。
- 差异空白检查：通过。
- 编辑器诊断：无错误或警告。
- 未提交，未触碰任务范围外的既有工作区改动。
