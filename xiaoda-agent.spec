# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for xiaoda-agent
Multi-agent AI assistant with QQ bot, web UI, and CLI interfaces.
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SPECPATH = os.path.dirname(os.path.abspath(SPEC))

# ---------------------------------------------------------------------------
# Helper: recursively collect all files under a directory as datas tuples
# ---------------------------------------------------------------------------
def _tree_datas(root, prefix):
    """Return list of (src, dest) tuples for every file under *root*."""
    result = []
    _exclude = {'.env', '.env.prod', '.env.local', 'webui_overrides.json',
                'USER.md', 'SOUL.md', 'IDENTITY.md', 'MEMORY.md',
                'credential_salt.bin'}
    _exclude_dirs = {'credentials', '__pycache__', '.git', 'node_modules',
                     'stickers', 'voice_refs'}
    if not os.path.isdir(root):
        print(f'[spec] WARNING: root dir does not exist: {root}')
        return result
    walk_count = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        # 跳过排除的目录
        _dirnames[:] = [d for d in _dirnames if d not in _exclude_dirs]
        for fn in filenames:
            if fn in _exclude:
                continue
            if fn.endswith(('.key', '.secret', '.part')):
                continue
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, SPECPATH)
            result.append((src, os.path.dirname(rel)))
            walk_count += 1
    print(f'[spec] _tree_datas({root!r}) found {walk_count} files')
    return result


# ---------------------------------------------------------------------------
# Data files to bundle
# ---------------------------------------------------------------------------
datas = []

# config/ directory (agent.json5, agents/*.json, agents/*.md, workspace/*.md)
datas += _tree_datas(os.path.join(SPECPATH, 'config'), 'config')

# web/dist/ directory (pre-built Vue frontend)
# 使用 _tree_datas 确保所有前端文件被正确打包
datas += _tree_datas(os.path.join(SPECPATH, 'web', 'dist'), os.path.join('web', 'dist'))

# web/splash/ directory (desktop mode splash screen)
datas += _tree_datas(os.path.join(SPECPATH, 'web', 'splash'), os.path.join('web', 'splash'))

# web/routers/__init__.py (required for package imports in PyInstaller)
datas.append((os.path.join(SPECPATH, 'web', 'routers', '__init__.py'), os.path.join('web', 'routers')))

# db/schema.sql
datas.append((os.path.join(SPECPATH, 'db', 'schema.sql'), 'db'))

# .env.example
datas.append((os.path.join(SPECPATH, '.env.example'), '.'))

# .version / .auto_update (runtime version display & auto-update flag)
for _vfile in ('.version', '.auto_update'):
    _vpath = os.path.join(SPECPATH, _vfile)
    if os.path.isfile(_vpath):
        datas.append((_vpath, '.'))

# web/media/stickers/ (runtime cache, populated by StickerManager)

# assets/ directory (icons and other resources)
datas += _tree_datas(os.path.join(SPECPATH, 'assets'), 'assets')

# 本地向量模型（bge-small-zh-v1.5）权重 ONNX（~95MB）不再随安装包内置：
# 检索默认走 SiliconFlow 远程 API，本地兜底模型由 scripts 手动下载到
# data 目录后通过 WebUI「本地部署」注册（见 docs/repo_hygiene_notes.md 方案）。
# 故此处不再收集 models/bge-small-zh-v1.5/onnx 权重；
# onnxruntime/tokenizers 运行库仍保留（见下），供用户自行放置模型后本地兜底。

# Windows launch scripts (bundled by CI, but also declare here for local builds)
# 清单与 .github/workflows/build-release.yml 和 scripts/build-release.sh 保持一致
for _script in ('xiaoda.bat', 'auto-update.bat', 'auto-update.ps1', 'open-browser.ps1', 'doctor.bat'):
    _script_path = os.path.join(SPECPATH, 'scripts', _script)
    if os.path.isfile(_script_path):
        datas.append((_script_path, '.'))



# ---------------------------------------------------------------------------
# Collect data files from packages that ship non-Python assets
# onnxruntime/tokenizers 显式收集（不依赖 hooks-contrib 自动 hook 兜底）：
# - onnxruntime: capi DLL/SO 必须进包，否则本地 embed 在打包版静默降级远程 API
# - tokenizers: Rust 扩展（tokenizers*.pyd/.so）+ 少量数据文件
# ---------------------------------------------------------------------------
for pkg in ('jieba', 'psutil', 'certifi', 'openai', 'PIL', 'sqlite_vec', 'webview',
            'onnxruntime', 'onnxruntime_genai', 'tokenizers'):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
