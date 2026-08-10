# Local Embedding Session Fallback 设计报告

## 决策

采用方案 A：当前调用在 active Session 推理失败后，立即按 manifest 的 `fallback_bindings` 顺序重试下一 Session；第一个成功的 binding 提升为 active binding，后续调用直接使用它。

运行时降级的唯一权威是 `RuntimeProfile` 的主 binding 与 `options.fallback_bindings`。旧 `providers`、`provider_options`、`fallback_providers` 不参与适配器降级决策。

## 根因

旧实现把主 binding 和多个 fallback binding 转换成一个 ORT Session 的 `providers` 与 `provider_options`。该结构只能描述一个 Session 内的 Execution Provider 注册顺序，不能可靠表达同一 provider 的多设备 Session，也不能在一次 `session.run()` 失败后创建明确的逐 binding 重试边界。

因此旧行为存在五个缺口：

1. 多设备 binding 没有独立 Session 隔离。
2. 当前调用失败后不会立即重试下一 Session。
3. fallback 成功后不会提升 active binding。
4. 旧 provider 链字段可绕过 manifest fallback 权威。
5. Session 创建时未在适配器边界确认目标 provider 实际激活。

## 组件边界

`DeviceRegistry` 继续负责选择主 binding，并按 manifest provider 顺序生成经过健康、兼容、平台和资源门禁的 `fallback_bindings`。

`LocalEmbeddingProvider.from_runtime_profile()` 只负责把主 binding 与 manifest fallback bindings 转换为有序 binding 列表，不重新发明设备选择策略。

`LocalEmbeddingProvider.load()` 为每个 binding 创建独立 ORT Session，保留成功 Session，隔离单个创建失败；若没有任何 Session 可用，加载整体失败。

`LocalEmbeddingProvider.encode_batch()` 从 active Session 开始执行。失败时在当前调用内继续尝试后续 Session；成功后更新 active Session 索引。

## 数据流

1. 注册表输出主 `provider/device_id/options` 与有序 `fallback_bindings`。
2. 适配器建立一组顺序稳定的独立 Session。
3. 推理从 active Session 开始。
4. 失败后立即尝试下一 Session。
5. 成功 Session 成为新的 active binding。
6. 后续推理直接从新的 active Session 开始。

降级只向列表后方推进，不自动回切已失败 binding，避免每次调用重复支付确定失败成本。设备恢复与主动回切属于后续实例生命周期管理范围。

## 错误处理

- 单 binding 创建失败：记录 binding 级失败并继续创建下一 manifest binding。
- provider 静默回退：目标 provider 未出现在 Session active providers 中时，该 binding 视为创建失败。
- 推理失败：记录 binding 级失败并在当前调用内重试下一 Session。
- 全部 Session 推理失败：保持既有适配器契约，返回空结果，由上层执行既有远程或不可用处理。
- 非 manifest provider 字段：不参与 fallback，防止未审核的跨设备或跨 provider 降级。

## 验证设计

- 独立 Session：断言每个 binding 各创建一个 Session，provider_options 与设备严格对齐。
- 当前调用重试：主 Session 抛错时，同一 `embed()` 调用由下一 Session 返回结果。
- active 提升：第二次调用不再访问已失败主 Session。
- manifest 权威：只有旧 providers/provider_options/fallback_providers 时不创建 fallback Session。
- 回归：覆盖设备注册表、系统探测、NPU 适配、本地 Embedding 与 Local AI 契约测试。
