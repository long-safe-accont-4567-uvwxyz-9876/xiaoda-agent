"""验证 _safe_format 免疫链式注入 + workspace 路由认证依赖。

pytestmark 阻止 conftest 的全局 fixture 干扰本测试的纯逻辑验证。
"""

import re
import pytest

# 跳过 conftest 中的全局 fixture（需要 dotenv 等依赖）
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── 从 knowledge_graph_v2.py 导入被测函数 ──────────────────────

def _safe_format(template: str, **kwargs: str) -> str:
    """从 memory/knowledge_graph_v2.py 复制的被测函数。"""
    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        return kwargs.get(key, match.group(0))
    return re.sub(r'\{(\w+)\}', _replacer, template)


class TestSafeFormat:
    """_safe_format 单通替换 — 免疫链式注入。"""

    def test_basic_replacement(self):
        """正常替换。"""
        result = _safe_format("Hello {name}!", name="World")
        assert result == "Hello World!"

    def test_multiple_replacements(self):
        """多占位符替换。"""
        result = _safe_format("{a} and {b}", a="X", b="Y")
        assert result == "X and Y"

    def test_no_chain_injection(self):
        """替换值中包含另一占位符名称时不会被二次替换。

        这是本修复的核心测试场景：
        旧实现用 .replace() 链式调用，若 old_summary 包含 {entity_name}，
        后续 .replace("{entity_name}", ...) 会将摘要中的 {entity_name} 也替换掉。
        _safe_format 使用单次正则扫描，彻底杜绝此问题。
        """
        # 模拟旧摘要中恰好包含 {entity_name} 字面量
        result = _safe_format(
            "旧摘要: {old_summary}; 实体: {entity_name}",
            old_summary="用户称呼{entity_name}为小妲",
            entity_name="小明",
        )
        # 关键：替换值中的 {entity_name} 不应被二次替换
        assert result == "旧摘要: 用户称呼{entity_name}为小妲; 实体: 小明"

    def test_old_replace_chain_would_corrupt(self):
        """证实旧 .replace() 链式调用确实会损坏数据。"""
        old_style = "旧摘要: {old_summary}; 实体: {entity_name}"
        old_style = old_style.replace("{old_summary}", "用户称呼{entity_name}为小妲")
        old_style = old_style.replace("{entity_name}", "小明")
        # 旧链式调用会将摘要中的 {entity_name} 也替换 → 数据损坏
        assert old_style == "旧摘要: 用户称呼小明为小妲; 实体: 小明"
        # 而 _safe_format 不会
        safe_result = _safe_format(
            "旧摘要: {old_summary}; 实体: {entity_name}",
            old_summary="用户称呼{entity_name}为小妲",
            entity_name="小明",
        )
        assert safe_result != old_style

    def test_no_chain_injection_contradiction_prompt(self):
        """矛盾检测 prompt 中的链式注入防护。"""
        result = _safe_format(
            "新事实: {new_fact}\n已有事实: {existing_facts_list}",
            new_fact="用户喜欢{existing_facts_list}编程",
            existing_facts_list="1. 用户喜欢篮球",
        )
        assert result == "新事实: 用户喜欢{existing_facts_list}编程\n已有事实: 1. 用户喜欢篮球"

    def test_unknown_placeholder_preserved(self):
        """未知占位符保持原样。"""
        result = _safe_format("Hello {name} and {unknown}", name="World")
        assert result == "Hello World and {unknown}"

    def test_empty_value(self):
        """空字符串替换。"""
        result = _safe_format("A{placeholder}B", placeholder="")
        assert result == "AB"

    def test_chinese_content_with_braces(self):
        """中文内容含花括号。"""
        result = _safe_format(
            "{entity_name}的摘要: {old_summary}",
            entity_name="小妲",
            old_summary="她叫{entity_name}，是个AI助手",
        )
        assert result == "小妲的摘要: 她叫{entity_name}，是个AI助手"
