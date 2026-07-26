"""TDD 测试：降级模式判定改为"任一 Provider 凭证"。

根因：原 lifespan 仅凭 MIMO_API_KEY 是否存在决定降级，但用户可能保存了
Agnes / OpenRouter / SiliconFlow 或自定义 provider 凭证。这些场景下若
MIMO_API_KEY 缺失，会一并跳过 _start_services，导致 _apply_model_overrides
不执行 —— 已保存的 provider 凭证全部失效。

修复后降级判定改用 _has_any_provider_credential()：MiMo / Agnes / 自定义
provider 任一存在即视为可用，避免误判降级。
"""
from __future__ import annotations

from unittest.mock import patch


def test_has_any_provider_credential_true_when_agnes_key_present():
    """有 Agnes 凭证（即使 MiMo 缺失）应返回 True，不进入降级模式。

    回归测试：旧实现仅检查 MIMO_API_KEY，用户只配 Agnes 时也会被判为降级，
    导致 _apply_model_overrides 不执行，agnes provider 不注册、路由不恢复。
    """
    from web.server import _has_any_provider_credential

    # MiMo key 缺失（模拟用户没配 MiMo，只配 Agnes）
    with patch("web.server._resolve_env_api_key", return_value=""):
        # _load_env_values 返回包含 AGNES_API_KEY 的字典
        with patch("setup_wizard._load_env_values",
                   return_value={"AGNES_API_KEY": "agnes-fake-key-12345"}):
            # 自定义 provider 列表为空，确保只验证 Agnes 分支
            with patch("web.config_service.get_config_service") as mock_cfg:
                mock_cfg.return_value.get.return_value = {}
                assert _has_any_provider_credential() is True


def test_has_any_provider_credential_true_when_mimo_key_present():
    """有 MiMo 凭证应返回 True（保留旧判定语义）。"""
    from web.server import _has_any_provider_credential

    with patch("web.server._resolve_env_api_key", return_value="mimo-fake-key"):
        # 即使 Agnes 和自定义 provider 都没有，MiMo 存在也应返回 True
        with patch("setup_wizard._load_env_values", return_value={}):
            with patch("web.config_service.get_config_service") as mock_cfg:
                mock_cfg.return_value.get.return_value = {}
                assert _has_any_provider_credential() is True


def test_has_any_provider_credential_true_when_custom_provider_has_key():
    """有自定义 provider 凭证（即使 MiMo/Agnes 都缺失）应返回 True。

    回归测试：用户通过 WebUI 保存了 OpenRouter/SiliconFlow/自定义 provider，
    但 .env 里没写 MIMO_API_KEY 与 AGNES_API_KEY。旧实现会判降级，导致
    保存的 provider 不注册、用户聊天模型被回落到 mimo。
    """
    from web.server import _has_any_provider_credential

    custom_providers = {"openrouter": {"label": "OpenRouter", "enabled": True}}

    with patch("web.server._resolve_env_api_key", return_value=""):
        with patch("setup_wizard._load_env_values", return_value={}):
            with patch("web.config_service.get_config_service") as mock_cfg:
                mock_cfg.return_value.get.return_value = custom_providers
                # load_provider_key 对 openrouter 返回非空 key
                with patch("web._provider_keys.load_provider_key",
                           return_value="or-fake-key"):
                    assert _has_any_provider_credential() is True


def test_has_any_provider_credential_false_when_all_empty():
    """全部凭证清空时应返回 False，进入降级模式。"""
    from web.server import _has_any_provider_credential

    with patch("web.server._resolve_env_api_key", return_value=""):
        with patch("setup_wizard._load_env_values", return_value={}):
            with patch("web.config_service.get_config_service") as mock_cfg:
                mock_cfg.return_value.get.return_value = {}
                with patch("web._provider_keys.load_provider_key",
                           return_value=""):
                    assert _has_any_provider_credential() is False


def test_has_any_provider_credential_skips_ollama():
    """ollama 不需要 API key，不应仅凭 ollama 配置存在就判为非降级。

    根因：ollama 注册时只看 OLLAMA_BASE_URL，没有 key 文件。若把 ollama
    视为"有凭证"，用户残留 ollama 配置时会误判非降级，但实际没有可用 provider。
    """
    from web.server import _has_any_provider_credential

    # 只有 ollama 一个 provider
    custom_providers = {"ollama": {"label": "Ollama", "enabled": True}}

    with patch("web.server._resolve_env_api_key", return_value=""):
        with patch("setup_wizard._load_env_values", return_value={}):
            with patch("web.config_service.get_config_service") as mock_cfg:
                mock_cfg.return_value.get.return_value = custom_providers
                # load_provider_key 对 ollama 返回空（ollama 没有 key 文件）
                with patch("web._provider_keys.load_provider_key",
                           return_value=""):
                    assert _has_any_provider_credential() is False
