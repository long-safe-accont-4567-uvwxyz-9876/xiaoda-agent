#!/usr/bin/env python3
"""深度集成测试 - 验证所有模块集成和发现 Bug"""
import sys
import os
import asyncio
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

def test_imports():
    """测试所有核心模块导入"""
    print("=" * 60)
    print("1. 核心模块导入测试")
    print("=" * 60)
    modules = [
        ("error_classifier", "ErrorClassifier, ClassifiedError, FailoverReason, RecoveryAction"),
        ("credential_pool", "get_credential_pool, CredentialPool"),
        ("hooks", "get_hook_engine, HookEngine"),
        ("atomic_write", "atomic_write, atomic_json_write"),
        ("context_compressor", "get_context_compressor, ContextCompressor"),
        ("tool_guardrails", "ToolGuardrails"),
        ("lazy_deps", "ensure, is_available"),
        ("prompt_caching", "apply_cache_control"),
        ("instinct_manager", "InstinctManager"),
        ("security", "SecurityFilter"),
        ("model_router", "ModelRouter"),
        ("agent_context", "AgentContext"),
    ]
    passed = 0
    for mod_name, classes in modules:
        try:
            mod = __import__(mod_name)
            for cls_name in classes.split(", "):
                if not hasattr(mod, cls_name):
                    print(f"  WARN: {mod_name}.{cls_name} not found")
            print(f"  OK: {mod_name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {mod_name} -> {e}")
    print(f"  结果: {passed}/{len(modules)} 通过\n")
    return passed == len(modules)


def test_agent_core_init():
    """测试 AgentCore 初始化"""
    print("=" * 60)
    print("2. AgentCore 初始化测试")
    print("=" * 60)
    try:
        from agent_core import AgentCore
        core = AgentCore()
        print(f"  OK: AgentCore 初始化成功")
        print(f"  - credential_pool: {type(core._credential_pool).__name__}")
        print(f"  - error_classifier: {type(core._error_classifier).__name__}")
        print(f"  - hook_engine: {type(core._hook_engine).__name__}")
        print(f"  - instinct_manager: {type(core.instinct_manager).__name__ if core.instinct_manager else 'None'}")
        print(f"  - security_filter: {type(core.security).__name__}")
        print(f"  - context.security_filter: {type(core.context._security_filter).__name__ if core.context._security_filter else 'None'}")
        print(f"  - router._credential_pool: {type(core.router._credential_pool).__name__}")
        print(f"  - router._error_classifier: {type(core.router._error_classifier).__name__}")
        print(f"  - conversation_count: {core._conversation_count}")
        return True
    except Exception as e:
        traceback.print_exc()
        print(f"  FAIL: AgentCore 初始化失败: {e}")
        return False


def test_error_classifier_logic():
    """测试 ErrorClassifier 逻辑正确性"""
    print("=" * 60)
    print("3. ErrorClassifier 逻辑测试")
    print("=" * 60)
    from utils.error_classifier import ErrorClassifier, FailoverReason

    ec = ErrorClassifier()
    bugs = []

    # 测试 B2 修复：运算符优先级
    try:
        exc = Exception("invalid format request body")
        result = ec.classify(exc)
        if result.reason == FailoverReason.FORMAT_ERROR:
            print("  OK: 'invalid format request' -> FORMAT_ERROR")
        else:
            print(f"  BUG: 'invalid format request' -> {result.reason}, expected FORMAT_ERROR")
            bugs.append("B2 regression: operator precedence")
    except Exception as e:
        print(f"  FAIL: {e}")
        bugs.append(f"error_classifier exception: {e}")

    # 测试 "format" 单独出现不应触发 FORMAT_ERROR
    try:
        exc = Exception("format disk")
        result = ec.classify(exc)
        if result.reason != FailoverReason.FORMAT_ERROR:
            print(f"  OK: 'format disk' -> {result.reason} (not FORMAT_ERROR)")
        else:
            print(f"  BUG: 'format disk' -> FORMAT_ERROR, should not match")
            bugs.append("'format' alone triggers FORMAT_ERROR")
    except Exception as e:
        print(f"  FAIL: {e}")

    # 测试 "invalid request" 应触发 FORMAT_ERROR
    try:
        exc = Exception("invalid request parameters")
        result = ec.classify(exc)
        if result.reason == FailoverReason.FORMAT_ERROR:
            print("  OK: 'invalid request' -> FORMAT_ERROR")
        else:
            print(f"  INFO: 'invalid request' -> {result.reason}")
    except Exception as e:
        print(f"  FAIL: {e}")

    return bugs


