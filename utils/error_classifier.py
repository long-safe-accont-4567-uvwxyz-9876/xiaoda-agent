"""
结构化错误分类器 - 将 API 异常分类为具体错误类型，每种类型对应恢复策略
借鉴 Hermes Agent 的错误分类机制，替代 ModelRouter 中简单的重试/降级逻辑
"""

from enum import Enum
from dataclasses import dataclass
from loguru import logger

import openai


class FailoverReason(Enum):
    """API 调用失败原因分类"""
    AUTH_ERROR = "auth_error"                    # 认证失败（401/403）
    RATE_LIMIT = "rate_limit"                    # 限速（429）
    TIMEOUT = "timeout"                          # 超时
    CONTEXT_OVERFLOW = "context_overflow"        # 上下文溢出
    CONTENT_POLICY = "content_policy"            # 内容策略阻止
    CONNECTION_ERROR = "connection_error"        # 连接错误
    MODEL_NOT_FOUND = "model_not_found"          # 模型不存在
    SERVER_ERROR = "server_error"                # 服务器错误（5xx）
    PAYLOAD_TOO_LARGE = "payload_too_large"      # 请求体过大
    FORMAT_ERROR = "format_error"                # 格式错误
    UNKNOWN = "unknown"                          # 未知错误


class RecoveryAction(Enum):
    """恢复策略"""
    RETRY = "retry"                              # 重试
    ROTATE_CREDENTIAL = "rotate_credential"      # 轮换凭证
    BACKOFF_RETRY = "backoff_retry"              # 退避重试
    COMPRESS_CONTEXT = "compress_context"        # 压缩上下文
    FALLBACK_PROVIDER = "fallback_provider"      # 故障转移到备用提供商
    ABORT = "abort"                              # 中止


@dataclass
class ClassifiedError:
    reason: FailoverReason
    action: RecoveryAction
    original_error: Exception
    message: str
    is_retryable: bool
    backoff_seconds: float = 0.0


# 错误类型到恢复策略的映射
RECOVERY_MAP: dict[FailoverReason, RecoveryAction] = {
    FailoverReason.AUTH_ERROR: RecoveryAction.ROTATE_CREDENTIAL,
    FailoverReason.RATE_LIMIT: RecoveryAction.BACKOFF_RETRY,
    FailoverReason.TIMEOUT: RecoveryAction.RETRY,
    FailoverReason.CONTEXT_OVERFLOW: RecoveryAction.COMPRESS_CONTEXT,
    FailoverReason.CONTENT_POLICY: RecoveryAction.ABORT,
    FailoverReason.CONNECTION_ERROR: RecoveryAction.RETRY,
    FailoverReason.MODEL_NOT_FOUND: RecoveryAction.FALLBACK_PROVIDER,
    FailoverReason.SERVER_ERROR: RecoveryAction.BACKOFF_RETRY,
    FailoverReason.PAYLOAD_TOO_LARGE: RecoveryAction.COMPRESS_CONTEXT,
    FailoverReason.FORMAT_ERROR: RecoveryAction.RETRY,
    FailoverReason.UNKNOWN: RecoveryAction.RETRY,
}

# 可重试的错误类型
RETRYABLE_REASONS = {
    FailoverReason.RATE_LIMIT,
    FailoverReason.TIMEOUT,
    FailoverReason.CONNECTION_ERROR,
    FailoverReason.SERVER_ERROR,
    FailoverReason.FORMAT_ERROR,
    FailoverReason.UNKNOWN,
}

# 默认退避时间（秒）
DEFAULT_BACKOFF = {
    FailoverReason.RATE_LIMIT: 5.0,
    FailoverReason.SERVER_ERROR: 3.0,
}


