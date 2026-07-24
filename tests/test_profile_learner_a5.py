"""A5 修复：测试 UserProfileLearner 的 format string 注入修复"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest

from core.user_profile_learner import UserProfileLearner


class TestProfileLearnerFormatInjection(unittest.TestCase):
    """A5 修复：验证用户消息中的 {} 不会破坏 build_insight_prompt 的 format()"""

    def test_build_prompt_with_curly_braces_in_message(self):
        """A5 修复：用户消息包含 {} 时不应触发 IndexError

        场景：用户消息中包含代码示例 "print({})" 或 "use {name} template"
        根因：_INSIGHT_PROMPT_TEMPLATE.format(conversation_summary=...) 中
              conversation_summary 包含 {} 被 .format() 误解析为占位符
        症状：IndexError: Replacement index 0 out of range for positional args tuple
        """
        recent = [
            {"role": "user", "content": "帮我看看这段 Python 代码 print({}) 有问题吗"},
            {"role": "assistant", "content": "这个代码有语法问题"},
        ]
        # 修复前会抛出 IndexError
        prompt = UserProfileLearner.build_insight_prompt(recent, xp_level=1)
        self.assertIsNotNone(prompt)
        self.assertIn("print({})", prompt, "用户消息中的 {} 应原样保留在 prompt 中")

    def test_build_prompt_with_named_placeholder_in_message(self):
        """A5 修复：用户消息包含 {name} 时不应触发 KeyError"""
        recent = [
            {"role": "user", "content": "请使用 {name} 模板和 {value} 变量"},
        ]
        prompt = UserProfileLearner.build_insight_prompt(recent, xp_level=1)
        self.assertIsNotNone(prompt)
        self.assertIn("{name}", prompt)
        self.assertIn("{value}", prompt)

    def test_build_prompt_with_normal_messages(self):
        """正常消息（无特殊字符）应正常工作"""
        recent = [
            {"role": "user", "content": "你好，今天天气怎么样"},
            {"role": "assistant", "content": "今天天气很好"},
        ]
        prompt = UserProfileLearner.build_insight_prompt(recent, xp_level=1)
        self.assertIn("你好", prompt)
        self.assertIn("天气", prompt)

    def test_build_prompt_with_mixed_curly_braces(self):
        """混合大括号和正常文本都应正确处理"""
        recent = [
            {"role": "user", "content": "正常文本 {}  {0}  {{escaped}} 混合"},
        ]
        prompt = UserProfileLearner.build_insight_prompt(recent, xp_level=2)
        self.assertIsNotNone(prompt)
        # 原始 {} 应保留
        self.assertIn("{}", prompt)


if __name__ == '__main__':
    unittest.main()
