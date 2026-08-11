# Task 14 实施报告

## 状态

完成。严格按 RED→GREEN 实施，未提交，未添加代码注释，未迁移或修改 `ModelRouter`。

## 实施范围

- 新增 `llm_gateway/transports/base.py`，定义统一的请求、完成结果、流式分片、工具调用、用量、能力报告、传输异常和抽象 transport 接口。
- 新增 `llm_gateway/transports/openai_compatible.py`，实现 OpenAI 兼容完成、流式、工具、用量、模型发现与发现失败回退。
- 新增 `llm_gateway/transports/anthropic.py`，实现 Anthropic Messages 原生完成与 SSE 流式协议、system 消息、工具历史、工具结果和工具选择转换。
- 新增 `llm_gateway/transports/ollama.py`，实现 Ollama `/api/chat` 与 `/api/tags` 原生协议。
- 新增 `llm_gateway/transports/custom_mapping.py`，实现声明式 JSON path 请求/响应/流式/模型映射和受限 header 模板。
- 新增 `llm_gateway/transports/local_ort.py`，将现有 `OrtGenAiChatRuntime.stream()` 适配为统一 transport 接口。
- 新增 `llm_gateway/transports/__init__.py`，导出统一 transport API。
- 修改 `web/custom_providers.py`，保留现有 OpenAI 形状兼容入口，并通过 `AnthropicTransport` 支持原生流式响应。
- 新增 `tests/test_provider_transports.py`，覆盖五类 transport 的共享合同与协议专项行为。

## TDD 证据

### RED 1

先创建 transport 合同和协议测试，再运行：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py -q
```

结果：测试收集失败，`ModuleNotFoundError: No module named 'llm_gateway.transports'`。失败原因与 Task 14 缺失 transport package 一致。

### GREEN 1

实现统一合同和五类 transport 后运行 Task 14 指定测试：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py tests/test_custom_anthropic_provider.py -q
```

结果：`26 passed in 2.36s`。

### RED 2

实现审查发现 custom mapping 默认 OpenAI 路径需要数组索引后，先补行为测试并单独运行：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py::test_custom_mapping_supports_array_indices_in_default_openai_paths -q
```

结果：`1 failed`，实际文本为空而预期为 `hello`，确认 `choices.0.message.content` 中的数组索引未被解析。

### GREEN 2

为安全 JSON path 语法增加非负整数段，并仅在列表边界内解析索引。重新运行同一测试结果为 `1 passed in 0.49s`。

## 验证摘要

最终运行：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py tests/test_custom_anthropic_provider.py tests/test_provider_catalog.py tests/test_local_ai_ort_genai.py -q
.venv/bin/python -m ruff check llm_gateway/transports tests/test_provider_transports.py web/custom_providers.py
.venv/bin/python -m compileall -q llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py
git diff --check -- llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py
```

- Transport、现有 Anthropic、provider catalog 与 ORT GenAI 回归：`89 passed in 5.64s`。
- Ruff：`All checks passed!`。
- Python 编译检查：退出码 0。
- 差异空白检查：退出码 0。

## 安全边界

- Custom mapping 只接受由对象键、数组索引和 `*` 组成的声明式 JSON path，不执行用户代码。
- JSON path 拒绝下划线开头的段，避免访问 Python 内部属性语义。
- Header 模板只允许 `{api_key}` 和 `{base_url}` 两个占位符，不使用 `eval`、`format` 或任意模板引擎。
- Custom endpoint 只接受以 `/` 开头且不包含协议分隔符的相对路径。
- Transport 对外统一抛出不包含上游敏感响应正文的 `TransportError`。

## 范围纪律

- 未修改 `model_router.py`，Task 16 的 ModelRouter 迁移保持未开始状态。
- 未执行 commit。
- 未添加代码注释。
- 工作树开始时已有大量不相关修改，本任务未回退或覆盖这些修改。
- 自动代码审查客户端可用，但远程审查在连接阶段超时，未将其计为通过证据；已执行本地行为测试、回归、Ruff、编译和差异检查。

## 审查 Warning 修复追加

### 核验结果

- 真实 health Warning 成立：远程 `discover_models()` 吞掉连接和认证异常后，基类会错误返回 `available=True`。
- Anthropic/Ollama 工具归一化 Warning 成立：Anthropic SSE 未处理 `tool_use` 与 `input_json_delta`，Ollama 完成和流式响应未读取 `message.tool_calls`。
- HTTP client 生命周期 Warning 成立：三个 HTTP transport 无统一关闭契约，Anthropic compat 临时流 transport 在正常结束、异常或提前终止时均无可靠清理。

### TDD RED

先新增真实上游健康、Anthropic/Ollama 工具响应、自建/注入 client 所有权和 compat 提前终止清理测试，再运行：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py -q
```

结果：`11 failed, 24 passed`。失败分别表现为离线及 401/403 仍为 `available=True`、工具调用集合为空、transport 缺少 `aclose()`、compat 流提前关闭后临时 transport 未关闭。

### TDD GREEN

- 远程 health 改为直接执行不吞错的模型端点探测；网络错误及 401/403 返回不可用，404 作为模型发现不受支持处理并保留配置模型回退。
- Anthropic SSE 按 content block index 累积 `input_json_delta.partial_json`，在 block 结束时产出统一 `ToolCall`；Ollama 完成和流式响应统一解析 `message.tool_calls`。
- `ProviderTransport` 增加 `aclose()` 和异步上下文管理契约；HTTP transport 记录 client 所有权，只关闭内部创建的 client。
- Anthropic compat 流通过 `finally` 关闭临时 transport，并把统一工具调用转换为 OpenAI 兼容 delta。

定向 GREEN 证据：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py -q -k 'health'
.venv/bin/python -m pytest tests/test_provider_transports.py -q -k 'tool_use_events or ollama_normalizes'
.venv/bin/python -m pytest tests/test_provider_transports.py -q -k 'closes_only_owned or compat_stream_closes'
```

- Health：`11 passed, 24 deselected`。
- 工具归一化：`2 passed, 33 deselected`。
- HTTP 生命周期：`4 passed, 31 deselected`。

### 修复后回归

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py tests/test_custom_anthropic_provider.py tests/test_provider_catalog.py tests/test_local_ai_ort_genai.py -q
.venv/bin/python -m ruff check llm_gateway/transports tests/test_provider_transports.py web/custom_providers.py
.venv/bin/python -m compileall -q llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py
git diff --check -- llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py task-14-report.md
```

- 回归：`98 passed in 4.55s`。
- Ruff：`All checks passed!`。
- Python 编译检查：退出码 0。
- 差异空白检查：退出码 0。
- 未执行 commit，未添加代码注释，未回退工作树中的既有修改。
