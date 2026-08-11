from pathlib import Path


ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_websocket_send_reports_delivery_failure():
    ws = source("web/frontend/src/api/ws.ts")
    assert "send(data: Record<string, unknown>): boolean" in ws
    assert "return false" in ws


def test_chat_returns_structured_failure_before_mutating_messages():
    chat = source("web/frontend/src/stores/chat.ts")
    assert "export type ChatSendResult" in chat
    assert "reason: 'DISCONNECTED'" in chat
    assert chat.index("ws.send(payload)") < chat.index("pushMessage(messages", chat.index("function sendMessage"))


def test_sliding_token_renewal_updates_expiry_and_runtime_store():
    api = source("web/frontend/src/api/index.ts")
    auth = source("web/frontend/src/stores/auth.ts")
    assert "X-New-Token-Expiry" in api
    assert "xiaoda-auth-renewed" in api
    assert "xiaoda-auth-renewed" in auth


def test_sliding_token_renewal_reconnects_websocket_with_new_token():
    auth = source("web/frontend/src/stores/auth.ts")
    ws = source("web/frontend/src/api/ws.ts")
    renewed = auth[auth.index("function onAuthRenewed"):auth.index("window.addEventListener")]
    assert "getWsClient().reconnect(detail.token)" in renewed
    assert "reconnect(token: string)" in ws


def test_chat_resend_uses_structured_image_option():
    view = source("web/frontend/src/views/ChatView.vue")
    assert "chat.retryMessage(msg.id)" in view
    assert "[Image:" not in view[view.index("function resend"):view.index("function clearAll")]


def test_mail_autosave_replays_changes_made_during_request():
    view = source("web/frontend/src/views/MailView.vue")
    assert "savePending" in view
    assert "if (savePending)" in view


def test_stale_session_history_cannot_overwrite_active_session():
    chat = source("web/frontend/src/stores/chat.ts")
    assert "loadSessionGeneration" in chat
    assert "generation !== loadSessionGeneration" in chat


def test_terminal_unmount_kills_sessions_and_blocks_delayed_start():
    terminal = source("web/frontend/src/components/chat/ChatTerminal.vue")
    unmount = terminal[terminal.index("onBeforeUnmount(() => {"):terminal.index("// ── 会话管理")]
    mount = terminal[terminal.index("function mountTerminal"):terminal.index("function closeSession")]
    assert "disposed = true" in unmount
    assert "type: 'terminal_kill'" in unmount
    assert "s.alive = false" in unmount
    assert "disposed || !session.alive" in mount


def test_local_deploy_disconnects_websocket_on_unmount():
    view = source("web/frontend/src/views/LocalDeployView.vue")
    mounted_idx = view.index("onMounted(() =>")
    unmount_idx = view.index("onBeforeUnmount(() =>")
    assert "store.connectWebSocket()" in view[mounted_idx:unmount_idx]
    assert "store.disconnectWebSocket()" in view[unmount_idx:]
