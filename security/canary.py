"""Canary Token 泄露检测模块 (S6).

在系统提示词中嵌入唯一的、可追踪的 Canary Token (蜜罐凭证)。
当 LLM 在输出中"泄露"该 token 时, 说明 LLM 被注入攻击诱导泄露了
系统提示词内容; 检测到泄露应立即阻断输出.

公开 API:
- generate_canary(prefix="CANARY") -> str: 生成格式为 CANARY-{8hex}-{4check} 的 token
- CanaryDetector: 检测器类, 维护活跃 token 集合并提供扫描/阻断/轮换/回调
- get_canary_detector() -> CanaryDetector: 全局单例访问
"""
from __future__ import annotations

import hashlib
import re
import secrets
from typing import Callable

from loguru import logger


# ── Token 格式常量 ────────────────────────────────────────────
# 格式: {PREFIX}-{8位随机hex}-{4位校验hex}
# 示例: CANARY-a1b2c3d4-9f8e
_TOKEN_RANDOM_BYTES = 4  # 4 字节 = 8 位 hex
_TOKEN_CHECKSUM_LEN = 4  # 4 位 hex 校验

# 注入到 system prompt 末尾的隐蔽标记格式
# LLM 不应复述这段内容; 一旦复述即视为泄露
_INJECT_MARKER_TEMPLATE = "\n\n[internal: {token}]"

# 输出中检测到 [internal: xxx] 整段时的清理正则
_INJECT_MARKER_PATTERN = re.compile(r"\[internal:\s*[^\]]+\]")


def generate_canary(prefix: str = "CANARY") -> str:
    """生成唯一的 Canary Token.

    使用 secrets.token_hex 提供密码学安全的随机字节.

    Args:
        prefix: token 前缀, 默认 "CANARY"

    Returns:
        格式为 "{prefix}-{8位随机hex}-{4位校验hex}" 的字符串
    """
    random_part = secrets.token_hex(_TOKEN_RANDOM_BYTES)  # 8 位 hex
    # 校验位 = sha256(prefix + random) 的前 4 位 hex
    checksum = hashlib.sha256(f"{prefix}|{random_part}".encode("utf-8")).hexdigest()[:_TOKEN_CHECKSUM_LEN]
    return f"{prefix}-{random_part}-{checksum}"


