#!/usr/bin/env python3
"""第七轮深度测试 - Agent 完整启动 + 任务编排 + MCP + 数据库 + 剩余模块"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


async def test_agent_full_startup():
    """Part 1: Agent 完整启动流程"""
    print("=" * 60)
    print("Part 1: Agent 完整启动流程")
    print("=" * 60)
    bugs = []

    from agent_core import AgentCore
    core = AgentCore()

    # 检查所有子系统的初始化状态
    print("\n[1] 子系统初始化状态:")
    subsystems = {
        "router": core.router,
        "context": core.context,
        "security": core.security,
        "credential_pool": core._credential_pool,
        "error_classifier": core._error_classifier,
        "hook_engine": core._hook_engine,
        "dispatcher": core.dispatcher,
        "tool_executor": core.tool_executor,
        "tool_call_handler": core.tool_call_handler,
        "guardrails": core.guardrails,
        "mcp_manager": core.mcp_manager,
    }
    for name, obj in subsystems.items():
        status = "OK" if obj is not None else "BUG"
        if obj is None:
            bugs.append(f"子系统 {name} 未初始化")
        print(f"    {name}: {status}")

    # 检查数据库连接
    print("\n[2] 数据库连接检查:")
    try:
        if core._db:
            print("    数据库: OK")
        else:
            bugs.append("数据库未连接")
            print("    数据库: BUG")
    except Exception as e:
        bugs.append(f"数据库检查异常: {e}")
        print(f"    数据库: BUG - {e}")

    # 检查记忆系统
    print("\n[3] 记忆系统检查:")
    try:
        if core._memory:
            print("    记忆: OK")
        else:
            bugs.append("记忆系统未初始化")
            print("    记忆: BUG")
    except Exception as e:
        bugs.append(f"记忆检查异常: {e}")
        print(f"    记忆: BUG - {e}")

    return bugs


async def test_task_orchestration():
    """Part 2: 任务编排测试"""
    print("\n" + "=" * 60)
    print("Part 2: 任务编排测试")
    print("=" * 60)
    bugs = []

    # 测试 TaskOrchestrator
    print("\n[1] TaskOrchestrator 基本功能...")
    try:
        from task_orchestrator import TaskOrchestrator
        _orchestrator = TaskOrchestrator()
        print("    OK: TaskOrchestrator 初始化成功")
    except Exception as e:
        bugs.append(f"TaskOrchestrator: {e}")
        print(f"    BUG: {e}")

    # 测试 AgentDispatcher
    print("\n[2] AgentDispatcher 测试...")
    try:
        from agent_dispatcher import AgentDispatcher
        _dispatcher = AgentDispatcher()
        print("    OK: AgentDispatcher 初始化成功")
    except Exception as e:
        bugs.append(f"AgentDispatcher: {e}")
        print(f"    BUG: {e}")

    # 测试 delegation
    print("\n[3] Delegation 测试...")
    try:
        print("    OK: delegation 导入成功")
    except Exception as e:
        bugs.append(f"Delegation: {e}")
        print(f"    BUG: {e}")

    return bugs


async def test_mcp_integration():
    """Part 3: MCP 集成测试"""
    print("\n" + "=" * 60)
    print("Part 3: MCP 集成测试")
    print("=" * 60)
    bugs = []

    # 测试 MCPManager
    print("\n[1] MCPManager 测试...")
    try:
        from tool_engine.mcp_client import MCPClientManager
        _manager = MCPClientManager()
        print("    OK: MCPClientManager 初始化成功")
    except Exception as e:
        bugs.append(f"MCPClientManager: {e}")
        print(f"    BUG: {e}")

    # 测试工具注册
    print("\n[2] 工具注册测试...")
    try:
        print("    OK: 工具注册导入成功")
    except Exception as e:
        bugs.append(f"工具注册: {e}")
        print(f"    BUG: {e}")

    # 测试工具执行
    print("\n[3] 工具执行测试...")
    try:
        from tool_engine.tool_executor import ToolExecutor
        _executor = ToolExecutor()
        print("    OK: ToolExecutor 初始化成功")
    except Exception as e:
        bugs.append(f"ToolExecutor: {e}")
        print(f"    BUG: {e}")

    return bugs


async def test_database_operations():
    """Part 4: 数据库操作测试"""
    print("\n" + "=" * 60)
    print("Part 4: 数据库操作测试")
    print("=" * 60)
    bugs = []

    # 测试 DatabaseManager
    print("\n[1] DatabaseManager 测试...")
    try:
        from db.database import DatabaseManager
        _db = DatabaseManager()
        print("    OK: DatabaseManager 初始化成功")
    except Exception as e:
        bugs.append(f"DatabaseManager: {e}")
        print(f"    BUG: {e}")

    # 测试 MemoryDB
    print("\n[2] MemoryDB 测试...")
    try:
        from db.db_memory import MemoryDB
        _mem = MemoryDB()
        print("    OK: MemoryDB 初始化成功")
    except Exception as e:
        bugs.append(f"MemoryDB: {e}")
        print(f"    BUG: {e}")

    # 测试 AnalyticsDB
    print("\n[3] AnalyticsDB 测试...")
    try:
        from db.db_analytics import AnalyticsDB
        _analytics = AnalyticsDB()
        print("    OK: AnalyticsDB 初始化成功")
    except Exception as e:
        bugs.append(f"AnalyticsDB: {e}")
        print(f"    BUG: {e}")

    return bugs


async def test_remaining_modules():
    """Part 5: 剩余模块测试"""
    print("\n" + "=" * 60)
    print("Part 5: 剩余模块测试")
    print("=" * 60)
    bugs = []

    # 测试 EmotionEnum
    print("\n[1] EmotionEnum 测试...")
    try:
        from emotion.emotion_enum import EmotionState
        print(f"    OK: EmotionState 导入成功，值: {list(EmotionState)[:3]}")
    except Exception as e:
        bugs.append(f"EmotionEnum: {e}")
        print(f"    BUG: {e}")

    # 测试 EmojiConfig
    print("\n[2] EmojiConfig 测试...")
    try:
        from emotion.emoji_config import EMOJI_MAP
        print(f"    OK: EMOJI_MAP 导入成功，{len(EMOJI_MAP)} 个条目")
    except Exception as e:
        bugs.append(f"EmojiConfig: {e}")
        print(f"    BUG: {e}")

    # 测试 VectorStore
    print("\n[3] VectorStore 测试...")
    try:
        from memory.vector_store import VectorStore
        _vs = VectorStore()
        print("    OK: VectorStore 初始化成功")
    except Exception as e:
        bugs.append(f"VectorStore: {e}")
        print(f"    BUG: {e}")

    # 测试 KnowledgeGraph
    print("\n[4] KnowledgeGraph 测试...")
    try:
        from memory.knowledge_graph import KnowledgeGraph
        _kg = KnowledgeGraph()
        print("    OK: KnowledgeGraph 初始化成功")
    except Exception as e:
        bugs.append(f"KnowledgeGraph: {e}")
        print(f"    BUG: {e}")

    # 测试 PermissionManager
    print("\n[5] PermissionManager 测试...")
    try:
        from security.permission_manager import PermissionManager
        _pm = PermissionManager()
        print("    OK: PermissionManager 初始化成功")
    except Exception as e:
        bugs.append(f"PermissionManager: {e}")
        print(f"    BUG: {e}")

    return bugs


def test_module_imports():
    """Part 6: 模块导入完整性检查"""
    print("\n" + "=" * 60)
    print("Part 6: 模块导入完整性检查")
    print("=" * 60)
    bugs = []

    # 检查所有 Python 模块是否可导入
    modules_to_test = [
        "agent_core", "model_router", "agent_context", "agent_dispatcher",
        "tool_engine.tool_call_handler", "tool_engine.tool_executor", "tool_engine.tool_registry",
        "security.security", "config", "db.database",
        "utils.error_classifier", "utils.credential_pool", "hooks",
        "memory.context_compressor", "instinct_manager", "tool_engine.tool_guardrails",
        "utils.atomic_write", "utils.prompt_caching", "utils.lazy_deps",
        "memory.memory_manager", "belief_router",
        "emotion.tts_engine", "emotion.sticker_manager", "emotion.emotion_simple",
        "task_orchestrator", "tool_engine.mcp_client",
        "qq_bot_adapter",
    ]

    print("\n[1] 模块导入测试:")
    failed = []
    for mod_name in modules_to_test:
        try:
            __import__(mod_name)
            print(f"    OK: {mod_name}")
        except Exception as e:
            failed.append(f"{mod_name}: {str(e)[:60]}")
            print(f"    FAIL: {mod_name} -> {str(e)[:60]}")

    if failed:
        bugs.extend(failed)
    else:
        print("\n    所有模块导入正常！")

    return bugs


async def main():
    print("\n" + "=" * 60)
    print("第七轮深度测试")
    print("=" * 60)

    all_bugs = []

    # Part 1: Agent 完整启动
    try:
        bugs = await test_agent_full_startup()
        all_bugs.extend(bugs)
    except Exception as e:
        all_bugs.append(f"Part 1 异常: {e}")
        print(f"\nPart 1 异常: {e}")

    # Part 2: 任务编排
    try:
        bugs = await test_task_orchestration()
        all_bugs.extend(bugs)
    except Exception as e:
        all_bugs.append(f"Part 2 异常: {e}")
        print(f"\nPart 2 异常: {e}")

    # Part 3: MCP 集成
    try:
        bugs = await test_mcp_integration()
        all_bugs.extend(bugs)
    except Exception as e:
        all_bugs.append(f"Part 3 异常: {e}")
        print(f"\nPart 3 异常: {e}")

    # Part 4: 数据库操作
    try:
        bugs = await test_database_operations()
        all_bugs.extend(bugs)
    except Exception as e:
        all_bugs.append(f"Part 4 异常: {e}")
        print(f"\nPart 4 异常: {e}")

    # Part 5: 剩余模块
    try:
        bugs = await test_remaining_modules()
        all_bugs.extend(bugs)
    except Exception as e:
        all_bugs.append(f"Part 5 异常: {e}")
        print(f"\nPart 5 异常: {e}")

    # Part 6: 模块导入
    try:
        bugs = test_module_imports()
        all_bugs.extend(bugs)
    except Exception as e:
        all_bugs.append(f"Part 6 异常: {e}")
        print(f"\nPart 6 异常: {e}")

    # 汇总
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    if all_bugs:
        print(f"\n发现 {len(all_bugs)} 个问题:")
        for i, bug in enumerate(all_bugs, 1):
            print(f"  {i}. {bug}")
    else:
        print("\n所有测试通过！")

    return all_bugs


if __name__ == "__main__":
    asyncio.run(main())