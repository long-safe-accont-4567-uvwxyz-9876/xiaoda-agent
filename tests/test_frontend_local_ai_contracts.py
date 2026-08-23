import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _esbuild_bin(frontend: Path) -> str:
    """返回可执行的 esbuild 路径。

    Windows 下 node_modules/.bin/esbuild 是 POSIX shell 脚本，CreateProcess
    无法直接执行（WinError 193）；必须用 .cmd 批处理封装。
    """
    if sys.platform == "win32":
        return str(frontend / "node_modules/.bin/esbuild.cmd")
    return str(frontend / "node_modules/.bin/esbuild")


def test_local_ai_client_exposes_typed_resource_collections_and_actions():
    api = source("web/frontend/src/api/localAi.ts")
    for contract in (
        "ComputeDevice",
        "CatalogModel",
        "InstalledModel",
        "DownloadTask",
        "ModelInstance",
    ):
        assert f"interface {contract}" in api
    for action in (
        "loadDevices",
        "loadCatalog",
        "loadModels",
        "loadDownloads",
        "loadInstances",
        "rescanDevices",
        "createDownload",
        "pauseDownload",
        "resumeDownload",
        "cancelDownload",
        "startInstance",
        "stopInstance",
        "removeModel",
        "browseStorage",
        "validateStorage",
        "loadDefaultStorage",
        "saveDefaultStorage",
    ):
        assert f"{action}:" in api


def test_installed_model_client_contract_exposes_removable():
    api = source("web/frontend/src/api/localAi.ts")
    installed_model = api.split("export interface InstalledModel {", 1)[1].split("}", 1)[0]

    assert "removable: boolean" in installed_model


def test_local_ai_store_normalizes_five_resource_collections():
    store = source("web/frontend/src/stores/localAi.ts")
    for collection in ("devices", "catalog", "models", "downloads", "instances"):
        assert f"const {collection}ById" in store
        assert f"const {collection} = computed" in store


