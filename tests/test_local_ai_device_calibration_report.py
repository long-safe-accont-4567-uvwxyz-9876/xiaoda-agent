import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs/superpowers/specs/2026-08-10-typed-local-ai-resource-requirements-design.md"
REPORTS = (
    ROOT / "task-3-report.md",
    ROOT / ".superpowers/sdd/task-3-report.md",
)


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
    for report in REPORTS:
        content = report.read_text(encoding="utf-8")
        assert "可见设备校准" in content
        assert "3 failed, 18 passed" in content
        assert "22 passed" in content
        assert "4 failed, 24 deselected" in content
        assert "28 passed" in content
