"""scripts/check_coverage_drop.py — 覆盖率下降检查脚本测试 (TDD).

覆盖:
    1. 覆盖率提升时输出 ::notice:: 改进消息
    2. 覆盖率下降 < 2% 时输出 ::notice:: 容差内消息
    3. 覆盖率下降 >= 2% 时输出 ::warning:: 超过阈值消息
    4. 空字符串静默返回 0 (不输出 annotation)
    5. 无效值输出 ::warning:: 错误消息 (不崩溃)
    6. 参数数量错误返回 2 (CLI 用法错误)
    7. 退出码始终为 0 (warning 不阻塞 CI, 由 continue-on-error 兜底)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "check_coverage_drop.py"


def _load_script_module():
    """以独立模块形式加载 scripts/check_coverage_drop.py.

    避免与 scripts/ 目录下其他脚本产生导入冲突.
    """
    spec = importlib.util.spec_from_file_location(
        "check_coverage_drop", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    """加载 check_coverage_drop 脚本为模块, 暴露 check_coverage_drop 函数."""
    return _load_script_module()


# ============================================================
# 1. 覆盖率提升 -> ::notice:: improved
# ============================================================
class TestCoverageImproved:
    def test_improved_outputs_notice(self, script, capsys):
        rc = script.check_coverage_drop("80.0", "85.5")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::notice::" in out
        assert "improved" in out.lower()
        # 应包含具体的覆盖率数值
        assert "80.0%" in out
        assert "85.5%" in out

    def test_improved_zero_diff_still_notice(self, script, capsys):
        """覆盖率无变化 (diff=0) 也算 improved (>= 0)."""
        rc = script.check_coverage_drop("80.0", "80.0")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::notice::" in out
        assert "improved" in out.lower() or "0.00%" in out

    def test_no_warning_when_improved(self, script, capsys):
        """提升时不应输出 ::warning::."""
        script.check_coverage_drop("50.0", "80.0")
        out = capsys.readouterr().out
        assert "::warning::" not in out


# ============================================================
# 2. 覆盖率下降 < 2% (容差内) -> ::notice:: within tolerance
# ============================================================
class TestCoverageDroppedWithinTolerance:
    def test_small_drop_outputs_notice(self, script, capsys):
        rc = script.check_coverage_drop("80.0", "79.0")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::notice::" in out
        assert "tolerance" in out.lower() or "within" in out.lower()

    def test_drop_exactly_2_percent_still_notice(self, script, capsys):
        """下降刚好 2% 仍算容差内 (阈值是 < -2, -2 不算 warning)."""
        rc = script.check_coverage_drop("80.0", "78.0")
        out = capsys.readouterr().out
        assert rc == 0
        # diff == -2.0, 不触发 warning (warning 条件是 diff < -2.0)
        assert "::warning::" not in out
        assert "::notice::" in out

    def test_drop_just_below_threshold_no_warning(self, script, capsys):
        """下降 1.99% 仍不触发 warning."""
        rc = script.check_coverage_drop("80.00", "78.01")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::warning::" not in out


# ============================================================
# 3. 覆盖率下降 >= 2% -> ::warning:: exceeding threshold
# ============================================================
class TestCoverageDroppedExceedingThreshold:
    def test_large_drop_outputs_warning(self, script, capsys):
        rc = script.check_coverage_drop("80.0", "75.0")
        out = capsys.readouterr().out
        assert rc == 0  # warning 不改变退出码
        assert "::warning::" in out
        assert "exceeding" in out.lower() or "threshold" in out.lower()

    def test_drop_exactly_2_01_percent_triggers_warning(self, script, capsys):
        """下降 2.01% 应触发 warning (超过阈值)."""
        rc = script.check_coverage_drop("80.00", "77.99")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::warning::" in out

    def test_warning_includes_diff_value(self, script, capsys):
        """warning 消息应包含具体的下降百分比."""
        script.check_coverage_drop("80.0", "70.0")
        out = capsys.readouterr().out
        # 下降 10%
        assert "10.00%" in out

    def test_warning_includes_ref_and_new_values(self, script, capsys):
        """warning 消息应包含基线和新覆盖率."""
        script.check_coverage_drop("85.5", "75.0")
        out = capsys.readouterr().out
        assert "85.5%" in out
        assert "75.0%" in out


# ============================================================
# 4. 空字符串静默返回 0 (不输出 annotation)
# ============================================================
class TestEmptyInputs:
    def test_empty_ref_returns_silently(self, script, capsys):
        rc = script.check_coverage_drop("", "80.0")
        out = capsys.readouterr().out
        assert rc == 0
        assert out == ""

    def test_empty_new_returns_silently(self, script, capsys):
        rc = script.check_coverage_drop("80.0", "")
        out = capsys.readouterr().out
        assert rc == 0
        assert out == ""

    def test_both_empty_returns_silently(self, script, capsys):
        rc = script.check_coverage_drop("", "")
        out = capsys.readouterr().out
        assert rc == 0
        assert out == ""


# ============================================================
# 5. 无效值 -> ::warning:: 错误消息 (不崩溃)
# ============================================================
class TestInvalidInputs:
    def test_non_numeric_ref_outputs_warning(self, script, capsys):
        rc = script.check_coverage_drop("not_a_number", "80.0")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::warning::" in out
        assert "Invalid" in out or "invalid" in out

    def test_non_numeric_new_outputs_warning(self, script, capsys):
        rc = script.check_coverage_drop("80.0", "NaN")
        out = capsys.readouterr().out
        assert rc == 0
        assert "::warning::" in out

    def test_none_value_handled_gracefully(self, script, capsys):
        """None 应被 ValueError/TypeError 捕获, 不应崩溃."""
        rc = script.check_coverage_drop(None, "80.0")  # type: ignore[arg-type]
        out = capsys.readouterr().out
        assert rc == 0
        assert "::warning::" in out


# ============================================================
# 6. CLI 参数错误 -> 返回 2
# ============================================================
class TestCliUsage:
    def test_no_args_returns_2(self, script, capsys):
        rc = script._main([])
        assert rc == 2
        err = capsys.readouterr().err
        assert "Usage" in err

    def test_one_arg_returns_2(self, script, capsys):
        rc = script._main(["80.0"])
        assert rc == 2

    def test_three_args_returns_2(self, script, capsys):
        rc = script._main(["80.0", "75.0", "70.0"])
        assert rc == 2

    def test_two_args_returns_0(self, script, capsys):
        rc = script._main(["80.0", "85.0"])
        assert rc == 0


# ============================================================
# 7. 边界值 / 数值精度
# ============================================================
class TestNumericPrecision:
    def test_round_to_2_decimal_places(self, script, capsys):
        """diff 应四舍五入到 2 位小数."""
        script.check_coverage_drop("80.0", "78.123")
        out = capsys.readouterr().out
        # 78.123 - 80.0 = -1.877, round 后 -1.88
        assert "-1.88%" in out

    def test_negative_diff_uses_plus_minus_sign(self, script, capsys):
        """diff 输出应带正负号 (+/-)."""
        script.check_coverage_drop("80.0", "75.0")
        out = capsys.readouterr().out
        assert "-5.00%" in out

    def test_positive_diff_uses_plus_sign(self, script, capsys):
        """正 diff 应带 + 号."""
        script.check_coverage_drop("80.0", "85.0")
        out = capsys.readouterr().out
        assert "+5.00%" in out


# ============================================================
# 8. 脚本可执行性 (作为 CLI 直接运行)
# ============================================================
class TestScriptExecutable:
    def test_script_exists(self):
        assert SCRIPT_PATH.exists(), f"脚本不存在: {SCRIPT_PATH}"

    def test_script_has_main_guard(self):
        """脚本应包含 if __name__ == '__main__': 守护, 支持直接运行."""
        content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "if __name__ ==" in content
        assert "__main__" in content

    def test_script_has_shebang(self):
        """脚本应有 shebang, 支持 chmod +x 后直接执行."""
        first_line = SCRIPT_PATH.read_text(encoding="utf-8").splitlines()[0]
        assert first_line.startswith("#!"), f"缺少 shebang: {first_line}"
        assert "python" in first_line.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
