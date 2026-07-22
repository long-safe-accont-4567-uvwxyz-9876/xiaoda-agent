"""P0 测试: prompt 模板不应使用双花括号 {{}} 转义。

背景: 项目已从 str.format() 迁移到 str.replace() 处理占位符替换，
str.replace() 不处理 {{}} 转义，因此模板里的 {{}} 会被原样发给 LLM，
导致 LLM 看到错误的 JSON 语法（如 `{{"entities": []}}` 而非 `{"entities": []}`）。

参考 project_memory:
- "str.format() 在用户/LLM 内容上是定时炸弹"
- "entity_extractor._llm_prompt_template 改用 str.replace 后，模板里的 {{...}} 转义必须改为 {...}"
"""
import re

import pytest


def _has_double_braces(text: str) -> list[str]:
    """检测字符串中是否存在 {{ 或 }}} 模式（str.replace 场景下属于 bug）。"""
    return re.findall(r"\{\{|\}\}", text)


# ---------- P0-1: ENTITY_EXTRACT_PROMPT_V2 ----------

def test_entity_extract_prompt_v2_no_double_braces():
    """ENTITY_EXTRACT_PROMPT_V2 中不应有 {{}} 转义。"""
    from memory.knowledge_graph_v2 import ENTITY_EXTRACT_PROMPT_V2

    offenders = _has_double_braces(ENTITY_EXTRACT_PROMPT_V2)
    assert not offenders, (
        f"ENTITY_EXTRACT_PROMPT_V2 仍含双花括号（str.replace 不处理转义）: {offenders}"
    )


def test_entity_extract_prompt_v2_has_valid_json_example():
    """ENTITY_EXTRACT_PROMPT_V2 必须包含合法的 JSON 示例（单花括号）。"""
    from memory.knowledge_graph_v2 import ENTITY_EXTRACT_PROMPT_V2

    # LLM 应看到 {"entities": ...} 而不是 {{...}}
    assert '{"entities"' in ENTITY_EXTRACT_PROMPT_V2, "缺少合法 JSON 示例"
    assert '"kind"' in ENTITY_EXTRACT_PROMPT_V2
    assert '"relations"' in ENTITY_EXTRACT_PROMPT_V2


def test_entity_extract_prompt_v2_replace_works():
    """模拟实际调用: str.replace 后 prompt 应是合法的（无双花括号残留）。"""
    from memory.knowledge_graph_v2 import ENTITY_EXTRACT_PROMPT_V2

    summary = "用户今天聊了打篮球和看动漫的事情"
    prompt = ENTITY_EXTRACT_PROMPT_V2.replace("{summary}", summary[:500])

    # 替换后不应有残留占位符
    assert "{summary}" not in prompt
    # 不应有双花括号
    assert not _has_double_braces(prompt), f"replace 后仍残留双花括号: {prompt[:200]}"


# ---------- P0-2: CONTRADICTION_PROMPT ----------

def test_contradiction_prompt_no_double_braces():
    """CONTRADICTION_PROMPT 中不应有 {{}} 转义。"""
    from memory.knowledge_graph_v2 import CONTRADICTION_PROMPT

    offenders = _has_double_braces(CONTRADICTION_PROMPT)
    assert not offenders, f"CONTRADICTION_PROMPT 仍含双花括号: {offenders}"


def test_contradiction_prompt_has_valid_json_instruction():
    """CONTRADICTION_PROMPT 应输出合法的 JSON 指令（CodeRabbit F2: 用 json.loads 验证）。"""
    import json
    from memory.knowledge_graph_v2 import CONTRADICTION_PROMPT

    # 应包含 {"contradicted_indices": ...} 而不是 {{...}}
    assert '{"contradicted_indices"' in CONTRADICTION_PROMPT
    # CodeRabbit F2: 提取示例并用 json.loads 验证是合法 JSON
    example = '{"contradicted_indices": [0, 2]}'
    assert example in CONTRADICTION_PROMPT, \
        f"CONTRADICTION_PROMPT 应包含具体索引示例 {example}"
    parsed = json.loads(example)
    assert parsed == {"contradicted_indices": [0, 2]}, \
        f"示例 JSON 解析结果不符预期: {parsed}"


def test_contradiction_prompt_replace_works():
    """模拟实际调用: 替换占位符后 prompt 应合法。"""
    from memory.knowledge_graph_v2 import CONTRADICTION_PROMPT

    prompt = (
        CONTRADICTION_PROMPT
        .replace("{new_fact}", "用户喜欢打篮球")
        .replace("{existing_facts_list}", "0: 用户喜欢踢足球")
    )

    assert "{new_fact}" not in prompt
    assert "{existing_facts_list}" not in prompt
    assert not _has_double_braces(prompt), f"replace 后仍残留双花括号: {prompt[:200]}"


# ---------- P0-3: LLM_JUDGE_RUBRIC ----------

def test_llm_judge_rubric_no_double_braces():
    """LLM_JUDGE_RUBRIC 中不应有 {{}} 转义。"""
    from memory.matrix_governance import LLM_JUDGE_RUBRIC

    offenders = _has_double_braces(LLM_JUDGE_RUBRIC)
    assert not offenders, f"LLM_JUDGE_RUBRIC 仍含双花括号: {offenders}"


def test_llm_judge_rubric_has_valid_json_instruction():
    """LLM_JUDGE_RUBRIC 应输出合法的 JSON 评分指令。"""
    from memory.matrix_governance import LLM_JUDGE_RUBRIC

    # 应包含 {"score": ...} 而不是 {{...}}
    assert '"score"' in LLM_JUDGE_RUBRIC
    assert '{"score"' in LLM_JUDGE_RUBRIC, "缺少合法 JSON 评分示例"


def test_llm_judge_rubric_replace_works():
    """模拟实际调用: 替换占位符后 prompt 应合法。"""
    from memory.matrix_governance import LLM_JUDGE_RUBRIC

    prompt = (
        LLM_JUDGE_RUBRIC
        .replace("{user_input}", "你好")
        .replace("{reference_answer}", "(无参考答案)")
        .replace("{response}", "你好呀，今天天气不错")
    )

    assert "{user_input}" not in prompt
    assert "{reference_answer}" not in prompt
    assert "{response}" not in prompt
    assert not _has_double_braces(prompt), f"replace 后仍残留双花括号: {prompt[:200]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
