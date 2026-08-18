from pathlib import Path


ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def function_source(source_text: str, name: str, next_name: str) -> str:
    start = source_text.index(f"async function {name}")
    end = source_text.index(f"function {next_name}", start)
    return source_text[start:end]


def test_prompt_keeps_draft_until_parent_confirms_success():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    view = source("web/frontend/src/views/ChatView.vue")
    assert "clearSubmittedDraft" in prompt
    assert "if (!result.ok)" in view
    assert view.index("if (!result.ok)") < view.index("clearSubmittedDraft")


def test_chat_request_snapshot_covers_modes_and_attachments():
    chat = source("web/frontend/src/stores/chat.ts")
    assert "export interface ChatRequestSnapshot" in chat
    assert "attachments: ChatAttachmentSnapshot[]" in chat
    assert "function sendMessage(request: ChatRequestSnapshot): ChatSendResult" in chat
    assert "function retryMessage(messageId: string): ChatSendResult" in chat


def test_prompt_supports_attachment_only_send_and_upload_state():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    assert "const hasAttachment = computed" in prompt
    assert "const hasSendableContent = computed" in prompt
    assert "const canSend = computed" in prompt
    assert "uploadState.value !== 'uploading'" in prompt
    assert "emit('send', request)" in prompt


def test_prompt_exposes_accessible_feedback_and_controls():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    assert 'role="status"' in prompt
    assert 'aria-live="polite"' in prompt
    assert prompt.count(":aria-label=") >= 8
    assert "promptInput.reconnect" in prompt
    assert "getWsClient().retry()" in prompt


def test_websocket_exposes_explicit_retry():
    ws = source("web/frontend/src/api/ws.ts")
    assert "retry(): boolean" in ws
    assert "const token = localStorage.getItem('token')" in ws
    assert "if (!token) return false" in ws


def test_chat_feedback_has_matching_bilingual_copy():
    zh = source("web/frontend/src/i18n/zh.ts")
    en = source("web/frontend/src/i18n/en.ts")
    keys = (
        "uploading", "unsupportedFile", "uploadFailed", "removeDocument",
        "disconnectedDraftKept", "reconnect", "reconnecting",
    )
    for key in keys:
        assert f"{key}:" in zh
        assert f"{key}:" in en


def test_workflow_preview_uses_chat_request_snapshot_signature():
    workflow = source("web/frontend/src/views/WorkflowView.vue")
    preview = function_source(workflow, "testWorkflow", "addNode")
    assert "const sendResult = chatStore.sendMessage({" in preview
    assert "text: result.prompt || JSON.stringify(result)" in preview
    assert "attachments: []" in preview


def test_workflow_preview_stays_put_and_reports_chat_send_failure():
    workflow = source("web/frontend/src/views/WorkflowView.vue")
    zh = source("web/frontend/src/i18n/zh.ts")
    en = source("web/frontend/src/i18n/en.ts")
    preview = function_source(workflow, "testWorkflow", "addNode")
    failure_check = preview.index("if (!sendResult.ok)")
    assert failure_check < preview.index("router.push('/')")
    assert failure_check < preview.index("message.success(t('workflowView.sentToChat'))")
    assert "message.warning(t('workflowView.chatSendFailed'))" in preview[failure_check:]
    assert "chatSendFailed:" in zh
    assert "chatSendFailed:" in en
