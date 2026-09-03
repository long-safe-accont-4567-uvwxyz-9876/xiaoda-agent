import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def method_source(source_text: str, signature: str) -> str:
    start = source_text.index(signature)
    body_start = source_text.index("{", start)
    depth = 0
    for index in range(body_start, len(source_text)):
        if source_text[index] == "{":
            depth += 1
        elif source_text[index] == "}":
            depth -= 1
            if depth == 0:
                return source_text[start:index + 1]
    raise ValueError(f"Unclosed method body: {signature}")


def assert_synchronous_pending_guard(method: str, pending_name: str) -> None:
    guard = f"if ({pending_name}.value) return"
    acquire = f"{pending_name}.value = true"
    release = f"{pending_name}.value = false"
    first_await = method.index("await ")
    assert method.index(guard) < method.index(acquire) < first_await
    assert "finally" in method
    assert method.index(release) > method.index("finally")


def test_websocket_send_reports_delivery_failure():
    ws = source("web/frontend/src/api/ws.ts")
    assert "send(data: Record<string, unknown>): boolean" in ws
    assert "return false" in ws


def test_websocket_send_converts_transport_exceptions_to_failure():
    ws = source("web/frontend/src/api/ws.ts")
    send = method_source(ws, "send(data: Record<string, unknown>): boolean")
    assert "try {" in send
    assert "catch" in send
    assert send.index("this.ws.send") < send.index("catch") < send.rindex("return false")


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
    settings = source("web/frontend/src/composables/useMailSettings.ts")
    assert "useMailSettings" in view
    assert "savePending" in settings
    assert "if (savePending)" in settings


def test_schedule_greeting_submit_is_single_flight_and_retryable():
    view = source("web/frontend/src/views/ScheduleView.vue")
    save = method_source(view, "async function saveGreeting()")
    assert_synchronous_pending_guard(save, "greetingSubmitting")
    assert ':loading="greetingSubmitting"' in view
    assert ':disabled="greetingSubmitting"' in view


def test_insight_crud_submit_is_single_flight_and_retryable():
    crud = source("web/frontend/src/composables/useInsightCrud.ts")
    save = method_source(crud, "async function handleModalOk(form: Record<string, any>)")
    assert_synchronous_pending_guard(save, "crudSubmitting")
    assert "provide(insightCrudSubmittingKey, crudSubmitting)" in crud


def test_insight_crud_modal_disables_actions_while_submitting():
    modal = source("web/frontend/src/components/insight/CrudModal.vue")
    assert "inject(insightCrudSubmittingKey" in modal
    assert ':disabled="submitting"' in modal
    assert ':loading="submitting"' in modal


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


def test_echarts_tooltips_cannot_interpret_persisted_values_as_markup():
    graph = source("web/frontend/src/components/insight/KnowledgeGraphPanel.vue")
    tooltip = graph[graph.index("tooltip: {"):graph.index("series: [{", graph.index("tooltip: {"))]
    for field in ("name", "kind", "source", "relation", "target"):
        assert f"escapeHtmlText(String(p.data.{field}" in tooltip
    assert "renderMode: 'html'" in tooltip
    assert "style=" not in tooltip

    models = source("web/frontend/src/views/ModelsView.vue")
    assert "tooltip: { trigger: 'axis', renderMode: 'richText' }" in models


def test_models_usage_legend_has_explicit_space_above_grid():
    view = source("web/frontend/src/views/ModelsView.vue")
    chart = view[view.index("usageChart.setOption({"):view.index("series,", view.index("usageChart.setOption({"))]
    assert "legend: {" in chart
    assert "top: 0" in chart
    assert "left: 60" in chart
    assert "right: 20" in chart
    assert "height: 28" in chart
    assert "grid: { left: 60, right: 20, top: 52" in chart


def test_rendered_band_scroll_uses_cached_geometry_and_resize_invalidates_it():
    band = source("web/frontend/src/composables/useRenderedBand.ts")
    assert "const onScroll = () => refresh()" in band
    assert "const onScroll = () => refresh(true)" not in band
    assert "const onResize = () => refresh(true)" in band
    assert "new ResizeObserver(() => refresh(true))" in band
    assert "rowResizeObserver.observe(el)" in band
    assert "rowResizeObserver.observe(rows[i])" in band
    assert "rowResizeObserver?.disconnect()" in band

    apply = method_source(band, "function apply()")
    dirty = apply.index("if (dirty)")
    assert dirty < apply.index("measure()")
    assert "measure()" not in apply[:dirty]


@pytest.mark.skipif(sys.platform == "win32", reason="esbuild binary path differs on Windows (.cmd)")
def test_rendered_band_compute_uses_cached_row_heights(tmp_path):
    frontend = ROOT / "web/frontend"
    entry = tmp_path / "rendered-band-contract.ts"
    bundle = tmp_path / "rendered-band-contract.mjs"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            import {{ computeBandRange, escapeHtmlText }} from {str(ROOT / 'web/frontend/src/composables/useRenderedBand.ts')!r}

            const assertRange = (actual, expected, label) => {{
              if (actual[0] !== expected[0] || actual[1] !== expected[1]) {{
                throw new Error(`${{label}}: expected ${{expected}}, got ${{actual}}`)
              }}
            }}
            assertRange(computeBandRange([], [], 0, 100, 0), [-1, -1], 'empty')
            assertRange(computeBandRange([0, 100], [20, 20], 40, 20, 0), [-1, -1], 'gap')
            assertRange(computeBandRange([0, 100, 160], [20, 60, 30], 110, 40, 0), [1, 1], 'measured rows')
            assertRange(computeBandRange([0, 100, 220], [20, 120, 30], 180, 20, 0), [1, 1], 'expanded row')

            const escaped = escapeHtmlText(`<img src=x onerror="alert('x')">&`)
            if (escaped !== '&lt;img src=x onerror=&quot;alert(&#39;x&#39;)&quot;&gt;&amp;') {{
              throw new Error(`unexpected HTML escaping: ${{escaped}}`)
            }}
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(frontend / "node_modules/.bin/esbuild"),
            str(entry),
            "--bundle",
            "--platform=node",
            "--format=esm",
            f"--outfile={bundle}",
        ],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