hiddenimports = [
    # Core dependencies
    'aiosqlite',
    'dotenv',
    'httpx',
    'loguru',
    'openai',
    'pydantic',
    'jieba',
    # 本地 embedding 运行时（local_embed.py 顶层 import；显式声明双保险）
    'onnxruntime',
    'onnxruntime.capi',
    'onnxruntime_genai',
    'tokenizers',
    'pilk',
    'yaml',
    'pdfplumber',
    'docx',
    'pptx',
    'openpyxl',
    'html2text',
    'lxml.html',

    # HTTP/2 支持（httpx[http2] extra 依赖）
    # 启用 http2=True 时 httpx 内部 import h2，PyInstaller 静态分析无法
    # 检测到这个条件导入，需显式声明。h2 依赖 hpack + hyperframe。
    # 未安装时 utils/http_pool.py 会优雅降级为 HTTP/1.1，但生产环境
    # 应包含以启用 HTTP/2 多路复用（降低高频 HTTP 调用尾延迟）。
    'h2',
    'hpack',
    'hyperframe',

    # Uvicorn internals (often missed by static analysis)
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',

    # Web framework
    'starlette',
    'anyio',

    # QQ bot SDK
    'qq_botpy',
    'botpy',

    # Search
    'duckduckgo_search',

    # SQLite extensions
    'sqlite_vec',
    'webview',
    'webview.guilib',
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'webview.util',
    'webview.screen',
    'webview.menu',
    'webview.window',
    'webview.events',

    'psutil',

    # Project sub-packages (ensure PyInstaller picks them up)
    'core',
    'core.background_tasks',
    'core.bootstrap',
    'core.chat_processor',
    'core.delegation',
    'core.jieba_prewarm',
    'core.router_engine',
    'core.tool_orchestrator',
    'db',
    'db.database',
    'db.db_analytics',
    'db.db_concept',
    'db.db_kg_v2',
    'db.db_knowledge',
    'db.db_learning',
    'db.db_memory',
    'db.db_notebook',
    'db.db_temporal_memory',
    'db.fts_utils',
    'db.index_manager',
    'db.repair_migration',
    'db.session_store',
    'emotion',
    'emotion.emoji_config',
    'emotion.emotion_enum',
    'emotion.emotion_simple',
    'emotion.nudge_engine',
    'emotion.portrait_manager',
    'emotion.sticker_manager',
    'emotion.tts_engine',
    'memory',
    # memory sub-modules
    'memory.context_governance',
    'memory.context_compressor',
    'memory.context_usage',
    'memory.emotional_memory',
    'memory.episodic_limiter',
    'memory.knowledge_graph',
    'memory.learning_manager',
    'memory.memory_distiller',
    'memory.memory_manager',
    'memory.notebook_manager',
    'memory.ontology_complexity',
    'memory.query_transform',
    'memory.recall_scheduler',
    'memory.reranker',
    'memory.vector_store',
    # memory v0.5 新模块
    'memory.bridge_memory',
    'memory.cognitive_memory',
    'memory.concept_graph',
    'memory.confirm_correct',
    'memory.entity_extractor',
    'memory.entity_store',
    'memory.fsrs_model',
    'memory.hopfield_layer',
    'memory.key_extractor',
    'memory.kg_search',
    'memory.knowledge_graph_v2',
    'memory.preference_discovery',
    'memory.query_cache',
    'plugins',
    'plugins.context',
    'plugins.discovery',
    'plugins.echo.echo_plugin',
    'plugins.manager',
    'plugins.manifest',
    'plugins.permissions',
    'plugins.sdk',
    'plugins.testing',
    'security',
    'security.permission_manager',
    'security.sandbox_config',
    'security.security',
    'tool_engine',
    'tool_engine.mcp_client',
    'tool_engine.tool_call_handler',
    'tool_engine.tool_executor',
    'tool_engine.tool_guardrails',
    'tool_engine.tool_registry',
    'tool_engine.tool_repair',
    'tools',
    'tools._builtin_manifest',
    'tools.agnes_tools',
    'tools.code_tools_v2',
    'tools.document_tools',
    'tools.file_tools_v2',
    'tools.mail_tools',
    'tools.memory_tool',
    'tools.multi_search_tools',
    'tools.nudge_tool',
    'tools.secrets_tool',
    'tools.system_tools',
    'tools.vision_tools',
    'tools.web_browse_tools',
    'tools.web_browse_enhanced',
    'tools.web_tools_v2',
    'tools.domestic_search_tools',
    'transports',
    'transports.agnes_transport',
    'transports.base',
    'transports.mimo_transport',
    'utils',
    'utils.atomic_write',
    'utils.credential_pool',
    'utils.error_classifier',
    'utils.file_receiver',
    'utils.lazy_deps',
    'utils.logging_config',
    'utils.metrics',
    'utils.xiaoda_acp',
    'utils.npu_inference',
    'utils.prompt_caching',
    'utils.result_wrapper',
    'utils.smart_error_handler',
    'utils.text_utils',
    'utils.vision_service',
    'web',
    'web.agent_registry',
    'web.app',
    'web.config_service',
    'web.custom_providers',
    'web.greeting_scheduler',
    'web.mail_poller',
    'web.media_tasks',
    'web.probes',
    'web.schemas',
    'web.server',
    'web.tool_events',
    'web.ws_hub',
    'web.routers',
    'web.routers.agents',
    'web.routers.auth',
    'web.routers.chat',
    'web.routers.health',
    'web.routers.insight',
    'web.routers.mail_manage',
    'web.routers.mcp',
    'web.routers.media',
    'web.routers.models',
    'web.routers.plugins',
    'web.routers.schedule',
    'web.routers.system',
    'web.routers.tools',
    'web.routers.setup',
    'web.routers.model_discovery',
    'web.routers.workflows',
    'web.routers.market',
    'web.model_capabilities',
    'web.pty_executor',
    'web._msg_context',
    'setup_wizard',
    'qq_bot_adapter',
    'cli_client',
    'market',
    'market.installer',
    'market.manifest',

    # Top-level modules imported by agent_core.py (imported in web.server lifespan)
    'model_router',
    'agent_context',
    'slash_commands',
    'xiaoli_agent',
    'agent_dispatcher',
    'task_orchestrator',
    'instinct_manager',
    'belief_router',
    'hooks',
    'config',
    'prompt_builder',
    'cli',

    # agent_core package (delayed __getattr__ imports — invisible to PyInstaller)
    'agent_core',
    'agent_core._shared',
    'agent_core.core',
    'agent_core.message_processor',
    'agent_core.shared_blackboard',
    'agent_core.structured_blackboard',
    'agent_core.sub_agent_manager',
    'agent_core.tool_executor_mixin',
    'agent_core.user_base',
    'agent_core.user_cli',
    'agent_core.user_qq',
    'agent_core.user_web',

    # core sub-modules (runtime imports)
    'core.agent_introspection',
    'core.agent_r_reflection',
    'core.agent_work_record',
    'core.app_exception',
    'core.behavioral_health',
    'core.behavioral_direction',
    'core.behavioral_signal',
    'core.cancel_token',
    'core.capability_detector',
    'core.circuit_breaker',
    'core.constraint_injector',
    'core.degradation',
    'core.degradation_detector',
    'core.degradation_strategy',
    'core.doctor',
    'core.dream_consolidation',
    'core.dream_engine_v2',
    'core.enhanced_router',
    'core.event_bus',
    'core.intervention_loop',
    'core.conflict_supersession',
    'core.tnr_self_heal',
    'core.error_codes',
    'core.failure_trigger',
    'core.growth_narrative',
    'core.lazy_loader',
    'core.learning_feedback',
    'core.learning_loop',
    'core.mental_state',
    'core.message',
    'core.meta_cognition',
    'core.metacognition_lite',
    'core.parallel_dag',
    'core.permanent_memory',
    'core.persona_coherence',
    'core.preference_pipeline',
    'core.preference_validator',
    'core.recovery_orchestrator',
    'core.risk_classifier',
    'core.self_diagnostic',
    'core.self_model',
    'core.sla_exporter',
    'core.slo_tracker',
    'core.spontaneous_recall',
    'core.user_profile_learner',
    'core.xp_system',
    'core.zombie_detector',

    # memory sub-modules
    'memory.emotional_memory',
    'memory.query_transform',
    'memory.recall_scheduler',
    'memory.reranker',

    # security sub-modules
    'security.anomaly_detector',
    'security.canary',
    'security.credential_vault',
    'security.human_approval',
    'security.instruction_hierarchy',
    'security.secrets_broker',
    'security.ssrf_guard',

    # doctor sub-modules
    'doctor.behavioral_health',

    # quality sub-modules
    'quality.triple_axis_degradation',

    # tool_engine sub-modules
    'tool_engine.error_rule_pipeline',

    # utils sub-modules
    'utils.async_compat',
    'utils.canary_guard',
    'utils.encrypted_credential',
    'utils.env_reader',
    'utils.llm_cleanup',
    'utils.watchdog_runner',

    # web sub-modules
    'web._app_ref',
    'web._discovery_cache',
    'web._provider_keys',
    'web.error_handler',
    'web.middleware.rate_limit',

    # CLI 交互输入（prompt_toolkit 动态 import，需显式声明）
    'prompt_toolkit',
    'prompt_toolkit.application',
    'prompt_toolkit.auto_suggest',
    'prompt_toolkit.completion',
    'prompt_toolkit.filters',
    'prompt_toolkit.formatted_text',
    'prompt_toolkit.history',
    'prompt_toolkit.key_binding',
    'prompt_toolkit.layout',
    'prompt_toolkit.layout.controls',
    'prompt_toolkit.styles',

    # CLI 交互式斜杠命令面板/菜单（cli.py 中为 try/except 守卫导入，
    # PyInstaller 静态分析可能漏掉，需显式声明——缺失会导致打包版 CLI
    # 回退到基础输入，命令面板/滚动全部失效）
    'cli_palette',
    'cli_menu',
]

