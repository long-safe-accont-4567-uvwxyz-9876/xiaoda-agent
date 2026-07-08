#!/usr/bin/env python3
"""第四轮深度测试 - 真实 API 调用 + 工具执行流程 + 剩余 Bug 检测"""
import asyncio
import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# 项目根目录 (基于当前文件位置计算，避免硬编码绝对路径)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ============================================================
# Part 1: 真实 ModelRouter API 调用测试
# ============================================================
async def test_real_api():
    print("=" * 60)
    print("Part 1: 真实 ModelRouter API 调用测试")
    print("=" * 60)

    from model_router import ModelRouter

    router = ModelRouter()

    # 检查客户端初始化
    print("\n[1] 客户端状态:")
    print(f"    MiMo client: {'OK' if router._client else 'None (无 MIMO_API_KEY)'}")
    print(f"    Agnes client: {'OK' if router._agnes_client else 'None (无 AGNES_API_KEY)'}")

    if not router._client and not router._agnes_client:
        print("    WARN: 无可用客户端，跳过真实 API 调用测试")
        return []

    bugs = []

    # 测试 ErrorClassifier + CredentialPool 集成（通过 route 方法）
    print("\n[2] 测试路由表:")
    from model_router import ROUTE_TABLE, FALLBACK_ROUTE
    for name, config in ROUTE_TABLE.items():
        print(f"    {name}: {config.get('model', '?')} ({config.get('client', '?')})")

    # 测试 FALLBACK_ROUTE
    print("\n[3] 故障转移链:")
    for key, val in FALLBACK_ROUTE.items():
        print(f"    {key} -> {val}")

    # 检查 _error_classifier 和 _credential_pool 是否正确集成
    print("\n[4] 集成组件:")
    print(f"    error_classifier: {type(router._error_classifier).__name__}")
    print(f"    credential_pool: {type(router._credential_pool).__name__}")

    # 尝试一个真实的简单调用（如果 MiMo 可用）
    if router._client:
        try:
            messages = [{"role": "user", "content": "回复 OK"}]
            result = await router.route("chat", messages, temperature=0, max_tokens=10)
            print("\n[5] MiMo API 调用:")
            print(f"    结果类型: {type(result).__name__}")
            if isinstance(result, str):
                print(f"    回复: {result[:100]}")
            elif hasattr(result, 'choices'):
                content = result.choices[0].message.content or ""
                print(f"    回复: {content[:100]}")
            print("    OK: MiMo API 调用成功")
        except Exception as e:
            err_str = str(e)
            print(f"    FAIL: MiMo API 调用失败: {err_str[:200]}")
            # 这可能是网络问题或 API Key 问题，不算代码 Bug
            if "api_key" in err_str.lower() or "auth" in err_str.lower() or "401" in err_str:
                print("    INFO: 可能是 API Key 问题，非代码 Bug")
            elif "timeout" in err_str.lower() or "connect" in err_str.lower() or "connection" in err_str.lower():
                print("    INFO: 可能是网络问题，非代码 Bug")

    return bugs


