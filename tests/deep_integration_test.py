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
        ("utils.error_classifier", "ErrorClassifier, ClassifiedError, FailoverReason, RecoveryAction"),
        ("utils.credential_pool", "get_credential_pool, CredentialPool"),
        ("hooks", "get_hook_engine, HookEngine"),
        ("utils.atomic_write", "atomic_write, atomic_json_write"),
        ("memory.context_compressor", "get_context_compressor, ContextCompressor"),
        ("tool_engine.tool_guardrails", "ToolGuardrails"),
        ("utils.lazy_deps", "ensure, is_available"),
        ("utils.prompt_caching", "apply_cache_control"),
        ("instinct_manager", "InstinctManager"),
        ("security.security", "SecurityFilter"),
        ("model_router", "ModelRouter"),
        ("agent_context", "AgentContext"),
    ]
    passed = 0
    for mod_name, classes in modules:
        try:
            mod = __import__(mod_name, fromlist=[mod_name.split('.')[-1]])
            for cls_name in classes.split(", "):
                if not hasattr(mod, cls_name):
                    print(f"  WARN: {mod_name}.{cls_name} not found")
            print(f"  OK: {mod_name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {mod_name} -> {e}")
    print(f"  结果: {passed}/{len(modules)} 通过\n")
    return passed == len(modules)