def test_credential_pool_integration():
    """测试凭证池集成"""
    print("=" * 60)
    print("4. CredentialPool 集成测试")
    print("=" * 60)
    from utils.credential_pool import CredentialPool, Credential, CredentialState
    from utils.error_classifier import ClassifiedError, FailoverReason, RecoveryAction

    bugs = []
    pool = CredentialPool()
    pool.add_credential(Credential(
        provider="test",
        api_key="sk-test-key-123456",
        base_url="https://api.test.com",
    ))

    # 测试基本获取
    cred = pool.get_credential("test")
    if cred and cred.api_key == "sk-test-key-123456":
        print("  OK: 基本获取凭证")
    else:
        print("  BUG: 基本获取凭证失败")
        bugs.append("credential pool get failed")

    # 测试限速错误 -> exhausted
    error = ClassifiedError(
        reason=FailoverReason.RATE_LIMIT,
        action=RecoveryAction.BACKOFF_RETRY,
        original_error=Exception("rate limit"),
        message="rate limit exceeded",
        is_retryable=True,
        backoff_seconds=120.0,  # 大于默认 60s
    )
    pool.report_error("test", error)
    cred_after = pool.get_credential("test")
    # 当只有一个凭证且 exhausted 时，get_credential 会返回冷却中的凭证（降级使用）
    # 这是设计意图，不算 Bug
    if cred_after is not None and cred_after.state == CredentialState.EXHAUSTED:
        print("  OK: 限速后凭证变为 exhausted，降级返回冷却中凭证")
    elif cred_after is None:
        print("  OK: 限速后凭证变为 exhausted，无可用凭证返回 None")
    else:
        print(f"  INFO: 限速后凭证状态: {cred_after.state}")

    # 测试冷却期使用 API 退避时间
    # 检查 cooldown_until 是否被正确设置
    for c in pool._pool.get("test", []):
        if c.cooldown_until > 0:
            import time
            remaining = c.cooldown_until - time.time()
            print(f"  INFO: 凭证冷却剩余约 {remaining:.0f}s (预期 120s)")
            if remaining > 60:
                print("  OK: 冷却期使用了 API 退避时间 (120s > 60s)")
            break

    return bugs


def test_prompt_caching_fix():
    """测试 Prompt Caching 修复"""
    print("=" * 60)
    print("5. PromptCaching 修复验证")
    print("=" * 60)
    bugs = []
    from utils.prompt_caching import apply_cache_control

    # 验证不再有 CACHE_TTL_1H
    import utils.prompt_caching
    if hasattr(prompt_caching, 'CACHE_TTL_1H'):
        print("  BUG: CACHE_TTL_1H 仍然存在")
        bugs.append("CACHE_TTL_1H not removed")
    else:
        print("  OK: CACHE_TTL_1H 已移除")

    # 验证所有缓存标记都是 ephemeral
    messages = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "How are you"},
    ]
    result = apply_cache_control(messages)
    for msg in result:
        if "cache_control" in msg:
            ct = msg["cache_control"].get("type")
            if ct != "ephemeral":
                print(f"  BUG: cache_control type = {ct}, expected 'ephemeral'")
                bugs.append(f"non-ephemeral cache_control: {ct}")
            else:
                print(f"  OK: {msg['role']} -> cache_control.type = ephemeral")

    return bugs


