# 任务 3 实施报告

## 范围

- 新增 `local_ai/devices/ort_providers.py`。
- 新增 `local_ai/devices/registry.py`。
- 修改 `local_ai/devices/__init__.py`，导出 ORT 探测与设备注册表接口。
- 新增 `tests/test_local_ai_device_registry.py`。
- 修改 `memory/local_embed.py`，移除固定 CPU provider 配置。
- 未提交任何改动。

## TDD 过程

### RED

先创建 8 个行为测试，覆盖：

- 保留 ONNX Runtime 返回的 provider 顺序。
- 可枚举但最小 Session 初始化失败的 provider 必须标记为不健康。
- 普通扫描复用缓存，`force=True` 必须重新枚举并逐个验证 provider。
- 推荐前过滤不兼容和不健康的 backend。
- 同时可用时优先健康的加速 backend，再使用 CPU fallback。
- 手动 override 必须通过模型兼容性校验。
- 可用内存低于 manifest 最低要求时拒绝推荐。
- `LocalEmbeddingProvider` 必须原样透传 registry 提供的有序 `providers` 与 `provider_options`。

首次运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_device_registry.py -q
```

结果在测试收集阶段按预期失败：`ModuleNotFoundError: No module named 'local_ai.devices.ort_providers'`。这证明测试先于生产实现，并且失败原因是 Task 3 接口尚不存在。

### GREEN

最小实现包括：

- `OrtProviderProbe.list_available()` 从 `onnxruntime.get_available_providers()` 读取实际顺序。
- `OrtProviderProbe.verify()` 使用内嵌的单节点 Identity ONNX 模型创建最小 `InferenceSession`；成功才标记健康，异常文本写入 evidence。
- `DeviceRegistry.scan()` 缓存结构化设备快照并返回列表副本，避免调用方修改缓存；`force=True` 忽略旧缓存并重新执行系统探测、provider 枚举和 Session 验证。
- CPU provider 绑定到系统 CPU；其它 ORT provider 形成基于 runtime 证据的 accelerator 设备，不推断具体 GPU 型号或显存。
- `recommend()` 只保留健康、设备可用且符合 manifest 架构、平台、provider、runtime、purpose、precision 与最低内存约束的候选项。
- 推荐排序优先非 CPU 加速 backend，再按可用内存排序；无候选或 override 不兼容时抛出 `IncompatibleBackendError`。
- `LocalEmbeddingProvider` 新增 `providers` 和 `provider_options` 参数；registry 未提供时交由 ONNX Runtime 使用自身默认 provider 顺序，不再在适配器内固定 `CPUExecutionProvider`。

首次 GREEN 运行得到 `1 failed, 7 passed`。失败是强制重扫结果的设备展开顺序没有保留 ORT provider 顺序。随后只调整 backend 设备组装顺序，同一测试文件得到 `8 passed`。

### REFACTOR

- 将 manifest 字符串列表读取、最低内存读取和 ORT provider 到设备 ID 的转换收敛为私有函数。
- 保持 `OrtProviderProbe` 只负责运行时枚举和 Session 健康验证，`DeviceRegistry` 负责缓存、设备组合、兼容过滤与推荐。
- 没有在 Embedding 适配器内增加硬件探测或推荐逻辑，provider 决策仍由 registry 提供。

## 验证

最终运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_device_registry.py tests/test_local_embed_mode.py -q
.venv/bin/python -m ruff check local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
.venv/bin/python -m compileall -q local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
git diff --check -- local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
```

结果：

- Task 3 与 Embedding 回归测试：最终复核为 `11 passed in 6.54s`。
- Ruff：`All checks passed!`。
- Python 编译检查：退出码 0。
- 差异空白检查：退出码 0。
- 编辑器工作区诊断：0 项。

`tests/test_local_embed_mode.py` 的 3 个真实模型集成测试在当前环境实际执行并通过，没有被跳过。

## 改动保护

- 开始前确认当前 `main` 工作区已有大量用户修改和未跟踪文件。
- 未执行 reset、checkout、clean、stash、commit、push 或其它覆盖现有改动的操作。
- 仅改动 Task 3 指定文件和本报告。
- 按要求未提交。

## 评审修复追加记录（2026-08-10）

### 评审材料核对