def test_local_ai_store_request_id_falls_back_without_random_uuid(tmp_path):
    entry = tmp_path / "local-ai-request-id.ts"
    bundle = tmp_path / "local-ai-request-id.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            Object.defineProperty(globalThis, 'crypto', {{ value: {{}}, configurable: true }})
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const first = store.createRequestId()
            const second = store.createRequestId()
            if (!first || !second || first === second) {{
              throw new Error('缺少安全上下文时未生成唯一 request ID')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_ai_store_uses_generation_safe_loads():
    store = source("web/frontend/src/stores/localAi.ts")
    assert "let loadGeneration = 0" in store
    assert "const generation = ++loadGeneration" in store
    assert "generation !== loadGeneration" in store


def test_local_ai_store_refreshes_models_when_download_completes(tmp_path):
    entry = tmp_path / "local-ai-completed-download.ts"
    bundle = tmp_path / "local-ai-completed-download.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})
            const {{ getWsClient }} = await import({str(ROOT / 'web/frontend/src/api/ws.ts')!r})

            let modelLoads = 0
            Object.assign(localAiApi, {{
              loadModels: async () => {{
                modelLoads += 1
                return [{{ id: 'installed:model', purpose: 'chat', validation_state: 'valid', removable: true }}]
              }},
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            store.connectWebSocket()
            getWsClient().emit({{
              type: 'local_ai_download_updated',
              download: {{
                id: 'download:one', model_id: 'installed:model', destination: '/models',
                state: 'completed', bytes_downloaded: 1, total_bytes: 1,
              }},
            }})
            await new Promise(resolve => setTimeout(resolve, 0))
            if (modelLoads !== 1 || !store.modelsById['installed:model']) {{
              throw new Error('下载完成后未刷新已安装模型')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_completed_download_model_refresh_cannot_be_overwritten_by_older_load(tmp_path):
    entry = tmp_path / "local-ai-model-refresh-race.ts"
    bundle = tmp_path / "local-ai-model-refresh-race.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})
            const {{ getWsClient }} = await import({str(ROOT / 'web/frontend/src/api/ws.ts')!r})

            const modelLoads = []
            Object.assign(localAiApi, {{
              loadDevices: async () => [],
              loadCatalog: async () => [],
              loadModels: () => new Promise(resolve => modelLoads.push(resolve)),
              loadDownloads: async () => [],
              loadInstances: async () => [],
              loadDefaultStorage: async () => ({{ default_model_root: '/models' }}),
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            store.connectWebSocket()
            const loading = store.load()
            getWsClient().emit({{
              type: 'local_ai_download_updated',
              download: {{
                id: 'download:one', model_id: 'new:model', destination: '/models',
                state: 'completed', bytes_downloaded: 1, total_bytes: 1,
              }},
            }})
            modelLoads[1]([{{ id: 'new:model', purpose: 'chat', validation_state: 'valid', removable: true }}])
            await new Promise(resolve => setTimeout(resolve, 0))
            modelLoads[0]([{{ id: 'old:model', purpose: 'chat', validation_state: 'valid', removable: true }}])
            await loading
            if (!store.modelsById['new:model'] || store.modelsById['old:model']) {{
              throw new Error('较旧 load 快照覆盖了下载完成后的模型刷新')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_completed_download_model_refresh_failure_is_visible(tmp_path):
    entry = tmp_path / "local-ai-model-refresh-error.ts"
    bundle = tmp_path / "local-ai-model-refresh-error.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})
            const {{ getWsClient }} = await import({str(ROOT / 'web/frontend/src/api/ws.ts')!r})

            Object.assign(localAiApi, {{ loadModels: async () => {{ throw new Error('模型刷新失败') }} }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            store.connectWebSocket()
            getWsClient().emit({{
              type: 'local_ai_download_updated',
              download: {{
                id: 'download:one', model_id: 'new:model', destination: '/models',
                state: 'completed', bytes_downloaded: 1, total_bytes: 1,
              }},
            }})
            await new Promise(resolve => setTimeout(resolve, 0))
            if (store.error !== '模型刷新失败') {{
              throw new Error(`模型刷新错误不可见: ${{store.error}}`)
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_model_removed_during_refresh_cannot_be_resurrected(tmp_path):
    entry = tmp_path / "local-ai-refresh-remove-race.ts"
    bundle = tmp_path / "local-ai-refresh-remove-race.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            let resolveModels
            Object.assign(localAiApi, {{
              loadModels: () => new Promise(resolve => {{ resolveModels = resolve }}),
              removeModel: async () => undefined,
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const refreshing = store.refreshModels()
            await store.remove('model:removed')
            resolveModels([
              {{ id: 'model:removed', purpose: 'chat', validation_state: 'valid', removable: true }},
              {{ id: 'model:unrelated', purpose: 'chat', validation_state: 'valid', removable: true }},
            ])
            await refreshing
            if (store.modelsById['model:removed']) {{
              throw new Error('删除期间启动的旧模型刷新使模型复活')
            }}
            if (!store.modelsById['model:unrelated']) {{
              throw new Error('删除模型使同一刷新中的无关新模型丢失')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_model_removed_during_load_cannot_be_resurrected(tmp_path):
    entry = tmp_path / "local-ai-load-remove-race.ts"
    bundle = tmp_path / "local-ai-load-remove-race.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            let resolveModels
            Object.assign(localAiApi, {{
              loadDevices: async () => [],
              loadCatalog: async () => [],
              loadModels: () => new Promise(resolve => {{ resolveModels = resolve }}),
              loadDownloads: async () => [],
              loadInstances: async () => [],
              loadDefaultStorage: async () => ({{ default_model_root: '/models' }}),
              removeModel: async () => undefined,
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const loading = store.load()
            await store.remove('model:removed')
            resolveModels([
              {{ id: 'model:removed', purpose: 'chat', validation_state: 'valid', removable: true }},
              {{ id: 'model:unrelated', purpose: 'chat', validation_state: 'valid', removable: true }},
            ])
            await loading
            if (store.modelsById['model:removed']) {{
              throw new Error('删除期间启动的旧 load 使模型复活')
            }}
            if (!store.modelsById['model:unrelated']) {{
              throw new Error('删除模型使旧 load 中的无关新模型丢失')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_removed_model_can_be_reinstalled_from_newer_snapshot(tmp_path):
    entry = tmp_path / "local-ai-remove-reinstall.ts"
    bundle = tmp_path / "local-ai-remove-reinstall.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            const modelLoads = []
            Object.assign(localAiApi, {{
              loadModels: () => new Promise(resolve => modelLoads.push(resolve)),
              removeModel: async () => undefined,
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const older = store.refreshModels()
            await store.remove('model:same')
            const reinstall = store.refreshModels()
            modelLoads[1]([
              {{ id: 'model:same', purpose: 'chat', validation_state: 'valid', removable: true }},
              {{ id: 'model:unrelated', purpose: 'chat', validation_state: 'valid', removable: true }},
            ])
            await reinstall
            modelLoads[0]([{{ id: 'model:same', purpose: 'chat', validation_state: 'valid', removable: true }}])
            await older
            if (!store.modelsById['model:same']) {{
              throw new Error('删除后较新快照未允许同 ID 模型重新安装')
            }}
            if (!store.modelsById['model:unrelated']) {{
              throw new Error('同 ID 重装使较新快照中的无关模型丢失')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_ai_store_tracks_rescan_loading_independently(tmp_path):
    entry = tmp_path / "local-ai-rescan-loading.ts"
    bundle = tmp_path / "local-ai-rescan-loading.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            let resolveRescan
            Object.assign(localAiApi, {{
              rescanDevices: () => new Promise(resolve => {{ resolveRescan = resolve }}),
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const rescanning = store.rescan()
            if (!store.rescanning || store.loading) {{
              throw new Error('重新扫描未使用独立 loading 状态')
            }}
            resolveRescan([])
            await rescanning
            if (store.rescanning || store.loading) {{
              throw new Error('重新扫描完成后 loading 状态未复位')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_rescan_result_cannot_be_overwritten_by_older_load(tmp_path):
    entry = tmp_path / "local-ai-rescan-load-race.ts"
    bundle = tmp_path / "local-ai-rescan-load-race.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            let resolveLoadDevices
            Object.assign(localAiApi, {{
              loadDevices: () => new Promise(resolve => {{ resolveLoadDevices = resolve }}),
              loadCatalog: async () => [],
              loadModels: async () => [],
              loadDownloads: async () => [],
              loadInstances: async () => [],
              loadDefaultStorage: async () => ({{ default_model_root: '/models' }}),
              rescanDevices: async () => [{{
                id: 'device:one', name: 'rescan-new', kind: 'cpu', architecture: 'x64', state: 'available',
                memory_total: 1, memory_available: 1, backends: [], system: {{}}, evidence: {{}},
              }}],
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const loading = store.load()
            await store.rescan()
            resolveLoadDevices([{{
              id: 'device:one', name: 'load-old', kind: 'cpu', architecture: 'x64', state: 'available',
              memory_total: 1, memory_available: 1, backends: [], system: {{}}, evidence: {{}},
            }}])
            await loading
            if (store.devicesById['device:one']?.name !== 'rescan-new') {{
              throw new Error('较旧 load 快照覆盖了重新扫描结果')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_rescan_loading_settles_when_newer_load_supersedes_result(tmp_path):
    entry = tmp_path / "local-ai-rescan-loading-race.ts"
    bundle = tmp_path / "local-ai-rescan-loading-race.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})

            let resolveRescan
            Object.assign(localAiApi, {{
              loadDevices: async () => [],
              loadCatalog: async () => [],
              loadModels: async () => [],
              loadDownloads: async () => [],
              loadInstances: async () => [],
              loadDefaultStorage: async () => ({{ default_model_root: '/models' }}),
              rescanDevices: () => new Promise(resolve => {{ resolveRescan = resolve }}),
            }})
            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const rescanning = store.rescan()
            await store.load()
            resolveRescan([])
            await rescanning
            if (store.rescanning) {{
              throw new Error('被较新 load 取代后重新扫描状态未复位')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_compute_devices_tab_uses_rescan_loading_state():
    component = source("web/frontend/src/components/local-ai/ComputeDevicesTab.vue")

    assert ':loading="store.rescanning"' in component
    assert ':loading="store.loading"' not in component


def test_local_ai_store_preserves_websocket_updates_during_load():
    store = source("web/frontend/src/stores/localAi.ts")
    assert "let loadingUpdates" in store
    assert "function reconcileSnapshot" in store
    assert "protectedIds.forEach" in store
    for resource in ("devices", "downloads", "instances"):
        assert f"loadingUpdates.{resource}.add" in store
        assert f"loadingUpdates.{resource})" in store


def test_local_ai_store_only_preserves_ids_updated_during_load(tmp_path):
    entry = tmp_path / "local-ai-store-race.ts"
    bundle = tmp_path / "local-ai-store-race.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})
            const {{ getWsClient }} = await import({str(ROOT / 'web/frontend/src/api/ws.ts')!r})

            let resolveDevices
            const devices = new Promise(resolve => {{ resolveDevices = resolve }})
            Object.assign(localAiApi, {{
              loadDevices: () => devices,
              loadCatalog: async () => [],
              loadModels: async () => [],
              loadDownloads: async () => [],
              loadInstances: async () => [],
              loadDefaultStorage: async () => ({{ default_model_root: '/models' }}),
            }})

            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const device = (id, name) => ({{
              id, name, kind: 'cpu', architecture: 'x64', state: 'available',
              memory_total: 1, memory_available: 1, backends: [], system: {{}}, evidence: {{}},
            }})
            store.upsertDevice(device('deleted-by-snapshot', 'old'))
            store.upsertDevice(device('updated-during-load', 'before'))
            store.connectWebSocket()

            const loading = store.load()
            getWsClient().emit({{
              type: 'local_ai_device_updated',
              device: device('updated-during-load', 'websocket'),
            }})
            resolveDevices([device('updated-during-load', 'snapshot')])
            await loading

            if (store.devicesById['updated-during-load']?.name !== 'websocket') {{
              throw new Error('加载期间的 WS 更新被快照覆盖')
            }}
            if ('deleted-by-snapshot' in store.devicesById) {{
              throw new Error('未被 WS 更新的旧实体未按快照删除')
            }}
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_ai_store_preserves_websocket_update_across_overlapping_loads(tmp_path):
    entry = tmp_path / "local-ai-store-overlapping-loads.ts"
    bundle = tmp_path / "local-ai-store-overlapping-loads.mjs"
    frontend = ROOT / "web/frontend"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            const {{ localAiApi }} = await import({str(ROOT / 'web/frontend/src/api/localAi.ts')!r})
            const {{ useLocalAiStore }} = await import({str(ROOT / 'web/frontend/src/stores/localAi.ts')!r})
            const {{ getWsClient }} = await import({str(ROOT / 'web/frontend/src/api/ws.ts')!r})

            const deviceLoads = []
            Object.assign(localAiApi, {{
              loadDevices: () => new Promise(resolve => deviceLoads.push(resolve)),
              loadCatalog: async () => [],
              loadModels: async () => [],
              loadDownloads: async () => [],
              loadInstances: async () => [],
              loadDefaultStorage: async () => ({{ default_model_root: '/models' }}),
            }})

            setActivePinia(createPinia())
            const store = useLocalAiStore()
            const device = name => ({{
              id: 'shared-device', name, kind: 'cpu', architecture: 'x64', state: 'available',
              memory_total: 1, memory_available: 1, backends: [], system: {{}}, evidence: {{}},
            }})
            store.connectWebSocket()

            const loadA = store.load()
            getWsClient().emit({{
              type: 'local_ai_device_updated',
              device: device('websocket'),
            }})
            const loadB = store.load()
            deviceLoads[1]([device('snapshot-b')])
            await loadB
            deviceLoads[0]([device('snapshot-a')])
            await loadA

            if (store.devicesById['shared-device']?.name !== 'websocket') {{
              throw new Error('loadA 与 loadB 的重叠加载窗口丢失了 WS 更新')
            }}
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_ai_store_reconciles_websocket_events():
    store = source("web/frontend/src/stores/localAi.ts")
    assert "local_ai_device_updated" in store
    assert "local_ai_download_updated" in store
    assert "local_ai_instance_updated" in store
    assert "upsertDevice" in store
    assert "upsertDownload" in store
    assert "upsertInstance" in store
    assert "ws.off" in store


def test_websocket_declares_typed_local_ai_events():
    ws = source("web/frontend/src/api/ws.ts")
    assert "LocalAiWsEvent" in ws
    assert "local_ai_device_updated" in ws
    assert "local_ai_download_updated" in ws
    assert "local_ai_instance_updated" in ws


def test_local_deploy_has_six_tabs_and_no_fixed_device_copy():
    # 2026-08-23 i18n 收口：tab 文案改走 t('localDeployView.tab*')，
    # 契约改为断言六个键存在，且 zh 字典值与文档口径（六个中文 tab 名）一致。
    view = source("web/frontend/src/views/LocalDeployView.vue")
    zh = source("web/frontend/src/i18n/zh.ts")
    tab_keys = {
        "tabDeploy": "部署", "tabMarket": "模型广场", "tabInstalled": "已安装",
        "tabDevicesShort": "算力设备", "tabNodes": "功能节点", "tabDownloads": "下载任务",
    }
    for key, name in tab_keys.items():
        assert f"t('localDeployView.{key}')" in view
        assert f"{key}: '{name}'" in zh
    for component in (
        "DeploymentsTab",
        "ModelMarketTab",
        "InstalledModelsTab",
        "ComputeDevicesTab",
        "SystemModelNodesTab",
        "DownloadTasksTab",
    ):
        assert component in view
    assert "Vivante VIP9000" not in view
    assert "3 TOPS INT8" not in view


def test_local_ai_tabs_consume_store_without_raw_http():
    # 契约：状态消费必须走 useLocalAiStore，禁止直连 API 模块
    # （'../../api' 裸客户端与 '../../api/localAi' 类型化模块均禁）。
    # 2026-08-22 曾放宽允许 localAi 模块（当时 ComputeDevicesTab 直连引擎
    # 生命周期接口）；随后组件完成 store 化（新增 loadLocalDeploy* 六个
    # store 动作），恢复原始严格契约。类型统一从 store 再导出入口取。
    component_paths = (
        "web/frontend/src/components/local-ai/DeploymentsTab.vue",
        "web/frontend/src/components/local-ai/ModelMarketTab.vue",
        "web/frontend/src/components/local-ai/InstalledModelsTab.vue",
        "web/frontend/src/components/local-ai/ComputeDevicesTab.vue",
        "web/frontend/src/components/local-ai/SystemModelNodesTab.vue",
        "web/frontend/src/components/local-ai/DownloadTasksTab.vue",
        # ModelDetailDrawer.vue 已删除（2026-08-23 死代码清理，全项目零引用）
        "web/frontend/src/components/local-ai/StoragePickerDialog.vue",
    )
    for path in component_paths:
        component = source(path)
        assert "useLocalAiStore" in component
        assert "from '../../api'" not in component
        assert "from '../../api/localAi'" not in component


def test_local_ai_storage_and_installation_flows_are_explicit():
    market = source("web/frontend/src/components/local-ai/ModelMarketTab.vue")
    storage = source("web/frontend/src/components/local-ai/StoragePickerDialog.vue")
    downloads = source("web/frontend/src/components/local-ai/DownloadTasksTab.vue")

    assert "StoragePickerDialog" in market
    assert "store.defaultStorage" in market
    assert "saveAsDefault" in storage
    assert "store.browseStorage" in storage
    assert "store.validateStorage" in storage
    assert "store.saveDefaultStorage" in storage
    assert "手动输入" in storage
    assert "安装完成" in downloads
    assert "确认启动" in downloads
    assert "store.start" in downloads
    assert "watch(() => store.downloads" in downloads
    assert "completedTask.value = completed.id" in downloads


def test_storage_picker_resolves_directory_entries_from_current_path():
    storage = source("web/frontend/src/components/local-ai/StoragePickerDialog.vue")

    assert "function resolveEntryPath" in storage
    assert "return `/${entry}`" in storage
    assert "return `${entry}\\\\`" in storage
    assert "path.value.endsWith('\\\\')" in storage
    assert "path.value.endsWith('/')" in storage
    assert "browse(resolveEntryPath(entry))" in storage


def test_storage_picker_ignores_stale_browse_response(tmp_path):
    frontend = ROOT / "web/frontend"
    component = source("web/frontend/src/components/local-ai/StoragePickerDialog.vue")
    script = component.split('<script setup lang="ts">', 1)[1].split("</script>", 1)[0]
    script = script.replace("'../../stores/localAi'", repr(str(ROOT / "web/frontend/src/stores/localAi.ts")))
    script = script.replace("const props = defineProps<{ show: boolean; initialPath?: string; requiredBytes?: number }>()", "const props = { show: false, initialPath: '', requiredBytes: 0 }")
    script = script.replace("const emit = defineEmits<{ select: [path: string]; cancel: [] }>()", "const emit = () => undefined")
    script = script.replace("const message = useMessage()", "const message = { warning: () => undefined, error: () => undefined }")
    script = script.replace("async function browse", "export async function browse")
    script += "\nexport { path, entries, loading, store }\n"
    instrumented = tmp_path / "StoragePickerDialog.instrumented.ts"
    entry = tmp_path / "storage-picker-browse-race.ts"
    bundle = tmp_path / "storage-picker-browse-race.mjs"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    instrumented.write_text(script, encoding="utf-8")
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            setActivePinia(createPinia())
            const picker = await import({str(instrumented)!r})
            const browses = []
            picker.store.browseStorage = target => new Promise(resolve => browses.push({{ target, resolve }}))
            const older = picker.browse('/old')
            const newer = picker.browse('/new')
            browses[1].resolve({{ path: '/new', entries: ['new-child'] }})
            await newer
            browses[0].resolve({{ path: '/old', entries: ['old-child'] }})
            await older
            if (picker.path.value !== '/new' || picker.entries.value.join(',') !== 'new-child') {{
              throw new Error('较旧目录浏览响应覆盖了较新浏览结果')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_model_market_validates_saved_default_for_each_download_before_use():
    market = source("web/frontend/src/components/local-ai/ModelMarketTab.vue")

    assert "async function choose" in market
    assert "store.validateStorage(store.defaultStorage, hubBytes.value)" in market
    assert "validation.writable && !validation.error" in market
    assert "showStorage.value = true" in market


def test_model_market_ignores_stale_choose_validation(tmp_path):
    frontend = ROOT / "web/frontend"
    component = source("web/frontend/src/components/local-ai/ModelMarketTab.vue")
    script = component.split('<script setup lang="ts">', 1)[1].split("</script>", 1)[0]
    script = script.replace("'../../stores/localAi'", repr(str(ROOT / "web/frontend/src/stores/localAi.ts")))
    script = script.replace("import StoragePickerDialog from './StoragePickerDialog.vue'", "")
    script = script.replace("const message = useMessage()", "const message = { warning: () => undefined, success: () => undefined, error: () => undefined }")
    script = script.replace("async function choose", "export async function choose")
    script += "\nexport { destination, showStorage, hubInspection, store }\n"
    instrumented = tmp_path / "ModelMarketTab.instrumented.ts"
    entry = tmp_path / "model-market-choose-race.ts"
    bundle = tmp_path / "model-market-choose-race.mjs"
    (tmp_path / "node_modules").symlink_to(frontend / "node_modules", target_is_directory=True)
    instrumented.write_text(script, encoding="utf-8")
    entry.write_text(
        textwrap.dedent(
            f"""
            globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }}
            globalThis.location = {{ protocol: 'http:', host: 'localhost', hash: '' }}
            const {{ createPinia, setActivePinia }} = await import('pinia')
            setActivePinia(createPinia())
            const market = await import({str(instrumented)!r})
            const validations = []
            const downloads = []
            market.store.defaultStorage = '/models'
            market.store.validateStorage = () => new Promise(resolve => validations.push(resolve))
            market.store.downloadHubRepository = async (...args) => {{ downloads.push(args) }}
            market.hubInspection.value = {{
              repository: 'owner/repo', revision: 'abcd123', purpose: 'embedding', runnable: true, state: 'ready',
              files: [{{ path: 'model.onnx', size: 1024, sha256: 'x' }}], missing: [], evidence: {{}},
            }}
            const chooseA = market.choose()
            const chooseB = market.choose()
            validations[1]({{ path: '/models/b', writable: true, error: null, reason: null }})
            await chooseB
            validations[0]({{ path: '/models/a', writable: true, error: null, reason: null }})
            await chooseA
            if (downloads.length !== 1 || downloads[0][2] !== '/models/b') {{
              throw new Error('较旧 choose 校验结果串入了当前模型')
            }}
            process.exit(0)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [_esbuild_bin(frontend), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
        cwd=frontend,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(["node", str(bundle)], cwd=frontend, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_deployments_tab_filters_stopped_instances_from_cards_and_empty_state():
    deployments = source("web/frontend/src/components/local-ai/DeploymentsTab.vue")

    assert "const activeInstances = computed" in deployments
    assert "instance.state !== 'stopped' && instance.state !== 'failed'" in deployments
    assert 'v-for="instance in activeInstances"' in deployments
    assert "!activeInstances.length && !store.models.length" in deployments


def test_local_deploy_summary_is_derived_from_store_resources():
    view = source("web/frontend/src/views/LocalDeployView.vue")
    assert "useLocalAiStore" in view
    assert "store.instances" in view
    assert "store.models" in view
    assert "store.devices" in view
    assert "store.downloads" in view
    assert "localDeployView.subtitle" not in view