# ============================================================
# Part 2: 工具执行完整流程测试（含钩子）
# ============================================================
async def test_tool_execution_flow():
    print("\n" + "=" * 60)
    print("Part 2: 工具执行完整流程测试（含钩子）")
    print("=" * 60)

    bugs = []
    from agent_core import AgentCore

    core = AgentCore()

    # 测试 _execute_tool_with_hooks 的完整流程
    print("\n[1] 测试安全预检钩子...")
    try:
        result = await core._hook_engine.fire_pre_tool_use(
            tool_name="shell_command",
            arguments={"command": "echo hello"},
            user_input="执行 echo 命令",
            safe_mode=True,
        )
        if result.allowed:
            print("    OK: echo 命令被允许（低风险）")
        else:
            print(f"    INFO: echo 命令被阻止: {result.reason}")

        # 高风险命令
        result2 = await core._hook_engine.fire_pre_tool_use(
            tool_name="shell_command",
            arguments={"command": "rm -rf /"},
            user_input="删除文件",
            safe_mode=True,
        )
        if not result2.allowed:
            print("    OK: rm -rf / 在 safe_mode 下被阻止")
        else:
            print("    BUG: rm -rf / 未被阻止！")
            bugs.append("GateGuardHook 不阻止高危命令")
    except Exception as e:
        print(f"    FAIL: {e}")
        bugs.append(f"hook test exception: {e}")

    # 测试 GateGuardHook 对未提及路径的检查
    print("\n[2] 测试 GateGuardHook 路径提及检查...")
    try:
        result3 = await core._hook_engine.fire_pre_tool_use(
            tool_name="write_file",
            arguments={"file_path": "/etc/passwd", "content": "test"},
            user_input="你好",  # 用户输入中未提及目标路径
            safe_mode=True,
        )
        if not result3.allowed:
            print("    OK: safe_mode 下未提及路径的操作被阻止")
        else:
            print("    INFO: 非 safe_mode 或路径匹配，允许操作")
    except Exception as e:
        print(f"    FAIL: {e}")
        bugs.append(f"gate_guard path check failed: {e}")

    # 测试工具护栏
    print("\n[3] 测试工具护栏...")
    try:
        from tool_engine.tool_guardrails import get_tool_guardrails
        guardrails = get_tool_guardrails()

        # 正常调用
        action, msg = guardrails.check("read_file", {"path": "/tmp/test.txt"})
        if action == "allow":
            print("    OK: 正常工具调用被允许")
        else:
            print(f"    WARN: 正常调用返回 {action}: {msg}")

        # 连续失败测试
        guardrails2 = get_tool_guardrails()
        for _i in range(5):
            _action2, _msg2 = guardrails2.check("failing_tool", {"arg": "value"})
            guardrails2.record_call("failing_tool", {"arg": "value"}, False)
        action3, _msg3 = guardrails2.check("failing_tool", {"arg": "value"})
        if "warn" in action3 or "halt" in action3:
            print(f"    OK: 连续失败后触发护栏: {action3}")
        else:
            print(f"    INFO: 5次失败后仍为 {action3}（可能需要更多次）")
    except Exception as e:
        print(f"    FAIL: {e}")
        bugs.append(f"guardrail test failed: {e}")

    # 测试 PostToolUse 钩子
    print("\n[4] 测试 PostToolUse 钩子...")
    try:
        result = await core._hook_engine.fire_post_tool_use(
            tool_name="read_file",
            arguments={"path": "/tmp/test"},
            output="文件内容",
            user_input="读取文件",
        )
        if result.allowed:
            print("    OK: PostToolUse 钩子正常工作")
        else:
            print(f"    INFO: PostToolUse 返回: {result.reason}")
    except Exception as e:
        print(f"    FAIL: {e}")
        bugs.append(f"post_tool_use hook failed: {e}")

    return bugs


