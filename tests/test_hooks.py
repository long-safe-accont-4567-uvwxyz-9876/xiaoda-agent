"""测试 hooks.py 的 HookEngine"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
import asyncio
from hooks import HookEngine, BaseHook, HookType, HookResult


class AllowHook(BaseHook):
    """始终允许的钩子"""
    name = "allow_hook"
    hook_type = HookType.PRE_TOOL_USE

    async def execute(self, context: dict) -> HookResult:
        return HookResult(allowed=True)


class DenyHook(BaseHook):
    """始终拒绝的钩子"""
    name = "deny_hook"
    hook_type = HookType.PRE_TOOL_USE

    async def execute(self, context: dict) -> HookResult:
        return HookResult(allowed=False, reason="安全策略禁止此操作")


class ModifyOutputHook(BaseHook):
    """修改输出的 PostToolUse 钩子"""
    name = "modify_output_hook"
    hook_type = HookType.POST_TOOL_USE

    async def execute(self, context: dict) -> HookResult:
        return HookResult(modified_output="[已修改] " + context.get("output", ""))


class PostResponseHook(BaseHook):
    """PostResponse 钩子，记录是否被触发"""
    name = "post_response_hook"
    hook_type = HookType.POST_RESPONSE
    triggered = False

    async def execute(self, context: dict) -> HookResult:
        PostResponseHook.triggered = True
        return HookResult()


class TestHookEngine(unittest.TestCase):
    """测试 HookEngine 钩子系统"""

    def setUp(self):
        self.engine = HookEngine()
        PostResponseHook.triggered = False

    def test_register_hook(self):
        """注册钩子后能列出"""
        hook = AllowHook()
        self.engine.register(hook)
        hooks = self.engine.get_registered_hooks()
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["name"], "allow_hook")
        self.assertEqual(hooks[0]["type"], "pre_tool_use")

    def test_fire_pre_tool_use_allow(self):
        """无钩子拒绝时返回 allowed"""
        # 注册允许钩子
        self.engine.register(AllowHook())
        result = asyncio.run(
            self.engine.fire_pre_tool_use("read_file", {"path": "/tmp/test"})
        )
        self.assertTrue(result.allowed)

    def test_fire_pre_tool_use_deny(self):
        """钩子返回不允许时阻止执行"""
        # 注册拒绝钩子
        self.engine.register(DenyHook())
        result = asyncio.run(
            self.engine.fire_pre_tool_use("rm_file", {"path": "/etc/passwd"})
        )
        self.assertFalse(result.allowed)
        self.assertIn("安全策略", result.reason)

    def test_fire_post_tool_use(self):
        """PostToolUse 钩子能修改输出"""
        self.engine.register(ModifyOutputHook())
        result = asyncio.run(
            self.engine.fire_post_tool_use("read_file", {}, "原始输出内容")
        )
        self.assertIsNotNone(result.modified_output)
        self.assertTrue(result.modified_output.startswith("[已修改]"))

    def test_fire_post_response(self):
        """PostResponse 触发批量处理"""
        self.engine.register(PostResponseHook())
        self.assertFalse(PostResponseHook.triggered)
        asyncio.run(
            self.engine.fire_post_response()
        )
        self.assertTrue(PostResponseHook.triggered)


if __name__ == '__main__':
    unittest.main()
