"""记忆蒸馏器 — 将旧情景记忆压缩为摘要，控制上下文长度。

使用硅基流动免费模型（THUDM/GLM-4-9B-0414）进行蒸馏，不占用主模型配额。
失败时降级到 ModelRouter.route。
"""
from typing import Any

import os
import time
import httpx
from loguru import logger


DISTILL_PROMPT = """你是记忆蒸馏助手。将以下旧对话记忆压缩为结构化摘要。

【强制保留规则】
1. 所有人物名/称呼必须原样保留，禁止代词化
2. 所有具体时间（日期/时段）必须保留
3. 所有地点、物品名称必须保留
4. 关键决策、结论、承诺必须原样保留
5. 用户偏好和情感态度必须保留

【禁止行为】
- 禁止编造原文没有的信息
- 禁止模糊化具体细节（如"某个时间"→保留原始"7月15日早上7点"）
- 禁止合并不同时间的事件

记忆列表：
{memories_text}

输出 400 字以内的结构化摘要，按时间顺序组织："""


RECALL_PROMPT_TEMPLATE = """你是{n}的回忆整理助手。把最近这段时间发生的重要记忆整理成一段"回忆笔记"，
让{n}日后能快速回忆起这段时间的故事。

要求：
- 用自然流畅的叙述风格（像写日记），不要用列表
- 按时间顺序串起关键事件，不遗漏任何记忆
- 【强制】所有人物名/称呼、具体时间、地点必须原样保留，禁止代词化或模糊化
- 【强制】关键决策、结论、承诺必须原样保留
- 【禁止】编造原文没有的信息，禁止合并不同时间的事件
- 末尾用一句话总结这段记忆的"情绪基调"
- 总字数 300-500 字

记忆列表（按时间排序）：
{memories_text}

回忆笔记："""


class MemoryDistiller:
    """记忆蒸馏器：调用硅基流动免费模型将旧记忆列表压缩为摘要。"""

    def __init__(self, router: Any | None=None) -> None:
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
            lines.append(f"{prefix}{summary[:500]}")

        if not lines:
            return ""

        memories_text = "\n".join(lines)
        prompt = DISTILL_PROMPT.format(memories_text=memories_text)
        messages = [{"role": "user", "content": prompt}]

        # 优先使用免费模型，失败降级到 router
        result = await self._call_free_model(
            messages, temperature=0.3, max_tokens=1500,
        )
        if result is None and self.router:
            try:
                result = await self.router.route(
                    task_type="memory_encoding",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1500,
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
            lines.append(f"{prefix}{summary[:500]}")

        if not lines:
            return ""

        memories_text = "\n".join(lines)
        from config import get_agent_display_name
        prompt = RECALL_PROMPT_TEMPLATE.format(
            n=get_agent_display_name("xiaoda"),
            memories_text=memories_text,
        )
        messages = [{"role": "user", "content": prompt}]

        # 优先使用免费模型，失败降级到 router
        result = await self._call_free_model(
            messages, temperature=0.4, max_tokens=1500,
        )
        if result is None and self.router:
            try:
                result = await self.router.route(
                    task_type="memory_encoding",
                    messages=messages,
                    temperature=0.4,
                    max_tokens=1500,
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

    async def merge_knowledge(self, existing: str, new_content: str) -> str:
        """合并已有提炼知识和新蒸馏内容（LLM 合并，避免信息丢失）。

        用于 ADD-only 架构的 UPDATE 场景：当发现相似的提炼知识时，
        用 LLM 合并新旧内容，而不是直接覆盖。

        Args:
            existing: 已有提炼知识文本
            new_content: 新蒸馏的内容
        Returns:
            合并后的文本。LLM 失败时返回 new_content（降级：不合并，用新内容）。
        """
        if not existing or not existing.strip():
            return new_content
        if not new_content or not new_content.strip():
            return existing

        merge_prompt = f"""你是知识合并助手。将以下两段知识合并为一段摘要，保留所有关键信息：

【强制】所有人名、时间、地点、关键事实必须原样保留，禁止模糊化或丢失
【禁止】编造原文没有的信息

已有知识：
{existing[:500]}

新知识：
{new_content[:500]}

输出合并后的摘要（300字以内，不要重复信息）："""

        messages = [{"role": "user", "content": merge_prompt}]

        # 优先使用免费模型
        result = await self._call_free_model(
            messages, temperature=0.3, max_tokens=1000,
        )
        if result is None and self.router:
            try:
                result = await self.router.route(
                    task_type="memory_encoding",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1000,
                )
            except Exception as e:
                logger.warning("memory_distiller.merge_router_fallback_failed", error=str(e))
                return existing + "\n" + new_content

        if not result or not isinstance(result, str):
            # LLM 失败时降级：拼接新旧内容（不丢失旧知识）
            return existing + "\n" + new_content

        merged = result.strip()
        # 去除可能的 <think> 标签内容
        if "<think>" in merged:
            import re
            merged = re.sub(r"<think>.*?</think>", "", merged, flags=re.DOTALL).strip()
        return merged
