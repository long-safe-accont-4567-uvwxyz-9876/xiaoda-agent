# WebUI Experience Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留草元素暗绿玻璃拟态的前提下，补齐聊天、异步任务、导航、视觉反馈与性能策略的体验闭环。

**Architecture:** 以 Pinia Store 作为业务和效果状态的权威来源，组件只负责收集意图与呈现状态；WebSocket 继续负责可靠发送和事件分发。实施拆成四个可独立验收阶段，所有修改采用窄补丁叠加到当前工作区，不覆盖已有 Provider 与本地 AI 未提交改动。

**Tech Stack:** Vue 3、TypeScript、Pinia、Naive UI、Vue Router、FastAPI、Python 3.11、pytest、Vite

## Global Constraints

- 保留现有草元素暗绿玻璃拟态，不整体替换主题。
- 以桌面端为主要验收环境，同时保证触屏和小屏基本可用。
- 自动性能降级不得覆盖或持久化用户偏好。
- 不删除或绕过任何 Setup 文件、路由、组件与逻辑。
- 不引入 WebSocket 隐式补发，发送失败必须保留草稿并由用户显式重试。
- 不使用 checkout、restore、reset、stash、整文件覆盖或全目录格式化处理当前未提交修改。
- 不提交代码，除非用户另行明确要求。

---

### Task 1: 聊天请求快照与可靠发送

**Files:**
- Modify: `web/frontend/src/stores/chat.ts`
- Modify: `web/frontend/src/api/ws.ts`
- Modify: `web/frontend/src/components/chat/PromptInput.vue`
- Modify: `web/frontend/src/views/ChatView.vue`
- Modify: `web/frontend/src/i18n/zh.ts`
- Modify: `web/frontend/src/i18n/en.ts`
- Test: `tests/test_frontend_runtime_contracts.py`
- Create: `tests/test_webui_chat_experience_contracts.py`

**Interfaces:**
- Produces: `ChatRequestSnapshot`、`ChatSendResult`、`sendMessage(request): ChatSendResult`、`retryMessage(messageId): ChatSendResult`、`ws.retry(): boolean`。
- Consumes: 现有 `ws.send(data): boolean`、上传 API、Naive UI message provider。

- [ ] **Step 1: 写聊天失败契约测试**

```python
def test_chat_returns_structured_failure_before_mutating_messages():
    chat = source("web/frontend/src/stores/chat.ts")
    assert "export type ChatSendResult" in chat
    assert "reason: 'DISCONNECTED'" in chat
    assert chat.index("ws.send(payload)") < chat.index("pushMessage(messages")

def test_prompt_keeps_draft_until_parent_confirms_success():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    view = source("web/frontend/src/views/ChatView.vue")
    assert "clearSubmittedDraft" in prompt
    assert "if (!result.ok)" in view
    assert view.index("if (!result.ok)") < view.index("clearSubmittedDraft")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_frontend_runtime_contracts.py tests/test_webui_chat_experience_contracts.py -q
```

Expected: 新契约因类型、结构化结果和成功后清理尚不存在而失败。

- [ ] **Step 3: 建立共享请求与结果类型**

```ts
export interface ChatAttachmentSnapshot {
  kind: 'image' | 'document'
  url: string
  name: string
  path?: string
  ext?: string
}

export interface ChatRequestSnapshot {
  text: string
  search: boolean
  think: boolean
  attachments: ChatAttachmentSnapshot[]
}

export type ChatSendResult =
  | { ok: true; msgId: string }
  | { ok: false; reason: 'EMPTY_REQUEST' | 'PROCESSING' | 'DISCONNECTED' }
```

- [ ] **Step 4: 让 Store 只在交付成功后变更状态**

```ts
function sendMessage(request: ChatRequestSnapshot): ChatSendResult {
  if (!request.text.trim() && request.attachments.length === 0) {
    return { ok: false, reason: 'EMPTY_REQUEST' }
  }
  if (isProcessing.value) return { ok: false, reason: 'PROCESSING' }
  const msgId = crypto.randomUUID()
  const payload = buildChatPayload(request, msgId)
  if (!wsConnected.value || !ws.send(payload)) {
    return { ok: false, reason: 'DISCONNECTED' }
  }
  pushMessage(messages, createUserMessage(request, msgId))
  isProcessing.value = true
  return { ok: true, msgId }
}
```

