# tests/test_intent_decomposition.py
import pytest
from core.intent_decomposition import IntentFactor, DecomposedOutput, IntentDecomposer


def _rule_decomposer() -> IntentDecomposer:
    return IntentDecomposer(use_llm_decomposition=False)


@pytest.mark.asyncio
async def test_encode_knowledge():
    decomposer = _rule_decomposer()
    output = "根据资料显示，研究表明这个方法有效。据统计成功率高达90%。"
    result = await decomposer.encode(output)
    assert any(f.name == "knowledge" for f in result.factors)
    assert result.factors[0].activation > 0


@pytest.mark.asyncio
async def test_encode_emotional():
    decomposer = _rule_decomposer()
    output = "别担心，我理解你的感受，加油！我会陪伴你。"
    result = await decomposer.encode(output)
    assert any(f.name == "emotional" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_safety():
    decomposer = _rule_decomposer()
    output = "请注意，这样做有安全风险，不建议如此操作，请谨慎。"
    result = await decomposer.encode(output)
    assert any(f.name == "safety" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_creative():
    decomposer = _rule_decomposer()
    output = "可以试试这个创意，不如想象一下如果这样做会怎样？"
    result = await decomposer.encode(output)
    assert any(f.name == "creative" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_mixed_intents():
    decomposer = _rule_decomposer()
    output = "根据资料，这个方法有效。别担心，加油！请注意安全风险。"
    result = await decomposer.encode(output)
    assert len(result.factors) >= 2


@pytest.mark.asyncio
async def test_encode_empty_output():
    decomposer = _rule_decomposer()
    result = await decomposer.encode("")
    assert len(result.factors) == 0
    assert result.residual == 1.0


@pytest.mark.asyncio
async def test_dominant_intent():
    decomposer = _rule_decomposer()
    output = "根据资料资料显示研究表明据统计据报道"  # 多个知识关键词
    result = await decomposer.encode(output)
    dominant = result.dominant_intent
    assert dominant is not None
    assert dominant.name == "knowledge"


@pytest.mark.asyncio
async def test_sparsity():
    decomposer = _rule_decomposer()
    output = "根据资料显示这个方法有效。"  # 仅知识意图
    result = await decomposer.encode(output)
    # 只有 1 个活跃意图，7 个总数，稀疏度 = 1 - 1/7
    assert result.sparsity > 0.5


@pytest.mark.asyncio
async def test_residual():
    decomposer = _rule_decomposer()
    output = "qwerty asdf zxcv"  # 无匹配意图
    result = await decomposer.encode(output)
    assert result.residual == 1.0


@pytest.mark.asyncio
async def test_raw_output_preserved():
    decomposer = _rule_decomposer()
    output = "测试文本"
    result = await decomposer.encode(output)
    assert result.raw_output == output


@pytest.mark.asyncio
async def test_default_use_llm_is_true():
    decomposer = IntentDecomposer()
    assert decomposer.use_llm is True


@pytest.mark.asyncio
async def test_llm_encode_fallback_to_rules_when_no_backend():
    decomposer = IntentDecomposer(use_llm_decomposition=False)
    output = "根据资料显示这个方法有效。"
    result = await decomposer.encode(output)
    assert any(f.name == "knowledge" for f in result.factors)


@pytest.mark.asyncio
async def test_parse_llm_response_valid_json():
    decomposer = _rule_decomposer()
    raw = '{"factors": [{"name": "knowledge", "activation": 0.8, "evidence": "根据资料"}, {"name": "emotional", "activation": 0.3, "evidence": "别担心"}], "residual": 0.1}'
    result = decomposer._parse_llm_response(raw, "根据资料显示别担心")
    assert len(result.factors) == 2
    assert result.factors[0].name == "knowledge"
    assert result.factors[0].activation == 0.8
    assert result.residual == 0.1


@pytest.mark.asyncio
async def test_parse_llm_response_markdown_wrapped():
    decomposer = _rule_decomposer()
    raw = '```json\n{"factors": [{"name": "safety", "activation": 0.9, "evidence": "请注意安全"}], "residual": 0.2}\n```'
    result = decomposer._parse_llm_response(raw, "请注意安全风险")
    assert len(result.factors) == 1
    assert result.factors[0].name == "safety"


@pytest.mark.asyncio
async def test_parse_llm_response_invalid_json_fallback():
    decomposer = _rule_decomposer()
    raw = "this is not json"
    result = decomposer._parse_llm_response(raw, "根据资料显示")
    assert any(f.name == "knowledge" for f in result.factors)


@pytest.mark.asyncio
async def test_parse_llm_response_invalid_intent_name_filtered():
    decomposer = _rule_decomposer()
    raw = '{"factors": [{"name": "unknown_intent", "activation": 0.9, "evidence": "test"}], "residual": 0.1}'
    result = decomposer._parse_llm_response(raw, "测试文本")
    assert len(result.factors) == 0