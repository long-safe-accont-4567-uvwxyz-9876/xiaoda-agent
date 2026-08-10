# 子代理隔离日志设计

## 目标

为结构化子代理调用补充可检索的安全与超时日志，使路径策略拒绝和调用超时能够定位到目标 Agent、请求和策略分支，同时避免把任务、上下文、工具内容或完整敏感路径写入日志。

## 日志事件

路径策略拒绝统一使用 `sub_agent.path_policy_denied`，日志级别为 `WARNING`。字段包括 `target`、`request_id`、`tool`、`reason`、清理并截断到 200 字符的 `path`、`allowed_pattern_count` 和 `forbidden_pattern_count`。`reason` 只能是 `missing_path`、`unsafe_path`、`forbidden_pattern` 或 `not_allowed`。

调用超时使用 `dispatcher.invocation_timeout`，日志级别为 `WARNING`。字段包括 `target`、`request_id`、`timeout_seconds` 和 `memory_scope`。不记录任务、背景上下文、系统提示或异常内部数据。

## 安全约束

- 路径日志移除换行、回车和 NUL，避免日志注入。
- 路径日志最多 200 字符。
- 不记录工具参数对象、文件内容、任务正文、上下文或人格提示。
- 日志字段名称固定，便于 Loguru sink 和结构化日志后端检索。

## 验收

- 缺失路径、不安全路径、禁止模式和未命中允许模式均发出对应 `reason` 的警告日志。
- 超时分支发出包含目标、请求 ID、超时值和 Scope 模式的警告日志。
- 日志中不包含任务正文、上下文和超出 200 字符的原始路径。
- 子代理契约与现有相关回归测试继续通过。
