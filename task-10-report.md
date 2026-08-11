# Task 10 审查修复报告

## 修复范围

- 生成流程改为 `Generator.append_tokens(input_ids)` 后循环调用 `generate_next_token()`，不再依赖 `GeneratorParams.input_ids` 或 `compute_logits()`。
- 模型初始化改为 `Config(model_dir)`、`clear_providers()`、provider 配置、`Model(config)`。
- provider 映射限定为已核验的 ORT GenAI 名称：`CPUExecutionProvider` 使用清空后的 CPU 默认路径，`CUDAExecutionProvider` 映射为 `cuda`，`DmlExecutionProvider` 映射为 `dml`；其他 provider 返回结构化验证错误。
- `Config.set_provider_option(provider, name, value)` 使用 ORT GenAI 0.7 的三参数契约，并应用 `RuntimeProfile.device_id` 与 provider options。
- profile 顶层 provider options 与嵌套 mapping 均可应用，生成参数、模板和 fallback 控制字段不会误传给 provider。
- Model 已创建但 Tokenizer 创建失败时关闭局部 Model；Generator 创建后的 `append_tokens()`、`create_stream()`、生成和解码均处于统一清理范围。
- `stop()` 尝试关闭全部资源、清空运行时状态并聚合清理异常。
- PyInstaller 动态库收集按包隔离，单个可选包收集失败不会阻断 `onnxruntime_genai` 收集。
- Windows x64 与 Linux x86_64 发布矩阵统一安装 `.[local-ai]`。
- 发布构建通过 `pyi-archive_viewer` 检查真实冻结可执行文件中的 `onnxruntime_genai` 模块，并按平台强制检查 ORT GenAI 原生 DLL/SO。

## TDD 证据

- RED：严格替身切换到三参数 `set_provider_option()` 后，旧实现因缺少 provider 参数失败；新增发布契约因 workflow 未安装 local-ai extra、未检查冻结模块和原生库而失败。
- GREEN：实现 provider 映射、Config 配置和发布构建契约后，目标测试共 70 项通过。

## 验证结果

```text
.venv/bin/python -m pytest tests/test_local_ai_ort_genai.py tests/test_windows_package_reliability.py -q
70 passed in 1.10s
```

```text
.venv/bin/python -m ruff check local_ai/runtimes/ort_genai.py tests/test_local_ai_ort_genai.py tests/test_windows_package_reliability.py
All checks passed!
```

```text
.venv/bin/python -m compileall -q local_ai tests/test_local_ai_ort_genai.py tests/test_windows_package_reliability.py
exit 0
```

```text
git diff --check -- .github/workflows/build-release.yml local_ai/runtimes/ort_genai.py tests/test_local_ai_ort_genai.py tests/test_windows_package_reliability.py xiaoda-agent.spec pyproject.toml
exit 0
```

全仓 `git diff --check` 仍报告既有未提交的 `agent_dispatcher.py` 与 `cli.py` 行尾空格。Task10 范围检查通过；为保护已有改动，未修改这些无关文件。

## 工作区约束

- 未执行 commit 或 push。
- 未回退、覆盖或清洗无关未提交改动。
- 未新增代码注释。
