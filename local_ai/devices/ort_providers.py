from __future__ import annotations

import base64
from typing import Any

import numpy as np
from loguru import logger

from local_ai.contracts import ExecutionBackend, RuntimeKind
from local_ai.runtimes.session_tuning import build_session_options

_PROBE_MODEL = base64.b64decode(
    "CAg6VgoZCgVpbnB1dBIGb3V0cHV0IghJZGVudGl0eRIOcHJvdmlkZXJfcHJvYmVaEwoFaW5wdXQSCgoICAESBAoCCAFiFAoGb3V0cHV0EgoKCAgBEgQKAggBQgQKABAN"
)


class OrtProviderProbe:
    def __init__(self, ort_module: Any) -> None:
        self._ort = ort_module

    def list_available(self) -> tuple[str, ...]:
        try:
            providers = self._ort.get_available_providers()
        except (OSError, RuntimeError, AttributeError) as e:
            logger.debug("ort_providers.list_failed error={}", str(e))
            return ()
        except Exception:
            logger.exception("ort_providers.list_available.unexpected_error")
            return ()
        return tuple(
            provider
            for provider in providers
            if isinstance(provider, str) and provider.strip()
        )

    def verify(
        self, provider: str, provider_options: dict[str, Any] | None = None
    ) -> ExecutionBackend:
        evidence: dict[str, Any] = {"probe": "minimal_inference"}
        try:
            session_options = build_session_options([provider], ort_module=self._ort)
            session = self._ort.InferenceSession(
                _PROBE_MODEL,
                providers=[provider],
                provider_options=[provider_options] if provider_options is not None else None,
                sess_options=session_options,
            )
            active_providers = list(session.get_providers())
            evidence["active_providers"] = active_providers
            if provider not in active_providers:
                raise RuntimeError(
                    f"provider {provider} fell back to {active_providers}"
                )
            session.run(None, {"input": np.zeros((1,), dtype=np.float32)})
            healthy = True
        except (OSError, RuntimeError, ValueError) as error:
            healthy = False
            evidence["error"] = str(error)
            logger.debug("ort_providers.verify_failed provider={} error={}", provider, str(error)[:200])
        except Exception as error:
            healthy = False
            evidence["error"] = str(error)
            logger.exception("ort_providers.verify.unexpected_error provider={}", provider)
        return ExecutionBackend(
            runtime=RuntimeKind.ORT,
            provider=provider,
            healthy=healthy,
            options=provider_options or {},
            evidence=evidence,
        )