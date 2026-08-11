import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).parents[1]


def source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


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


def test_local_ai_store_uses_generation_safe_loads():
    store = source("web/frontend/src/stores/localAi.ts")
    assert "let loadGeneration = 0" in store
    assert "const generation = ++loadGeneration" in store
    assert "generation !== loadGeneration" in store


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
        [str(frontend / "node_modules/.bin/esbuild"), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
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
        [str(frontend / "node_modules/.bin/esbuild"), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}"],
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


def test_local_deploy_has_five_tabs_and_no_fixed_device_copy():
    view = source("web/frontend/src/views/LocalDeployView.vue")
    for name in ("部署", "模型市场", "已安装", "算力设备", "下载任务"):
        assert name in view
    for component in (
        "DeploymentsTab",
        "ModelMarketTab",
        "InstalledModelsTab",
        "ComputeDevicesTab",
        "DownloadTasksTab",
    ):
        assert component in view
    assert "Vivante VIP9000" not in view
    assert "3 TOPS INT8" not in view


def test_local_ai_tabs_consume_store_without_raw_http():
    component_paths = (
        "web/frontend/src/components/local-ai/DeploymentsTab.vue",
        "web/frontend/src/components/local-ai/ModelMarketTab.vue",
        "web/frontend/src/components/local-ai/InstalledModelsTab.vue",
        "web/frontend/src/components/local-ai/ComputeDevicesTab.vue",
        "web/frontend/src/components/local-ai/DownloadTasksTab.vue",
        "web/frontend/src/components/local-ai/ModelDetailDrawer.vue",
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
    detail = source("web/frontend/src/components/local-ai/ModelDetailDrawer.vue")

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
    assert "store.download" in detail


def test_storage_picker_resolves_directory_entries_from_current_path():
    storage = source("web/frontend/src/components/local-ai/StoragePickerDialog.vue")

    assert "function resolveEntryPath" in storage
    assert "path.value.endsWith('\\\\')" in storage
    assert "path.value.endsWith('/')" in storage
    assert "browse(resolveEntryPath(entry))" in storage


def test_local_deploy_summary_is_derived_from_store_resources():
    view = source("web/frontend/src/views/LocalDeployView.vue")
    assert "useLocalAiStore" in view
    assert "store.instances" in view
    assert "store.models" in view
    assert "store.devices" in view
    assert "store.downloads" in view
    assert "localDeployView.subtitle" not in view
