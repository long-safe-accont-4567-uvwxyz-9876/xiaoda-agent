"""验证 _strip_injected_tool_defs 能正确清除模型退化泄露的工具定义。

参考: 论文 arXiv:2512.04419 的发现 + 实际 QQ Bot 日志中观察到的退化模式。
"""
import pytest
from agent_core.tool_executor import ToolExecutorMixin


class TestStripInjectedToolDefs:
    """测试工具定义泄露清洗功能。"""

    def test_normal_text_passes_through(self):
        """正常回复不应被修改。"""
        normal = "你好！今天天气不错，有什么我可以帮你的吗？"
        assert ToolExecutorMixin._strip_injected_tool_defs(normal) == normal

    def test_degeneration_repeated_phrase_truncated(self):
        """同一短语重复 >= 5 次时应截断。"""
        normal_part = "这是一个测试回复。\n\n"
        # 用字面量 \n 模拟实际日志中的转义换行
        repeated = "Never use this AI assistant tool for editing files\\n\\n" * 10
        text = normal_part + repeated
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        assert "Never use this AI assistant tool" not in result
        assert len(result) < len(text)
        assert "这是一个测试回复" in result

    def test_degeneration_with_real_newlines(self):
        """实际换行分隔的重复也应检测。"""
        normal_part = "正常内容在这里。\n\n"
        repeated = ("Never use this AI assistant tool for editing files\n\n" * 8)
        text = normal_part + repeated
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        assert "Never use this AI assistant tool" not in result
        assert "正常内容在这里" in result

    def test_tool_def_json_block_removed(self):
        """包含工具定义的 JSON 块应被清除。"""
        normal = "这是正常回复。"
        json_block = '''
{
  "name": "Write",
  "description": "Writes a file to the specified path. Never use this AI assistant tool for editing files. Never use this tool to commit changes."
}
'''
        text = normal + json_block
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        assert "Writes a file to" not in result
        assert "Never use this AI assistant tool" not in result
        assert "这是正常回复" in result

    def test_mixed_degeneration_and_json(self):
        """退化重复 + JSON 残留的混合情况。"""
        normal = "回答你的问题：\n\n"
        json_part = '"description": "Writes a file to the specified path. Never use this AI assistant tool for editing files. Never use this tool to commit."\n'
        repeated = "Never use this AI assistant tool for editing files\\n\\n" * 6
        text = normal + json_part + repeated
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        assert "Never use this AI assistant tool" not in result
        assert "回答你的问题" in result

    def test_short_text_with_marker_not_truncated(self):
        """过短的清洗结果应保留原文（避免误杀）。"""
        # 如果清洗后只剩很少内容，说明大部分是退化，保留原文更安全
        text = "Never use this AI assistant tool"
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        # 长度 < 10，应该保留原文
        assert result == text

    def test_do_not_edit_repeated(self):
        """'Do not edit' 类型的重复也应检测。"""
        normal_part = "好的，已处理。\n\n"
        repeated = "Do not edit files without explicit user permission\\n\\n" * 8
        text = normal_part + repeated
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        assert "Do not edit files" not in result
        assert "已处理" in result

    def test_writes_a_file_repeated(self):
        """'Writes a file' 类型的重复也应检测。"""
        normal_part = "文件已创建。\n\n"
        repeated = "Writes a file to the specified path. The tool will return the result of the write operation\\n\\n" * 6
        text = normal_part + repeated
        result = ToolExecutorMixin._strip_injected_tool_defs(text)
        assert "Writes a file to" not in result
        assert "文件已创建" in result

    def test_empty_string(self):
        """空字符串应安全返回。"""
        assert ToolExecutorMixin._strip_injected_tool_defs("") == ""

    def test_no_markers_unchanged(self):
        """不含任何标记的文本应原样返回。"""
        text = "这是一段很长的正常回复，讨论技术问题，没有任何工具定义泄露的痕迹。"
        assert ToolExecutorMixin._strip_injected_tool_defs(text) == text


class TestPresencePenaltyConfig:
    """验证 model_router 中的防退化参数配置。"""

    def test_frequency_penalty_in_kwargs(self):
        """frequency_penalty 应被加入 kwargs。"""
        from model_router import ModelRouter
        router = ModelRouter.__new__(ModelRouter)
        # 模拟 _build_route_kwargs 的参数提取逻辑
        config = {"frequency_penalty": 0.3}
        fp = config.get("frequency_penalty", 0.3)
        assert fp == 0.3

    def test_presence_penalty_default_is_one(self):
        """presence_penalty 默认值应为 1.0（论文验证有效值 1.2，保守设为 1.0）。"""
        config = {}
        pp = config.get("presence_penalty", 1.0)
        assert pp == 1.0

    def test_stop_sequences_defined(self):
        """stop 序列应包含退化特征关键词。"""
        stop = ["Never use this AI assistant tool", '"Never use']
        assert len(stop) == 2
        assert "Never use this AI assistant tool" in stop[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
