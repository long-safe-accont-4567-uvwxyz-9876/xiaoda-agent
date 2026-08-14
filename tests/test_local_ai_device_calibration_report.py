import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs/superpowers/specs/2026-08-10-typed-local-ai-resource-requirements-design.md"
PROBE_TESTS = ROOT / "tests/test_local_ai_system_probe.py"


def test_device_design_records_visibility_calibration_contract():
    content = DESIGN.read_text(encoding="utf-8")

    assert "CUDA_VISIBLE_DEVICES" in content
    assert "NVIDIA_VISIBLE_DEVICES" in content
    assert "规范化后完全一致" in content
    assert "完整序列" in content
    assert "长度、逐项身份和顺序" in content
    assert "返回空映射" in content
    assert "AMD" in content
    assert "完整 PCI BDF" in content


def test_task_reports_record_visibility_calibration_red_green_evidence():
    """设备校准契约的稳定来源是设计文档 + 真实回归测试。

    历史 SDD 证据文件（task-3-report.md / .superpowers/sdd/task-3-report.md）
    会被后续任务反复覆盖，不能作为稳定断言依据；改用设计文档契约与实现测试
    作为红绿证据的稳定来源。
    """
    design = DESIGN.read_text(encoding="utf-8")
    assert "可见设备校准" in design

    probe = PROBE_TESTS.read_text(encoding="utf-8")
    assert "_calibrate_rocm_ordinals" in probe
    assert "CUDA_VISIBLE_DEVICES" in probe
    assert "NVIDIA_VISIBLE_DEVICES" in probe
