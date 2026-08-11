# Task 14 审查：Complete Protocol Transports

**审查日期：** 2026-08-11  
**审查范围：** `.superpowers/sdd/task-14-brief.md`、`task-14-report.md`、`.superpowers/sdd/task-14-review-local-ai.md`、`llm_gateway/transports/`、`web/custom_providers.py`、`tests/test_provider_transports.py` 及相关回归测试  
**Spec 结论：** FAIL（五类统一接口和文本流式合同已落地，但 health check、流式工具归一化和部分协议错误处理不符合完整 transport 语义）  
**质量结论：** FAIL（没有 Critical，但存在 3 个会导致错误可用性判断、工具调用丢失或长期资源泄漏的 Warning）

## 结论摘要

实现完成了 Task 14 的主要文件与接口交付：五类 transport 均提供 `complete()`、`stream()`、`discover_models()` 和 `health_check()`；OpenAI 完成响应可归一化工具与 usage；Anthropic 已支持原生 SSE 文本流；Ollama 和 Local ORT 的文本流合同成立；custom mapping 使用受限 JSON path 与 header 占位符，没有任意代码执行。相关 89 个测试、Ruff 和差异空白检查均通过。

但是当前实现尚不能通过验收。远程 transport 把模型发现失败无条件降级为空结果或默认模型，随后基类 `health_check()` 仍报告 `available=True`，上游完全不可达也会被判定健康。与此同时，Anthropic 原生流忽略 `tool_use` / `input_json_delta` 事件，Ollama 完成和流式响应也没有读取 `message.tool_calls`，与 brief 明示的“normalized streaming, tools”不符。内部创建的持久 `httpx.AsyncClient` 没有关闭接口，`AnthropicCompatClient` 每次开启流还会新建一个无法释放的 transport/client。

## 发现项

### [Warning] 上游不可达时 health_check 仍报告 available=True

**位置：** `llm_gateway/transports/base.py:84-92`、`llm_gateway/transports/anthropic.py:120-127`、`llm_gateway/transports/ollama.py:79-86`、`llm_gateway/transports/custom_mapping.py:157-165`、`llm_gateway/transports/openai_compatible.py:66-72`

各远程 transport 的 `discover_models()` 捕获所有异常并返回 `super().discover_models()`；没有 `default_model` 时该结果为空 tuple，有默认模型时则只是配置值。异常已被吞掉，因此基类 `health_check()` 看不到 `TransportError`，无条件返回 `CapabilityReport(available=True, ...)`。

定向复现中，给 Anthropic 和 Ollama 注入一个所有 `get()` 都抛出 `RuntimeError("offline")` 的客户端，两者均返回：

```text
AnthropicTransport True () None
OllamaTransport True () None
```

这会直接误导 Task 15 的 `ProviderService.test(draft) -> CapabilityReport`：不可连接、认证失败或 endpoint 错误的 provider 可能通过连接测试并被启用。配置默认模型也不能证明服务可用，因此不能用 discovery fallback 代替健康探测。

建议区分“模型发现不受支持”和“连接/认证失败”。`discover_models()` 可以保留回退语义，但 `health_check()` 必须执行不会被吞错的真实探测，或让 discovery 同时保留失败状态；至少覆盖网络错误、401/403、404/不支持模型发现和有/无默认模型四类场景。

### [Warning] Anthropic 与 Ollama 的工具调用没有完整归一化，流式工具调用会静默丢失

**位置：** `llm_gateway/transports/anthropic.py:105-118`、`llm_gateway/transports/ollama.py:48-77`、`llm_gateway/transports/custom_mapping.py:123-155`、`tests/test_provider_transports.py:191-227,241-254`

Anthropic SSE 解析器只处理 `text_delta` 和 `message_delta`，忽略原生工具流所需的 `content_block_start`（`tool_use`）与 `content_block_delta`（`input_json_delta`），因此 `CompletionChunk.tool_calls` 永远为空。若模型只请求工具，调用方最终只收到 finish reason，没有工具 ID、名称或参数。

Ollama 请求端虽然发送 `tools`，但完成和流式响应都只读取 `message.content`，没有解析 Ollama `message.tool_calls`。Custom mapping 的完成和流式映射也没有任何 tool call 或 usage 字段入口。结果是接口表面包含 `Completion.tool_calls` / `CompletionChunk.tool_calls`，实际只有 OpenAI 非流式和 Anthropic 非流式能可靠产出工具调用。

现有共享合同只断言文本拼接和最终 finish reason；专项工具测试也只覆盖 OpenAI 非流式与 Anthropic 请求转换，没有覆盖任一协议的流式工具响应或 Ollama 工具响应，因此未能发现该缺口。

建议定义流式工具分片的合并语义，包括 call index/id/name、增量 arguments 和结束原因；为 Anthropic、OpenAI 与 Ollama 分别添加真实协议形状测试。若 Custom mapping 被要求支持 tools，应增加受限声明式 tool call/usage 路径；若某协议明确不支持，则 capability 必须准确报告 `tools=False`，不能接受并静默丢弃。

### [Warning] 内部 HTTP 客户端没有生命周期接口，流式兼容路径会持续泄漏连接池

