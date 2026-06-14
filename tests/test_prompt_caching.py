"""测试 prompt_caching.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from utils.prompt_caching import apply_cache_control, CACHE_TTL_5M


class TestPromptCaching(unittest.TestCase):
    """测试 Prompt Caching 策略"""

    def test_apply_cache_control_system(self):
        """system prompt 获得缓存标记"""
        messages = [
            {"role": "system", "content": "你是一个助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ]
        result = apply_cache_control(messages, cache_ttl=CACHE_TTL_5M, max_breakpoints=4)
        # system 消息应有 cache_control
        system_content = result[0]["content"]
        self.assertIsInstance(system_content, list)
        self.assertIn("cache_control", system_content[0])

    def test_apply_cache_control_recent_messages(self):
        """仅在 system 消息上放置缓存标记，非 system 消息不放置"""
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "消息1"},
            {"role": "assistant", "content": "回复1"},
            {"role": "user", "content": "消息2"},
            {"role": "assistant", "content": "回复2"},
            {"role": "user", "content": "消息3"},
        ]
        result = apply_cache_control(messages, cache_ttl=CACHE_TTL_5M, max_breakpoints=4)
        # 统计有 cache_control 的消息数
        cached_count = 0
        for msg in result:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        cached_count += 1
                        break
        # 只有 system(1) 有断点
        self.assertEqual(cached_count, 1)

    def test_apply_cache_control_max_breakpoints(self):
        """总断点数不超过 max_breakpoints"""
        messages = [
            {"role": "system", "content": "系统提示"},
        ] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"}
            for i in range(20)
        ]
        max_bp = 3
        result = apply_cache_control(messages, cache_ttl=CACHE_TTL_5M, max_breakpoints=max_bp)
        # 统计断点数
        cached_count = 0
        for msg in result:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        cached_count += 1
                        break
        self.assertLessEqual(cached_count, max_bp)

    def test_apply_cache_control_empty(self):
        """空消息列表不报错"""
        result = apply_cache_control([], cache_ttl=CACHE_TTL_5M)
        self.assertEqual(result, [])

    def test_apply_cache_control_no_mutation(self):
        """不修改原始消息列表（深拷贝）"""
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "你好"},
        ]
        # 保存原始内容的快照
        original_content = messages[0]["content"]
        result = apply_cache_control(messages, cache_ttl=CACHE_TTL_5M, max_breakpoints=4)
        # 原始列表不应被修改
        self.assertEqual(messages[0]["content"], original_content)
        self.assertIsInstance(messages[0]["content"], str)
        # 返回值是深拷贝，内容可能已变
        self.assertIsInstance(result[0]["content"], list)


if __name__ == '__main__':
    unittest.main()
