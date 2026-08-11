from __future__ import annotations

import builtins
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

from core.cancel_token import CancelToken
from local_ai.contracts import RuntimeKind, RuntimeProfile


class _FakeModel:
    def __init__(self, config: _FakeConfig) -> None:
        self.config = config
        self.model_dir = config.model_dir
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeTokenizerStream:
    def __init__(self) -> None:
        self.decoded: list[int] = []

    def decode(self, token: int) -> str:
        self.decoded.append(token)
        return {1: "你", 2: "好"}[token]


class _FakeTokenizer:
    def __init__(self, model: _FakeModel) -> None:
        self.model = model
        self.encoded: list[str] = []
        self.stream = _FakeTokenizerStream()
        self.chat_template_calls: list[tuple[object, bool]] = []
        self.closed = False

    def apply_chat_template(
        self, messages: str, *, add_generation_prompt: bool
    ) -> str:
        parsed = json.loads(messages)
        if not isinstance(parsed, list):
            raise TypeError("messages must encode a JSON array")
        self.chat_template_calls.append((messages, add_generation_prompt))
        return "<official>chat</official>"

    def encode(self, prompt: str) -> list[int]:
        self.encoded.append(prompt)
        return [101, 102]

    def create_stream(self) -> _FakeTokenizerStream:
        return self.stream

    def close(self) -> None:
        self.closed = True


class _FakeGeneratorParams:
    def __init__(self, model: _FakeModel) -> None:
        self.model = model
        self.search_options: dict[str, object] = {}

    def set_search_options(self, **options: object) -> None:
        self.search_options = options


class _FakeGenerator:
    def __init__(self, module: _FakeOrtGenAi, model: _FakeModel, params: _FakeGeneratorParams) -> None:
        self.module = module
        self.model = model
        self.params = params
        self.index = 0
        self.appended_tokens: list[int] = []
        self.closed = False
        module.generators.append(self)

    def is_done(self) -> bool:
        return self.index >= 2

    def append_tokens(self, tokens: list[int]) -> None:
        self.appended_tokens = list(tokens)

    def generate_next_token(self) -> None:
        self.index += 1

    def get_next_tokens(self) -> list[int]:
        return [self.index]

    def close(self) -> None:
        self.closed = True


class _FakeOrtGenAi:
    def __init__(self, *, cuda_available: bool = True, dml_available: bool = True) -> None:
        self.models: list[_FakeModel] = []
        self.configs: list[_FakeConfig] = []
        self.tokenizers: list[_FakeTokenizer] = []
        self.params: list[_FakeGeneratorParams] = []
        self.generators: list[_FakeGenerator] = []
        self.cuda_available = cuda_available
        self.dml_available = dml_available

    def is_cuda_available(self) -> bool:
        return self.cuda_available

    def is_dml_available(self) -> bool:
        return self.dml_available

    def Config(self, model_dir: str) -> _FakeConfig:
        config = _FakeConfig(model_dir)
        self.configs.append(config)
        return config

    def Model(self, config: _FakeConfig) -> _FakeModel:
        model = _FakeModel(config)
        self.models.append(model)
        return model

    def Tokenizer(self, model: _FakeModel) -> _FakeTokenizer:
        tokenizer = _FakeTokenizer(model)
        self.tokenizers.append(tokenizer)
        return tokenizer

    def GeneratorParams(self, model: _FakeModel) -> _FakeGeneratorParams:
        params = _FakeGeneratorParams(model)
        self.params.append(params)
        return params

    def Generator(self, model: _FakeModel, params: _FakeGeneratorParams) -> _FakeGenerator:
        return _FakeGenerator(self, model, params)


class _FakeConfig:
    def __init__(self, model_dir: str) -> None:
        self.model_dir = model_dir
        self.providers: list[str] = ["configured-provider"]
        self.provider_options: dict[str, dict[str, str]] = {}

    def clear_providers(self) -> None:
        self.providers.clear()

    def append_provider(self, provider: str) -> None:
        self.providers.append(provider)

    def set_provider_option(self, provider: str, name: str, value: str) -> None:
        self.provider_options.setdefault(provider, {})[name] = value


