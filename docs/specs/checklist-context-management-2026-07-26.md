# 上下文管理优化验证清单（checklist.md）

> 配套 spec：`spec-context-management-2026-07-26.md`
> 配套 tasks：`tasks-context-management-2026-07-26.md`
> 执行原则：**evidence before assertions**——所有"已修复"声明必须有日志/DB/测试输出证据

---

## 一、阶段一验证（P0 截断重试 + 图片识别）

### Task 1.1：截断重试去递归化
- [ ] 单元测试 `tests/test_model_router_truncation.py::test_no_recursion` 通过
  - 模拟连续 `finish_reason="length"`
  - 断言：最多 2 次 LLM 调用（非 2^N 次）
- [ ] 日志证据（连续 1 小时观测）：
  - `llm.truncated_retry_success` 出现次数 ≤ 5
  - `llm.call` 出现次数 ≤ 单次请求 8 次
- [ ] Feature flag `TRUNCATION_RETRY_DERECURSE=false` 时回退到旧行为（兼容性）

### Task 1.2：修复 finish_reason 检测
- [ ] 单元测试 `test_finish_reason_detection`：
  - retry 后 `finish_reason="stop"` → 正确 break
  - retry 后 `finish_reason="length"` → 继续重试
- [ ] 日志证据：`llm.truncated_retry_success` 后不再出现递归 `llm.call`

### Task 1.3：fallback 链透传 max_tokens
- [ ] 集成测试 `test_fallback_max_tokens_passthrough`：
  - Web UI 传入 32768
  - 触发 fallback 后 max_tokens ≥ 32768
- [ ] 日志证据：`router.*_fallback` 后不再立即出现 `llm.truncated_by_max_tokens`
- [ ] 验证 line 711 硬编码 1000 已移除

### Task 1.4：收敛多层重试
- [ ] 单次请求最大 LLM 调用数 ≤ 8（日志统计）
- [ ] 集成测试：复杂工具调用场景（搜索 + 文件读取 + 计算器）仍能完成
- [ ] `MAX_VERIFICATION_TURNS` 已改为 4
- [ ] `early_retry` 已改为 `range(1)`

### Task 1.5：empty_content 不触发 fallback
- [ ] 单元测试 `test_empty_content_no_fallback`：
  - 模拟 empty_content 错误
  - 断言：不重试，直接降级到 DEGRADED_REPLY
- [ ] 日志证据：`router.retry_exhausted` → `llm.call_failed` 频率下降 ≥ 80%
- [ ] `FailoverReason.EMPTY_REPLY` 已定义并映射到 `RecoveryAction.ABORT`

### Task 1.6：`_describe_images` 安全客户端
- [ ] 单元测试 `test_vision_client_concurrent_refresh`：
  - 模拟 `refresh_client` 并发触发
  - 断言：`_describe_images` 不抛 AttributeError
- [ ] 集成测试：上传图片不出现 "cannot read image"
- [ ] 日志证据：`agent.vision_client_acquired` 出现
- [ ] 代码审查：`self.router._client` 直接访问已全部替换为 `_select_client_for_provider`

### Task 1.7：校验 vision 响应内容
- [ ] 单元测试 `test_vision_failure_pattern_detection`：
  - 模拟 MiMo 返回 "cannot read image"
  - 断言：返回 ""，走兜底分支
- [ ] 日志证据：`agent.vision_suspicious_response` 出现时已降级
- [ ] `VISION_FAILURE_PATTERNS` 已定义并包含中英文模式

### Task 1.8：捕获 BadRequestError
- [ ] 日志证据：`agent.vision_bad_request` 包含 status_code 和 body
- [ ] 代码审查：`except Exception` 已拆分为 `except BadRequestError` + `except Exception`

### Task 1.9：前端区分文档 vs 图片上传
- [ ] 手动测试：上传 .pdf/.docx 不触发 vision API
- [ ] 手动测试：上传 .png/.jpg 仍走 vision API
- [ ] UI 截图：上传组件有 "图片" / "文档" tab 切换

---

## 二、阶段二验证（P1 前端按钮 + 模式持久化）

### Task 2.1：WS payload 结构化字段
- [ ] 集成测试：四个按钮点击后发送，DB `user_message` 为纯净原文
- [ ] WS payload 抓包：包含 `search_mode` / `think_mode` / `image_url` / `doc_paths`
- [ ] Feature flag `USE_STRUCTURED_MODE_FLAGS=false` 时回退到 marker 解析

### Task 2.2：mode_flags 列
- [ ] 数据库迁移成功：`PRAGMA table_info(conversation_logs)` 包含 `mode_flags`
- [ ] 新记录 `mode_flags` 列正确写入 JSON
- [ ] 旧记录 `mode_flags` 为空字符串（不影响）

### Task 2.3：get_messages 返回 mode_flags
- [ ] API 测试：`GET /chat/messages` 返回包含 `mode_flags`
- [ ] 前端 `loadSession` 正确还原 `msg.searchMode` / `msg.thinkMode` / `msg.hasImage`
- [ ] 重试按钮点击后行为与首次发送一致