# Collect any sub-modules that static analysis might miss
for pkg in ('openai', 'pydantic', 'starlette', 'anyio', 'uvicorn', 'psutil', 'httpx', 'certifi', 'httpcore', 'pilk', 'PIL', 'webview', 'h2', 'hpack', 'hyperframe', 'prompt_toolkit'):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

try:
    hiddenimports += collect_submodules('onnxruntime_genai')
except Exception:
    pass

try:
    hiddenimports += collect_submodules('local_ai')
except Exception:
    pass

try:
    hiddenimports += collect_submodules('llm_gateway')
except Exception:
    pass

# 确保 pilk 的 C 扩展二进制（_pilk.so/.pyd）被正确打包
# pilk 在 try/except 中懒加载，PyInstaller 静态分析容易漏掉 C 扩展
# onnxruntime: capi 下的 onnxruntime.dll/.so 用 collect_dynamic_libs 强制进 binaries
#              （仅 collect_data_files 可能被当 data 处理，加载路径不一致导致失败）
# tokenizers: Rust 扩展二进制（tokenizers*.pyd/.so）双保险收集
binaries = []
# 动态壁纸转码：vendored ffmpeg 静态构建（scripts/fetch_ffmpeg.py 预先下载）
_vendored_ffmpeg_dir = os.path.join(SPECPATH, 'vendor', 'ffmpeg')
for _plat_dir in ('win32-x64',):
    for _exe in ('ffmpeg.exe', 'ffprobe.exe'):
        _p = os.path.join(_vendored_ffmpeg_dir, _plat_dir, _exe)
        if os.path.isfile(_p):
            binaries.append((_p, os.path.join('ffmpeg', _plat_dir)))
