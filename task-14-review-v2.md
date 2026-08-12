# Task 14 二审：Complete Protocol Transports

**审查日期：** 2026-08-11  
**审查范围：** `.superpowers/sdd/task-14-brief.md`、`task-14-report.md`、`task-14-review.md`、`.superpowers/sdd/task-14-review-local-ai-v2.md`、`llm_gateway/transports/`、`web/custom_providers.py`、`tests/test_provider_transports.py` 及相关回归测试  
**Spec 结论：** PASS  
**质量结论：** PASS

## 结论摘要

一审的 3 个 Warning 均已修复并由针对性测试覆盖。远程 transport 的健康检查不再把连接失败或认证失败误判为可用；Anthropic 原生 SSE 与 Ollama 完成/流式响应均能归一化工具调用；HTTP transport 已建立统一异步关闭契约，并正确区分内部创建与外部注入 client 的所有权，Anthropic 兼容流在正常结束、异常和消费者提前终止时均通过 `finally` 清理临时 transport。

Task 14 brief 要求的五类统一 transport、完成与流式接口、工具归一化、模型发现回退、错误归一化和安全 custom mapping 均达到验收条件。二审未发现新的 Critical 或 Warning。

## Warning 复核

### 1. 上游不可达时 health_check 仍报告 available=True

**状态：已修复。**

- `OpenAICompatibleTransport`、`AnthropicTransport`、`OllamaTransport` 与 `CustomMappingTransport` 均以真实模型端点请求执行健康探测，不再复用会吞掉发现异常的 `discover_models()` 结果作为健康依据。
- 网络异常以及 401/403 会返回 `CapabilityReport(available=False, error="health check failed")`，不会用 configured model 掩盖连接或认证失败。
- 模型发现端点返回 404 时按“不支持发现”处理，provider 保持可用，并安全回退到 configured model；这保留了 brief 所需的 discovery fallback，同时区分了真正不可达。
- 定向测试覆盖离线、401、403、404，以及有 configured model 时的回退语义。

**证据：** `tests/test_provider_transports.py -k health`：`11 passed, 24 deselected`。

### 2. Anthropic 与 Ollama 工具调用没有完整归一化

**状态：已修复。**

- Anthropic SSE 按 content block index 跟踪 `content_block_start` 中的 `tool_use`，累积 `input_json_delta.partial_json`，在 `content_block_stop` 解析参数并产出统一 `ToolCall`。
- Anthropic 的 `message_delta.stop_reason=tool_use` 被统一为 `finish_reason="tool_calls"`。
- Ollama 完成与 NDJSON 流式响应均读取 `message.tool_calls`，通过共享解析器归一化工具名称、参数和结束原因。
- Anthropic 兼容流把统一 `ToolCall` 转换为 OpenAI 形状的 `delta.tool_calls`，工具调用不再在兼容层丢失。

**证据：** `tests/test_provider_transports.py -k 'tool_use_events or ollama_normalizes'`：`2 passed, 33 deselected`。

### 3. 内部 HTTP 客户端没有生命周期接口

**状态：已修复。**

- `ProviderTransport` 新增 `aclose()` 统一契约，并实现异步上下文管理器入口与退出清理。
- Anthropic、Ollama、Custom Mapping transport 通过 `_owns_http` 记录所有权：只关闭内部创建的 `httpx.AsyncClient`，不会误关调用方注入的 client。
- Anthropic 兼容流使用 `try/finally` 关闭临时 transport；消费者在首个分片后调用 `aclose()` 时也会执行清理。
- 定向测试覆盖三类 HTTP transport 的自建/注入 client 所有权，以及兼容流提前终止场景。

**证据：** `tests/test_provider_transports.py -k 'closes_only_owned or compat_stream_closes'`：`4 passed, 31 deselected`。

## Spec 核对

| 要求 | 结果 | 二审证据 |
|---|---|---|
| 五类 transport 与统一四接口 | PASS | 五个实现继承统一 `ProviderTransport`，并提供 complete、stream、discover_models、health_check |
| 共享文本流合同 | PASS | 五类 transport 的合同测试通过，文本拼接与最终 stop 语义成立 |
| 原生 Anthropic streaming | PASS | 文本 delta、工具 content block 与结束原因均完成归一化 |
| normalized streaming 与 tools | PASS | Anthropic、Ollama 定向工具响应测试通过；OpenAI 既有工具路径保持通过 |
| discovery fallback | PASS | 发现失败可回退 configured model；health check 独立区分不可达、认证失败与 404 不支持发现 |
| health_check -> CapabilityReport | PASS | 离线和 401/403 返回不可用，404 返回可用并回退配置模型 |
| errors 归一化 | PASS（验收范围） | 请求异常统一为不暴露上游正文的 `TransportError`；健康错误返回稳定通用消息 |
| safe custom field mapping | PASS | 仅允许受限 JSON path、数组索引、通配符与白名单 header 占位符，无任意执行 |
| HTTP client 生命周期 | PASS | 统一 `aclose()`、所有权保护和 compat 流 `finally` 清理均有测试 |
| 修改 web/custom_providers.py 支持 Anthropic 流 | PASS | 文本与工具流转换成立，临时 transport 可可靠释放 |
| 指定测试与相关回归 | PASS | 98 个相关测试全部通过 |
| Task 14 commit | 未执行 | 与报告及当前工作流一致，不作为实现缺陷 |

## 验证证据

- `.venv/bin/python -m pytest tests/test_provider_transports.py tests/test_custom_anthropic_provider.py tests/test_provider_catalog.py tests/test_local_ai_ort_genai.py -q`：`98 passed in 5.27s`。
- `.venv/bin/python -m pytest tests/test_provider_transports.py -q -k 'health'`：`11 passed, 24 deselected`。
- `.venv/bin/python -m pytest tests/test_provider_transports.py -q -k 'tool_use_events or ollama_normalizes'`：`2 passed, 33 deselected`。
- `.venv/bin/python -m pytest tests/test_provider_transports.py -q -k 'closes_only_owned or compat_stream_closes'`：`4 passed, 31 deselected`。
- `.venv/bin/python -m ruff check llm_gateway/transports tests/test_provider_transports.py web/custom_providers.py`：`All checks passed!`。
- `.venv/bin/python -m compileall -q llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py`：退出码 0。
- `git diff --check -- llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py task-14-report.md`：退出码 0。
- CodeRabbit CLI 0.7.2 已安装并认证；远程审查仅到达 connecting 阶段，没有返回发现项，因此不将其计作 PASS 证据。

## 最终判定

- **Spec：PASS。** 一审阻塞验收的健康检查语义和 Anthropic/Ollama 工具归一化缺口均已关闭；Task 14 brief 中的核心 transport 合同与协议行为具备实现和测试证据。
- **质量：PASS。** 一审 3 个 Warning 全部修复，相关资源所有权与提前终止清理已有回归保护；本次二审未发现新的 Critical 或 Warning。
