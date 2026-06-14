"""测试 context_compressor.py 的 ContextCompressor"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch
from memory.context_compressor import ContextCompressor, SUMMARY_PREFIX, CompressionResult


class TestContextCompressor(unittest.TestCase):
    """测试 ContextCompressor 上下文压缩"""

    def setUp(self):
        """创建压缩器实例，使用临时目录替代 DATA_DIR"""
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.cache_dir = self.tmp_dir / "ccr_cache"
        # mock DATA_DIR 为临时目录，使 ContextCompressor.CACHE_DIR 指向临时目录
        with patch('memory.context_compressor.DATA_DIR', self.tmp_dir):
            self.compressor = ContextCompressor(router=None)
            # 确保缓存目录正确
            self.compressor._cache_dir = self.cache_dir
            self.compressor._cache_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_compress_short_output(self):
        """短输出不压缩"""
        short = "这是一段短输出"
        result = self.compressor.compress_tool_output(short, tool_name="test")
        self.assertEqual(result, short)

    def test_compress_long_output(self):
        """长输出压缩，包含 CCR key"""
        # 生成超长输出（超过 TOOL_OUTPUT_THRESHOLD=2000）
        long_output = "这是一行重要的数据内容\n" * 200
        self.assertGreater(len(long_output), 2000)
        result = self.compressor.compress_tool_output(long_output, tool_name="test")
        self.assertIn("key=", result)
        self.assertLess(len(result), len(long_output))

    def test_retrieve_context(self):
        """压缩后能通过 key 检索原始数据"""
        long_output = "重要数据行内容，这是一段很长的测试文本\n" * 200
        self.assertGreater(len(long_output), 2000)
        compressed = self.compressor.compress_tool_output(long_output, tool_name="test")
        # 从压缩结果中提取 key
        import re
        match = re.search(r'key=([a-f0-9]+)', compressed)
        self.assertIsNotNone(match, f"未在压缩结果中找到 key，内容: {compressed[:200]}")
        ccr_key = match.group(1)
        # 检索原始数据
        retrieved = self.compressor.retrieve(ccr_key)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved, long_output)

    def test_compress_history_short(self):
        """短历史不压缩"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = self.compressor.compress_history(messages, keep_recent=5)
        self.assertIsInstance(result, CompressionResult)
        self.assertEqual(len(result.messages), 2)

    def test_compress_history_long(self):
        """长历史压缩，包含 SUMMARY_PREFIX"""
        # 构建长对话历史
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"用户消息 {i}"})
            messages.append({"role": "assistant", "content": f"助手回复 {i}"})
        result = self.compressor.compress_history(messages, keep_recent=5)
        self.assertIsInstance(result, CompressionResult)
        # 压缩后消息数应少于原始
        self.assertLess(len(result.messages), len(messages))
        # 第一条消息应包含 SUMMARY_PREFIX
        self.assertTrue(result.messages[0]["content"].startswith(SUMMARY_PREFIX))

    def test_summary_prefix_present(self):
        """压缩摘要包含幽灵指令防护标记"""
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"用户消息 {i}"})
            messages.append({"role": "assistant", "content": f"助手回复 {i}"})
        result = self.compressor.compress_history(messages, keep_recent=5)
        self.assertIsInstance(result, CompressionResult)
        # 检查防护标记关键词
        first_content = result.messages[0]["content"]
        self.assertIn("仅供参考", first_content)
        self.assertIn("不是当前指令", first_content)

    def test_deterministic_fallback(self):
        """确定性回退提取关键锚点"""
        messages = [
            {"role": "user", "content": "请帮我调试代码"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read_file"}}
            ]},
            {"role": "user", "content": "文件读取失败，error occurred"},
        ]
        result = self.compressor._deterministic_fallback(messages)
        # 应包含用户请求锚点
        self.assertIn("用户", result)

    def test_tool_output_summary(self):
        """工具输出信息性摘要"""
        # 终端命令摘要
        summary = self.compressor._tool_output_summary("shell_command", "line1\nline2\nexit code 1")
        self.assertIn("shell_command", summary)

        # 文件操作摘要
        summary = self.compressor._tool_output_summary("read_file", "/etc/passwd 第一行内容")
        self.assertIn("read_file", summary)

        # 搜索结果摘要
        summary = self.compressor._tool_output_summary("search", "result1\nresult2\nresult3")
        self.assertIn("search", summary)
        self.assertIn("3 条结果", summary)


if __name__ == '__main__':
    unittest.main()
