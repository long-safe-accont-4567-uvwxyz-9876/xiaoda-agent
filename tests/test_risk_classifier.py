"""执行纪律层单元测试 — 危险分级 + 证据门禁 + 改完验证"""

from core.risk_classifier import EvidenceGate, PostValidator, RiskClassifier, RiskLevel


# ── RiskLevel 枚举值 ──


def test_risk_level_values():
    assert RiskLevel.SAFE == 0
    assert RiskLevel.LOW == 1
    assert RiskLevel.MEDIUM == 2
    assert RiskLevel.HIGH == 3
    assert RiskLevel.FORBIDDEN == 4


# ── RiskClassifier.classify ──


def test_classify_safe_tool():
    rc = RiskClassifier()
    assert rc.classify("read_file", {}) == RiskLevel.SAFE


def test_classify_medium_tool():
    rc = RiskClassifier()
    assert rc.classify("write_file", {}) == RiskLevel.MEDIUM


def test_classify_forbidden_command():
    rc = RiskClassifier()
    assert rc.classify("shell_command", {"command": "rm -rf /"}) == RiskLevel.FORBIDDEN


def test_classify_forbidden_sql():
    rc = RiskClassifier()
    assert rc.classify("shell_command", {"sql": "DROP TABLE users"}) == RiskLevel.FORBIDDEN


def test_classify_high_risk_command():
    rc = RiskClassifier()
    assert rc.classify("shell_command", {"command": "rm -r dir"}) == RiskLevel.HIGH


def test_classify_high_risk_sql():
    rc = RiskClassifier()
    assert rc.classify("shell_command", {"sql": "DELETE FROM users"}) == RiskLevel.HIGH


# ── RiskClassifier.pre_check ──


def test_pre_check_allows_safe():
    rc = RiskClassifier()
    result = rc.pre_check("read_file", {})
    assert result["allow"] is True


def test_pre_check_blocks_forbidden():
    rc = RiskClassifier()
    result = rc.pre_check("shell_command", {"command": "rm -rf /"})
    assert result["allow"] is False


def test_pre_check_needs_confirm_high():
    rc = RiskClassifier()
    result = rc.pre_check("shell_command", {"command": "rm -r dir"})
    assert result["need_confirm"] is True
    assert result["allow"] is False


def test_pre_check_evidence_gate_medium():
    rc = RiskClassifier()
    result = rc.pre_check("write_file", {"file_path": "/tmp/test.txt"}, has_read_target=False)
    assert result["allow"] is False
    assert "证据门禁" in result["reason"]


def test_pre_check_evidence_gate_passed():
    rc = RiskClassifier()
    result = rc.pre_check("write_file", {"file_path": "/tmp/test.txt"}, has_read_target=True)
    assert result["allow"] is True


# ── EvidenceGate ──


def test_evidence_gate_mark_and_check():
    gate = EvidenceGate()
    gate.mark_read("/tmp/test.txt")
    assert gate.has_read("/tmp/test.txt") is True
    assert gate.has_read("/tmp/other.txt") is False


def test_evidence_gate_clear():
    gate = EvidenceGate()
    gate.mark_read("/tmp/test.txt")
    gate.clear()
    assert gate.has_read("/tmp/test.txt") is False


# ── PostValidator.validate ──


def test_post_validate_json_valid():
    result = PostValidator.validate(
        "write_file",
        {"file_path": "data.json", "output": '{"key": "value"}'},
        RiskLevel.MEDIUM,
    )
    assert result["valid"] is True


def test_post_validate_json_invalid():
    result = PostValidator.validate(
        "write_file",
        {"file_path": "data.json", "output": "{invalid json}"},
        RiskLevel.MEDIUM,
    )
    assert result["valid"] is False
    assert "JSON" in result["reason"]


def test_post_validate_python_valid():
    result = PostValidator.validate(
        "write_file",
        {"file_path": "script.py", "output": "x = 1 + 2"},
        RiskLevel.MEDIUM,
    )
    assert result["valid"] is True


def test_post_validate_python_invalid():
    result = PostValidator.validate(
        "write_file",
        {"file_path": "script.py", "output": "def foo("},
        RiskLevel.MEDIUM,
    )
    assert result["valid"] is False
    assert "语法错误" in result["reason"]


def test_post_validate_low_risk_skipped():
    # L0/L1 不验证，直接返回 valid=True
    result = PostValidator.validate("read_file", {"output": "anything"}, RiskLevel.SAFE)
    assert result["valid"] is True

    result = PostValidator.validate("create_file", {"output": "anything"}, RiskLevel.LOW)
    assert result["valid"] is True