- [ ] **Step 5: 改造输入组件的可发送状态和上传反馈**

```ts
const hasAttachment = computed(() => uploadedImage.value !== null || uploadedDoc.value !== null)
const hasSendableContent = computed(() => props.modelValue.trim().length > 0 || hasAttachment.value)
const canSend = computed(() =>
  hasSendableContent.value && uploadState.value !== 'uploading' &&
  !props.isLoading && props.connected && !props.disabled,
)
```

组件只发出 `ChatRequestSnapshot`；仅由 `clearSubmittedDraft()` 清附件和模式。上传失败、不支持类型、断线和重连动作使用 `role="status" aria-live="polite"` 展示，所有图标按钮补 `aria-label`。

- [ ] **Step 6: 父页面按结果决定是否清草稿**

```ts
function handlePromptSend(request: ChatRequestSnapshot) {
  const result = chat.sendMessage(request)
  if (!result.ok) {
    if (result.reason === 'DISCONNECTED') message.warning(t('promptInput.disconnectedDraftKept'))
    return
  }
  inputText.value = ''
  promptInputRef.value?.clearSubmittedDraft()
}
```

- [ ] **Step 7: 增加显式重连与双语文案**

```ts
retry(): boolean {
  const token = localStorage.getItem('token')
  if (!token) return false
  this.connect(token)
  return true
}
```

文案覆盖上传中、不支持文件、上传失败、移除文档、草稿已保留和重新连接。

- [ ] **Step 8: 运行聊天契约与类型检查**

```bash
.venv/bin/python -m pytest tests/test_frontend_runtime_contracts.py tests/test_webui_chat_experience_contracts.py -q
cd web/frontend && npx vue-tsc --noEmit
```

Expected: 契约全部通过；类型检查退出码为 0。

### Task 2: 附件独立发送与历史重试

**Files:**
- Modify: `web/ws_hub.py`
- Modify: `db/database.py`
- Modify: `core/background_tasks.py`
- Modify: `web/schemas.py`
- Modify: `web/routers/chat.py`
- Modify: `web/frontend/src/stores/chat.ts`
- Modify: `web/frontend/src/views/ChatView.vue`
- Create: `tests/test_webui_chat_request_contract.py`

**Interfaces:**
- Consumes: Task 1 的 `ChatRequestSnapshot`。
- Produces: `conversation_logs.request_context_json`、历史响应 `request_context`、可完整重放的用户消息。

- [ ] **Step 1: 写服务端附件与历史契约测试**

```python
@pytest.mark.asyncio
async def test_ws_accepts_attachment_without_text():
    payload = {"type": "chat", "text": "", "image_url": "/media/uploads/a.png"}
    assert await dispatch_chat(payload) != "EMPTY_REQUEST"

@pytest.mark.asyncio
async def test_invalid_history_context_degrades_to_none():
    item = decode_history_context("{broken")
    assert item is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_webui_chat_request_contract.py -q
```

Expected: 空文本附件和历史请求快照尚未支持而失败。

- [ ] **Step 3: 服务端接受安全附件请求**

```python
if not text and not image_url_field and not doc_path_field:
    await manager.send_to(conn_id, {
        "type": "error", "msg_id": msg_id,
        "code": "EMPTY_REQUEST", "message": "消息或附件不能为空",
    })
    return

effective_text = text or (
    "请分析用户上传的图片。" if image_url_field else "请读取并处理用户上传的文档。"
)
```

附件 URL 和文档路径必须经过现有媒体目录与上传目录约束；请求快照只保留白名单字段及长度受限的名称。

- [ ] **Step 4: 增加数据库迁移与写入参数**

```sql
ALTER TABLE conversation_logs ADD COLUMN request_context_json TEXT DEFAULT '{}'
```

新增迁移版本 26，并让 `insert_conversation_log(..., request_context_json: str = "{}")` 向后兼容现有调用者。

- [ ] **Step 5: 在后台持久化安全请求快照**

```python
request_context_json = json.dumps(request_context or {}, ensure_ascii=False)
await db.insert_conversation_log(
    user_id, source, original_user_message, assistant_reply,
    emotion_label, model_used, session_id, request_context_json,
)
```

