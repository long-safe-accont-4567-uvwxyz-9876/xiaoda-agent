# tests/test_intent_decomposition.py
import pytest
from core.intent_decomposition import IntentFactor, DecomposedOutput, IntentDecomposer


@pytest.mark.asyncio
async def test_encode_knowledge():
    decomposer = IntentDecomposer()
    output = "根据资料显示，研究表明这个方法有效。据统计成功率高达90%。"
    result = await decomposer.encode(output)
    assert any(f.name == "knowledge" for f in result.factors)
    assert result.factors[0].activation > 0


@pytest.mark.asyncio
async def test_encode_emotional():
    decomposer = IntentDecomposer()
    output = "别担心，我理解你的感受，加油！我会陪伴你。"
    result = await decomposer.encode(output)
    assert any(f.name == "emotional" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_safety():
    decomposer = IntentDecomposer()
    output = "请注意，这样做有安全风险，不建议如此操作，请谨慎。"
    result = await decomposer.encode(output)
    assert any(f.name == "safety" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_creative():
    decomposer = IntentDecomposer()
    output = "可以试试这个创意，不如想象一下如果这样做会怎样？"
    result = await decomposer.encode(output)
    assert any(f.name == "creative" for f in result.factors)


@pytest.mark.asyncio
async def test_encode_mixed_intents():
    decomposer = IntentDecomposer()
    output = "根据资料，这个方法有效。别担心，加油！请注意安全风险。"
    result = await decomposer.encode(output)
    assert len(result.factors) >= 2


@pytest.mark.asyncio
async def test_encode_empty_output():
    decomposer = IntentDecomposer()
    result = await decomposer.encode("")
    assert len(result.factors) == 0
    assert result.residual == 1.0


@pytest.mark.asyncio
async def test_dominant_intent():
    decomposer = IntentDecomposer()
    output = "根据资料资料显示研究表明据统计据报道"  # 多个知识关键词
    result = await decomposer.encode(output)
    dominant = result.dominant_intent
    assert dominant is not None
    assert dominant.name == "knowledge"


@pytest.mark.asyncio
async def test_sparsity():
    decomposer = IntentDecomposer()
    output = "根据资料显示这个方法有效。"  # 仅知识意图
    result = await decomposer.encode(output)
    # 只有 1 个活跃意图，7 个总数，稀疏度 = 1 - 1/7
    assert result.sparsity > 0.5


@pytest.mark.asyncio
async def test_residual():
    decomposer = IntentDecomposer()
    output = "hello world"  # 无匹配意图
    result = await decomposer.encode(output)
    assert result.residual == 1.0


@pytest.mark.asyncio
async def test_raw_output_preserved():
    decomposer = IntentDecomposer()
    output = "测试文本"
    result = await decomposer.encode(output)
    assert result.raw_output == output
