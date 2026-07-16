"""全局错误码体系 — 6位编码 = 模块(2位) + 严重度(1位) + 序号(3位)

严重度: 0=info, 1=warn, 2=error, 3=critical
模块: 01=LLM, 02=Memory, 03=Tool, 04=Security, 05=Network, 06=Web, 07=Agent, 08=DB

Q1 扩展: 新增 ErrorCodeEnum (E_AUTH001 风格)，每个错误码携带 code/http_status/message/retryable，
并提供 from_exception() 根据异常类型推断错误码。旧的 ErrorCode 类与 make_error 保持兼容。
"""
from typing import Any
from dataclasses import dataclass
from enum import Enum
from loguru import logger


class ErrorCode:
    """全局错误码定义"""

    # LLM 模块 (01)
    LLM_TIMEOUT        = "01_2_001"
    LLM_RATE_LIMIT     = "01_2_002"
    LLM_CONTENT_FILTER = "01_3_001"
    LLM_INVALID_RESPONSE = "01_2_003"
    LLM_PROVIDER_DOWN  = "01_3_002"

    # 记忆模块 (02)
    MEMORY_FULL       = "02_1_001"
    MEMORY_CORRUPT    = "02_3_001"
    MEMORY_NOT_FOUND  = "02_1_002"
    MEMORY_WRITE_FAIL = "02_2_001"

    # 工具模块 (03)
    TOOL_NOT_FOUND    = "03_2_001"
    TOOL_EXEC_ERROR   = "03_2_002"
    TOOL_TIMEOUT      = "03_2_003"
    TOOL_INVALID_ARGS = "03_1_001"

    # 安全模块 (04)
    SECURITY_SSRF     = "04_3_001"
    SECURITY_CANARY   = "04_3_002"
    SECURITY_INJECTION = "04_3_003"
    SECURITY_PERMISSION = "04_2_001"

    # 网络模块 (05)
    NETWORK_DNS       = "05_2_001"
    NETWORK_TIMEOUT   = "05_2_002"
    NETWORK_CONN_REFUSED = "05_3_001"

    # Web 模块 (06)
    WEB_RATE_LIMIT    = "06_1_001"
    WEB_WS_DISCONNECT = "06_1_002"
    WEB_AUTH_FAIL     = "06_2_001"

    # Agent 模块 (07)
    AGENT_OVERLOAD    = "07_2_001"
    AGENT_STUCK       = "07_2_002"
    AGENT_CONFIG_ERROR = "07_2_003"

    # DB 模块 (08)
    DB_CONN_FAIL     = "08_3_001"
    DB_QUERY_TIMEOUT = "08_2_001"
    DB_MIGRATION_FAIL = "08_3_002"


@dataclass
class StructuredError:
    """结构化错误"""
    code: str
    message: str
    module: str = ""
    recoverable: bool = True
    context: dict | None = None

    def to_dict(self) -> dict:
        """将结构化错误序列化为字典."""
        return {
            "error_code": self.code,
            "message": self.message,
            "module": self.module,
            "recoverable": self.recoverable,
            "context": self.context or {},
        }

    def log(self) -> None:
        """按严重度分级日志"""
        severity = int(self.code.split("_")[1]) if "_" in self.code else 2
        if severity >= 3:
            logger.critical(f"[{self.code}] {self.message}", context=self.context)
        elif severity >= 2:
            logger.error(f"[{self.code}] {self.message}", context=self.context)
        elif severity >= 1:
            logger.warning(f"[{self.code}] {self.message}", context=self.context)
        else:
            logger.info(f"[{self.code}] {self.message}")


def make_error(code: str, message: str, **kwargs: Any) -> StructuredError:
    """快速创建结构化错误"""
    module_map = {
        "01": "LLM", "02": "Memory", "03": "Tool", "04": "Security",
        "05": "Network", "06": "Web", "07": "Agent", "08": "DB",
    }
    module = module_map.get(code[:2], "Unknown")
    return StructuredError(
        code=code,
        message=message,
        module=module,
        recoverable=not code.endswith("_3_001") and not code.endswith("_3_002"),
        context=kwargs if kwargs else None,
    )


# ============================================================
# Q1 结构化错误码体系 (E_AUTH001 风格)
# 格式: E_{类别}{3位编号}，如 E_AUTH001、E_TOOL042
# 每个错误码携带: code / http_status / message / retryable
# ============================================================