- [ ] **Step 6: 历史接口返回可恢复请求**

```python
class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    emotion: str | None = None
    timestamp: float
    tool_calls: list | None = None
    request_context: dict | None = None
```

无效 JSON 降级为 `None`；助手消息不返回用户请求上下文。

- [ ] **Step 7: 前端从快照重试而非反推文本**

```ts
function retryMessage(messageId: string): ChatSendResult {
  const message = messages.value.find(item => item.id === messageId)
  if (!message?.request) return { ok: false, reason: 'EMPTY_REQUEST' }
  return sendMessage(structuredClone(message.request))
}
```

- [ ] **Step 8: 运行聊天后端回归**

```bash
.venv/bin/python -m pytest tests/test_webui_chat_request_contract.py tests/test_frontend_runtime_contracts.py tests/test_ws_heartbeat.py tests/test_ws_broadcast_backpressure.py -q
```

Expected: 附件、历史、心跳和背压测试全部通过。

### Task 3: 响应式导航与可访问菜单

**Files:**
- Modify: `web/frontend/src/components/layout/AppLayout.vue`
- Modify: `web/frontend/src/components/layout/SideBar.vue`
- Modify: `web/frontend/src/components/layout/TopBar.vue`
- Modify: `web/frontend/src/components/chat/PromptInput.vue`
- Modify: `web/frontend/src/components/chat/SlashPalette.vue`
- Modify: `web/frontend/src/views/ChatView.vue`
- Modify: `web/frontend/src/styles/sumeru-tokens.css`
- Modify: `web/frontend/src/i18n/zh.ts`
- Modify: `web/frontend/src/i18n/en.ts`
- Create: `tests/test_webui_navigation_contracts.py`

**Interfaces:**
- Produces: AppLayout 管理的 `mobileSidebarOpen`、TopBar `toggle-sidebar`、SideBar `close`、PromptInput `keydown` 转发。
- Consumes: 现有路由、导航项、SlashPalette `move/selectCurrent`。

- [ ] **Step 1: 写导航和菜单契约测试**

```python
def test_mobile_sidebar_has_explicit_open_close_paths():
    layout = source("web/frontend/src/components/layout/AppLayout.vue")
    assert "mobileSidebarOpen" in layout
    assert "@click=\"closeMobileSidebar\"" in layout
    assert "event.key === 'Escape'" in layout

def test_slash_palette_exposes_listbox_semantics():
    palette = source("web/frontend/src/components/chat/SlashPalette.vue")
    assert 'role="listbox"' in palette
    assert 'role="option"' in palette
    assert 'aria-selected' in palette
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_webui_navigation_contracts.py -q
```

- [ ] **Step 3: 由布局层统一管理桌面和移动侧栏**

```ts
const desktopSidebarExpanded = ref(false)
const mobileSidebarOpen = ref(false)
const closeMobileSidebar = () => { mobileSidebarOpen.value = false }

watch(() => route.fullPath, closeMobileSidebar)
function onShellKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeMobileSidebar()
}
```

移动侧栏开启时渲染遮罩；桌面 hover 与移动 open 不共用一个状态。

- [ ] **Step 4: 增加菜单按钮和侧栏可访问状态**

TopBar 发出 `toggle-sidebar`，按钮提供 `aria-expanded` 和 `aria-controls`。SideBar 使用 `nav aria-label`、显式关闭按钮、选择路由后发出 `close`，并保留现有全部路由和精确高亮。

- [ ] **Step 5: 修复 TopBar 头像错误和连接状态语义**

```ts
const failedAvatars = ref(new Set<string>())
function onAvatarError(name: string) {
  failedAvatars.value = new Set(failedAvatars.value).add(name)
}
```

Agent 按钮增加 `aria-pressed`；连接状态使用 `role="status"` 和可读文本，不只依赖颜色。

- [ ] **Step 6: 接通斜杠菜单键盘事件**

PromptInput 先向父层发出 `keydown`，若事件已 `preventDefault()` 则不执行普通 Enter 发送。ChatView 在面板可见时优先处理上下键、Tab、Enter 和 Escape；SlashPalette 提供 listbox/option/active descendant 语义。

