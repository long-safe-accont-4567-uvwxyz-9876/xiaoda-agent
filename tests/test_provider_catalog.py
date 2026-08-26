from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest


@pytest.fixture
def metadata_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "provider_metadata.json"


@pytest.fixture
def catalog(metadata_path: Path):
    from llm_gateway.provider_catalog import ProviderCatalog

    return ProviderCatalog.from_path(metadata_path)


def test_provider_catalog_is_authoritative_for_known_provider_aliases(catalog):
    assert catalog.get("modelscope").auth.environment_aliases == (
        "MODELSCOPE_ACCESS_TOKEN",
        "MODELSCOPE_API_KEY",
    )


def test_all_builtin_providers_are_protected(catalog):
    assert {provider.id for provider in catalog.list() if provider.builtin} >= {
        "mimo",
        "agnes",
    }


def test_metadata_provider_ids_are_unique(metadata_path: Path):
    from llm_gateway.provider_catalog import ProviderCatalog

    ids = [provider.id for provider in ProviderCatalog.from_path(metadata_path).list()]

    assert len(ids) == len(set(ids))


def test_get_normalizes_provider_id_and_rejects_unknown_provider(catalog):
    assert catalog.get("  MiMo ").id == "mimo"
    with pytest.raises(KeyError, match="unknown provider"):
        catalog.get("missing")


def test_register_accepts_valid_custom_provider_and_rejects_duplicates(catalog):
    custom = replace(catalog.get("openrouter"), id="private-gateway", builtin=False)

    catalog.register(custom)

    assert catalog.get("private-gateway") == custom
    with pytest.raises(ValueError, match="already registered"):
        catalog.register(custom)


def test_builtin_provider_definition_cannot_be_replaced(catalog):
    replacement = replace(catalog.get("mimo"), default_model="replacement")

    with pytest.raises(ValueError, match="builtin provider"):
        catalog.register(replacement, replace_existing=True)


def test_validate_rejects_invalid_definition(catalog):
    invalid = replace(catalog.get("openrouter"), id="Invalid Provider")

    with pytest.raises(ValueError, match="provider id"):
        catalog.validate(invalid)


def test_config_compatibility_functions_delegate_to_catalog(monkeypatch, catalog):
    import config

    monkeypatch.setattr(config, "get_provider_catalog", lambda: catalog)
    monkeypatch.delenv("MODELSCOPE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("MODELSCOPE_API_KEY", "secondary-token")

    assert config.get_default_model_for_provider("deepseek") == "deepseek-chat"
    assert config.get_builtin_providers() >= {"mimo", "agnes"}
    assert config.get_provider_config("modelscope") == {
        "base_url": "https://api-inference.modelscope.cn/v1/",
        "api_key_env": "MODELSCOPE_API_KEY",
    }


def test_catalog_rejects_duplicate_provider_keys(tmp_path: Path):
    from llm_gateway.provider_catalog import ProviderCatalog

    path = tmp_path / "providers.json"
    path.write_text(
        '{"schema_version": 1, "providers": {'
        '"mimo": {"protocol": "openai_compatible"},'
        '"mimo": {"protocol": "ollama"}}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate metadata key: mimo"):
        ProviderCatalog.from_path(path)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"providers": {}}, "schema_version is required"),
        ({"schema_version": 2, "providers": {}}, "unsupported schema_version: 2"),
    ],
)
def test_catalog_rejects_missing_or_unsupported_schema_version(
    tmp_path: Path,
    metadata: dict,
    message: str,
):
    from llm_gateway.provider_catalog import ProviderCatalog

    path = tmp_path / "providers.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        ProviderCatalog.from_path(path)


def test_catalog_falls_back_to_bundled_metadata_when_user_metadata_is_invalid(
    tmp_path: Path,
    metadata_path: Path,
):
    from llm_gateway.provider_catalog import ProviderCatalog

    user_path = tmp_path / "provider_metadata.json"
    user_path.write_text("{broken", encoding="utf-8")

    catalog = ProviderCatalog.from_paths(user_path, metadata_path)

    assert catalog.get("mimo").default_model == "mimo-v2.5"
    assert catalog.source_path == metadata_path
    assert catalog.load_errors[0][0] == user_path


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"MODELSCOPE_ACCESS_TOKEN": "primary"}, ("MODELSCOPE_ACCESS_TOKEN", "primary")),
        ({"MODELSCOPE_API_KEY": "fallback"}, ("MODELSCOPE_API_KEY", "fallback")),
    ],
)
def test_catalog_resolves_each_modelscope_alias(catalog, environment, expected):
    assert catalog.resolve_environment_alias("modelscope", environment) == expected