### Task 2.4：agent_context.cwd 字段
- [ ] 代码审查：`AgentContext.__init__` 包含 `self.cwd: str = ""`
- [ ] `/workspace/confirm` 调用后 `agent_context.cwd` 更新
- [ ] `_process_impl` 入口主动读取 `PermissionManager.cwd`

### Task 2.5：prompt_builder 注入 cwd
- [ ] 日志证据：系统 prompt 包含 `<workspace>当前授权工作目录：{cwd}</workspace>`
- [ ] 手动测试：切换目录后，LLM 回复能引用当前目录

### Task 2.6：workspace/confirm 追加系统消息
- [ ] DB `conversation_logs` 出现 system 消息记录目录切换
- [ ] 日志证据：`agent_context.add_message("system", ...)` 被调用

### Task 2.7：PermissionManager 按 session 隔离
- [ ] 集成测试：两个标签页切换不同目录，工具调用基线不串味
- [ ] 代码审查：`PermissionManager` 按 `session_id` 隔离 cwd

### Task 2.8：过渡兼容旧 marker
- [ ] 旧客户端发送 `[Search:]` marker 仍能正常工作
- [ ] 新客户端发送结构化字段不重复解析
- [ ] 代码审查：`message_processor.py:421-432` 优先使用结构化字段

---

## 三、阶段三验证（P1 空回复 + 主动问候）

### Task 3.1：空回复不入库
- [ ] DB 查询：`SELECT COUNT(*) FROM conversation_logs WHERE assistant_reply='' OR assistant_reply IS NULL` ≤ 总数 1%
- [ ] 日志证据：`bg.skip_empty_reply` 出现
- [ ] 错误仍记录到 `errors` 表

### Task 3.2：agent_context 跳过空 assistant_reply
- [ ] 单元测试 `test_skip_empty_assistant_reply`：
  - DB 中有空回复时，注入的历史摘要不出现空 asst_preview
- [ ] 代码审查：`agent_context.py:792-801` 包含 `if not asst_msg: continue`

### Task 3.3：proactive_greeting 独立通道
- [ ] DB 查询：`SELECT * FROM conversation_logs WHERE user_message LIKE '（场景：%'` 返回 0 行（新记录）
- [ ] 主动问候功能仍正常工作（手动测试）
- [ ] `greeting_log` 表记录"问候已发送"
- [ ] Feature flag `GREETING_INDEPENDENT_CHANNEL=false` 时回退到旧行为

### Task 3.4：截断续写指令不入历史
- [ ] 单元测试 `test_retry_message_not_in_history`：
  - 截断重试后 `context.history` 不含 "请继续完成你的回复..."
- [ ] 代码审查：`retry_messages` 是临时 copy，不写回 `context.history`

---

## 四、阶段四验证（P2 内部提示词收敛）

### Task 4.1：系统 prompt 移除元词汇
- [ ] 代码审查：`prompt_builder.py` 全文无 "工具调用" / "DSML" / "上下文压缩" / "记忆编码" 等元词汇
- [ ] LLM 回复中不再出现技术性元词汇（除非用户主动问）
- [ ] 角色 personality 保持一致

### Task 4.2：截断续写指令角色化
- [ ] 代码审查：line 1052 已改为 `"（继续说完）"` 或 system message
- [ ] 手动测试：截断续写不影响 LLM 角色

### Task 4.3：`[Search:]` 模式不重写 user_input
- [ ] DB 查询：`SELECT * FROM conversation_logs WHERE user_message LIKE '请使用 web_search 工具%'` 返回 0 行（新记录）
- [ ] LLM 仍能正确调用 web_search（手动测试）
- [ ] 代码审查：通过 system prompt 注入或 tool-forcing 实现

### Task 4.4：上下文压缩摘要角色化
- [ ] 代码审查：`agent_context.py:208-210` 关键词改为 "历史回顾" 或 "过往点滴"
- [ ] LLM 看到的压缩摘要符合角色设定

---

## 五、阶段五验证（P2 跨渠道一致性）

### Task 5.1：统一 max_tokens 配置
- [ ] 代码审查：`config.py` 包含 `CHANNEL_MAX_TOKENS` JSON 配置
- [ ] 各渠道 max_tokens 配置统一管理
- [ ] 手动测试：QQ 通道不再被超长回复截断

### Task 5.2：渠道感知场景标识
- [ ] 代码审查：`_SCENE_HINTS` 扩展为完整渠道策略表
- [ ] 各渠道场景标识完整

### Task 5.3：群聊多用户隔离
- [ ] 集成测试：群聊多用户场景无串话
- [ ] 用户 A 的隐私不泄露给用户 B
- [ ] `switch_user_context` 在群聊场景下被正确调用

---

## 六、阶段六验证（端到端 + 监控）

### Task 6.1：端到端测试
- [ ] `tests/test_context_management_e2e.py` 所有测试通过
- [ ] 每个按钮点击 → 发送 → 持久化 → 重载会话 → 校验流程完整
- [ ] 校验 `user_message` 是否为用户原话
- [ ] 校验模式是否可还原
- [ ] 校验重试是否复现首次行为