from PyInstaller.utils.hooks import collect_dynamic_libs
for pkg in ('pilk', 'sqlite_vec', 'onnxruntime', 'onnxruntime_genai', 'tokenizers'):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Excludes – trim the bundle by removing unused heavy modules
# ---------------------------------------------------------------------------
excludes = [
    'tkinter',
    '_tkinter',
    'Tkinter',
    'tcl',
    'tk',
    'curses',
    'pdb',
    'pydoc',
    'doctest',
    'unittest',
    'test',
    'tests',
    # 注意：不要 exclude 'setuptools'。pkg_resources 是 setuptools 的一部分，
    # PyInstaller 的 pyi_rth_pkgres.py runtime hook 在启动时自动执行 import pkg_resources，
    # 新版 pkg_resources 依赖 jaraco 模块。exclude setuptools 会导致 jaraco 缺失，
    # frozen exe 启动时 ModuleNotFoundError: No module named 'jaraco'（CI Linux 复现）。
    'pip',
    # 注意：不要 exclude 'wheel'。PyInstaller 的 setuptools hook 会给
    # setuptools._vendor.wheel 建别名到 wheel，若 wheel 被 exclude 会抛
    # ValueError: Target module "wheel" already imported as "ExcludedModule('wheel',)"
    'distutils',
    'lib2to3',
    'xmlrpc',
    'py_compile',
    'compileall',
    'win32com',
    'pythoncom',
    'pywin',
]