- `task-3-review.md` 实际审查的是旧的 G1 问候短路任务，`task-3-review-diff.md` 实际记录的是另一项记忆 CRUD 任务，均不对应当前 ONNX Execution Provider Registry。
- 本次以 `task-3-brief.md`、本报告和 `task-3-review.diff` 中的 Task 3 原始差异为审查基线。
- 原始差异确认两处缺陷：`OrtProviderProbe.verify()` 只创建 Session 就报告健康，没有检查 Session 实际启用的 provider，也没有执行首次推理；非 CPU ORT provider 设备直接复制 CPU 的架构、总内存、可用内存和系统字段。

### 严格 TDD 补测

先针对原始评审基线补充 5 个回归测试：

- `test_provider_probe_rejects_silent_cpu_fallback`：请求 ROCm、实际 Session 仅启用 CPU 时必须判定不健康，且不得继续执行推理。
- `test_provider_probe_rejects_first_inference_failure`：Session 可创建但首次最小推理失败时必须判定不健康。
- `test_provider_probe_disables_cpu_fallback_for_accelerators`：验证非 CPU provider 的探测 Session 显式禁用 CPU EP fallback。
- `test_unknown_accelerator_does_not_inherit_cpu_resources`：非 CPU provider 未有独立硬件证据时，架构必须为 `unknown`，内存必须为 0，系统字段必须为空。
- `test_unknown_accelerator_resources_fail_positive_memory_requirement`：未知加速器资源不得借用 CPU 内存通过模型最低内存约束。

RED 依据来自测试与 `task-3-review.diff` 原始实现的逐项对照：原始 `verify()` 没有 `get_providers()`、`run()` 和禁用 CPU fallback，前三个测试会分别在健康状态、推理调用和 Session 配置断言处失败；原始 `_attach_backends()` 明确使用 `cpu.architecture`、`cpu.memory_total`、`cpu.memory_available`、`cpu.system`，后两个测试会分别在资源字段断言及应抛出 `IncompatibleBackendError` 处失败。进入本次复核时工作树已包含对应最小生产修复，因此没有回退或覆盖现有用户改动来伪造第二次 RED。

### 最小修复

- 非 CPU provider 探测 Session 写入 `session.disable_cpu_ep_fallback=1`。
- Session 创建后读取 `get_providers()`，请求 provider 不在实际 provider 列表时记录静默回退并判定不健康。
- provider 校验执行一次单元素 float32 Identity 推理；首次推理异常即判定不健康。
- 非 CPU provider 仅依据 ORT runtime 证据登记为 accelerator；没有独立硬件探测证据时使用 `architecture="unknown"`、`memory_total=0`、`memory_available=0`、`system={}`，不复制 CPU 元数据。

### 最终复核

```bash
.venv/bin/python -m pytest tests/test_local_ai_device_registry.py tests/test_local_embed_mode.py -q
.venv/bin/python -m ruff check local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
.venv/bin/python -m compileall -q local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
git diff --check -- local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py task-3-report.md .superpowers/sdd/task-3-report.md
```

- Task 3 与 Embedding 回归：`16 passed in 3.24s`。
- Ruff：`All checks passed!`。
- Python 编译检查与差异空白检查：退出码 0。
- 真实 ONNX Runtime 枚举到 `AzureExecutionProvider`、`CPUExecutionProvider`。Azure provider 在禁用 CPU fallback 后因最小 Identity 图节点只能分配给默认 CPU EP 而被正确判定为不健康；CPU provider 实际 provider 为 `CPUExecutionProvider` 并成功完成最小推理。
- 真实设备扫描中 `ort:azure` 为 `architecture="unknown"`、内存 0、`system={}`；系统 CPU 保留独立探测到的 `aarch64` 与系统内存，两者没有资源串用。

## v2 复审修复追加记录（2026-08-10）

### 设计核对

- 依据设计 §Device Registry 的顺序约束，将架构兼容、最低/推荐资源、健康加速和 manifest fallback 分为独立决策，不用 CPU 内存填充未知加速器。
- ORT provider 代表当前主机上的执行能力，因此未知加速器可使用真实主机架构参与 manifest 兼容判断；设备对外字段仍保持 `architecture="unknown"`、内存 0，避免伪造硬件证据。
- fallback 不再固定开启，只在 manifest 的有序 provider 列表中所选 provider 后面仍有声明项时启用。

### 严格 TDD

- 先新增 5 项行为覆盖：主机架构兼容但不复制内存、recommended 资源优先级、manifest fallback、无系统探测时 CPU 建模、真实主机架构测试。
- 首次运行共 `18` 项：`4 failed, 14 passed`。失败分别命中缺少的前四项行为；真实主机架构测试已通过，确认夹具没有继续硬编码 x86_64。
- 最小实现后同一文件得到 `18 passed`。

