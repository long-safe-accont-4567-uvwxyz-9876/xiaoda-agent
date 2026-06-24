"""agent_core 包 —— 由原 agent_core.py 拆分而来。

对外保持与原 agent_core.py 模块完全一致的接口：
    from agent_core import AgentCore
    from agent_core import ProcessResult, RequestContext, UserIdentity
    from agent_core import _current_request_ctx, StickerManager

原 agent_core.py 顶部的 sys.path 注入、.env 加载与日志初始化等副作用
迁移至本 __init__.py 中执行，确保包导入时即完成环境准备。
"""
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path（原 agent_core.py 顶部：
#   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 拆分为包后 __file__ 位于 agent_core/ 子目录，需上溯一级指向项目根目录）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 加载 .env（原 agent_core.py 顶部的 load_dotenv 逻辑）
import sys as _sys
from dotenv import load_dotenv
if getattr(_sys, 'frozen', False):
    _env_path = str(Path.home() / ".ai-agent" / ".env")
else:
    _env_path = str(Path(_PROJECT_ROOT) / ".env")
load_dotenv(_env_path, override=True)

# 日志初始化（原 agent_core.py 顶部的 setup_logging 调用）
from utils.logging_config import setup_logging
setup_logging()

# 导出对外接口（保持与原 agent_core.py 模块命名空间一致）
from agent_core.core import (
    AgentCore,
    ProcessResult,
    RequestContext,
    UserIdentity,
    DEGRADED_REPLY,
    _current_request_ctx,
    StickerManager,
    # 以下名称在原 agent_core.py 顶部以 from xxx import yyy 形式导入，
    # 测试与其他模块会通过 agent_core.XXX 形式访问（如 mock.patch），
    # 故在此一并重新导出，以保持与原模块命名空间完全一致
    ModelRouter,
    DatabaseManager,
    AgentContext,
    ToolExecutor,
    ToolCallRepair,
    ResultWrapper,
    FileReceiver,
    KleeAgent,
    TTSEngine,
    AgentDispatcher,
    MCPManager,
    ToolCallHandler,
    to_openai_tools,
    get_credential_pool,
    ErrorClassifier,
    get_hook_engine,
)

__all__ = [
    "AgentCore",
    "ProcessResult",
    "RequestContext",
    "UserIdentity",
    "DEGRADED_REPLY",
    "_current_request_ctx",
    "StickerManager",
    "ModelRouter",
    "DatabaseManager",
    "AgentContext",
    "ToolExecutor",
    "ToolCallRepair",
    "ResultWrapper",
    "FileReceiver",
    "KleeAgent",
    "TTSEngine",
    "AgentDispatcher",
    "MCPManager",
    "ToolCallHandler",
    "to_openai_tools",
    "get_credential_pool",
    "ErrorClassifier",
    "get_hook_engine",
]
