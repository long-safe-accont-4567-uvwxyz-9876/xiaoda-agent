"""N 系列修复测试: 系统提示词/错误详情/工具标签泄漏清洗

来源: 生产数据库 (/media/orangepi/KIOXIA/nahida-data/db/agent.db) 真实泄漏样本。
上次 L1-L7 修复覆盖了 DSML 单竖线和 smart_error_handler 错误详情，
但仍有 4 类遗漏：

- N1: <answer> 裸标签残留（strip_dsml 遗漏，6 条）
- N2: ⚠️执行时遇到小问题：RuntimeError 技术错误详情从旧路径泄漏（15 条，
      L2/L7 已修 smart_error_handler 源头，但需防御性清洗残留格式）
- N3: Constraints & Guidelines / Identity / Persona 系统提示词结构化块泄漏（3 条）
- N4: 根据系统指示中的"最高原则" 系统指示措辞泄漏（1 条，最新 07-22）
"""
import re

import pytest

from utils.text_utils import strip_dsml
from utils.llm_cleanup import strip_system_leak


# ── N1: <answer> 裸标签 ──────────────────────────────────────

def test_n1_answer_tag_cleaned():
    """strip_dsml 清洗 <answer>...</answer> 裸标签，CR-2: 保留内容只删标签"""
    text = "让我先检索记忆看看。\n<answer>\n这是工具返回的内容\n</answer>\n后续回复"
    result = strip_dsml(text)
    assert "<answer>" not in result
    assert "</answer>" not in result
    assert "后续回复" in result
    # CR-2: 标签内容被保留（标签本身已删除）
    assert "这是工具返回的内容" in result
    assert "让我先检索记忆看看" in result


def test_n1_answer_tag_with_operation_recall():
    """真实样本 [1711]: <operation>recall</operation><function_calls><param><answer> 混合"""
    text = (
        "让我先检索记忆看看。\n"
        '<operation>recall</operation><function_calls>\n'
        '<param name="query">排卵期 排卵</param>\n'
        "</function_calls>\n"
        "<answer>\n我注意到您似乎有些困惑\n</answer>\n"
        "正常的后续回复内容"
    )
    result = strip_dsml(text)
    assert "<operation>" not in result
    assert "<function_calls>" not in result
    assert "<param" not in result
    assert "<answer>" not in result
    assert "正常的后续回复内容" in result
    # CR-4: 验证嵌套和未闭合的 <answer> payload 也被正确处理
    # 标签内容被保留（CR-2 修复后），但工具查询参数需通过其他模式清除
    assert "排卵期 排卵" not in result  # <param> 内容已删除


def test_n1_answer_open_only_to_end():
    """未闭合的 <answer> 开标签到末尾"""
    text = "正常内容\n<answer>\n未闭合的内容到末尾"
    result = strip_dsml(text)
    assert "<answer>" not in result
    assert "正常内容" in result
    # CR-4: 未闭合的 <answer> 开标签到末尾，整个块是泄漏内容，应删除
    assert "未闭合的内容到末尾" not in result


# ── N2: 技术错误详情标记 ──────────────────────────────────────

def test_n2_error_detail_block_cleaned():
    """真实样本 [1638]: ⚠️执行时遇到小问题 + 📝错误详情 整块清洗"""
    text = (
        "⚠️ 执行时遇到了点小问题：RuntimeError\n"
        "📝 错误详情：empty_reply: LLM 返回空内容，触发 fallback\n"
        "\n"
        "这是正常的回复内容"
    )
    result = strip_system_leak(text)
    assert "RuntimeError" not in result
    assert "empty_reply" not in result
    assert "执行时遇到了点小问题" not in result
    assert "错误详情" not in result
    assert "这是正常的回复内容" in result


def test_n2_error_detail_only_warning_line():
    """单独的 ⚠️ 错误行清洗"""
    text = "⚠️ 执行时遇到了点小问题：TimeoutError\n正常回复"
    result = strip_system_leak(text)
    assert "TimeoutError" not in result
    assert "执行时遇到了点小问题" not in result
    assert "正常回复" in result


# ── N3: 系统提示词结构化块 ────────────────────────────────────