### 最小修复

- 对架构未知的 ORT 设备，仅在兼容判断内部使用标准化后的 `platform.machine()`；不修改设备资源证据。
- 推荐排序先判断是否达到 `recommended_memory`、`recommended_ram` 或 `recommended_vram`，再比较加速/CPU 和可用内存；缺少 recommended 时回落到 minimum。
- `RuntimeProfile.allow_fallback` 由 manifest provider 顺序推导。
- 系统探测未返回 CPU、但 ORT 暴露 CPU provider 时，登记真实主机架构、零内存的 `cpu:0`，平台来自当前运行时。
- 测试 CPU 架构改为读取并标准化真实 `platform.machine()`，覆盖当前 ARM64 主机。

### 验证

- Task 3 与真实 Embedding 回归：`21 passed in 3.32s`。
- Ruff：`All checks passed!`。
- 编辑器诊断：生产文件与测试文件均为 0 项。

## 方案 A 复审修复追加记录（2026-08-10）

### 范围裁决

- Task 3 在强制重扫时保留本次枚举中消失的 provider 设备，设备状态设为 `UNAVAILABLE`，对应 backend 标记为不健康，推荐必须拒绝。
- 正在运行的模型实例如何降级、停止或恢复属于 Task 11 的实例生命周期，本次不提前实现。

### 严格 TDD

- 先新增两个行为测试，覆盖消失设备保留及推荐拒绝。
- RED 切片得到 `1 failed, 1 passed`；失败为 `StopIteration`，证明原实现直接删除了消失的 `ort:rocm` 设备。
- 最小实现后同一切片得到 `2 passed`。

### 最小修复

- 强制重扫对比上次 backend 集合与本次 ORT 枚举结果。
- 对消失 provider 保留原 backend 契约，但设置 `healthy=False`，并记录 `reason="provider_disappeared"`。
- 对应设备保留原 ID 与元数据，状态改为 `DeviceState.UNAVAILABLE`；既有兼容过滤同时检查设备可用与 backend 健康，因此自动拒绝推荐。
- 未引入实例引用、路由依赖或运行时停止逻辑。

### 最终验证

- Task 3 与真实 Embedding 回归：`23 passed in 3.32s`。
- Ruff：`All checks passed!`。
- Python 编译检查：退出码 0。
- 编辑器诊断接口因当前工作区路径映射限制返回访问拒绝，未将其记为成功；使用 Ruff、compileall 与 pytest 完成静态和运行验证。

## 方案 A 平台 GPU 探测完成记录（2026-08-10）

### 已确认实现

- Windows 在同一次 PowerShell 调用中读取 `Win32_Processor`、`Win32_OperatingSystem` 和 `Win32_VideoController`，显卡只使用 CIM 返回的名称、AdapterRAM、PNP PCI 标识和驱动版本。
- Linux NVIDIA 使用 `nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.free,driver_version,pci.bus_id --format=csv,noheader,nounits`，通过标准 CSV 解析器处理带逗号的引号名称。
- Linux AMD 只遍历 `/sys/class/drm/card[0-9]*/device`，要求 PCI vendor 为 `0x1002`，从 sysfs 的 device、uevent、VRAM 和 product_name 文件构造证据。
- CIM 的 AdapterRAM 只代表容量，不代表当前空闲显存，因此 Windows GPU 的 `memory_available` 保持 0；NVIDIA 使用 `memory.free`，AMD 使用 `total-used`。
- ORT 的 CUDA、ROCm 和 DirectML backend 绑定所有匹配的真实 GPU 设备，并为多卡写入独立 `device_id`；没有匹配硬件证据时仍保留原有 `ort:*` 未知资源设备，不借用 CPU 或其它 GPU 数据。

### 严格 TDD

- 系统探测先新增 4 个用例：Windows CIM VideoController、NVIDIA CSV、AMD DRM PCI、过滤非 AMD 与 DRM connector。首次运行得到 `4 failed, 10 passed`，失败分别为只返回 CPU 及缺少 `_glob_paths` 接缝。
- 注册表先新增 CUDA、ROCm、DirectML 三组参数化硬件绑定用例。首次运行得到 `3 failed, 20 passed`，真实 GPU 没有绑定 backend。
- 最小实现后，系统探测切片为 `14 passed`，注册表切片为 `23 passed`。
- 自审发现 DRM glob 也可能返回 connector，补充真实 AMD connector fixture 后先得到 `1 failed`，再以严格 card 名称过滤修复。随后补充 NVIDIA 多卡与手动 override 用例，先得到 `1 failed`，再让每张匹配 GPU 获得独立 backend options。