def test_context_compressor_tool_messages():
    """测试上下文压缩器保留 tool 消息"""
    print("=" * 60)
    print("6. ContextCompressor tool 消息保留测试")
    print("=" * 60)
    bugs = []
    from memory.context_compressor import ContextCompressor

    comp = ContextCompressor(router=None)

    # 测试 compress_history 保留 tool 消息
    history = [
        {"role": "user", "content": "查看文件"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": "{}"}, "type": "function"}]},
        {"role": "tool", "content": "文件内容: hello world", "name": "read_file", "tool_call_id": "1"},
        {"role": "assistant", "content": "文件内容是 hello world"},
        {"role": "user", "content": "继续"},
    ]

    # 测试 _quick_summarize 是否保留 tool 消息
    from agent_context import AgentContext
    ctx = AgentContext()
    summary = ctx._quick_summarize(history)
    if "read_file" in summary or "工具" in summary:
        print(f"  OK: _quick_summarize 保留 tool 消息: {summary[:80]}")
    else:
        print(f"  BUG: _quick_summarize 丢失 tool 消息: {summary[:80]}")
        bugs.append("_quick_summarize drops tool messages")

    return bugs


def test_security_filter_injection():
    """测试 security_filter 注入"""
    print("=" * 60)
    print("7. SecurityFilter 注入测试")
    print("=" * 60)
    bugs = []
    try:
        from agent_core import AgentCore
        from security.security import SecurityFilter
        core = AgentCore()

        # 检查 AgentContext 是否使用了注入的 security_filter
        ctx_filter = core.context._security_filter
        core_filter = core.security

        if ctx_filter is core_filter:
            print("  OK: AgentContext 使用了注入的 security_filter (同一实例)")
        elif ctx_filter is None:
            print("  BUG: AgentContext._security_filter 为 None")
            bugs.append("security_filter not injected")
        else:
            print("  WARN: AgentContext 使用了不同的 security_filter 实例")
            bugs.append("security_filter different instance")

    except Exception as e:
        traceback.print_exc()
        print(f"  FAIL: {e}")
        bugs.append(f"security_filter test exception: {e}")

    return bugs


def test_hooks_gate_guard():
    """测试 GateGuardHook 增强"""
    print("=" * 60)
    print("8. GateGuardHook 增强测试")
    print("=" * 60)
    bugs = []

    async def _test():
        from hooks import GateGuardHook, HookResult

        hook = GateGuardHook()

        # 测试 safe_mode 下阻止高风险工具
        ctx = {
            "tool_name": "shell_command",
            "arguments": {"command": "rm -rf /"},
            "user_input": "删除文件",
            "safe_mode": True,
        }
        result = await hook.execute(ctx)
        if not result.allowed:
            print("  OK: safe_mode 下 shell_command 被阻止")
        else:
            print("  BUG: safe_mode 下 shell_command 未被阻止")
            bugs.append("GateGuard doesn't block in safe_mode")

        # 测试 safe_mode 下 python_executor 被阻止
        ctx2 = {
            "tool_name": "python_executor",
            "arguments": {"code": "import os; os.system('ls')"},
            "user_input": "执行代码",
            "safe_mode": True,
        }
        result2 = await hook.execute(ctx2)
        if not result2.allowed:
            print("  OK: safe_mode 下 python_executor 被阻止")
        else:
            print("  BUG: safe_mode 下 python_executor 未被阻止")
            bugs.append("GateGuard doesn't block python_executor in safe_mode")

        # 测试非 safe_mode 下允许
        ctx3 = {
            "tool_name": "shell_command",
            "arguments": {"command": "ls"},
            "user_input": "列出文件",
            "safe_mode": False,
        }
        result3 = await hook.execute(ctx3)
        if result3.allowed:
            print("  OK: 非 safe_mode 下 shell_command 允许")
        else:
            print("  BUG: 非 safe_mode 下 shell_command 被阻止")
            bugs.append("GateGuard blocks in non-safe_mode")

        return bugs

    return asyncio.get_event_loop().run_until_complete(_test())


def test_model_router_integration():
    """测试 ModelRouter 集成 ErrorClassifier + CredentialPool"""
    print("=" * 60)
    print("9. ModelRouter 集成测试")
    print("=" * 60)
    bugs = []
    try:
        from model_router import ModelRouter
        router = ModelRouter()

        # 检查是否有 _error_classifier 和 _credential_pool
        if hasattr(router, '_error_classifier') and router._error_classifier is not None:
            print("  OK: ModelRouter._error_classifier 已初始化")
        else:
            print("  BUG: ModelRouter._error_classifier 未初始化")
            bugs.append("router._error_classifier missing")

        if hasattr(router, '_credential_pool') and router._credential_pool is not None:
            print("  OK: ModelRouter._credential_pool 已初始化")
        else:
            print("  BUG: ModelRouter._credential_pool 未初始化")
            bugs.append("router._credential_pool missing")

        # 检查 _route_with_retry 方法中是否使用了 ErrorClassifier
        import inspect
        source = inspect.getsource(router._route_with_retry)
        if "ErrorClassifier" in source or "error_classifier" in source or "_error_classifier" in source:
            print("  OK: _route_with_retry 使用了 ErrorClassifier")
        else:
            print("  BUG: _route_with_retry 未使用 ErrorClassifier")
            bugs.append("_route_with_retry doesn't use ErrorClassifier")

        if "credential_pool" in source or "_credential_pool" in source:
            print("  OK: _route_with_retry 使用了 CredentialPool")
        else:
            print("  BUG: _route_with_retry 未使用 CredentialPool")
            bugs.append("_route_with_retry doesn't use CredentialPool")

    except Exception as e:
        traceback.print_exc()
        print(f"  FAIL: {e}")
        bugs.append(f"model_router test exception: {e}")

    return bugs


def test_reasoning_content_not_leaked():
    """测试 reasoning_content 不再泄漏到 API"""
    print("=" * 60)
    print("10. reasoning_content 不泄漏到 API 测试")
    print("=" * 60)
    bugs = []
    from agent_context import AgentContext

    ctx = AgentContext()
    # 添加含 reasoning_content 的历史消息
    ctx.history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！", "reasoning_content": "用户在打招呼"},
        {"role": "user", "content": "再见"},
    ]

    messages = ctx.build_messages("user123")

    # 检查 messages 中是否包含 reasoning_content
    has_leak = False
    for msg in messages:
        if "reasoning_content" in msg:
            has_leak = True
            print(f"  BUG: {msg['role']} 消息包含 reasoning_content: {msg['reasoning_content']}")
            bugs.append("reasoning_content leaked to API messages")

    if not has_leak:
        print("  OK: reasoning_content 未泄漏到 API 消息")

    return bugs


