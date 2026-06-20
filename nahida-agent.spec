# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for nahida-agent
Multi-agent AI assistant with QQ bot, web UI, and CLI interfaces.
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SPECPATH = os.path.dirname(os.path.abspath(SPEC))  # /home/orangepi/ai-agent

# ---------------------------------------------------------------------------
# Helper: recursively collect all files under a directory as datas tuples
# ---------------------------------------------------------------------------
def _tree_datas(root, prefix):
    """Return list of (src, dest) tuples for every file under *root*."""
    result = []
    _exclude = {'.env', '.env.prod', '.env.local', 'webui_overrides.json',
                'USER.md', 'SOUL.md', 'IDENTITY.md', 'MEMORY.md'}
    _exclude_dirs = {'credentials', '__pycache__', '.git', 'node_modules'}
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
datas += _tree_datas(os.path.join(SPECPATH, 'web', 'dist'), os.path.join('web', 'dist'))

# web/routers/__init__.py (required for package imports in PyInstaller)
datas.append((os.path.join(SPECPATH, 'web', 'routers', '__init__.py'), os.path.join('web', 'routers')))

# db/schema.sql
datas.append((os.path.join(SPECPATH, 'db', 'schema.sql'), 'db'))

# .env.example
datas.append((os.path.join(SPECPATH, '.env.example'), '.'))

# web/media/stickers/ (runtime cache, populated by StickerManager)

# assets/ directory (icons and other resources)
datas += _tree_datas(os.path.join(SPECPATH, 'assets'), 'assets')

# Debug: print summary of datas
print(f'[spec] SPECPATH = {SPECPATH}')
print(f'[spec] Total datas entries: {len(datas)}')
assets_datas = [d for d in datas if 'assets' in d[1] or 'assets' in d[0]]
print(f'[spec] Datas entries containing "assets": {len(assets_datas)}')
for i, (src, dst) in enumerate(assets_datas[:5]):
    print(f'[spec]   assets[{i}]: src={src!r} dest={dst!r}')
if len(assets_datas) > 5:
    print(f'[spec]   ... and {len(assets_datas) - 5} more')

# ---------------------------------------------------------------------------
# Collect data files from packages that ship non-Python assets
# ---------------------------------------------------------------------------
for pkg in ('jieba',):
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
    'pdfplumber',
    'docx',
    'pptx',
    'openpyxl',
    'html2text',

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

    # Project sub-packages (ensure PyInstaller picks them up)
    'core',
    'core.background_tasks',
    'core.bootstrap',
    'core.chat_processor',
    'core.delegation',
    'core.router_engine',
    'core.tool_orchestrator',
    'db',
    'db.database',
    'db.db_analytics',
    'db.db_knowledge',
    'db.db_learning',
    'db.db_memory',
    'db.db_notebook',
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
    'memory.context_compressor',
    'memory.context_usage',
    'memory.knowledge_graph',
    'memory.learning_manager',
    'memory.memory_manager',
    'memory.notebook_manager',
    'memory.vector_store',
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
    'tools.agnes_tools',
    'tools.code_tools_v2',
    'tools.document_tools',
    'tools.file_tools_v2',
    'tools.hardware_tools',
    'tools.memory_tool',
    'tools.multi_search_tools',
    'tools.nudge_tool',
    'tools.system_tools',
    'tools.vision_tools',
    'tools.web_browse_tools',
    'tools.web_tools_v2',
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
    'utils.nahida_acp',
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
    'web.routers.mcp',
    'web.routers.media',
    'web.routers.models',
    'web.routers.plugins',
    'web.routers.schedule',
    'web.routers.system',
    'web.routers.tools',
    'web.routers.setup',
    'web.routers.model_discovery',
    'web.model_capabilities',
    'setup_wizard',
    'qq_bot_adapter',

    # Top-level modules imported by agent_core.py (imported in web.server lifespan)
    'model_router',
    'agent_context',
    'slash_commands',
    'klee_agent',
    'agent_dispatcher',
    'task_orchestrator',
    'instinct_manager',
    'belief_router',
    'hooks',
]

# Collect any sub-modules that static analysis might miss
for pkg in ('openai', 'pydantic', 'starlette', 'anyio', 'uvicorn'):
    try:
        hiddenimports += collect_submodules(pkg)
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
    'msvcrt',
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [os.path.join(SPECPATH, 'agent.py')],
    pathex=[SPECPATH],
    binaries=[],
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
    name='nahida-agent',
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
    icon=os.path.join(SPECPATH, 'assets', 'nahida-icon.ico'),
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
    name='nahida-agent',
)
