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
    result = []
    _exclude = {'.env', '.env.prod', '.env.local', 'webui_overrides.json'}
    _exclude_dirs = {'credentials', '__pycache__', '.git', 'node_modules'}
    for dirpath, _dirnames, filenames in os.walk(root):
        _dirnames[:] = [d for d in _dirnames if d not in _exclude_dirs]
        for fn in filenames:
            if fn in _exclude:
                continue
            if fn.endswith('.key') or fn.endswith('.secret'):
                continue
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, SPECPATH)
            result.append((src, os.path.dirname(rel)))
    return result


# ---------------------------------------------------------------------------
# Data files – folders that Python code reads at runtime
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

# assets/ directory (icons and other resources)
datas += _tree_datas(os.path.join(SPECPATH, 'assets'), 'assets')

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
    # LLM clients
    'openai',
    'httpx',
    # QQ bot
    'qq_bot_adapter',
    # Plugins
    'plugins',
    'plugins.manager',
    'plugins.hello',
    'plugins.builtin',
    'plugins.weather_plugin',
    'plugins.translation_plugin',
    'plugins.news_plugin',
    'plugins.market_plugin',
    'plugins.calendar_plugin',
    'plugins.code_plugin',
    'plugins.file_plugin',
    'plugins.email_plugin',
    'plugins.reminder_plugin',
    'plugins.search_plugin',
    'plugins.stock_plugin',
    'plugins.time_plugin',
    'plugins.wolfram_plugin',
    # MCP tools
    'tool_engine',
    'tool_engine.mcp_client',
    'tool_engine.tool_registry',
    # Web modules
    'web',
    'web.ws_hub',
    'web.config_service',
    'web.custom_providers',
    'web.media_tasks',
    'web.greeting_scheduler',
    'web.agent_registry',
    # CLI
    'cli_client',
    'cli',
    # Config
    'config',
    'utils',
    'utils.logging_config',
    'utils.vision_service',
    # Core
    'agent_core',
    'core',
    'model_router',
    'setup_wizard',
    'knowledge_graph',
    'memory',
    'models',
    # Database
    'aiosqlite',
    'sqlite3',
    # Logging
    'loguru',
    # Serialization
    'pydantic',
    'yaml',
    # Data processing
    'jieba',
    'numpy',
    'PIL',
    # Web server
    'uvicorn',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'websockets',
    'sse_starlette',
    'python_multipart',
    'starlette',
    # Rich console
    'rich',
    # Other
    'dotenv',
]

# submodules collect
for pkg in ('plugins', 'web', 'config', 'utils', 'tool_engine'):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# web/routers – every router module MUST be listed for PyInstaller
_router_mods = [
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
]
hiddenimports.extend(_router_mods)

try:
    hiddenimports += collect_submodules('web.routers')
except Exception:
    pass

# Exclude large unnecessary packages
excluded = []

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    ['agent.py'],
    pathex=[SPECPATH],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------------------
# PYZ – all pure-Python modules compiled into a zip
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
