"""依赖锁定 + 结构化日志 + CI 门槛 (Ar1-Ar3)

Ar1: pip-compile 风格的依赖锁定脚本
Ar2: 统一结构化日志格式
Ar3: CI 测试覆盖率门槛

注意: 本文件作为脚本, 不被其他模块导入
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


# ============================================================
# Ar1: 依赖锁定 (生成 requirements.lock.txt)
# ============================================================

def generate_requirements_lock() -> bool:
    """生成 requirements.lock.txt (固定所有依赖的精确版本)"""
    print("[Ar1] Generating requirements.lock.txt ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True, text=True, timeout=60
, check=False)
    if result.returncode != 0:
        print(f"  ✗ pip freeze failed: {result.stderr}")
        return False

    lock_path = ROOT / "requirements.lock.txt"
    # 按 package name 排序
    lines = sorted(line.strip() for line in result.stdout.splitlines()
                    if line.strip() and "==" in line)
    lock_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  ✓ Locked {len(lines)} packages → {lock_path}")
    return True


# ============================================================
# Ar2: 结构化日志格式
# ============================================================

STRUCTURED_LOG_FORMAT = """\
# Structured Logging Format (Ar2)
#
# 统一所有模块的日志格式:
#   {timestamp} | {level} | {module}:{function}:{line} | {event} | {kwargs}
#
# 使用方法:
#   from loguru import logger
#   logger.bind(event="tool_call", tool="search", user="u1").info("called")
#
# 输出:
#   2026-06-29 10:00:00 | INFO     | tool_engine.tool_executor:execute:120 |
#   tool_call | tool='search' user='u1'
#
# 关键事件 (event) 列表:
#   - tool_call_start / tool_call_end
#   - llm_request_start / llm_request_end
#   - cache_hit / cache_miss
#   - security_check_pass / security_check_fail
#   - recovery_trigger / recovery_success
#   - metacog_drift_detected / metacog_action
#   - dream_consolidate / dream_decayed
"""


def write_structured_log_config() -> None:
    """输出结构化日志配置说明到 STRUCTURED_LOGGING.md"""
    out = ROOT / "STRUCTURED_LOGGING.md"
    out.write_text(STRUCTURED_LOG_FORMAT, encoding="utf-8")
    print(f"[Ar2] Wrote {out}")


# ============================================================
# Ar3: CI 测试覆盖率门槛
# ============================================================

COVERAGE_CONFIG = """\
[run]
source = .
omit =
    tests/*
    */__pycache__/*
    setup_wizard.py
    web/splash/*
    build/*
    dist/*

[report]
# Ar3: CI 门槛 — 不低于 60%
fail_under = 60
show_missing = True
exclude_lines =
    pragma: no cover
    def __repr__
    raise NotImplementedError
    if __name__ == .__main__.:
    if TYPE_CHECKING:
    @abstractmethod
"""


def write_coverage_config() -> None:
    """写入 .coveragerc"""
    out = ROOT / ".coveragerc"
    out.write_text(COVERAGE_CONFIG, encoding="utf-8")
    print(f"[Ar3] Wrote {out} (fail_under=60)")


def update_pytest_ini() -> None:
    """更新 pytest.ini 添加 --cov"""
    p = ROOT / "pytest.ini"
    if not p.exists():
        return
    content = p.read_text(encoding="utf-8")
    if "--cov" in content:
        return
    # 在 addopts 中添加 --cov 选项 (注释形式, 不强制)
    if "addopts" in content:
        content = content.replace(
            "addopts =",
            "addopts =\n    # --cov=. --cov-report=term-missing --cov-fail-under=60  # Ar3 (取消注释启用)"
        )
        p.write_text(content, encoding="utf-8")
        print(f"[Ar3] Updated {p}")


if __name__ == "__main__":
    ok = True
    ok &= generate_requirements_lock()
    write_structured_log_config()
    write_coverage_config()
    update_pytest_ini()
    print("\nDone.")
    sys.exit(0 if ok else 1)