@pytest.mark.parametrize("alias", ["MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_API_KEY"])
def test_startup_registers_modelscope_from_each_catalog_alias(
    monkeypatch,
    catalog,
    alias: str,
):
    from web import server

    class Config:
        def __init__(self):
            self.providers = {}

        def get(self, path, default=None):
            return self.providers if path == "models.providers" else default

        def set(self, path, value):
            self.providers[path.rsplit(".", 1)[-1]] = value

    cfg = Config()
    written = []
    monkeypatch.setattr("config.get_provider_catalog", lambda: catalog)
    monkeypatch.setattr(server, "_ensure_provider_key_file", lambda pid, key, _: written.append((pid, key)))

    server._register_env_providers(cfg, {alias: "modelscope-token"}, object())

    assert "modelscope" in cfg.providers
    assert written == [("modelscope", "modelscope-token")]


@pytest.mark.parametrize("alias", ["MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_API_KEY"])
def test_credential_pool_loads_modelscope_from_each_catalog_alias(monkeypatch, alias: str):
    from utils.credential_pool import CredentialPool

    for key in (
        "MIMO_API_KEY",
        "AGNES_API_KEY",
        "SILICONFLOW_API_KEY",
        "OPENROUTER_API_KEY",
        "MODELSCOPE_ACCESS_TOKEN",
        "MODELSCOPE_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(alias, "modelscope-token")

    pool = CredentialPool()

    assert pool._pool["modelscope"][0].api_key == "modelscope-token"


@pytest.mark.parametrize(
    "alias,provider",
    [
        ("SILICONFLOW_API_KEY", "siliconflow"),
        ("OPENROUTER_API_KEY", "openrouter"),
    ],
)
def test_credential_pool_free_providers_delegate_to_catalog(monkeypatch, alias, provider):
    from utils.credential_pool import CredentialPool

    for key in (
        "MIMO_API_KEY",
        "AGNES_API_KEY",
        "SILICONFLOW_API_KEY",
        "OPENROUTER_API_KEY",
        "MODELSCOPE_ACCESS_TOKEN",
        "MODELSCOPE_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(alias, "delegate-token")

    import config as config_module

    class _Catalog:
        def __init__(self, provider_id):
            self._provider_id = provider_id

        def resolve_environment_alias(self, provider_id, environment):
            if provider_id == self._provider_id:
                return alias, "delegate-token"
            return None

        def get(self, provider_id):
            if provider_id != self._provider_id:
                raise KeyError(provider_id)
            return type("D", (), {"endpoint": type("E", (), {"base_url": "https://catalog-authority.test/v1"})})()

    monkeypatch.setattr(config_module, "get_provider_catalog", lambda: _Catalog(provider))

    pool = CredentialPool()

    assert provider in pool._pool
    assert pool._pool[provider][0].api_key == "delegate-token"
    assert pool._pool[provider][0].base_url == "https://catalog-authority.test/v1"


@pytest.mark.parametrize("alias", ["MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_API_KEY"])
@pytest.mark.asyncio
async def test_setup_validates_each_modelscope_alias(monkeypatch, alias: str):
    from web.routers import setup, setup_key_probes

    async def validate(value):
        return True, value

    # 打桩必须落在名字被查找的模块：test_single_key 已随拆分蓝图 P1 迁入
    # setup_key_probes（setup 侧仅 re-export），patch 旧位置不再拦截
    monkeypatch.setattr(setup_key_probes, "_test_modelscope", validate)
    assert setup.test_single_key is not None

    assert await setup.test_single_key(alias, "modelscope-token") == (
        True,
        "modelscope-token",
    )


@pytest.mark.parametrize("alias", ["MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_API_KEY"])
def test_setup_resets_modelscope_from_each_catalog_alias(monkeypatch, alias: str):
    from utils import credential_pool
    from web.routers import setup

    class Pool:
        def __init__(self):
            self.replacements = []

        def reset_provider(self, provider):
            pass

        def replace_provider(self, provider, credential):
            self.replacements.append((provider, credential.api_key))

    pool = Pool()
    monkeypatch.setattr(credential_pool, "get_credential_pool", lambda: pool)

    setup._reset_credential_pool({alias: "modelscope-token"})

    assert pool.replacements == [("modelscope", "modelscope-token")]
