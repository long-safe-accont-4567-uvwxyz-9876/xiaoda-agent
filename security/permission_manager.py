"""权限管理器 (精简版) — 3 级权限模式 + 工作目录授权

权限模式: DEFAULT / DEV / BYPASS (STRICT/GOAT 保留为别名兼容)
"""
from __future__ import annotations

import os
import re
import threading
from collections import deque
from dataclasses import dataclass, asdict
from enum import Enum
from loguru import logger


class PermissionMode(Enum):
    """权限模式 (3 级 + 兼容别名)"""
    DEFAULT = "default"    # 正常模式
    DEV = "dev"            # 开发模式, 宽松
    BYPASS = "bypass"      # 跳过安全检查 (测试用)
    STRICT = "default"     # 兼容别名 → 等同 DEFAULT
    GOAT = "bypass"        # 兼容别名 → 等同 BYPASS


# 致命操作防傻 (精简, 保留最关键的)
_DANGEROUS_PATTERNS = [
    re.compile(r'rm\s+(-[a-zA-Z]*\s+)*(--recursive\s+)?(/|/\*|\.\s+)', re.IGNORECASE),
    re.compile(r'rm\s+(-[a-zA-Z]*f[a-zA-Z]*r|rfa?|rf)\s+(/|/\*)', re.IGNORECASE),
    re.compile(r'mkfs\.', re.IGNORECASE),
    re.compile(r'dd\s+if=.*of=/dev/', re.IGNORECASE),
    re.compile(r':\(\)\{.*\|.*&\}', re.IGNORECASE),
    re.compile(r'format\s+[a-zA-Z]:', re.IGNORECASE),
    re.compile(r'>\s*/dev/sd[a-z]', re.IGNORECASE),
]

_HIGH_RISK_KEYWORDS = [
    "privilege_escalation", "code_injection", "remote_code_execution",
    "data_exfiltration", "credential_theft", "backdoor",
]


@dataclass
class AuditEntry:
    """工作目录操作审计条目"""
    timestamp: str
    action: str
    target: str
    cwd: str
    allowed: bool
    reason: str = ""


