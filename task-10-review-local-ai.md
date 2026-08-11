# Task 10 本地 AI 代码审查

## 结论

**不通过。** Task 10 的目标是增加一个按 `RuntimeProfile`/manifest 驱动、懒加载可选依赖、支持取消与流式解码，并满足 PyInstaller 契约的 ORT GenAI 聊天运行时。当前实现的专用测试全部通过，但假模块固化了 ORT GenAI 0.6 之前的接口，因此没有发现真实 `onnxruntime-genai>=0.7.0` 上所有生成请求都会失败的阻断问题。另有 profile 后端未生效、发布包未安装可选依赖及异常路径资源泄漏问题。

```mermaid
flowchart TD
    A[调用 start(profile)] --> B{依赖可导入?}
    B -->|否| C[结构化 RuntimeDependencyError]
    B -->|是| D[直接 Model(model_dir)]
    D --> E[profile.provider/device 未应用]
    E --> F[stream 编码 prompt]
    F --> G[写 params.input_ids]
    G --> H[调用 compute_logits]
    H --> I[ORT GenAI 0.7+ 接口失败]
    F --> J[正确接口应创建 Generator 后 append_tokens]
    J --> K[generate_next_token]
    K --> L[TokenizerStream.decode]
    classDef ok fill:#c8e6c9,color:#1a5e20;
    classDef bad fill:#ffcdd2,color:#b71c1c;
    classDef warn fill:#fff3e0,color:#e65100;
    class C,J,K,L ok;
    class G,H,I bad;
    class D,E warn;
```

## 审查发现