class ErrorCodeEnum(Enum):
    """结构化错误码枚举

    每个成员的 value 为 (code, http_status, message, retryable) 四元组。
    类别编号区间:
      AUTH(1xx) TOOL(2xx) LLM(3xx) MEM(4xx) NET(5xx)
      CFG(6xx) DB(7xx)   RATE(8xx) SYS(9xx)
    """

    # ---- AUTH 认证/授权 (1xx) ----
    E_AUTH001 = ("E_AUTH001", 401, "未授权", False)
    E_AUTH002 = ("E_AUTH002", 401, "Token 已过期", True)
    E_AUTH003 = ("E_AUTH003", 403, "权限不足", False)
    E_AUTH004 = ("E_AUTH004", 401, "认证失败", False)

    # ---- TOOL 工具调用 (2xx) ----
    E_TOOL001 = ("E_TOOL001", 404, "工具不存在", False)
    E_TOOL002 = ("E_TOOL002", 400, "工具参数错误", False)
    E_TOOL003 = ("E_TOOL003", 504, "工具执行超时", True)
    E_TOOL004 = ("E_TOOL004", 500, "工具执行失败", True)
    E_TOOL005 = ("E_TOOL005", 403, "工具未启用", False)
    E_TOOL006 = ("E_TOOL006", 403, "Path forbidden by sub-agent whitelist", False)

    # ---- LLM 调用 (3xx) ----
    E_LLM001 = ("E_LLM001", 502, "LLM API 错误", True)
    E_LLM002 = ("E_LLM002", 413, "上下文超限", False)
    E_LLM003 = ("E_LLM003", 400, "内容被安全过滤", False)
    E_LLM004 = ("E_LLM004", 504, "LLM 调用超时", True)
    E_LLM005 = ("E_LLM005", 404, "模型不存在", False)
    E_LLM006 = ("E_LLM006", 401, "LLM 认证失败", False)
    E_LLM007 = ("E_LLM007", 429, "LLM 速率限制", True)

    # ---- MEM 记忆系统 (4xx) ----
    E_MEM001 = ("E_MEM001", 500, "记忆检索失败", True)
    E_MEM002 = ("E_MEM002", 500, "记忆写入失败", True)
    E_MEM003 = ("E_MEM003", 404, "记忆不存在", False)
    E_MEM004 = ("E_MEM004", 507, "记忆存储已满", False)

    # ---- NET 网络 (5xx) ----
    E_NET001 = ("E_NET001", 504, "网络超时", True)
    E_NET002 = ("E_NET002", 502, "网络连接失败", True)
    E_NET003 = ("E_NET003", 502, "DNS 解析失败", True)
    E_NET004 = ("E_NET004", 403, "SSRF 拦截", False)

    # ---- CFG 配置 (6xx) ----
    E_CFG001 = ("E_CFG001", 500, "缺少必要配置", False)
    E_CFG002 = ("E_CFG002", 500, "配置格式错误", False)
    E_CFG003 = ("E_CFG003", 500, "配置加载失败", False)

    # ---- DB 数据库 (7xx) ----
    E_DB001 = ("E_DB001", 500, "数据库连接失败", True)
    E_DB002 = ("E_DB002", 500, "数据库查询失败", True)
    E_DB003 = ("E_DB003", 500, "数据库迁移失败", False)
    E_DB004 = ("E_DB004", 500, "数据库写入失败", True)

    # ---- RATE 速率限制 (8xx) ----
    E_RATE001 = ("E_RATE001", 429, "全局速率限制", True)
    E_RATE002 = ("E_RATE002", 429, "用户速率限制", True)
    E_RATE003 = ("E_RATE003", 429, "端点速率限制", True)

    # ---- SYS 系统 (9xx) ----
    E_SYS001 = ("E_SYS001", 503, "内存不足", True)
    E_SYS002 = ("E_SYS002", 503, "磁盘已满", False)
    E_SYS003 = ("E_SYS003", 500, "内部错误", True)
    E_SYS999 = ("E_SYS999", 500, "未知错误", False)

    def __init__(self, code: str, http_status: int, message: str, retryable: bool) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict:
        """序列化为字典"""
        return {
            "error_code": self.code,
            "http_status": self.http_status,
            "message": self.message,
            "retryable": self.retryable,
        }


