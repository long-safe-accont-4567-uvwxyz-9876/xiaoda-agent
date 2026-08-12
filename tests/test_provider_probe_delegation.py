from types import SimpleNamespace

import pytest

from llm_gateway.contracts import ProviderCapabilities
from llm_gateway.transports import CapabilityReport
from web.probes import probe_provider


@pytest.mark.asyncio
async def test_provider_probe_delegates_to_provider_service_capabilities():
    calls = []

    class Service:
        async def capabilities(self, provider_id):
            calls.append(provider_id)
            return CapabilityReport(
                True,
                ProviderCapabilities(tools=True),
                models=("model-a",),
            )

    result = await probe_provider(SimpleNamespace(), "custom", Service())

    assert calls == ["custom"]
    assert result == {
        "ok": True,
        "latency_ms": result["latency_ms"],
        "models": ["model-a"],
        "error": "",
    }
