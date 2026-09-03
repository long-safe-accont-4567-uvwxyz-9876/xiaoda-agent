"""audit-fix-20260829 Task 10：前端六项修复行为契约（静态断言源码模式）。

与 tests/test_frontend_runtime_contracts.py 同惯例：不启动运行时，直接对
前端源码做结构性断言，任一侧实现漂移即红。

覆盖：
① logout() 先 best-effort 调后端 POST /api/v1/auth/logout，再本地清理；
② uploadFile() 消费滑动续签（X-New-Token → 存储 → xiaoda-auth-renewed）
   并复用 401 清理路径（与 request() 共享小函数）；
③ ws.ts 处理器按 socket 实例身份守卫，迟到事件不影响新连接；
④ chat.ts 流式/终态/工具/错误/音频事件按 msg_id 发起时会话过滤；
⑤ KeyAccordion 头部为 button + aria-expanded/aria-controls（键盘可达）；
⑥ setup 保存流捕获 SETUP_TOKEN_REQUIRED 并把 setup_token 放进请求 body。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

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


def _esbuild_bin(frontend: Path) -> str:
    if sys.platform == "win32":
        return str(frontend / "node_modules/.bin/esbuild.cmd")
    return str(frontend / "node_modules/.bin/esbuild")


def run_frontend_node(tmp_path: Path, name: str, script: str) -> None:
    frontend = ROOT / "web/frontend"
    entry = tmp_path / f"{name}.ts"
    bundle = tmp_path / f"{name}.mjs"
    (tmp_path / "node_modules").symlink_to(
        frontend / "node_modules", target_is_directory=True,
    )
    entry.write_text(textwrap.dedent(script), encoding="utf-8")
    subprocess.run(
        [
            _esbuild_bin(frontend), str(entry), "--bundle", "--platform=node",
            "--format=esm", f"--outfile={bundle}",
        ],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["node", str(bundle)], cwd=frontend, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


# ── ① 登出：先通知后端吊销 bearer，本地清理照旧 ──────────────────────


def test_logout_invokes_backend_revocation_before_local_cleanup():
    auth = source("web/frontend/src/stores/auth.ts")
    logout = method_source(auth, "async function logout()")
    assert "/api/v1/auth/logout" in logout
    assert "method: 'POST'" in logout
    assert "Authorization: `Bearer" in logout
    # 必须先发起后端吊销，再清本地 token
    assert logout.index("/api/v1/auth/logout") < logout.index(
        "localStorage.removeItem('token')"
    )
    # 失败忽略：try/catch 包裹，本地清理照旧
    assert "catch" in logout
    assert "localStorage.removeItem('token')" in logout
    assert "getWsClient().disconnect()" in logout


def test_settings_view_logout_navigation_unchanged():
    view = source("web/frontend/src/views/SettingsView.vue")
    logout = method_source(view, "function logout() {")
    assert "auth.logout()" in logout
    assert "router.replace('/login')" in logout


# ── ② 上传消费滑动续签 + 401 清理（与 request 共享路径） ───────────────


def test_upload_file_consumes_sliding_renewal_and_unauthorized():
    api = source("web/frontend/src/api/index.ts")
    renewal = method_source(api, "function consumeAuthRenewal(res: Response): void {")
    assert "X-New-Token" in renewal
    assert "X-New-Token-Expiry" in renewal
    assert "xiaoda-auth-renewed" in renewal
    assert "localStorage.setItem('token', newToken)" in renewal

    upload = method_source(api, "async function uploadFile")
    assert "consumeAuthRenewal(res)" in upload
    assert "handleUnauthorized()" in upload
    assert "res.status === 401" in upload

    # 共享 401 清理路径（request 与 uploadFile 同源，防两处语义漂移）
    unauthorized = method_source(api, "function handleUnauthorized() {")
    assert "localStorage.removeItem('token')" in unauthorized
    assert "location.hash.includes('login')" in unauthorized

    request_body = method_source(api, "async function request<T>(")
    assert "handleUnauthorized()" in request_body
    assert "consumeAuthRenewal(res)" in request_body


# ── ③ WS 处理器按 socket 实例身份守卫 ────────────────────────────────


def test_ws_handlers_guarded_by_socket_identity():
    ws = source("web/frontend/src/api/ws.ts")
    open_fn = method_source(ws, "private _open(token: string) {")
    # 先取 socket 实例再挂处理器
    assert "const socket = token ? new WebSocket(" in open_fn
    assert "this.ws = socket" in open_fn
    # 四类处理器（onopen/onmessage/onclose/onerror）都以身份守卫开头
    assert open_fn.count("if (stale()) return") == 4
    # 守卫函数本体：实例不符即视为陈旧
    stale = open_fn[open_fn.index("const stale ="):open_fn.index("const stale =") + 80]
    assert "this.ws !== socket" in stale
    # 旧 socket 迟到 error 只关闭自身，不操作 this.ws（不能误伤新连接）
    assert "socket.close()" in open_fn
    # 心跳定时器单实例：startHeartbeat 只在 onopen 触发一次
    assert open_fn.count("startHeartbeat()") == 1


def test_ws_reconnect_reuses_cleanup_path_and_timers():
    ws = source("web/frontend/src/api/ws.ts")
    reconnect = method_source(ws, "reconnect(token: string) {")
    assert "disconnect()" in reconnect
    assert "connect(token)" in reconnect
    disconnect = method_source(ws, "  disconnect() {")
    assert "clearTimeout(this.reconnectTimer)" in disconnect
    assert "stopHeartbeat()" in disconnect


# ── ④ 会话切换串流：按 msg_id 发起时会话过滤 ─────────────────────────


def test_chat_stream_events_filtered_by_origin_session():
    chat = source("web/frontend/src/stores/chat.ts")
    # 映射：msg_id → 发起时所在会话
    assert "const msgSessionMap = new Map" in chat
    send = method_source(chat, "function sendMessage(request: ChatRequestSnapshot)")
    assert "recordMsgSession(msgId)" in send
    # 流式/终态/工具/错误/音频回调全部先过滤
    for signature in (
        "onStreamText = (e: WsEvent)",
        "onToolEvent = (e: WsEvent)",
        "onFinal = (e: WsEvent)",
        "onError = (e: WsEvent)",
        "onAudioReady = (e: WsEvent)",
    ):
        handler = method_source(chat, signature)
        assert "inCurrentSession(msgId)" in handler, signature
        assert "return" in handler[handler.index("inCurrentSession(msgId)"):]
    # 丢弃的终态帧若是当前 pending，需顺手收尾避免 isProcessing 卡死
    final = method_source(chat, "onFinal = (e: WsEvent)")
    assert "clearProcessing(msgId)" in final
    # 终态消息完成即移除映射条目
    assert "msgSessionMap.delete(msgId)" in final
    # 映射随会话加载/新建修剪，防无界增长
    session_fns = method_source(chat, "async function newSession()") \
        + method_source(chat, "async function loadSession(sid: string) {")
    assert session_fns.count("pruneMsgSessions()") == 2


def test_chat_terminal_cleanup_is_pending_message_scoped():
    chat = source("web/frontend/src/stores/chat.ts")
    clear = method_source(chat, "function clearProcessing(expectedMsgId?: string)")
    assert "expectedMsgId !== pendingMsgId.value" in clear

    error = method_source(chat, "onError = (e: WsEvent)")
    stale_branch = error[error.index("!inCurrentSession(msgId)"):]
    assert "clearProcessing(msgId)" in stale_branch

    final = method_source(chat, "onFinal = (e: WsEvent)")
    assert final.count("clearProcessing(msgId)") >= 2

    for signature in (
        "async function newSession()",
        "async function loadSession(sid: string) {",
    ):
        switch = method_source(chat, signature)
        assert "switchSession(" in switch


def test_chat_pending_cleanup_across_session_switches_at_runtime(tmp_path):
    run_frontend_node(
        tmp_path,
        "chat-session-pending",
        f"""
        globalThis.localStorage = {{
          getItem: () => null, setItem: () => {{}}, removeItem: () => {{}},
        }}
        globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}

        const {{ createPinia, setActivePinia }} = await import('pinia')
        const {{ api }} = await import({str(ROOT / 'web/frontend/src/api/index.ts')!r})
        const {{ getWsClient }} = await import({str(ROOT / 'web/frontend/src/api/ws.ts')!r})
        const {{ useChatStore }} = await import({str(ROOT / 'web/frontend/src/stores/chat.ts')!r})

        Object.assign(api, {{
          getMessages: async () => [],
          createSession: async () => ({{ session_id: 'created-session' }}),
        }})
        const ws = getWsClient()
        ws.connected = true
        ws.send = () => true
        setActivePinia(createPinia())
        const chat = useChatStore()
        const emit = event => ws.emit(event)
        const request = text => ({{ text, search: false, think: false, attachments: [] }})

        await chat.loadSession('old-session')
        const first = chat.sendMessage(request('first'))
        if (!first.ok || !chat.isProcessing) throw new Error('首条请求未进入 pending')

        // 模拟任一外部会话同步路径：旧请求的 error 必须解锁自己。
        chat.sessionId = 'other-session'
        emit({{ type: 'error', msg_id: first.msgId, code: 'ABORTED', message: 'aborted' }})
        if (chat.isProcessing) throw new Error('旧会话 ABORTED 未清理匹配 pending')

        const second = chat.sendMessage(request('second'))
        if (!second.ok) throw new Error('旧 error 清理后输入仍被锁定')
        await chat.loadSession('new-session')
        if (chat.isProcessing) throw new Error('loadSession 未释放旧 pending')

        const third = chat.sendMessage(request('third'))
        if (!third.ok || !chat.isProcessing) throw new Error('新会话无法发送请求')
        const beforeErrors = chat.messages.length
        emit({{ type: 'error', msg_id: second.msgId, code: 'CHAT_ERROR', message: 'late' }})
        if (!chat.isProcessing) throw new Error('旧请求 error 误清新会话 pending')
        if (chat.messages.length !== beforeErrors) throw new Error('旧请求 error 写入新会话')

        await chat.newSession()
        if (chat.isProcessing) throw new Error('newSession 未释放旧 pending')
        chat.cleanup()
        """,
    )


def test_export_download_uses_shared_auth_and_safe_markdown_filename():
    api = source("web/frontend/src/api/index.ts")
    export = method_source(api, "export async function exportSessionDownload")
    assert "res.status === 401" in export
    assert "handleUnauthorized()" in export
    assert "consumeAuthRenewal(res)" in export
    assert "Content-Disposition" in export
    assert "safeMarkdownDownloadName" in export
    assert ".json`" not in export


def test_export_download_auth_and_filename_at_runtime(tmp_path):
    run_frontend_node(
        tmp_path,
        "export-session-download",
        f"""
        const values = new Map([['token', 'old-token']])
        globalThis.localStorage = {{
          getItem: key => values.get(key) ?? null,
          setItem: (key, value) => values.set(key, String(value)),
          removeItem: key => values.delete(key),
        }}
        globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
        const renewals = []
        globalThis.CustomEvent = class {{
          constructor(type, init) {{ this.type = type; this.detail = init?.detail }}
        }}
        globalThis.window = {{ dispatchEvent: event => renewals.push(event) }}

        const downloads = []
        const anchor = {{
          href: '', download: '', click() {{ downloads.push(this.download) }},
        }}
        globalThis.document = {{
          createElement: tag => tag === 'a' ? anchor : {{}},
          body: {{ appendChild: () => {{}}, removeChild: () => {{}} }},
        }}
        URL.createObjectURL = () => 'blob:test'
        URL.revokeObjectURL = () => {{}}

        const responses = []
        globalThis.fetch = async () => responses.shift()
        const {{ exportSessionDownload }} = await import({str(ROOT / 'web/frontend/src/api/index.ts')!r})

        responses.push(new Response('# transcript', {{
          status: 200,
          headers: {{
            'Content-Type': 'text/markdown; charset=utf-8',
            'Content-Disposition': 'attachment; filename="conversation.md"',
            'X-New-Token': 'renewed-token',
            'X-New-Token-Expiry': '12345',
          }},
        }}))
        await exportSessionDownload('safe-session')
        if (downloads.at(-1) !== 'conversation.md') throw new Error('未采用安全的后端 Markdown 文件名')
        if (values.get('token') !== 'renewed-token' || values.get('expires_at') !== '12345') {{
          throw new Error('导出响应未消费滑动续签')
        }}
        if (renewals.at(-1)?.type !== 'xiaoda-auth-renewed') throw new Error('导出续签未通知运行时')

        responses.push(new Response('# transcript', {{
          status: 200,
          headers: {{
            'Content-Type': 'text/markdown; charset=utf-8',
            'Content-Disposition': 'attachment; filename="../../escape.json"',
          }},
        }}))
        await exportSessionDownload('safe-session')
        if (downloads.at(-1) !== 'safe-session.md') throw new Error('不安全文件名未回退为安全 .md 名称')

        values.set('token', 'expired-token')
        responses.push(new Response('{{}}', {{ status: 401 }}))
        let rejected = false
        try {{
          await exportSessionDownload('safe-session')
        }} catch {{
          rejected = true
        }}
        if (!rejected) throw new Error('401 导出未拒绝')
        if (values.has('token') || location.hash !== '#/login') throw new Error('401 未走统一登录清理路径')
        if (downloads.length !== 2) throw new Error('401 响应触发了下载')
        """,
    )


# ── ⑤ KeyAccordion：头部为 button + aria，键盘可达 ────────────────────


def test_key_accordion_header_is_button_with_aria():
    accordion = source("web/frontend/src/components/setup/KeyAccordion.vue")
    # 头部改为 button（Enter/Space 原生触发 click，无需自实现键盘逻辑）
    assert "<button" in accordion
    assert "type=\"button\"" in accordion
    assert ":aria-expanded=\"isExpanded(item.key)\"" in accordion
    assert ":aria-controls=" in accordion
    assert "@click=\"toggle(item.key)\"" in accordion
    # 展开体有对应 id 供 aria-controls 指向
    assert ":id=\"`key-acc-body-${item.key}`\"" in accordion
    # 视觉行为保留：展开箭头/展开态样式链还在
    assert ":class=\"{ 'arrow-open': isExpanded(item.key) }\"" in accordion
    assert ".accordion-header:hover" in accordion
    assert "focus-visible" in accordion


# ── ⑥ setup 保存流：捕获 SETUP_TOKEN_REQUIRED → body 带 setup_token ──


def test_setup_error_surfaces_code_for_contract_branches():
    api = source("web/frontend/src/api/index.ts")
    request = method_source(api, "async function request<T>(")
    # HTTPException detail {code, message} → Error.code（SETUP_TOKEN_REQUIRED 走此路）
    assert "errCode" in request
    assert "err.code = errCode" in request
    assert "detail" in request


def test_setup_save_captures_setup_token_required_and_resends_token():
    view = source("web/frontend/src/views/SetupWizardView.vue")
    handle = method_source(view, "async function handleSave() {")
    # 捕获错误码分支（按 code 精确判断，不解析本地化文本）
    assert "e?.code === 'SETUP_TOKEN_REQUIRED'" in handle
    assert "setupTokenRequired.value = true" in handle
    # 重试时把 setup_token 放进保存请求 body（与 X-Setup-Token 头二选一）
    assert "extraBody.setup_token = setupToken.value.trim()" in handle
    # 保存成功后清空内存令牌；令牌只存内存，不写入任何持久化存储
    # （注意 handleSave 内既有 localStorage.setItem('xiaoda_disclaimer…')，
    # 断言须只针对 setup_token 相关键）
    assert "setupToken.value = ''" in handle
    assert "localStorage.setItem('setupToken" not in handle
    assert "sessionStorage.setItem('setupToken" not in handle
    # 输入框渲染与提示走字典（硬编码中文棘轮约束下必须走 t()）
    assert "v-model=\"setupToken\"" in view
    assert "t('setupWizard.setupTokenTitle')" in view
    assert "t('setupWizard.setupTokenHint')" in view


def test_setup_token_hint_mentions_secret_file():
    zh = source("web/frontend/src/i18n/zh.ts")
    assert "setup_bootstrap_secret" in zh
    en = source("web/frontend/src/i18n/en.ts")
    assert "setup_bootstrap_secret" in en