### 最终验证

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py tests/test_npu_embed.py tests/test_local_ai_device_registry.py tests/test_local_embed_mode.py tests/test_local_ai_contracts.py -q
.venv/bin/python -m ruff check local_ai/devices memory/local_embed.py tests/test_local_ai_system_probe.py tests/test_local_ai_device_registry.py
.venv/bin/python -m compileall -q local_ai/devices memory/local_embed.py tests/test_local_ai_system_probe.py tests/test_local_ai_device_registry.py
git diff --check -- local_ai/devices tests/test_local_ai_system_probe.py tests/test_local_ai_device_registry.py task-3-report.md .superpowers/sdd/progress.md
```

- 相关回归：`122 passed in 5.78s`。
- Ruff：`All checks passed!`。
- Python 编译检查与差异空白检查：退出码 0。
- 编辑器工作区诊断：0 项。
- 当前 Linux ARM64 主机真实探测只返回有证据的 `cpu:0`；未检测到 NVIDIA/AMD GPU 时没有虚构 GPU 设备。

## Task 3 未完成项修复记录（2026-08-10）

### 严格 TDD 证据

- 修复前注册表基线为 `24 passed`。
- 首轮先新增 CPU EP、未知 ORT 平台、fallback provider 顺序、真实 GPU 身份四项测试；运行得到 `3 failed, 24 passed`，失败分别为缺少 `fallback_providers`、无 CPU EP 仍合成 CPU、未知 ORT 设备缺少 host platform。
- 真实 GPU 首版夹具因第二次仍返回 GPU 而意外通过；收紧为 provider 与系统 GPU 证据同时消失后，单测按预期以 `StopIteration` 失败，证明原实现退化成 `ort:cuda` 并丢失原设备 ID。
- 四项最小实现转绿后，自审补充“系统仍探测到 GPU、只有 provider 消失”的边界测试；该测试先以设备仍为 `AVAILABLE` 失败，再修复为复用原设备身份并标记 `UNAVAILABLE`。

### 最小实现

- 仅当本轮 ORT 枚举包含 `CPUExecutionProvider` 且系统探测未提供 CPU 时，才合成零内存 `cpu:0`。
- 未绑定真实硬件的 ORT 设备继续保持 `architecture="unknown"` 和内存 0，但 `system.platform` 写入真实 host platform。
- `RuntimeProfile.options.fallback_providers` 保存 manifest 中所选 provider 后面的全部 provider，保持原始后继顺序；无后继时不写入该项，`allow_fallback` 与该序列是否非空一致。
- provider 消失时，从上次设备快照恢复原真实 GPU ID、backend options 与硬件元数据，backend 标记不健康，设备标记 `UNAVAILABLE`；无论本轮硬件探测是否仍看到该 GPU，都不生成替代 `ort:*` ID。

### 最新验证

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py tests/test_npu_embed.py tests/test_local_ai_device_registry.py tests/test_local_embed_mode.py tests/test_local_ai_contracts.py -q
.venv/bin/python -m ruff check local_ai/devices memory/local_embed.py tests/test_local_ai_system_probe.py tests/test_local_ai_device_registry.py
.venv/bin/python -m compileall -q local_ai/devices memory/local_embed.py tests/test_local_ai_system_probe.py tests/test_local_ai_device_registry.py
git diff --check -- local_ai/devices/registry.py tests/test_local_ai_device_registry.py task-3-report.md .superpowers/sdd/progress.md
```

- 相关回归：`126 passed in 4.02s`。
- Ruff：`All checks passed!`。
- Python 编译检查与差异空白检查：退出码 0。
- 生产文件与测试文件编辑器诊断：0 项。
- Task 3 进度仍保持未完成，等待完成门禁复核，不以本报告提前标记完成。

## v4 Critical/Important 完成记录（2026-08-10）

### 方案 A 资源契约

