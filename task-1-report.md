# 任务 1 实施报告

## 范围

- 新增 `local_ai/__init__.py`。
- 新增 `local_ai/contracts.py`。
- 新增 `tests/test_local_ai_contracts.py`。
- 未修改任务范围外的现有文件，未提交任何改动。

## TDD 过程

### RED

先创建契约测试，再运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_contracts.py -q
```

测试在收集阶段按预期失败，原因是 `ModuleNotFoundError: No module named 'local_ai'`。这证明测试先于生产实现存在，并直接约束任务要求的公开模块。

复审 P1/P2 时先补充严格校验回归测试，再次运行同一命令。新增用例按预期出现 `29 failed, 26 passed`，失败均为目标字段未抛出 `ValueError`：

- 必填 UTC 时间戳接受了 `None`。
- `int` 字段接受了 `bool` 和 `float`。
- `bool` 字段接受了整数 `1`。
- 字符串序列接受裸字符串，且未逐项拒绝空白或非字符串值。

复审实现后另发现 `from_dict()` 在构造数据类前将裸字符串转换为元组，绕过了数据类入口校验。补充 2 个反序列化回归用例后按预期出现 `2 failed, 55 passed`，再修复该入口。

### GREEN

实现 4 个 JSON 安全字符串枚举：

- `ModelPurpose`
- `RuntimeKind`
- `TaskState`
- `DeviceState`

实现 7 个冻结数据类：

- `ComputeDevice`
- `ExecutionBackend`
- `CatalogModel`
- `InstalledModel`
- `DownloadTask`
- `RuntimeProfile`
- `ModelInstance`

所有公开数据类均提供 `to_dict()` 与 `from_dict()`，并支持枚举、嵌套记录、元组、映射和 UTC 时间戳的 JSON 安全往返。

实现的显式校验包括：

- 必填标识和文本不得为空。
- 大小、内存、速度、剩余时间和资源估算不得为负数。
- 所有声明为 `int` 的字段严格要求 `type(value) is int`，拒绝 `bool` 和 `float`；可选整数仅额外接受 `None`。
- `healthy`、`resumable` 和 `allow_fallback` 严格要求 `type(value) is bool`。
- 可用内存不得超过总内存，已下载字节不得超过总字节。
- catalog revision 不得为空，也不得使用 `main`、`master`、`latest` 或 `head` 等可变引用。
- catalog 文件清单不得为空，每个文件必须包含非空路径、正数大小和校验和。
- 传输记录使用路径字符串；已安装模型目录必须是绝对路径字符串。
- 必填时间戳不得为 `None`，且必须是 UTC 感知时间。
- `precisions` 与 `active_routes` 拒绝裸字符串，并要求每一项都是非空字符串；直接构造和 `from_dict()` 使用相同校验。
- 外部可变映射和嵌套集合在构造时被递归冻结，避免调用方后续修改影响记录。

### REFACTOR

抽取统一的文本、非负数、严格整数、严格布尔、字符串序列、UTC、路径、递归冻结和 JSON 转换辅助逻辑，保持各数据类校验集中且序列化行为一致。

## P1/P2 复审结论

- P1：必填 UTC 字段拒绝 `None`，`installed_at`、`created_at`、`updated_at`、`started_at` 已覆盖。
- P1：整数语义不再受 Python 中 `bool` 是 `int` 子类的影响；内存、字节数、参数量、下载大小、资源估算和 manifest 文件大小均严格校验。
- P1：布尔字段不再依赖类型注解，构造与反序列化均严格拒绝非 `bool` 值。
- P2：字符串序列不再通过 `tuple("text")` 静默拆成字符；裸字符串、空字符串、纯空白和非字符串项均被拒绝。
- 复审范围内未发现剩余 P1/P2 校验缺口。

## 验证

最终运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_contracts.py -q
.venv/bin/python -m ruff check local_ai tests/test_local_ai_contracts.py
git diff --check -- local_ai tests/test_local_ai_contracts.py task-1-report.md
```

结果：

- 契约测试：`57 passed`。
- Ruff：`All checks passed!`。
- 差异格式检查：通过，无空白错误。
- 额外执行 `json.dumps(record.to_dict())` 与 `from_dict()` 往返检查，退出码为 0。

## 改动保护

- 开始前检查到工作区已有大量已修改和未跟踪文件。
- 本任务只新增 `local_ai/`、`tests/test_local_ai_contracts.py` 和本报告。
- 未执行 reset、checkout、clean、stash、commit 或其他会覆盖现有改动的操作。
- 按要求未提交。