# ---------------------------------------------------------------------------
# 产物身份元数据 — BUILD_INFO.json 注入构建时可追溯信息
#   必须在 Analysis() 之前生成并 append 到 datas，否则 a.datas 不会包含它，
#   COLLECT(a.datas) 会漏掉这个文件（CodeRabbit 审查发现）。
#   build_date 用 git commit 时间戳（SOURCE_DATE_EPOCH 优先）而非 datetime.now，
#   保证同一 commit 可复现构建产生相同 BUILD_INFO.json（CodeRabbit 审查发现）。
# ---------------------------------------------------------------------------
import subprocess as _sp
import datetime as _dt

def _git(cmd):
    try:
        return _sp.check_output(cmd, cwd=SPECPATH, stderr=_sp.DEVNULL,
                                timeout=5).decode().strip()
    except Exception:
        return 'unknown'

# 可复现构建：优先 SOURCE_DATE_EPOCH，其次 git commit 时间戳
# P2-7: 移除 now() fallback —— 非可重现字段不应混入 BUILD_INFO.json
def _deterministic_build_date():
    sde = os.environ.get('SOURCE_DATE_EPOCH')
    if sde:
        try:
            return _dt.datetime.fromtimestamp(int(sde), tz=_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        except (ValueError, OSError):
            pass
    commit_ts = _git(['git', 'log', '-1', '--format=%ct'])
    if commit_ts and commit_ts.isdigit():
        return _dt.datetime.fromtimestamp(int(commit_ts), tz=_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    # P2-7: 不再 fallback 到 now() —— 同一 commit 应产生相同 BUILD_INFO.json
    # 非 git 环境返回 'unknown' 而非当前时间，标记为不可复现
    return 'unknown'

_build_info = {
    # P2-7: 仅保留 commit 链可重现字段
    # 移除 branch/python/platform（环境相关，影响可复现性）
    # 移除 git describe 的 version（依赖本地 tag，不同机器可能不同）
    'commit': _git(['git', 'rev-parse', '--short', 'HEAD']),
    'commit_hash': _git(['git', 'rev-parse', 'HEAD']),
    'build_date': _deterministic_build_date(),
}
_bi_path = os.path.join(SPECPATH, 'BUILD_INFO.json')
with open(_bi_path, 'w', encoding='utf-8') as _f:
    import json as _json
    _json.dump(_build_info, _f, indent=2, ensure_ascii=False)
datas.append((_bi_path, '.'))
print(f'[spec] BUILD_INFO.json: {_build_info}')

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [os.path.join(SPECPATH, 'agent.py')],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # optimize=1: 移除 assert 语句（启动加速 ~5-10%），保留 docstring
    # 不用 optimize=2 是因为 FastAPI/Pydantic 的 OpenAPI 文档依赖 docstring
    optimize=1,
)

# ---------------------------------------------------------------------------
# PYZ – compressed Python modules archive
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# EXE – the main executable
#   - upx=False: UPX 压缩会触发 Windows Defender 误报（PyInstaller 官方已知问题），
#     且现代磁盘空间充裕，不值得为 ~30% 体积节省牺牲杀软兼容性
#   - console=True: 保留控制台以支持 --web (uvicorn 日志)、doctor (stdout)、
#     CLI 模式。--desktop 桌面模式在 agent.py::_run_desktop 中用 Windows API
#     隐藏控制台窗口，避免双击快捷方式时弹出黑窗。
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='xiaoda-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(SPECPATH, 'assets', 'xiaoda-icon.ico'),
)

# ---------------------------------------------------------------------------
# COLLECT – onedir bundle (all files in one folder for data file compatibility)
#   upx=False: 同上，避免杀软误报
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='xiaoda-agent',
)
