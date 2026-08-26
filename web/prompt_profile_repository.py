from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from loguru import logger

from web.prompt_ab import promote_gate
from web.prompt_profiles import profile_by_id

# 安全占位符：仅做字面替换，不走 str.format_map——后者会解析索引/属性/
# 格式说明符（IndexError 逃逸、__class__ 属性穿越），且模板无法含字面花括号
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MARKER = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")


class PromptProfileRepository:
    """ConfigService-backed prompt staging/production/rollback repository."""

    def __init__(self, config: Any) -> None:
        self._config = config

    @staticmethod
    def _hash(record: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "prompt_id": record["prompt_id"],
                "version": record["version"],
                "system_template": record.get("system_template", ""),
                "user_template": record.get("user_template", ""),
                "variables": record.get("variables", {}),
                "output_schema": record.get("output_schema", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _validate_schema(schema: dict[str, Any]) -> None:
        schema_type = schema.get("type")
        if schema_type not in {"object", "array", "string"}:
            raise ValueError("prompt output_schema type is invalid")
        if schema_type == "object":
            properties = schema.get("properties")
            required = schema.get("required")
            if not isinstance(properties, dict) or not properties:
                raise ValueError("object output_schema requires properties")
            if not isinstance(required, list) or not required:
                raise ValueError("object output_schema requires required fields")
            if not set(required) <= set(properties):
                raise ValueError("required fields must exist in properties")

    def stage(self, record: dict[str, Any]) -> dict[str, Any]:
        prompt_id = str(record.get("prompt_id") or "").strip()
        version = str(record.get("version") or "").strip()
        if profile_by_id(prompt_id) is None:
            raise ValueError(f"unknown prompt profile: {prompt_id}")
        if not version:
            raise ValueError("prompt version is required")
        variables = record.get("variables") or {}
        output_schema = record.get("output_schema") or {}
        if not isinstance(variables, dict) or not isinstance(output_schema, dict):
            raise ValueError("prompt variables and output_schema must be objects")
        self._validate_schema(output_schema)
        profile = profile_by_id(prompt_id)
        if profile is not None and profile.system_slot and not str(record.get("system_template") or "").strip():
            raise ValueError(
                f"{prompt_id} 治理对象是 system 槽：改进内容必须写入 system_template"
            )
        staged = {
            "prompt_id": prompt_id,
            "version": version,
            "system_template": str(record.get("system_template") or ""),
            "user_template": str(record.get("user_template") or ""),
            "variables": dict(variables),
            "output_schema": dict(output_schema),
            "status": "staging",
        }
        # 入库前试渲染：坏模板在 stage 就拒绝，杜绝经 promote 免报告通道直达 production
        self._validate_by_trial_render(staged)
        staged["template_hash"] = self._hash(staged)
        self._config.set(f"prompt_profiles.staging.{prompt_id}", staged)
        return staged

    def _validate_by_trial_render(self, record: dict[str, Any]) -> None:
        """用声明变量的样本值做试渲染，非法模板拒绝入库。

        覆盖：system_template 引用受信变量、渲染路径任何意外异常——
        两者在运行期才暴露会让 production override 每次渲染失败并被
        消费方静默回退内置模板，promote 看似生效实则无效。
        """
        declared = record.get("variables") or {}
        sample = {name: f"<sample:{name}>" for name in declared}
        try:
            self._render_record(record, sample)
        except ValueError as exc:
            raise ValueError(f"prompt template failed trial render: {exc}") from exc

    def promote(self, prompt_id: str, ab_report: dict[str, Any] | None = None,
                force: bool = False) -> dict[str, Any]:
        staged = self._config.get(f"prompt_profiles.staging.{prompt_id}")
        if not isinstance(staged, dict):
            raise ValueError(f"no staged prompt profile: {prompt_id}")
        # 门禁强制：所有生成节点均已具备 golden cases 与 ab-run 通道，
        # 未评测候选不得进入 production。force=True 为运维逃生舱
        # （快速版本翻转/回滚链演练），必须显式传递且被审计记录。
        if ab_report is None and not force:
            raise ValueError(
                f"promotion of {prompt_id} requires a passing prompt AB gate report "
                "(run ab-run first); pass force=true to override deliberately"
            )
        if ab_report is not None:
            passed, reasons = promote_gate(ab_report)
            if not passed:
                raise ValueError(
                    f"prompt AB gate rejected promotion of {prompt_id}: "
                    f"{'; '.join(reasons)}"
                )
        current = self._config.get(f"prompt_profiles.production.{prompt_id}")
        history = self._config.get(f"prompt_profiles.history.{prompt_id}", [])
        history = list(history) if isinstance(history, list) else []
        if isinstance(current, dict):
            history.append(current)
        production = {**staged, "status": "production"}
        self._config.replace_many(
            {
                f"prompt_profiles.production.{prompt_id}": production,
                f"prompt_profiles.history.{prompt_id}": history[-20:],
            },
            [f"prompt_profiles.staging.{prompt_id}"],
        )
        return production

    @staticmethod
    def _render_record(record: dict[str, Any], variables: dict[str, Any]) -> tuple[str, str]:
        declared = record.get("variables") or {}
        allowed = set(declared) if isinstance(declared, dict) else set()
        if set(variables) - allowed:
            raise ValueError("prompt variables contain undeclared names")
        required = {
            name for name, spec in declared.items()
            if isinstance(spec, dict) and spec.get("required")
        }
        if required - set(variables):
            raise ValueError("prompt variables are missing required names")
        safe_values = {name: str(value) for name, value in variables.items()}
        system_template = str(record.get("system_template") or "")
        if any(f"{{{name}}}" in system_template for name in safe_values):
            raise ValueError("untrusted variables are not allowed in system_template")
        user_template = str(record.get("user_template") or "")
        unknown = sorted({
            token for token in _PLACEHOLDER.findall(user_template)
            if token not in safe_values
        })
        if unknown:
            logger.warning("prompt_profile.unknown_placeholders tokens={}", unknown)
        user = ""
        try:
            user = _PLACEHOLDER.sub(
                lambda m: safe_values.get(m.group(1), m.group(0)), user_template)
            user = _MARKER.sub(
                lambda m: safe_values.get(m.group(1), m.group(0)), user)
        except Exception as exc:
            # 兜底收窄：白名单字面替换不解析索引/属性/格式说明符，正常不会因
            # 模板内容抛错；万一抛出也只允许带明确信息的 ValueError 逃逸，
            # 绝不让 IndexError 等穿透进消息处理链路（stage 试渲染同样据此拒绝）
            logger.exception("prompt_profile.render_failed prompt_id={}",
                             record.get("prompt_id"))
            raise ValueError(f"prompt template render failed: {exc}") from exc
        return system_template, user

    def resolve(self, prompt_id: str, variables: dict[str, Any]) -> tuple[str, str] | None:
        production = self._config.get(f"prompt_profiles.production.{prompt_id}")
        if not isinstance(production, dict):
            return None
        return self._render_record(production, variables)

    def render_staged(self, prompt_id: str, variables: dict[str, Any]) -> tuple[str, str] | None:
        staged = self._config.get(f"prompt_profiles.staging.{prompt_id}")
        if not isinstance(staged, dict):
            return None
        return self._render_record(staged, variables)

    def rollback(self, prompt_id: str) -> dict[str, Any]:
        stored = self._config.get(f"prompt_profiles.history.{prompt_id}", [])
        if not isinstance(stored, list) or not stored:
            raise ValueError(f"no prompt profile rollback available: {prompt_id}")
        current = self._config.get(f"prompt_profiles.production.{prompt_id}")
        history = list(stored)
        if isinstance(current, dict):
            # current unshift 进队首、pop 队尾构成轮转队列：连续回滚可依次
            # 走遍历史且任何一版不丢失；若 append 到队尾则最后两版乒乓，
            # 若直接丢弃 current 则中间版本永久丢失（原缺陷）
            history.insert(0, dict(current))
        previous = dict(history.pop())
        previous["status"] = "production"
        self._config.set_many({
            f"prompt_profiles.production.{prompt_id}": previous,
            f"prompt_profiles.history.{prompt_id}": history[-20:],
        })
        return previous


def try_resolve(prompt_id: str, variables: dict[str, Any]) -> tuple[str, str] | None:
    """运行时统一消费缝：production override 存在则渲染返回，否则 None。

    任何失败（web 配置不可用、变量不匹配、存储不可读）都安全回退 None，
    调用方使用内置模板——治理层故障绝不阻断业务路径。
    """
    try:
        from web.config_service import get_config_service

        repo = PromptProfileRepository(get_config_service())
        return repo.resolve(prompt_id, variables)
    except ValueError as exc:
        # 有 production 记录但渲染失败：配置错误必须显性可见，
        # 否则运营者会看到"已晋级却仍走旧提示词"的假象
        logger.warning("prompt_profile.override_render_failed prompt={} error={}",
                       prompt_id, exc)
        return None
    except Exception as exc:
        logger.debug("prompt_profile.resolve_fallback prompt={} error={}",
                     prompt_id, exc)
        return None