- Manifest 新选择只接受 `minimum_ram`、`recommended_ram`、`minimum_vram`、`recommended_vram`。
- 旧 `minimum_memory`、`min_memory`、`recommended_memory` 仅允许零值表示未声明；任何正值均抛出 `InvalidResourceRequirementsError` 并指出字段，不按 provider 或设备类型猜测。
- 类型化字段不是非负整数时立即报告配置错误，不退化成笼统的无兼容 backend。
- 推荐分别使用主机 CPU 设备的可用 RAM 和候选 GPU/accelerator 自身的可用 VRAM；未知 ORT 加速器保持零 VRAM。
- `RuntimeProfile` 将歧义的 `estimated_memory` 替换为 `estimated_ram` 与 `estimated_vram`，旧 payload 明确拒绝。

### 多 backend 状态修复

- 同一 GPU 绑定多个 provider 时，设备状态由全部 backend 合并决定，不再被最后处理的失败 backend 覆盖。
- peer provider 重扫消失时保留其不健康 backend 证据，同时保留当前健康 backend；只要仍有健康 backend，设备保持 `AVAILABLE` 并可推荐该健康 binding。

### TDD 与验证

- RED：注册表测试先因 `InvalidResourceRequirementsError` 不存在而在收集阶段失败；契约测试得到 `5 failed, 75 passed`，命中缺少类型化运行配置字段及旧字段仍被接受。
- GREEN：契约与注册表合计 `119 passed`；Local AI 相关完整回归 `138 passed in 9.36s`。
- Ruff 对本次生产与测试范围返回 `All checks passed!`；compileall、diff check 退出码 0；三个核心生产/测试文件编辑器诊断均为 0 项。
- 扩大 Ruff 到既有 `memory/npu_embed.py` 时发现两项既存 E741 单字变量问题，不属于本次 v4 差异；本次精确范围 Ruff 通过，未借机修改无关代码。
- CodeRabbit CLI 0.7.2 已认证；对当前全部未提交工作树执行审查命令后退出码 0、无发现输出。工作树包含大量其它用户改动，因此最终结论同时以本次精确范围测试、静态检查和人工差异复核为准。

## ROCm card 批次原子映射（2026-08-10）

### 验收行为

- `rocm-smi --showbus --json` 的 card 批次按 PCI Bus 映射到 Linux AMD DRM 设备，不依赖 DRM card 枚举顺序。
- 每个 card 必须同时具备合法 `cardN` 名称、对象结构和 PCI Bus；任一条目畸形、PCI Bus 重复或批次结构非法时，整批返回空映射。
- 只有完整批次才写入 `ROCMExecutionProvider` 的 `provider_ordinals`，避免跳过坏条目后把剩余 ordinal 部分错配到物理 GPU。

### TDD 与验证

- RED 1：原子失败测试先以缺少 `_parse_rocm_card_ordinals` 失败；最小解析实现后通过。
- RED 2：完整批次按 PCI Bus 映射测试先以缺少 `provider_ordinals` 失败；接入 AMD 探测后两个验收测试均通过。
- 系统探测与设备注册表回归：`80 passed in 1.05s`；Local AI 相关完整回归：`165 passed in 4.33s`。
- Ruff 返回 `All checks passed!`；compileall、diff check 退出码 0；生产与测试文件编辑器诊断均为 0 项。

## 三接缝切片 1 实施记录（2026-08-10）

### 接缝确认

- 设备证据到 backend 绑定：稳定 `ComputeDevice.id` 必须携带运行时可执行的 `device_id`，不能依赖探测列表顺序。
- 注册表推荐到运行时配置：`RuntimeProfile` 必须给出已验证、兼容且保持 manifest 顺序的 `providers` 与对齐的 `provider_options`。
- 运行时配置到 Embedding 适配器：`LocalEmbeddingProvider` 必须能直接消费 `RuntimeProfile`，调用方不再自行拆装 provider 配置。

### 严格 TDD

- RED：先新增三个最小行为测试，同一注册表文件得到 `3 failed, 39 passed`。
- 稳定绑定测试观察到 NVIDIA 设备被错误分配为探测顺序 `0/1`，而非证据中的运行时索引 `2/7`。
- 可执行 fallback 测试因 `RuntimeProfile.options` 缺少 `providers` 失败；适配器测试因缺少 `from_runtime_profile` 失败。
- GREEN：读取 NVIDIA `evidence.index` 生成 `device_id`；推荐只把当前健康、可用且兼容的 manifest 后继 provider 放入执行链，并生成一一对齐的 options；Embedding 工厂将不可变运行配置转换为 ORT 所需列表。
- REFACTOR：provider 链组装收敛在 `DeviceRegistry._provider_chain()`，适配器只负责类型转换和 Session 构造，不重复设备兼容判断。

