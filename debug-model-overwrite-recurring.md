# Debug: 模型反复被覆盖为 mimo / Agnes

## Status
[OPEN] — 前端 request storm 已修复，等待用户确认后清理

## 根因（已确认，证据在 journal）
- 前端 `ModelSelector.selectModel` 每次选择模型会触发**请求风暴**：单次点击引发 10+ 个
  重复 `POST /api/v1/models/chat-model`（journal 中 20+ 个不同 request_id，同一秒内）。
- 每个 POST 都调用 `set_chat_model` 并持久化 `models.chat_model` + `models.routes.*`。
- 竞态下最后一次写覆盖用户选择 → 用户选 agnes，但 mimo 被写回并持久化 → 启动恢复时
  读到 mimo，"Agnes 反复被覆盖回 mimo"。
- 证据：`config_service.models_write path=models.chat_model` 堆栈全部来自
  `model_discovery.py:500 set_chat_model`（即前端 POST），无其他写入路径。

## 修复（已实施并验证）
- `web/frontend/src/components/chat/ModelSelector.vue`：
  - `selectModel` 增加 `switching` 锁，切换期间忽略重复点击（一次选择 = 一次 POST）。
  - 点击后立即关闭弹层，移除可点击目标，避免用户因无响应而反复点击。
  - 乐观更新 `currentModel`，让切换即时生效；失败时回滚到后端真实值。
- 已 `npm run build` 重建前端并 `systemctl restart nahida-web`。
- 验证：浏览器模拟 5 次快速点击 → 仅 1 个 POST；3 次快速点击 agnes → 仅 1 个 POST；
  文件最终正确持久化为 agnes。

## 症状
- WebUI 主聊天模型反复被覆盖为 `mimo/mimo-v2.5`，用户之前一直手动切到 `agnes`。
- 已反复出现 10+ 次，长期未根治。

## 已确认事实（证据）
1. 服务 `nahida-web` 正常运行（PID 4913，20:37:24 启动）。
2. 持久化文件 `/media/orangepi/KIOXIA/nahida-data/config/webui_overrides.json`：
   - mtime = `2026-08-05 20:09:43`（服务当时未运行，journal 最早 20:17）。
   - `models.chat_model = {provider: mimo, model_id: mimo-v2.5}`
   - `models.routes`：chat / tool_result_wrap / memory_encoding / emotion_analysis 全为 mimo；chat_agnes 仍为 agnes。
3. 启动时（20:38:04）`webui.chat_model_restore_attempt saved=mimo current_route=mimo` → `webui.chat_model_restored provider=mimo`。即启动前文件就已是被覆盖成 mimo 的状态。
4. 当日日志中**没有任何** `router.chat_model_changed` / `discover.chat_model_set` 事件（唯一入口 set_chat_model 未触发），说明 mimo 写入**绕过了 set_chat_model**，或相关日志已被 journal 轮转。

## 假说（falsifiable）
- H1: WebUI 前端在加载/保存时自动写回 mimo（如 ModelSelector 默认值或 route 编辑）。
- H2: `PUT /models/routes/{task}`（web/routers/models.py:255）在 task=chat 时用 registry chat 路由的 client 覆盖 `models.chat_model`，chat 路由为 mimo 时写回 mimo。
- H3: 存在 sticky fallback / degrade 路径自动调用 `set_chat_model("mimo", ...)` 持久化 mimo。
- H4: 引用污染仍存在（非 models 路径深拷贝缺口），某处直接改 `_data` 后任意 set() 持久化 mimo。

## 计划
1. 在 ConfigService.set() 对 `models.` 路径写入时记录堆栈 + 值（唯一合法写入阀点）。
2. 复现：WebUI 切到 agnes → 观察是否被写回 mimo。
3. 依据堆栈定位根因，实施最小修复。
4. 修复前后日志对比验证。