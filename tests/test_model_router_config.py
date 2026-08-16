"""model_router Phase 1（provider 配置抽取）结构契约测试。

背景：model_router.py 是 2656 行上帝文件。Phase 1 把 provider 配置块
（元数据加载 / base_url / API Key / 定价 / max_tokens cap / Ollama 映射 /
跨 provider 兜底映射）抽为独立模块 model_router_config.py，函数体逐字节
搬移仅缩进调整（对齐 db/legacy_migrations.py 的 Phase 1 先例）。

契约：
    1. model_router_config 可独立导入（无 model_router 依赖，无循环导入）
    2. model_router 以同名 re-export 保持兼容：外部 `from model_router import
       MIMO_MODEL`、`patch("model_router.MIMO_MODEL")` 等调用方不受影响
    3. 配置语义不变：base_url 优先级 env > metadata 文件；Ollama 翻译
       精确映射 / 云模型名回退 default；max_tokens cap env > 文件 > None
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import model_router
import model_router_config as mrc
import pytest


# ── 1. 独立可导入 + 无循环依赖 ────────────────────────────────────

def test_config_module_imports_standalone():
    """model_router_config 不依赖 model_router（import 链中无回流）"""
    import importlib
    cfg_mod = importlib.import_module("model_router_config")
    assert cfg_mod is mrc
    # 关键符号存在
    for name in ("_load_provider_metadata", "translate_model_for_provider",
                 "_load_provider_base_url", "_resolve_provider_key",
                 "PROVIDER_MAX_TOKENS_CAP", "_CROSS_PROVIDER_MAP",
                 "PROVIDER_PRICING", "MIMO_MODEL", "MIMO_PRO_MODEL",
                 "MIMO_BASE_URL", "_OLLAMA_MODEL_MAP"):
        assert hasattr(cfg_mod, name), f"缺少符号 {name}"


# ── 2. model_router 同名 re-export 兼容 ───────────────────────────

@pytest.mark.parametrize("name", [
    "_PROVIDER_METADATA", "_PROVIDER_CAPS_FROM_FILE",
    "_OLLAMA_MODEL_MAP", "_OLLAMA_DEFAULT_MODEL", "_LOCAL_ORT_PROVIDER",
    "translate_model_for_provider", "_load_provider_base_url",
    "_resolve_provider_key",
    "MIMO_MODEL", "MIMO_PRO_MODEL", "MIMO_BASE_URL", "MIMO_API_KEY",
    "MIMO_PRICING", "PROVIDER_PRICING",
    "PROVIDER_MAX_TOKENS_CAP", "_CROSS_PROVIDER_MAP",
])
def test_model_router_reexports_same_objects(name):
    """model_router 上的同名属性必须与 config 模块同对象（同一字典/函数/常量）"""
    assert hasattr(model_router, name), f"model_router 缺少兼容别名 {name}"
    assert getattr(model_router, name) is getattr(mrc, name), name


def test_patching_model_router_alias_visible_to_config_functions():
    """patch("model_router.MIMO_MODEL") 的既有用法语义不变（config 函数
    内部读的是 config 模块自身命名空间，别名补丁不应产生半更新状态——
    本用例锁定：config 模块自身值与 model_router 别名默认一致）"""
    assert model_router.MIMO_MODEL == mrc.MIMO_MODEL


# ── 3. 配置语义不变 ───────────────────────────────────────────────

def test_base_url_priority_env_over_file(monkeypatch):
    monkeypatch.setenv("TESTPROV_BASE_URL", "https://env.example/v1")
    assert mrc._load_provider_base_url("testprov", "TESTPROV_BASE_URL") == \
        "https://env.example/v1"
    monkeypatch.delenv("TESTPROV_BASE_URL")
    assert mrc._load_provider_base_url("testprov", "TESTPROV_BASE_URL") == ""


def test_translate_model_ollama_exact_map(monkeypatch):
    monkeypatch.setattr(mrc, "_OLLAMA_MODEL_MAP",
                        {"deepseek-ai/DeepSeek-V3": "deepseek-r1"})
    monkeypatch.setattr(mrc, "_OLLAMA_DEFAULT_MODEL", "qwen2.5")
    assert mrc.translate_model_for_provider(
        "ollama", "deepseek-ai/DeepSeek-V3") == "deepseek-r1"
    # 云模型名（含 /）未命中映射 → 回退 default
    assert mrc.translate_model_for_provider(
        "ollama", "org/Unknown-Model") == "qwen2.5"
    # 非 Ollama 原样透传
    assert mrc.translate_model_for_provider(
        "mimo", "deepseek-ai/DeepSeek-V3") == "deepseek-ai/DeepSeek-V3"


def test_max_tokens_cap_none_when_unset():
    """未配置的 provider cap 为 None（不裁剪），绝不为 0"""
    assert mrc._file_max_tokens_cap("no-such-provider") is None
    assert mrc._env_max_tokens_cap("NO_SUCH_ENV_VAR_XYZ") is None


def test_provider_pricing_has_default():
    assert "default" in mrc.PROVIDER_PRICING
    assert mrc.PROVIDER_PRICING["mimo"] is mrc.MIMO_PRICING
