"""记忆蒸馏器 — 将旧情景记忆压缩为摘要，控制上下文长度。

使用硅基流动免费模型（Qwen/Qwen2.5-7B-Instruct）进行蒸馏，不占用主模型配额。
失败时降级到 ModelRouter.route。
"""

import os
import time
import httpx
from loguru import logger


DISTILL_PROMPT = """你是记忆蒸馏助手。将以下旧对话记忆压缩为简洁摘要，保留关键信息：
- 涉及的人物
- 重要事件
- 用户偏好
- 时间和地点
- 关键决策和结论

记忆列表：
{memories_text}

输出 300 字以内的摘要："""


class MemoryDistiller:
    """记忆蒸馏器：调用硅基流动免费模型将旧记忆列表压缩为摘要。"""

    def __init__(self, router=None):
        self.router = router
        self._free_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._free_base_url = "https://api.siliconflow.cn/v1"
        self._free_model = "Qwen/Qwen2.5-7B-Instruct"
        logger.info("memory_distiller.ready")

    def set_free_model_client(self, api_key: str, base_url: str, model: str):
        """配置硅基流动免费模型客户端"""
        self._free_api_key = api_key
        self._free_base_url = base_url
        self._free_model = model

    async def _call_free_model(self, messages: list, temperature: float = 0.6,
                                max_tokens: int = 800) -> str | None:
        """调用硅基流动免费模型"""
        if not self._free_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{self._free_base_url}/chat/completions",
                    json={
                        "model": self._free_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    headers={
                        "Authorization": f"Bearer {self._free_api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("memory_distiller.free_model_failed", error=str(e))
            return None

    async def distill(self, memories: list[dict]) -> str:
        """将旧记忆列表蒸馏为摘要。

        Args:
            memories: 旧记忆 dict 列表，每条至少包含 summary 字段。

        Returns:
            蒸馏后的摘要文本。失败时返回空串。
        """
        if not memories:
            return ""

        # 构建记忆文本：编号 + 摘要，截断避免超出模型上下文
        lines = []
        for i, mem in enumerate(memories, start=1):
            summary = (mem.get("summary") or "").strip()
            if not summary:
                continue
            ts = mem.get("timestamp", 0)
            if ts:
                try:
                    time_str = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
                except (TypeError, ValueError, OSError):
                    time_str = ""
            else:
                time_str = ""
            prefix = f"[{i}]{time_str} " if time_str else f"[{i}] "
            lines.append(f"{prefix}{summary[:200]}")

        if not lines:
            return ""

        memories_text = "\n".join(lines)
        prompt = DISTILL_PROMPT.format(memories_text=memories_text)
        messages = [{"role": "user", "content": prompt}]

        # 优先使用免费模型，失败降级到 router
        result = await self._call_free_model(
            messages, temperature=0.4, max_tokens=600,
        )
        if result is None and self.router:
            try:
                result = await self.router.route(
                    task_type="memory_encoding",
                    messages=messages,
                    temperature=0.4,
                    max_tokens=600,
                )
            except Exception as e:
                logger.warning("memory_distiller.router_fallback_failed", error=str(e))
                return ""

        if not result or not isinstance(result, str):
            return ""

        summary = result.strip()
        # 去除可能的 <think> 标签内容
        if "<think>" in summary:
            import re
            summary = re.sub(r"<think>.*?</think>", "", summary, flags=re.DOTALL).strip()
        return summary