def _profile(**options: object) -> RuntimeProfile:
    return RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id="cpu:0",
        provider="CPUExecutionProvider",
        options=options,
    )


@pytest.fixture
def fake_module() -> _FakeOrtGenAi:
    return _FakeOrtGenAi()


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    (tmp_path / "genai_config.json").write_text("{}", encoding="utf-8")
    return tmp_path


@pytest.fixture
def runtime(fake_module: _FakeOrtGenAi, model_dir: Path):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    instance = OrtGenAiChatRuntime()
    instance.start(
        _profile(max_tokens=32, temperature=0.8),
        model_dir=model_dir,
        genai_module=fake_module,
    )
    return instance


@pytest.mark.asyncio
async def test_stream_yields_tokens_and_honors_cancel(runtime, fake_module: _FakeOrtGenAi):
    cancel_token = CancelToken(timeout=None)
    chunks = []
    async for chunk in runtime.stream([{"role": "user", "content": "hi"}], {}, cancel_token):
        chunks.append(chunk)
        cancel_token.cancel()
    assert chunks == ["你"]
    assert fake_module.generators[0].closed is True


@pytest.mark.asyncio
async def test_stream_normalizes_prompt_and_generation_options(runtime, fake_module: _FakeOrtGenAi):
    chunks = [
        chunk
        async for chunk in runtime.stream(
            [
                {"role": "system", "content": "be useful"},
                {"role": "user", "content": "hi"},
            ],
            {"max_tokens": 12, "top_p": 0.9, "unknown": "ignored"},
            CancelToken(timeout=None),
        )
    ]
    assert chunks == ["你", "好"]
    messages = [
        {"role": "system", "content": "be useful"},
        {"role": "user", "content": "hi"},
    ]
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    assert fake_module.tokenizers[0].chat_template_calls == [(serialized, True)]
    assert fake_module.tokenizers[0].encoded == ["<official>chat</official>"]
    assert fake_module.generators[0].appended_tokens == [101, 102]
    assert fake_module.params[0].search_options == {
        "max_length": 14,
        "temperature": 0.8,
        "top_p": 0.9,
    }


@pytest.mark.asyncio
async def test_generator_closes_when_decode_raises(runtime, fake_module: _FakeOrtGenAi):
    def broken_decode(token: int) -> str:
        raise ValueError(token)

    fake_module.tokenizers[0].stream.decode = broken_decode
    with pytest.raises(ValueError):
        async for _ in runtime.stream(
            [{"role": "user", "content": "hi"}], {}, CancelToken(timeout=None)
        ):
            pass
    assert fake_module.generators[0].closed is True


@pytest.mark.asyncio
async def test_generator_closes_when_tokenizer_stream_creation_raises(
    runtime, fake_module: _FakeOrtGenAi
):
    def broken_create_stream() -> None:
        raise ValueError("stream unavailable")

    fake_module.tokenizers[0].create_stream = broken_create_stream
    with pytest.raises(ValueError, match="stream unavailable"):
        async for _ in runtime.stream(
            [{"role": "user", "content": "hi"}], {}, CancelToken(timeout=None)
        ):
            pass
    assert fake_module.generators[0].closed is True


def test_lifecycle_and_runtime_contract(fake_module: _FakeOrtGenAi, model_dir: Path):
    from local_ai.runtimes.base import Runtime
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    instance = OrtGenAiChatRuntime(model_dir)
    assert isinstance(instance, Runtime)
    assert instance.health() is False
    assert instance.start(_profile(), genai_module=fake_module) is True
    assert instance.health() is True
    model = fake_module.models[0]
    tokenizer = fake_module.tokenizers[0]
    instance.stop()
    assert instance.health() is False
    assert model.closed is True
    assert tokenizer.closed is True


def test_start_configures_provider_device_and_provider_options(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id="cuda:2",
        provider="CUDAExecutionProvider",
        options={
            "provider_options": {
                "arena_extend_strategy": "kSameAsRequested",
                "enable_cuda_graph": True,
            }
        },
    )
    OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)
    config = fake_module.configs[0]
    assert config.providers == ["cuda"]
    assert config.provider_options == {
        "cuda": {
            "device_id": "2",
            "arena_extend_strategy": "kSameAsRequested",
            "enable_cuda_graph": "1",
        }
    }
    assert fake_module.models[0].config is config


