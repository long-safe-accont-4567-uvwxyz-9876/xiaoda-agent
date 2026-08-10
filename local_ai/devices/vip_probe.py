from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from local_ai.contracts import ComputeDevice, DeviceState, ExecutionBackend, RuntimeKind


def _reject_non_finite(value: str) -> None:
    raise ValueError(value)


def parse_vip_probe(payload: str) -> ComputeDevice | None:
    try:
        evidence = json.loads(payload, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(evidence, dict) or evidence.get("available") is not True:
        return None
    model = evidence.get("model")
    architecture = evidence.get("architecture")
    return ComputeDevice(
        id="npu:vip:0",
        name=model.strip() if isinstance(model, str) and model.strip() else "VIP NPU",
        kind="npu",
        architecture=(
            architecture.strip()
            if isinstance(architecture, str) and architecture.strip()
            else "unknown"
        ),
        state=DeviceState.AVAILABLE,
        memory_total=0,
        memory_available=0,
        backends=(
            ExecutionBackend(
                runtime=RuntimeKind.VIP,
                provider="VIPLite",
                healthy=True,
                evidence=evidence,
            ),
        ),
        system={"platform": "linux"},
        evidence=evidence,
    )


def probe_vip_backend(
    runner_path: str,
    *,
    timeout_s: float = 15.0,
    command_runner: Callable[..., Any] = subprocess.run,
    platform: str | None = None,
) -> ComputeDevice | None:
    target = (platform or sys.platform).casefold()
    path = Path(runner_path)
    if not target.startswith("linux") or not path.is_file():
        return None
    try:
        result = command_runner(
            ["sudo", "-n", str(path), "--probe", "--quiet"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    stdout = result.stdout.strip()
    if stdout:
        return parse_vip_probe(stdout)
    return parse_vip_probe('{"available":true,"probe":"runner_exit_code"}')
