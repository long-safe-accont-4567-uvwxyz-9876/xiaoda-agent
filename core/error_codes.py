"""全局错误码体系 — 6位编码 = 模块(2位) + 严重度(1位) + 序号(3位)

严重度: 0=info, 1=warn, 2=error, 3=critical
模块: 01=LLM, 02=Memory, 03=Tool, 04=Security, 05=Network, 06=Web, 07=Agent, 08=DB
"""
from dataclasses import dataclass
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
        return {
            "error_code": self.code,
            "message": self.message,
            "module": self.module,
            "recoverable": self.recoverable,
            "context": self.context or {},
        }

    def log(self):
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


def make_error(code: str, message: str, **kwargs) -> StructuredError:
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
