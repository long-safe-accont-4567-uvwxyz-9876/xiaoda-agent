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
            if fn.endswith('.key') or fn.endswith('.secret'):
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
# 使用相对路径让 PyInstaller 自行解析，避免 Windows 路径分隔符问题
datas.append(('web/dist', 'web/dist'))

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

# Windows launch scripts (bundled by CI, but also declare here for local builds)
for _script in ('start-windows.bat', 'auto-update.bat', 'open-browser.ps1', 'doctor.bat'):
    _script_path = os.path.join(SPECPATH, 'scripts', _script)
    if os.path.isfile(_script_path):
        datas.append((_script_path, '.'))



# ---------------------------------------------------------------------------
# Collect data files from packages that ship non-Python assets
# ---------------------------------------------------------------------------
for pkg in ('jieba', 'psutil', 'certifi', 'openai', 'PIL', 'sqlite_vec', 'webview'):
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
    'pilk',
    'yaml',
    'pdfplumber',
    'docx',
    'pptx',
    'openpyxl',
    'html2text',
    'lxml.html',

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
    'sse_starlette',
    'starlette',
    'anyio',

    # QQ bot SDK
    'qq_botpy',
    'botpy',

    # Search
    'duckduckgo_search',

    # SQLite extensions
    'sqlite_vec',
    # v0.5.18 新增依赖
    'structlog',
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
    'db.idempotent_migrator',
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
    'memory.fluid_memory',
    'memory.knowledge_graph',
    'memory.learning_manager',
    'memory.matrix_governance',
    'memory.memory_distiller',
    'memory.memory_manager',
    'memory.notebook_manager',
    'memory.ontology_complexity',
    'memory.prompt_complexity',
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
    'tools.hardware_tools',
    'tools.mail_tools',
    'tools.memory_tool',
    'tools.multi_search_tools',
    'tools.nudge_tool',
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
    'agent_core.shared_blackboard_db',
    'agent_core.structured_blackboard',
    'agent_core.sub_agent_manager',
    'agent_core.tool_executor',
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
    'core.secrets_broker',
    'core.self_diagnostic',
    'core.self_model',
    'core.sla_exporter',
    'core.slo_tracker',
    'core.spontaneous_recall',
    'core.tiered_cache',
    'core.user_profile_learner',
    'core.xp_system',
    'core.zombie_detector',

    # memory sub-modules
    'memory.emotional_memory',
    'memory.fluid_memory',
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
    'utils.instruction_hierarchy',
    'utils.llm_cleanup',
    'utils.ssrf_guard',

    # web sub-modules
    'web._app_ref',
    'web._discovery_cache',
    'web._provider_keys',
    'web.error_handler',
    'web.middleware.rate_limit',
]

# Collect any sub-modules that static analysis might miss
for pkg in ('openai', 'pydantic', 'starlette', 'anyio', 'uvicorn', 'psutil', 'httpx', 'certifi', 'httpcore', 'pilk', 'PIL', 'webview'):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# 确保 pilk 的 C 扩展二进制（_pilk.so/.pyd）被正确打包
# pilk 在 try/except 中懒加载，PyInstaller 静态分析容易漏掉 C 扩展
binaries = []
try:
    from PyInstaller.utils.hooks import collect_dynamic_libs
    binaries += collect_dynamic_libs('pilk')
    binaries += collect_dynamic_libs('sqlite_vec')
except Exception:
    pass

# cryptography 使用 Rust 编译的 _rust.pyd，collect_dynamic_libs 可能漏掉
# 用 collect_all 确保完整打包（包括 _rust.abi3.so/.pyd）
try:
    from PyInstaller.utils.hooks import collect_all
    _cry_datas, _cry_binaries, _cry_hiddenimports = collect_all('cryptography')
    datas += _cry_datas
    binaries += _cry_binaries
    hiddenimports += _cry_hiddenimports
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
    'setuptools',
    'pip',
    'wheel',
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
    optimize=0,
)

# ---------------------------------------------------------------------------
# PYZ – compressed Python modules archive
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure)

# ---------------------------------------------------------------------------
# EXE – the main executable
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
    upx=True,
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
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='xiaoda-agent',
)