"""统一异常基类 — 所有业务异常都携带结构化 error_code

子类与默认错误码:
    AuthError      -> E_AUTH001 (401)
    ToolError      -> E_TOOL004 (500)
    LLMError       -> E_LLM001 (502)
    MemoryError    -> E_MEM001 (500)  注意: 此处遮蔽内置 MemoryError
    NetworkError   -> E_NET002 (502)
    ConfigError    -> E_CFG001 (500)
    DatabaseError  -> E_DB001 (500)
    RateLimitError -> E_RATE001 (429)
    SystemError    -> E_SYS003 (500)

用法:
    raise LLMError("MiMo client not initialized, check MIMO_API_KEY")
    raise ToolError("工具参数错误", error_code=ErrorCodeEnum.E_TOOL002, details={"arg": "q"})
"""
from __future__ import annotations

from typing import Any

from core.error_codes import ErrorCodeEnum


class AppException(Exception):
    """应用异常基类

    属性:
        error_code: ErrorCodeEnum 错误码
        message:    人类可读消息 (缺省取 error_code.message)
        details:    额外上下文字典
        cause:      原始异常 (可选)
    """

    # 子类覆盖此默认错误码
    DEFAULT_ERROR_CODE: ErrorCodeEnum = ErrorCodeEnum.E_SYS003

    def __init__(
        self,
        message: str = "",
        *,
        error_code: ErrorCodeEnum | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.error_code: ErrorCodeEnum = error_code if error_code is not None else self.DEFAULT_ERROR_CODE
        self.message: str = message if message else self.error_code.message
        self.details: dict[str, Any] = dict(details) if details else {}
        self.cause: Exception | None = cause
        super().__init__(self.message)
        # 链接原始异常，便于 traceback
        if cause is not None and isinstance(cause, BaseException):
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        """序列化为响应字典

        返回: {"error_code": "E_AUTH001", "message": "...", "details": {...}, "retryable": true}
        """
        return {
            "error_code": self.error_code.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.error_code.retryable,
        }

    def __str__(self) -> str:
        return f"[{self.error_code.code}] {self.message}"


# ============================================================
# 分类异常子类 —— 每个默认关联一个错误码
# ============================================================


class AuthError(AppException):
    """认证/授权错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_AUTH001


class ToolError(AppException):
    """工具调用错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_TOOL004


class LLMError(AppException):
    """LLM 调用错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_LLM001


class MemoryError(AppException):  # noqa: A001 故意遮蔽内置，业务内存系统异常
    """记忆系统错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_MEM001


class NetworkError(AppException):
    """网络错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_NET002


class ConfigError(AppException):
    """配置错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_CFG001


class DatabaseError(AppException):
    """数据库错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_DB001


class RateLimitError(AppException):
    """速率限制错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_RATE001


class SystemError(AppException):
    """系统错误"""
    DEFAULT_ERROR_CODE = ErrorCodeEnum.E_SYS003


# 子类 -> 默认错误码 的映射表，便于 from_exception 等场景反向查询
SUBCLASS_DEFAULT_MAP: dict[type[AppException], ErrorCodeEnum] = {
    AuthError: ErrorCodeEnum.E_AUTH001,
    ToolError: ErrorCodeEnum.E_TOOL004,
    LLMError: ErrorCodeEnum.E_LLM001,
    MemoryError: ErrorCodeEnum.E_MEM001,
    NetworkError: ErrorCodeEnum.E_NET002,
    ConfigError: ErrorCodeEnum.E_CFG001,
    DatabaseError: ErrorCodeEnum.E_DB001,
    RateLimitError: ErrorCodeEnum.E_RATE001,
    SystemError: ErrorCodeEnum.E_SYS003,
}
