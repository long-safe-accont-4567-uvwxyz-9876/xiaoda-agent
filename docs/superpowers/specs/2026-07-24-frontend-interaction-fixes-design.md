# 前端交互流畅性修复设计

**日期**: 2026-07-24
**来源**: 前端代码审查（交互流畅性专项）

## 背景

对整个项目前端进行代码审查后，识别出若干影响交互流畅性的问题。经逐项核对实际当前代码，部分审查发现为误报或过度设计，本设计仅保留经验证成立的修复项。

## 修复范围（经验证成立，共 10 项）

### #3 音频资源未完全释放
- **位置**: `web/frontend/src/views/ChatView.vue` `play()` L114-122
- **现状**: `play()` 切换音频时执行 `pause()` + 置空回调 + `audioEl = null`，但未执行 `audioEl.src = ''`。而 `onBeforeUnmount` (L62) 与 `onDeactivated` (L67) 都正确执行了 `src = ''`。
- **修复**: 在 `play()` 置空前补 `audioEl.src = ''`，与卸载清理逻辑保持一致，及时释放底层媒体资源。

### #4 会话切换缺少加载状态
- **位置**: `web/frontend/src/views/ChatView.vue` `switchSession()` L190-195
- **现状**: `await chat.loadSession(sid)` 期间无任何 UI 反馈，慢网络下用户不知是否在加载。
- **修复**: 新增 `loadingSessionId` ref；加载期间禁用会话列表点击并显示旋转指示器；finally 中复位。

### #7 懒加载图片无加载反馈
- **位置**: `web/frontend/src/views/ChatView.vue` L297-298（生成产物图）及 L288-289（用户上传图）
- **现状**: `<img loading="lazy">` 在慢网络下显示空白，无骨架/淡入。
- **修复**: 为图片增加 `@load`/`@error` 处理与 `loaded` 状态类，加载前显示占位背景，加载完成后淡入。

### #11 数组 reverse 拷贝开销
- **位置**: `web/frontend/src/stores/chat.ts` L134（`onToolEvent`）、L288（`retryLast`）
- **现状**: `[...msg.toolCalls].reverse().find(...)` 与 `[...messages.value].reverse().findIndex(...)` 每次创建中间数组拷贝。
- **修复**: 改为反向 `for` 循环，与 ChatView.vue `findLastFinalAssistant` (L106-112) 既有模式保持一致。

### #6 输入框焦点管理（审查中发现的真实 bug，原误报修正）
- **位置**: `web/frontend/src/views/ChatView.vue` `selectCommand()`；`web/frontend/src/components/chat/PromptInput.vue`
- **现状**: 经核对，"发送后焦点丢失"是误报（PromptInput 的 textarea 在 emit 后不失焦）。但发现真实 bug：`selectCommand` 调用 `inputEl.value?.focus()`，而 `inputEl` 在 ChatView 模板中**从未绑定**（始终为 null）→ 选择斜杠命令后焦点不回到输入框，用户须手动点击。同时 PromptInput 语音识别完成路径用脆弱的 `document.querySelector('.prompt-input textarea')` 聚焦。
- **修复**: PromptInput 新增 `focus()` 方法并 `defineExpose({ focus })`；ChatView 新增 `promptInputRef` 绑定到 `<PromptInput>`，`selectCommand` 改为 `nextTick(() => promptInputRef.value?.focus())`；PromptInput 语音路径改用内部 `focus()` 替代 querySelector。

### #1 消息列表滚动 watcher 用 flush:'post'
- **位置**: `web/frontend/src/views/ChatView.vue` L71-81
- **现状**: `watch(() => chat.messages.length, async () => { await nextTick(); ... })` 用 async + nextTick 等待 DOM 更新。
- **修复**: 改为 `{ flush: 'post' }` 选项，回调在 DOM 更新后执行，语义等价且更简洁（移除 async/await 样板）。

### #2 WS 重连放弃后复位 _reconnecting 标志
- **位置**: `web/frontend/src/api/ws.ts` `scheduleReconnect()` L140-141
- **现状**: 当 `reconnectAttempts >= 20` 时 `scheduleReconnect` 提前 return，但 `_reconnecting` 保持 true（重连逻辑本身已幂等，但标志未复位）。
- **修复**: 放弃重连时 `this._reconnecting = false`，保持状态一致性。

### #5 终端挂载放弃时记录告警
- **位置**: `web/frontend/src/components/chat/ChatTerminal.vue` `mountTerminal()` L221-227
- **现状**: 容器查找重试 20 次后静默放弃，无任何日志，排查挂载竞态困难。
- **修复**: 会话仍存活但重试耗尽时 `console.warn` 记录，便于排查。

### #8 markdown 缓存增加 TTL
- **位置**: `web/frontend/src/utils/markdown.ts` L47-80
- **现状**: LRU 已限 100 条 + 切换会话时 `clearMarkdownCache()`，但长会话内条目无时间淘汰。
- **修复**: 缓存值改为 `{ html, ts }`，新增 `CACHE_TTL_MS = 10min`；命中时校验未过期才复用，过期则淘汰重渲染。每次读取仅一次 `Date.now()` 比较，开销可忽略。

### #10 EmotionAvatar 瞬态 GPU 提升
- **位置**: `web/frontend/src/components/chat/EmotionAvatar.vue` `.ring`
- **现状**: `.ring` 的 `grass-ring` 是 0.8s 一次性脉冲；原审查建议加 `will-change`，但永久 `will-change` 会常驻合成器层（反模式）。
- **修复**: 采用 Chrome 推荐的瞬态用法——仅在 `.pulse .ring`（动画期间）声明 `will-change: transform`，并给 `<span class="ring">` 加 `@animationend="pulse = false"`，动画结束后 `.pulse` 类移除、`will-change` 随之失效。无视觉回归（动画终态本就是 opacity:0）。

## 明确不修复项（附证据，共 1 项）

| # | 发现 | 不修复理由 |
|---|------|-----------|
| #9 | SlashPalette 无 Esc | 父级 ChatView L165 已处理 `Escape`（`if (e.key === 'Escape') { inputText.value = ''; return }`，清空 inputText 即关闭 palette）。再加属死代码。已完成。 |

> 注：原审查 11 项中，10 项已实施（含 #6 在核对中发现并修复了 `inputEl` 未绑定的真实 bug），仅 #9 因确属死代码而不实施。

## 验证策略

- 无前端测试框架（package.json 无 vitest/jest），不引入新框架（超出范围）。
- 每个文件组修复后运行 `npx vue-tsc --noEmit` 与 `node build.mjs` 确认无类型/编译错误。
- 交互行为通过代码推理 + 边界条件分析保证正确性。

## 实施顺序（依次）

1. **chat.ts** — #11（纯逻辑，最低风险，隔离）
2. **ChatView.vue** — #3、#4、#7（同文件三项 UX 修复）
