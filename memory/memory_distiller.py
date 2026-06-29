"""记忆蒸馏器 — 将旧情景记忆压缩为摘要，控制上下文长度。

使用硅基流动免费模型（THUDM/GLM-4-9B-0414）进行蒸馏，不占用主模型配额。
失败时降级到 ModelRouter.route。
"""
from typing import Any, Optional

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


RECALL_PROMPT = """你是纳西妲的回忆整理助手。把最近这段时间发生的重要记忆整理成一段"回忆笔记"，
让纳西妲日后能快速回忆起这段时间的故事。

要求：
- 用自然流畅的叙述风格（像写日记），不要用列表
- 按时间顺序串起 3-5 个关键事件
- 保留人物、时间、地点、情感、决策
- 末尾用一句话总结这段记忆的"情绪基调"
- 总字数 200-400 字

记忆列表（按时间排序）：
{memories_text}

回忆笔记："""


class MemoryDistiller:
    """记忆蒸馏器：调用硅基流动免费模型将旧记忆列表压缩为摘要。"""

    def __init__(self, router: Optional[Any]=None) -> None:
        self.router = router
        self._free_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._free_base_url = "https://api.siliconflow.cn/v1"
        self._free_model = "THUDM/GLM-4-9B-0414"
        logger.info("memory_distiller.ready")

    def set_free_model_client(self, api_key: str, base_url: str, model: str) -> None:
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

    async def distill_recall(self, memories: list[dict]) -> str:
        """将一段时间窗内的高重要性记忆整理成"回忆笔记"（叙事风格）。

        与 distill() 的区别：
        - prompt 强调故事性叙述（像日记），而非压缩摘要
        - 输出 200-400 字，末尾带"情绪基调"总结
        - 供定时回忆任务写入 memory_recall_notes 表

        Args:
            memories: 时间窗内的高重要性记忆 dict 列表，每条至少含 summary/timestamp

        Returns:
            回忆笔记文本。失败返回空串。
        """
        if not memories:
            return ""

        # 按时间升序排列，便于"按时间顺序串起事件"
        try:
            sorted_mems = sorted(memories, key=lambda m: float(m.get("timestamp", 0)))
        except Exception:
            sorted_mems = memories

        lines = []
        for i, mem in enumerate(sorted_mems, start=1):
            summary = (mem.get("summary") or "").strip()
            if not summary:
                continue
            ts = mem.get("timestamp", 0)
            time_str = ""
            if ts:
                try:
                    time_str = time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
                except (TypeError, ValueError, OSError):
                    time_str = ""
            prefix = f"[{i}]{time_str} " if time_str else f"[{i}] "
            lines.append(f"{prefix}{summary[:200]}")

        if not lines:
            return ""

        memories_text = "\n".join(lines)
        prompt = RECALL_PROMPT.format(memories_text=memories_text)
        messages = [{"role": "user", "content": prompt}]

        # 优先使用免费模型，失败降级到 router
        result = await self._call_free_model(
            messages, temperature=0.5, max_tokens=700,
        )
        if result is None and self.router:
            try:
                result = await self.router.route(
                    task_type="memory_encoding",
                    messages=messages,
                    temperature=0.5,
                    max_tokens=700,
                )
            except Exception as e:
                logger.warning("memory_distiller.recall_router_fallback_failed", error=str(e))
                return ""

        if not result or not isinstance(result, str):
            return ""

        note = result.strip()
        if "<think>" in note:
            import re
            note = re.sub(r"<think>.*?</think>", "", note, flags=re.DOTALL).strip()
        return note