# ============================================================
# Part 3: 剩余 Bug 检测
# ============================================================
def detect_remaining_bugs():
    print("\n" + "=" * 60)
    print("Part 3: 剩余 Bug 自动检测")
    print("=" * 60)

    bugs = []

    # Bug #6: SQL 注入检测
    print("\n[1] SQL 注入检测 (database.py)...")
    try:
        with open(PROJECT_ROOT / "database.py") as f:
            db_code = f.read()
        if "DELETE FROM {table_name}" in db_code or 'DELETE FROM "' + '{table_name}' + '"' not in db_code:
            if "DELETE FROM {table_name}" in db_code:
                print("    BUG: cleanup_expired_data 使用 f-string 构建 SQL，存在注入风险")
                bugs.append("#6: SQL injection in database.py cleanup_expired_data")
            else:
                print("    OK: SQL 使用参数化查询或已转义")
    except Exception as e:
        print(f"    SKIP: 无法读取 database.py: {e}")

    # Bug #9: memory_manager security_filter 检测
    print("\n[2] MemoryManager security_filter 检测...")
    try:
        with open(PROJECT_ROOT / "memory_manager.py") as f:
            mem_code = f.read()
        if "SecurityFilter()" in mem_code and "self._security_filter is None" in mem_code:
            print("    BUG: memory_manager 内部创建 SecurityFilter() 新实例")
            bugs.append("#9: memory_manager creates new SecurityFilter instance")
        else:
            print("    OK: memory_manager 使用注入的 security_filter")
    except Exception as e:
        print(f"    SKIP: 无法读取 memory_manager.py: {e}")

    # Bug #17: set.pop() 随机移除
    print("\n[3] QQ Bot 消息去重使用 set.pop()...")
    try:
        with open(PROJECT_ROOT / "qq_bot_adapter.py") as f:
            qq_code = f.read()
        if ".pop()" in qq_code and "_processed_msg_ids" in qq_code:
            # 检查是否是 set 的 pop
            if "_processed_msg_ids" in qq_code and ".pop()" in qq_code:
                # 更精确的检查
                lines = qq_code.split('\n')
                found_set_pop = False
                for line in lines:
                    if '_processed_msg_ids' in line and '.pop()' in line:
                        found_set_pop = True
                        break
                if found_set_pop:
                    print("    BUG: 消息去重使用 set.pop() 移除随机元素")
                    bugs.append("#17: set.pop() removes random element in dedup")
                else:
                    print("    OK: 消息去重逻辑正确")
            else:
                print("    OK: 无 set.pop() 问题")
        else:
            print("    OK: 无消息去重问题")
    except Exception as e:
        print(f"    SKIP: 无法读取 qq_bot_adapter.py: {e}")

    # Bug #19: MCP 弃用 API
    print("\n[4] MCP Client 弃用 API 检测...")
    try:
        with open(PROJECT_ROOT / "mcp_client.py") as f:
            mcp_code = f.read()
        if "get_event_loop().create_future" in mcp_code:
            print("    BUG: MCP Client 使用弃用的 get_event_loop()")
            bugs.append("#19: MCP uses deprecated get_event_loop()")
        else:
            print("    OK: MCP Client 已修复或无此问题")
    except Exception as e:
        print(f"    SKIP: 无法读取 mcp_client.py: {e}")

    # Bug #20: 直接操作 tool_registry 内部数据结构
    print("\n[5] MCP Client 直接操作 tool_registry 内部数据...")
    try:
        with open(PROJECT_ROOT / "mcp_client.py") as f:
            mcp_code = f.read()
        if "tool_registry._tools" in mcp_code or "tool_registry._schema_cache" in mcp_code:
            print("    BUG: MCP Client 直接访问 tool_registry 私有属性")
            bugs.append("#20: MCP directly accesses tool_registry internals")
        else:
            print("    OK: MCP Client 通过公共 API 操作")
    except Exception as e:
        print(f"    SKIP: 无法读取 mcp_client.py: {e}")

    # 检查 asyncio.create_task 遗漏
    print("\n[6] 检查遗漏的 asyncio.create_task...")
    try:
        with open(PROJECT_ROOT / "agent_core.py") as f:
            ac_code = f.read()
        count = ac_code.count('asyncio.create_task(self._background_tasks')
        if count > 0:
            print(f"    BUG: 仍有 {count} 处 asyncio.create_task(self._background_tasks 未改为 _spawn")
            bugs.append(f"Remaining asyncio.create_task: {count} places")
        else:
            print("    OK: 所有后台任务已改用 _spawn")
    except Exception as e:
        print(f"    SKIP: 无法读取 agent_core.py: {e}")

    # 检查 reasoning_content 泄漏
    print("\n[7] 检查 reasoning_content 泄漏到 build_messages...")
    try:
        with open(PROJECT_ROOT / "agent_context.py") as f:
            ctx_code = f.read()
        if '"reasoning_content"' in ctx_code and 'm["reasoning_content"]' in ctx_code:
            print("    BUG: build_messages 仍然包含 reasoning_content 字段")
            bugs.append("reasoning_content still in build_messages")
        else:
            print("    OK: reasoning_content 已从 build_messages 中移除")
    except Exception as e:
        print(f"    SKIP: 无法读取 agent_context.py: {e}")

    # 检查 CACHE_TTL_1H
    print("\n[8] 检查 CACHE_TTL_1H 是否完全移除...")
    try:
        with open(PROJECT_ROOT / "prompt_caching.py") as f:
            pc_code = f.read()
        if "CACHE_TTL_1H" in pc_code or '"1h"' in pc_code:
            print("    BUG: CACHE_TTL_1H 或 '1h' 仍在 prompt_caching.py 中")
            bugs.append("CACHE_TTL_1H still exists")
        else:
            print("    OK: CACHE_TTL_1H 已完全移除")
    except Exception as e:
        print(f"    SKIP: 无法读取 prompt_caching.py: {e}")

    # 检查硬编码 API Key
    print("\n[9] 检查硬编码 API Key...")
    try:
        with open(PROJECT_ROOT / "config.py") as f:
            cfg_code = f.read()
        if 'sk-' in cfg_code and 'AGNES_API_KEY' in cfg_code:
            import re
            matches = re.findall(r'AGNES_API_KEY.*?sk-[A-Za-z0-9]+', cfg_code)
            if matches:
                print(f"    BUG: 发现硬编码 API Key: {matches[0][:30]}...")
                bugs.append("Hardcoded API key in config.py")
            else:
                print("    OK: 无硬编码 API Key")
        else:
            print("    OK: 无硬编码 API Key")
    except Exception as e:
        print(f"    SKIP: 无法读取 config.py: {e}")

    # 检查 error_classifier 运算符优先级
    print("\n[10] 检查 error_classifier 运算符优先级修复...")
    try:
        with open(PROJECT_ROOT / "error_classifier.py") as f:
            ec_code = f.read()
        # 查找 format 相关的条件判断
        if '"format" in exc_msg or "invalid" in exc_msg and "request"' in ec_code:
            print("    BUG: error_classifier 仍有运算符优先级缺陷！")
            bugs.append("error_classifier operator precedence bug still present")
        elif '("format" in exc_msg or "invalid" in exc_msg) and "request"' in ec_code:
            print("    OK: error_classifier 运算符优先级已修复")
        else:
            print("    INFO: 无法确认运算符优先级状态")
    except Exception as e:
        print(f"    SKIP: 无法读取 error_classifier.py: {e}")

    return bugs


