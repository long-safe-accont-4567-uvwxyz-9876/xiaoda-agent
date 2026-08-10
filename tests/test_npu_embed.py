from local_ai.contracts import ComputeDevice, DeviceState
from memory import npu_embed


def test_probe_npu_delegates_to_evidence_probe(monkeypatch, tmp_path):
    runner = tmp_path / "runner"
    runner.write_bytes(b"")
    calls = []
    device = ComputeDevice(
        id="npu:vip:0",
        name="VIP NPU",
        kind="npu",
        architecture="unknown",
        state=DeviceState.AVAILABLE,
        memory_total=0,
        memory_available=0,
        evidence={"available": True, "probe": "runner_exit_code"},
    )

    def probe(path, *, timeout_s):
        calls.append((path, timeout_s))
        return device

    monkeypatch.setattr(npu_embed, "probe_vip_backend", probe, raising=False)

    assert npu_embed.probe_npu(str(runner), timeout_s=2.5) is True
    assert calls == [(str(runner), 2.5)]


def test_probe_npu_returns_false_without_vip_evidence(monkeypatch, tmp_path):
    runner = tmp_path / "runner"
    runner.write_bytes(b"")
    monkeypatch.setattr(
        npu_embed,
        "probe_vip_backend",
        lambda path, *, timeout_s: None,
        raising=False,
    )

    assert npu_embed.probe_npu(str(runner)) is False