**位置：** `llm_gateway/transports/anthropic.py:21-35`、`llm_gateway/transports/ollama.py:19-32`、`llm_gateway/transports/custom_mapping.py:23-43`、`web/custom_providers.py:118-146`

三个 HTTP transport 在未注入客户端时各自创建持久 `httpx.AsyncClient`，但 `ProviderTransport` 没有 `close()` / `aclose()` 或 async context manager 契约，具体实现也没有记录客户端所有权并释放资源。调用方无法安全关闭连接池。

问题在 Anthropic 兼容流式路径更明显：每次 `_create(stream=True)` 都新建 `AnthropicTransport`，异步生成器结束后没有关闭其内部 client。频繁聊天、取消流或异常退出会累积未关闭连接与文件描述符，并可能触发 `ResourceWarning` 或最终耗尽连接资源。

建议给 transport 增加统一异步关闭契约，仅关闭自身创建的客户端、不关闭外部注入客户端；流式生成器需在正常完成、异常和调用方提前 `aclose()` 时都进入清理路径。`AnthropicCompatClient` 应复用一个受控 transport，或在生成器 `finally` 中关闭临时实例，并增加提前中断流的资源释放测试。

## Spec 核对

| 要求 | 结果 | 证据 |
|---|---|---|
| 五类 transport 与统一四接口 | PASS（接口形状） | `llm_gateway/transports/base.py:66-92`；五个实现均继承统一基类 |
| 共享文本流合同 | PASS | 五类 transport 均拼接为 `hello` 且末片为 `stop`；专项测试通过 |
| 原生 Anthropic streaming | PASS（纯文本） | `anthropic.py:105-118`；原生 SSE 文本测试通过 |
| normalized streaming | FAIL（工具流） | Anthropic 忽略工具 SSE，Ollama 不解析流式 `message.tool_calls` |
| tools | FAIL（跨 transport） | OpenAI/Anthropic 非流式部分实现；Anthropic/Ollama 流式及 Ollama 非流式不完整 |
| discovery fallback | PASS（返回值） | 远程 transport 均能退回 configured model/空 tuple |
| health_check -> CapabilityReport | FAIL（语义） | discovery 异常被吞后仍返回 `available=True`；离线复现稳定 |
| errors 归一化 | PARTIAL | 请求异常转为不泄露正文的 `TransportError`；协议内流式 error event 未覆盖 |
| safe custom field mapping | PASS（当前声明式边界） | JSON path/header 模板受白名单限制，无 eval/format/任意模板执行 |
| 修改 `web/custom_providers.py` 支持 Anthropic 流 | PASS（功能），FAIL（资源生命周期） | 文本流兼容测试通过；每次流新建 client 且无关闭路径 |
| 指定测试通过 | PASS | 任务指定测试及相关回归共 89 个通过 |
| Task 14 commit | 未执行 | 报告明确按用户工作流保持未提交；不作为实现质量缺陷 |

## 测试评价

现有 23 个 transport 专项测试对统一接口、五类纯文本完成/流式、OpenAI 工具与 usage、Anthropic 请求转换、发现回退、custom path/header 安全和错误正文隐藏提供了良好基础。加上既有 Anthropic、provider catalog 和 ORT GenAI 回归，主 happy path 的证据充分。

关键缺口如下：

- 远程不可达、认证失败和模型发现不受支持时的 `CapabilityReport.available` 语义。
- OpenAI 分片 arguments、Anthropic `tool_use`/`input_json_delta`、Ollama `message.tool_calls` 的完成与流式归一化。
- SSE/NDJSON 中的协议级 error event、畸形事件、空 choices/content 和流中断行为。
- 自建与外部注入 HTTP client 的所有权、正常关闭、异常关闭和提前取消流。
- Custom mapping 的嵌套 request path 冲突、负数组索引拒绝、tool/usage 映射能力和模型列表非数组返回。

## 验证证据

- `.venv/bin/python -m pytest tests/test_provider_transports.py tests/test_custom_anthropic_provider.py tests/test_provider_catalog.py tests/test_local_ai_ort_genai.py -q`：**89 passed in 3.95s**。
- `.venv/bin/python -m ruff check llm_gateway/transports tests/test_provider_transports.py web/custom_providers.py`：**All checks passed**。
- `git diff --check -- llm_gateway/transports web/custom_providers.py tests/test_provider_transports.py`：通过。
- 离线 health check 定向复现：Anthropic 与 Ollama 均返回 **`available=True, models=(), error=None`**。
- CodeRabbit CLI 0.7.2 已安装并认证；本次 verdict 以本地静态审查、专项测试和定向运行时复现为依据。

## 最终判定

- **Spec：FAIL。** 五类 transport 的统一形状、纯文本完成/流式、发现回退和安全 custom mapping 已基本完成，但 health check 会把不可达上游判为健康，且“normalized streaming, tools”在 Anthropic/Ollama 工具响应上未完成，属于验收核心行为缺口。
- **质量：FAIL。** 没有发现 Critical；3 个 Warning 分别影响 provider 上线判断、工具调用正确性和长期连接资源稳定性。建议修复后补齐定向合同测试再复审。