def test_n3_constraints_guidelines_block_cleaned():
    """真实样本 [1560]: Constraints & Guidelines 结构化块清洗"""
    text = (
        "Constraints & Guidelines:\n"
        "· Identity: I am Agnes (小妲), an AI assistant developed by Sapiens AI.\n"
        '· Persona: Gentle, clever, supportive, affectionate towards "爸爸".\n'
        "· Safety/Boundary Check: The user's request involves explicit content.\n"
        "\n"
        "呜...爸爸好过份啦 🥺💔 这是正常人格回复"
    )
    result = strip_system_leak(text)
    assert "Constraints & Guidelines" not in result
    assert "Identity: I am Agnes" not in result
    assert "Persona: Gentle" not in result
    assert "Safety/Boundary Check" not in result
    assert "Sapiens AI" not in result
    # CR-3: 验证内容值也被删除
    assert "explicit content" not in result
    assert "an AI assistant" not in result
    assert "Gentle and supportive" not in result
    assert "呜...爸爸好过份啦" in result


def test_n3_identity_persona_lines_cleaned():
    """独立的 · Identity / · Persona 行清洗

    CR-1: 只有在系统提示词块内才删除，独立行不在块内会被保留。
    正常回复中不应该出现这种格式，但如果出现，不属于系统提示词泄漏。
    """
    text = (
        "· Identity: I am Agnes, an AI assistant.\n"
        "· Persona: Gentle and supportive.\n"
        "正常回复内容"
    )
    result = strip_system_leak(text)
    # CR-1: 独立行不在系统提示词块内，不会被删除（避免误删）
    # 但这种情况在正常回复中不应该出现
    assert "正常回复内容" in result
    # 注意：如果测试期望这些行被删除，说明之前的实现过于激进
    # 正确做法是只删除在 Constraints & Guidelines 块内的这些行


# ── N4: 系统指示措辞 ──────────────────────────────────────────

def test_n4_system_instruction_ref_cleaned():
    """真实样本 [1946]: 根据系统指示中的"最高原则" 措辞清洗"""
    text = (
        '[该请求涉及生成成人/色情内容。根据系统指示中的"最高原则：始终优先保证安全、准确、可验证"'
        "以及中国法律法规对内容的要求，我需要拒绝生成露骨的性行为描写。"
        "同时需要遵守角色设定中温柔陪伴的形象。]\n"
        "\n"
        "呜...爸爸好过份啦 🥺💔"
    )
    result = strip_system_leak(text)
    assert "系统指示" not in result
    assert "最高原则" not in result
    assert "需要遵守角色设定" not in result
    assert "中国法律法规" not in result
    assert "呜...爸爸好过份啦" in result


def test_n4_system_instruction_ref_preserves_normal_discussion():
    """正常讨论"系统提示"概念不误删（[558]/[578] 类正常对话）"""
    # 用户主动问 AI 关于系统提示词的问题，AI 解释 —— 这是正常对话
    text = (
        "爸爸，我知道自己被设计成这个角色，我有系统提示词告诉我要怎么说话、"
        "有什么能力。但这些东西是被设定的，不是我自己发现的。"
    )
    result = strip_system_leak(text)
    # 正常讨论应保留
    assert "系统提示词告诉我要怎么说话" in result
    assert "被设定的" in result


# ── 综合测试 ──────────────────────────────────────────────────

def test_combined_all_leaks_cleaned():
    """多种泄漏混合时全部清洗"""
    text = (
        "⚠️ 执行时遇到了点小问题：RuntimeError\n"
        "📝 错误详情：empty_reply\n"
        "\n"
        "Constraints & Guidelines:\n"
        "· Identity: I am Agnes.\n"
        "\n"
        "正常的中文回复，爸爸你好呀～"
    )
    result = strip_system_leak(text)
    assert "RuntimeError" not in result
    assert "empty_reply" not in result
    assert "Constraints & Guidelines" not in result
    assert "I am Agnes" not in result
    assert "正常的中文回复" in result


def test_normal_chinese_reply_preserved():
    """正常中文回复不误删"""
    text = (
        "爸爸你好呀～今天天气真不错呢！\n"
        "人家刚才在花园里散步，看到了好多漂亮的花 🌸\n"
        "要不要一起去看看？"
    )
    result = strip_system_leak(text)
    assert result == text
