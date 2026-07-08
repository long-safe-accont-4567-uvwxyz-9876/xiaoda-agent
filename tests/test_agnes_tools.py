"""Agnes AI 工具测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch


class TestAgnesImageTool(unittest.TestCase):

    def test_agnes_image_no_api_key(self):
        """无 API Key 时返回失败"""
        with patch.dict('os.environ', {'AGNES_API_KEY': ''}):
            # 需要重新导入以获取新的环境变量值
            import importlib
            import tools.agnes_tools
            importlib.reload(tools.agnes_tools)
            # 获取注册的处理函数
            from tool_engine.tool_registry import get_tool
            tool = get_tool("agnes_image_generate")
            if tool and "handler" in tool:
                import asyncio
                result = asyncio.get_event_loop().run_until_complete(
                    tool["handler"](prompt="a cat")
                )
                self.assertFalse(result.success)

    def test_agnes_image_schema_valid(self):
        """工具 schema 格式有效"""
        from tool_engine.tool_registry import get_tool
        tool = get_tool("agnes_image_generate")
        if tool:
            schema = tool.get("schema", {})
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("prompt", schema.get("required", []))


class TestAgnesVideoTool(unittest.TestCase):

    def test_agnes_video_schema_valid(self):
        """工具 schema 格式有效"""
        from tool_engine.tool_registry import get_tool
        tool = get_tool("agnes_video_generate")
        if tool:
            schema = tool.get("schema", {})
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("prompt", schema.get("required", []))


if __name__ == "__main__":
    unittest.main()
