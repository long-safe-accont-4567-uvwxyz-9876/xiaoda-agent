from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_dispatcher import SubAgent, SubAgentConfig


def _config() -> SubAgentConfig:
    return SubAgentConfig(
        name="xiaoke",
        display_name="小可",
        provider="test-provider",
        model="test-model",
        base_url="https://example.test/v1",
        api_key_env="TEST_API_KEY",
    )


@pytest.mark.asyncio
async def test_subagent_uses_core_shared_router_without_own_client():
    router = MagicMock()
    router.route_config = AsyncMock(return_value="共享路由回复")
    core = MagicMock(router=router)
    agent = SubAgent(config=_config(), tts=None, core=core)

    await agent.init()
    with patch("config.get_temperature", return_value=0.9):
        result = await agent._call_llm_one_round(
            working=[{"role": "user", "content": "hi"}],
            tools=None,
            remaining=120,
            round_idx=0,
        )

    assert "_client" not in vars(agent)
    assert result.choices[0].message.content == "共享路由回复"
    router.route_config.assert_awaited_once_with(
        config={"client": "test-provider", "model": "test-model", "max_tokens": 3072},
        messages=[{"role": "user", "content": "hi"}],
        temperature=pytest.approx(0.9),
        max_tokens=3072,
        tools=None,
        tool_choice=None,
        timeout=60,
    )
