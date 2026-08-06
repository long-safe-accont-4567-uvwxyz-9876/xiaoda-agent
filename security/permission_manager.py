"""权限管理器 — 借鉴 Claude Agent SDK 的 PermissionMode 设计

将 AGENT_DEV_MODE 二值开关升级为多级权限模式，
支持运行时动态切换，向后兼容环境变量。
"""
from __future__ import annotations

import os
import re
import threading
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from loguru import logger


class PermissionMode(Enum):
    """权限模式 — 借鉴 OpenWorker coworker/permissions.py 的五态设计。

    原有模式（向后兼容）：
    - DEFAULT: 安全威胁按置信度决定 block/warn
    - DEV: 开发模式，block 降级为 warn
    - STRICT: 严格模式，所有威胁 block
    - BYPASS: 绕过模式，跳过所有安全检查
    - GOAT: 梭哈模式，全部权限开放

    新增模式（借鉴 OpenWorker）：
    - DISCUSS: 只读对话模式，只允许只读工具，不允许写/执行
    - PLAN: 规划模式，可以规划但不能执行写操作
    - INTERACTIVE: 交互式确认模式，写/执行操作需用户审批
    - AUTO: 全自动模式，跳过审批但仍受路径范围限制
    - CUSTOM: 自定义模式，交互式 + 配置的 auto_allow 工具白名单
    """
    DEFAULT = "default"        # 默认：安全威胁按置信度决定 block/warn
    DEV = "dev"                # 开发模式：block 降级为 warn，只读查询放行
    STRICT = "strict"          # 严格模式：所有威胁 block，修改性工具需确认
    BYPASS = "bypass"          # 绕过模式：跳过所有安全检查（兼容旧代码）
    GOAT = "goat"              # 梭哈模式：全部权限开放，最大自由度
    # ── 新增：借鉴 OpenWorker 五态 ──
    DISCUSS = "discuss"        # 只读对话：只允许只读工具
    PLAN = "plan"              # 规划模式：只读 + 规划，不执行写操作
    INTERACTIVE = "interactive"  # 交互式：写/执行操作需用户审批
    AUTO = "auto"              # 全自动：跳过审批，但仍受路径范围限制
    CUSTOM = "custom"          # 自定义：交互式 + auto_allow 工具白名单


# ── 新增：借鉴 OpenWorker 的只读模式集合 ──
# DISCUSS 和 PLAN 共享只读门控，区别在于 PLAN 额外驱动 agent 走规划流程
READ_ONLY_MODES = frozenset({PermissionMode.DISCUSS, PermissionMode.PLAN})

# ── 新增：借鉴 OpenWorker 的审批跳过模式 ──
# AUTO 和 BYPASS/GOAT 模式跳过审批
AUTO_APPROVE_MODES = frozenset({
    PermissionMode.AUTO, PermissionMode.BYPASS, PermissionMode.GOAT
})


# 敏感操作工具列表（strict 模式下需要确认）
_SENSITIVE_TOOLS = {
    "shell_command", "execute_code", "python_executor",
    "write_file", "edit_file", "create_file",
    "agnes_image", "agnes_video",
}

# ── 防傻机制：即使用梭哈模式也会拦截的危险操作 ──────────────
# 匹配 shell 命令中的致命操作（不区分大小写）
_GOAT_DANGEROUS_SHELL_PATTERNS = [
    # ── Linux/macOS ──
    # 根目录删除
    r'rm\s+(-[a-zA-Z]*\s+)*(--recursive\s+)?(/|/\*|\.\s+)',
    r'rm\s+(-[a-zA-Z]*f[a-zA-Z]*r|rfa?|rf)\s+(/|/\*)',
    r'rm\s+-[a-zA-Z]*\s*/\s*$',
    # 磁盘格式化 / 覆写
    r'mkfs\.',
    r'dd\s+if=.*of=/dev/',
    r'>\s*/dev/sd[a-z]',
    # 叉子炸弹
    r':\(\)\{.*\|.*&\}',
    r'fork\s*bomb',
    # 关键系统文件破坏
    r'chmod\s+(-[a-zA-Z]*\s+)?(000|777)\s+/',
    r'chown\s+.*\s+/',
    # init / systemd 杀进程
    r'kill\s+-9\s+1\b',
    r'killall\s+(init|systemd|sshd)',
    r'pkill\s+-(9|SIGKILL)\s+(init|systemd|sshd)',
    # 网络破坏
    r'iptables\s+-F',
    r'ip\s+link\s+set\s+.*down',

    # ── Windows ──
    # 磁盘格式化
    r'format\s+[a-zA-Z]:',
    # 递归删除根目录/系统目录
    r'(del|erase)\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\?\s*$',
    r'(rd|smdir)\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\?\s*$',
    # 危险系统命令
    r'rd\s+/[sS]\s+/[qQ]\s+(C:\\|C:\\Windows)',
    r'del\s+/[fF]\s+/[sS]\s+/[qQ]\s+C:\\',
    # 关键进程强杀
    r'taskkill\s+/[fF]\s+/[iI][mM]\s+(csrss|smss|wininit|services)\s*\.exe',
    # 启动配置破坏
    r'bcdedit\s+(/delete|/set)',
    # 磁盘分区操作
    r'diskpart',
    # 关机/重启（强制无延迟）
    r'shutdown\s+(/[sSrR]|/g)\s+.*(/[tT]\s*0)',
    # 注册表破坏
    r'reg\s+(delete|import)\s+HKLM\\SYSTEM',
]