- [ ] **Step 7: 增加响应式令牌和 reduced-motion 样式**

```css
:root {
  --sidebar-mobile-width: min(82vw, 320px);
  --z-overlay: 70;
  --z-sidebar: 80;
  --z-palette: 90;
  --motion-fast: 120ms;
  --motion-normal: 220ms;
}
```

移动端使用 transform 进出；`prefers-reduced-motion` 下移除门扉、位移和 3D 转场。

- [ ] **Step 8: 运行导航契约与类型检查**

```bash
.venv/bin/python -m pytest tests/test_webui_navigation_contracts.py tests/test_frontend_runtime_contracts.py -q
cd web/frontend && npx vue-tsc --noEmit
```

Expected: 所有导航契约通过，类型检查零错误。

### Task 4: 目录选择器视觉与竞态修复

**Files:**
- Modify: `web/routers/workspace.py`
- Modify: `web/frontend/src/stores/workspace.ts`
- Modify: `web/frontend/src/components/workspace/DirectoryPickerDialog.vue`
- Modify: `web/frontend/src/styles/components.css`
- Modify: `web/frontend/src/i18n/zh.ts`
- Modify: `web/frontend/src/i18n/en.ts`
- Create: `tests/test_webui_directory_picker_contracts.py`

**Interfaces:**
- Produces: `DirectoryEntry { name: string; path: string }`、类型化 `browse(path?)` 响应。
- Consumes: 现有目录选择 `select(path)` 与 `cancel` 事件、后端权限边界。

- [ ] **Step 1: 写目录返回结构和竞态契约测试**

```python
def test_workspace_browse_returns_complete_child_paths():
    result = browse_fixture("/home/user", ["docs"])
    assert result["entries"] == [{"name": "docs", "path": "/home/user/docs"}]

def test_dialog_ignores_stale_browse_response():
    dialog = source("web/frontend/src/components/workspace/DirectoryPickerDialog.vue")
    assert "requestGeneration" in dialog
    assert "if (generation !== requestGeneration) return" in dialog
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_webui_directory_picker_contracts.py -q
```

- [ ] **Step 3: 后端返回完整子目录路径**

```python
entries = [
    {"name": name, "path": os.path.join(target, name)}
    for name in dirs
]
return ok({"current": target, "parent": parent, "dirs": dirs, "entries": entries})
```

保留 `dirs` 以兼容旧调用者，前端优先使用 `entries`。

- [ ] **Step 4: Store 声明目录浏览类型**

```ts
export interface DirectoryEntry { name: string; path: string }
export interface DirectoryBrowseResult {
  current: string
  parent: string | null
  dirs: string[]
  entries: DirectoryEntry[]
}
```

- [ ] **Step 5: 修复重开刷新和并发覆盖**

```ts
let requestGeneration = 0
async function browse(path?: string) {
  const generation = ++requestGeneration
  loading.value = true
  try {
    const result = await workspace.browse(path)
    if (generation !== requestGeneration) return
    applyBrowseResult(result)
  } finally {
    if (generation === requestGeneration) loading.value = false
  }
}
```

- [ ] **Step 6: 统一暗色主题和键盘操作**

目录项改用 `button`；加载时禁用目录和确认按钮；移除 inline style 与 `#eee/#f5f5f5/#888/#999`，改用令牌和统一 `:focus-visible`。

- [ ] **Step 7: 运行目录和权限回归**

```bash
.venv/bin/python -m pytest tests/test_webui_directory_picker_contracts.py tests/test_workspace_api.py -q
cd web/frontend && npx vue-tsc --noEmit
```

Expected: Unix、Windows 盘符与竞态契约通过，既有权限测试保持通过。

### Task 5: 本地 AI 启动状态闭环

**Files:**
- Modify: `web/routers/local_ai.py`
- Modify: `web/frontend/src/api/localAi.ts`
- Modify: `web/frontend/src/api/ws.ts`
- Modify: `web/frontend/src/stores/localAi.ts`
- Modify: `web/frontend/src/components/local-ai/DeploymentsTab.vue`
- Test: `tests/test_local_ai_api.py`
- Test: `tests/test_frontend_local_ai_contracts.py`