def from_exception(exc: Exception) -> "ErrorCodeEnum":
    """根据异常类型/消息推断错误码

    规则:
      1. 若异常自带 error_code (AppException)，直接返回其错误码
      2. 按异常类型与消息关键字匹配
      3. 兜底返回 E_SYS999
    """
    # 1. 已是结构化异常（duck typing，避免与 app_exception 循环导入）
    ec = getattr(exc, "error_code", None)
    if isinstance(ec, ErrorCodeEnum):
        return ec

    import asyncio as _asyncio

    exc_name = type(exc).__name__.lower()
    exc_msg = str(exc).lower()

    # 2. 优先匹配 openai 库异常类型（若已安装）
    reason = _from_openai_exception(exc, exc_msg)
    if reason is not None:
        return reason

    # 3. 内置异常类型匹配
    reason = _from_builtin_exception(exc, _asyncio)
    if reason is not None:
        return reason

    # 4. 消息关键字兜底匹配
    reason = _from_message_keywords(exc_name, exc_msg)
    if reason is not None:
        return reason

    return ErrorCodeEnum.E_SYS999


def _from_openai_exception(exc: Exception, exc_msg: str) -> "ErrorCodeEnum | None":
    """优先匹配 openai 库异常类型（若已安装）"""
    try:
        import openai as _openai

        if isinstance(exc, _openai.AuthenticationError):
            return ErrorCodeEnum.E_LLM006
        if isinstance(exc, _openai.PermissionDeniedError):
            return ErrorCodeEnum.E_AUTH003
        if isinstance(exc, _openai.RateLimitError):
            return ErrorCodeEnum.E_LLM007
        if isinstance(exc, _openai.APITimeoutError):
            return ErrorCodeEnum.E_LLM004
        if isinstance(exc, _openai.APIConnectionError):
            return ErrorCodeEnum.E_NET002
        if isinstance(exc, _openai.NotFoundError):
            if "model" in exc_msg:
                return ErrorCodeEnum.E_LLM005
            return ErrorCodeEnum.E_SYS999
        if isinstance(exc, _openai.BadRequestError):
            if "context" in exc_msg or "token" in exc_msg or "maximum" in exc_msg:
                return ErrorCodeEnum.E_LLM002
            if "content" in exc_msg or "safety" in exc_msg:
                return ErrorCodeEnum.E_LLM003
            return ErrorCodeEnum.E_LLM001
        if isinstance(exc, _openai.APIStatusError):
            status = getattr(exc, "status_code", None)
            if status == 429:
                return ErrorCodeEnum.E_LLM007
            if status in (401, 403):
                return ErrorCodeEnum.E_LLM006
            if status and 500 <= status < 600:
                return ErrorCodeEnum.E_LLM001
    except Exception as e:
        logger.debug("error_codes.classify_exception_failed", error=str(e))
    return None


def _from_builtin_exception(exc: Exception, _asyncio: Any) -> "ErrorCodeEnum | None":
    """内置异常类型匹配"""
    if isinstance(exc, (TimeoutError, _asyncio.TimeoutError)):
        return ErrorCodeEnum.E_NET001
    if isinstance(exc, ConnectionError):
        return ErrorCodeEnum.E_NET002
    if isinstance(exc, PermissionError):
        return ErrorCodeEnum.E_AUTH003
    if isinstance(exc, FileNotFoundError):
        return ErrorCodeEnum.E_CFG001
    # 注意：此处 MemoryError 指内置内存耗出异常（模块未导入 app_exception.MemoryError）
    if isinstance(exc, MemoryError):
        return ErrorCodeEnum.E_SYS001
    if isinstance(exc, RecursionError):
        return ErrorCodeEnum.E_SYS003
    return None


def _from_message_keywords(exc_name: str, exc_msg: str) -> "ErrorCodeEnum | None":
    """消息关键字兜底匹配"""
    if "ssrf" in exc_msg:
        return ErrorCodeEnum.E_NET004
    if "timeout" in exc_name or "timeout" in exc_msg:
        return ErrorCodeEnum.E_NET001
    if "rate" in exc_msg or "429" in exc_msg or "too many requests" in exc_msg:
        return ErrorCodeEnum.E_LLM007
    if "unauthorized" in exc_msg or "401" in exc_msg or "forbidden" in exc_msg or "403" in exc_msg:
        return ErrorCodeEnum.E_AUTH001
    if "context" in exc_msg and ("exceed" in exc_msg or "overflow" in exc_msg):
        return ErrorCodeEnum.E_LLM002
    if "content_policy" in exc_msg or "content policy" in exc_msg:
        return ErrorCodeEnum.E_LLM003
    if "connection" in exc_name or "connection" in exc_msg:
        return ErrorCodeEnum.E_NET002
    return None