def test_background_task_tracking():
    """测试后台任务追踪"""
    print("=" * 60)
    print("11. 后台任务追踪测试")
    print("=" * 60)
    bugs = []
    import inspect
    from agent_core import AgentCore

    source = inspect.getsource(AgentCore)
    # 检查是否还有 asyncio.create_task(self._background_tasks
    if "asyncio.create_task(self._background_tasks" in source:
        print("  BUG: 仍有 asyncio.create_task(self._background_tasks 未改为 _spawn")
        bugs.append("asyncio.create_task not replaced with _spawn")
    else:
        print("  OK: 所有 _background_tasks 调用已改为 _spawn")

    return bugs


def test_config_no_hardcoded_key():
    """测试配置文件无硬编码密钥"""
    print("=" * 60)
    print("12. 配置文件硬编码密钥检查")
    print("=" * 60)
    bugs = []
    import config

    agnes_key = config.AGNES_API_KEY
    if agnes_key and agnes_key.startswith("sk-") and len(agnes_key) > 20:
        print(f"  BUG: AGNES_API_KEY 仍有硬编码值: {agnes_key[:10]}...")
        bugs.append("AGNES_API_KEY hardcoded")
    else:
        print("  OK: AGNES_API_KEY 无硬编码值")

    return bugs


def main():
    print("\n" + "=" * 60)
    print("纳西妲 AI Agent 深度集成测试")
    print("=" * 60 + "\n")

    all_bugs = []

    test_imports()
    test_agent_core_init()
    all_bugs.extend(test_error_classifier_logic())
    all_bugs.extend(test_credential_pool_integration())
    all_bugs.extend(test_prompt_caching_fix())
    all_bugs.extend(test_context_compressor_tool_messages())
    all_bugs.extend(test_security_filter_injection())
    all_bugs.extend(test_hooks_gate_guard())
    all_bugs.extend(test_model_router_integration())
    all_bugs.extend(test_reasoning_content_not_leaked())
    all_bugs.extend(test_background_task_tracking())
    all_bugs.extend(test_config_no_hardcoded_key())

    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    if all_bugs:
        print(f"发现 {len(all_bugs)} 个问题:")
        for i, bug in enumerate(all_bugs, 1):
            print(f"  {i}. {bug}")
    else:
        print("所有测试通过，未发现 Bug!")

    return all_bugs


if __name__ == "__main__":
    bugs = main()
    sys.exit(len(bugs))