_GOAT_DANGEROUS_SHELL_RE = [
    re.compile(p, re.IGNORECASE) for p in _GOAT_DANGEROUS_SHELL_PATTERNS
]

# 高危安全威胁类型（即使用梭哈模式也记录并返回 warn）
_GOAT_WARN_THREAT_KEYWORDS = [
    "privilege_escalation", "code_injection", "remote_code_execution",
    "data_exfiltration", "credential_theft", "backdoor",
]


@dataclass
class AuditEntry:
    """工作目录操作审计条目"""
    timestamp: str       # ISO8601
    action: str          # "read" / "write" / "delete" / "exec"
    target: str          # 文件路径或命令
    cwd: str             # 当时的工作目录
    allowed: bool        # 是否放行
    reason: str = ""     # 拒绝原因（如适用）


class PermissionManager:
    """权限管理器 — 全局单例"""

    def __init__(self) -> None:
        self._mode = self._init_mode_from_env()
        self._lock = threading.Lock()
        # ── 工作目录授权（叠加层，不影响 PermissionMode） ──
        # 用户通过 webui 显式授权 Agent 在指定目录下读写文件 + 执行受限命令
        # 该授权独立于 PermissionMode，是额外的边界约束
        self._cwd: str = ""                          # 当前授权工作目录（realpath 规范化）
        self._cwd_authorized: bool = False           # 是否已授权
        self._cmd_whitelist: set[str] = set()        # 用户命令白名单（命令名，非完整命令行）
        self._audit_buffer: deque = deque(maxlen=200)  # 审计环形缓冲（不落盘，重启清空）
        # ── 新增：CUSTOM 模式的 auto_allow 工具白名单（借鉴 OpenWorker）──
        self._auto_allow_tools: set[str] = set()

    @staticmethod
    def _init_mode_from_env() -> PermissionMode:
        """从环境变量初始化权限模式（向后兼容）

        默认 DEFAULT 模式。未显式设置时打印 CRITICAL 警告。
        """
        # 优先检查显式权限模式设置
        perm_env = os.getenv("AGENT_PERMISSION_MODE", "").strip().lower()
        mode_map = {m.value: m for m in PermissionMode}
        if perm_env in mode_map:
            return mode_map[perm_env]

        # 向后兼容 AGENT_DEV_MODE
        env_val = os.getenv("AGENT_DEV_MODE", "").strip().lower()
        if env_val in ("1", "true", "yes"):
            return PermissionMode.DEV

        # 环境变量未显式设置 → 读持久化文件（webui 上次切换的模式）
        persisted = _load_persisted_mode()
        if persisted is not None:
            logger.info(
                "permission_manager.using_persisted_mode",
                mode=persisted.value,
            )
            return persisted

        # 未显式配置 → 默认 DEFAULT 并打印提示
        logger.info(
            "permission_manager.using_default_mode",
            msg="未设置 AGENT_PERMISSION_MODE，使用 DEFAULT 模式。"
                "可设置 AGENT_PERMISSION_MODE=default/dev/strict/bypass 切换",
        )
        return PermissionMode.DEFAULT

    @property
    def mode(self) -> PermissionMode:
        """获取当前权限模式"""
        return self._mode

    def set_mode(self, mode: PermissionMode | str) -> None:
        """设置权限模式

        Args:
            mode: PermissionMode 枚举或字符串值
        """
        if isinstance(mode, str):
            mode_map = {m.value: m for m in PermissionMode}
            mode = mode_map.get(mode, PermissionMode.DEFAULT)

        with self._lock:
            old = self._mode
            self._mode = mode
            logger.info(
                "permission_manager.mode_changed",
                old=old.value, new=mode.value,
            )
            # 落盘：切换到哪档就持久化哪档，重启后仍生效（尽力而为，失败不阻断）。
            # 放在锁内：内存态更新与磁盘写入串行化，避免并发 set_mode 时
            # 磁盘残留旧的高权限档位（如内存已回 DEFAULT 而磁盘仍是 GOAT，重启恢复高权限）。
            _persist_mode(mode)

    def is_dev_mode(self) -> bool:
        """是否开发模式"""
        return self._mode == PermissionMode.DEV

    def is_bypass_mode(self) -> bool:
        """是否绕过/梭哈模式"""
        return self._mode in (PermissionMode.BYPASS, PermissionMode.GOAT)

    def is_goat_mode(self) -> bool:
        """是否梭哈模式"""
        return self._mode == PermissionMode.GOAT

    def is_strict_mode(self) -> bool:
        """是否严格模式"""
        return self._mode == PermissionMode.STRICT

    # ── 新增：CUSTOM 模式 auto_allow 工具管理（借鉴 OpenWorker）──

    def add_auto_allow_tool(self, tool_name: str) -> None:
        """添加工具到 auto_allow 白名单（CUSTOM 模式下自动放行）。"""
        with self._lock:
            self._auto_allow_tools.add(tool_name)
            logger.info("permission_manager.auto_allow_added", tool=tool_name)

    def remove_auto_allow_tool(self, tool_name: str) -> None:
        """从 auto_allow 白名单移除工具。"""
        with self._lock:
            self._auto_allow_tools.discard(tool_name)

    def get_auto_allow_tools(self) -> list[str]:
        """获取 auto_allow 白名单。"""
        with self._lock:
            return sorted(self._auto_allow_tools)

    def set_auto_allow_tools(self, tools: list[str]) -> None:
        """批量设置 auto_allow 白名单。"""
        with self._lock:
            self._auto_allow_tools = set(tools)

    def check_goat_dangerous_command(self, command: str) -> tuple[bool, str]:
        """梭哈模式防傻检查：拦截明显致命的 shell 命令

        Returns:
            (is_dangerous, reason) — is_dangerous=True 时应拒绝执行
        """
        for pattern in _GOAT_DANGEROUS_SHELL_RE:
            if pattern.search(command):
                reason = f"防傻拦截：检测到致命操作 [{pattern.pattern}]，即使用梭哈模式也不允许执行"
                logger.critical("permission_manager.goat_dangerous_blocked",
                                pattern=pattern.pattern, command=command[:200])
                return True, reason
        return False, ""

    def check_tool_permission(self, tool_name: str, tool_input: dict | None = None) -> tuple[bool, str]:
        """检查工具是否被允许执行

        Args:
            tool_name: 工具名称
            tool_input: 工具输入参数（可选，用于梭哈模式防傻检查）

        Returns:
            (allowed, reason) 元组
        """
        # ── 新增模式：DISCUSS/PLAN 只读门控（借鉴 OpenWorker）──
        if self._mode in READ_ONLY_MODES:
            # 获取工具权限级别
            from tool_engine.tool_registry import get_tool, ToolPermission
            tool = get_tool(tool_name)
            if tool:
                perm = tool.get("permission", ToolPermission.READ_ONLY)
                if perm != ToolPermission.READ_ONLY:
                    return False, f"{self._mode.value} 模式是只读的，不允许执行 {tool_name}"
            elif tool_name in _SENSITIVE_TOOLS:
                # 未注册的敏感工具（如工具注册失败）也必须拒绝，与 INTERACTIVE/CUSTOM 模式一致
                return False, f"{self._mode.value} 模式是只读的，不允许执行 {tool_name}"

        # GOAT 模式：全部放行，但对 shell 命令做防傻检查
        if self._mode == PermissionMode.GOAT:
            if tool_name == "shell_command" and tool_input:
                cmd = tool_input.get("command", "")
                if cmd:
                    is_dangerous, reason = self.check_goat_dangerous_command(cmd)
                    if is_dangerous:
                        return False, reason
            return True, ""

        # BYPASS 模式：全部放行，但对 shell 命令做防傻检查（与 GOAT 一致）
        if self._mode == PermissionMode.BYPASS:
            if tool_name == "shell_command" and tool_input:
                cmd = tool_input.get("command", "")
                if cmd:
                    is_dangerous, reason = self.check_goat_dangerous_command(cmd)
                    if is_dangerous:
                        return False, reason
            return True, ""

        # ── 新增模式：AUTO 全自动放行（但仍受路径范围限制）──
        if self._mode == PermissionMode.AUTO:
            if tool_name == "shell_command" and tool_input:
                cmd = tool_input.get("command", "")
                if cmd:
                    is_dangerous, reason = self.check_goat_dangerous_command(cmd)
                    if is_dangerous:
                        return False, reason
            return True, ""

        # ── 新增模式：CUSTOM 交互式 + auto_allow 白名单 ──
        if self._mode == PermissionMode.CUSTOM:
            if tool_name in self._auto_allow_tools:
                return True, "auto-allowed by config"
            # 非 auto_allow 的工具：READ_ONLY 放行，READ_WRITE/EXECUTE 需确认
            from tool_engine.tool_registry import get_tool, ToolPermission
            tool = get_tool(tool_name)
            if tool:
                perm = tool.get("permission", ToolPermission.READ_ONLY)
                if perm == ToolPermission.READ_ONLY:
                    return True, ""
                perm_label = perm.value if hasattr(perm, "value") else str(perm)
                return False, f"自定义模式下 {tool_name}（{perm_label}）需要用户确认"
            # 未知工具回退到 _SENSITIVE_TOOLS
            if tool_name in _SENSITIVE_TOOLS:
                return False, f"自定义模式下 {tool_name} 需要确认"
            return True, ""

        # STRICT 模式：敏感工具需要确认
        if self._mode == PermissionMode.STRICT and tool_name in _SENSITIVE_TOOLS:
            return False, f"严格模式下 {tool_name} 需要确认"

        # ── 新增模式：INTERACTIVE 交互式确认 ──
        if self._mode == PermissionMode.INTERACTIVE:
            # 只读工具直接放行
            from tool_engine.tool_registry import get_tool, ToolPermission
            tool = get_tool(tool_name)
            if tool:
                perm = tool.get("permission", ToolPermission.READ_ONLY)
                if perm == ToolPermission.READ_ONLY:
                    return True, ""
                # READ_WRITE/EXECUTE 工具需要确认
                perm_label = perm.value if hasattr(perm, "value") else str(perm)
                return False, f"交互式模式下 {tool_name}（{perm_label}）需要用户确认"
            # 未知工具回退到 _SENSITIVE_TOOLS
            if tool_name in _SENSITIVE_TOOLS:
                return False, f"交互式模式下 {tool_name} 需要用户确认"

        return True, ""

    def decide_security_action(self, threat_type: str, confidence: float) -> str:
        """根据权限模式决定安全动作

        替代 SecurityFilter._decide_action 中的 _is_dev_mode 检查

        Returns:
            "allow" / "warn" / "block"
        """
        # GOAT 模式：跳过安全检查，但高危威胁返回 warn（不 block，仅警告）
        if self._mode == PermissionMode.GOAT:
            if any(kw in threat_type.lower() for kw in _GOAT_WARN_THREAT_KEYWORDS):
                logger.warning(
                    "permission_manager.goat_high_risk_warn",
                    threat_type=threat_type, confidence=confidence,
                    msg="梭哈模式下检测到高危威胁，返回 warn 但不拦截",
                )
                return "warn"
            if confidence >= 0.95:
                logger.critical(
                    "permission_manager.goat_high_confidence_threat",
                    threat_type=threat_type, confidence=confidence,
                    msg="梭哈模式下检测到高置信度安全威胁，已放行但强烈建议检查",
                )
            return "allow"

        # BYPASS 模式：跳过所有安全检查（兼容旧代码）
        if self._mode == PermissionMode.BYPASS:
            if confidence >= 0.95:
                logger.critical(
                    "permission_manager.bypass_high_confidence_threat",
                    threat_type=threat_type, confidence=confidence,
                    msg="BYPASS 模式下检测到高置信度安全威胁，已放行但强烈建议检查",
                )
            return "allow"

        # 基于置信度的基础动作
        if confidence >= 0.8:
            base_action = "block"
        elif confidence >= 0.6:
            base_action = "warn"
        else:
            return "allow"

        # DEV 模式：block 降级为 warn，只读查询直接放行
        if self._mode == PermissionMode.DEV:
            # 只读类威胁（查看信息、查询数据）在 DEV 模式下直接放行
            readonly_keywords = ["info_disclosure", "read_only", "query", "inspect"]
            if any(kw in threat_type.lower() for kw in readonly_keywords):
                logger.info(f"[DEV_MODE] 只读操作放行: {threat_type} (置信度={confidence:.2f})")
                return "allow"
            # 其他 block 威胁降级为 warn
            if base_action == "block":
                logger.warning(f"[DEV_MODE] 安全威胁降级为 warn: {threat_type} (置信度={confidence:.2f})")
                return "warn"

        # STRICT 模式：warn 也升级为 block
        if self._mode == PermissionMode.STRICT and base_action == "warn":
            return "block"

        return base_action

    # ── 工作目录授权 API（叠加层，独立于 PermissionMode） ──────
    def set_cwd(self, path: str) -> None:
        """设置并授权工作目录"""
        with self._lock:
            self._cwd = os.path.realpath(path)
            self._cwd_authorized = True
            logger.info("permission_manager.cwd_set", cwd=self._cwd)

    def clear_cwd(self) -> None:
        """撤销工作目录授权"""
        with self._lock:
            self._cwd = ""
            self._cwd_authorized = False
            logger.info("permission_manager.cwd_cleared")

    def is_cwd_authorized(self) -> bool:
        """是否已授权工作目录"""
        return self._cwd_authorized

    @property
    def cwd(self) -> str:
        """当前授权工作目录"""
        return self._cwd

    def _norm(self, path: str) -> str:
        """规范化路径（realpath + Windows 大小写不敏感）"""
        p = os.path.realpath(path)
        if os.name == "nt":
            p = os.path.normcase(p)
        return p

    def is_path_allowed(self, file_path: str) -> tuple[bool, str]:
        """检查路径是否在 cwd 内

        Returns:
            (allowed, reason) — allowed=True 放行；allowed=False 时 reason 为拒绝原因
        """
        if not self._cwd_authorized or not self._cwd:
            return False, "未授权工作目录，请先在聊天框上方选择并授权工作目录"
        if not file_path:
            return False, "路径为空"
        norm_cwd = self._norm(self._cwd)
        norm_target = self._norm(file_path)
        # 允许 cwd 本身
        if norm_target == norm_cwd:
            return True, ""
        # 前缀匹配（防 ../ 逃逸，realpath 已规范化）
        if norm_target.startswith(norm_cwd + os.sep):
            return True, ""
        return False, f"路径超出工作目录：{file_path}（cwd={self._cwd}）"

    # 命令分隔符（用于拆分复合命令）
    _CMD_SEPARATORS = ("&&", "||", ";", "|")

    def _split_compound_command(self, command: str) -> list[str]:
        """拆分复合命令为子命令列表

        对包含 && / || / ; / | 的复合命令拆分，对每段独立检查。
        """
        import re as _re
        parts = _re.split(r"\s*(?:&&|\|\||;|\|)\s*", command)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _extract_cmd_name(sub_cmd: str) -> str:
        """提取命令名（首个 token，如 'npm install' → 'npm'）"""
        tokens = sub_cmd.split()
        return tokens[0] if tokens else ""

    def is_command_allowed(self, command: str) -> tuple[bool, str, bool]:
        """检查命令是否允许执行

        Returns:
            (allowed, reason, needs_confirmation)
            - 黑名单命中 → (False, reason, False)  永远拒绝
            - 白名单命中 → (True, "", False)        放行
            - 都未命中 → (False, "需用户确认", True) 弹窗
            - 高权限模式（GOAT/BYPASS/AUTO）→ 非黑名单命令直接放行（不弹确认）
        """
        if not command.strip():
            return False, "命令为空", False

        sub_cmds = self._split_compound_command(command)
        # 高权限模式：黑名单始终生效，其余命令直接放行（无需确认）
        if self._mode in AUTO_APPROVE_MODES:
            for sub in sub_cmds:
                for pattern in _GOAT_DANGEROUS_SHELL_RE:
                    if pattern.search(sub):
                        reason = f"危险命令被拦截：{pattern.pattern}"
                        logger.critical("permission_manager.workspace_dangerous_blocked",
                                        pattern=pattern.pattern, command=sub[:200])
                        return False, reason, False
            return True, "", False

        needs_conf_flag = False
        for sub in sub_cmds:
            # 1. 黑名单始终生效（复用 _GOAT_DANGEROUS_SHELL_RE，不论 PermissionMode）
            for pattern in _GOAT_DANGEROUS_SHELL_RE:
                if pattern.search(sub):
                    reason = f"危险命令被拦截：{pattern.pattern}"
                    logger.critical("permission_manager.workspace_dangerous_blocked",
                                    pattern=pattern.pattern, command=sub[:200])
                    return False, reason, False
            # 2. 白名单检查（命令名匹配）
            cmd_name = self._extract_cmd_name(sub)
            if cmd_name and cmd_name not in self._cmd_whitelist:
                needs_conf_flag = True
        if needs_conf_flag:
            return False, "命令不在白名单，需用户确认", True
        return True, "", False

    def add_to_whitelist(self, command: str) -> None:
        """添加命令名到白名单（自动提取首个 token）"""
        cmd_name = self._extract_cmd_name(command)
        if cmd_name:
            with self._lock:
                self._cmd_whitelist.add(cmd_name)
                logger.info("permission_manager.whitelist_added", command=cmd_name)

    def remove_from_whitelist(self, command: str) -> None:
        """从白名单删除命令名

        command 可以是命令名（如 "npm"）或完整命令行（自动提取）。
        """
        cmd_name = command.strip()
        # 如果含空格，按完整命令行提取命令名；否则视为命令名
        if " " in cmd_name:
            cmd_name = self._extract_cmd_name(cmd_name)
        with self._lock:
            self._cmd_whitelist.discard(cmd_name)
            logger.info("permission_manager.whitelist_removed", command=cmd_name)

    def get_whitelist(self) -> list[str]:
        """获取白名单（排序后列表）"""
        with self._lock:
            return sorted(self._cmd_whitelist)

    def set_whitelist(self, items: list[str]) -> None:
        """批量设置白名单"""
        with self._lock:
            self._cmd_whitelist = set(items)

    def add_audit_entry(self, entry: AuditEntry) -> None:
        """添加审计条目到环形缓冲"""
        with self._lock:
            self._audit_buffer.append(asdict(entry))

    def clear_audit_log(self) -> None:
        """清空审计环形缓冲。

        全局单例 PermissionManager 的 ``_audit_buffer`` 跨测试保留，
        若测试不显式清理，先写入的 audit 条目会污染后续断言
        （例如 ``test_get_audit_with_entries`` 期望 len==1，
        实际从 ``test_delete_action_classified`` 遗留 1 条 → len==2）。
        测试 fixture 应在每用例前调用本方法，保证隔离。
        """
        with self._lock:
            self._audit_buffer.clear()

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        """获取审计日志（最近 limit 条）"""
        with self._lock:
            entries = list(self._audit_buffer)
        return entries[-limit:]


