"""config.py Phase 2（provider 目录块抽出）结构契约测试。

背景：config.py 的 provider 目录机制（provider_metadata.json 缓存加载、
ProviderCatalog、默认模型/base_url 派生、内置 provider 集合、provider 配置
映射）抽为 config_providers.py，逐字节搬移。

契约：
    1. config_providers 独立可导入（仅依赖 config_paths + llm_gateway，
       不 import config，无循环导入）
    2. config 同名 re-export：from config import MIMO_MODEL /
       get_builtin_providers / get_default_model_for_provider 等
       既有用法不受影响（同对象）
    3. 可变状态（DEFAULT_PROVIDER/set_default_provider）留在 config.py：
       set_default_provider 后 config.DEFAULT_PROVIDER 更新可见
    4. 语义不变：default_provider / builtin 派生自 provider_metadata.json
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# ── 1/2. 独立导入 + re-export 同对象 ─────────────────────────────

def test_config_providers_imports_standalone():
    import importlib
    mod = importlib.import_module("config_providers")
    for name in ("_load_provider_metadata_cached", "get_provider_catalog",
                 "get_default_model_for_provider", "get_base_url_for_provider",
                 "get_default_provider", "get_builtin_providers",
                 "get_provider_config",
                 "MIMO_MODEL", "MIMO_BASE_URL", "DEEPSEEK_BASE_URL"):
        assert hasattr(mod, name), f"缺少符号 {name}"


@pytest.mark.parametrize("name", [
    "get_provider_catalog", "get_default_model_for_provider",
    "get_base_url_for_provider", "get_default_provider",
    "get_builtin_providers", "get_provider_config",
    "MIMO_MODEL", "MIMO_BASE_URL", "DEEPSEEK_BASE_URL",
])
def test_config_reexports_same_objects(name):
    import config
    import config_providers
    assert hasattr(config, name), f"config 缺少兼容别名 {name}"
    assert getattr(config, name) is getattr(config_providers, name), name


def test_config_providers_does_not_import_config():
    import config_providers as mod
    assert "config" not in getattr(mod, "__dict__", {})
    assert "config_providers" not in getattr(mod, "__dict__", {})


# ── 3. 可变状态留在 config ──────────────────────────────────────

def test_set_default_provider_updates_config_namespace():
    import config
    original = config.DEFAULT_PROVIDER
    try:
        config.set_default_provider("agnes")
        assert config.DEFAULT_PROVIDER == "agnes"
    finally:
        config.set_default_provider(original)
    assert config.DEFAULT_PROVIDER == original


# ── 4. 语义不变 ─────────────────────────────────────────────────

def test_default_model_and_base_url_priority(monkeypatch):
    """env 优先：设 MIMO_MODEL_NAME / MIMO_BASE_URL 后立即生效"""
    import config_providers
    monkeypatch.setenv("TESTPROV_MODEL_NAME", "test-model-x")
    monkeypatch.setenv("TESTPROV_BASE_URL", "https://test.example.com/v1")
    assert config_providers.get_default_model_for_provider("testprov") == "test-model-x"
    assert config_providers.get_base_url_for_provider("testprov") == "https://test.example.com/v1"


def test_default_provider_from_metadata():
    """default_provider 派生自 provider_metadata.json（无硬编码 "mimo"）"""
    import config_providers
    meta = config_providers._load_provider_metadata_cached()
    if os.getenv("DEFAULT_PROVIDER", "").strip():
        assert config_providers.get_default_provider() == \
            os.getenv("DEFAULT_PROVIDER", "").strip().lower()
    else:
        expected = str((meta or {}).get("default_provider", "") or "").strip().lower()
        assert config_providers.get_default_provider() == expected


def test_builtin_providers_derived_from_metadata():
    import config_providers
    builtin = config_providers.get_builtin_providers()
    assert isinstance(builtin, frozenset)
    catalog = config_providers.get_provider_catalog()
    expected = {p.id for p in catalog.list() if p.builtin}
    assert builtin == frozenset(expected)