class ErrorClassifier:
    """结构化错误分类器"""

    def classify(self, exc: Exception) -> ClassifiedError:
        """分类异常，返回 ClassifiedError"""
        reason = self._identify_reason(exc)
        action = RECOVERY_MAP[reason]
        is_retryable = reason in RETRYABLE_REASONS
        backoff = self._calc_backoff(exc, reason)

        classified = ClassifiedError(
            reason=reason,
            action=action,
            original_error=exc,
            message=str(exc),
            is_retryable=is_retryable,
            backoff_seconds=backoff,
        )

        logger.warning("error_classifier.classified",
                       reason=reason.value,
                       action=action.value,
                       retryable=is_retryable,
                       backoff=f"{backoff:.1f}s",
                       error_type=type(exc).__name__)

        return classified

    def _identify_reason(self, exc: Exception) -> FailoverReason:
        """从异常类型、消息、状态码中识别失败原因"""
        # 优先匹配 openai 库的异常类型
        reason = self._match_openai_exception(exc)
        if reason is not None:
            return reason

        # 从 HTTP 状态码属性中识别
        reason = self._match_http_status(exc)
        if reason is not None:
            return reason

        # 递归检查异常链 (__cause__ / __context__)
        reason = self._inspect_exception_chain(exc)
        if reason is not None:
            return reason

        # 从异常消息中提取信息
        return self._match_by_message(exc)

    def _match_openai_exception(self, exc: Exception) -> FailoverReason | None:
        """识别 openai 库的异常类型"""
        try:
            if isinstance(exc, openai.AuthenticationError):
                return FailoverReason.AUTH_ERROR
            if isinstance(exc, openai.RateLimitError):
                return FailoverReason.RATE_LIMIT
            if isinstance(exc, openai.PermissionDeniedError):
                return FailoverReason.AUTH_ERROR
            if isinstance(exc, openai.NotFoundError):
                msg = str(exc).lower()
                if "model" in msg:
                    return FailoverReason.MODEL_NOT_FOUND
                return FailoverReason.UNKNOWN
            if isinstance(exc, openai.BadRequestError):
                msg = str(exc).lower()
                if "context" in msg or "token" in msg or "maximum" in msg:
                    return FailoverReason.CONTEXT_OVERFLOW
                if "content_policy" in msg or "content" in msg:
                    return FailoverReason.CONTENT_POLICY
                if "payload" in msg or "too large" in msg:
                    return FailoverReason.PAYLOAD_TOO_LARGE
                return FailoverReason.FORMAT_ERROR
            if isinstance(exc, openai.APIStatusError):
                status_code = getattr(exc, "status_code", None)
                if status_code == 401 or status_code == 403:
                    return FailoverReason.AUTH_ERROR
                if status_code == 429:
                    return FailoverReason.RATE_LIMIT
                if status_code and 500 <= status_code < 600:
                    return FailoverReason.SERVER_ERROR
            if isinstance(exc, openai.APIConnectionError):
                return FailoverReason.CONNECTION_ERROR
            if isinstance(exc, openai.APITimeoutError):
                return FailoverReason.TIMEOUT
        except (AttributeError, TypeError):
            pass

        return None

    def _match_by_message(self, exc: Exception) -> FailoverReason:
        """从异常消息中识别失败原因"""
        import asyncio

        exc_name = type(exc).__name__.lower()
        exc_msg = str(exc).lower()
        # 共享上下文，便于辅助方法复用
        ctx = {"exc_name": exc_name, "exc_msg": exc_msg, "asyncio": asyncio, "exc": exc}

        for matcher in (
            self._match_timeout_by_message,
            self._match_auth_error_by_message,
            self._match_rate_limit_by_message,
            self._match_context_overflow_by_message,
            self._match_content_policy_by_message,
            self._match_connection_error_by_message,
            self._match_model_not_found_by_message,
            self._match_payload_too_large_by_message,
            self._match_server_error_by_message,
            self._match_format_error_by_message,
        ):
            reason = matcher(ctx)
            if reason is not None:
                return reason

        return FailoverReason.UNKNOWN

    def _match_timeout_by_message(self, ctx: dict) -> FailoverReason | None:
        """超时"""
        asyncio = ctx["asyncio"]
        exc = ctx["exc"]
        exc_name = ctx["exc_name"]
        exc_msg = ctx["exc_msg"]
        if isinstance(exc, asyncio.TimeoutError) or "timeout" in exc_name or "timeout" in exc_msg:
            return FailoverReason.TIMEOUT
        return None

    def _match_auth_error_by_message(self, ctx: dict) -> FailoverReason | None:
        """认证失败"""
        exc_msg = ctx["exc_msg"]
        if "401" in exc_msg or "403" in exc_msg or "unauthorized" in exc_msg or "forbidden" in exc_msg:
            return FailoverReason.AUTH_ERROR
        if "invalid api key" in exc_msg or "invalid x-api-key" in exc_msg or "authentication" in exc_msg:
            return FailoverReason.AUTH_ERROR
        return None

    def _match_rate_limit_by_message(self, ctx: dict) -> FailoverReason | None:
        """限速"""
        exc_name = ctx["exc_name"]
        exc_msg = ctx["exc_msg"]
        if "429" in exc_msg or "rate" in exc_msg or "rate_limit" in exc_name or "too many requests" in exc_msg:
            return FailoverReason.RATE_LIMIT
        return None

    def _match_context_overflow_by_message(self, ctx: dict) -> FailoverReason | None:
        """上下文溢出"""
        exc_msg = ctx["exc_msg"]
        if "context" in exc_msg and ("overflow" in exc_msg or "exceed" in exc_msg or "maximum" in exc_msg):
            return FailoverReason.CONTEXT_OVERFLOW
        if "token" in exc_msg and ("limit" in exc_msg or "exceed" in exc_msg or "maximum" in exc_msg):
            return FailoverReason.CONTEXT_OVERFLOW
        if "maximum context length" in exc_msg:
            return FailoverReason.CONTEXT_OVERFLOW
        return None

    def _match_content_policy_by_message(self, ctx: dict) -> FailoverReason | None:
        """内容策略"""
        exc_msg = ctx["exc_msg"]
        if "content_policy" in exc_msg or "content policy" in exc_msg or "safety" in exc_msg:
            return FailoverReason.CONTENT_POLICY
        return None

    def _match_connection_error_by_message(self, ctx: dict) -> FailoverReason | None:
        """连接错误"""
        exc_name = ctx["exc_name"]
        exc_msg = ctx["exc_msg"]
        if "connection" in exc_name or "connection" in exc_msg or "connect" in exc_msg:
            return FailoverReason.CONNECTION_ERROR
        if "network" in exc_msg or "unreachable" in exc_msg or "dns" in exc_msg:
            return FailoverReason.CONNECTION_ERROR
        return None

    def _match_model_not_found_by_message(self, ctx: dict) -> FailoverReason | None:
        """模型不存在"""
        exc_msg = ctx["exc_msg"]
        if "model" in exc_msg and ("not found" in exc_msg or "does not exist" in exc_msg):
            return FailoverReason.MODEL_NOT_FOUND
        return None

    def _match_payload_too_large_by_message(self, ctx: dict) -> FailoverReason | None:
        """请求体过大"""
        exc_msg = ctx["exc_msg"]
        if "payload" in exc_msg and "large" in exc_msg:
            return FailoverReason.PAYLOAD_TOO_LARGE
        if "413" in exc_msg:
            return FailoverReason.PAYLOAD_TOO_LARGE
        return None

    def _match_server_error_by_message(self, ctx: dict) -> FailoverReason | None:
        """服务器错误"""
        exc_msg = ctx["exc_msg"]
        if "500" in exc_msg or "502" in exc_msg or "503" in exc_msg or "504" in exc_msg:
            return FailoverReason.SERVER_ERROR
        if "internal server error" in exc_msg or "bad gateway" in exc_msg or "service unavailable" in exc_msg:
            return FailoverReason.SERVER_ERROR
        return None

    def _match_format_error_by_message(self, ctx: dict) -> FailoverReason | None:
        """格式错误"""
        exc_msg = ctx["exc_msg"]
        if ("format" in exc_msg or "invalid" in exc_msg) and "request" in exc_msg:
            return FailoverReason.FORMAT_ERROR
        if "400" in exc_msg:
            return FailoverReason.FORMAT_ERROR
        return None

    def _match_http_status(self, exc: Exception) -> FailoverReason | None:
        """从异常的 status_code 属性中识别失败原因"""
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            # 尝试从 response 属性中获取
            response = getattr(exc, "response", None)
            if response is not None:
                status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            return None

        if status_code in (401, 403):
            return FailoverReason.AUTH_ERROR
        if status_code == 429:
            return FailoverReason.RATE_LIMIT
        if 500 <= status_code < 600:
            return FailoverReason.SERVER_ERROR

        return None

    def _inspect_exception_chain(self, exc: Exception, depth: int = 0) -> FailoverReason | None:
        """递归检查异常链 (__cause__ / __context__)，最深 5 层"""
        if depth >= 5:
            return None

        for chained in (exc.__cause__, exc.__context__):
            if chained is None or chained is exc:
                continue

            # 先尝试 openai 异常匹配
            reason = self._match_openai_exception(chained)
            if reason is not None:
                logger.debug("error_classifier.chain_hit",
                             depth=depth, chain_type="cause" if chained is exc.__cause__ else "context",
                             reason=reason.value, error_type=type(chained).__name__)
                return reason

            # 再尝试 HTTP 状态码匹配
            reason = self._match_http_status(chained)
            if reason is not None:
                logger.debug("error_classifier.chain_http_status",
                             depth=depth, reason=reason.value, error_type=type(chained).__name__)
                return reason

            # 递归深入
            reason = self._inspect_exception_chain(chained, depth + 1)
            if reason is not None:
                return reason

        return None

    def _calc_backoff(self, exc: Exception, reason: FailoverReason) -> float:
        """计算退避时间，限速错误根据 Retry-After 头计算"""
        if reason == FailoverReason.RATE_LIMIT:
            retry_after = self._extract_retry_after(exc)
            if retry_after > 0:
                return retry_after
            return DEFAULT_BACKOFF.get(reason, 5.0)

        return DEFAULT_BACKOFF.get(reason, 0.0)

    def _extract_retry_after(self, exc: Exception) -> float:
        """从异常中提取 Retry-After 头的值"""
        # 直接从 headers 属性获取
        headers = getattr(exc, "headers", None)
        if headers is not None:
            if isinstance(headers, dict):
                retry_after = headers.get("retry-after") or headers.get("Retry-After")
                if retry_after:
                    try:
                        return float(retry_after)
                    except (ValueError, TypeError):
                        return 0.0
            # httpx.Headers 对象也支持 .get()
            if hasattr(headers, 'get'):
                retry_after = headers.get("retry-after") or headers.get("Retry-After")
                if retry_after:
                    try:
                        return float(retry_after)
                    except (ValueError, TypeError):
                        return 0.0

        # 从 httpx.Response 对象中获取
        response = getattr(exc, "response", None)
        if response is not None:
            resp_headers = getattr(response, "headers", None)
            if resp_headers is not None:
                retry_after = resp_headers.get("retry-after") or resp_headers.get("Retry-After")
                if retry_after:
                    try:
                        return float(retry_after)
                    except (ValueError, TypeError):
                        return 0.0

        return 0.0
