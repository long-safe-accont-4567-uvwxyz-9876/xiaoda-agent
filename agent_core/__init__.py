"""agent_core 包 —— 由原 agent_core.py 拆分而来。

冷启动优化: 使用 __getattr__ 延迟导入, 避免包导入时触发全部子模块。
只有实际访问 agent_core.AgentCore 等名称时才触发导入。
"""
from typing import Any
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 加载 .env
import sys as _sys
from dotenv import load_dotenv
if getattr(_sys, 'frozen', False):
    _env_path = str(Path.home() / ".ai-agent" / ".env")
else:
    _env_path = str(Path(_PROJECT_ROOT) / ".env")
load_dotenv(_env_path, override=True)

# 日志初始化
from utils.logging_config import setup_logging
setup_logging()

# 延迟导入映射表 — 只有实际访问时才触发导入
# 共享类型 (ProcessResult / RequestContext / UserIdentity / DEGRADED_REPLY / _current_request_ctx)
# 直接从 _shared 导入, 避免触发 agent_core.core 整体加载 (打破循环)
_LAZY_IMPORTS = {
    "AgentCore": "agent_core.core",
    "ProcessResult": "agent_core._shared",
    "RequestContext": "agent_core._shared",
    "UserIdentity": "agent_core._shared",
    "DEGRADED_REPLY": "agent_core._shared",
    "_current_request_ctx": "agent_core._shared",
    "StickerManager": "agent_core.core",
    "ModelRouter": "agent_core.core",
    "DatabaseManager": "agent_core.core",
    "AgentContext": "agent_core.core",
    "ToolExecutor": "agent_core.core",
    "ToolCallRepair": "agent_core.core",
    "ResultWrapper": "agent_core.core",
    "FileReceiver": "agent_core.core",
    "KleeAgent": "agent_core.core",
    "TTSEngine": "agent_core.core",
    "AgentDispatcher": "agent_core.core",
    "MCPManager": "agent_core.core",
    "ToolCallHandler": "agent_core.core",
    "to_openai_tools": "agent_core.core",
    "get_credential_pool": "agent_core.core",
    "ErrorClassifier": "agent_core.core",
    "get_hook_engine": "agent_core.core",
    "HookEngine": "agent_core.core",
}


def __getattr__(name: str) -> Any:
    """模块级 __getattr__ — 延迟导入"""
    if name in _LAZY_IMPORTS:
        import importlib
        module = importlib.import_module(_LAZY_IMPORTS[name])
        value = getattr(module, name)
        globals()[name] = value  # 缓存, 下次直接访问
        return value
    raise AttributeError(f"module 'agent_core' has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(_LAZY_IMPORTS.keys())


__all__ = list(_LAZY_IMPORTS.keys())
