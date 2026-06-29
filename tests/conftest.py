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
