import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def function_source(source_text: str, name: str, next_name: str) -> str:
    start = source_text.index(f"async function {name}")
    end = source_text.index(f"function {next_name}", start)
    return source_text[start:end]


def run_frontend_node(tmp_path: Path, name: str, script: str) -> None:
    frontend = ROOT / "web/frontend"
    esbuild = frontend / "node_modules/.bin/esbuild"
    if sys.platform == "win32":
        esbuild = esbuild.with_suffix(".cmd")
    entry = tmp_path / f"{name}.ts"
    bundle = tmp_path / f"{name}.mjs"
    (tmp_path / "node_modules").symlink_to(
        frontend / "node_modules", target_is_directory=True,
    )
    entry.write_text(textwrap.dedent(script), encoding="utf-8")
    subprocess.run(
        [
            str(esbuild), str(entry), "--bundle", "--platform=node",
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


def test_prompt_image_paste_is_scoped_to_chat_textarea():
    prompt = source("web/frontend/src/components/chat/PromptInput.vue")
    assert '@paste="onPaste"' in prompt
    assert "document.addEventListener('paste'" not in prompt
    assert "document.removeEventListener('paste'" not in prompt
    handler = prompt[prompt.index("async function onPaste"):prompt.index("// 外部 modelValue")]
    assert handler.index("if (!hit.found) return") < handler.index("e.preventDefault()")


def test_prompt_uploads_ignore_stale_responses_after_replacement_or_cleanup():
    uploads = source(
        "web/frontend/src/components/chat/prompt-input/usePromptUploads.ts"
    )
    upload = uploads[
        uploads.index("async function uploadFile"):
        uploads.index("function removeImage")
    ]

    assert "let uploadGeneration = 0" in uploads
    assert "const generation = nextUploadGeneration()" in upload
    assert upload.count("generation !== uploadGeneration") >= 2

    for signature in (
        "function removeImage()",
        "function removeDoc()",
        "function resetAttachments()",
        "onBeforeUnmount(() => {",
    ):
        start = uploads.index(signature)
        body = uploads[start:uploads.index("}", start) + 1]
        assert "invalidateUpload()" in body, signature


def test_prompt_upload_generation_at_runtime(tmp_path):
    run_frontend_node(
        tmp_path,
        "prompt-upload-generation",
        f"""
        globalThis.localStorage = {{
          getItem: () => null, setItem: () => {{}}, removeItem: () => {{}},
        }}

        const {{ createRenderer, defineComponent, ref }} = await import('vue')
        const {{ api }} = await import({str(ROOT / 'web/frontend/src/api/index.ts')!r})
        const {{ usePromptUploads }} = await import(
          {str(ROOT / 'web/frontend/src/components/chat/prompt-input/usePromptUploads.ts')!r}
        )

        const pending = new Map()
        const defer = file => new Promise((resolve, reject) => {{
          pending.set(file.name, {{ resolve, reject }})
        }})
        api.uploadImage = defer
        api.uploadDoc = defer
        URL.createObjectURL = file => `blob:${{file.name}}`
        URL.revokeObjectURL = () => {{}}

        const renderer = createRenderer({{
          patchProp() {{}}, insert() {{}}, remove() {{}},
          createElement() {{ return {{}} }}, createText() {{ return {{}} }},
          createComment() {{ return {{}} }}, setText() {{}}, setElementText() {{}},
          parentNode() {{ return null }}, nextSibling() {{ return null }},
          querySelector() {{ return null }}, setScopeId() {{}},
          cloneNode(node) {{ return node }}, insertStaticContent() {{ return [{{}}, {{}}] }},
        }})
        let uploads
        const app = renderer.createApp(defineComponent({{
          setup() {{
            uploads = usePromptUploads(ref(''))
            return () => null
          }},
        }}))
        app.mount({{}})
        const doc = name => ({{
          url: `/uploads/${{name}}`, name, path: `/tmp/${{name}}`, ext: '.txt',
        }})

        const a = uploads.uploadFile(new File(['a'], 'A.png', {{ type: 'image/png' }}))
        const b = uploads.uploadFile(new File(['b'], 'B.txt', {{ type: 'text/plain' }}))
        pending.get('B.txt').resolve(doc('B.txt'))
        await b
        pending.get('A.png').resolve({{ url: '/uploads/A.png', name: 'A.png' }})
        await a
        if (uploads.uploadedImage.value !== null || uploads.uploadedDoc.value?.name !== 'B.txt') {{
          throw new Error('迟到 A 覆盖或复活了 B')
        }}

        const oldFailure = uploads.uploadFile(new File(['c'], 'C.txt', {{ type: 'text/plain' }}))
        const replacement = uploads.uploadFile(new File(['d'], 'D.txt', {{ type: 'text/plain' }}))
        pending.get('D.txt').resolve(doc('D.txt'))
        await replacement
        pending.get('C.txt').reject(new Error('late failure'))
        await oldFailure
        if (uploads.uploadedDoc.value?.name !== 'D.txt' || uploads.uploadState.value !== 'idle') {{
          throw new Error('迟到失败清空了新附件或覆盖上传状态')
        }}

        const removed = uploads.uploadFile(new File(['e'], 'E.txt', {{ type: 'text/plain' }}))
        uploads.removeDoc()
        pending.get('E.txt').resolve(doc('E.txt'))
        await removed
        if (uploads.uploadedDoc.value !== null || uploads.uploadState.value !== 'idle') {{
          throw new Error('移除后迟到上传复活')
        }}

        const sent = uploads.uploadFile(new File(['f'], 'F.txt', {{ type: 'text/plain' }}))
        uploads.resetAttachments()
        pending.get('F.txt').resolve(doc('F.txt'))
        await sent
        if (uploads.uploadedDoc.value !== null || uploads.uploadState.value !== 'idle') {{
          throw new Error('发送重置后迟到上传复活')
        }}

        const unmounted = uploads.uploadFile(new File(['g'], 'G.txt', {{ type: 'text/plain' }}))
        app.unmount()
        pending.get('G.txt').resolve(doc('G.txt'))
        await unmounted
        if (uploads.uploadedDoc.value !== null || uploads.uploadState.value !== 'idle') {{
          throw new Error('卸载后迟到上传仍落地')
        }}
        """,
    )


def test_directory_picker_only_applies_latest_browse_while_open():
    picker = source(
        "web/frontend/src/components/workspace/DirectoryPickerDialog.vue"
    )
    browse = picker[
        picker.index("async function browse"):
        picker.index("watch(() => props.show")
    ]

    assert "let browseGeneration = 0" in picker
    assert "const generation = ++browseGeneration" in browse
    assert browse.count("generation !== browseGeneration") >= 2
    assert "generation === browseGeneration" in browse
    assert "browseGeneration += 1" in picker[picker.index("watch(() => props.show"):]


def test_directory_picker_disables_navigation_and_selection_while_loading():
    picker = source(
        "web/frontend/src/components/workspace/DirectoryPickerDialog.vue"
    )

    assert "if (loading.value) return" in picker
    assert picker.count(':disabled="loading"') >= 4


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
