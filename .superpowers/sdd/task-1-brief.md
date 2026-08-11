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