@pytest.mark.parametrize(
    ("provider", "device_id", "configured_provider"),
    [
        ("CPUExecutionProvider", "cpu:0", None),
        ("DmlExecutionProvider", "dml:1", "dml"),
    ],
)
def test_start_maps_verified_ort_genai_providers(
    fake_module: _FakeOrtGenAi,
    model_dir: Path,
    provider: str,
    device_id: str,
    configured_provider: str | None,
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id=device_id,
        provider=provider,
        options={"provider_options": {"enabled": False}},
    )
    OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)
    config = fake_module.configs[0]
    assert config.providers == ([] if configured_provider is None else [configured_provider])
    if configured_provider is None:
        assert config.provider_options == {}
    else:
        assert config.provider_options == {
            configured_provider: {
                "device_id": device_id.rsplit(":", 1)[1],
                "enabled": "0",
            }
        }


def test_cpu_profile_never_sets_provider_options(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id="cpu:0",
        provider="CPUExecutionProvider",
        options={
            "arena_extend_strategy": "kSameAsRequested",
            "provider_options": {"enabled": False},
        },
    )
    OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)
    assert fake_module.configs[0].provider_options == {}


@pytest.mark.parametrize(
    ("provider", "device_id", "dependency", "capability"),
    [
        ("CUDAExecutionProvider", "cuda:0", "onnxruntime-genai-cuda", "CUDA"),
        ("DmlExecutionProvider", "dml:0", "onnxruntime-genai-directml", "DirectML"),
    ],
)
def test_start_structurally_rejects_provider_missing_from_installed_wheel(
    model_dir: Path,
    provider: str,
    device_id: str,
    dependency: str,
    capability: str,
):
    from local_ai.runtimes.base import RuntimeDependencyError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    module = _FakeOrtGenAi(cuda_available=False, dml_available=False)
    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id=device_id,
        provider=provider,
    )
    with pytest.raises(RuntimeDependencyError) as caught:
        OrtGenAiChatRuntime(model_dir).start(profile, genai_module=module)
    assert caught.value.code == "runtime_dependency_missing"
    assert caught.value.dependency == dependency
    assert capability in caught.value.detail
    assert module.models == []


def test_frozen_cpu_smoke_constructs_config_and_model(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import run_cpu_smoke

    assert run_cpu_smoke(model_dir, genai_module=fake_module) is True
    assert fake_module.configs[0].providers == []
    assert fake_module.configs[0].provider_options == {}
    assert len(fake_module.models) == 1


def test_start_applies_primary_profile_options_without_forwarding_runtime_controls(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id="cuda:3",
        provider="CUDAExecutionProvider",
        options={
            "arena_extend_strategy": "kSameAsRequested",
            "max_tokens": 8,
            "chat_template": "{messages}",
            "providers": ("CUDAExecutionProvider", "CPUExecutionProvider"),
            "provider_options": ({"ignored": True}, {}),
        },
    )
    OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)
    assert fake_module.configs[0].provider_options == {
        "cuda": {
            "arena_extend_strategy": "kSameAsRequested",
            "device_id": "3",
            "ignored": "1",
        }
    }


def test_start_uses_primary_mapping_from_provider_options_sequence(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id="cuda:4",
        provider="CUDAExecutionProvider",
        options={
            "provider_options": (
                {"arena_extend_strategy": "kSameAsRequested"},
                {"device_id": "fallback"},
            )
        },
    )
    OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)
    assert fake_module.configs[0].provider_options == {
        "cuda": {
            "arena_extend_strategy": "kSameAsRequested",
            "device_id": "4",
        }
    }


def test_start_rejects_unverified_provider_name(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.base import RuntimeValidationError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT_GENAI,
        device_id="rocm:0",
        provider="ROCMExecutionProvider",
    )
    with pytest.raises(RuntimeValidationError, match="ROCMExecutionProvider"):
        OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)
    assert fake_module.models == []


