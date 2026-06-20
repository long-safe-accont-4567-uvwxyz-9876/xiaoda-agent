"""执行纪律层 — 危险分级 L0-L4 + 证据门禁 + 改完验证"""
import re
import logging
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class RiskLevel(IntEnum):
    """危险分级"""
    SAFE = 0       # L0 读取操作
    LOW = 1        # L1 创建新文件
    MEDIUM = 2     # L2 覆写/修改
    HIGH = 3       # L3 删除/重启
    FORBIDDEN = 4  # L4 危险操作


class RiskClassifier:
    """危险分级器 — 根据工具名和参数分类风险等级"""

    # L4 禁止操作模式（静态正则，不拼接用户输入）
    FORBIDDEN_PATTERNS = [
        re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
        re.compile(r"DROP\s+TABLE", re.IGNORECASE),
        re.compile(r"FORMAT\s+[A-Z]:", re.IGNORECASE),
        re.compile(r"mkfs\.", re.IGNORECASE),
        re.compile(r":\(\)\{.*\};:", re.IGNORECASE),  # fork bomb
        re.compile(r"dd\s+if=.*of=/dev/", re.IGNORECASE),
    ]

    # L3 高风险操作模式
    HIGH_RISK_PATTERNS = [
        re.compile(r"rm\s+-r", re.IGNORECASE),
        re.compile(r"restart|reboot|shutdown", re.IGNORECASE),
        re.compile(r"DELETE\s+FROM", re.IGNORECASE),
        re.compile(r"TRUNCATE\s+TABLE", re.IGNORECASE),
        re.compile(r"systemctl\s+(stop|disable)", re.IGNORECASE),
    ]

    # L2 中风险工具名
    MEDIUM_RISK_TOOLS = {"write_file", "edit_file", "shell_command", "python_executor", "create_file"}

    # L1 低风险工具名
    LOW_RISK_TOOLS = {"create_file", "mkdir", "touch"}

    # L0 安全工具名
    SAFE_TOOLS = {"read_file", "list_dir", "search", "test", "ping", "cat", "ls", "grep"}

    def classify(self, tool_name: str, params: dict) -> RiskLevel:
        """根据工具名和参数分类风险等级"""
        # 检查参数中的命令/内容是否匹配禁止模式
        for key in ("command", "cmd", "code", "content", "query", "sql"):
            val = params.get(key, "")
            if isinstance(val, str):
                for pattern in self.FORBIDDEN_PATTERNS:
                    if pattern.search(val):
                        return RiskLevel.FORBIDDEN
                for pattern in self.HIGH_RISK_PATTERNS:
                    if pattern.search(val):
                        return RiskLevel.HIGH

        # 按工具名分类
        if tool_name in self.SAFE_TOOLS:
            return RiskLevel.SAFE
        if tool_name in self.LOW_RISK_TOOLS:
            return RiskLevel.LOW
        if tool_name in self.MEDIUM_RISK_TOOLS:
            return RiskLevel.MEDIUM
        # 默认中风险
        return RiskLevel.MEDIUM

    def pre_check(self, tool_name: str, params: dict, has_read_target: bool = False) -> dict:
        """执行前检查"""
        risk = self.classify(tool_name, params)

        # L4: 直接拒绝
        if risk >= RiskLevel.FORBIDDEN:
            logger.warning("risk_classifier.forbidden_operation", extra={"tool": tool_name})
            return {"allow": False, "reason": "危险操作，已拒绝", "risk": risk, "need_confirm": False}

        # L3: 需要用户确认
        if risk >= RiskLevel.HIGH:
            return {"allow": False, "reason": "高风险操作，需要用户确认", "risk": risk, "need_confirm": True}

        # L2: 证据门禁（先读再写）
        if risk >= RiskLevel.MEDIUM:
            if not has_read_target:
                return {"allow": False, "reason": "证据门禁：请先读取目标文件再修改", "risk": risk, "need_confirm": False}

        return {"allow": True, "risk": risk, "need_confirm": False}


class EvidenceGate:
    """证据门禁 — 追踪已读取的文件路径"""

    def __init__(self):
        self._read_targets: set[str] = set()

    def mark_read(self, file_path: str):
        """标记文件已读取"""
        if file_path:
            self._read_targets.add(str(file_path))

    def has_read(self, file_path: str) -> bool:
        """检查是否已读取目标文件"""
        return str(file_path) in self._read_targets

    def clear(self):
        """清空追踪记录"""
        self._read_targets.clear()


class PostValidator:
    """改完验证 — L2+ 操作执行后自动验证"""

    @staticmethod
    def validate(tool_name: str, result: dict, risk: RiskLevel) -> dict:
        """验证执行结果"""
        if risk < RiskLevel.MEDIUM:
            return {"valid": True}

        output = result.get("output", "") or result.get("result", "")
        file_path = result.get("file_path", "")

        # JSON 文件验证
        if file_path.endswith(".json"):
            import json
            try:
                json.loads(output) if isinstance(output, str) else None
            except json.JSONDecodeError as e:
                return {"valid": False, "reason": f"JSON 解析失败: {e}"}

        # Python 文件语法检查
        elif file_path.endswith(".py"):
            try:
                compile(output, file_path, "exec") if isinstance(output, str) else None
            except SyntaxError as e:
                return {"valid": False, "reason": f"语法错误: {e}"}

        return {"valid": True}
