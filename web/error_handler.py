"""FastAPI 统一异常处理器

注册后:
    - AppException 及其子类 -> JSON {"error_code", "message", "retryable", "details"}
      HTTP 状态码取自 error_code.http_status
    - 其它未捕获异常 -> E_SYS999 (500)
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from core.app_exception import AppException
from core.error_codes import ErrorCodeEnum


def _build_body(error_code: ErrorCodeEnum, message: str, details: dict) -> dict:
    """构造统一错误响应体"""
    return {
        "error_code": error_code.code,
        "message": message,
        "retryable": error_code.retryable,
        "details": details,
    }


async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """处理 AppException 及其子类"""
    logger.warning(
        "web.app_exception",
        error_code=exc.error_code.code,
        message=exc.message,
        path=request.url.path,
        method=request.method,
    )
    return JSONResponse(
        status_code=exc.error_code.http_status,
        content=_build_body(exc.error_code, exc.message, exc.details),
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底处理所有未捕获异常 -> E_SYS999 (500)

    注意: HTTPException / RequestValidationError 拥有 FastAPI 默认处理器,
    不会被本 handler 拦截, 保持现有行为不变。
    """
    logger.exception(
        "web.unhandled_exception",
        error_type=type(exc).__name__,
        path=request.url.path,
        method=request.method,
    )
    err = ErrorCodeEnum.E_SYS999
    return JSONResponse(
        status_code=err.http_status,
        content=_build_body(err, err.message, {"exception_type": type(exc).__name__}),
    )


def register_error_handlers(app: FastAPI) -> None:
    """向 FastAPI 注册统一异常处理器"""
    app.add_exception_handler(AppException, _app_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    logger.info("web.error_handlers.registered")