def test_start_closes_model_when_tokenizer_creation_fails(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    def broken_tokenizer(model: _FakeModel) -> None:
        raise ValueError("tokenizer unavailable")

    fake_module.Tokenizer = broken_tokenizer
    with pytest.raises(ValueError, match="tokenizer unavailable"):
        OrtGenAiChatRuntime(model_dir).start(_profile(), genai_module=fake_module)
    assert fake_module.models[0].closed is True


def test_stop_attempts_all_resources_clears_state_and_aggregates_failures(
    runtime, fake_module: _FakeOrtGenAi
):
    from local_ai.runtimes.base import RuntimeValidationError

    model = fake_module.models[0]
    tokenizer = fake_module.tokenizers[0]

    def broken_tokenizer_close() -> None:
        tokenizer.closed = True
        raise ValueError("tokenizer close failed")

    def broken_model_close() -> None:
        model.closed = True
        raise OSError("model close failed")

    tokenizer.close = broken_tokenizer_close
    model.close = broken_model_close
    with pytest.raises(RuntimeValidationError, match="tokenizer close failed.*model close failed"):
        runtime.stop()
    assert tokenizer.closed is True
    assert model.closed is True
    assert runtime.health() is False
    assert runtime._module is None
    assert runtime._profile is None


@pytest.mark.asyncio
async def test_stream_requires_started_runtime():
    from local_ai.runtimes.base import RuntimeValidationError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    instance = OrtGenAiChatRuntime("/models/chat")
    with pytest.raises(RuntimeValidationError):
        async for _ in instance.stream([], {}, CancelToken(timeout=None)):
            pass


def test_start_rejects_non_genai_profile(fake_module: _FakeOrtGenAi, model_dir: Path):
    from local_ai.runtimes.base import RuntimeValidationError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    profile = RuntimeProfile(
        runtime=RuntimeKind.ORT,
        device_id="cpu:0",
        provider="CPUExecutionProvider",
    )
    with pytest.raises(RuntimeValidationError):
        OrtGenAiChatRuntime(model_dir).start(profile, genai_module=fake_module)


def test_start_requires_model_directory():
    from local_ai.runtimes.base import RuntimeValidationError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    with pytest.raises(RuntimeValidationError, match="model_dir"):
        OrtGenAiChatRuntime().start(_profile(), genai_module=_FakeOrtGenAi())


def test_start_requires_genai_config(tmp_path: Path):
    from local_ai.runtimes.base import RuntimeValidationError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    with pytest.raises(RuntimeValidationError, match="genai_config.json"):
        OrtGenAiChatRuntime(tmp_path).start(_profile(), genai_module=_FakeOrtGenAi())


def test_missing_dependency_raises_structured_error(
    monkeypatch: pytest.MonkeyPatch, model_dir: Path
):
    from local_ai.runtimes.base import RuntimeDependencyError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    real_import = builtins.__import__

    def missing_import(name: str, *args: object, **kwargs: object):
        if name == "onnxruntime_genai":
            raise ImportError("wheel unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_import)
    with pytest.raises(RuntimeDependencyError) as caught:
        OrtGenAiChatRuntime(model_dir).start(_profile())
    assert caught.value.code == "runtime_dependency_missing"
    assert caught.value.dependency == "onnxruntime-genai"
    assert caught.value.runtime == "ort_genai"
    assert caught.value.platform


def test_start_accepts_factory_injected_directory(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    runtime = OrtGenAiChatRuntime()
    assert runtime.start(
        _profile(), model_dir=model_dir, genai_module=fake_module
    ) is True
    assert fake_module.models[0].model_dir == str(model_dir)


@pytest.mark.asyncio
async def test_stream_uses_explicit_chat_template_when_tokenizer_has_no_helper(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    runtime = OrtGenAiChatRuntime(model_dir)
    runtime.start(
        _profile(prompt_template="<chat>{messages}</chat>"),
        genai_module=fake_module,
    )
    delattr(_FakeTokenizer, "apply_chat_template")
    messages = [{"role": "user", "content": "hi"}]
    try:
        chunks = [
            chunk
            async for chunk in runtime.stream(messages, {}, CancelToken(timeout=None))
        ]
    finally:
        setattr(
            _FakeTokenizer,
            "apply_chat_template",
            lambda self, messages, *, add_generation_prompt: "<official>chat</official>",
        )
    assert chunks == ["你", "好"]
    assert fake_module.tokenizers[0].encoded == [
        f"<chat>{json.dumps(messages, ensure_ascii=False, separators=(',', ':'))}</chat>"
    ]


@pytest.mark.asyncio
async def test_explicit_prompt_template_overrides_tokenizer_chat_template(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    runtime = OrtGenAiChatRuntime(model_dir)
    runtime.start(
        _profile(prompt_template="<manifest>{messages}</manifest>"),
        genai_module=fake_module,
    )
    messages = [
        {"role": "system", "content": "custom system"},
        {"role": "user", "content": "custom user"},
    ]
    chunks = [
        chunk
        async for chunk in runtime.stream(messages, {}, CancelToken(timeout=None))
    ]
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    assert chunks == ["你", "好"]
    assert fake_module.tokenizers[0].chat_template_calls == []
    assert fake_module.tokenizers[0].encoded == [
        f"<manifest>{serialized}</manifest>"
    ]


@pytest.mark.asyncio
async def test_stream_rejects_missing_chat_template_capability(
    fake_module: _FakeOrtGenAi, model_dir: Path
):
    from local_ai.runtimes.base import RuntimeValidationError
    from local_ai.runtimes.ort_genai import OrtGenAiChatRuntime

    runtime = OrtGenAiChatRuntime(model_dir)
    runtime.start(_profile(), genai_module=fake_module)
    delattr(_FakeTokenizer, "apply_chat_template")
    try:
        with pytest.raises(RuntimeValidationError, match="chat_template"):
            async for _ in runtime.stream(
                [{"role": "user", "content": "hi"}], {}, CancelToken(timeout=None)
            ):
                pass
    finally:
        setattr(
            _FakeTokenizer,
            "apply_chat_template",
            lambda self, messages, *, add_generation_prompt: "<official>chat</official>",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": 1.5}, "max_tokens"),
        ({"temperature": 0}, "temperature"),
        ({"top_p": 0}, "top_p"),
        ({"top_p": 1.1}, "top_p"),
        ({"top_k": 0}, "top_k"),
        ({"do_sample": 1}, "do_sample"),
        ({"repetition_penalty": 0}, "repetition_penalty"),
    ],
)
async def test_stream_validates_sampling_ranges(runtime, options: dict[str, object], error: str):
    from local_ai.runtimes.base import RuntimeValidationError

    with pytest.raises(RuntimeValidationError, match=error):
        async for _ in runtime.stream(
            [{"role": "user", "content": "hi"}], options, CancelToken(timeout=None)
        ):
            pass


@pytest.mark.asyncio
async def test_cancel_token_is_checked_before_and_after_each_generation(
    runtime, fake_module: _FakeOrtGenAi
):
    token = SimpleNamespace(is_cancelled=False, checks=0)

    def check() -> None:
        token.checks += 1

    token.check = check
    chunks = [
        chunk
        async for chunk in runtime.stream(
            [{"role": "user", "content": "hi"}], {}, token
        )
    ]
    assert chunks == ["你", "好"]
    assert token.checks == 4
    assert fake_module.generators[0].closed is True


def test_onnxruntime_genai_is_lazy_loaded():
    source = Path("local_ai/runtimes/ort_genai.py").read_text(encoding="utf-8")
    assert "import onnxruntime_genai" not in source.split("class OrtGenAiChatRuntime", 1)[0]


def test_optional_dependency_and_pyinstaller_contracts():
    with Path("pyproject.toml").open("rb") as file:
        local_ai = tomllib.load(file)["project"]["optional-dependencies"]["local-ai"]
    requirement = Requirement(local_ai[0])
    spec = Path("xiaoda-agent.spec").read_text(encoding="utf-8")
    assert requirement.name == "onnxruntime-genai"
    assert requirement.specifier.contains("0.15.2")
    assert "collect_submodules('onnxruntime_genai')" in spec
