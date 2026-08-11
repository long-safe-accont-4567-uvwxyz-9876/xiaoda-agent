# Local AI Platform 平台说明

> 面向运维与用户的本地 AI 平台文档。涵盖 Provider 接入、模型来源策略、存储行为、
> 断点恢复、算力设备证据与 Android 范围界定。

## 概览

Local AI Platform 在现有 Web UI 内提供统一的「本地部署」入口，包含设备探测、模型市场、
安装管理、运行时管理与下载任务。所有云端 / 本地 / 自定义 Provider 经统一接入流程管理，
凭证加密存储，绝不进入全局进程环境变量。

## 平台 Provider

平台支持以下 Provider 协议，统一在「模型接入」向导中完成接入：

- `openai`：OpenAI 兼容端点（siliconflow、modelscope、openrouter 等）。
- `anthropic`：Anthropic Messages 兼容端点。
- `ollama`：本地 Ollama 服务（仅允许回环地址 127.0.0.1:11434）。
- `custom-map`：自定义令牌映射端点，仅允许 `{api_key}` / `{base_url}` 占位符。

接入遵循「先测试、后保存」的原子流程；保存失败时自动补偿回滚，避免半成品状态。

## ModelScope 模型来源策略

模型市场以 ModelScope 为权威来源。每个清单条目必须携带**不可变 revision**
（拒绝 `main` / `master` / `latest`），并校验文件哈希与运行时清单：

- ORT GenAI 对话模型：`genai_config.json`。
- 标准 EMBEDDING / RERANKER：对应 ONNX 清单。

市场默认仅展示下载体积 ≤ 5 GiB 的精选模型；更大体积的自定义模型需高级确认。
市场模型**绝不捆绑**进发布产物。

## 存储行为

- 非捆绑模型必须由服务端选择目标目录，默认目录仅在用户勾选保存时持久化。
- 下载前校验路径可写与可用空间；未保存的默认目录不会被复用。
- 下载任务持久化到独立状态文件，重启后自动恢复。

## 断点恢复

下载任务支持断点续传（`pause` / `resume` / `cancel`），已下载的分片在恢复后继续，
`cancel` 可选择是否丢弃部分文件。服务重启后通过状态文件恢复未完成任务。

## 算力设备证据

设备探测基于运行时证据（`/proc/cpuinfo`、`device-tree/model`、VIP/NPU 探测驱动等），
不解析 UI 文案，也不臆造型号或算力。所有探索到的算力指标均带来源证据字段。
平台不写死任何固定设备型号或 TOPS 数值。

## Android 范围

当前发布产物覆盖 Windows x64、Linux x64 与 Linux ARM64；**不包含 Android 客户端**。
Android 仅提供契约（contracts），不提供完整安装路径。

## 本地模型

- Chat 使用 ONNX Runtime GenAI；Embedding / Reranker 使用标准 ONNX Runtime。
- 显式选中的本地实例不可用时抛出明确错误，**绝不静默回退到云端**。
- 未选择本地模型时保持既有云端路径，兼容性不受影响。