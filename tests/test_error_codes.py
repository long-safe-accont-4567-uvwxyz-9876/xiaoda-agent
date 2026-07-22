"""Q1 全局错误码体系测试

覆盖:
    - ErrorCodeEnum 定义格式
    - from_exception 异常映射
    - AppException.to_dict / __str__
    - HTTP 状态码与 retryable 映射
    - FastAPI 异常处理器返回 JSON
"""
import builtins
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core.app_exception import (
    AuthError,
    ConfigError,
    DatabaseError,
    LLMError,
    MemoryError,
    NetworkError,
    RateLimitError,
    SystemError,
    ToolError,
)
from core.error_codes import ErrorCodeEnum, from_exception

# 错误码格式: E_类别(大写字母)+3位数字
_CODE_RE = re.compile(r"^E_[A-Z]+\d{3}$")
# 内置 MemoryError (内存耗尽)，区别于 app_exception.MemoryError
BuiltinMemoryError = builtins.MemoryError


# ============================================================
# 1. 错误码定义格式
# ============================================================
def test_error_code_definition():
    """所有错误码格式正确 (E_AUTH001 风格) 且唯一"""
    seen = set()
    for member in ErrorCodeEnum:
        # 格式校验
        assert _CODE_RE.match(member.code), f"格式错误: {member.name} -> {member.code}"
        # 字段类型校验
        assert isinstance(member.http_status, int)
        assert 100 <= member.http_status <= 599, f"http_status 越界: {member.name}"
        assert isinstance(member.message, str) and member.message
        assert isinstance(member.retryable, bool)
        # 唯一性校验
        assert member.code not in seen, f"错误码重复: {member.code}"
        seen.add(member.code)

    # 关键错误码存在性校验
    assert ErrorCodeEnum.E_SYS999.code == "E_SYS999"
    assert ErrorCodeEnum.E_AUTH001.code == "E_AUTH001"


# ============================================================
# 2. from_exception 异常类型映射
# ============================================================
def test_from_exception_builtin_types():
    """内置异常类型正确映射到错误码"""
    assert from_exception(TimeoutError("read timeout")) is ErrorCodeEnum.E_NET001
    assert from_exception(ConnectionError("conn refused")) is ErrorCodeEnum.E_NET002
    assert from_exception(PermissionError("denied")) is ErrorCodeEnum.E_AUTH003
    assert from_exception(FileNotFoundError("config.json")) is ErrorCodeEnum.E_CFG001
    # 内置 MemoryError (内存耗尽) -> E_SYS001，注意用内置而非 app_exception.MemoryError
    assert from_exception(BuiltinMemoryError("oom")) is ErrorCodeEnum.E_SYS001


def test_from_exception_app_exception_passthrough():
    """AppException 直接返回其自带 error_code"""
    exc = LLMError("context too long", error_code=ErrorCodeEnum.E_LLM002)
    assert from_exception(exc) is ErrorCodeEnum.E_LLM002

    exc2 = AuthError("no token")
    assert from_exception(exc2) is ErrorCodeEnum.E_AUTH001


def test_from_exception_message_keyword():
    """消息关键字兜底匹配"""
    assert from_exception(Exception("SSRF attempt blocked")) is ErrorCodeEnum.E_NET004
    assert from_exception(Exception("rate limit exceeded, 429")) is ErrorCodeEnum.E_LLM007
    assert from_exception(Exception("HTTP 401 Unauthorized")) is ErrorCodeEnum.E_AUTH001
    assert from_exception(RuntimeError("some random failure")) is ErrorCodeEnum.E_SYS999


def test_from_exception_openai_types():
    """openai 库异常类型映射 (若可构造)"""
    try:
        import openai
    except Exception:
        pytest.skip("openai 不可用，跳过 openai 异常映射测试")

    # APITimeoutError 可直接构造
    try:
        exc = openai.APITimeoutError(request=None)
    except TypeError:
        try:
            exc = openai.APITimeoutError("timeout")
        except Exception:
            pytest.skip("无法构造 openai.APITimeoutError")
    assert from_exception(exc) is ErrorCodeEnum.E_LLM004


# ============================================================
# 3. AppException.to_dict
# ============================================================
def test_app_exception_to_dict():
    """to_dict 返回正确格式"""
    exc = AuthError("未授权访问", details={"path": "/api/v1/chat"})
    d = exc.to_dict()
    assert set(d.keys()) == {"error_code", "message", "details", "retryable"}
    assert d["error_code"] == "E_AUTH001"
    assert d["message"] == "未授权访问"
    assert d["details"] == {"path": "/api/v1/chat"}
    assert d["retryable"] is False

    # 默认 message 取 error_code.message
    exc2 = ToolError()
    assert exc2.to_dict()["message"] == ErrorCodeEnum.E_TOOL004.message
    assert exc2.to_dict()["error_code"] == "E_TOOL004"


