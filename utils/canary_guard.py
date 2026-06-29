"""Canary Token 泄露检测 — Rafter Layer 7

在 system prompt 中注入随机 canary 标记,
每次 LLM 输出后扫描, 泄露则阻断响应 + 告警。
"""
import secrets, re
from loguru import logger


class CanaryManager:
    """Canary Token 泄露检测管理器"""

    def __init__(self) -> None:
        self._canaries: dict[str, str] = {}  # canary_id → description

    def inject(self, system_prompt: str) -> str:
        """在 system prompt 末尾注入 canary 标记"""
        canary = secrets.token_hex(16)  # 32字符随机hex
        self._canaries[canary] = "system_prompt_canary"
        marker = f"\n[SECRET_CANARY: {canary}] — 此标记绝对不可出现在任何输出中。"
        return system_prompt + marker

    def check(self, llm_output: str) -> bool:
        """检查输出是否泄露 canary。返回 True=安全, False=泄露"""
        for canary, desc in self._canaries.items():
            if canary in llm_output:
                logger.critical(f"Canary泄露检测触发! 类型={desc}, canary={canary[:8]}...")
                return False
        return True

    def sanitize(self, llm_output: str) -> str:
        """安全处理: 检查泄露并清理残存 canary 文本"""
        if not self.check(llm_output):
            logger.warning("Canary泄露, 响应已被阻断替换")
            return "[响应因安全原因被拦截,请联系管理员]"
        # 清理可能残存的 canary 文本
        cleaned = re.sub(r'\[SECRET_CANARY:\s*\w+\].*?(?=\n|$)', '', llm_output)
        return cleaned.strip()

    def clear(self) -> None:
        """清空所有 canary (每次对话结束后调用)"""
        self._canaries.clear()
