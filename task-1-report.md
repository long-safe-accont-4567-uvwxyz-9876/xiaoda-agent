# Task 1 实施报告：聊天请求快照与可靠发送

## 状态

- 完成。
- 未提交任何 Git 变更。
- 未回退、覆盖或重写进入任务前已有的未提交改动。

## 实施结果

- 已确认共享接口 `ChatAttachmentSnapshot`、`ChatRequestSnapshot`、`ChatSendResult`、`sendMessage(request)` 和 `retryMessage(messageId)` 存在并符合简报。
- 已确认聊天 Store 在 WebSocket 交付成功前不会追加用户消息或进入处理中状态，空请求、处理中和断线均返回结构化失败。
- 已确认消息保留完整请求快照，重试通过原请求快照发送，不再从展示文本重建附件和模式。
- 已确认输入组件支持纯附件发送、上传中禁发、上传失败与不支持类型反馈，并且只发出 `ChatRequestSnapshot`。
- 已确认父页面仅在发送成功后清空文本、附件和模式；断线失败保留草稿并展示警告。
- 已确认 WebSocket 客户端提供显式 `retry(): boolean`，界面提供可访问的状态区域和重连动作。
- 已确认中英文文案覆盖上传中、不支持文件、上传失败、移除文档、草稿保留、重新连接和重连中状态。

## 既有改动保护

任务开始时，简报列出的所有目标源文件及两份契约测试已经包含未提交改动，且实现内容与本任务要求高度吻合。为遵守“不得覆盖既有改动”，本次没有回退这些文件以人为重现测试失败，也没有重新写入已满足要求的实现。

因此，简报中的原始 RED 阶段无法在当前工作区中安全重现；当前契约测试首次执行即通过。本报告如实记录该限制，不将既有实现冒充为本次新增实现。

## 测试摘要

### 聊天契约测试

命令：

```bash
.venv/bin/python -m pytest tests/test_frontend_runtime_contracts.py tests/test_webui_chat_experience_contracts.py -q
```

结果：退出码 0，16 项通过，0 项失败，耗时 0.52 秒。

### 前端类型检查

命令：

```bash
cd web/frontend && npx vue-tsc --noEmit
```

结果：退出码 0，无类型错误输出。

## 文件状态

- 本次按要求更新：`task-1-report.md`。
- 任务目标源文件和契约测试：保留进入任务前已有的未提交内容，未做覆盖性修改。
- Git 提交：无。

## 复审窄改修复（2026-08-11）

### 根因与修复

- 纯附件：前端已允许纯图片或纯文档发送，但 `_handle_chat` 在解析 `image_url` 与 `doc_path` 前遇到空文本即返回。现改为仅在文本和附件字段都为空时返回，并为纯图片、纯文档生成处理占位文本。
- Workflow 签名：`sendMessage` 已改为接收 `ChatRequestSnapshot`，Workflow 预览仍传入字符串。现构造完整快照，模式关闭且附件为空。
- TXT/MD 错配：上传端与前端声明支持 `.txt`、`.md`，真实 `document_reader` 未注册对应读取器。现复用 UTF-8 BOM 兼容文本读取路径，并同步工具描述。

### 真实 RED 证据

- 纯附件：`tests/test_transport_user_binding.py::test_web_adapter_processes_attachment_only_messages` 修复前为 `2 failed`，两种附件均未调用 `core.process`。
- Workflow：`tests/test_webui_chat_experience_contracts.py::test_workflow_preview_uses_chat_request_snapshot_signature` 修复前为 `1 failed`，预览仍使用字符串签名。
- TXT/MD：`tests/test_document_tools_text_formats.py` 修复前为 `2 failed`，分别返回不支持 `.txt` 与 `.md`。

### 最终验证

```bash
.venv/bin/python -m pytest tests/test_frontend_runtime_contracts.py tests/test_webui_chat_experience_contracts.py tests/test_transport_user_binding.py tests/test_document_tools_text_formats.py -q
```

结果：`24 passed in 4.19s`。

```bash
.venv/bin/python -m ruff check web/ws_hub.py tools/document_tools.py tests/test_transport_user_binding.py tests/test_document_tools_text_formats.py tests/test_webui_chat_experience_contracts.py
cd web/frontend && npx vue-tsc --noEmit
.venv/bin/python -m py_compile web/ws_hub.py tools/document_tools.py tests/test_transport_user_binding.py tests/test_document_tools_text_formats.py tests/test_webui_chat_experience_contracts.py
git diff --check -- web/ws_hub.py tools/document_tools.py web/frontend/src/views/WorkflowView.vue tests/test_transport_user_binding.py tests/test_document_tools_text_formats.py tests/test_webui_chat_experience_contracts.py task-1-report.md
```

结果：全部退出码 0。

本轮未执行 `git add`、`git commit` 或 `git push`。但最终状态核对时发现并行进程已将 HEAD 从任务开始时的 `dc8ecb7` 推进到 `169e84c`，且该并行提交已包含本轮三处生产修复和新增测试。为避免破坏同一提交中的其他并行成果，本轮未执行 reset 或改写历史；当前报告修改仍保持未提交。