**Interfaces:**
- Produces: `local_ai_instance_updated` 的 `starting|succeeded|failed` 操作状态、`instanceStartsByRequestId`。
- Consumes: 当前未提交的 tuple 幂等键、实例恢复、停止实例过滤与竞态保护实现。

- [ ] **Step 1: 保存目标文件外部补丁快照**

```bash
git diff --binary -- web/routers/local_ai.py web/frontend/src/components/local-ai/DeploymentsTab.vue tests/test_local_ai_api.py tests/test_frontend_local_ai_contracts.py > /tmp/webui-local-ai-before.patch
```

Expected: `/tmp` 中存在快照，仓库状态不变。

- [ ] **Step 2: 追加启动失败契约测试**

```python
assert event["type"] == "local_ai_instance_updated"
assert event["request_id"] == request_id
assert event["model_id"] == model_id
assert event["status"] == "failed"
assert event["error"]["code"]
assert event["error"]["retryable"] is True
```

- [ ] **Step 3: 运行局部测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_local_ai_api.py tests/test_frontend_local_ai_contracts.py -q
```

- [ ] **Step 4: 后端广播操作生命周期**

```python
await broadcast({
    "type": "local_ai_instance_updated",
    "request_id": request_id,
    "model_id": model_id,
    "operation": "start",
    "status": "failed",
    "error": safe_operation_error(exc),
})
```

接受任务后广播 `starting`，成功广播 `succeeded + instance`，失败广播白名单错误码和脱敏消息；终态最多广播一次，并给结果缓存增加有界清理。

- [ ] **Step 5: Store 建立请求级状态机**

```ts
interface InstanceStartState {
  requestId: string
  modelId: string
  status: 'starting' | 'succeeded' | 'failed'
  error?: LocalAiOperationError
  updatedAt: number
}
```

HTTP 失败立即置 failed；WS 事件按 request ID 更新；成功同时 upsert instance；未知 request ID 的终态也可被吸收。

- [ ] **Step 6: 页面展示真实生命周期**

HTTP 202 只提示“已提交启动请求”；按钮 loading 由 Store 状态驱动；失败错误持续显示并提供生成新 request ID 的重试按钮；断线时显示“状态待确认”而非无限转圈。

- [ ] **Step 7: 运行本地 AI 回归并核对原修改**

```bash
.venv/bin/python -m pytest tests/test_local_ai_api.py tests/test_local_ai_instances.py tests/test_frontend_local_ai_contracts.py tests/test_frontend_runtime_contracts.py -q
git diff --check
git diff --binary -- web/routers/local_ai.py web/frontend/src/components/local-ai/DeploymentsTab.vue
```

Expected: 测试全部通过；tuple 幂等键、恢复、关闭和 stopped 过滤改动仍存在。

### Task 6: 集中式动效性能策略

**Files:**
- Modify: `web/frontend/src/stores/ui.ts`
- Modify: `web/frontend/src/App.vue`
- Modify: `web/frontend/src/components/fx/GrassParticles.vue`
- Modify: `web/frontend/src/components/fx/DendroCursor.vue`
- Modify: `web/frontend/src/components/fx/Tilt3D.vue`
- Modify: `web/frontend/src/components/layout/AgentBackdrop.vue`
- Modify: `web/frontend/src/styles/theme.css`
- Test: `tests/test_frontend_runtime_contracts.py`
- Create: `tests/test_webui_effect_policy_contracts.py`

**Interfaces:**
- Produces: `PerformanceTier`、`effectsReady`、`effectiveParticles`、`effectiveTilt3d`、`effectiveDendroCursor`。
- Consumes: 现有用户 UI 偏好，且不修改其持久化语义。

- [ ] **Step 1: 写效果策略契约测试**

```python
def test_runtime_tier_never_persists_user_preferences():
    ui = source("web/frontend/src/stores/ui.ts")
    body = function_body(ui, "setPerformanceTier")
    assert "localStorage" not in body
    assert "/system/config" not in body

def test_particles_mount_only_after_effects_ready():
    app = source("web/frontend/src/App.vue")
    assert "ui.effectsReady && ui.effectiveParticles !== 'off'" in app
```

- [ ] **Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_webui_effect_policy_contracts.py -q
```

- [ ] **Step 3: UI Store 分离用户偏好与运行时档位**

