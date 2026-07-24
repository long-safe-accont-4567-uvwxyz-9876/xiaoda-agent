"""测试 tool_guardrails.py 的 ToolGuardrails"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import unittest

from tool_engine.tool_guardrails import ToolGuardrails


class TestToolGuardrails(unittest.TestCase):
    """测试 ToolGuardrails 工具调用护栏"""

    def setUp(self):
        self.guardrails = ToolGuardrails()
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()

    def test_allow_normal_call(self):
        """正常调用允许"""
        action, msg = self.loop.run_until_complete(self.guardrails.check("read_file", {"path": "/tmp/test"}))
        self.assertEqual(action, "allow")
        self.assertEqual(msg, "")

    def test_exact_failure_warn(self):
        """同工具同参数连续失败2次警告"""
        args = {"path": "/tmp/test"}
        # 记录2次失败
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        # 第3次检查应该警告
        action, msg = self.loop.run_until_complete(self.guardrails.check("read_file", args))
        self.assertEqual(action, "warn")
        self.assertIn("连续失败", msg)

    def test_exact_failure_halt(self):
        """同工具同参数连续失败3次硬停止"""
        args = {"path": "/tmp/test"}
        # 记录3次失败
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        # 第4次检查应该硬停止
        action, msg = self.loop.run_until_complete(self.guardrails.check("read_file", args))
        self.assertEqual(action, "halt")
        self.assertIn("硬停止", msg)

    def test_same_tool_failure_halt(self):
        """同工具累计失败8次硬停止"""
        # 用不同参数，避免触发精确匹配
        for i in range(8):
            self.loop.run_until_complete(self.guardrails.record_call("read_file", {"path": f"/tmp/file{i}"}, success=False, output="error"))
        action, msg = self.loop.run_until_complete(self.guardrails.check("read_file", {"path": "/tmp/new"}))
        self.assertEqual(action, "halt")
        self.assertIn("累计失败", msg)

    def test_no_progress_halt(self):
        """8次调用无成功时中断"""
        # 8次不同工具的失败调用
        for i in range(8):
            self.loop.run_until_complete(self.guardrails.record_call(f"tool_{i}", {"arg": f"val_{i}"}, success=False, output="error"))
        action, msg = self.loop.run_until_complete(self.guardrails.check("another_tool", {"arg": "val"}))
        self.assertEqual(action, "halt")
        self.assertIn("无进展", msg)

    def test_record_and_reset(self):
        """记录调用后重置"""
        args = {"path": "/tmp/test"}
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        self.loop.run_until_complete(self.guardrails.record_call("read_file", args, success=False, output="error"))
        # 重置
        self.guardrails.reset()
        # 重置后应该允许
        action, _msg = self.loop.run_until_complete(self.guardrails.check("read_file", args))
        self.assertEqual(action, "allow")
        # 统计也应清零
        stats = self.guardrails.get_stats()
        self.assertEqual(stats["total_calls"], 0)


if __name__ == '__main__':
    unittest.main()