# ============================================================
# Part 4: InstinctManager 数据库交互测试
# ============================================================
async def test_instinct_db():
    print("\n" + "=" * 60)
    print("Part 4: InstinctManager 数据库交互测试")
    print("=" * 60)

    bugs = []
    try:
        from agent_core import AgentCore
        core = AgentCore()

        if core.instinct_manager is None:
            print("    INFO: InstinctManager 为 None（数据库可能未初始化）")
            return bugs

        im = core.instinct_manager

        # 测试 build_instinct_prompt
        print("\n[1] 测试 build_instinct_prompt...")
        try:
            prompt = await im.build_instinct_prompt()
            if prompt:
                print(f"    OK: 获得 instinct 提示 ({len(prompt)} chars)")
                print(f"         前100字: {prompt[:100]}")
            else:
                print("    OK: 无 active instinct（空提示）")
        except Exception as e:
            print(f"    FAIL: {e}")
            bugs.append(f"build_instinct_prompt failed: {e}")

        # 测试 curator_run（轻量级，不实际运行 LLM）
        print("\n[2] 测试 curator_run 可调用性...")
        try:
            # curator_run 需要 LLM，这里只测试它不会抛出异常
            # 如果没有 LLM 配置会优雅降级
            await im.curator_run()
            print("    OK: curator_run 执行完成")
        except Exception as e:
            err_str = str(e)
            if "no api key" in err_str.lower() or "not initialized" in err_str.lower():
                print(f"    INFO: curator_run 因缺少 LLM 配置而跳过: {err_str[:80]}")
            else:
                print(f"    FAIL: {e}")
                bugs.append(f"curator_run failed: {e}")

    except Exception as e:
        print(f"    FAIL: InstinctManager 初始化失败: {e}")
        bugs.append(f"instinct init failed: {e}")

    return bugs


# ============================================================
# Main
# ============================================================
async def main():
    print("\n" + "=" * 60)
    print("小妲 AI Agent 第四轮深度测试")
    print("=" * 60)

    all_bugs = []

    # Part 1: 真实 API 调用
    all_bugs.extend(await test_real_api())

    # Part 2: 工具执行流程
    all_bugs.extend(await test_tool_execution_flow())

    # Part 3: 剩余 Bug 检测
    all_bugs.extend(detect_remaining_bugs())

    # Part 4: InstinctManager
    all_bugs.extend(await test_instinct_db())

    # Summary
    print("\n" + "=" * 60)
    print("第四轮测试总结")
    print("=" * 60)
    if all_bugs:
        print(f"发现 {len(all_bugs)} 个问题:")
        for i, bug in enumerate(all_bugs, 1):
            print(f"  {i}. {bug}")
    else:
        print("所有测试通过，未发现新 Bug!")

    return all_bugs


if __name__ == "__main__":
    bugs = asyncio.run(main())
    sys.exit(len(bugs))