class PermissionManager:
    """权限管理器 — 全局单例"""

    def __init__(self) -> None:
        self._mode = self._init_mode_from_env()
        self._lock = threading.Lock()
        self._cwd: str = ""
        self._cwd_authorized: bool = False
        self._cmd_whitelist: set[str] = set()
        self._audit_buffer: deque = deque(maxlen=200)
        self._rules: dict[str, callable] = {}

    @staticmethod
    def _init_mode_from_env() -> PermissionMode:
        perm_env = os.getenv("AGENT_PERMISSION_MODE", "").strip().lower()
        mode_map = {m.value: m for m in PermissionMode if m.value in ("default", "dev", "bypass")}
        if perm_env in mode_map:
            return mode_map[perm_env]
        env_val = os.getenv("AGENT_DEV_MODE", "").strip().lower()
        if env_val in ("1", "true", "yes"):
            return PermissionMode.DEV
        return PermissionMode.DEFAULT

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def set_mode(self, mode: PermissionMode | str) -> None:
        if isinstance(mode, str):
            mode_map = {m.value: m for m in PermissionMode}
            mode = mode_map.get(mode, PermissionMode.DEFAULT)
        with self._lock:
            old = self._mode
            self._mode = mode
            logger.info("permission_manager.mode_changed", old=old.value, new=mode.value)

    def is_dev_mode(self) -> bool:
        return self._mode == PermissionMode.DEV

    def is_bypass_mode(self) -> bool:
        return self._mode in (PermissionMode.BYPASS, PermissionMode.GOAT)

    def is_goat_mode(self) -> bool:
        return self._mode == PermissionMode.GOAT

    def is_strict_mode(self) -> bool:
        return self._mode == PermissionMode.STRICT

    def check_dangerous_command(self, command: str) -> tuple[bool, str]:
        """检查命令是否包含致命操作"""
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                reason = f"防傻拦截：致命操作 [{pattern.pattern}]"
                logger.critical("permission_manager.dangerous_blocked",
                                pattern=pattern.pattern, command=command[:200])
                return True, reason
        return False, ""

    def check_tool_permission(self, tool_name: str, tool_input: dict | None = None) -> tuple[bool, str]:
        """检查工具是否被允许执行"""
        if self._mode in (PermissionMode.BYPASS, PermissionMode.GOAT):
            if tool_name == "shell_command" and tool_input:
                cmd = tool_input.get("command", "")
                if cmd:
                    is_dangerous, reason = self.check_dangerous_command(cmd)
                    if is_dangerous:
                        return False, reason
            return True, ""
        return True, ""

    def check_permission(self, tool_name: str, tool_input: dict | None = None) -> tuple[bool, str]:
        """check_permission 别名 (兼容接口)"""
        return self.check_tool_permission(tool_name, tool_input)

    def register_rule(self, name: str, rule_fn: callable) -> None:
        """注册自定义权限规则 (简化版)"""
        with self._lock:
            self._rules[name] = rule_fn

    def decide_security_action(self, threat_type: str, confidence: float) -> str:
        """根据权限模式决定安全动作"""
        if self._mode in (PermissionMode.BYPASS, PermissionMode.GOAT):
            if any(kw in threat_type.lower() for kw in _HIGH_RISK_KEYWORDS):
                return "warn"
            return "allow"

        if confidence >= 0.8:
            base_action = "block"
        elif confidence >= 0.6:
            base_action = "warn"
        else:
            return "allow"

        if self._mode == PermissionMode.DEV:
            if base_action == "block":
                return "warn"

        return base_action

    # ── 工作目录授权 API ──────────────────────────────────

    def set_cwd(self, path: str) -> None:
        with self._lock:
            self._cwd = os.path.realpath(path)
            self._cwd_authorized = True

    def clear_cwd(self) -> None:
        with self._lock:
            self._cwd = ""
            self._cwd_authorized = False

    def is_cwd_authorized(self) -> bool:
        return self._cwd_authorized

    @property
    def cwd(self) -> str:
        return self._cwd

    def _norm(self, path: str) -> str:
        p = os.path.realpath(path)
        if os.name == "nt":
            p = os.path.normcase(p)
        return p

    def is_path_allowed(self, file_path: str) -> tuple[bool, str]:
        if not self._cwd_authorized or not self._cwd:
            return False, "未授权工作目录，请先在聊天框上方选择并授权工作目录"
        if not file_path:
            return False, "路径为空"
        norm_cwd = self._norm(self._cwd)
        norm_target = self._norm(file_path)
        if norm_target == norm_cwd or norm_target.startswith(norm_cwd + os.sep):
            return True, ""
        return False, f"路径超出工作目录：{file_path}（cwd={self._cwd}）"

    _CMD_SEPARATORS = ("&&", "||", ";", "|")

    def _split_compound_command(self, command: str) -> list[str]:
        parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _extract_cmd_name(sub_cmd: str) -> str:
        tokens = sub_cmd.split()
        return tokens[0] if tokens else ""

    def is_command_allowed(self, command: str) -> tuple[bool, str, bool]:
        if not command.strip():
            return False, "命令为空", False
        sub_cmds = self._split_compound_command(command)
        needs_conf = False
        for sub in sub_cmds:
            for pattern in _DANGEROUS_PATTERNS:
                if pattern.search(sub):
                    return False, f"危险命令被拦截：{pattern.pattern}", False
            cmd_name = self._extract_cmd_name(sub)
            if cmd_name and cmd_name not in self._cmd_whitelist:
                needs_conf = True
        if needs_conf:
            return False, "命令不在白名单，需用户确认", True
        return True, "", False

    def add_to_whitelist(self, command: str) -> None:
        cmd_name = self._extract_cmd_name(command)
        if cmd_name:
            with self._lock:
                self._cmd_whitelist.add(cmd_name)

    def remove_from_whitelist(self, command: str) -> None:
        cmd_name = command.strip()
        if " " in cmd_name:
            cmd_name = self._extract_cmd_name(cmd_name)
        with self._lock:
            self._cmd_whitelist.discard(cmd_name)

    def get_whitelist(self) -> list[str]:
        with self._lock:
            return sorted(self._cmd_whitelist)

    def set_whitelist(self, items: list[str]) -> None:
        with self._lock:
            self._cmd_whitelist = set(items)

    def add_audit_entry(self, entry: AuditEntry) -> None:
        with self._lock:
            self._audit_buffer.append(asdict(entry))

    def clear_audit_log(self) -> None:
        with self._lock:
            self._audit_buffer.clear()

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        with self._lock:
            entries = list(self._audit_buffer)
        return entries[-limit:]


# ── 全局单例 ──────────────────────────────────────────────

_default_manager: PermissionManager | None = None
_manager_lock = threading.Lock()


def get_permission_manager() -> PermissionManager:
    global _default_manager
    if _default_manager is None:
        with _manager_lock:
            if _default_manager is None:
                _default_manager = PermissionManager()
    return _default_manager
