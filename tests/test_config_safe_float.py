"""config.py / prompt_builder.py 配置无类型保护的回归测试。

背景：
- config.py 的 QUERY_CACHE_THRESHOLD 此前用裸 float(os.getenv(...)) 转换，
  环境变量被设为非法值（如 "abc"）时会在 import 阶段抛 ValueError 崩溃。
- prompt_builder.py 的 _BASE_STICKINESS_THRESHOLD 存在同类裸 float() 转换。

本测试验证：
1. _safe_float 辅助函数对非法输入回退默认值、对合法输入正常解析；
2. 在环境变量为非法值时，import config / import prompt_builder 不崩溃，
   且取默认值。
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_python(code: str, env_var: str, value: str) -> subprocess.CompletedProcess:
    """在干净子进程中执行一段代码，并注入非法环境变量值。"""
    env = os.environ.copy()
    env[env_var] = value
    # 与 tests/conftest.py 保持一致，避免安全模块在测试子进程中误拦截
    env.setdefault("AGENT_DEV_MODE", "1")
    env.setdefault("TEST_MODE", "true")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_safe_float_returns_default_on_invalid_input():
    from config import _safe_float

    assert _safe_float("abc", 0.88) == 0.88
    assert _safe_float(None, 0.88) == 0.88


def test_safe_float_parses_valid_input():
    from config import _safe_float

    assert _safe_float("0.5", 0.88) == 0.5


def test_config_import_not_crash_on_invalid_query_cache_threshold():
    result = _run_python(
        "import config; print(config.QUERY_CACHE_THRESHOLD)",
        "QUERY_CACHE_THRESHOLD",
        "abc",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.88"


def test_prompt_builder_import_not_crash_on_invalid_scene_stickiness():
    result = _run_python(
        "import prompt_builder; print(prompt_builder._BASE_STICKINESS_THRESHOLD)",
        "SCENE_STICKINESS_THRESHOLD",
        "abc",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.5"