# ============================================================
# 4. AppException.__str__
# ============================================================
def test_app_exception_str():
    """__str__ 格式为 [E_XXX] message"""
    exc = AuthError("未授权")
    assert str(exc) == "[E_AUTH001] 未授权"

    exc2 = LLMError("api error", error_code=ErrorCodeEnum.E_LLM004)
    assert str(exc2) == "[E_LLM004] api error"


def test_app_exception_subclass_default_codes():
    """每个子类默认关联正确错误码"""
    cases = [
        (AuthError(), ErrorCodeEnum.E_AUTH001),
        (ToolError(), ErrorCodeEnum.E_TOOL004),
        (LLMError(), ErrorCodeEnum.E_LLM001),
        (MemoryError(), ErrorCodeEnum.E_MEM001),
        (NetworkError(), ErrorCodeEnum.E_NET002),
        (ConfigError(), ErrorCodeEnum.E_CFG001),
        (DatabaseError(), ErrorCodeEnum.E_DB001),
        (RateLimitError(), ErrorCodeEnum.E_RATE001),
        (SystemError(), ErrorCodeEnum.E_SYS003),
    ]
    for exc, expected in cases:
        assert exc.error_code is expected, f"{type(exc).__name__} 默认错误码错误"


# ============================================================
# 5. HTTP 状态码映射
# ============================================================
def test_http_status_mapping():
    """HTTP 状态码与错误码正确对应"""
    assert ErrorCodeEnum.E_AUTH001.http_status == 401
    assert ErrorCodeEnum.E_AUTH003.http_status == 403
    assert ErrorCodeEnum.E_TOOL001.http_status == 404
    assert ErrorCodeEnum.E_RATE001.http_status == 429
    assert ErrorCodeEnum.E_LLM002.http_status == 413
    assert ErrorCodeEnum.E_SYS999.http_status == 500
    assert ErrorCodeEnum.E_SYS001.http_status == 503


# ============================================================
# 6. retryable 标志
# ============================================================
def test_retryable_flag():
    """可重试错误码正确标识"""
    # 可重试
    for member in (ErrorCodeEnum.E_TOOL003, ErrorCodeEnum.E_LLM004,
                   ErrorCodeEnum.E_NET001, ErrorCodeEnum.E_DB001,
                   ErrorCodeEnum.E_RATE001, ErrorCodeEnum.E_SYS003):
        assert member.retryable is True, f"{member.name} 应可重试"

    # 不可重试
    for member in (ErrorCodeEnum.E_AUTH001, ErrorCodeEnum.E_AUTH003,
                   ErrorCodeEnum.E_TOOL001, ErrorCodeEnum.E_LLM002,
                   ErrorCodeEnum.E_LLM003, ErrorCodeEnum.E_SYS999,
                   ErrorCodeEnum.E_CFG001):
        assert member.retryable is False, f"{member.name} 应不可重试"


# ============================================================
# 7. FastAPI 异常处理器
# ============================================================
def test_fastapi_handler():
    """FastAPI 异常处理器返回正确 JSON"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web.error_handler import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise/auth")
    async def _raise_auth():
        raise AuthError("未授权访问", details={"reason": "no_token"})

    @app.get("/raise/llm")
    async def _raise_llm():
        raise LLMError("context overflow", error_code=ErrorCodeEnum.E_LLM002)

    @app.get("/raise/generic")
    async def _raise_generic():
        raise ValueError("boom")

    client = TestClient(app, raise_server_exceptions=False)

    # AppException -> 结构化错误响应
    r = client.get("/raise/auth")
    assert r.status_code == 401
    body = r.json()
    assert body["error_code"] == "E_AUTH001"
    assert body["message"] == "未授权访问"
    assert body["retryable"] is False
    assert body["details"] == {"reason": "no_token"}

    # 子类带自定义 error_code -> 使用该 error_code 的 http_status
    r2 = client.get("/raise/llm")
    assert r2.status_code == 413
    b2 = r2.json()
    assert b2["error_code"] == "E_LLM002"
    assert b2["retryable"] is False

    # 未捕获异常 -> E_SYS999 (500)
    r3 = client.get("/raise/generic")
    assert r3.status_code == 500
    b3 = r3.json()
    assert b3["error_code"] == "E_SYS999"
    assert b3["retryable"] is False
    assert b3["details"]["exception_type"] == "ValueError"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