# ── 权限模式持久化 ────────────────────────────────────────
# webui 切换的权限模式只驻内存，重启后被环境变量/默认值覆盖，
# 导致用户"选了随心(goat)却重启后失效"。这里把模式落盘，
# 初始化时优先环境变量，其次读盘，最后默认 DEFAULT。
# 测试可用 AGENT_PERMISSION_FILE 指向临时文件，避免污染真实配置。
_PERMISSION_FILE = os.getenv("AGENT_PERMISSION_FILE", "")


def _permission_file_path() -> str:
    """解析权限模式持久化文件路径（惰性，避免模块导入期 IO/循环依赖）。"""
    if _PERMISSION_FILE:
        return _PERMISSION_FILE
    try:
        from config import get_config_dir
        return str(get_config_dir() / "permission_mode.json")
    except Exception:
        return ""


def _load_persisted_mode() -> PermissionMode | None:
    """从磁盘读取持久化的权限模式；无文件/解析失败返回 None（不阻塞）。"""
    path = _permission_file_path()
    if not path:
        return None
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mode = (data or {}).get("mode", "").strip().lower()
        if mode:
            return PermissionMode(mode)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug("permission_manager.load_persisted_failed path=%s err=%s", path, e)
    return None


def _persist_mode(mode: PermissionMode) -> None:
    """把权限模式写入磁盘（尽力而为，失败仅记 debug，不阻断主流程）。"""
    path = _permission_file_path()
    if not path:
        return
    import json
    try:
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mode": mode.value}, f, ensure_ascii=False)
    except Exception as e:
        logger.debug("permission_manager.persist_failed path=%s err=%s", path, e)


# ── 全局单例 ──────────────────────────────────────────────

_default_manager: PermissionManager | None = None
_manager_lock = threading.Lock()


def get_permission_manager() -> PermissionManager:
    """获取全局权限管理器"""
    global _default_manager
    if _default_manager is None:
        with _manager_lock:
            if _default_manager is None:
                _default_manager = PermissionManager()
    return _default_manager