### 验证与状态

- 定向 registry 与真实 Embedding 回归：`45 passed in 3.15s`。
- Local AI 相关完整回归：`141 passed in 3.99s`。
- Ruff：`All checks passed!`；compileall 与 diff check 退出码 0；三个改动文件编辑器诊断均为 0 项。
- Task 3 继续保持未完成；切片 1 完成不等同于 Task 3 完成，后续切片与完成门禁仍待执行。

## 三接缝切片 2 实施记录（2026-08-10）

### 行为范围

- `RuntimeProfile.options.fallback_bindings` 按 manifest provider 顺序输出可执行候选；同 provider 多卡按可用资源排序，允许逐设备 fallback。
- 每个 binding 固定包含稳定 `device_id`、`provider` 和 `provider_options`，不再只表达 provider 名称。
- fallback 候选复用推荐兼容性门禁，只包含健康、设备可用、架构/平台/runtime/purpose/precision 兼容且满足最低 RAM/VRAM 的候选。
- `LocalEmbeddingProvider.from_runtime_profile()` 将主绑定与 `fallback_bindings` 转换为一一对齐的 ORT `providers`/`provider_options`。
- DirectML 的最小 probe 和实际 Embedding load 均设置 `enable_mem_pattern=False` 与 `ORT_SEQUENTIAL`。

### 严格 TDD 证据

- fallback binding 首测先以 `KeyError: 'fallback_bindings'` 失败，最小实现后通过。
- 健康/兼容/资源过滤边界测试通过，确认不健康 ROCm、低 VRAM 同 provider 设备及平台不兼容设备不会进入 bindings。
- Embedding 映射测试先观察到实际 providers 仅有主 `CUDAExecutionProvider`，预期的同 provider 第二张卡和 CPU 均缺失；实现 bindings 映射后通过。
- DirectML probe 测试先观察到 `enable_mem_pattern` 仍为 `True`；最小设置后通过。
- DirectML load 测试先观察到实际 SessionOptions 的 `enable_mem_pattern` 仍为 `True`；最小设置后通过。

### 验证与状态

```bash
.venv/bin/python -m pytest tests/test_local_ai_system_probe.py tests/test_npu_embed.py tests/test_local_ai_device_registry.py tests/test_local_embed_mode.py tests/test_local_ai_contracts.py -q
.venv/bin/python -m ruff check local_ai/devices/registry.py local_ai/devices/ort_providers.py memory/local_embed.py tests/test_local_ai_device_registry.py
.venv/bin/python -m compileall -q local_ai/devices memory/local_embed.py tests/test_local_ai_device_registry.py
git diff --check -- local_ai/devices/registry.py local_ai/devices/ort_providers.py memory/local_embed.py tests/test_local_ai_device_registry.py task-3-report.md .superpowers/sdd/progress.md
```

- Local AI 相关回归：`153 passed in 4.01s`。
- Ruff：`All checks passed!`。
- Python 编译检查与差异空白检查：退出码 0。
- Task 3 仍保持未完成；切片 2 的测试与实现通过不代表完成门禁已通过。

## 多卡消失恢复与报告纠偏（2026-08-10）

### 验收合同

- 可跨重扫保留的物理设备仅限 `kind == "gpu"` 且 `identity_persistent` 不是 `False` 的真实 GPU。
- `ort:dml:default`、通用 accelerator 和 ephemeral/弱证据 GPU 不进入消失设备保留集合。
- 多张真实 GPU 中单卡从系统探测消失时，只保留该卡的稳定 ID、backend options 和硬件证据；backend 标记为不健康并记录 `reason="device_disappeared"`，设备为 `UNAVAILABLE`。
- 同一卡再次被系统探测到时复用当前健康绑定，不重复保留旧绑定，设备恢复为 `AVAILABLE`。
- 同一真实 GPU 只要仍有健康 backend 就保持 `AVAILABLE`；全部 backend 不健康时才为 `DEGRADED`，自动推荐只选择健康 binding。

### 严格 TDD 证据

- 多卡消失恢复测试首次运行得到 `1 failed, 1 passed`；失败为 `StopIteration`，证明旧实现直接删除了 provider 仍存在但物理卡消失的 GPU。
- 最小实现加入真实 GPU 身份门禁与按设备消失保留后，同一切片得到 `2 passed`；default/ephemeral 排除测试保持通过。
- 本轮曾按旧语义收紧为“任一 backend 不健康即 DEGRADED”，后续状态语义修正确认为“任一 healthy 即 AVAILABLE”，以文末最新合同为准。
- 对应历史测试曾得到 `69 passed in 1.00s`；后续 RED→GREEN 已覆盖并替代该状态断言。