| No. | 严重性 | 问题 | 建议 | 代码位置 |
|---:|:---:|---|---|---|
| 1 | P0 | 生成循环使用已被 0.6 移除的 API，与声明的 `onnxruntime-genai>=0.7.0` 不兼容 | 创建 `Generator` 后调用 `append_tokens(input_ids)`，删除 `params.input_ids` 和 `compute_logits()`；让 fake 精确模拟 0.7+ API，并增加禁止旧接口的契约测试 | [ort_genai.py:78-90](file:///home/orangepi/ai-agent/local_ai/runtimes/ort_genai.py#L78-L90)、[test_local_ai_ort_genai.py:49-78](file:///home/orangepi/ai-agent/tests/test_local_ai_ort_genai.py#L49-L78) |
| 2 | P1 | `RuntimeProfile` 的 provider/device 完全未参与模型构造，运行时并非 manifest 驱动 | 用 `genai_module.Config` 加载目录，按 profile 清空并设置 provider/provider options，再以 Config 构造 Model；为 CPU/CUDA/DML 及 device option 增加测试 | [ort_genai.py:26-45](file:///home/orangepi/ai-agent/local_ai/runtimes/ort_genai.py#L26-L45) |
| 3 | P1 | PyInstaller 配置声明收集 ORT GenAI，但发布构建从未安装 `local-ai` extra，最终包不会包含该运行时 | 在目标发布 job 显式安装受支持平台的 `.[local-ai]` 或 `onnxruntime-genai`，并在构建后验证模块及原生库确实进入产物；不要只做源码字符串断言 | [pyproject.toml:74-79](file:///home/orangepi/ai-agent/pyproject.toml#L74-L79)、[xiaoda-agent.spec:99-104](file:///home/orangepi/ai-agent/xiaoda-agent.spec#L99-L104)、[build-release.yml:128-132](file:///home/orangepi/ai-agent/.github/workflows/build-release.yml#L128-L132) |
| 4 | P1 | 初始化和流创建异常会泄漏原生资源 | 在局部变量上显式清理部分构造成功的资源，并把 generator 创建后的所有操作纳入同一个 `try/finally`；增加 tokenizer 构造失败及 `create_stream()` 失败测试 | [ort_genai.py:43-48](file:///home/orangepi/ai-agent/local_ai/runtimes/ort_genai.py#L43-L48)、[ort_genai.py:84-100](file:///home/orangepi/ai-agent/local_ai/runtimes/ort_genai.py#L84-L100) |

## 详细证据

### 1. P0：真实 ORT GenAI 0.7+ 无法生成

- `pyproject.toml` 声明 `onnxruntime-genai>=0.7.0`，但实现先写 `params.input_ids`，随后每轮调用 `generator.compute_logits()`。
- ORT GenAI 从 0.6 起的迁移契约要求：以 `generator.append_tokens(input_tokens)` 注入输入，并删除 `compute_logits()` 调用；0.7 官方示例同样使用 `append_tokens`。
- 当前 fake 为 `GeneratorParams` 人工提供可写 `input_ids`，并为 `Generator` 人工提供 `compute_logits()`，恰好复制了旧接口，使 13 个测试产生假阳性。
- 使用只暴露 0.7 公共接口的最小 fake 复现，`stream()` 在 `params.input_ids = input_ids` 立即抛出：`AttributeError: 'Params' object has no attribute 'input_ids'`。
- 影响：所有真实 `onnxruntime-genai>=0.7.0` 聊天请求都不能进入 token 解码阶段，属于功能阻断。

### 2. P1：profile 选择的后端不会生效

- `start()` 只验证 `profile.runtime`，然后直接执行 `Model(str(resolved_model_dir))`。
- `profile.provider`、`profile.device_id`、provider options 和 fallback 信息均未读取；设备注册表选出的 CPU/CUDA/DML 后端不会改变 ORT GenAI 实际加载配置。
- ORT GenAI 提供 `Config`、`clear_providers()`、`append_provider()` 和 `set_provider_option()` 用于在模型构造前设置执行后端。当前运行时只能服从模型目录中 `genai_config.json` 的固化配置，可能在用户选择 CPU 时仍尝试不可用 GPU，或反之。
- 当前“manifest 驱动”测试只覆盖 prompt 与采样参数，没有断言 profile 的 provider/device 被应用。

### 3. P1：源码打包声明与真实发布环境脱节

- spec 已调用 `collect_data_files`、`collect_submodules`、`collect_dynamic_libs` 并声明 hidden import，这部分方向正确。
- 这些收集调用全部吞掉异常；当构建环境没有安装 `onnxruntime_genai` 时，spec 会继续成功，但不会收集任何 ORT GenAI 文件。
- Windows 发布 job 仅安装 `requirements.txt` 和一组显式依赖；两者都不包含 `onnxruntime-genai`，也没有安装 `.[local-ai]`。
- 当前契约测试只搜索 spec/pyproject 中的字符串，无法证明 wheel 被安装、原生 DLL 被收集或冻结产物可导入该模块。
- 影响：源码环境可通过 extra 安装功能，但正式 PyInstaller 安装包仍固定返回依赖缺失错误，Task 10 的打包契约未闭环。

### 4. P1：清理仅覆盖部分成功路径

- `start()` 中 model 和 tokenizer 都是局部变量；若 `Model()` 成功而 `Tokenizer()` 失败，`self.stop()` 看不到尚未赋给 `self._model` 的 model，无法关闭它。最小复现结果为 `model_closed=False`。
- `stream()` 在创建 generator 后、进入 `try/finally` 前调用 `create_stream()`；该调用抛错时 generator 不会关闭。
- 原生模型和 generator 通常持有大量内存、执行 provider 状态或图缓存；反复失败重试会累积资源。
- 正常完成、取消及 decode 异常时的 generator 清理已有测试且行为正确，但未覆盖上述“部分初始化成功”窗口。

## 逐项核对

| 核对项 | 结果 | 说明 |
|---|:---:|---|
| 懒加载 | 通过 | `onnxruntime_genai` 仅在 `start()` 内导入，模块导入不要求可选 wheel。 |
| 结构化依赖错误 | 通过 | 缺失依赖时抛出稳定的 `RuntimeDependencyError`，包含 code、dependency、runtime、platform。 |
| Manifest 驱动 | 不通过 | prompt/采样默认项来自 profile，但决定执行位置的 provider/device 未应用。 |
| 取消 | 基本通过 | 每轮生成前后检查取消，取消及异常可进入 generator 清理；真实 0.7 API 阻断修复后需重新验证。 |
| 解码 | 条件通过 | `TokenizerStream.decode(token)` 的逐 token 方式正确，但当前真实生成循环到不了解码阶段。 |
| 清理 | 不通过 | 正常、取消、decode 异常路径正确；tokenizer 初始化失败和 tokenizer stream 创建失败会泄漏资源。 |
| 打包契约 | 不通过 | spec 收集项存在，但发布 job 不安装对应 wheel，且测试只验证文本存在。 |
| 代码质量 | 不通过 | Ruff/compileall 通过，但测试替身偏离最低支持版本的公共 API，掩盖 P0 集成错误。 |

## 验证记录

- `.venv/bin/python -m pytest tests/test_local_ai_ort_genai.py tests/test_windows_package_reliability.py -q`：`48 passed`。
- `.venv/bin/python -m ruff check local_ai/runtimes/ort_genai.py tests/test_local_ai_ort_genai.py`：通过。
- `.venv/bin/python -m compileall -q local_ai/runtimes/ort_genai.py tests/test_local_ai_ort_genai.py`：通过。
- `git diff --check -- ...`：通过。
- 当前虚拟环境导入 `onnxruntime_genai`：`ModuleNotFoundError`，与平台 marker 在 Linux ARM 上排除 wheel 的声明一致。
- 0.7 接口最小 fake 复现：`AttributeError: 'Params' object has no attribute 'input_ids'`。
- tokenizer 构造失败最小复现：已创建 model 的 `closed` 仍为 `False`。

## 测试缺口

- fake 应以最低支持版本 `0.7.0` 的公开 API 为准，必须实现 `Generator.append_tokens()`，且不应提供 `GeneratorParams.input_ids` 或 `Generator.compute_logits()`。
- 应断言 `RuntimeProfile.provider`、device option 及必要 fallback 配置实际进入 `Config`。
- 应覆盖 `Model` 成功但 `Tokenizer` 失败、generator 成功但 `create_stream` 失败的清理。
- 应在受支持平台执行冻结产物 smoke test，至少验证 `onnxruntime_genai` 可导入、原生库可加载；源码文本断言不足以验证打包。

## 审查范围

本次读取并核对了 `.superpowers/sdd/task-10-brief.md`、`.superpowers/sdd/task-10-report.md`、`local_ai/runtimes/ort_genai.py`、`tests/test_local_ai_ort_genai.py`、相关运行时与设备 profile 代码、`pyproject.toml`、`xiaoda-agent.spec`、Windows 打包可靠性测试及发布 workflow。除新增本报告外，未修改生产代码、测试或配置。
