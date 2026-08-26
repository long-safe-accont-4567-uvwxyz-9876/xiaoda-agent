# AstrBot/TDBM Companion Upgrade 核验记录

> 核验日期：2026-08-23  
> 本项目固定点：`eba85d91e0bf9f6cbd831cdad2dadcf6a53fe190`  
> 结论：外部借鉴方向成立，但原 v1.0 不能直接实施；P0-2、P0-3、P0-4 均存在必须先修正的仓库契约。

## 第一手来源固定点

- AstrBot：`19d00fb1f0d822690a467e8dca498adebbb2d67b`，官方仓库 <https://github.com/AstrBotDevs/AstrBot/commit/19d00fb1f0d822690a467e8dca498adebbb2d67b>。
- TencentDB-Agent-Memory：`97f94654280b2932c35ba4806a491999ed244cc9`，官方仓库 <https://github.com/TencentCloudADP/tencentdb-agent-memory/commit/97f94654280b2932c35ba4806a491999ed244cc9>。本机固定点是 `v2.0.1-beta.2` 后 1 个提交，不是 v1.0 文档写的 beta.1。
- QQ/NapCat：本次不 clone NapCatQQ，也不把其源码作为实现或验收门禁。QQ 能力边界以项目实际使用的 botpy SDK 和官方回调为准。

## 已核实的 AstrBot 契约

- `PlatformMetadata.support_streaming_message` 与 adapter 注册能力位存在：`astrbot/core/platform/platform_metadata.py`、`astrbot/core/platform/register.py`。
- 工具前 `break` 信号存在：`astrbot/core/astr_agent_run_util.py` 在固定点产出 `MessageChain(type="break")`。
- group context、概率回复和 rate-limit stage 存在：`astrbot/builtin_stars/astrbot/group_chat_context.py`、`astrbot/core/pipeline/rate_limit_check/stage.py`。
- 这些模式只能作为接口设计参考；本项目不能直接删除 `not tools`，因为当前 `ModelRouter.chat_stream()` 把 provider chunk 降维成文本，结构化 tool calls 会丢失。

## 已核实的 TDBM 契约

- 官方 README 明确 L0 Conversation -> L1 Atom -> L2 Scenario -> L3 Persona 分层。
- `MemoryCore` 中存在 `maxScenes`、persona.md、source message IDs 和四动作 dedup 相关实现。
- 四动作适合借鉴写入侧协议，但其团队资产模型、部署形态和情感提取偏好不能直接照搬。

## 仓库实施前阻断项

1. `core/conflict_supersession.py::apply_supersession()` 明确是 stub，不修改数据库；其检测也只覆盖高相似度数值 token 差异，不能承担 P0-2。
2. `llm_gateway/router_execution.py::chat_stream()` 当前只 yield 文本；P0-3 必须先建立结构化 stream event/turn result，再接 verification 和前端。
3. 官方 QQ 群回调只有 `on_group_at_message_create`，只能观察 @bot 消息；P0-4 不得把“非 @ 群消息”写进验收标准。
4. 当前业务已经实现“主人群聊当前轮可进入个人记忆，非主人不进入”。P0-4 必须保持成员级个人 context，仅把群 buffer 作为独立易失提示注入。
5. `eba85d91` 的 adapter 模板重构缺少契约测试，且存在无类型 `extra_kwargs`、微信锁清理顺序漂移、session 异常边界扩大、paced helper 仅接微信且不兼容 QQ TimeoutError 语义。必须先修地基。

## 实施与发布门禁

- 所有 schema 迁移只增不删，不调用 LLM，不覆盖生产数据；生产库本轮只做只读 readiness 检查。
- P0-1 的 CI 只验证严格 parser、状态机和失败回退；模型分类质量使用固定 golden dataset 离线评估。
- P0-2 默认 shadow，不改变检索可见性；enforce 按 store/skip、update、merge 分阶段启用。
- P0-3/P0-4 默认关闭，旧字符串流、旧 WS 事件和现有群聊行为至少保留一个发布周期。
- 前端修改必须通过类型检查、测试、production build，并刷新 `web/dist`。
- 不部署、不重启服务、不修改 `.env`；提交代码需要单独授权。
