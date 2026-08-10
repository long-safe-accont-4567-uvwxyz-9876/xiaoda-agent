# 路线 A：资源后端与子代理隔离设计

## 目标

保留现有 FastAPI、自研 Agent 循环、人格、记忆和 Tool Search，通过轻量协议和显式 DTO 收紧资源访问与父子代理状态边界，不引入 LangChain 或 LangGraph 运行时。

## 组件边界

`ResourceBackend` 是 Agent 可访问资源的最小能力协议，定义 `read`、`write`、`edit`、`glob` 和 `grep`。协议不暴露宿主机路径，不包含 Shell 执行，也不绑定具体存储实现。本阶段交付接口及运行时协议检查；具体工作区、持久记忆和产物 Adapter 与组合路由属于后续实施，不宣称现有文件工具已迁移到该协议。

`SubAgentInvocation` 是父代理到子代理的显式结构化输入契约。它只包含目标、任务、必要背景、工具白名单、虚拟路径规则、权限模式、超时和请求标识。主会话消息、待办、审批状态、内部记忆对象和中间件私有状态不属于 DTO，因此不能被隐式传播。现有字符串 `dispatch()` 作为兼容入口保留，新增隔离策略的调用使用 `dispatch_invocation()`。

`SubAgentInvocationResult` 是隔离执行的结构化结果契约。它只公开最终报告、执行状态、稳定错误信息和耗时，不公开中间工具结果或子代理内部消息。兼容期内现有 `dispatch()` 继续返回 `str | None`，结构化结果由后续适配层逐步接入 Web、工具和编排器。

结构化调用采用 fail-closed 策略：空 `allowed_tools` 表示不允许任何工具；非空白名单同时约束模型可见工具和执行入口；带路径规则的资源工具缺少可识别路径时直接拒绝；`strict` 模式只允许只读工具。旧 `dispatch()` 不携带结构化策略，继续维持既有行为。

## 数据流

```text
Web / CLI / delegate_task / orchestrator
                  │
                  ▼
        SubAgentInvocation 构造与校验
        ├─ 标识符、任务和上下文
        ├─ 工具与虚拟路径集合
        ├─ 权限模式与有限超时
        └─ 请求标识
                  │
                  ▼
    AgentDispatcher.dispatch_invocation
        ├─ 校验发生在 Agent 查找之前
        ├─ 只传递 DTO 中的任务与背景
        ├─ 绑定 shared/isolated 记忆 Scope
        └─ 未注册 Agent 返回 unavailable
                  │
                  ▼
              SubAgent.chat
        ├─ 模型与工具循环
        ├─ 工具、路径和权限策略
        └─ 私有运行状态不回传
                  │
                  ▼
        SubAgentInvocationResult
        └─ 最终报告或稳定失败状态
                  │
                  ▼
     str / ProcessResult / ToolResult 兼容适配
```

## 校验规则

- `target` 必须是 1 至 64 位小写 Python 标识符，与现有 Agent Registry 的 Unicode 自定义名称兼容。
- `task` 去除首尾空白后必须非空，最多 100,000 个字符。
- `context` 最多 200,000 个字符。
- 工具和路径集合必须是有序字符串序列，拒绝字符串、集合等会产生歧义或不稳定顺序的输入，最多各 256 项，稳定去重。
- 工具名称只能包含字母、数字、下划线、点、冒号和连字符。
- 路径规则必须是虚拟相对模式，拒绝 NUL、反斜杠、`~`、宿主绝对路径和独立的 `..` 路径段。
- `permission_mode` 只能是 `default`、`dev` 或 `strict`。
- 超时必须是有限正数，且不超过 600 秒。
- DTO 使用冻结数据类，构造后不能被调用链原位篡改。

## 错误处理

DTO 校验失败抛出 `InvalidSubAgentInvocation`，包含稳定的 `field` 和 `reason`，便于 HTTP、WebSocket 和工具 Adapter 映射错误，但不携带密钥、模型连接配置或内部消息。

旧 `dispatch()` 在目标 Agent 未注册时仍返回 `None`，保持现有调用兼容。`dispatch_invocation()` 返回 `unavailable`，并通过 `asyncio.wait_for` 把调用级超时映射为 `timeout`。结构化结果预留 `cancelled` 和 `failed` 状态，最终报告仅在 `completed` 状态存在。

资源层后续采用稳定领域错误：访问越界映射 `ResourceAccessDeniedError`，资源缺失映射 `ResourceNotFoundError`，编辑冲突映射 `ResourceConflictError`。领域错误不直接依赖 FastAPI 异常。

## 测试验收

- 协议：结构化 Fake Backend 满足运行时 `ResourceBackend` 检查，缺失任一方法的对象不满足协议。
- DTO 正向：输入清理、稳定去重、冻结行为和最终报告构造正确。
- DTO 反向：非法目标、空任务、零值或无限超时、字符串伪列表、路径逃逸、绝对路径和 NUL 全部稳定失败并指出字段。
- 隔离：DTO 字段中不存在 `messages`、`todos`、`approval_state` 和 `memory_state`。
- 执行隔离：结构化入口同时限制模型可见工具与执行工具，拒绝嵌套 Agent 通信、宿主绝对路径、`~`、父级穿越和缺失路径。
- Scope：`shared` 保留父 Agent/Session，`isolated` 切换到子 Agent/Session；两者都使用调用级 request ID 并在结束后恢复父 Scope。
- 调度：无效任务在 Agent 查找或模型调用前失败；合法旧签名保持原行为。
- 回归：子代理路径白名单、事件、超时、并行调度和 WebUI 子代理测试通过。
- 静态质量：新增模块无诊断，Ruff 检查修改文件通过，不新增第三方依赖。

## 非目标

- 不整体迁移 LangChain、LangGraph 或 Deep Agents Runtime。
- 不替换现有认知记忆、人格子代理、Tool Search 或多通道产品层。
- 本阶段不提供 Shell Backend，也不让文件路径权限对 Shell 执行作虚假安全承诺。
- 本阶段不一次性改变所有调用方的返回类型。