### Task 6.2：监控指标基线
- [ ] 新增指标 `llm.calls_per_request` 可在监控面板查看
- [ ] 新增指标 `context.empty_reply_skipped` 可查看
- [ ] 新增指标 `vision.suspicious_response` 可查看
- [ ] 基线对比：单次请求平均 LLM 调用次数下降 ≥ 60%
- [ ] 基线对比：单次请求平均 token 消耗下降 ≥ 30%

### Task 6.3：文档更新
- [ ] `docs/ARCHITECTURE.md` 上下文管理架构图已更新
- [ ] 渠道差异说明已更新
- [ ] 前端按钮状态机已更新
- [ ] 文档与代码一致（人工 review）

---

## 七、最终验收（全流程）

### 7.1 日志侧（连续 24 小时观测）
- [ ] `llm.truncated_retry_success` 出现次数 ≤ 5/小时
- [ ] `router.retry_exhausted` + `llm.call_failed` + `router.*_fallback` 链路触发频率 ≤ 1 次/10 分钟
- [ ] 无 `agent.vision_suspicious_response` WARNING（或出现时已正确走兜底）
- [ ] 无 `error_classifier.classified` + `credential_pool.error_no_state_change` 风暴

### 7.2 数据库侧
- [ ] `conversation_logs.assistant_reply` 为空的记录数 ≤ 总数 1%
- [ ] `conversation_logs.user_message` 不含：
  - `（场景：`
  - `请使用 web_search 工具`
  - `[Image:`
  - `[Search:`
  - `[Think:`
- [ ] `mode_flags` 列正确持久化模式状态

### 7.3 用户体验侧（手动测试）
- [ ] 用户气泡显示纯净原文，无 marker
- [ ] 重试按钮点击后行为与首次发送一致
- [ ] mimo-v2.5 上传图片不再出现 "cannot read image"
- [ ] 切换工作目录后，LLM 后续回复能感知新目录
- [ ] 主动问候不再让 LLM 出戏
- [ ] 单次请求 LLM 调用次数 ≤ 8
- [ ] 长回复完整（无截断感）
- [ ] 工具调用结果正确反映在回复中

### 7.4 成本侧
- [ ] 单次请求平均 LLM 调用次数下降 ≥ 60%
- [ ] 单次请求平均 token 消耗下降 ≥ 30%

### 7.5 回归测试
- [ ] `/compress` 斜杠命令仍正常工作
- [ ] `switch_user_context` 群聊隔离仍正常工作
- [ ] TTS 自动触发不受影响
- [ ] 笔记自动提取不受影响
- [ ] 画像冷启动不受影响
- [ ] 学习评估不受影响
- [ ] 本能提取不受影响

---

## 八、回滚预案验证

- [ ] `TRUNCATION_RETRY_DERECURSE=false` 可回退到旧截断重试行为
- [ ] `USE_STRUCTURED_MODE_FLAGS=false` 可回退到旧 marker 解析
- [ ] `GREETING_INDEPENDENT_CHANNEL=false` 可回退到旧主动问候路径
- [ ] 每个 Task 独立 commit，可二分回滚
- [ ] 数据库迁移支持降级（`mode_flags` 列可保留不影响）

---

## 九、签收

- [ ] 开发自测通过（以上所有 checkbox 打勾）
- [ ] 用户验收测试通过（UAT）
- [ ] Code review 通过（TRAE-code-review + TRAE-security-review）
- [ ] 监控指标稳定运行 24 小时
- [ ] 文档更新完成
- [ ] Git commit history 清晰（每个 Task 独立 commit）

---

## 十、附：验证命令速查

```bash
# 1. 查看截断重试风暴
journalctl -u nahida-web.service --since "1 hour ago" | grep -E "truncated_retry_success|retry_exhausted|call_failed" | wc -l

# 2. 查看 conversation_logs 空回复
python3 -c "
import sqlite3
c = sqlite3.connect('file:/media/orangepi/KIOXIA/nahida-data/db/agent.db?mode=ro', uri=True)
cur = c.cursor()
cur.execute(\"SELECT COUNT(*) FROM conversation_logs WHERE assistant_reply='' OR assistant_reply IS NULL\")
print('空回复数:', cur.fetchone()[0])
cur.execute(\"SELECT COUNT(*) FROM conversation_logs WHERE user_message LIKE '%（场景：%' OR user_message LIKE '%请使用 web_search 工具%' OR user_message LIKE '%[Image:%' OR user_message LIKE '%[Search:%' OR user_message LIKE '%[Think:%'\")
print('marker 污染数:', cur.fetchone()[0])
"

# 3. 查看单次请求 LLM 调用次数
journalctl -u nahida-web.service --since "10 minutes ago" | grep -c "llm.call"

# 4. 运行单元测试
cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_router_truncation.py -v

# 5. 运行端到端测试
cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_context_management_e2e.py -v
```
