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


# ---------- P0-0: ENTITY_EXTRACT_PROMPT (V1) ----------

def test_entity_extract_prompt_v1_no_double_braces():
    """V1 ENTITY_EXTRACT_PROMPT 不应含 {{}} 转义。

    根因：V1 prompt 残留 {{}} 导致 LLM 输出 {{"entities": []}}，
    json.loads 在 char 1 处失败，触发 kg.extract_json_error 139 次。
    V2 已修复且有测试守护，V1 此前遗漏，本次补齐。
    """
    from memory.knowledge_graph import ENTITY_EXTRACT_PROMPT

    offenders = _has_double_braces(ENTITY_EXTRACT_PROMPT)
    assert not offenders, (
        f"ENTITY_EXTRACT_PROMPT (V1) 仍含双花括号（str.replace 不处理转义）: {offenders}"
    )


def test_entity_extract_prompt_v1_has_valid_json_example():
    """V1 ENTITY_EXTRACT_PROMPT 必须包含合法的 JSON 示例（单花括号）。

    CodeRabbit F1: 用 json.loads 验证示例结构，而非独立 substring 检查——
    substring 无法区分 ``{"entities": []}``（合法）与 ``{{entities}}``（非法但 substring
    仍可能命中），结构化解析才能确保 LLM 看到的是可被 json.loads 接受的合法 JSON。
    """
    import json
    import re

    from memory.knowledge_graph import ENTITY_EXTRACT_PROMPT

    # 匹配单层花括号（无嵌套）且含 entities + relations 的 JSON 示例
    match = re.search(r'\{[^{}]*"entities"[^{}]*"relations"[^{}]*\}', ENTITY_EXTRACT_PROMPT)
    assert match is not None, "ENTITY_EXTRACT_PROMPT 缺少含 entities+relations 的合法 JSON 示例"
    parsed = json.loads(match.group(0))
    assert "entities" in parsed, f"JSON 示例缺少 entities 字段: {parsed}"
    assert "relations" in parsed, f"JSON 示例缺少 relations 字段: {parsed}"


def test_entity_extract_prompt_v1_replace_works():
    """模拟实际调用: str.replace 后 V1 prompt 应合法（无双花括号残留）。

    CodeRabbit F2: 先断言模板含 ``{summary}`` 占位符（确保 replace 有目标），再断言
    替换后的 summary 内容出现在 prompt 中，保留原有占位符/双花括号检查。
    """
    from memory.knowledge_graph import ENTITY_EXTRACT_PROMPT

    assert "{summary}" in ENTITY_EXTRACT_PROMPT, "ENTITY_EXTRACT_PROMPT 缺少 {summary} 占位符"
    summary = "用户今天聊了打篮球和看动漫的事情"
    prompt = ENTITY_EXTRACT_PROMPT.replace("{summary}", summary[:500])

    assert "{summary}" not in prompt
    assert summary in prompt, "替换后 summary 内容应出现在 prompt 中"
    assert not _has_double_braces(prompt), f"replace 后仍残留双花括号: {prompt[:200]}"


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