### 报告验证

- 本节的设备筛选、消失原因、恢复去重、精确状态和推荐拒绝均有 `tests/test_local_ai_device_registry.py` 公共接口测试对应。
- 最终状态语义以文末“状态语义修正与规范键”为准。

## 方案 A 弱证据身份门禁（2026-08-10）

### 四项行为

- Windows 缺少 PNP 身份时，以规范化名称和显存容量生成可复现的 SHA-256 截断哈希，不再使用枚举序号。
- 弱证据设备明确写入 `identity_persistent=false`；具有完整 PNP 身份的设备写入 `identity_persistent=true`。
- 非持久身份即使携带 provider ordinal，也禁止建立物理 backend 绑定。
- 非持久身份禁止作为手动 override 目标，避免跨重启把偏好错误绑定到另一块物理设备。

### TDD 与验证

- RED：四项新增测试得到 `4 failed`，分别命中枚举序号 ID、缺少持久标志、弱身份仍可绑定、弱身份仍可 override。
- GREEN：规范化规则固定为大小写折叠、首尾裁剪与内部空白折叠，哈希输入为 `name|adapter_ram`；绑定与 override 统一检查持久标志。
- 定向测试：`5 passed`；Local AI 相关完整回归：`162 passed in 4.33s`。
- Ruff、compileall、diff check 通过；四个改动文件编辑器诊断均为 0 项。
- CodeRabbit CLI 0.7.2 审查命令退出码 0，输出仅含连接状态，未返回问题项。

## 方案 A 精确设备状态合同（已被后续语义修正替代，2026-08-10）

### 历史合同

- 本节记录曾采用的“任一不健康即 `DEGRADED`”语义，已被文末最新合同替代。
- 当前有效语义为任一 healthy 即 `AVAILABLE`，全部不健康才为 `DEGRADED`。
- 自动推荐只选择 healthy binding。
- 所有已登记 backend 均不可用且 provider 已消失时，设备继续登记为 `UNAVAILABLE`。

### TDD 与验证

- RED：先收紧 peer provider 失败与消失两个测试，得到 `2 failed, 62 deselected`；两项均观察到旧实现错误返回 `DeviceState.AVAILABLE`。
- GREEN：本节历史实现曾改为“任一 backend 不健康即 `DEGRADED`”；该行为已在后续 RED→GREEN 中纠正。
- Local AI 相关回归：`167 passed in 4.26s`。
- Ruff：`All checks passed!`；compileall 与 diff check 退出码 0。
- 编辑器诊断因工作区路径访问限制返回拒绝，未把该项计为通过证据。

## 状态语义修正与规范键（2026-08-10）

### 合同

- binding 规范键由 runtime、provider 与递归规范化的 provider options 组成，映射字段顺序不影响 binding 身份。
- 同一设备任一 backend 健康即为 `AVAILABLE`，全部 backend 不健康才为 `DEGRADED`。
- 自动推荐只选择健康 binding；健康 peer 可在同设备其它 binding 不健康时继续被推荐。

### TDD 与验证

- RED：规范键测试先在收集阶段因缺少 `_binding_key` 失败。
- GREEN：加入递归规范键并将 backend 注册和去重切换到规范键；定向及注册表完整测试转绿。
- 注册表完整测试：`71 passed in 4.04s`。
- Ruff、compileall 与 diff check 通过；编辑器诊断因工作区路径访问限制返回拒绝，未计为通过。
- Task 3 继续保持未完成。

## Backend 多 binding 查询遗漏修复（2026-08-10）

### 行为合同

- `backend(provider, device_id=None)` 先收集该 provider 的全部 binding。
- 零个 binding 抛出 `IncompatibleBackendError`；一个 binding 且未指定 `device_id` 时直接返回。
- 多个 binding 且未指定 `device_id` 时抛出包含 `ambiguous` 与可用 `device_id` 的错误。
- 指定 `device_id` 时只匹配 `options["device_id"]` 为严格 `int` 且值相等的 binding；字符串、布尔值、无匹配及多重匹配均抛出 `IncompatibleBackendError`。
- CUDA 双卡分别执行健康探测；自动推荐过滤不健康 binding 并选择健康卡。

### 严格 TDD 证据

