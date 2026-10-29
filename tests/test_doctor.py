"""core/doctor.py 回归测试。

doctor 是用户"装完用不了"时的第一道自检，长期零测试覆盖。
本文件锁住三类曾真实踩坑的行为：
  1. L0-Dependencies 能区分"未安装"与"装了但导入崩溃"（Python 3.12+ 的
     pkgutil.ImpImporter 问题会让 jieba 的 find_spec 正常但 import 抛错）；
  2. 云盘 / FUSE 挂载返回占位容量时不得报出天文数字；
  3. 配置检查必须认多 provider 凭证，不能只看 MIMO_API_KEY。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import doctor  # noqa: E402

# ============================================================
# L0-Dependencies：依赖完整性
# ============================================================

def test_required_deps_all_present() -> None:
    """本机依赖齐全时应通过（本仓库 requirements 已装）。"""
    passed, msg = doctor._check_required_deps()
    assert passed is True
    assert "核心依赖就绪" in msg


def test_required_deps_detects_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """模块未安装 → FAIL，且消息点名该模块。"""
    real_find = importlib.util.find_spec

    def fake_find(name, *args, **kwargs):
        if name == "jieba":
            return None
        return real_find(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find)
    passed, msg = doctor._check_required_deps()

    assert passed is False
    assert "jieba" in msg
    assert "未安装" in msg
    assert "pip install -r requirements.txt" in msg


def test_required_deps_detects_broken_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """关键回归：装上了但 import 崩溃必须被判失败，不能只看 find_spec。

    这正是 Python 3.12+ 下 jieba 的真实症状——文件在、find_spec 通过、
    import 抛 AttributeError: module 'pkgutil' has no attribute 'ImpImporter'。
    """
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())

    def broken_import(name, *args, **kwargs):
        raise AttributeError("module 'pkgutil' has no attribute 'ImpImporter'")

    # import_module 位于 importlib 顶层，不是 importlib.util
    monkeypatch.setattr(importlib, "import_module", broken_import)
    passed, msg = doctor._check_required_deps()

    assert passed is False
    assert "jieba" in msg
    assert "导入失败" in msg
    assert "pkg_resources" in msg  # 提示必须指向真实根因


def test_required_deps_find_spec_itself_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """find_spec 自身抛错（sys.path 异常/父包损坏）不应让 doctor 崩。"""

    def boom(name, *args, **kwargs):
        raise ValueError("sys.path contains invalid entry")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    passed, msg = doctor._check_required_deps()

    assert passed is False
    assert "探测失败" in msg


def test_optional_deps_missing_does_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """可选依赖缺失只降级对应功能，不能判 FAIL 吓到用户。"""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    passed, msg = doctor._check_optional_deps()

    assert passed is True
    assert "降级" in msg


# ============================================================
# 磁盘空间：占位值兜底
# ============================================================

class _FakeStatVFS:
    def __init__(self, bavail: int, frsize: int) -> None:
        self.f_bavail = bavail
        self.f_frsize = frsize


def test_disk_space_placeholder_values_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """云盘挂载返回 2^54-1 个块 × 512B 时不得算出 8.6e9 GB。"""
    import os

    monkeypatch.setattr(
        os, "statvfs", lambda _p: _FakeStatVFS(bavail=(2 ** 54) - 1, frsize=512)
    )
    passed, msg = doctor._check_disk_space()

    assert passed is True
    assert "placeholder" in msg
    assert "8589934592" not in msg


def test_disk_space_normal_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常挂载（约 100 GB 空闲）应报 OK 并给出实际数值。"""
    import os

    monkeypatch.setattr(
        os, "statvfs", lambda _p: _FakeStatVFS(bavail=100 * 1024 * 1024, frsize=1024)
    )
    passed, msg = doctor._check_disk_space()

    assert passed is True
    assert "100.0 GB free" in msg


def test_disk_space_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """空闲不足 0.5 GB → FAIL。"""
    import os

    monkeypatch.setattr(
        os, "statvfs", lambda _p: _FakeStatVFS(bavail=100 * 1024, frsize=1024)
    )
    passed, msg = doctor._check_disk_space()

    assert passed is False
    assert "Low disk space" in msg


# ============================================================
# 配置：多 provider 凭证识别
# ============================================================

def test_config_accepts_non_mimo_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """只配 AGNES_API_KEY（无 MIMO_API_KEY）也必须判通过。

    回归：原实现只看 MIMO_API_KEY，用 Agnes/DeepSeek 的用户会被误报
    "Config Loaded FAIL"，直接被引导去查错方向。
    """
    import config

    monkeypatch.setattr(config, "MIMO_API_KEY", "", raising=False)
    monkeypatch.setenv("AGNES_API_KEY", "sk-test-agnes")
    monkeypatch.setattr(
        "setup_wizard._load_env_values", lambda: {"AGNES_API_KEY": "sk-test-agnes"}
    )

    passed, msg = doctor._check_config()

    assert passed is True
    assert "AGNES_API_KEY" in msg


def test_config_reports_actionable_hint_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个凭证都没有时，提示必须给出具体该填哪个变量。"""
    import config

    monkeypatch.setattr(config, "MIMO_API_KEY", "", raising=False)
    monkeypatch.setattr("setup_wizard._load_env_values", lambda: {})
    for key in (
        "MIMO_API_KEY", "AGNES_API_KEY", "OPENROUTER_API_KEY",
        "SILICONFLOW_API_KEY", "DEEPSEEK_API_KEY", "ZHIPU_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    passed, msg = doctor._check_config()

    assert passed is False
    assert "MIMO_API_KEY" in msg
    assert "AGNES_API_KEY" in msg
