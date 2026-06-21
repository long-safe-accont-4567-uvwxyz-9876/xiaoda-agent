"""权限管理器 — 借鉴 Claude Agent SDK 的 PermissionMode 设计

将 AGENT_DEV_MODE 二值开关升级为多级权限模式，
支持运行时动态切换，向后兼容环境变量。
"""
from __future__ import annotations

import os
import threading
from enum import Enum
from loguru import logger


class PermissionMode(Enum):
    """权限模式"""
    DEFAULT = "default"    # 默认：安全威胁按置信度决定 block/warn
    DEV = "dev"            # 开发模式：block 降级为 warn，修改性工具自动允许
    STRICT = "strict"      # 严格模式：所有威胁 block，修改性工具需确认
    BYPASS = "bypass"      # 绕过模式：跳过所有安全检查（仅限受信环境）


# 敏感操作工具列表（strict 模式下需要确认）
_SENSITIVE_TOOLS = {
    "shell_command", "execute_code", "python_executor",
    "write_file", "edit_file", "create_file",
    "agnes_image", "agnes_video",
}


class PermissionManager:
    """权限管理器 — 全局单例"""

    def __init__(self):
        self._mode = self._init_mode_from_env()
        self._lock = threading.Lock()

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
                f"permission_manager.mode_changed",
                old=old.value, new=mode.value,
            )

    def is_dev_mode(self) -> bool:
        """是否开发模式"""
        return self._mode == PermissionMode.DEV

    def is_bypass_mode(self) -> bool:
        """是否绕过模式"""
        return self._mode == PermissionMode.BYPASS

    def is_strict_mode(self) -> bool:
        """是否严格模式"""
        return self._mode == PermissionMode.STRICT

    def check_tool_permission(self, tool_name: str) -> tuple[bool, str]:
        """检查工具是否被允许执行

        Returns:
            (allowed, reason) 元组
        """
        if self._mode == PermissionMode.BYPASS:
            return True, ""

        if self._mode == PermissionMode.STRICT:
            if tool_name in _SENSITIVE_TOOLS:
                return False, f"严格模式下 {tool_name} 需要确认"

        return True, ""

    def decide_security_action(self, threat_type: str, confidence: float) -> str:
        """根据权限模式决定安全动作

        替代 SecurityFilter._decide_action 中的 _is_dev_mode 检查

        Returns:
            "allow" / "warn" / "block"
        """
        if self._mode == PermissionMode.BYPASS:
            # BYPASS 模式下高置信度威胁仍记录 CRITICAL 日志
            if confidence >= 0.95:
                logger.critical(
                    "permission_manager.bypass_high_confidence_threat",
                    threat_type=threat_type, confidence=confidence,
                    msg="BYPASS 模式下检测到高置信度安全威胁，已放行但强烈建议检查",
                )
            return "allow"

        if confidence >= 0.8:
            base_action = "block"
        elif confidence >= 0.6:
            base_action = "warn"
        else:
            return "allow"

        if self._mode == PermissionMode.DEV and base_action == "block":
            logger.warning(f"[DEV_MODE] 安全威胁降级为 warn: {threat_type} (置信度={confidence:.2f})")
            return "warn"

        if self._mode == PermissionMode.STRICT:
            # strict 模式下 warn 也升级为 block
            if base_action == "warn":
                return "block"

        return base_action


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
