# 类型化本地 AI 资源要求设计

## 目标

设备推荐只使用有明确资源种类的 RAM/VRAM 要求，不把 CPU 内存、GPU 显存和未知加速器资源混为一个通用内存值。

## 契约

- Manifest 可使用 `minimum_ram`、`recommended_ram`、`minimum_vram`、`recommended_vram`。
- 四个字段必须是非负整数，布尔值、浮点数、负数和其它类型均拒绝。
- `recommended_ram` 小于 `minimum_ram` 时按最低 RAM 要求排序；VRAM 同理。
- 旧 `minimum_memory`、`min_memory`、`recommended_memory` 的零值可继续读取为未声明资源要求。
- 旧通用字段只要是正值就抛出 `InvalidResourceRequirementsError`，错误指出具体字段并要求改用类型化 RAM/VRAM，不根据 provider、设备类型或数值猜测含义。

## 推荐语义

- RAM 约束使用当前可用 CPU/主机内存证据。
- VRAM 约束只使用候选 GPU 或加速器自身的可用内存证据。
- CPU 设备的内存不得满足 VRAM 要求。
- 未知 ORT 加速器保持零 VRAM，因此不能满足正 VRAM 要求。
- 候选必须同时满足最低 RAM 和最低 VRAM；推荐值只影响候选排序。
- `RuntimeProfile` 分别返回 `estimated_ram` 和 `estimated_vram`，不再输出歧义的 `estimated_memory`。

## 设备状态合并

- 同一真实 GPU 可同时绑定多个 execution provider。
- binding 规范键由 runtime、provider 和递归规范化的 provider options 组成，映射键顺序不影响身份。
- 同一设备任一已登记 backend 健康时，设备为 `AVAILABLE`。
- 同一设备全部已登记 backend 均不健康时，设备为 `DEGRADED`。
- 自动推荐只选择健康 backend。
- 所有已绑定 backend 均不可用且 provider 已消失时，设备进入 `UNAVAILABLE`。

## 可见设备校准

- NVIDIA 的 provider ordinal 必须按运行时可见设备顺序校准，不能直接把物理 `nvidia-smi` index 当作受可见性设置影响后的 `device_id`。
- 只设置 `CUDA_VISIBLE_DEVICES` 或 `NVIDIA_VISIBLE_DEVICES` 时，仅接受完整 GPU UUID，并按声明顺序生成从零开始的 provider ordinal；数字索引、MIG 标识和特殊值不能证明稳定物理身份，因此不生成映射。
- 两者同时设置时，必须分别解析为物理 GPU UUID 完整序列；仅当两条完整序列的长度、逐项身份和顺序规范化后完全一致时才写入 provider ordinal。
- 任一设置无法解析、包含重复设备，或两条规范化完整序列不一致时，校准函数返回空映射；探测仍保留可证明的物理设备证据，但不写入 provider ordinal。
- 两者都未设置时，NVIDIA provider ordinal 使用 `nvidia-smi` 返回的物理 index。
- AMD 在未设置可见设备变量时，按 `rocm-smi` 的完整 PCI BDF 映射运行时 ordinal；设置 `HIP_VISIBLE_DEVICES` 或 `ROCR_VISIBLE_DEVICES` 时，仅接受完整 BDF 序列并按声明顺序重建映射。
- `HIP_VISIBLE_DEVICES` 与 `ROCR_VISIBLE_DEVICES` 同时存在时，规范化后的完整 BDF 序列必须完全一致；数字、UUID、空值、畸形 BDF、重复设备或冲突序列均不生成映射。

## 错误与测试

- Manifest 资源字段错误在候选筛选前报告，不能退化为笼统的“无兼容 backend”。
- 测试覆盖旧通用正值拒绝、旧零值兼容、非法类型拒绝、RAM/VRAM 独立校验、运行配置序列化、多 provider 状态合并、消失 provider 保留和可见设备校准。
