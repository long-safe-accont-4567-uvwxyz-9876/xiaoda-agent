"""共享测试配置和 fixtures"""
import os
import sys
from pathlib import Path

import pytest

# 统一设置项目路径
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试环境默认启用开发板模式（安全威胁 warn 不 block）
os.environ.setdefault("AGENT_DEV_MODE", "1")

# 测试模式下跳过文件 sink，防止测试日志污染生产日志
os.environ.setdefault("TEST_MODE", "true")


@pytest.fixture(autouse=True)
def _isolate_permission_persistence(tmp_path, monkeypatch):
    """全局隔离权限模式落盘：任何测试 set_mode() 都写入临时文件。

    set_mode() 会把模式持久化到 get_config_dir()/permission_mode.json（真实
    用户配置）。部分测试用独立 PermissionManager 实例（test_permission_mode_
    five_states）或全局单例（test_e2e_functional）切换模式且未重定向落盘，
    会导致完整套件跑完后覆盖用户配置（如 custom 被改成 default）。
    这里统一重定向 _PERMISSION_FILE，测试结束后自动恢复。
    """
    import security.permission_manager as _pm
    monkeypatch.setattr(_pm, "_PERMISSION_FILE", str(tmp_path / "permission_mode.json"))


@pytest.fixture
def project_root() -> Path:
    """返回项目根目录路径"""
    return PROJECT_ROOT


@pytest.fixture
def test_data_dir(project_root) -> Path:
    """返回测试数据目录路径 (tests/data)"""
    return project_root / "tests" / "data"


@pytest.fixture
def test_fixtures_dir(project_root) -> Path:
    """返回测试 fixtures 目录路径 (tests/fixtures)"""
    return project_root / "tests" / "fixtures"


@pytest.fixture
def temp_db_path(tmp_path) -> Path:
    """返回临时数据库文件路径 (基于 pytest 的 tmp_path)"""
    return tmp_path / "test.db"


@pytest.fixture
def temp_config_path(tmp_path) -> Path:
    """返回临时配置文件路径"""
    return tmp_path / "test_config.yaml"


@pytest.fixture(autouse=True)
def _restore_module_global_state():
    """自动还原模块级全局状态，防止测试间污染。

    CodeRabbit#12 + Qodo#12 根治修复：ModelRouter.set_chat_model 末尾调用
    _set_default_provider(provider) 改写进程级 config.DEFAULT_PROVIDER。
    若测试 finally 只还原 ROUTE_TABLE 不还原 DEFAULT_PROVIDER，后续测试读到
    被污染的 provider（如 agnes），导致 _restore_chat_model fallback 断言
    "应回退到 mimo" 失败（实际回退到被污染的 agnes）。

    本 fixture 在每个测试结束后检测并还原 DEFAULT_PROVIDER，覆盖所有
    4 个涉及 set_chat_model 的测试文件（test_model_switching_refactor /
    test_model_persistence_bugfix / test_fallback_optimization /
    test_agnes_max_tokens_and_sticky_fallback），无需逐个测试手写还原。
    """
    import config as _cfg_mod
    _orig_default_provider = _cfg_mod.DEFAULT_PROVIDER
    yield
    if _cfg_mod.DEFAULT_PROVIDER != _orig_default_provider:
        _cfg_mod.set_default_provider(_orig_default_provider)
