# Trace ID 设计文档

**日期**: 2026-07-08
**目标**: 添加 trace ID 串联日志和指标，实现端到端可观测性

## 现状

- `utils/logging_config.py` 已预留 `trace_id` extra 字段，但几乎未被使用
- `web/ws_hub.py` 仅一处手动 `logger.bind(trace_id=...)`
- `core/sla_exporter.py` 有 SLA 指标但无 trace_id 关联
- HTTP 中间件记录请求指标但未注入 trace_id

## 设计

### 数据源：contextvars

```python
# utils/trace_context.py
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
new_trace_id() -> str   # 生成短 ID: {ts_ms后6位}{rand_hex4}
get_trace_id() -> str   # 获取当前 trace_id
```

### 日志自动绑定：loguru patcher

在 `logging_config.py` 中添加 patcher，自动将 `trace_id_var` 注入每条日志的 extra，
无需每处手动 `logger.bind(trace_id=...)`。

### HTTP 中间件注入

在 `_allow_frame_embed` 中间件开头：
1. 生成 trace_id 并存入 contextvars
2. 响应头返回 `X-Trace-Id`
3. SLA 指标带 trace_id 标签

### WebSocket 复用

WebSocket 连接时复用 contextvars 机制，替代 ws_hub.py 中手动 bind。

## 文件变更

| 文件 | 操作 | 说明 |
|------|------|------|
| `utils/trace_context.py` | 新建 | contextvars + ID 生成 |
| `utils/logging_config.py` | 修改 | 添加 patcher 自动绑定 trace_id |
| `web/server.py` | 修改 | 中间件注入 trace_id + 响应头 |
| `web/ws_hub.py` | 修改 | 复用 contextvars |
| `core/sla_exporter.py` | 修改 | 指标支持 trace_id 标签 |