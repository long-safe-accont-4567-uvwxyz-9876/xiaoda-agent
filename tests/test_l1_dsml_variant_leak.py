"""L1+L4 测试: strip_dsml 必须覆盖所有 DSML 变体和 <operation> 标签泄漏.

Bug: QQ 聊天日志显示 19 条对话中，LLM 的 DSML 工具调用标记以多种格式泄漏给用户：
- 旧格式 (双竖线): <｜｜DSML｜｜tool_calls> ... (已在 2cf3201 修复)
- 新格式 (单竖线): <｜DSML｜function_calls> ... (07-19 仍在泄漏!)
- 新格式 (单竖线): <｜DSML｜invoke name="recall"> ...
- <operation>recall</operation> 标签泄漏
- 裸 <function_calls>/<invoke>/<parameter> 无 DSML 前缀

根因: DSML_PATTERN/DSML_INVOKE_PATTERN/DSML_LEFTOVER 正则只匹配双竖线 <｜｜DSML｜｜>，
完全不匹配单竖线 <｜DSML｜> 变体。<operation> 标签也未被任何正则处理。

修复目标: strip_dsml 清除所有变体，has_dsml_tool_calls 检测所有变体。
"""
import pytest

# ========== 真实泄漏样本（从 QQ 聊天日志提取） ==========

# L1: 单竖线 DSML function_calls 泄漏 (07-19 13:34)
SINGLE_PIPE_DSML = """<｜DSML｜function_calls>
<｜DSML｜invoke name="recall">
<｜DSML｜parameter name="query" string="true">2026年7月19日 责任 义务 责任和义务
<｜DSML｜parameter name="top_k" string="true">15
<｜DSML｜function_calls>
<｜DSML｜invoke name="recall">
<｜DSML｜parameter name="query" string="true">2026年7月19日 爸爸说 从此以后 你的责任 义务 懂了吗 牢牢记住"""

# 旧格式双竖线 (05-29, 已修复但确保不回退)
DOUBLE_PIPE_DSML = """让纳西妲看看配置～

<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="read_file">
<｜｜DSML｜｜parameter name="path" string="true">/root/.ai-agent/agent.json5</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>"""

# L4: <operation> 标签泄漏 (07-19 10:22)
OPERATION_TAG_LEAK = """等等... 爸爸连发三次问号是在问什么呢？让我回想一下...

让我先检索记忆看看。
<operation>recall</operation><function_calls>
<param name="query">排卵期 排卵 你排卵了吗 爸爸追问
</param>
</function_calls>
<answer>
我"""

# 裸 function_calls 无 DSML 前缀
BARE_FUNCTION_CALLS = """<function_calls>
<invoke name="web_search">
<parameter name="query">最新 AI 新闻</parameter>
</invoke>
</function_calls>"""


# ========== strip_dsml 测试 ==========

def test_strip_dsml_single_pipe_function_calls():
    """L1: 单竖线 <｜DSML｜function_calls> 应被完全清除。"""
    from utils.text_utils import strip_dsml
    result = strip_dsml(SINGLE_PIPE_DSML)
    assert "DSML" not in result, f"单竖线 DSML 未被清除: {result[:200]}"
    assert "function_calls" not in result
    assert "invoke name" not in result
    assert "parameter name" not in result


def test_strip_dsml_double_pipe_tool_calls():
    """旧格式双竖线 <｜｜DSML｜｜tool_calls> 应继续被清除（不回退）。"""
    from utils.text_utils import strip_dsml
    result = strip_dsml(DOUBLE_PIPE_DSML)
    assert "DSML" not in result, f"双竖线 DSML 未被清除: {result[:200]}"
    # 正常文本应保留
    assert "让纳西妲看看配置" in result


def test_strip_dsml_operation_tag():
    """L4: <operation>recall</operation> 应被清除。"""
    from utils.text_utils import strip_dsml
    result = strip_dsml(OPERATION_TAG_LEAK)
    assert "<operation>" not in result.lower(), f"<operation> 标签未清除: {result[:200]}"
    assert "</operation>" not in result.lower()
    # 正常文本应保留
    assert "爸爸连发三次问号" in result


def test_strip_dsml_bare_function_calls():
    """裸 <function_calls>/<invoke>/<parameter> 无 DSML 前缀也应被清除。"""
    from utils.text_utils import strip_dsml
    result = strip_dsml(BARE_FUNCTION_CALLS)
    assert "<function_calls>" not in result.lower()
    assert "<invoke name" not in result.lower()
    assert "<parameter name" not in result.lower()


def test_strip_dsml_preserves_normal_text():
    """清洗后正常文本不应被误删。"""
    from utils.text_utils import strip_dsml
    normal = "爸爸你好～今天天气真好呢！小妲想出去玩～🌿✨"
    assert strip_dsml(normal) == normal


def test_strip_dsml_mixed_content():
    """混合内容：正常文本 + DSML 泄漏 + 正常文本，应只保留正常文本。"""
    from utils.text_utils import strip_dsml
    mixed = "爸爸你好～\n<｜DSML｜function_calls>\n<｜DSML｜invoke name=\"recall\">\n<｜DSML｜parameter name=\"query\">test\n</function_calls>\n小妲在呢～"
    result = strip_dsml(mixed)
    assert "爸爸你好" in result
    assert "小妲在呢" in result
    assert "DSML" not in result
    assert "function_calls" not in result
    assert "invoke name" not in result


# ========== has_dsml_tool_calls 测试 ==========

def test_has_dsml_single_pipe():
    """L1: has_dsml_tool_calls 应检测单竖线 DSML。"""
    from utils.text_utils import has_dsml_tool_calls
    assert has_dsml_tool_calls(SINGLE_PIPE_DSML), "单竖线 DSML 未被检测到"


def test_has_dsml_double_pipe():
    """双竖线 DSML 应继续被检测到（不回退）。"""
    from utils.text_utils import has_dsml_tool_calls
    assert has_dsml_tool_calls(DOUBLE_PIPE_DSML), "双竖线 DSML 未被检测到"


def test_has_dsml_bare_function_calls():
    """裸 function_calls 应被检测到。"""
    from utils.text_utils import has_dsml_tool_calls
    assert has_dsml_tool_calls(BARE_FUNCTION_CALLS), "裸 function_calls 未被检测到"


def test_has_dsml_normal_text():
    """正常文本不应被误判为 DSML。"""
    from utils.text_utils import has_dsml_tool_calls
    assert not has_dsml_tool_calls("爸爸你好～今天天气真好"), "正常文本被误判为 DSML"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
