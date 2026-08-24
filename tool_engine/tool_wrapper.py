"""工具调用 Wrapper 层.

为每个工具调用添加：参数校验、路径校验、格式转换、错误恢复。
参考:
  - Amazon SSA: 模型特定接口适配
  - AHE: 工具结构性改进贡献最大性能提升
  - Niklaus 实验: 文件路径错误是 0% 得分根因
"""
import contextvars
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("tool_wrapper")

# ============================================================
# 路径校验器（文件类工具专用）
# ============================================================

def validate_file_path(
    path: str,
    *,
    must_exist: bool = False,
    must_not_exist: bool = False,
    must_be_dir: bool = False,
    must_be_file: bool = False,
    allow_write: bool = False,
) -> tuple[bool, str]:
    """校验文件路径的合法性、安全性、存在性.

    Returns:
        (is_valid, error_message)
    """
    if not path:
        return False, "路径不能为空"

    abs_path = os.path.abspath(path)

    # 安全检查: 路径穿越
    if ".." in Path(abs_path).parts:
        return False, f"路径不允许包含 '..': {path}"

    # 复用 file_tools_v2 的白名单（fail-closed：导入失败时拒绝访问）
    try:
        from tools.file_tools_v2 import ALLOWED_BASE_DIRS, SENSITIVE_PATHS
        # 敏感路径黑名单
        real_path = os.path.realpath(abs_path)
        for sp in SENSITIVE_PATHS:
            if real_path == sp or real_path.startswith(sp + os.sep):
                return False, f"路径属于敏感路径: {path}"
        # 白名单检查（追加 os.sep 防止同级目录前缀混淆）
        if ALLOWED_BASE_DIRS and not any(real_path == base or real_path.startswith(base + os.sep)
                  for base in ALLOWED_BASE_DIRS):
            return False, f"路径不在允许范围内: {path}"
    except ImportError:
        logger.warning("tool_wrapper.safety_module_unavailable")
        return False, "安全模块不可用，拒绝访问"

    # 存在性检查
    if must_exist and not os.path.exists(abs_path):
        return False, f"路径不存在: {path}"
    if must_not_exist and os.path.exists(abs_path):
        return False, f"路径已存在: {path}"

    # 类型检查
    if must_be_dir and os.path.exists(abs_path) and not os.path.isdir(abs_path):
        return False, f"路径不是目录: {path}"
    if must_be_file and os.path.exists(abs_path) and not os.path.isfile(abs_path):
        return False, f"路径不是文件: {path}"

    # 写权限检查
    if allow_write:
        parent = os.path.dirname(abs_path)
        if os.path.exists(parent) and not os.access(parent, os.W_OK):
            return False, f"目录不可写: {parent}"

    return True, ""


# ============================================================
# 参数校验器
# ============================================================

def validate_tool_params(schema: dict, params: dict) -> tuple[bool, list[str]]:
    """按 JSON Schema 校验工具参数.

    Returns:
        (is_valid, error_messages)
    """
    errors: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # 必填检查
    for field in required:
        if field not in params:
            errors.append(f"缺少必填参数: {field}")

    # 类型检查（基础）
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for key, value in params.items():
        if key in properties:
            expected_type = properties[key].get("type")
            if expected_type and expected_type in type_map:
                # bool 是 int 的子类，integer/number 类型需显式排除 bool
                if expected_type in ("integer", "number") and isinstance(value, bool):
                    errors.append(
                        f"参数 '{key}' 类型错误: 期望 {expected_type}, 实际 bool"
                    )
                elif not isinstance(value, type_map[expected_type]):
                    errors.append(
                        f"参数 '{key}' 类型错误: 期望 {expected_type}, "
                        f"实际 {type(value).__name__}"
                    )
            # 枚举检查
            enum_vals = properties[key].get("enum")
            if enum_vals and value not in enum_vals:
                errors.append(f"参数 '{key}' 值不在允许范围: {enum_vals}")
            # 范围检查
            minimum = properties[key].get("minimum")
            maximum = properties[key].get("maximum")
            if minimum is not None and isinstance(value, (int, float)) and value < minimum:
                errors.append(f"参数 '{key}' 值 {value} 小于最小值 {minimum}")
            if maximum is not None and isinstance(value, (int, float)) and value > maximum:
                errors.append(f"参数 '{key}' 值 {value} 大于最大值 {maximum}")

    return len(errors) == 0, errors


# ============================================================
# 统一返回格式
# ============================================================

class ToolResultV2:
    """工具调用统一返回格式 V2.

    成功: {"ok": true, "data": ..., "metadata": {...}}
    失败: {"ok": false, "error": {"code": "ERR_XXX", "message": "...", "suggestion": "..."}}
    """

    def __init__(
        self,
        ok: bool,
        data: Any = None,
        error_code: str = "",
        error_msg: str = "",
        suggestion: str = "",
        metadata: dict | None = None,
    ):
        self.ok = ok
        self.data = data
        self.error_code = error_code
        self.error_msg = error_msg
        self.suggestion = suggestion
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        if self.ok:
            return {"ok": True, "data": self.data, "metadata": self.metadata}
        return {
            "ok": False,
            "error": {
                "code": self.error_code,
                "message": self.error_msg,
                "suggestion": self.suggestion,
            },
        }

    @classmethod
    def success(cls, data: Any = None, **metadata) -> "ToolResultV2":
        return cls(ok=True, data=data, metadata=metadata)

    @classmethod
    def fail(cls, code: str, message: str, suggestion: str = "") -> "ToolResultV2":
        return cls(ok=False, error_code=code, error_msg=message, suggestion=suggestion)


# ============================================================
# 模型特定接口适配
# ============================================================

_CURRENT_MODEL_FAMILY: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_CURRENT_MODEL_FAMILY", default="default"
)


def set_model_family(family: str):
    """设置当前模型家族（基于 ContextVar，协程隔离）."""
    _CURRENT_MODEL_FAMILY.set(family)


def get_tool_description_for_model(
    base_description: str,
    model_overrides: dict | None,
    model_family: str | None = None,
) -> str:
    """根据当前模型家族返回适配后的工具描述."""
    family = model_family or _CURRENT_MODEL_FAMILY.get()
    if model_overrides and family in model_overrides:
        return model_overrides[family].get("description", base_description)
    return base_description


def get_tool_schema_for_model(
    base_schema: dict,
    model_overrides: dict | None,
    model_family: str | None = None,
) -> dict:
    """根据当前模型家族返回适配后的工具 Schema."""
    family = model_family or _CURRENT_MODEL_FAMILY.get()
    if model_overrides and family in model_overrides:
        return model_overrides[family].get("schema", base_schema)
    return base_schema