- RED 定向切片共 8 项，首次有效运行得到 `6 failed, 2 passed`。
- 多 binding 无设备参数测试因旧实现直接返回首个 binding 而未抛错；指定设备测试因旧签名不接受 `device_id` 而失败。
- CUDA 双卡测试配置 0 号卡 Session 初始化失败、1 号卡成功，先因无法按设备查询 backend 而失败。
- 最小实现后同一定向切片得到 `8 passed`；CUDA 测试确认 0 号 binding 不健康、1 号 binding 健康，推荐结果为 `nvidia:GPU-1` 且 provider options 为 `{"device_id": 1}`。

### 验证

- 注册表完整测试：`79 passed in 1.01s`。
- Local AI 相关扩大回归：`182 passed in 4.53s`。
- Ruff：`All checks passed!`。
- Python compileall 与差异空白检查退出码 0。
- 编辑器诊断因工作区路径访问限制返回拒绝，未计为通过。
- 未提交任何改动。

## 可见设备校准与设计报告验证（2026-08-10）

### 合同

- NVIDIA 只设置一个可见设备变量时，将物理 index 或 UUID 规范化为物理 GPU UUID 序列，并按声明顺序生成运行时 provider ordinal。
- `CUDA_VISIBLE_DEVICES` 与 `NVIDIA_VISIBLE_DEVICES` 同时设置时，两条物理 GPU UUID 完整序列的长度、逐项身份和顺序必须完全一致；否则校准返回空映射，保留物理设备但不写 ordinal。
- 两个变量均未设置时保留 `nvidia-smi` 物理 index 语义。
- AMD 仅在完整 PCI BDF 批次及可见设备序列可校准时写入 provider ordinal；无法完整证明映射时不写入。

### 严格 TDD 证据

- RED：系统探测切片得到 `3 failed, 18 passed`，分别命中 AMD 未经完整 BDF 校准即写 ordinal、NVIDIA 未按可见顺序过滤校准、冲突双设置仍写 ordinal。
- GREEN：加入可见设备规范化、双设置一致性证明和 NVIDIA 运行时顺序校准；补充无设置物理 index 回归后，系统探测完整测试得到 `22 passed`。
- 最后确认 RED：新增完整序列专用接口与前缀、逆序、重复、未知标识、空成员边界，首次运行得到 `4 failed, 24 deselected`，失败原因均为 `_nvidia_runtime_ordinals` 尚不存在。
- 最后确认 GREEN：校准入口统一返回 UUID 到 ordinal 的映射，双变量使用 tuple 全长逐项有序相等；不一致或非法输入返回空映射。系统探测完整测试得到 `28 passed`。
- 报告验证先得到 `2 failed`，证明设计与两份实施报告缺少新合同及 RED/GREEN 证据；补齐文档后转绿。

## 终审 I1-I3 与 Minor 修复（2026-08-10）

### 严格 TDD

- NVIDIA 小写 UUID 测试先以缺少 `provider_ordinals` 失败；实现改为 token 先 `casefold()`，再校验规范化后的 `gpu-` 前缀，原测试转绿。
- Embedding 事务测试先观察到 tokenizer 失败后 `active_binding` 仍返回主 binding；测试同时约束未就绪状态、全部运行字段清空及重试 Session 不重复。
- 最小修复使用局部 `sessions`、`session`、`tokenizer`、`dimensions` 完成全部加载阶段，再提交到实例；任一后续异常将 `_session`、`_sessions`、`_tokenizer`、`_active_session_index`、`_dimensions` 和 `_loaded` 恢复为空状态。
- `active_binding` 在 provider 未 ready 时固定返回 `None`；失败后的第二次 `load()` 重新构造一次 Session，不追加首次失败事务的临时 Session。

### 文档与验证

- 更新 `memory/local_embed.py` 现有模块说明为 RuntimeProfile 多 binding 独立 Session 与按清单降级语义。
- 清理本报告及 SDD 报告中 AMD 永不写 ordinal 的旧陈述，改为完整 PCI BDF 与可见序列可校准时才写入；同步现有报告契约测试。
- `.superpowers/sdd/task-3-brief.md` 的五个执行步骤已勾选；`.superpowers/sdd/progress.md` 的 Task 3 仍保持 `[ ]`，未标记完成。
- 指定 Local AI 回归：`194 passed in 4.42s`；Ruff 返回 `All checks passed!`；compileall 与 `git diff --check` 退出码均为 0。
- 未提交任何改动。