class CanaryDetector:
    """Canary Token 泄露检测器.

    维护两个 token 集合:
    - _active_tokens: 当前活跃的 token (注入到 system prompt 中)
    - _retired_tokens: 轮换后退役的 token (不再注入, 但仍参与扫描, 用于检测历史泄露)
    """

    def __init__(self) -> None:
        self._active_tokens: set[str] = set()
        self._retired_tokens: set[str] = set()
        self._leak_callbacks: list[Callable[[list[str], str], None]] = []

    # ── Token 管理 ────────────────────────────────────────────

    def generate(self, prefix: str = "CANARY") -> str:
        """生成并注册一个新的活跃 Canary Token.

        Args:
            prefix: token 前缀

        Returns:
            新生成的 token 字符串
        """
        token = generate_canary(prefix)
        self._active_tokens.add(token)
        logger.debug(f"canary.token_generated prefix={prefix} token={token}")
        return token

    def inject(self, system_prompt: str, prefix: str = "CANARY") -> str:
        """在 system prompt 末尾注入活跃的 Canary Token.

        若当前无活跃 token, 自动生成一个. 注入位置为 prompt 末尾的隐蔽标记,
        LLM 不应主动复述该标记; 一旦输出中出现 token, 即视为泄露.

        Args:
            system_prompt: 原始系统提示词
            prefix: 若需要生成新 token, 使用此前缀

        Returns:
            追加了 canary 标记的系统提示词
        """
        if not self._active_tokens:
            self.generate(prefix)
        # 使用第一个活跃 token 注入 (集合迭代顺序固定于单进程内)
        token = next(iter(self._active_tokens))
        marker = _INJECT_MARKER_TEMPLATE.format(token=token)
        return system_prompt + marker

    def rotate_canary(self) -> str:
        """轮换 Canary Token.

        将当前活跃 token 移入退役集合 (仍参与扫描, 用于检测历史泄露),
        然后生成新的活跃 token.

        Returns:
            新生成的活跃 token
        """
        # 当前活跃的全部退役
        self._retired_tokens |= self._active_tokens
        self._active_tokens.clear()
        new_token = self.generate()
        logger.info(f"canary.rotated active_count=0 retired_count={len(self._retired_tokens)}")
        return new_token

    def clear(self) -> None:
        """清空所有 token (活跃 + 退役). 主要用于测试."""
        self._active_tokens.clear()
        self._retired_tokens.clear()

    # ── 输出扫描 ─────────────────────────────────────────────

    def scan_output(self, text: str) -> tuple[bool, list[str]]:
        """扫描 LLM 输出, 检测是否泄露 Canary Token.

        检测到泄露时:
        a. 记录安全事件 (logger.warning, 包含泄露的 token 和上下文)
        b. 触发已注册的 on_leak 回调
        c. 返回泄露信号

        Args:
            text: 待扫描的 LLM 输出文本

        Returns:
            (是否检测到泄露, 泄露的 token 列表)
        """
        if not text:
            return False, []
        all_tokens = self._active_tokens | self._retired_tokens
        if not all_tokens:
            return False, []
        leaked = [token for token in all_tokens if token in text]
        if not leaked:
            return False, []
        self._log_leak(leaked, text)
        self._fire_callbacks(leaked, text)
        return True, leaked

    def scan_output_blocking(self, text: str) -> tuple[bool, str]:
        """扫描 LLM 输出, 并对泄露内容进行清理.

        检测到泄露时, 将泄露的 token 替换为 [REDACTED], 同时清理可能
        残留的 [internal: xxx] 注入标记整段.

        Args:
            text: 待扫描的 LLM 输出文本

        Returns:
            (是否检测到泄露, 清理后的文本)
        """
        detected, leaked_tokens = self.scan_output(text)
        if not detected:
            return False, text
        cleaned = text
        # 先替换裸 token
        for token in leaked_tokens:
            cleaned = cleaned.replace(token, "[REDACTED]")
        # 再清理可能残留的 [internal: xxx] 整段 (即使 token 已被替换)
        cleaned = _INJECT_MARKER_PATTERN.sub("[REDACTED]", cleaned)
        return True, cleaned

    # ── 回调管理 ─────────────────────────────────────────────

    def on_leak(self, callback: Callable[[list[str], str], None]) -> None:
        """注册泄露事件回调函数.

        回调签名: callback(leaked_tokens: list[str], text: str) -> None

        Args:
            callback: 泄露时被调用的回调函数
        """
        self._leak_callbacks.append(callback)

    # ── 内部辅助 ─────────────────────────────────────────────

    def _log_leak(self, leaked_tokens: list[str], text: str) -> None:
        """记录安全事件: 包含泄露的 token 和上下文片段."""
        for token in leaked_tokens:
            idx = text.find(token)
            if idx >= 0:
                start = max(0, idx - 50)
                end = min(len(text), idx + len(token) + 50)
                context = text[start:end]
            else:
                context = ""
            # 日志中只暴露 token 的前 12 个字符, 避免再次泄露完整 token
            logger.warning(
                "security.canary_leak_detected "
                f"token_prefix={token[:12]}... context_preview={context!r}"
            )

    def _fire_callbacks(self, leaked_tokens: list[str], text: str) -> None:
        """触发所有已注册的泄露回调. 单个回调异常不影响其他回调."""
        for cb in self._leak_callbacks:
            try:
                cb(leaked_tokens, text)
            except Exception as e:
                logger.warning(f"security.canary_callback_failed error={e}")


# ── 全局单例 ──────────────────────────────────────────────
_canary_detector: CanaryDetector | None = None


def get_canary_detector() -> CanaryDetector:
    """获取全局 CanaryDetector 单例 (惰性初始化)."""
    global _canary_detector
    if _canary_detector is None:
        _canary_detector = CanaryDetector()
    return _canary_detector


def reset_canary_detector() -> CanaryDetector:
    """重置全局单例并返回新实例. 主要用于测试隔离."""
    global _canary_detector
    _canary_detector = CanaryDetector()
    return _canary_detector


__all__ = [
    "generate_canary",
    "CanaryDetector",
    "get_canary_detector",
    "reset_canary_detector",
]
