from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import ClassVar, Any, AsyncIterator, Mapping, Sequence

from loguru import logger

from local_ai.contracts import RuntimeKind, RuntimeProfile
from local_ai.runtimes.base import Runtime, RuntimeDependencyError, RuntimeValidationError


class OrtGenAiChatRuntime(Runtime):
    _PROVIDERS: ClassVar[dict[str, str]] = {
        "CPUExecutionProvider": "cpu",
        "CUDAExecutionProvider": "cuda",
        "DmlExecutionProvider": "dml",
    }
    _RUNTIME_OPTION_NAMES: ClassVar[set[str]] = {
        "chat_template",
        "do_sample",
        "fallback_bindings",
        "fallback_providers",
        "max_length",
        "max_tokens",
        "prompt_template",
        "provider_options",
        "providers",
        "repetition_penalty",
        "temperature",
        "top_k",
        "top_p",
    }

    def __init__(self, model_dir: str | Path | None = None) -> None:
        self._model_dir = Path(model_dir) if model_dir is not None else None
        self._module: Any = None
        self._model: Any = None
        self._tokenizer: Any = None
        self._profile: RuntimeProfile | None = None

    def start(
        self,
        profile: RuntimeProfile,
        *,
        model_dir: str | Path | None = None,
        genai_module: Any = None,
    ) -> bool:
        if profile.runtime is not RuntimeKind.ORT_GENAI:
            raise RuntimeValidationError("ORT GenAI runtime requires an ort_genai profile")
        if self.health():
            return True
        resolved_model_dir = Path(model_dir) if model_dir is not None else self._model_dir
        if resolved_model_dir is None:
            raise RuntimeValidationError("model_dir is required for ORT GenAI runtime")
        config_path = resolved_model_dir / "genai_config.json"
        if not config_path.is_file():
            # ModelScope 官方 ONNX 仓库（如 microsoft/Phi-3-*-onnx）把
            # genai_config.json 放在 cpu_and_mobile/<variant>/ 等子目录，
            # 递归定位第一个 genai_config.json 所在目录作为模型根。
            found = next(
                (path for path in resolved_model_dir.rglob("genai_config.json") if path.is_file()),
                None,
            )
            if found is None:
                raise RuntimeValidationError(
                    f"genai_config.json not found in {resolved_model_dir}"
                )
            resolved_model_dir = found.parent
            config_path = found
        if genai_module is None:
            try:
                import onnxruntime_genai as genai_module
            except ImportError as error:
                raise RuntimeDependencyError(
                    "onnxruntime-genai", "ort_genai", str(error)
                ) from error
        model = None
        tokenizer = None
        try:
            provider = self._provider_name(profile.provider)
            self._require_provider_capability(genai_module, provider)
            config = genai_module.Config(str(resolved_model_dir))
            config.clear_providers()
            if provider != "cpu":
                config.append_provider(provider)
                for name, value in self._provider_options(profile).items():
                    config.set_provider_option(provider, name, value)
            model = genai_module.Model(config)
            tokenizer = genai_module.Tokenizer(model)
        except (OSError, RuntimeError, ValueError, ImportError) as error:
            cleanup_errors = self._close_resources((tokenizer, model))
            logger.warning("ort_genai.load_failed error={}", str(error)[:200])
            if cleanup_errors:
                raise RuntimeValidationError(
                    self._cleanup_error_message(cleanup_errors)
                ) from error
            raise
        except Exception as error:
            cleanup_errors = self._close_resources((tokenizer, model))
            logger.exception("ort_genai.start.unexpected_error")
            if cleanup_errors:
                raise RuntimeValidationError(
                    self._cleanup_error_message(cleanup_errors)
                ) from error
            raise
        self._model_dir = resolved_model_dir
        self._module = genai_module
        self._model = model
        self._tokenizer = tokenizer
        self._profile = profile
        return True

    def health(self) -> bool:
        return self._model is not None and self._tokenizer is not None

    def stop(self) -> None:
        errors: list[Exception] = []
        try:
            errors = self._close_resources((self._tokenizer, self._model))
        finally:
            self._tokenizer = None
            self._model = None
            self._module = None
            self._profile = None
        if errors:
            raise RuntimeValidationError(self._cleanup_error_message(errors))

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        options: Mapping[str, Any],
        cancel_token: Any,
    ) -> AsyncIterator[str]:
        if not self.health() or self._profile is None:
            raise RuntimeValidationError("ORT GenAI runtime not started")
        prompt = self._normalize_prompt(messages)
        input_ids = self._tokenizer.encode(prompt)
        params = self._module.GeneratorParams(self._model)
        search_options = self._normalize_options(options, len(input_ids))
        if search_options:
            params.set_search_options(**search_options)
        generator = self._module.Generator(self._model, params)
        try:
            generator.append_tokens(input_ids)
            tokenizer_stream = self._tokenizer.create_stream()
            while not generator.is_done() and not cancel_token.is_cancelled:
                cancel_token.check()
                generator.generate_next_token()
                cancel_token.check()
                for token in generator.get_next_tokens():
                    chunk = tokenizer_stream.decode(token)
                    if chunk:
                        yield chunk
                await asyncio.sleep(0)
        finally:
            close = getattr(generator, "close", None)
            if close is not None:
                close()

    def _normalize_prompt(self, messages: Sequence[Mapping[str, Any]]) -> str:
        normalized = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise RuntimeValidationError(f"unsupported message role: {role}")
            if not isinstance(content, str):
                raise RuntimeValidationError("message content must be a string")
            normalized.append({"role": role, "content": content})
        serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        template = self._profile.options.get("prompt_template")
        if template is not None:
            if not isinstance(template, str) or template.count("{messages}") != 1:
                raise RuntimeValidationError(
                    "profile.options.prompt_template must contain one {messages} placeholder"
                )
            return template.replace("{messages}", serialized)
        apply_chat_template = getattr(self._tokenizer, "apply_chat_template", None)
        if callable(apply_chat_template):
            return apply_chat_template(serialized, add_generation_prompt=True)
        raise RuntimeValidationError(
            "tokenizer does not support apply_chat_template and profile.options.prompt_template is not configured"
        )

    def _normalize_options(
        self, options: Mapping[str, Any], input_length: int
    ) -> dict[str, Any]:
        configured = dict(self._profile.options)
        merged = {**configured, **dict(options)}
        normalized: dict[str, Any] = {}
        if "max_tokens" in merged:
            max_tokens = merged["max_tokens"]
            if type(max_tokens) is not int or max_tokens <= 0:
                raise RuntimeValidationError("max_tokens must be a positive integer")
            normalized["max_length"] = input_length + max_tokens
        elif "max_length" in merged:
            max_length = merged["max_length"]
            if type(max_length) is not int or max_length <= input_length:
                raise RuntimeValidationError(
                    "max_length must be an integer greater than the input token length"
                )
            normalized["max_length"] = max_length
        for name in ("temperature", "top_p", "top_k", "do_sample", "repetition_penalty"):
            if name in merged:
                normalized[name] = self._sampling_value(name, merged[name])
        return normalized

    @staticmethod
    def _provider_options(profile: RuntimeProfile) -> dict[str, str]:
        options = {
            name: value
            for name, value in profile.options.items()
            if name not in OrtGenAiChatRuntime._RUNTIME_OPTION_NAMES
        }
        configured = profile.options.get("provider_options", {})
        if isinstance(configured, Mapping):
            options.update(configured)
        elif isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
            if configured:
                primary = configured[0]
                if not isinstance(primary, Mapping):
                    raise RuntimeValidationError(
                        "provider_options sequence must contain mappings"
                    )
                options.update(primary)
        else:
            raise RuntimeValidationError("provider_options must be a mapping or sequence")
        _, separator, device_index = profile.device_id.rpartition(":")
        if separator and device_index:
            options.setdefault("device_id", device_index)
        return {
            str(name): "1" if value is True else "0" if value is False else str(value)
            for name, value in options.items()
        }

    @classmethod
    def _provider_name(cls, provider: str) -> str:
        try:
            return cls._PROVIDERS[provider]
        except KeyError as error:
            raise RuntimeValidationError(
                f"unsupported ORT GenAI provider: {provider}"
            ) from error

    @staticmethod
    def _require_provider_capability(genai_module: Any, provider: str) -> None:
        capabilities = {
            "cuda": ("is_cuda_available", "onnxruntime-genai-cuda", "CUDA"),
            "dml": ("is_dml_available", "onnxruntime-genai-directml", "DirectML"),
        }
        if provider not in capabilities:
            return
        probe_name, dependency, label = capabilities[provider]
        probe = getattr(genai_module, probe_name, None)
        if callable(probe) and probe():
            return
        raise RuntimeDependencyError(
            dependency,
            "ort_genai",
            f"installed ONNX Runtime GenAI wheel does not provide {label} capability",
        )

    @staticmethod
    def _sampling_value(name: str, value: Any) -> Any:
        if name == "do_sample":
            if type(value) is not bool:
                raise RuntimeValidationError("do_sample must be a boolean")
            return value
        if name == "top_k":
            if type(value) is not int or value < 1:
                raise RuntimeValidationError("top_k must be a positive integer")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeValidationError(f"{name} must be a finite number")
        if name == "top_p" and not 0 < value <= 1:
            raise RuntimeValidationError("top_p must be greater than 0 and at most 1")
        if name in {"temperature", "repetition_penalty"} and value <= 0:
            raise RuntimeValidationError(f"{name} must be greater than 0")
        return value

    @staticmethod
    def _close_resources(resources: Sequence[Any]) -> list[Exception]:
        errors = []
        for resource in resources:
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    close()
                except (OSError, RuntimeError, AttributeError) as error:
                    logger.debug("ort_genai.resource_close_failed error={}", str(error))
                    errors.append(error)
                except Exception as error:
                    logger.exception("ort_genai._close_resources.unexpected_error")
                    errors.append(error)
        return errors

    @staticmethod
    def _cleanup_error_message(errors: Sequence[Exception]) -> str:
        details = "; ".join(str(error) or type(error).__name__ for error in errors)
        return f"ORT GenAI resource cleanup failed: {details}"


def run_cpu_smoke(model_dir: str | Path, *, genai_module: Any = None) -> bool:
    if genai_module is None:
        import onnxruntime_genai as genai_module

    config = genai_module.Config(str(model_dir))
    config.clear_providers()
    genai_module.Model(config)
    return True