```ts
export type PerformanceTier = 'full' | 'reduced' | 'minimal'
const effectsReady = ref(false)
const reducedMotion = ref(false)
const performanceTier = ref<PerformanceTier>('full')
```

所有 `effective*` 由三者共同派生；`setPerformanceTier()` 只改瞬时状态。

- [ ] **Step 4: 首屏绘制后再启用装饰**

```ts
booting.value = false
await nextTick()
await nextAnimationFrame()
await nextAnimationFrame()
scheduleEffectsWithIdleCallback()
```

保存并在卸载时取消 idle、timeout、rAF 和媒体查询监听。

- [ ] **Step 5: 统一 reduced-motion 与效果消费**

App 监听 `matchMedia('(prefers-reduced-motion: reduce)')` 的初值和 change；粒子、光标、Tilt3D 不再自行探测，统一读取 Store 的有效状态。reduced-motion 下关闭持续粒子、拖尾、Tilt3D 和定制光标 rAF。

- [ ] **Step 6: 移除重复 FPS 探测和偏好污染**

删除 App 与 GrassParticles 的独立固定阈值探测；软件渲染只设置 `minimal`；相对基线探测只允许设置运行时档位，禁止调用 `setParticles('low')`。

- [ ] **Step 7: 增加 reduced/minimal 样式组合**

```css
body.effects-reduced { --glass-blur: 4px; }
body.effects-minimal { --glass-blur: 0px; }
body.effects-minimal .decorative-motion { animation: none !important; }
```

背景切换在 reduced-motion 下仅保留不超过 100ms 的透明度变化。

- [ ] **Step 8: 运行效果契约与构建**

```bash
.venv/bin/python -m pytest tests/test_webui_effect_policy_contracts.py tests/test_frontend_runtime_contracts.py -q
cd web/frontend && npx vue-tsc --noEmit && npm run build
```

Expected: 契约、类型检查和生产构建全部通过。

### Task 7: 全量验证与浏览器验收

**Files:**
- Verify: `web/frontend/src/**`
- Verify: `web/dist/**`
- Verify: `tests/test_webui_*.py`

**Interfaces:**
- Consumes: Tasks 1–6 的所有公开接口。
- Produces: 可交付的构建产物与验证证据。

- [ ] **Step 1: 运行前端和关键后端测试**

```bash
.venv/bin/python -m pytest \
  tests/test_frontend_runtime_contracts.py \
  tests/test_frontend_local_ai_contracts.py \
  tests/test_webui_chat_experience_contracts.py \
  tests/test_webui_chat_request_contract.py \
  tests/test_webui_navigation_contracts.py \
  tests/test_webui_directory_picker_contracts.py \
  tests/test_webui_effect_policy_contracts.py \
  tests/test_local_ai_api.py tests/test_local_ai_instances.py -q
```

Expected: 全部通过，无 FAILED 或 ERROR。

- [ ] **Step 2: 运行类型检查和生产构建**

```bash
cd web/frontend
npx vue-tsc --noEmit
npm run build
```

Expected: 两个命令退出码均为 0，`web/dist/index.html` 与哈希资源生成。

- [ ] **Step 3: 启动或复用 Web 服务**

```bash
setsid nohup .venv/bin/python agent.py --web > /tmp/webui.log 2>&1 < /dev/null &
```

Expected: `http://127.0.0.1:8080/` 可访问，日志无启动异常。

- [ ] **Step 4: 浏览器验证桌面主流程**

验证登录、文本发送、纯图片、纯文档、断线保留草稿、重新连接、完整重试、侧栏悬停/固定、斜杠菜单键盘、目录选择与本地 AI 启动成功/失败。

- [ ] **Step 5: 浏览器验证响应式与减少动态**

在 360、768、1024 和宽屏视口检查菜单抽屉、遮罩、滚动、焦点与内容遮挡；模拟 reduced-motion，确认粒子、拖尾、Tilt3D 和持续定制光标循环均停用。

- [ ] **Step 6: 检查控制台、网络和工作区差异**

```bash
git diff --check
git status --short
```

Expected: 无空白错误；只包含预期修改和原有未提交工作，不出现意外删除、密钥或整文件格式化噪声。
