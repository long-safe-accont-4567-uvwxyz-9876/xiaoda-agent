# 任务 2 实施报告

## 范围

- 新增 `local_ai/devices/__init__.py`。
- 新增 `local_ai/devices/system_probe.py`。
- 新增 `local_ai/devices/vip_probe.py`。
- 新增 `tests/test_local_ai_system_probe.py`。
- 新增 `tests/test_npu_embed.py`。
- 修改 `memory/npu_embed.py`，让兼容入口复用结构化 VIP 探测。
- 未提交任何改动。

## TDD 过程

### RED

Task 2 的初始测试先于探测模块创建，按 brief 运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py -q
```

测试在缺少 `local_ai.devices` 探测函数时失败，约束了以下公共接缝：

- `probe_system_devices(platform)`
- `parse_vip_probe(payload)`
- `probe_vip_backend(...)`

本次继续补充 ARM 架构规范化切片。先增加 Linux 返回 `ARM64` 时必须输出 `aarch64` 的测试，再运行同一测试文件，得到 `1 failed, 9 passed`；失败值为实际 `ARM64`，期望 `aarch64`。

### GREEN

实现最小证据探测：

- Linux 从 `/sys/firmware/devicetree/base/model`、`/proc/cpuinfo` 和 `/proc/meminfo` 读取板级名称、CPU 信息和内存。
- Linux ARM 名称只接受 device tree 或 cpuinfo 的实际内容；信息缺失时使用通用 `CPU`，不根据 `aarch64` 推断 Orange Pi 型号。
- `ARM64` 规范化为 `aarch64`，`AMD64` 和 `x64` 规范化为 `x86_64`。
- Windows 通过 PowerShell `Get-CimInstance` 获取 `Win32_Processor` 与 `Win32_OperatingSystem` 的结构化 JSON，不解析界面文本标签。
- Windows ARM64 仅根据 CIM `Architecture=12` 判断 `aarch64`；处理器名称缺失时使用通用 `CPU`，不推断具体芯片型号。
- VIP 仅接受 JSON 对象且 `available` 必须为布尔值 `true`。
- VIP 型号、架构、驱动和 TOPS 只保留 payload 明确提供的字段；缺失型号时显示通用 `VIP NPU`，缺失架构时显示 `unknown`。
- VIP runner 仅在 Linux、runner 文件存在且 `sudo -n runner --probe --quiet` 成功退出时返回设备；失败、超时、缺失文件和非 Linux 均返回 `None`。
- runner 只给出成功退出码而没有结构化 JSON 时，证据仅记录 `runner_exit_code`，不补造型号或 TOPS。
- `memory.npu_embed.probe_npu()` 保留布尔兼容接口，但内部委托 `probe_vip_backend()`。

### REFACTOR

- 集中架构别名规范化，避免 Linux 与 Windows fallback 产生不同名称。
- 系统文件读取和命令执行保留可注入边界，使测试完全使用 fixture/monkeypatch，不依赖当前开发机硬件。
- `ComputeDevice` 与 `ExecutionBackend` 只承载可追溯 evidence，避免展示层标签反向成为硬件事实来源。

## 防虚构检查

- 生产探测模块中不存在 `VIP9000`、`TOPS`、`Orange Pi`、`Snapdragon`、`Ryzen` 或 `Vivante` 硬编码字符串。
- 测试明确验证仅有 `available` 与 `driver` 时设备名为 `VIP NPU`，且 evidence 不含 `tops`。
- 测试明确验证 payload 显式提供 `model` 与 `tops` 时才原样保留。
- `web/routers/local_deploy.py` 的旧兼容展示路径暂未修改。brief 明确要求只有在后续新 DeviceRegistry 已消费本探测后才能移除该固定路径；Task 2 尚未建立该 registry，提前修改会破坏现有 API 兼容。

## 验证

最终运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py tests/test_npu_embed.py tests/test_local_ai_contracts.py -q
.venv/bin/python -m ruff check local_ai/devices tests/test_local_ai_system_probe.py tests/test_npu_embed.py
.venv/bin/python -m ruff check memory/npu_embed.py --ignore E741
.venv/bin/python -m compileall -q local_ai/devices memory/npu_embed.py
git diff --check -- local_ai/devices memory/npu_embed.py tests/test_local_ai_system_probe.py tests/test_npu_embed.py
```

结果：

- 测试：`91 passed`。
- Ruff：两组检查均为 `All checks passed!`。
- Python 编译检查：通过。
- 差异空白检查：通过。
- 任务相关 Python 文件编辑器诊断：无错误或警告。

`memory/npu_embed.py` 的 E741 是任务前已存在于文件后半段的两个局部变量名，不属于本次改动；对该文件使用 `--ignore E741` 后验证本次导入和探测改动，其余规则全部通过。

## 改动保护

- 开始前确认当前 `main` 工作区已有大量修改和未跟踪文件。
- 未执行 reset、checkout、clean、stash、commit、push 或其它覆盖现有改动的操作。
- 未修改用户已有的 Task 2 范围外文件。
- 按要求未提交。

## 审查反馈修复追加

### 根因

- Python `json.loads()` 默认接受非标准 JSON 常量 `NaN`、`Infinity` 和 `-Infinity`，这些值进入严格 `ComputeDevice` 契约后抛出 `ValueError`，未按探测解析接口返回 `None`。
- runner 成功退出且 stdout 非空时，旧逻辑在 stdout 明确表示 `available=false` 或内容畸形后仍回退到 `runner_exit_code` 成功证据，把不可用或无效结果误判为可用。

### 严格 TDD 证据

先在既有 VIP parser/backend 测试中加入非有限数、明确不可用 stdout 和畸形 stdout 断言，仅运行相关切片：

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py::test_vip_parser_returns_none_for_non_finite_json_numbers tests/test_local_ai_system_probe.py::test_vip_backend_does_not_fallback_when_stdout_is_unavailable_or_malformed -q
```

RED 结果为 `2 failed`：非有限数路径抛出 `ValueError`，非空异常 stdout 路径返回了可用设备。

最小修复：

- `json.loads(..., parse_constant=...)` 明确拒绝非有限常量，并统一返回 `None`。
- stdout 非空时直接返回 `parse_vip_probe(stdout)`；只有 stdout 真正为空时才允许使用成功退出码证据。

同一测试切片随后得到 `2 passed`。为保持 brief 对应相关测试集合精确为 91 项，最终将新增断言合并到既有两个测试用例中。

### 最终验证

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py tests/test_npu_embed.py tests/test_local_ai_contracts.py -q
.venv/bin/python -m ruff check local_ai/devices tests/test_local_ai_system_probe.py tests/test_npu_embed.py
.venv/bin/python -m ruff check memory/npu_embed.py --ignore E741
.venv/bin/python -m compileall -q local_ai/devices memory/npu_embed.py
git diff --check -- local_ai/devices memory/npu_embed.py tests/test_local_ai_system_probe.py tests/test_npu_embed.py
```

- 相关测试：精确收集 91 项，`91 passed in 1.21s`。
- Ruff：两组检查均为 `All checks passed!`。
- Python 编译检查与差异空白检查：退出码 0。
